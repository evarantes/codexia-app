import json
import math
import os
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.models import ScheduledVideo, User, VideoTask
from app.services.financial_guardian.adapters import youtube_auto_financial_adapter
from app.services.financial_guardian_service import financial_guardian_service


_AUDIT_TABLE = "codexia_financial_audit_events"
_CACHE_TABLE = "codexia_asset_generation_cache"
_LEDGER_TABLE = "codexia_financial_ledger_entries"

_PERIOD_LABELS = {
    "today": "Hoje",
    "last_7_days": "Últimos 7 dias",
    "last_30_days": "Últimos 30 dias",
    "current_month": "Mês atual",
    "previous_month": "Mês anterior",
}

_SIMULATION_SCENARIOS = {
    "A": "Execução normal",
    "B": "Cache",
    "C": "Recovery útil",
    "D": "Recovery inútil",
    "E": "Orçamento excedido",
    "F": "Persistência temporariamente indisponível",
    "G": "Receita e ROI",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _json_loads(value: Any, default: Any) -> Any:
    try:
        if isinstance(value, str) and value.strip():
            return json.loads(value)
    except Exception:
        pass
    return default


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return json.dumps({"raw": str(value)}, ensure_ascii=False, sort_keys=True)


def _ascii_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text_value = str(value)
    normalized = unicodedata.normalize("NFKD", text_value).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.strip()
    return normalized or None


def _ascii_safe(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = _ascii_text(key)
            cleaned[str(key_text or key)] = _ascii_safe(item)
        return cleaned
    if isinstance(value, list):
        return [_ascii_safe(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_ascii_safe(item) for item in value)
    if isinstance(value, set):
        return [_ascii_safe(item) for item in value]
    if isinstance(value, str):
        return _ascii_text(value) or ""
    return value


def _round_money(value: Any) -> float:
    return round(_safe_float(value, 0.0), 4)


def _dt_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    text_value = str(value).strip()
    return text_value or None

def _path_exists(value: Any) -> bool:
    path = str(value or "").strip()
    if not path:
        return False
    try:
        return os.path.exists(path)
    except Exception:
        return False


def _unique_strings(values: List[Any]) -> List[str]:
    seen = set()
    items: List[str] = []
    for value in values:
        text_value = str(value or "").strip()
        if not text_value or text_value in seen:
            continue
        seen.add(text_value)
        items.append(text_value)
    return items


def _period_bounds(period: str, now: Optional[datetime] = None) -> Tuple[datetime, datetime, str]:
    current = now or _utcnow()
    day_start = datetime(current.year, current.month, current.day)
    tomorrow = day_start + timedelta(days=1)
    period_key = str(period or "today").strip().lower()
    if period_key == "last_7_days":
        return day_start - timedelta(days=6), tomorrow, period_key
    if period_key == "last_30_days":
        return day_start - timedelta(days=29), tomorrow, period_key
    if period_key == "current_month":
        return datetime(current.year, current.month, 1), tomorrow, period_key
    if period_key == "previous_month":
        current_month_start = datetime(current.year, current.month, 1)
        previous_month_end = current_month_start
        previous_month_start = datetime(
            previous_month_end.year if previous_month_end.month > 1 else previous_month_end.year - 1,
            previous_month_end.month - 1 if previous_month_end.month > 1 else 12,
            1,
        )
        return previous_month_start, previous_month_end, period_key
    return day_start, tomorrow, "today"


def _previous_period_bounds(period: str, now: Optional[datetime] = None) -> Tuple[datetime, datetime, str]:
    current = now or _utcnow()
    start, end, period_key = _period_bounds(period, current)
    if period_key == "today":
        return start - timedelta(days=1), start, "yesterday"
    if period_key == "last_7_days":
        return start - timedelta(days=7), start, "previous_7_days"
    if period_key == "last_30_days":
        return start - timedelta(days=30), start, "previous_30_days"
    if period_key == "current_month":
        return _period_bounds("previous_month", current)
    if period_key == "previous_month":
        previous_anchor = start - timedelta(days=1)
        return _period_bounds("previous_month", previous_anchor)
    return start - timedelta(days=1), start, "previous"


def _normalized_event_type(raw: Any, details: Optional[Dict[str, Any]] = None) -> str:
    details = details or {}
    value = str(raw or "").strip().upper()
    if not value:
        return "UNKNOWN"
    aliases = {
        "PREFLIGHT_ALLOWED": "PRE_ESTIMATE",
        "PREFLIGHT_BLOCKED": "BUDGET_BLOCKED",
        "PRODUCTION_STARTED": "JOB_STARTED",
        "IMAGE_CACHE_APPLIED": "CACHE_HIT",
        "IMAGE_CACHE_STORED": "IMAGE_GENERATED",
        "PRODUCTION_FAILED": "VIDEO_FAILED",
        "RECOVERY_LOOP_BLOCKED": "RECOVERY_STOPPED",
        "PRE_ESTIMATE": "PRE_ESTIMATE",
        "JOB_STARTED": "JOB_STARTED",
        "SCRIPT_STARTED": "SCRIPT_STARTED",
        "SCRIPT_COMPLETED": "SCRIPT_COMPLETED",
        "IMAGE_REQUESTED": "IMAGE_REQUESTED",
        "IMAGE_GENERATED": "IMAGE_GENERATED",
        "CACHE_HIT": "CACHE_HIT",
        "IMAGE_REUSED": "IMAGE_REUSED",
        "AUDIO_STARTED": "AUDIO_STARTED",
        "AUDIO_COMPLETED": "AUDIO_COMPLETED",
        "TRANSCRIPTION_STARTED": "TRANSCRIPTION_STARTED",
        "TRANSCRIPTION_REUSED": "TRANSCRIPTION_REUSED",
        "RENDER_STARTED": "RENDER_STARTED",
        "RENDER_COMPLETED": "RENDER_COMPLETED",
        "RECOVERY_STARTED": "RECOVERY_STARTED",
        "RECOVERY_STOPPED": "RECOVERY_STOPPED",
        "BUDGET_WARNING": "BUDGET_WARNING",
        "BUDGET_BLOCKED": "BUDGET_BLOCKED",
        "VIDEO_COMPLETED": "VIDEO_COMPLETED",
        "VIDEO_FAILED": "VIDEO_FAILED",
        "VIDEO_PUBLISHED": "VIDEO_PUBLISHED",
    }
    mapped = aliases.get(value, value)
    if mapped == "PRODUCTION_COMPLETED":
        upload_mock = isinstance(details.get("upload_result"), dict) and details.get("upload_result")
        return "VIDEO_PUBLISHED" if upload_mock else "VIDEO_COMPLETED"
    return mapped


def _scenario_task_id(user_id: int, scenario_code: str) -> str:
    return f"fgsim-yt-{int(user_id)}-{str(scenario_code or '').strip().lower()}"


class YouTubeFinancialGuardianObservabilityService:
    def ensure_schema(self, db: Session) -> None:
        financial_guardian_service.ensure_schema(db)
        tables = set(inspect(db.bind).get_table_names())
        if _LEDGER_TABLE in tables:
            return
        dialect = (getattr(getattr(db.bind, "dialect", None), "name", "") or "").lower()
        is_postgres = dialect in {"postgres", "postgresql"}
        pk_type = "SERIAL PRIMARY KEY" if is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        timestamp_type = "TIMESTAMP" if is_postgres else "DATETIME"
        db.execute(text(
            f"""
            CREATE TABLE {_LEDGER_TABLE} (
                id {pk_type},
                user_id INTEGER NULL,
                tenant_id INTEGER NULL,
                source_type VARCHAR(64) NOT NULL,
                entry_kind VARCHAR(16) NOT NULL,
                category VARCHAR(64) NOT NULL,
                provider VARCHAR(128) NULL,
                model VARCHAR(128) NULL,
                currency VARCHAR(16) NOT NULL,
                amount DOUBLE PRECISION NOT NULL,
                description TEXT NULL,
                reference VARCHAR(255) NULL,
                notes TEXT NULL,
                receipt_reference VARCHAR(255) NULL,
                metadata_json TEXT NULL,
                occurred_at {timestamp_type} NOT NULL,
                created_at {timestamp_type} NOT NULL,
                updated_at {timestamp_type} NOT NULL
            )
            """
        ))
        db.commit()

    def _fetch_ledger_rows(
        self,
        db: Session,
        *,
        user: User,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        self.ensure_schema(db)
        clauses = ["source_type = 'youtube_auto'"]
        params: Dict[str, Any] = {}
        if not bool(getattr(user, "is_admin", False)):
            clauses.insert(0, "user_id = :user_id")
            params["user_id"] = int(user.id)
        if start is not None:
            clauses.append("occurred_at >= :start")
            params["start"] = start
        if end is not None:
            clauses.append("occurred_at < :end")
            params["end"] = end
        rows = db.execute(text(
            f"""
            SELECT id, entry_kind, category, provider, model, currency, amount, description, reference,
                   notes, receipt_reference, metadata_json, occurred_at, created_at, updated_at
            FROM {_LEDGER_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY occurred_at DESC, id DESC
            """
        ), params).mappings().all()
        return [dict(row) for row in rows]

    def list_ledger_entries(self, db: Session, *, user: User) -> List[Dict[str, Any]]:
        rows = self._fetch_ledger_rows(db, user=user)
        items: List[Dict[str, Any]] = []
        for row in rows:
            items.append({
                "id": row.get("id"),
                "entry_kind": row.get("entry_kind"),
                "category": row.get("category"),
                "provider": row.get("provider"),
                "model": row.get("model"),
                "currency": row.get("currency"),
                "amount": _round_money(row.get("amount")),
                "description": row.get("description"),
                "reference": row.get("reference"),
                "notes": row.get("notes"),
                "receipt_reference": row.get("receipt_reference"),
                "metadata": _json_loads(row.get("metadata_json"), {}),
                "occurred_at": _dt_iso(row.get("occurred_at")),
                "created_at": _dt_iso(row.get("created_at")),
                "updated_at": _dt_iso(row.get("updated_at")),
            })
        return items

    def save_ledger_entry(self, db: Session, *, user: User, payload: Dict[str, Any], entry_id: Optional[int] = None) -> Dict[str, Any]:
        self.ensure_schema(db)
        now = _utcnow()
        occurred_at = payload.get("occurred_at")
        if isinstance(occurred_at, str) and occurred_at.strip():
            try:
                occurred_dt = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                if getattr(occurred_dt, "tzinfo", None) is not None:
                    occurred_dt = occurred_dt.astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                occurred_dt = now
        else:
            occurred_dt = now
        data = {
            "user_id": int(user.id),
            "tenant_id": getattr(user, "tenant_id", None),
            "source_type": "youtube_auto",
            "entry_kind": "revenue" if str(payload.get("entry_kind") or "").strip().lower() == "revenue" else "expense",
            "category": (_ascii_text(payload.get("category") or "outros") or "outros")[:64],
            "provider": (_ascii_text(payload.get("provider")) or None),
            "model": (_ascii_text(payload.get("model")) or None),
            "currency": (_ascii_text(payload.get("currency") or "BRL") or "BRL").upper()[:16],
            "amount": _round_money(payload.get("amount")),
            "description": (_ascii_text(payload.get("description")) or None),
            "reference": (_ascii_text(payload.get("reference")) or None),
            "notes": (_ascii_text(payload.get("notes")) or None),
            "receipt_reference": (_ascii_text(payload.get("receipt_reference")) or None),
            "metadata_json": _json_dumps(_ascii_safe(payload.get("metadata") or {})),
            "occurred_at": occurred_dt,
            "updated_at": now,
        }
        if data["provider"] is not None:
            data["provider"] = data["provider"][:128]
        if data["model"] is not None:
            data["model"] = data["model"][:128]
        if data["description"] is not None:
            data["description"] = data["description"][:2000]
        if data["reference"] is not None:
            data["reference"] = data["reference"][:255]
        if data["notes"] is not None:
            data["notes"] = data["notes"][:4000]
        if data["receipt_reference"] is not None:
            data["receipt_reference"] = data["receipt_reference"][:255]
        if entry_id:
            db.execute(text(
                f"""
                UPDATE {_LEDGER_TABLE}
                SET entry_kind = :entry_kind,
                    category = :category,
                    provider = :provider,
                    model = :model,
                    currency = :currency,
                    amount = :amount,
                    description = :description,
                    reference = :reference,
                    notes = :notes,
                    receipt_reference = :receipt_reference,
                    metadata_json = :metadata_json,
                    occurred_at = :occurred_at,
                    updated_at = :updated_at
                WHERE id = :id AND user_id = :user_id
                """
            ), {**data, "id": int(entry_id)})
            db.commit()
            return {"id": int(entry_id), **payload, "amount": data["amount"], "occurred_at": occurred_dt.isoformat()}
        data["created_at"] = now
        row = db.execute(text(
            f"""
            INSERT INTO {_LEDGER_TABLE} (
                user_id, tenant_id, source_type, entry_kind, category, provider, model, currency, amount,
                description, reference, notes, receipt_reference, metadata_json, occurred_at, created_at, updated_at
            ) VALUES (
                :user_id, :tenant_id, :source_type, :entry_kind, :category, :provider, :model, :currency, :amount,
                :description, :reference, :notes, :receipt_reference, :metadata_json, :occurred_at, :created_at, :updated_at
            )
            """
        ), data)
        db.commit()
        inserted_id = None
        try:
            inserted_id = row.lastrowid
        except Exception:
            inserted_id = None
        if inserted_id is None:
            inserted = db.execute(text(
                f"""
                SELECT id
                FROM {_LEDGER_TABLE}
                WHERE user_id = :user_id
                ORDER BY id DESC
                LIMIT 1
                """
            ), {"user_id": int(user.id)}).scalar()
            inserted_id = inserted
        return {"id": inserted_id, **payload, "amount": data["amount"], "occurred_at": occurred_dt.isoformat()}

    def delete_ledger_entry(self, db: Session, *, user: User, entry_id: int) -> None:
        self.ensure_schema(db)
        db.execute(text(
            f"DELETE FROM {_LEDGER_TABLE} WHERE id = :id AND user_id = :user_id"
        ), {"id": int(entry_id), "user_id": int(user.id)})
        db.commit()

    def estimate_preproduction(self, *, user: User, payload: Dict[str, Any]) -> Dict[str, Any]:
        duration_min = max(1, min(60, _safe_int(payload.get("duration"), 8) or 8))
        selected_images = payload.get("selected_images") if isinstance(payload.get("selected_images"), list) else []
        selected_images = [item for item in selected_images if isinstance(item, str) and item.strip()]
        scene_count = max(4, min(24, duration_min * 2))
        reused_images = min(len(selected_images), scene_count)
        new_images = max(0, scene_count - reused_images)
        estimated_ctx = youtube_auto_financial_adapter.build_context(
            task_id=_scenario_task_id(int(user.id), "estimate"),
            payload=payload,
            user_id=int(user.id),
            status="estimated",
        )
        expected_cost = _round_money(estimated_ctx.estimated_cost)
        confidence = 86 if payload.get("story_content") else 72
        quality_score = 80 + (3 if payload.get("kind") == "story" else 1) + (2 if reused_images else 0)
        probable_min = round(max(0.0, expected_cost * 0.91), 2)
        probable_max = round(expected_cost * 1.17, 2)
        emergency_limit = round(max(probable_max * 1.42, expected_cost * 1.65), 2)
        return {
            "source_type": "youtube_auto",
            "task_id": _scenario_task_id(int(user.id), "estimate"),
            "duration_predicted_min": duration_min,
            "duration_predicted_label": f"{duration_min} min",
            "scenes_predicted": scene_count,
            "new_images_predicted": new_images,
            "reused_images_predicted": reused_images,
            "quality_predicted_score": min(99, quality_score),
            "resolution": str(payload.get("aspect_ratio") or "16:9"),
            "calls_estimated": {
                "text": 1,
                "image": new_images,
                "audio": 1,
                "render": 1,
                "youtube_publish": 1 if payload.get("auto_upload") else 0,
            },
            "expected_cost": expected_cost,
            "probable_range": {
                "min": probable_min,
                "max": probable_max,
            },
            "emergency_limit": emergency_limit,
            "confidence_percent": confidence,
            "provider_mix": [
                {"provider": "mock-openai", "model": "mock-gpt-4o-mini", "calls": 1},
                {"provider": "mock-image-cache", "model": "local-manifest", "calls": reused_images},
                {"provider": "mock-renderer", "model": "local-ffmpeg", "calls": 1},
            ],
            "note": "Estimativa 100% simulada. Nenhuma chamada paga foi executada.",
        }

    def _fetch_audit_rows(
        self,
        db: Session,
        *,
        user: User,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        task_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self.ensure_schema(db)
        clauses = ["source_type = 'youtube_auto'"]
        params: Dict[str, Any] = {}
        if not bool(getattr(user, "is_admin", False)):
            clauses.insert(0, "user_id = :user_id")
            params["user_id"] = int(user.id)
        if start is not None:
            clauses.append("created_at >= :start")
            params["start"] = start
        if end is not None:
            clauses.append("created_at < :end")
            params["end"] = end
        if task_id:
            clauses.append("context_id = :task_id")
            params["task_id"] = str(task_id)
        rows = db.execute(text(
            f"""
            SELECT id, event_type, stage, severity, estimated_cost, actual_cost, context_json, details_json, created_at, context_id
            FROM {_AUDIT_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at ASC, id ASC
            """
        ), params).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_youtube_tasks(
        self,
        db: Session,
        *,
        user: User,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        task_id: Optional[str] = None,
    ) -> List[VideoTask]:
        q = db.query(VideoTask)
        if not bool(getattr(user, "is_admin", False)):
            q = q.filter(VideoTask.user_id == int(user.id))
        if task_id:
            q = q.filter(VideoTask.id == str(task_id))
        if start is not None:
            q = q.filter(VideoTask.created_at >= start)
        if end is not None:
            q = q.filter(VideoTask.created_at < end)
        rows = q.order_by(VideoTask.updated_at.desc(), VideoTask.created_at.desc()).all()
        return [row for row in rows if self._task_is_youtube_auto(row)]

    def _task_is_youtube_auto(self, row: VideoTask) -> bool:
        result = _json_loads(getattr(row, "result_json", None), {})
        if not isinstance(result, dict):
            return False
        if str(result.get("kind") or "").strip().lower() == "youtube_story_video":
            return True
        guardian = result.get("financial_guardian") if isinstance(result.get("financial_guardian"), dict) else {}
        if str(guardian.get("source_type") or "").strip().lower() == "youtube_auto":
            return True
        simulation = result.get("simulation") if isinstance(result.get("simulation"), dict) else {}
        return bool(simulation.get("scenario_code"))

    def _task_result(self, row: VideoTask) -> Dict[str, Any]:
        result = _json_loads(getattr(row, "result_json", None), {})
        return result if isinstance(result, dict) else {}

    def _task_payload(self, row: VideoTask) -> Dict[str, Any]:
        result = self._task_result(row)
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        return payload if isinstance(payload, dict) else {}

    def _task_artifact_snapshot(self, row: VideoTask) -> Dict[str, Any]:
        result = self._task_result(row)
        payload = self._task_payload(row)
        script = result.get("script") if isinstance(result.get("script"), dict) else {}
        render_report = result.get("render_report") if isinstance(result.get("render_report"), dict) else {}
        audio_generation = result.get("audio_generation") if isinstance(result.get("audio_generation"), dict) else {}
        if not audio_generation:
            audio_generation = render_report.get("audio_generation") if isinstance(render_report.get("audio_generation"), dict) else {}
        official_audio_transcription = (
            result.get("official_audio_transcription")
            if isinstance(result.get("official_audio_transcription"), dict)
            else {}
        )
        if not official_audio_transcription:
            official_audio_transcription = (
                render_report.get("official_audio_transcription")
                if isinstance(render_report.get("official_audio_transcription"), dict)
                else {}
            )
        scene_visuals = render_report.get("scene_visuals") if isinstance(render_report.get("scene_visuals"), list) else []

        image_candidates: List[str] = []
        for container in (
            script.get("selected_images"),
            payload.get("selected_images"),
            result.get("selected_images"),
        ):
            if isinstance(container, list):
                image_candidates.extend(container)
        for visual in scene_visuals:
            if isinstance(visual, dict):
                image_candidates.append(visual.get("image_path"))
        image_paths = _unique_strings(image_candidates)

        audio_path = (
            audio_generation.get("output_path")
            or audio_generation.get("main_audio_path")
            or script.get("reuse_existing_audio_path")
            or ""
        )
        subtitle_path = (
            official_audio_transcription.get("srt_path")
            or official_audio_transcription.get("subtitle_path")
            or ""
        )
        video_path = result.get("file_path") or render_report.get("file_path") or ""
        video_url = result.get("video_url") or render_report.get("video_url") or ""
        transcription_ready = bool(
            subtitle_path
            or official_audio_transcription.get("full_text")
            or official_audio_transcription.get("text")
            or official_audio_transcription.get("segments")
            or official_audio_transcription.get("words")
        )
        return {
            "script_ready": bool(script),
            "image_paths": image_paths,
            "image_count": len(image_paths),
            "images_available": sum(1 for path in image_paths if _path_exists(path)),
            "audio_path": str(audio_path or "").strip(),
            "audio_available": _path_exists(audio_path),
            "subtitle_path": str(subtitle_path or "").strip(),
            "subtitle_available": _path_exists(subtitle_path),
            "transcription_ready": transcription_ready,
            "video_path": str(video_path or "").strip(),
            "video_available": _path_exists(video_path),
            "video_url": str(video_url or "").strip(),
        }

    def _audit_rollup_by_task(self, db: Session, *, user: User) -> Dict[str, Dict[str, Any]]:
        rows = self._fetch_audit_rows(db, user=user)
        rollup: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            details = _json_loads(row.get("details_json"), {})
            context_json = _json_loads(row.get("context_json"), {})
            task_id = str(row.get("context_id") or context_json.get("context_id") or "").strip()
            if not task_id:
                continue
            event_type = _normalized_event_type(row.get("event_type"), details)
            entry = rollup.setdefault(task_id, {
                "events_total": 0,
                "published": False,
                "blocked": False,
                "failed": False,
                "last_event": None,
                "last_event_at": None,
            })
            entry["events_total"] += 1
            entry["published"] = bool(entry["published"] or event_type == "VIDEO_PUBLISHED")
            entry["blocked"] = bool(entry["blocked"] or event_type == "BUDGET_BLOCKED")
            entry["failed"] = bool(entry["failed"] or event_type in {"VIDEO_FAILED", "RECOVERY_STOPPED"})
            entry["last_event"] = event_type
            entry["last_event_at"] = _dt_iso(row.get("created_at"))
        return rollup

    def _fetch_cache_rows(self, db: Session, *, user: User) -> List[Dict[str, Any]]:
        self.ensure_schema(db)
        tables = set(inspect(db.bind).get_table_names())
        if _CACHE_TABLE not in tables:
            return []
        rows = db.execute(text(
            f"""
            SELECT id, context_id, asset_kind, cache_key, file_path, hit_count, last_used_at, created_at, updated_at, meta_json
            FROM {_CACHE_TABLE}
            WHERE user_id = :user_id
              AND source_type = 'youtube_auto'
            ORDER BY updated_at DESC, id DESC
            """
        ), {"user_id": int(user.id)}).mappings().all()
        return [dict(row) for row in rows]

    def _build_content_registry(
        self,
        db: Session,
        *,
        user: User,
        limit: int = 12,
    ) -> Dict[str, Any]:
        tasks = self._fetch_youtube_tasks(db, user=user)
        audit_rollup = self._audit_rollup_by_task(db, user=user)
        grouped: Dict[str, Dict[str, Any]] = {}

        for row in tasks:
            result = self._task_result(row)
            payload = self._task_payload(row)
            script = result.get("script") if isinstance(result.get("script"), dict) else {}
            title_control = result.get("title_control") if isinstance(result.get("title_control"), dict) else {}
            artifacts = self._task_artifact_snapshot(row)
            content_fingerprint = str(payload.get("content_fingerprint") or "").strip()
            registry_key = content_fingerprint or f"task:{row.id}"
            created_at = _dt_iso(getattr(row, "created_at", None))
            updated_at = _dt_iso(getattr(row, "updated_at", None))
            audit_info = audit_rollup.get(str(row.id), {})

            item = grouped.get(registry_key)
            current_sort_key = str(updated_at or created_at or "")
            if item is None or current_sort_key >= str(item.get("_sort_key") or ""):
                grouped[registry_key] = {
                    "_sort_key": current_sort_key,
                    "task_id": str(row.id),
                    "content_fingerprint": content_fingerprint or None,
                    "status": str(getattr(row, "status", "") or ""),
                    "mode": str(payload.get("mode") or "").strip() or None,
                    "kind": str(result.get("kind") or payload.get("kind") or "").strip() or None,
                    "internal_title": (
                        title_control.get("internal_title")
                        or script.get("internal_title")
                        or payload.get("internal_title")
                        or payload.get("topic")
                    ),
                    "youtube_title": (
                        title_control.get("youtube_title")
                        or script.get("youtube_title")
                        or result.get("title")
                        or payload.get("youtube_title")
                        or payload.get("override_title")
                        or payload.get("topic")
                    ),
                    "narrated_title": (
                        title_control.get("narrated_title")
                        or script.get("narrated_title")
                        or payload.get("narrated_title")
                    ),
                    "auto_upload": bool(payload.get("auto_upload")),
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "published": bool(audit_info.get("published")),
                    "last_guardian_event": audit_info.get("last_event"),
                    "events_total": int(audit_info.get("events_total") or 0),
                    "cost_control": result.get("cost_control") if isinstance(result.get("cost_control"), dict) else {},
                    "artifacts": {
                        "script_ready": artifacts["script_ready"],
                        "images_registered": artifacts["image_count"],
                        "audio_registered": bool(artifacts["audio_path"]),
                        "video_registered": bool(artifacts["video_path"] or artifacts["video_url"]),
                        "transcription_ready": bool(artifacts["transcription_ready"]),
                    },
                    "duplicate_versions": 0,
                }
                item = grouped[registry_key]
            item["duplicate_versions"] = int(item.get("duplicate_versions") or 0) + 1

        items = sorted(grouped.values(), key=lambda entry: str(entry.get("_sort_key") or ""), reverse=True)
        for item in items:
            item.pop("_sort_key", None)

        fingerprinted = sum(1 for item in items if item.get("content_fingerprint"))
        published = sum(1 for item in items if item.get("published"))
        duplicated = sum(1 for item in items if int(item.get("duplicate_versions") or 0) > 1)
        reusable_ready = sum(
            1
            for item in items
            if bool((item.get("cost_control") or {}).get("reused_video"))
            or bool((item.get("artifacts") or {}).get("video_registered"))
        )
        return {
            "scope": "all_time",
            "items_total": len(items),
            "fingerprinted_total": fingerprinted,
            "published_total": published,
            "duplicate_groups": duplicated,
            "reusable_ready_total": reusable_ready,
            "recent_items": items[:max(1, int(limit or 12))],
        }

    def _build_artifact_library(
        self,
        db: Session,
        *,
        user: User,
        limit: int = 12,
    ) -> Dict[str, Any]:
        tasks = self._fetch_youtube_tasks(db, user=user)
        cache_rows = self._fetch_cache_rows(db, user=user)
        scripts_ready = 0
        transcription_ready = 0
        image_registry = set()
        image_available = set()
        audio_registry = set()
        audio_available = set()
        video_registry = set()
        video_available = set()
        recent_artifacts: List[Dict[str, Any]] = []

        for row in tasks:
            artifacts = self._task_artifact_snapshot(row)
            if artifacts["script_ready"]:
                scripts_ready += 1
            if artifacts["transcription_ready"]:
                transcription_ready += 1
            for path in artifacts["image_paths"]:
                image_registry.add(path)
                if _path_exists(path):
                    image_available.add(path)
            if artifacts["audio_path"]:
                audio_registry.add(artifacts["audio_path"])
                if artifacts["audio_available"]:
                    audio_available.add(artifacts["audio_path"])
            if artifacts["video_path"] or artifacts["video_url"]:
                canonical_video = artifacts["video_path"] or artifacts["video_url"]
                video_registry.add(canonical_video)
                if artifacts["video_available"]:
                    video_available.add(canonical_video)
            recent_artifacts.append({
                "task_id": str(row.id),
                "status": str(getattr(row, "status", "") or ""),
                "updated_at": _dt_iso(getattr(row, "updated_at", None)),
                "script_ready": artifacts["script_ready"],
                "images_registered": artifacts["image_count"],
                "audio_registered": bool(artifacts["audio_path"]),
                "audio_available": bool(artifacts["audio_available"]),
                "video_registered": bool(artifacts["video_path"] or artifacts["video_url"]),
                "video_available": bool(artifacts["video_available"]),
                "transcription_ready": bool(artifacts["transcription_ready"]),
            })

        cache_keys = {str(row.get("cache_key") or "").strip() for row in cache_rows if str(row.get("cache_key") or "").strip()}
        cache_available = sum(1 for row in cache_rows if _path_exists(row.get("file_path")))
        cache_hits = sum(max(0, _safe_int(row.get("hit_count"), 0)) for row in cache_rows)
        recent_cache = [
            {
                "cache_key": row.get("cache_key"),
                "asset_kind": row.get("asset_kind"),
                "context_id": row.get("context_id"),
                "file_path": row.get("file_path"),
                "available": _path_exists(row.get("file_path")),
                "hit_count": max(0, _safe_int(row.get("hit_count"), 0)),
                "updated_at": _dt_iso(row.get("updated_at")),
                "last_used_at": _dt_iso(row.get("last_used_at")),
            }
            for row in cache_rows[:max(1, int(limit or 12))]
        ]
        recent_artifacts.sort(key=lambda entry: str(entry.get("updated_at") or ""), reverse=True)
        return {
            "scope": "all_time",
            "scripts_ready_total": scripts_ready,
            "transcriptions_ready_total": transcription_ready,
            "images_registered_total": len(image_registry),
            "images_available_total": len(image_available),
            "audio_registered_total": len(audio_registry),
            "audio_available_total": len(audio_available),
            "videos_registered_total": len(video_registry),
            "videos_available_total": len(video_available),
            "image_cache_rows_total": len(cache_rows),
            "image_cache_keys_total": len(cache_keys),
            "image_cache_available_total": cache_available,
            "image_cache_hits_total": cache_hits,
            "recent_task_artifacts": recent_artifacts[:max(1, int(limit or 12))],
            "recent_cached_assets": recent_cache,
        }

    def _build_shorts_summary(
        self,
        db: Session,
        *,
        user: User,
        limit: int = 12,
    ) -> Dict[str, Any]:
        rows = (
            db.query(ScheduledVideo)
            .filter(ScheduledVideo.user_id == int(user.id))
            .order_by(ScheduledVideo.updated_at.desc(), ScheduledVideo.id.desc())
            .all()
        )
        shorts = [row for row in rows if str(getattr(row, "video_type", "") or "").strip().lower() == "short"]
        parent_ids = {int(row.parent_video_id) for row in shorts if getattr(row, "parent_video_id", None)}
        parent_map: Dict[int, str] = {}
        if parent_ids:
            parents = db.query(ScheduledVideo).filter(ScheduledVideo.id.in_(list(parent_ids))).all()
            parent_map = {int(row.id): str(row.title or row.theme or f"ID {row.id}") for row in parents}

        published = 0
        ready = 0
        failed = 0
        orphaned = 0
        recent_items: List[Dict[str, Any]] = []
        for row in shorts:
            status = str(getattr(row, "status", "") or "").strip().lower()
            is_published = bool(getattr(row, "uploaded_at", None) or getattr(row, "youtube_video_id", None) or "published" in status)
            if is_published:
                published += 1
            elif status in {"completed", "ready", "awaiting_publish"}:
                ready += 1
            elif status == "failed":
                failed += 1
            if getattr(row, "parent_video_id", None) and int(row.parent_video_id) not in parent_map:
                orphaned += 1
            recent_items.append({
                "id": int(row.id),
                "title": str(getattr(row, "title", None) or getattr(row, "theme", None) or f"Short {row.id}"),
                "status": str(getattr(row, "status", "") or ""),
                "parent_video_id": getattr(row, "parent_video_id", None),
                "parent_title": parent_map.get(int(row.parent_video_id)) if getattr(row, "parent_video_id", None) else None,
                "youtube_video_id": getattr(row, "youtube_video_id", None),
                "uploaded_at": _dt_iso(getattr(row, "uploaded_at", None)),
                "updated_at": _dt_iso(getattr(row, "updated_at", None)),
                "video_url": getattr(row, "video_url", None),
            })
        recent_items.sort(key=lambda entry: str(entry.get("updated_at") or ""), reverse=True)
        return {
            "scope": "all_time",
            "shorts_total": len(shorts),
            "published_total": published,
            "ready_total": ready,
            "failed_total": failed,
            "pending_total": max(0, len(shorts) - published - ready - failed),
            "orphaned_total": orphaned,
            "recent_items": recent_items[:max(1, int(limit or 12))],
        }

    def _summarize_window(self, db: Session, *, user: User, start: datetime, end: datetime, label: str) -> Dict[str, Any]:
        audit_rows = self._fetch_audit_rows(db, user=user, start=start, end=end)
        task_rows = self._fetch_youtube_tasks(db, user=user, start=start, end=end)
        ledger_rows = self._fetch_ledger_rows(db, user=user, start=start, end=end)

        contexts: Dict[str, Dict[str, Any]] = {}
        provider_buckets: Dict[str, Dict[str, Any]] = {}
        model_buckets: Dict[str, Dict[str, Any]] = {}
        stage_costs: Dict[str, float] = {}
        stage_times: Dict[str, float] = {}
        event_counts: Dict[str, int] = {}
        cache_hits = 0
        cache_misses = 0
        images_reused = 0
        audio_reused = 0
        transcription_reused = 0
        renders_reused = 0
        recovery_count = 0
        recovery_stopped = 0
        loops_detected = 0
        ai_calls = 0
        regenerations = 0
        economy_cache_total = 0.0
        economy_reuse_total = 0.0

        for task in task_rows:
            result = _json_loads(getattr(task, "result_json", None), {})
            payload = result.get("payload") if isinstance(result, dict) and isinstance(result.get("payload"), dict) else {}
            simulation = result.get("simulation") if isinstance(result, dict) and isinstance(result.get("simulation"), dict) else {}
            context = contexts.setdefault(str(task.id), {
                "task_id": str(task.id),
                "title": payload.get("topic") or payload.get("override_title") or simulation.get("title") or "Vídeo do YouTube Auto",
                "status": str(getattr(task, "status", "") or ""),
                "estimated_cost": 0.0,
                "actual_cost": 0.0,
                "duration_seconds": 0.0,
                "quality_score": None,
                "attempts": 1,
                "scenario_code": simulation.get("scenario_code"),
                "final_event": None,
                "cache_savings": 0.0,
                "reuse_savings": 0.0,
                "events": [],
                "created_at": _dt_iso(getattr(task, "created_at", None)),
                "updated_at": _dt_iso(getattr(task, "updated_at", None)),
            })
            guardian = result.get("financial_guardian") if isinstance(result.get("financial_guardian"), dict) else {}
            context["estimated_cost"] = _round_money(guardian.get("estimated_cost") or context["estimated_cost"])
            context["actual_cost"] = _round_money(guardian.get("actual_cost") or context["actual_cost"])

        for row in audit_rows:
            details = _json_loads(row.get("details_json"), {})
            context_json = _json_loads(row.get("context_json"), {})
            event_type = _normalized_event_type(row.get("event_type"), details)
            context_id = str(row.get("context_id") or context_json.get("context_id") or "").strip()
            if not context_id:
                continue
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
            context = contexts.setdefault(context_id, {
                "task_id": context_id,
                "title": context_json.get("title") or "Vídeo do YouTube Auto",
                "status": context_json.get("status") or "",
                "estimated_cost": _round_money(context_json.get("estimated_cost")),
                "actual_cost": _round_money(context_json.get("actual_cost")),
                "duration_seconds": 0.0,
                "quality_score": None,
                "attempts": 1,
                "scenario_code": (details.get("scenario_code") or (context_json.get("metadata") or {}).get("scenario_code")),
                "final_event": None,
                "cache_savings": 0.0,
                "reuse_savings": 0.0,
                "events": [],
                "created_at": context_json.get("created_at"),
                "updated_at": _dt_iso(row.get("created_at")),
            })
            context["estimated_cost"] = max(context["estimated_cost"], _round_money(row.get("estimated_cost")))
            context["actual_cost"] = max(context["actual_cost"], _round_money(row.get("actual_cost")))
            context["duration_seconds"] += _safe_float(details.get("duration_seconds"), 0.0)
            if details.get("quality_score") is not None:
                context["quality_score"] = _safe_float(details.get("quality_score"), 0.0)
            context["attempts"] = max(context["attempts"], _safe_int(details.get("attempt"), 1))
            context["cache_savings"] += _safe_float(details.get("cache_savings"), 0.0)
            context["reuse_savings"] += _safe_float(details.get("reuse_savings"), 0.0)
            context["events"].append(event_type)
            context["updated_at"] = _dt_iso(row.get("created_at"))
            context["title"] = context_json.get("title") or details.get("title") or context["title"]
            if context["final_event"] is None or str(context["updated_at"] or "") <= str(_dt_iso(row.get("created_at")) or ""):
                context["final_event"] = event_type

            provider = str(details.get("provider") or "").strip() or "mock-local"
            model = str(details.get("model") or "").strip() or "mock-default"
            step_cost = _safe_float(details.get("step_cost"), 0.0)
            provider_bucket = provider_buckets.setdefault(provider, {"provider": provider, "total_cost": 0.0, "calls": 0})
            model_bucket = model_buckets.setdefault(model, {"model": model, "total_cost": 0.0, "calls": 0})
            provider_bucket["total_cost"] = round(provider_bucket["total_cost"] + step_cost, 4)
            model_bucket["total_cost"] = round(model_bucket["total_cost"] + step_cost, 4)
            if step_cost > 0 or bool(details.get("ai_call")):
                provider_bucket["calls"] += 1
                model_bucket["calls"] += 1
                ai_calls += 1

            stage_costs[event_type] = round(stage_costs.get(event_type, 0.0) + step_cost, 4)
            stage_times[event_type] = round(stage_times.get(event_type, 0.0) + _safe_float(details.get("duration_seconds"), 0.0), 4)

            if event_type == "CACHE_HIT":
                cache_hits += max(1, _safe_int(details.get("cache_hits"), 1))
            if event_type == "IMAGE_GENERATED":
                cache_misses += max(1, _safe_int(details.get("generated_count"), 1))
            if event_type == "IMAGE_REUSED":
                images_reused += max(1, _safe_int(details.get("reuse_count"), 1))
            if event_type == "AUDIO_COMPLETED" and bool(details.get("audio_reused")):
                audio_reused += 1
            if event_type == "TRANSCRIPTION_REUSED":
                transcription_reused += 1
            if event_type == "RENDER_COMPLETED" and bool(details.get("render_reused")):
                renders_reused += 1
            if event_type == "RECOVERY_STARTED":
                recovery_count += 1
                regenerations += 1
            if event_type == "RECOVERY_STOPPED":
                recovery_stopped += 1
                if bool(details.get("loop_detected")):
                    loops_detected += 1
            if event_type == "BUDGET_WARNING":
                loops_detected += 0
            economy_cache_total += _safe_float(details.get("cache_savings"), 0.0)
            economy_reuse_total += _safe_float(details.get("reuse_savings"), 0.0)

        completed = 0
        failed = 0
        blocked = 0
        in_progress = 0
        actual_total = 0.0
        estimated_total = 0.0
        total_duration = 0.0
        quality_scores: List[float] = []
        attempt_total = 0

        for context in contexts.values():
            status_value = str(context.get("status") or "").strip().lower()
            final_event = str(context.get("final_event") or "").strip().upper()
            if "BUDGET_BLOCKED" in context.get("events", []) or final_event == "BUDGET_BLOCKED":
                blocked += 1
                context["status"] = "blocked"
            elif final_event in {"VIDEO_FAILED", "RECOVERY_STOPPED"} or status_value == "failed":
                failed += 1
                context["status"] = "failed"
            elif final_event in {"VIDEO_COMPLETED", "VIDEO_PUBLISHED"} or status_value == "completed":
                completed += 1
                context["status"] = "completed"
            elif status_value in {"pending", "processing"}:
                in_progress += 1
                context["status"] = status_value
            actual_total += _safe_float(context.get("actual_cost"), 0.0)
            estimated_total += _safe_float(context.get("estimated_cost"), 0.0)
            total_duration += _safe_float(context.get("duration_seconds"), 0.0)
            if context.get("quality_score") is not None:
                quality_scores.append(_safe_float(context.get("quality_score"), 0.0))
            attempt_total += max(1, _safe_int(context.get("attempts"), 1))

        expense_total = 0.0
        revenue_total = 0.0
        for row in ledger_rows:
            amount = _safe_float(row.get("amount"), 0.0)
            if str(row.get("entry_kind") or "").strip().lower() == "revenue":
                revenue_total += amount
            else:
                expense_total += amount

        investment_total = actual_total + expense_total
        profit_loss = revenue_total - investment_total
        roi_percent = round((profit_loss / investment_total) * 100, 2) if investment_total > 0 else 0.0
        produced_count = max(1, completed + failed)
        avg_cost = round(actual_total / produced_count, 4) if (completed + failed) > 0 else 0.0
        avg_time = round(total_duration / produced_count, 2) if (completed + failed) > 0 else 0.0
        avg_quality = round(sum(quality_scores) / len(quality_scores), 2) if quality_scores else 0.0
        success_rate = round((completed / max(1, completed + failed + blocked)) * 100, 2) if (completed + failed + blocked) > 0 else 0.0
        slowest_stage = max(stage_times.items(), key=lambda item: item[1])[0] if stage_times else None
        most_expensive_stage = max(stage_costs.items(), key=lambda item: item[1])[0] if stage_costs else None
        most_expensive_provider = None
        if provider_buckets:
            most_expensive_provider = max(provider_buckets.values(), key=lambda item: item["total_cost"])["provider"]
        most_expensive_model = None
        if model_buckets:
            most_expensive_model = max(model_buckets.values(), key=lambda item: item["total_cost"])["model"]
        return {
            "label": label,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "jobs_total": len(contexts),
            "jobs_completed": completed,
            "jobs_failed": failed,
            "jobs_blocked": blocked,
            "jobs_in_progress": in_progress,
            "estimated_cost_total": round(estimated_total, 4),
            "actual_cost_total": round(actual_total, 4),
            "daily_cost_accumulated": 0.0,
            "monthly_cost_accumulated": 0.0,
            "avg_cost_per_video": avg_cost,
            "avg_production_time_seconds": avg_time,
            "avg_quality_score": avg_quality,
            "roi_percent": roi_percent,
            "profit_loss": round(profit_loss, 4),
            "revenue_total": round(revenue_total, 4),
            "expense_total": round(expense_total, 4),
            "investment_total": round(investment_total, 4),
            "calls_count": ai_calls,
            "attempts_count": attempt_total,
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "images_reused": images_reused,
            "audio_reused": audio_reused,
            "transcription_reused": transcription_reused,
            "renders_reused": renders_reused,
            "economy_cache_total": round(economy_cache_total, 4),
            "economy_reuse_total": round(economy_reuse_total, 4),
            "recovery_count": recovery_count,
            "recovery_stopped": recovery_stopped,
            "loops_detected": loops_detected,
            "regenerations_count": regenerations,
            "success_rate": success_rate,
            "provider_costs": sorted(provider_buckets.values(), key=lambda item: (-item["total_cost"], item["provider"])),
            "model_costs": sorted(model_buckets.values(), key=lambda item: (-item["total_cost"], item["model"])),
            "stage_costs": [{"stage": k, "total_cost": v} for k, v in sorted(stage_costs.items(), key=lambda item: (-item[1], item[0]))],
            "stage_times": [{"stage": k, "total_seconds": v} for k, v in sorted(stage_times.items(), key=lambda item: (-item[1], item[0]))],
            "slowest_stage": slowest_stage,
            "most_expensive_stage": most_expensive_stage,
            "most_expensive_provider": most_expensive_provider,
            "most_expensive_model": most_expensive_model,
            "recent_jobs": sorted(contexts.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True)[:12],
            "event_counts": event_counts,
            "no_paid_calls_confirmed": True,
        }

    def _trend(self, current: float, previous: float, *, prefer_lower: bool = False) -> Dict[str, Any]:
        delta = round(_safe_float(current) - _safe_float(previous), 4)
        if previous:
            delta_pct = round((delta / previous) * 100, 2)
        else:
            delta_pct = 100.0 if current else 0.0
        if abs(delta) < 0.0001:
            direction = "estável"
        else:
            direction = "caiu" if delta < 0 else "subiu"
        impact = "melhorou" if ((prefer_lower and delta < 0) or (not prefer_lower and delta > 0)) else "piorou"
        if abs(delta) < 0.0001:
            impact = "estável"
        return {
            "current": current,
            "previous": previous,
            "delta": delta,
            "delta_percent": delta_pct,
            "direction": direction,
            "impact": impact,
        }

    def _build_health(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        reasons: List[str] = []
        success_rate = _safe_float(summary.get("success_rate"), 0.0)
        if success_rate < 60:
            reasons.append("Taxa de sucesso abaixo de 60%.")
        elif success_rate < 85:
            reasons.append("Taxa de sucesso abaixo da faixa ideal.")
        if _safe_int(summary.get("jobs_blocked"), 0) > 0:
            reasons.append("Existem jobs bloqueados por orçamento.")
        if _safe_int(summary.get("recovery_stopped"), 0) > 0:
            reasons.append("Houve recoveries interrompidos por baixo ganho.")
        if _safe_int(summary.get("loops_detected"), 0) > 0:
            reasons.append("Foram detectados loops de recuperação.")
        if not reasons:
            status = "Saudável"
            reason = "Taxa de sucesso alta, sem bloqueios críticos e sem loops relevantes."
        elif success_rate < 60 or _safe_int(summary.get("loops_detected"), 0) > 0:
            status = "Crítico"
            reason = " ".join(reasons)
        else:
            status = "Atenção"
            reason = " ".join(reasons)
        return {
            "status": status,
            "reason": reason,
            "executions_completed": _safe_int(summary.get("jobs_completed"), 0),
            "executions_failed": _safe_int(summary.get("jobs_failed"), 0),
            "executions_blocked": _safe_int(summary.get("jobs_blocked"), 0),
            "success_rate": _safe_float(summary.get("success_rate"), 0.0),
            "recoveries": _safe_int(summary.get("recovery_count"), 0),
            "recoveries_stopped": _safe_int(summary.get("recovery_stopped"), 0),
            "loops_detected": _safe_int(summary.get("loops_detected"), 0),
            "cache_hits": _safe_int(summary.get("cache_hits"), 0),
            "cache_misses": _safe_int(summary.get("cache_misses"), 0),
            "images_reused": _safe_int(summary.get("images_reused"), 0),
            "audios_reused": _safe_int(summary.get("audio_reused"), 0),
            "transcriptions_reused": _safe_int(summary.get("transcription_reused"), 0),
            "renders_reused": _safe_int(summary.get("renders_reused"), 0),
            "avg_time_per_stage_seconds": summary.get("stage_times") or [],
            "slowest_stage": summary.get("slowest_stage"),
            "most_expensive_stage": summary.get("most_expensive_stage"),
            "most_expensive_provider": summary.get("most_expensive_provider"),
            "most_expensive_model": summary.get("most_expensive_model"),
        }

    def _build_comparison(self, current: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "period_label": current.get("label"),
            "previous_label": previous.get("label"),
            "metrics": {
                "avg_cost": self._trend(_safe_float(current.get("avg_cost_per_video"), 0.0), _safe_float(previous.get("avg_cost_per_video"), 0.0), prefer_lower=True),
                "avg_time": self._trend(_safe_float(current.get("avg_production_time_seconds"), 0.0), _safe_float(previous.get("avg_production_time_seconds"), 0.0), prefer_lower=True),
                "avg_quality": self._trend(_safe_float(current.get("avg_quality_score"), 0.0), _safe_float(previous.get("avg_quality_score"), 0.0)),
                "success_rate": self._trend(_safe_float(current.get("success_rate"), 0.0), _safe_float(previous.get("success_rate"), 0.0)),
                "videos": self._trend(_safe_float(current.get("jobs_total"), 0.0), _safe_float(previous.get("jobs_total"), 0.0)),
                "cache_savings": self._trend(_safe_float(current.get("economy_cache_total"), 0.0), _safe_float(previous.get("economy_cache_total"), 0.0)),
                "ai_calls": self._trend(_safe_float(current.get("calls_count"), 0.0), _safe_float(previous.get("calls_count"), 0.0), prefer_lower=True),
                "regenerations": self._trend(_safe_float(current.get("regenerations_count"), 0.0), _safe_float(previous.get("regenerations_count"), 0.0), prefer_lower=True),
                "revenue": self._trend(_safe_float(current.get("revenue_total"), 0.0), _safe_float(previous.get("revenue_total"), 0.0)),
                "profit_loss": self._trend(_safe_float(current.get("profit_loss"), 0.0), _safe_float(previous.get("profit_loss"), 0.0)),
            },
            "trend_summary": {
                "cost": "caiu" if _safe_float(current.get("avg_cost_per_video"), 0.0) < _safe_float(previous.get("avg_cost_per_video"), 0.0) else "subiu",
                "quality": "subiu" if _safe_float(current.get("avg_quality_score"), 0.0) >= _safe_float(previous.get("avg_quality_score"), 0.0) else "caiu",
                "time": "caiu" if _safe_float(current.get("avg_production_time_seconds"), 0.0) < _safe_float(previous.get("avg_production_time_seconds"), 0.0) else "subiu",
                "efficiency": "melhorou" if (_safe_float(current.get("success_rate"), 0.0) >= _safe_float(previous.get("success_rate"), 0.0) and _safe_float(current.get("avg_cost_per_video"), 0.0) <= _safe_float(previous.get("avg_cost_per_video"), 0.0)) else "piorou",
            },
        }

    def _build_progress(self, current: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, Any]:
        metrics = [
            ("redução de custo médio", _safe_float(current.get("avg_cost_per_video"), 0.0) < _safe_float(previous.get("avg_cost_per_video"), 0.0)),
            ("aumento de qualidade média", _safe_float(current.get("avg_quality_score"), 0.0) > _safe_float(previous.get("avg_quality_score"), 0.0)),
            ("redução de tempo médio", _safe_float(current.get("avg_production_time_seconds"), 0.0) < _safe_float(previous.get("avg_production_time_seconds"), 0.0)),
            ("aumento da taxa de conclusão", _safe_float(current.get("success_rate"), 0.0) > _safe_float(previous.get("success_rate"), 0.0)),
            ("redução de falhas", _safe_int(current.get("jobs_failed"), 0) < _safe_int(previous.get("jobs_failed"), 0)),
            ("aumento de cache/reutilização", (_safe_float(current.get("economy_cache_total"), 0.0) + _safe_float(current.get("economy_reuse_total"), 0.0)) > (_safe_float(previous.get("economy_cache_total"), 0.0) + _safe_float(previous.get("economy_reuse_total"), 0.0))),
            ("aumento de receita", _safe_float(current.get("revenue_total"), 0.0) > _safe_float(previous.get("revenue_total"), 0.0)),
            ("redução de prejuízo", _safe_float(current.get("profit_loss"), 0.0) > _safe_float(previous.get("profit_loss"), 0.0)),
        ]
        score = sum(1 if ok else -1 for _, ok in metrics)
        if score >= 5:
            label = "Progresso comprovado"
        elif score >= 2:
            label = "Progresso parcial"
        elif score >= 0:
            label = "Sem progresso mensurável"
        else:
            label = "Regressão"
        supporting_metrics = [name for name, ok in metrics if ok]
        blocking_metrics = [name for name, ok in metrics if not ok]
        return {
            "label": label,
            "score": score,
            "supporting_metrics": supporting_metrics,
            "blocking_metrics": blocking_metrics,
            "reason": (
                "As melhorias compensaram o investimento no período."
                if label == "Progresso comprovado"
                else (
                    "Houve avanços, mas ainda não suficientes para provar retorno completo."
                    if label == "Progresso parcial"
                    else (
                        "As métricas não mostram ganho líquido consistente."
                        if label == "Sem progresso mensurável"
                        else "O período atual regrediu em relação ao anterior."
                    )
                )
            ),
        }

    def build_overview(self, db: Session, *, user: User, period: str) -> Dict[str, Any]:
        current_start, current_end, period_key = _period_bounds(period)
        previous_start, previous_end, _ = _previous_period_bounds(period_key)
        current = self._summarize_window(db, user=user, start=current_start, end=current_end, label=_PERIOD_LABELS.get(period_key, "Período"))
        previous = self._summarize_window(db, user=user, start=previous_start, end=previous_end, label="Período anterior")
        today_start, today_end, _ = _period_bounds("today")
        month_start, month_end, _ = _period_bounds("current_month")
        today_summary = self._summarize_window(db, user=user, start=today_start, end=today_end, label="Hoje")
        month_summary = self._summarize_window(db, user=user, start=month_start, end=month_end, label="Mês atual")
        current["daily_cost_accumulated"] = today_summary["actual_cost_total"]
        current["monthly_cost_accumulated"] = month_summary["actual_cost_total"]
        ledger_entries = self.list_ledger_entries(db, user=user)
        total_invested = month_summary["investment_total"]
        month_revenue = month_summary["revenue_total"]
        remaining_to_break_even = round(max(0.0, total_invested - month_revenue), 4)
        avg_daily_revenue = round(month_revenue / max(1, (_utcnow() - datetime(_utcnow().year, _utcnow().month, 1)).days + 1), 4)
        recovery_days = round(remaining_to_break_even / avg_daily_revenue, 2) if avg_daily_revenue > 0 else None
        content_registry = self._build_content_registry(db, user=user)
        artifact_library = self._build_artifact_library(db, user=user)
        shorts_summary = self._build_shorts_summary(db, user=user)
        return {
            "source_type": "youtube_auto",
            "period": period_key,
            "dashboard": current,
            "health": self._build_health(current),
            "comparison": self._build_comparison(current, previous),
            "progress_indicator": self._build_progress(current, previous),
            "ledger_summary": {
                "entries_count": len(ledger_entries),
                "total_invested": round(total_invested, 4),
                "monthly_expense": round(month_summary["expense_total"] + month_summary["actual_cost_total"], 4),
                "monthly_revenue": round(month_summary["revenue_total"], 4),
                "profit_loss": round(month_summary["profit_loss"], 4),
                "roi_percent": round(month_summary["roi_percent"], 2),
                "break_even_point": round(total_invested, 4),
                "remaining_to_break_even": remaining_to_break_even,
                "estimated_recovery_days": recovery_days,
            },
            "recent_jobs": current.get("recent_jobs") or [],
            "ledger_entries": ledger_entries[:20],
            "content_registry": content_registry,
            "artifact_library": artifact_library,
            "shorts_summary": shorts_summary,
            "no_paid_calls_confirmed": True,
            "scenarios": [{"code": code, "label": label} for code, label in _SIMULATION_SCENARIOS.items()],
            "generated_at": _utcnow().isoformat(),
        }

    def build_timeline(self, db: Session, *, user: User, task_id: str) -> Dict[str, Any]:
        rows = self._fetch_audit_rows(db, user=user, task_id=task_id)
        tasks = self._fetch_youtube_tasks(db, user=user, task_id=task_id)
        if not rows and not tasks:
            return {"task_id": str(task_id), "events": [], "found": False}
        task = tasks[0] if tasks else None
        result = _json_loads(getattr(task, "result_json", None), {}) if task else {}
        payload = result.get("payload") if isinstance(result, dict) and isinstance(result.get("payload"), dict) else {}
        simulation = result.get("simulation") if isinstance(result, dict) and isinstance(result.get("simulation"), dict) else {}
        items: List[Dict[str, Any]] = []
        total_duration = 0.0
        final_estimated = 0.0
        final_actual = 0.0
        for row in rows:
            details = _json_loads(row.get("details_json"), {})
            context_json = _json_loads(row.get("context_json"), {})
            event_type = _normalized_event_type(row.get("event_type"), details)
            duration = _safe_float(details.get("duration_seconds"), 0.0)
            total_duration += duration
            final_estimated = max(final_estimated, _safe_float(row.get("estimated_cost"), 0.0))
            final_actual = max(final_actual, _safe_float(row.get("actual_cost"), 0.0))
            items.append({
                "id": row.get("id"),
                "event_type": event_type,
                "timestamp": _dt_iso(row.get("created_at")),
                "duration_seconds": duration,
                "status": str(details.get("status") or row.get("severity") or "info"),
                "estimated_cost": _round_money(row.get("estimated_cost")),
                "actual_cost": _round_money(row.get("actual_cost")),
                "provider": details.get("provider"),
                "model": details.get("model"),
                "attempt": _safe_int(details.get("attempt"), 1),
                "reason": details.get("reason") or details.get("message") or details.get("observation"),
                "identifiers": {
                    "job_id": details.get("job_id") or (context_json.get("metadata") or {}).get("job_id"),
                    "task_id": str(task_id),
                    "video_id": details.get("video_id"),
                    "scene_id": details.get("scene_id"),
                    "scene_number": details.get("scene_number"),
                },
                "technical_note": details.get("technical_note") or details.get("note"),
                "details": details,
            })
        return {
            "task_id": str(task_id),
            "found": True,
            "title": payload.get("topic") or simulation.get("title") or (items[-1]["details"].get("title") if items else None),
            "status": str(getattr(task, "status", "") or (items[-1]["status"] if items else "")),
            "scenario_code": simulation.get("scenario_code"),
            "scenario_label": _SIMULATION_SCENARIOS.get(str(simulation.get("scenario_code") or "").upper()),
            "estimated_cost": round(final_estimated, 4),
            "actual_cost": round(final_actual, 4),
            "total_duration_seconds": round(total_duration, 2),
            "events": items,
            "no_paid_calls_confirmed": True,
        }

    def _insert_audit_row(
        self,
        db: Session,
        *,
        user: User,
        context_id: str,
        context: Dict[str, Any],
        event_type: str,
        stage: str,
        severity: str,
        estimated_cost: float,
        actual_cost: float,
        details: Dict[str, Any],
        created_at: datetime,
    ) -> None:
        db.execute(text(
            f"""
            INSERT INTO {_AUDIT_TABLE} (
                user_id, job_id, source_type, context_id, scope_key, event_type, stage, severity,
                estimated_cost, actual_cost, context_json, details_json, created_at
            ) VALUES (
                :user_id, NULL, 'youtube_auto', :context_id, :scope_key, :event_type, :stage, :severity,
                :estimated_cost, :actual_cost, :context_json, :details_json, :created_at
            )
            """
        ), {
            "user_id": int(user.id),
            "context_id": str(context_id),
            "scope_key": f"user:{int(user.id)}",
            "event_type": _ascii_text(event_type) or "UNKNOWN",
            "stage": _ascii_text(stage),
            "severity": _ascii_text(severity) or "info",
            "estimated_cost": _round_money(estimated_cost),
            "actual_cost": _round_money(actual_cost),
            "context_json": _json_dumps(_ascii_safe(context)),
            "details_json": _json_dumps(_ascii_safe(details)),
            "created_at": created_at,
        })

    def _upsert_simulated_task(
        self,
        db: Session,
        *,
        user: User,
        task_id: str,
        status: str,
        message: str,
        payload: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        existing = db.query(VideoTask).filter(VideoTask.id == str(task_id)).first()
        now = _utcnow()
        if existing:
            existing.status = status
            existing.progress = 100 if status == "completed" else (0 if status == "failed" else 80)
            existing.message = _ascii_text(message) or message
            existing.result_json = _json_dumps(_ascii_safe(result))
            existing.updated_at = now
            return
        row = VideoTask(
            id=str(task_id),
            user_id=int(user.id),
            status=status,
            progress=100 if status == "completed" else (0 if status == "failed" else 80),
            message=_ascii_text(message) or message,
            result_json=_json_dumps(_ascii_safe(result)),
            created_at=now,
            updated_at=now,
        )
        db.add(row)

    def _delete_scenario_records(self, db: Session, *, user: User, task_id: str, scenario_code: str) -> None:
        self.ensure_schema(db)
        db.execute(text(
            f"DELETE FROM {_AUDIT_TABLE} WHERE user_id = :user_id AND source_type = 'youtube_auto' AND context_id = :task_id"
        ), {"user_id": int(user.id), "task_id": str(task_id)})
        db.execute(text(
            f"""
            DELETE FROM {_LEDGER_TABLE}
            WHERE user_id = :user_id
              AND source_type = 'youtube_auto'
              AND metadata_json LIKE :marker
            """
        ), {"user_id": int(user.id), "marker": f"%\"scenario_code\": \"{str(scenario_code).upper()}\"%"})

    def _scenario_blueprint(self, *, user: User, scenario_code: str) -> Dict[str, Any]:
        code = str(scenario_code or "").strip().upper()
        if code not in _SIMULATION_SCENARIOS:
            raise ValueError("Cenário inválido.")
        task_id = _scenario_task_id(int(user.id), code)
        payload = {
            "mode": "story",
            "kind": "story",
            "topic": f"{_SIMULATION_SCENARIOS[code]} — Financial Guardian",
            "duration": 8 if code not in {"E", "G"} else (12 if code == "G" else 20),
            "aspect_ratio": "16:9",
            "auto_upload": code == "A",
            "story_content": "Execução 100% simulada para validar observabilidade, timeline, ROI e segurança financeira.",
        }
        base_context = youtube_auto_financial_adapter.build_context(
            task_id=task_id,
            payload=payload,
            user_id=int(user.id),
            status="processing",
        )
        estimated_cost = max(0.12, round(base_context.estimated_cost + (0.35 if code == "G" else 0.0), 4))
        final_actual = {
            "A": estimated_cost * 0.96,
            "B": max(0.01, estimated_cost * 0.38),
            "C": estimated_cost * 1.18,
            "D": estimated_cost * 1.07,
            "E": 0.0,
            "F": estimated_cost * 0.64,
            "G": estimated_cost * 0.92,
        }[code]
        start = _utcnow() - timedelta(minutes={"A": 45, "B": 40, "C": 35, "D": 30, "E": 25, "F": 20, "G": 15}[code])

        def event(
            offset_seconds: int,
            event_type: str,
            *,
            stage: str,
            estimated: float = estimated_cost,
            actual: float = 0.0,
            severity: str = "info",
            duration: float = 0.0,
            step_cost: float = 0.0,
            provider: Optional[str] = None,
            model: Optional[str] = None,
            attempt: int = 1,
            reason: Optional[str] = None,
            scene_number: Optional[int] = None,
            quality_score: Optional[float] = None,
            note: Optional[str] = None,
            extra: Optional[Dict[str, Any]] = None,
        ) -> Dict[str, Any]:
            details = {
                "status": "simulated",
                "provider": provider,
                "model": model,
                "attempt": attempt,
                "reason": reason,
                "scene_number": scene_number,
                "duration_seconds": duration,
                "step_cost": round(step_cost, 4),
                "quality_score": quality_score,
                "technical_note": note,
                "scenario_code": code,
                "ai_call": bool(provider),
                "video_id": f"sim-video-{code.lower()}",
                "scene_id": f"scene-{scene_number}" if scene_number else None,
            }
            if extra:
                details.update(extra)
            return {
                "event_type": event_type,
                "stage": stage,
                "severity": severity,
                "estimated_cost": estimated,
                "actual_cost": actual,
                "details": details,
                "created_at": start + timedelta(seconds=offset_seconds),
            }

        events: List[Dict[str, Any]] = []
        if code == "A":
            events = [
                event(0, "PRE_ESTIMATE", stage="preflight", actual=0.0, note="Estimativa aprovada sem consumo pago."),
                event(5, "JOB_STARTED", stage="job", duration=5, note="Job simulado iniciado no runtime real."),
                event(15, "SCRIPT_STARTED", stage="script", provider="mock-openai", model="mock-gpt-4o-mini", duration=12, step_cost=0.08),
                event(30, "SCRIPT_COMPLETED", stage="script", actual=0.08, duration=15, provider="mock-openai", model="mock-gpt-4o-mini", quality_score=82.0),
                event(45, "IMAGE_REQUESTED", stage="images", duration=4, provider="mock-image", model="mock-gpt-image-1", scene_number=1, step_cost=0.03),
                event(60, "IMAGE_GENERATED", stage="images", actual=0.11, duration=18, provider="mock-image", model="mock-gpt-image-1", scene_number=1, step_cost=0.03, extra={"generated_count": 1}),
                event(78, "AUDIO_STARTED", stage="audio", duration=5, provider="mock-elevenlabs", model="mock-voice-v1", step_cost=0.02),
                event(94, "AUDIO_COMPLETED", stage="audio", actual=0.13, duration=16, provider="mock-elevenlabs", model="mock-voice-v1", step_cost=0.02),
                event(112, "TRANSCRIPTION_STARTED", stage="transcription", duration=3, provider="mock-openai", model="mock-whisper", step_cost=0.01),
                event(124, "TRANSCRIPTION_REUSED", stage="transcription", actual=0.14, duration=12, provider="mock-openai", model="mock-whisper", step_cost=0.0, note="Legenda reaproveitada do cache local."),
                event(138, "RENDER_STARTED", stage="render", duration=8, provider="mock-renderer", model="local-ffmpeg", step_cost=0.02),
                event(164, "RENDER_COMPLETED", stage="render", actual=0.16, duration=26, provider="mock-renderer", model="local-ffmpeg", quality_score=88.0, step_cost=0.02),
                event(192, "VIDEO_COMPLETED", stage="final", actual=round(final_actual, 4), duration=12, note="Vídeo concluído em modo 100% simulado."),
                event(208, "VIDEO_PUBLISHED", stage="publish", actual=round(final_actual, 4), duration=10, provider="mock-youtube", model="upload-mock", note="Publicação simulada; nenhum upload real ocorreu.", extra={"upload_result": {"status": "uploaded_mock"}}),
            ]
            status = "completed"
            message = "Cenário A concluído com sucesso."
        elif code == "B":
            events = [
                event(0, "PRE_ESTIMATE", stage="preflight", note="Estimativa com potencial alto de cache."),
                event(4, "JOB_STARTED", stage="job", duration=4),
                event(12, "SCRIPT_STARTED", stage="script", provider="mock-openai", model="mock-gpt-4o-mini", duration=8, step_cost=0.05),
                event(24, "SCRIPT_COMPLETED", stage="script", actual=0.05, duration=12, provider="mock-openai", model="mock-gpt-4o-mini", quality_score=80.0),
                event(32, "CACHE_HIT", stage="images", actual=0.05, duration=2, provider="mock-image-cache", model="local-manifest", scene_number=1, extra={"cache_hits": 1, "cache_savings": 0.07, "reuse_savings": 0.05}),
                event(38, "IMAGE_REUSED", stage="images", actual=0.05, duration=4, provider="mock-image-cache", model="local-manifest", scene_number=1, extra={"reuse_count": 1, "reuse_savings": 0.05}),
                event(46, "AUDIO_STARTED", stage="audio", duration=4, provider="mock-elevenlabs", model="mock-voice-v1", step_cost=0.02),
                event(58, "AUDIO_COMPLETED", stage="audio", actual=0.07, duration=12, provider="mock-elevenlabs", model="mock-voice-v1", step_cost=0.02, extra={"audio_reused": True}),
                event(72, "RENDER_STARTED", stage="render", duration=6, provider="mock-renderer", model="local-ffmpeg", step_cost=0.01),
                event(90, "RENDER_COMPLETED", stage="render", actual=0.08, duration=18, provider="mock-renderer", model="local-ffmpeg", quality_score=84.0, step_cost=0.01, extra={"render_reused": True}),
                event(108, "VIDEO_COMPLETED", stage="final", actual=round(final_actual, 4), duration=10, note="Nenhuma geração nova de imagem foi necessária."),
            ]
            status = "completed"
            message = "Cenário B concluiu com cache hit e economia registrada."
        elif code == "C":
            events = [
                event(0, "PRE_ESTIMATE", stage="preflight"),
                event(5, "JOB_STARTED", stage="job", duration=5),
                event(14, "SCRIPT_STARTED", stage="script", provider="mock-openai", model="mock-gpt-4o-mini", duration=9, step_cost=0.06),
                event(28, "SCRIPT_COMPLETED", stage="script", actual=0.06, duration=14, provider="mock-openai", model="mock-gpt-4o-mini", quality_score=74.0),
                event(40, "IMAGE_REQUESTED", stage="images", duration=5, provider="mock-image", model="mock-gpt-image-1", scene_number=1, step_cost=0.04),
                event(56, "IMAGE_GENERATED", stage="images", actual=0.10, duration=16, provider="mock-image", model="mock-gpt-image-1", scene_number=1, step_cost=0.04, extra={"generated_count": 1}),
                event(74, "RECOVERY_STARTED", stage="recovery", actual=0.10, duration=6, provider="mock-openai", model="mock-qc-loop", attempt=2, reason="Score abaixo do alvo inicial", quality_score=74.0),
                event(88, "IMAGE_REQUESTED", stage="recovery", actual=0.10, duration=4, provider="mock-image", model="mock-gpt-image-1", scene_number=2, step_cost=0.03, attempt=2),
                event(102, "IMAGE_GENERATED", stage="recovery", actual=0.13, duration=14, provider="mock-image", model="mock-gpt-image-1", scene_number=2, step_cost=0.03, attempt=2, extra={"generated_count": 1}),
                event(120, "RENDER_STARTED", stage="render", actual=0.13, duration=7, provider="mock-renderer", model="local-ffmpeg", step_cost=0.02, attempt=2),
                event(148, "RENDER_COMPLETED", stage="render", actual=0.15, duration=28, provider="mock-renderer", model="local-ffmpeg", quality_score=91.0, step_cost=0.02, attempt=2, note="Recovery trouxe ganho real de qualidade."),
                event(166, "VIDEO_COMPLETED", stage="final", actual=round(final_actual, 4), duration=12, attempt=2),
            ]
            status = "completed"
            message = "Cenário C concluiu com recovery útil."
        elif code == "D":
            events = [
                event(0, "PRE_ESTIMATE", stage="preflight"),
                event(5, "JOB_STARTED", stage="job", duration=5),
                event(15, "SCRIPT_STARTED", stage="script", provider="mock-openai", model="mock-gpt-4o-mini", duration=10, step_cost=0.05),
                event(29, "SCRIPT_COMPLETED", stage="script", actual=0.05, duration=14, provider="mock-openai", model="mock-gpt-4o-mini", quality_score=77.0),
                event(44, "RECOVERY_STARTED", stage="recovery", actual=0.05, duration=6, provider="mock-openai", model="mock-qc-loop", attempt=2, reason="Tentativa extra para subir score", quality_score=77.0),
                event(58, "RECOVERY_STOPPED", stage="recovery", actual=0.07, duration=14, provider="mock-openai", model="mock-qc-loop", attempt=2, reason="Sem ganho real após nova iteração", quality_score=77.2, step_cost=0.02, extra={"loop_detected": True}),
                event(78, "VIDEO_FAILED", stage="final", actual=round(final_actual, 4), duration=10, severity="warning", reason="Proteção ativada para evitar gasto improdutivo."),
            ]
            status = "failed"
            message = "Cenário D interrompido por recovery inútil."
        elif code == "E":
            events = [
                event(0, "PRE_ESTIMATE", stage="preflight", estimated=round(estimated_cost * 1.8, 4), actual=0.0, note="Estimativa elevada para disparar trava financeira."),
                event(3, "BUDGET_WARNING", stage="preflight", estimated=round(estimated_cost * 1.8, 4), actual=0.0, severity="warning", reason="Custo projetado acima da faixa normal."),
                event(7, "BUDGET_BLOCKED", stage="preflight", estimated=round(estimated_cost * 1.8, 4), actual=0.0, severity="warning", reason="Bloqueado antes de qualquer chamada paga.", note="Nenhuma chamada externa foi executada."),
            ]
            status = "failed"
            message = "Cenário E bloqueado por orçamento."
        elif code == "F":
            events = [
                event(0, "PRE_ESTIMATE", stage="preflight"),
                event(6, "JOB_STARTED", stage="job", duration=6),
                event(16, "SCRIPT_STARTED", stage="script", provider="mock-openai", model="mock-gpt-4o-mini", duration=10, step_cost=0.05),
                event(30, "SCRIPT_COMPLETED", stage="script", actual=0.05, duration=14, provider="mock-openai", model="mock-gpt-4o-mini", quality_score=81.0),
                event(46, "IMAGE_REQUESTED", stage="images", duration=4, provider="mock-image", model="mock-gpt-image-1", scene_number=1, step_cost=0.03),
                event(62, "IMAGE_GENERATED", stage="images", actual=0.08, duration=16, provider="mock-image", model="mock-gpt-image-1", scene_number=1, step_cost=0.03, extra={"generated_count": 1}),
                event(76, "CACHE_HIT", stage="fallback", actual=0.08, duration=3, provider="mock-image-cache", model="local-manifest", scene_number=1, extra={"cache_hits": 1, "cache_savings": 0.04, "reuse_savings": 0.03}, note="Persistência principal indisponível; fallback local aplicado."),
                event(92, "IMAGE_REUSED", stage="fallback", actual=0.08, duration=5, provider="mock-image-cache", model="local-manifest", scene_number=1, extra={"reuse_count": 1, "reuse_savings": 0.03}),
                event(112, "RENDER_STARTED", stage="render", duration=7, provider="mock-renderer", model="local-ffmpeg", step_cost=0.02),
                event(138, "RENDER_COMPLETED", stage="render", actual=0.10, duration=26, provider="mock-renderer", model="local-ffmpeg", quality_score=85.0, step_cost=0.02),
                event(156, "VIDEO_COMPLETED", stage="final", actual=round(final_actual, 4), duration=10, note="Fallback local preservou o cache e evitou regeneração desnecessária."),
            ]
            status = "completed"
            message = "Cenário F concluiu usando fallback local."
        else:
            events = [
                event(0, "PRE_ESTIMATE", stage="preflight"),
                event(5, "JOB_STARTED", stage="job", duration=5),
                event(14, "SCRIPT_STARTED", stage="script", provider="mock-openai", model="mock-gpt-4o-mini", duration=9, step_cost=0.07),
                event(29, "SCRIPT_COMPLETED", stage="script", actual=0.07, duration=15, provider="mock-openai", model="mock-gpt-4o-mini", quality_score=86.0),
                event(42, "CACHE_HIT", stage="images", actual=0.07, duration=3, provider="mock-image-cache", model="local-manifest", scene_number=1, extra={"cache_hits": 1, "cache_savings": 0.09, "reuse_savings": 0.07}),
                event(58, "AUDIO_STARTED", stage="audio", duration=4, provider="mock-elevenlabs", model="mock-voice-v1", step_cost=0.03),
                event(72, "AUDIO_COMPLETED", stage="audio", actual=0.10, duration=14, provider="mock-elevenlabs", model="mock-voice-v1", step_cost=0.03),
                event(88, "RENDER_STARTED", stage="render", duration=7, provider="mock-renderer", model="local-ffmpeg", step_cost=0.02),
                event(114, "RENDER_COMPLETED", stage="render", actual=0.12, duration=26, provider="mock-renderer", model="local-ffmpeg", quality_score=90.0, step_cost=0.02),
                event(132, "VIDEO_COMPLETED", stage="final", actual=round(final_actual, 4), duration=10, note="Base pronta para análise de ROI."),
            ]
            status = "completed"
            message = "Cenário G concluiu com receitas e ROI simulados."

        return {
            "task_id": task_id,
            "scenario_code": code,
            "scenario_label": _SIMULATION_SCENARIOS[code],
            "payload": payload,
            "estimated_cost": round(estimated_cost, 4),
            "final_actual_cost": round(final_actual, 4),
            "events": events,
            "status": status,
            "message": message,
        }

    def simulate_scenario(self, db: Session, *, user: User, scenario_code: str) -> Dict[str, Any]:
        blueprint = self._scenario_blueprint(user=user, scenario_code=scenario_code)
        task_id = blueprint["task_id"]
        self._delete_scenario_records(db, user=user, task_id=task_id, scenario_code=blueprint["scenario_code"])
        payload = blueprint["payload"]
        context_payload = {
            "source_type": "youtube_auto",
            "context_id": task_id,
            "scope_key": f"user:{int(user.id)}",
            "user_id": int(user.id),
            "title": payload.get("topic"),
            "status": blueprint["status"],
            "estimated_cost": blueprint["estimated_cost"],
            "actual_cost": blueprint["final_actual_cost"],
            "aspect_ratio": payload.get("aspect_ratio"),
            "created_at": blueprint["events"][0]["created_at"].isoformat() if blueprint["events"] else _utcnow().isoformat(),
            "metadata": {
                "task_id": task_id,
                "scenario_code": blueprint["scenario_code"],
                "simulated": True,
            },
        }
        for event in blueprint["events"]:
            self._insert_audit_row(
                db,
                user=user,
                context_id=task_id,
                context=context_payload,
                event_type=event["event_type"],
                stage=event["stage"],
                severity=event["severity"],
                estimated_cost=event["estimated_cost"],
                actual_cost=event["actual_cost"],
                details=event["details"],
                created_at=event["created_at"],
            )

        if blueprint["scenario_code"] == "G":
            base_date = blueprint["events"][-1]["created_at"]
            seed_entries = [
                {"entry_kind": "expense", "category": "TRAE", "currency": "BRL", "amount": 89.90, "description": "Assinatura simulada do ambiente", "occurred_at": (base_date - timedelta(days=2)).isoformat(), "metadata": {"scenario_code": "G"}},
                {"entry_kind": "expense", "category": "Hetzner", "currency": "BRL", "amount": 54.00, "description": "Infraestrutura simulada", "occurred_at": (base_date - timedelta(days=1)).isoformat(), "metadata": {"scenario_code": "G"}},
                {"entry_kind": "revenue", "category": "YouTube", "currency": "BRL", "amount": 210.00, "description": "Receita simulada do canal", "occurred_at": base_date.isoformat(), "metadata": {"scenario_code": "G"}},
                {"entry_kind": "revenue", "category": "Afiliados", "currency": "BRL", "amount": 74.50, "description": "Afiliados simulados", "occurred_at": base_date.isoformat(), "metadata": {"scenario_code": "G"}},
            ]
            for entry in seed_entries:
                self.save_ledger_entry(db, user=user, payload=entry)

        result = {
            "kind": "youtube_story_video",
            "payload": payload,
            "financial_guardian": {
                "source_type": "youtube_auto",
                "estimated_cost": blueprint["estimated_cost"],
                "actual_cost": blueprint["final_actual_cost"],
                "estimated_savings": round(max(0.0, blueprint["estimated_cost"] - blueprint["final_actual_cost"]), 4),
                "simulated": True,
                "scenario_code": blueprint["scenario_code"],
            },
            "simulation": {
                "scenario_code": blueprint["scenario_code"],
                "title": blueprint["scenario_label"],
                "message": blueprint["message"],
                "zero_paid_calls": True,
            },
        }
        self._upsert_simulated_task(
            db,
            user=user,
            task_id=task_id,
            status=blueprint["status"],
            message=blueprint["message"],
            payload=payload,
            result=result,
        )
        db.commit()
        return {
            "task_id": task_id,
            "scenario_code": blueprint["scenario_code"],
            "scenario_label": blueprint["scenario_label"],
            "status": blueprint["status"],
            "message": blueprint["message"],
            "timeline": self.build_timeline(db, user=user, task_id=task_id),
            "no_paid_calls_confirmed": True,
        }


youtube_financial_guardian_observability_service = YouTubeFinancialGuardianObservabilityService()
