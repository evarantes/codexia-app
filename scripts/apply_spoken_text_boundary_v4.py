#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "app/services/narration_contract_guard.py"
YOUTUBE_GATE = ROOT / "app/services/youtube_narration_gate.py"
NARRATION_LAB = ROOT / "app/services/narration_lab.py"
YOUTUBE_ROUTER = ROOT / "app/routers/youtube.py"
GATE_JS = ROOT / "app/static/youtube_narration_gate.js"

MARKER = "CODEXIA_SPOKEN_TEXT_BOUNDARY_V4"
VERSION = 4


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho original, encontrado {count}")
    return text.replace(old, new, 1)


def patch_guard(text: str) -> str:
    old_validate = '''    cleaned = sanitize_narration_text(text)\n    if not cleaned:\n        raise NarrationContractError(f"{label}: texto vazio; TTS bloqueado antes de qualquer chamada paga.")'''
    new_validate = '''    # CODEXIA_SPOKEN_TEXT_BOUNDARY_V4\n    # Primeira fronteira: separa fala de roteiro/prompt/direção técnica ANTES\n    # que a normalização destrua quebras de linha úteis para a classificação.\n    from app.services.spoken_text_boundary import prepare_spoken_narration_text\n    try:\n        spoken_only = prepare_spoken_narration_text(text)\n    except ValueError as exc:\n        raise NarrationContractError(\n            f"{label}: conteúdo técnico/ambíguo detectado ({exc}); "\n            "TTS bloqueado antes de qualquer chamada paga."\n        ) from exc\n    cleaned = sanitize_narration_text(spoken_only)\n    if not cleaned:\n        raise NarrationContractError(f"{label}: texto vazio; TTS bloqueado antes de qualquer chamada paga.")'''
    text = _replace_once(text, old_validate, new_validate, label="guard/spoken-boundary")

    old_raw = '''            raw_opening = _clean(meta.get("opening_text"))\n            raw_body = _clean(meta.get("body_text"))\n            raw_reflection = _clean(meta.get("reflection_text"))\n            raw_cta = _clean(meta.get("cta_text") or meta.get("closing_text"))'''
    new_raw = '''            # Preserva quebras de linha até a fronteira falável v4; elas são\n            # essenciais para distinguir NARRAÇÃO de PROMPT VISUAL/CENA.\n            raw_opening = str(meta.get("opening_text") or "").strip()\n            raw_body = str(meta.get("body_text") or "").strip()\n            raw_reflection = str(meta.get("reflection_text") or "").strip()\n            raw_cta = str(meta.get("cta_text") or meta.get("closing_text") or "").strip()'''
    text = _replace_once(text, old_raw, new_raw, label="guard/preserve-lines")

    old_audio = '''        def generate_audio_guarded(self: Any, text: str, *args: Any, **kwargs: Any):\n            raw = _clean(text)\n            clean = validate_narration_text(raw, label="texto enviado ao TTS")'''
    new_audio = '''        def generate_audio_guarded(self: Any, text: str, *args: Any, **kwargs: Any):\n            # Não achata o texto antes da fronteira v4.\n            raw = str(text or "").strip()\n            clean = validate_narration_text(raw, label="texto enviado ao TTS")'''
    text = _replace_once(text, old_audio, new_audio, label="guard/generate-audio-lines")

    text = text.replace('"version": 3,', '"version": 4,')
    text = text.replace('debug["narration_contract_version"] = 3', 'debug["narration_contract_version"] = 4')
    text = text.replace('video_generator_cls._codexia_narration_contract_guard_version = 3', 'video_generator_cls._codexia_narration_contract_guard_version = 4')
    return text


