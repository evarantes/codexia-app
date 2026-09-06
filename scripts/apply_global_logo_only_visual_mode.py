#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "app/main.py"
AI_GENERATOR = ROOT / "app/services/ai_generator.py"
AI_ROUTER = ROOT / "app/services/ai_router.py"
STORYBOARD = ROOT / "app/services/image_storyboard_service.py"
YOUTUBE = ROOT / "app/routers/youtube.py"
UNIFIED = ROOT / "app/services/unified_video_pipeline.py"
VIDEO_GENERATOR = ROOT / "app/services/video_generator.py"
INDEX = ROOT / "app/static/index.html"
PAGES = ROOT / "app/static/pages"

MARKER = "CODEXIA_GLOBAL_LOGO_ONLY_VISUALS_V1"
SCRIPT_TAG = '<script src="/static/youtube_logo_test_mode.js"></script>'


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"global logo-only: {label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1)


def _insert_after_def_signature(text: str, def_name: str, snippet: str, *, occurrence: int = 1) -> str:
    needle = f"def {def_name}("
    starts = [m.start() for m in re.finditer(re.escape(needle), text)]
    if len(starts) < occurrence:
        raise SystemExit(f"global logo-only: função {def_name} ocorrência {occurrence} não encontrada")
    start = starts[occurrence - 1]
    paren = text.find("(", start)
    depth = 0
    i = paren
    quote = None
    escaped = False
    while i < len(text):
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        else:
            if ch in {"'", '"'}:
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == ":" and depth == 0:
                nl = text.find("\n", i)
                if nl < 0:
                    raise SystemExit(f"global logo-only: fim da assinatura {def_name} inválido")
                return text[: nl + 1] + snippet + text[nl + 1 :]
        i += 1
    raise SystemExit(f"global logo-only: assinatura {def_name} não finalizada")


def _apply_provider_guard(path: Path, *, method_indent: str = "        ") -> bool:
    text = _read(path)
    local_marker = f"{MARKER}:provider:{path.name}"
    if local_marker in text:
        return False
    snippet = (
        f'{method_indent}# {local_marker}\n'
        f'{method_indent}from app.services.logo_only_visual_mode import image_provider_override\n'
        f'{method_indent}_logo_only_override = image_provider_override()\n'
        f'{method_indent}if _logo_only_override:\n'
        f'{method_indent}    return _logo_only_override\n'
    )
    text = _insert_after_def_signature(text, "generate_image", snippet)
    _write(path, text)
    return True


def _apply_main_middleware() -> bool:
    text = _read(MAIN)
    if f"{MARKER}:middleware" in text:
        return False
    block = f'''\n\n# {MARKER}:middleware\n@app.middleware("http")\nasync def _codexia_logo_only_visual_middleware(request: Request, call_next):\n    """Per-request fail-closed override for every synchronous image provider."""\n    from app.services.logo_only_visual_mode import (\n        LogoOnlyVisualModeError,\n        is_truthy,\n        logo_only_visual_context,\n    )\n\n    requested = is_truthy(request.headers.get("X-Codexia-Logo-Only-Visuals"))\n    if not requested:\n        return await call_next(request)\n\n    db = SessionLocal()\n    try:\n        try:\n            with logo_only_visual_context(True, db=db):\n                return await call_next(request)\n        except LogoOnlyVisualModeError as exc:\n            return JSONResponse(\n                status_code=422,\n                content={{\n                    "detail": {{\n                        "error": "official_logo_required",\n                        "message": str(exc),\n                        "logo_only_visuals": True,\n                    }}\n                }},\n            )\n    finally:\n        db.close()\n'''
    _write(MAIN, text.rstrip() + block + "\n")
    return True


