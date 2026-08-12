import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.database import Base
from app.models import EpisodeReview, ScheduledVideo, SeriesEpisode, SeriesPlan, UnifiedVideo, UnifiedVideoStatus, User, VideoTask, YouTubeAutoAuditEvent
from app.services.financial_guardian.youtube_observability import (
    youtube_financial_guardian_observability_service,
)
from app.services.task_manager import (
    acquire_distributed_lock,
    get_task,
    release_distributed_lock,
    request_cancel_task,
    update_task,
)
from app.services.video_resource_guard import (
    evaluate_series_video_resources,
    resource_guard_message,
)
from app.services.youtube_auto_identity import (
    build_video_content_fingerprint,
    normalize_list_for_fingerprint,
    normalize_text_for_fingerprint,
)


SERIES_STATUSES = {
    "draft",
    "planned",
    "active",
    "paused",
    "completed",
    "cancelled",
    "pending_issue",
}
EPISODE_STATUSES = {
    "planned",
    "awaiting_production",
    "in_production",
    "awaiting_review",
    "approved",
    "rejected",
    "in_correction",
    "scheduled",
    "published",
    "failed",
    "cancelled",
    "publication_blocked",
}
REVIEW_REASONS = {
    "script",
    "title",
    "description",
    "image",
    "thumbnail",
    "narration",
    "pronunciation",
    "subtitle",
    "sync",
    "duration",
    "opening",
    "ending",
    "next_hook",
    "repetitive_content",
    "other",
}

try:
    from app.services.unified_video_pipeline import (
        UnifiedVideoPipelineService as _UVP,
        UnifiedVideoRequest as _UReq,
        unified_video_pipeline as _unified_video_pipeline_factory,
    )
    _UVP_OK = True
except Exception:
    _UVP = None  # type: ignore[assignment,misc]
    _UReq = None  # type: ignore[assignment,misc]
    _unified_video_pipeline_factory = None  # type: ignore[assignment,misc]
    _UVP_OK = False


def _require_unified_pipeline(caller: str) -> Any:
    """Garante que UnifiedVideoPipeline está disponível. Fora de dev levanta RuntimeError."""
    if not _UVP_OK or _UVP is None or _unified_video_pipeline_factory is None or _UReq is None:
        try:
            from app.config import unified_pipeline_required_error as _req_err
            msg = _req_err(caller, None)
        except Exception:
            msg = (
                f"[{caller}] UnifiedVideoPipeline é OBRIGATÓRIO em Séries Programadas. "
                "Não há fallback legado. Verifique se app/services/unified_video_pipeline.py está importável."
            )
        raise RuntimeError(msg)
    uvpf = _unified_video_pipeline_factory()
    if uvpf is None:
        raise RuntimeError(f"[{caller}] UnifiedVideoPipeline factory retornou None.")
    return uvpf

#region debug-point youtube-finalize-stuck
_DEBUG_ENV_PATH = os.path.join(".dbg", "youtube-finalize-stuck.env")

def _dbg_event(hypothesis_id: str, msg: str, data: Optional[Dict[str, Any]] = None):
    try:
        import json as _json
        import urllib.request as _urlreq

        url = "http://127.0.0.1:7777/event"
        session_id = "youtube-finalize-stuck"
        if os.path.exists(_DEBUG_ENV_PATH):
            try:
                with open(_DEBUG_ENV_PATH, "r", encoding="utf-8") as f:
                    for line in f.read().splitlines():
                        if line.startswith("DEBUG_SERVER_URL="):
                            url = line.split("=", 1)[1].strip() or url
                        elif line.startswith("DEBUG_SESSION_ID="):
                            session_id = line.split("=", 1)[1].strip() or session_id
            except Exception:
                pass

        run_id = str(os.getenv("DEBUG_RUN_ID") or "pre").strip() or "pre"
        payload = {
            "sessionId": session_id,
            "runId": run_id,
            "hypothesisId": str(hypothesis_id or "").strip() or "NA",
            "location": "app/services/youtube_series_service.py",
            "msg": str(msg or ""),
            "data": data or {},
        }
        req = _urlreq.Request(
            url,
            data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        _urlreq.urlopen(req, timeout=0.25).read()
    except Exception:
        pass
#endregion


def _audit_event(
    db: Session,
    *,
    event_type: str,
    series_id: Optional[int] = None,
    episode_id: Optional[int] = None,
    task_id: Optional[str] = None,
    scheduled_video_id: Optional[int] = None,
    status_before: Optional[str] = None,
    status_after: Optional[str] = None,
    duration_ms: Optional[int] = None,
    payload: Optional[Dict[str, Any]] = None,
    error_stack: Optional[str] = None,
) -> None:
    try:
        row = YouTubeAutoAuditEvent(
            event_type=str(event_type or "").strip() or "unknown",
            series_id=int(series_id) if series_id is not None else None,
            episode_id=int(episode_id) if episode_id is not None else None,
            task_id=str(task_id)[:255] if task_id else None,
            scheduled_video_id=int(scheduled_video_id) if scheduled_video_id is not None else None,
            status_before=str(status_before)[:255] if status_before else None,
            status_after=str(status_after)[:255] if status_after else None,
            duration_ms=int(duration_ms) if duration_ms is not None else None,
            payload_json=_json_dumps(payload) if payload else None,
            error_stack=str(error_stack) if error_stack else None,
        )
        db.add(row)
    except Exception:
        pass
CONTENT_KIND_MAP = {
    "reflection": "story",
    "devotional": "devotional",
    "history": "story",
    "study": "devotional",
    "motivational": "story",
    "other": "story",
}


def _json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _get_tz(name: Optional[str]) -> ZoneInfo:
    raw = str(name or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(raw)
    except Exception:
        return ZoneInfo("UTC")


def _parse_date(date_text: str) -> datetime.date:
    return datetime.strptime(str(date_text).strip(), "%Y-%m-%d").date()


def _parse_time(time_text: str, default: str) -> Tuple[int, int]:
    raw = str(time_text or default).strip() or default
    dt = datetime.strptime(raw, "%H:%M")
    return dt.hour, dt.minute


def _dt_to_iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _series_status(raw: Any) -> str:
    value = str(raw or "draft").strip().lower() or "draft"
    return value if value in SERIES_STATUSES else "draft"


def _episode_status(raw: Any) -> str:
    value = str(raw or "planned").strip().lower() or "planned"
    return value if value in EPISODE_STATUSES else "planned"


def _normalize_reason_list(values: Any) -> List[str]:
    items = values if isinstance(values, list) else []
    normalized: List[str] = []
    for item in items:
        value = str(item or "").strip().lower()
        if value and value in REVIEW_REASONS and value not in normalized:
            normalized.append(value)
    return normalized


def _extract_scene_numbers_from_feedback(feedback: str) -> List[int]:
    raw = str(feedback or "").strip()
    if not raw:
        return []
    matches = re.findall(r"cenas?\s+((?:\d+\s*(?:,|e)?\s*)+)", raw, flags=re.IGNORECASE)
    scene_numbers: List[int] = []
    for chunk in matches:
        for value in re.findall(r"\d+", chunk):
            number = _safe_int(value, 0)
            if number > 0 and number not in scene_numbers:
                scene_numbers.append(number)
    return scene_numbers


def _existing_artifact_path(path_value: Any) -> str:
    raw = str(path_value or "").strip()
    if not raw:
        return ""
    candidates = [raw]
    if not os.path.isabs(raw):
        candidates.append(os.path.join(os.getcwd(), raw))
        candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), raw))
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


def _extract_scene_image_slots(render_report: Dict[str, Any]) -> List[str]:
    scene_visuals = render_report.get("scene_visuals") if isinstance(render_report, dict) else None
    if not isinstance(scene_visuals, list):
        return []
    slots: List[str] = []
    for item in scene_visuals:
        if not isinstance(item, dict):
            slots.append("")
            continue
        slots.append(_existing_artifact_path(item.get("image_path")))
    while slots and not str(slots[-1] or "").strip():
        slots.pop()
    return slots


def _build_canonical_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    mode = normalize_text_for_fingerprint(payload.get("mode") or "topic", lower=True) or "topic"
    kind = normalize_text_for_fingerprint(payload.get("kind") or "story", lower=True) or "story"
    image_mode = normalize_text_for_fingerprint(payload.get("image_mode") or "", lower=True)
    voice_style = normalize_text_for_fingerprint(payload.get("voice_style") or "", lower=True)
    voice_gender = normalize_text_for_fingerprint(payload.get("voice_gender") or "", lower=True)
    aspect_ratio = normalize_text_for_fingerprint(payload.get("aspect_ratio") or "16:9", lower=True) or "16:9"
    override_tags = normalize_list_for_fingerprint(payload.get("override_tags") or [], lower=True)
    selected_images = normalize_list_for_fingerprint(payload.get("selected_images") or [])
    custom_image_paths = normalize_list_for_fingerprint(payload.get("custom_image_paths") or [])
    content_identity = build_video_content_fingerprint(payload)
    return {
        "mode": mode,
        "kind": kind,
        "topic": normalize_text_for_fingerprint(payload.get("topic") or ""),
        "story_content": normalize_text_for_fingerprint(payload.get("story_content") or ""),
        "duration": max(1, min(60, int(payload.get("duration") or 5))),
        "aspect_ratio": aspect_ratio,
        "voice_style": voice_style,
        "voice_gender": voice_gender,
        "image_mode": image_mode,
        "thumbnail_path": normalize_text_for_fingerprint(payload.get("thumbnail_path") or ""),
        "override_title": normalize_text_for_fingerprint(payload.get("override_title") or ""),
        "override_description": normalize_text_for_fingerprint(payload.get("override_description") or ""),
        "override_tags": override_tags,
        "selected_images": selected_images,
        "custom_image_paths": custom_image_paths,
        "content_fingerprint": content_identity["content_fingerprint"],
        "internal_title": content_identity["internal_title"],
        "youtube_title": content_identity["youtube_title"],
        "narrated_title": content_identity["narrated_title"],
    }


