from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app/routers/youtube.py"
MARKER = "CODEXIA_STAGE6_RETRY_COST_GUARD_V1"


class PatchError(RuntimeError):
    pass


OLD = '''            if choice.get("reason") == "ambiguous_final_render_candidates":\n                message = (\n                    "Recuperação segura encontrou mais de um MP4 final compatível e não escolheu automaticamente. "\n                    "Nenhuma nova mídia foi gerada."\n                )\n                update_task(task_id, message=message)\n                return {"recovered": False, "blocked": True, "task_id": task_id, "message": message}\n            return None'''

NEW = '''            if choice.get("reason") == "ambiguous_final_render_candidates":\n                message = (\n                    "Recuperação segura encontrou mais de um MP4 final compatível e não escolheu automaticamente. "\n                    "Nenhuma nova mídia foi gerada."\n                )\n                update_task(task_id, message=message)\n                return {"recovered": False, "blocked": True, "task_id": task_id, "message": message}\n\n            # CODEXIA_STAGE6_RETRY_COST_GUARD_V1\n            # Se a falha ocorreu já no render final, um novo retry não pode\n            # silenciosamente voltar para TTS/imagens depois de o MP4 local\n            # não passar na validação. Pare aqui e exija uma nova ação explícita\n            # do usuário. Isso transforma a autorização paga anterior em uma\n            # autorização de tentativa única para a mídia já produzida.\n            recovery_checkpoint = result_obj.get("recovery_checkpoint") if isinstance(result_obj.get("recovery_checkpoint"), dict) else {}\n            runtime_telemetry = result_obj.get("runtime_telemetry") if isinstance(result_obj.get("runtime_telemetry"), dict) else {}\n            manifest_obj = result_obj.get("production_manifest") if isinstance(result_obj.get("production_manifest"), dict) else {}\n            stage6_text = " ".join(\n                str(value or "").strip().lower()\n                for value in (\n                    recovery_checkpoint.get("stage"),\n                    recovery_checkpoint.get("checkpoint"),\n                    runtime_telemetry.get("stage"),\n                    manifest_obj.get("checkpoint"),\n                    manifest_obj.get("stage"),\n                    getattr(row, "message", None),\n                )\n            )\n            if (\n                "stage_6_render" in stage6_text\n                or "renderizando vídeo final" in stage6_text\n                or "renderizando video final" in stage6_text\n                or "renderizando arquivo final" in stage6_text\n            ):\n                message = (\n                    "O MP4 preservado do render final não passou na validação automática. "\n                    "A recuperação foi interrompida antes de qualquer nova narração ou imagem paga. "\n                    "Use Corrigir com ativos para revisar e autorizar explicitamente uma nova tentativa, se desejar."\n                )\n                update_task(task_id, message=message)\n                return {\n                    "recovered": False,\n                    "blocked": True,\n                    "task_id": task_id,\n                    "message": message,\n                    "reason": choice.get("reason") or "stage6_final_render_not_recoverable",\n                    "checked_without_paid_calls": True,\n                }\n            return None'''


def patch_youtube(text: str) -> str:
    if MARKER in text:
        return text
    if "_recovery_try_promote_final_render" not in text:
        raise PatchError("final-render recovery deve ser aplicado antes do stage6 cost guard")
    count = text.count(OLD)
    if count != 1:
        raise PatchError(f"bloco de fallback do final render esperado 1 vez; encontrado {count}")
    return text.replace(OLD, NEW, 1)


def apply() -> None:
    original = YOUTUBE.read_text(encoding="utf-8")
    transformed = patch_youtube(original)
    if patch_youtube(transformed) != transformed:
        raise PatchError("patch stage6 cost guard não é idempotente")
    if transformed != original:
        YOUTUBE.write_text(transformed, encoding="utf-8")


def check() -> None:
    text = YOUTUBE.read_text(encoding="utf-8")
    required = (
        MARKER,
        '"stage_6_render" in stage6_text',
        "A recuperação foi interrompida antes de qualquer nova narração ou imagem paga.",
        '"checked_without_paid_calls": True',
        "Use Corrigir com ativos para revisar e autorizar explicitamente uma nova tentativa",
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError("stage6 retry cost guard incompleto: " + ", ".join(missing))
    compile(text, str(YOUTUBE), "exec")


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
        print(f"ERRO STAGE6 RETRY COST GUARD: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
