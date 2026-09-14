from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Settings, User
from app.routers.auth import get_current_admin_user
from app.services.cinematic_director import CinematicDirector, CinematicDirectorError
from app.services.cinematic_video_provider import CinematicProviderError, CinematicVideoProvider


router = APIRouter(prefix="/cinematic", tags=["Codexia Cinematic"])

_STORE_ROOT = Path("/data/codexia/cinematic") if os.path.isdir("/data") else Path(".codexia/cinematic")
_STORE_ROOT.mkdir(parents=True, exist_ok=True)


def _user_id(current_user: Optional[User]) -> int:
    try:
        return int(getattr(current_user, "id", 0) or 0)
    except Exception:
        return 0


def _settings(db: Session, user_id: int) -> Optional[Settings]:
    q = db.query(Settings)
    if user_id:
        row = q.filter(Settings.user_id == user_id).order_by(Settings.id.desc()).first()
        if row:
            return row
    return q.order_by(Settings.id.desc()).first()


def _store_path(user_id: int) -> Path:
    return _STORE_ROOT / f"campaign-{max(0, user_id)}.json"


def _default_campaign() -> Dict[str, Any]:
    start = datetime.now(timezone.utc)
    return {
        "name": "Meta Monetização — 30 dias",
        "budget_brl": 1000.0,
        "spent_brl": 0.0,
        "current_subscribers": 442,
        "current_watch_hours": 31.0,
        "current_valid_uploads_90d": 3,
        "target_subscribers": 1000,
        "target_watch_hours": 4000.0,
        "early_target_subscribers": 500,
        "early_target_watch_hours": 3000.0,
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(days=30)).isoformat(),
        "long_form_target": 16,
        "cinematic_story_target": 8,
        "devotional_target": 8,
        "shorts_target_min": 32,
        "shorts_target_max": 48,
        "target_avg_view_minutes": 6.0,
        "strategy": "cinematic_intelligent",
        "updated_at": start.isoformat(),
    }


