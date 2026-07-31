import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, Optional, TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Settings
from app.models import VideoTask
if TYPE_CHECKING:
    from app.modules.bible_video_factory.models import BibleVideoJob, BibleVideoMetric


def _bible_video_models():
    from app.modules.bible_video_factory.models import BibleVideoJob, BibleVideoMetric
    return BibleVideoJob, BibleVideoMetric


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _json_loads(value: Any, default: Any) -> Any:
    try:
        if isinstance(value, str) and value.strip():
            return json.loads(value)
    except Exception:
        pass
    return default


def _env_float(name: str, default: float) -> float:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except Exception:
        return float(default)


@dataclass
class FinancialContext:
    source_type: str
    context_id: str
    user_id: Optional[int] = None
    title: str = ""
    status: str = ""
    estimated_cost: float = 0.0
    actual_cost: float = 0.0
    aspect_ratio: str = "16:9"
    created_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def scope_key(self) -> str:
        if self.user_id is not None:
            return f"user:{int(self.user_id)}"
        return f"source:{self.source_type}"


class BibleVideoFinancialAdapter:
    source_type = "bible_video_factory"

    def build_context(self, job: Any) -> FinancialContext:
        return FinancialContext(
            source_type=self.source_type,
            context_id=str(getattr(job, "id", "") or ""),
            user_id=getattr(job, "user_id", None),
            title=str(getattr(job, "title", "") or ""),
            status=str(getattr(job, "status", "") or ""),
            estimated_cost=_safe_float(getattr(job, "estimated_cost", 0.0), 0.0),
            actual_cost=_safe_float(getattr(job, "actual_cost", 0.0), 0.0),
            aspect_ratio=str(getattr(job, "aspect_ratio", "16:9") or "16:9"),
            created_at=getattr(job, "created_at", None),
            metadata={
                "job_id": getattr(job, "id", None),
                "job_type": getattr(job, "job_type", None),
                "platform": getattr(job, "platform", None),
            },
        )

    def cost_snapshot(self, db: Session, *, user_id: Optional[int], day_start: datetime, month_start: datetime) -> Dict[str, float]:
        spent_today = db.execute(text(
            """
            SELECT COALESCE(SUM(actual_cost), 0) AS total
            FROM codexia_bible_video_jobs
            WHERE user_id = :user_id AND created_at >= :day_start
            """
        ), {"user_id": user_id, "day_start": day_start}).scalar() or 0.0
        spent_month = db.execute(text(
            """
            SELECT COALESCE(SUM(actual_cost), 0) AS total
            FROM codexia_bible_video_jobs
            WHERE user_id = :user_id AND created_at >= :month_start
            """
        ), {"user_id": user_id, "month_start": month_start}).scalar() or 0.0
        return {
            "spent_today": round(_safe_float(spent_today, 0.0), 4),
            "spent_month": round(_safe_float(spent_month, 0.0), 4),
        }

    def load_context(self, db: Session, context_id: str) -> Optional[FinancialContext]:
        try:
            job_id = int(context_id)
        except Exception:
            return None
        BibleVideoJob, _ = _bible_video_models()
        job = db.query(BibleVideoJob).filter(BibleVideoJob.id == job_id).first()
        return self.build_context(job) if job else None

    def list_contexts_for_day(self, db: Session, *, user_id: Optional[int], day_start: datetime, day_end: datetime) -> list:
        BibleVideoJob, _ = _bible_video_models()
        return db.query(BibleVideoJob).filter(
            BibleVideoJob.user_id == user_id,
            BibleVideoJob.created_at >= day_start,
            BibleVideoJob.created_at < day_end,
        ).all()

    def build_dashboard_metrics(self, db: Session, *, user_id: Optional[int]) -> Dict[str, Any]:
        BibleVideoJob, BibleVideoMetric = _bible_video_models()
        jobs_q = db.query(BibleVideoJob).filter(BibleVideoJob.user_id == user_id)
        metrics = db.query(BibleVideoMetric).filter(BibleVideoMetric.user_id == user_id).all()
        completed_jobs = jobs_q.filter(BibleVideoJob.status.in_(["ready", "published"])).all()
        actual_cost_total = round(sum(_safe_float(job.actual_cost, 0.0) for job in completed_jobs), 4)
        total_views = sum(_safe_int(item.view_count, 0) for item in metrics)
        total_subscribers = sum(_safe_int(item.subscribers_gained, 0) for item in metrics)
        roi_proxy = 0.0
        if actual_cost_total > 0:
            roi_proxy = round(((total_views * 0.002) + (total_subscribers * 0.8)) / actual_cost_total, 4)
        return {
            "roi": {
                "actual_cost_total": actual_cost_total,
                "views_total": total_views,
                "subscribers_total": total_subscribers,
                "roi_proxy": roi_proxy,
                "cost_per_1000_views": round((actual_cost_total / total_views) * 1000, 4) if total_views > 0 else 0.0,
            },
            "operations": {
                "completed_contexts": len(completed_jobs),
                "queued_contexts": jobs_q.filter(BibleVideoJob.status.in_(["queued", "processing"])).count(),
            },
        }


