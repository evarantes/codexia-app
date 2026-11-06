"""Paid ElevenLabs single-turn TTS with durable duplicate-charge guard.

The feature is disabled by default, both by feature flag and server USD cap.
This performs exactly one provider POST after explicit operator authorization.
No retries: ambiguous outcomes stay reserved for manual provider reconciliation.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Callable

from app.services.entre_nos_dois_references import StudioReferenceError
from app.services.entre_nos_dois_audio_review import (
    StudioAudioError, audio_file, store_segment_audio, _segment,
)
from app.services.entre_nos_dois_cost_worksheet import worksheet, CostWorksheetError
from app.services.entre_nos_dois_tts_adapter import (
    ELEVEN_API_ROOT, VOICE_MODEL, AUDIO_FORMAT, _post_elevenlabs,
)
from app.services.entre_nos_dois_voice_preflight import check_approved_voices


class PaidPilotTtsError(RuntimeError):
    pass


def _amount(value: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except Exception:
        raise PaidPilotTtsError("Limite monetário inválido.") from None
    if not amount.is_finite() or amount <= 0:
        raise PaidPilotTtsError("Limite monetário inválido.")
    return amount


def submit_one_turn(
    *,
    user_id: int,
    index: int,
    api_key: str,
    quote_digest: str,
    usd_per_1000_characters: str,
    approved_max_usd: str,
    confirmation: str,
    rate_checked_in_provider_account: bool,
    post_audio: Callable[[str, dict, str], bytes] = _post_elevenlabs,
    verify_voices: Callable[..., dict] = check_approved_voices,
) -> dict:
    if os.getenv("CODEXIA_STUDIO_ENABLE_PAID_TTS") != "1":
        raise PaidPilotTtsError("Geração paga desabilitada neste ambiente.")
    server_ceiling = _amount(os.getenv("CODEXIA_STUDIO_MAX_TTS_USD", "0"))
    if not api_key or not api_key.strip():
        raise PaidPilotTtsError("Credencial de voz ausente.")
    segment = _segment(index)
    if confirmation != f"AUTORIZO FALA {index}" or rate_checked_in_provider_account is not True:
        raise PaidPilotTtsError("Confirmação explícita da fala e tarifa exigida.")
    try:
        quote = worksheet(usd_per_1000_characters=usd_per_1000_characters)
    except CostWorksheetError as exc:
        raise PaidPilotTtsError(str(exc)) from None
    if quote_digest != quote["manifest_sha256"]:
        raise PaidPilotTtsError("Roteiro ou vozes mudaram após o orçamento.")
    estimated = _amount(quote["segments"][index - 1]["estimated_usd"])
    maximum = _amount(approved_max_usd)
    if estimated > maximum or maximum > server_ceiling:
        raise PaidPilotTtsError("Limite monetário insuficiente ou superior ao teto do servidor.")

    try:
        destination = audio_file(user_id=user_id, index=index)
    except (StudioAudioError, StudioReferenceError, OSError) as exc:
        raise PaidPilotTtsError("Armazenamento privado indisponível.") from None
    if destination.exists():
        raise PaidPilotTtsError("Já existe áudio nesta fala; revisão antes de qualquer nova cobrança.")

    # The free preflight takes place before reserving a potentially billable POST.
    try:
        result = verify_voices(api_key=api_key)
        checked = {v["speaker"]: v["voice_id"] for v in result.get("checked", [])}
        from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest
        manifest = prepare_speech_manifest()
        if not result.get("all_accessible") or checked != manifest["approved_voices"]:
            raise PaidPilotTtsError("Acesso às vozes oficiais não confirmado na API direta.")
    except PaidPilotTtsError:
        raise
    except Exception:
        raise PaidPilotTtsError("Pré-verificação de vozes falhou; nenhuma geração paga enviada.") from None

    destination.parent.mkdir(parents=True, exist_ok=True)
    reservation = destination.with_suffix(".paid-request-pending")
    try:
        fd = os.open(str(reservation), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise PaidPilotTtsError("Já existe solicitação reservada; conferir histórico da ElevenLabs.") from None

    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump({
            "status": "uncertain_until_reconciled",
            "index": index,
            "user_id": user_id,
            "quote_sha256": quote_digest,
            "approved_max_usd": str(maximum),
            "estimated_usd": str(estimated),
        }, file)
        file.flush()
        os.fsync(file.fileno())

    try:
        url = f"{ELEVEN_API_ROOT}/text-to-speech/{segment['voice_id']}?output_format={AUDIO_FORMAT}"
        audio = post_audio(url, {"text": segment["text"], "model_id": VOICE_MODEL}, api_key)
        result = store_segment_audio(user_id=user_id, index=index, body=audio)
    except Exception:
        # Never remove the reservation after timeout, transport ambiguity or
        # malformed output: the charge may already have happened.
        raise PaidPilotTtsError(
            "Envio ou áudio inconclusivo; solicitação reservada. Confira cobrança antes de repetir."
        ) from None
    # A finalized local MP3 means a further POST is not necessary.
    reservation.unlink(missing_ok=True)
    return {
        "index": index,
        "speaker": segment["speaker"],
        "duration_seconds": result["duration_seconds"],
        "status": "awaiting_human_audio_review",
        "estimated_usd": str(estimated),
        "is_final_provider_charge": False,
        "auto_publish": False,
    }
