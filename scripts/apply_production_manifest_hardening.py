from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK_MANAGER = ROOT / "app/services/task_manager.py"
YOUTUBE = ROOT / "app/routers/youtube.py"
SERVICE = ROOT / "app/services/production_manifest.py"
MARKER_TASK = "# CODEXIA_PRODUCTION_MANIFEST_TASK_SYNC_V1"
MARKER_YT = "# CODEXIA_PRODUCTION_MANIFEST_RECOVERY_V1"


TASK_OLD = '''def _redis_set(task_id: str, data: Dict[str, Any]):\n    if not _redis_conn:\n        return\n    try:\n        _redis_conn.set(_REDIS_PREFIX + task_id, json.dumps(data), ex=60 * 60)\n    except Exception:\n        pass'''

TASK_NEW = '''def _redis_set(task_id: str, data: Dict[str, Any]):\n    # CODEXIA_PRODUCTION_MANIFEST_TASK_SYNC_V1\n    # Persist every canonical task snapshot before Redis so a cache outage,\n    # deploy or late quality-gate failure cannot orphan already-paid assets.\n    try:\n        from app.services.production_manifest import sync_task_snapshot\n        sync_task_snapshot(str(task_id), data if isinstance(data, dict) else {})\n    except Exception:\n        # Manifest persistence is protective and must never make the pipeline fail.\n        pass\n    if not _redis_conn:\n        return\n    try:\n        _redis_conn.set(_REDIS_PREFIX + task_id, json.dumps(data), ex=60 * 60)\n    except Exception:\n        pass'''


IMPORT_OLD = '''from app.services.media_probe import media_durations_match, probe_media_file'''
IMPORT_NEW = '''from app.services.media_probe import media_durations_match, probe_media_file\nfrom app.services.production_manifest import (\n    build_recovery_plan,\n    confirm_or_prepare_partial_recovery,\n    recovery_confirmation_message,\n    recovery_ready_message,\n)'''


PAID_GUARD_OLD = '''        payload = _maybe_enable_render_only_flags(payload, task_id)\n        if bool(payload.get("_recovery_block_paid_regeneration")):\n            missing = [str(item) for item in (payload.get("_recovery_missing_assets") or []) if str(item or "").strip()]\n            missing_label = ", ".join(missing) if missing else "ativos necessários"\n            recovery_message = (\n                "Recuperação segura bloqueada antes de novas chamadas pagas: "\n                f"faltam {missing_label}. Os ativos existentes foram preservados. "\n                "Nenhuma nova mídia foi gerada nesta tentativa."\n            )\n            update_task(task_id, message=recovery_message)\n            raise HTTPException(status_code=409, detail=recovery_message)\n        try:\n            VideoRequest(**payload)'''

PAID_GUARD_NEW = '''        payload = _maybe_enable_render_only_flags(payload, task_id)\n        if bool(payload.get("_recovery_block_paid_regeneration")):\n            missing = [str(item) for item in (payload.get("_recovery_missing_assets") or []) if str(item or "").strip()]\n            # Quando roteiro + áudio estão preservados e apenas imagens faltam,\n            # transformar o bloqueio cego em um plano explícito de menor custo.\n            # O primeiro clique em Retomar apenas mostra custo/quantidade; o\n            # segundo clique, para o mesmo plano e dentro de 10 min, confirma.\n            if missing and set(missing).issubset({"imagens"}):\n                try:\n                    manifest_decision = confirm_or_prepare_partial_recovery(task_id, payload)\n                except Exception:\n                    manifest_decision = {"allow": False, "plan": {}, "reason": "manifest_error"}\n                manifest_plan = manifest_decision.get("plan") if isinstance(manifest_decision, dict) else {}\n                if isinstance(manifest_decision, dict) and bool(manifest_decision.get("allow")):\n                    recovered_payload = manifest_decision.get("payload")\n                    if isinstance(recovered_payload, dict):\n                        payload = recovered_payload\n                    ready_message = recovery_ready_message(manifest_plan if isinstance(manifest_plan, dict) else {})\n                    update_task(task_id, message=ready_message)\n                elif isinstance(manifest_plan, dict) and manifest_plan.get("action") == "regenerate_missing_images":\n                    recovery_message = recovery_confirmation_message(manifest_plan)\n                    update_task(task_id, message=recovery_message)\n                    raise HTTPException(status_code=409, detail=recovery_message)\n                else:\n                    missing_label = ", ".join(missing) if missing else "ativos necessários"\n                    recovery_message = (\n                        "Recuperação segura bloqueada antes de novas chamadas pagas: "\n                        f"faltam {missing_label}. Os ativos existentes foram preservados. "\n                        "Nenhuma nova mídia foi gerada nesta tentativa."\n                    )\n                    update_task(task_id, message=recovery_message)\n                    raise HTTPException(status_code=409, detail=recovery_message)\n            else:\n                missing_label = ", ".join(missing) if missing else "ativos necessários"\n                recovery_message = (\n                    "Recuperação segura bloqueada antes de novas chamadas pagas: "\n                    f"faltam {missing_label}. Os ativos existentes foram preservados. "\n                    "Nenhuma nova mídia foi gerada nesta tentativa."\n                )\n                update_task(task_id, message=recovery_message)\n                raise HTTPException(status_code=409, detail=recovery_message)\n        try:\n            VideoRequest(**payload)'''