class YouTubeAutoFinancialAdapter:
    source_type = "youtube_auto"

    def build_guardrail_config(self) -> Any:
        per_video = _env_float("YOUTUBE_AUTO_PER_VIDEO_SPEND_LIMIT", 0.0)
        daily = _env_float("YOUTUBE_AUTO_DAILY_SPEND_LIMIT", 0.0)
        monthly = _env_float("YOUTUBE_AUTO_MONTHLY_SPEND_LIMIT", 0.0)
        if any(v > 0 for v in (per_video, daily, monthly)):
            return SimpleNamespace(per_video_spend_limit=per_video, daily_spend_limit=daily, monthly_spend_limit=monthly)
        db = SessionLocal()
        try:
            row = db.query(Settings).order_by(Settings.id.desc()).first()
            if row is None:
                return SimpleNamespace(per_video_spend_limit=0.0, daily_spend_limit=0.0, monthly_spend_limit=0.0)
            return SimpleNamespace(
                per_video_spend_limit=_safe_float(getattr(row, "per_video_spend_limit", 0.0), 0.0),
                daily_spend_limit=_safe_float(getattr(row, "daily_spend_limit", 0.0), 0.0),
                monthly_spend_limit=_safe_float(getattr(row, "monthly_spend_limit", 0.0), 0.0),
            )
        except Exception:
            return SimpleNamespace(per_video_spend_limit=0.0, daily_spend_limit=0.0, monthly_spend_limit=0.0)
        finally:
            db.close()

    def _estimate_cost(self, payload: Dict[str, Any], script: Optional[Dict[str, Any]] = None, video_result: Optional[Dict[str, Any]] = None) -> float:
        try:
            duration_min = int(payload.get("duration") or 5)
        except Exception:
            duration_min = 5
        duration_min = max(1, min(60, duration_min))

        scenes = []
        if isinstance(script, dict) and isinstance(script.get("scenes"), list):
            scenes = [item for item in script.get("scenes") if isinstance(item, dict)]
        scene_count = len(scenes) if scenes else max(4, min(24, duration_min * 2))
        selected_count = 0
        for key in ("selected_images", "custom_image_paths"):
            value = payload.get(key)
            if isinstance(value, list):
                selected_count += len([item for item in value if isinstance(item, str) and item.strip()])
        image_mode = str(payload.get("image_mode") or "").strip().lower()
        if image_mode == "single":
            generated_image_count = 0 if selected_count > 0 else 1
        else:
            generated_image_count = max(0, scene_count - selected_count)

        text_cost = scene_count * _env_float("YOUTUBE_AUTO_TEXT_SCENE_COST_UNIT", 0.0020)
        voice_cost = duration_min * _env_float("YOUTUBE_AUTO_TTS_MINUTE_COST_UNIT", 0.0120)
        image_cost = generated_image_count * _env_float("YOUTUBE_AUTO_IMAGE_COST_UNIT", 0.0350)
        upload_cost = _env_float("YOUTUBE_AUTO_UPLOAD_COST_UNIT", 0.0) if payload.get("auto_upload") else 0.0
        render_cost = max(duration_min / 10.0, 0.5) * _env_float("YOUTUBE_AUTO_RENDER_COST_UNIT", 0.0100)
        total = round(text_cost + voice_cost + image_cost + render_cost + upload_cost, 4)

        if isinstance(video_result, dict):
            render_report = video_result.get("render_report") if isinstance(video_result.get("render_report"), dict) else {}
            visual_plan = render_report.get("visual_plan") if isinstance(render_report.get("visual_plan"), dict) else {}
            reused_image_count = _safe_int(visual_plan.get("reused_image_count"), 0)
            total = round(max(0.0, total - (reused_image_count * _env_float("YOUTUBE_AUTO_IMAGE_COST_UNIT", 0.0350))), 4)

        return total

    def build_context(
        self,
        *,
        task_id: str,
        payload: Dict[str, Any],
        script: Optional[Dict[str, Any]] = None,
        video_result: Optional[Dict[str, Any]] = None,
        user_id: Optional[int] = None,
        status: str = "",
    ) -> FinancialContext:
        kind = str(payload.get("kind") or "").strip().lower() or "story"
        title = str(payload.get("override_title") or payload.get("topic") or "").strip()
        if not title and isinstance(script, dict):
            title = str(script.get("title") or "").strip()
        if not title and str(payload.get("mode") or "").strip().lower() == "story":
            title = "Video narrado"
        if not title:
            title = "Video do YouTube Auto"

        estimated_cost = self._estimate_cost(payload, script=script, video_result=None)
        actual_cost = self._estimate_cost(payload, script=script, video_result=video_result)
        return FinancialContext(
            source_type=self.source_type,
            context_id=str(task_id or "").strip(),
            user_id=user_id,
            title=title[:160],
            status=str(status or ("completed" if video_result else "processing")).strip(),
            estimated_cost=estimated_cost,
            actual_cost=actual_cost,
            aspect_ratio=str(payload.get("aspect_ratio") or "16:9"),
            metadata={
                "task_id": str(task_id or "").strip(),
                "mode": payload.get("mode"),
                "kind": kind,
                "auto_upload": bool(payload.get("auto_upload")),
                "requested_duration_min": _safe_int(payload.get("duration"), 5),
                "scene_count": len(script.get("scenes") or []) if isinstance(script, dict) and isinstance(script.get("scenes"), list) else None,
            },
        )

    def cost_snapshot(self, db: Session, *, user_id: Optional[int], day_start: datetime, month_start: datetime) -> Dict[str, float]:
        spent_today = db.execute(text(
            """
            SELECT COALESCE(SUM(actual_cost), 0) AS total
            FROM codexia_financial_audit_events
            WHERE source_type = :source_type
              AND (:user_id IS NULL OR user_id = :user_id)
              AND created_at >= :day_start
            """
        ), {
            "source_type": self.source_type,
            "user_id": user_id,
            "day_start": day_start,
        }).scalar() or 0.0
        spent_month = db.execute(text(
            """
            SELECT COALESCE(SUM(actual_cost), 0) AS total
            FROM codexia_financial_audit_events
            WHERE source_type = :source_type
              AND (:user_id IS NULL OR user_id = :user_id)
              AND created_at >= :month_start
            """
        ), {
            "source_type": self.source_type,
            "user_id": user_id,
            "month_start": month_start,
        }).scalar() or 0.0
        return {
            "spent_today": round(_safe_float(spent_today, 0.0), 4),
            "spent_month": round(_safe_float(spent_month, 0.0), 4),
        }

    def load_context(self, db: Session, context_id: str) -> Optional[FinancialContext]:
        row = db.query(VideoTask).filter(VideoTask.id == str(context_id or "").strip()).first()
        if not row:
            return None
        result = _json_loads(getattr(row, "result_json", None), {})
        payload = result.get("payload") if isinstance(result, dict) and isinstance(result.get("payload"), dict) else {}
        return self.build_context(
            task_id=row.id,
            payload=payload,
            user_id=getattr(row, "user_id", None),
            status=str(getattr(row, "status", "") or ""),
        )

    def build_dashboard_metrics(self, db: Session, *, user_id: Optional[int]) -> Dict[str, Any]:
        q = db.query(VideoTask)
        if user_id is not None:
            q = q.filter(VideoTask.user_id == user_id)
        rows = q.all()
        story_rows = []
        for row in rows:
            result = _json_loads(getattr(row, "result_json", None), {})
            if not isinstance(result, dict):
                continue
            if str(result.get("kind") or "").strip().lower() == "youtube_story_video":
                story_rows.append(row)
        completed_rows = [row for row in story_rows if str(getattr(row, "status", "") or "").lower() == "completed"]
        pending_rows = [row for row in story_rows if str(getattr(row, "status", "") or "").lower() in {"pending", "processing"}]
        return {
            "roi": {
                "actual_cost_total": 0.0,
                "views_total": 0,
                "subscribers_total": 0,
                "roi_proxy": 0.0,
                "cost_per_1000_views": 0.0,
            },
            "operations": {
                "completed_contexts": len(completed_rows),
                "queued_contexts": len(pending_rows),
            },
        }


bible_video_financial_adapter = BibleVideoFinancialAdapter()
youtube_auto_financial_adapter = YouTubeAutoFinancialAdapter()
