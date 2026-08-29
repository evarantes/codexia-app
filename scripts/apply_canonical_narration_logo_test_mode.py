#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

INDEX = Path("app/static/index.html")
GATE_JS = Path("app/static/youtube_narration_gate.js")
LOGO_JS = Path("app/static/youtube_logo_test_mode.js")

LOGO_TAG = '<script src="/static/youtube_logo_test_mode.js"></script>'
BODY = "</body>"
MARKER = "CODEXIA_DIRECT_USES_SUPERVISION_PATH_V1"

OLD_DIRECT = '''    panel.querySelector('[data-ng-direct]').addEventListener('click', () => {\n      clearApproved();\n      const button = existingGenerateButton(card);\n      if (button) button.click(); else setStatus(panel, 'Não encontrei o botão de geração do vídeo.', 'error');\n    });'''

NEW_DIRECT = '''    panel.querySelector('[data-ng-direct]').addEventListener('click', async () => {\n      // CODEXIA_DIRECT_USES_SUPERVISION_PATH_V1\n      // O modo direto não possui mais um TTS paralelo. Ele executa exatamente\n      // generatePreview -> approvePreview -> continueWithApproved, pulando apenas\n      // a escuta humana. Assim o MP3 que sai da Supervisão é o MP3 do vídeo.\n      clearApproved();\n      setStatus(panel, 'Gerando a narração pelo mesmo caminho da Supervisão antes de iniciar o vídeo…');\n      await generatePreview(panel, card);\n      if (!preview || !preview.preview_id) return;\n      await approvePreview(panel, card);\n      if (!approved || !approved.reuse_audio_from) return;\n      await continueWithApproved(panel, card);\n    });'''


def apply() -> bool:
    changed = False
    if not LOGO_JS.is_file():
        raise SystemExit("canonical narration/logo mode: JS do teste com logo ausente")

    index = INDEX.read_text(encoding="utf-8")
    if LOGO_TAG not in index:
        if BODY not in index:
            raise SystemExit("canonical narration/logo mode: </body> não encontrado")
        index = index.replace(BODY, f"    {LOGO_TAG}\n{BODY}", 1)
        INDEX.write_text(index, encoding="utf-8")
        changed = True

    gate = GATE_JS.read_text(encoding="utf-8")
    if MARKER not in gate:
        count = gate.count(OLD_DIRECT)
        if count != 1:
            raise SystemExit(f"canonical narration/logo mode: esperado 1 listener direto legado, encontrado {count}")
        gate = gate.replace(OLD_DIRECT, NEW_DIRECT, 1)
        GATE_JS.write_text(gate, encoding="utf-8")
        changed = True
    return changed


def check() -> None:
    index = INDEX.read_text(encoding="utf-8")
    gate = GATE_JS.read_text(encoding="utf-8")
    logo = LOGO_JS.read_text(encoding="utf-8")
    if index.count(LOGO_TAG) != 1:
        raise SystemExit("canonical narration/logo mode: script do teste com logo deve aparecer exatamente uma vez")
    required_gate = [
        MARKER,
        "await generatePreview(panel, card)",
        "await approvePreview(panel, card)",
        "await continueWithApproved(panel, card)",
    ]
    missing_gate = [needle for needle in required_gate if needle not in gate]
    if missing_gate:
        raise SystemExit(f"canonical narration/logo mode: caminho direto divergente: {missing_gate}")
    if OLD_DIRECT in gate:
        raise SystemExit("canonical narration/logo mode: caminho direto legado ainda presente")
    required_logo = [
        "/youtube/narration-lab/production-preview",
        "/youtube/narration-lab/production-preview/logo-test",
        "imagens IA: 0",
        "áudio reutilizado: SIM",
    ]
    missing_logo = [needle for needle in required_logo if needle not in logo]
    if missing_logo:
        raise SystemExit(f"canonical narration/logo mode: contrato do teste econômico incompleto: {missing_logo}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply e/ou --check")
    if args.apply:
        print("canonical narration/logo mode:", "applied" if apply() else "already applied")
    if args.check:
        check()
        print("canonical narration/logo mode: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
