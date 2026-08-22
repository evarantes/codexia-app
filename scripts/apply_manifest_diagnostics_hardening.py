#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app/routers/youtube.py"
MARKER = "CODEXIA_MANIFEST_DIAGNOSTICS_V1"


class PatchError(RuntimeError):
    pass


def patch_youtube(text: str) -> str:
    if MARKER in text:
        return text

    route_marker = '@router.get("/diagnostics/video_generation")'
    next_marker = 'def process_video_generation_payload('
    start = text.find(route_marker)
    if start < 0:
        raise PatchError("rota de diagnóstico não encontrada")
    end = text.find(next_marker, start)
    if end < 0:
        raise PatchError("fim da rota de diagnóstico não encontrado")

    block = text[start:end]
    return_marker = "\n    return report\n"
    pos = block.rfind(return_marker)
    if pos < 0:
        raise PatchError("return final do diagnóstico não encontrado")

    injected = (
        "\n    # CODEXIA_MANIFEST_DIAGNOSTICS_V1\n"
        "    # Somente leitura: nunca inicia recuperação nem chama provedores pagos.\n"
        "    if task_id:\n"
        "        try:\n"
        "            from app.services.production_manifest_diagnostics import enrich_video_diagnostic_report\n"
        "            report = enrich_video_diagnostic_report(report, task_id=task_id)\n"
        "        except Exception as exc:\n"
        "            report.setdefault(\"checks\", []).append({\n"
        "                \"name\": \"Manifesto da produção\",\n"
        "                \"ok\": False,\n"
        "                \"value\": f\"erro ao consultar: {type(exc).__name__}: {str(exc)[:180]}\",\n"
        "            })\n"
        "            report.setdefault(\"recommendations\", []).append(\n"
        "                \"Não reinicie a tarefa até o manifesto ser verificado.\"\n"
        "            )\n"
    )

    block = block[:pos] + injected + block[pos:]
    return text[:start] + block + text[end:]


def apply(*, write: bool) -> int:
    original = YOUTUBE.read_text(encoding="utf-8")
    transformed = patch_youtube(original)
    if patch_youtube(transformed) != transformed:
        raise PatchError("patch não idempotente")
    changed = int(transformed != original)
    if changed and write:
        YOUTUBE.write_text(transformed, encoding="utf-8")
    print(f"Manifest diagnostics hardening: {changed} arquivo(s) {'aplicados' if write else 'necessários'}")
    return changed


def check() -> None:
    text = patch_youtube(YOUTUBE.read_text(encoding="utf-8"))
    if MARKER not in text or "enrich_video_diagnostic_report" not in text:
        raise PatchError("contrato de diagnóstico de manifesto ausente")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply ou --check")
    try:
        apply(write=bool(args.apply))
        if args.check:
            check()
    except PatchError as exc:
        print(f"ERRO MANIFEST DIAGNOSTICS: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
