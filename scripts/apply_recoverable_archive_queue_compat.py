from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app/routers/youtube.py"

MARKER = "CODEXIA_RECOVERABLE_ARCHIVE_QUEUE_V1"
OLD = 'discarded = request_cancel_task(task_id, message="Tarefa descartada definitivamente da recuperação pelo usuário.")'
NEW = 'discarded = request_cancel_task(task_id, message="Tarefa falhada descartada pelo usuário.")'


class PatchError(RuntimeError):
    pass


def apply() -> None:
    text = YOUTUBE.read_text(encoding="utf-8")
    if MARKER not in text:
        raise PatchError("hardening de histórico recuperável deve ser aplicado antes da compatibilidade")
    if NEW in text:
        return
    count = text.count(OLD)
    if count != 1:
        raise PatchError(f"mensagem de descarte esperada 1 vez, encontrada {count}")
    YOUTUBE.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")


def check() -> None:
    text = YOUTUBE.read_text(encoding="utf-8")
    if MARKER not in text or NEW not in text:
        raise PatchError("compatibilidade da mensagem de descarte não aplicada")
    if OLD in text:
        raise PatchError("mensagem incompatível ainda presente")


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