def _apply_storyboard_guards() -> bool:
    text = _read(STORYBOARD)
    changed = False
    marker_image = f"{MARKER}:storyboard-image"
    if marker_image not in text:
        snippet = f'''    # {marker_image}\n    from app.services.logo_only_visual_mode import current_logo_only_visual\n    _logo_state = current_logo_only_visual()\n    if _logo_state:\n        _logo_path = str(_logo_state.get("path") or "")\n        _logo_url = str(_logo_state.get("url") or _logo_path)\n        return {{\n            "scene": int(index),\n            "prompt": (prompt or "").strip(),\n            "file": _logo_path,\n            "url": _logo_url,\n            "model_used": "official_channel_logo",\n            "images_generated": 0,\n            "logo_only_visuals": True,\n        }}\n'''
        text = _insert_after_def_signature(text, "generate_image", snippet)
        changed = True

    marker_storyboard = f"{MARKER}:storyboard-batch"
    if marker_storyboard not in text:
        snippet = f'''    # {marker_storyboard}\n    from app.services.logo_only_visual_mode import current_logo_only_visual\n    _logo_state = current_logo_only_visual()\n    if _logo_state:\n        qty = max(1, int(quantity or 1))\n        _logo_path = str(_logo_state.get("path") or "")\n        _logo_url = str(_logo_state.get("url") or _logo_path)\n        return {{\n            "success": True,\n            "quantity": qty,\n            "images_generated": 0,\n            "logo_only_visuals": True,\n            "images": [\n                {{"scene": i, "prompt": "", "file": _logo_path, "url": _logo_url, "model_used": "official_channel_logo"}}\n                for i in range(1, qty + 1)\n            ],\n        }}\n'''
        text = _insert_after_def_signature(text, "generate_storyboard_images", snippet)
        changed = True

    marker_thumb = f"{MARKER}:thumbnail"
    if marker_thumb not in text:
        snippet = f'''    # {marker_thumb}\n    from app.services.logo_only_visual_mode import current_logo_only_visual\n    _logo_state = current_logo_only_visual()\n    if _logo_state:\n        _logo_path = str(_logo_state.get("path") or "")\n        _logo_url = str(_logo_state.get("url") or _logo_path)\n        return {{\n            "file": _logo_path,\n            "url": _logo_url,\n            "base_file": _logo_path,\n            "base_url": _logo_url,\n            "text": (text or "").strip(),\n            "image_prompt_used": "official_channel_logo",\n            "images_generated": 0,\n            "thumbnail_generated_by_ai": False,\n            "logo_only_visuals": True,\n        }}\n'''
        text = _insert_after_def_signature(text, "generate_thumbnail_with_text", snippet)
        changed = True

    if changed:
        _write(STORYBOARD, text)
    return changed


def _apply_youtube_contract() -> bool:
    text = _read(YOUTUBE)
    changed = False

    video_field = "    editorial_review_ready: bool = False\n    logo_only_visuals: bool = False\n"
    if video_field not in text:
        old = "    editorial_review_ready: bool = False\n"
        text = _replace_once(text, old, old + "    logo_only_visuals: bool = False\n", "VideoRequest logo_only_visuals")
        changed = True

    story_images_field = "    image_mode: Optional[str] = None  # single | multiple\n    logo_only_visuals: bool = False\n"
    if story_images_field not in text:
        old = "    image_mode: Optional[str] = None  # single | multiple\n"
        text = _replace_once(text, old, old + "    logo_only_visuals: bool = False\n", "StoryImagesRequest logo_only_visuals")
        changed = True

    canonical_marker = f"{MARKER}:canonical-hash"
    if canonical_marker not in text:
        old = '        "custom_image_paths": custom_image_paths,\n'
        new = old + f'        "logo_only_visuals": bool(payload.get("logo_only_visuals")),  # {canonical_marker}\n'
        text = _replace_once(text, old, new, "canonical hash")
        changed = True

    dispatch_marker = f"{MARKER}:dispatch"
    if dispatch_marker not in text:
        old = '''def _dispatch_video_generation_task(payload: Dict[str, Any], task_id: str):\n    """Enfileira vídeo pesado no RQ e nunca cai para execução local em produção.\n'''
        new = f'''def _dispatch_video_generation_task(payload: Dict[str, Any], task_id: str):\n    """Enfileira vídeo pesado no RQ e nunca cai para execução local em produção.\n'''
        if old not in text:
            raise SystemExit("global logo-only: início de _dispatch_video_generation_task não encontrado")
        text = text.replace(old, new, 1)
        insertion_point = text.find('    payload = _maybe_enable_render_only_flags(payload, task_id)', text.find('def _dispatch_video_generation_task'))
        if insertion_point < 0:
            raise SystemExit("global logo-only: ponto de normalização do dispatch não encontrado")
        snippet = f'''    # {dispatch_marker}\n    from app.services.logo_only_visual_mode import apply_logo_only_to_payload, payload_requests_logo_only\n    if payload_requests_logo_only(payload):\n        _logo_db = SessionLocal()\n        try:\n            payload = apply_logo_only_to_payload(\n                payload,\n                db=_logo_db,\n                user_id=(payload or {{}}).get("user_id"),\n            )\n        finally:\n            _logo_db.close()\n\n'''
        text = text[:insertion_point] + snippet + text[insertion_point:]
        changed = True

    story_images_marker = f"{MARKER}:story-images"
    if story_images_marker not in text:
        anchor = "def _generate_story_images_payload(request: StoryImagesRequest, progress_callback=None) -> Dict[str, Any]:\n"
        if anchor not in text:
            raise SystemExit("global logo-only: _generate_story_images_payload não encontrado")
        snippet = f'''    # {story_images_marker}\n    if bool(getattr(request, "logo_only_visuals", False)):\n        from app.services.logo_only_visual_mode import resolve_official_logo\n        _logo_db = SessionLocal()\n        try:\n            _logo_path, _logo_url = resolve_official_logo(db=_logo_db)\n        finally:\n            _logo_db.close()\n        return {{\n            "count": 1,\n            "images_generated": 0,\n            "logo_only_visuals": True,\n            "images": [{{"url": _logo_url, "file": _logo_path, "prompt": "official_channel_logo", "reused": True}}],\n            "kind": (request.kind or "story").strip().lower(),\n            "aspect_ratio": request.aspect_ratio or "16:9",\n            "image_mode": "single",\n        }}\n'''
        text = text.replace(anchor, anchor + snippet, 1)
        changed = True

    if changed:
        _write(YOUTUBE, text)
    return changed


