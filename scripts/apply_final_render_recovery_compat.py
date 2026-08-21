from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "app/routers/youtube.py"

OLD_SNAPSHOT = '''        unified_obj = _recovery_unified_snapshot(db, str(task_id))\n        uv = ('''
NEW_SNAPSHOT = '''        try:\n            unified_obj = _recovery_unified_snapshot(db, str(task_id))\n        except Exception:\n            # Legacy/unit-test recovery paths may not expose a complete UnifiedVideo\n            # row. Final-render salvage is opportunistic and must fail open without\n            # changing the canonical retry behavior.\n            unified_obj = {}\n        uv = ('''

OLD_UV_GUARD = '''        if uv is None:\n            return None\n\n        choice = _recovery_choose_existing_final_video'''
NEW_UV_GUARD = '''        if uv is None or not isinstance(getattr(uv, "task_id", None), str):\n            return None\n\n        choice = _recovery_choose_existing_final_video'''

MARKER = "# CODEXIA_FINAL_RENDER_RECOVERY_COMPAT_V1"


class PatchError(RuntimeError):
    pass


def apply() -> None:
    text = TARGET.read_text(encoding="utf-8")
    if "CODEXIA_FINAL_RENDER_RECOVERY_V1_START" not in text:
        raise PatchError("final render recovery v1 deve ser aplicado antes do compat")
    changed = False
    if NEW_SNAPSHOT not in text:
        if OLD_SNAPSHOT not in text:
            raise PatchError("anchor de snapshot unificado não encontrado")
        text = text.replace(OLD_SNAPSHOT, NEW_SNAPSHOT, 1)
        changed = True
    if NEW_UV_GUARD not in text:
        if OLD_UV_GUARD not in text:
            raise PatchError("anchor de guarda UnifiedVideo não encontrado")
        text = text.replace(OLD_UV_GUARD, NEW_UV_GUARD, 1)
        changed = True
    if MARKER not in text:
        text = text.rstrip() + f"\n\n{MARKER}\n"
        changed = True
    if changed:
        TARGET.write_text(text, encoding="utf-8")


def check() -> None:
    text = TARGET.read_text(encoding="utf-8")
    required = (
        MARKER,
        "try:\n            unified_obj = _recovery_unified_snapshot(db, str(task_id))",
        'if uv is None or not isinstance(getattr(uv, "task_id", None), str):',
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError(f"final render recovery compat incompleto: {missing}")
    compile(text, str(TARGET), "exec")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply e/ou --check")
    try:
        if args.apply:
            apply()
        if args.check:
            check()
    except PatchError as exc:
        print(f"ERRO FINAL RENDER RECOVERY COMPAT: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
