"""UnifiedVideoPipelineService — Pipeline Central Único do Codexia.

Objetivo estrutural:
- Remover lógica de render/TTS/imagens/conclusão espalhada pelos módulos.
- Forçar validação real de artefatos antes de ``awaiting_review``.
- Garantir 1 idempotency_key = 1 task = 1 MP4 = no máximo 1 upload YouTube.

Compatibilidade com o existente:
- Os fluxos manuais (História/Devocional / YouTube Story) continuam usando o mesmo
  ``VideoGenerator`` e ``AIContentGenerator``, mas passam por aqui.
- ``VideoTask`` (video_tasks) continua sendo a referência de execução (task_id).
- ``UnifiedVideo`` é a linha de auditoria/estado central que reflete o que é real.

Módulos consumidores (gradualmente):
- História/Devocional (youtube.py /generate_video)
- Séries Programadas (youtube_series_service._enqueue_episode_generation)
- Fábrica de Conteúdo / Fábrica de Vídeos Bíblicos / Shorts / Regeneração
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

from app.config import (
    UNIFIED_AUDIO_DIR,
    UNIFIED_IMAGES_DIR,
    UNIFIED_MUSIC_DIR,
    UNIFIED_VIDEO_DIR,
    UNIFIED_VIDEO_URL_PREFIX,
    absolute_path_for_audio,
    absolute_path_for_image,
    absolute_path_for_video,
)
from app.database import SessionLocal
from app.models import UnifiedVideo, UnifiedVideoStatus, User
from app.services.task_manager import (
    acquire_distributed_lock,
    acquire_task_execution_lease,
    claim_video_task,
    finalize_task_once,
    get_task,
    get_task_by_idempotency_key,
    heartbeat_task_execution_lease,
    release_distributed_lock,
    release_task_execution_lease,
    update_task,
)

_CANONICAL_IGNORE_KEYS = {
    "task_id",
    "force_regenerate",
    "force_render_only",
    "force_reuse_assets",
    "seeded_script",
    "selected_images",
    "reuse_audio_from",
    "callback_url",
}


# ---------------------------------------------------------------------------
# Contrato Único de Entrada
# ---------------------------------------------------------------------------


class UnifiedVideoRequest(BaseModel):
    """Payload padronizado. Todos os módulos devem enviar um objeto assim."""

    source_module: str = Field(
        ...,
        description="Módulo de origem: story | youtube_series | ai_factory | bible_video_factory | short | manual | scheduled",
        max_length=64,
    )
    source_id: str = Field(
        ...,
        description="Id único no módulo de origem (ex.: episode:123).",
        max_length=191,
    )
    idempotency_key: str = Field(
        ...,
        description="Chave global de idempotência (deve ser única para 1 render).",
        max_length=255,
        min_length=8,
    )
    content_type: str = Field(
        "devotional",
        description="Tipo de conteúdo: devotional | story | prayer | sermon | teaching | short | custom",
        max_length=64,
    )
    topic: Optional[str] = Field(None, description="Tema/título em linguagem natural.")
    script_text: Optional[str] = Field(None, description="Texto pré-escrito. Se nulo, IA gera.")
    duration_minutes: int = Field(3, ge=1, le=180, description="Duração alvo em minutos.")
    aspect_ratio: str = Field("16:9", description="16:9 | 9:16 | 1:1 | 4:5")
    image_count: int = Field(8, ge=1, le=64, description="Quantidade alvo de imagens/cenas.")
    text_provider: str = Field("configured", max_length=64)
    image_provider: str = Field("configured", max_length=64)
    voice_provider: str = Field("configured", max_length=64)
    voice_id: Optional[str] = Field(None, max_length=128)
    music_enabled: bool = False
    visibility: str = Field("unlisted", description="private | unlisted | public", max_length=32)
    auto_publish: bool = False
    review_required: bool = True
    user_id: Optional[int] = None
    force_regenerate: bool = False
    force_reuse_assets: bool = False
    force_render_only: bool = False
    override_title: Optional[str] = None
    override_description: Optional[str] = None
    override_tags: Optional[List[str]] = None
    seeded_script: Optional[Dict[str, Any]] = None
    selected_images: Optional[List[str]] = None
    reuse_audio_from: Optional[Dict[str, Any]] = None
    request_hash: Optional[str] = Field(None, description="Hash canônico (se omitido é calculado aqui).")
    legacy_payload: Optional[Dict[str, Any]] = Field(
        None, description="Payload bruto do módulo de origem para compatibilidade (não participa do request_hash)."
    )

    @field_validator("aspect_ratio", mode="before")
    @classmethod
    def _validate_aspect(cls, v: Any) -> str:  # noqa: N805
        ok = {"16:9", "9:16", "1:1", "4:5", "4:3", "3:4"}
        val = (str(v) if v is not None else "16:9").strip().lower()
        return val if val in ok else "16:9"

    @field_validator("visibility", mode="before")
    @classmethod
    def _validate_visibility(cls, v: Any) -> str:  # noqa: N805
        ok = {"private", "unlisted", "public"}
        val = (str(v) if v is not None else "unlisted").strip().lower()
        return val if val in ok else "unlisted"

    def canonical_payload(self) -> Dict[str, Any]:
        """Somente campos estáveis para dedupe.``source_module+source_id`` + ``idempotency_key`` são primários."""
        raw = self.model_dump(mode="python")
        canon: Dict[str, Any] = {}
        for k in sorted(raw.keys()):
            if k in _CANONICAL_IGNORE_KEYS:
                continue
            if k == "legacy_payload":
                continue
            v = raw[k]
            if isinstance(v, list):
                canon[k] = [str(x) for x in v]
            elif isinstance(v, dict):
                canon[k] = _sort_jsonable(v)
            elif v is None:
                canon[k] = None
            else:
                canon[k] = str(v)
        return canon

    def request_hash_hex(self) -> str:
        js = json.dumps(self.canonical_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(js.encode("utf-8")).hexdigest()


class UnifiedValidationResult(BaseModel):
    ok: bool = False
    checks: Dict[str, bool] = {}
    first_failed: Optional[str] = None
    details: Dict[str, Any] = {}


class UnifiedPipelineResult(BaseModel):
    unified_video_id: Optional[int] = None
    task_id: Optional[str] = None
    idempotency_key: str
    status: str
    message: str
    created_new: bool = False
    reused_existing: bool = False
    reused_completed: bool = False
    queue_position: int = 0
    already_processing: bool = False
    validation: Optional[UnifiedValidationResult] = None
    errors: List[str] = []
    # Dados que já existiam se a tarefa for reaproveitada.
    video_url: Optional[str] = None
    youtube_video_id: Optional[str] = None
    providers: Dict[str, Any] = {}


def build_unified_video_request(
    payload: Dict[str, Any],
    *,
    source_module: str,
    source_id: Optional[str] = None,
    user_id: Optional[int] = None,
) -> UnifiedVideoRequest:
    """Converte entradas de qualquer módulo para o contrato canônico.

    A conversão fica no serviço central para impedir que routers, schedulers e
    fábricas mantenham interpretações diferentes do payload do
    História/Devocional. O ``legacy_payload`` existe somente para preservar
    opções de renderização do adaptador; identidade, fila, estado e validação
    continuam sob responsabilidade do ``UnifiedVideoPipelineService``.
    """
    raw = dict(payload or {})
    module = str(source_module or raw.get("source_module") or "system").strip().lower()
    module = re.sub(r"[^a-z0-9_.:-]+", "_", module).strip("_.:-")[:64] or "system"

    digest_payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
    normalized_source_id = str(source_id or raw.get("source_id") or f"request:{digest[:24]}").strip()[:191]
    if not normalized_source_id:
        normalized_source_id = f"request:{digest[:24]}"
    idempotency_key = str(raw.get("idempotency_key") or "").strip()[:255]
    if len(idempotency_key) < 8:
        idempotency_key = f"uvp:{module}:{normalized_source_id}:{digest[:16]}"[:255]

    kind = str(raw.get("kind") or raw.get("content_type") or "").strip().lower()
    allowed_kinds = {"story", "devotional", "prayer", "sermon", "teaching", "short", "custom"}
    if kind not in allowed_kinds:
        mode = str(raw.get("mode") or "topic").strip().lower()
        kind = "story" if mode == "story" else ("short" if str(raw.get("video_type") or "").lower() == "short" else "custom")

    try:
        duration = int(raw.get("duration") or raw.get("duration_minutes") or 5)
    except Exception:
        duration = 5
    duration = max(1, min(180, duration))
    try:
        image_count = int(raw.get("image_count") or (8 if str(raw.get("image_mode") or "").lower() == "multiple" else 1))
    except Exception:
        image_count = 8
    image_count = max(1, min(64, image_count))

    tags = raw.get("override_tags")
    override_tags = [str(item) for item in tags if item is not None] if isinstance(tags, list) else None
    selected = raw.get("selected_images")
    selected_images = [str(item).strip() for item in selected if str(item).strip()] if isinstance(selected, list) else None

    resolved_user_id: Optional[int]
    try:
        resolved_user_id = int(user_id or raw.get("user_id") or 0) or None
    except Exception:
        resolved_user_id = None

    return UnifiedVideoRequest(
        source_module=module,
        source_id=normalized_source_id,
        idempotency_key=idempotency_key,
        content_type=kind,
        topic=str(raw.get("topic") or raw.get("title") or raw.get("theme") or "")[:4000] or None,
        script_text=str(raw.get("story_content") or raw.get("script_text") or "")[:120000] or None,
        duration_minutes=duration,
        aspect_ratio=str(raw.get("aspect_ratio") or "16:9").strip()[:12],
        image_count=image_count,
        text_provider=str(raw.get("text_provider") or "configured").strip()[:64] or "configured",
        image_provider=str(raw.get("image_provider") or "configured").strip()[:64] or "configured",
        voice_provider=str(raw.get("voice_provider") or "configured").strip()[:64] or "configured",
        voice_id=(str(raw.get("voice_id") or raw.get("voice_style") or "").strip()[:128] or None),
        music_enabled=bool(raw.get("music_enabled") or raw.get("music_file_path")),
        visibility=str(raw.get("visibility") or "unlisted").strip().lower()[:32] or "unlisted",
        auto_publish=bool(raw.get("auto_upload") or raw.get("auto_publish") or False),
        review_required=bool(raw.get("review_required") if raw.get("review_required") is not None else True),
        user_id=resolved_user_id,
        force_regenerate=bool(raw.get("force_regenerate") or False),
        force_reuse_assets=bool(raw.get("force_reuse_assets") or False),
        force_render_only=bool(raw.get("force_render_only") or False),
        override_title=(str(raw.get("override_title") or "").strip()[:300] or None),
        override_description=(str(raw.get("override_description") or "").strip()[:5000] or None),
        override_tags=override_tags,
        seeded_script=(raw.get("seeded_script") if isinstance(raw.get("seeded_script"), dict) else None),
        selected_images=selected_images,
        reuse_audio_from=(raw.get("reuse_audio_from") if isinstance(raw.get("reuse_audio_from"), dict) else None),
        request_hash=(str(raw.get("request_hash") or "").strip() or None),
        legacy_payload={
            key: value
            for key, value in raw.items()
            if key not in {"idempotency_key", "request_hash", "seeded_script", "selected_images", "reuse_audio_from"}
        },
    )
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sort_jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _sort_jsonable(v[k]) for k in sorted(v.keys(), key=str)}
    if isinstance(v, list):
        return [_sort_jsonable(x) for x in v]
    return str(v) if not isinstance(v, (int, float, bool, type(None))) else v


def _json_dumps(data: Any) -> Optional[str]:
    if data is None:
        return None
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        try:
            return json.dumps({"raw": str(data)}, ensure_ascii=False)
        except Exception:
            return None


def _json_loads(txt: Any) -> Any:
    if txt is None:
        return None
    if isinstance(txt, (dict, list)):
        return txt
    try:
        return json.loads(txt)
    except Exception:
        return None


def _safe_bool(v: Any) -> bool:
    try:
        return bool(v)
    except Exception:
        return False


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v or default)
    except Exception:
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v or default)
    except Exception:
        return default


def _file_size_bytes(path: Optional[str]) -> int:
    try:
        if not path:
            return 0
        if os.path.isfile(path):
            return int(os.path.getsize(path))
    except Exception:
        return 0
    return 0


def _ffprobe_streams(path: str) -> Dict[str, Any]:
    try:
        import subprocess  # noqa: S404
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=index,codec_type,duration,width,height",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if r.returncode != 0:
            return {}
        try:
            data = json.loads(r.stdout or "{}")
        except Exception:
            return {}
        streams = data.get("streams") if isinstance(data, dict) else None
        if not isinstance(streams, list):
            return {}
        has_video = any(str(s.get("codec_type") or "").lower() == "video" for s in streams if isinstance(s, dict))
        has_audio = any(str(s.get("codec_type") or "").lower() == "audio" for s in streams if isinstance(s, dict))
        video_duration = 0.0
        for s in streams:
            if not isinstance(s, dict):
                continue
            if str(s.get("codec_type") or "").lower() == "video":
                video_duration = max(video_duration, _safe_float(s.get("duration"), 0.0))
        return {"has_video": has_video, "has_audio": has_audio, "video_duration": video_duration, "raw": streams}
    except Exception:
        return {}


def _http_head_ok_for_media(url: str, timeout_seconds: int = 12) -> bool:
    """Valida se URL está servindo 200/206 (range-friendly). Evita dependência extra.

    Roda apenas quando o backend está expondo via static/media (na inicialização da UI o browser testa de verdade).
    """
    if not url:
        return False
    try:
        import requests  # já está em requirements (vídeos/YT)
        final_url = url
        base_url = str(os.getenv("BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
        if final_url.startswith("/"):
            final_url = base_url + final_url
        r = requests.get(
            final_url,
            headers={"Range": "bytes=0-0"},
            timeout=timeout_seconds,
            allow_redirects=True,
            stream=True,
        )
        try:
            r.close()
        except Exception:
            pass
        return bool(r.status_code in {200, 206})
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Serviço central
# ---------------------------------------------------------------------------


class UnifiedVideoPipelineService:
    """Pipeline único de geração de vídeo.

    Princípios:
    1. TODO pedido entra por ``submit_or_reuse`` → retorna ``UnifiedPipelineResult`` com task_id.
    2. A execução real continua sendo realizada pelo executor legado de ``process_video_generation`` (História/Devocional)
       via VideoTask (assíncrona); porém esse executor **deve** atualizar ``UnifiedVideo`` a cada etapa.
       No futuro, o executor migrará integralmente para cá.
    3. Antes de permitir ``awaiting_review``, o método ``validate_before_awaiting_review`` valida os 10 itens reais.
    4. ``publish_if_ready`` garante upload único.
    """

    _instance: Optional["UnifiedVideoPipelineService"] = None
    _init_lock = threading.Lock()

    def __init__(self) -> None:
        try:
            os.makedirs(UNIFIED_VIDEO_DIR, exist_ok=True)
            os.makedirs(UNIFIED_AUDIO_DIR, exist_ok=True)
            os.makedirs(UNIFIED_IMAGES_DIR, exist_ok=True)
            os.makedirs(UNIFIED_MUSIC_DIR, exist_ok=True)
        except Exception:
            pass

    # Singleton para garantir que todos os módulos usem a mesma instância.
    @classmethod
    def get(cls) -> "UnifiedVideoPipelineService":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = UnifiedVideoPipelineService()
        return cls._instance

    # ------------------------------------------------------------------
    # Garantias de schema (SQLite dev)
    # ------------------------------------------------------------------
    def ensure_schema(self, db) -> None:
        try:
            from sqlalchemy import inspect, text

            insp = inspect(db.bind)
            tables = set(insp.get_table_names())
            if "unified_videos" not in tables:
                dialect = str(getattr(getattr(db.bind, "dialect", None), "name", "") or "").lower()
                if dialect == "sqlite":
                    db.execute(
                        text(
                            """
                            CREATE TABLE IF NOT EXISTS unified_videos (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                idempotency_key VARCHAR(255) NOT NULL UNIQUE,
                                task_id VARCHAR(64) NULL,
                                source_module VARCHAR(64) NOT NULL,
                                source_id VARCHAR(191) NOT NULL,
                                user_id INTEGER NULL,
                                content_type VARCHAR(64) NOT NULL DEFAULT 'devotional',
                                topic TEXT NULL,
                                script_text TEXT NULL,
                                duration_minutes INTEGER NOT NULL DEFAULT 3,
                                aspect_ratio VARCHAR(12) NOT NULL DEFAULT '16:9',
                                image_count INTEGER NOT NULL DEFAULT 8,
                                visibility VARCHAR(32) NOT NULL DEFAULT 'unlisted',
                                review_required TINYINT NOT NULL DEFAULT 1,
                                auto_publish TINYINT NOT NULL DEFAULT 0,
                                force_regenerate TINYINT NOT NULL DEFAULT 0,
                                force_reuse_assets TINYINT NOT NULL DEFAULT 0,
                                force_render_only TINYINT NOT NULL DEFAULT 0,
                                text_provider VARCHAR(64) NULL,
                                text_model VARCHAR(64) NULL,
                                image_provider VARCHAR(64) NULL,
                                image_model VARCHAR(64) NULL,
                                voice_provider VARCHAR(64) NULL,
                                voice_model VARCHAR(64) NULL,
                                call_count_text INTEGER NOT NULL DEFAULT 0,
                                call_count_image INTEGER NOT NULL DEFAULT 0,
                                call_count_audio INTEGER NOT NULL DEFAULT 0,
                                estimated_cost REAL NOT NULL DEFAULT 0.0,
                                actual_cost REAL NOT NULL DEFAULT 0.0,
                                status VARCHAR(32) NOT NULL DEFAULT 'queued',
                                current_step VARCHAR(32) NULL,
                                progress INTEGER NOT NULL DEFAULT 0,
                                last_message TEXT NULL,
                                last_error TEXT NULL,
                                script_json TEXT NULL,
                                storyboard_json TEXT NULL,
                                audio_path TEXT NULL,
                                audio_size_bytes INTEGER NULL,
                                audio_duration_seconds REAL NULL,
                                images_json TEXT NULL,
                                video_path TEXT NULL,
                                video_size_bytes INTEGER NULL,
                                video_duration_seconds REAL NULL,
                                video_url TEXT NULL,
                                cover_path TEXT NULL,
                                cover_url TEXT NULL,
                                result_json TEXT NULL,
                                youtube_video_id VARCHAR(64) NULL,
                                youtube_url TEXT NULL,
                                published_at DATETIME NULL,
                                approved_at DATETIME NULL,
                                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            )
                            """
                        )
                    )
                    db.commit()
        except Exception:
            # Em produção o Alembic cria a tabela; deixamos passar para não quebrar startup.
            pass

    # ------------------------------------------------------------------
    # Entrada 1: submit ou reaproveita (idempotência).
    # ------------------------------------------------------------------
    def submit_or_reuse(
        self,
        db,
        *,
        request: UnifiedVideoRequest,
        kick_queue_callback: Optional[Callable[[], None]] = None,
        legacy_initial_result: Optional[Dict[str, Any]] = None,
        user: Optional[User] = None,
    ) -> UnifiedPipelineResult:
        """Serializa claim e UnifiedVideo para impedir corrida entre cliques."""
        key = str(request.idempotency_key or "").strip()
        lock_info = acquire_distributed_lock(
            f"unified-submit:{key}",
            timeout_seconds=20,
            ttl_seconds=45,
        )
        try:
            return self._submit_or_reuse_locked(
                db,
                request=request,
                kick_queue_callback=kick_queue_callback,
                legacy_initial_result=legacy_initial_result,
                user=user,
            )
        finally:
            release_distributed_lock(lock_info)

    def _submit_or_reuse_locked(
        self,
        db,
        *,
        request: UnifiedVideoRequest,
        kick_queue_callback: Optional[Callable[[], None]] = None,
        legacy_initial_result: Optional[Dict[str, Any]] = None,
        user: Optional[User] = None,
    ) -> UnifiedPipelineResult:
        self.ensure_schema(db)

        if not request.request_hash:
            request.request_hash = request.request_hash_hex()

        user_id = int(request.user_id or (getattr(user, "id", None) or 0) or 0) or None
        initial_result = dict(legacy_initial_result or {}) or None

        # 1. Reclama task idempotente via task_manager (garante 1 executor em processamento).
        claimed = claim_video_task(
            idempotency_key=str(request.idempotency_key).strip(),
            request_hash=request.request_hash,
            payload=_jsonable_payload_for_legacy(request),
            dedupe_window_seconds=6 * 60 * 60,
            force_regenerate=bool(request.force_regenerate),
            user_id=user_id,
            initial_result=initial_result,
        )
        task_id = str(claimed.get("task_id"))
        created_new = bool(claimed.get("created_new_task"))
        reused_existing = bool(claimed.get("reused_existing_task"))
        reused_completed = bool(claimed.get("reused_completed_task"))

        # 2. Cria/atualiza a linha central UnifiedVideo.
        uv: Optional[UnifiedVideo] = (
            db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == str(request.idempotency_key).strip()).first()
        )
        uv_created_now = False
        if uv is None:
            uv = UnifiedVideo(
                idempotency_key=str(request.idempotency_key).strip(),
                task_id=task_id,
                source_module=str(request.source_module)[:64],
                source_id=str(request.source_id)[:191],
                user_id=user_id,
                content_type=str(request.content_type)[:64] or "devotional",
                topic=request.topic,
                script_text=request.script_text,
                duration_minutes=int(request.duration_minutes),
                aspect_ratio=str(request.aspect_ratio)[:12],
                image_count=int(request.image_count),
                visibility=str(request.visibility)[:32] or "unlisted",
                review_required=bool(request.review_required),
                auto_publish=bool(request.auto_publish),
                force_regenerate=bool(request.force_regenerate),
                force_reuse_assets=bool(request.force_reuse_assets),
                force_render_only=bool(request.force_render_only),
                text_provider=str(request.text_provider)[:64] or "configured",
                image_provider=str(request.image_provider)[:64] or "configured",
                voice_provider=str(request.voice_provider)[:64] or "configured",
                voice_model=request.voice_id,
                result_json=_json_dumps({"request": request.model_dump(mode="python")}),
            )
            db.add(uv)
            uv_created_now = True
        else:
            # Atualiza só campos administrativos — NUNCA sobrescreve artefatos já produzidos.
            uv.task_id = task_id
            uv.source_module = str(request.source_module)[:64] or uv.source_module
            uv.source_id = str(request.source_id)[:191] or uv.source_id
            uv.user_id = user_id or uv.user_id
            uv.content_type = str(request.content_type)[:64] or uv.content_type
            uv.duration_minutes = int(request.duration_minutes)
            uv.aspect_ratio = str(request.aspect_ratio)[:12]
            uv.image_count = int(request.image_count)
            uv.visibility = str(request.visibility)[:32] or uv.visibility
            uv.review_required = bool(request.review_required)
            uv.auto_publish = bool(request.auto_publish)
            uv.force_regenerate = bool(request.force_regenerate)
            uv.force_reuse_assets = bool(request.force_reuse_assets)
            uv.force_render_only = bool(request.force_render_only)
            uv.text_provider = str(request.text_provider)[:64] or uv.text_provider
            uv.image_provider = str(request.image_provider)[:64] or uv.image_provider
            uv.voice_provider = str(request.voice_provider)[:64] or uv.voice_provider
            if request.voice_id:
                uv.voice_model = request.voice_id
            # Uma task nova após descarte/falha deve reabrir a linha canônica.
            # Preservamos artefatos e custos para auditoria, mas não carregamos
            # o estado terminal da tentativa antiga para a nova execução.
            if created_new and str(uv.status or "").strip().lower() in {
                UnifiedVideoStatus.FAILED,
                UnifiedVideoStatus.CANCELLED,
            }:
                uv.status = UnifiedVideoStatus.QUEUED
                uv.current_step = None
                uv.progress = 0
                uv.last_message = "Nova geração criada após descarte da tentativa anterior."
                uv.last_error = None
        try:
            db.flush()
        except Exception:
            db.rollback()
            raise

        # 3. Posição na fila para a UI informar ao usuário.
        queue_position, already_processing = self._queue_position(db, task_id)

        # 4. Kick (se for criado nova task). O callback é o kick legado do router.
        if created_new and kick_queue_callback is not None:
            try:
                kick_queue_callback()
            except Exception as exc:  # pragma: no cover - erro no kick não invalida o submit
                errors = [f"Falha ao iniciar queue kick: {type(exc).__name__}: {str(exc)[:200]}"]
                self._mark_failed(db, uv, message=errors[0])
                try:
                    db.commit()
                except Exception:
                    db.rollback()
                return UnifiedPipelineResult(
                    unified_video_id=getattr(uv, "id", None),
                    task_id=task_id,
                    idempotency_key=str(request.idempotency_key),
                    status=str(getattr(uv, "status", "failed") or "failed"),
                    message=errors[0],
                    errors=errors,
                )

        try:
            db.commit()
        except Exception:
            db.rollback()
            raise

        # 5. Popula resultado reaproveitado (já tem vídeo / upload) para o cliente imediatamente.
        t = get_task(task_id) or {}
        task_result = t.get("result") if isinstance(t.get("result"), dict) else {}
        video_url = None
        youtube_video_id = None
        if reused_completed:
            video_url = uv.video_url if uv and getattr(uv, "video_url", None) else task_result.get("video_url")
            youtube_video_id = (
                uv.youtube_video_id
                if uv and getattr(uv, "youtube_video_id", None)
                else task_result.get("youtube_video_id")
            )
        else:
            video_url = uv.video_url if uv and getattr(uv, "video_url", None) else None
            youtube_video_id = uv.youtube_video_id if uv and getattr(uv, "youtube_video_id", None) else None

        return UnifiedPipelineResult(
            unified_video_id=getattr(uv, "id", None),
            task_id=task_id,
            idempotency_key=str(request.idempotency_key),
            status=str(getattr(uv, "status", "queued") or "queued"),
            message=(
                "Esta geração já está em andamento."
                if reused_existing and not reused_completed
                else ("Resultado reaproveitado dentro da janela de idempotência." if reused_completed else "Vídeo enviado para a fila de produção (UnifiedVideoPipeline).")
            ),
            created_new=created_new or uv_created_now,
            reused_existing=reused_existing,
            reused_completed=reused_completed,
            queue_position=int(queue_position or 0),
            already_processing=bool(already_processing),
            video_url=video_url,
            youtube_video_id=youtube_video_id,
            providers={
                "text": {"provider": getattr(uv, "text_provider", None), "model": getattr(uv, "text_model", None)},
                "image": {"provider": getattr(uv, "image_provider", None), "model": getattr(uv, "image_model", None)},
                "voice": {"provider": getattr(uv, "voice_provider", None), "model": getattr(uv, "voice_model", None)},
                "cost": {
                    "estimated": _safe_float(getattr(uv, "estimated_cost", 0.0), 0.0),
                    "actual": _safe_float(getattr(uv, "actual_cost", 0.0), 0.0),
                    "call_count_text": _safe_int(getattr(uv, "call_count_text", 0), 0),
                    "call_count_image": _safe_int(getattr(uv, "call_count_image", 0), 0),
                    "call_count_audio": _safe_int(getattr(uv, "call_count_audio", 0), 0),
                },
            },
        )

    # ------------------------------------------------------------------
    # Atualizações de etapa — chamadas pelo executor legado no futuro.
    # ------------------------------------------------------------------
    def transition_status(
        self,
        db,
        idempotency_key_or_task_id: str,
        *,
        status: str,
        step: Optional[str] = None,
        progress: Optional[int] = None,
        message: Optional[str] = None,
        merge_result: Optional[Dict[str, Any]] = None,
    ) -> Optional[UnifiedVideo]:
        uv = self._find_any(db, idempotency_key_or_task_id)
        if uv is None:
            return None
        # Só permite transições para estados oficiais.
        if status in UnifiedVideoStatus.ALL:
            uv.status = status
        else:
            # Mapeamentos legados (mantemos funcionando enquanto migramos)
            norm = str(status).strip().lower()
            if norm in {"pending", "processing"}:
                uv.status = UnifiedVideoStatus.QUEUED if norm == "pending" else UnifiedVideoStatus.PROCESSING_SCRIPT
            elif norm in {"completed", "ready"}:
                uv.status = UnifiedVideoStatus.AWAITING_REVIEW if bool(uv.review_required) else UnifiedVideoStatus.APPROVED
            elif norm in {"rendered_upload_failed"}:
                uv.status = UnifiedVideoStatus.AWAITING_REVIEW if bool(uv.review_required) else UnifiedVideoStatus.FAILED
            elif norm == "failed":
                uv.status = UnifiedVideoStatus.FAILED
            elif norm == "cancelled":
                uv.status = UnifiedVideoStatus.CANCELLED
            elif norm == "published":
                uv.status = UnifiedVideoStatus.PUBLISHED
            else:
                raise ValueError(f"[UnifiedVideoPipeline] status inválido: {status}")
        if step:
            uv.current_step = str(step)[:32]
        if progress is not None:
            try:
                uv.progress = max(0, min(100, int(progress)))
            except Exception:
                pass
        if message:
            uv.last_message = str(message)[:1000]
        if isinstance(merge_result, dict):
            self._merge_result_artifacts(db, uv, merge_result)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        return uv

    def mark_step_with_progress(
        self,
        db,
        idempotency_key_or_task_id: str,
        *,
        step_status: str,
        progress_pct: int,
        message: str,
        extra_result: Optional[Dict[str, Any]] = None,
    ) -> Optional[UnifiedVideo]:
        """Atalho para etapas internas do pipeline."""
        return self.transition_status(
            db,
            idempotency_key_or_task_id,
            status=step_status,
            step=step_status,
            progress=progress_pct,
            message=message,
            merge_result=extra_result,
        )

    # ------------------------------------------------------------------
    # Validação de artefatos reais — NÃO permite awaiting_review sem isso.
    # ------------------------------------------------------------------
    def validate_before_awaiting_review(
        self,
        db,
        idempotency_key_or_task_id: str,
        *,
        probe_local_paths: bool = True,
        probe_http: bool = False,
    ) -> UnifiedValidationResult:
        uv = self._find_any(db, idempotency_key_or_task_id)
        checks: Dict[str, bool] = {}
        details: Dict[str, Any] = {}
        if uv is None:
            return UnifiedValidationResult(
                ok=False,
                checks={"unified_video_found": False},
                first_failed="unified_video_found",
                details={"reason": "UnifiedVideo não encontrado para task_id / idempotency_key"},
            )
        task = get_task(str(uv.task_id)) if uv.task_id else None
        task_result = task.get("result") if isinstance(task, dict) and isinstance(task.get("result"), dict) else {}
        script_obj = _json_loads(uv.script_json) if isinstance(uv.script_json, str) else None
        if not isinstance(script_obj, dict):
            script_obj = task_result.get("script") if isinstance(task_result.get("script"), dict) else None
        storyboard_obj = _json_loads(uv.storyboard_json) if isinstance(uv.storyboard_json, str) else None
        if not isinstance(storyboard_obj, dict):
            render_report = task_result.get("render_report") if isinstance(task_result.get("render_report"), dict) else {}
            storyboard_obj = {
                "scenes": (script_obj.get("scenes") if isinstance(script_obj, dict) else None)
                or (render_report.get("scene_visuals") if isinstance(render_report, dict) else None)
            }
        images_paths: List[str] = self._resolve_image_paths(uv, task_result, storyboard_obj)
        audio_candidate = uv.audio_path or self._task_audio_path(task_result)
        video_candidate = uv.video_path or self._task_video_path(task_result)
        abs_audio = absolute_path_for_audio(audio_candidate) if probe_local_paths else audio_candidate
        abs_video = absolute_path_for_video(video_candidate) if probe_local_paths else video_candidate

        # 1. roteiro válido
        script_ok = isinstance(script_obj, dict) and bool(
            str(script_obj.get("title") or script_obj.get("text") or script_obj.get("narration") or "").strip()
        )
        checks["script_valid"] = bool(script_ok)
        details["script"] = {
            "has_script_obj": isinstance(script_obj, dict),
            "title": str((script_obj or {}).get("title") or "")[:120],
        }

        # 2. cenas válidas
        scenes = storyboard_obj.get("scenes") if isinstance(storyboard_obj, dict) else None
        if not isinstance(scenes, list):
            scenes = script_obj.get("scenes") if isinstance(script_obj, dict) else None
        scenes_ok = isinstance(scenes, list) and len(scenes) >= 1
        checks["storyboard_valid"] = bool(scenes_ok)
        details["storyboard"] = {"scene_count": len(scenes) if isinstance(scenes, list) else 0}

        # 3. quantidade mínima de imagens
        min_images = max(1, min(int(uv.image_count or 1), 256))
        actual_images = sum(1 for p in images_paths if self._file_exists_or_url(p, probe_local_paths))
        checks["image_count_minimum"] = bool(actual_images >= min_images)
        details["images"] = {
            "expected_min": min_images,
            "actual_found": actual_images,
            "paths": images_paths[: min(24, len(images_paths))],
        }

        # 4. arquivos de imagem existentes
        checks["image_files_exist"] = bool(actual_images >= 1)
        if checks["image_count_minimum"] and not checks["image_files_exist"]:
            checks["image_files_exist"] = False

        # 5. áudio existente e > 0 bytes
        audio_exists = bool(self._file_exists_or_url(abs_audio, probe_local_paths))
        audio_size = _file_size_bytes(abs_audio) if probe_local_paths else (1 if audio_exists else 0)
        checks["audio_exists_and_non_empty"] = bool(audio_exists and audio_size > 0)
        details["audio"] = {
            "path": audio_candidate,
            "abs_path": abs_audio,
            "size_bytes": int(audio_size),
        }

        # 6-10. MP4 + tamanho + ffprobe streams + duração + URL HTTP 200/206
        video_exists = bool(self._file_exists_or_url(abs_video, probe_local_paths))
        video_size = _file_size_bytes(abs_video) if probe_local_paths else (1 if video_exists else 0)
        ffprobe = _ffprobe_streams(abs_video) if probe_local_paths and video_exists else {}
        has_video_stream = bool(ffprobe.get("has_video"))
        has_audio_stream = bool(ffprobe.get("has_audio"))
        video_duration = _safe_float(ffprobe.get("video_duration"), 0.0)
        checks["mp4_exists"] = bool(video_exists)
        checks["mp4_larger_than_100kb"] = bool(video_size > 100 * 1024)
        checks["ffprobe_has_video_stream"] = bool(has_video_stream)
        checks["ffprobe_has_audio_stream"] = bool(has_audio_stream)
        checks["duration_valid"] = bool(video_duration >= 1.0)
        details["mp4"] = {
            "path": video_candidate,
            "abs_path": abs_video,
            "size_bytes": int(video_size),
            "streams": {"video": bool(has_video_stream), "audio": bool(has_audio_stream)},
            "duration_seconds": float(video_duration),
        }
        # Sincroniza tamanhos/durações do banco para auditabilidade.
        if probe_local_paths:
            uv.audio_size_bytes = int(audio_size) if audio_size else None
            if isinstance(uv.audio_duration_seconds, float) and uv.audio_duration_seconds <= 0:
                uv.audio_duration_seconds = None
            uv.video_size_bytes = int(video_size) if video_size else None
            if video_duration > 0:
                uv.video_duration_seconds = float(video_duration)
            if not uv.video_url and video_candidate:
                # Se path começa por /data/media/videos e não tem URL, gera a prefixada.
                if str(video_candidate).startswith("/data/media/videos/"):
                    uv.video_url = UNIFIED_VIDEO_URL_PREFIX + "/" + str(video_candidate).split("/data/media/videos/", 1)[1]
                elif UNIFIED_VIDEO_DIR and str(abs_video).startswith(str(UNIFIED_VIDEO_DIR)):
                    tail = str(abs_video)[len(str(UNIFIED_VIDEO_DIR)):].lstrip("\\/")
                    uv.video_url = f"{UNIFIED_VIDEO_URL_PREFIX}/{tail}" if tail else None
                else:
                    uv.video_url = str(video_candidate)
            # URL final serve HEAD para testar.
            url_to_check = uv.video_url or task_result.get("video_url")
            url_ok = True
            if probe_http:
                url_ok = _http_head_ok_for_media(url_to_check or "")
            checks["http_200_or_206"] = bool(url_ok)
            details["mp4"]["url"] = url_to_check
            details["mp4"]["http_ok"] = bool(url_ok)
        else:
            checks["http_200_or_206"] = True  # skip de propósito (testes / sem network)

        # Apura primeiro falho
        first_failed = next((k for k in [
            "script_valid",
            "storyboard_valid",
            "image_count_minimum",
            "image_files_exist",
            "audio_exists_and_non_empty",
            "mp4_exists",
            "mp4_larger_than_100kb",
            "ffprobe_has_video_stream",
            "ffprobe_has_audio_stream",
            "duration_valid",
            "http_200_or_206",
        ] if not checks.get(k)), None)
        ok = first_failed is None and all(checks.values())

        try:
            db.commit()
        except Exception:
            db.rollback()
        return UnifiedValidationResult(
            ok=bool(ok),
            checks=checks,
            first_failed=first_failed,
            details=details,
        )

    def transition_to_awaiting_review_if_valid(
        self,
        db,
        idempotency_key_or_task_id: str,
        *,
        probe_local_paths: bool = True,
        probe_http: bool = False,
    ) -> Tuple[UnifiedValidationResult, Optional[UnifiedVideo]]:
        validation = self.validate_before_awaiting_review(
            db,
            idempotency_key_or_task_id,
            probe_local_paths=probe_local_paths,
            probe_http=probe_http,
        )
        uv = self._find_any(db, idempotency_key_or_task_id)
        if uv is None:
            return validation, None
        if validation.ok:
            new_status = (
                UnifiedVideoStatus.AWAITING_REVIEW
                if bool(uv.review_required)
                else UnifiedVideoStatus.APPROVED
            )
            uv.status = new_status
            uv.progress = 100
            uv.last_message = (
                "Validação concluída — aguardando revisão."
                if new_status == UnifiedVideoStatus.AWAITING_REVIEW
                else "Validação concluída — aprovação automática (review_required=False)."
            )
            try:
                db.commit()
            except Exception:
                db.rollback()
        else:
            uv.status = UnifiedVideoStatus.FAILED
            uv.last_error = f"Validação falhou em: {validation.first_failed or 'unknown'}"
            try:
                db.commit()
            except Exception:
                db.rollback()
        return validation, uv

    # ------------------------------------------------------------------
    # Publicação única (proibe 2 uploads YouTube para mesmo idempotency_key)
    # ------------------------------------------------------------------
    def publish_if_ready(
        self,
        db,
        idempotency_key_or_task_id: str,
        *,
        upload_callable: Callable[[str, Dict[str, Any]], Dict[str, Any]],
        upload_metadata: Optional[Dict[str, Any]] = None,
        visibility_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        uv = self._find_any(db, idempotency_key_or_task_id)
        if uv is None:
            return {"ok": False, "code": "not_found", "error": "UnifiedVideo não encontrado"}
        if uv.youtube_video_id and not getattr(uv, "force_regenerate", False):
            return {
                "ok": True,
                "already_uploaded": True,
                "youtube_video_id": uv.youtube_video_id,
                "youtube_url": uv.youtube_url,
                "message": "Upload único já realizado — pulando segundo upload.",
            }
        if str(uv.status) not in {UnifiedVideoStatus.APPROVED, UnifiedVideoStatus.PUBLISHED, UnifiedVideoStatus.AWAITING_REVIEW}:
            return {
                "ok": False,
                "code": "not_approved",
                "error": f"Status atual '{uv.status}' não permite publicação.",
            }
        video_candidate = uv.video_path or (
            absolute_path_for_video(uv.video_url) if uv.video_url else None
        )
        if not self._file_exists_or_url(video_candidate, local_only=True):
            return {"ok": False, "code": "mp4_missing", "error": f"MP4 não encontrado em: {video_candidate}"}
        transitioned = self.transition_status(db, str(uv.task_id or uv.idempotency_key), status=UnifiedVideoStatus.UPLOADING, message="Iniciando upload para o YouTube (único).")
        metadata = dict(upload_metadata or {})
        visibility = (visibility_override or uv.visibility or "unlisted").lower()
        metadata.setdefault("visibility", visibility)
        try:
            out = upload_callable(str(video_candidate), metadata)
        except Exception as exc:
            self.transition_status(
                db,
                str(uv.task_id or uv.idempotency_key),
                status=UnifiedVideoStatus.FAILED,
                message=f"Upload falhou: {type(exc).__name__}: {str(exc)[:300]}",
                merge_result={"publish_error": {"type": type(exc).__name__, "message": str(exc)[:300]}},
            )
            return {"ok": False, "code": "exception", "error": f"{type(exc).__name__}: {str(exc)[:300]}", "youtube_video_id": None}
        yid = str((out or {}).get("youtube_video_id") or (out or {}).get("video_id") or "").strip()
        if not yid:
            self.transition_status(
                db,
                str(uv.task_id or uv.idempotency_key),
                status=UnifiedVideoStatus.AWAITING_REVIEW if bool(uv.review_required) else UnifiedVideoStatus.APPROVED,
                message="Upload não retornou YouTube Video ID. Verifique credenciais/permissões.",
                merge_result={"publish_result": out},
            )
            return {"ok": False, "code": "no_video_id", "error": "Upload não retornou YouTube Video ID", "raw": out}
        url = f"https://www.youtube.com/watch?v={yid}"
        merge_artifacts: Dict[str, Any] = {
            "youtube_video_id": yid,
            "youtube_url": url,
            "publish_result": out,
        }
        uv.youtube_video_id = yid
        uv.youtube_url = url
        uv.published_at = datetime.utcnow()
        self.transition_status(
            db,
            str(uv.task_id or uv.idempotency_key),
            status=UnifiedVideoStatus.PUBLISHED,
            progress=100,
            message="Publicação única concluída.",
            merge_result=merge_artifacts,
        )
        return {"ok": True, "youtube_video_id": yid, "youtube_url": url, "already_uploaded": False, "raw": out}

    # ------------------------------------------------------------------
    # Internas utilitárias
    # ------------------------------------------------------------------
    def _queue_position(self, db, task_id: str) -> Tuple[int, bool]:
        try:
            rows = db.execute(
                __import__("sqlalchemy").text(
                    "SELECT t.id, t.status FROM video_tasks t WHERE t.status IN ('pending','processing') ORDER BY t.created_at ASC, t.id ASC LIMIT 200"
                )
            ).mappings().all()
            ids = [str(r["id"]) for r in rows]
            position = ids.index(str(task_id)) + 1 if str(task_id) in ids else 0
            processing = any(str(r["status"] or "").lower() == "processing" for r in rows if str(r["id"]) != str(task_id))
            return int(position or 0), bool(processing)
        except Exception:
            return 0, False

    def _find_any(self, db, idempotency_key_or_task_id: str) -> Optional[UnifiedVideo]:
        key = str(idempotency_key_or_task_id).strip()
        if not key:
            return None
        uv = db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == key).first()
        if uv is None:
            uv = db.query(UnifiedVideo).filter(UnifiedVideo.task_id == key).first()
        return uv

    def _mark_failed(self, db, uv: Optional[UnifiedVideo], *, message: str) -> None:
        if uv is None:
            return
        uv.status = UnifiedVideoStatus.FAILED
        uv.last_error = str(message)[:1000]

    def _merge_result_artifacts(self, db, uv: UnifiedVideo, result: Dict[str, Any]) -> None:
        if not isinstance(result, dict):
            return
        # script
        if isinstance(result.get("script"), dict):
            uv.script_json = _json_dumps(result["script"])
        rr = result.get("render_report") if isinstance(result.get("render_report"), dict) else None
        if isinstance(rr, dict):
            scenes = rr.get("scene_visuals")
            if isinstance(scenes, list):
                uv.storyboard_json = _json_dumps({"scenes": scenes})

        audio_gen = rr.get("audio_generation") if isinstance(rr, dict) and isinstance(rr.get("audio_generation"), dict) else None
        if not isinstance(audio_gen, dict) and isinstance(result.get("audio_generation"), dict):
            audio_gen = result.get("audio_generation")
        if isinstance(audio_gen, dict):
            audio_path = (
                audio_gen.get("final_audio_path")
                or audio_gen.get("output_path")
                or audio_gen.get("audio_path")
            )
            if audio_path and not uv.audio_path:
                uv.audio_path = str(audio_path)
            audio_duration = (
                audio_gen.get("duration_seconds")
                or audio_gen.get("final_audio_duration_sec")
                or audio_gen.get("audio_duration_sec")
            )
            if audio_duration and not uv.audio_duration_seconds:
                uv.audio_duration_seconds = _safe_float(audio_duration)
            provider = audio_gen.get("provider") or audio_gen.get("provider_used") or audio_gen.get("configured_provider")
            if provider:
                uv.voice_provider = str(provider)[:64]
            model = audio_gen.get("model") or audio_gen.get("voice_id") or audio_gen.get("voice_id_used")
            if model:
                uv.voice_model = str(model)[:128]
            uv.call_count_audio = int(audio_gen.get("call_count") or uv.call_count_audio or 0)
        # selected_images / custom_image_paths / images
        imgs = (
            result.get("selected_images")
            or result.get("custom_image_paths")
            or result.get("images")
            or (
                result.get("render_report", {}).get("scene_visuals")
                if isinstance(result.get("render_report"), dict)
                else None
            )
        )
        if isinstance(imgs, list) and imgs:
            paths: List[str] = []
            for it in imgs:
                if isinstance(it, str) and it:
                    paths.append(it)
                elif isinstance(it, dict):
                    for k in ("image_path", "image_url", "path", "url", "storage_key"):
                        if it.get(k):
                            paths.append(str(it[k]))
                            break
            if paths:
                uv.images_json = _json_dumps({"paths": paths})
        # vídeo
        for k in ("video_url", "video_path", "file_path"):
            v = result.get(k)
            if v and not uv.video_path:
                # Preferimos path absoluto para probe, mas se vier URL pode ser reconstruído depois.
                if k == "file_path" or k == "video_path":
                    uv.video_path = str(v)
                elif k == "video_url":
                    uv.video_url = str(v)
        # youtube
        if result.get("youtube_video_id") and not uv.youtube_video_id:
            uv.youtube_video_id = str(result["youtube_video_id"])[:64]
        if result.get("youtube_url") and not uv.youtube_url:
            uv.youtube_url = str(result["youtube_url"])
        if result.get("published_at") and not uv.published_at:
            try:
                uv.published_at = datetime.fromisoformat(str(result["published_at"]).replace("Z", "+00:00"))
            except Exception:
                uv.published_at = datetime.utcnow()
        # financeiro / providers
        fg = result.get("financial_guardian") if isinstance(result.get("financial_guardian"), dict) else None
        if fg:
            uv.estimated_cost = _safe_float(fg.get("estimated_cost"), uv.estimated_cost)
            uv.actual_cost = _safe_float(fg.get("actual_cost"), uv.actual_cost)
        # calls
        for k, attr in (("script", "text"), ("images", "image"), ("audio", "voice")):
            rep = result.get(f"api_report_{k}")
            if isinstance(rep, dict):
                setattr(uv, f"call_count_{attr}", _safe_int(rep.get("calls"), getattr(uv, f"call_count_{attr}", 0) or 0))
                if rep.get("provider"):
                    setattr(uv, f"{attr}_provider", str(rep.get("provider"))[:64])
                if rep.get("model"):
                    setattr(uv, f"{attr}_model", str(rep.get("model"))[:64])
        # result_json acumulado sem segredos
        prev = _json_loads(uv.result_json) or {}
        if not isinstance(prev, dict):
            prev = {}
        safe_merge = {
            k: (str(v)[:500] if isinstance(v, str) else v)
            for k, v in result.items()
            if k
            not in {
                "access_token",
                "refresh_token",
                "client_secret",
                "authorization_code",
                "openai_api_key",
            }
        }
        prev.update(safe_merge)
        uv.result_json = _json_dumps(prev)

    def _resolve_image_paths(self, uv: UnifiedVideo, task_result: Dict[str, Any], storyboard_obj: Any) -> List[str]:
        paths: List[str] = []
        # unified first
        if isinstance(_json_loads(uv.images_json), dict):
            pl = (_json_loads(uv.images_json) or {}).get("paths")
            if isinstance(pl, list):
                paths.extend([str(x) for x in pl if x])
        # task_result legacy
        for k in ("selected_images", "custom_image_paths", "images"):
            v = task_result.get(k) if isinstance(task_result, dict) else None
            if isinstance(v, list):
                for it in v:
                    if isinstance(it, str) and it:
                        paths.append(it)
        # storyboard scenes
        if isinstance(storyboard_obj, dict):
            scenes = storyboard_obj.get("scenes")
            if isinstance(scenes, list):
                for s in scenes:
                    if not isinstance(s, dict):
                        continue
                    for field in ("image_path", "image_url", "path", "url", "storage_key"):
                        if s.get(field):
                            paths.append(str(s[field]))
                            break
        out: List[str] = []
        seen = set()
        for p in paths:
            if not p:
                continue
            if p in seen:
                continue
            seen.add(p)
            out.append(p)
        return out

    def _task_audio_path(self, task_result: Dict[str, Any]) -> Optional[str]:
        if not isinstance(task_result, dict):
            return None
        rr = task_result.get("render_report") if isinstance(task_result.get("render_report"), dict) else None
        audio_gen = rr.get("audio_generation") if isinstance(rr, dict) and isinstance(rr.get("audio_generation"), dict) else None
        if not isinstance(audio_gen, dict) and isinstance(task_result.get("audio_generation"), dict):
            audio_gen = task_result.get("audio_generation")
        if isinstance(audio_gen, dict):
            audio_path = (
                audio_gen.get("final_audio_path")
                or audio_gen.get("output_path")
                or audio_gen.get("audio_path")
            )
            if audio_path:
                return str(audio_path)
        return task_result.get("audio_path") or task_result.get("audio_url")

    def _task_video_path(self, task_result: Dict[str, Any]) -> Optional[str]:
        if not isinstance(task_result, dict):
            return None
        return task_result.get("file_path") or task_result.get("video_path") or task_result.get("video_url")

    def _file_exists_or_url(self, path_or_url: Optional[str], local_only: bool = False) -> bool:
        if not path_or_url:
            return False
        s = str(path_or_url).strip()
        if not s:
            return False
        if s.startswith("http://") or s.startswith("https://"):
            return False if local_only else _http_head_ok_for_media(s)
        try:
            return bool(os.path.exists(s))
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Singleton público
# ---------------------------------------------------------------------------

unified_video_pipeline = UnifiedVideoPipelineService.get


def _jsonable_payload_for_legacy(req: UnifiedVideoRequest) -> Dict[str, Any]:
    """Monta payload compatível com o executor legado (VideoRequest + campos que ele espera)."""
    base = req.model_dump(mode="python")
    legacy = req.legacy_payload if isinstance(req.legacy_payload, dict) else {}
    merged = dict(legacy)
    merged.update({k: v for k, v in base.items() if k != "legacy_payload"})
    # Garante que VideoRequest parseie.
    merged.setdefault("topic", req.topic or "")
    merged.setdefault("duration", int(req.duration_minutes))
    merged.setdefault("kind", req.content_type)
    merged.setdefault("mode", "story" if req.content_type in {"devotional", "prayer", "story"} else "topic")
    merged.setdefault("image_mode", "multiple")
    merged.setdefault("aspect_ratio", req.aspect_ratio)
    merged.setdefault("force_regenerate", bool(req.force_regenerate))
    merged.setdefault("force_reuse_assets", bool(req.force_reuse_assets))
    merged.setdefault("force_render_only", bool(req.force_render_only))
    merged.setdefault("idempotency_key", str(req.idempotency_key))
    merged.setdefault("request_hash", str(req.request_hash or req.request_hash_hex()))
    merged.setdefault("seeded_script", req.seeded_script)
    merged.setdefault("selected_images", req.selected_images)
    merged.setdefault("reuse_audio_from", req.reuse_audio_from)
    merged.setdefault("override_title", req.override_title)
    merged.setdefault("override_description", req.override_description)
    merged.setdefault("override_tags", list(req.override_tags or []))
    merged.setdefault("auto_upload", bool(req.auto_publish))
    if req.script_text and not merged.get("story_content"):
        merged["story_content"] = req.script_text
    if req.visibility:
        merged["visibility"] = req.visibility
    return merged


__all__ = [
    "UnifiedVideoRequest",
    "build_unified_video_request",
    "UnifiedValidationResult",
    "UnifiedPipelineResult",
    "UnifiedVideoPipelineService",
    "unified_video_pipeline",
    "UnifiedVideoStatus",
]
