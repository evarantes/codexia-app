from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import UnifiedVideo, User
from app.routers.auth import get_current_admin_user
from app.services.video_cost_estimator import estimate_video_cost, normalize_mode


router = APIRouter(prefix="/video-costs", tags=["video-costs"])


class VideoCostEstimateRequest(BaseModel):
    duration_minutes: float = Field(default=2.0, ge=0.25, le=60.0)
    mode: str = "balanced"
    regeneration_rate: float = Field(default=0.10, ge=0.0, le=1.0)
    projection_minutes: List[float] = Field(default_factory=lambda: [2.0, 5.0, 10.0, 15.0])


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def get_usd_brl_rate() -> float:
    rate = _safe_float(os.getenv("CODEXIA_USD_BRL"), 5.20)
    return round(max(0.01, rate), 6)


def _brl(value_usd: Any, rate: float) -> float:
    return round(max(0.0, _safe_float(value_usd)) * rate, 2)


def _mode_label(mode: str) -> str:
    return {
        "economy": "Econômico",
        "balanced": "Equilibrado",
        "premium": "Qualidade Máxima",
    }.get(normalize_mode(mode), "Equilibrado")


def _quality_label(quality: str) -> str:
    return {
        "low": "Baixa",
        "medium": "Média",
        "high": "Alta",
        "auto": "Automática",
    }.get(str(quality or "").lower(), str(quality or "-").title())


def build_cost_estimate_payload(
    duration_minutes: float,
    *,
    mode: str = "balanced",
    regeneration_rate: float = 0.10,
    projection_minutes: Optional[List[float]] = None,
) -> Dict[str, Any]:
    normalized_mode = normalize_mode(mode)
    rate = get_usd_brl_rate()
    estimate = estimate_video_cost(
        duration_minutes,
        mode=normalized_mode,
        regeneration_rate=regeneration_rate,
    )
    projections: List[Dict[str, Any]] = []
    targets = projection_minutes or [2.0, 5.0, 10.0, 15.0]
    seen = set()
    for raw in targets:
        minutes = round(max(0.25, min(60.0, _safe_float(raw, 0.0))), 3)
        if minutes in seen:
            continue
        seen.add(minutes)
        projected = estimate_video_cost(
            minutes,
            mode=normalized_mode,
            regeneration_rate=regeneration_rate,
        )
        projections.append(
            {
                "duration_minutes": minutes,
                "total_cost_usd": projected.total_cost_usd,
                "total_cost_brl": _brl(projected.total_cost_usd, rate),
                "estimated_images": projected.estimated_images,
                "estimated_regenerations": projected.estimated_regenerations,
            }
        )

    data = estimate.to_dict()
    data.update(
        {
            "mode_label": _mode_label(normalized_mode),
            "image_quality_label": _quality_label(estimate.image_quality),
            "total_cost_brl": _brl(estimate.total_cost_usd, rate),
            "variable_cost_brl": _brl(estimate.variable_cost_usd, rate),
            "fixed_cost_brl": _brl(estimate.fixed_cost_usd, rate),
            "image_unit_cost_brl": _brl(estimate.image_unit_cost_usd, rate),
            "cost_per_minute_brl": _brl(estimate.cost_per_minute_usd, rate),
            "usd_brl": rate,
            "fx_source": "CODEXIA_USD_BRL" if os.getenv("CODEXIA_USD_BRL") else "default_reference",
            "projections": projections,
            "is_invoice": False,
            "notice": "Estimativa preventiva. A cobrança oficial do provedor pode variar.",
        }
    )
    return data


