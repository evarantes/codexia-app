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
    return text[:1800]


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


# Cada papel visual força uma função narrativa diferente. Isso evita que um vídeo
# inteiro vire apenas variações do mesmo retrato de dois personagens.
VISUAL_SEQUENCE = (
    (
        "establishing_wide",
        "environment_story",
        "wide establishing shot with the environment as the main subject, cinematic depth and a clear sense of place",
        "Make the location and atmosphere the primary visual story. Characters, if present, should be small or secondary rather than a centered consolation portrait.",
    ),
    (
        "medium_action",
        "human_action",
        "medium shot focused on a specific physical action, natural body language and visible interaction with the environment",
        "Show a concrete action instead of a static pose. Avoid repeating a standing or seated two-person portrait from the previous scene.",
    ),
    (
        "symbolic_cutaway",
        "symbolic_metaphor",
        "cinematic symbolic cutaway connected directly to the narrated idea, strong composition and no repeated portrait framing",
        "Use an environment, object, weather, light, path, water or other story symbol as the primary subject. Omit nonessential characters when the narration can be represented symbolically.",
    ),
    (
        "close_emotion",
        "emotion_detail",
        "close emotional portrait with a natural expression, believable skin and eyes, shallow depth of field",
        "Use one clear emotional focal point. Do not reproduce the same character pairing, pose, camera angle or background from adjacent scenes.",
    ),
    (
        "environment_detail",
        "object_or_place",
        "environmental detail shot of a meaningful object, architecture, landscape or natural element with cinematic lighting",
        "No centered talking or consoling portrait. Let an object or place carry the meaning of this scene whenever context allows.",
    ),
    (
        "over_shoulder",
        "perspective_change",
        "over-the-shoulder composition with layered foreground and background, clear spatial storytelling",
        "Change viewpoint and depth decisively. If characters recur, place them differently and show a new environment or action.",
    ),
    (
        "wide_motion",
        "movement",
        "wide dynamic composition with visible movement through the environment, natural gesture and cinematic scale",
        "Prioritize movement, journey or transition. Avoid a static portrait or repeated room/background.",
    ),
    (
        "close_detail",
        "meaningful_detail",
        "close detail shot of hands, an object, light, texture or environment connected to the narration",
        "Use a meaningful detail rather than faces when possible. Do not repeat the immediately previous composition.",
    ),
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
    "solidão": "a solitary figure in a spacious environment, meaningful negative space",
    "solidao": "a solitary figure in a spacious environment, meaningful negative space",
    "ansiedade": "restless hands, tense posture or a confined environment shown naturally",
    "peso": "a visual sense of burden through posture, shadow, weather or a difficult path",
    "oração": "hands in prayer, a quiet room, open Bible or kneeling silhouette shown naturally",
    "oracao": "hands in prayer, a quiet room, open Bible or kneeling silhouette shown naturally",
    "esperança": "dawn light, an opening path, horizon or warm light entering the scene",
    "esperanca": "dawn light, an opening path, horizon or warm light entering the scene",
    "lágrima": "a subtle tear or emotional detail without melodrama",
    "lagrima": "a subtle tear or emotional detail without melodrama",
    "noite": "a believable night environment with motivated practical light",
    "amanhecer": "early dawn light and a horizon suggesting renewal",
    "bíblia": "an open Bible used naturally as part of the scene",
    "biblia": "an open Bible used naturally as part of the scene",
}


def direct_scene_plan(plan: Any) -> tuple[Any, Dict[str, Any]]:
    """Adds strong but reversible visual direction without changing narration, timing or scene count."""
    enabled = _enabled("ENABLE_SCENE_DIRECTOR", "true")
    report: Dict[str, Any] = {
        "version": 2,
        "generated_at": _utc_iso(),
        "mode": "active" if enabled else "disabled",
        "enabled": enabled,
        "mutated_scene_count": 0,
        "anti_repetition_interventions": 0,
        "directives": [],
        "blocking": False,
        "changes_narration": False,
        "changes_scene_count": False,
    }
    if not enabled or not isinstance(plan, dict):
        return plan, report

    directed = deepcopy(plan)
    scenes = _scene_list(directed)
    previous_prompt = ""
    previous_role = ""

    for idx, scene in enumerate(scenes):
        original_prompt = str(scene.get("image_prompt") or scene.get("visual_prompt") or "").strip()
        narration = str(scene.get("text") or scene.get("narration_text") or scene.get("narration") or scene.get("content") or "").strip()
        shot_name, visual_role, shot_instruction, subject_rule = VISUAL_SEQUENCE[idx % len(VISUAL_SEQUENCE)]
        similarity = _jaccard(previous_prompt, original_prompt) if previous_prompt and original_prompt else 0.0

        additions = [
            f"Camera direction: {shot_instruction}.",
            f"Visual role: {visual_role}. {subject_rule}",
            "Sequence diversity rule: this scene must be visually distinguishable from both adjacent scenes at a glance. Change at least three of these when possible: camera distance, camera angle, location/background, subject placement, action, dominant visual symbol, lighting direction.",
        ]

        # Limite mais rígido: prompts já moderadamente semelhantes recebem intervenção.
        if similarity >= 0.55 or visual_role == previous_role:
            additions.append(
                "Anti-repetition requirement: do NOT reuse the same room, same two-person grouping, same consoling pose, same centered portrait, same background or same camera setup as the immediately previous scene. If the narration permits, make an environment/object/metaphor the primary subject and keep recurring characters secondary."
            )
            report["anti_repetition_interventions"] += 1

        normalized_narration = _norm(narration)
        normalized_prompt = _norm(original_prompt)
        cues_added: List[str] = []
        for cue, visual in SYMBOLIC_CUES.items():
            if cue in normalized_narration and cue not in normalized_prompt:
                additions.append(
                    f"Narrative visual cue: represent the narrated idea with {visual} when contextually appropriate; integrate it naturally rather than as decoration."
                )
                cues_added.append(cue)
                if len(cues_added) >= 2:
                    break

        additions.append(
            "Continuity: preserve established character identity, age, gender, clothing palette and historical/biblical style where those characters are required; continuity does not mean repeating pose, room, framing or composition."
        )
        additions.append(
            "Quality: realistic eyes, hands and anatomy; natural expressions; coherent lighting; no text baked into the generated image; no duplicated people or malformed limbs."
        )

        base = original_prompt or narration
        if base:
            directed_prompt = re.sub(r"\s+", " ", f"{base} {' '.join(additions)}").strip()
            directed_prompt = directed_prompt[:2400].rstrip()
            scene["image_prompt"] = directed_prompt
            scene["_scene_director"] = {
                "shot": shot_name,
                "visual_role": visual_role,
                "previous_prompt_similarity": round(similarity, 3),
                "symbolic_cues_added": cues_added,
                "anti_repetition": bool(similarity >= 0.55 or visual_role == previous_role),
                "mutated": directed_prompt != original_prompt,
            }
            if directed_prompt != original_prompt:
                report["mutated_scene_count"] += 1
        else:
            scene["_scene_director"] = {
                "shot": shot_name,
                "visual_role": visual_role,
                "previous_prompt_similarity": round(similarity, 3),
                "symbolic_cues_added": [],
                "anti_repetition": False,
                "mutated": False,
            }

        report["directives"].append({
            "scene": idx + 1,
            "shot": shot_name,
            "visual_role": visual_role,
            "previous_prompt_similarity": round(similarity, 3),
            "symbolic_cues_added": cues_added,
            "anti_repetition": bool(similarity >= 0.55 or visual_role == previous_role),
        })
        previous_prompt = original_prompt
        previous_role = visual_role

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
