from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "app/routers/youtube.py"
MARKER = "# CODEXIA_FINAL_RENDER_RECOVERY_SCOPE_V1"

OLD = '''            message = (\n                "Recuperação segura não encontrou um MP4 final utilizável antes da retomada paga. "\n                f"Motivo: {base_reason}. Arquivos analisados: {len(diagnostics)}"\n                + (f"; rejeições: {reason_text}." if reason_text else ".")\n                + " Os ativos existentes foram preservados e nenhuma nova mídia foi gerada."\n            )\n            update_task(task_id, message=message)\n            return {\n                "recovered": False,\n                "blocked": True,\n                "task_id": task_id,\n                "message": message,\n                "reason": base_reason,\n            }'''

NEW = '''            message = (\n                "Recuperação segura não encontrou um MP4 final utilizável antes da retomada paga. "\n                f"Motivo: {base_reason}. Arquivos analisados: {len(diagnostics)}"\n                + (f"; rejeições: {reason_text}." if reason_text else ".")\n                + " Os ativos existentes foram preservados e nenhuma nova mídia foi gerada."\n            )\n            # Ausência de MP4 só deve bloquear quando o checkpoint V3 já marcou\n            # esta tentativa como recuperação protegida contra nova mídia paga.\n            # Um retry normal continua usando o fluxo histórico quando não há\n            # render final para reaproveitar.\n            if bool(payload.get("_recovery_block_paid_regeneration")):\n                update_task(task_id, message=message)\n                return {\n                    "recovered": False,\n                    "blocked": True,\n                    "task_id": task_id,\n                    "message": message,\n                    "reason": base_reason,\n                }\n            return None'''


class PatchError(RuntimeError):
    pass


def apply() -> None:
    text = TARGET.read_text(encoding="utf-8")
    if "CODEXIA_FINAL_RENDER_RECOVERY_COMPAT_V2" not in text:
        raise PatchError("final render recovery compat v2 deve ser aplicado antes do scope v1")
    if NEW not in text:
        count = text.count(OLD)
        if count != 1:
            raise PatchError(f"bloco de diagnóstico esperado uma vez; encontrado {count}")
        text = text.replace(OLD, NEW, 1)
    if MARKER not in text:
        text = text.rstrip() + f"\n\n{MARKER}\n"
    TARGET.write_text(text, encoding="utf-8")


def check() -> None:
    text = TARGET.read_text(encoding="utf-8")
    required = (
        MARKER,
        'if bool(payload.get("_recovery_block_paid_regeneration")):',
        "Um retry normal continua usando o fluxo histórico",
        "return None",
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError(f"final render recovery scope incompleto: {missing}")
    if OLD in text:
        raise PatchError("bloqueio incondicional sem MP4 ainda existe")
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
        print(f"ERRO FINAL RENDER RECOVERY SCOPE V1: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
