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
    """Mantém validate_narration_text estrito; a fronteira roda só nas portas TTS."""
    old_setup = '''    original_prepare = getattr(video_generator_cls, "prepare_final_narration_text", None)\n    original_generate_audio = getattr(video_generator_cls, "generate_audio", None)\n    original_segmented = getattr(video_generator_cls, "_compose_segmented_narration_audio", None)'''
    new_setup = '''    original_prepare = getattr(video_generator_cls, "prepare_final_narration_text", None)\n    original_generate_audio = getattr(video_generator_cls, "generate_audio", None)\n    original_segmented = getattr(video_generator_cls, "_compose_segmented_narration_audio", None)\n\n    # CODEXIA_SPOKEN_TEXT_BOUNDARY_V4\n    # O validador global permanece fail-closed para JSON/código bruto. Somente\n    # estas portas oficiais de TTS podem extrair campos narrativos seguros.\n    from app.services.spoken_text_boundary import prepare_spoken_narration_text\n\n    def _spoken_boundary(value: Any, *, label: str) -> str:\n        try:\n            spoken = prepare_spoken_narration_text(value)\n        except ValueError as exc:\n            raise NarrationContractError(\n                f"{label}: conteúdo técnico/ambíguo detectado ({exc}); "\n                "TTS bloqueado antes de qualquer chamada paga."\n            ) from exc\n        if not spoken:\n            raise NarrationContractError(\n                f"{label}: nenhum texto falável seguro encontrado; "\n                "TTS bloqueado antes de qualquer chamada paga."\n            )\n        return spoken'''
    text = _replace_once(text, old_setup, new_setup, label="guard/install-boundary-helper")

    old_block = '''            raw_opening = _clean(meta.get("opening_text"))\n            raw_body = _clean(meta.get("body_text"))\n            raw_reflection = _clean(meta.get("reflection_text"))\n            raw_cta = _clean(meta.get("cta_text") or meta.get("closing_text"))\n\n            opening = validate_narration_text(raw_opening, label="abertura")\n            body = validate_narration_text(raw_body, label="corpo da narração")\n            reflection = validate_narration_text(raw_reflection, label="reflexão final") if raw_reflection else ""'''
    new_block = '''            # Preserve quebras de linha até a fronteira: elas distinguem\n            # NARRAÇÃO de PROMPT VISUAL, DURAÇÃO, CÂMERA etc.\n            raw_opening = str(meta.get("opening_text") or "").strip()\n            raw_body = str(meta.get("body_text") or "").strip()\n            raw_reflection = str(meta.get("reflection_text") or "").strip()\n            raw_cta = str(meta.get("cta_text") or meta.get("closing_text") or "").strip()\n\n            opening = validate_narration_text(\n                _spoken_boundary(raw_opening, label="abertura"), label="abertura"\n            )\n            body = validate_narration_text(\n                _spoken_boundary(raw_body, label="corpo da narração"), label="corpo da narração"\n            )\n            reflection = (\n                validate_narration_text(\n                    _spoken_boundary(raw_reflection, label="reflexão final"), label="reflexão final"\n                )\n                if raw_reflection else ""\n            )'''
    text = _replace_once(text, old_block, new_block, label="guard/prepare-fields")

    old_cta = '''            cta = _pick_complete_cta(safe_plan, marked_cta, raw_cta)\n            cta = validate_narration_text(cta, label="CTA final", require_complete_cta=True)'''
    new_cta = '''            cta = _pick_complete_cta(safe_plan, marked_cta, raw_cta)\n            cta = validate_narration_text(\n                _spoken_boundary(cta, label="CTA final"),\n                label="CTA final",\n                require_complete_cta=True,\n            )'''
    text = _replace_once(text, old_cta, new_cta, label="guard/prepare-cta")

    old_segmented = '''        def segmented_guarded(self: Any, *, main_text: str, cta_text: str, **kwargs: Any):\n            main_clean = validate_narration_text(main_text, label="narração principal")\n            cta_clean = sanitize_narration_text(cta_text)'''
    new_segmented = '''        def segmented_guarded(self: Any, *, main_text: str, cta_text: str, **kwargs: Any):\n            main_clean = validate_narration_text(\n                _spoken_boundary(main_text, label="narração principal"),\n                label="narração principal",\n            )\n            cta_clean = sanitize_narration_text(\n                _spoken_boundary(cta_text, label="CTA final")\n            )'''
    text = _replace_once(text, old_segmented, new_segmented, label="guard/segmented-boundary")

    old_audio = '''        def generate_audio_guarded(self: Any, text: str, *args: Any, **kwargs: Any):\n            raw = _clean(text)\n            clean = validate_narration_text(raw, label="texto enviado ao TTS")'''
    new_audio = '''        def generate_audio_guarded(self: Any, text: str, *args: Any, **kwargs: Any):\n            # Última barreira antes do provider: nunca normalize/achate antes de\n            # separar fala de roteiro técnico.\n            raw = str(text or "").strip()\n            spoken = _spoken_boundary(raw, label="texto enviado ao TTS")\n            clean = validate_narration_text(spoken, label="texto enviado ao TTS")'''
    text = _replace_once(text, old_audio, new_audio, label="guard/generate-audio-boundary")

    text = text.replace('"version": 3,', '"version": 4,')
    text = text.replace('debug["narration_contract_version"] = 3', 'debug["narration_contract_version"] = 4')
    text = text.replace('video_generator_cls._codexia_narration_contract_guard_version = 3', 'video_generator_cls._codexia_narration_contract_guard_version = 4')
    return text