def _apply_unified_contract() -> bool:
    text = _read(UNIFIED)
    changed = False
    if "logo_only_visuals: bool = False" not in text:
        old = "    reuse_audio_from: Optional[Dict[str, Any]] = None\n    request_hash: Optional[str]"
        new = "    reuse_audio_from: Optional[Dict[str, Any]] = None\n    logo_only_visuals: bool = False\n    request_hash: Optional[str]"
        text = _replace_once(text, old, new, "UnifiedVideoRequest logo flag")
        changed = True

    marker = f"{MARKER}:unified-builder"
    if marker not in text:
        old = '''    try:\n        image_count = int(raw.get("image_count") or (8 if str(raw.get("image_mode") or "").lower() == "multiple" else 1))\n    except Exception:\n        image_count = 8\n    image_count = max(1, min(64, image_count))\n'''
        new = old + f'''    logo_only_visuals = bool(raw.get("logo_only_visuals"))  # {marker}\n    if logo_only_visuals:\n        image_count = 1\n'''
        text = _replace_once(text, old, new, "Unified image count")
        old2 = "        reuse_audio_from=(raw.get(\"reuse_audio_from\") if isinstance(raw.get(\"reuse_audio_from\"), dict) else None),\n        request_hash="
        new2 = "        reuse_audio_from=(raw.get(\"reuse_audio_from\") if isinstance(raw.get(\"reuse_audio_from\"), dict) else None),\n        logo_only_visuals=logo_only_visuals,\n        request_hash="
        text = _replace_once(text, old2, new2, "Unified builder flag")
        changed = True

    marker2 = f"{MARKER}:legacy-payload"
    if marker2 not in text:
        old = '    merged.setdefault("reuse_audio_from", req.reuse_audio_from)\n'
        new = old + f'    merged.setdefault("logo_only_visuals", bool(req.logo_only_visuals))  # {marker2}\n'
        text = _replace_once(text, old, new, "legacy logo flag")
        changed = True

    if changed:
        _write(UNIFIED, text)
    return changed


def _apply_renderer_guards() -> bool:
    text = _read(VIDEO_GENERATOR)
    changed = False

    opening_marker = f"{MARKER}:opening"
    if opening_marker not in text:
        signature = "    def _resolve_opening_background_image(\n"
        pos = text.find(signature)
        if pos < 0:
            raise SystemExit("global logo-only: opening resolver não encontrado")
        body_anchor = '        provided_cover = str(cover_image_path or "").strip()\n'
        body_pos = text.find(body_anchor, pos)
        if body_pos < 0:
            raise SystemExit("global logo-only: opening body anchor não encontrado")
        snippet = f'''        # {opening_marker}\n        if isinstance(plan, dict) and bool(plan.get("logo_only_visuals")):\n            logo_candidate = str(selected_primary_path or cover_image_path or plan.get("logo_only_logo_path") or "").strip()\n            if logo_candidate and os.path.exists(logo_candidate):\n                return {{\n                    "path": logo_candidate,\n                    "source": "official_channel_logo",\n                    "generated": False,\n                    "generation_attempted": False,\n                    "generation_error": None,\n                    "fallback_reason": None,\n                }}\n            raise Exception("Modo usar apenas a logo está ativo, mas a logo oficial não está disponível para a abertura.")\n\n'''
        text = text[:body_pos] + snippet + text[body_pos:]
        changed = True

    mode_marker = f"{MARKER}:plan-mode"
    if mode_marker not in text:
        old = '        kind_norm = str(plan.get("kind") or "").strip().lower() if isinstance(plan, dict) else ""\n'
        new = old + f'        logo_only_visuals = bool(plan.get("logo_only_visuals")) if isinstance(plan, dict) else False  # {mode_marker}\n'
        text = _replace_once(text, old, new, "renderer plan flag")
        changed = True

    music_marker = f"{MARKER}:music"
    if music_marker not in text:
        old = '''                    img_path = self._ensure_image_for_scene(\n                        image_prompt,\n                        text_fallback=title,\n                        aspect_ratio=aspect_ratio,\n                        status_callback=_music_status,\n                        max_rounds=image_max_rounds,\n                        allow_non_ai_fallback=allow_non_ai_fallback,\n                        paid_call_guard=paid_image_call_guard,\n                    )\n'''
        new = f'''                    # {music_marker}\n                    if logo_only_visuals:\n                        img_path = selected_primary_path\n                        if not img_path or not os.path.exists(img_path):\n                            raise Exception("Modo usar apenas a logo está ativo, mas a logo oficial não foi encontrada para o clipe.")\n                    else:\n                        img_path = self._ensure_image_for_scene(\n                            image_prompt,\n                            text_fallback=title,\n                            aspect_ratio=aspect_ratio,\n                            status_callback=_music_status,\n                            max_rounds=image_max_rounds,\n                            allow_non_ai_fallback=allow_non_ai_fallback,\n                            paid_call_guard=paid_image_call_guard,\n                        )\n'''
        text = _replace_once(text, old, new, "music image provider guard")
        changed = True

    report_marker = f"{MARKER}:report"
    if report_marker not in text:
        old = '            render_report["visual_plan"] = {\n                "continuity_anchor": continuity_anchor,\n'
        new = f'            render_report["visual_plan"] = {{\n                "logo_only_visuals": bool(logo_only_visuals),  # {report_marker}\n                "ai_image_generation_disabled": bool(logo_only_visuals),\n                "continuity_anchor": continuity_anchor,\n'
        text = _replace_once(text, old, new, "render report logo-only")
        changed = True

    if changed:
        _write(VIDEO_GENERATOR, text)
    return changed


