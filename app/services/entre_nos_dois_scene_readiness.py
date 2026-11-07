"""Studio lip-sync readiness and scenario estimate without external requests.

This only reads user-private portrait/MP3 presence, never exports their bytes
or produces public URLs. A future explicit external-media consent step is needed
before signed URL generation or any billable provider call.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_UP

from app.services.entre_nos_dois_audio_review import list_segment_audio, preview_audio_timeline
from app.services.entre_nos_dois_references import host_reference_status
from app.services.entre_nos_dois_fal_sync3 import FAL_MODEL, USD_PER_OUTPUT_SECOND_REFERENCE


class LipSyncReadinessError(ValueError):
    pass


def _operator_rate(value: str | None) -> tuple[Decimal, bool]:
    if value is None:
        return Decimal(str(USD_PER_OUTPUT_SECOND_REFERENCE)), False
    if not isinstance(value, str) or not value.strip() or len(value) > 24:
        raise LipSyncReadinessError("Informe a tarifa em USD por segundo.")
    try:
        rate = Decimal(value.strip())
    except InvalidOperation:
        raise LipSyncReadinessError("Tarifa inválida.") from None
    if not rate.is_finite() or not Decimal("0") < rate <= Decimal("100"):
        raise LipSyncReadinessError("Tarifa fora do intervalo válido.")
    return rate, True


def lip_sync_readiness(*, user_id: int, usd_per_second: str | None = None) -> dict:
    if type(user_id) is not int or user_id <= 0:
        raise LipSyncReadinessError("Usuário inválido.")
    rate, operator_provided = _operator_rate(usd_per_second)
    references = host_reference_status(user_id=user_id)
    audios = list_segment_audio(user_id=user_id)
    timeline = preview_audio_timeline(user_id=user_id)
    missing = []
    for host in ("Gabriel", "Helena"):
        if references.get(host) is not True:
            missing.append(f"reference_{host.lower()}")
    for item in audios["segments"]:
        if not item["uploaded"]:
            missing.append(f"audio_{item['index']}")
    scenes = []
    total = Decimal("0")
    if timeline["ready"]:
        for segment in timeline["segments"]:
            amount = (Decimal(str(segment["duration_seconds"])) * rate).quantize(
                Decimal("0.0001"), rounding=ROUND_UP,
            )
            total += amount
            scenes.append({
                "scene_index": segment["index"],
                "speaker": segment["speaker"],
                "duration_seconds": segment["duration_seconds"],
                "estimated_usd": str(amount),
                "portrait_present": bool(references.get(segment["speaker"])),
                "audio_present": True,
                "requires_lip_sync": True,
                "status": "awaiting_provider_media_consent",
            })
    return {
        "program": "entre-nos-dois",
        "candidate_provider": "fal.ai",
        "candidate_model": FAL_MODEL,
        "rate_usd_per_second": str(rate),
        "rate_source": "operator_unverified" if operator_provided else "planning_reference_unverified",
        "pricing_verified_in_provider_account": False,
        "estimated_total_usd": str(total) if timeline["ready"] else None,
        "scenes": scenes,
        "missing": missing,
        "inputs_ready": not missing and timeline["ready"],
        "planned_seconds": 30,
        "measured_total_seconds": timeline.get("total_seconds"),
        "private_media_export_authorized": False,
        "signed_asset_urls_created": False,
        "billable_calls_performed": False,
        "rendered_video_ready": False,
        "generation_enabled": False,
        "auto_publish": False,
        "next_action": (
            "Cadastrar os retratos privados e os quatro MP3s"
            if missing else
            "Conferir valores e termos do provedor, autorizar envio externo de mídia e a primeira cena"
        ),
    }
