import hashlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.services.financial_guardian.adapters import (
    FinancialContext,
    bible_video_financial_adapter,
    youtube_auto_financial_adapter,
)


_AUDIT_TABLE = "codexia_financial_audit_events"
_CACHE_TABLE = "codexia_asset_generation_cache"
_MANIFEST_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "financial_guardian"
_schema_ready = False
_schema_lock = threading.Lock()
_AUDIT_EXTRA_COLUMNS = {
    "source_type": "VARCHAR(64)",
    "context_id": "VARCHAR(128)",
    "scope_key": "VARCHAR(128)",
    "context_json": "TEXT",
}
_CACHE_EXTRA_COLUMNS = {
    "source_type": "VARCHAR(64)",
    "context_id": "VARCHAR(128)",
    "scope_key": "VARCHAR(128)",
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


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return json.dumps({"raw": str(value)}, ensure_ascii=False, sort_keys=True)


def _json_loads(value: Any, default: Any) -> Any:
    try:
        if isinstance(value, str) and value.strip():
            return json.loads(value)
    except Exception:
        pass
    return default


def _serialize_datetime(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    text_value = str(value).strip()
    return text_value or None


def _normalize_context(value: FinancialContext) -> FinancialContext:
    return FinancialContext(
        source_type=str(value.source_type or "generic").strip() or "generic",
        context_id=str(value.context_id or "").strip(),
        user_id=value.user_id,
        title=str(value.title or "").strip(),
        status=str(value.status or "").strip(),
        estimated_cost=round(_safe_float(value.estimated_cost, 0.0), 4),
        actual_cost=round(_safe_float(value.actual_cost, 0.0), 4),
        aspect_ratio=str(value.aspect_ratio or "16:9"),
        created_at=value.created_at,
        metadata=dict(value.metadata or {}),
    )


def _context_payload(context: FinancialContext) -> Dict[str, Any]:
    normalized = _normalize_context(context)
    return {
        "source_type": normalized.source_type,
        "context_id": normalized.context_id,
        "scope_key": normalized.scope_key,
        "user_id": normalized.user_id,
        "title": normalized.title,
        "status": normalized.status,
        "estimated_cost": normalized.estimated_cost,
        "actual_cost": normalized.actual_cost,
        "aspect_ratio": normalized.aspect_ratio,
        "created_at": _serialize_datetime(normalized.created_at),
        "metadata": normalized.metadata,
    }


def _manifest_path() -> Path:
    _MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    return _MANIFEST_DIR / "image_cache_manifest.json"


def _load_manifest() -> List[Dict[str, Any]]:
    path = _manifest_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_manifest(entries: List[Dict[str, Any]]) -> None:
    path = _manifest_path()
    path.write_text(_json_dumps(entries), encoding="utf-8")


def build_image_cache_key(
    *,
    aspect_ratio: str,
    scene_number: int,
    image_prompt: str,
    scene_text: str,
) -> str:
    payload = {
        "aspect_ratio": str(aspect_ratio or "").strip().lower(),
        "scene_number": int(scene_number or 0),
        "image_prompt": str(image_prompt or "").strip(),
        "scene_text": str(scene_text or "").strip(),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluate_budget_guard(
    *,
    estimated_cost: float,
    spent_today: float,
    spent_month: float,
    per_video_limit: float,
    daily_limit: float,
    monthly_limit: float,
) -> Dict[str, Any]:
    estimate = round(max(0.0, _safe_float(estimated_cost, 0.0)), 4)
    today = round(max(0.0, _safe_float(spent_today, 0.0)), 4)
    month = round(max(0.0, _safe_float(spent_month, 0.0)), 4)
    per_video = round(max(0.0, _safe_float(per_video_limit, 0.0)), 4)
    daily = round(max(0.0, _safe_float(daily_limit, 0.0)), 4)
    monthly = round(max(0.0, _safe_float(monthly_limit, 0.0)), 4)

    projected_today = round(today + estimate, 4)
    projected_month = round(month + estimate, 4)
    reasons: List[str] = []

    if per_video > 0 and estimate > per_video:
        reasons.append(f"estimativa por vídeo ({estimate:.4f}) excede limite por vídeo ({per_video:.4f})")
    if daily > 0 and projected_today > daily:
        reasons.append(f"projeção diária ({projected_today:.4f}) excede limite diário ({daily:.4f})")
    if monthly > 0 and projected_month > monthly:
        reasons.append(f"projeção mensal ({projected_month:.4f}) excede limite mensal ({monthly:.4f})")

    allowed = not reasons
    return {
        "allowed": allowed,
        "status": "allowed" if allowed else "blocked",
        "reason": "; ".join(reasons),
        "estimated_cost": estimate,
        "spent_today": today,
        "spent_month": month,
        "projected_today": projected_today,
        "projected_month": projected_month,
        "limits": {
            "per_video": per_video,
            "daily": daily,
            "monthly": monthly,
        },
    }


def evaluate_recovery_loop(
    *,
    stage: str,
    attempt_number: int,
    before_score: float,
    after_score: float,
    min_score_delta: float,
    max_attempts: int,
) -> Dict[str, Any]:
    current_stage = str(stage or "").strip().lower()
    attempt = max(1, _safe_int(attempt_number, 1))
    before = _safe_float(before_score, 0.0)
    after = _safe_float(after_score, 0.0)
    min_delta = max(0.0, _safe_float(min_score_delta, 0.0))
    max_allowed = max(1, _safe_int(max_attempts, 1))
    delta = round(after - before, 4)

    stop = False
    reason = ""
    if delta < min_delta:
        stop = True
        reason = (
            f"etapa {current_stage} melhorou apenas {delta:.4f} ponto(s), "
            f"abaixo do mínimo configurado ({min_delta:.4f})"
        )

    return {
        "stop": stop,
        "reason": reason,
        "score_delta": delta,
        "stage": current_stage,
        "attempt_number": attempt,
        "max_attempts": max_allowed,
        "min_score_delta": min_delta,
    }


class FinancialGuardianService:
    def __init__(self) -> None:
        self._default_sources = {
            "bible_video_factory": bible_video_financial_adapter,
            "youtube_auto": youtube_auto_financial_adapter,
        }

    def _ensure_extra_columns(self, db: Session, table_name: str, column_types: Dict[str, str]) -> None:
        existing = {str(col.get("name") or "").strip().lower() for col in inspect(db.bind).get_columns(table_name)}
        for column_name, column_type in column_types.items():
            if column_name.lower() in existing:
                continue
            db.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type} NULL"))

    def _upsert_manifest_entry(
        self,
        *,
        context: FinancialContext,
        asset_kind: str,
        cache_key: str,
        file_hash: Optional[str],
        file_path: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        entries = _load_manifest()
        now = _utcnow().isoformat()
        normalized_path = os.path.abspath(file_path)
        payload = {
            "user_id": context.user_id,
            "source_type": context.source_type,
            "context_id": context.context_id,
            "scope_key": context.scope_key,
            "asset_kind": str(asset_kind or "").strip() or "image",
            "cache_key": str(cache_key or "").strip(),
            "file_hash": str(file_hash or "").strip() or None,
            "file_path": normalized_path,
            "meta": meta or {},
            "last_used_at": now,
            "created_at": now,
            "updated_at": now,
        }
        updated = False
        for idx, entry in enumerate(entries):
            if (
                entry.get("scope_key") == payload["scope_key"]
                and entry.get("asset_kind") == payload["asset_kind"]
                and entry.get("cache_key") == payload["cache_key"]
            ):
                payload["created_at"] = entry.get("created_at") or now
                entries[idx] = payload
                updated = True
                break
        if not updated:
            entries.append(payload)
        _save_manifest(entries)
        return payload

    def _find_manifest_entry(
        self,
        *,
        user_id: Optional[int],
        asset_kind: str,
        cache_key: str,
        source_type: Optional[str] = None,
        scope_key: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        effective_scope_key = str(scope_key or "").strip()
        if not effective_scope_key:
            if user_id is not None:
                effective_scope_key = f"user:{int(user_id)}"
            elif source_type:
                effective_scope_key = f"source:{str(source_type).strip()}"
        for entry in reversed(_load_manifest()):
            entry_scope_key = str(entry.get("scope_key") or "").strip()
            if not entry_scope_key and entry.get("user_id") is not None:
                entry_scope_key = f"user:{int(entry.get('user_id'))}"
            if (
                entry_scope_key == effective_scope_key
                and entry.get("asset_kind") == asset_kind
                and entry.get("cache_key") == cache_key
            ):
                file_path = str(entry.get("file_path") or "").strip()
                if file_path and os.path.exists(file_path):
                    return entry
        return None

    def ensure_schema(self, db: Session) -> None:
        global _schema_ready
        if _schema_ready:
            return
        with _schema_lock:
            if _schema_ready:
                return
            inspector = inspect(db.bind)
            tables = set(inspector.get_table_names())
            dialect = (getattr(getattr(db.bind, "dialect", None), "name", "") or "").lower()
            is_postgres = dialect in {"postgres", "postgresql"}
            pk_type = "SERIAL PRIMARY KEY" if is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
            timestamp_type = "TIMESTAMP" if is_postgres else "DATETIME"

            if _AUDIT_TABLE not in tables:
                db.execute(text(
                    f"""
                    CREATE TABLE {_AUDIT_TABLE} (
                        id {pk_type},
                        user_id INTEGER NULL,
                        job_id INTEGER NULL,
                        source_type VARCHAR(64) NULL,
                        context_id VARCHAR(128) NULL,
                        scope_key VARCHAR(128) NULL,
                        event_type VARCHAR(64) NOT NULL,
                        stage VARCHAR(64) NULL,
                        severity VARCHAR(32) NULL,
                        estimated_cost DOUBLE PRECISION NULL,
                        actual_cost DOUBLE PRECISION NULL,
                        context_json TEXT NULL,
                        details_json TEXT NULL,
                        created_at {timestamp_type} NOT NULL
                    )
                    """
                ))
            if _CACHE_TABLE not in tables:
                db.execute(text(
                    f"""
                    CREATE TABLE {_CACHE_TABLE} (
                        id {pk_type},
                        user_id INTEGER NULL,
                        job_id INTEGER NULL,
                        source_type VARCHAR(64) NULL,
                        context_id VARCHAR(128) NULL,
                        scope_key VARCHAR(128) NULL,
                        asset_kind VARCHAR(32) NOT NULL,
                        cache_key VARCHAR(64) NOT NULL,
                        file_hash VARCHAR(64) NULL,
                        file_path TEXT NULL,
                        hit_count INTEGER NOT NULL DEFAULT 0,
                        last_used_at {timestamp_type} NULL,
                        created_at {timestamp_type} NOT NULL,
                        updated_at {timestamp_type} NOT NULL,
                        meta_json TEXT NULL
                    )
                    """
                ))
            db.commit()
            self._ensure_extra_columns(db, _AUDIT_TABLE, _AUDIT_EXTRA_COLUMNS)
            self._ensure_extra_columns(db, _CACHE_TABLE, _CACHE_EXTRA_COLUMNS)
            db.commit()
            _schema_ready = True

    def _job_cost_snapshot(self, db: Session, *, user_id: Optional[int], adapter: Any) -> Dict[str, float]:
        now = _utcnow()
        day_start = datetime(now.year, now.month, now.day)
        month_start = datetime(now.year, now.month, 1)
        if adapter and hasattr(adapter, "cost_snapshot"):
            return adapter.cost_snapshot(db, user_id=user_id, day_start=day_start, month_start=month_start)
        return {"spent_today": 0.0, "spent_month": 0.0}

    def record_context_event(
        self,
        db: Session,
        *,
        context: FinancialContext,
        event_type: str,
        stage: Optional[str] = None,
        severity: str = "info",
        estimated_cost: Optional[float] = None,
        actual_cost: Optional[float] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self.ensure_schema(db)
        now = _utcnow()
        normalized = _normalize_context(context)
        effective_estimated = normalized.estimated_cost if estimated_cost is None else _safe_float(estimated_cost, 0.0)
        effective_actual = normalized.actual_cost if actual_cost is None else _safe_float(actual_cost, 0.0)
        payload = {
            "user_id": normalized.user_id,
            "job_id": _safe_int(normalized.metadata.get("job_id"), 0) or None,
            "source_type": normalized.source_type,
            "context_id": normalized.context_id or None,
            "scope_key": normalized.scope_key,
            "event_type": str(event_type or "").strip() or "event",
            "stage": str(stage or "").strip() or None,
            "severity": str(severity or "info").strip() or "info",
            "estimated_cost": round(_safe_float(effective_estimated, 0.0), 4),
            "actual_cost": round(_safe_float(effective_actual, 0.0), 4),
            "context_json": _json_dumps(_context_payload(normalized)),
            "details_json": _json_dumps(details or {}),
            "created_at": now,
        }
        db.execute(text(
            f"""
            INSERT INTO {_AUDIT_TABLE} (
                user_id, job_id, source_type, context_id, scope_key, event_type, stage, severity,
                estimated_cost, actual_cost, context_json, details_json, created_at
            ) VALUES (
                :user_id, :job_id, :source_type, :context_id, :scope_key, :event_type, :stage, :severity,
                :estimated_cost, :actual_cost, :context_json, :details_json, :created_at
            )
            """
        ), payload)
        return {
            "event_type": payload["event_type"],
            "stage": payload["stage"],
            "severity": payload["severity"],
            "details": details or {},
            "context": _context_payload(normalized),
            "created_at": now.isoformat(),
        }

    def record_event(
        self,
        db: Session,
        *,
        user_id: Optional[int],
        job_id: Optional[int],
        event_type: str,
        stage: Optional[str] = None,
        severity: str = "info",
        estimated_cost: Optional[float] = None,
        actual_cost: Optional[float] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        context = FinancialContext(
            source_type="bible_video_factory",
            context_id=str(job_id or ""),
            user_id=user_id,
            estimated_cost=_safe_float(estimated_cost, 0.0),
            actual_cost=_safe_float(actual_cost, 0.0),
            metadata={"job_id": job_id},
        )
        return self.record_context_event(
            db,
            context=context,
            event_type=event_type,
            stage=stage,
            severity=severity,
            estimated_cost=estimated_cost,
            actual_cost=actual_cost,
            details=details,
        )

    def evaluate_context_preflight(
        self,
        db: Session,
        *,
        context: FinancialContext,
        config: Any,
        adapter: Optional[Any] = None,
    ) -> Dict[str, Any]:
        self.ensure_schema(db)
        normalized = _normalize_context(context)
        snapshot = self._job_cost_snapshot(db, user_id=normalized.user_id, adapter=adapter)
        decision = evaluate_budget_guard(
            estimated_cost=normalized.estimated_cost,
            spent_today=snapshot["spent_today"],
            spent_month=snapshot["spent_month"],
            per_video_limit=_safe_float(getattr(config, "per_video_spend_limit", 0.0), 0.0),
            daily_limit=_safe_float(getattr(config, "daily_spend_limit", 0.0), 0.0),
            monthly_limit=_safe_float(getattr(config, "monthly_spend_limit", 0.0), 0.0),
        )
        self.record_context_event(
            db,
            context=normalized,
            event_type="preflight_allowed" if decision["allowed"] else "preflight_blocked",
            stage="preflight",
            severity="info" if decision["allowed"] else "warning",
            estimated_cost=normalized.estimated_cost,
            actual_cost=normalized.actual_cost,
            details=decision,
        )
        return decision

    def evaluate_job_preflight(self, db: Session, *, job: Any, config: Any) -> Dict[str, Any]:
        context = bible_video_financial_adapter.build_context(job)
        return self.evaluate_context_preflight(db, context=context, config=config, adapter=bible_video_financial_adapter)

    def hydrate_plan_with_cached_images_for_context(
        self,
        db: Session,
        *,
        context: FinancialContext,
        plan: Dict[str, Any],
    ) -> Dict[str, Any]:
        self.ensure_schema(db)
        normalized = _normalize_context(context)
        updated = dict(plan or {})
        scenes = updated.get("scenes") if isinstance(updated.get("scenes"), list) else []
        selected_images: List[str] = []
        hits: List[Dict[str, Any]] = []

        for idx, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                continue
            cache_key = build_image_cache_key(
                aspect_ratio=str(normalized.aspect_ratio or "16:9"),
                scene_number=idx + 1,
                image_prompt=str(scene.get("image_prompt") or ""),
                scene_text=str(scene.get("text") or ""),
            )
            row = db.execute(text(
                f"""
                SELECT id, file_path, hit_count, meta_json
                FROM {_CACHE_TABLE}
                WHERE scope_key = :scope_key
                  AND asset_kind = 'image'
                  AND cache_key = :cache_key
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ), {"scope_key": normalized.scope_key, "cache_key": cache_key}).mappings().first()
            row_id = row.get("id") if row else None
            file_path = str((row or {}).get("file_path") or "").strip()
            meta = _json_loads((row or {}).get("meta_json"), {})
            hit_count = _safe_int((row or {}).get("hit_count"), 0)
            if not file_path:
                manifest_entry = self._find_manifest_entry(
                    user_id=normalized.user_id,
                    source_type=normalized.source_type,
                    scope_key=normalized.scope_key,
                    asset_kind="image",
                    cache_key=cache_key,
                )
                if manifest_entry:
                    file_path = str(manifest_entry.get("file_path") or "").strip()
                    meta = manifest_entry.get("meta") or {}
            if not file_path or not os.path.exists(file_path):
                continue
            selected_images.append(file_path)
            hit_count += 1
            if row_id is not None:
                now = _utcnow()
                db.execute(text(
                    f"""
                    UPDATE {_CACHE_TABLE}
                    SET hit_count = :hit_count,
                        last_used_at = :last_used_at,
                        updated_at = :updated_at
                    WHERE id = :id
                    """
                ), {
                    "id": row_id,
                    "hit_count": hit_count,
                    "last_used_at": now,
                    "updated_at": now,
                })
            hits.append(
                {
                    "scene_number": idx + 1,
                    "file_path": file_path,
                    "cache_key": cache_key,
                    "meta": meta,
                }
            )

        if selected_images:
            updated["selected_images"] = selected_images
            updated["allow_image_reuse"] = True
            guardian = updated.get("financial_guardian") if isinstance(updated.get("financial_guardian"), dict) else {}
            guardian["image_cache_hits"] = hits
            guardian["selected_images_reused"] = len(selected_images)
            updated["financial_guardian"] = guardian
            estimated_savings = round(
                len(selected_images) * normalized.estimated_cost / max(1, len(scenes)),
                4,
            )
            self.record_context_event(
                db,
                context=normalized,
                event_type="image_cache_applied",
                stage="images_generated",
                estimated_cost=normalized.estimated_cost,
                actual_cost=normalized.actual_cost,
                details={
                    "cache_hits": len(selected_images),
                    "selected_images": selected_images,
                    "estimated_savings": estimated_savings,
                },
            )
        return updated

    def hydrate_plan_with_cached_images(self, db: Session, *, job: Any, plan: Dict[str, Any]) -> Dict[str, Any]:
        return self.hydrate_plan_with_cached_images_for_context(
            db,
            context=bible_video_financial_adapter.build_context(job),
            plan=plan,
        )

    def cache_images_from_context_result(
        self,
        db: Session,
        *,
        context: FinancialContext,
        plan: Dict[str, Any],
        image_paths: List[str],
    ) -> Dict[str, Any]:
        self.ensure_schema(db)
        normalized = _normalize_context(context)
        scenes = plan.get("scenes") if isinstance(plan.get("scenes"), list) else []
        stored = 0
        cache_keys: List[str] = []
        now = _utcnow()

        for idx, file_path in enumerate(image_paths):
            if idx >= len(scenes) or not os.path.exists(file_path):
                continue
            scene = scenes[idx] if isinstance(scenes[idx], dict) else {}
            cache_key = build_image_cache_key(
                aspect_ratio=str(normalized.aspect_ratio or "16:9"),
                scene_number=idx + 1,
                image_prompt=str(scene.get("image_prompt") or ""),
                scene_text=str(scene.get("text") or ""),
            )
            with open(file_path, "rb") as file_obj:
                file_hash = hashlib.sha256(file_obj.read()).hexdigest()
            meta = {
                "scene_number": idx + 1,
                "image_prompt": scene.get("image_prompt"),
                "scene_text": scene.get("text"),
            }
            self._upsert_manifest_entry(
                context=normalized,
                asset_kind="image",
                cache_key=cache_key,
                file_hash=file_hash,
                file_path=file_path,
                meta=meta,
            )
            db.execute(text(
                f"""
                INSERT INTO {_CACHE_TABLE} (
                    user_id, job_id, source_type, context_id, scope_key, asset_kind, cache_key, file_hash, file_path,
                    hit_count, last_used_at, created_at, updated_at, meta_json
                ) VALUES (
                    :user_id, :job_id, :source_type, :context_id, :scope_key, 'image', :cache_key, :file_hash, :file_path,
                    :hit_count, :last_used_at, :created_at, :updated_at, :meta_json
                )
                """
            ), {
                "user_id": normalized.user_id,
                "job_id": _safe_int(normalized.metadata.get("job_id"), 0) or None,
                "source_type": normalized.source_type,
                "context_id": normalized.context_id or None,
                "scope_key": normalized.scope_key,
                "cache_key": cache_key,
                "file_hash": file_hash,
                "file_path": file_path,
                "hit_count": 0,
                "last_used_at": now,
                "created_at": now,
                "updated_at": now,
                "meta_json": _json_dumps(meta),
            })
            stored += 1
            cache_keys.append(cache_key)

        self.record_context_event(
            db,
            context=normalized,
            event_type="image_cache_stored",
            stage="images_generated",
            estimated_cost=normalized.estimated_cost,
            actual_cost=normalized.actual_cost,
            details={"stored_assets": stored, "cache_keys": cache_keys},
        )
        return {"stored_assets": stored, "cache_keys": cache_keys}

    def cache_images_from_result(self, db: Session, *, job: Any, plan: Dict[str, Any], image_paths: List[str]) -> Dict[str, Any]:
        return self.cache_images_from_context_result(
            db,
            context=bible_video_financial_adapter.build_context(job),
            plan=plan,
            image_paths=image_paths,
        )

    def get_context_audit(self, db: Session, *, source_type: str, context_id: str) -> Dict[str, Any]:
        self.ensure_schema(db)
        rows = db.execute(text(
            f"""
            SELECT id, event_type, stage, severity, estimated_cost, actual_cost, context_json, details_json, created_at
            FROM {_AUDIT_TABLE}
            WHERE context_id = :context_id
              AND (source_type = :source_type OR (source_type IS NULL AND :source_type = 'bible_video_factory'))
            ORDER BY created_at ASC, id ASC
            """
        ), {"context_id": str(context_id or ""), "source_type": str(source_type or "")}).mappings().all()
        events = []
        for row in rows:
            events.append(
                {
                    "id": row.get("id"),
                    "event_type": row.get("event_type"),
                    "stage": row.get("stage"),
                    "severity": row.get("severity"),
                    "estimated_cost": _safe_float(row.get("estimated_cost"), 0.0),
                    "actual_cost": _safe_float(row.get("actual_cost"), 0.0),
                    "context": _json_loads(row.get("context_json"), {}),
                    "details": _json_loads(row.get("details_json"), {}),
                    "created_at": _serialize_datetime(row.get("created_at")),
                }
            )
        return {"source_type": source_type, "context_id": str(context_id or ""), "events": events}

    def get_job_audit(self, db: Session, job_id: int) -> Dict[str, Any]:
        audit = self.get_context_audit(db, source_type="bible_video_factory", context_id=str(job_id))
        return {"job_id": job_id, "events": audit["events"]}

    def build_context_financial_report(
        self,
        db: Session,
        *,
        source_type: str,
        context_id: str,
        adapter: Optional[Any] = None,
    ) -> Dict[str, Any]:
        self.ensure_schema(db)
        audit = self.get_context_audit(db, source_type=source_type, context_id=context_id)
        events = audit.get("events") or []
        latest_context = {}
        for event in reversed(events):
            if isinstance(event.get("context"), dict) and event.get("context"):
                latest_context = event["context"]
                break
        loaded_context = adapter.load_context(db, str(context_id)) if adapter and hasattr(adapter, "load_context") else None
        context_payload = _context_payload(loaded_context) if loaded_context else latest_context
        blocked = any(event.get("event_type") == "preflight_blocked" for event in events)
        cache_hits = sum(_safe_int((event.get("details") or {}).get("cache_hits"), 0) for event in events)
        estimated_savings = round(sum(_safe_float((event.get("details") or {}).get("estimated_savings"), 0.0) for event in events), 4)
        recovery_loops_blocked = sum(1 for event in events if event.get("event_type") == "recovery_loop_blocked")
        return {
            "source_type": source_type,
            "context_id": str(context_id or ""),
            "found": bool(events or loaded_context),
            "title": context_payload.get("title"),
            "status": context_payload.get("status"),
            "estimated_cost": round(_safe_float(context_payload.get("estimated_cost"), 0.0), 4),
            "actual_cost": round(_safe_float(context_payload.get("actual_cost"), 0.0), 4),
            "cost_delta": round(
                _safe_float(context_payload.get("actual_cost"), 0.0) - _safe_float(context_payload.get("estimated_cost"), 0.0),
                4,
            ),
            "blocked_by_budget": blocked,
            "cache_hits": cache_hits,
            "estimated_savings": estimated_savings,
            "recovery_loops_blocked": recovery_loops_blocked,
            "audit_event_count": len(events),
            "events": events,
        }

    def build_job_financial_report(self, db: Session, *, job_id: int) -> Dict[str, Any]:
        report = self.build_context_financial_report(
            db,
            source_type="bible_video_factory",
            context_id=str(job_id),
            adapter=bible_video_financial_adapter,
        )
        report["job_id"] = job_id
        return report

    def build_daily_financial_report_for_source(
        self,
        db: Session,
        *,
        source_type: str,
        user_id: Optional[int],
        day: Optional[datetime] = None,
        adapter: Optional[Any] = None,
    ) -> Dict[str, Any]:
        self.ensure_schema(db)
        reference = day or _utcnow()
        day_start = datetime(reference.year, reference.month, reference.day)
        day_end = day_start + timedelta(days=1)
        events = db.execute(text(
            f"""
            SELECT event_type, details_json, estimated_cost, actual_cost, context_id
            FROM {_AUDIT_TABLE}
            WHERE created_at >= :day_start
              AND created_at < :day_end
              AND (user_id = :user_id OR (:user_id IS NULL AND user_id IS NULL))
              AND (source_type = :source_type OR (source_type IS NULL AND :source_type = 'bible_video_factory'))
            ORDER BY created_at ASC, id ASC
            """
        ), {
            "day_start": day_start,
            "day_end": day_end,
            "user_id": user_id,
            "source_type": source_type,
        }).mappings().all()
        jobs = adapter.list_contexts_for_day(db, user_id=user_id, day_start=day_start, day_end=day_end) if adapter and hasattr(adapter, "list_contexts_for_day") else []
        estimated_total = round(sum(_safe_float(getattr(job, "estimated_cost", 0.0), 0.0) for job in jobs), 4) if jobs else round(sum(_safe_float(row.get("estimated_cost"), 0.0) for row in events if str(row.get("event_type") or "") == "preflight_allowed"), 4)
        actual_total = round(sum(_safe_float(getattr(job, "actual_cost", 0.0), 0.0) for job in jobs), 4) if jobs else round(sum(_safe_float(row.get("actual_cost"), 0.0) for row in events), 4)
        blocked_jobs = 0
        cache_hits = 0
        estimated_savings = 0.0
        context_ids = set()
        for row in events:
            event_type = str(row.get("event_type") or "").strip()
            details = _json_loads(row.get("details_json"), {})
            if event_type == "preflight_blocked":
                blocked_jobs += 1
            cache_hits += _safe_int(details.get("cache_hits"), 0)
            estimated_savings += _safe_float(details.get("estimated_savings"), 0.0)
            if row.get("context_id"):
                context_ids.add(str(row.get("context_id")))
        return {
            "source_type": source_type,
            "user_id": user_id,
            "day": day_start.date().isoformat(),
            "jobs_count": len(jobs) if jobs else len(context_ids),
            "estimated_cost_total": estimated_total,
            "actual_cost_total": actual_total,
            "blocked_jobs": blocked_jobs,
            "cache_hits": cache_hits,
            "estimated_savings": round(estimated_savings, 4),
            "event_count": len(events),
        }

    def build_daily_financial_report(
        self,
        db: Session,
        *,
        user_id: Optional[int],
        day: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        return self.build_daily_financial_report_for_source(
            db,
            source_type="bible_video_factory",
            user_id=user_id,
            day=day,
            adapter=bible_video_financial_adapter,
        )

    def build_user_dashboard_for_source(
        self,
        db: Session,
        *,
        source_type: str,
        user_id: Optional[int],
        adapter: Optional[Any] = None,
    ) -> Dict[str, Any]:
        self.ensure_schema(db)
        audit_rows = db.execute(text(
            f"""
            SELECT event_type, details_json
            FROM {_AUDIT_TABLE}
            WHERE (user_id = :user_id OR (:user_id IS NULL AND user_id IS NULL))
              AND (source_type = :source_type OR (source_type IS NULL AND :source_type = 'bible_video_factory'))
            ORDER BY created_at DESC
            LIMIT 500
            """
        ), {"user_id": user_id, "source_type": source_type}).mappings().all()

        event_counts: Dict[str, int] = {}
        estimated_savings = 0.0
        blocked_jobs = 0
        recovery_stops = 0
        cache_hits = 0
        for row in audit_rows:
            event_type = str(row.get("event_type") or "").strip()
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
            details = _json_loads(row.get("details_json"), {})
            estimated_savings += _safe_float(details.get("estimated_savings"), 0.0)
            cache_hits += _safe_int(details.get("cache_hits"), 0)
            if event_type == "preflight_blocked":
                blocked_jobs += 1
            if event_type == "recovery_loop_blocked":
                recovery_stops += 1

        cache_stats = db.execute(text(
            f"""
            SELECT
                COALESCE(COUNT(*), 0) AS total_assets,
                COALESCE(SUM(hit_count), 0) AS total_hits
            FROM {_CACHE_TABLE}
            WHERE (user_id = :user_id OR (:user_id IS NULL AND user_id IS NULL))
              AND asset_kind = 'image'
              AND (source_type = :source_type OR (source_type IS NULL AND :source_type = 'bible_video_factory'))
            """
        ), {"user_id": user_id, "source_type": source_type}).mappings().first() or {}

        adapter_metrics = adapter.build_dashboard_metrics(db, user_id=user_id) if adapter and hasattr(adapter, "build_dashboard_metrics") else {}
        roi = adapter_metrics.get("roi") if isinstance(adapter_metrics.get("roi"), dict) else {
            "actual_cost_total": 0.0,
            "views_total": 0,
            "subscribers_total": 0,
            "roi_proxy": 0.0,
            "cost_per_1000_views": 0.0,
        }
        operations = adapter_metrics.get("operations") if isinstance(adapter_metrics.get("operations"), dict) else {}
        return {
            "source_type": source_type,
            "finance": {
                "blocked_jobs": blocked_jobs,
                "preflight_passed": event_counts.get("preflight_allowed", 0),
                "preflight_blocked": event_counts.get("preflight_blocked", 0),
            },
            "efficiency": {
                "image_cache_assets": _safe_int(cache_stats.get("total_assets"), 0),
                "image_cache_hits": max(cache_hits, _safe_int(cache_stats.get("total_hits"), 0)),
                "recovery_loops_blocked": recovery_stops,
                "estimated_savings": round(estimated_savings, 4),
            },
            "roi": roi,
            "operations": operations,
            "audit": {
                "recent_event_counts": event_counts,
            },
        }

    def build_user_dashboard(self, db: Session, *, user_id: Optional[int]) -> Dict[str, Any]:
        return self.build_user_dashboard_for_source(
            db,
            source_type="bible_video_factory",
            user_id=user_id,
            adapter=bible_video_financial_adapter,
        )

    def build_admin_dashboard(self, db: Session) -> Dict[str, Any]:
        self.ensure_schema(db)
        rows = db.execute(text(
            f"""
            SELECT user_id, source_type, event_type, created_at, details_json
            FROM {_AUDIT_TABLE}
            ORDER BY created_at DESC
            LIMIT 1000
            """
        )).mappings().all()
        preflight_blocked = 0
        recovery_blocked = 0
        estimated_savings = 0.0
        active_users = set()
        by_source: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            active_users.add(row.get("user_id"))
            event_type = str(row.get("event_type") or "").strip()
            source_type = str(row.get("source_type") or "bible_video_factory").strip() or "bible_video_factory"
            details = _json_loads(row.get("details_json"), {})
            estimated_savings += _safe_float(details.get("estimated_savings"), 0.0)
            source_bucket = by_source.setdefault(source_type, {
                "events": 0,
                "preflight_blocked": 0,
                "recovery_loops_blocked": 0,
                "estimated_savings": 0.0,
            })
            source_bucket["events"] += 1
            source_bucket["estimated_savings"] = round(source_bucket["estimated_savings"] + _safe_float(details.get("estimated_savings"), 0.0), 4)
            if event_type == "preflight_blocked":
                preflight_blocked += 1
                source_bucket["preflight_blocked"] += 1
            if event_type == "recovery_loop_blocked":
                recovery_blocked += 1
                source_bucket["recovery_loops_blocked"] += 1
        return {
            "summary": {
                "active_users_with_events": len([item for item in active_users if item is not None]),
                "preflight_blocked": preflight_blocked,
                "recovery_loops_blocked": recovery_blocked,
                "estimated_savings": round(estimated_savings, 4),
            },
            "by_source": by_source,
            "recent_events": [
                {
                    "user_id": row.get("user_id"),
                    "source_type": row.get("source_type") or "bible_video_factory",
                    "event_type": row.get("event_type"),
                    "created_at": _serialize_datetime(row.get("created_at")),
                    "details": _json_loads(row.get("details_json"), {}),
                }
                for row in rows[:30]
            ],
        }


financial_guardian_service = FinancialGuardianService()
