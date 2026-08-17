from __future__ import annotations

import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Type


def _enabled(name: str, default: str = "true") -> bool:
    return str(os.getenv(name) or default).strip().lower() in {
        "1", "true", "yes", "sim", "on", "enabled", "enable"
    }


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    text = re.sub(r"[^a-z0-9áàâãéêíóôõúç ]+", "", text)
    return text[:1600]


def _scene_list(plan: Any) -> List[Dict[str, Any]]:
    if not isinstance(plan, dict):
        return []
    raw = plan.get("scenes")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _jaccard(left: str, right: str) -> float:
    a = set(_norm(left).split())
    b = set(_norm(right).split())
    union = a | b
    return (len(a & b) / len(union)) if union else 0.0


SHOT_SEQUENCE = (
    ("establishing_wide", "wide establishing shot, environment clearly visible, cinematic depth"),
    ("medium_action", "medium shot focused on a clear physical action, natural body language"),
    ("close_emotion", "close emotional portrait, expressive but natural face, shallow depth of field"),
    ("symbolic_cutaway", "symbolic cinematic cutaway connected to the narrated idea, no repeated portrait composition"),
    ("environment_detail", "environmental detail shot, meaningful object or landscape detail, strong visual storytelling"),
    ("over_shoulder", "over-the-shoulder composition with layered foreground and background"),
    ("wide_motion", "wide dynamic composition with visible movement through the environment"),
    ("close_detail", "close detail shot of hands, object, light or environment; avoid repeating the previous portrait framing"),
)

SYMBOLIC_CUES = {
    "tempestade": "storm clouds, wind or rough water",
    "barco": "a small boat or ship as a narrative element",
    "farol": "a lighthouse or guiding beam of light",
    "porto": "a safe harbor or sheltered shore",
    "névoa": "mist or fog creating depth",
    "nevoa": "mist or fog creating depth",
    "deserto": "a vast desert landscape",
    "caminho": "a visible path or road leading forward",
    "luz": "a motivated beam of light",
    "escuridão": "deep shadows contrasted with a believable light source",
    "escuridao": "deep shadows contrasted with a believable light source",
    "refúgio": "a visually clear place of shelter",
    "refugio": "a visually clear place of shelter",
    "mar": "a sea or shoreline environment",
    "ondas": "visible waves with realistic scale and motion",
}


def direct_scene_plan(plan: Any) -> tuple[Any, Dict[str, Any]]:
    """Adds conservative visual direction without changing narration, timing or scene count."""
    report: Dict[str, Any] = {
        "version": 1,
        "generated_at": _utc_iso(),
        "mode": "active" if _enabled("ENABLE_SCENE_DIRECTOR", "true") else "disabled",
        "enabled": _enabled("ENABLE_SCENE_DIRECTOR", "true"),
        "mutated_scene_count": 0,
        "directives": [],
        "blocking": False,
        "changes_narration": False,
        "changes_scene_count": False,
    }
    if not report["enabled"] or not isinstance(plan, dict):
        return plan, report

    directed = deepcopy(plan)
    scenes = _scene_list(directed)
    previous_prompt = ""

    for idx, scene in enumerate(scenes):
        original_prompt = str(scene.get("image_prompt") or scene.get("visual_prompt") or "").strip()
        narration = str(scene.get("text") or scene.get("narration") or scene.get("content") or "").strip()
        shot_name, shot_instruction = SHOT_SEQUENCE[idx % len(SHOT_SEQUENCE)]
        similarity = _jaccard(previous_prompt, original_prompt) if previous_prompt and original_prompt else 0.0

        additions = [f"Camera direction: {shot_instruction}."]
        if similarity >= 0.72:
            additions.append(
                "Composition requirement: make this shot clearly distinct from the immediately previous scene by changing camera distance, angle and foreground/background arrangement while preserving character identity."
            )

        normalized_narration = _norm(narration)
        normalized_prompt = _norm(original_prompt)
        cues_added: List[str] = []
        for cue, visual in SYMBOLIC_CUES.items():
            if cue in normalized_narration and cue not in normalized_prompt:
                additions.append(f"Narrative visual cue: include {visual} when contextually appropriate; keep it natural and story-driven.")
                cues_added.append(cue)
                if len(cues_added) >= 2:
                    break

        additions.append(
            "Continuity: preserve established character identity, age, gender, clothing palette and historical/biblical visual style; do not clone the previous composition."
        )

        base = original_prompt or narration
        if base:
            directed_prompt = re.sub(r"\s+", " ", f"{base} {' '.join(additions)}").strip()
            # Keep prompts bounded so the director cannot drown out the original scene intent.
            directed_prompt = directed_prompt[:1800].rstrip()
            scene["image_prompt"] = directed_prompt
            scene["_scene_director"] = {
                "shot": shot_name,
                "previous_prompt_similarity": round(similarity, 3),
                "symbolic_cues_added": cues_added,
                "mutated": directed_prompt != original_prompt,
            }
            if directed_prompt != original_prompt:
                report["mutated_scene_count"] += 1
        else:
            scene["_scene_director"] = {
                "shot": shot_name,
                "previous_prompt_similarity": round(similarity, 3),
                "symbolic_cues_added": [],
                "mutated": False,
            }

        report["directives"].append({
            "scene": idx + 1,
            "shot": shot_name,
            "previous_prompt_similarity": round(similarity, 3),
            "symbolic_cues_added": cues_added,
        })
        previous_prompt = original_prompt

    directed["scenes"] = scenes
    return directed, report


def install_scene_director_active_patch(video_generator_cls: Type[Any]) -> Type[Any]:
    """Wraps create_video_from_plan; rollback is immediate with ENABLE_SCENE_DIRECTOR=false."""
    if getattr(video_generator_cls, "_codexia_scene_director_active_installed", False):
        return video_generator_cls
    original_create = getattr(video_generator_cls, "create_video_from_plan", None)
    if not callable(original_create):
        return video_generator_cls

    def create_with_scene_director(self: Any, plan: Any, *args: Any, **kwargs: Any):
        directed_plan, report = direct_scene_plan(plan)
        result = original_create(self, directed_plan, *args, **kwargs)
        if isinstance(result, dict):
            result["scene_director"] = deepcopy(report)
            render_report = result.get("render_report") if isinstance(result.get("render_report"), dict) else {}
            render_report["scene_director"] = deepcopy(report)
            result["render_report"] = render_report
            try:
                ai_service = getattr(self, "ai_service", None)
                task_id = getattr(ai_service, "ai_task_id", None) if ai_service is not None else None
                if task_id:
                    from app.services.task_manager import merge_task_result
                    merge_task_result(str(task_id), {
                        "scene_director": deepcopy(report),
                        "render_report": deepcopy(render_report),
                    })
            except Exception:
                pass
        return result

    video_generator_cls.create_video_from_plan = create_with_scene_director
    video_generator_cls._codexia_scene_director_active_installed = True
    return video_generator_cls
