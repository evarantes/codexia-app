import os
import glob
import shutil
import json
import uuid
from datetime import datetime, timedelta
try:
    from filelock import FileLock, Timeout
except Exception:
    # Fallback para ambientes sem dependência instalada.
    # Mantém o app inicializando e evita quebra total do deploy.
    class Timeout(Exception):
        pass

    class FileLock:  # type: ignore
        def __init__(self, *_args, **_kwargs):
            self._locked = False

        def acquire(self, *_args, **_kwargs):
            self._locked = True
            return True

        def release(self):
            self._locked = False
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
try:
    from rq import Worker
    RQ_AVAILABLE = True
except Exception:
    # No Windows, RQ pode falhar devido ao fork()
    RQ_AVAILABLE = False
    Worker = None
from app.services.youtube_service import YouTubeService
from app.services.ai_generator import AIContentGenerator
from app.services.task_manager import create_task, update_task, get_task
from app.database import get_db, SessionLocal
from app.services.video_factory import VideoFactory
from app.models import ScheduledVideo, ChannelReport, Settings, ContentPlan, Video, Job, Asset, Scene
from app.redis_client import conn

FACTORY_LOCK_KEY = "codexia:video_factory:single_worker_lock"
# Lock file para quando Redis não está disponível (garante 1 job por vez)
_lock_dir = "/data" if os.path.isdir("/data") else os.path.expanduser("~")
_FACTORY_LOCK_PATH = os.path.join(_lock_dir, ".codexia_factory.lock")

def _rq_workers_online() -> bool:
    """Retorna True quando há pelo menos um worker RQ ouvindo a fila."""
    if not conn or not RQ_AVAILABLE or Worker is None:
        return False
    try:
        return Worker.count(conn) > 0
    except Exception:
        return False

def process_jobs_background():
    """Background task to process video generation jobs. Um vídeo por vez."""
    db = SessionLocal()
    redis_lock = None
    file_lock = None
    try:
        if conn:
            try:
                redis_lock = conn.lock(FACTORY_LOCK_KEY, timeout=4 * 60 * 60, blocking_timeout=1)
                if not redis_lock.acquire(blocking=False):
                    return  # Outro worker já está processando
            except Exception as e:
                print(f"Error acquiring Redis factory lock: {e}")
                redis_lock = None

        # Sem Redis: usar file lock para garantir 1 job por vez (evita múltiplos processando)
        if not conn or not redis_lock:
            try:
                file_lock = FileLock(_FACTORY_LOCK_PATH, timeout=0)
                file_lock.acquire()
            except Timeout:
                return  # Outro processo já está processando
            except Exception as e:
                print(f"Error acquiring file factory lock: {e}")

        if _rq_workers_online():
            return

        factory = VideoFactory(db)
        factory.process_next_job()
    except Exception as e:
        print(f"Error processing background job: {e}")
    finally:
        if redis_lock:
            try:
                redis_lock.release()
            except Exception:
                pass
        if file_lock:
            try:
                file_lock.release()
            except Exception:
                pass
        db.close()

def _resolve_video_file_path(raw_path: Optional[str]) -> str:
    """
    Resolve path robusto para arquivos de vídeo, cobrindo:
    - path absoluto salvo no banco
    - path relativo legado
    - URL /media/videos/... ou /static/videos/...
    """
    if not raw_path:
        return ""

    value = str(raw_path).strip()
    if not value:
        return ""
    # Normaliza separador e remove query/hash legados
    value = value.replace("\\", "/").split("?", 1)[0].split("#", 1)[0].strip()
    if not value:
        return ""

    candidates: List[str] = []
    if os.path.isabs(value):
        candidates.append(value)

    # Relativo ao cwd atual (legado)
    candidates.append(os.path.abspath(value))

    try:
        from app.config import absolute_path_for_video, STATIC_DIR
        candidates.append(absolute_path_for_video(value))
        name = os.path.basename(value)
        if name:
            candidates.append(os.path.join("/data", "media", "videos", name))
            candidates.append(str(STATIC_DIR / "videos" / name))
            candidates.append(os.path.join("/app", "static", "videos", name))  # legado em alguns deploys
    except Exception:
        pass

    checked = set()
    for path in candidates:
        if not path or path in checked:
            continue
        checked.add(path)
        if os.path.exists(path) and os.path.isfile(path):
            return path
    return ""

def _normalize_video_url_for_client(raw_url: Optional[str]) -> Optional[str]:
    """Normaliza URLs legadas/paths absolutos para URL pública reproduzível no browser."""
    if not raw_url:
        return raw_url

    value = str(raw_url).strip()
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("/media/videos/") or value.startswith("/static/videos/"):
        return value

    try:
        from app.config import VIDEO_URL_PREFIX
        resolved = _resolve_video_file_path(value)
        name = os.path.basename(resolved) if resolved else os.path.basename(value)
        if name:
            return f"{VIDEO_URL_PREFIX}/{name}"
    except Exception:
        pass
    return value

def _latest_final_asset_path(db: Session, video_id: int) -> str:
    """Retorna o caminho existente do asset FINAL mais recente do vídeo."""
    assets = (
        db.query(Asset)
        .filter(Asset.video_id == video_id, Asset.kind == "FINAL")
        .order_by(Asset.created_at.desc(), Asset.id.desc())
        .all()
    )
    for asset in assets:
        resolved = _resolve_video_file_path(asset.storage_key)
        if resolved:
            return resolved
    return ""

