import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.modules.bible_video_factory.models import (
    BibleVideoCharacter,
    BibleVideoEpisode,
    BibleVideoJob,
    BibleVideoMetric,
    BibleVideoPrompt,
    BibleVideoScenario,
    BibleVideoScene,
    BibleVideoScript,
    BibleVideoSeries,
)
from app.modules.bible_video_factory.service import BibleVideoFactoryService, process_bible_video_job
from app.routers.auth import get_current_user
from app.redis_client import queue as rq_queue
from app.models import User


router = APIRouter(prefix="/bible-video-factory", tags=["bible-video-factory"])
_service = None


def get_service() -> BibleVideoFactoryService:
    global _service
    if _service is None:
        _service = BibleVideoFactoryService()
    return _service


class SeriesRequest(BaseModel):
    name: str
    bible_book: Optional[str] = None
    main_character: Optional[str] = None
    target_audience: Optional[str] = None
    visual_style: Optional[str] = None
    narrative_tone: Optional[str] = None
    planned_episodes: int = Field(default=10, ge=1, le=300)
    episode_duration_minutes: int = Field(default=5, ge=1, le=60)
    language: str = "pt-BR"
    linked_channel: Optional[str] = None
    status: str = "draft"
    bible_story_text: Optional[str] = None
    series_summary: Optional[str] = None
    notes: Optional[str] = None


class EpisodeUpdateRequest(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    biblical_basis: Optional[str] = None
    opening_hook: Optional[str] = None
    development_text: Optional[str] = None
    tension_moment: Optional[str] = None
    impact_phrase: Optional[str] = None
    ending_hook: Optional[str] = None
    short_suggestion: Optional[str] = None
    thumbnail_suggestion: Optional[str] = None
    youtube_title_suggestion: Optional[str] = None
    estimated_minutes: Optional[int] = Field(default=None, ge=1, le=60)
    status: Optional[str] = None
    approval_status: Optional[str] = None


class SplitEpisodesRequest(BaseModel):
    replace_existing: bool = True


class ScriptGenerateRequest(BaseModel):
    desired_duration_minutes: int = Field(default=5, ge=1, le=60)
    narrative_style: str = "emocionante"
    drama_level: int = Field(default=7, ge=1, le=10)
    biblical_fidelity_level: int = Field(default=9, ge=1, le=10)
    target_audience: str = "familia"
    subscribe_cta: Optional[str] = None
    next_episode_cta: Optional[str] = None


class CharacterRequest(BaseModel):
    series_id: Optional[int] = None
    name: str
    description: Optional[str] = None
    approximate_age: Optional[str] = None
    clothing: Optional[str] = None
    hair: Optional[str] = None
    default_expression: Optional[str] = None
    visual_style: Optional[str] = None
    base_prompt: Optional[str] = None
    reference_image_url: Optional[str] = None
    emotions: List[str] = Field(default_factory=list)


class ScenarioRequest(BaseModel):
    series_id: Optional[int] = None
    name: str
    description: Optional[str] = None
    base_prompt: Optional[str] = None
    visual_style: Optional[str] = None
    reference_image_url: Optional[str] = None


class PromptRequest(BaseModel):
    category: str
    title: str
    content: str
    is_active: bool = True


class ConfigRequest(BaseModel):
    text_provider: str = "openai"
    voice_provider: str = "elevenlabs"
    image_provider: str = "openai"
    video_provider: str = "luma"
    music_provider: str = "musicgen"
    caption_provider: str = "native"
    thumbnail_provider: str = "openai"
    text_api_key: Optional[str] = None
    voice_api_key: Optional[str] = None
    image_api_key: Optional[str] = None
    video_api_key: Optional[str] = None
    youtube_api_key: Optional[str] = None
    tiktok_api_key: Optional[str] = None
    instagram_api_key: Optional[str] = None
    default_voice: Optional[str] = None
    default_voice_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    default_voice_emotion: Optional[str] = None
    default_voice_intensity: float = Field(default=0.7, ge=0.0, le=1.0)
    default_language: str = "pt-BR"
    default_cta: Optional[str] = None
    default_next_episode_cta: Optional[str] = None
    default_playlist: Optional[str] = None
    made_for_kids_default: bool = False
    daily_spend_limit: float = 0.0
    monthly_spend_limit: float = 0.0
    text_cost_unit: float = 0.0
    voice_cost_unit: float = 0.0
    image_cost_unit: float = 0.0
    video_cost_unit: float = 0.0
    music_cost_unit: float = 0.0
    caption_cost_unit: float = 0.0
    thumbnail_cost_unit: float = 0.0


class JobCreateRequest(BaseModel):
    script_id: int
    platform: str = "youtube"
    aspect_ratio: str = "16:9"
    start_immediately: bool = False
    scheduled_for: Optional[str] = None
    job_type: str = "episode"


class ApprovalRequest(BaseModel):
    notes: Optional[str] = ""


class MetricRequest(BaseModel):
    job_id: Optional[int] = None
    series_id: Optional[int] = None
    episode_id: Optional[int] = None
    platform: str = "youtube"
    video_id: Optional[str] = None
    view_count: int = 0
    ctr: float = 0.0
    retention: float = 0.0
    subscribers_gained: int = 0
    likes: int = 0
    comments: int = 0
    extra: Optional[Dict[str, Any]] = None


def _save_upload(file: UploadFile, folder: str) -> Dict[str, str]:
    target_dir = Path("app/static/generated") / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch for ch in (file.filename or "arquivo.png") if ch.isalnum() or ch in {".", "_", "-"}).strip()
    if not safe_name:
        safe_name = "arquivo.png"
    filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
    target_path = target_dir / filename
    content = file.file.read()
    with open(target_path, "wb") as f:
        f.write(content)
    return {
        "absolute_path": str(target_path.absolute()),
        "public_url": f"/static/generated/{folder}/{filename}",
    }


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else ""
    if value is None:
        return ""
    return value