def _inject_script_into_pages() -> bool:
    changed = False
    paths = [INDEX]
    if PAGES.is_dir():
        paths.extend(sorted(PAGES.rglob("*.html")))
    for path in paths:
        text = _read(path)
        if SCRIPT_TAG in text:
            continue
        if "</body>" not in text:
            continue
        text = text.replace("</body>", f"    {SCRIPT_TAG}\n</body>", 1)
        _write(path, text)
        changed = True
    return changed


def apply() -> bool:
    changed = False
    changed |= _apply_main_middleware()
    changed |= _apply_provider_guard(AI_GENERATOR)
    changed |= _apply_provider_guard(AI_ROUTER)
    changed |= _apply_storyboard_guards()
    changed |= _apply_youtube_contract()
    changed |= _apply_unified_contract()
    changed |= _apply_renderer_guards()
    changed |= _inject_script_into_pages()
    return changed


def check() -> None:
    requirements = {
        MAIN: [f"{MARKER}:middleware", "X-Codexia-Logo-Only-Visuals", "logo_only_visual_context"],
        AI_GENERATOR: [f"{MARKER}:provider:ai_generator.py", "image_provider_override"],
        AI_ROUTER: [f"{MARKER}:provider:ai_router.py", "image_provider_override"],
        STORYBOARD: [f"{MARKER}:storyboard-image", f"{MARKER}:storyboard-batch", f"{MARKER}:thumbnail", "images_generated"],
        YOUTUBE: ["logo_only_visuals: bool = False", f"{MARKER}:dispatch", f"{MARKER}:story-images", f"{MARKER}:canonical-hash"],
        UNIFIED: ["logo_only_visuals: bool = False", f"{MARKER}:unified-builder", f"{MARKER}:legacy-payload"],
        VIDEO_GENERATOR: [f"{MARKER}:opening", f"{MARKER}:music", f"{MARKER}:report", "ai_image_generation_disabled"],
    }
    for path, needles in requirements.items():
        text = _read(path)
        missing = [needle for needle in needles if needle not in text]
        if missing:
            raise SystemExit(f"global logo-only: contrato incompleto em {path}: {missing}")

    html_paths = [INDEX]
    if PAGES.is_dir():
        html_paths.extend(sorted(PAGES.rglob("*.html")))
    missing_script = [str(path.relative_to(ROOT)) for path in html_paths if "</body>" in _read(path) and SCRIPT_TAG not in _read(path)]
    if missing_script:
        raise SystemExit(f"global logo-only: script global ausente em páginas: {missing_script[:20]}")

    js = _read(ROOT / "app/static/youtube_logo_test_mode.js")
    for needle in [
        "Usar apenas a logo do canal",
        "logo_only_visuals",
        "X-Codexia-Logo-Only-Visuals",
        "Não gerar imagens nem thumbnail por IA",
    ]:
        if needle not in js:
            raise SystemExit(f"global logo-only: UI global incompleta: {needle}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply e/ou --check")
    if args.apply:
        print("global logo-only visual mode:", "applied" if apply() else "already applied")
    if args.check:
        check()
        print("global logo-only visual mode: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
