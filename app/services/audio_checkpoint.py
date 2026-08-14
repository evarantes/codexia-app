from __future__ import annotations

import hashlib
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, Optional, Type


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^\w\sáàâãéêíóôõúüç]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _text_sha256(value: Any) -> str:
    return hashlib.sha256(_normalize_text(value).encode("utf-8")).hexdigest()


def _file_sha256(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _file_metadata(path: str) -> Dict[str, Any]:
    if not path or not os.path.isfile(path):
        return {"exists": False, "path": path or None, "size_bytes": 0, "sha256": None}
    try:
        size = int(os.path.getsize(path))
    except Exception:
        size = 0
    return {
        "exists": bool(size > 0),
        "path": path,
        "size_bytes": size,
        "sha256": _file_sha256(path),
    }


def _task_id_from_generator(generator: Any) -> Optional[str]:
    ai_service = getattr(generator, "ai_service", None)
    task_id = getattr(ai_service, "ai_task_id", None) if ai_service is not None else None
    return str(task_id).strip() if task_id else None


def _safe_attempts(debug: Dict[str, Any]) -> list[Dict[str, Any]]:
    out: list[Dict[str, Any]] = []
    for attempt in list(debug.get("attempts") or [])[:12]:
        if not isinstance(attempt, dict):
            continue
        item = {
            "provider": str(attempt.get("provider") or "unknown")[:64],
            "status": str(attempt.get("status") or "unknown")[:64],
        }
        reason = str(attempt.get("reason") or "").strip()
        if reason:
            item["reason"] = reason[:500]
        out.append(item)
    return out


def _fallback_reason(configured: str, used: str, attempts: list[Dict[str, Any]]) -> Optional[str]:
    configured = str(configured or "").strip().lower()
    used = str(used or "").strip().lower()
    if not used or not configured or used == configured:
        return None
    failures = []
    for attempt in attempts:
        status = str(attempt.get("status") or "").lower()
        provider = str(attempt.get("provider") or "unknown")
        reason = str(attempt.get("reason") or "").strip()
        if status not in {"ok", "success", "completed"}:
            failures.append(f"{provider}: {reason or status or 'falhou'}")
    if failures:
        return "; ".join(failures)[:1000]
    return f"Provider configurado {configured} não foi o provider efetivamente usado ({used})."


def _plan_narration_text(plan: Any) -> str:
    if not isinstance(plan, dict):
        return ""
    parts = []
    for scene in plan.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        text = (
            scene.get("_tts_text")
            or scene.get("text")
            or scene.get("narration_text")
            or scene.get("narration")
            or ""
        )
        if str(text or "").strip():
            parts.append(str(text).strip())
    for key in ("closing_message", "cta_text", "end_message"):
        value = str(plan.get(key) or "").strip()
        if value:
            parts.append(value)
    return " ".join(parts).strip()


def _text_compatibility(a: Any, b: Any) -> float:
    aa = _normalize_text(a)
    bb = _normalize_text(b)
    if not aa or not bb:
        return 0.0
    if aa == bb or aa in bb or bb in aa:
        return 1.0
    return float(SequenceMatcher(None, aa, bb).ratio())


def _checkpoint_from_task(task_id: str) -> Dict[str, Any]:
    try:
        from app.services.task_manager import get_task

        task = get_task(task_id) or {}
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        checkpoint = result.get("audio_checkpoint") if isinstance(result.get("audio_checkpoint"), dict) else None
        if checkpoint:
            return dict(checkpoint)
        rr = result.get("render_report") if isinstance(result.get("render_report"), dict) else {}
        audio = rr.get("audio_generation") if isinstance(rr.get("audio_generation"), dict) else {}
        return dict(audio or {})
    except Exception:
        return {}


def _persist_checkpoint(
    generator: Any,
    checkpoint: Dict[str, Any],
    *,
    failed: bool = False,
    failure_message: Optional[str] = None,
) -> bool:
    task_id = _task_id_from_generator(generator)
    if not task_id or not isinstance(checkpoint, dict):
        return False

    try:
        from app.services.task_manager import get_task, merge_task_result

        task = get_task(task_id) or {}
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        rr = deepcopy(result.get("render_report")) if isinstance(result.get("render_report"), dict) else {}
        rr["audio_generation"] = deepcopy(checkpoint)
        merged = merge_task_result(
            task_id,
            {
                "audio_checkpoint": deepcopy(checkpoint),
                "audio_generation": deepcopy(checkpoint),
                "render_report": rr,
                "pipeline_stage": "audio_validation_failed" if failed else "audio_checkpoint",
            },
        )
        if merged is None:
            return False
        task = merged if isinstance(merged, dict) else task
        progress = max(23, int(task.get("progress") or 0))
    except Exception:
        return False

    # Espelha imediatamente no registro canônico. Assim uma falha posterior
    # (crítica de áudio, imagens ou render) não deixa o MP3 órfão do task_id.
    try:
        from app.database import SessionLocal
        from app.services.unified_video_pipeline import unified_video_pipeline

        db = SessionLocal()
        try:
            service = unified_video_pipeline()
            uv = service.transition_status(
                db,
                task_id,
                status="failed" if failed else "processing",
                step="audio_failed" if failed else "audio_checkpoint",
                progress=progress,
                message=(
                    (failure_message or "Falha após a geração do áudio; artefato preservado para diagnóstico/retry.")
                    if failed
                    else "Áudio gerado e vinculado à tarefa antes das validações posteriores."
                ),
                merge_result={"audio_generation": deepcopy(checkpoint), "audio_checkpoint": deepcopy(checkpoint)},
            )
            if uv is not None:
                path = str(checkpoint.get("output_path") or checkpoint.get("final_audio_path") or "").strip()
                if path:
                    uv.audio_path = path
                try:
                    uv.audio_size_bytes = int(checkpoint.get("audio_size_bytes") or 0) or None
                except Exception:
                    pass
                try:
                    uv.audio_duration_seconds = float(
                        checkpoint.get("final_audio_duration_sec")
                        or checkpoint.get("duration_seconds")
                        or 0.0
                    ) or None
                except Exception:
                    pass
                provider = checkpoint.get("provider_used") or checkpoint.get("configured_provider")
                if provider:
                    uv.voice_provider = str(provider)[:64]
                model = checkpoint.get("voice_id_used") or checkpoint.get("requested_voice_hint")
                if model:
                    uv.voice_model = str(model)[:128]
                try:
                    uv.call_count_audio = max(
                        int(getattr(uv, "call_count_audio", 0) or 0),
                        int(checkpoint.get("call_count") or 0),
                    )
                except Exception:
                    pass
                if failed and hasattr(uv, "last_error"):
                    uv.last_error = str(failure_message or "Falha após checkpoint de áudio")[:1000]
                db.commit()
        finally:
            db.close()
    except Exception:
        # O checkpoint em video_tasks já foi persistido. Não transformamos uma
        # falha auxiliar do espelho em nova chamada TTS.
        pass
    return True


def _build_checkpoint(generator: Any, segmented: Dict[str, Any], kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    path = str(segmented.get("audio_path") or "").strip()
    meta = _file_metadata(path)
    if not meta.get("exists") or int(meta.get("size_bytes") or 0) < 1000:
        return None

    debug = dict(getattr(generator, "_last_tts_debug", {}) or {})
    attempts = _safe_attempts(debug)
    configured = str(debug.get("configured_provider") or "unknown").strip()
    used = str(debug.get("provider_used") or "unknown").strip()
    main_text = str(kwargs.get("main_text") or "").strip()
    cta_text = str(kwargs.get("cta_text") or "").strip()
    full_text = " ".join(part for part in (main_text, cta_text) if part).strip()
    try:
        duration = float(segmented.get("total_duration_sec") or 0.0)
    except Exception:
        duration = 0.0
    if duration <= 0:
        try:
            duration = float(debug.get("final_audio_duration_sec") or 0.0)
        except Exception:
            duration = 0.0

    segment_count = int(bool(segmented.get("main_audio_path"))) + int(bool(segmented.get("cta_audio_path")))
    checkpoint: Dict[str, Any] = {
        "checkpoint_version": 1,
        "checkpoint_status": "generated",
        "checkpoint_at": _utc_iso(),
        "task_id": _task_id_from_generator(generator),
        "output_path": path,
        "final_audio_path": path,
        "audio_path": path,
        "audio_size_bytes": int(meta.get("size_bytes") or 0),
        "audio_sha256": meta.get("sha256"),
        "final_audio_duration_sec": round(duration, 3) if duration > 0 else None,
        "duration_seconds": round(duration, 3) if duration > 0 else None,
        "configured_provider": configured,
        "provider_used": used,
        "fallback_used": bool(debug.get("fallback_used") or (configured and used and configured != used)),
        "fallback_reason": _fallback_reason(configured, used, attempts),
        "requested_voice_hint": debug.get("requested_voice_hint"),
        "voice_id_used": debug.get("voice_id_used") or debug.get("voice_id"),
        "requested_voice_style": debug.get("requested_voice_style"),
        "requested_voice_gender": debug.get("requested_voice_gender"),
        "attempts": attempts,
        "provider_attempt_count": len(attempts),
        "segment_count": segment_count,
        "call_count": max(1, segment_count),
        "main_audio_path": segmented.get("main_audio_path"),
        "cta_audio_path": segmented.get("cta_audio_path"),
        "main_duration_sec": segmented.get("main_duration_sec"),
        "cta_duration_sec": segmented.get("cta_duration_sec"),
        "initial_silence_duration_sec": segmented.get("initial_silence_duration_sec"),
        "pause_duration_sec": segmented.get("pause_duration_sec"),
        "final_text_sent_to_tts": full_text,
        "final_text_sha256": _text_sha256(full_text),
        "source_plan_fingerprint": getattr(generator, "_codexia_source_plan_fingerprint", None),
        "validation_status": "pending",
    }
    return checkpoint


def _validate_seed_before_reuse(generator: Any, plan: Any) -> Dict[str, Any]:
    if not isinstance(plan, dict):
        return {"has_seed": False, "compatible": True, "reason": "no_plan"}
    seed_path = str(plan.get("seed_audio_path") or "").strip()
    if not seed_path:
        return {"has_seed": False, "compatible": True, "reason": "no_seed"}

    task_id = _task_id_from_generator(generator)
    checkpoint = _checkpoint_from_task(task_id) if task_id else {}
    cp_path = str(checkpoint.get("output_path") or checkpoint.get("final_audio_path") or "").strip()
    reason = None

    if not checkpoint:
        reason = "checkpoint_missing_for_task"
    elif not cp_path or os.path.abspath(cp_path) != os.path.abspath(seed_path):
        reason = "seed_not_linked_to_current_task"
    elif not os.path.isfile(seed_path) or os.path.getsize(seed_path) < 1000:
        reason = "seed_file_missing_or_empty"
    elif checkpoint.get("audio_sha256") and _file_sha256(seed_path) != checkpoint.get("audio_sha256"):
        reason = "seed_hash_changed"
    else:
        expected_text = str(checkpoint.get("final_text_sent_to_tts") or plan.get("seed_narration_text") or "").strip()
        current_plan_text = _plan_narration_text(plan)
        compatibility = _text_compatibility(current_plan_text, expected_text) if current_plan_text and expected_text else 1.0
        if compatibility < 0.86:
            reason = f"narration_changed_similarity_{compatibility:.3f}"

    compatible = reason is None
    result = {
        "has_seed": True,
        "compatible": compatible,
        "reason": reason or "same_task_checkpoint_valid",
        "seed_audio_path": seed_path,
        "checkpoint_path": cp_path or None,
        "checked_at": _utc_iso(),
    }
    if compatible:
        return result

    # Nunca adota MP3 órfão ou incompatível. Mantém roteiro/imagens e permite
    # regenerar somente a narração necessária.
    plan.pop("seed_audio_path", None)
    plan.pop("seed_narration_text", None)
    plan["force_render_only"] = False
    plan["force_reuse_assets"] = True
    try:
        from app.services.task_manager import merge_task_result

        if task_id:
            merge_task_result(task_id, {"audio_seed_validation": result})
    except Exception:
        pass
    return result


def _diagnostic_suffix(checkpoint: Dict[str, Any]) -> str:
    configured = str(checkpoint.get("configured_provider") or "unknown")
    used = str(checkpoint.get("provider_used") or "unknown")
    fallback = "sim" if checkpoint.get("fallback_used") else "não"
    reason = str(checkpoint.get("fallback_reason") or "").strip()
    path = str(checkpoint.get("output_path") or "").strip()
    parts = [f"provider solicitado={configured}", f"provider usado={used}", f"fallback={fallback}"]
    if reason:
        parts.append(f"motivo={reason[:300]}")
    if path:
        parts.append(f"áudio preservado={path}")
    return "; ".join(parts)


def install_audio_checkpoint_patch(video_generator_cls: Optional[Type[Any]] = None) -> Type[Any]:
    """Instala checkpoint persistente no executor de vídeo.

    A instalação é idempotente e foi isolada neste módulo para não alterar o
    pipeline visual. O worker chama esta função uma vez no startup.
    """
    if video_generator_cls is None:
        from app.services.video_generator import VideoGenerator

        video_generator_cls = VideoGenerator

    if getattr(video_generator_cls, "_codexia_audio_checkpoint_patch_installed", False):
        return video_generator_cls

    original_compose = video_generator_cls._compose_segmented_narration_audio
    original_create = video_generator_cls.create_video_from_plan

    def compose_with_checkpoint(self: Any, *args: Any, **kwargs: Any):
        segmented = original_compose(self, *args, **kwargs)
        if not isinstance(segmented, dict):
            return segmented
        call_kwargs = dict(kwargs)
        # O método é keyword-only no fluxo atual; mantemos compatibilidade se
        # algum teste/legado enviar argumentos posicionais.
        checkpoint = _build_checkpoint(self, segmented, call_kwargs)
        if checkpoint:
            _persist_checkpoint(self, checkpoint)
        return segmented

    def create_with_checkpoint(self: Any, plan: Any, *args: Any, **kwargs: Any):
        if isinstance(plan, dict):
            source_text = _plan_narration_text(plan)
            self._codexia_source_plan_fingerprint = _text_sha256(source_text) if source_text else None
            _validate_seed_before_reuse(self, plan)
        try:
            result = original_create(self, plan, *args, **kwargs)
            task_id = _task_id_from_generator(self)
            checkpoint = _checkpoint_from_task(task_id) if task_id else {}
            if checkpoint:
                checkpoint["validation_status"] = "passed"
                checkpoint["validation_completed_at"] = _utc_iso()
                _persist_checkpoint(self, checkpoint)
            return result
        except Exception as exc:
            # Pausa/cancelamento são controle cooperativo, não falha de áudio.
            if type(exc).__name__ in {"_TaskPaused", "_TaskCancelled"}:
                raise
            task_id = _task_id_from_generator(self)
            checkpoint = _checkpoint_from_task(task_id) if task_id else {}
            if checkpoint:
                message = str(exc)
                audio_rejection = any(
                    marker in message.lower()
                    for marker in ("crítica de áudio", "critica de audio", "transcrição oficial", "transcricao oficial")
                )
                checkpoint["validation_status"] = "rejected" if audio_rejection else "preserved_after_failure"
                checkpoint["validation_error"] = message[:1200]
                checkpoint["validation_failed_at"] = _utc_iso()
                _persist_checkpoint(self, checkpoint, failed=True, failure_message=message)
                if audio_rejection:
                    raise RuntimeError(f"{message} | Diagnóstico TTS: {_diagnostic_suffix(checkpoint)}") from exc
            raise

    video_generator_cls._compose_segmented_narration_audio = compose_with_checkpoint
    video_generator_cls.create_video_from_plan = create_with_checkpoint
    video_generator_cls._codexia_audio_checkpoint_patch_installed = True
    return video_generator_cls
