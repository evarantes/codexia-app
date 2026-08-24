from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "app/services/ready_video_repair.py"
YOUTUBE = ROOT / "app/routers/youtube.py"
INDEX = ROOT / "app/static/index.html"
MARKER = "CODEXIA_READY_VIDEO_ASSET_REPAIR_V4"


class PatchError(RuntimeError):
    pass


SERVICE_OLD = '''    unit_cost = max(0.0, _as_float(image_cost_unit))
    return {'''

SERVICE_NEW = '''    unit_cost = max(0.0, _as_float(image_cost_unit))
    # CODEXIA_READY_VIDEO_ASSET_REPAIR_V4
    # O reparo sempre reconstrói a narração. Mesmo com zero imagens novas,
    # mostre uma estimativa preventiva de TTS e exija confirmação explícita.
    try:
        audio_unit = max(0.0, float(os.getenv("YOUTUBE_AUTO_TTS_MINUTE_COST_UNIT") or "0.0120"))
    except Exception:
        audio_unit = 0.0120
    try:
        usd_brl = max(0.01, float(os.getenv("CODEXIA_USD_BRL") or "5.20"))
    except Exception:
        usd_brl = 5.20
    audio_minutes = max(0.0, duration_sec / 60.0)
    audio_cost_usd = round(audio_unit * audio_minutes, 6)
    audio_cost_brl = round(audio_cost_usd * usd_brl, 2)
    return {'''

SERVICE_FIELD_OLD = '''        "regenerate_audio": True,
        "reuse_old_audio": False,'''
SERVICE_FIELD_NEW = '''        "regenerate_audio": True,
        "paid_audio_calls_require_confirmation": True,
        "audio_provider_policy": "premium_configured_then_free_fallback",
        "audio_cost_unit_usd_per_minute": round(audio_unit, 6),
        "estimated_new_audio_cost_usd": audio_cost_usd,
        "estimated_new_audio_cost_brl": audio_cost_brl,
        "reuse_old_audio": False,'''

YOUTUBE_OLD = '''    missing = int(preview.get("missing_image_count") or 0)
    confirmed = bool((body or {}).get("confirm_paid_images"))'''
YOUTUBE_NEW = '''    missing = int(preview.get("missing_image_count") or 0)
    # CODEXIA_READY_VIDEO_ASSET_REPAIR_V4
    # A confirmação de imagens não autoriza implicitamente TTS premium.
    # Sem consentimento explícito para a nova narração, falhe fechado antes
    # de retry/worker/provedor.
    audio_confirmed = bool((body or {}).get("confirm_paid_audio"))
    if bool(preview.get("regenerate_audio")) and not audio_confirmed:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "paid_audio_confirmation_required",
                "message": (
                    "A correção precisa reconstruir a narração e pode usar o TTS premium configurado. "
                    "Confirme explicitamente o custo preventivo de áudio antes de continuar."
                ),
                "preview": preview,
            },
        )
    confirmed = bool((body or {}).get("confirm_paid_images"))'''

UI_MINUTES_OLD = '''                        const minutes = Number(preview.duration_minutes || 0).toFixed(1);
                        let costLine = '';'''
UI_MINUTES_NEW = '''                        const minutes = Number(preview.duration_minutes || 0).toFixed(1);
                        // CODEXIA_READY_VIDEO_ASSET_REPAIR_V4
                        const audioCostUsd = Number(preview.estimated_new_audio_cost_usd || 0);
                        const audioCostBrl = Number(preview.estimated_new_audio_cost_brl || 0);
                        const audioCostLine = `\\nNarração nova: TTS premium configurado (com fallback gratuito se necessário)` +
                            `\\nEstimativa preventiva da narração: US$ ${audioCostUsd.toFixed(4)} / aprox. R$ ${audioCostBrl.toFixed(2)}`;
                        let costLine = '';'''

UI_PUBLICATION_OLD = '''                            `Publicação automática: bloqueada durante a correção` + costLine + `\\n\\n` +'''
UI_PUBLICATION_NEW = '''                            `Publicação automática: bloqueada durante a correção` + costLine + audioCostLine + `\\n\\n` +'''

UI_NO_IMAGE_OLD = '''                                : `Nenhuma nova imagem paga é necessária pelo plano atual.`);'''
UI_NO_IMAGE_NEW = '''                                : `Nenhuma nova imagem paga é necessária pelo plano atual. O OK também autoriza a nova narração dentro da estimativa preventiva exibida acima.`);'''

UI_BODY_OLD = '''                                confirm_paid_images: missing > 0,
                                max_new_images: missing,'''
UI_BODY_NEW = '''                                confirm_paid_images: missing > 0,
                                confirm_paid_audio: true,
                                max_new_images: missing,'''


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1)


def patch_service(text: str) -> str:
    if MARKER not in text:
        text = _replace_once(text, SERVICE_OLD, SERVICE_NEW, "estimativa de áudio")
    text = _replace_once(text, SERVICE_FIELD_OLD, SERVICE_FIELD_NEW, "campos de custo de áudio")
    return text


def patch_youtube(text: str) -> str:
    if "/schedule/{video_id}/repair-with-assets" not in text:
        raise PatchError("ready-video repair V2 deve ser aplicado antes do V4")
    return _replace_once(text, YOUTUBE_OLD, YOUTUBE_NEW, "confirmação de áudio no backend")


def patch_index(text: str) -> str:
    if "repairScheduledVideoWithAssets" not in text:
        raise PatchError("UI de repair deve existir antes do V4")
    text = _replace_once(text, UI_MINUTES_OLD, UI_MINUTES_NEW, "estimativa de áudio na UI")
    text = _replace_once(text, UI_PUBLICATION_OLD, UI_PUBLICATION_NEW, "linha de custo de áudio")
    text = _replace_once(text, UI_NO_IMAGE_OLD, UI_NO_IMAGE_NEW, "confirmação quando imagens=0")
    text = _replace_once(text, UI_BODY_OLD, UI_BODY_NEW, "flag confirm_paid_audio")
    return text


def apply() -> None:
    for path, patcher in ((SERVICE, patch_service), (YOUTUBE, patch_youtube), (INDEX, patch_index)):
        original = path.read_text(encoding="utf-8")
        transformed = patcher(original)
        if patcher(transformed) != transformed:
            raise PatchError(f"patch V4 não idempotente: {path.name}")
        if transformed != original:
            path.write_text(transformed, encoding="utf-8")


def check() -> None:
    service = SERVICE.read_text(encoding="utf-8")
    youtube = YOUTUBE.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    required = (
        (service, MARKER),
        (service, '"paid_audio_calls_require_confirmation": True'),
        (service, '"estimated_new_audio_cost_usd": audio_cost_usd'),
        (youtube, '"paid_audio_confirmation_required"'),
        (youtube, 'audio_confirmed = bool((body or {}).get("confirm_paid_audio"))'),
        (index, "Estimativa preventiva da narração"),
        (index, "confirm_paid_audio: true"),
    )
    missing = [token for content, token in required if token not in content]
    if missing:
        raise PatchError("ready-video repair V4 incompleto: " + ", ".join(missing))
    compile(service, str(SERVICE), "exec")
    compile(youtube, str(YOUTUBE), "exec")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply ou --check")
    try:
        if args.apply:
            apply()
        if args.check:
            check()
    except PatchError as exc:
        print(f"ERRO READY VIDEO ASSET REPAIR V4: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