def _normalize_video_status(value: Optional[str]) -> str:
    """Normaliza status de vídeo para evitar divergência de caixa/espaços/legado."""
    raw = (value or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    aliases = {
        "COMPLETED": "READY",
        "FAILED": "ERROR",
    }
    return aliases.get(upper, upper)

def _progress_from_video_status(status: Optional[str]) -> int:
    """Fallback de progresso quando o job ativo não reportou progresso ainda."""
    s = _normalize_video_status(status)
    mapping = {
        "QUEUED": 5,
        "SCRIPT": 25,
        "TTS": 45,
        "VISUALS": 65,
        "RENDER": 85,
        "READY": 100,
        "PUBLISHED": 100,
        "PAUSED": 0,
        "CANCELLED": 0,
        "ERROR": 0,
    }
    return mapping.get(s, 0)

def _last_log_line(logs: Optional[str], max_len: int = 220) -> str:
    """Extrai a última linha útil dos logs para exibir status curto na UI."""
    text = (logs or "").strip()
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    msg = lines[-1]
    if len(msg) > max_len:
        msg = msg[: max_len - 3] + "..."
    return msg

def _is_mock_upload(upload_result: Any) -> bool:
    return isinstance(upload_result, dict) and upload_result.get("status") == "uploaded_mock"

def _publish_error_message(upload_result: Any, action_label: str = "publicar") -> str:
    """Mensagem amigável e consistente para falhas de upload no YouTube."""
    if _is_mock_upload(upload_result):
        return "Canal não conectado ao YouTube. Configure as credenciais em Configurações antes de publicar."
    if isinstance(upload_result, dict):
        raw = (upload_result.get("error") or "").strip()
        if raw:
            return raw
    raw = str(upload_result or "").strip()
    if raw and raw not in {"{}", "None"}:
        return raw
    return (
        f"Falha ao {action_label} no YouTube. Verifique as credenciais em Configurações "
        f"ou variáveis YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN."
    )

def _append_upload_error_note(description: Optional[str], message: str) -> str:
    note = f"[UPLOAD_ERRO]: {message}"
    current = (description or "").strip()
    if note in current:
        return current
    if current:
        return f"{current}\n\n{note}"
    return note

def _infer_resume_step(db: Session, video: Video) -> Optional[str]:
    """Infere próxima etapa para retomar produção após pausa."""
    paused_or_pending = (
        db.query(Job)
        .filter(Job.video_id == video.id, Job.status.in_(["paused", "pending"]))
        .order_by(Job.created_at.asc(), Job.id.asc())
        .first()
    )
    if paused_or_pending:
        return (paused_or_pending.step or "").strip().lower() or "script"

    latest_completed = (
        db.query(Job)
        .filter(Job.video_id == video.id, Job.status == "completed")
        .order_by(Job.created_at.desc(), Job.id.desc())
        .first()
    )
    if latest_completed:
        step = (latest_completed.step or "").strip().lower()
        next_map = {
            "script": "tts",
            "tts": "visuals",
            "visuals": "render",
        }
        return next_map.get(step)

    status = _normalize_video_status(video.status)
    from_status = {
        "QUEUED": "script",
        "SCRIPT": "tts",
        "TTS": "visuals",
        "VISUALS": "render",
    }
    return from_status.get(status, "script")

def _build_public_video_url_from_path(resolved_path: Optional[str]) -> Optional[str]:
    """Converte path físico do vídeo para URL pública servida pela API."""
    if not resolved_path:
        return None
    name = os.path.basename(str(resolved_path).strip())
    if not name:
        return None
    try:
        from app.config import VIDEO_URL_PREFIX
        return f"{VIDEO_URL_PREFIX}/{name}"
    except Exception:
        return f"/media/videos/{name}"

def _find_scheduled_mirror_by_source(db: Session, production_video_id: int) -> Optional[ScheduledVideo]:
    """Encontra item em scheduled_videos criado a partir do vídeo de produção."""
    candidates = (
        db.query(ScheduledVideo)
        .filter(ScheduledVideo.script_data.isnot(None))
        .filter(ScheduledVideo.script_data.contains("source_production_video_id"))
        .all()
    )
    for item in candidates:
        try:
            data = json.loads(item.script_data or "{}")
            if str(data.get("source_production_video_id")) == str(production_video_id):
                return item
        except Exception:
            continue
    return None

def _build_scheduled_mirror_index(db: Session) -> Dict[str, ScheduledVideo]:
    """Indexa scheduled_videos espelhados por source_production_video_id."""
    index: Dict[str, ScheduledVideo] = {}
    candidates = (
        db.query(ScheduledVideo)
        .filter(ScheduledVideo.script_data.isnot(None))
        .filter(ScheduledVideo.script_data.contains("source_production_video_id"))
        .all()
    )
    for item in candidates:
        try:
            data = json.loads(item.script_data or "{}")
            source_id = data.get("source_production_video_id")
            if source_id is not None:
                index[str(source_id)] = item
        except Exception:
            continue
    return index

def _upsert_scheduled_from_production(db: Session, video: Video, mirror_index: Optional[Dict[str, ScheduledVideo]] = None):
    """Garante que vídeo READY/PUBLISHED da produção apareça na fila de aguardando publicação."""
    norm_status = _normalize_video_status(video.status)
    if norm_status not in {"READY", "PUBLISHED"}:
        return

    plan = video.plan
    final_path = _latest_final_asset_path(db, video.id) or _resolve_video_file_path(video.youtube_video_id)
    public_video_url = _normalize_video_url_for_client(_build_public_video_url_from_path(final_path)) if final_path else None

    if mirror_index is not None:
        mirror = mirror_index.get(str(video.id))
    else:
        mirror = _find_scheduled_mirror_by_source(db, video.id)
    payload = {}
    if mirror and mirror.script_data:
        try:
            payload = json.loads(mirror.script_data)
        except Exception:
            payload = {}
    payload.update({
        "source": "production_queue",
        "source_production_video_id": video.id,
        "production_status": norm_status,
    })

    target_status = "published" if norm_status == "PUBLISHED" else "completed"
    target_type = "short" if (video.type or "").upper() == "SHORT" else "video"
    target_scheduled_for = video.scheduled_at or (mirror.scheduled_for if mirror else None) or datetime.now()

    if mirror:
        mirror.theme = (plan.theme if plan and getattr(plan, "theme", None) else mirror.theme) or "Produção"
        mirror.title = video.title or mirror.title or f"Vídeo {video.id}"
        mirror.description = video.description or mirror.description or ""
        mirror.scheduled_for = target_scheduled_for
        mirror.status = target_status
        mirror.progress = 100
        mirror.video_type = target_type
        mirror.voice_style = getattr(plan, "voice_style", None) or mirror.voice_style or "human"
        mirror.voice_gender = getattr(plan, "voice_gender", None) or mirror.voice_gender or "female"
        if public_video_url:
            mirror.video_url = public_video_url
        if norm_status == "PUBLISHED" and video.youtube_video_id:
            mirror.youtube_video_id = video.youtube_video_id
            mirror.uploaded_at = mirror.uploaded_at or datetime.now()
        mirror.script_data = json.dumps(payload)
    else:
        mirror = ScheduledVideo(
            theme=(plan.theme if plan and getattr(plan, "theme", None) else "Produção"),
            title=video.title or f"Vídeo {video.id}",
            description=video.description or "",
            scheduled_for=target_scheduled_for,
            status=target_status,
            video_type=target_type,
            script_data=json.dumps(payload),
            video_url=public_video_url,
            progress=100,
            auto_post=False,
            voice_style=getattr(plan, "voice_style", "human") if plan else "human",
            voice_gender=getattr(plan, "voice_gender", "female") if plan else "female",
            music_file_path=getattr(plan, "music_file", None) if plan else None,
            youtube_video_id=(video.youtube_video_id if norm_status == "PUBLISHED" else None),
            uploaded_at=(datetime.now() if norm_status == "PUBLISHED" else None),
        )
        db.add(mirror)
        if mirror_index is not None:
            mirror_index[str(video.id)] = mirror

def _sync_ready_production_to_scheduled(db: Session, limit: int = 200):
    """Sincroniza vídeos READY/PUBLISHED da produção para a fila de aguardando publicação."""
    from sqlalchemy import func
    candidates = (
        db.query(Video)
        .filter(func.upper(func.trim(Video.status)).in_(["READY", "PUBLISHED", "COMPLETED"]))
        .order_by(Video.created_at.desc())
        .limit(limit)
        .all()
    )
    mirror_index = _build_scheduled_mirror_index(db)
    for video in candidates:
        _upsert_scheduled_from_production(db, video, mirror_index=mirror_index)
    db.commit()

def _delete_scheduled_mirror(db: Session, production_video_id: int):
    """Remove item espelho em scheduled_videos de um vídeo de produção."""
    mirror = _find_scheduled_mirror_by_source(db, production_video_id)
    if mirror:
        db.delete(mirror)

router = APIRouter(
    prefix="/youtube",
    tags=["youtube"],
    responses={404: {"description": "Not found"}},
)

# --- Video Factory Models & Endpoints ---

class PlanRequest(BaseModel):
    mode: str = "theme" # theme | music
    theme: Optional[str] = None
    music_file: Optional[str] = None
    days: int = 7
    videos_per_day: int = 1
    shorts_per_day: int = 1
    duration_min: int = 8
    voice_style: str = "human"
    voice_gender: str = "female"
    start_date: str  # YYYY-MM-DD

@router.post("/auto/plans")
def create_content_plan(plan: PlanRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Cria um novo plano de conteúdo e enfileira a geração."""
    # TODO: Get user_id from auth (using 1 for now as placeholder if no auth)
    user_id = 1 
    
    factory = VideoFactory(db)
    new_plan = factory.create_plan(plan.dict(), user_id=user_id)
    
    # Trigger processing in background (MVP without Redis for now)
    background_tasks.add_task(process_jobs_background)
    
    return {"status": "Plan created", "plan_id": new_plan.id, "message": "Vídeos enfileirados para produção."}

@router.get("/auto/plans/{plan_id}")
def get_content_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.query(ContentPlan).filter(ContentPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan

@router.get("/auto/stats")
def get_production_stats(db: Session = Depends(get_db)):
    """Retorna contagem de vídeos/plans para diagnóstico (ex: vídeos sumiram após deploy)."""
    from sqlalchemy import func
    total_videos = db.query(Video).count()
    total_plans = db.query(ContentPlan).count()
    by_status = (
        db.query(func.upper(func.trim(Video.status)).label("s"), func.count(Video.id))
        .group_by(func.upper(func.trim(Video.status)))
        .all()
    )
    return {
        "total_videos": total_videos,
        "total_plans": total_plans,
        "videos_by_status": {s or "null": c for s, c in by_status},
    }

def _reset_stuck_jobs(db: Session, timeout_minutes: int = 10):
    """Reseta Jobs travados em 'processing' há muito tempo (ex: servidor reiniciou)."""
    from datetime import timedelta
    from sqlalchemy import func
    cutoff = datetime.now() - timedelta(minutes=timeout_minutes)
    stuck = (
        db.query(Job)
        .filter(Job.status == "processing")
        .filter(func.coalesce(Job.updated_at, Job.created_at) < cutoff)
        .all()
    )
    for j in stuck:
        j.status = "pending"
        j.progress = 0
        j.logs = (j.logs or "") + f"\n[Recovery] Job travado por {timeout_minutes}+ min. Reenfileirado em {datetime.now()}."
        v = db.query(Video).get(j.video_id)
        if v and (v.status or "").upper() not in ("PAUSED", "CANCELLED", "CANCELED"):
            v.status = "queued"
        print(f"[Factory] Recovery: Job {j.id} (video {j.video_id}) reenfileirado.")
    if stuck:
        db.commit()

@router.get("/auto/queue")
def get_production_queue(background_tasks: BackgroundTasks, status: Optional[str] = None, limit: int = 50, db: Session = Depends(get_db)):
    """Retorna a fila de produção (vídeos e jobs). Dispara processamento se houver jobs pendentes."""
    _reset_stuck_jobs(db)
    pending = db.query(Job).filter(Job.status == "pending").first()
    processing = db.query(Job).filter(Job.status == "processing").first()
    if pending and not processing:
        background_tasks.add_task(process_jobs_background)
    query = db.query(Video).order_by(Video.scheduled_at.asc())
    
    if status:
        from sqlalchemy import func
        normalized_status = (status or "").strip().upper()
        query = query.filter(func.upper(func.trim(Video.status)) == normalized_status)
        
    videos = query.limit(limit).all()
    
    result = []
    for v in videos:
        normalized_video_status = _normalize_video_status(v.status)
        # Prioridade: job em processamento > pendente > último job
        processing_job = (
            db.query(Job)
            .filter(Job.video_id == v.id, Job.status == "processing")
            .order_by(Job.created_at.desc())
            .first()
        )
        pending_job = (
            db.query(Job)
            .filter(Job.video_id == v.id, Job.status == "pending")
            .order_by(Job.created_at.desc())
            .first()
        )
        latest_job = (
            db.query(Job)
            .filter(Job.video_id == v.id)
            .order_by(Job.created_at.desc())
            .first()
        )
        active_job = processing_job or pending_job or latest_job

        fallback_progress = _progress_from_video_status(normalized_video_status)
        job_progress = int(active_job.progress or 0) if active_job else 0
        # Quando há job em processamento, usar progresso real (evita travar em 85% do fallback RENDER)
        if active_job and active_job.status == "processing" and job_progress > 0:
            progress = job_progress
        else:
            progress = max(job_progress, fallback_progress)

        if normalized_video_status == "PAUSED":
            current_step = "paused"
        elif normalized_video_status == "CANCELLED":
            current_step = "cancelled"
        elif active_job:
            if active_job.status == "processing":
                current_step = active_job.step or "processing"
            elif active_job.status == "pending":
                current_step = active_job.step or "queued"
            else:
                current_step = active_job.step or "queued"
        else:
            current_step = "queued"

        status_message = _last_log_line(active_job.logs if active_job else "")
        if not status_message:
            if active_job and active_job.status == "processing":
                status_message = f"Processando etapa: {active_job.step or 'produção'}..."
            elif active_job and active_job.status == "pending":
                status_message = f"Aguardando início da etapa: {active_job.step or 'produção'}."
            elif normalized_video_status == "QUEUED":
                status_message = "Aguardando vez na fila de produção."
            elif normalized_video_status == "PAUSED":
                status_message = "Produção pausada pelo usuário."
            elif normalized_video_status == "CANCELLED":
                status_message = "Produção cancelada pelo usuário."

        result.append({
            "id": v.id,
            "title": v.title,
            "type": v.type,
            "status": normalized_video_status,
            "created_at": v.created_at,
            "scheduled_at": v.scheduled_at,
            "progress": progress,
            "current_step": current_step,
            "status_message": status_message,
            "logs": active_job.logs if active_job else "",
            "youtube_id": v.youtube_video_id
        })
    
    return result

@router.post("/videos/{video_id}/retry")
def retry_video_step(video_id: int, step: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Reinicia uma etapa específica para um vídeo com erro."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # Reset status para permitir reprocessamento (vídeos em erro ficam travados)
    if (video.status or "").upper() in {"ERROR", "FAILED"}:
        video.status = "queued"

    # Mapeia nomes do frontend para steps do VideoFactory
    raw_step = (step or "").strip().lower()
    step_map = {
        "script_generate": "script",
        "queued": "script",
        "error": "script",
    }
    factory_step = step_map.get(raw_step, raw_step)
    valid_steps = {"script", "tts", "visuals", "render", "shorts_extract"}
    if factory_step not in valid_steps:
        factory_step = "script"

    factory = VideoFactory(db)
    factory._add_job(video.id, factory_step)
    db.commit()

    background_tasks.add_task(process_jobs_background)

    return {"status": "Job added", "step": factory_step}

@router.post("/videos/{video_id}/publish")
def publish_video(video_id: int, db: Session = Depends(get_db)):
    """Publica o vídeo no YouTube (Integração real)."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
        
    # Check if video is ready
    if (video.status or "").upper() != "READY":
        raise HTTPException(status_code=400, detail="Video is not ready for publication")
        
    # Get Final Asset
    final_path = _latest_final_asset_path(db, video.id)
    if not final_path:
        # Compatibilidade com registros legados que salvaram path em youtube_video_id
        final_path = _resolve_video_file_path(video.youtube_video_id)
    if not final_path:
        raise HTTPException(status_code=500, detail="Video file not found")
        
    # Call YouTube Service (real; quando não conectado, service retorna mock id)
    try:
        tags: List[str] = []
        if video.tags:
            tags = [t.strip() for t in str(video.tags).split(",") if t.strip()]

        service = YouTubeService()
        upload_result = service.upload_video(
            final_path,
            title=video.title or f"Vídeo {video.id}",
            description=video.description or "Vídeo gerado automaticamente por Codexia.",
            tags=tags
        )

        is_error = False
        youtube_id = None
        if isinstance(upload_result, dict):
            if upload_result.get("error"):
                is_error = True
            elif _is_mock_upload(upload_result):
                is_error = True
            else:
                youtube_id = upload_result.get("id") or str(upload_result)
        else:
            youtube_id = str(upload_result) if upload_result else None
            if not youtube_id:
                is_error = True

        if is_error or not youtube_id:
            # Falha de publicação não deve destruir estado READY do vídeo gerado.
            # Isso permite corrigir credenciais e tentar publicar novamente sem reprocessar.
            video.status = "READY"
            err_msg = _publish_error_message(upload_result, action_label="publicar")
            video.description = _append_upload_error_note(video.description, err_msg)
            db.commit()
            raise HTTPException(status_code=502, detail=err_msg)
        
        video.status = "PUBLISHED"
        video.published_at = datetime.now()
        video.youtube_video_id = youtube_id
        _upsert_scheduled_from_production(db, video)
        db.commit()
        
        return {"status": "Published", "youtube_id": youtube_id}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/videos/{video_id}/schedule")
def schedule_production_video(video_id: int, data: Dict[str, Any], db: Session = Depends(get_db)):
    """Atualiza data/hora agendada de publicação para vídeo da fila de produção."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    dt_raw = (data.get("scheduled_at") or data.get("scheduled_for") or "").strip()
    if not dt_raw:
        raise HTTPException(status_code=400, detail="Data de agendamento não informada.")

    try:
        try:
            scheduled_at = datetime.fromisoformat(dt_raw)
        except Exception:
            scheduled_at = datetime.strptime(dt_raw, "%Y-%m-%dT%H:%M")
    except Exception:
        raise HTTPException(status_code=400, detail="Formato de data inválido.")

    video.scheduled_at = scheduled_at
    _upsert_scheduled_from_production(db, video)
    db.commit()
    return {
        "status": "scheduled",
        "id": video.id,
        "scheduled_at": video.scheduled_at.isoformat() if video.scheduled_at else None,
    }

@router.post("/videos/{video_id}/pause")
def pause_production_video(video_id: int, db: Session = Depends(get_db)):
    """Pausa a produção de um vídeo (cooperativo entre etapas)."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    status = _normalize_video_status(video.status)
    if status in {"READY", "PUBLISHED"}:
        raise HTTPException(status_code=400, detail="Vídeo já concluído/publicado; não há produção para pausar.")
    if status == "CANCELLED":
        raise HTTPException(status_code=400, detail="Vídeo cancelado não pode ser pausado.")
    if status == "PAUSED":
        return {"status": "paused", "message": "Produção já está pausada."}

    pending_jobs = db.query(Job).filter(Job.video_id == video.id, Job.status == "pending").all()
    for j in pending_jobs:
        j.status = "paused"
        j.logs = (j.logs or "") + "Pausa solicitada pelo usuário.\n"

    processing_job = (
        db.query(Job)
        .filter(Job.video_id == video.id, Job.status == "processing")
        .order_by(Job.created_at.desc())
        .first()
    )
    if processing_job:
        processing_job.logs = (processing_job.logs or "") + "Pausa solicitada pelo usuário (aplicada após a etapa atual).\n"

    video.status = "PAUSED"
    db.commit()
    return {"status": "paused", "message": "Produção pausada com sucesso."}

@router.post("/videos/{video_id}/resume")
def resume_production_video(video_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Retoma a produção de um vídeo pausado."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    status = _normalize_video_status(video.status)
    if status == "CANCELLED":
        raise HTTPException(status_code=400, detail="Vídeo cancelado não pode ser retomado.")
    if status != "PAUSED":
        raise HTTPException(status_code=400, detail="Apenas vídeos pausados podem ser retomados.")

    processing_job = (
        db.query(Job)
        .filter(Job.video_id == video.id, Job.status == "processing")
        .order_by(Job.created_at.desc())
        .first()
    )
    if processing_job:
        # Caso raro: pausa solicitada e retomada quase simultânea.
        video.status = (processing_job.step or "processing").upper()
        db.commit()
        return {"status": "processing", "message": "Vídeo já estava em processamento."}

    paused_jobs = (
        db.query(Job)
        .filter(Job.video_id == video.id, Job.status == "paused")
        .order_by(Job.created_at.asc(), Job.id.asc())
        .all()
    )
    if paused_jobs:
        for j in paused_jobs:
            j.status = "pending"
            j.logs = (j.logs or "") + "Produção retomada pelo usuário.\n"

    next_step = _infer_resume_step(db, video)
    if not next_step:
        # Não há etapa restante: marca como pronto.
        video.status = "READY"
        _upsert_scheduled_from_production(db, video)
        db.commit()
        return {"status": "ready", "message": "Vídeo já estava concluído."}

    video.status = "queued"
    db.commit()

    factory = VideoFactory(db)
    factory._add_job(video.id, next_step)
    background_tasks.add_task(process_jobs_background)
    return {"status": "queued", "step": next_step, "message": "Produção retomada com sucesso."}

@router.post("/videos/{video_id}/cancel")
def cancel_production_video(video_id: int, db: Session = Depends(get_db)):
    """Cancela a produção de um vídeo."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    status = _normalize_video_status(video.status)
    if status in {"READY", "PUBLISHED"}:
        raise HTTPException(status_code=400, detail="Vídeo já concluído/publicado; não é possível cancelar produção.")
    if status == "CANCELLED":
        return {"status": "cancelled", "message": "Produção já estava cancelada."}

    queued_jobs = (
        db.query(Job)
        .filter(Job.video_id == video.id, Job.status.in_(["pending", "paused"]))
        .all()
    )
    for j in queued_jobs:
        j.status = "cancelled"
        j.logs = (j.logs or "") + "Produção cancelada pelo usuário.\n"

    processing_job = (
        db.query(Job)
        .filter(Job.video_id == video.id, Job.status == "processing")
        .order_by(Job.created_at.desc())
        .first()
    )
    if processing_job:
        processing_job.logs = (processing_job.logs or "") + "Cancelamento solicitado pelo usuário (aplicado após a etapa atual).\n"

    video.status = "CANCELLED"
    db.commit()
    return {"status": "cancelled", "message": "Produção cancelada com sucesso."}

@router.post("/videos/{video_id}/regenerate")
def regenerate_production_video(video_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Refaz um vídeo da fila de produção desde a etapa de script."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # Remove vídeos derivados (shorts) para refazer pipeline limpo
    children = db.query(Video).filter(Video.parent_video_id == video.id).all()
    for child in children:
        child_assets = db.query(Asset).filter(Asset.video_id == child.id).all()
        for asset in child_assets:
            path = _resolve_video_file_path(asset.storage_key)
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    print(f"Erro ao remover asset do derivado {path}: {e}")
        db.delete(child)

    # Remove arquivos físicos já gerados do vídeo principal
    assets = db.query(Asset).filter(Asset.video_id == video.id).all()
    for asset in assets:
        path = _resolve_video_file_path(asset.storage_key)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                print(f"Erro ao remover asset antigo {path}: {e}")

    # Limpa entidades derivadas do pipeline para recomeçar do zero
    db.query(Job).filter(Job.video_id == video.id).delete(synchronize_session=False)
    db.query(Scene).filter(Scene.video_id == video.id).delete(synchronize_session=False)
    db.query(Asset).filter(Asset.video_id == video.id).delete(synchronize_session=False)

    video.status = "queued"
    video.youtube_video_id = None
    video.published_at = None
    _delete_scheduled_mirror(db, video.id)
    db.commit()

    factory = VideoFactory(db)
    factory._add_job(video.id, "script")
    background_tasks.add_task(process_jobs_background)
    return {"status": "queued", "message": "Vídeo reenfileirado para regeneração."}

@router.delete("/videos/{video_id}")
def delete_production_video(video_id: int, db: Session = Depends(get_db)):
    """Exclui um vídeo da fila de produção, removendo assets e derivados."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # Remove arquivos físicos dos assets
    assets = db.query(Asset).filter(Asset.video_id == video.id).all()
    for asset in assets:
        path = _resolve_video_file_path(asset.storage_key)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                print(f"Erro ao remover arquivo de asset {path}: {e}")

    # Remove vídeos derivados (shorts) para evitar órfãos
    children = db.query(Video).filter(Video.parent_video_id == video.id).all()
    for child in children:
        _delete_scheduled_mirror(db, child.id)
        db.delete(child)

    _delete_scheduled_mirror(db, video.id)
    db.delete(video)
    db.commit()
    return {"status": "deleted"}

@router.get("/videos/{video_id}")
def get_video_details(video_id: int, db: Session = Depends(get_db)):
    """Retorna detalhes do vídeo e jobs para a fila de produção."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    jobs = db.query(Job).filter(Job.video_id == video_id).order_by(Job.created_at.desc()).all()
    return {
        "id": video.id,
        "title": video.title,
        "status": video.status,
        "jobs": [{"step": j.step, "status": j.status, "logs": j.logs or ""} for j in jobs]
    }

@router.get("/videos/{video_id}/download")
def download_video(video_id: int, token: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """Download do arquivo de vídeo final (para fila de produção)."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    final_path = _latest_final_asset_path(db, video.id)
    if not final_path:
        # Compatibilidade com registros legados que salvaram path em youtube_video_id
        final_path = _resolve_video_file_path(video.youtube_video_id)
    if not final_path:
        raise HTTPException(status_code=404, detail="Video file not found")
    return FileResponse(final_path, media_type="video/mp4", filename=os.path.basename(final_path))

@router.get("/videos/{video_id}/watch")
def watch_video(video_id: int, token: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """Abre/streama o vídeo final da fila de produção no navegador."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    final_path = _latest_final_asset_path(db, video.id)
    if not final_path:
        final_path = _resolve_video_file_path(video.youtube_video_id)
    if not final_path:
        raise HTTPException(status_code=404, detail="Video file not found")

    return FileResponse(final_path, media_type="video/mp4")

@router.post("/auto/process-job")
def trigger_process_job(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Manually trigger job processing (for testing/worker simulation)."""
    background_tasks.add_task(process_jobs_background)
    return {"status": "Processing triggered"}

@router.post("/auto/unblock")
def unblock_production_queue(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Reseta jobs travados em processing (5+ min) e dispara processamento. Use quando a fila travar."""
    _reset_stuck_jobs(db, timeout_minutes=5)
    background_tasks.add_task(process_jobs_background)
    return {"status": "ok", "message": "Fila desbloqueada. Processamento disparado."}

@router.post("/upload-music")
async def upload_music(file: UploadFile = File(...)):
    upload_dir = Path("app/static/music_uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    
    # Sanitize filename
    safe_filename = "".join([c for c in file.filename if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
    file_path = upload_dir / f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_filename}"
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    # Return absolute path for internal usage or relative for client if needed
    # Using absolute path for backend processing consistency
    return {"file_path": str(file_path.absolute()), "filename": safe_filename}

class VideoRequest(BaseModel):
    topic: Optional[str] = None
    duration: int = 5
    auto_upload: bool = False
    mode: str = "topic" # topic | story
    story_content: Optional[str] = None
    custom_image_paths: Optional[List[str]] = None
    selected_images: Optional[List[str]] = None

class StoryTextGenerateRequest(BaseModel):
    kind: str = "story"  # story | devotional
    instruction: str
    duration_min: int = 10
    duration_max: Optional[int] = None

class StoryTextImproveRequest(BaseModel):
    kind: str = "story"  # story | devotional
    instruction: str = ""
    original_text: str
    duration_min: int = 10
    duration_max: Optional[int] = None
class StoryImagesRequest(BaseModel):
    kind: str = "story"  # story | devotional
    story_content: str
    count: int = 4
    aspect_ratio: str = "16:9"  # 16:9 | 9:16

class StoryShortsRequest(BaseModel):
    kind: str = "story"  # story | devotional
    story_content: str
    count: int = 3
    selected_images: Optional[List[str]] = None
    voice_style: Optional[str] = None
    voice_gender: Optional[str] = None

class CreateShortsFromScheduledRequest(BaseModel):
    count: int = 3

def _generate_story_images_payload(request: StoryImagesRequest, progress_callback=None) -> Dict[str, Any]:
    ai_service = AIContentGenerator()
    kind = (request.kind or "story").strip().lower()
    if kind not in {"story", "devotional"}:
        kind = "story"

    try:
        count = int(request.count or 1)
    except Exception:
        count = 1
    count = max(1, min(12, count))

    aspect_ratio = (request.aspect_ratio or "16:9").strip()
    if aspect_ratio not in {"16:9", "9:16"}:
        aspect_ratio = "16:9"

    story_content = (request.story_content or "").strip()
    if not story_content:
        raise HTTPException(status_code=400, detail="story_content é obrigatório.")
    story_content = story_content[:8000]

    def _progress(pct: int, msg: str):
        if progress_callback:
            try:
                progress_callback(pct, msg)
            except Exception:
                pass

    def _extract_scene_chunks(text: str, n: int) -> List[str]:
        raw = (text or "").replace("\r\n", "\n").strip()
        if not raw:
            return []
        parts = [p.strip() for p in raw.split("\n") if p.strip()]
        if len(parts) < n:
            import re
            sents = re.split(r"(?<=[.!?])\s+", raw.replace("\n", " ").strip())
            parts = [s.strip() for s in sents if s and s.strip()]
        if not parts:
            return []
        chunks: List[str] = []
        idx = 0
        max_chars = 420
        while idx < len(parts) and len(chunks) < n:
            buf = parts[idx].strip()
            idx += 1
            while idx < len(parts) and len(buf) < int(max_chars * 0.7):
                cand = parts[idx].strip()
                if not cand:
                    idx += 1
                    continue
                if len(buf) + 1 + len(cand) > max_chars:
                    break
                buf = f"{buf} {cand}"
                idx += 1
            chunks.append(buf[:max_chars].strip())
        while len(chunks) < n:
            chunks.append(chunks[-1])
        return chunks[:n]

    _progress(5, "Preparando cenas para gerar imagens...")
    scene_chunks = _extract_scene_chunks(story_content, count)
    if not scene_chunks:
        base = story_content.replace("\n", " ").strip()[:320]
        scene_chunks = [base] * count

    prompts: List[str] = []
    _progress(8, "Gerando prompts de imagem por cena...")
    for idx, chunk in enumerate(scene_chunks[:count]):
        try:
            p_list = ai_service.generate_story_image_prompts(chunk, n=1, kind=kind) or []
            p = (p_list[0] if isinstance(p_list, list) and p_list else "") if p_list is not None else ""
        except Exception:
            p = ""
        p = (p or "").strip()
        if not p:
            safe_kind = "story" if kind == "story" else "devotional"
            p = (
                f"Cinematic digital art illustration of a scene inspired by this {safe_kind} excerpt: {chunk}. "
                "High detail, cinematic lighting, expressive atmosphere, no text, no watermark, no logo."
            )
        prompts.append(p[:900])

    covers_dir = Path("app/static/covers")
    covers_dir.mkdir(parents=True, exist_ok=True)

    def _local_fallback_image(aspect: str) -> str:
        from PIL import Image, ImageDraw
        import random

        if str(aspect).strip() == "9:16":
            width, height = (720, 1280)
        else:
            width, height = (1280, 720)

        small_h = 256
        small_w = max(1, int(width * (small_h / height)))
        color_top = (random.randint(90, 150), random.randint(90, 160), random.randint(120, 210))
        color_bottom = (random.randint(30, 90), random.randint(30, 90), random.randint(60, 130))

        small_img = Image.new("RGB", (small_w, small_h))
        small_draw = ImageDraw.Draw(small_img)
        for y in range(small_h):
            ratio = y / small_h
            r = int(color_top[0] + (color_bottom[0] - color_top[0]) * ratio)
            g = int(color_top[1] + (color_bottom[1] - color_top[1]) * ratio)
            b = int(color_top[2] + (color_bottom[2] - color_top[2]) * ratio)
            small_draw.line([(0, y), (small_w, y)], fill=(r, g, b))

        img = small_img.resize((width, height), Image.LANCZOS)
        draw = ImageDraw.Draw(img)
        for _ in range(18):
            x = random.randint(-int(width * 0.2), int(width * 1.2))
            y = random.randint(-int(height * 0.2), int(height * 1.2))
            radius = random.randint(int(min(width, height) * 0.06), int(min(width, height) * 0.22))
            alpha = random.randint(10, 40)
            overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            o = ImageDraw.Draw(overlay)
            o.ellipse([x - radius, y - radius, x + radius, y + radius], fill=(255, 255, 255, alpha))
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
            draw = ImageDraw.Draw(img)

        filename = f"storyimg_{uuid.uuid4().hex}.png"
        file_path = covers_dir / filename
        img.save(file_path, format="PNG", optimize=True)
        if not file_path.exists() or file_path.stat().st_size < 1024:
            try:
                file_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None
        return filename

    images: List[Dict[str, Any]] = []
    forced_providers = ["openai_direct", "leonardo", "edenai", "pollinations_flux", "pollinations_turbo", "pollinations"]
    extra_image_providers = ["openai_direct", "leonardo"]
    has_openai_or_leonardo = bool((getattr(ai_service, "api_key", None) or "").strip() or (getattr(ai_service, "leonardo_key", None) or "").strip())
    allow_non_ai_fallback = os.getenv("ALLOW_NON_AI_IMAGE_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}

    all_prompts = prompts[:count]
    if has_openai_or_leonardo:
        try:
            extra_prompt_list = ai_service.generate_story_image_prompts(
                f"{story_content}\n\nFoco: a cena mais marcante e emocional.",
                n=1,
                kind=kind,
            ) or []
            extra_prompt = (extra_prompt_list[0] if isinstance(extra_prompt_list, list) and extra_prompt_list else "") if extra_prompt_list is not None else ""
        except Exception:
            extra_prompt = ""
        extra_prompt = (extra_prompt or "").strip() or (all_prompts[-1] if all_prompts else "")
        if extra_prompt:
            all_prompts.append(extra_prompt[:900])

    total = max(1, len(all_prompts))
    for idx, p in enumerate(all_prompts):
        step_pct = 15 + int((idx / total) * 80)
        prompt_text = (p or "").strip()
        if not prompt_text:
            continue
        try:
            def _status(msg: str, scene_idx=idx, total_scenes=count, pct=step_pct):
                _progress(pct, f"Imagem {scene_idx+1}/{total}: {msg}")

            _progress(step_pct, f"Gerando imagem {idx+1}/{total}...")
            providers = extra_image_providers if (has_openai_or_leonardo and idx == (len(all_prompts) - 1) and len(all_prompts) > count) else forced_providers
            url = ai_service.generate_image(prompt_text, aspect_ratio=aspect_ratio, providers=providers, status_callback=_status)
        except Exception:
            url = None
        if not url:
            if allow_non_ai_fallback:
                _progress(step_pct, f"Imagem {idx+1}/{total}: IA indisponível; usando fundo local.")
                filename = _local_fallback_image(aspect_ratio)
                if filename:
                    images.append({"url": f"/static/covers/{filename}", "prompt": prompt_text})
            else:
                _progress(step_pct, f"Imagem {idx+1}/{total}: IA indisponível; pulando.")
            continue

        try:
            resp = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code >= 400:
                raise Exception(f"HTTP {resp.status_code}")
            content_type = (resp.headers.get("content-type") or "").lower()
            ext = ".png"
            if "jpeg" in content_type or "jpg" in content_type:
                ext = ".jpg"
            filename = f"storyimg_{uuid.uuid4().hex}{ext}"
            file_path = covers_dir / filename
            with open(file_path, "wb") as f:
                f.write(resp.content or b"")
            if not file_path.exists() or file_path.stat().st_size < 1024:
                try:
                    file_path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise Exception("Arquivo vazio")
            images.append({"url": f"/static/covers/{filename}", "prompt": prompt_text})
        except Exception:
            if allow_non_ai_fallback:
                _progress(step_pct, f"Imagem {idx+1}/{total}: download falhou; usando fundo local.")
                filename = _local_fallback_image(aspect_ratio)
                if filename:
                    images.append({"url": f"/static/covers/{filename}", "prompt": prompt_text})
            else:
                _progress(step_pct, f"Imagem {idx+1}/{total}: download falhou; pulando.")
            continue

    if not images:
        raise HTTPException(
            status_code=503,
            detail="Nenhum provedor de IA retornou imagem. Configure OpenAI/Leonardo em Configurações."
        )

    _progress(100, "Imagens prontas.")
    return {"count": len(images), "images": images, "kind": kind, "aspect_ratio": aspect_ratio}

def _generate_story_shorts_payload(request: StoryShortsRequest, progress_callback=None) -> Dict[str, Any]:
    ai_service = AIContentGenerator()
    kind = (request.kind or "story").strip().lower()
    if kind not in {"story", "devotional"}:
        kind = "story"

    try:
        count = int(request.count or 1)
    except Exception:
        count = 1
    count = max(1, min(8, count))

    story_content = (request.story_content or "").strip()
    if not story_content:
        raise HTTPException(status_code=400, detail="story_content é obrigatório.")
    story_content = story_content[:12000]

    selected_images = []
    if request.selected_images and isinstance(request.selected_images, list):
        for v in request.selected_images:
            if isinstance(v, str) and v.strip():
                selected_images.append(v.strip())
    selected_images = selected_images[:24]

    voice_style = (request.voice_style or "").strip() or None
    voice_gender = (request.voice_gender or "").strip() or None

    def _progress(pct: int, msg: str):
        if progress_callback:
            try:
                progress_callback(pct, msg)
            except Exception:
                pass

    angles = (
        ["Gancho forte (início da história)", "Momento mais impactante", "Lição final e CTA"]
        if kind == "story"
        else ["Gancho de fé (início)", "Aplicação prática", "Mensagem final e CTA"]
    )

    from app.services.video_generator import VideoGenerator
    video_service = VideoGenerator(ai_service=ai_service)

    shorts = []
    for idx in range(count):
        angle = angles[idx % len(angles)]
        _progress(5 + int((idx / max(1, count)) * 75), f"Gerando short {idx+1}/{count} ({angle})...")
        prompt = (
            f"Crie UM roteiro de YouTube Short vertical (30-60s), baseado nesta {('história' if kind == 'story' else 'mensagem/devocional')}.\n"
            f"Foco: {angle}.\n\n"
            f"TEXTO BASE:\n{story_content}\n\n"
            "Regras: gancho no início, 3 a 5 cenas, frases curtas, sem texto na imagem."
        )
        plan = ai_service.generate_short_script_from_prompt(prompt)
        if not isinstance(plan, dict):
            plan = {"title": f"Short {idx+1}", "scenes": [{"text": "Assista até o fim.", "image_prompt": "cinematic inspiring scene"}]}
        if selected_images:
            plan["selected_images"] = selected_images

        def _video_progress(p, m, short_idx=idx, total=count):
            base = 10 + int((short_idx / max(1, total)) * 80)
            span = int((1 / max(1, total)) * 80)
            mapped = min(95, base + int((p or 0) / 100 * max(1, span)))
            _progress(mapped, f"Short {short_idx+1}/{total}: {m}")

        result = video_service.create_video_from_plan(
            plan,
            aspect_ratio="9:16",
            progress_callback=_video_progress,
            voice_style=voice_style,
            voice_gender=voice_gender,
        )
        video_url = result.get("video_url") if isinstance(result, dict) else None
        shorts.append({
            "title": plan.get("title") or f"Short {idx+1}",
            "description": plan.get("description") or "",
            "video_url": video_url,
            "kind": kind,
            "video_type": "short",
        })

    _progress(100, "Shorts prontos.")
    return {"count": len(shorts), "shorts": shorts, "kind": kind}

class QueueGeneratedVideoRequest(BaseModel):
    video_url: str
    title: Optional[str] = None
    description: Optional[str] = None
    kind: Optional[str] = None
    video_type: Optional[str] = None
    auto_post: bool = False
    scheduled_for: Optional[str] = None
    voice_style: Optional[str] = None
    voice_gender: Optional[str] = None

@router.post("/story/generate_text")
def generate_story_text(request: StoryTextGenerateRequest):
    ai_service = AIContentGenerator()
    kind = (request.kind or "story").strip().lower()
    if kind not in {"story", "devotional"}:
        kind = "story"
    text = ai_service.generate_story_or_devotional_text(
        instruction=request.instruction,
        kind=kind,
        duration_min_minutes=request.duration_min,
        duration_max_minutes=request.duration_max,
    )
    return {"text": text, "kind": kind, "duration_min": request.duration_min, "duration_max": request.duration_max}

@router.post("/story/improve_text")
def improve_story_text(request: StoryTextImproveRequest):
    ai_service = AIContentGenerator()
    kind = (request.kind or "story").strip().lower()
    if kind not in {"story", "devotional"}:
        kind = "story"
    instruction = (request.instruction or "").strip() or "Melhore o texto mantendo o sentido e aumentando a retenção."
    text = ai_service.improve_story_or_devotional_text(
        original_text=request.original_text,
        instruction=instruction,
        kind=kind,
        duration_min_minutes=request.duration_min,
        duration_max_minutes=request.duration_max,
    )
    return {"text": text, "kind": kind, "duration_min": request.duration_min, "duration_max": request.duration_max}

@router.post("/story/generate_images_task")
def generate_story_images_task(request: StoryImagesRequest, background_tasks: BackgroundTasks):
    task_id = create_task()
    update_task(task_id, status="processing", progress=0, message="Iniciando geração de imagens...")
    background_tasks.add_task(process_story_images_generation, request, task_id)
    return {"message": "Processo iniciado", "task_id": task_id}

@router.post("/story/generate_images")
def generate_story_images(request: StoryImagesRequest):
    return _generate_story_images_payload(request)

def process_story_images_generation(request: StoryImagesRequest, task_id: str):
    try:
        def progress_callback(progress, message):
            try:
                update_task(task_id, progress=int(progress or 0), message=message)
            except Exception:
                pass

        result = _generate_story_images_payload(request, progress_callback=progress_callback)
        update_task(task_id, progress=100, status="completed", message="Imagens geradas com sucesso!", result=result)
    except Exception as e:
        update_task(task_id, status="failed", message=f"Erro: {str(e)}")

@router.post("/story/generate_shorts_task")
def generate_story_shorts_task(request: StoryShortsRequest, background_tasks: BackgroundTasks):
    task_id = create_task()
    update_task(task_id, status="processing", progress=0, message="Iniciando geração de shorts...")
    background_tasks.add_task(process_story_shorts_generation, request, task_id)
    return {"message": "Processo iniciado", "task_id": task_id}

@router.post("/story/generate_shorts")
def generate_story_shorts(request: StoryShortsRequest):
    return _generate_story_shorts_payload(request)

def process_story_shorts_generation(request: StoryShortsRequest, task_id: str):
    try:
        def progress_callback(progress, message):
            try:
                update_task(task_id, progress=int(progress or 0), message=message)
            except Exception:
                pass

        result = _generate_story_shorts_payload(request, progress_callback=progress_callback)
        update_task(task_id, progress=100, status="completed", message="Shorts gerados com sucesso!", result=result)
    except Exception as e:
        update_task(task_id, status="failed", message=f"Erro: {str(e)}")

@router.post("/schedule/{video_id}/create_shorts_task")
def create_shorts_from_scheduled_task(video_id: int, request: CreateShortsFromScheduledRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado.")
    if (video.video_type or "video").strip().lower() != "video":
        raise HTTPException(status_code=400, detail="Apenas vídeos longos (não-shorts) podem gerar shorts.")

    task_id = create_task()
    update_task(task_id, status="processing", progress=0, message="Iniciando criação de shorts a partir do vídeo...")
    payload = {"count": int(getattr(request, "count", 3) or 3)}
    background_tasks.add_task(process_create_shorts_from_scheduled_video, video_id, payload, task_id)
    return {"message": "Processo iniciado", "task_id": task_id}

def process_create_shorts_from_scheduled_video(video_id: int, payload: Dict[str, Any], task_id: str):
    db = SessionLocal()
    try:
        scheduled = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
        if not scheduled:
            raise Exception("Vídeo não encontrado.")
        if (scheduled.video_type or "video").strip().lower() != "video":
            raise Exception("Apenas vídeos longos (não-shorts) podem gerar shorts.")

        try:
            count = int((payload or {}).get("count") or 3)
        except Exception:
            count = 3
        count = max(1, min(8, count))

        kind = "story"
        base_text = ""
        data = {}
        if scheduled.script_data:
            try:
                data = json.loads(scheduled.script_data or "{}") or {}
            except Exception:
                data = {}

        if isinstance(data, dict):
            raw_kind = str(data.get("kind") or "").strip().lower()
            if raw_kind in {"story", "devotional"}:
                kind = raw_kind

            scenes = data.get("scenes")
            if isinstance(scenes, list) and scenes:
                parts = []
                for s in scenes:
                    if isinstance(s, dict):
                        t = (s.get("text") or s.get("narration_text") or s.get("narration") or "").strip()
                        if t:
                            parts.append(t)
                base_text = "\n".join(parts).strip()

            if not base_text:
                for k in ("story_content", "text", "content", "script", "concept", "narration_text", "narration"):
                    v = data.get(k)
                    if isinstance(v, str) and v.strip():
                        base_text = v.strip()
                        break

        if not base_text:
            title = (scheduled.title or "").strip()
            desc = (scheduled.description or "").strip()
            base_text = f"{title}\n\n{desc}".strip()

        base_text = (base_text or "").strip()[:12000]
        if not base_text:
            raise Exception("Sem conteúdo para gerar shorts.")

        ai_service = AIContentGenerator()
        voice_style = (getattr(scheduled, "voice_style", "") or "").strip() or None
        voice_gender = (getattr(scheduled, "voice_gender", "") or "").strip() or None

        def progress_callback(progress, message):
            try:
                update_task(task_id, progress=int(progress or 0), message=message)
            except Exception:
                pass

        req = StoryShortsRequest(
            kind=kind,
            story_content=base_text,
            count=count,
            selected_images=None,
            voice_style=voice_style,
            voice_gender=voice_gender,
        )
        result = _generate_story_shorts_payload(req, progress_callback=progress_callback) or {}
        shorts = result.get("shorts") if isinstance(result, dict) else None
        shorts = shorts if isinstance(shorts, list) else []

        created_ids: List[int] = []
        now = datetime.now()
        theme = f"Shorts: {(scheduled.title or 'Vídeo').strip()}"[:120]
        for idx, s in enumerate(shorts):
            if not isinstance(s, dict):
                continue
            video_url = (s.get("video_url") or "").strip()
            if not video_url:
                continue
            title = (s.get("title") or "").strip() or f"{(scheduled.title or 'Vídeo').strip()} (Short {idx+1})"
            description = (s.get("description") or "").strip()
            scheduled_for = now + timedelta(minutes=idx + 1)
            short_payload = {
                "source": "derived_from_scheduled",
                "source_scheduled_video_id": scheduled.id,
                "parent_video_id": scheduled.id,
                "kind": kind,
                "video_type": "short",
                "title": title,
                "description": description,
                "video_url": video_url,
            }
            item = ScheduledVideo(
                theme=theme,
                title=title,
                description=description,
                scheduled_for=scheduled_for,
                status="completed",
                video_type="short",
                parent_video_id=scheduled.id,
                script_data=json.dumps(short_payload),
                auto_post=False,
                voice_style=getattr(scheduled, "voice_style", "human"),
                voice_gender=getattr(scheduled, "voice_gender", "female"),
            )
            try:
                setattr(item, "progress", 100)
            except Exception:
                pass
            try:
                setattr(item, "video_url", video_url)
            except Exception:
                pass
            db.add(item)
            db.flush()
            if item.id:
                created_ids.append(int(item.id))

        db.commit()
        update_task(
            task_id,
            progress=100,
            status="completed",
            message="Shorts criados e enviados para Aguardando Publicação.",
            result={"created_ids": created_ids, "count": len(created_ids)},
        )
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        update_task(task_id, status="failed", message=f"Erro: {str(e)}")
    finally:
        try:
            db.close()
        except Exception:
            pass

@router.post("/schedule/from_generated")
def schedule_from_generated(request: QueueGeneratedVideoRequest, db: Session = Depends(get_db)):
    """Envia um vídeo já gerado para a fila 'Aguardando Publicação'."""
    video_url = (request.video_url or "").strip()
    if not video_url:
        raise HTTPException(status_code=400, detail="video_url é obrigatório.")

    kind = (request.kind or "").strip().lower()
    if kind not in {"story", "devotional"}:
        kind = "story"

    video_type = (request.video_type or "").strip().lower() or "video"
    if video_type not in {"video", "short"}:
        video_type = "video"

    title = (request.title or "").strip() or f"Vídeo {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    description = (request.description or "").strip()

    scheduled_for = datetime.now()
    if request.scheduled_for:
        raw = str(request.scheduled_for).strip()
        try:
            scheduled_for = datetime.fromisoformat(raw)
        except Exception:
            try:
                scheduled_for = datetime.strptime(raw, "%Y-%m-%d %H:%M")
            except Exception:
                scheduled_for = datetime.now()

    payload = {
        "source": "generated_story",
        "kind": kind,
        "video_type": video_type,
        "title": title,
        "description": description,
        "video_url": video_url,
    }
    if request.voice_style:
        payload["voice_style"] = request.voice_style
    if request.voice_gender:
        payload["voice_gender"] = request.voice_gender

    video = ScheduledVideo(
        theme="História/Devocional",
        title=title,
        description=description,
        scheduled_for=scheduled_for,
        video_type=video_type,
        script_data=json.dumps(payload),
        status="completed",
        auto_post=bool(request.auto_post),
    )
    try:
        setattr(video, "progress", 100)
    except Exception:
        pass
    if request.voice_style:
        try:
            setattr(video, "voice_style", request.voice_style)
        except Exception:
            pass
    if request.voice_gender:
        try:
            setattr(video, "voice_gender", request.voice_gender)
        except Exception:
            pass
    try:
        setattr(video, "video_url", video_url)
    except Exception:
        pass

    db.add(video)
    db.commit()
    db.refresh(video)

    return {"id": video.id, "status": video.status, "video_url": video.video_url}

@router.get("/reports")
def get_reports(db: Session = Depends(get_db)):
    """Retorna o histórico de relatórios de monitoramento"""
    return db.query(ChannelReport).order_by(ChannelReport.id.desc()).limit(20).all()

@router.get("/debug-auth")
def debug_auth(db: Session = Depends(get_db)):
    """Debug endpoint to check DB credentials state"""
    settings = db.query(Settings).first()
    service = YouTubeService()
    env_client_id = bool((os.getenv("YOUTUBE_CLIENT_ID") or "").strip())
    env_client_secret = bool((os.getenv("YOUTUBE_CLIENT_SECRET") or "").strip())
    env_refresh = bool((os.getenv("YOUTUBE_REFRESH_TOKEN") or "").strip())
    return {
        "status": "Settings found" if settings else "No settings found",
        "db_has_client_id": bool(settings and settings.youtube_client_id),
        "db_client_id_prefix": (settings.youtube_client_id[:5] + "...") if (settings and settings.youtube_client_id) else None,
        "db_has_client_secret": bool(settings and settings.youtube_client_secret),
        "db_has_refresh_token": bool(settings and settings.youtube_refresh_token),
        "db_refresh_token_prefix": (settings.youtube_refresh_token[:5] + "...") if (settings and settings.youtube_refresh_token) else None,
        "env_has_client_id": env_client_id,
        "env_has_client_secret": env_client_secret,
        "env_has_refresh_token": env_refresh,
        "service_connected": bool(service.service),
        "service_auth_source": getattr(service, "auth_source", None),
        "service_auth_error": getattr(service, "auth_error", None),
    }

@router.get("/stats")
def get_stats():
    service = YouTubeService()
    return service.get_channel_stats()

@router.get("/videos")
def list_videos():
    """Lista os vídeos gerados na pasta videos"""
    # Corrigido para listar da pasta correta onde o VideoGenerator salva
    video_files = glob.glob("app/static/videos/*.mp4")
    videos = []
    for f in video_files:
        filename = os.path.basename(f)
        videos.append({
            "filename": filename,
            "url": f"/static/videos/{filename}",
            "created_at": os.path.getctime(f)
        })
    # Ordenar por data de criação (mais recente primeiro)
    videos.sort(key=lambda x: x['created_at'], reverse=True)
    return videos

@router.get("/auth_url")
def get_auth_url(db: Session = Depends(get_db)):
    """Retorna sempre JSON. Verifica credenciais antes de instanciar YouTubeService."""
    try:
        # Verificar se há credenciais no banco (evita exceção genérica ao instanciar o serviço)
        settings = db.query(Settings).first()
        has_db_creds = settings and (settings.youtube_client_id or "").strip() and (settings.youtube_client_secret or "").strip()
        has_env_creds = (os.getenv("YOUTUBE_CLIENT_ID") or "").strip() and (os.getenv("YOUTUBE_CLIENT_SECRET") or "").strip()
        has_file = os.path.exists("client_secret.json")
        if not has_db_creds and not has_env_creds and not has_file:
            raise HTTPException(
                status_code=503,
                detail="Configure as credenciais do YouTube em Configurações (Client ID e Client Secret), ou nas variáveis YOUTUBE_CLIENT_ID/YOUTUBE_CLIENT_SECRET, ou use client_secret.json no servidor."
            )
        service = YouTubeService()
        auth_url = service.get_auth_url()
        if not auth_url:
            raise HTTPException(
                status_code=503,
                detail="Não foi possível gerar a URL de autorização. Verifique se Client ID e Client Secret em Configurações estão corretos (Google Cloud Console)."
            )
        return {"auth_url": auth_url}
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(
            status_code=503,
            detail="Arquivo client_secret.json não encontrado. Configure Client ID e Client Secret em Configurações (Google Cloud Console > APIs & Services > Credentials)."
        )
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Erro ao conectar ao YouTube: {str(e)}"
        )

@router.post("/auth/exchange")
def exchange_code(data: Dict[str, str]):
    code = data.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Código não fornecido")
    
    # Sanitizar código: espaços e quebras de linha ao copiar do Google quebram a troca
    original_code = code
    code = str(code).strip().replace(" ", "").replace("\n", "").replace("\r", "")
    print(f"Exchange code: original length {len(original_code)}, sanitized length {len(code)}")
    
    service = YouTubeService()
    success, message = service.exchange_code_for_token(code)
    
    if success:
        return {"message": message}
    else:
        print(f"Erro detalhado na troca de código: {message}")
        raise HTTPException(
            status_code=400, 
            detail=f"Falha ao autenticar: {message}\n\n"
                   "Verifique:\n"
                   "1. O código foi copiado corretamente (sem espaços, quebras de linha)\n"
                   "2. O código não expirou (códigos de autorização expiram em ~10 minutos)\n"
                   "3. O Client ID e Client Secret estão configurados corretamente\n"
                   "4. A API 'YouTube Data API v3' está ativada no Google Cloud Console\n"
                   "5. O tipo de aplicativo é 'Desktop' ou 'Web' com redirect URI 'urn:ietf:wg:oauth:2.0:oob'"
        )


@router.post("/optimize")
def optimize_channel(execute: bool = False):
    yt_service = YouTubeService()
    ai_service = AIContentGenerator()
    
    analysis = yt_service.optimize_channel(ai_service)
    
    if execute and analysis:
        # Map analysis result to execution format
        # analysis expected to have: title, description, strategy (for banner prompt)
        exec_data = {
            "title": analysis.get("title_suggestion"),
            "description": analysis.get("description_suggestion"),
            "banner_prompt": analysis.get("banner_prompt")
        }
        
        # Execute immediately
        execution_results = execute_optimization(exec_data)
        
        # Merge results
        analysis["execution_results"] = execution_results
        
    return analysis

@router.post("/auto-analysis")
def auto_analysis():
    yt_service = YouTubeService()
    ai_service = AIContentGenerator()
    
    # 1. Fetch Stats
    stats = yt_service.get_channel_stats()
    # Optimized: Limit to 5 videos to speed up AI analysis (was 10)
    recent_videos = yt_service.get_recent_videos_stats(limit=5)
    
    if not stats.get("connected"):
        raise HTTPException(status_code=400, detail="Canal não conectado. Por favor, conecte-se na aba Configurações.")
    
    # 2. Analyze with AI using centralized service
    return ai_service.generate_auto_insights(stats, recent_videos)

@router.post("/monetization-status")
def monetization_status():
    yt_service = YouTubeService()
    ai_service = AIContentGenerator()
    
    stats = yt_service.get_channel_stats()
    
    if not stats.get("connected"):
        raise HTTPException(status_code=400, detail="Canal não conectado.")

    # Estimate Watch Hours (very rough assumption: 3 mins per view average)
    total_views = int(stats.get('views', 0))
    estimated_minutes = total_views * 3
    estimated_hours = int(estimated_minutes / 60)
    
    subscribers = int(stats.get('subscribers', 0))
    
    # Prepare data for AI service
    progress_data = {
        "subscribers": subscribers,
        "subscribers_target": 1000,
        "estimated_watch_hours": estimated_hours,
        "watch_hours_target": 4000,
        "subscribers_progress_pct": round((subscribers / 1000) * 100, 1),
        "watch_hours_progress_pct": round((estimated_hours / 4000) * 100, 1)
    }
    
    # Analyze with AI
    ai_result = ai_service.generate_monetization_insights(progress_data)
    
    # Structure for Frontend
    final_response = {
        "ai_insights": ai_result,
        "progress": {
            "subscribers": subscribers,
            "subscribers_progress_pct": progress_data["subscribers_progress_pct"],
            "estimated_watch_hours": estimated_hours,
            "watch_hours_progress_pct": progress_data["watch_hours_progress_pct"]
        }
    }
    
    return final_response

@router.post("/optimize/execute")
def execute_optimization(data: Dict[str, Any]):
    """Executa as melhorias sugeridas (título/descrição/banner)"""
    from app.services.ai_generator import AIContentGenerator
    yt_service = YouTubeService()
    ai_service = AIContentGenerator()
    
    # data expects {'title': '...', 'description': '...', 'banner_prompt': '...'}
    
    results = {
        "banner_generated": False,
        "banner_uploaded": False,
        "channel_updated": False,
        "errors": []
    }

    banner_url = None
    if data.get('banner_prompt'):
        # 1. Generate Image
        try:
            generated_image_url = ai_service.generate_banner_image(data['banner_prompt'])
            if generated_image_url:
                results["banner_generated"] = True
                # 2. Upload to YouTube
                # Convert relative path to absolute (compatível com Docker)
                from app.config import absolute_path_for_static
                banner_path = generated_image_url
                if banner_path.startswith("/"):
                    banner_path = absolute_path_for_static(banner_path)
                
                banner_url = yt_service.upload_channel_banner(banner_path)
                if banner_url:
                    results["banner_uploaded"] = True
                else:
                    results["errors"].append("Falha ao fazer upload do banner para o YouTube")
            else:
                results["errors"].append("Falha ao gerar imagem do banner com IA")
        except Exception as e:
            results["errors"].append(f"Erro no processamento do banner: {str(e)}")
    
    # 3. Update Channel Info
    update_res = yt_service.update_channel_info(
        title=data.get('title'), 
        description=data.get('description'),
        banner_external_url=banner_url
    )
    
    if "error" in update_res:
        results["errors"].append(f"Erro ao atualizar canal: {update_res['error']}")
    else:
        results["channel_updated"] = True
        results["update_details"] = update_res
        
    return results

class ScheduleRequest(BaseModel):
    theme: str
    duration_type: str = "days" # days, weeks, months
    duration_value: int = 7
    start_date: Optional[str] = None # YYYY-MM-DD
    videos_per_day: int = 1
    shorts_per_day: int = 0
    video_duration: int = 5

    script_data: Optional[str] = None
    music_file_path: Optional[str] = None # Path to uploaded music file
    music_mode: bool = False

@router.put("/schedule/{video_id}")
def update_scheduled_video(video_id: int, data: Dict[str, Any], db: Session = Depends(get_db)):
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    
    if "scheduled_for" in data:
        try:
            # Expects ISO format or "YYYY-MM-DD HH:MM"
            dt_str = data["scheduled_for"]
            if "T" in dt_str:
                video.scheduled_for = datetime.fromisoformat(dt_str)
            else:
                video.scheduled_for = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        except ValueError:
            pass # Keep old value if format error
            
    if "auto_post" in data:
        video.auto_post = bool(data["auto_post"])
        
    if "title" in data:
        video.title = data["title"]

    if "voice_style" in data:
        video.voice_style = data["voice_style"]
        
    if "voice_gender" in data:
        video.voice_gender = data["voice_gender"]
        
    db.commit()
    return {"message": "Video updated", "video": {
        "id": video.id, 
        "scheduled_for": video.scheduled_for.isoformat() if video.scheduled_for else None,
        "auto_post": video.auto_post
    }}

@router.post("/schedule/generate")
def generate_schedule(request: ScheduleRequest):
    from app.services.ai_generator import AIContentGenerator
    ai_service = AIContentGenerator()
    try:
        return ai_service.generate_content_plan(
            request.theme, 
            request.duration_type, 
            request.duration_value, 
            request.start_date,
            request.videos_per_day,
            request.shorts_per_day,
            request.video_duration
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

from sqlalchemy import text, inspect

@router.post("/schedule/save")
def save_schedule(plan: List[Dict[str, Any]], background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Salva o plano no banco de dados e inicia geração"""
    
    # Auto-fix: Ensure columns exist (fail-safe for migration issues)
    # List of potentially missing columns and their types
    # Added comprehensive check for all new columns
    missing_cols = [
        ("progress", "INTEGER DEFAULT 0"),
        ("publish_at", "DATETIME"),
        ("auto_post", "BOOLEAN DEFAULT 0"),
        ("voice_style", "VARCHAR"),
        ("voice_gender", "VARCHAR"),
        ("music_file_path", "VARCHAR"),
        ("youtube_video_id", "VARCHAR"),
        ("uploaded_at", "DATETIME"),
        ("updated_at", "DATETIME")
    ]
    
    # Simple migration check for SQLite
    try:
        inspector = inspect(db.get_bind())
        columns = [c["name"] for c in inspector.get_columns("scheduled_videos")]
        
        for col_name, col_type in missing_cols:
            if col_name not in columns:
                try:
                    db.execute(text(f"ALTER TABLE scheduled_videos ADD COLUMN {col_name} {col_type}"))
                    db.commit()
                    print(f"Migration: Added column {col_name} to scheduled_videos")
                except Exception as e:
                    print(f"Migration error ({col_name}): {e}")
                    db.rollback()
    except Exception as e:
        print(f"Migration check failed: {e}")

    saved_videos = []
    
    for item in plan:
        # Extrair dados do item
        # Se for music_mode, o item já deve vir com music_file_path
        
        video = ScheduledVideo(
            theme=item.get("theme_of_day", "Geral"),
            title=item.get("videos", [{}])[0].get("title", "Vídeo Agendado") if isinstance(item.get("videos"), list) else item.get("title", "Vídeo"),
            description=item.get("videos", [{}])[0].get("concept", "") if isinstance(item.get("videos"), list) else item.get("concept", ""),
            scheduled_for=datetime.strptime(f"{item.get('date')} {item.get('videos', [{}])[0].get('time', '12:00')}", "%Y-%m-%d %H:%M") if item.get("date") else datetime.now(),
            video_type=item.get("videos", [{}])[0].get("type", "video") if isinstance(item.get("videos"), list) else item.get("type", "video"),
            script_data=json.dumps(item.get("videos", [{}])[0]) if isinstance(item.get("videos"), list) else json.dumps(item),
            status="queued", # Start as queued
            auto_post=item.get("videos", [{}])[0].get("auto_post", True) if isinstance(item.get("videos"), list) else item.get("auto_post", True),
            voice_style=item.get("videos", [{}])[0].get("voice_style", "human") if isinstance(item.get("videos"), list) else item.get("voice_style", "human"),
            voice_gender=item.get("videos", [{}])[0].get("voice_gender", "female") if isinstance(item.get("videos"), list) else item.get("voice_gender", "female"),
            music_file_path=item.get("videos", [{}])[0].get("music_file_path") if isinstance(item.get("videos"), list) else item.get("music_file_path")
        )
        db.add(video)
        db.flush() # get ID
        saved_videos.append(video)
    
    db.commit()
    
    # Kickoff imediato do primeiro item para não depender exclusivamente do scheduler
    # (evita sensação de "não está gerando").
    if saved_videos:
        try:
            processing = db.query(ScheduledVideo).filter(ScheduledVideo.status == "processing").first()
            if not processing:
                from app.services.video_processing import process_scheduled_video
                background_tasks.add_task(process_scheduled_video, saved_videos[0].id)
        except Exception as e:
            print(f"Erro ao iniciar geração imediata: {e}")
    
    return {"message": "Schedule saved", "count": len(saved_videos)}

@router.post("/schedule/{video_id}/generate")
def generate_scheduled_video(video_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    
    # Limpar cache de script se existir, para forçar regeneração da IA (pois o usuário pediu explicitamente)
    if video.script_data:
        try:
            data = json.loads(video.script_data)
            changed = False
            # Remove chaves geradas pela IA para garantir novo conteúdo
            keys_to_remove = ["scenes", "audio_path", "background_music", "music_credit"]
            for k in keys_to_remove:
                if k in data:
                    del data[k]
                    changed = True
            
            if changed:
                video.script_data = json.dumps(data)
        except Exception as e:
            print(f"Erro ao limpar cache do script: {e}")

    # IMPORTANTE: força regeneração real.
    # Sem limpar video_url, o processador interpreta como "já pronto" e só recupera status.
    old_video_url = (video.video_url or "").strip()
    if old_video_url:
        try:
            from app.config import absolute_path_for_video
            old_abs_path = absolute_path_for_video(old_video_url)
            if old_abs_path and os.path.exists(old_abs_path):
                os.remove(old_abs_path)
        except Exception as e:
            print(f"Erro ao remover vídeo antigo ({old_video_url}): {e}")

    # Limpar metadados de publicação/arquivo para que "Regerar" não reutilize artefatos antigos
    video.video_url = None
    video.youtube_video_id = None
    video.uploaded_at = None

    # Limpa marcadores de erro sistêmico antigos para não poluir UI após retry
    if video.description:
        markers = ("[ERRO]", "[SISTEMA]", "[UPLOAD_ERRO]")
        cleaned_lines = [ln for ln in video.description.splitlines() if not any(m in ln for m in markers)]
        video.description = "\n".join(cleaned_lines).strip()

    video.status = "queued"
    video.progress = 0 # Reset progress
    db.commit()
    
    # Dispara tentativa imediata quando a fila está livre.
    # Se já houver um item processando, o scheduler assume o próximo ciclo.
    try:
        processing = db.query(ScheduledVideo).filter(
            ScheduledVideo.status == "processing",
            ScheduledVideo.id != video.id
        ).first()
        if not processing:
            from app.services.video_processing import process_scheduled_video
            background_tasks.add_task(process_scheduled_video, video_id)
    except Exception as e:
        print(f"Erro ao iniciar regeneração imediata do vídeo {video_id}: {e}")

    return {"status": "queued"}

@router.post("/schedule/{video_id}/regenerate")
def regenerate_scheduled_video(video_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Mesma coisa que generate, mas semanticamente explícito"""
    return generate_scheduled_video(video_id, background_tasks, db)

@router.post("/schedule/{video_id}/publish-now")
def publish_now_scheduled_video(video_id: int, db: Session = Depends(get_db)):
    """Publica imediatamente um vídeo que está em Aguardando Publicação (status completed)."""
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado.")
    normalized_status = (video.status or "").lower().strip()
    if normalized_status not in ("completed", "ready"):
        raise HTTPException(status_code=400, detail="Só é possível publicar vídeos prontos (status concluído).")
    if video.uploaded_at:
        raise HTTPException(status_code=400, detail="Este vídeo já foi publicado.")
    if not video.video_url:
        raise HTTPException(status_code=400, detail="Vídeo sem arquivo. Regenere o vídeo.")

    abs_video_path = _resolve_video_file_path(video.video_url)
    if not abs_video_path or not os.path.exists(abs_video_path):
        raise HTTPException(
            status_code=503,
            detail="Arquivo de vídeo não encontrado no servidor. Exclua este item ou agende um novo."
        )

    tags = ["motivação", "sucesso"]
    if video.script_data:
        try:
            script = json.loads(video.script_data)
            if script.get("tags"):
                tags = script["tags"]
        except Exception:
            pass

    yt_service = YouTubeService()
    upload_result = yt_service.upload_video(
        abs_video_path,
        title=video.title,
        description=video.description or "Vídeo gerado automaticamente por Codexia.",
        tags=tags,
    )

    is_error = False
    video_id_value = None
    if isinstance(upload_result, dict):
        if upload_result.get("error"):
            is_error = True
        elif _is_mock_upload(upload_result):
            is_error = True
        else:
            video_id_value = upload_result.get("id") or str(upload_result)
    else:
        video_id_value = str(upload_result) if upload_result else None
        if not video_id_value:
            is_error = True

    if is_error or not video_id_value:
        # Mantém vídeo pronto para nova tentativa manual após configurar credenciais.
        if normalized_status in ("ready", "completed"):
            video.status = normalized_status
        err_msg = _publish_error_message(upload_result, action_label="publicar")
        video.description = _append_upload_error_note(video.description, err_msg)
        db.commit()
        raise HTTPException(status_code=502, detail=err_msg)

    video.uploaded_at = datetime.now()
    video.youtube_video_id = video_id_value
    video.status = "published"
    db.commit()
    return {"status": "published", "youtube_video_id": video_id_value, "message": "Vídeo publicado com sucesso!"}

@router.post("/schedule/{video_id}/republish")
def republish_scheduled_video(video_id: int, db: Session = Depends(get_db)):
    """Republica no YouTube um vídeo já publicado (re-envia o mesmo arquivo; gera novo ID no YouTube)."""
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado.")
    normalized_status = (video.status or "").lower().strip()
    if normalized_status not in ("completed", "ready", "published"):
        raise HTTPException(status_code=400, detail="Só é possível republicar vídeos já produzidos ou publicados.")
    if not video.video_url:
        raise HTTPException(status_code=400, detail="Vídeo sem arquivo. Regenere o vídeo.")

    abs_video_path = _resolve_video_file_path(video.video_url)
    if not abs_video_path or not os.path.exists(abs_video_path):
        raise HTTPException(
            status_code=503,
            detail="Arquivo de vídeo não encontrado no servidor. Não é possível republicar."
        )

    tags = ["motivação", "sucesso"]
    if video.script_data:
        try:
            script = json.loads(video.script_data)
            if script.get("tags"):
                tags = script["tags"]
        except Exception:
            pass

    yt_service = YouTubeService()
    upload_result = yt_service.upload_video(
        abs_video_path,
        title=video.title,
        description=video.description or "Vídeo gerado automaticamente por Codexia.",
        tags=tags,
    )

    is_error = False
    video_id_value = None
    if isinstance(upload_result, dict):
        if upload_result.get("error"):
            is_error = True
        elif _is_mock_upload(upload_result):
            is_error = True
        else:
            video_id_value = upload_result.get("id") or str(upload_result)
    else:
        video_id_value = str(upload_result) if upload_result else None
        if not video_id_value:
            is_error = True

    if is_error or not video_id_value:
        err_msg = _publish_error_message(upload_result, action_label="republicar")
        video.description = _append_upload_error_note(video.description, err_msg)
        db.commit()
        raise HTTPException(status_code=502, detail=err_msg)

    video.uploaded_at = datetime.now()
    video.youtube_video_id = video_id_value
    video.status = "published"
    db.commit()
    return {"status": "published", "youtube_video_id": video_id_value, "message": "Vídeo republicado com sucesso!"}

@router.delete("/schedule/{video_id}")
def delete_scheduled_video(video_id: int, db: Session = Depends(get_db)):
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    
    # Opcional: deletar arquivo físico se existir
    if video.video_url:
        try:
            abs_path = _resolve_video_file_path(video.video_url)
            if os.path.exists(abs_path):
                os.remove(abs_path)
        except Exception as e:
            print(f"Erro ao deletar arquivo: {e}")

    db.delete(video)
    db.commit()
    return {"status": "deleted"}

@router.get("/schedule/{video_id}/download")
def download_scheduled_video(video_id: int, token: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """Download do arquivo de vídeo de um item agendado."""
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    path = _resolve_video_file_path(video.video_url)
    if not path:
        raise HTTPException(status_code=404, detail="Video file not found")

    return FileResponse(path, media_type="video/mp4", filename=os.path.basename(path))

@router.get("/schedule")
def get_schedule(db: Session = Depends(get_db)):
    """Lista vídeos agendados; inclui description e error_msg para exibir erro na UI (Ver Erro)."""
    _sync_ready_production_to_scheduled(db)
    videos = db.query(ScheduledVideo).order_by(ScheduledVideo.id.desc()).all()
    result = []
    for v in videos:
        desc = v.description or ""
        err = ""
        if "[ERRO]" in desc:
            idx = desc.find("[ERRO]")
            err = desc[idx:].replace("[ERRO]:", "").strip()[:2000]
        result.append({
            "id": v.id,
            "theme": v.theme,
            "title": v.title,
            "description": desc,
            "error_msg": err or (desc if (v.status or "").lower() == "failed" else ""),
            "status": v.status,
            "progress": v.progress or 0,
            "scheduled_for": v.scheduled_for.isoformat() if v.scheduled_for else None,
            "auto_post": getattr(v, "auto_post", False),
            "video_type": v.video_type,
            "video_url": _normalize_video_url_for_client(v.video_url),
            "youtube_video_id": v.youtube_video_id,
            "uploaded_at": v.uploaded_at.isoformat() if getattr(v, "uploaded_at", None) else None,
            "voice_style": getattr(v, "voice_style", "human"),
            "voice_gender": getattr(v, "voice_gender", "female"),
        })
    return result

@router.get("/auto_insights")
def get_auto_insights():
    """
    Auto Análise:
    - Lê estatísticas gerais do canal
    - Lê performance recente dos vídeos
    - Pede para a IA gerar resumo + novas ideias de vídeos/shorts
    """
    yt = YouTubeService()
    ai = AIContentGenerator()

    stats = yt.get_channel_stats()
    videos = yt.get_recent_videos_performance(max_results=20)
    ai_insights = ai.generate_auto_insights(stats, videos)

    return {
        "stats": stats,
        "recent_videos": videos,
        "ai_insights": ai_insights,
    }

@router.get("/monetization_status")
def get_monetization_status():
    """
    Análise de Monetização:
    - Resume progresso estimado rumo à monetização
    - Pede para a IA gerar diagnóstico + plano de ação
    """
    from app.services.ai_generator import AIContentGenerator
    yt = YouTubeService()
    ai = AIContentGenerator()

    progress = yt.get_monetization_progress()
    ai_insights = ai.generate_monetization_insights(progress)

    return {
        "progress": progress,
        "ai_insights": ai_insights,
    }

@router.post("/generate_video")
def generate_video(request: VideoRequest, background_tasks: BackgroundTasks):
    """Gera um vídeo motivacional e opcionalmente faz upload"""
    
    # Cria ID da tarefa
    task_id = create_task()
    
    # Inicia processo em background
    background_tasks.add_task(process_video_generation, request, task_id)
    
    return {"message": "Processo iniciado", "task_id": task_id}

@router.get("/task/{task_id}")
def get_task_status(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    return task

def process_video_generation(request: VideoRequest, task_id):
    # Lazy import VideoGenerator (moviepy/PIL/numpy) para reduzir memória no startup
    from app.services.video_generator import VideoGenerator

    try:
        topic_display = request.topic if request.mode == 'topic' else "História Personalizada"
        update_task(task_id, status="processing", progress=5, message=f"Iniciando geração sobre: {topic_display}")
        print(f"Iniciando geração de vídeo ({request.mode}): {topic_display}")
        
        ai_service = AIContentGenerator()
        video_service = VideoGenerator(ai_service=ai_service)
        yt_service = YouTubeService()
        
        # 1. Gerar Roteiro
        update_task(task_id, progress=10, message="Estruturando roteiro com IA...")
        
        if request.mode == 'story' and request.story_content:
            script = ai_service.generate_script_from_text(request.story_content, request.duration)
        else:
            # Fallback to topic mode if no story content
            topic = request.topic or "Motivação Genérica"
            script = ai_service.generate_motivational_script(topic, request.duration)
            
        print("Roteiro gerado/estruturado.")

        if isinstance(script, dict):
            selected = []
            if request.selected_images and isinstance(request.selected_images, list):
                for v in request.selected_images:
                    if isinstance(v, str) and v.strip():
                        selected.append(v.strip())
            if request.custom_image_paths and isinstance(request.custom_image_paths, list):
                for v in request.custom_image_paths:
                    if isinstance(v, str) and v.strip():
                        selected.append(v.strip())
            if selected:
                script["selected_images"] = selected[:24]
        
        # 2. Gerar Vídeo (16:9)
        # Passamos uma função de callback para atualizar o progresso
        def progress_callback(progress, message):
            # Mapeia progresso do vídeo (0-100) para progresso da tarefa (20-90)
            task_progress = 20 + int(progress * 0.7)
            update_task(task_id, progress=task_progress, message=message)
            
        video_result = video_service.create_video_from_plan(script, aspect_ratio="16:9", progress_callback=progress_callback)
        video_path = video_result["video_url"]
        
        # Path absoluto para upload (compatível com Docker e /data/media)
        from app.config import absolute_path_for_video
        abs_video_path = absolute_path_for_video(video_path)
        print(f"Vídeo gerado em: {abs_video_path}")
        
        # 3. Upload (se solicitado)
        if request.auto_upload:
            update_task(task_id, progress=90, message="Iniciando upload para o YouTube...")
            print("Iniciando upload para YouTube...")
            
            description = script.get('description', 'Vídeo motivacional.')
            if video_result.get("music_credit"):
                description += f"\n\n{video_result['music_credit']}"
            
            yt_service.upload_video(
                abs_video_path,
                title=script.get('title', f"Motivação: {topic}"),
                description=description,
                tags=script.get('tags', ['motivação', 'sucesso'])
            )
            update_task(task_id, progress=100, status="completed", message="Vídeo gerado e publicado com sucesso!", result={
                "video_url": video_path,
                "title": script.get("title"),
                "description": description,
                "tags": script.get("tags"),
                "kind": "story" if request.mode == "story" else "topic",
            })
        else:
            update_task(task_id, progress=100, status="completed", message="Vídeo gerado com sucesso!", result={
                "video_url": video_path,
                "title": script.get("title"),
                "description": script.get("description"),
                "tags": script.get("tags"),
                "kind": "story" if request.mode == "story" else "topic",
            })
            
    except Exception as e:
        print(f"Erro na tarefa {task_id}: {e}")
        update_task(task_id, status="failed", message=f"Erro: {str(e)}")
