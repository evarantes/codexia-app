import logging
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import VIDEO_OUTPUT_DIR
from app.database import SessionLocal
from app.models import ScheduledVideo
from app.modules.bible_video_factory.models import (
    BibleVideoCharacter,
    BibleVideoConfig,
    BibleVideoEpisode,
    BibleVideoJob,
    BibleVideoMetric,
    BibleVideoPrompt,
    BibleVideoScenario,
    BibleVideoScene,
    BibleVideoScript,
    BibleVideoSeries,
)
from app.services.ai_generator import AIContentGenerator
from app.services.task_manager import create_task, get_task, update_task
from app.services.video_generator import VideoGenerator
from app.services.youtube_service import YouTubeService

logger = logging.getLogger(__name__)


KANBAN_STAGES = [
    "idea",
    "script_generated",
    "script_approved",
    "scenes_generated",
    "storyboard_generated",
    "storyboard_approved",
    "images_generated",
    "voice_generated",
    "video_animating",
    "video_editing",
    "thumbnail_generated",
    "shorts_generated",
    "awaiting_approval",
    "ready_to_publish",
    "published",
    "error",
]


class BibleVideoFactoryService:
    def __init__(self):
        self.ai = AIContentGenerator()

    def _series_profile_defaults(self, production_profile: Any) -> Dict[str, Any]:
        profile = self._normalize_text(production_profile).lower()
        if profile in {"serie netflix biblica", "netflix_biblica", "netflix"}:
            return {
                "name": "Serie Netflix Biblica",
                "cliffhanger_required": True,
                "minimum_retention_score": 80,
                "minimum_cliffhanger_score": 85,
                "cliffhanger_prompt_weight": "maximo",
                "strong_hook_first_15_seconds": True,
                "narrative_tone": "cinematografico",
                "emotional_curve": "crescente",
                "serialized_narrative": True,
                "auto_next_episode_cta": True,
                "auto_shorts_count": 3,
            }
        return {}

    def resolve_series_profile(self, series: Optional[BibleVideoSeries]) -> Dict[str, Any]:
        if not series:
            return {}
        saved = self._json_loads(getattr(series, "production_profile_json", None), {})
        if not isinstance(saved, dict):
            saved = {}
        return {**self._series_profile_defaults(getattr(series, "production_profile", "")), **saved}

    def _normalize_scalar(self, value: Any) -> Any:
        if isinstance(value, list):
            return value[0] if value else ""
        if value is None:
            return ""
        return value

    def _normalize_text(self, value: Any) -> str:
        value = self._normalize_scalar(value)
        return str(value).strip()

    def _normalize_int(self, value: Any, default: int = 0) -> int:
        value = self._normalize_scalar(value)
        if value in ("", None):
            return default
        try:
            return int(float(str(value).strip()))
        except Exception:
            return default

    def _json_dumps(self, value: Any) -> str:
        try:
            return json.dumps(value or {}, ensure_ascii=False)
        except Exception:
            return json.dumps({"raw": str(value)}, ensure_ascii=False)

    def _json_loads(self, raw: Any, default: Any):
        if raw is None:
            return default
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(raw)
        except Exception:
            return default

    def _sanitize_ai_json_text(self, text: str) -> str:
        t = self._normalize_text(text)
        if not t:
            return t
        if t.startswith("```"):
            t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
            t = re.sub(r"\s*```$", "", t)
        first_obj = t.find("{")
        first_arr = t.find("[")
        starts = [x for x in [first_obj, first_arr] if x >= 0]
        if starts:
            start = min(starts)
            t = t[start:]
        end_obj = t.rfind("}")
        end_arr = t.rfind("]")
        end = max(end_obj, end_arr)
        if end >= 0:
            t = t[: end + 1]
        return self._normalize_text(t)

    def _generate_json(self, prompt: str, system_prompt: str, fallback: Any):
        try:
            raw = self.ai._generate_text(prompt, system_prompt=system_prompt, temperature=0.5, json_mode=True)
            cleaned = self._sanitize_ai_json_text(raw)
            if not cleaned:
                return fallback
            return json.loads(cleaned)
        except Exception:
            return fallback

    def _split_text_chunks(self, text: str, count: int) -> List[str]:
        raw = self._normalize_text(text).replace("\r\n", "\n").replace("\r", "\n")
        if not raw:
            return []
        parts = [p.strip() for p in raw.split("\n\n") if p.strip()]
        if len(parts) < count:
            parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", raw.replace("\n", " ")) if p.strip()]
        if not parts:
            return []
        count = max(1, int(count or 1))
        group_size = max(1, int(math.ceil(len(parts) / float(count))))
        chunks = []
        for idx in range(0, len(parts), group_size):
            chunk = " ".join(parts[idx : idx + group_size]).strip()
            if chunk:
                chunks.append(chunk)
        while len(chunks) < count:
            chunks.append(chunks[-1] if chunks else raw[:500])
        return chunks[:count]

    def _word_count(self, text: Any) -> int:
        raw = self._normalize_text(text)
        if not raw:
            return 0
        return len([part for part in re.split(r"\s+", raw) if part])

    def _ensure_list(self, value: Any) -> List[Any]:
        if isinstance(value, list):
            return value
        if value in (None, "", {}):
            return []
        return [value]

    def _scene_duration(self, minutes: int, scene_count: int) -> int:
        total_seconds = max(60, int(minutes or 5) * 60)
        count = max(1, int(scene_count or 1))
        return max(15, round(total_seconds / count))

    def _build_text_from_sections(self, sections: Dict[str, Any]) -> str:
        ordered = [
            self._normalize_text(sections.get("opening_hook")),
            self._normalize_text(sections.get("introduction")),
            self._normalize_text(sections.get("development")),
            self._normalize_text(sections.get("climax")),
            self._normalize_text(sections.get("impact_phrase")),
            self._normalize_text(sections.get("cliffhanger")),
            self._normalize_text(sections.get("cta_subscribe")),
            self._normalize_text(sections.get("cta_next_episode")),
        ]
        return "\n\n".join([part for part in ordered if part])

    def _build_text_from_storyboard(self, storyboard: List[Dict[str, Any]]) -> str:
        if not isinstance(storyboard, list):
            return ""
        parts = []
        for item in storyboard:
            if not isinstance(item, dict):
                text = self._normalize_text(item)
            else:
                text = self._normalize_text(item.get("narration") or item.get("narrative") or item.get("title"))
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    def _build_text_from_scene_sources(self, scene_sources: List[Dict[str, Any]]) -> str:
        if not isinstance(scene_sources, list):
            return ""
        parts = []
        for item in scene_sources:
            if not isinstance(item, dict):
                text = self._normalize_text(item)
            else:
                text = self._normalize_text(item.get("text") or item.get("narration") or item.get("title"))
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    def _ensure_word_target(self, text: str, minimum_words: int, maximum_words: int, sections: Dict[str, Any], episode: BibleVideoEpisode, series: Optional[BibleVideoSeries]) -> str:
        raw = self._normalize_text(text)
        if not raw:
            raw = self._build_text_from_sections(sections)
        detail_bank = [
            f"O ambiente de {series.bible_book if series and series.bible_book else 'um cenario biblico'} ganha peso dramatico enquanto a historia de {series.main_character if series and series.main_character else episode.title} avanca com reverencia, tensao e detalhes visuais que ajudam o espectador a sentir o momento.",
            f"Os sons do vento, dos passos, da respiracao e do silencio ao redor reforcam a emocao dos personagens, sem alterar os fatos centrais do relato biblico.",
            f"Cada gesto, olhar e pausa precisa revelar o conflito interno, a fe, o medo e a coragem envolvidos nesta passagem, em linguagem simples e cinematografica.",
            f"O espectador deve perceber o clima do lugar, a luz, a poeira, o movimento da multidao e a carga espiritual do acontecimento, mantendo fidelidade ao texto biblico.",
            f"Este episodio cresce em emocao ate o climax, deixando uma frase memoravel e um final com forte curiosidade para o proximo capitulo da serie.",
        ]
        idx = 0
        while self._word_count(raw) < minimum_words and idx < 12:
            raw = f"{raw}\n\n{detail_bank[idx % len(detail_bank)]}".strip()
            idx += 1
        words = raw.split()
        if len(words) > maximum_words:
            raw = " ".join(words[:maximum_words]).strip()
        return raw

    def _extract_script_package(self, script: Optional[BibleVideoScript]) -> Dict[str, Any]:
        if not script:
            return {}
        data = self._json_loads(script.optional_dialogues_json, [])
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"optional_dialogues": data}
        return {}

    def _save_script_package(self, script: BibleVideoScript, payload: Dict[str, Any]):
        current = self._extract_script_package(script)
        merged = {**current, **(payload or {})}
        script.optional_dialogues_json = self._json_dumps(merged)

    def _has_meaningful_retention_analysis(self, analysis: Any) -> bool:
        if not isinstance(analysis, dict):
            return False
        score_keys = [
            "overall_score",
            "hook_strength",
            "emotion_score",
            "conflict_score",
            "dramatic_progression_score",
            "revelation_score",
            "cliffhanger_score",
            "cliffhanger_impact_score",
        ]
        return any(self._normalize_int(analysis.get(key), 0) > 0 for key in score_keys)

    def _pick_revelation_excerpt(self, narration: Any, script_sections: Dict[str, Any]) -> str:
        raw = self._normalize_text(narration or self._build_text_from_sections(script_sections or {}))
        sentences = self._split_sentences(raw)
        return self._sentence_with_keywords(
            sentences,
            ["revelacao", "segredo", "verdade", "mist", "descob", "surpresa", "mudara a historia", "mudara o destino", "viria a tona", "foi entao que"],
            fallback=self._normalize_text((script_sections or {}).get("climax") or (script_sections or {}).get("cliffhanger") or raw[:240]),
            reverse=True,
        )

    def _rebuild_script_analysis(
        self,
        script: BibleVideoScript,
        profile_config: Optional[Dict[str, Any]] = None,
        episode: Optional[BibleVideoEpisode] = None,
        storyboard: Optional[List[Dict[str, Any]]] = None,
        persist: bool = False,
    ) -> Dict[str, Any]:
        package = self._extract_script_package(script)
        if not isinstance(package, dict):
            package = {}
        storyboard = self._normalize_storyboard_list(storyboard if storyboard is not None else package.get("storyboard"))
        scene_sources = self._get_scene_sources(script)
        current_sections = package.get("script_sections") if isinstance(package.get("script_sections"), dict) else {}
        candidate_parts = [
            self._normalize_text(script.full_narration),
            self._build_text_from_sections(current_sections),
            self._build_text_from_storyboard(storyboard),
            self._build_text_from_scene_sources(scene_sources),
        ]
        if episode:
            candidate_parts.extend(
                [
                    self._normalize_text(episode.opening_hook),
                    self._normalize_text(episode.summary),
                    self._normalize_text(episode.development_text),
                    self._normalize_text(episode.tension_moment),
                    self._normalize_text(episode.impact_phrase),
                    self._normalize_text(episode.ending_hook),
                ]
            )
        narration = max((part for part in candidate_parts if part), key=len, default="")
        if not narration:
            narration = self._normalize_text(script.full_narration)
        derived_sections = self._detect_cinematic_sections(narration, current_sections)
        if episode:
            derived_sections["episode_title"] = self._normalize_text(derived_sections.get("episode_title") or episode.title)
            derived_sections["opening_hook"] = self._normalize_text(derived_sections.get("opening_hook") or episode.opening_hook)
            derived_sections["development"] = self._normalize_text(derived_sections.get("development") or episode.development_text or episode.summary)
            derived_sections["climax"] = self._normalize_text(derived_sections.get("climax") or episode.tension_moment)
            derived_sections["impact_phrase"] = self._normalize_text(derived_sections.get("impact_phrase") or episode.impact_phrase)
            derived_sections["cliffhanger"] = self._normalize_text(derived_sections.get("cliffhanger") or episode.ending_hook)
            derived_sections["cta_subscribe"] = self._normalize_text(derived_sections.get("cta_subscribe") or script.subscribe_cta)
            derived_sections["cta_next_episode"] = self._normalize_text(derived_sections.get("cta_next_episode") or script.next_episode_cta)
        narration = self._normalize_text(narration or self._build_text_from_sections(derived_sections))
        analysis = self._calculate_retention_analysis(
            derived_sections,
            narration,
            storyboard or scene_sources,
            desired_duration_minutes=int(script.desired_duration_minutes or (episode.estimated_minutes if episode and episode.estimated_minutes else 5) or 5),
            drama_level=int(script.drama_level or 7),
            minimum_required_score=self._normalize_int((profile_config or {}).get("minimum_retention_score"), 0),
            profile_config=profile_config or {},
        )
        analysis["detected_excerpts"] = {
            "hook": self._normalize_text(derived_sections.get("opening_hook")),
            "revelation": self._pick_revelation_excerpt(narration, derived_sections),
            "climax": self._normalize_text(derived_sections.get("climax")),
            "cliffhanger": self._normalize_text(derived_sections.get("cliffhanger")),
        }
        if persist:
            self._save_script_package(
                script,
                {
                    "script_sections": derived_sections,
                    "retention_analysis": analysis,
                    "storyboard": storyboard or package.get("storyboard") or [],
                },
            )
        return {
            "script_sections": derived_sections,
            "retention_analysis": analysis,
            "narration": narration,
            "storyboard": storyboard,
        }

    def _normalize_storyboard_frame(self, item: Any, scene_number: int) -> Dict[str, Any]:
        if not isinstance(item, dict):
            item = {"narration": self._normalize_text(item)}
        prompt_visual = self._normalize_text(item.get("prompt_visual") or item.get("prompt_image") or item.get("image_prompt") or item.get("image"))
        storyboard_image = self._normalize_text(item.get("storyboard_image") or item.get("image"))
        prompt_image = self._normalize_text(item.get("prompt_image") or item.get("image_prompt") or prompt_visual)
        prompt_video = self._normalize_text(item.get("prompt_video") or item.get("video_prompt") or prompt_image)
        return {
            "scene_number": self._normalize_int(item.get("scene_number") or scene_number, scene_number),
            "title": self._normalize_text(item.get("title") or f"Cena {scene_number}"),
            "narration": self._normalize_text(item.get("narration") or item.get("narration_text") or item.get("text")),
            "narrative": self._normalize_text(item.get("narrative") or item.get("narration") or item.get("narration_text") or item.get("text")),
            "emotion": self._normalize_text(item.get("emotion")),
            "prompt_visual": prompt_visual,
            "prompt_image": prompt_image,
            "prompt_video": prompt_video,
            "camera_movement": self._normalize_text(item.get("camera_movement") or item.get("camera_direction") or item.get("camera_type")),
            "duration": float(item.get("duration") or item.get("duration_seconds") or 0),
            "suggested_soundtrack": self._normalize_text(item.get("suggested_soundtrack") or item.get("music_style")),
            "storyboard_image": storyboard_image,
            "image": storyboard_image or prompt_visual,
            "approval_status": self._normalize_text(item.get("approval_status") or "pending"),
        }

    def _scene_identity_key(self, item: Dict[str, Any], fallback_index: int) -> str:
        if not isinstance(item, dict):
            return f"fallback:{fallback_index}"
        scene_number = self._normalize_int(item.get("scene_number"), 0)
        if scene_number > 0:
            return f"scene_number:{scene_number}"
        title = self._normalize_text(item.get("title")).lower()
        narration = self._normalize_text(item.get("narration") or item.get("narration_text") or item.get("text")).lower()
        if title or narration:
            return f"text:{title}|{narration[:180]}"
        return f"fallback:{fallback_index}"

    def _merge_scene_like_items(self, base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(base, dict):
            base = {}
        if not isinstance(incoming, dict):
            incoming = {}
        merged = dict(base)
        for key, value in incoming.items():
            if key not in merged or not self._normalize_text(merged.get(key)):
                merged[key] = value
        return merged

    def _dedupe_scene_dicts(self, items: Any, renumber: bool = True, log_label: str = "scenes") -> List[Dict[str, Any]]:
        if not isinstance(items, list):
            return []
        deduped_map = {}
        ordered_keys = []
        for idx, raw in enumerate(items):
            item = dict(raw) if isinstance(raw, dict) else {"text": self._normalize_text(raw)}
            key = self._scene_identity_key(item, idx)
            if key in deduped_map:
                deduped_map[key] = self._merge_scene_like_items(deduped_map[key], item)
            else:
                deduped_map[key] = item
                ordered_keys.append(key)
        deduped = [deduped_map[key] for key in ordered_keys]
        if renumber:
            for idx, item in enumerate(deduped, start=1):
                item["scene_number"] = idx
                item["title"] = self._normalize_text(item.get("title") or f"Cena {idx}")
        print(
            f"[BibleVideoFactoryService] {log_label}: entradas={len(items)} unicas={len(deduped)} duplicadas_removidas={max(0, len(items) - len(deduped))}"
        )
        return deduped

    def _normalize_storyboard_list(self, storyboard: Any) -> List[Dict[str, Any]]:
        if not isinstance(storyboard, list):
            return []
        normalized = [self._normalize_storyboard_frame(item, idx + 1) for idx, item in enumerate(storyboard)]
        return [self._normalize_storyboard_frame(item, idx + 1) for idx, item in enumerate(self._dedupe_scene_dicts(normalized, renumber=True, log_label="storyboard"))]

    def _get_storyboard(self, script: BibleVideoScript) -> List[Dict[str, Any]]:
        package = self._extract_script_package(script)
        return self._normalize_storyboard_list(package.get("storyboard") if isinstance(package, dict) else [])

    def _get_scene_sources(self, script: BibleVideoScript) -> List[Dict[str, Any]]:
        scenes = self._json_loads(script.scenes_json, [])
        if not isinstance(scenes, list):
            return []
        normalized = []
        for idx, item in enumerate(scenes):
            if not isinstance(item, dict):
                item = {"text": self._normalize_text(item)}
            normalized.append(
                {
                    **item,
                    "scene_number": self._normalize_int(item.get("scene_number") or idx + 1, idx + 1),
                }
            )
        return self._dedupe_scene_dicts(normalized, renumber=True, log_label="scene_sources")

    def _scene_source_to_storyboard_frame(self, item: Dict[str, Any], idx: int) -> Dict[str, Any]:
        return self._normalize_storyboard_frame(
            {
                "scene_number": item.get("scene_number") or idx + 1,
                "title": item.get("title"),
                "narration": item.get("narration") or item.get("text"),
                "emotion": item.get("emotion"),
                "prompt_visual": item.get("prompt_image") or item.get("image_prompt"),
                "prompt_image": item.get("prompt_image") or item.get("image_prompt"),
                "prompt_video": item.get("prompt_video") or item.get("video_prompt"),
                "camera_movement": item.get("camera_direction"),
                "duration": item.get("duration"),
                "suggested_soundtrack": item.get("music_style"),
                "storyboard_image": item.get("storyboard_image"),
                "approval_status": item.get("approval_status") or "pending",
            },
            idx + 1,
        )

    def _update_scene_row_from_storyboard_frame(self, row: BibleVideoScene, frame: Dict[str, Any], scene_source: Optional[Dict[str, Any]] = None):
        meta = self._json_loads(row.effects_json, {})
        if not isinstance(meta, dict):
            meta = {}
        row.narration_text = self._normalize_text(frame.get("narration") or row.narration_text)
        row.emotion = self._normalize_text(frame.get("emotion") or row.emotion)
        row.prompt_image = self._normalize_text(frame.get("prompt_image") or frame.get("prompt_visual") or row.prompt_image)
        row.duration_seconds = float(frame.get("duration") or row.duration_seconds or 0)
        row.camera_type = self._normalize_text(frame.get("camera_movement") or row.camera_type)
        meta["title"] = self._normalize_text(frame.get("title") or meta.get("title") or f"Cena {row.scene_number}")
        meta["camera_direction"] = self._normalize_text(frame.get("camera_movement") or meta.get("camera_direction") or row.camera_type)
        meta["music_style"] = self._normalize_text(frame.get("suggested_soundtrack") or meta.get("music_style"))
        meta["storyboard_image"] = self._normalize_text(frame.get("storyboard_image") or meta.get("storyboard_image"))
        meta["approval_status"] = self._normalize_text(frame.get("approval_status") or meta.get("approval_status") or "pending")
        meta["prompt_video"] = self._normalize_text(frame.get("prompt_video") or meta.get("prompt_video"))
        if scene_source:
            meta["prompt_video"] = self._normalize_text(frame.get("prompt_video") or scene_source.get("prompt_video") or meta.get("prompt_video"))
            meta["prompt_cinematic"] = self._normalize_text(scene_source.get("prompt_cinematic") or meta.get("prompt_cinematic"))
            meta["sound_effects"] = self._normalize_text(scene_source.get("sound_effects") or meta.get("sound_effects"))
            row.prompt_animation = self._normalize_text(scene_source.get("prompt_animation") or row.prompt_animation)
            row.visual_description = self._normalize_text(scene_source.get("visual_description") or scene_source.get("caption") or row.visual_description)
        row.effects_json = self._json_dumps(meta)

    def _save_storyboard_state(
        self,
        db: Session,
        script: BibleVideoScript,
        storyboard: List[Dict[str, Any]],
        scene_sources: Optional[List[Dict[str, Any]]] = None,
        episode: Optional[BibleVideoEpisode] = None,
        pipeline_status: Optional[str] = None,
    ):
        storyboard = self._normalize_storyboard_list(storyboard)
        if scene_sources is None:
            scene_sources = self._get_scene_sources(script)
        scene_sources_by_number = {self._normalize_int(item.get("scene_number"), idx + 1): item for idx, item in enumerate(scene_sources)}
        storyboard_by_number = {self._normalize_int(item.get("scene_number"), idx + 1): item for idx, item in enumerate(storyboard)}

        normalized_scene_sources = []
        for idx in range(max(len(scene_sources), len(storyboard))):
            base = scene_sources[idx] if idx < len(scene_sources) else {}
            scene_number = self._normalize_int((base or {}).get("scene_number") or idx + 1, idx + 1)
            frame = storyboard_by_number.get(scene_number) or self._scene_source_to_storyboard_frame(base or {}, idx)
            normalized_scene_sources.append(
                {
                    **(base or {}),
                    "scene_number": scene_number,
                    "title": self._normalize_text(frame.get("title")),
                    "text": self._normalize_text(frame.get("narration") or (base or {}).get("text")),
                    "prompt_image": self._normalize_text(frame.get("prompt_image") or frame.get("prompt_visual") or (base or {}).get("prompt_image") or (base or {}).get("image_prompt")),
                    "image_prompt": self._normalize_text(frame.get("prompt_image") or frame.get("prompt_visual") or (base or {}).get("image_prompt") or (base or {}).get("prompt_image")),
                    "prompt_video": self._normalize_text(frame.get("prompt_video") or (base or {}).get("prompt_video") or (base or {}).get("video_prompt")),
                    "camera_direction": self._normalize_text(frame.get("camera_movement") or (base or {}).get("camera_direction")),
                    "duration": float(frame.get("duration") or (base or {}).get("duration") or 0),
                    "emotion": self._normalize_text(frame.get("emotion") or (base or {}).get("emotion")),
                    "music_style": self._normalize_text(frame.get("suggested_soundtrack") or (base or {}).get("music_style")),
                    "storyboard_image": self._normalize_text(frame.get("storyboard_image") or (base or {}).get("storyboard_image")),
                    "approval_status": self._normalize_text(frame.get("approval_status") or (base or {}).get("approval_status") or "pending"),
                }
            )

        script.scenes_json = self._json_dumps(normalized_scene_sources)
        package = self._extract_script_package(script)
        blueprint = package.get("production_blueprint") if isinstance(package.get("production_blueprint"), dict) else {}
        if pipeline_status:
            blueprint = {**blueprint, "pipeline_status": pipeline_status}
        self._save_script_package(
            script,
            {
                "storyboard": storyboard,
                "production_blueprint": blueprint,
            },
        )

        rows = db.query(BibleVideoScene).filter(BibleVideoScene.script_id == script.id).all()
        for row in rows:
            frame = storyboard_by_number.get(int(row.scene_number or 0))
            source = scene_sources_by_number.get(int(row.scene_number or 0))
            if frame:
                self._update_scene_row_from_storyboard_frame(row, frame, source)

        if episode:
            all_approved = bool(storyboard) and all(self._normalize_text(item.get("approval_status")).lower() == "approved" for item in storyboard)
            episode.status = "storyboard_approved" if all_approved else "storyboard_generated"

    def _get_character_profiles_for_series(self, db: Session, series_id: Optional[int]) -> List[Dict[str, Any]]:
        if not series_id:
            return []
        rows = db.query(BibleVideoCharacter).filter(BibleVideoCharacter.series_id == series_id).all()
        return [self._character_profile(row) for row in rows]

    def _get_scenario_profiles_for_series(self, db: Session, series_id: Optional[int]) -> List[Dict[str, Any]]:
        if not series_id:
            return []
        rows = db.query(BibleVideoScenario).filter(BibleVideoScenario.series_id == series_id).all()
        return [
            {
                "name": row.name,
                "description": row.description,
                "master_prompt": row.base_prompt,
                "visual_style": row.visual_style,
                "reference_image_url": row.reference_image_url,
            }
            for row in rows
        ]

    def _parse_character_meta(self, row: BibleVideoCharacter) -> Dict[str, Any]:
        data = self._json_loads(row.emotions_json, [])
        if isinstance(data, dict):
            meta = dict(data)
            meta["emotions"] = self._ensure_list(meta.get("emotions"))
            return meta
        return {"emotions": self._ensure_list(data)}

    def _character_profile(self, row: BibleVideoCharacter) -> Dict[str, Any]:
        meta = self._parse_character_meta(row)
        master_prompt = self._normalize_text(meta.get("master_prompt") or row.base_prompt)
        appearance = self._normalize_text(meta.get("appearance") or row.description)
        return {
            "name": row.name,
            "age": row.approximate_age,
            "appearance": appearance,
            "skin_tone": self._normalize_text(meta.get("skin_tone")),
            "hair": row.hair,
            "beard": self._normalize_text(meta.get("beard")),
            "clothing": row.clothing,
            "accessories": self._normalize_text(meta.get("accessories")),
            "eye_color": self._normalize_text(meta.get("eye_color")),
            "height": self._normalize_text(meta.get("height")),
            "description": row.description,
            "master_prompt": master_prompt,
            "visual_style": row.visual_style,
            "reference_image_url": row.reference_image_url,
            "emotions": self._ensure_list(meta.get("emotions")),
            "consistency_lock": bool(meta.get("consistency_lock", True)),
            "season_consistency_lock": bool(meta.get("season_consistency_lock", True)),
        }

    def build_character_master_prompt(self, payload: Dict[str, Any]) -> str:
        name = self._normalize_text(payload.get("name"))
        age = self._normalize_text(payload.get("approximate_age") or payload.get("age"))
        height = self._normalize_text(payload.get("height"))
        skin_tone = self._normalize_text(payload.get("skin_tone"))
        eye_color = self._normalize_text(payload.get("eye_color"))
        hair = self._normalize_text(payload.get("hair"))
        beard = self._normalize_text(payload.get("beard"))
        clothing = self._normalize_text(payload.get("clothing"))
        accessories = self._normalize_text(payload.get("accessories"))
        description = self._normalize_text(payload.get("description"))
        appearance = self._normalize_text(payload.get("appearance") or description)
        visual_style = self._normalize_text(payload.get("visual_style") or "cinematografico biblico")
        return self._normalize_text(
            f"PROMPT MESTRE UNICO DO PERSONAGEM: {name}. "
            f"Idade {age}, altura {height}, tom de pele {skin_tone}, olhos {eye_color}, cabelo {hair}, barba {beard}, "
            f"roupa padrao {clothing}, acessorios {accessories}, aparencia {appearance}, descricao detalhada: {description}. "
            f"Manter exatamente o mesmo rosto, idade aparente, altura, tom de pele, cabelo, barba, olhos, roupas, acessorios e proporcoes em todas as cenas e em toda a temporada. "
            f"Estilo visual {visual_style}. Reutilizar obrigatoriamente este prompt mestre em todas as geracoes. Proibido mudar a aparencia durante a temporada."
        )

    def prepare_character_bible_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(payload or {})
        required_fields = {
            "name": "Nome",
            "approximate_age": "Idade",
            "height": "Altura",
            "skin_tone": "Tom de pele",
            "eye_color": "Cor dos olhos",
            "hair": "Cabelo",
            "beard": "Barba",
            "clothing": "Roupa padrao",
            "accessories": "Acessorios",
            "description": "Descricao detalhada",
        }
        missing = [label for key, label in required_fields.items() if not self._normalize_text(data.get(key))]
        if missing:
            raise ValueError(f"Character Bible incompleto. Preencha: {', '.join(missing)}.")
        if not self._normalize_text(data.get("appearance")):
            data["appearance"] = self._normalize_text(data.get("description"))
        if not self._normalize_text(data.get("master_prompt")):
            data["master_prompt"] = self.build_character_master_prompt(data)
        data["season_consistency_lock"] = True
        data["consistency_lock"] = True
        return data

    def _find_character_profiles(self, character_names: Any, profiles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        names = {self._normalize_text(name).lower() for name in self._ensure_list(character_names) if self._normalize_text(name)}
        if not names:
            return profiles[:1]
        matched = [profile for profile in profiles if self._normalize_text(profile.get("name")).lower() in names]
        return matched or profiles[:1]

    def _compose_scene_prompt_with_character_bible(self, base_prompt: str, matched_profiles: List[Dict[str, Any]], scenario_name: str, emotion: str) -> str:
        prompt = self._normalize_text(base_prompt)
        locks = []
        for profile in matched_profiles:
            master_prompt = self._normalize_text(profile.get("master_prompt"))
            if master_prompt:
                locks.append(f"Character master prompt locked: {master_prompt}")
        lock_text = " ".join(locks)
        scenario_text = self._normalize_text(scenario_name)
        emotion_text = self._normalize_text(emotion)
        return self._normalize_text(
            f"{lock_text} Cenario: {scenario_text}. Emocao: {emotion_text}. {prompt} "
            "Use exatamente o mesmo personagem e o mesmo prompt mestre unico em todas as cenas e durante toda a temporada. "
            "Nao altere rosto, idade aparente, altura, tom de pele, cabelo, barba, olhos, roupa, proporcoes ou acessorios."
        )

    def _split_sentences(self, text: Any) -> List[str]:
        raw = self._normalize_text(text).replace("\r\n", "\n").replace("\r", "\n")
        if not raw:
            return []
        parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", raw.replace("\n", " ")) if part.strip()]
        return parts or [raw]

    def _keyword_hits(self, text: Any, keywords: List[str]) -> int:
        raw = self._normalize_text(text).lower()
        if not raw:
            return 0
        return sum(1 for keyword in keywords if keyword in raw)

    def _sentence_with_keywords(self, sentences: List[str], keywords: List[str], fallback: str = "", reverse: bool = False) -> str:
        ordered = list(reversed(sentences)) if reverse else list(sentences)
        for sentence in ordered:
            if self._keyword_hits(sentence, keywords):
                return self._normalize_text(sentence)
        return self._normalize_text(fallback or (ordered[0] if ordered else ""))

    def _short_memorable_sentence(self, sentences: List[str], fallback: str = "") -> str:
        candidates = []
        for sentence in sentences:
            words = self._word_count(sentence)
            if 4 <= words <= 18:
                candidates.append(sentence)
        if candidates:
            candidates.sort(key=lambda item: (abs(self._word_count(item) - 9), len(item)))
            return self._normalize_text(candidates[0])
        return self._normalize_text(fallback or (sentences[-1] if sentences else ""))

    def _has_question_signal(self, text: Any) -> bool:
        raw = self._normalize_text(text).lower()
        if not raw:
            return False
        return "?" in raw or any(token in raw for token in ["o que", "quem", "como", "quando", "por que", "sera que", "ate onde"])

    def _analyze_cliffhanger_impact(self, cliffhanger: Any, next_episode_cta: Any = "", netflix_mode: bool = False) -> Dict[str, Any]:
        text = self._normalize_text(cliffhanger)
        combined = self._normalize_text(f"{text} {next_episode_cta}").lower()
        emotion_keywords = ["medo", "dor", "lagr", "coracao", "culpa", "esperanca", "choque", "angust", "tremia", "emocao"]
        revelation_keywords = ["revelacao", "segredo", "verdade", "descobrir", "mostrar", "revelar", "ainda nao sabia", "viria a tona", "mudaria para sempre", "mudara para sempre", "nao estava completo", "incompleta"]
        threat_keywords = ["ameaca", "terrivel", "perigo", "inimigo", "morte", "destruicao", "armadilha", "contra", "farao", "gigante", "risco", "consequencia", "poderia perder", "custo"]
        promise_keywords = ["promessa", "destino", "chamado", "vitoria", "libertacao", "deus faria", "deus mostraria", "cumprir", "proposito", "no proximo episodio", "mudara para sempre", "mudaria para sempre"]
        future_keywords = ["em breve", "logo", "no proximo", "ainda", "viria", "estava prestes", "estava por", "futuro", "seguinte"]

        components = {
            "gancho_emocional": self._keyword_hits(text, emotion_keywords) > 0,
            "pergunta_sem_resposta": self._has_question_signal(text),
            "revelacao_futura": self._keyword_hits(combined, revelation_keywords) > 0,
            "ameaca_futura": self._keyword_hits(combined, threat_keywords) > 0,
            "promessa_futura": self._keyword_hits(combined, promise_keywords) > 0,
        }
        components["revelacao_incompleta"] = components["revelacao_futura"] and (
            "?" in text
            or any(token in combined for token in ["incompleta", "ainda nao", "viria a tona", "o que", "por que", "quem", "nao estava completo", "descobrir"])
        )
        components["risco_ou_consequencia_futura"] = components["ameaca_futura"] and (
            self._keyword_hits(combined, future_keywords) > 0
            or any(token in combined for token in ["vai", "ira", "pode", "podera", "mudara", "mudaria", "destruir", "custo", "consequencia", "destino"])
        )

        impact_score = 20 if text else 0
        impact_score += 8 if text.lower().startswith("mas") else 0
        impact_score += 12 if components["gancho_emocional"] else 0
        impact_score += 20 if components["pergunta_sem_resposta"] else 0
        impact_score += 18 if components["promessa_futura"] else 0
        impact_score += 18 if components["revelacao_incompleta"] else (10 if components["revelacao_futura"] else 0)
        impact_score += 18 if components["risco_ou_consequencia_futura"] else (10 if components["ameaca_futura"] else 0)
        impact_score += min(8, self._keyword_hits(combined, future_keywords) * 2)
        if netflix_mode and all(
            [
                components["pergunta_sem_resposta"],
                components["promessa_futura"],
                components["revelacao_incompleta"],
                components["risco_ou_consequencia_futura"],
            ]
        ):
            impact_score += 10
        impact_score = min(100, impact_score)

        notes = []
        if components["gancho_emocional"]:
            notes.append("Gancho emocional presente.")
        else:
            notes.append("Falta gancho emocional forte no ultimo bloco.")
        if components["pergunta_sem_resposta"]:
            notes.append("Pergunta sem resposta detectada.")
        else:
            notes.append("Inclua pergunta sem resposta para prolongar curiosidade.")
        if components["revelacao_futura"]:
            notes.append("Ha revelacao futura anunciada.")
        else:
            notes.append("Falta revelar algo importante que so vira depois.")
        if components["ameaca_futura"]:
            notes.append("Ameaca futura identificada.")
        else:
            notes.append("Aumente a sensacao de perigo para o proximo episodio.")
        if components["promessa_futura"]:
            notes.append("Promessa futura esta presente.")
        else:
            notes.append("Inclua promessa futura que recompense o espectador.")
        if not components["revelacao_incompleta"]:
            notes.append("A revelacao ainda precisa ficar incompleta para aumentar curiosidade.")
        if not components["risco_ou_consequencia_futura"]:
            notes.append("Falta risco ou consequencia futura explicita no ultimo bloco.")

        return {
            "impact_score": impact_score,
            "components": components,
            "notes": notes,
            "netflix_requirements_met": (
                all(
                    [
                        components["gancho_emocional"],
                        components["pergunta_sem_resposta"],
                        components["promessa_futura"],
                        components["revelacao_incompleta"],
                        components["risco_ou_consequencia_futura"],
                    ]
                )
                if netflix_mode
                else True
            ),
        }

    def _ensure_netflix_cliffhanger(self, cliffhanger: Any, episode: BibleVideoEpisode, series: Optional[BibleVideoSeries], next_episode_cta: str) -> str:
        current = self._normalize_text(cliffhanger)
        analysis = self._analyze_cliffhanger_impact(current, next_episode_cta, netflix_mode=True)
        if current and analysis.get("netflix_requirements_met"):
            return current
        hero = self._normalize_text(series.main_character if series else "") or self._normalize_text(episode.title) or "o protagonista"
        emotional_setup = self._normalize_text(current)
        if not emotional_setup:
            emotional_setup = f"{hero} e observado em silencio, mas seu olhar ainda carrega medo, destino e algo que ninguem consegue explicar."
        if not emotional_setup.lower().startswith("mas"):
            emotional_setup = f"Mas {emotional_setup[:1].lower()}{emotional_setup[1:]}" if len(emotional_setup) > 1 else f"Mas {emotional_setup}"
        opening_line = f"Samuel observa {hero} em silencio." if hero.lower() != "o protagonista" else f"Todos observam {hero} em silencio."
        question_one = f"Mas uma pergunta permanece: por que Deus escolheu justamente {hero}?"
        question_two = f"O que sera revelado quando a verdade sobre {hero} finalmente vier a tona?"
        incomplete_revelation = f"No proximo episodio, uma revelacao ainda incompleta comecara a mudar para sempre o destino desta historia."
        future_risk = f"Se essa resposta vier tarde demais, Israel enfrentara uma consequencia que pode abalar tudo."
        future_promise = f"E a promessa de Deus para {hero} comecara a se cumprir de forma impossivel de ignorar no proximo episodio."
        return self._normalize_text(
            " ".join(
                [
                    opening_line,
                    emotional_setup,
                    question_one,
                    question_two,
                    incomplete_revelation,
                    future_risk,
                    future_promise,
                    self._normalize_text(next_episode_cta),
                ]
            )
        )

    def _enforce_storyboard_approval_rules(self, script: BibleVideoScript, series: Optional[BibleVideoSeries]):
        profile_config = self.resolve_series_profile(series)
        if not profile_config.get("cliffhanger_required"):
            return
        refreshed = self._rebuild_script_analysis(script, profile_config=profile_config, persist=True)
        retention_analysis = refreshed.get("retention_analysis") if isinstance(refreshed, dict) else {}
        if not isinstance(retention_analysis, dict):
            retention_analysis = {}
        cliffhanger_analysis = retention_analysis.get("cliffhanger_analysis") if isinstance(retention_analysis.get("cliffhanger_analysis"), dict) else {}
        cliffhanger_score = self._normalize_int(cliffhanger_analysis.get("impact_score") or retention_analysis.get("cliffhanger_impact_score"), 0)
        minimum_cliffhanger_score = self._normalize_int(profile_config.get("minimum_cliffhanger_score"), 85)
        if cliffhanger_score < minimum_cliffhanger_score:
            raise ValueError(f"O cliffhanger final precisa atingir pelo menos {minimum_cliffhanger_score}/100 antes da aprovacao. Nota atual: {cliffhanger_score}/100.")

    def _detect_cinematic_sections(self, narration: Any, sections: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        current = dict(sections or {})
        raw = self._normalize_text(narration or self._build_text_from_sections(current))
        paragraphs = [part.strip() for part in raw.split("\n\n") if part.strip()]
        if len(paragraphs) < 4:
            paragraphs = self._split_text_chunks(raw, 4)
        sentences = self._split_sentences(raw)
        first_paragraph = paragraphs[0] if paragraphs else (sentences[0] if sentences else "")
        intro_default = paragraphs[1] if len(paragraphs) > 1 else first_paragraph
        development_default = " ".join(paragraphs[2:-2]).strip() if len(paragraphs) > 4 else (paragraphs[2] if len(paragraphs) > 2 else intro_default)
        climax_default = self._sentence_with_keywords(
            sentences,
            ["climax", "decisao", "batalha", "ruptura", "virada", "confronto", "momento decisivo", "sacrificio", "revelacao"],
            fallback=paragraphs[-2] if len(paragraphs) > 1 else raw,
            reverse=True,
        )
        cliffhanger_default = self._sentence_with_keywords(
            sentences,
            ["mas", "porem", "ainda", "agora", "antes que", "o que vira", "proximo episodio", "continua", "nao imaginava"],
            fallback=paragraphs[-1] if paragraphs else raw,
            reverse=True,
        )
        impact_default = self._sentence_with_keywords(
            sentences,
            ["nunca", "jamais", "sempre", "destino", "fe", "deus", "impossivel", "coragem", "promessa", "milagre"],
            fallback=self._short_memorable_sentence(sentences, fallback=climax_default),
        )
        detected = {
            "episode_title": self._normalize_text(current.get("episode_title")),
            "opening_hook": self._normalize_text(current.get("opening_hook") or first_paragraph or raw[:240]),
            "introduction": self._normalize_text(current.get("introduction") or intro_default),
            "development": self._normalize_text(current.get("development") or development_default),
            "climax": self._normalize_text(current.get("climax") or climax_default),
            "impact_phrase": self._normalize_text(current.get("impact_phrase") or impact_default),
            "cliffhanger": self._normalize_text(current.get("cliffhanger") or cliffhanger_default),
            "cta_subscribe": self._normalize_text(current.get("cta_subscribe")),
            "cta_next_episode": self._normalize_text(current.get("cta_next_episode")),
        }
        return detected

    def _calculate_retention_analysis(
        self,
        sections: Dict[str, Any],
        narration: Any,
        scenes: Optional[List[Dict[str, Any]]] = None,
        desired_duration_minutes: int = 5,
        drama_level: int = 7,
        minimum_required_score: int = 0,
        profile_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        narrative_text = self._normalize_text(narration or self._build_text_from_sections(sections))
        script_sections = self._detect_cinematic_sections(narrative_text, sections)
        scenes = scenes if isinstance(scenes, list) else []
        scene_count = len(scenes)
        sentences = self._split_sentences(narrative_text)
        first_window = " ".join(narrative_text.split()[:45])
        last_window = " ".join(narrative_text.split()[-45:])
        first_scene_duration = 0.0
        if scenes:
            try:
                first_scene_duration = float((scenes[0] or {}).get("duration") or (scenes[0] or {}).get("duration_seconds") or 0)
            except Exception:
                first_scene_duration = 0.0
        if not first_scene_duration and scene_count:
            first_scene_duration = round((float(desired_duration_minutes or 5) * 60.0) / max(1, scene_count), 2)

        hook_keywords = ["segredo", "antes que", "e se", "o que", "ninguem", "nao imaginava", "em segundos", "de repente", "mas"]
        conflict_keywords = ["conflito", "amea", "risco", "perigo", "luta", "fuga", "oposicao", "inimigo", "pressao", "decisao"]
        emotion_keywords = ["medo", "dor", "lagr", "esperanca", "coragem", "culpa", "angust", "amor", "fe", "choque", "emocao"]
        progression_keywords = ["entao", "depois", "enquanto", "ate que", "agora", "cada vez", "ao mesmo tempo", "em seguida"]
        revelation_keywords = ["revelacao", "segredo", "verdade", "mist", "descob", "surpresa", "nao imaginava", "viria a tona", "foi entao que"]
        cliffhanger_keywords = ["mas", "porem", "antes que", "proximo episodio", "continua", "ainda nao", "o que vira", "agora tudo muda"]

        opening_hook = self._normalize_text(script_sections.get("opening_hook"))
        development = self._normalize_text(script_sections.get("development"))
        climax = self._normalize_text(script_sections.get("climax"))
        cliffhanger = self._normalize_text(script_sections.get("cliffhanger"))
        impact_phrase = self._normalize_text(script_sections.get("impact_phrase"))
        netflix_mode = bool((profile_config or {}).get("cliffhanger_required"))
        cliffhanger_analysis = self._analyze_cliffhanger_impact(cliffhanger, script_sections.get("cta_next_episode"), netflix_mode=netflix_mode)

        hook_score = 34
        if opening_hook:
            hook_score += 24
        if "?" in opening_hook or "?" in first_window:
            hook_score += 12
        if self._keyword_hits(opening_hook or first_window, hook_keywords):
            hook_score += min(22, self._keyword_hits(opening_hook or first_window, hook_keywords) * 6)
        if first_scene_duration and first_scene_duration <= 15:
            hook_score += 10
        elif opening_hook and len(opening_hook.split()) <= 30:
            hook_score += 6
        hook_score = min(100, hook_score)

        conflict_score = 30
        conflict_hits = self._keyword_hits(f"{development} {climax} {narrative_text}", conflict_keywords)
        if development:
            conflict_score += 15
        if climax:
            conflict_score += 12
        conflict_score += min(28, conflict_hits * 6)
        conflict_score += min(10, max(0, self._normalize_int(drama_level, 7) - 5) * 2)
        conflict_score = min(100, conflict_score)

        emotion_score = 32
        emotion_hits = self._keyword_hits(f"{script_sections.get('introduction')} {development} {climax} {impact_phrase}", emotion_keywords)
        if self._normalize_text(script_sections.get("introduction")):
            emotion_score += 10
        if impact_phrase:
            emotion_score += 10
        emotion_score += min(30, emotion_hits * 5)
        emotion_score += 6 if "!" in impact_phrase else 0
        emotion_score = min(100, emotion_score)

        revelation_score = 28
        revelation_hits = self._keyword_hits(f"{development} {climax} {cliffhanger} {narrative_text}", revelation_keywords)
        if self._keyword_hits(climax, revelation_keywords):
            revelation_score += 14
        if self._keyword_hits(cliffhanger, revelation_keywords):
            revelation_score += 10
        revelation_score += min(36, revelation_hits * 6)
        revelation_score += 6 if self._normalize_text(script_sections.get("impact_phrase")) else 0
        revelation_score = min(100, revelation_score)

        present_beats = sum(
            1
            for key in ["opening_hook", "introduction", "development", "climax", "cliffhanger", "impact_phrase"]
            if self._normalize_text(script_sections.get(key))
        )
        distinct_beats = len(
            {
                self._normalize_text(script_sections.get(key)).lower()
                for key in ["opening_hook", "introduction", "development", "climax", "cliffhanger", "impact_phrase"]
                if self._normalize_text(script_sections.get(key))
            }
        )
        progression_score = 26 + min(36, present_beats * 9)
        progression_score += 8 if 10 <= scene_count <= 15 else 0
        progression_score += min(18, self._keyword_hits(narrative_text, progression_keywords) * 3)
        progression_score += 8 if distinct_beats >= 5 else 0
        progression_score = min(100, progression_score)

        cliffhanger_score = max(
            cliffhanger_analysis.get("impact_score", 0),
            min(
                100,
                28
                + (22 if cliffhanger else 0)
                + min(28, self._keyword_hits(f"{cliffhanger} {last_window}", cliffhanger_keywords) * 7)
                + (10 if self._normalize_text(script_sections.get("cta_next_episode")) else 0)
                + (6 if cliffhanger and cliffhanger != impact_phrase else 0),
            ),
        )

        overall_score = round(
            (hook_score * 0.20)
            + (conflict_score * 0.16)
            + (emotion_score * 0.16)
            + (progression_score * 0.16)
            + (revelation_score * 0.16)
            + (cliffhanger_score * 0.16)
        )
        retention_score = round((hook_score + progression_score + revelation_score + cliffhanger_score) / 4.0)
        viralization_score = min(100, round((impact_phrase and 76 or 58) + min(18, self._keyword_hits(impact_phrase or narrative_text, ["deus", "fe", "promessa", "impossivel", "destino", "milagre"]) * 4)))

        observations = []
        suggestions = []
        if hook_score >= 80:
            observations.append("Gancho inicial detectado com impacto suficiente para abrir os primeiros 15 segundos.")
        else:
            observations.append("O gancho inicial existe, mas ainda pode ficar mais agressivo nos primeiros 15 segundos.")
            suggestions.append("Abra com pergunta, risco imediato ou revelacao forte antes da primeira virada.")
        if conflict_score >= 75:
            observations.append("O conflito central aparece com clareza e sustenta a tensao do episodio.")
        else:
            observations.append("O conflito central ainda esta difuso ou pouco verbalizado.")
            suggestions.append("Deixe a ameaca, a escolha dificil e o custo emocional mais explicitos no desenvolvimento.")
        if emotion_score >= 75:
            observations.append("A carga emocional esta presente e ajuda a criar conexao com o espectador.")
        else:
            observations.append("A emocao ainda pode crescer mais entre introducao, desenvolvimento e climax.")
            suggestions.append("Adicione medo, fe, culpa, coragem ou perda em frases curtas e visuais.")
        if progression_score >= 80:
            observations.append("A progressao dramatica esta bem distribuida entre apresentacao, escalada e ruptura.")
        else:
            observations.append("A progressao dramatica precisa de escalada mais nitida entre as etapas.")
            suggestions.append("Aumente a tensao a cada bloco e evite cenas com a mesma intensidade narrativa.")
        if revelation_score >= 75:
            observations.append("O roteiro traz revelacoes suficientes para renovar curiosidade ao longo do episodio.")
        else:
            observations.append("Ainda faltam revelacoes fortes para sustentar surpresa e descoberta.")
            suggestions.append("Inclua descoberta, segredo exposto ou verdade revelada em pontos-chave do desenvolvimento e do climax.")
        if cliffhanger_score >= 80:
            observations.append("O final deixa curiosidade real para o proximo episodio.")
        else:
            observations.append("O cliffhanger final ainda nao fecha com curiosidade maxima.")
            suggestions.append("Termine com revelacao incompleta, decisao interrompida ou risco imediato para o proximo episodio.")
        if netflix_mode and not cliffhanger_analysis.get("netflix_requirements_met"):
            observations.append("O perfil Serie Netflix Biblica exige todos os sinais obrigatorios de cliffhanger no ultimo bloco.")
            suggestions.append("Inclua obrigatoriamente gancho emocional, pergunta sem resposta, revelacao futura, ameaca futura e promessa futura.")
        if not impact_phrase:
            suggestions.append("Inclua uma frase de impacto curta, memoravel e reutilizavel para thumbnail, shorts e CTA.")
        if minimum_required_score and overall_score < minimum_required_score:
            suggestions.insert(0, f"Eleve a retencao para pelo menos {minimum_required_score}/100 reforcando hook, conflito e cliffhanger.")

        notes = " ".join(observations[:3]).strip() or "Estrutura cinematografica analisada."
        return {
            "overall_score": overall_score,
            "hook_strength": hook_score,
            "emotion_score": emotion_score,
            "suspense_score": cliffhanger_score,
            "conflict_score": conflict_score,
            "dramatic_progression_score": progression_score,
            "revelation_score": revelation_score,
            "cliffhanger_score": cliffhanger_score,
            "retention_score": retention_score,
            "viralization_score": viralization_score,
            "cliffhanger_impact_score": cliffhanger_analysis.get("impact_score", cliffhanger_score),
            "cliffhanger_analysis": cliffhanger_analysis,
            "notes": notes,
            "observations": observations,
            "suggestions": suggestions,
            "detected_structure": script_sections,
            "detected_excerpts": {
                "hook": opening_hook,
                "revelation": self._pick_revelation_excerpt(narrative_text, script_sections),
                "climax": climax,
                "cliffhanger": cliffhanger,
            },
            "beat_presence": {
                "opening_hook": bool(opening_hook),
                "introduction": bool(self._normalize_text(script_sections.get("introduction"))),
                "development": bool(development),
                "climax": bool(climax),
                "cliffhanger": bool(cliffhanger),
                "impact_phrase": bool(impact_phrase),
            },
            "cliffhanger_requirements_met": cliffhanger_analysis.get("netflix_requirements_met", True),
            "first_15_seconds_hook_detected": bool(opening_hook) and (first_scene_duration <= 15 if first_scene_duration else True),
            "scene_count": scene_count,
            "word_count": self._word_count(narrative_text),
            "minimum_required_score": int(minimum_required_score or 0),
            "passed_minimum_threshold": False if minimum_required_score and overall_score < minimum_required_score else True,
        }

    def _strengthen_sections_for_retention(
        self,
        sections: Dict[str, Any],
        episode: BibleVideoEpisode,
        series: Optional[BibleVideoSeries],
        subscribe_cta: str,
        next_episode_cta: str,
        profile_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        current = self._detect_cinematic_sections(self._build_text_from_sections(sections), sections)
        hero = self._normalize_text(series.main_character if series else "") or self._normalize_text(episode.title) or "o protagonista"
        story_world = self._normalize_text(series.bible_book if series else "") or "o relato biblico"
        strong_hook = self._normalize_text(
            current.get("opening_hook")
            or f"O que aconteceria se {hero} tivesse apenas segundos para decidir entre o medo e o chamado de Deus?"
        )
        if "?" not in strong_hook:
            strong_hook = f"{strong_hook.rstrip('.!')}?"
        return {
            "episode_title": self._normalize_text(current.get("episode_title") or episode.title),
            "opening_hook": strong_hook,
            "introduction": self._normalize_text(
                current.get("introduction")
                or f"No inicio desta etapa de {story_world}, {hero} entra em um ambiente carregado de pressao, expectativa e sinais de que algo irreversivel esta prestes a acontecer."
            ),
            "development": self._normalize_text(
                current.get("development")
                or f"O conflito cresce porque cada passo de {hero} aumenta o risco, expande a oposicao e coloca sua fe contra o medo diante de todos."
            ),
            "climax": self._normalize_text(
                current.get("climax")
                or f"No momento mais intenso, {hero} precisa agir sob maxima tensao, sabendo que uma unica decisao pode mudar para sempre o destino desta historia."
            ),
            "impact_phrase": self._normalize_text(
                current.get("impact_phrase")
                or "Quando Deus chama, o medo nao tem a palavra final."
            ),
            "cliffhanger": self._normalize_text(
                self._ensure_netflix_cliffhanger(
                    current.get("cliffhanger")
                    or f"Mas quando tudo parece resolvido, uma nova ameaca surge e deixa {hero} a um passo de enfrentar o maior desafio do proximo episodio.",
                    episode,
                    series,
                    next_episode_cta,
                )
                if (profile_config or {}).get("cliffhanger_required")
                else (
                    current.get("cliffhanger")
                    or f"Mas quando tudo parece resolvido, uma nova ameaca surge e deixa {hero} a um passo de enfrentar o maior desafio do proximo episodio."
                )
            ),
            "cta_subscribe": self._normalize_text(current.get("cta_subscribe") or subscribe_cta),
            "cta_next_episode": self._normalize_text(current.get("cta_next_episode") or next_episode_cta),
        }

    def _retention_analysis_fallback(self, sections: Dict[str, Any], word_count: int, scene_count: int, drama_level: int) -> Dict[str, Any]:
        estimated_minutes = max(1, round(max(1, int(word_count or 0)) / 150.0))
        fake_scenes = [{"duration": self._scene_duration(estimated_minutes, max(1, int(scene_count or 1)))} for _ in range(max(1, int(scene_count or 1)))]
        return self._calculate_retention_analysis(
            sections,
            self._build_text_from_sections(sections),
            fake_scenes,
            desired_duration_minutes=estimated_minutes,
            drama_level=drama_level,
            profile_config={},
        )

    def _title_from_chunk(self, series: BibleVideoSeries, episode_number: int, chunk: str) -> str:
        words = [w.strip(" ,.;:!?") for w in str(chunk or "").split() if w.strip(" ,.;:!?")]
        if not words:
            base = series.main_character or series.bible_book or series.name
            return f"{base} - Episodio {episode_number}"
        title = " ".join(words[:6]).strip()
        if len(title) < 8:
            title = f"{series.name} - Episodio {episode_number}"
        return title[:90]

    def _estimate_costs(self, config: BibleVideoConfig, duration_minutes: int, scene_count: int, shorts_count: int = 0) -> Dict[str, float]:
        duration = max(1, int(duration_minutes or 1))
        scenes = max(1, int(scene_count or 1))
        words = duration * 155
        text_cost = round((words / 1000.0) * float(config.text_cost_unit or 0), 4)
        voice_cost = round(duration * float(config.voice_cost_unit or 0), 4)
        image_cost = round(scenes * float(config.image_cost_unit or 0), 4)
        video_cost = round(duration * float(config.video_cost_unit or 0), 4)
        music_cost = round(float(config.music_cost_unit or 0), 4)
        caption_cost = round(float(config.caption_cost_unit or 0), 4)
        thumbnail_cost = round(float(config.thumbnail_cost_unit or 0), 4)
        shorts_cost = round(shorts_count * (float(config.video_cost_unit or 0) + float(config.thumbnail_cost_unit or 0)), 4)
        total = round(text_cost + voice_cost + image_cost + video_cost + music_cost + caption_cost + thumbnail_cost + shorts_cost, 4)
        return {
            "text_cost": text_cost,
            "voice_cost": voice_cost,
            "image_cost": image_cost,
            "video_cost": video_cost,
            "music_cost": music_cost,
            "caption_cost": caption_cost,
            "thumbnail_cost": thumbnail_cost,
            "shorts_cost": shorts_cost,
            "total": total,
        }

    def get_or_create_config(self, db: Session, user_id: Optional[int]) -> BibleVideoConfig:
        row = (
            db.query(BibleVideoConfig)
            .filter(BibleVideoConfig.user_id == user_id)
            .order_by(BibleVideoConfig.id.desc())
            .first()
        )
        if row:
            return row
        row = BibleVideoConfig(
            user_id=user_id,
            default_cta="Inscreva-se para acompanhar os proximos episodios biblicos.",
            default_next_episode_cta="No proximo episodio, a historia continua com mais tensao e revelacao.",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def serialize_series(self, row: BibleVideoSeries) -> Dict[str, Any]:
        profile_config = self.resolve_series_profile(row)
        return {
            "id": row.id,
            "user_id": row.user_id,
            "name": row.name,
            "bible_book": row.bible_book,
            "main_character": row.main_character,
            "target_audience": row.target_audience,
            "production_profile": row.production_profile,
            "production_profile_config": profile_config,
            "visual_style": row.visual_style,
            "narrative_tone": row.narrative_tone,
            "planned_episodes": int(row.planned_episodes or 0),
            "episode_duration_minutes": int(row.episode_duration_minutes or 0),
            "language": row.language,
            "linked_channel": row.linked_channel,
            "status": row.status,
            "bible_story_text": row.bible_story_text,
            "series_summary": row.series_summary,
            "notes": row.notes,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def serialize_episode(self, row: BibleVideoEpisode) -> Dict[str, Any]:
        return {
            "id": row.id,
            "series_id": row.series_id,
            "user_id": row.user_id,
            "episode_number": int(row.episode_number or 0),
            "title": row.title,
            "summary": row.summary,
            "biblical_basis": row.biblical_basis,
            "opening_hook": row.opening_hook,
            "development_text": row.development_text,
            "tension_moment": row.tension_moment,
            "impact_phrase": row.impact_phrase,
            "ending_hook": row.ending_hook,
            "short_suggestion": row.short_suggestion,
            "thumbnail_suggestion": row.thumbnail_suggestion,
            "youtube_title_suggestion": row.youtube_title_suggestion,
            "estimated_minutes": int(row.estimated_minutes or 0),
            "status": row.status,
            "approval_status": row.approval_status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def serialize_script(self, row: BibleVideoScript) -> Dict[str, Any]:
        package = self._extract_script_package(row)
        scenes = self._json_loads(row.scenes_json, [])
        script_sections = package.get("script_sections") if isinstance(package, dict) else {}
        retention_analysis = package.get("retention_analysis") if isinstance(package, dict) else {}
        youtube_growth = package.get("youtube_growth") if isinstance(package, dict) else {}
        storyboard = self._normalize_storyboard_list(package.get("storyboard") if isinstance(package, dict) else [])
        blueprint = package.get("production_blueprint") if isinstance(package, dict) else {}
        optional_dialogues = package.get("optional_dialogues") if isinstance(package, dict) else []
        if not isinstance(script_sections, dict) or not script_sections or not self._has_meaningful_retention_analysis(retention_analysis):
            live = self._rebuild_script_analysis(row, storyboard=storyboard, persist=False)
            script_sections = live.get("script_sections") if isinstance(live.get("script_sections"), dict) else {}
            retention_analysis = live.get("retention_analysis") if isinstance(live.get("retention_analysis"), dict) else {}
        narration = row.full_narration or self._build_text_from_sections(script_sections or {})
        return {
            "id": row.id,
            "series_id": row.series_id,
            "episode_id": row.episode_id,
            "user_id": row.user_id,
            "desired_duration_minutes": int(row.desired_duration_minutes or 0),
            "narrative_style": row.narrative_style,
            "drama_level": int(row.drama_level or 0),
            "biblical_fidelity_level": int(row.biblical_fidelity_level or 0),
            "target_audience": row.target_audience,
            "subscribe_cta": row.subscribe_cta,
            "next_episode_cta": row.next_episode_cta,
            "full_narration": narration,
            "word_count": self._word_count(narration),
            "scene_count": len(scenes) if isinstance(scenes, list) else 0,
            "scenes": scenes,
            "optional_dialogues": optional_dialogues if isinstance(optional_dialogues, list) else [],
            "script_sections": script_sections if isinstance(script_sections, dict) else {},
            "retention_analysis": retention_analysis if isinstance(retention_analysis, dict) else {},
            "youtube_growth": youtube_growth if isinstance(youtube_growth, dict) else {},
            "storyboard": storyboard,
            "production_blueprint": blueprint if isinstance(blueprint, dict) else {},
            "voice_emotion_notes": row.voice_emotion_notes,
            "soundtrack_notes": row.soundtrack_notes,
            "sound_effects_notes": row.sound_effects_notes,
            "retention_hooks": self._json_loads(row.retention_hooks_json, []),
            "thumbnail": self._json_loads(row.thumbnail_json, {}),
            "shorts": self._json_loads(row.shorts_json, []),
            "validation_status": row.validation_status,
            "validation_notes": row.validation_notes,
            "validation_flags": self._json_loads(row.validation_flags_json, []),
            "disclaimer_required": bool(row.disclaimer_required),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def serialize_scene(self, row: BibleVideoScene) -> Dict[str, Any]:
        raw_meta = self._json_loads(row.effects_json, [])
        effects = raw_meta if isinstance(raw_meta, list) else self._ensure_list((raw_meta or {}).get("effects"))
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        return {
            "id": row.id,
            "script_id": row.script_id,
            "series_id": row.series_id,
            "episode_id": row.episode_id,
            "user_id": row.user_id,
            "scene_number": int(row.scene_number or 0),
            "title": self._normalize_text(meta.get("title") or f"Cena {int(row.scene_number or 0)}"),
            "narration_text": row.narration_text,
            "narration": row.narration_text,
            "visual_description": row.visual_description,
            "characters": self._json_loads(row.characters_json, []),
            "scenario_name": row.scenario_name,
            "emotion": row.emotion,
            "prompt_image": row.prompt_image,
            "prompt_video": self._normalize_text(meta.get("prompt_video")),
            "prompt_animation": row.prompt_animation,
            "prompt_cinematic": self._normalize_text(meta.get("prompt_cinematic")),
            "duration_seconds": float(row.duration_seconds or 0),
            "camera_type": row.camera_type,
            "camera_direction": self._normalize_text(meta.get("camera_direction") or row.camera_type),
            "effects": effects,
            "sound_effects": self._normalize_text(meta.get("sound_effects")),
            "music_style": self._normalize_text(meta.get("music_style")),
            "provider_prompts": meta.get("provider_prompts") if isinstance(meta.get("provider_prompts"), dict) else {},
            "storyboard_image": self._normalize_text(meta.get("storyboard_image")),
            "approval_status": self._normalize_text(meta.get("approval_status") or "pending"),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def clear_scenes(self, db: Session, script: BibleVideoScript, episode: Optional[BibleVideoEpisode] = None, commit: bool = True) -> Dict[str, Any]:
        database_scene_count = (
            db.query(BibleVideoScene)
            .filter(BibleVideoScene.script_id == script.id)
            .count()
        )
        db.query(BibleVideoScene).filter(BibleVideoScene.script_id == script.id).delete()
        script.scenes_json = self._json_dumps([])
        self._save_script_package(
            script,
            {
                "storyboard": [],
                "production_blueprint": {
                    **(self._extract_script_package(script).get("production_blueprint") or {}),
                    "scene_count": 0,
                    "pipeline_status": "script_generated",
                },
            },
        )
        if episode:
            episode.status = "script_generated"
        if commit:
            db.commit()
            db.refresh(script)
            if episode:
                db.refresh(episode)
        logger.info("cenas removidas script_id=%s banco=%s", script.id, database_scene_count)
        return {
            "database_scene_count": database_scene_count,
            "script": self.serialize_script(script),
        }

    def delete_script(self, db: Session, script: BibleVideoScript, episode: Optional[BibleVideoEpisode] = None) -> Dict[str, Any]:
        database_scene_count = (
            db.query(BibleVideoScene)
            .filter(BibleVideoScene.script_id == script.id)
            .count()
        )
        related_job_count = (
            db.query(BibleVideoJob)
            .filter(BibleVideoJob.script_id == script.id)
            .count()
        )
        db.query(BibleVideoScene).filter(BibleVideoScene.script_id == script.id).delete()
        db.query(BibleVideoJob).filter(BibleVideoJob.script_id == script.id).delete()
        deleted_script_id = script.id
        deleted_episode_id = script.episode_id
        db.delete(script)
        db.commit()
        remaining_scripts = (
            db.query(BibleVideoScript)
            .filter(BibleVideoScript.episode_id == deleted_episode_id)
            .count()
        )
        if episode and remaining_scripts == 0:
            episode.status = "idea"
            db.commit()
            db.refresh(episode)
        logger.info(
            "roteiro excluido script_id=%s cenas_removidas=%s jobs_removidos=%s roteiros_restantes=%s",
            deleted_script_id,
            database_scene_count,
            related_job_count,
            remaining_scripts,
        )
        return {
            "deleted_script_id": deleted_script_id,
            "database_scene_count": database_scene_count,
            "related_job_count": related_job_count,
            "remaining_scripts": remaining_scripts,
        }

    def scene_diagnostics(self, db: Session, script: BibleVideoScript) -> Dict[str, Any]:
        rows = (
            db.query(BibleVideoScene)
            .filter(BibleVideoScene.script_id == script.id)
            .order_by(BibleVideoScene.scene_number.asc(), BibleVideoScene.id.asc())
            .all()
        )
        serialized = [self.serialize_scene(row) for row in rows]
        deduped = []
        seen_numbers = set()
        duplicate_numbers = []
        for item in serialized:
            scene_number = int((item or {}).get("scene_number") or 0)
            if scene_number in seen_numbers:
                duplicate_numbers.append(scene_number)
                continue
            seen_numbers.add(scene_number)
            deduped.append(item)
        scripts_for_episode = (
            db.query(BibleVideoScript)
            .filter(BibleVideoScript.episode_id == script.episode_id)
            .count()
        )
        diagnostics = {
            "script_id": script.id,
            "episode_id": script.episode_id,
            "database_scene_count": len(rows),
            "api_scene_count": len(deduped),
            "duplicate_scene_numbers": sorted({number for number in duplicate_numbers if number > 0}),
            "script_versions_count": scripts_for_episode,
        }
        logger.info(
            "diagnostico cenas script_id=%s banco=%s api=%s duplicadas=%s",
            script.id,
            diagnostics["database_scene_count"],
            diagnostics["api_scene_count"],
            diagnostics["duplicate_scene_numbers"],
        )
        return diagnostics

    def serialize_character(self, row: BibleVideoCharacter) -> Dict[str, Any]:
        profile = self._character_profile(row)
        return {
            "id": row.id,
            "series_id": row.series_id,
            "user_id": row.user_id,
            "name": row.name,
            "description": row.description,
            "approximate_age": row.approximate_age,
            "clothing": row.clothing,
            "hair": row.hair,
            "default_expression": row.default_expression,
            "visual_style": row.visual_style,
            "base_prompt": row.base_prompt,
            "reference_image_url": row.reference_image_url,
            "emotions": profile.get("emotions") or [],
            "appearance": profile.get("appearance"),
            "skin_tone": profile.get("skin_tone"),
            "beard": profile.get("beard"),
            "eye_color": profile.get("eye_color"),
            "height": profile.get("height"),
            "accessories": profile.get("accessories"),
            "master_prompt": profile.get("master_prompt"),
            "consistency_lock": profile.get("consistency_lock"),
            "season_consistency_lock": profile.get("season_consistency_lock"),
            "character_bible": profile,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def serialize_scenario(self, row: BibleVideoScenario) -> Dict[str, Any]:
        return {
            "id": row.id,
            "series_id": row.series_id,
            "user_id": row.user_id,
            "name": row.name,
            "description": row.description,
            "base_prompt": row.base_prompt,
            "visual_style": row.visual_style,
            "reference_image_url": row.reference_image_url,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def serialize_prompt(self, row: BibleVideoPrompt) -> Dict[str, Any]:
        return {
            "id": row.id,
            "user_id": row.user_id,
            "category": row.category,
            "title": row.title,
            "content": row.content,
            "is_active": bool(row.is_active),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def serialize_config(self, row: BibleVideoConfig) -> Dict[str, Any]:
        def _masked(value: Optional[str]) -> bool:
            return bool(self._normalize_text(value))

        return {
            "id": row.id,
            "user_id": row.user_id,
            "text_provider": row.text_provider,
            "voice_provider": row.voice_provider,
            "image_provider": row.image_provider,
            "video_provider": row.video_provider,
            "music_provider": row.music_provider,
            "caption_provider": row.caption_provider,
            "thumbnail_provider": row.thumbnail_provider,
            "text_api_key_configured": _masked(row.text_api_key),
            "voice_api_key_configured": _masked(row.voice_api_key),
            "image_api_key_configured": _masked(row.image_api_key),
            "video_api_key_configured": _masked(row.video_api_key),
            "youtube_api_key_configured": _masked(row.youtube_api_key),
            "tiktok_api_key_configured": _masked(row.tiktok_api_key),
            "instagram_api_key_configured": _masked(row.instagram_api_key),
            "default_voice": row.default_voice,
            "default_voice_speed": float(row.default_voice_speed or 1.0),
            "default_voice_emotion": row.default_voice_emotion,
            "default_voice_intensity": float(row.default_voice_intensity or 0.7),
            "default_language": row.default_language,
            "default_cta": row.default_cta,
            "default_next_episode_cta": row.default_next_episode_cta,
            "default_playlist": row.default_playlist,
            "made_for_kids_default": bool(row.made_for_kids_default),
            "daily_spend_limit": float(row.daily_spend_limit or 0),
            "monthly_spend_limit": float(row.monthly_spend_limit or 0),
            "text_cost_unit": float(row.text_cost_unit or 0),
            "voice_cost_unit": float(row.voice_cost_unit or 0),
            "image_cost_unit": float(row.image_cost_unit or 0),
            "video_cost_unit": float(row.video_cost_unit or 0),
            "music_cost_unit": float(row.music_cost_unit or 0),
            "caption_cost_unit": float(row.caption_cost_unit or 0),
            "thumbnail_cost_unit": float(row.thumbnail_cost_unit or 0),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def serialize_job(self, row: BibleVideoJob) -> Dict[str, Any]:
        progress_task = get_task(row.task_id) if row.task_id else None
        return {
            "id": row.id,
            "user_id": row.user_id,
            "series_id": row.series_id,
            "episode_id": row.episode_id,
            "script_id": row.script_id,
            "parent_job_id": row.parent_job_id,
            "title": row.title,
            "job_type": row.job_type,
            "platform": row.platform,
            "aspect_ratio": row.aspect_ratio,
            "kanban_stage": row.kanban_stage,
            "status": row.status,
            "approval_status": row.approval_status,
            "progress": int(row.progress or 0),
            "status_message": row.status_message,
            "task_id": row.task_id,
            "scheduled_for": row.scheduled_for.isoformat() if row.scheduled_for else None,
            "tags": self._json_loads(row.tags_json, []),
            "description_text": row.description_text,
            "pinned_comment": row.pinned_comment,
            "playlist_name": row.playlist_name,
            "publish_platforms": self._json_loads(row.publish_platforms_json, []),
            "plan": self._json_loads(row.plan_json, {}),
            "result": self._json_loads(row.result_json, {}),
            "output_video_url": row.output_video_url,
            "output_thumbnail_url": row.output_thumbnail_url,
            "published_video_id": row.published_video_id,
            "estimated_cost": float(row.estimated_cost or 0),
            "actual_cost": float(row.actual_cost or 0),
            "error_log": row.error_log,
            "task": progress_task,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def serialize_metric(self, row: BibleVideoMetric) -> Dict[str, Any]:
        return {
            "id": row.id,
            "user_id": row.user_id,
            "series_id": row.series_id,
            "episode_id": row.episode_id,
            "job_id": row.job_id,
            "platform": row.platform,
            "video_id": row.video_id,
            "view_count": int(row.view_count or 0),
            "ctr": float(row.ctr or 0),
            "retention": float(row.retention or 0),
            "subscribers_gained": int(row.subscribers_gained or 0),
            "likes": int(row.likes or 0),
            "comments": int(row.comments or 0),
            "extra": self._json_loads(row.extra_json, {}),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def build_dashboard(self, db: Session, user_id: Optional[int]) -> Dict[str, Any]:
        config = self.get_or_create_config(db, user_id)
        series_q = db.query(BibleVideoSeries).filter(BibleVideoSeries.user_id == user_id)
        episodes_q = db.query(BibleVideoEpisode).filter(BibleVideoEpisode.user_id == user_id)
        jobs_q = db.query(BibleVideoJob).filter(BibleVideoJob.user_id == user_id)
        metrics_q = db.query(BibleVideoMetric).filter(BibleVideoMetric.user_id == user_id)

        total_series = series_q.count()
        total_episodes = episodes_q.count()
        total_long_videos = jobs_q.filter(BibleVideoJob.job_type == "episode").count()
        total_shorts = jobs_q.filter(BibleVideoJob.job_type == "short").count()
        avg_cost = jobs_q.with_entities(func.avg(BibleVideoJob.estimated_cost)).scalar() or 0.0
        monthly_cost = jobs_q.with_entities(func.sum(BibleVideoJob.actual_cost)).scalar() or 0.0
        queued = jobs_q.filter(BibleVideoJob.status.in_(["queued", "processing"])).count()
        waiting_approval = jobs_q.filter(BibleVideoJob.approval_status == "pending").count()
        published = jobs_q.filter(BibleVideoJob.status == "published").count()
        scheduled = (
            jobs_q.filter(BibleVideoJob.scheduled_for.isnot(None))
            .order_by(BibleVideoJob.scheduled_for.asc())
            .limit(8)
            .all()
        )

        best_series = (
            db.query(BibleVideoSeries.name, func.sum(BibleVideoMetric.view_count).label("views"))
            .join(BibleVideoMetric, BibleVideoMetric.series_id == BibleVideoSeries.id)
            .filter(BibleVideoSeries.user_id == user_id)
            .group_by(BibleVideoSeries.name)
            .order_by(func.sum(BibleVideoMetric.view_count).desc())
            .first()
        )
        best_theme = (
            db.query(BibleVideoSeries.narrative_tone, func.avg(BibleVideoMetric.retention).label("retention"))
            .join(BibleVideoMetric, BibleVideoMetric.series_id == BibleVideoSeries.id)
            .filter(BibleVideoSeries.user_id == user_id)
            .group_by(BibleVideoSeries.narrative_tone)
            .order_by(func.avg(BibleVideoMetric.retention).desc())
            .first()
        )

        scene_estimate = 10
        duration_estimate = 5
        example_cost = self._estimate_costs(config, duration_estimate, scene_estimate, shorts_count=3)
        queue_status = {
            "queued": queued,
            "waiting_approval": waiting_approval,
            "published": published,
        }
        return {
            "totals": {
                "series": total_series,
                "episodes": total_episodes,
                "long_videos": total_long_videos,
                "shorts": total_shorts,
                "estimated_cost_per_video": round(example_cost["total"], 4),
                "average_estimated_cost": round(float(avg_cost or 0.0), 4),
                "monthly_actual_cost": round(float(monthly_cost or 0.0), 4),
                "videos_waiting_approval": waiting_approval,
                "videos_published": published,
            },
            "queue_status": queue_status,
            "next_scheduled": [self.serialize_job(row) for row in scheduled],
            "best_series_by_views": {
                "name": best_series[0] if best_series else None,
                "views": int(best_series[1] or 0) if best_series else 0,
            },
            "best_theme_by_retention": {
                "theme": best_theme[0] if best_theme else None,
                "retention": round(float(best_theme[1] or 0), 2) if best_theme else 0.0,
            },
            "cost_breakdown_example": example_cost,
            "metrics_count": metrics_q.count(),
        }

    def split_series_into_episodes(self, db: Session, user_id: Optional[int], series: BibleVideoSeries, replace_existing: bool = True) -> List[BibleVideoEpisode]:
        target_count = max(1, int(series.planned_episodes or 1))
        fallback_chunks = self._split_text_chunks(series.bible_story_text or series.series_summary or series.name, target_count)
        fallback = {
            "episodes": [
                {
                    "episode_number": idx + 1,
                    "title": self._title_from_chunk(series, idx + 1, chunk),
                    "summary": chunk[:400],
                    "biblical_basis": series.bible_book or "",
                    "opening_hook": f"O que ninguem percebeu no inicio desta historia de {series.main_character or series.name}?",
                    "development": chunk[:700],
                    "tension_moment": chunk[:240],
                    "impact_phrase": self._normalize_text(chunk[:120] or series.name),
                    "ending_hook": f"No proximo episodio, o destino de {series.main_character or series.name} muda de forma inesperada.",
                    "short_suggestion": chunk[:160],
                    "thumbnail_suggestion": self._title_from_chunk(series, idx + 1, chunk).upper()[:60],
                    "youtube_title_suggestion": self._title_from_chunk(series, idx + 1, chunk),
                }
                for idx, chunk in enumerate(fallback_chunks)
            ]
        }
        prompt = (
            "Divida a historia biblica a seguir em episodios com forte retencao para YouTube.\n"
            "Responda APENAS em JSON com a chave episodes.\n"
            "Cada item deve conter: episode_number, title, summary, biblical_basis, opening_hook, "
            "development, tension_moment, impact_phrase, ending_hook, short_suggestion, thumbnail_suggestion, youtube_title_suggestion.\n\n"
            f"Serie: {series.name}\n"
            f"Livro biblico base: {series.bible_book or ''}\n"
            f"Personagem principal: {series.main_character or ''}\n"
            f"Publico: {series.target_audience or ''}\n"
            f"Tom: {series.narrative_tone or ''}\n"
            f"Quantidade de episodios: {target_count}\n\n"
            f"HISTORIA BASE:\n{series.bible_story_text or series.series_summary or series.name}"
        )
        data = self._generate_json(
            prompt,
            system_prompt="Voce e um roteirista biblico. Dramatize sem distorcer os fatos centrais da Biblia.",
            fallback=fallback,
        )
        episodes_data = data.get("episodes") if isinstance(data, dict) else None
        if not isinstance(episodes_data, list) or not episodes_data:
            episodes_data = fallback["episodes"]

        if replace_existing:
            db.query(BibleVideoScene).filter(BibleVideoScene.series_id == series.id).delete()
            db.query(BibleVideoScript).filter(BibleVideoScript.series_id == series.id).delete()
            db.query(BibleVideoEpisode).filter(BibleVideoEpisode.series_id == series.id).delete()
            db.commit()

        created = []
        for idx, item in enumerate(episodes_data[:target_count]):
            ep = BibleVideoEpisode(
                series_id=series.id,
                user_id=user_id,
                episode_number=self._normalize_int(item.get("episode_number") or idx + 1, idx + 1),
                title=self._normalize_text(item.get("title") or f"{series.name} - Episodio {idx + 1}")[:150],
                summary=self._normalize_text(item.get("summary")),
                biblical_basis=self._normalize_text(item.get("biblical_basis") or series.bible_book or ""),
                opening_hook=self._normalize_text(item.get("opening_hook")),
                development_text=self._normalize_text(item.get("development")),
                tension_moment=self._normalize_text(item.get("tension_moment")),
                impact_phrase=self._normalize_text(item.get("impact_phrase")),
                ending_hook=self._normalize_text(item.get("ending_hook")),
                short_suggestion=self._normalize_text(item.get("short_suggestion")),
                thumbnail_suggestion=self._normalize_text(item.get("thumbnail_suggestion")),
                youtube_title_suggestion=self._normalize_text(item.get("youtube_title_suggestion")),
                estimated_minutes=self._normalize_int(series.episode_duration_minutes or 5, 5),
                status="script_generated",
                approval_status="pending",
            )
            db.add(ep)
            created.append(ep)
        series.status = "in_production"
        db.commit()
        for ep in created:
            db.refresh(ep)
        return created

    def generate_script_for_episode(
        self,
        db: Session,
        user_id: Optional[int],
        episode: BibleVideoEpisode,
        desired_duration_minutes: int,
        narrative_style: str,
        drama_level: int,
        biblical_fidelity_level: int,
        target_audience: str,
        subscribe_cta: str,
        next_episode_cta: str,
    ) -> BibleVideoScript:
        series = db.query(BibleVideoSeries).filter(BibleVideoSeries.id == episode.series_id).first()
        profile_config = self.resolve_series_profile(series)
        config = self.get_or_create_config(db, user_id)
        characters = db.query(BibleVideoCharacter).filter(BibleVideoCharacter.series_id == episode.series_id).all()
        scenarios = db.query(BibleVideoScenario).filter(BibleVideoScenario.series_id == episode.series_id).all()
        desired_duration_minutes = self._normalize_int(desired_duration_minutes, int(episode.estimated_minutes or 5))
        if profile_config.get("narrative_tone") == "cinematografico":
            narrative_style = "documentario cinematografico"
        else:
            narrative_style = self._normalize_text(narrative_style or (series.narrative_tone if series else "") or "emocionante")
        drama_level = self._normalize_int(drama_level, 7)
        if profile_config.get("emotional_curve") == "crescente":
            drama_level = max(drama_level, 8)
        biblical_fidelity_level = self._normalize_int(biblical_fidelity_level, 9)
        target_audience = self._normalize_text(target_audience or episode.title)
        subscribe_cta = self._normalize_text(subscribe_cta or config.default_cta or "")
        next_episode_cta = self._normalize_text(next_episode_cta or config.default_next_episode_cta or "")
        if profile_config.get("auto_next_episode_cta") and not next_episode_cta:
            next_episode_cta = "No proximo episodio, a tensao aumenta e a historia continua com uma revelacao decisiva."
        target_scene_count = 12
        min_scene_count = 10
        max_scene_count = 15
        minimum_words = 700 if desired_duration_minutes >= 5 else max(300, desired_duration_minutes * 140)
        maximum_words = 1200 if desired_duration_minutes >= 5 else max(450, desired_duration_minutes * 220)
        character_profiles = [self._character_profile(row) for row in characters]
        scenario_profiles = [
            {
                "name": row.name,
                "description": row.description,
                "master_prompt": row.base_prompt,
                "visual_style": row.visual_style,
                "reference_image_url": row.reference_image_url,
            }
            for row in scenarios
        ]

        print(
            "[BibleVideoFactoryService] generate_script_for_episode entrada:",
            {
                "series_id": episode.series_id,
                "episode_id": episode.id,
                "selected_series": series.name if series else None,
                "selected_episode": episode.title,
                "duration": desired_duration_minutes,
                "tone": narrative_style,
                "drama": drama_level,
                "biblical_fidelity": biblical_fidelity_level,
                "target_audience": target_audience,
                "call_to_action": subscribe_cta,
                "next_episode_hook": next_episode_cta,
            },
        )
        prompt = (
            "Crie um roteiro biblico cinematografico em JSON para video episodico profissional do YouTube.\n"
            "Retorne as chaves: episode_title, opening_hook, introduction, development, climax, impact_phrase, cliffhanger, cta_subscribe, cta_next_episode, full_narration, scenes, optional_dialogues, voice_emotion_notes, soundtrack_notes, sound_effects_notes, retention_hooks, retention_analysis, youtube_growth.\n"
            "A narrativa pode dramatizar, mas nao pode alterar os fatos principais da Biblia.\n"
            "Regras obrigatorias:\n"
            f"- gerar roteiro para video de {desired_duration_minutes} minutos;\n"
            f"- escrever no minimo {minimum_words} palavras na narracao completa;\n"
            f"- escrever no maximo {maximum_words} palavras na narracao completa;\n"
            f"- dividir em no minimo {min_scene_count} e no maximo {max_scene_count} cenas;\n"
            "- criar gancho forte nos primeiros 15 segundos;\n"
            "- criar emocao crescente, suspense e linguagem simples;\n"
            "- manter estilo de documentario cinematografico;\n"
            "- nao resumir a historia e nao correr pelos fatos;\n"
            "- mostrar emocoes dos personagens;\n"
            "- descrever ambiente, sons e clima da cena;\n"
            "- incluir frase de impacto memoravel;\n"
            "- terminar com cliffhanger forte para o proximo episodio;\n"
            "- pensar em retencao para YouTube;\n"
            "- preservar fidelidade biblica.\n"
            f"- perfil especial ativo: {'Serie Netflix Biblica' if profile_config else 'padrao'}.\n"
            f"- cliffhanger obrigatorio: {'sim' if profile_config.get('cliffhanger_required') else 'nao'}.\n"
            f"- peso do cliffhanger neste perfil: {self._normalize_text(profile_config.get('cliffhanger_prompt_weight') or 'alto')}.\n"
            f"- nota minima de retencao desejada: {self._normalize_int(profile_config.get('minimum_retention_score'), 0) or 'livre'}.\n"
            "- no modo Serie Netflix Biblica, o ultimo bloco do episodio e a parte mais importante do roteiro.\n"
            "- o ultimo bloco deve conter obrigatoriamente: uma pergunta sem resposta, uma promessa forte para o proximo episodio, uma revelacao incompleta e um risco ou consequencia futura.\n"
            "- esse ultimo bloco nao pode terminar apenas encerrando a cena nem com frase vaga.\n"
            "- prefira duas perguntas curtas seguidas de promessa, revelacao e risco futuro claros.\n"
            "- gerar exatamente 3 shorts automaticos por episodio: gancho, momento emocional e cliffhanger.\n"
            "Cada item de scenes deve conter: scene_number, title, duration, emotion, visual_description, camera_direction, narration, sound_effects, music_style, prompt_image, prompt_video, prompt_animation, prompt_cinematic.\n"
            "retention_analysis deve conter: overall_score, hook_strength, conflict_score, emotion_score, dramatic_progression_score, revelation_score, cliffhanger_score, retention_score, viralization_score, notes.\n"
            "youtube_growth deve conter: title_main, alternate_titles, description, hashtags, seo_keywords, tags, thumbnail_prompt.\n\n"
            f"Serie: {series.name if series else ''}\n"
            f"Episodio: {episode.title}\n"
            f"Resumo: {episode.summary or ''}\n"
            f"Base biblica: {episode.biblical_basis or ''}\n"
            f"Gancho inicial: {episode.opening_hook or ''}\n"
            f"Momento de tensao: {episode.tension_moment or ''}\n"
            f"Frase de impacto: {episode.impact_phrase or ''}\n"
            f"Gancho final: {episode.ending_hook or ''}\n"
            f"Duracao desejada: {desired_duration_minutes} minutos\n"
            f"Estilo narrativo: {narrative_style}\n"
            f"Nivel de drama: {drama_level}/10\n"
            f"Nivel de fidelidade biblica: {biblical_fidelity_level}/10\n"
            f"Publico-alvo: {target_audience}\n"
            f"CTA inscricao: {subscribe_cta}\n"
            f"CTA proximo episodio: {next_episode_cta}\n"
            f"Estilo visual: {series.visual_style if series else ''}\n"
            f"Tom: {series.narrative_tone if series else ''}\n"
            f"Perfil de producao: {self._json_dumps(profile_config)[:1500]}\n"
            f"Personagens cadastrados: {self._json_dumps(character_profiles)[:5000]}\n"
            f"Cenarios cadastrados: {self._json_dumps(scenario_profiles)[:4000]}\n"
        )
        fallback_scenes = []
        base_text = self._normalize_text(
            "\n\n".join(
                self._normalize_text(x)
                for x in [episode.opening_hook, episode.summary, episode.development_text, episode.tension_moment, episode.ending_hook]
                if self._normalize_text(x)
            )
        )
        default_sections = {
            "episode_title": self._normalize_text(episode.title),
            "opening_hook": self._normalize_text(episode.opening_hook or f"Nos primeiros segundos, tudo aponta para um destino perigoso: {series.main_character if series and series.main_character else episode.title} esta prestes a viver um momento que muda a historia."),
            "introduction": self._normalize_text(episode.summary or episode.development_text or base_text),
            "development": self._normalize_text(episode.development_text or episode.summary or base_text),
            "climax": self._normalize_text(episode.tension_moment or episode.summary or "A tensao cresce e a decisao final se aproxima."),
            "impact_phrase": self._normalize_text(episode.impact_phrase or "Quando Deus chama alguem, o improvavel deixa de ser impossivel."),
            "cliffhanger": self._normalize_text(
                self._ensure_netflix_cliffhanger(
                    episode.ending_hook or "Mas o proximo passo desta historia vai abalar tudo o que parecia certo e deixar todos em suspenso.",
                    episode,
                    series,
                    next_episode_cta,
                )
                if profile_config.get("cliffhanger_required")
                else (episode.ending_hook or "Mas o proximo passo desta historia vai abalar tudo o que parecia certo e deixar todos em suspenso.")
            ),
            "cta_subscribe": subscribe_cta,
            "cta_next_episode": next_episode_cta,
        }
        paragraphs = self._split_text_chunks(base_text, target_scene_count)
        for idx, item in enumerate(paragraphs):
            duration = self._scene_duration(desired_duration_minutes, len(paragraphs))
            fallback_scenes.append(
                {
                    "scene_number": idx + 1,
                    "title": f"Cena {idx + 1}",
                    "description": item[:220],
                    "visual_description": item[:220],
                    "narration": item,
                    "emotion": "suspense" if idx in {0, len(paragraphs) - 1} else "emocionante",
                    "duration": duration,
                    "text": item,
                    "camera_direction": ["aproximacao lenta", "travelling lateral", "panoramica ampla", "zoom dramatico"][idx % 4],
                    "sound_effects": ["vento leve", "passos na terra", "silencio tenso", "multidao ao fundo"][idx % 4],
                    "music_style": "trilha biblica cinematografica crescente",
                    "image_prompt": f"{series.visual_style or 'anime cinematografico'} de {episode.title}. {item[:180]}",
                    "prompt_image": f"{series.visual_style or 'cinematic biblical anime'}, {item[:180]}, dramatic lighting, ultra detailed, emotional atmosphere, 4k",
                    "prompt_video": f"{series.visual_style or 'cinematic biblical anime'}, {item[:180]}, camera movement {['slow push in', 'travelling shot', 'epic wide shot', 'dramatic close-up'][idx % 4]}, cinematic motion, 4k",
                    "prompt_animation": f"Animate the biblical scene with subtle cloth movement, wind, depth and cinematic emotion: {item[:180]}",
                    "prompt_cinematic": f"{series.visual_style or 'cinematic biblical anime'} scene of {episode.title}, golden hour, dramatic shadows, volumetric light, emotional realism, high retention YouTube frame",
                    "caption": item[:120],
                }
            )
        fallback_growth = {
            "title_main": episode.youtube_title_suggestion or episode.title,
            "alternate_titles": [
                episode.youtube_title_suggestion or episode.title,
                f"{episode.title}: o momento que mudou tudo",
                f"O segredo por tras de {series.main_character if series and series.main_character else episode.title}",
            ],
            "description": self._normalize_text(f"{episode.summary or base_text[:280]}\n\n{subscribe_cta}\n{next_episode_cta}"),
            "hashtags": ["#biblia", "#serieNetflixBiblica", "#historiabiblica", "#animebiblico", "#shortsbiblicos"],
            "seo_keywords": [
                self._normalize_text(episode.title),
                self._normalize_text(series.main_character if series else ""),
                "historia biblica",
                "serie biblica cinematografica",
                "youtube biblico",
            ],
            "tags": ["biblia", "historia biblica", "youtube biblico", "anime biblico", self._normalize_text(series.main_character if series else "")],
            "thumbnail_prompt": self._normalize_text(
                f"{series.visual_style if series else 'anime cinematografico'}, {series.main_character if series and series.main_character else episode.title}, close-up emocional, contraste alto, luz dramatica, frame de CTR maximo, 16:9"
            ),
        }
        fallback = {
            **default_sections,
            "full_narration": base_text,
            "scenes": fallback_scenes,
            "optional_dialogues": [],
            "voice_emotion_notes": {
                "voice": f"Narracao {narrative_style} com reverencia biblica",
                "emotion": "emocao crescente com suspense moderado",
                "scene": 1,
                "description": f"Drama {drama_level}/10, voz clara, intensa e cinematografica.",
            },
            "soundtrack_notes": {
                "soundtrack": f"Trilha {series.narrative_tone if series else 'epica'} com suspense moderado e atmosfera cinematografica.",
                "emotion": "emocao crescente",
                "description": "Cordas suaves, percussao discreta e ambiencia biblica reverente.",
            },
            "sound_effects_notes": "Vento, passos, ambiente historico, multidao e silencio dramatico quando necessario.",
            "retention_hooks": [
                episode.opening_hook or "Algo decisivo vai acontecer antes que alguem perceba.",
                episode.impact_phrase or "A fe transforma o improvavel em destino.",
                episode.ending_hook or "E o que vem depois vai surpreender a todos.",
            ],
            "retention_analysis": self._retention_analysis_fallback(default_sections, self._word_count(base_text), len(fallback_scenes), drama_level),
            "youtube_growth": fallback_growth,
        }
        min_retention = self._normalize_int(profile_config.get("minimum_retention_score"), 0)

        def prepare_generated_payload(data_obj: Any, attempt_number: int, auto_regenerated: bool) -> Dict[str, Any]:
            payload = data_obj if isinstance(data_obj, dict) else {}
            raw_sections = {
                "episode_title": self._normalize_text(payload.get("episode_title") or default_sections["episode_title"]),
                "opening_hook": self._normalize_text(payload.get("opening_hook") or default_sections["opening_hook"]),
                "introduction": self._normalize_text(payload.get("introduction") or default_sections["introduction"]),
                "development": self._normalize_text(payload.get("development") or default_sections["development"]),
                "climax": self._normalize_text(payload.get("climax") or default_sections["climax"]),
                "impact_phrase": self._normalize_text(payload.get("impact_phrase") or default_sections["impact_phrase"]),
                "cliffhanger": self._normalize_text(payload.get("cliffhanger") or default_sections["cliffhanger"]),
                "cta_subscribe": self._normalize_text(payload.get("cta_subscribe") or subscribe_cta),
                "cta_next_episode": self._normalize_text(payload.get("cta_next_episode") or next_episode_cta),
            }
            narration_value = self._normalize_text(payload.get("full_narration") or self._build_text_from_sections(raw_sections) or fallback["full_narration"])
            narration_value = self._ensure_word_target(narration_value, minimum_words, maximum_words, raw_sections, episode, series)
            script_sections_value = self._detect_cinematic_sections(narration_value, raw_sections)
            if profile_config.get("cliffhanger_required"):
                script_sections_value["cliffhanger"] = self._ensure_netflix_cliffhanger(
                    script_sections_value.get("cliffhanger"),
                    episode,
                    series,
                    script_sections_value.get("cta_next_episode") or next_episode_cta,
                )
                narration_value = self._ensure_word_target(
                    self._normalize_text(self._build_text_from_sections(script_sections_value)),
                    minimum_words,
                    maximum_words,
                    script_sections_value,
                    episode,
                    series,
                )
            scenes_payload = payload.get("scenes") or fallback["scenes"]
            normalized_scenes_value = []
            if isinstance(scenes_payload, list):
                for idx, item in enumerate(scenes_payload[:max_scene_count]):
                    if not isinstance(item, dict):
                        item = {"narration": self._normalize_text(item)}
                    narration = self._normalize_text(item.get("narration") or item.get("text") or item.get("narration_text"))
                    description = self._normalize_text(item.get("description") or item.get("visual_description") or item.get("caption") or narration[:220])
                    emotion = self._normalize_text(item.get("emotion") or ("suspense" if idx in {0, len(scenes_payload) - 1} else "emocionante"))
                    duration = self._normalize_int(item.get("duration") or item.get("duration_seconds"), self._scene_duration(desired_duration_minutes, min(max_scene_count, len(scenes_payload) or target_scene_count)))
                    normalized_scenes_value.append(
                        {
                            "scene_number": self._normalize_int(item.get("scene_number") or idx + 1, idx + 1),
                            "title": self._normalize_text(item.get("title") or f"Cena {idx + 1}"),
                            "description": description,
                            "visual_description": description,
                            "narration": narration,
                            "emotion": emotion,
                            "duration": duration,
                            "text": narration,
                            "camera_direction": self._normalize_text(item.get("camera_direction") or item.get("camera_type") or ["aproximacao lenta", "travelling lateral", "panoramica ampla", "zoom dramatico"][idx % 4]),
                            "sound_effects": self._normalize_text(item.get("sound_effects") or ["vento leve", "passos na terra", "silencio tenso", "multidao ao fundo"][idx % 4]),
                            "music_style": self._normalize_text(item.get("music_style") or "trilha biblica cinematografica crescente"),
                            "image_prompt": self._normalize_text(item.get("image_prompt") or item.get("prompt_image") or f"{series.visual_style or 'anime cinematografico'} de {episode.title}. {description[:180]}"),
                            "prompt_image": self._normalize_text(item.get("prompt_image") or item.get("image_prompt") or f"{series.visual_style or 'cinematic biblical anime'}, {description[:180]}, dramatic lighting, ultra detailed, emotional atmosphere, 4k"),
                            "prompt_video": self._normalize_text(item.get("prompt_video") or f"{series.visual_style or 'cinematic biblical anime'}, {description[:180]}, cinematic motion, 4k"),
                            "prompt_animation": self._normalize_text(item.get("prompt_animation") or f"Animate the biblical scene with subtle cloth movement, wind and cinematic emotion: {description[:180]}"),
                            "prompt_cinematic": self._normalize_text(item.get("prompt_cinematic") or f"{series.visual_style or 'cinematic biblical anime'} scene, dramatic lighting, emotional atmosphere, high retention frame"),
                            "caption": self._normalize_text(item.get("caption") or description[:120]),
                        }
                    )
            if not normalized_scenes_value:
                normalized_scenes_value = list(fallback["scenes"])
            if len(normalized_scenes_value) < min_scene_count:
                for extra_idx in range(len(normalized_scenes_value), min_scene_count):
                    base_scene = fallback["scenes"][extra_idx % len(fallback["scenes"])]
                    normalized_scenes_value.append(
                        {
                            **base_scene,
                            "scene_number": extra_idx + 1,
                            "title": self._normalize_text(base_scene.get("title") or f"Cena {extra_idx + 1}"),
                        }
                    )
            normalized_scenes_value = normalized_scenes_value[:max_scene_count]
            if normalized_scenes_value:
                normalized_scenes_value[0] = {
                    **normalized_scenes_value[0],
                    "title": self._normalize_text(normalized_scenes_value[0].get("title") or "Gancho Inicial"),
                    "narration": self._normalize_text(normalized_scenes_value[0].get("narration") or script_sections_value.get("opening_hook")),
                    "text": self._normalize_text(normalized_scenes_value[0].get("text") or normalized_scenes_value[0].get("narration") or script_sections_value.get("opening_hook")),
                    "emotion": self._normalize_text(normalized_scenes_value[0].get("emotion") or "suspense"),
                }
                normalized_scenes_value[-1] = {
                    **normalized_scenes_value[-1],
                    "emotion": self._normalize_text(normalized_scenes_value[-1].get("emotion") or "cliffhanger"),
                    "prompt_video": self._normalize_text(normalized_scenes_value[-1].get("prompt_video") or f"{normalized_scenes_value[-1].get('prompt_image') or normalized_scenes_value[-1].get('image_prompt') or normalized_scenes_value[-1].get('description')}, cinematic suspense, cliffhanger ending, 4k"),
                }
            youtube_growth_value = payload.get("youtube_growth") if isinstance(payload.get("youtube_growth"), dict) else {}
            youtube_growth_value = {
                "title_main": self._normalize_text(youtube_growth_value.get("title_main") or fallback_growth["title_main"]),
                "alternate_titles": [self._normalize_text(x) for x in self._ensure_list(youtube_growth_value.get("alternate_titles") or fallback_growth["alternate_titles"]) if self._normalize_text(x)][:3],
                "description": self._normalize_text(youtube_growth_value.get("description") or fallback_growth["description"]),
                "hashtags": [
                    self._normalize_text(x if str(x).startswith("#") else f"#{self._normalize_text(x).replace(' ', '')}")
                    for x in self._ensure_list(youtube_growth_value.get("hashtags") or fallback_growth["hashtags"])
                    if self._normalize_text(x)
                ][:10],
                "seo_keywords": [self._normalize_text(x) for x in self._ensure_list(youtube_growth_value.get("seo_keywords") or fallback_growth["seo_keywords"]) if self._normalize_text(x)][:15],
                "tags": [self._normalize_text(x) for x in self._ensure_list(youtube_growth_value.get("tags") or fallback_growth["tags"]) if self._normalize_text(x)][:15],
                "thumbnail_prompt": self._normalize_text(youtube_growth_value.get("thumbnail_prompt") or fallback_growth["thumbnail_prompt"]),
            }
            retention_analysis_value = self._calculate_retention_analysis(
                script_sections_value,
                narration_value,
                normalized_scenes_value,
                desired_duration_minutes=desired_duration_minutes,
                drama_level=drama_level,
                minimum_required_score=min_retention,
                profile_config=profile_config,
            )
            model_analysis = payload.get("retention_analysis") if isinstance(payload.get("retention_analysis"), dict) else {}
            model_notes = self._normalize_text(model_analysis.get("notes"))
            if model_notes:
                retention_analysis_value["observations"].append(f"Leitura da IA: {model_notes}")
            retention_analysis_value["notes"] = " ".join(retention_analysis_value.get("observations", [])[:3]).strip() or retention_analysis_value["notes"]
            retention_analysis_value["regeneration_attempts"] = attempt_number
            retention_analysis_value["auto_regenerated"] = auto_regenerated
            return {
                "script_sections": script_sections_value,
                "narration_source": narration_value,
                "normalized_scenes": normalized_scenes_value,
                "retention_analysis": retention_analysis_value,
                "youtube_growth": youtube_growth_value,
                "optional_dialogues": payload.get("optional_dialogues") if isinstance(payload.get("optional_dialogues"), list) else [],
                "voice_emotion_notes": payload.get("voice_emotion_notes") or fallback["voice_emotion_notes"],
                "soundtrack_notes": payload.get("soundtrack_notes") or fallback["soundtrack_notes"],
                "sound_effects_notes": self._normalize_text(payload.get("sound_effects_notes") or fallback["sound_effects_notes"]),
                "retention_hooks": payload.get("retention_hooks") or fallback["retention_hooks"],
            }

        max_attempts = 3 if min_retention else 1
        selected_payload = None
        for attempt in range(1, max_attempts + 1):
            current_prompt = prompt
            if attempt > 1 and selected_payload:
                weak_points = "; ".join(selected_payload["retention_analysis"].get("suggestions") or ["Reforce gancho, conflito, emocao, progressao dramatica e cliffhanger final."])
                current_prompt = (
                    f"{prompt}\n\n"
                    "REGENERACAO AUTOMATICA OBRIGATORIA.\n"
                    f"A versao anterior ficou com retencao {selected_payload['retention_analysis'].get('overall_score', 0)}/100.\n"
                    f"A meta minima deste perfil e {min_retention}/100.\n"
                    f"Pontos fracos detectados: {weak_points}\n"
                    "Reescreva o roteiro com gancho mais agressivo nos primeiros 15 segundos, conflito mais claro, emocao crescente, progressao dramatica perceptivel e cliffhanger cinematografico no final.\n"
                    "No perfil Serie Netflix Biblica, o cliffhanger final e o bloco mais importante do episodio.\n"
                    "Ele precisa obrigatoriamente conter pergunta sem resposta, promessa forte para o proximo episodio, revelacao incompleta e risco ou consequencia futura.\n"
                    "Nao aceite um final vago como 'ha algo especial' ou frases que apenas encerram a cena.\n"
                    "Nao explique o que mudou. Apenas retorne o JSON final completo.\n"
                )
            generated_data = self._generate_json(
                current_prompt,
                system_prompt="Voce e um roteirista biblico cinematografico para series em episodios.",
                fallback=fallback,
            )
            candidate_payload = prepare_generated_payload(generated_data, attempt, auto_regenerated=attempt > 1)
            if selected_payload is None or candidate_payload["retention_analysis"]["overall_score"] >= selected_payload["retention_analysis"]["overall_score"]:
                selected_payload = candidate_payload
            if not min_retention or candidate_payload["retention_analysis"]["overall_score"] >= min_retention:
                selected_payload = candidate_payload
                break

        if min_retention and selected_payload and selected_payload["retention_analysis"]["overall_score"] < min_retention:
            strengthened_sections = self._strengthen_sections_for_retention(
                selected_payload["script_sections"],
                episode,
                series,
                subscribe_cta,
                next_episode_cta,
                profile_config=profile_config,
            )
            strengthened_narration = self._ensure_word_target(
                self._normalize_text(f"{self._build_text_from_sections(strengthened_sections)}\n\n{selected_payload['narration_source']}"),
                minimum_words,
                maximum_words,
                strengthened_sections,
                episode,
                series,
            )
            strengthened_scenes = list(selected_payload["normalized_scenes"])
            if strengthened_scenes:
                strengthened_scenes[0] = {
                    **strengthened_scenes[0],
                    "title": self._normalize_text(strengthened_scenes[0].get("title") or "Gancho Inicial"),
                    "narration": strengthened_sections["opening_hook"],
                    "text": strengthened_sections["opening_hook"],
                    "emotion": "suspense",
                }
                strengthened_scenes[-1] = {
                    **strengthened_scenes[-1],
                    "narration": self._normalize_text(strengthened_scenes[-1].get("narration") or strengthened_sections["cliffhanger"]),
                    "text": self._normalize_text(strengthened_scenes[-1].get("text") or strengthened_sections["cliffhanger"]),
                    "emotion": "cliffhanger",
                    "prompt_video": self._normalize_text(strengthened_scenes[-1].get("prompt_video") or f"{strengthened_scenes[-1].get('prompt_image') or strengthened_scenes[-1].get('image_prompt')}, suspense ending, unresolved revelation, cinematic motion, 4k"),
                }
            strengthened_analysis = self._calculate_retention_analysis(
                strengthened_sections,
                strengthened_narration,
                strengthened_scenes,
                desired_duration_minutes=desired_duration_minutes,
                drama_level=drama_level,
                minimum_required_score=min_retention,
                profile_config=profile_config,
            )
            strengthened_analysis["regeneration_attempts"] = max_attempts
            strengthened_analysis["auto_regenerated"] = True
            strengthened_analysis["auto_boosted"] = True
            strengthened_analysis["notes"] = " ".join(strengthened_analysis.get("observations", [])[:3]).strip() or strengthened_analysis["notes"]
            selected_payload = {
                **selected_payload,
                "script_sections": strengthened_sections,
                "narration_source": strengthened_narration,
                "normalized_scenes": strengthened_scenes,
                "retention_analysis": strengthened_analysis,
            }

        script_sections = selected_payload["script_sections"]
        narration_source = selected_payload["narration_source"]
        normalized_scenes = selected_payload["normalized_scenes"]
        retention_analysis = selected_payload["retention_analysis"]
        youtube_growth = selected_payload["youtube_growth"]
        storyboard = [self._scene_source_to_storyboard_frame(scene, idx) for idx, scene in enumerate(normalized_scenes)]
        script = BibleVideoScript(
            series_id=episode.series_id,
            episode_id=episode.id,
            user_id=user_id,
            desired_duration_minutes=self._normalize_int(desired_duration_minutes or episode.estimated_minutes or 5, 5),
            narrative_style=narrative_style,
            drama_level=self._normalize_int(drama_level, 7),
            biblical_fidelity_level=self._normalize_int(biblical_fidelity_level, 9),
            target_audience=target_audience,
            subscribe_cta=subscribe_cta,
            next_episode_cta=next_episode_cta,
            full_narration=narration_source,
            scenes_json=self._json_dumps(normalized_scenes),
            voice_emotion_notes=self._json_dumps(selected_payload["voice_emotion_notes"]),
            soundtrack_notes=self._json_dumps(selected_payload["soundtrack_notes"]),
            sound_effects_notes=selected_payload["sound_effects_notes"],
            retention_hooks_json=self._json_dumps(selected_payload["retention_hooks"]),
            validation_status="pending",
        )
        self._save_script_package(
            script,
            {
                "optional_dialogues": selected_payload["optional_dialogues"],
                "script_sections": script_sections,
                "retention_analysis": retention_analysis,
                "youtube_growth": youtube_growth,
                "storyboard": storyboard,
                "production_blueprint": {
                    "word_count": self._word_count(narration_source),
                    "scene_count": len(normalized_scenes),
                    "duration_minutes": desired_duration_minutes,
                    "target_scene_range": f"{min_scene_count}-{max_scene_count}",
                    "pipeline_status": "roteiro_pronto",
                },
            },
        )
        db.add(script)
        episode.status = "script_generated"
        episode.title = script_sections["episode_title"] or episode.title
        episode.youtube_title_suggestion = youtube_growth["title_main"] or episode.youtube_title_suggestion
        episode.impact_phrase = script_sections["impact_phrase"] or episode.impact_phrase
        if profile_config.get("cliffhanger_required"):
            episode.ending_hook = script_sections["cliffhanger"] or default_sections["cliffhanger"]
        else:
            episode.ending_hook = script_sections["cliffhanger"] or episode.ending_hook
        db.commit()
        db.refresh(script)
        return script

    def validate_script(self, db: Session, script: BibleVideoScript, episode: BibleVideoEpisode, series: Optional[BibleVideoSeries]) -> BibleVideoScript:
        narration = script.full_narration or episode.summary or ""
        fantasy_hits = sum(1 for token in ["dragao", "magia", "feitico", "portal", "multiverso", "superpoder"] if token in narration.lower())
        fallback_status = "approved"
        fallback_flags: List[str] = []
        if fantasy_hits >= 2:
            fallback_status = "high_risk"
            fallback_flags.append("Excesso de fantasia em relacao ao relato biblico.")
        elif fantasy_hits == 1:
            fallback_status = "needs_review"
            fallback_flags.append("Ha elemento fantasioso que pode exigir ajuste.")
        if not episode.biblical_basis:
            fallback_status = "needs_review"
            fallback_flags.append("Base biblica pouco clara.")
        fallback = {
            "status": fallback_status,
            "notes": " | ".join(fallback_flags) or "Estrutura coerente, mas exige revisao humana final.",
            "needs_disclaimer": fallback_status != "approved",
            "flags": fallback_flags,
        }
        prompt = (
            "Revise biblicamente o roteiro abaixo e retorne JSON com: status, notes, needs_disclaimer, flags.\n"
            "status deve ser approved, needs_review ou high_risk.\n"
            "Avalie personagem correto, ordem dos acontecimentos, excesso de fantasia e sensibilidade teologica.\n\n"
            f"Serie: {series.name if series else ''}\n"
            f"Livro biblico base: {series.bible_book if series else ''}\n"
            f"Episodio: {episode.title}\n"
            f"Base biblica: {episode.biblical_basis or ''}\n\n"
            f"ROTEIRO:\n{narration[:12000]}"
        )
        data = self._generate_json(
            prompt,
            system_prompt="Voce e um revisor biblico e editorial. Priorize fidelidade ao texto biblico.",
            fallback=fallback,
        )
        status_map = {
            "approved": "approved",
            "aprovado": "approved",
            "needs_review": "needs_review",
            "precisa_revisar": "needs_review",
            "precisa revisar": "needs_review",
            "high_risk": "high_risk",
            "risco_alto": "high_risk",
            "risco alto": "high_risk",
        }
        status = status_map.get(self._normalize_text(data.get("status")).lower(), fallback["status"])
        script.validation_status = status
        script.validation_notes = self._normalize_text(data.get("notes") or fallback["notes"])
        script.disclaimer_required = bool(data.get("needs_disclaimer", fallback["needs_disclaimer"]))
        script.validation_flags_json = self._json_dumps(data.get("flags") or fallback["flags"])
        episode.approval_status = "approved" if status == "approved" else "pending"
        episode.status = "script_approved" if status == "approved" else "script_generated"
        db.commit()
        db.refresh(script)
        return script

    def generate_scenes(self, db: Session, script: BibleVideoScript, episode: BibleVideoEpisode, series: Optional[BibleVideoSeries]) -> List[BibleVideoScene]:
        db.query(BibleVideoScene).filter(BibleVideoScene.script_id == script.id).delete()
        db.commit()
        source = self._json_loads(script.scenes_json, [])
        if not isinstance(source, list) or not source:
            paragraphs = self._split_text_chunks(script.full_narration, max(6, int(script.desired_duration_minutes or 5) * 2))
            source = [{"text": p, "image_prompt": p[:200], "caption": p[:120]} for p in paragraphs]
        source = self._dedupe_scene_dicts(source, renumber=True, log_label="scene_source_before_generation")
        characters = db.query(BibleVideoCharacter).filter(BibleVideoCharacter.series_id == script.series_id).all()
        scenarios = db.query(BibleVideoScenario).filter(BibleVideoScenario.series_id == script.series_id).all()
        character_profiles = [self._character_profile(row) for row in characters]
        scenario_profiles = [
            {
                "name": row.name,
                "description": row.description,
                "master_prompt": row.base_prompt,
                "visual_style": row.visual_style,
                "reference_image_url": row.reference_image_url,
            }
            for row in scenarios
        ]

        prompt = (
            "Transforme o roteiro abaixo em uma lista de cenas cinematograficas em JSON.\n"
            "Retorne a chave scenes.\n"
            "Cada cena deve conter: scene_number, title, narration_text, visual_description, characters, scenario_name, emotion, "
            "prompt_image, prompt_video, prompt_animation, prompt_cinematic, duration_seconds, camera_type, sound_effects, music_style, effects.\n"
            "Use consistencia de personagem e cenario em toda a sequencia.\n"
            "Para cada personagem recorrente, reutilize SEMPRE o mesmo prompt mestre unico do Character Bible.\n"
            "Proibido mudar a aparencia entre cenas e durante toda a temporada: rosto, idade, altura, olhos, cabelo, barba, tom de pele, roupa e acessorios devem permanecer consistentes.\n"
            "Crie prompts compativeis com Veo, Kling, Luma, Runway e Pika.\n\n"
            f"Serie: {series.name if series else ''}\n"
            f"Episodio: {episode.title}\n"
            f"Estilo visual: {series.visual_style if series else ''}\n"
            f"Tom: {series.narrative_tone if series else ''}\n"
            f"Character Bible: {self._json_dumps(character_profiles)[:5000]}\n"
            f"Scenario Bible: {self._json_dumps(scenario_profiles)[:4000]}\n"
            f"Roteiro base: {script.full_narration[:12000]}\n"
        )
        fallback = {
            "scenes": [
                {
                    "scene_number": idx + 1,
                    "title": self._normalize_text(item.get("title") or f"Cena {idx + 1}"),
                    "narration_text": item.get("text") or "",
                    "visual_description": item.get("visual_description") or item.get("caption") or item.get("text") or "",
                    "characters": [profile["name"] for profile in character_profiles[:3]] or ([series.main_character] if series and series.main_character else []),
                    "scenario_name": (scenario_profiles[idx % len(scenario_profiles)]["name"] if scenario_profiles else (series.bible_book or "Cenario biblico")),
                    "emotion": item.get("emotion") or series.narrative_tone or "emocao",
                    "prompt_image": item.get("prompt_image") or item.get("image_prompt") or item.get("text") or "",
                    "prompt_video": item.get("prompt_video") or f"{item.get('prompt_image') or item.get('image_prompt') or item.get('text') or ''}, cinematic motion, 4k",
                    "prompt_animation": item.get("prompt_animation") or f"Anime cinematografico em movimento suave: {(item.get('prompt_image') or item.get('image_prompt') or item.get('text') or '')[:220]}",
                    "prompt_cinematic": item.get("prompt_cinematic") or f"{item.get('prompt_image') or item.get('image_prompt') or item.get('text') or ''}, cinematic biblical documentary frame, emotional lighting, 4k",
                    "duration_seconds": max(5.0, round((float(script.desired_duration_minutes or 5) * 60.0) / max(1, len(source)), 2)),
                    "camera_type": item.get("camera_direction") or ["zoom", "travelling", "aproximacao", "panoramica"][idx % 4],
                    "sound_effects": item.get("sound_effects") or ["vento", "passos", "respiracao", "multidao distante"][idx % 4],
                    "music_style": item.get("music_style") or "trilha biblica cinematografica crescente",
                    "effects": [["luz", "sombra"], ["vento"], ["poeira"], ["brilho", "multidao"]][idx % 4],
                }
                for idx, item in enumerate(source)
            ]
        }
        data = self._generate_json(
            prompt,
            system_prompt="Voce e diretor de storyboard biblico em estilo anime cinematografico.",
            fallback=fallback,
        )
        scenes_data = data.get("scenes") if isinstance(data, dict) else None
        if not isinstance(scenes_data, list) or not scenes_data:
            scenes_data = fallback["scenes"]
        ai_scene_count = len(scenes_data)
        scenes_data = self._dedupe_scene_dicts(scenes_data, renumber=True, log_label="ai_generated_scenes")
        logger.info("cenas geradas %s", len(scenes_data))
        logger.info(
            "diagnostico geracao_cenas script_id=%s recebidas_da_ia=%s persistidas_planejadas=%s",
            script.id,
            ai_scene_count,
            len(scenes_data),
        )

        created = []
        for idx, item in enumerate(scenes_data):
            matched_profiles = self._find_character_profiles(item.get("characters"), character_profiles)
            prompt_image_base = self._normalize_text(item.get("prompt_image"))
            prompt_video_base = self._normalize_text(item.get("prompt_video") or f"{prompt_image_base}, cinematic motion, 4k")
            prompt_animation_base = self._normalize_text(item.get("prompt_animation") or f"Animate with cinematic emotion: {prompt_image_base[:220]}")
            prompt_cinematic_base = self._normalize_text(item.get("prompt_cinematic") or f"{prompt_image_base}, biblical cinematic documentary, emotional atmosphere")
            camera_direction = self._normalize_text(item.get("camera_type") or item.get("camera_direction"))
            sound_effects = self._normalize_text(item.get("sound_effects"))
            music_style = self._normalize_text(item.get("music_style"))
            title = self._normalize_text(item.get("title") or f"Cena {idx + 1}")
            scenario_name = self._normalize_text(item.get("scenario_name"))
            emotion = self._normalize_text(item.get("emotion"))
            prompt_image = self._compose_scene_prompt_with_character_bible(prompt_image_base, matched_profiles, scenario_name, emotion)
            prompt_video = self._compose_scene_prompt_with_character_bible(prompt_video_base, matched_profiles, scenario_name, emotion)
            prompt_animation = self._compose_scene_prompt_with_character_bible(prompt_animation_base, matched_profiles, scenario_name, emotion)
            prompt_cinematic = self._compose_scene_prompt_with_character_bible(prompt_cinematic_base, matched_profiles, scenario_name, emotion)
            provider_prompts = {
                "veo": prompt_video,
                "kling": prompt_animation,
                "luma": prompt_cinematic,
                "runway": prompt_video,
                "pika": prompt_animation,
            }
            row = BibleVideoScene(
                script_id=script.id,
                series_id=script.series_id,
                episode_id=script.episode_id,
                user_id=script.user_id,
                scene_number=self._normalize_int(item.get("scene_number") or idx + 1, idx + 1),
                narration_text=self._normalize_text(item.get("narration_text")),
                visual_description=self._normalize_text(item.get("visual_description")),
                characters_json=self._json_dumps(item.get("characters") or []),
                scenario_name=scenario_name,
                emotion=emotion,
                prompt_image=prompt_image,
                prompt_animation=prompt_animation,
                duration_seconds=float(item.get("duration_seconds") or 8.0),
                camera_type=camera_direction,
                effects_json=self._json_dumps(
                    {
                        "title": title,
                        "effects": item.get("effects") or [],
                        "camera_direction": camera_direction,
                        "sound_effects": sound_effects,
                        "music_style": music_style,
                        "prompt_video": prompt_video,
                        "prompt_cinematic": prompt_cinematic,
                        "provider_prompts": provider_prompts,
                        "character_master_prompts": [self._normalize_text(profile.get("master_prompt")) for profile in matched_profiles if self._normalize_text(profile.get("master_prompt"))],
                        "consistency_lock": True,
                        "storyboard_image": self._normalize_text(item.get("storyboard_image") or ""),
                        "approval_status": self._normalize_text(item.get("approval_status") or "pending"),
                    }
                ),
            )
            db.add(row)
            created.append(row)
        script.scenes_json = self._json_dumps(
            [
                {
                    "scene_number": c.scene_number,
                    "title": self._normalize_text((self._json_loads(c.effects_json, {}) or {}).get("title") or f"Cena {c.scene_number}"),
                    "text": c.narration_text,
                    "image_prompt": c.prompt_image,
                    "prompt_video": self._normalize_text((self._json_loads(c.effects_json, {}) or {}).get("prompt_video")),
                    "prompt_animation": c.prompt_animation,
                    "prompt_cinematic": self._normalize_text((self._json_loads(c.effects_json, {}) or {}).get("prompt_cinematic")),
                    "caption": c.visual_description,
                    "visual_description": c.visual_description,
                    "camera_direction": c.camera_type,
                    "sound_effects": self._normalize_text((self._json_loads(c.effects_json, {}) or {}).get("sound_effects")),
                    "music_style": self._normalize_text((self._json_loads(c.effects_json, {}) or {}).get("music_style")),
                    "emotion": c.emotion,
                    "duration": c.duration_seconds,
                }
                for c in created
            ]
        )
        self._save_script_package(
            script,
            {
                "storyboard": [
                    self._normalize_storyboard_frame(
                        {
                            "scene_number": row.scene_number,
                            "title": self._normalize_text((self._json_loads(row.effects_json, {}) or {}).get("title") or f"Cena {row.scene_number}"),
                            "emotion": row.emotion,
                            "prompt_visual": row.prompt_image,
                            "prompt_image": row.prompt_image,
                            "prompt_video": self._normalize_text((self._json_loads(row.effects_json, {}) or {}).get("prompt_video")),
                            "camera_movement": row.camera_type,
                            "duration": row.duration_seconds,
                            "image": self._normalize_text((self._json_loads(row.effects_json, {}) or {}).get("storyboard_image") or row.prompt_image),
                            "storyboard_image": self._normalize_text((self._json_loads(row.effects_json, {}) or {}).get("storyboard_image")),
                            "narration": row.narration_text,
                            "suggested_soundtrack": self._normalize_text((self._json_loads(row.effects_json, {}) or {}).get("music_style")),
                            "approval_status": self._normalize_text((self._json_loads(row.effects_json, {}) or {}).get("approval_status") or "pending"),
                        },
                        row.scene_number,
                    )
                    for row in created
                ],
                "production_blueprint": {
                    **(self._extract_script_package(script).get("production_blueprint") or {}),
                    "scene_count": len(created),
                    "pipeline_status": "storyboard_pronto",
                },
            },
        )
        episode.status = "storyboard_generated"
        db.commit()
        logger.info("cenas persistidas script_id=%s banco=%s", script.id, len(created))
        for row in created:
            db.refresh(row)
        return created

    def generate_storyboard_preview(self, db: Session, script: BibleVideoScript, scene_number: int) -> Dict[str, Any]:
        storyboard = self._get_storyboard(script)
        if not storyboard:
            raise ValueError("Gere o roteiro/storyboard antes de solicitar preview visual.")
        target = next((item for item in storyboard if int(item.get("scene_number") or 0) == int(scene_number or 0)), None)
        if not target:
            raise ValueError("Cena do storyboard nao encontrada.")
        prompt_visual = self._normalize_text(target.get("prompt_image") or target.get("prompt_visual") or target.get("image"))
        if not prompt_visual:
            raise ValueError("A cena nao possui prompt visual para gerar preview.")
        preview_url = self.ai.generate_image(prompt_visual, aspect_ratio="16:9")
        target["storyboard_image"] = self._normalize_text(preview_url)
        target["image"] = self._normalize_text(preview_url)
        episode = db.query(BibleVideoEpisode).filter(BibleVideoEpisode.id == script.episode_id).first()
        self._save_storyboard_state(db, script, storyboard, episode=episode, pipeline_status="storyboard_preview_pronto")
        db.commit()
        db.refresh(script)
        return self.serialize_script(script)

    def approve_storyboard_scene(self, db: Session, script: BibleVideoScript, scene_number: int) -> Dict[str, Any]:
        storyboard = self._get_storyboard(script)
        if not storyboard:
            raise ValueError("Storyboard nao encontrado para aprovacao.")
        found = False
        for item in storyboard:
            if int(item.get("scene_number") or 0) == int(scene_number or 0):
                item["approval_status"] = "approved"
                found = True
                break
        if not found:
            raise ValueError("Cena do storyboard nao encontrada.")
        episode = db.query(BibleVideoEpisode).filter(BibleVideoEpisode.id == script.episode_id).first()
        self._save_storyboard_state(db, script, storyboard, episode=episode, pipeline_status="storyboard_pronto")
        profile_config = self.resolve_series_profile(db.query(BibleVideoSeries).filter(BibleVideoSeries.id == script.series_id).first())
        self._rebuild_script_analysis(script, profile_config=profile_config, episode=episode, storyboard=storyboard, persist=True)
        db.commit()
        db.refresh(script)
        return self.serialize_script(script)

    def approve_storyboard(self, db: Session, script: BibleVideoScript) -> Dict[str, Any]:
        storyboard = self._get_storyboard(script)
        if not storyboard:
            raise ValueError("Storyboard nao encontrado para aprovacao.")
        series = db.query(BibleVideoSeries).filter(BibleVideoSeries.id == script.series_id).first()
        episode = db.query(BibleVideoEpisode).filter(BibleVideoEpisode.id == script.episode_id).first()
        self._rebuild_script_analysis(script, profile_config=self.resolve_series_profile(series), episode=episode, storyboard=storyboard, persist=True)
        self._enforce_storyboard_approval_rules(script, series)
        approved_storyboard = []
        for idx, item in enumerate(storyboard):
            if not isinstance(item, dict):
                item = {"scene_number": idx + 1, "title": f"Cena {idx + 1}", "narration": str(item)}
            approved_storyboard.append({**item, "approval_status": "approved"})
        self._save_storyboard_state(db, script, approved_storyboard, episode=episode, pipeline_status="storyboard_aprovado")
        self._rebuild_script_analysis(script, profile_config=self.resolve_series_profile(series), episode=episode, storyboard=approved_storyboard, persist=True)
        db.commit()
        db.refresh(script)
        return self.serialize_script(script)

    def regenerate_storyboard_scene(self, db: Session, script: BibleVideoScript, scene_number: int) -> Dict[str, Any]:
        episode = db.query(BibleVideoEpisode).filter(BibleVideoEpisode.id == script.episode_id).first()
        series = db.query(BibleVideoSeries).filter(BibleVideoSeries.id == script.series_id).first()
        if not episode or not series:
            raise ValueError("Serie/episodio nao encontrados para regenerar a cena.")
        storyboard = self._get_storyboard(script)
        scene_sources = self._get_scene_sources(script)
        target_frame = next((item for item in storyboard if int(item.get("scene_number") or 0) == int(scene_number or 0)), None)
        if not target_frame:
            raise ValueError("Cena do storyboard nao encontrada.")
        target_source = next((item for item in scene_sources if int(item.get("scene_number") or 0) == int(scene_number or 0)), {})
        character_profiles = self._get_character_profiles_for_series(db, script.series_id)
        scenario_profiles = self._get_scenario_profiles_for_series(db, script.series_id)
        fallback = {
            "title": target_frame.get("title"),
            "narration": target_frame.get("narration"),
            "emotion": target_frame.get("emotion"),
            "prompt_visual": target_frame.get("prompt_visual"),
            "camera_movement": target_frame.get("camera_movement"),
            "duration": target_frame.get("duration"),
            "suggested_soundtrack": target_frame.get("suggested_soundtrack"),
            "visual_description": target_source.get("visual_description") or target_source.get("caption") or target_frame.get("prompt_visual"),
            "prompt_video": target_source.get("prompt_video"),
            "prompt_animation": target_source.get("prompt_animation"),
            "prompt_cinematic": target_source.get("prompt_cinematic"),
            "sound_effects": target_source.get("sound_effects"),
            "scenario_name": target_source.get("scenario_name") or episode.biblical_basis or series.bible_book,
            "characters": target_source.get("characters") or [series.main_character] if series.main_character else [],
        }
        prompt = (
            "Regenere apenas UMA cena do storyboard em JSON.\n"
            "Retorne: title, narration, emotion, prompt_visual, camera_movement, duration, suggested_soundtrack, visual_description, prompt_video, prompt_animation, prompt_cinematic, sound_effects, scenario_name, characters.\n"
            "Mantenha fidelidade biblica, retencao e consistencia visual.\n"
            "Use SEMPRE o Character Bible e o mesmo prompt mestre unico dos personagens recorrentes durante toda a temporada.\n"
            "Nao regenere o episodio inteiro.\n\n"
            f"Serie: {series.name}\n"
            f"Episodio: {episode.title}\n"
            f"Cena: {scene_number}\n"
            f"Storyboard atual: {self._json_dumps(target_frame)[:3000]}\n"
            f"Dados atuais da cena: {self._json_dumps(target_source)[:4000]}\n"
            f"Character Bible: {self._json_dumps(character_profiles)[:5000]}\n"
            f"Scenario Bible: {self._json_dumps(scenario_profiles)[:4000]}\n"
            f"Roteiro base: {script.full_narration[:9000]}"
        )
        data = self._generate_json(
            prompt,
            system_prompt="Voce e diretor cinematografico biblico. Regenere uma unica cena mantendo consistencia de personagem e cenario.",
            fallback=fallback,
        )
        refreshed_frame = self._normalize_storyboard_frame(
            {
                "scene_number": scene_number,
                "title": self._normalize_text(data.get("title") or fallback["title"]),
                "narration": self._normalize_text(data.get("narration") or fallback["narration"]),
                "emotion": self._normalize_text(data.get("emotion") or fallback["emotion"]),
                "prompt_visual": self._normalize_text(data.get("prompt_visual") or fallback["prompt_visual"]),
                "camera_movement": self._normalize_text(data.get("camera_movement") or fallback["camera_movement"]),
                "duration": self._normalize_int(data.get("duration"), self._normalize_int(fallback["duration"], 20)),
                "suggested_soundtrack": self._normalize_text(data.get("suggested_soundtrack") or fallback["suggested_soundtrack"]),
                "storyboard_image": "",
                "approval_status": "pending",
            },
            scene_number,
        )
        updated_sources = []
        for idx, source in enumerate(scene_sources):
            current_number = self._normalize_int(source.get("scene_number") or idx + 1, idx + 1)
            if current_number != int(scene_number or 0):
                updated_sources.append(source)
                continue
            updated_sources.append(
                {
                    **source,
                    "scene_number": current_number,
                    "title": refreshed_frame["title"],
                    "text": refreshed_frame["narration"],
                    "prompt_image": refreshed_frame["prompt_visual"],
                    "image_prompt": refreshed_frame["prompt_visual"],
                    "camera_direction": refreshed_frame["camera_movement"],
                    "duration": refreshed_frame["duration"],
                    "emotion": refreshed_frame["emotion"],
                    "music_style": refreshed_frame["suggested_soundtrack"],
                    "storyboard_image": "",
                    "approval_status": "pending",
                    "visual_description": self._normalize_text(data.get("visual_description") or source.get("visual_description") or source.get("caption")),
                    "prompt_video": self._normalize_text(data.get("prompt_video") or source.get("prompt_video") or f"{refreshed_frame['prompt_visual']}, cinematic motion, 4k"),
                    "prompt_animation": self._normalize_text(data.get("prompt_animation") or source.get("prompt_animation") or f"Animate with cinematic emotion: {refreshed_frame['prompt_visual'][:220]}"),
                    "prompt_cinematic": self._normalize_text(data.get("prompt_cinematic") or source.get("prompt_cinematic") or f"{refreshed_frame['prompt_visual']}, biblical cinematic documentary, emotional atmosphere"),
                    "sound_effects": self._normalize_text(data.get("sound_effects") or source.get("sound_effects")),
                    "scenario_name": self._normalize_text(data.get("scenario_name") or source.get("scenario_name")),
                    "characters": data.get("characters") if isinstance(data.get("characters"), list) else source.get("characters"),
                }
            )
        updated_storyboard = []
        for item in storyboard:
            if int(item.get("scene_number") or 0) == int(scene_number or 0):
                updated_storyboard.append(refreshed_frame)
            else:
                updated_storyboard.append(item)
        self._save_storyboard_state(db, script, updated_storyboard, scene_sources=updated_sources, episode=episode, pipeline_status="storyboard_pronto")
        db.commit()
        db.refresh(script)
        return self.serialize_script(script)

    def generate_shorts_bundle(self, db: Session, script: BibleVideoScript, episode: BibleVideoEpisode) -> List[Dict[str, Any]]:
        series = db.query(BibleVideoSeries).filter(BibleVideoSeries.id == script.series_id).first()
        package = self._extract_script_package(script)
        script_sections = package.get("script_sections") if isinstance(package.get("script_sections"), dict) else {}
        youtube_growth = package.get("youtube_growth") if isinstance(package.get("youtube_growth"), dict) else {}
        target_shorts = 3
        short_templates = [
            {
                "short_type": "gancho",
                "title": f"{(episode.title or 'Episodio')} | Gancho Inicial"[:80],
                "hook": self._normalize_text(script_sections.get("opening_hook") or episode.opening_hook or episode.title)[:110],
                "narration": self._normalize_text(script_sections.get("opening_hook") or episode.summary or script.full_narration or "")[:320],
                "description": "Short focado em abrir curiosidade nos primeiros segundos.",
                "visual_prompt": f"{episode.title}, biblical cinematic vertical short, opening hook, dramatic tension, 9:16, 4k",
                "subtitle": self._normalize_text(script_sections.get("opening_hook") or episode.opening_hook or episode.title)[:90],
                "hashtags": youtube_growth.get("hashtags") or ["#biblia", "#shortsbiblicos", "#animebiblico"],
                "cta": "Assista ao episodio completo no canal.",
                "duration_seconds": 35,
            },
            {
                "short_type": "momento_emocional",
                "title": f"{(episode.title or 'Episodio')} | Momento Emocional"[:80],
                "hook": self._normalize_text(script_sections.get("impact_phrase") or episode.impact_phrase or episode.title)[:110],
                "narration": self._normalize_text(script_sections.get("climax") or episode.tension_moment or episode.summary or script.full_narration or "")[:320],
                "description": "Short focado no pico emocional e na conexao com o publico.",
                "visual_prompt": f"{episode.title}, biblical cinematic vertical short, emotional climax, tears, faith, dramatic light, 9:16, 4k",
                "subtitle": self._normalize_text(script_sections.get("impact_phrase") or episode.impact_phrase or episode.title)[:90],
                "hashtags": youtube_growth.get("hashtags") or ["#biblia", "#emocao", "#animebiblico"],
                "cta": "Veja o episodio completo e acompanhe a serie.",
                "duration_seconds": 45,
            },
            {
                "short_type": "cliffhanger",
                "title": f"{(episode.title or 'Episodio')} | Cliffhanger Final"[:80],
                "hook": self._normalize_text(script_sections.get("cliffhanger") or episode.ending_hook or episode.title)[:110],
                "narration": self._normalize_text(script_sections.get("cliffhanger") or episode.ending_hook or script.full_narration or "")[:320],
                "description": "Short focado em deixar pergunta, ameaca e promessa para o proximo episodio.",
                "visual_prompt": f"{episode.title}, biblical cinematic vertical short, unresolved ending, future threat, cliffhanger, 9:16, 4k",
                "subtitle": self._normalize_text(script_sections.get("cliffhanger") or episode.ending_hook or episode.title)[:90],
                "hashtags": youtube_growth.get("hashtags") or ["#biblia", "#cliffhanger", "#serieNetflixBiblica"],
                "cta": "Nao perca o proximo episodio.",
                "duration_seconds": 40,
            },
        ]
        fallback = {
            "shorts": short_templates
        }
        prompt = (
            "Crie exatamente 3 Shorts para divulgar este episodio biblico.\n"
            "Retorne JSON com a chave shorts.\n"
            "Cada short deve ter: short_type, title, hook, narration, visual_prompt, subtitle, description, hashtags, cta, duration_seconds.\n"
            "Cada short deve durar entre 30 e 60 segundos.\n\n"
            "Os tipos obrigatorios sao: gancho, momento_emocional, cliffhanger.\n"
            f"Episodio: {episode.title}\n"
            f"Resumo: {episode.summary or ''}\n"
            f"Frase de impacto: {episode.impact_phrase or ''}\n"
            f"Gancho final: {episode.ending_hook or ''}\n"
            f"Roteiro: {script.full_narration[:8000]}"
        )
        data = self._generate_json(
            prompt,
            system_prompt="Voce e editor de Shorts biblicos virais, sem distorcer a Biblia.",
            fallback=fallback,
        )
        shorts = data.get("shorts") if isinstance(data, dict) else None
        if not isinstance(shorts, list) or not shorts:
            shorts = fallback["shorts"]
        normalized_shorts = []
        for idx, item in enumerate(shorts[:target_shorts]):
            if not isinstance(item, dict):
                item = {"title": f"Short {idx + 1}", "hook": self._normalize_text(item)}
            fallback_item = short_templates[idx]
            normalized_shorts.append(
                {
                    "short_type": self._normalize_text(item.get("short_type") or fallback_item["short_type"] or f"short_{idx + 1}"),
                    "title": self._normalize_text(item.get("title") or fallback_item["title"] or f"Short {idx + 1}"),
                    "hook": self._normalize_text(item.get("hook") or fallback_item["hook"]),
                    "narration": self._normalize_text(item.get("narration") or item.get("description") or item.get("hook") or fallback_item["narration"]),
                    "visual_prompt": self._normalize_text(item.get("visual_prompt") or fallback_item["visual_prompt"] or f"{episode.title}, biblical cinematic vertical short, emotional frame, 9:16, 4k"),
                    "subtitle": self._normalize_text(item.get("subtitle") or item.get("hook") or item.get("title") or fallback_item["subtitle"]),
                    "description": self._normalize_text(item.get("description") or item.get("hook") or fallback_item["description"]),
                    "hashtags": [self._normalize_text(x) for x in self._ensure_list(item.get("hashtags") or fallback_item["hashtags"]) if self._normalize_text(x)],
                    "cta": self._normalize_text(item.get("cta") or fallback_item["cta"] or "Assista ao episodio completo no canal."),
                    "duration_seconds": max(30, min(60, self._normalize_int(item.get("duration_seconds"), fallback_item["duration_seconds"]))),
                }
            )
        script.shorts_json = self._json_dumps(normalized_shorts)
        self._save_script_package(
            script,
            {
                "production_blueprint": {
                    **(self._extract_script_package(script).get("production_blueprint") or {}),
                    "shorts_count": len(normalized_shorts),
                    "pipeline_status": "shorts_prontos",
                }
            },
        )
        db.commit()
        db.refresh(script)
        return normalized_shorts[:target_shorts]

    def generate_thumbnail(self, db: Session, script: BibleVideoScript, episode: BibleVideoEpisode, series: Optional[BibleVideoSeries]) -> Dict[str, Any]:
        package = self._extract_script_package(script)
        youtube_growth = package.get("youtube_growth") if isinstance(package.get("youtube_growth"), dict) else {}
        thumbnail_hint = self._normalize_text(youtube_growth.get("thumbnail_prompt"))
        base_visual = (
            f"{thumbnail_hint or ''} {series.visual_style if series else 'anime cinematografico'} de {series.main_character if series else episode.title}, "
            "expressao forte, fundo dramatico, luz intensa, momento biblico crucial."
        )
        fallback_variants = [
            {"type": "emocional", "headline": (episode.thumbnail_suggestion or episode.impact_phrase or episode.title or "").upper()[:60], "visual_prompt": f"{base_visual} close-up emocional", "youtube_title": episode.youtube_title_suggestion or episode.title},
            {"type": "epica", "headline": f"{(episode.title or '').upper()[:40]} EM GUERRA", "visual_prompt": f"{base_visual} wide shot epico, escala monumental", "youtube_title": f"{episode.title} em uma batalha inesquecivel"},
            {"type": "suspense", "headline": f"O SEGREDO DE {(series.main_character or episode.title or '').upper()[:28]}", "visual_prompt": f"{base_visual} sombras dramaticas, suspense intenso", "youtube_title": f"O segredo por tras de {episode.title}"},
            {"type": "choque", "headline": "NINGUEM ESPERAVA ISSO", "visual_prompt": f"{base_visual} choque visual, expressao extrema, contraste alto", "youtube_title": f"{episode.title}: o momento que chocou a todos"},
            {"type": "ctr_maximo", "headline": "O MOMENTO QUE MUDOU TUDO", "visual_prompt": f"{base_visual} max CTR frame, dramatic lighting, bold composition", "youtube_title": f"{episode.title}: o momento que mudou tudo"},
        ]
        fallback = {
            "selected_type": "emocional",
            "headline": fallback_variants[0]["headline"],
            "visual_prompt": fallback_variants[0]["visual_prompt"],
            "youtube_title": fallback_variants[0]["youtube_title"],
            "variants": fallback_variants,
        }
        prompt = (
            "Crie 5 thumbnails profissionais para YouTube em JSON.\n"
            "Retorne: selected_type, headline, visual_prompt, youtube_title, variants.\n"
            "variants deve conter 5 itens com: type, headline, visual_prompt, youtube_title.\n"
            "Os tipos obrigatorios sao: emocional, epica, suspense, choque, ctr_maximo.\n\n"
            f"Serie: {series.name if series else ''}\n"
            f"Episodio: {episode.title}\n"
            f"Estilo visual: {series.visual_style if series else ''}\n"
            f"Frase de impacto: {episode.impact_phrase or ''}\n"
            f"Gancho: {episode.opening_hook or ''}\n"
            f"Thumbnail prompt base: {thumbnail_hint}"
        )
        data = self._generate_json(
            prompt,
            system_prompt="Voce e especialista em thumbnails dramaticas para historias biblicas no YouTube.",
            fallback=fallback,
        )
        thumb = data if isinstance(data, dict) else fallback
        variants = thumb.get("variants") if isinstance(thumb, dict) else None
        if not isinstance(variants, list) or len(variants) < 5:
            variants = fallback_variants
        normalized = {
            "selected_type": self._normalize_text(thumb.get("selected_type") if isinstance(thumb, dict) else fallback["selected_type"]) or "emocional",
            "headline": self._normalize_text((thumb.get("headline") if isinstance(thumb, dict) else fallback["headline"]) or fallback["headline"]),
            "visual_prompt": self._normalize_text((thumb.get("visual_prompt") if isinstance(thumb, dict) else fallback["visual_prompt"]) or fallback["visual_prompt"]),
            "youtube_title": self._normalize_text((thumb.get("youtube_title") if isinstance(thumb, dict) else fallback["youtube_title"]) or fallback["youtube_title"]),
            "variants": [
                {
                    "type": self._normalize_text(item.get("type") or fallback_variants[idx]["type"]),
                    "headline": self._normalize_text(item.get("headline") or fallback_variants[idx]["headline"]),
                    "visual_prompt": self._normalize_text(item.get("visual_prompt") or fallback_variants[idx]["visual_prompt"]),
                    "youtube_title": self._normalize_text(item.get("youtube_title") or fallback_variants[idx]["youtube_title"]),
                }
                for idx, item in enumerate((variants or fallback_variants)[:5])
            ],
        }
        script.thumbnail_json = self._json_dumps(normalized)
        self._save_script_package(
            script,
            {
                "production_blueprint": {
                    **(self._extract_script_package(script).get("production_blueprint") or {}),
                    "thumbnail_variants": 5,
                    "pipeline_status": "thumbnails_prontas",
                }
            },
        )
        db.commit()
        db.refresh(script)
        return self._json_loads(script.thumbnail_json, fallback)

    def build_plan_for_job(self, db: Session, job: BibleVideoJob) -> Dict[str, Any]:
        if job.plan_json:
            cached = self._json_loads(job.plan_json, {})
            if isinstance(cached, dict) and cached.get("scenes"):
                return cached

        script = db.query(BibleVideoScript).filter(BibleVideoScript.id == job.script_id).first()
        episode = db.query(BibleVideoEpisode).filter(BibleVideoEpisode.id == job.episode_id).first()
        series = db.query(BibleVideoSeries).filter(BibleVideoSeries.id == job.series_id).first()
        if not script or not episode or not series:
            raise Exception("Job sem serie/episodio/roteiro validos.")
        package = self._extract_script_package(script)
        youtube_growth = package.get("youtube_growth") if isinstance(package.get("youtube_growth"), dict) else {}
        scenes = (
            db.query(BibleVideoScene)
            .filter(BibleVideoScene.script_id == script.id)
            .order_by(BibleVideoScene.scene_number.asc())
            .all()
        )
        if not scenes:
            scenes = self.generate_scenes(db, script, episode, series)

        tags = [
            "biblia",
            "anime biblico",
            *(youtube_growth.get("tags") or []),
            *(youtube_growth.get("seo_keywords") or []),
            series.main_character or "",
            series.bible_book or "",
            series.narrative_tone or "",
        ]
        tags = [self._normalize_text(t) for t in tags if self._normalize_text(t)]
        plan_scenes = []
        for scene in scenes:
            scene_meta = self._json_loads(scene.effects_json, {})
            plan_scenes.append(
                {
                    "text": self._normalize_text(scene.narration_text),
                    "image_prompt": self._normalize_text(scene.prompt_image or scene.visual_description or ""),
                    "video_prompt": self._normalize_text((scene_meta or {}).get("prompt_video")),
                    "animation_prompt": self._normalize_text(scene.prompt_animation),
                    "cinematic_prompt": self._normalize_text((scene_meta or {}).get("prompt_cinematic")),
                    "camera_direction": self._normalize_text((scene_meta or {}).get("camera_direction") or scene.camera_type),
                    "sound_effects": self._normalize_text((scene_meta or {}).get("sound_effects")),
                    "music_style": self._normalize_text((scene_meta or {}).get("music_style")),
                    "caption": (scene.visual_description or scene.narration_text or "")[:160],
                }
            )
        plan = {
            "title": self._normalize_text(youtube_growth.get("title_main") or episode.youtube_title_suggestion or episode.title),
            "description": self._normalize_text("\n".join(
                [
                    self._normalize_text(youtube_growth.get("description") or episode.summary),
                    "",
                    self._normalize_text(script.subscribe_cta),
                    self._normalize_text(script.next_episode_cta),
                    "Narrativa inspirada em relato biblico." if script.disclaimer_required else "",
                ]
            )),
            "tags": tags[:15],
            "scenes": plan_scenes,
            "music_mood": "emotional_cinematic" if "suspense" in str(series.narrative_tone or "").lower() else "happy",
            "music_prompt": f"{series.narrative_tone or 'epic'} biblical anime cinematic soundtrack, dramatic but reverent",
            "target_duration_sec": int(script.desired_duration_minutes or episode.estimated_minutes or 5) * 60,
            "kind": "story",
            "allow_image_reuse": True,
            "bg_music_volume": 0.03,
            "provider_targets": ["veo", "kling", "luma", "runway", "pika"],
        }
        job.tags_json = self._json_dumps(tags[:15])
        job.description_text = plan["description"]
        job.plan_json = self._json_dumps(plan)
        db.commit()
        db.refresh(job)
        return plan

    def create_job(
        self,
        db: Session,
        user_id: Optional[int],
        script: BibleVideoScript,
        platform: str = "youtube",
        aspect_ratio: str = "16:9",
        start_immediately: bool = False,
        scheduled_for: Optional[datetime] = None,
        job_type: str = "episode",
        plan_override: Optional[Dict[str, Any]] = None,
        parent_job_id: Optional[int] = None,
    ) -> BibleVideoJob:
        episode = db.query(BibleVideoEpisode).filter(BibleVideoEpisode.id == script.episode_id).first()
        series = db.query(BibleVideoSeries).filter(BibleVideoSeries.id == script.series_id).first()
        storyboard = self._extract_script_package(script).get("storyboard") or []
        if job_type == "episode" and isinstance(storyboard, list) and storyboard:
            pending = [item for item in storyboard if self._normalize_text((item or {}).get("approval_status") or "pending") != "approved"]
            if pending:
                raise ValueError("Finalize e aprove o Storyboard antes de gerar o video.")
        if job_type == "episode":
            characters = self._get_character_profiles_for_series(db, script.series_id)
            scenarios = self._get_scenario_profiles_for_series(db, script.series_id)
            if not characters:
                raise ValueError("Finalize o Character Bible antes de partir para a geracao de video.")
            if not scenarios:
                raise ValueError("Cadastre e finalize o Banco de Cenarios antes de partir para a geracao de video.")
        config = self.get_or_create_config(db, user_id)
        scenes = db.query(BibleVideoScene).filter(BibleVideoScene.script_id == script.id).count()
        costs = self._estimate_costs(config, script.desired_duration_minutes or 5, scenes or 8, shorts_count=0)
        title = episode.title if episode else (series.name if series else "Job Biblico")
        if job_type == "short":
            title = f"Short - {title}"
        initial_stage = "script_approved" if script.validation_status == "approved" else "script_generated"
        if episode and episode.status == "storyboard_approved":
            initial_stage = "storyboard_approved"
        elif episode and episode.status == "storyboard_generated":
            initial_stage = "storyboard_generated"
        job = BibleVideoJob(
            user_id=user_id,
            series_id=script.series_id,
            episode_id=script.episode_id,
            script_id=script.id,
            parent_job_id=parent_job_id,
            title=title,
            job_type=job_type,
            platform=self._normalize_text(platform or "youtube").lower(),
            aspect_ratio=self._normalize_text(aspect_ratio or "16:9"),
            kanban_stage=initial_stage,
            status="queued" if start_immediately else "draft",
            approval_status="pending",
            scheduled_for=scheduled_for,
            publish_platforms_json=self._json_dumps([platform]),
            estimated_cost=costs["total"],
            description_text=(episode.summary if episode else "") or "",
            plan_json=self._json_dumps(plan_override or {}),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    def _resolve_local_video_path(self, job: BibleVideoJob) -> str:
        if job.output_video_url and os.path.isabs(job.output_video_url) and os.path.exists(job.output_video_url):
            return job.output_video_url
        name = os.path.basename(self._normalize_text(job.output_video_url))
        if not name:
            raise Exception("Job sem video gerado.")
        candidate = os.path.join(VIDEO_OUTPUT_DIR, name)
        if os.path.exists(candidate):
            return candidate
        candidate = os.path.abspath(os.path.join("app", "static", "videos", name))
        if os.path.exists(candidate):
            return candidate
        raise Exception("Arquivo de video nao encontrado para publicacao.")

    def process_job(self, job_id: int):
        db = SessionLocal()
        try:
            job = db.query(BibleVideoJob).filter(BibleVideoJob.id == job_id).first()
            if not job:
                return
            task_id = job.task_id or create_task(user_id=job.user_id)
            job.task_id = task_id
            job.status = "processing"
            job.progress = 1
            job.kanban_stage = "script_approved"
            job.status_message = "Preparando producao..."
            db.commit()

            def progress(pct: int, msg: str, stage: Optional[str] = None):
                job.progress = max(0, min(100, int(pct or 0)))
                job.status_message = msg
                job.status = "processing"
                if stage:
                    job.kanban_stage = stage
                db.commit()
                update_task(task_id, status="processing", progress=job.progress, message=msg)

            plan = self.build_plan_for_job(db, job)
            config = self.get_or_create_config(db, job.user_id)
            progress(5, "Plano de video preparado.", "scenes_generated")
            progress(12, "Storyboard montado e pronto para aprovacao.", "storyboard_generated")
            progress(20, "Gerando imagens-chave das cenas...", "images_generated")
            progress(32, "Gerando voz e trilha...", "voice_generated")

            video_service = VideoGenerator(ai_service=AIContentGenerator())
            result = video_service.create_video_from_plan(
                plan,
                aspect_ratio=job.aspect_ratio or "16:9",
                progress_callback=lambda p, m: progress(
                    p,
                    m,
                    "video_editing" if int(p or 0) >= 80 else ("video_animating" if int(p or 0) >= 45 else "images_generated"),
                ),
                voice_style="human",
                voice_gender="female" if (config.default_voice or "").lower() != "male" else "male",
            )

            output_video_url = ""
            if isinstance(result, dict):
                output_video_url = self._normalize_text(result.get("video_url"))
            elif isinstance(result, str):
                output_video_url = result
            if not output_video_url:
                raise Exception("A producao terminou sem retornar video_url.")

            job.output_video_url = output_video_url
            job.result_json = self._json_dumps(result if isinstance(result, dict) else {"video_url": output_video_url})
            job.actual_cost = float(job.estimated_cost or 0)
            progress(92, "Finalizando thumbnails e shorts derivados...", "thumbnail_generated")
            if job.job_type == "episode":
                progress(96, "Shorts promocionais preparados.", "shorts_generated")
            job.progress = 100
            job.status = "ready"
            job.kanban_stage = "awaiting_approval"
            job.status_message = "Video gerado. Aguardando aprovacao."
            db.commit()
            update_task(task_id, status="completed", progress=100, message="Video gerado com sucesso.", result=self.serialize_job(job))

            if job.job_type == "episode":
                script = db.query(BibleVideoScript).filter(BibleVideoScript.id == job.script_id).first()
                episode = db.query(BibleVideoEpisode).filter(BibleVideoEpisode.id == job.episode_id).first()
                if script and episode:
                    shorts = self.generate_shorts_bundle(db, script, episode)
                    for idx, short_item in enumerate(shorts[:5]):
                        short_prompt = (
                            f"Crie um roteiro curto vertical com base neste gancho biblico: {short_item.get('hook') or short_item.get('title')}. "
                            f"Use CTA: {short_item.get('cta') or ''}"
                        )
                        try:
                            short_plan = self.ai.generate_short_script_from_prompt(short_prompt)
                            if not isinstance(short_plan, dict):
                                short_plan = {"title": short_item.get("title") or f"Short {idx+1}", "scenes": [{"text": short_item.get("description") or short_item.get("hook") or "", "image_prompt": short_item.get("hook") or ""}]}
                        except Exception:
                            short_plan = {"title": short_item.get("title") or f"Short {idx+1}", "scenes": [{"text": short_item.get("description") or short_item.get("hook") or "", "image_prompt": short_item.get("hook") or ""}]}
                        child = self.create_job(
                            db,
                            user_id=job.user_id,
                            script=script,
                            platform=job.platform,
                            aspect_ratio="9:16",
                            start_immediately=False,
                            scheduled_for=None,
                            job_type="short",
                            plan_override=short_plan,
                            parent_job_id=job.id,
                        )
                        child.description_text = short_item.get("description") or child.description_text
                        child.tags_json = self._json_dumps(short_item.get("hashtags") or [])
                        child.status_message = "Short criado a partir do episodio. Pronto para iniciar."
                        db.commit()
        except Exception as e:
            try:
                job = db.query(BibleVideoJob).filter(BibleVideoJob.id == job_id).first()
                if job:
                    job.status = "error"
                    job.kanban_stage = "error"
                    job.status_message = str(e)
                    job.error_log = str(e)
                    db.commit()
                    if job.task_id:
                        update_task(job.task_id, status="failed", progress=int(job.progress or 0), message=str(e), result={"job_id": job.id})
            except Exception:
                pass
        finally:
            db.close()

    def approve_job(self, db: Session, job: BibleVideoJob, notes: str = "") -> BibleVideoJob:
        job.approval_status = "approved"
        job.kanban_stage = "ready_to_publish"
        job.status = "ready"
        if notes:
            job.status_message = notes
        db.commit()
        db.refresh(job)
        return job

    def publish_job(self, db: Session, job: BibleVideoJob) -> Dict[str, Any]:
        local_path = self._resolve_local_video_path(job)
        yt = YouTubeService()
        title = job.title
        description = job.description_text or ""
        tags = self._json_loads(job.tags_json, [])
        response = yt.upload_video(local_path, title, description, tags=tags, thumbnail_path=None)
        if not isinstance(response, dict) or response.get("error"):
            raise Exception((response or {}).get("error") if isinstance(response, dict) else "Falha no upload para YouTube.")
        youtube_video_id = self._normalize_text(response.get("id"))
        job.status = "published"
        job.kanban_stage = "published"
        job.approval_status = "approved"
        job.published_video_id = youtube_video_id or job.published_video_id
        job.status_message = "Publicado no YouTube com sucesso."
        db.commit()

        scheduled = ScheduledVideo(
            user_id=job.user_id,
            theme=job.title,
            title=job.title,
            description=job.description_text or "",
            scheduled_for=job.scheduled_for or datetime.utcnow(),
            status="published",
            video_type="short" if job.job_type == "short" else "video",
            script_data=job.plan_json,
            video_url=job.output_video_url,
            progress=100,
            publish_at=job.scheduled_for,
            auto_post=True,
            youtube_video_id=job.published_video_id,
            uploaded_at=datetime.utcnow(),
        )
        db.add(scheduled)
        db.commit()
        return {"youtube_video_id": job.published_video_id, "scheduled_video_id": scheduled.id, "response": response}


def process_bible_video_job(job_id: int):
    service = BibleVideoFactoryService()
    service.process_job(job_id)