def _build_identity(payload: Dict[str, Any]) -> Dict[str, Any]:
    canonical = _build_canonical_payload(payload)
    canonical_json = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    content_identity = build_video_content_fingerprint(payload)
    return {
        "canonical_json": canonical_json,
        "request_hash": request_hash,
        "idempotency_key": f"ytv1:{request_hash}",
        "content_fingerprint": content_identity["content_fingerprint"],
        "internal_title": content_identity["internal_title"],
        "youtube_title": content_identity["youtube_title"],
        "narrated_title": content_identity["narrated_title"],
    }


class YouTubeSeriesService:
    def ensure_schema(self, db: Session) -> None:
        Base.metadata.create_all(bind=db.get_bind())

    def _count_total_episodes(self, start_date: str, end_date: str) -> int:
        start = _parse_date(start_date)
        end = _parse_date(end_date)
        if end < start:
            raise ValueError("A data final deve ser igual ou posterior à data inicial.")
        return ((end - start).days) + 1

    def _build_publication_datetime(self, date_text: str, time_text: str, timezone_name: str) -> datetime:
        base_date = _parse_date(date_text)
        hour, minute = _parse_time(time_text, "19:00")
        tz = _get_tz(timezone_name)
        local_dt = datetime(base_date.year, base_date.month, base_date.day, hour, minute, tzinfo=tz)
        return local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    def _build_production_datetime(
        self,
        publication_dt_utc: datetime,
        timezone_name: str,
        lead_days: int,
        production_time: str,
    ) -> datetime:
        tz = _get_tz(timezone_name)
        publication_local = publication_dt_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        target_day = publication_local.date() - timedelta(days=max(0, int(lead_days or 0)))
        hour, minute = _parse_time(production_time, "06:00")
        local_dt = datetime(target_day.year, target_day.month, target_day.day, hour, minute, tzinfo=tz)
        return local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    def _build_editorial_plan(self, payload: Dict[str, Any], total_episodes: int) -> List[Dict[str, Any]]:
        theme = str(payload.get("main_theme") or payload.get("theme") or "Tema").strip()
        objective = str(payload.get("objective") or "").strip()
        content_type = str(payload.get("content_type") or "reflection").strip().lower()
        target = str(payload.get("target_audience") or "").strip()
        base_steps = [
            "Quando o desafio aparece",
            "Reconhecendo o conflito interior",
            "O primeiro passo de transformação",
            "Fortalecendo a confiança na caminhada",
            "Superando a repetição do medo",
            "Transformando fé em atitude",
            "Consolidando uma nova rotina",
            "Mantendo constância nos dias difíceis",
            "Atravessando o silêncio sem desistir",
            "Colhendo a mudança construída",
        ]
        thematic_prefix = {
            "devotional": "Devocional",
            "study": "Estudo",
            "history": "História",
            "motivational": "Motivação",
        }.get(content_type, "Reflexão")
        plan: List[Dict[str, Any]] = []
        for index in range(total_episodes):
            step_title = base_steps[index] if index < len(base_steps) else f"Próximo passo {index + 1}"
            title = f"{thematic_prefix} {index + 1} — {step_title}"
            summary = (
                f"Episódio {index + 1} da série sobre {theme}. "
                f"Aborda {step_title.lower()} com progressão narrativa e ligação com o próximo episódio."
            ).strip()
            if objective:
                summary += f" Objetivo editorial: {objective}."
            if target:
                summary += f" Público prioritário: {target}."
            previous_hook = (
                f"Cumprir a promessa aberta no episódio {index}."
                if index > 0
                else "Apresentar a jornada e abrir a expectativa para os próximos passos."
            )
            next_hook = (
                "Concluir a jornada, agradecer a audiência e convidar o público para a próxima série."
                if index == total_episodes - 1
                else f"Amanhã, no próximo episódio, vamos avançar para: {base_steps[index + 1] if index + 1 < len(base_steps) else f'Próximo passo {index + 2}' }."
            )
            plan.append({
                "episode_number": index + 1,
                "planned_title": title,
                "summary": summary,
                "previous_episode_hook": previous_hook,
                "next_episode_hook": next_hook,
            })
        return plan

    def _series_memory(self, series: SeriesPlan) -> Dict[str, Any]:
        return _json_loads(series.editorial_memory_json, {
            "theme_summary": series.main_theme,
            "published_episodes": [],
            "produced_episodes": [],
            "ideas_used": [],
            "references_used": [],
            "last_promise": None,
            "next_planned_hook": None,
            "narrative_progress": 0,
        })

    def _save_series_memory(self, series: SeriesPlan, memory: Dict[str, Any]) -> None:
        series.editorial_memory_json = _json_dumps(memory)

    def create_series(self, db: Session, *, user: User, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.ensure_schema(db)
        name = str(payload.get("name") or payload.get("internal_name") or "").strip()
        main_theme = str(payload.get("main_theme") or "").strip()
        if not name or not main_theme:
            raise ValueError("Nome da série e tema principal são obrigatórios.")
        start_date = str(payload.get("start_date") or "").strip()
        end_date = str(payload.get("end_date") or "").strip()
        total_episodes = self._count_total_episodes(start_date, end_date)
        timezone_name = str(payload.get("timezone") or "UTC").strip() or "UTC"
        publication_time = str(payload.get("publication_time") or "19:00").strip() or "19:00"
        production_time = str(payload.get("production_time") or "06:00").strip() or "06:00"
        production_lead_days = max(0, min(3, int(payload.get("production_lead_days") or 0)))
        status = _series_status(payload.get("status") or "draft")
        editorial_plan = payload.get("editorial_plan")
        if not isinstance(editorial_plan, list) or not editorial_plan:
            editorial_plan = self._build_editorial_plan(payload, total_episodes)
        series = SeriesPlan(
            user_id=int(user.id),
            channel_id=str(payload.get("channel_id") or "").strip() or None,
            name=name,
            main_theme=main_theme,
            objective=str(payload.get("objective") or "").strip() or None,
            target_audience=str(payload.get("target_audience") or "").strip() or None,
            content_type=str(payload.get("content_type") or "reflection").strip().lower() or "reflection",
            start_date=self._build_publication_datetime(start_date, "00:00", timezone_name),
            end_date=self._build_publication_datetime(end_date, "23:59", timezone_name),
            publication_time=publication_time,
            timezone=timezone_name,
            production_lead_days=production_lead_days,
            production_time=production_time,
            duration_minutes=max(1, min(60, int(payload.get("duration_minutes") or 10))),
            visibility=str(payload.get("visibility") or "unlisted").strip().lower() or "unlisted",
            tone=str(payload.get("tone") or "").strip() or None,
            narration_style=str(payload.get("narration_style") or "").strip() or None,
            continuity_level=str(payload.get("continuity_level") or "medium").strip().lower() or "medium",
            hook_intensity=str(payload.get("hook_intensity") or "medium").strip().lower() or "medium",
            use_biblical_references=bool(payload.get("use_biblical_references", True)),
            cta_subscribe=bool(payload.get("cta_subscribe", True)),
            cta_next_episode=bool(payload.get("cta_next_episode", True)),
            auto_approval=bool(payload.get("auto_approval", False)),
            status=status,
            total_episodes=total_episodes,
            current_episode=0,
            editorial_plan_json=_json_dumps(editorial_plan),
            editorial_memory_json=_json_dumps({
                "theme_summary": main_theme,
                "plan_total": total_episodes,
                "published_episodes": [],
                "produced_episodes": [],
                "ideas_used": [],
                "references_used": [],
                "last_promise": None,
                "next_planned_hook": editorial_plan[0].get("next_episode_hook") if editorial_plan else None,
                "narrative_progress": 0,
                "idempotency_key": str(payload.get("idempotency_key") or "").strip() or None,
                "series_idempotency_key": str(payload.get("idempotency_key") or "").strip() or None,
                "requested_title": str(payload.get("title") or payload.get("name") or "").strip() or None,
            }),
        )
        db.add(series)
        db.flush()

        start = _parse_date(start_date)
        for item in editorial_plan[:total_episodes]:
            number = _safe_int(item.get("episode_number"), 0) or (len(series.episodes) + 1)
            publication_dt = None
            if str(item.get("publication_datetime") or "").strip():
                try:
                    publication_dt = datetime.fromisoformat(str(item.get("publication_datetime")).replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    publication_dt = None
            if publication_dt is None:
                day = start + timedelta(days=number - 1)
                publication_dt = self._build_publication_datetime(day.isoformat(), publication_time, timezone_name)
            production_dt = None
            if str(item.get("production_datetime") or "").strip():
                try:
                    production_dt = datetime.fromisoformat(str(item.get("production_datetime")).replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    production_dt = None
            if production_dt is None:
                production_dt = self._build_production_datetime(publication_dt, timezone_name, production_lead_days, production_time)
            episode_status = "awaiting_production" if status == "active" else "planned"
            db.add(SeriesEpisode(
                series_id=series.id,
                episode_number=number,
                planned_title=str(item.get("planned_title") or f"Episódio {number}").strip(),
                narrated_title=str(item.get("narrated_title") or item.get("planned_title") or f"Episódio {number}").strip(),
                summary=str(item.get("summary") or "").strip() or None,
                previous_episode_hook=str(item.get("previous_episode_hook") or "").strip() or None,
                next_episode_hook=str(item.get("next_episode_hook") or "").strip() or None,
                publication_datetime=publication_dt,
                production_datetime=production_dt,
                duration_minutes=max(1, min(60, _safe_int(item.get("duration_minutes"), int(series.duration_minutes or 10)))),
                status=episode_status,
                metadata_json=_json_dumps({"editorial_plan_item": item}),
            ))
        db.commit()
        db.refresh(series)
        return self.get_series_detail(db, user=user, series_id=series.id)

    def _episode_planned_cost(self, series: SeriesPlan, episode: SeriesEpisode) -> Dict[str, Any]:
        estimate = youtube_financial_guardian_observability_service.estimate_preproduction(
            user=User(id=series.user_id),  # type: ignore[arg-type]
            payload={"topic": self._episode_topic_prompt(series, episode, correction_feedback=None)},
        )
        return estimate if isinstance(estimate, dict) else {}

    def _serialize_episode(self, db: Session, series: SeriesPlan, episode: SeriesEpisode) -> Dict[str, Any]:
        task = get_task(str(episode.task_id)) if episode.task_id else None
        task_result = task.get("result") if isinstance(task, dict) and isinstance(task.get("result"), dict) else {}
        review_rows = (
            db.query(EpisodeReview)
            .filter(EpisodeReview.episode_id == int(episode.id))
            .order_by(EpisodeReview.version.desc(), EpisodeReview.reviewed_at.desc())
            .all()
        )
        latest_review = review_rows[0] if review_rows else None
        approved_snapshot = _json_loads(episode.approved_snapshot_json, {})
        episode_metadata = _json_loads(episode.metadata_json, {})
        if not isinstance(episode_metadata, dict):
            episode_metadata = {}
        cost_control = task_result.get("cost_control") if isinstance(task_result.get("cost_control"), dict) else {}
        estimated_cost = _safe_float(cost_control.get("estimated_cost"), 0.0)
        actual_cost = _safe_float(cost_control.get("actual_cost"), 0.0)
        planned_estimate = self._episode_planned_cost(series, episode)
        watch_url = f"/youtube/task/{episode.task_id}/watch" if episode.task_id and str((task or {}).get('status') or '').lower() == "completed" else None
        narrated_text = ""
        script = task_result.get("script") if isinstance(task_result.get("script"), dict) else {}
        if isinstance(script.get("scenes"), list):
            narrated_text = "\n\n".join(
                str(scene.get("text") or "").strip()
                for scene in script.get("scenes")
                if isinstance(scene, dict) and str(scene.get("text") or "").strip()
            )[:12000]
        return {
            "id": int(episode.id),
            "episode_number": int(episode.episode_number),
            "planned_title": episode.planned_title,
            "narrated_title": episode.narrated_title,
            "summary": episode.summary,
            "previous_episode_hook": episode.previous_episode_hook,
            "next_episode_hook": episode.next_episode_hook,
            "publication_datetime": _dt_to_iso(episode.publication_datetime),
            "production_datetime": _dt_to_iso(episode.production_datetime),
            "status": _episode_status(episode.status),
            "task_id": episode.task_id,
            "scheduled_video_id": episode.scheduled_video_id,
            "content_fingerprint": episode.content_fingerprint,
            "approved_at": _dt_to_iso(episode.approved_at),
            "published_at": _dt_to_iso(episode.published_at),
            "youtube_video_id": episode.youtube_video_id,
            "youtube_url": episode.youtube_url,
            "current_version": int(episode.current_version or 1),
            "watch_url": watch_url,
            "review_deadline_status": self._deadline_status(episode),
            "costs": {
                "planned_estimated": _safe_float(planned_estimate.get("estimated_cost"), 0.0),
                "actual": actual_cost,
                "estimated": estimated_cost,
                "savings": round(max(0.0, estimated_cost - actual_cost), 4),
            },
            "review": {
                "latest_decision": latest_review.decision if latest_review else None,
                "latest_feedback": latest_review.feedback if latest_review else None,
                "history_total": len(review_rows),
            },
            "approval_snapshot": approved_snapshot,
            "task_status": str((task or {}).get("status") or "").lower() if task else None,
            "task_error": str((task or {}).get("message") or "").strip() or None,
            "provider_error": task_result.get("provider_error") if isinstance(task_result.get("provider_error"), dict) else None,
            "resource_guard": episode_metadata.get("resource_guard")
            if isinstance(episode_metadata.get("resource_guard"), dict)
            else None,
            "video_preview": {
                "title": task_result.get("title") or episode.planned_title,
                "description": task_result.get("description"),
                "thumbnail_path": task_result.get("thumbnail_path"),
                "narrated_text": narrated_text,
                "subtitles_path": ((task_result.get("render_report") or {}).get("official_audio_transcription") or {}).get("srt_path")
                if isinstance(task_result.get("render_report"), dict)
                else None,
                "video_url": task_result.get("video_url"),
            },
        }

    def _deadline_status(self, episode: SeriesEpisode) -> Dict[str, Any]:
        now = datetime.utcnow()
        remaining = episode.publication_datetime - now
        seconds = int(remaining.total_seconds())
        if seconds <= 0:
            color = "red"
        elif seconds <= 6 * 60 * 60:
            color = "yellow"
        else:
            color = "green"
        return {"seconds_remaining": seconds, "color": color}

    def _series_progress(self, episodes: List[SeriesEpisode]) -> Dict[str, Any]:
        total = len(episodes)
        published = len([ep for ep in episodes if _episode_status(ep.status) == "published"])
        approved = len([ep for ep in episodes if _episode_status(ep.status) in {"approved", "scheduled", "published"}])
        current = max(
            [
                int(ep.episode_number or 0)
                for ep in episodes
                if _episode_status(ep.status) not in {"planned", "awaiting_production"}
            ] or [0]
        )
        return {"published": published, "approved": approved, "current_episode": current, "total": total}

    def get_series_detail(self, db: Session, *, user: User, series_id: int) -> Dict[str, Any]:
        self.ensure_schema(db)
        series = db.query(SeriesPlan).filter(SeriesPlan.id == int(series_id), SeriesPlan.user_id == int(user.id)).first()
        if not series:
            raise ValueError("Série não encontrada.")
        episodes = (
            db.query(SeriesEpisode)
            .filter(SeriesEpisode.series_id == int(series.id))
            .order_by(SeriesEpisode.episode_number.asc())
            .all()
        )
        progress = self._series_progress(episodes)
        next_episode = next((ep for ep in episodes if _episode_status(ep.status) not in {"published", "cancelled"}), None)
        sm = self._series_memory(series)
        return {
            "id": int(series.id),
            "name": series.name,
            "main_theme": series.main_theme,
            "objective": series.objective,
            "target_audience": series.target_audience,
            "content_type": series.content_type,
            "status": _series_status(series.status),
            "start_date": _dt_to_iso(series.start_date),
            "end_date": _dt_to_iso(series.end_date),
            "publication_time": series.publication_time,
            "production_time": series.production_time,
            "timezone": series.timezone,
            "production_lead_days": int(series.production_lead_days or 0),
            "duration_minutes": int(series.duration_minutes or 10),
            "visibility": series.visibility,
            "tone": series.tone,
            "narration_style": series.narration_style,
            "continuity_level": series.continuity_level,
            "hook_intensity": series.hook_intensity,
            "use_biblical_references": bool(series.use_biblical_references),
            "cta_subscribe": bool(series.cta_subscribe),
            "cta_next_episode": bool(series.cta_next_episode),
            "auto_approval": bool(series.auto_approval),
            "total_episodes": int(series.total_episodes or len(episodes)),
            "current_episode": int(progress["current_episode"]),
            "progress_label": f"{progress['current_episode']} de {progress['total']} episódios",
            "idempotency_key": str(sm.get("idempotency_key") or sm.get("series_idempotency_key") or "").strip() or None,
            "editorial_plan": _json_loads(series.editorial_plan_json, []),
            "editorial_memory": sm,
            "episodes": [self._serialize_episode(db, series, ep) for ep in episodes],
            "next_episode": {
                "episode_number": int(next_episode.episode_number),
                "planned_title": next_episode.planned_title,
                "status": _episode_status(next_episode.status),
            } if next_episode else None,
            "costs": self._series_costs(db, series, episodes),
        }

    def _series_costs(self, db: Session, series: SeriesPlan, episodes: List[SeriesEpisode]) -> Dict[str, Any]:
        planned = 0.0
        actual = 0.0
        estimated = 0.0
        for ep in episodes:
            estimate = self._episode_planned_cost(series, ep)
            planned += _safe_float(estimate.get("estimated_cost"), 0.0)
            task = get_task(str(ep.task_id)) if ep.task_id else None
            result = task.get("result") if isinstance(task, dict) and isinstance(task.get("result"), dict) else {}
            cost = result.get("cost_control") if isinstance(result.get("cost_control"), dict) else {}
            estimated += _safe_float(cost.get("estimated_cost"), 0.0)
            actual += _safe_float(cost.get("actual_cost"), 0.0)
        total = max(1, len(episodes))
        return {
            "planned_total": round(planned, 4),
            "actual_total": round(actual, 4),
            "estimated_total": round(estimated, 4),
            "average_per_episode": round(actual / total, 4),
            "savings_total": round(max(0.0, estimated - actual), 4),
        }

    def list_series(self, db: Session, *, user: User) -> Dict[str, Any]:
        self.ensure_schema(db)
        rows = (
            db.query(SeriesPlan)
            .filter(SeriesPlan.user_id == int(user.id), SeriesPlan.archived_at.is_(None))
            .order_by(SeriesPlan.updated_at.desc(), SeriesPlan.id.desc())
            .all()
        )
        items: List[Dict[str, Any]] = []
        for series in rows:
            episodes = (
                db.query(SeriesEpisode)
                .filter(SeriesEpisode.series_id == int(series.id))
                .order_by(SeriesEpisode.episode_number.asc())
                .all()
            )
            progress = self._series_progress(episodes)
            next_episode = next((ep for ep in episodes if _episode_status(ep.status) not in {"published", "cancelled"}), None)
            items.append({
                "id": int(series.id),
                "name": series.name,
                "main_theme": series.main_theme,
                "start_date": _dt_to_iso(series.start_date),
                "end_date": _dt_to_iso(series.end_date),
                "publication_time": series.publication_time,
                "status": _series_status(series.status),
                "progress": f"{progress['current_episode']}/{progress['total']}",
                "progress_label": f"{progress['current_episode']} de {progress['total']} episódios",
                "next_episode_status": _episode_status(next_episode.status) if next_episode else "completed",
                "next_episode_title": next_episode.planned_title if next_episode else None,
            })
        return {"items": items, "count": len(items)}

    def update_series_status(self, db: Session, *, user: User, series_id: int, status: str) -> Dict[str, Any]:
        series = db.query(SeriesPlan).filter(SeriesPlan.id == int(series_id), SeriesPlan.user_id == int(user.id)).first()
        if not series:
            raise ValueError("Série não encontrada.")
        target_status = _series_status(status)
        episodes = db.query(SeriesEpisode).filter(SeriesEpisode.series_id == int(series.id)).all()
        cancelled_task_ids: List[str] = []

        if target_status == "cancelled":
            # Arquivar é terminal: interrompe tarefas ainda vinculadas e impede
            # que o scheduler volte a produzir ou publicar qualquer episódio.
            for episode in episodes:
                episode_status = _episode_status(episode.status)
                if episode_status == "published":
                    continue
                task_id = str(episode.task_id or "").strip()
                if task_id:
                    request_cancel_task(
                        task_id,
                        message="Cancelado pelo usuário ao arquivar a série.",
                    )
                    cancelled_task_ids.append(task_id)
                episode.status = "cancelled"
                if episode.scheduled_video_id:
                    scheduled = db.query(ScheduledVideo).filter(
                        ScheduledVideo.id == int(episode.scheduled_video_id)
                    ).first()
                    if scheduled and str(scheduled.status or "").strip().lower() not in {"published", "completed"}:
                        scheduled.status = "failed"
                        scheduled.progress = 0
                        scheduled.description = (
                            ((scheduled.description or "") + "\n\n[CANCELADO]: Série arquivada pelo usuário.")
                            .strip()[:5000]
                        )
            series.status = "cancelled"
            series.archived_at = datetime.utcnow()
        else:
            series.status = target_status
            if target_status == "active":
                series.archived_at = None
                for episode in episodes:
                    if _episode_status(episode.status) == "planned":
                        episode.status = "awaiting_production"
        db.commit()
        detail = self.get_series_detail(db, user=user, series_id=series.id)
        if target_status == "cancelled":
            detail["archived"] = True
            detail["cancelled_task_ids"] = sorted(set(cancelled_task_ids))
        return detail

    def pause_for_server_shutdown(self, db: Session, *, cancelled_task_ids: List[str]) -> Dict[str, int]:
        """Pausa séries ativas e encerra episódios ligados às tarefas paradas."""
        task_ids = {
            str(task_id).strip()
            for task_id in (cancelled_task_ids or [])
            if str(task_id).strip()
        }
        series_rows = (
            db.query(SeriesPlan)
            .filter(
                SeriesPlan.archived_at.is_(None),
                SeriesPlan.status.in_(["active", "pending_issue"]),
            )
            .all()
        )
        paused_series = 0
        cancelled_episodes = 0
        for series in series_rows:
            series.status = "paused"
            paused_series += 1
            episodes = db.query(SeriesEpisode).filter(SeriesEpisode.series_id == int(series.id)).all()
            for episode in episodes:
                episode_status = _episode_status(episode.status)
                linked_task_id = str(episode.task_id or "").strip()
                if episode_status == "in_production" or (linked_task_id and linked_task_id in task_ids):
                    if episode_status not in {"published", "cancelled"}:
                        episode.status = "cancelled"
                        cancelled_episodes += 1
        db.commit()
        return {
            "paused_series": paused_series,
            "cancelled_series_episodes": cancelled_episodes,
        }

    def update_episode_plan(
        self,
        db: Session,
        *,
        user: User,
        episode_id: int,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        episode = (
            db.query(SeriesEpisode)
            .join(SeriesPlan, SeriesPlan.id == SeriesEpisode.series_id)
            .filter(SeriesEpisode.id == int(episode_id), SeriesPlan.user_id == int(user.id))
            .first()
        )
        if not episode:
            raise ValueError("Episódio não encontrado.")
        series = db.query(SeriesPlan).filter(SeriesPlan.id == int(episode.series_id)).first()
        if not series:
            raise ValueError("Série não encontrada.")
        if "planned_title" in payload:
            episode.planned_title = str(payload.get("planned_title") or "").strip() or episode.planned_title
        if "narrated_title" in payload:
            episode.narrated_title = str(payload.get("narrated_title") or "").strip() or episode.narrated_title
        if "summary" in payload:
            episode.summary = str(payload.get("summary") or "").strip() or None
        if "publication_datetime" in payload and str(payload.get("publication_datetime") or "").strip():
            episode.publication_datetime = datetime.fromisoformat(str(payload.get("publication_datetime")).replace("Z", "+00:00")).replace(tzinfo=None)
            episode.production_datetime = self._build_production_datetime(
                episode.publication_datetime,
                series.timezone,
                int(series.production_lead_days or 1),
                series.production_time,
            )
        if "duration_minutes" in payload:
            episode.duration_minutes = max(1, min(60, _safe_int(payload.get("duration_minutes"), int(episode.duration_minutes or series.duration_minutes or 10))))
        db.commit()
        return self.get_series_detail(db, user=user, series_id=series.id)

    def _episode_topic_prompt(
        self,
        series: SeriesPlan,
        episode: SeriesEpisode,
        correction_feedback: Optional[str],
    ) -> str:
        memory = self._series_memory(series)
        previous_promise = str(memory.get("last_promise") or episode.previous_episode_hook or "").strip()
        next_hook = str(episode.next_episode_hook or "").strip()
        script = (
            f"Série: {series.name}. Tema central: {series.main_theme}. "
            f"Episódio {episode.episode_number}/{series.total_episodes}: {episode.planned_title}. "
            f"Resumo do episódio: {episode.summary or ''}. "
            f"Promessa anterior a cumprir: {previous_promise or 'Apresente a jornada da série.'} "
            f"Gancho planejado para o próximo: {next_hook or 'Prepare o próximo passo sem repetir a mesma chamada.'} "
            f"Objetivo da série: {series.objective or ''}. Público: {series.target_audience or ''}. "
            f"Tom: {series.tone or ''}. Continuidade: {series.continuity_level or ''}. "
            f"{'Use referências bíblicas com naturalidade. ' if series.use_biblical_references else ''}"
            f"{'Inclua chamada discreta para inscrição. ' if series.cta_subscribe else ''}"
            f"{'Finalize com convite para o próximo episódio. ' if series.cta_next_episode and episode.episode_number < int(series.total_episodes or 0) else 'Conclua a série sem prometer continuação inexistente. '}"
            f"{'Correção editorial obrigatória: ' + correction_feedback if correction_feedback else ''}"
        ).strip()
        return script

    def _episode_planned_cost(self, series: SeriesPlan, episode: SeriesEpisode) -> Dict[str, Any]:
        estimate = youtube_financial_guardian_observability_service.estimate_preproduction(
            user=User(id=series.user_id),  # type: ignore[arg-type]
            payload={"topic": self._episode_topic_prompt(series, episode, correction_feedback=None)},
        )
        return estimate if isinstance(estimate, dict) else {}

    def _find_unified_video_by_episode(self, db: Session, episode: SeriesEpisode) -> Optional[UnifiedVideo]:
        rows = (
            db.query(UnifiedVideo)
            .filter(
                UnifiedVideo.source_module == "youtube_series",
                UnifiedVideo.source_id == f"episode:{int(episode.id)}",
            )
            .order_by(UnifiedVideo.id.desc())
            .limit(1)
            .all()
        )
        if rows:
            return rows[0]
        if getattr(episode, "task_id", None):
            uv = db.query(UnifiedVideo).filter(UnifiedVideo.task_id == str(episode.task_id)).first()
            if uv is not None:
                return uv
        return None

    def _enqueue_episode_generation(
        self,
        db: Session,
        *,
        user: User,
        series: SeriesPlan,
        episode: SeriesEpisode,
        correction_feedback: Optional[str] = None,
        initial_result: Optional[Dict[str, Any]] = None,
        force_regenerate: bool = False,
        series_idempotency_key: Optional[str] = None,
        selected_images: Optional[List[str]] = None,
        reuse_audio_from: Optional[Dict[str, Any]] = None,
        seeded_script: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """SÉRIES: MONTAR UnifiedVideoRequest + CHAMAR UnifiedVideoPipeline SOMENTE.

        - NÃO gera roteiro, imagens, áudio ou renderização.
        - NÃO faz claim_video_task legado.
        - NÃO usa initial_result legado; os únicos seeds aceitos são os campos do
          UnifiedVideoRequest (seeded_script, selected_images, reuse_audio_from, force_regenerate).
        """
        uvpsvc = _require_unified_pipeline("_enqueue_episode_generation")

        uv_old = self._find_unified_video_by_episode(db, episode)
        version_token = f"v{int(episode.current_version or 1)}"
        try:
            _series_sid_key = str(series_idempotency_key or "").strip()
            if _series_sid_key and len(_series_sid_key) < 4:
                _series_sid_key = ""
        except Exception:
            _series_sid_key = ""
        base_ik = _series_sid_key or (
            f"yts:series:{int(series.id)}:episode:{int(episode.episode_number)}"
        )
        explicit_ik = str((initial_result or {}).get("idempotency_key") if isinstance(initial_result, dict) else "").strip()
        _explicit_ik_valid = bool(explicit_ik) and len(explicit_ik) >= 6
        effective_ik = (explicit_ik if _explicit_ik_valid else None) or f"{base_ik}:{version_token}"
        if len(str(effective_ik).strip()) < 6:
            effective_ik = f"yts:series:{int(series.id)}:episode:{int(episode.episode_number)}:{version_token}"
        topic_text = self._episode_topic_prompt(series, episode, correction_feedback)
        identity_payload_for_hash: Dict[str, Any] = {
            "topic": topic_text,
            "correction": str(correction_feedback or "").strip(),
            "override_title": str(episode.planned_title or "").strip(),
            "override_description": str(getattr(episode, "planned_description", "") or "").strip()[:5000],
            "aspect_ratio": str(getattr(series, "aspect_ratio", "") or "16:9"),
            "duration_minutes": int(episode.duration_minutes or series.duration_minutes or 10),
            "content_type": CONTENT_KIND_MAP.get(str(series.content_type or "").strip().lower(), "devotional"),
            "voice_style": str(series.narration_style or "human").strip()[:128],
            "auto_publish": bool(getattr(series, "auto_approval", False)),
            "version": version_token,
        }
        identity = _build_identity(identity_payload_for_hash)
        content_fp = identity["content_fingerprint"]

        if _UReq is None:
            raise RuntimeError("[_enqueue_episode_generation] UnifiedVideoRequest não carregado.")
        kind = CONTENT_KIND_MAP.get(str(series.content_type or "").strip().lower(), "devotional")
        req = _UReq(
            source_module="youtube_series",
            source_id=f"episode:{int(episode.id)}",
            idempotency_key=str(effective_ik).strip()[:255],
            content_type=kind,
            topic=str(topic_text).strip()[:4000] or None,
            script_text=None,
            duration_minutes=int(episode.duration_minutes or series.duration_minutes or 10),
            aspect_ratio=str(getattr(series, "aspect_ratio", "") or "16:9").strip()[:12] or "16:9",
            image_count=8,
            text_provider="configured",
            image_provider="configured",
            voice_provider="configured",
            voice_id=(str(series.narration_style or "human").strip()[:128] or None),
            music_enabled=False,
            visibility=str(getattr(series, "visibility", "") or "unlisted").strip().lower()[:32] or "unlisted",
            auto_publish=bool(getattr(series, "auto_approval", False) or getattr(series, "auto_publish", False)),
            review_required=bool(not getattr(series, "auto_approval", False)),
            user_id=int(user.id or 0) or None,
            force_regenerate=bool(force_regenerate),
            force_reuse_assets=False,
            override_title=(str(episode.planned_title or "").strip()[:300] or None),
            override_description=(str(getattr(episode, "planned_description", "") or "").strip()[:5000] or None),
            override_tags=None,
            seeded_script=(seeded_script if isinstance(seeded_script, dict) else None),
            selected_images=([str(x).strip() for x in selected_images if str(x).strip()] if isinstance(selected_images, list) else None),
            reuse_audio_from=(reuse_audio_from if isinstance(reuse_audio_from, dict) else None),
            request_hash=str(identity["request_hash"]).strip() or None,
            legacy_payload={
                "series_context": {
                    "series_id": int(series.id),
                    "series_name": series.name,
                    "episode_id": int(episode.id),
                    "episode_number": int(episode.episode_number),
                    "publication_datetime": _dt_to_iso(episode.publication_datetime),
                },
                "correction_feedback": str(correction_feedback or "").strip()[:5000] or None,
            },
        )

        kick_cb = None
        try:
            from app.routers.youtube import _kick_story_video_task_queue_async as _kick  # type: ignore[attr-defined]
            if callable(_kick):
                kick_cb = _kick
        except Exception:
            kick_cb = None

        uvpsvc.ensure_schema(db)
        res = uvpsvc.submit_or_reuse(
            db,
            request=req,
            kick_queue_callback=kick_cb,
            legacy_initial_result=None,
            user=user,
        )
        task_id = str(res.task_id)
        update_task(task_id, result={
            **(((get_task(task_id) or {}).get("result") if isinstance((get_task(task_id) or {}).get("result"), dict) else {}) or {}),
            "series_context": {
                "series_id": int(series.id),
                "series_name": series.name,
                "episode_id": int(episode.id),
                "episode_number": int(episode.episode_number),
                "publication_datetime": _dt_to_iso(episode.publication_datetime),
                "idempotency_key": effective_ik,
                "pipeline": "unified_video_pipeline",
                "unified_video_id": getattr(res, "unified_video_id", None),
                "version": version_token,
            },
            "content_fingerprint": content_fp,
        })
        episode.task_id = task_id
        episode.content_fingerprint = content_fp
        if getattr(res, "unified_video_id", None):
            try:
                uv_after = db.query(UnifiedVideo).filter(UnifiedVideo.id == int(res.unified_video_id)).first()
                if uv_after is not None and getattr(uv_after, "episode_id", None) in (None, 0):
                    try:
                        uv_after.episode_id = int(episode.id)
                    except Exception:
                        pass
            except Exception:
                pass
        tstatus = str((get_task(task_id) or {}).get("status") or "").lower()
        if res.reused_completed:
            episode.status = "awaiting_review"
        elif tstatus in {"pending", "processing", "queued"}:
            episode.status = "in_production"
        else:
            episode.status = "in_production"
        return {
            "task_id": task_id,
            "content_fingerprint": content_fp,
            "idempotency_key": effective_ik,
            "reused_existing_task": bool(res.reused_existing),
            "reused_completed_task": bool(res.reused_completed),
            "unified_video_id": getattr(res, "unified_video_id", None),
            "pipeline": "unified_video_pipeline",
            "video_url": getattr(res, "video_url", None),
            "youtube_video_id": getattr(res, "youtube_video_id", None),
            "providers": getattr(res, "providers", None),
        }

    def sync_series_scheduler(self, db: Session, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        self.ensure_schema(db)
        current = now or datetime.utcnow()
        activated = 0
        blocked = 0
        synced = 0
        shutdown_blocked = 0
        resource_blocked = 0
        shutdown_in_progress = False
        try:
            # Import tardio evita ciclo de importação. O scheduler continua
            # sincronizando estados existentes, mas não cria trabalho novo
            # enquanto o encerramento global está tirando o snapshot da fila.
            from app.routers.youtube import _cancel_all_active

            shutdown_in_progress = bool(_cancel_all_active())
        except Exception:
            shutdown_in_progress = False
        auto_approve_queue: List[Tuple[int, int]] = []
        rows = (
            db.query(SeriesPlan)
            .filter(SeriesPlan.status.in_(["active", "planned", "pending_issue"]))
            .order_by(SeriesPlan.id.asc())
            .all()
        )
        for series in rows:
            user = db.query(User).filter(User.id == int(series.user_id)).first()
            if not user:
                continue
            episodes = (
                db.query(SeriesEpisode)
                .filter(SeriesEpisode.series_id == int(series.id))
                .order_by(SeriesEpisode.episode_number.asc())
                .all()
            )
            for episode in episodes:
                status = _episode_status(episode.status)
                if status == "approved" and episode.scheduled_video_id:
                    before = str(episode.status)
                    episode.status = "scheduled"
                    synced += 1
                    _audit_event(
                        db,
                        event_type="episode_status_changed",
                        series_id=int(series.id),
                        episode_id=int(episode.id),
                        task_id=str(episode.task_id) if episode.task_id else None,
                        scheduled_video_id=int(episode.scheduled_video_id),
                        status_before=before,
                        status_after="scheduled",
                    )
                    status = "scheduled"
                if status in {"approved", "scheduled"} and episode.scheduled_video_id:
                    try:
                        scheduled = db.query(ScheduledVideo).filter(ScheduledVideo.id == int(episode.scheduled_video_id)).first()
                    except Exception:
                        scheduled = None
                    if scheduled and getattr(scheduled, "uploaded_at", None) and status != "published":
                        try:
                            self.update_publication_state_from_schedule(db, scheduled_video_id=int(scheduled.id))
                            synced += 1
                        except Exception:
                            pass
                        status = _episode_status(episode.status)
                if episode.task_id:
                    task = get_task(str(episode.task_id)) or {}
                    task_status = str(task.get("status") or "").lower()
                    if task_status in {"pending", "processing"} and status != "in_production":
                        episode.status = "in_production"
                        synced += 1
                        _dbg_event("H4", "episode marked in_production", {
                            "series_id": int(series.id),
                            "episode_id": int(episode.id),
                            "episode_number": int(episode.episode_number or 0),
                            "task_id": str(episode.task_id),
                            "task_status": task_status,
                        })
                    elif task_status in {"completed", "awaiting_review", "approved"} and status in {"in_production", "awaiting_production", "planned", "in_correction"}:
                        before = str(episode.status)
                        episode.status = "awaiting_review"
                        synced += 1
                        _audit_event(
                            db,
                            event_type="episode_status_changed",
                            series_id=int(series.id),
                            episode_id=int(episode.id),
                            task_id=str(episode.task_id) if episode.task_id else None,
                            status_before=before,
                            status_after="awaiting_review",
                        )
                        _dbg_event("H4", "episode transitioned to awaiting_review", {
                            "series_id": int(series.id),
                            "episode_id": int(episode.id),
                            "episode_number": int(episode.episode_number or 0),
                            "task_id": str(episode.task_id),
                            "task_status": task_status,
                        })
                        if bool(getattr(series, "auto_approval", False)) and not episode.approved_at:
                            auto_approve_queue.append((int(episode.id), int(user.id)))
                    elif task_status in {"failed", "cancelled"} and status in {"in_production", "awaiting_production"}:
                        before = str(episode.status)
                        episode.status = "failed"
                        synced += 1
                        task_result = task.get("result") if isinstance(task.get("result"), dict) else {}
                        _audit_event(
                            db,
                            event_type="episode_status_changed",
                            series_id=int(series.id),
                            episode_id=int(episode.id),
                            task_id=str(episode.task_id) if episode.task_id else None,
                            status_before=before,
                            status_after="failed",
                            payload={
                                "task_status": task_status,
                                "message": str(task.get("message") or "")[:500],
                                "provider_error": task_result.get("provider_error") if isinstance(task_result, dict) else None,
                            },
                        )
                        _dbg_event("H4", "episode marked failed (task failed/cancelled)", {
                            "series_id": int(series.id),
                            "episode_id": int(episode.id),
                            "episode_number": int(episode.episode_number or 0),
                            "task_status": task_status,
                        })
                    status = _episode_status(episode.status)
                if status in {"awaiting_review", "in_correction", "rejected"} and current >= episode.publication_datetime and not episode.approved_at:
                    episode.status = "publication_blocked"
                    series.status = "pending_issue"
                    blocked += 1
                    status = "publication_blocked"
                should_queue = (
                    _series_status(series.status) == "active"
                    and status in {"planned", "awaiting_production", "in_correction"}
                    and current >= episode.production_datetime
                    and not episode.task_id
                )
                previous_episodes = [ep for ep in episodes if int(ep.episode_number or 0) < int(episode.episode_number or 0)]
                previous_generation_pending = any(
                    _episode_status(prev.status) in {"planned", "awaiting_production", "in_production"}
                    for prev in previous_episodes
                )
                if should_queue:
                    if shutdown_in_progress:
                        shutdown_blocked += 1
                        continue
                    if previous_generation_pending:
                        continue
                    duration_minutes = int(episode.duration_minutes or series.duration_minutes or 10)
                    resource_report = evaluate_series_video_resources(duration_minutes)
                    resource_entry = {
                        **resource_report,
                        "message": resource_guard_message(resource_report),
                        "checked_at": datetime.utcnow().isoformat(),
                    }
                    episode_metadata = _json_loads(episode.metadata_json, {})
                    if not isinstance(episode_metadata, dict):
                        episode_metadata = {}
                    episode_metadata["resource_guard"] = resource_entry
                    episode.metadata_json = _json_dumps(episode_metadata)
                    if not bool(resource_report.get("allowed")):
                        resource_blocked += 1
                        series.status = "pending_issue"
                        _audit_event(
                            db,
                            event_type="series_resource_guard_blocked",
                            series_id=int(series.id),
                            episode_id=int(episode.id),
                            status_before=status,
                            status_after=status,
                            payload={**resource_entry, "series_status_after": "pending_issue"},
                        )
                        continue
                    series_ik = ""
                    try:
                        sm = self._series_memory(series)
                        series_ik = str(sm.get("idempotency_key") or sm.get("series_idempotency_key") or "").strip()
                    except Exception:
                        series_ik = ""
                    self._enqueue_episode_generation(
                        db, user=user, series=series, episode=episode,
                        series_idempotency_key=series_ik or None,
                    )
                    activated += 1
                    episode.status = "in_production"
            published = len([ep for ep in episodes if _episode_status(ep.status) == "published"])
            if episodes and published >= len(episodes):
                series.status = "completed"
            else:
                in_problem = any(_episode_status(ep.status) == "publication_blocked" for ep in episodes)
                if in_problem and _series_status(series.status) == "active":
                    series.status = "pending_issue"
        db.commit()
        for episode_id, user_id in auto_approve_queue:
            self._auto_approve_episode(db, episode_id=episode_id, user_id=user_id)
        return {
            "queued": activated,
            "blocked": blocked,
            "synced": synced,
            "shutdown_in_progress": shutdown_in_progress,
            "shutdown_blocked": shutdown_blocked,
            "resource_blocked": resource_blocked,
        }

    def _auto_approve_episode(self, db: Session, *, episode_id: int, user_id: int) -> None:
        lock = acquire_distributed_lock(f"auto_approve_episode:{int(episode_id)}", timeout_seconds=10, ttl_seconds=120)
        try:
            user = db.query(User).filter(User.id == int(user_id)).first()
            if not user:
                return
            episode = (
                db.query(SeriesEpisode)
                .join(SeriesPlan, SeriesPlan.id == SeriesEpisode.series_id)
                .filter(SeriesEpisode.id == int(episode_id), SeriesPlan.user_id == int(user.id))
                .first()
            )
            if not episode:
                return
            series = db.query(SeriesPlan).filter(SeriesPlan.id == int(episode.series_id)).first()
            if not series or not bool(getattr(series, "auto_approval", False)):
                return
            if episode.approved_at:
                return
            if _episode_status(episode.status) != "awaiting_review":
                return
            try:
                self.approve_episode(db, user=user, episode_id=int(episode.id))
            except Exception as e:
                try:
                    import traceback as _tb
                    _audit_event(
                        db,
                        event_type="approval_failed",
                        series_id=int(series.id),
                        episode_id=int(episode.id),
                        task_id=str(episode.task_id) if episode.task_id else None,
                        scheduled_video_id=int(episode.scheduled_video_id) if episode.scheduled_video_id else None,
                        status_before="awaiting_review",
                        status_after=str(episode.status),
                        payload={"error": str(e)},
                        error_stack=_tb.format_exc()[-8000:],
                    )
                    db.commit()
                except Exception:
                    pass
        finally:
            release_distributed_lock(lock)

    def _episode_task_result(self, episode: SeriesEpisode) -> Dict[str, Any]:
        task = get_task(str(episode.task_id)) if episode.task_id else None
        if isinstance(task, dict) and isinstance(task.get("result"), dict):
            return task.get("result") or {}
        return {}

    def approve_episode(self, db: Session, *, user: User, episode_id: int) -> Dict[str, Any]:
        started_at = datetime.utcnow()
        episode = (
            db.query(SeriesEpisode)
            .join(SeriesPlan, SeriesPlan.id == SeriesEpisode.series_id)
            .filter(SeriesEpisode.id == int(episode_id), SeriesPlan.user_id == int(user.id))
            .first()
        )
        if not episode:
            raise ValueError("Episódio não encontrado.")
        series = db.query(SeriesPlan).filter(SeriesPlan.id == int(episode.series_id)).first()
        if not series:
            raise ValueError("Série não encontrada.")

        # ===== UnifiedVideoPipeline: consulta estado real + aprovação + (opcional) upload único =====
        uvpsvc = _require_unified_pipeline("approve_episode")
        uv = self._find_unified_video_by_episode(db, episode)
        video_url: Optional[str] = None
        youtube_video_id: Optional[str] = None
        youtube_url: Optional[str] = None
        providers_cost: Dict[str, Any] = {}
        title: str = str(episode.planned_title).strip()
        description: str = str(getattr(episode, "planned_description", "") or "").strip()
        if uv is not None:
            if getattr(uv, "status", None) not in {
                UnifiedVideoStatus.AWAITING_REVIEW,
                UnifiedVideoStatus.APPROVED,
                UnifiedVideoStatus.UPLOADING,
                UnifiedVideoStatus.PUBLISHED,
            }:
                raise ValueError(
                    f"Episódio não está pronto para aprovação. Status atual do UnifiedVideo: {getattr(uv, 'status', 'unknown')}."
                )
            video_url = getattr(uv, "video_url", None) or video_url
            youtube_video_id = getattr(uv, "youtube_video_id", None) or youtube_video_id
            youtube_url = getattr(uv, "youtube_url", None) or youtube_url
            title = str(getattr(uv, "title", None) or title).strip() or title
            description = str(getattr(uv, "description", None) or description).strip() or description
            providers_cost = {
                "estimated_cost": _safe_float(getattr(uv, "estimated_cost", 0.0), 0.0),
                "actual_cost": _safe_float(getattr(uv, "actual_cost", 0.0), 0.0),
                "call_count_text": _safe_int(getattr(uv, "call_count_text", 0), 0),
                "call_count_image": _safe_int(getattr(uv, "call_count_image", 0), 0),
                "call_count_audio": _safe_int(getattr(uv, "call_count_audio", 0), 0),
            }
        if not video_url:
            legacy = self._episode_task_result(episode)
            if isinstance(legacy, dict) and legacy.get("video_url"):
                video_url = str(legacy.get("video_url") or "").strip() or None
            if isinstance(legacy, dict) and legacy.get("youtube_video_id") and not youtube_video_id:
                youtube_video_id = str(legacy.get("youtube_video_id") or "").strip() or None
                youtube_url = str((legacy.get("youtube_url") or f"https://www.youtube.com/watch?v={youtube_video_id}") or "").strip() or None
                title = str(legacy.get("title") or title).strip() or title
                description = str(legacy.get("description") or description).strip() or description
        if not video_url:
            raise ValueError("O episódio ainda não possui vídeo pronto para aprovação.")

        _audit_event(
            db,
            event_type="approval_started",
            series_id=int(series.id),
            episode_id=int(episode.id),
            task_id=str(episode.task_id) if episode.task_id else None,
            scheduled_video_id=int(episode.scheduled_video_id) if episode.scheduled_video_id else None,
            status_before=str(episode.status),
            payload={"auto_approval": bool(getattr(series, "auto_approval", False)), "actor_user_id": int(user.id)},
        )
        scheduled = None
        if episode.scheduled_video_id:
            scheduled = db.query(ScheduledVideo).filter(ScheduledVideo.id == int(episode.scheduled_video_id)).first()
        if not scheduled:
            scheduled = ScheduledVideo(
                user_id=int(user.id),
                theme=series.main_theme,
                title=title,
                description=description,
                scheduled_for=episode.publication_datetime,
                status="completed",
                video_type="video",
                script_data=_json_dumps({
                    "source": "series_episode",
                    "series_id": int(series.id),
                    "episode_id": int(episode.id),
                    "episode_number": int(episode.episode_number),
                    "approved": True,
                    "auto_processing_eligible": False,
                    "pipeline": "unified_video_pipeline",
                }),
                video_url=video_url,
                progress=100,
                auto_post=True,
                voice_style=series.narration_style or "human",
                voice_gender="female",
                youtube_video_id=(youtube_video_id or None),
                uploaded_at=(datetime.utcnow() if (youtube_video_id and youtube_url) else None),
            )
            if scheduled.uploaded_at is not None:
                scheduled.status = "published"
            db.add(scheduled)
            db.flush()
            episode.scheduled_video_id = int(scheduled.id)
            _audit_event(
                db,
                event_type="scheduled_created",
                series_id=int(series.id),
                episode_id=int(episode.id),
                task_id=str(episode.task_id) if episode.task_id else None,
                scheduled_video_id=int(scheduled.id),
                payload={"auto_post": True, "scheduled_for": _dt_to_iso(episode.publication_datetime), "status": str(scheduled.status)},
            )
        else:
            scheduled.title = title
            scheduled.description = description
            scheduled.scheduled_for = episode.publication_datetime
            scheduled.video_url = video_url
            scheduled.status = "published" if (youtube_video_id and youtube_url) else "completed"
            scheduled.auto_post = True
            scheduled.progress = 100
            if youtube_video_id:
                scheduled.youtube_video_id = youtube_video_id
                if not scheduled.uploaded_at:
                    scheduled.uploaded_at = datetime.utcnow()
            _audit_event(
                db,
                event_type="scheduled_updated",
                series_id=int(series.id),
                episode_id=int(episode.id),
                task_id=str(episode.task_id) if episode.task_id else None,
                scheduled_video_id=int(scheduled.id),
                payload={"auto_post": True, "scheduled_for": _dt_to_iso(episode.publication_datetime), "status": str(scheduled.status)},
            )
        episode.approved_at = datetime.utcnow()
        episode.approved_by = int(user.id)
        episode.status = "approved"

        # ===== UnifiedVideoPipeline: transição APPROVED =====
        if uv is not None:
            try:
                uvpsvc.transition_status(
                    db,
                    idempotency_key_or_task_id=str(uv.task_id or uv.idempotency_key or ""),
                    status=UnifiedVideoStatus.APPROVED,
                    progress=99,
                    message=f"Aprovado via série/episode#{int(episode.id)}.",
                    merge_result={
                        "episode_id": int(episode.id),
                        "series_id": int(series.id),
                        "scheduled_video_id": int(episode.scheduled_video_id or 0) or None,
                    },
                )
            except Exception:
                # transição opcional: não bloqueia aprovação do episódio
                pass

        # ===== UnifiedVideoPipeline: upload ÚNICO YouTube (publish_if_ready) =====
        auto_publish = bool(getattr(series, "auto_approval", False) or getattr(series, "auto_publish", False))
        already_on_youtube = bool(youtube_video_id and youtube_url)
        if auto_publish and not already_on_youtube:
            try:
                from app.routers.youtube import (
                    _build_youtube_watch_url as _watch_url,
                    _extract_uploaded_youtube_id as _extract_id,
                )
                from app.services.youtube_service import YouTubeService

                def _series_upload_callable(video_path: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
                    yt = YouTubeService()
                    meta = dict(metadata or {})
                    title = str(
                        meta.get("title") or meta.get("override_title") or title or episode.planned_title or "Vídeo Codexia"
                    ).strip()[:100] or "Vídeo Codexia"
                    description = str(meta.get("description") or meta.get("override_description") or description or "").strip()[:5000]
                    tags = meta.get("tags") if isinstance(meta.get("tags"), list) else []
                    raw = yt.upload_video(
                        str(video_path),
                        title=title,
                        description=description,
                        tags=([str(x).strip() for x in tags if str(x).strip()] or []),
                    )
                    if isinstance(raw, dict):
                        yid = _extract_id(raw) or str(raw.get("id") or raw.get("videoId") or raw.get("youtube_video_id") or "").strip()
                        return {
                            **dict(raw),
                            "youtube_video_id": (yid or None),
                            "youtube_url": (_watch_url(yid) if yid else None),
                        }
                    return {"youtube_video_id": None, "youtube_url": None, "raw": str(raw or "")[:5000]}

                pub = uvpsvc.publish_if_ready(
                    db,
                    idempotency_key_or_task_id=str(
                        getattr(uv, "task_id", None) or getattr(uv, "idempotency_key", None) or str(episode.task_id or "")
                    ),
                    upload_callable=_series_upload_callable,
                    upload_metadata={
                        "title": title,
                        "description": description,
                        "tags": None,
                        "episode_id": int(episode.id),
                        "series_id": int(series.id),
                    },
                    visibility_override=str(getattr(series, "visibility", "") or "unlisted").strip().lower() or "unlisted",
                )
                if isinstance(pub, dict) and bool(pub.get("ok")):
                    youtube_video_id = str(pub.get("youtube_video_id") or "").strip() or youtube_video_id
                    youtube_url = str(pub.get("youtube_url") or "").strip() or youtube_url
            except Exception:
                # Não publica agora; deixa como approved + scheduled.
                already_on_youtube = already_on_youtube
        if youtube_video_id and youtube_url:
            episode.youtube_video_id = youtube_video_id
            episode.youtube_url = youtube_url
            episode.published_at = episode.approved_at
            episode.status = "published"
            if scheduled is not None:
                try:
                    scheduled.uploaded_at = episode.published_at
                    scheduled.status = "published"
                    scheduled.youtube_video_id = youtube_video_id
                    scheduled.video_url = youtube_url
                except Exception:
                    pass
        episode.approved_snapshot_json = _json_dumps({
            "approved_at": _dt_to_iso(episode.approved_at),
            "task_id": episode.task_id,
            "unified_video_id": (getattr(uv, "id", None) if uv is not None else None),
            "video_url": video_url,
            "title": title,
            "description": description,
            "youtube_video_id": youtube_video_id,
            "youtube_url": youtube_url,
            "providers_cost": providers_cost,
        })
        db.add(EpisodeReview(
            episode_id=int(episode.id),
            version=int(episode.current_version or 1),
            decision="approved",
            feedback="Aprovado para fila de publicação.",
            affected_components=_json_dumps([]),
            reused_components=_json_dumps(["script", "images", "audio", "video"]),
            regenerated_components=_json_dumps([]),
            estimated_cost=_safe_float(providers_cost.get("estimated_cost"), 0.0),
            actual_cost=_safe_float(providers_cost.get("actual_cost"), 0.0),
            reviewed_at=datetime.utcnow(),
            reviewed_by=int(user.id),
        ))
        _audit_event(
            db,
            event_type="approval_completed",
            series_id=int(series.id),
            episode_id=int(episode.id),
            task_id=str(episode.task_id) if episode.task_id else None,
            scheduled_video_id=int(episode.scheduled_video_id) if episode.scheduled_video_id else None,
            status_before="awaiting_review",
            status_after="approved",
            duration_ms=int((datetime.utcnow() - started_at).total_seconds() * 1000),
        )
        db.commit()
        return self.get_series_detail(db, user=user, series_id=int(series.id))

    def build_correction_plan(self, episode: SeriesEpisode, reasons: List[str], feedback: str) -> Dict[str, Any]:
        reasons = _normalize_reason_list(reasons)
        if not reasons:
            raise ValueError("Selecione ao menos um motivo de reprovação.")
        if not str(feedback or "").strip():
            raise ValueError("Explique o que deve ser corrigido.")
        target_scene_numbers = _extract_scene_numbers_from_feedback(feedback)
        affected: List[str] = []
        reused: List[str] = []
        regenerated: List[str] = []
        if "image" in reasons:
            affected.extend(["image", "render"])
            reused.extend(["script", "audio", "subtitle", "other_images"])
            regenerated.extend(["image", "render"])
        if "subtitle" in reasons or "sync" in reasons:
            affected.extend(["subtitle", "timeline", "render"])
            reused.extend(["script", "images", "audio"])
            regenerated.extend(["subtitle", "timeline", "render"])
        if "script" in reasons or "repetitive_content" in reasons or "opening" in reasons or "ending" in reasons or "next_hook" in reasons:
            affected.extend(["script", "audio", "images", "render"])
            regenerated.extend(["script", "audio", "images", "render"])
        if not affected:
            affected = ["metadata"]
            reused = ["script", "audio", "images", "video"]
            regenerated = ["metadata"]
        affected = list(dict.fromkeys(affected))
        reused = list(dict.fromkeys(reused))
        regenerated = list(dict.fromkeys(regenerated))
        result = self._episode_task_result(episode)
        estimated_cost = 0.0
        base_cost = result.get("cost_control") if isinstance(result.get("cost_control"), dict) else {}
        if "script" in regenerated:
            estimated_cost += _safe_float(base_cost.get("estimated_cost"), 0.0)
        elif "image" in regenerated:
            estimated_cost += max(0.05, _safe_float(base_cost.get("actual_cost"), 0.0) * 0.35)
        else:
            estimated_cost += max(0.03, _safe_float(base_cost.get("actual_cost"), 0.0) * 0.15)
        return {
            "reasons": reasons,
            "feedback": str(feedback).strip(),
            "affected_components": affected,
            "reused_components": reused,
            "regenerated_components": regenerated,
            "estimated_cost": round(estimated_cost, 4),
            "target_scene_numbers": target_scene_numbers,
        }

    def reject_episode(
        self,
        db: Session,
        *,
        user: User,
        episode_id: int,
        reasons: List[str],
        feedback: str,
    ) -> Dict[str, Any]:
        episode = (
            db.query(SeriesEpisode)
            .join(SeriesPlan, SeriesPlan.id == SeriesEpisode.series_id)
            .filter(SeriesEpisode.id == int(episode_id), SeriesPlan.user_id == int(user.id))
            .first()
        )
        if not episode:
            raise ValueError("Episódio não encontrado.")
        series = db.query(SeriesPlan).filter(SeriesPlan.id == int(episode.series_id)).first()
        if not series:
            raise ValueError("Série não encontrada.")
        correction_plan = self.build_correction_plan(episode, reasons, feedback)
        regenerated = list(correction_plan.get("regenerated_components") or [])

        # ===== UnifiedVideoPipeline: seeds exclusivamente por campos do UnifiedVideoRequest =====
        uv = self._find_unified_video_by_episode(db, episode)
        uv_result: Dict[str, Any] = {}
        if uv is not None:
            uv_result = _json_loads(getattr(uv, "result_json", None), {}) or {}

        task_result = self._episode_task_result(episode) or {}
        legacy_render = task_result.get("render_report") if isinstance(task_result.get("render_report"), dict) else {}
        legacy_script = task_result.get("script") if isinstance(task_result.get("script"), dict) else {}

        # ---- determine_seed_assets: NÃO gera NADA, só escolhe o que reaproveitar ----
        seeded_script: Optional[Dict[str, Any]] = None
        if "script" not in regenerated:
            # Reaproveita o roteiro anterior.
            candidate = None
            if isinstance(uv_result.get("script"), dict):
                candidate = dict(uv_result.get("script") or {})
            elif isinstance(legacy_script, dict):
                candidate = dict(legacy_script or {})
            if candidate:
                seeded_script = candidate

        selected_images: Optional[List[str]] = None
        if "image" not in regenerated:
            slots: List[str] = []
            if isinstance(uv_result.get("images"), list) and uv_result.get("images"):
                slots = [_existing_artifact_path(x) for x in (uv_result.get("images") or []) if x]
            elif isinstance(legacy_render, dict):
                slots = _extract_scene_image_slots(legacy_render)
            if not slots and isinstance(legacy_script.get("selected_images"), list):
                slots = [str(x).strip() for x in (legacy_script.get("selected_images") or []) if str(x).strip()]
            if slots:
                selected_images = list(slots)
        else:
            # Se alguma cena específica for re-gerar imagem, apaga o slot correspondente.
            existing: List[str] = []
            if isinstance(uv_result.get("images"), list) and uv_result.get("images"):
                existing = [_existing_artifact_path(x) for x in (uv_result.get("images") or []) if x]
            elif isinstance(legacy_render, dict):
                existing = _extract_scene_image_slots(legacy_render)
            if not existing and isinstance(legacy_script.get("selected_images"), list):
                existing = [str(x).strip() for x in (legacy_script.get("selected_images") or []) if str(x).strip()]
            if existing:
                for scene_number in correction_plan.get("target_scene_numbers") or []:
                    idx = int(scene_number) - 1
                    if 0 <= idx < len(existing):
                        existing[idx] = ""
                selected_images = list(existing)

        reuse_audio_from: Optional[Dict[str, Any]] = None
        if "audio" not in regenerated and "script" not in regenerated:
            audio_generation: Dict[str, Any] = {}
            official_audio: Dict[str, Any] = {}
            if isinstance(uv_result.get("audio_generation"), dict):
                audio_generation = dict(uv_result.get("audio_generation") or {})
            elif isinstance(legacy_render.get("audio_generation"), dict):
                audio_generation = dict(legacy_render.get("audio_generation") or {})
            if isinstance(uv_result.get("official_audio_transcription"), dict):
                official_audio = dict(uv_result.get("official_audio_transcription") or {})
            elif isinstance(legacy_render.get("official_audio_transcription"), dict):
                official_audio = dict(legacy_render.get("official_audio_transcription") or {})
            if audio_generation or official_audio:
                reuse_audio_from = {
                    "audio_generation": audio_generation,
                    "official_audio_transcription": official_audio,
                }

        force_regenerate = bool(
            "script" in regenerated or "image" in regenerated or "subtitle" in regenerated or "sync" in regenerated
            or "render" in regenerated
        )

        episode.current_version = int(episode.current_version or 1) + 1
        episode.status = "in_correction"
        episode.task_id = None
        episode.approved_at = None
        episode.approved_by = None
        episode.correction_plan_json = _json_dumps(correction_plan)
        db.add(EpisodeReview(
            episode_id=int(episode.id),
            version=int(episode.current_version),
            decision="rejected",
            reason_categories=_json_dumps(correction_plan["reasons"]),
            feedback=correction_plan["feedback"],
            affected_components=_json_dumps(correction_plan["affected_components"]),
            reused_components=_json_dumps(correction_plan["reused_components"]),
            regenerated_components=_json_dumps(correction_plan["regenerated_components"]),
            estimated_cost=_safe_float(correction_plan.get("estimated_cost"), 0.0),
            actual_cost=None,
            reviewed_at=datetime.utcnow(),
            reviewed_by=int(user.id),
        ))
        self._enqueue_episode_generation(
            db,
            user=user,
            series=series,
            episode=episode,
            correction_feedback=correction_plan["feedback"],
            initial_result=None,
            force_regenerate=force_regenerate,
            selected_images=selected_images,
            reuse_audio_from=reuse_audio_from,
            seeded_script=seeded_script,
        )
        db.commit()
        return self.get_series_detail(db, user=user, series_id=int(series.id))

    def update_publication_state_from_schedule(self, db: Session, *, scheduled_video_id: int) -> None:
        scheduled = db.query(ScheduledVideo).filter(ScheduledVideo.id == int(scheduled_video_id)).first()
        if not scheduled:
            return
        episode = db.query(SeriesEpisode).filter(SeriesEpisode.scheduled_video_id == int(scheduled.id)).first()
        if not episode:
            return
        if _episode_status(episode.status) == "published":
            return
        if getattr(scheduled, "uploaded_at", None):
            before = str(episode.status)
            episode.published_at = scheduled.uploaded_at
            episode.youtube_video_id = scheduled.youtube_video_id
            if scheduled.youtube_video_id:
                episode.youtube_url = f"https://www.youtube.com/watch?v={scheduled.youtube_video_id}"
            episode.status = "published"
            series = db.query(SeriesPlan).filter(SeriesPlan.id == int(episode.series_id)).first()
            if series:
                series.current_episode = max(int(series.current_episode or 0), int(episode.episode_number or 0))
                memory = self._series_memory(series)
                published_list = memory.get("published_episodes") if isinstance(memory.get("published_episodes"), list) else []
                published_list = [item for item in published_list if int(item.get("episode_number") or 0) != int(episode.episode_number)]
                published_list.append({
                    "episode_number": int(episode.episode_number),
                    "title": episode.planned_title,
                    "published_at": _dt_to_iso(episode.published_at),
                })
                memory["published_episodes"] = sorted(published_list, key=lambda item: int(item.get("episode_number") or 0))
                memory["last_promise"] = episode.next_episode_hook
                memory["next_planned_hook"] = None
                memory["narrative_progress"] = int(episode.episode_number)
                self._save_series_memory(series, memory)
            _audit_event(
                db,
                event_type="publication_completed",
                series_id=int(episode.series_id),
                episode_id=int(episode.id),
                task_id=str(episode.task_id) if episode.task_id else None,
                scheduled_video_id=int(scheduled.id),
                status_before=before,
                status_after="published",
                payload={"youtube_video_id": scheduled.youtube_video_id, "uploaded_at": _dt_to_iso(scheduled.uploaded_at)},
            )
        db.commit()


youtube_series_service = YouTubeSeriesService()
