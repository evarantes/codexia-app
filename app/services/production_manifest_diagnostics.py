from __future__ import annotations

from typing import Any, Dict, List

from app.services.production_manifest import build_recovery_plan, load_manifest


def _artifact_exists(item: Dict[str, Any]) -> bool:
    return bool(item.get("exists")) or bool(item.get("resolved_path"))


def _valid_artifacts(manifest: Dict[str, Any], kind: str) -> List[Dict[str, Any]]:
    items = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), list) else []
    return [
        dict(item)
        for item in items
        if isinstance(item, dict)
        and str(item.get("kind") or "").strip().lower() == kind
        and _artifact_exists(item)
    ]


def _audio_trust(manifest: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    audio_items = _valid_artifacts(manifest, "audio")
    audio_path = str(plan.get("audio_path") or "").strip()
    audio_found = bool(audio_items or audio_path)
    protected = any(
        str(item.get("source") or "").strip().lower() == "tts_immediate"
        for item in audio_items
    )
    if not audio_found:
        return {
            "found": False,
            "reusable": False,
            "trust": "missing",
            "reason": "Nenhum áudio durável válido foi encontrado no manifesto.",
        }
    if protected:
        return {
            "found": True,
            "reusable": bool(plan.get("audio_ok")),
            "trust": "narration_contract_v1",
            "reason": "Áudio foi persistido pelo guard de narração após validação pré-TTS.",
        }
    return {
        "found": True,
        "reusable": False,
        "trust": "legacy_unverified",
        "reason": (
            "Áudio anterior ao guard de narração não possui prova de sanitização. "
            "Ele pode existir fisicamente, mas não deve ser reutilizado automaticamente."
        ),
    }


def _max_checkpoint(plan: Dict[str, Any], audio_info: Dict[str, Any]) -> str:
    if bool(plan.get("video_ok")):
        return "stage_6_render"
    if int(plan.get("valid_image_count") or 0) > 0:
        return "stage_3_images"
    if bool(audio_info.get("found")):
        return "stage_2_voice"
    if bool(plan.get("script_ok")):
        return "stage_1_editorial"
    return "starting"


def build_manifest_diagnostic(task_id: Any) -> Dict[str, Any]:
    task_key = str(task_id or "").strip()
    if not task_key:
        return {
            "manifest_found": False,
            "task_id": "",
            "error": "task_id ausente",
        }

    manifest = load_manifest(task_key)
    if not isinstance(manifest, dict) or not manifest:
        return {
            "manifest_found": False,
            "task_id": task_key,
            "max_recoverable_checkpoint": "starting",
            "automatic_paid_recovery_allowed": False,
            "recommendation": "Manifesto durável não encontrado para esta tarefa.",
        }

    plan = build_recovery_plan(task_key)
    plan = dict(plan or {}) if isinstance(plan, dict) else {}
    audio_info = _audio_trust(manifest, plan)
    valid_images = int(plan.get("valid_image_count") or 0)
    expected_images = int(plan.get("expected_image_count") or 0)
    missing_images = int(plan.get("missing_image_count") or 0)

    planned_action = str(plan.get("action") or "blocked")
    effective_action = planned_action
    if audio_info.get("found") and not audio_info.get("reusable"):
        if valid_images > 0 or bool(plan.get("script_ok")):
            effective_action = "rebuild_untrusted_audio_then_recover"
        else:
            effective_action = "blocked"

    recommendation = "Recuperação bloqueada até confirmação dos ativos."
    if effective_action == "rebuild_untrusted_audio_then_recover":
        recommendation = (
            "Preservar roteiro/imagens encontrados, rejeitar o áudio legado e reconstruir somente "
            "a narração após validação. Nenhuma chamada paga deve ocorrer sem confirmação explícita."
        )
    elif planned_action == "review_existing_render":
        recommendation = "Existe MP4 candidato; revisar o arquivo antes de qualquer nova geração."
    elif planned_action == "rerender_without_paid_media":
        recommendation = "Ativos validados permitem novo render sem regenerar mídia paga."
    elif planned_action == "regenerate_missing_images":
        recommendation = (
            f"Faltam {missing_images} imagem(ns). Exigir confirmação explícita do custo antes de gerar."
        )

    return {
        "manifest_found": True,
        "task_id": task_key,
        "manifest_schema_version": manifest.get("schema_version"),
        "script_preserved": bool(plan.get("script_ok")),
        "audio": audio_info,
        "images": {
            "valid": valid_images,
            "expected": expected_images,
            "missing": missing_images,
            "complete": bool(plan.get("images_ok")),
        },
        "video_preserved": bool(plan.get("video_ok")),
        "max_recoverable_checkpoint": _max_checkpoint(plan, audio_info),
        "planned_action": planned_action,
        "effective_action": effective_action,
        "estimated_missing_image_cost_usd": float(plan.get("estimated_new_cost_usd") or 0.0),
        "estimated_missing_image_cost_brl": float(plan.get("estimated_new_cost_brl") or 0.0),
        "automatic_paid_recovery_allowed": False,
        "requires_explicit_paid_confirmation": bool(
            missing_images > 0 or (audio_info.get("found") and not audio_info.get("reusable"))
        ),
        "recommendation": recommendation,
    }


def enrich_video_diagnostic_report(report: Any, *, task_id: Any) -> Dict[str, Any]:
    enriched = dict(report or {}) if isinstance(report, dict) else {}
    checks = enriched.get("checks")
    if not isinstance(checks, list):
        checks = []
        enriched["checks"] = checks
    recommendations = enriched.get("recommendations")
    if not isinstance(recommendations, list):
        recommendations = []
        enriched["recommendations"] = recommendations

    diagnostic = build_manifest_diagnostic(task_id)
    enriched["production_manifest"] = diagnostic

    if not diagnostic.get("manifest_found"):
        checks.append({"name": "Manifesto da produção", "ok": False, "value": "não encontrado"})
        recommendations.append(str(diagnostic.get("recommendation") or "Manifesto não encontrado."))
        return enriched

    images = diagnostic.get("images") if isinstance(diagnostic.get("images"), dict) else {}
    audio = diagnostic.get("audio") if isinstance(diagnostic.get("audio"), dict) else {}
    image_label = f"{int(images.get('valid') or 0)}/{int(images.get('expected') or 0) or '?'}"
    audio_label = "não encontrado"
    if audio.get("found"):
        audio_label = "validado pelo guard" if audio.get("reusable") else "encontrado, porém NÃO confiável para reutilização"

    checks.extend([
        {"name": "Manifesto da produção", "ok": True, "value": "encontrado"},
        {"name": "Roteiro preservado", "ok": bool(diagnostic.get("script_preserved")), "value": "sim" if diagnostic.get("script_preserved") else "não"},
        {"name": "Imagens preservadas", "ok": int(images.get("valid") or 0) > 0, "value": image_label},
        {"name": "Áudio preservado", "ok": bool(audio.get("found")), "value": audio_label},
        {"name": "MP4 preservado", "ok": bool(diagnostic.get("video_preserved")), "value": "sim" if diagnostic.get("video_preserved") else "não"},
        {"name": "Checkpoint máximo recuperável", "ok": True, "value": diagnostic.get("max_recoverable_checkpoint")},
        {"name": "Recuperação paga automática", "ok": True, "value": "BLOQUEADA"},
    ])
    recommendations.append(str(diagnostic.get("recommendation") or ""))
    return enriched


__all__ = ["build_manifest_diagnostic", "enrich_video_diagnostic_report"]