def _normalize_text(value: Any) -> str:
    value = _normalize_scalar(value)
    return str(value).strip()


def _normalize_int(value: Any, default: int) -> int:
    value = _normalize_scalar(value)
    if value in ("", None):
        return default
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def _enqueue_job(job_id: int):
    if not rq_queue:
        process_bible_video_job(job_id)
        return
    rq_queue.enqueue(process_bible_video_job, job_id)


@router.get("/bootstrap")
def bootstrap(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    service = get_service()
    user_id = current_user.id
    config = service.get_or_create_config(db, user_id)
    dashboard = service.build_dashboard(db, user_id)
    series = db.query(BibleVideoSeries).filter(BibleVideoSeries.user_id == user_id).order_by(BibleVideoSeries.id.desc()).limit(50).all()
    jobs = db.query(BibleVideoJob).filter(BibleVideoJob.user_id == user_id).order_by(BibleVideoJob.id.desc()).limit(60).all()
    prompts = db.query(BibleVideoPrompt).filter(BibleVideoPrompt.user_id == user_id).order_by(BibleVideoPrompt.id.desc()).limit(50).all()
    characters = db.query(BibleVideoCharacter).filter(BibleVideoCharacter.user_id == user_id).order_by(BibleVideoCharacter.id.desc()).limit(80).all()
    scenarios = db.query(BibleVideoScenario).filter(BibleVideoScenario.user_id == user_id).order_by(BibleVideoScenario.id.desc()).limit(80).all()
    metrics = db.query(BibleVideoMetric).filter(BibleVideoMetric.user_id == user_id).order_by(BibleVideoMetric.id.desc()).limit(50).all()
    return {
        "dashboard": dashboard,
        "config": service.serialize_config(config),
        "series": [service.serialize_series(row) for row in series],
        "jobs": [service.serialize_job(row) for row in jobs],
        "prompts": [service.serialize_prompt(row) for row in prompts],
        "characters": [service.serialize_character(row) for row in characters],
        "scenarios": [service.serialize_scenario(row) for row in scenarios],
        "metrics": [service.serialize_metric(row) for row in metrics],
    }


@router.get("/dashboard")
def get_dashboard(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return get_service().build_dashboard(db, current_user.id)


@router.get("/series")
def list_series(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = db.query(BibleVideoSeries).filter(BibleVideoSeries.user_id == current_user.id).order_by(BibleVideoSeries.id.desc()).all()
    return [get_service().serialize_series(row) for row in rows]


@router.post("/series")
def create_series(payload: SeriesRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = BibleVideoSeries(user_id=current_user.id)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    db.refresh(row)
    return get_service().serialize_series(row)


@router.put("/series/{series_id}")
def update_series(series_id: int, payload: SeriesRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(BibleVideoSeries).filter(BibleVideoSeries.id == series_id, BibleVideoSeries.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Serie nao encontrada.")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return get_service().serialize_series(row)


@router.delete("/series/{series_id}")
def delete_series(series_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(BibleVideoSeries).filter(BibleVideoSeries.id == series_id, BibleVideoSeries.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Serie nao encontrada.")
    db.query(BibleVideoScene).filter(BibleVideoScene.series_id == row.id).delete()
    db.query(BibleVideoScript).filter(BibleVideoScript.series_id == row.id).delete()
    db.query(BibleVideoEpisode).filter(BibleVideoEpisode.series_id == row.id).delete()
    db.query(BibleVideoCharacter).filter(BibleVideoCharacter.series_id == row.id).delete()
    db.query(BibleVideoScenario).filter(BibleVideoScenario.series_id == row.id).delete()
    db.query(BibleVideoJob).filter(BibleVideoJob.series_id == row.id).delete()
    db.query(BibleVideoMetric).filter(BibleVideoMetric.series_id == row.id).delete()
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.post("/series/{series_id}/split-episodes")
def split_episodes(series_id: int, payload: SplitEpisodesRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(BibleVideoSeries).filter(BibleVideoSeries.id == series_id, BibleVideoSeries.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Serie nao encontrada.")
    created = get_service().split_series_into_episodes(db, current_user.id, row, replace_existing=payload.replace_existing)
    return [get_service().serialize_episode(ep) for ep in created]


@router.get("/series/{series_id}/episodes")
def list_series_episodes(series_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = (
        db.query(BibleVideoEpisode)
        .filter(BibleVideoEpisode.series_id == series_id, BibleVideoEpisode.user_id == current_user.id)
        .order_by(BibleVideoEpisode.episode_number.asc())
        .all()
    )
    return [get_service().serialize_episode(row) for row in rows]


@router.put("/episodes/{episode_id}")
def update_episode(episode_id: int, payload: EpisodeUpdateRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(BibleVideoEpisode).filter(BibleVideoEpisode.id == episode_id, BibleVideoEpisode.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Episodio nao encontrado.")
    data = payload.model_dump(exclude_none=True)
    for key, value in data.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return get_service().serialize_episode(row)


@router.get("/episodes/{episode_id}/scripts")
def list_episode_scripts(episode_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = (
        db.query(BibleVideoScript)
        .filter(BibleVideoScript.episode_id == episode_id, BibleVideoScript.user_id == current_user.id)
        .order_by(BibleVideoScript.id.desc())
        .all()
    )
    return [get_service().serialize_script(row) for row in rows]


@router.post("/episodes/{episode_id}/generate-script")
async def generate_script(episode_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"raw_payload": payload}

    raw_episode_id = payload.get("episode_id") or payload.get("selected_episode") or episode_id
    raw_series_id = payload.get("series_id") or payload.get("selected_series") or ""
    desired_duration_minutes = _normalize_int(payload.get("desired_duration_minutes"), 5)
    narrative_style = _normalize_text(payload.get("narrative_style") or payload.get("tone") or "emocionante")
    drama_level = _normalize_int(payload.get("drama_level"), 7)
    biblical_fidelity_level = _normalize_int(payload.get("biblical_fidelity_level"), 9)
    target_audience = _normalize_text(payload.get("target_audience") or payload.get("audience") or "familia")
    subscribe_cta = _normalize_text(payload.get("subscribe_cta") or payload.get("call_to_action") or "")
    next_episode_cta = _normalize_text(payload.get("next_episode_cta") or payload.get("next_episode_hook") or "")

    print("[BibleVideoFactory] generate-script payload recebido:", payload)
    print(
        "[BibleVideoFactory] generate-script campos normalizados:",
        {
            "series_id": raw_series_id,
            "episode_id_path": episode_id,
            "episode_id_payload": raw_episode_id,
            "selected_series": payload.get("selected_series"),
            "selected_episode": payload.get("selected_episode"),
            "duration": desired_duration_minutes,
            "tone": narrative_style,
            "drama": drama_level,
            "biblical_fidelity": biblical_fidelity_level,
            "target_audience": target_audience,
            "call_to_action": subscribe_cta,
            "next_episode_hook": next_episode_cta,
        },
    )

    try:
        safe_episode_id = _normalize_int(raw_episode_id, episode_id)
        episode = db.query(BibleVideoEpisode).filter(BibleVideoEpisode.id == safe_episode_id, BibleVideoEpisode.user_id == current_user.id).first()
        if not episode:
            return JSONResponse(status_code=404, content={"success": False, "error": "Episodio nao encontrado."})

        series = db.query(BibleVideoSeries).filter(BibleVideoSeries.id == episode.series_id).first()
        print("[BibleVideoFactory] serie recebida:", get_service().serialize_series(series) if series else None)
        print("[BibleVideoFactory] episodio recebido:", get_service().serialize_episode(episode))

        script = get_service().generate_script_for_episode(
            db,
            user_id=current_user.id,
            episode=episode,
            desired_duration_minutes=desired_duration_minutes,
            narrative_style=narrative_style,
            drama_level=drama_level,
            biblical_fidelity_level=biblical_fidelity_level,
            target_audience=target_audience,
            subscribe_cta=subscribe_cta,
            next_episode_cta=next_episode_cta,
        )
        return get_service().serialize_script(script)
    except Exception as e:
        tb = traceback.format_exc()
        print("[BibleVideoFactory] Erro ao gerar roteiro:", str(e))
        print(tb)
        return JSONResponse(status_code=500, content={"success": False, "error": f"{str(e)}", "traceback": tb})


@router.get("/scripts/{script_id}")
def get_script(script_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(BibleVideoScript).filter(BibleVideoScript.id == script_id, BibleVideoScript.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Roteiro nao encontrado.")
    return get_service().serialize_script(row)


@router.post("/scripts/{script_id}/validate")
def validate_script(script_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    script = db.query(BibleVideoScript).filter(BibleVideoScript.id == script_id, BibleVideoScript.user_id == current_user.id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Roteiro nao encontrado.")
    episode = db.query(BibleVideoEpisode).filter(BibleVideoEpisode.id == script.episode_id).first()
    series = db.query(BibleVideoSeries).filter(BibleVideoSeries.id == script.series_id).first()
    if not episode:
        raise HTTPException(status_code=404, detail="Episodio nao encontrado.")
    updated = get_service().validate_script(db, script, episode, series)
    return get_service().serialize_script(updated)


@router.post("/scripts/{script_id}/generate-scenes")
def generate_scenes(script_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    script = db.query(BibleVideoScript).filter(BibleVideoScript.id == script_id, BibleVideoScript.user_id == current_user.id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Roteiro nao encontrado.")
    episode = db.query(BibleVideoEpisode).filter(BibleVideoEpisode.id == script.episode_id).first()
    series = db.query(BibleVideoSeries).filter(BibleVideoSeries.id == script.series_id).first()
    if not episode:
        raise HTTPException(status_code=404, detail="Episodio nao encontrado.")
    created = get_service().generate_scenes(db, script, episode, series)
    return [get_service().serialize_scene(row) for row in created]


@router.get("/scripts/{script_id}/scenes")
def list_scenes(script_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = (
        db.query(BibleVideoScene)
        .filter(BibleVideoScene.script_id == script_id, BibleVideoScene.user_id == current_user.id)
        .order_by(BibleVideoScene.scene_number.asc())
        .all()
    )
    return [get_service().serialize_scene(row) for row in rows]


@router.post("/scripts/{script_id}/generate-shorts")
def generate_shorts(script_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    script = db.query(BibleVideoScript).filter(BibleVideoScript.id == script_id, BibleVideoScript.user_id == current_user.id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Roteiro nao encontrado.")
    episode = db.query(BibleVideoEpisode).filter(BibleVideoEpisode.id == script.episode_id).first()
    if not episode:
        raise HTTPException(status_code=404, detail="Episodio nao encontrado.")
    return get_service().generate_shorts_bundle(db, script, episode)


@router.post("/scripts/{script_id}/generate-thumbnail")
def generate_thumbnail(script_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    script = db.query(BibleVideoScript).filter(BibleVideoScript.id == script_id, BibleVideoScript.user_id == current_user.id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Roteiro nao encontrado.")
    episode = db.query(BibleVideoEpisode).filter(BibleVideoEpisode.id == script.episode_id).first()
    series = db.query(BibleVideoSeries).filter(BibleVideoSeries.id == script.series_id).first()
    if not episode:
        raise HTTPException(status_code=404, detail="Episodio nao encontrado.")
    return get_service().generate_thumbnail(db, script, episode, series)


@router.get("/characters")
def list_characters(series_id: Optional[int] = Query(None), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    q = db.query(BibleVideoCharacter).filter(BibleVideoCharacter.user_id == current_user.id)
    if series_id:
        q = q.filter(BibleVideoCharacter.series_id == series_id)
    rows = q.order_by(BibleVideoCharacter.id.desc()).all()
    return [get_service().serialize_character(row) for row in rows]


@router.post("/characters")
def create_character(payload: CharacterRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = BibleVideoCharacter(user_id=current_user.id, emotions_json=get_service()._json_dumps(payload.emotions))
    for key, value in payload.model_dump(exclude={"emotions"}).items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    db.refresh(row)
    return get_service().serialize_character(row)


@router.put("/characters/{character_id}")
def update_character(character_id: int, payload: CharacterRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(BibleVideoCharacter).filter(BibleVideoCharacter.id == character_id, BibleVideoCharacter.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Personagem nao encontrado.")
    for key, value in payload.model_dump(exclude={"emotions"}).items():
        setattr(row, key, value)
    row.emotions_json = get_service()._json_dumps(payload.emotions)
    db.commit()
    db.refresh(row)
    return get_service().serialize_character(row)


@router.delete("/characters/{character_id}")
def delete_character(character_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(BibleVideoCharacter).filter(BibleVideoCharacter.id == character_id, BibleVideoCharacter.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Personagem nao encontrado.")
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.get("/scenarios")
def list_scenarios(series_id: Optional[int] = Query(None), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    q = db.query(BibleVideoScenario).filter(BibleVideoScenario.user_id == current_user.id)
    if series_id:
        q = q.filter(BibleVideoScenario.series_id == series_id)
    rows = q.order_by(BibleVideoScenario.id.desc()).all()
    return [get_service().serialize_scenario(row) for row in rows]


@router.post("/scenarios")
def create_scenario(payload: ScenarioRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = BibleVideoScenario(user_id=current_user.id)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    db.refresh(row)
    return get_service().serialize_scenario(row)


@router.put("/scenarios/{scenario_id}")
def update_scenario(scenario_id: int, payload: ScenarioRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(BibleVideoScenario).filter(BibleVideoScenario.id == scenario_id, BibleVideoScenario.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Cenario nao encontrado.")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return get_service().serialize_scenario(row)


@router.delete("/scenarios/{scenario_id}")
def delete_scenario(scenario_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(BibleVideoScenario).filter(BibleVideoScenario.id == scenario_id, BibleVideoScenario.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Cenario nao encontrado.")
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.get("/prompts")
def list_prompts(category: Optional[str] = Query(None), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    q = db.query(BibleVideoPrompt).filter(BibleVideoPrompt.user_id == current_user.id)
    if category:
        q = q.filter(BibleVideoPrompt.category == category)
    rows = q.order_by(BibleVideoPrompt.id.desc()).all()
    return [get_service().serialize_prompt(row) for row in rows]


@router.post("/prompts")
def create_prompt(payload: PromptRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = BibleVideoPrompt(user_id=current_user.id, **payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return get_service().serialize_prompt(row)


@router.put("/prompts/{prompt_id}")
def update_prompt(prompt_id: int, payload: PromptRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(BibleVideoPrompt).filter(BibleVideoPrompt.id == prompt_id, BibleVideoPrompt.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Prompt nao encontrado.")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return get_service().serialize_prompt(row)


@router.delete("/prompts/{prompt_id}")
def delete_prompt(prompt_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(BibleVideoPrompt).filter(BibleVideoPrompt.id == prompt_id, BibleVideoPrompt.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Prompt nao encontrado.")
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.get("/config")
def get_config(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = get_service().get_or_create_config(db, current_user.id)
    return get_service().serialize_config(row)


@router.post("/config")
def save_config(payload: ConfigRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    service = get_service()
    row = service.get_or_create_config(db, current_user.id)
    for key, value in payload.model_dump().items():
        if key.endswith("_api_key") and value in (None, ""):
            continue
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return service.serialize_config(row)


@router.get("/jobs")
def list_jobs(
    status: Optional[str] = Query(None),
    stage: Optional[str] = Query(None),
    series_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(BibleVideoJob).filter(BibleVideoJob.user_id == current_user.id)
    if status:
        q = q.filter(BibleVideoJob.status == status)
    if stage:
        q = q.filter(BibleVideoJob.kanban_stage == stage)
    if series_id:
        q = q.filter(BibleVideoJob.series_id == series_id)
    rows = q.order_by(BibleVideoJob.id.desc()).all()
    return [get_service().serialize_job(row) for row in rows]


@router.post("/jobs")
def create_job(payload: JobCreateRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    script = db.query(BibleVideoScript).filter(BibleVideoScript.id == payload.script_id, BibleVideoScript.user_id == current_user.id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Roteiro nao encontrado.")
    scheduled_for = None
    if payload.scheduled_for:
        try:
            scheduled_for = datetime.fromisoformat(payload.scheduled_for)
        except Exception:
            raise HTTPException(status_code=400, detail="scheduled_for invalido.")
    row = get_service().create_job(
        db,
        user_id=current_user.id,
        script=script,
        platform=payload.platform,
        aspect_ratio=payload.aspect_ratio,
        start_immediately=payload.start_immediately,
        scheduled_for=scheduled_for,
        job_type=payload.job_type,
    )
    if payload.start_immediately:
        _enqueue_job(row.id)
        db.refresh(row)
    return get_service().serialize_job(row)


@router.post("/jobs/{job_id}/start")
def start_job(job_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(BibleVideoJob).filter(BibleVideoJob.id == job_id, BibleVideoJob.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Job nao encontrado.")
    row.status = "queued"
    row.status_message = "Job enviado para processamento."
    db.commit()
    _enqueue_job(row.id)
    db.refresh(row)
    return {"status": "queued", "job": get_service().serialize_job(row)}


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(BibleVideoJob).filter(BibleVideoJob.id == job_id, BibleVideoJob.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Job nao encontrado.")
    return get_service().serialize_job(row)


@router.post("/jobs/{job_id}/approve")
def approve_job(job_id: int, payload: ApprovalRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(BibleVideoJob).filter(BibleVideoJob.id == job_id, BibleVideoJob.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Job nao encontrado.")
    updated = get_service().approve_job(db, row, notes=_normalize_text(payload.notes))
    return get_service().serialize_job(updated)


@router.post("/jobs/{job_id}/publish")
def publish_job(job_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(BibleVideoJob).filter(BibleVideoJob.id == job_id, BibleVideoJob.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Job nao encontrado.")
    try:
        result = get_service().publish_job(db, row)
        db.refresh(row)
        return {"job": get_service().serialize_job(row), "publish_result": result}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/metrics")
def list_metrics(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = db.query(BibleVideoMetric).filter(BibleVideoMetric.user_id == current_user.id).order_by(BibleVideoMetric.id.desc()).all()
    return [get_service().serialize_metric(row) for row in rows]


@router.post("/metrics")
def create_metric(payload: MetricRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = BibleVideoMetric(
        user_id=current_user.id,
        job_id=payload.job_id,
        series_id=payload.series_id,
        episode_id=payload.episode_id,
        platform=payload.platform,
        video_id=payload.video_id,
        view_count=payload.view_count,
        ctr=payload.ctr,
        retention=payload.retention,
        subscribers_gained=payload.subscribers_gained,
        likes=payload.likes,
        comments=payload.comments,
        extra_json=get_service()._json_dumps(payload.extra or {}),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return get_service().serialize_metric(row)


@router.post("/uploads/character-reference")
async def upload_character_reference(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    _ = current_user.id
    return _save_upload(file, "bible_video_factory/characters")


@router.post("/uploads/scenario-reference")
async def upload_scenario_reference(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    _ = current_user.id
    return _save_upload(file, "bible_video_factory/scenarios")