def patch_youtube_gate(text: str) -> str:
    text = _replace_once(
        text,
        'from app.services.narration_contract_guard import NarrationContractError, validate_narration_text\n',
        'from app.services.narration_contract_guard import NarrationContractError, validate_narration_text\n'
        'from app.services.spoken_text_boundary import prepare_spoken_narration_text\n',
        label="youtube-gate/import-boundary",
    )
    text = _replace_once(
        text,
        'MAX_TEXT_CHARS = 30000\nSUPPORTED_VOICES = {"pt-BR-FranciscaNeural", "pt-BR-AntonioNeural"}',
        'MAX_TEXT_CHARS = 30000\nNARRATION_GATE_CONTRACT_VERSION = 4\nSUPPORTED_VOICES = {"pt-BR-FranciscaNeural", "pt-BR-AntonioNeural"}',
        label="youtube-gate/version-constant",
    )
    old_validate = '''        try:\n            validated = validate_narration_text(value)\n        except NarrationContractError as exc:\n            raise YouTubeNarrationGateError(\n                f"Narração bloqueada antes do TTS: {exc}",\n                code="NARRATION_CONTRACT_BLOCKED",\n                status_code=422,\n            ) from exc\n        return str(validated or "").strip()'''
    new_validate = '''        try:\n            # CODEXIA_SPOKEN_TEXT_BOUNDARY_V4\n            spoken = prepare_spoken_narration_text(value)\n            validated = validate_narration_text(spoken, label="narração do YouTube Auto")\n        except (ValueError, NarrationContractError) as exc:\n            raise YouTubeNarrationGateError(\n                f"Narração bloqueada antes do TTS: {exc}",\n                code="NARRATION_CONTRACT_BLOCKED",\n                status_code=422,\n            ) from exc\n        return str(validated or "").strip()'''
    text = _replace_once(text, old_validate, new_validate, label="youtube-gate/normalize-boundary")
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
    old_restore = '''                if isinstance(old, dict) and old.get("approved") and old.get("text_sha256") == meta["text_sha256"]:\n                    meta["approved"] = True'''
    new_restore = '''                if (\n                    isinstance(old, dict)\n                    and old.get("approved")\n                    and int(old.get("narration_contract_version") or 0) == NARRATION_GATE_CONTRACT_VERSION\n                    and old.get("text_sha256") == meta["text_sha256"]\n                ):\n                    meta["approved"] = True'''
    text = _replace_once(text, old_restore, new_restore, label="youtube-gate/no-old-approval-restore")
    old_approve = '''        expected_hash = hashlib.sha256(spoken.encode("utf-8")).hexdigest()\n        if str(meta.get("text_sha256") or "") != expected_hash:'''
    new_approve = '''        if int(meta.get("narration_contract_version") or 0) != NARRATION_GATE_CONTRACT_VERSION:\n            raise YouTubeNarrationGateError(\n                "Esta narração foi criada por um contrato antigo e foi invalidada por segurança. Gere uma nova prévia.",\n                code="STALE_NARRATION_CONTRACT",\n                status_code=409,\n            )\n        expected_hash = hashlib.sha256(spoken.encode("utf-8")).hexdigest()\n        if str(meta.get("text_sha256") or "") != expected_hash:'''
    return _replace_once(text, old_approve, new_approve, label="youtube-gate/reject-old-cache")


def patch_narration_lab(text: str) -> str:
    text = _replace_once(
        text,
        'from app.services.narration_contract_guard import (\n    NarrationContractError,\n    validate_narration_text,\n)\n',
        'from app.services.narration_contract_guard import (\n    NarrationContractError,\n    validate_narration_text,\n)\n'
        'from app.services.spoken_text_boundary import prepare_spoken_narration_text\n',
        label="narration-lab/import-boundary",
    )
    old_validate = '''        try:\n            clean_text = validate_narration_text(payload.get("text"), label="amostra de narração")\n        except NarrationContractError as exc:\n            raise NarrationLabError(str(exc), code="NARRATION_CONTRACT_BLOCKED") from exc'''
    new_validate = '''        try:\n            # CODEXIA_SPOKEN_TEXT_BOUNDARY_V4\n            spoken = prepare_spoken_narration_text(payload.get("text"))\n            clean_text = validate_narration_text(spoken, label="amostra de narração")\n        except (ValueError, NarrationContractError) as exc:\n            raise NarrationLabError(str(exc), code="NARRATION_CONTRACT_BLOCKED") from exc'''
    text = _replace_once(text, old_validate, new_validate, label="narration-lab/text-boundary")
    old_fingerprint = '''        fingerprint_payload = {\n            "contract_version": 1,'''
    new_fingerprint = '''        fingerprint_payload = {\n            # CODEXIA_SPOKEN_TEXT_BOUNDARY_V4: invalida MP3s cacheados pelo contrato antigo.\n            "contract_version": 4,'''
    return _replace_once(text, old_fingerprint, new_fingerprint, label="narration-lab/cache-version")


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
        GUARD: [MARKER, "_spoken_boundary", "narration_contract_guard_version = 4"],
        YOUTUBE_GATE: [MARKER, "NARRATION_GATE_CONTRACT_VERSION = 4", "STALE_NARRATION_CONTRACT", "prepare_spoken_narration_text"],
        NARRATION_LAB: [MARKER, '"contract_version": 4', "prepare_spoken_narration_text"],
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
