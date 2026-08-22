from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "app/services/production_manifest.py"
DIAGNOSTICS = ROOT / "app/services/production_manifest_diagnostics.py"

MARKER_POOL = "CODEXIA_TASK_OWNED_IMAGE_POOL_RECOVERY_V2"
MARKER_COMPAT = "CODEXIA_RECOVERY_RENDER_STALL_COMPAT_V1"


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1)


POOL_OLD = '''            if not raw_references:\n                for entry in remaining[:needed]:\n                    if _choose(entry):\n                        fallback_count += 1\n            else:'''

POOL_NEW = '''            # CODEXIA_RECOVERY_RENDER_STALL_COMPAT_V1\n            # O fallback determinístico só é permitido quando a própria tarefa\n            # comprova que já alcançou renderização (85%+). Em estágios iniciais\n            # mantemos a proteção histórica contra escolher candidatos ambíguos.\n            try:\n                _manifest_progress = int(manifest.get("progress") or 0)\n            except Exception:\n                _manifest_progress = 0\n            if not raw_references and _manifest_progress >= 85:\n                for entry in remaining[:needed]:\n                    if _choose(entry):\n                        fallback_count += 1\n            else:'''

DIAG_OLD = '''def _audio_trust(manifest: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:\n    # O plano é a fonte única de confiança: ele já cruza o artefato físico com\n    # o checkpoint da mesma tarefa e, quando disponível, com o hash do MP3.\n    found = bool(plan.get("audio_found"))\n    reusable = bool(plan.get("audio_reusable"))\n    trust = str(plan.get("audio_trust") or ("missing" if not found else "legacy_unverified"))\n    if not found:\n        reason = "Nenhum áudio durável compatível foi encontrado para esta tarefa."\n    elif reusable and trust == "audio_checkpoint_v2":\n        reason = "Áudio físico confere com o checkpoint persistido da própria tarefa."\n    elif reusable:\n        reason = "Áudio persistido pelo contrato de narração está apto para reutilização."\n    else:\n        reason = "Áudio físico existe, mas não há prova suficiente para reutilização automática."\n    return {"found": found, "reusable": reusable, "trust": trust, "reason": reason}'''

DIAG_NEW = '''def _audio_trust(manifest: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:\n    # CODEXIA_RECOVERY_RENDER_STALL_COMPAT_V1\n    # Planos novos carregam explicitamente audio_found/audio_reusable/audio_trust\n    # após cruzar o arquivo com o checkpoint da mesma tarefa. Para manifestos\n    # antigos e testes que ainda não possuem esses campos, preserve a regra\n    # histórica baseada em tts_immediate; jamais transforme áudio legado em\n    # reutilizável apenas porque audio_ok veio True de um mock/versão antiga.\n    explicit = any(key in plan for key in ("audio_found", "audio_reusable", "audio_trust"))\n    if explicit:\n        found = bool(plan.get("audio_found"))\n        reusable = bool(plan.get("audio_reusable"))\n        trust = str(plan.get("audio_trust") or ("missing" if not found else "legacy_unverified"))\n        if not found:\n            reason = "Nenhum áudio durável compatível foi encontrado para esta tarefa."\n        elif reusable and trust == "audio_checkpoint_v2":\n            reason = "Áudio físico confere com o checkpoint persistido da própria tarefa."\n        elif reusable:\n            reason = "Áudio persistido pelo contrato de narração está apto para reutilização."\n        else:\n            reason = "Áudio físico existe, mas não há prova suficiente para reutilização automática."\n        return {"found": found, "reusable": reusable, "trust": trust, "reason": reason}\n\n    audio_items = _valid_artifacts(manifest, "audio")\n    audio_path = str(plan.get("audio_path") or "").strip()\n    found = bool(audio_items or audio_path)\n    protected = any(\n        str(item.get("source") or "").strip().lower() == "tts_immediate"\n        for item in audio_items\n    )\n    if not found:\n        return {\n            "found": False,\n            "reusable": False,\n            "trust": "missing",\n            "reason": "Nenhum áudio durável válido foi encontrado no manifesto.",\n        }\n    if protected:\n        return {\n            "found": True,\n            "reusable": bool(plan.get("audio_ok")),\n            "trust": "narration_contract_v1",\n            "reason": "Áudio foi persistido pelo guard de narração após validação pré-TTS.",\n        }\n    return {\n        "found": True,\n        "reusable": False,\n        "trust": "legacy_unverified",\n        "reason": (\n            "Áudio anterior ao guard de narração não possui prova de sanitização. "\n            "Ele pode existir fisicamente, mas não deve ser reutilizado automaticamente."\n        ),\n    }'''


def apply() -> None:
    service = SERVICE.read_text(encoding="utf-8")
    if MARKER_POOL not in service:
        raise PatchError("hardening principal de recovery/render não foi aplicado antes do compat")
    service = _replace_once(service, POOL_OLD, POOL_NEW, "conservative task-owned image fallback")
    SERVICE.write_text(service, encoding="utf-8")

    diagnostics = DIAGNOSTICS.read_text(encoding="utf-8")
    diagnostics = _replace_once(diagnostics, DIAG_OLD, DIAG_NEW, "backward-compatible audio diagnostic")
    DIAGNOSTICS.write_text(diagnostics, encoding="utf-8")


def check() -> None:
    service = SERVICE.read_text(encoding="utf-8")
    diagnostics = DIAGNOSTICS.read_text(encoding="utf-8")
    if MARKER_COMPAT not in service or "_manifest_progress >= 85" not in service:
        raise PatchError("fallback conservador de imagens não aplicado")
    if MARKER_COMPAT not in diagnostics or 'explicit = any(key in plan' not in diagnostics:
        raise PatchError("compatibilidade do diagnóstico de áudio não aplicada")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.apply:
        apply()
    if args.check:
        check()
    if not args.apply and not args.check:
        parser.error("use --apply e/ou --check")


if __name__ == "__main__":
    main()
