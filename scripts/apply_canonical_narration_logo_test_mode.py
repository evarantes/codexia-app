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
SOURCE_TEXT_MARKER = "CODEXIA_APPROVED_SOURCE_TEXT_GUARD_V1"
GLOBAL_NETWORK_MARKER = "CODEXIA_APPROVED_NARRATION_NETWORK_GUARD_V1"
GLOBAL_LOGO_ONLY_MARKERS = [
    "codexia.logoOnlyVisuals.v1",
    "Usar apenas a logo do canal",
    "logo_only_visuals",
    "X-Codexia-Logo-Only-Visuals",
]
LEGACY_LOGO_TEST_MARKERS = [
    "/youtube/narration-lab/production-preview",
    "/youtube/narration-lab/production-preview/logo-test",
    "imagens IA: 0",
    "áudio reutilizado: SIM",
]

OLD_DIRECT = '''    panel.querySelector('[data-ng-direct]').addEventListener('click', () => {\n      clearApproved();\n      const button = existingGenerateButton(card);\n      if (button) button.click(); else setStatus(panel, 'Não encontrei o botão de geração do vídeo.', 'error');\n    });'''

NEW_DIRECT = '''    panel.querySelector('[data-ng-direct]').addEventListener('click', async () => {\n      // CODEXIA_DIRECT_USES_SUPERVISION_PATH_V1\n      // O modo direto não possui mais um TTS paralelo. Ele executa exatamente\n      // generatePreview -> approvePreview -> continueWithApproved, pulando apenas\n      // a escuta humana. Assim o MP3 que sai da Supervisão é o MP3 do vídeo.\n      clearApproved();\n      setStatus(panel, 'Gerando a narração pelo mesmo caminho da Supervisão antes de iniciar o vídeo…');\n      await generatePreview(panel, card);\n      if (!preview || !preview.preview_id) return;\n      await approvePreview(panel, card);\n      if (!approved || !approved.reuse_audio_from) return;\n      await continueWithApproved(panel, card);\n    });'''

OLD_APPROVED_SAVE = '''      saveApproved({\n        preview_id: data.preview_id,\n        text_sha256: data.text_sha256,\n        reuse_audio_from: data.reuse_audio_from,\n        approved_at: new Date().toISOString()\n      });'''

NEW_APPROVED_SAVE = '''      saveApproved({\n        // CODEXIA_APPROVED_SOURCE_TEXT_GUARD_V1\n        preview_id: data.preview_id,\n        text_sha256: data.text_sha256,\n        source_text_sha256: await sha256(text),\n        reuse_audio_from: data.reuse_audio_from,\n        approved_at: new Date().toISOString()\n      });'''

OLD_CONTINUE_GUARD = '''    const currentHash = await sha256(text);\n    if (currentHash !== approved.text_sha256) {\n      clearApproved();\n      panel.querySelector('[data-ng-continue]').disabled = true;\n      return setStatus(panel, 'O texto foi alterado após a aprovação. Gere e aprove uma nova narração antes de continuar.', 'error');\n    }'''

NEW_CONTINUE_GUARD = '''    const currentHash = await sha256(text);\n    const approvedSourceHash = approved.source_text_sha256 || '';\n    if (!approvedSourceHash || currentHash !== approvedSourceHash) {\n      clearApproved();\n      panel.querySelector('[data-ng-continue]').disabled = true;\n      return setStatus(panel, 'O texto foi alterado após a aprovação. Gere e aprove uma nova narração antes de continuar.', 'error');\n    }'''

OLD_INPUT_GUARD = '''    textarea.addEventListener('input', async () => {\n      if (!approved) return;\n      if (await sha256(textarea.value) !== approved.text_sha256) {\n        clearApproved();\n        panel.querySelector('[data-ng-continue]').disabled = true;\n        setStatus(panel, 'Texto alterado: a aprovação do áudio foi invalidada. Gere uma nova narração.', 'error');\n      }\n    });'''

NEW_INPUT_GUARD = '''    textarea.addEventListener('input', async () => {\n      if (!approved) return;\n      const approvedSourceHash = approved.source_text_sha256 || '';\n      if (!approvedSourceHash || await sha256(textarea.value) !== approvedSourceHash) {\n        clearApproved();\n        panel.querySelector('[data-ng-continue]').disabled = true;\n        setStatus(panel, 'Texto alterado: a aprovação do áudio foi invalidada. Gere uma nova narração.', 'error');\n      }\n    });'''

OLD_FETCH_GUARD = '''        const text = normalizeText(payload.story_content || payload.script_text || '');\n        if (text && await sha256(text) === approved.text_sha256) {\n          payload.reuse_audio_from = approved.reuse_audio_from;\n          payload.approved_narration_preview_id = approved.preview_id;\n          payload.approved_narration_text_sha256 = approved.text_sha256;\n          init = { ...init, body: JSON.stringify(payload) };\n        } else if (text) {\n          clearApproved();\n        }'''

