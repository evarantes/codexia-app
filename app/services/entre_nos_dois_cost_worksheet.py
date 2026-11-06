"""Nonbillable, deterministic cost worksheet for the canonical four-turn pilot.

This is not a provider price quote: the account's real ElevenLabs plan,
voice/model entitlement and billable credit ratio must be checked externally.
No API key, provider call, token, download or expense approval is performed.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_UP

from app.services.entre_nos_dois_tts_adapter import VOICE_MODEL, AUDIO_FORMAT
from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest


class CostWorksheetError(ValueError):
    pass


def _rate(value: str) -> Decimal:
    """Decimal USD per 1000 input characters, as declared by the operator."""
    if not isinstance(value, str) or not value.strip() or len(value) > 20:
        raise CostWorksheetError("Informe a tarifa USD por mil caracteres.")
    try:
        rate = Decimal(value.strip())
    except InvalidOperation:
        raise CostWorksheetError("Tarifa inválida.") from None
    if not rate.is_finite() or rate <= 0 or rate > 1000:
        raise CostWorksheetError("Tarifa deve ser positiva e menor ou igual a US$ 1.000 por mil caracteres.")
    return rate


def worksheet(*, usd_per_1000_characters: str | None = None) -> dict:
    manifest = prepare_speech_manifest()
    segments = manifest["segments"]
    canonical = json.dumps(
        {
            "model": VOICE_MODEL,
            "format": AUDIO_FORMAT,
            "segments": [(x["index"], x["voice_id"], x["text"]) for x in segments],
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    entries = [{
        "index": x["index"],
        "speaker": x["speaker"],
        "characters": len(x["text"]),
        "target_seconds": x["target_seconds"],
    } for x in segments]
    result = {
        "program": "entre-nos-dois",
        "manifest_sha256": digest,
        "tts_provider": "ElevenLabs (API direta)",
        "model": VOICE_MODEL,
        "audio_format": AUDIO_FORMAT,
        "segments": entries,
        "total_characters": manifest["estimated_characters"],
        "target_seconds": manifest["target_seconds"],
        "account_rate_verified": False,
        "account_access_verified": False,
        "quote_is_binding": False,
        "billable_calls_performed": False,
        "generation_enabled": False,
        "auto_publish": False,
        "requirements": [
            "Conferir acesso às vozes na mesma API/conta usada para o TTS.",
            "Conferir o preço, a quantidade de créditos e o modelo elegível no plano da conta.",
            "Aprovar explicitamente o gasto antes de liberar cada chamada paga.",
        ],
    }
    if usd_per_1000_characters is not None:
        rate = _rate(usd_per_1000_characters)
        factor = Decimal("1000")
        def estimate(chars: int) -> str:
            return str((Decimal(chars) * rate / factor).quantize(Decimal("0.0001"), rounding=ROUND_UP))
        for item in result["segments"]:
            item["estimated_usd"] = estimate(item["characters"])
        result["operator_entered_usd_per_1000_characters"] = str(rate)
        result["estimated_total_usd"] = estimate(result["total_characters"])
        result["estimate_only"] = True
    return result
