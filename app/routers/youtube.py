import os
import glob
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.services.youtube_service import YouTubeService
# from app.services.ai_generator import AIContentGenerator
from app.services.task_manager import create_task, update_task, get_task
from app.database import get_db, SessionLocal
from app.services.video_factory import VideoFactory
from app.models import ScheduledVideo, ChannelReport, Settings, ContentPlan, Video, Job, Asset

def process_jobs_background():
    """Background task to process video generation jobs."""
    db = SessionLocal()
    try:
        factory = VideoFactory(db)
        # Process up to 5 jobs per request trigger
        for _ in range(5):
            if not factory.process_next_job():
                break
    except Exception as e:
        print(f"Error processing background job: {e}")
    finally:
        db.close()

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

@router.get("/auto/queue")
def get_production_queue(status: Optional[str] = None, limit: int = 50, db: Session = Depends(get_db)):
    """Retorna a fila de produção (vídeos e jobs)."""
    query = db.query(Video).order_by(Video.scheduled_at.asc())
    
    if status:
        query = query.filter(Video.status == status)
        
    videos = query.limit(limit).all()
    
    result = []
    for v in videos:
        # Get active job
        active_job = db.query(Job).filter(Job.video_id == v.id).order_by(Job.created_at.desc()).first()
        result.append({
            "id": v.id,
            "title": v.title,
            "type": v.type,
            "status": v.status,
            "scheduled_at": v.scheduled_at,
            "progress": active_job.progress if active_job else 0,
            "current_step": active_job.step if active_job else "queued",
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

    # Reset status e progresso para permitir reprocessamento (vídeos em ERROR ficam travados)
    if video.status == "ERROR":
        video.status = "queued"
        video.progress = 0

    # Mapeia nomes do frontend para steps do VideoFactory
    step_map = {"script_generate": "script"}
    factory_step = step_map.get(step, step)

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
    if video.status != "READY":
        raise HTTPException(status_code=400, detail="Video is not ready for publication")
        
    # Get Final Asset
    asset = db.query(Asset).filter(Asset.video_id == video.id, Asset.kind == "FINAL").first()
    if not asset or not os.path.exists(asset.storage_key):
        raise HTTPException(status_code=500, detail="Video file not found")
        
    # Call YouTube Service
    try:
        service = YouTubeService() # Assumes auth is set up
        
        # Tags e descrição do script se disponível
        tags = ["motivação", "sucesso"]
        description = video.description or "Vídeo gerado automaticamente por Codexia."
        
        # Tentar extrair tags reais do plano se existirem
        if video.plan and video.plan.content:
            try:
                import json
                plan_data = json.loads(video.plan.content)
                if plan_data.get("tags"):
                    tags = plan_data["tags"]
            except: pass

        # Upload real
        upload_result = service.upload_video(
            asset.storage_key,
            title=video.title,
            description=description,
            tags=tags
        )
        
        if isinstance(upload_result, dict) and upload_result.get("error"):
            raise Exception(upload_result["error"])
            
        youtube_id = upload_result.get("id") if isinstance(upload_result, dict) else str(upload_result)
        
        video.status = "PUBLISHED"
        video.published_at = datetime.now()
        video.youtube_video_id = youtube_id
        db.commit()
        
        return {"status": "Published", "youtube_id": youtube_id}
    except Exception as e:
        video.status = "ERROR"
        db.commit()
        raise HTTPException(status_code=500, detail=f"Erro no upload: {str(e)}")

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
    asset = db.query(Asset).filter(Asset.video_id == video.id, Asset.kind == "FINAL").first()
    if not asset or not os.path.exists(asset.storage_key):
        raise HTTPException(status_code=404, detail="Video file not found")
    return FileResponse(asset.storage_key, media_type="video/mp4", filename=os.path.basename(asset.storage_key))

@router.post("/auto/process-job")
def trigger_process_job(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Manually trigger job processing (for testing/worker simulation)."""
    background_tasks.add_task(process_jobs_background)
    return {"status": "Processing triggered"}

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

@router.get("/reports")
def get_reports(db: Session = Depends(get_db)):
    """Retorna o histórico de relatórios de monitoramento"""
    return db.query(ChannelReport).order_by(ChannelReport.id.desc()).limit(20).all()

@router.get("/debug-auth")
def debug_auth(db: Session = Depends(get_db)):
    """Debug endpoint to check DB credentials state"""
    settings = db.query(Settings).first()
    if not settings:
        return {"status": "No settings found"}
    
    return {
        "status": "Settings found",
        "has_client_id": bool(settings.youtube_client_id),
        "client_id_prefix": settings.youtube_client_id[:5] + "..." if settings.youtube_client_id else None,
        "has_client_secret": bool(settings.youtube_client_secret),
        "has_refresh_token": bool(settings.youtube_refresh_token),
        "refresh_token_prefix": settings.youtube_refresh_token[:5] + "..." if settings.youtube_refresh_token else None
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
        has_file = os.path.exists("client_secret.json")
        if not has_db_creds and not has_file:
            raise HTTPException(
                status_code=503,
                detail="Configure as credenciais do YouTube em Configurações: informe Client ID e Client Secret do Google Cloud (APIs & Services > Credentials). Ou coloque o arquivo client_secret.json na raiz do projeto no servidor."
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
    code = str(code).strip().replace(" ", "").replace("\n", "").replace("\r", "")
    
    service = YouTubeService()
    success = service.exchange_code_for_token(code)
    
    if success:
        return {"message": "Autenticação realizada com sucesso!"}
    else:
        raise HTTPException(status_code=400, detail="Falha ao autenticar com o YouTube. Verifique o código.")


@router.post("/optimize")
def optimize_channel(execute: bool = False):
    from app.services.ai_generator import AIContentGenerator
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
    from app.services.ai_generator import AIContentGenerator
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
    from app.services.ai_generator import AIContentGenerator
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
    
    # Iniciar processamento em background (apenas para os primeiros 2 para não sobrecarregar)
    # Mas como usamos APScheduler agora, apenas deixamos como 'queued' e o scheduler pega.
    # Se quiser forçar start imediato de 1:
    # background_tasks.add_task(process_scheduled_video, saved_videos[0].id)
    
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

    video.status = "queued"
    video.progress = 0 # Reset progress
    db.commit()
    
    # background_tasks.add_task(process_scheduled_video, video_id)
    # Não iniciar imediatamente para respeitar a fila sequencial
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
    if video.status not in ("completed", "ready"):
        raise HTTPException(status_code=400, detail="Só é possível publicar vídeos prontos (status concluído).")
    if video.uploaded_at:
        raise HTTPException(status_code=400, detail="Este vídeo já foi publicado.")
    if not video.video_url:
        raise HTTPException(status_code=400, detail="Vídeo sem arquivo. Regenere o vídeo.")

    from app.config import absolute_path_for_video
    abs_video_path = absolute_path_for_video(video.video_url)
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
        else:
            video_id_value = upload_result.get("id") or str(upload_result)
    else:
        video_id_value = str(upload_result) if upload_result else None
        if not video_id_value:
            is_error = True

    if is_error or not video_id_value:
        video.status = "failed"
        video.description = (video.description or "") + "\n\n[UPLOAD_ERRO]: falha ao enviar para o YouTube. Veja logs do servidor."
        db.commit()
        err_msg = (upload_result.get("error") if isinstance(upload_result, dict) else str(upload_result)) or "Falha ao publicar no YouTube. Verifique as credenciais em Configurações."
        raise HTTPException(status_code=502, detail=err_msg)

    video.uploaded_at = datetime.datetime.now()
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
    if video.status not in ("completed", "ready", "published"):
        raise HTTPException(status_code=400, detail="Só é possível republicar vídeos já produzidos ou publicados.")
    if not video.video_url:
        raise HTTPException(status_code=400, detail="Vídeo sem arquivo. Regenere o vídeo.")

    from app.config import absolute_path_for_video
    abs_video_path = absolute_path_for_video(video.video_url)
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
        else:
            video_id_value = upload_result.get("id") or str(upload_result)
    else:
        video_id_value = str(upload_result) if upload_result else None
        if not video_id_value:
            is_error = True

    if is_error or not video_id_value:
        err_msg = (upload_result.get("error") if isinstance(upload_result, dict) else str(upload_result)) or "Falha ao republicar no YouTube."
        raise HTTPException(status_code=502, detail=err_msg)

    video.uploaded_at = datetime.datetime.now()
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
            from app.config import absolute_path_for_video
            abs_path = absolute_path_for_video(video.video_url)
            if os.path.exists(abs_path):
                os.remove(abs_path)
        except Exception as e:
            print(f"Erro ao deletar arquivo: {e}")

    db.delete(video)
    db.commit()
    return {"status": "deleted"}

@router.get("/schedule")
def get_schedule(db: Session = Depends(get_db)):
    """Lista vídeos agendados; inclui description e error_msg para exibir erro na UI (Ver Erro)."""
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
            "error_msg": err or (desc if v.status == "failed" else ""),
            "status": v.status,
            "progress": v.progress or 0,
            "scheduled_for": v.scheduled_for.isoformat() if v.scheduled_for else None,
            "auto_post": getattr(v, "auto_post", False),
            "video_type": v.video_type,
            "video_url": v.video_url,
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
    from app.services.ai_generator import AIContentGenerator
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
    from app.services.ai_generator import AIContentGenerator

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
            update_task(task_id, progress=100, status="completed", message="Vídeo gerado e publicado com sucesso!", result={"video_url": video_path})
        else:
            update_task(task_id, progress=100, status="completed", message="Vídeo gerado com sucesso!", result={"video_url": video_path})
            
    except Exception as e:
        print(f"Erro na tarefa {task_id}: {e}")
        update_task(task_id, status="failed", message=f"Erro: {str(e)}")
