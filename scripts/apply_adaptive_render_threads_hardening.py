#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT / "app/services/video_generator.py"
MARKER = "CODEXIA_ADAPTIVE_FFMPEG_THREADS_V1"


class PatchError(RuntimeError):
    pass


def patch_video(text: str) -> str:
    if MARKER not in text:
        needle = "from app.services.media_probe import duration_sync_tolerance_seconds\n"
        replacement = (
            "from app.services.media_probe import duration_sync_tolerance_seconds\n"
            "from app.services.render_performance import choose_ffmpeg_threads  # CODEXIA_ADAPTIVE_FFMPEG_THREADS_V1\n"
        )
        if needle not in text:
            raise PatchError("import de media_probe não encontrado")
        text = text.replace(needle, replacement, 1)

    pattern = re.compile(r"threads=1(?P<suffix>\s*,)")
    matches = list(pattern.finditer(text))
    if matches:
        text = pattern.sub(r"threads=choose_ffmpeg_threads()\g<suffix>", text)

    if "threads=choose_ffmpeg_threads()" not in text:
        raise PatchError("nenhum write_videofile passou a usar threads adaptativas")
    return text


def apply(*, write: bool) -> int:
    original = VIDEO.read_text(encoding="utf-8")
    transformed = patch_video(original)
    if patch_video(transformed) != transformed:
        raise PatchError("patch não idempotente")
    changed = int(transformed != original)
    if changed and write:
        VIDEO.write_text(transformed, encoding="utf-8")
    print(f"Adaptive render threads hardening: {changed} arquivo(s) {'aplicados' if write else 'necessários'}")
    return changed


def check() -> None:
    text = patch_video(VIDEO.read_text(encoding="utf-8"))
    if MARKER not in text:
        raise PatchError("marcador de threads adaptativas ausente")
    if re.search(r"threads=1\s*,", text):
        raise PatchError("write_videofile ainda contém threads=1 fixo")
    if text.count("threads=choose_ffmpeg_threads()") < 2:
        raise PatchError("esperados pelo menos dois caminhos de render adaptativos")


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
        print(f"ERRO ADAPTIVE RENDER THREADS: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