def patch_youtube_gate(text: str) -> str:
    text = _replace_once(
        text,
        'MAX_TEXT_CHARS = 30000\nSUPPORTED_VOICES = {"pt-BR-FranciscaNeural", "pt-BR-AntonioNeural"}',
        'MAX_TEXT_CHARS = 30000\nNARRATION_GATE_CONTRACT_VERSION = 4\nSUPPORTED_VOICES = {"pt-BR-FranciscaNeural", "pt-BR-AntonioNeural"}',
        label="youtube-gate/version-constant",
    )
    text = _replace_once(
        text,
        'fingerprint = hashlib.sha256(f"v1\\n{selected_voice}\\n{spoken}".encode("utf-8")).hexdigest()',
        'fingerprint = hashlib.sha256(f"v{NARRATION_GATE_CONTRACT_VERSION}\\n{selected_voice}\\n{spoken}".encode("utf-8")).hexdigest()',
        label="youtube-gate/cache-version",
    )
    text = _replace_once(
        text,
        '            "preview_id": preview_id,\n            "text_sha256": hashlib.sha256(spoken.encode("utf-8")).hexdigest(),',
        '            "preview_id": preview_id,\n            "narration_contract_version": NARRATION_GATE_CONTRACT_VERSION,\n            "text_sha256": hashlib.sha256(spoken.encode("utf-8")).hexdigest(),',
        label="youtube-gate/meta-version",
    )
    old_approve = '''        expected_hash = hashlib.sha256(spoken.encode("utf-8")).hexdigest()\n        if str(meta.get("text_sha256") or "") != expected_hash:'''
    new_approve = '''        if int(meta.get("narration_contract_version") or 0) != NARRATION_GATE_CONTRACT_VERSION:\n            raise YouTubeNarrationGateError(\n                "Esta narração foi criada por um contrato antigo e foi invalidada por segurança. Gere uma nova prévia.",\n                code="STALE_NARRATION_CONTRACT",\n                status_code=409,\n            )\n        expected_hash = hashlib.sha256(spoken.encode("utf-8")).hexdigest()\n        if str(meta.get("text_sha256") or "") != expected_hash:'''
    return _replace_once(text, old_approve, new_approve, label="youtube-gate/reject-old-cache")


def patch_narration_lab(text: str) -> str:
    old = '''        fingerprint_payload = {\n            "contract_version": 1,'''
    new = '''        fingerprint_payload = {\n            # CODEXIA_SPOKEN_TEXT_BOUNDARY_V4: invalida MP3s cacheados pelo contrato antigo.\n            "contract_version": 4,'''
    return _replace_once(text, old, new, label="narration-lab/cache-version")


def patch_gate_js(text: str) -> str:
    return _replace_once(
        text,
        "  const STORAGE_KEY = 'codexia.youtubeAuto.approvedNarration.v1';",
        "  const STORAGE_KEY = 'codexia.youtubeAuto.approvedNarration.v4';",
        label="gate-js/storage-version",
    )


def patch_router(text: str) -> str:
    old = '''                        and approved_meta.get("approved") is True\n                        and str(approved_meta.get("preview_id") or "").strip().lower() == approved_preview_id'''
    new = '''                        and approved_meta.get("approved") is True\n                        and int(approved_meta.get("narration_contract_version") or 0) >= 4\n                        and str(approved_meta.get("preview_id") or "").strip().lower() == approved_preview_id'''
    return _replace_once(text, old, new, label="youtube-router/reject-old-approved-audio")


def apply(*, write: bool) -> int:
    changed = 0
    for path, patcher in (
        (GUARD, patch_guard),
        (YOUTUBE_GATE, patch_youtube_gate),
        (NARRATION_LAB, patch_narration_lab),
        (GATE_JS, patch_gate_js),
        (YOUTUBE_ROUTER, patch_router),
    ):
        source = path.read_text(encoding="utf-8")
        transformed = patcher(source)
        if patcher(transformed) != transformed:
            raise PatchError(f"{path.name}: patch v4 não idempotente")
        if transformed != source:
            changed += 1
            if write:
                path.write_text(transformed, encoding="utf-8")
    print(f"Spoken text boundary v4: {changed} arquivo(s) {'aplicados' if write else 'necessários'}")
    return changed


def check() -> None:
    requirements = {
        GUARD: [MARKER, "prepare_spoken_narration_text", "narration_contract_guard_version = 4"],
        YOUTUBE_GATE: ["NARRATION_GATE_CONTRACT_VERSION = 4", "STALE_NARRATION_CONTRACT", "narration_contract_version"],
        NARRATION_LAB: [MARKER, '"contract_version": 4'],
        GATE_JS: ["approvedNarration.v4"],
        YOUTUBE_ROUTER: ['int(approved_meta.get("narration_contract_version") or 0) >= 4'],
    }
    for path, needles in requirements.items():
        source = path.read_text(encoding="utf-8")
        missing = [needle for needle in needles if needle not in source]
        if missing:
            raise PatchError(f"{path.name}: contrato falável v4 ausente: {missing}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply e/ou --check")
    try:
        if args.apply:
            apply(write=True)
        if args.check:
            check()
    except PatchError as exc:
        print(f"ERRO SPOKEN TEXT BOUNDARY V4: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
