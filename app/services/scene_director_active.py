from __future__ import annotations

import math
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


def _scene_text(scene: Dict[str, Any]) -> str:
    return str(
        scene.get("text")
        or scene.get("narration_text")
        or scene.get("narration")
        or scene.get("content")
        or ""
    ).strip()


def _set_scene_text(scene: Dict[str, Any], text: str) -> None:
    if "text" in scene or not any(key in scene for key in ("narration_text", "narration", "content")):
        scene["text"] = text
    elif "narration_text" in scene:
        scene["narration_text"] = text
    elif "narration" in scene:
        scene["narration"] = text
    else:
        scene["content"] = text


def _sentence_chunks(text: str, *, target_words: int = 20, hard_max_words: int = 28) -> List[str]:
    """Divide a narração em beats curtos preservando ordem e palavras.

    O objetivo é gerar uma imagem REAL nova por beat, em vez de apenas aplicar
    zoom/pan durante muitos segundos sobre a mesma arte.
    """
    clean = re.sub(r"\s+", " ", str(text or "").strip())
    if not clean:
        return []
    sentences = [
        part.strip()
        for part in re.findall(r"[^.!?…]+(?:[.!?…]+|$)", clean)
        if part and part.strip()
    ] or [clean]

    chunks: List[str] = []
    current: List[str] = []
    current_words = 0

    def flush() -> None:
        nonlocal current, current_words
        if current:
            chunks.append(" ".join(current).strip())
            current = []
            current_words = 0

    for sentence in sentences:
        words = sentence.split()
        if len(words) > hard_max_words:
            flush()
            for start in range(0, len(words), target_words):
                piece = words[start:start + target_words]
                if piece:
                    chunks.append(" ".join(piece).strip())
            continue
        if current and current_words + len(words) > hard_max_words:
            flush()
        current.append(sentence)
        current_words += len(words)
        if current_words >= target_words:
            flush()
    flush()
    return [chunk for chunk in chunks if chunk]


