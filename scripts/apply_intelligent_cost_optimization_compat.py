from __future__ import annotations

import argparse

try:
    from scripts import apply_intelligent_cost_optimization as base
except ModuleNotFoundError:
    import apply_intelligent_cost_optimization as base


MARKER = "CODEXIA_INTELLIGENT_COST_OPTIMIZATION_COMPAT_V3"

SMALL_SEED_ANCHOR = '''                seed_script_ok = _is_valid_seed_script(seed_script)'''

SMALL_SEED_REPLACEMENT = '''                # CODEXIA_INTELLIGENT_COST_OPTIMIZATION_V1
                # Compat V3: use a confirmação/payload como fallback antes da
                # validação. A âncora curta preserva o guard de áudio do reparo
                # que é inserido entre seed_audio_ok e seed_images_ok.
                request_seed_script = getattr(request, "seeded_script", None)
                if isinstance(request_seed_script, dict) and not _is_valid_seed_script(seed_script):
                    seed_script = dict(request_seed_script)

                request_selected_images = getattr(request, "selected_images", None)
                if not seed_selected_images and isinstance(request_selected_images, list):
                    seed_selected_images = [
                        str(x).strip()
                        for x in request_selected_images
                        if isinstance(x, str) and str(x).strip()
                    ]
                if isinstance(seed_script, dict) and seed_selected_images:
                    seed_script = dict(seed_script)
                    seed_script["selected_images"] = list(seed_selected_images)

                if not seed_audio_path:
                    request_reuse_audio = getattr(request, "reuse_audio_from", None)
                    if isinstance(request_reuse_audio, dict):
                        seed_audio_path = str(
                            request_reuse_audio.get("output_path")
                            or request_reuse_audio.get("final_audio_path")
                            or request_reuse_audio.get("audio_path")
                            or ""
                        ).strip()
                        if not seed_narration_text:
                            seed_narration_text = str(
                                request_reuse_audio.get("final_text_sent_to_tts")
                                or request_reuse_audio.get("narration_text")
                                or ""
                            ).strip()

                seed_script_ok = _is_valid_seed_script(seed_script)'''


def _patch_index_all(text: str) -> str:
    """Protect every equivalent retry entry point, never only the first one."""
    if base.MARKER in text:
        return text
    count = text.count(base.UI_RETRY_OLD)
    if count < 1:
        raise base.PatchError("retry optimization confirmation UI: nenhum caminho legado encontrado")
    text = text.replace(base.UI_RETRY_OLD, base.UI_RETRY_NEW)
    return text.rstrip() + f"\n<!-- {base.MARKER} -->\n"


def _configure_base() -> None:
    base.WORKER_SEED_ANCHOR = SMALL_SEED_ANCHOR
    base.WORKER_SEED_NEW = SMALL_SEED_REPLACEMENT
    # O V2/V3 de reparo pode produzir mais de um caminho equivalente de retry.
    # Todos precisam consultar o retry-plan e exigir o mesmo plan_hash.
    base.patch_index = _patch_index_all


def apply() -> None:
    _configure_base()
    base.apply()


def check() -> None:
    _configure_base()
    base.check()
    text = base.YOUTUBE.read_text(encoding="utf-8")
    if "request_seed_script = getattr(request, \"seeded_script\", None)" not in text:
        raise base.PatchError("compat V3 não aplicou fallback de roteiro")
    if "request_selected_images = getattr(request, \"selected_images\", None)" not in text:
        raise base.PatchError("compat V3 não aplicou fallback de imagens")
    if "request_reuse_audio = getattr(request, \"reuse_audio_from\", None)" not in text:
        raise base.PatchError("compat V3 não aplicou fallback de áudio")

    index = base.INDEX.read_text(encoding="utf-8")
    if base.UI_RETRY_OLD in index:
        raise base.PatchError("compat V3 deixou caminho de retry sem confirmação inteligente")
    if "retry-plan" not in index or "optimization_plan_hash" not in index:
        raise base.PatchError("compat V3 não protegeu a UI com plano/hash de confirmação")


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
    except base.PatchError as exc:
        print(f"ERRO INTELLIGENT COST OPTIMIZATION COMPAT V3: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
