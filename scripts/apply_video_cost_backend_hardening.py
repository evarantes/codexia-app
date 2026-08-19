#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho original, encontrado {count}")
    return text.replace(old, new, 1)


def patch_main(text: str) -> str:
    text = _replace_once(
        text,
        "from app.routers import books, marketing, settings, video, crm, webhook, youtube, youtube_series, book_factory, auth, diagnostics, hotmart, music, admin, social_media, image_storyboard, whatsapp",
        "from app.routers import books, marketing, settings, video, crm, webhook, youtube, youtube_series, book_factory, auth, diagnostics, hotmart, music, admin, social_media, image_storyboard, whatsapp, video_costs",
        label="main/import-video-costs",
    )
    return _replace_once(
        text,
        "app.include_router(youtube.router)\napp.include_router(youtube_series.router)",
        "app.include_router(youtube.router)\napp.include_router(video_costs.router)\napp.include_router(youtube_series.router)",
        label="main/include-video-costs",
    )


def patch_youtube(text: str) -> str:
    text = _replace_once(
        text,
        "    force_reuse_assets: bool = False\n    force_render_only: bool = False",
        "    force_reuse_assets: bool = False\n    force_render_only: bool = False\n    production_mode: str = \"balanced\"\n    max_cost_brl: Optional[float] = None\n    cost_override_approved: bool = False",
        label="youtube/cost-request-fields",
    )
    text = _replace_once(
        text,
        '        "duration_override_approved": bool(payload.get("duration_override_approved")),\n        "aspect_ratio": aspect_ratio,',
        '        "duration_override_approved": bool(payload.get("duration_override_approved")),\n        "production_mode": _normalize_hash_text(payload.get("production_mode") or "balanced", lower=True) or "balanced",\n        "aspect_ratio": aspect_ratio,',
        label="youtube/dedupe-production-mode",
    )
    old = "    identity = _build_video_generation_identity(payload)"
    new = '''    # CODEXIA_VIDEO_COST_CONTROL_V1
    try:
        from app.routers.video_costs import build_cost_estimate_payload
        cost_estimate = build_cost_estimate_payload(
            float(payload.get("duration") or 5),
            mode=str(payload.get("production_mode") or "balanced"),
        )
        payload["production_mode"] = str(cost_estimate.get("mode") or "balanced")
        payload["cost_estimate"] = cost_estimate
        max_cost_brl = float(payload.get("max_cost_brl") or 0.0)
        if max_cost_brl > 0 and float(cost_estimate.get("total_cost_brl") or 0.0) > max_cost_brl and not bool(payload.get("cost_override_approved")):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "VIDEO_COST_CONFIRMATION_REQUIRED",
                    "message": "A estimativa ultrapassa o limite definido.",
                    "estimated_cost_brl": cost_estimate.get("total_cost_brl"),
                    "max_cost_brl": round(max_cost_brl, 2),
                    "estimate": cost_estimate,
                },
            )
    except HTTPException:
        raise
    except Exception as cost_exc:
        if float(payload.get("max_cost_brl") or 0.0) > 0:
            raise HTTPException(status_code=503, detail=f"Não foi possível validar o teto de custo: {cost_exc}")
        payload["cost_estimate_error"] = str(cost_exc)[:500]

    identity = _build_video_generation_identity(payload)'''
    return _replace_once(text, old, new, label="youtube/cost-before-queue")


def patch_unified(text: str) -> str:
    text = _replace_once(
        text,
        '        user_id = int(request.user_id or (getattr(user, "id", None) or 0) or 0) or None\n        initial_result = dict(legacy_initial_result or {}) or None\n\n        # 1. Reclama task idempotente',
        '        user_id = int(request.user_id or (getattr(user, "id", None) or 0) or 0) or None\n        initial_result = dict(legacy_initial_result or {}) or None\n        legacy_cost_estimate = ((request.legacy_payload or {}).get("cost_estimate") if isinstance(request.legacy_payload, dict) else None)\n        try:\n            preflight_cost_usd = float((legacy_cost_estimate or {}).get("total_cost_usd") or 0.0)\n        except Exception:\n            preflight_cost_usd = 0.0\n\n        # 1. Reclama task idempotente',
        label="unified/read-cost",
    )
    return _replace_once(
        text,
        '                uv.last_message = "Nova geração criada após descarte da tentativa anterior."\n                uv.last_error = None\n        try:\n            db.flush()',
        '                uv.last_message = "Nova geração criada após descarte da tentativa anterior."\n                uv.last_error = None\n        if preflight_cost_usd > 0:\n            uv.estimated_cost = round(preflight_cost_usd, 6)\n        try:\n            db.flush()',
        label="unified/persist-cost",
    )