def _expand_scenes_for_cinematic_variety(scenes: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Cria beats visuais reais para impedir longas permanências na mesma imagem.

    Limites de custo/segurança:
    - alvo de ~20 palavras por imagem em vídeos curtos;
    - máximo de 48 cenas/imagens por vídeo;
    - não inventa texto: somente reparte a narração existente;
    - selecionados manuais permanecem intactos quando a cena já traz image_path.
    """
    enabled = _enabled("ENABLE_REAL_VISUAL_BEATS", "true")
    max_scenes = 48
    try:
        max_scenes = max(8, min(64, int(str(os.getenv("VIDEO_MAX_UNIQUE_SCENES") or "48").strip())))
    except Exception:
        max_scenes = 48

    if not enabled or not scenes:
        return scenes, {"enabled": enabled, "before": len(scenes), "after": len(scenes), "expanded": 0}

    total_words = sum(len(_scene_text(scene).split()) for scene in scenes)
    # Para vídeos curtos, ~7-9 imagens/minuto. Para longos, o cap evita custo explosivo.
    estimated_minutes = max(1.0, total_words / 145.0) if total_words else 1.0
    desired_total = min(max_scenes, max(len(scenes), int(math.ceil(estimated_minutes * 8.0))))
    target_words = max(18, min(30, int(math.ceil(total_words / desired_total)))) if total_words else 22

    expanded: List[Dict[str, Any]] = []
    for scene_index, scene in enumerate(scenes):
        if len(expanded) >= max_scenes:
            break
        text = _scene_text(scene)
        # Cena com imagem escolhida manualmente não é multiplicada silenciosamente.
        manual_image = bool(scene.get("image_path") or scene.get("selected_image") or scene.get("image"))
        chunks = [text] if manual_image else _sentence_chunks(text, target_words=target_words, hard_max_words=max(target_words + 8, 28))
        if not chunks:
            chunks = [text]
        remaining = max_scenes - len(expanded)
        chunks = chunks[:remaining]
        for beat_index, chunk in enumerate(chunks):
            item = deepcopy(scene)
            _set_scene_text(item, chunk)
            original_prompt = str(item.get("image_prompt") or item.get("visual_prompt") or "").strip()
            if len(chunks) > 1:
                beat_hint = (
                    f"Narrative beat {beat_index + 1}/{len(chunks)} from source scene {scene_index + 1}. "
                    "Generate a genuinely new composition for this beat; do not reuse the previous scene image."
                )
                item["image_prompt"] = re.sub(r"\s+", " ", f"{original_prompt} {beat_hint}").strip()
            item["_real_visual_beat"] = {
                "source_scene": scene_index + 1,
                "beat": beat_index + 1,
                "beats_in_source_scene": len(chunks),
            }
            expanded.append(item)

    if len(expanded) < len(scenes):
        expanded.extend(deepcopy(scenes[len(expanded):max_scenes]))

    return expanded[:max_scenes], {
        "enabled": True,
        "before": len(scenes),
        "after": len(expanded[:max_scenes]),
        "expanded": max(0, len(expanded[:max_scenes]) - len(scenes)),
        "total_words": total_words,
        "estimated_minutes": round(estimated_minutes, 2),
        "target_words_per_visual": target_words,
        "max_unique_scenes": max_scenes,
    }


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
    enabled = _enabled("ENABLE_SCENE_DIRECTOR", "true")
    report: Dict[str, Any] = {
        "version": 3,
        "generated_at": _utc_iso(),
        "mode": "active" if enabled else "disabled",
        "enabled": enabled,
        "mutated_scene_count": 0,
        "anti_repetition_interventions": 0,
        "directives": [],
        "blocking": False,
        "changes_narration": False,
        "changes_scene_count": False,
        "real_visual_beats": {},
    }
    if not enabled or not isinstance(plan, dict):
        return plan, report

    directed = deepcopy(plan)
    original_scenes = _scene_list(directed)
    scenes, expansion = _expand_scenes_for_cinematic_variety(original_scenes)
    report["real_visual_beats"] = expansion
    report["changes_scene_count"] = len(scenes) != len(original_scenes)

    previous_prompt = ""
    previous_role = ""
    for idx, scene in enumerate(scenes):
        original_prompt = str(scene.get("image_prompt") or scene.get("visual_prompt") or "").strip()
        narration = _scene_text(scene)
        shot_name, visual_role, shot_instruction, subject_rule = VISUAL_SEQUENCE[idx % len(VISUAL_SEQUENCE)]
        similarity = _jaccard(previous_prompt, original_prompt) if previous_prompt and original_prompt else 0.0

        additions = [
            f"Camera direction: {shot_instruction}.",
            f"Visual role: {visual_role}. {subject_rule}",
            f"Unique scene identity: scene {idx + 1} of {len(scenes)} must look unmistakably different from scene {idx} and scene {idx + 2}.",
            "Sequence diversity rule: change at least three of these: camera distance, camera angle, location/background, subject placement, action, dominant visual symbol, lighting direction.",
            "Do not reuse a previous generated image or merely crop/zoom the same composition. This must be a newly generated visual concept.",
        ]

        if similarity >= 0.45 or visual_role == previous_role:
            additions.append(
                "Anti-repetition requirement: do NOT reuse the same room, same two-person grouping, same consoling pose, same centered portrait, same background or same camera setup as the immediately previous scene. If narration permits, make environment/object/metaphor primary and recurring characters secondary."
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
            "Continuity: preserve established character identity, age, gender, clothing palette and historical/biblical style where required; continuity does not mean repeating pose, room, framing or composition."
        )
        additions.append(
            "Quality: realistic eyes, hands and anatomy; natural expressions; coherent lighting; no text baked into the generated image; no duplicated people or malformed limbs."
        )

        base = original_prompt or narration
        if base:
            directed_prompt = re.sub(r"\s+", " ", f"{base} {' '.join(additions)}").strip()[:3000].rstrip()
            scene["image_prompt"] = directed_prompt
            scene["_scene_director"] = {
                "shot": shot_name,
                "visual_role": visual_role,
                "previous_prompt_similarity": round(similarity, 3),
                "symbolic_cues_added": cues_added,
                "anti_repetition": bool(similarity >= 0.45 or visual_role == previous_role),
                "mutated": directed_prompt != original_prompt,
            }
            if directed_prompt != original_prompt:
                report["mutated_scene_count"] += 1

        report["directives"].append({
            "scene": idx + 1,
            "shot": shot_name,
            "visual_role": visual_role,
            "previous_prompt_similarity": round(similarity, 3),
            "symbolic_cues_added": cues_added,
            "anti_repetition": bool(similarity >= 0.45 or visual_role == previous_role),
        })
        # Compare the next scene against this scene's original visual concept,
        # not against the fully decorated director prompt. Otherwise the added
        # camera/quality directives dilute similarity and hide true duplicates.
        previous_prompt = original_prompt
        previous_role = visual_role

    directed["scenes"] = scenes
    return directed, report


def _spoken_ptbr(text: Any) -> str:
    value = str(text or "")
    # A forma fonética só vai para o TTS; legenda/texto aprovado continuam "Jesus".
    value = re.sub(r"(?i)\bjesus\b", "Jêzus", value)
    return value


def install_scene_director_active_patch(video_generator_cls: Type[Any]) -> Type[Any]:
    """Instala o hardening cinematográfico na classe canônica, com rollback por flags."""
    if getattr(video_generator_cls, "_codexia_scene_director_active_installed", False):
        return video_generator_cls

    original_create = getattr(video_generator_cls, "create_video_from_plan", None)
    if callable(original_create):
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

    original_audio = getattr(video_generator_cls, "generate_audio", None)
    if callable(original_audio):
        def audio_with_ptbr_guard(self: Any, text: Any, *args: Any, **kwargs: Any):
            if not _enabled("ENABLE_PTBR_TTS_GUARD", "true"):
                return original_audio(self, text, *args, **kwargs)
            mutable_args = list(args)
            if mutable_args and isinstance(mutable_args[0], str) and mutable_args[0].strip().lower().startswith("pt"):
                mutable_args[0] = "pt"
            if "lang" in kwargs and str(kwargs.get("lang") or "").strip().lower().startswith("pt"):
                kwargs["lang"] = "pt"
            return original_audio(self, _spoken_ptbr(text), *mutable_args, **kwargs)
        video_generator_cls.generate_audio = audio_with_ptbr_guard

    original_logo = getattr(video_generator_cls, "_build_logo_overlay", None)
    if callable(original_logo):
        def logo_safe_zone(self: Any, logo_path: str, size: Any, *args: Any, **kwargs: Any):
            if _enabled("ENABLE_PREMIUM_OPENING_SAFE_ZONE", "true"):
                if str(kwargs.get("position") or "").strip().lower() == "top_center":
                    kwargs["position"] = "top_right"
                try:
                    kwargs["width_ratio"] = min(float(kwargs.get("width_ratio") or 0.10), 0.11)
                except Exception:
                    kwargs["width_ratio"] = 0.10
                try:
                    kwargs["opacity"] = min(float(kwargs.get("opacity") or 0.84), 0.88)
                except Exception:
                    kwargs["opacity"] = 0.84
            return original_logo(self, logo_path, size, *args, **kwargs)
        video_generator_cls._build_logo_overlay = logo_safe_zone

    original_closing_bg = getattr(video_generator_cls, "_resolve_closing_background_image", None)
    if callable(original_closing_bg):
        def dedicated_endcard_background(self: Any, branding: Dict[str, Any], *args: Any, **kwargs: Any):
            if not _enabled("ENABLE_DEDICATED_PREMIUM_ENDCARD", "true"):
                return original_closing_bg(self, branding, *args, **kwargs)
            # Retornar sem path força o renderer canônico a criar um background
            # dedicado no tamanho correto, em vez de reaproveitar a última cena.
            return {"path": None, "source": "dedicated_generated_endcard"}
        video_generator_cls._resolve_closing_background_image = dedicated_endcard_background

    video_generator_cls._codexia_scene_director_active_installed = True
    return video_generator_cls
