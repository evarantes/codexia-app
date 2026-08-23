from __future__ import annotations

import argparse
from pathlib import Path

try:
    # Funciona quando importado a partir da raiz do repositório.
    from scripts import apply_ready_video_asset_repair as legacy
except ModuleNotFoundError:
    # Funciona quando executado diretamente: python scripts/<arquivo>.py
    import apply_ready_video_asset_repair as legacy


ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app/routers/youtube.py"
GENERATOR = ROOT / "app/services/video_generator.py"
INDEX = ROOT / "app/static/index.html"
MARKER = "CODEXIA_READY_VIDEO_ASSET_REPAIR_V2"


class PatchError(RuntimeError):
    pass


def _insert_after_first(text: str, anchor: str, insertion: str, label: str) -> str:
    if insertion.strip() and insertion.strip() in text:
        return text
    pos = text.find(anchor)
    if pos < 0:
        raise PatchError(f"{label}: âncora não encontrada")
    pos += len(anchor)
    return text[:pos] + insertion + text[pos:]


def _insert_before_first(text: str, anchor: str, insertion: str, label: str) -> str:
    if insertion.strip() and insertion.strip() in text:
        return text
    pos = text.find(anchor)
    if pos < 0:
        raise PatchError(f"{label}: âncora não encontrada")
    return text[:pos] + insertion + text[pos:]