SCOPE_OLD = '''            if bool(payload.get("_recovery_block_paid_regeneration")):\n                update_task(task_id, message=message)\n                return {\n                    "recovered": False,\n                    "blocked": True,\n                    "task_id": task_id,\n                    "message": message,\n                    "reason": base_reason,\n                }\n            return None'''

SCOPE_NEW = '''            if bool(payload.get("_recovery_block_paid_regeneration")):\n                # Se o único ativo ausente são imagens e o manifesto prova que\n                # roteiro + áudio são reutilizáveis, deixe o fluxo chegar ao\n                # guard de confirmação de custo. Não gerar nada aqui.\n                try:\n                    manifest_plan = build_recovery_plan(task_id, payload_override=payload)\n                except Exception:\n                    manifest_plan = {}\n                if not (\n                    isinstance(manifest_plan, dict)\n                    and manifest_plan.get("action") == "regenerate_missing_images"\n                ):\n                    update_task(task_id, message=message)\n                    return {\n                        "recovered": False,\n                        "blocked": True,\n                        "task_id": task_id,\n                        "message": message,\n                        "reason": base_reason,\n                    }\n            return None'''


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, label: str) -> tuple[str, bool]:
    if new in text:
        return text, False
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1), True


def apply() -> None:
    task_text = TASK_MANAGER.read_text(encoding="utf-8")
    task_text, _ = _replace_once(task_text, TASK_OLD, TASK_NEW, "task manifest sync")
    TASK_MANAGER.write_text(task_text, encoding="utf-8")

    yt_text = YOUTUBE.read_text(encoding="utf-8")
    if "CODEXIA_RECOVERY_CHECKPOINT_V3_START" not in yt_text:
        raise PatchError("recovery checkpoint v3 deve ser aplicado antes do manifesto")
    if "CODEXIA_FINAL_RENDER_RECOVERY_SCOPE_V1" not in yt_text:
        raise PatchError("final render scope v1 deve ser aplicado antes do manifesto")
    yt_text, _ = _replace_once(yt_text, IMPORT_OLD, IMPORT_NEW, "production manifest import")
    yt_text, _ = _replace_once(yt_text, PAID_GUARD_OLD, PAID_GUARD_NEW, "paid recovery confirmation")
    yt_text, _ = _replace_once(yt_text, SCOPE_OLD, SCOPE_NEW, "final render partial recovery scope")
    if MARKER_YT not in yt_text:
        yt_text = yt_text.rstrip() + f"\n\n{MARKER_YT}\n"
    YOUTUBE.write_text(yt_text, encoding="utf-8")


def check() -> None:
    task_text = TASK_MANAGER.read_text(encoding="utf-8")
    yt_text = YOUTUBE.read_text(encoding="utf-8")
    service_text = SERVICE.read_text(encoding="utf-8")
    task_required = (
        MARKER_TASK,
        "sync_task_snapshot(str(task_id)",
        "before Redis",
    )
    yt_required = (
        MARKER_YT,
        "confirm_or_prepare_partial_recovery(task_id, payload)",
        "recovery_confirmation_message(manifest_plan)",
        "recovery_ready_message(manifest_plan",
        'manifest_plan.get("action") == "regenerate_missing_images"',
    )
    service_required = (
        "def sync_task_snapshot(",
        "def build_recovery_plan(",
        "def confirm_or_prepare_partial_recovery(",
        "Nenhuma chamada paga foi feita ainda",
        "_recovery_generate_missing_images_only",
    )
    missing = [token for token in task_required if token not in task_text]
    missing.extend(token for token in yt_required if token not in yt_text)
    missing.extend(token for token in service_required if token not in service_text)
    if missing:
        raise PatchError(f"production manifest hardening incompleto: {missing}")
    if PAID_GUARD_OLD in yt_text:
        raise PatchError("guard antigo de recuperação paga ainda está presente")
    compile(task_text, str(TASK_MANAGER), "exec")
    compile(yt_text, str(YOUTUBE), "exec")
    compile(service_text, str(SERVICE), "exec")


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
        print(f"ERRO PRODUCTION MANIFEST HARDENING: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