def _load_campaign(user_id: int) -> Dict[str, Any]:
    path = _store_path(user_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = _default_campaign()
                merged.update(data)
                return merged
        except Exception:
            pass
    return _default_campaign()


def _save_campaign(user_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    current = _load_campaign(user_id)
    current.update(payload)
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = _store_path(user_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return current


def _derived(c: Dict[str, Any]) -> Dict[str, Any]:
    subs = int(c.get("current_subscribers") or 0)
    hours = float(c.get("current_watch_hours") or 0)
    target_subs = int(c.get("target_subscribers") or 1000)
    target_hours = float(c.get("target_watch_hours") or 4000)
    budget = float(c.get("budget_brl") or 0)
    spent = float(c.get("spent_brl") or 0)
    missing_hours = max(0.0, target_hours - hours)
    avg_min = max(0.5, float(c.get("target_avg_view_minutes") or 6.0))
    views_needed = int(round((missing_hours * 60.0) / avg_min)) if missing_hours else 0
    try:
        end = datetime.fromisoformat(str(c.get("end_at") or "").replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days_left = max(0, (end - now).days + (1 if end > now else 0))
    except Exception:
        days_left = 30
    return {
        "missing_subscribers": max(0, target_subs - subs),
        "missing_watch_hours": round(missing_hours, 1),
        "estimated_long_views_needed": views_needed,
        "target_views_per_day": int(round(views_needed / max(1, days_left))) if views_needed else 0,
        "target_hours_per_day": round(missing_hours / max(1, days_left), 1) if missing_hours else 0.0,
        "budget_remaining_brl": round(max(0.0, budget - spent), 2),
        "budget_used_pct": round((spent / budget * 100.0), 1) if budget > 0 else 0.0,
        "days_left": days_left,
    }


def _usd_brl() -> float:
    try:
        return max(0.01, float(os.getenv("CODEXIA_USD_BRL") or "5.12"))
    except Exception:
        return 5.12


def _estimate_components(
    *,
    content_type: str,
    duration_minutes: int,
    motion_seconds: int,
    premium_motion_seconds: int,
    estimated_images: int,
    voice: str = "elevenlabs",
) -> Dict[str, float]:
    image_usd = 0.02 * max(1, int(estimated_images))
    total_motion = max(0, int(motion_seconds))
    premium_seconds = max(0, min(total_motion, int(premium_motion_seconds)))
    economy_seconds = max(0, total_motion - premium_seconds)
    motion_usd = economy_seconds * 0.05 + premium_seconds * 0.11
    voice_usd = (max(1, int(duration_minutes)) * 0.10) if voice == "elevenlabs" else 0.0
    claude_usd = 0.35 if str(content_type or "story").lower() == "story" else 0.22
    subtotal_usd = image_usd + motion_usd + voice_usd + claude_usd
    reserve_usd = subtotal_usd * 0.15
    return {
        "claude": claude_usd,
        "images": image_usd,
        "motion": motion_usd,
        "voice": voice_usd,
        "recovery_reserve": reserve_usd,
        "total_usd": subtotal_usd + reserve_usd,
    }


def _rebalance_plan_to_budget(
    plan: Dict[str, Any],
    *,
    content_type: str,
    duration_minutes: int,
    budget_brl: float,
) -> Dict[str, Any]:
    """Keep cinematic movement while demoting premium scenes until cost fits.

    Strategy: preserve as much total motion as possible, then spend premium motion
    only on the highest-value A scenes. We target 95% of the requested ceiling so
    small provider/currency variance does not turn an R$85 plan into R$86 at run time.
    """
    scenes = plan.get("scenes") if isinstance(plan.get("scenes"), list) else []
    clean: List[Dict[str, Any]] = [scene for scene in scenes if isinstance(scene, dict)]
    if not clean:
        plan["budget_guard"] = {
            "enabled": True,
            "budget_limit_brl": round(float(budget_brl or 0), 2),
            "estimated_brl": 0.0,
            "budget_respected": True,
            "rebalanced": False,
        }
        return plan

    budget_limit = max(1.0, float(budget_brl or 1.0))
    safe_budget = budget_limit * 0.95
    fx = _usd_brl()
    reserve_factor = 1.15
    image_usd = 0.02 * len(clean)
    voice_usd = max(1, int(duration_minutes)) * 0.10
    claude_usd = 0.35 if str(content_type or "story").lower() == "story" else 0.22
    fixed_usd = image_usd + voice_usd + claude_usd
    pre_reserve_budget_usd = safe_budget / fx / reserve_factor
    motion_budget_usd = max(0.0, pre_reserve_budget_usd - fixed_usd)

    rebalanced = False
    original_premium = sum(
        max(0, int(scene.get("generative_video_seconds") or 0))
        for scene in clean
        if str(scene.get("tier") or "").upper() == "A"
    )

    # First guarantee that even all-economy motion fits. If not, trim B scenes
    # before touching hook/climax A scenes.
    total_motion = sum(max(0, int(scene.get("generative_video_seconds") or 0)) for scene in clean)
    max_economy_seconds = max(0, int(math.floor(motion_budget_usd / 0.05)))
    excess = max(0, total_motion - max_economy_seconds)
    if excess:
        def trim_rank(scene: Dict[str, Any]) -> tuple[int, int]:
            tier = str(scene.get("tier") or "C").upper()
            purpose = str(scene.get("purpose") or "story").lower()
            if tier == "B":
                priority = 0
            elif tier == "A" and purpose not in {"hook", "climax"}:
                priority = 1
            elif tier == "A" and purpose == "hook":
                priority = 2
            else:
                priority = 3
            return priority, -int(scene.get("index") or 0)

        for scene in sorted(clean, key=trim_rank):
            if excess <= 0:
                break
            current = max(0, int(scene.get("generative_video_seconds") or 0))
            if current <= 0:
                continue
            cut = min(current, excess)
            scene["generative_video_seconds"] = current - cut
            excess -= cut
            rebalanced = True

    total_motion = sum(max(0, int(scene.get("generative_video_seconds") or 0)) for scene in clean)
    economy_floor_usd = total_motion * 0.05
    premium_extra_budget = max(0.0, motion_budget_usd - economy_floor_usd)
    premium_cap_seconds = max(0, int(math.floor(premium_extra_budget / 0.06)))

    # Keep premium only on the strongest moments. Whole scenes stay premium or
    # are demoted, so a single generated clip never changes provider mid-scene.
    def premium_rank(scene: Dict[str, Any]) -> tuple[int, int]:
        purpose = str(scene.get("purpose") or "story").lower()
        order = {
            "climax": 0,
            "hook": 1,
            "tension": 2,
            "story": 3,
            "application": 4,
            "prayer": 5,
            "cta": 6,
        }
        return order.get(purpose, 3), int(scene.get("index") or 0)

    premium_used = 0
    for scene in sorted(
        [s for s in clean if str(s.get("tier") or "").upper() == "A"],
        key=premium_rank,
    ):
        seconds = max(0, int(scene.get("generative_video_seconds") or 0))
        if seconds <= 0:
            scene["tier"] = "B"
            scene["recommended_provider"] = "runway"
            rebalanced = True
            continue
        if premium_used + seconds <= premium_cap_seconds:
            premium_used += seconds
            if str(scene.get("recommended_provider") or "").lower() not in {"kling", "veo"}:
                scene["recommended_provider"] = "veo"
            continue
        scene["tier"] = "B"
        scene["recommended_provider"] = "runway"
        rebalanced = True

    premium_seconds = sum(
        max(0, int(scene.get("generative_video_seconds") or 0))
        for scene in clean
        if str(scene.get("tier") or "").upper() == "A"
    )
    total_motion = sum(max(0, int(scene.get("generative_video_seconds") or 0)) for scene in clean)
    components = _estimate_components(
        content_type=content_type,
        duration_minutes=duration_minutes,
        motion_seconds=total_motion,
        premium_motion_seconds=premium_seconds,
        estimated_images=len(clean),
        voice="elevenlabs",
    )
    estimated_brl = components["total_usd"] * fx

    # Rounding whole premium scenes can still leave us a few centavos above the
    # ceiling. If that ever happens, demote remaining premium scenes until safe.
    if estimated_brl > budget_limit:
        premium_scenes = sorted(
            [s for s in clean if str(s.get("tier") or "").upper() == "A"],
            key=premium_rank,
            reverse=True,
        )
        for scene in premium_scenes:
            scene["tier"] = "B"
            scene["recommended_provider"] = "runway"
            rebalanced = True
            premium_seconds = sum(
                max(0, int(s.get("generative_video_seconds") or 0))
                for s in clean
                if str(s.get("tier") or "").upper() == "A"
            )
            components = _estimate_components(
                content_type=content_type,
                duration_minutes=duration_minutes,
                motion_seconds=total_motion,
                premium_motion_seconds=premium_seconds,
                estimated_images=len(clean),
                voice="elevenlabs",
            )
            estimated_brl = components["total_usd"] * fx
            if estimated_brl <= budget_limit:
                break

    plan["scenes"] = clean
    plan["motion_planned_seconds"] = total_motion
    checks = plan.get("quality_checks") if isinstance(plan.get("quality_checks"), dict) else {}
    checks["budget_respected"] = estimated_brl <= budget_limit
    plan["quality_checks"] = checks
    plan["budget_guard"] = {
        "enabled": True,
        "budget_limit_brl": round(budget_limit, 2),
        "safe_target_brl": round(safe_budget, 2),
        "estimated_brl": round(estimated_brl, 2),
        "budget_respected": estimated_brl <= budget_limit,
        "rebalanced": rebalanced,
        "original_premium_motion_seconds": int(original_premium),
        "premium_motion_seconds": int(premium_seconds),
        "economy_motion_seconds": int(max(0, total_motion - premium_seconds)),
        "total_motion_seconds": int(total_motion),
        "headroom_brl": round(max(0.0, budget_limit - estimated_brl), 2),
    }
    return plan


class CampaignUpdate(BaseModel):
    budget_brl: Optional[float] = Field(None, ge=0, le=100000)
    spent_brl: Optional[float] = Field(None, ge=0, le=100000)
    current_subscribers: Optional[int] = Field(None, ge=0)
    current_watch_hours: Optional[float] = Field(None, ge=0)
    current_valid_uploads_90d: Optional[int] = Field(None, ge=0)
    target_avg_view_minutes: Optional[float] = Field(None, ge=0.5, le=60)
    end_at: Optional[str] = None


class DirectorRequest(BaseModel):
    theme: str = Field(..., min_length=3, max_length=500)
    content_type: str = Field("story", pattern="^(story|devotional|short)$")
    duration_minutes: int = Field(10, ge=1, le=30)
    budget_brl: float = Field(90.0, ge=1, le=1000)


class EstimateRequest(BaseModel):
    content_type: str = "story"
    duration_minutes: int = Field(10, ge=1, le=30)
    motion_seconds: int = Field(0, ge=0, le=1800)
    premium_motion_seconds: int = Field(0, ge=0, le=1800)
    estimated_images: int = Field(40, ge=1, le=300)
    voice: str = "elevenlabs"


class SceneSubmitRequest(BaseModel):
    provider: str = Field("runway", pattern="^(runway|veo|kling)$")
    prompt: str = Field(..., min_length=3, max_length=4000)
    image_uri: Optional[str] = None
    image_base64: Optional[str] = None
    duration_seconds: int = Field(5, ge=3, le=15)
    premium: bool = False
    aspect_ratio: str = Field("16:9", pattern="^(16:9|9:16)$")


class ScenePollRequest(BaseModel):
    provider: str = Field(..., pattern="^(runway|veo|kling)$")
    job_id: str = Field(..., min_length=3, max_length=500)
    status_url: Optional[str] = None
    response_url: Optional[str] = None
    model_path: Optional[str] = None


@router.get("/status")
def cinematic_status(
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _user_id(current_user)
    settings = _settings(db, uid)
    campaign = _load_campaign(uid)
    director = CinematicDirector(settings)
    videos = CinematicVideoProvider(settings)
    return {
        "campaign": campaign,
        "derived": _derived(campaign),
        "providers": {
            "claude": director.status(),
            **videos.status(),
            "elevenlabs": {"configured": bool(str(getattr(settings, "elevenlabs_api_key", None) or os.getenv("ELEVENLABS_API_KEY") or "").strip())},
            "openai_images": {"configured": bool(str(getattr(settings, "openai_api_key", None) or os.getenv("OPENAI_API_KEY") or "").strip())},
        },
        "product": {
            "mode": "cinematic_intelligent",
            "story_motion_target_pct": 28,
            "devotional_motion_target_pct": 10,
            "shorts_reuse_assets": True,
            "legacy_ui_available": True,
        },
    }


@router.post("/campaign")
def update_campaign(
    body: CampaignUpdate,
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _user_id(current_user)
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    if payload.get("end_at"):
        try:
            datetime.fromisoformat(str(payload["end_at"]).replace("Z", "+00:00"))
        except Exception as exc:
            raise HTTPException(status_code=422, detail="end_at deve ser uma data ISO válida.") from exc
    campaign = _save_campaign(uid, payload)
    return {"campaign": campaign, "derived": _derived(campaign)}


@router.post("/director")
def director_plan(
    body: DirectorRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _user_id(current_user)
    settings = _settings(db, uid)
    try:
        result = CinematicDirector(settings).build_plan(
            theme=body.theme,
            content_type=body.content_type,
            duration_minutes=body.duration_minutes,
            budget_brl=body.budget_brl,
        )
        plan = _rebalance_plan_to_budget(
            result.plan,
            content_type=body.content_type,
            duration_minutes=body.duration_minutes,
            budget_brl=body.budget_brl,
        )
        usd_brl = _usd_brl()
        return {
            "plan": plan,
            "director": {
                "provider": result.provider,
                "model": result.model,
                "usage": result.usage,
                "estimated_cost_usd": round(result.estimated_cost_usd, 4),
                "estimated_cost_brl": round(result.estimated_cost_usd * usd_brl, 2),
            },
        }
    except CinematicDirectorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/estimate")
def estimate_cost(body: EstimateRequest):
    usd_brl = _usd_brl()
    components = _estimate_components(
        content_type=body.content_type,
        duration_minutes=body.duration_minutes,
        motion_seconds=body.motion_seconds,
        premium_motion_seconds=body.premium_motion_seconds,
        estimated_images=body.estimated_images,
        voice=body.voice,
    )
    return {
        "currency_rate_usd_brl": usd_brl,
        "components_usd": {
            "claude": round(components["claude"], 2),
            "images": round(components["images"], 2),
            "motion": round(components["motion"], 2),
            "voice": round(components["voice"], 2),
            "recovery_reserve": round(components["recovery_reserve"], 2),
        },
        "total_usd": round(components["total_usd"], 2),
        "total_brl": round(components["total_usd"] * usd_brl, 2),
        "note": "Estimativa prévia. O custo real deve ser registrado pelas respostas de cada provedor antes de novas regenerações.",
    }


@router.post("/scene/submit")
def submit_scene(
    body: SceneSubmitRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _user_id(current_user)
    provider = CinematicVideoProvider(_settings(db, uid))
    try:
        return provider.submit(
            provider=body.provider,
            prompt=body.prompt,
            image_uri=body.image_uri,
            image_base64=body.image_base64,
            duration=body.duration_seconds,
            premium=body.premium,
            aspect_ratio=body.aspect_ratio,
        )
    except CinematicProviderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/scene/poll")
def poll_scene(
    body: ScenePollRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_admin_user),
):
    uid = _user_id(current_user)
    provider = CinematicVideoProvider(_settings(db, uid))
    try:
        if body.provider == "runway":
            return provider.poll_runway(body.job_id)
        if body.provider == "veo":
            return provider.poll_veo(body.job_id)
        return provider.poll_kling(
            job_id=body.job_id,
            status_url=body.status_url,
            response_url=body.response_url,
            model_path=body.model_path,
        )
    except CinematicProviderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/content-calendar")
def content_calendar():
    pairs: List[Dict[str, Any]] = [
        {"story": "Davi e Golias — enfrentando o impossível", "devotional": "Quando você achar que não vai conseguir"},
        {"story": "José do Egito — traído pela própria família", "devotional": "Quando alguém que você ama decepciona você"},
        {"story": "Daniel na cova dos leões — fé quando tudo parece perdido", "devotional": "Uma oração para quando você estiver com medo"},
        {"story": "Moisés diante do Mar Vermelho — sem saída aparente", "devotional": "Quando você não consegue enxergar uma saída"},
        {"story": "Jó — quando quase tudo é perdido", "devotional": "Quando Deus parece estar em silêncio"},
        {"story": "Gideão — de incapaz a escolhido", "devotional": "Para quem acha que não é bom o suficiente"},
        {"story": "Pedro sobre as águas — fé contra medo", "devotional": "Ansiedade: quando o medo começa a dominar"},
        {"story": "Filho Pródigo — ainda existe caminho de volta", "devotional": "Deus ainda permite recomeçar"},
    ]
    out: List[Dict[str, Any]] = []
    day = 1
    for idx, pair in enumerate(pairs, start=1):
        out.append({"day": day, "kind": "story", "theme": pair["story"], "budget_brl": 85, "duration_minutes": 11, "shorts": 2 if idx % 2 else 3})
        day += 2
        out.append({"day": min(30, day), "kind": "devotional", "theme": pair["devotional"], "budget_brl": 25, "duration_minutes": 10, "shorts": 2})
        day += 2
    return {"items": out, "strategy": "8 historias + 8 devocionais + 32-48 Shorts derivados", "budget_target_brl": 1000}