def _replace_first(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    pos = text.find(old)
    if pos < 0:
        raise PatchError(f"{label}: trecho não encontrado")
    return text[:pos] + new + text[pos + len(old):]


def _patch_visual_selected_guard(text: str) -> str:
    """Adiciona o limite do reparo sem apagar condições de hardenings anteriores."""
    if "can_use_selected_image =" in text:
        return text

    marker = "                selected_image_index = None\n"
    start = text.find(marker)
    if start < 0:
        raise PatchError("grupos além do pool preservado: selected_image_index não encontrado")
    search_from = start + len(marker)
    block_end = text.find("                elif use_single_bg", search_from)
    if block_end < 0:
        block_end = min(len(text), search_from + 4000)

    cursor = search_from
    condition_pos = -1
    condition_end = -1
    condition_expr = ""
    while cursor < block_end:
        pos = text.find("                if ", cursor, block_end)
        if pos < 0:
            break
        line_end = text.find("\n", pos, block_end)
        if line_end < 0:
            line_end = block_end
        line = text[pos:line_end]
        stripped = line.strip()
        if "selected_image_paths" in stripped and stripped.startswith("if ") and stripped.endswith(":"):
            condition_pos = pos
            condition_end = line_end
            condition_expr = stripped[3:-1].strip()
            break
        cursor = line_end + 1

    if condition_pos < 0 or not condition_expr:
        raise PatchError("grupos além do pool preservado: condição selected_image_paths não encontrada")

    replacement = f'''                # CODEXIA_READY_VIDEO_ASSET_REPAIR_V2
                # Preserve qualquer condição já adicionada por hardenings anteriores.
                # Em reparo, cada imagem preservada é usada no máximo uma vez;
                # grupos seguintes seguem para geração sob RecoveryImageCallBudget.
                repair_complete_visuals = bool(isinstance(plan, dict) and plan.get("repair_complete_visuals"))
                can_use_selected_image = bool({condition_expr}) and (
                    (not repair_complete_visuals)
                    or int(visual_group_id or 0) < len(selected_image_paths)
                )
                if can_use_selected_image:'''
    return text[:condition_pos] + replacement + text[condition_end:]


RAW_REPAIR_GUARD = '''
    # CODEXIA_READY_VIDEO_ASSET_REPAIR_V2
    # Uma correção editorial precisa reconstruir voz e vídeo. Não permita que
    # um checkpoint antigo converta a tarefa em render-only.
    if bool(payload.get("repair_mode") or payload.get("repair_exclude_video")):
        payload["force_render_only"] = False
        return payload
'''

SEED_AUDIO_GUARD = '''
                # CODEXIA_READY_VIDEO_ASSET_REPAIR_V2
                # O áudio antigo pode ser tecnicamente válido, mas conter
                # metadados falados. O roteiro de reparo é a fonte autoritativa.
                if isinstance(seed_script, dict) and bool(seed_script.get("repair_complete_visuals")):
                    seed_audio_ok = False
'''

PROMOTE_GUARD = '''
    # CODEXIA_READY_VIDEO_ASSET_REPAIR_V2
    # O MP4 anterior permanece para auditoria, porém nunca pode ser promovido
    # como resultado de uma correção editorial.
    if bool(payload.get("repair_mode") or payload.get("repair_exclude_video")):
        return None
'''

TARGET_SELECTED_OLD = '''        if selected_image_count > 0:
            return min(scene_count, selected_image_count)'''
TARGET_SELECTED_NEW = '''        # CODEXIA_READY_VIDEO_ASSET_REPAIR_V2
        # Em reparo, imagens preservadas são um pool inicial, não o teto visual.
        repair_complete_visuals = bool(isinstance(plan, dict) and plan.get("repair_complete_visuals"))
        if selected_image_count > 0 and not repair_complete_visuals:
            return min(scene_count, selected_image_count)'''

VISUAL_SELECTED_OLD = '''                selected_image_index = None
                if selected_image_paths:
                    bg_image_path = self._selected_image_for_visual_group(
                        selected_image_paths,
                        visual_group_id,
                    )'''
VISUAL_SELECTED_NEW = '''                selected_image_index = None
                # CODEXIA_READY_VIDEO_ASSET_REPAIR_V2
                # Use cada imagem preservada uma vez. Grupos seguintes entram
                # na geração controlada pelo RecoveryImageCallBudget.
                repair_complete_visuals = bool(isinstance(plan, dict) and plan.get("repair_complete_visuals"))
                can_use_selected_image = bool(selected_image_paths) and (
                    (not repair_complete_visuals)
                    or int(visual_group_id or 0) < len(selected_image_paths)
                )
                if can_use_selected_image:
                    bg_image_path = self._selected_image_for_visual_group(
                        selected_image_paths,
                        visual_group_id,
                    )'''

OPENING_ANCHOR = '''        provided_cover = str(cover_image_path or "").strip()
        if provided_cover and os.path.exists(provided_cover):
            return {
                "path": provided_cover,
                "source": "provided_cover_image",
                "generated": False,
                "generation_attempted": False,
                "generation_error": None,
                "fallback_reason": None,
            }
'''
OPENING_REPAIR = '''
        # CODEXIA_READY_VIDEO_ASSET_REPAIR_V2
        # A abertura não deve consumir uma chamada paga que foi confirmada para
        # completar cenas. Reaproveite a primeira imagem válida já preservada.
        repair_selected = str(selected_primary_path or "").strip()
        if bool(isinstance(plan, dict) and plan.get("repair_complete_visuals")) and repair_selected and os.path.exists(repair_selected):
            return {
                "path": repair_selected,
                "source": "repair_selected_primary",
                "generated": False,
                "generation_attempted": False,
                "generation_error": None,
                "fallback_reason": "repair_budget_reserved_for_missing_story_visuals",
            }
'''


def _repair_endpoints() -> str:
    endpoints = legacy.ENDPOINTS
    # O runtime model pode ignorar campos extras; por isso a intenção editorial
    # também viaja dentro do seeded_script, que é preservado pelo contrato atual.
    endpoints = endpoints.replace(
        'script["repair_complete_visuals"] = True\n',
        'script["repair_complete_visuals"] = True\n    script["repair_regenerate_audio"] = True\n    script["repair_exclude_video"] = True\n',
        1,
    )
    return endpoints


def patch_youtube(text: str) -> str:
    # Não altera VideoRequest: isso evita conflito com os hardenings que o
    # antecedem. O payload bruto e seeded_script carregam as flags de reparo.
    if "CODEXIA_READY_VIDEO_ASSET_REPAIR_V2" not in text:
        text = _insert_after_first(
            text,
            '    payload.setdefault("force_reuse_assets", True)\n',
            RAW_REPAIR_GUARD,
            "bloqueio de render-only no reparo",
        )
        text = _insert_after_first(
            text,
            '                seed_audio_ok = _file_ok(seed_audio_path)\n',
            SEED_AUDIO_GUARD,
            "bloqueio de áudio antigo",
        )
        text = _insert_after_first(
            text,
            'def _recovery_try_promote_final_render(payload: Dict[str, Any], task_id: str) -> Optional[Dict[str, Any]]:\n',
            PROMOTE_GUARD,
            "bloqueio de promoção do MP4 antigo",
        )
    endpoints = _repair_endpoints()
    if "/schedule/{video_id}/repair-with-assets" not in text:
        text = _insert_before_first(
            text,
            '@router.get("/diagnostics/video_generation")',
            endpoints,
            "endpoints de correção seletiva",
        )
    return text


def patch_generator(text: str) -> str:
    if "if selected_image_count > 0 and not repair_complete_visuals:" not in text:
        text = _replace_first(text, TARGET_SELECTED_OLD, TARGET_SELECTED_NEW, "imagens preservadas não são teto")
    if "can_use_selected_image =" not in text:
        if VISUAL_SELECTED_OLD in text:
            text = _replace_first(text, VISUAL_SELECTED_OLD, VISUAL_SELECTED_NEW, "grupos além do pool preservado")
        else:
            text = _patch_visual_selected_guard(text)
    if "repair_selected_primary" not in text:
        text = _insert_after_first(text, OPENING_ANCHOR, OPENING_REPAIR, "abertura sem gasto adicional")
    return text


def patch_index(text: str) -> str:
    if "Corrigir com ativos" not in text:
        count = text.count(legacy.SCHEDULED_BUTTON_OLD)
        if count <= 0:
            raise PatchError("botão Refazer da lista de publicação não encontrado")
        # Se a mesma ação aparece em mais de uma tabela de ScheduledVideo, todas
        # devem oferecer o reparo seguro quando houver task_id.
        text = text.replace(legacy.SCHEDULED_BUTTON_OLD, legacy.SCHEDULED_BUTTON_NEW)
    if "async repairScheduledVideoWithAssets(video)" not in text:
        text = _insert_before_first(
            text,
            '                async regenerateScheduledVideo(video) {',
            legacy.UI_METHOD,
            "método UI de correção",
        )
    return text


def apply() -> None:
    youtube = YOUTUBE.read_text(encoding="utf-8")
    generator = GENERATOR.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    YOUTUBE.write_text(patch_youtube(youtube), encoding="utf-8")
    GENERATOR.write_text(patch_generator(generator), encoding="utf-8")
    INDEX.write_text(patch_index(index), encoding="utf-8")


def check() -> None:
    youtube = YOUTUBE.read_text(encoding="utf-8")
    generator = GENERATOR.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    checks = [
        (youtube, "/schedule/{video_id}/repair-with-assets", "endpoint de reparo"),
        (youtube, "repair_mode", "flag de payload de reparo"),
        (youtube, "seed_audio_ok = False", "exclusão do áudio antigo"),
        (youtube, "O MP4 anterior permanece para auditoria", "exclusão do MP4 antigo"),
        (generator, "repair_complete_visuals", "pool preservado sem teto"),
        (generator, "can_use_selected_image", "geração após imagens preservadas"),
        (generator, "repair_selected_primary", "abertura sem consumir budget"),
        (index, "Corrigir com ativos", "botão de correção"),
        (index, "repairScheduledVideoWithAssets", "método de correção"),
    ]
    missing = [label for content, token, label in checks if token not in content]
    if missing:
        raise PatchError("reparo seletivo incompleto: " + ", ".join(missing))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        args.check = True
    if args.apply:
        apply()
    if args.check:
        check()


if __name__ == "__main__":
    main()
