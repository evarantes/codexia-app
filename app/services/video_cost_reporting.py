from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from sqlalchemy import text

from app.database import SessionLocal
from app.services.video_cost_estimator import estimate_video_cost, project_from_baseline


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


def _task_payload(task: Dict[str, Any]) -> Dict[str, Any]:
    candidates = []
    result = task.get("result")
    if isinstance(result, dict):
        candidates.append(result.get("payload"))
        candidates.append(result)
    result_json = task.get("result_json")
    if isinstance(result_json, str) and result_json.strip():
        try:
            parsed = json.loads(result_json)
            if isinstance(parsed, dict):
                candidates.append(parsed.get("payload"))
                candidates.append(parsed)
        except Exception:
            pass
    candidates.append(task.get("payload"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
    return {}


def _duration_minutes(task: Dict[str, Any]) -> float:
    payload = _task_payload(task)
    # A API histórica usa duration em minutos para vídeos História/Devocional.
    for source in (payload, task):
        for key in ("duration", "duration_minutes", "requested_duration", "target_duration"):
            value = _safe_float(source.get(key), 0.0) if isinstance(source, dict) else 0.0
            if value > 0:
                return max(0.25, value)
    return 2.0


def _brl_rate() -> float:
    return max(0.01, _safe_float(os.getenv("USD_BRL_RATE") or os.getenv("CODEXIA_USD_BRL_RATE"), 5.25))


def _query_operations(task_id: str) -> List[Dict[str, Any]]:
    db = SessionLocal()
    try:
        rows = db.execute(text(
            """
            SELECT capability, provider, model, status, estimated_cost, actual_cost,
                   COALESCE(latency_ms, 0), created_at
            FROM ai_operation_runs
            WHERE scope_id = :task_id
            ORDER BY created_at ASC
            """
        ), {"task_id": str(task_id)}).fetchall()
        return [
            {
                "capability": str(row[0] or ""),
                "provider": str(row[1] or ""),
                "model": str(row[2] or ""),
                "status": str(row[3] or ""),
                "estimated_cost_usd": round(_safe_float(row[4]), 6),
                "actual_cost_usd": round(_safe_float(row[5]), 6),
                "latency_ms": _safe_int(row[6]),
            }
            for row in rows
        ]
    except Exception:
        return []
    finally:
        db.close()


def _project_10_minutes(minutes: float, tracked_total: float, fixed_cost_usd: float) -> float:
    """Projeta 10 min sem achatar chamadas rastreadas pequenas.

    `project_from_baseline` separa custo fixo/variável. Em uma amostra parcial,
    porém, o total rastreado pode ser igual ou menor que o custo fixo de
    referência; nesse caso a parte variável vira zero e a projeção de 10 min
    ficaria artificialmente igual ao vídeo curto. Quando isso ocorrer e houver
    custo rastreado real/estimado, escalamos pelo custo por minuto observado.
    """
    projection = project_from_baseline(
        minutes,
        tracked_total,
        10.0,
        fixed_cost_usd=fixed_cost_usd,
    )
    projected = _safe_float(projection.get("projected_total_cost_usd"))
    if 10.0 > minutes and tracked_total > 0 and projected <= tracked_total:
        projected = tracked_total * (10.0 / max(0.25, minutes))
    return max(0.0, projected)


def build_task_cost_summary(task_id: str, task: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Resumo financeiro de uma produção.

    `actual_cost` é usado quando o provedor devolve custo mensurado. Quando ele
    não está disponível, o Codexia usa o `estimated_cost` registrado para aquela
    chamada e marca explicitamente o total como rastreado/estimado, nunca como
    fatura oficial da OpenAI.
    """
    task_data = dict(task or {})
    minutes = _duration_minutes(task_data)
    operations = _query_operations(str(task_id))
    rate = _brl_rate()

    completed = [op for op in operations if str(op.get("status") or "").lower() == "completed"]
    estimated_ops = sum(_safe_float(op.get("estimated_cost_usd")) for op in completed)
    measured_ops = sum(_safe_float(op.get("actual_cost_usd")) for op in completed)
    measured_count = sum(1 for op in completed if _safe_float(op.get("actual_cost_usd")) > 0)
    tracked_total = sum(
        _safe_float(op.get("actual_cost_usd"))
        if _safe_float(op.get("actual_cost_usd")) > 0
        else _safe_float(op.get("estimated_cost_usd"))
        for op in completed
    )

    pre = estimate_video_cost(minutes, mode="balanced")
    if tracked_total <= 0:
        tracked_total = pre.total_cost_usd
        cost_source = "pre_generation_estimate"
    elif measured_count == len(completed) and completed:
        cost_source = "provider_measured"
    else:
        cost_source = "tracked_calls_with_estimated_unit_cost"

    image_ops = [
        op for op in completed
        if str(op.get("capability") or "").upper() in {"IMAGE_GENERATION", "THUMBNAIL_GENERATION"}
    ]
    image_generation_ops = [
        op for op in completed
        if str(op.get("capability") or "").upper() == "IMAGE_GENERATION"
    ]
    models = sorted({str(op.get("model") or "").strip() for op in image_ops if str(op.get("model") or "").strip()})
    providers = sorted({str(op.get("provider") or "").strip() for op in image_ops if str(op.get("provider") or "").strip()})

    projected_10_usd = _project_10_minutes(minutes, tracked_total, pre.fixed_cost_usd)

    # Regenerações são inferidas apenas quando há mais chamadas de imagem que o
    # número-base estimado. É uma métrica operacional, não de faturamento.
    inferred_regens = max(0, len(image_generation_ops) - int(pre.estimated_images) - int(pre.estimated_endcards))

    return {
        "version": 1,
        "task_id": str(task_id),
        "currency": "USD",
        "brl_rate": round(rate, 4),
        "duration_minutes": round(minutes, 3),
        "provider": providers[0] if len(providers) == 1 else (", ".join(providers) if providers else pre.provider),
        "model": models[0] if len(models) == 1 else (", ".join(models) if models else pre.model),
        "image_quality": pre.image_quality,
        "operation_count": len(completed),
        "image_operation_count": len(image_generation_ops),
        "thumbnail_operation_count": max(0, len(image_ops) - len(image_generation_ops)),
        "inferred_regenerations": inferred_regens,
        "pre_generation_estimate_usd": round(pre.total_cost_usd, 4),
        "pre_generation_estimate_brl": round(pre.total_cost_usd * rate, 2),
        "tracked_estimated_cost_usd": round(estimated_ops, 4),
        "provider_measured_cost_usd": round(measured_ops, 4),
        "tracked_total_usd": round(tracked_total, 4),
        "tracked_total_brl": round(tracked_total * rate, 2),
        "cost_per_minute_usd": round(tracked_total / max(0.25, minutes), 4),
        "cost_per_minute_brl": round((tracked_total * rate) / max(0.25, minutes), 2),
        "projected_10_min_usd": round(projected_10_usd, 4),
        "projected_10_min_brl": round(projected_10_usd * rate, 2),
        "cost_source": cost_source,
        "is_official_invoice": False,
        "note": "Valor rastreado pelo Codexia; a fatura oficial continua sendo a do provedor.",
    }