def _parse_json(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _measure_task_cost(db: Session, task_id: str) -> Dict[str, Any]:
    task_key = str(task_id or "").strip()
    if not task_key:
        return {
            "available": False,
            "total_cost_usd": 0.0,
            "breakdown": {},
            "source": "unavailable",
        }

    try:
        tables = set(inspect(db.bind).get_table_names())
    except Exception:
        tables = set()
    if "codexia_financial_audit_events" not in tables:
        return {
            "available": False,
            "total_cost_usd": 0.0,
            "breakdown": {},
            "source": "unavailable",
        }

    try:
        rows = db.execute(
            text(
                """
                SELECT id, estimated_cost, actual_cost, details_json, created_at
                FROM codexia_financial_audit_events
                WHERE context_id = :task_id AND event_type = 'AI_OPERATION'
                ORDER BY created_at ASC, id ASC
                """
            ),
            {"task_id": task_key},
        ).mappings().all()
    except Exception:
        return {
            "available": False,
            "total_cost_usd": 0.0,
            "breakdown": {},
            "source": "unavailable",
        }

    total = 0.0
    breakdown: Dict[str, float] = {}
    operation_count = 0
    provider_reported_count = 0
    usage_estimated_count = 0
    seen_operations = set()
    for row in rows:
        details = _parse_json(row.get("details_json"))
        if bool(details.get("cache_hit")):
            continue
        operation_id = str(details.get("operation_id") or f"audit:{row.get('id')}")
        if operation_id in seen_operations:
            continue
        seen_operations.add(operation_id)

        actual = max(0.0, _safe_float(row.get("actual_cost"), 0.0))
        estimated = max(0.0, _safe_float(row.get("estimated_cost"), 0.0))
        amount = actual if actual > 0 else estimated
        if amount <= 0:
            continue

        operation_count += 1
        if actual > 0:
            provider_reported_count += 1
        else:
            usage_estimated_count += 1
        capability = str(details.get("capability") or "other").strip().lower() or "other"
        breakdown[capability] = round(breakdown.get(capability, 0.0) + amount, 6)
        total += amount

    if total <= 0:
        return {
            "available": False,
            "total_cost_usd": 0.0,
            "breakdown": {},
            "source": "unavailable",
            "operation_count": 0,
        }

    source = "provider_reported" if usage_estimated_count == 0 and provider_reported_count > 0 else "usage_based_estimate"
    return {
        "available": True,
        "total_cost_usd": round(total, 6),
        "breakdown": breakdown,
        "source": source,
        "operation_count": operation_count,
        "provider_reported_operations": provider_reported_count,
        "usage_estimated_operations": usage_estimated_count,
    }


def _video_row_payload(db: Session, uv: UnifiedVideo, rate: float) -> Dict[str, Any]:
    task_id = str(getattr(uv, "task_id", None) or "")
    measured = _measure_task_cost(db, task_id)
    stored_actual = max(0.0, _safe_float(getattr(uv, "actual_cost", 0.0), 0.0))
    actual_usd = stored_actual if stored_actual > 0 else _safe_float(measured.get("total_cost_usd"), 0.0)
    cost_source = "stored_actual" if stored_actual > 0 else str(measured.get("source") or "unavailable")
    seconds = max(0.0, _safe_float(getattr(uv, "video_duration_seconds", 0.0), 0.0))
    minutes = seconds / 60.0 if seconds > 0 else max(0.0, _safe_float(getattr(uv, "duration_minutes", 0.0), 0.0))
    per_minute = actual_usd / minutes if actual_usd > 0 and minutes > 0 else 0.0
    result_json = _parse_json(getattr(uv, "result_json", None))
    request_payload = result_json.get("request") if isinstance(result_json.get("request"), dict) else {}
    legacy_payload = request_payload.get("legacy_payload") if isinstance(request_payload.get("legacy_payload"), dict) else {}
    production_mode = normalize_mode(
        legacy_payload.get("production_mode")
        or request_payload.get("production_mode")
        or "balanced"
    )

    return {
        "task_id": task_id,
        "unified_video_id": getattr(uv, "id", None),
        "status": str(getattr(uv, "status", "") or ""),
        "title": str(getattr(uv, "title", "") or request_payload.get("topic") or "")[:160],
        "production_mode": production_mode,
        "mode_label": _mode_label(production_mode),
        "image_provider": getattr(uv, "image_provider", None),
        "image_model": getattr(uv, "image_model", None),
        "estimated_cost_usd": round(max(0.0, _safe_float(getattr(uv, "estimated_cost", 0.0), 0.0)), 6),
        "estimated_cost_brl": _brl(getattr(uv, "estimated_cost", 0.0), rate),
        "actual_cost_available": bool(actual_usd > 0),
        "actual_cost_usd": round(actual_usd, 6),
        "actual_cost_brl": _brl(actual_usd, rate),
        "cost_source": cost_source,
        "cost_breakdown_usd": measured.get("breakdown") or {},
        "operation_count": int(measured.get("operation_count") or 0),
        "duration_minutes": round(minutes, 3),
        "cost_per_minute_usd": round(per_minute, 6),
        "cost_per_minute_brl": _brl(per_minute, rate),
        "call_count_text": int(getattr(uv, "call_count_text", 0) or 0),
        "call_count_image": int(getattr(uv, "call_count_image", 0) or 0),
        "call_count_audio": int(getattr(uv, "call_count_audio", 0) or 0),
        "youtube_video_id": getattr(uv, "youtube_video_id", None),
        "created_at": getattr(uv, "created_at", None).isoformat() if getattr(uv, "created_at", None) else None,
        "billing_note": "Uso calculado a partir das operações registradas; a fatura oficial da OpenAI pode variar.",
    }


@router.post("/estimate")
def estimate_cost(
    request: VideoCostEstimateRequest,
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    return build_cost_estimate_payload(
        request.duration_minutes,
        mode=request.mode,
        regeneration_rate=request.regeneration_rate,
        projection_minutes=request.projection_minutes,
    )


@router.get("/task/{task_id}")
def task_cost(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    query = db.query(UnifiedVideo).filter(UnifiedVideo.task_id == str(task_id))
    user_id = getattr(current_user, "id", None) if current_user else None
    if user_id is not None:
        query = query.filter(UnifiedVideo.user_id == int(user_id))
    uv = query.order_by(UnifiedVideo.id.desc()).first()
    if uv is None:
        raise HTTPException(status_code=404, detail="Produção não encontrada.")
    rate = get_usd_brl_rate()
    return {"usd_brl": rate, "item": _video_row_payload(db, uv, rate)}


@router.get("/history")
def cost_history(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    query = db.query(UnifiedVideo)
    user_id = getattr(current_user, "id", None) if current_user else None
    if user_id is not None:
        query = query.filter(UnifiedVideo.user_id == int(user_id))
    rows = query.order_by(UnifiedVideo.created_at.desc(), UnifiedVideo.id.desc()).limit(limit).all()
    rate = get_usd_brl_rate()
    items = [_video_row_payload(db, row, rate) for row in rows]

    measurable = [item for item in items if item.get("actual_cost_available") and _safe_float(item.get("duration_minutes")) > 0]
    avg_per_min_usd = 0.0
    if measurable:
        total_cost = sum(_safe_float(item.get("actual_cost_usd")) for item in measurable)
        total_minutes = sum(_safe_float(item.get("duration_minutes")) for item in measurable)
        if total_minutes > 0:
            avg_per_min_usd = total_cost / total_minutes
    projections = []
    for minutes in (2, 5, 10, 15):
        projected_usd = avg_per_min_usd * minutes
        projections.append(
            {
                "duration_minutes": minutes,
                "projected_total_cost_usd": round(projected_usd, 6),
                "projected_total_cost_brl": _brl(projected_usd, rate),
            }
        )

    return {
        "usd_brl": rate,
        "items": items,
        "measurable_count": len(measurable),
        "average_cost_per_minute_usd": round(avg_per_min_usd, 6),
        "average_cost_per_minute_brl": _brl(avg_per_min_usd, rate),
        "historical_projections": projections,
        "billing_note": "Histórico de uso do Codexia. Valores calculados por operações registradas não substituem a fatura do provedor.",
    }


__all__ = ["router", "build_cost_estimate_payload", "get_usd_brl_rate"]
