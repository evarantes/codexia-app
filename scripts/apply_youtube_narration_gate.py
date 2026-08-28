from __future__ import annotations

import argparse
from pathlib import Path

INDEX = Path("app/static/index.html")
JS = Path("app/static/youtube_narration_gate.js")
TAG = '<script src="/static/youtube_narration_gate.js"></script>'
MARKER = "</body>"

OLD_BUTTON_LOOKUP = "return [...card.querySelectorAll('button')].find(btn => normalizeText(btn.textContent).toLowerCase().includes('gerar vídeo narrado')) || null;"
NEW_BUTTON_LOOKUP = "return [...card.querySelectorAll('button')].find(btn => !btn.closest('[data-youtube-narration-gate]') && normalizeText(btn.textContent).toLowerCase().includes('gerar vídeo narrado')) || null;"


def apply() -> bool:
    changed = False
    text = INDEX.read_text(encoding="utf-8")
    if TAG not in text:
        if MARKER not in text:
            raise SystemExit("youtube narration gate: </body> marker not found")
        text = text.replace(MARKER, f"    {TAG}\n{MARKER}", 1)
        INDEX.write_text(text, encoding="utf-8")
        changed = True

    if not JS.is_file():
        raise SystemExit("youtube narration gate: JS asset missing")
    source = JS.read_text(encoding="utf-8")
    if OLD_BUTTON_LOOKUP in source:
        source = source.replace(OLD_BUTTON_LOOKUP, NEW_BUTTON_LOOKUP, 1)
        JS.write_text(source, encoding="utf-8")
        changed = True
    elif NEW_BUTTON_LOOKUP not in source:
        raise SystemExit("youtube narration gate: canonical video button lookup not found")

    return changed


def check() -> None:
    text = INDEX.read_text(encoding="utf-8")
    if text.count(TAG) != 1:
        raise SystemExit(f"youtube narration gate: expected exactly one script tag, found {text.count(TAG)}")
    if not JS.is_file():
        raise SystemExit("youtube narration gate: JS asset missing")
    source = JS.read_text(encoding="utf-8")
    required = [
        "Gerar primeiro o áudio da narração",
        "Avançar para geração do vídeo com este áudio",
        "/youtube/narration-lab/production-preview",
        "reuse_audio_from",
        "approved_narration_text_sha256",
        NEW_BUTTON_LOOKUP,
    ]
    missing = [item for item in required if item not in source]
    if missing:
        raise SystemExit(f"youtube narration gate: JS contract missing {missing}")
    if OLD_BUTTON_LOOKUP in source:
        raise SystemExit("youtube narration gate: supervised button can still shadow canonical video button")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply and/or --check")
    if args.apply:
        changed = apply()
        print("youtube narration gate:", "applied" if changed else "already applied")
    if args.check:
        check()
        print("youtube narration gate: OK")


if __name__ == "__main__":
    main()