def patch_ai_router(text: str) -> str:
    text = _replace_once(text, "from dataclasses import dataclass", "from dataclasses import dataclass, replace", label="ai/import-replace")
    helper = '''def _task_openai_image_profile(task_id: Optional[str]) -> Tuple[str, str, float]:
    mode = "balanced"
    try:
        if task_id:
            from app.services.task_manager import get_task
            task = get_task(str(task_id)) or {}
            result = task.get("result") if isinstance(task, dict) else {}
            payload = result.get("payload") if isinstance(result, dict) else {}
            if isinstance(payload, dict):
                mode = str(payload.get("production_mode") or "balanced")
    except Exception:
        pass
    try:
        from app.services.video_cost_estimator import image_profile_for_mode
        profile = image_profile_for_mode(mode)
        return str(profile["mode"]), str(profile["image_quality"]), float(profile["image_unit_cost_usd"])
    except Exception:
        return "balanced", "medium", 0.05


class AIRouter:'''
    text = _replace_once(text, "class AIRouter:", helper, label="ai/task-profile-helper")
    text = _replace_once(
        text,
        "def _call_openai_image(self, *, api_key: str, model: str, prompt: str) -> bytes:",
        "def _call_openai_image(self, *, api_key: str, model: str, prompt: str, quality: Optional[str] = None) -> bytes:",
        label="ai/openai-quality-arg",
    )
    text = _replace_once(
        text,
        '            quality = str(os.getenv("OPENAI_IMAGE_QUALITY") or "medium").strip().lower()\n            kwargs["quality"] = quality if quality in {"low", "medium", "high", "auto"} else "medium"',
        '            requested_quality = str(quality or os.getenv("OPENAI_IMAGE_QUALITY") or "medium").strip().lower()\n            kwargs["quality"] = requested_quality if requested_quality in {"low", "medium", "high", "auto"} else "medium"',
        label="ai/use-task-quality",
    )
    text = _replace_once(
        text,
        '            settings = self._get_settings(db, user_id=user_id)\n            policy = self._load_policy(db, user_id=user_id, capability=capability, settings=settings)\n            if str(policy.primary_provider or "").strip().lower() != "openai":',
        '            settings = self._get_settings(db, user_id=user_id)\n            policy = self._load_policy(db, user_id=user_id, capability=capability, settings=settings)\n            production_mode, production_quality, production_unit_cost = _task_openai_image_profile(task_id)\n            policy = replace(policy, estimated_cost=production_unit_cost)\n            if str(policy.primary_provider or "").strip().lower() != "openai":',
        label="ai/resolve-task-profile",
    )
    text = _replace_once(
        text,
        '            input_hash = _sha256_text(json.dumps({"prompt": raw_prompt}, ensure_ascii=False, sort_keys=True))',
        '            input_hash = _sha256_text(json.dumps({"prompt": raw_prompt, "quality": production_quality, "mode": production_mode}, ensure_ascii=False, sort_keys=True))',
        label="ai/cache-profile",
    )
    return _replace_once(
        text,
        "                image_bytes = self._call_openai_image(api_key=api_key, model=model_id, prompt=raw_prompt)",
        "                image_bytes = self._call_openai_image(api_key=api_key, model=model_id, prompt=raw_prompt, quality=production_quality)",
        label="ai/pass-task-quality",
    )


PATCHERS: dict[str, Callable[[str], str]] = {
    "app/main.py": patch_main,
    "app/routers/youtube.py": patch_youtube,
    "app/services/unified_video_pipeline.py": patch_unified,
    "app/services/ai_router.py": patch_ai_router,
}


def apply(write: bool) -> None:
    changed = 0
    for rel, patcher in PATCHERS.items():
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        transformed = patcher(original)
        if transformed != original:
            changed += 1
            if write:
                path.write_text(transformed, encoding="utf-8")
        if patcher(transformed) != transformed:
            raise PatchError(f"{rel}: patch não idempotente")
    print(f"Video cost backend: {changed} arquivo(s) alterados; contratos={len(PATCHERS)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply ou --check")
    try:
        apply(bool(args.apply))
    except PatchError as exc:
        print(f"ERRO VIDEO COST BACKEND: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
