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


KANBAN_STAGES = [
    "idea",
    "script_generated",
    "script_approved",
    "scenes_generated",
    "voice_generated",
    "video_animating",
    "video_editing",
    "awaiting_approval",
    "ready_to_publish",
    "published",
    "error",
]


class BibleVideoFactoryService:
    def __init__(self):
        self.ai = AIContentGenerator()

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
        t = str(text or "").strip()
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
        return t.strip()

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
        raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
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
        return {
            "id": row.id,
            "user_id": row.user_id,
            "name": row.name,
            "bible_book": row.bible_book,
            "main_character": row.main_character,
            "target_audience": row.target_audience,
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
            "full_narration": row.full_narration,
            "scenes": self._json_loads(row.scenes_json, []),
            "optional_dialogues": self._json_loads(row.optional_dialogues_json, []),
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
        return {
            "id": row.id,
            "script_id": row.script_id,
            "series_id": row.series_id,
            "episode_id": row.episode_id,
            "user_id": row.user_id,
            "scene_number": int(row.scene_number or 0),
            "narration_text": row.narration_text,
            "visual_description": row.visual_description,
            "characters": self._json_loads(row.characters_json, []),
            "scenario_name": row.scenario_name,
            "emotion": row.emotion,
            "prompt_image": row.prompt_image,
            "prompt_animation": row.prompt_animation,
            "duration_seconds": float(row.duration_seconds or 0),
            "camera_type": row.camera_type,
            "effects": self._json_loads(row.effects_json, []),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def serialize_character(self, row: BibleVideoCharacter) -> Dict[str, Any]:
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
            "emotions": self._json_loads(row.emotions_json, []),
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
            return bool(str(value or "").strip())

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
                    "impact_phrase": (chunk[:120] or series.name).strip(),
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
                episode_number=int(item.get("episode_number") or idx + 1),
                title=(item.get("title") or f"{series.name} - Episodio {idx + 1}").strip()[:150],
                summary=(item.get("summary") or "").strip(),
                biblical_basis=(item.get("biblical_basis") or series.bible_book or "").strip(),
                opening_hook=(item.get("opening_hook") or "").strip(),
                development_text=(item.get("development") or "").strip(),
                tension_moment=(item.get("tension_moment") or "").strip(),
                impact_phrase=(item.get("impact_phrase") or "").strip(),
                ending_hook=(item.get("ending_hook") or "").strip(),
                short_suggestion=(item.get("short_suggestion") or "").strip(),
                thumbnail_suggestion=(item.get("thumbnail_suggestion") or "").strip(),
                youtube_title_suggestion=(item.get("youtube_title_suggestion") or "").strip(),
                estimated_minutes=int(series.episode_duration_minutes or 5),
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
        config = self.get_or_create_config(db, user_id)
        characters = db.query(BibleVideoCharacter).filter(BibleVideoCharacter.series_id == episode.series_id).all()
        scenarios = db.query(BibleVideoScenario).filter(BibleVideoScenario.series_id == episode.series_id).all()
        prompt = (
            "Crie um roteiro biblico em JSON para video episodico.\n"
            "Retorne as chaves: full_narration, scenes, optional_dialogues, voice_emotion_notes, soundtrack_notes, sound_effects_notes, retention_hooks.\n"
            "Cada item de scenes deve conter: text, image_prompt, caption.\n"
            "A narrativa pode dramatizar, mas nao pode alterar os fatos principais da Biblia.\n\n"
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
            f"Personagens cadastrados: {', '.join([c.name for c in characters])}\n"
            f"Cenarios cadastrados: {', '.join([s.name for s in scenarios])}\n"
        )
        fallback_scenes = []
        base_text = "\n\n".join(
            x for x in [episode.opening_hook, episode.summary, episode.development_text, episode.tension_moment, episode.ending_hook] if x
        ).strip()
        paragraphs = self._split_text_chunks(base_text, max(6, desired_duration_minutes * 2))
        for idx, item in enumerate(paragraphs):
            fallback_scenes.append(
                {
                    "text": item,
                    "image_prompt": f"{series.visual_style or 'anime cinematografico'} de {episode.title}. {item[:180]}",
                    "caption": item[:120],
                }
            )
        fallback = {
            "full_narration": base_text,
            "scenes": fallback_scenes,
            "optional_dialogues": [],
            "voice_emotion_notes": f"Narracao {narrative_style} com drama {drama_level}/10 e reverencia biblica.",
            "soundtrack_notes": f"Trilha {series.narrative_tone if series else 'epica'} com suspense moderado e atmosfera cinematografica.",
            "sound_effects_notes": "Vento, passos, ambiente historico, multidao e silencio dramatico quando necessario.",
            "retention_hooks": [episode.opening_hook, episode.impact_phrase, episode.ending_hook],
        }
        data = self._generate_json(
            prompt,
            system_prompt="Voce e um roteirista biblico cinematografico para series em episodios.",
            fallback=fallback,
        )
        script = BibleVideoScript(
            series_id=episode.series_id,
            episode_id=episode.id,
            user_id=user_id,
            desired_duration_minutes=int(desired_duration_minutes or episode.estimated_minutes or 5),
            narrative_style=(narrative_style or series.narrative_tone or "emocionante").strip(),
            drama_level=int(drama_level or 7),
            biblical_fidelity_level=int(biblical_fidelity_level or 9),
            target_audience=(target_audience or episode.title).strip(),
            subscribe_cta=(subscribe_cta or config.default_cta or "").strip(),
            next_episode_cta=(next_episode_cta or config.default_next_episode_cta or "").strip(),
            full_narration=(data.get("full_narration") or fallback["full_narration"]).strip(),
            scenes_json=self._json_dumps(data.get("scenes") or fallback["scenes"]),
            optional_dialogues_json=self._json_dumps(data.get("optional_dialogues") or []),
            voice_emotion_notes=(data.get("voice_emotion_notes") or fallback["voice_emotion_notes"]).strip(),
            soundtrack_notes=(data.get("soundtrack_notes") or fallback["soundtrack_notes"]).strip(),
            sound_effects_notes=(data.get("sound_effects_notes") or fallback["sound_effects_notes"]).strip(),
            retention_hooks_json=self._json_dumps(data.get("retention_hooks") or fallback["retention_hooks"]),
            validation_status="pending",
        )
        db.add(script)
        episode.status = "script_generated"
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
        status = status_map.get(str(data.get("status") or "").strip().lower(), fallback["status"])
        script.validation_status = status
        script.validation_notes = (data.get("notes") or fallback["notes"]).strip()
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

        prompt = (
            "Transforme o roteiro abaixo em uma lista de cenas em JSON.\n"
            "Retorne a chave scenes.\n"
            "Cada cena deve conter: scene_number, narration_text, visual_description, characters, scenario_name, emotion, "
            "prompt_image, prompt_animation, duration_seconds, camera_type, effects.\n\n"
            f"Serie: {series.name if series else ''}\n"
            f"Episodio: {episode.title}\n"
            f"Estilo visual: {series.visual_style if series else ''}\n"
            f"Tom: {series.narrative_tone if series else ''}\n"
            f"Roteiro base: {script.full_narration[:12000]}\n"
        )
        fallback = {
            "scenes": [
                {
                    "scene_number": idx + 1,
                    "narration_text": item.get("text") or "",
                    "visual_description": item.get("caption") or item.get("text") or "",
                    "characters": [series.main_character] if series and series.main_character else [],
                    "scenario_name": series.bible_book or "Cenario biblico",
                    "emotion": series.narrative_tone or "emocao",
                    "prompt_image": item.get("image_prompt") or item.get("text") or "",
                    "prompt_animation": f"Anime cinematografico em movimento suave: {(item.get('image_prompt') or item.get('text') or '')[:220]}",
                    "duration_seconds": max(5.0, round((float(script.desired_duration_minutes or 5) * 60.0) / max(1, len(source)), 2)),
                    "camera_type": ["zoom", "travelling", "aproximacao", "panoramica"][idx % 4],
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

        created = []
        for idx, item in enumerate(scenes_data):
            row = BibleVideoScene(
                script_id=script.id,
                series_id=script.series_id,
                episode_id=script.episode_id,
                user_id=script.user_id,
                scene_number=int(item.get("scene_number") or idx + 1),
                narration_text=(item.get("narration_text") or "").strip(),
                visual_description=(item.get("visual_description") or "").strip(),
                characters_json=self._json_dumps(item.get("characters") or []),
                scenario_name=(item.get("scenario_name") or "").strip(),
                emotion=(item.get("emotion") or "").strip(),
                prompt_image=(item.get("prompt_image") or "").strip(),
                prompt_animation=(item.get("prompt_animation") or "").strip(),
                duration_seconds=float(item.get("duration_seconds") or 8.0),
                camera_type=(item.get("camera_type") or "").strip(),
                effects_json=self._json_dumps(item.get("effects") or []),
            )
            db.add(row)
            created.append(row)
        script.scenes_json = self._json_dumps(
            [
                {
                    "text": c.narration_text,
                    "image_prompt": c.prompt_image,
                    "caption": c.visual_description,
                }
                for c in created
            ]
        )
        episode.status = "scenes_generated"
        db.commit()
        for row in created:
            db.refresh(row)
        return created

    def generate_shorts_bundle(self, db: Session, script: BibleVideoScript, episode: BibleVideoEpisode) -> List[Dict[str, Any]]:
        fallback = {
            "shorts": [
                {
                    "title": (episode.impact_phrase or episode.title or "Short Biblico")[:80],
                    "hook": (episode.opening_hook or episode.impact_phrase or episode.title)[:110],
                    "description": (episode.summary or script.full_narration or "")[:240],
                    "hashtags": ["#biblia", "#shortsbiblicos", "#animebiblico"],
                    "cta": "Assista ao episodio completo no canal.",
                }
                for _ in range(3)
            ]
        }
        prompt = (
            "Crie entre 3 e 5 Shorts para divulgar este episodio biblico.\n"
            "Retorne JSON com a chave shorts.\n"
            "Cada short deve ter: title, hook, description, hashtags, cta.\n\n"
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
        script.shorts_json = self._json_dumps(shorts[:5])
        db.commit()
        db.refresh(script)
        return shorts[:5]

    def generate_thumbnail(self, db: Session, script: BibleVideoScript, episode: BibleVideoEpisode, series: Optional[BibleVideoSeries]) -> Dict[str, Any]:
        fallback = {
            "headline": (episode.thumbnail_suggestion or episode.impact_phrase or episode.title or "").upper()[:60],
            "visual_prompt": (
                f"{series.visual_style if series else 'anime cinematografico'} de {series.main_character if series else episode.title}, "
                f"expressao forte, fundo dramatico, luz intensa, momento biblico crucial."
            ),
            "youtube_title": episode.youtube_title_suggestion or episode.title,
        }
        prompt = (
            "Crie uma sugestao de thumbnail para YouTube em JSON.\n"
            "Retorne: headline, visual_prompt, youtube_title.\n\n"
            f"Serie: {series.name if series else ''}\n"
            f"Episodio: {episode.title}\n"
            f"Estilo visual: {series.visual_style if series else ''}\n"
            f"Frase de impacto: {episode.impact_phrase or ''}\n"
            f"Gancho: {episode.opening_hook or ''}"
        )
        data = self._generate_json(
            prompt,
            system_prompt="Voce e especialista em thumbnails dramaticas para historias biblicas no YouTube.",
            fallback=fallback,
        )
        script.thumbnail_json = self._json_dumps(data if isinstance(data, dict) else fallback)
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
            series.main_character or "",
            series.bible_book or "",
            series.narrative_tone or "",
        ]
        tags = [t.strip() for t in tags if str(t or "").strip()]
        plan_scenes = []
        for scene in scenes:
            plan_scenes.append(
                {
                    "text": (scene.narration_text or "").strip(),
                    "image_prompt": (scene.prompt_image or scene.visual_description or "").strip(),
                    "caption": (scene.visual_description or scene.narration_text or "")[:160],
                }
            )
        plan = {
            "title": episode.youtube_title_suggestion or episode.title,
            "description": "\n".join(
                [
                    (episode.summary or "").strip(),
                    "",
                    (script.subscribe_cta or "").strip(),
                    (script.next_episode_cta or "").strip(),
                    "Narrativa inspirada em relato biblico." if script.disclaimer_required else "",
                ]
            ).strip(),
            "tags": tags[:15],
            "scenes": plan_scenes,
            "music_mood": "emotional_cinematic" if "suspense" in str(series.narrative_tone or "").lower() else "happy",
            "music_prompt": f"{series.narrative_tone or 'epic'} biblical anime cinematic soundtrack, dramatic but reverent",
            "target_duration_sec": int(script.desired_duration_minutes or episode.estimated_minutes or 5) * 60,
            "kind": "story",
            "allow_image_reuse": True,
            "bg_music_volume": 0.03,
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
        config = self.get_or_create_config(db, user_id)
        scenes = db.query(BibleVideoScene).filter(BibleVideoScene.script_id == script.id).count()
        costs = self._estimate_costs(config, script.desired_duration_minutes or 5, scenes or 8, shorts_count=0)
        title = episode.title if episode else (series.name if series else "Job Biblico")
        if job_type == "short":
            title = f"Short - {title}"
        job = BibleVideoJob(
            user_id=user_id,
            series_id=script.series_id,
            episode_id=script.episode_id,
            script_id=script.id,
            parent_job_id=parent_job_id,
            title=title,
            job_type=job_type,
            platform=(platform or "youtube").strip().lower(),
            aspect_ratio=(aspect_ratio or "16:9").strip(),
            kanban_stage="script_approved" if script.validation_status == "approved" else "script_generated",
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
        name = os.path.basename(str(job.output_video_url or "").strip())
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
            progress(15, "Gerando voz e trilha...", "voice_generated")

            video_service = VideoGenerator(ai_service=AIContentGenerator())
            result = video_service.create_video_from_plan(
                plan,
                aspect_ratio=job.aspect_ratio or "16:9",
                progress_callback=lambda p, m: progress(p, m, "video_editing" if int(p or 0) >= 80 else "video_animating"),
                voice_style="human",
                voice_gender="female" if (config.default_voice or "").lower() != "male" else "male",
            )

            output_video_url = ""
            if isinstance(result, dict):
                output_video_url = str(result.get("video_url") or "").strip()
            elif isinstance(result, str):
                output_video_url = result
            if not output_video_url:
                raise Exception("A producao terminou sem retornar video_url.")

            job.output_video_url = output_video_url
            job.result_json = self._json_dumps(result if isinstance(result, dict) else {"video_url": output_video_url})
            job.actual_cost = float(job.estimated_cost or 0)
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
        youtube_video_id = str(response.get("id") or "").strip()
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
