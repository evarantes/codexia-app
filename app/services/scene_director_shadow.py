from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Type


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(text: Any) -> str:
    value = re.sub(r"\s+", " ", str(text or "").strip().lower())
    value = re.sub(r"[^a-z0-9áàâãéêíóôõúç ]+", "", value)
    return value[:1200]


def _scene_list(plan: Any) -> List[Dict[str, Any]]:
    if not isinstance(plan, dict):
        return []
    for key in ("scenes", "blocks", "segments", "parts", "chapters"):
        raw = plan.get(key)
        if isinstance(raw, list) and raw:
            return [item for item in raw if isinstance(item, dict)]
    return []


def _shot_hint(index: int) -> str:
    sequence = (
        "establishing_wide",
        "medium_action",
        "close_emotion",
        "symbolic_cutaway",
        "environment_detail",
        "over_shoulder",
        "wide_motion",
        "close_detail",
    )
    return sequence[index % len(sequence)]


def analyze_scene_plan(plan: Any) -> Dict[str, Any]:
    scenes = _scene_list(plan)
    prompts = [_norm(scene.get("image_prompt") or scene.get("visual_prompt") or "") for scene in scenes]
    texts = [_norm(scene.get("text") or scene.get("narration") or scene.get("content") or "") for scene in scenes]

    repeated_prompt_pairs = []
    near_generic_pairs = []
    for idx in range(1, len(prompts)):
        prev = prompts[idx - 1]
        cur = prompts[idx]
        if prev and cur and prev == cur:
            repeated_prompt_pairs.append([idx, idx + 1])
        elif prev and cur:
            prev_words = set(prev.split())
            cur_words = set(cur.split())
            union = prev_words | cur_words
            similarity = (len(prev_words & cur_words) / len(union)) if union else 0.0
            if similarity >= 0.78:
                near_generic_pairs.append({"scenes": [idx, idx + 1], "similarity": round(similarity, 3)})

    symbolic_terms = {
        "tempestade": ("storm", "tempestade", "waves", "ondas", "rain", "chuva"),
        "barco": ("boat", "ship", "barco", "navio"),
        "farol": ("lighthouse", "beacon", "farol", "light beam", "facho"),
        "porto": ("harbor", "porto", "safe haven", "refúgio"),
        "névoa": ("fog", "mist", "névoa", "neblina"),
        "deserto": ("desert", "deserto", "dunes", "dunas"),
        "caminho": ("road", "path", "caminho", "estrada"),
    }
    missed_symbolic_opportunities = []
    for idx, (text, prompt) in enumerate(zip(texts, prompts), start=1):
        for cue, visual_words in symbolic_terms.items():
            if cue in text and not any(word in prompt for word in visual_words):
                missed_symbolic_opportunities.append({"scene": idx, "cue": cue})

    suggestions = []
    for idx, scene in enumerate(scenes):
        suggestions.append({
            "scene": idx + 1,
            "recommended_shot": _shot_hint(idx),
            "has_visual_prompt": bool(prompts[idx]),
        })

    scene_count = len(scenes)
    repeat_penalty = (len(repeated_prompt_pairs) * 2.0) + (len(near_generic_pairs) * 1.0)
    symbolism_penalty = min(3.0, len(missed_symbolic_opportunities) * 0.35)
    score = max(0.0, min(10.0, 10.0 - repeat_penalty - symbolism_penalty))

    return {
        "version": 1,
        "generated_at": _utc_iso(),
        "mode": "shadow",
        "scene_count": scene_count,
        "variety_score": round(score, 2),
        "repeated_prompt_pairs": repeated_prompt_pairs,
        "near_duplicate_prompt_pairs": near_generic_pairs,
        "missed_symbolic_opportunities": missed_symbolic_opportunities[:30],
        "shot_suggestions": suggestions[:100],
        "blocking": False,
        "mutated_plan": False,
    }


def install_scene_director_shadow_patch(video_generator_cls: Type[Any]) -> Type[Any]:
    """Audita variedade narrativa sem alterar o plano nem bloquear a produção."""
    if getattr(video_generator_cls, "_codexia_scene_director_shadow_installed", False):
        return video_generator_cls
    original_create = getattr(video_generator_cls, "create_video_from_plan", None)
    if not callable(original_create):
        return video_generator_cls

    def create_with_scene_director_shadow(self: Any, plan: Any, *args: Any, **kwargs: Any):
        report = analyze_scene_plan(plan)
        result = original_create(self, plan, *args, **kwargs)
        if isinstance(result, dict):
            result["scene_director_shadow"] = deepcopy(report)
            render_report = result.get("render_report") if isinstance(result.get("render_report"), dict) else {}
            render_report["scene_director_shadow"] = deepcopy(report)
            result["render_report"] = render_report
            try:
                ai_service = getattr(self, "ai_service", None)
                task_id = getattr(ai_service, "ai_task_id", None) if ai_service is not None else None
                if task_id:
                    from app.services.task_manager import merge_task_result
                    merge_task_result(str(task_id), {
                        "scene_director_shadow": deepcopy(report),
                        "render_report": deepcopy(render_report),
                    })
            except Exception:
                pass
        return result

    video_generator_cls.create_video_from_plan = create_with_scene_director_shadow
    video_generator_cls._codexia_scene_director_shadow_installed = True
    return video_generator_cls
