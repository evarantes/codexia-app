from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "app/main.py"
MARKER = "CODEXIA_LOCAL_RENDER_WORKER_PHASE1"


class PatchError(RuntimeError):
    pass


IMPORT_OLD = "from app.routers import books, marketing, settings, video, crm, webhook, youtube, youtube_series, book_factory, auth, diagnostics, hotmart, music, admin, social_media, image_storyboard, whatsapp"
IMPORT_NEW = IMPORT_OLD + ", local_render_worker"
ROUTER_OLD = "app.include_router(humor_factory.router)"
ROUTER_NEW = ROUTER_OLD + "\napp.include_router(local_render_worker.router)  # CODEXIA_LOCAL_RENDER_WORKER_PHASE1"


def patch(text: str) -> str:
    if MARKER in text:
        return text
    if text.count(IMPORT_OLD) != 1:
        raise PatchError("import anchor do main.py não encontrado exatamente uma vez")
    if text.count(ROUTER_OLD) != 1:
        raise PatchError("router anchor do main.py não encontrado exatamente uma vez")
    text = text.replace(IMPORT_OLD, IMPORT_NEW, 1)
    text = text.replace(ROUTER_OLD, ROUTER_NEW, 1)
    return text


def apply() -> None:
    original = MAIN.read_text(encoding="utf-8")
    transformed = patch(original)
    if transformed != original:
        MAIN.write_text(transformed, encoding="utf-8")
    if patch(transformed) != transformed:
        raise PatchError("patch não é idempotente")


def check() -> None:
    text = MAIN.read_text(encoding="utf-8")
    required = (
        "local_render_worker",
        "app.include_router(local_render_worker.router)",
        MARKER,
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError("integração local worker incompleta: " + ", ".join(missing))
    compile(text, str(MAIN), "exec")


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
        print(f"ERRO LOCAL RENDER WORKER PHASE1: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