NEW_FETCH_GUARD = '''        const text = normalizeText(payload.story_content || payload.script_text || '');\n        const approvedSourceHash = approved.source_text_sha256 || '';\n        if (text && approvedSourceHash && await sha256(text) === approvedSourceHash) {\n          payload.reuse_audio_from = approved.reuse_audio_from;\n          payload.approved_narration_preview_id = approved.preview_id;\n          payload.approved_narration_text_sha256 = approved.text_sha256;\n          init = { ...init, body: JSON.stringify(payload) };\n        } else if (text) {\n          clearApproved();\n        }'''

OLD_GLOBAL_FETCH_GUARD = '''        const payload = JSON.parse(init.body);\n        const text = normalizeText(payload.story_content || payload.script_text || '');\n        if (!text || await sha256(text) !== approved.text_sha256) {\n          approvedLaunchArmed = false;\n          clearApproved();\n          throw new Error('O texto do vídeo não corresponde à narração aprovada; geração bloqueada.');\n        }\n        payload.reuse_audio_from = approved.reuse_audio_from;'''

NEW_GLOBAL_FETCH_GUARD = '''        const payload = JSON.parse(init.body);\n        const text = normalizeText(payload.story_content || payload.script_text || '');\n        const approvedSourceHash = approved.source_text_sha256 || '';\n        if (!text || !approvedSourceHash || await sha256(text) !== approvedSourceHash) {\n          approvedLaunchArmed = false;\n          clearApproved();\n          throw new Error('O texto do vídeo não corresponde ao texto-fonte aprovado; geração bloqueada.');\n        }\n        payload.reuse_audio_from = approved.reuse_audio_from;'''


def _replace_once(text: str, old: str, new: str, label: str) -> tuple[str, bool]:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"canonical narration/logo mode: esperado 1 bloco {label}, encontrado {count}")
    return text.replace(old, new, 1), True


def apply() -> bool:
    changed = False
    if not LOGO_JS.is_file():
        raise SystemExit("canonical narration/logo mode: JS do modo com logo ausente")

    index = INDEX.read_text(encoding="utf-8")
    if LOGO_TAG not in index:
        if BODY not in index:
            raise SystemExit("canonical narration/logo mode: </body> não encontrado")
        index = index.replace(BODY, f"    {LOGO_TAG}\n{BODY}", 1)
        INDEX.write_text(index, encoding="utf-8")
        changed = True

    gate = GATE_JS.read_text(encoding="utf-8")
    gate_changed = False
    if MARKER not in gate:
        gate, _ = _replace_once(gate, OLD_DIRECT, NEW_DIRECT, "listener direto legado")
        gate_changed = True
    if SOURCE_TEXT_MARKER not in gate:
        gate, _ = _replace_once(gate, OLD_APPROVED_SAVE, NEW_APPROVED_SAVE, "saveApproved")
        gate, _ = _replace_once(gate, OLD_CONTINUE_GUARD, NEW_CONTINUE_GUARD, "continue guard")
        gate, _ = _replace_once(gate, OLD_INPUT_GUARD, NEW_INPUT_GUARD, "input guard")
        if GLOBAL_NETWORK_MARKER in gate:
            gate, _ = _replace_once(gate, OLD_GLOBAL_FETCH_GUARD, NEW_GLOBAL_FETCH_GUARD, "global fetch guard")
        else:
            gate, _ = _replace_once(gate, OLD_FETCH_GUARD, NEW_FETCH_GUARD, "fetch guard")
        gate_changed = True
    if gate_changed:
        GATE_JS.write_text(gate, encoding="utf-8")
        changed = True
    return changed


def check() -> None:
    index = INDEX.read_text(encoding="utf-8")
    gate = GATE_JS.read_text(encoding="utf-8")
    logo = LOGO_JS.read_text(encoding="utf-8")
    if index.count(LOGO_TAG) != 1:
        raise SystemExit("canonical narration/logo mode: script do modo com logo deve aparecer exatamente uma vez")
    required_gate = [
        MARKER,
        SOURCE_TEXT_MARKER,
        "source_text_sha256: await sha256(text)",
        "approved.source_text_sha256 || ''",
        "await generatePreview(panel, card)",
        "await approvePreview(panel, card)",
        "await continueWithApproved(panel, card)",
    ]
    missing_gate = [needle for needle in required_gate if needle not in gate]
    if missing_gate:
        raise SystemExit(f"canonical narration/logo mode: caminho de narração divergente: {missing_gate}")
    if OLD_DIRECT in gate:
        raise SystemExit("canonical narration/logo mode: caminho direto legado ainda presente")
    if "await sha256(text) === approved.text_sha256" in gate or "await sha256(text) !== approved.text_sha256" in gate:
        raise SystemExit("canonical narration/logo mode: frontend ainda compara texto bruto com hash do texto autenticado do TTS")

    global_mode_complete = all(needle in logo for needle in GLOBAL_LOGO_ONLY_MARKERS)
    legacy_mode_complete = all(needle in logo for needle in LEGACY_LOGO_TEST_MARKERS)
    if not global_mode_complete and not legacy_mode_complete:
        missing_global = [needle for needle in GLOBAL_LOGO_ONLY_MARKERS if needle not in logo]
        missing_legacy = [needle for needle in LEGACY_LOGO_TEST_MARKERS if needle not in logo]
        raise SystemExit(
            "canonical narration/logo mode: nenhum contrato válido encontrado; "
            f"global ausente={missing_global}; legado ausente={missing_legacy}"
        )


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
