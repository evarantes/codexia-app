from __future__ import annotations

import argparse
from pathlib import Path

try:
    from scripts import apply_ready_video_asset_repair_v4 as v4
    from scripts import apply_ready_queue_title_edit as title_edit
except ModuleNotFoundError:
    import apply_ready_video_asset_repair_v4 as v4
    import apply_ready_queue_title_edit as title_edit


ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app/routers/youtube.py"
MARKER = "CODEXIA_READY_VIDEO_ASSET_REPAIR_V3"


class PatchError(RuntimeError):
    pass


RETRY_OLD = '''        payload = dict(saved_payload)
        payload["force_reuse_assets"] = True
        payload.pop("force_render_only", None)
        payload = _maybe_enable_render_only_flags(payload, task_id)'''

RETRY_NEW = '''        payload = dict(saved_payload)
        payload["force_reuse_assets"] = True
        payload.pop("force_render_only", None)
        # CODEXIA_READY_VIDEO_ASSET_REPAIR_V3
        # O botão Corrigir com ativos já exigiu confirmação EXATA do orçamento
        # e persistiu a mesma cópia dentro do seeded_script. Quando essas duas
        # cópias ainda coincidem, não volte ao guard legado de "Retomar duas
        # vezes"; ele pertence ao fluxo genérico de recuperação e pode bloquear
        # um reparo já confirmado. Qualquer divergência falha fechado e mantém o
        # comportamento legado.
        try:
            from app.services.ready_video_repair_gate import confirmed_ready_video_repair_budget
            ready_repair_confirmed = confirmed_ready_video_repair_budget(payload)
        except Exception:
            ready_repair_confirmed = False
        if ready_repair_confirmed:
            payload["force_render_only"] = False
            payload.pop("_recovery_block_paid_regeneration", None)
            payload.pop("_recovery_missing_assets", None)
        else:
            payload = _maybe_enable_render_only_flags(payload, task_id)'''


def patch_youtube(text: str) -> str:
    if MARKER in text:
        return text
    if "/schedule/{video_id}/repair-with-assets" not in text:
        raise PatchError("ready-video repair V2 deve ser aplicado antes do V3")
    count = text.count(RETRY_OLD)
    if count != 1:
        raise PatchError(f"bloco retry esperado uma vez; encontrado {count}")
    return text.replace(RETRY_OLD, RETRY_NEW, 1)


def apply() -> None:
    original = YOUTUBE.read_text(encoding="utf-8")
    transformed = patch_youtube(original)
    if patch_youtube(transformed) != transformed:
        raise PatchError("patch V3 não é idempotente")
    if transformed != original:
        YOUTUBE.write_text(transformed, encoding="utf-8")
    # V4 e a edição segura do título são encadeados aqui porque API/CI já executam V3.
    v4.apply()
    title_edit.apply()


def check() -> None:
    text = YOUTUBE.read_text(encoding="utf-8")
    required = (
        MARKER,
        "confirmed_ready_video_repair_budget(payload)",
        'payload.pop("_recovery_block_paid_regeneration", None)',
        'payload.pop("_recovery_missing_assets", None)',
        "ready_repair_confirmed",
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError("ready-video repair V3 incompleto: " + ", ".join(missing))
    compile(text, str(YOUTUBE), "exec")
    v4.check()
    title_edit.check()


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
    except (PatchError, v4.PatchError, title_edit.PatchError) as exc:
        print(f"ERRO READY VIDEO ASSET REPAIR V3/V4/TITLE: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
