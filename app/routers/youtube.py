import os
import glob
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from app.services.youtube_service import YouTubeService
from app.services.ai_generator import AIContentGenerator
from app.services.video_generator import VideoGenerator
from app.services.task_manager import create_task, update_task, get_task
from app.database import get_db
from app.models import ScheduledVideo, ChannelReport
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import json
from datetime import datetime
from app.services.video_processing import process_scheduled_video

router = APIRouter(
    prefix="/youtube",
    tags=["youtube"],
    responses={404: {"description": "Not found"}},
)

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
def get_auth_url():
    try:
        service = YouTubeService()
        auth_url = service.get_auth_url()
        if not auth_url:
            raise HTTPException(
                status_code=503,
                detail="Não foi possível gerar a URL de autorização. Verifique se o arquivo client_secret.json está configurado (Google Cloud Console) ou se as credenciais do YouTube estão em Configurações."
            )
        return {"auth_url": auth_url}
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=503,
            detail="Arquivo client_secret.json não encontrado. Faça o download no Google Cloud Console (APIs & Services > Credentials) e coloque na raiz do projeto, ou configure Client ID e Client Secret em Configurações."
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
    
    service = YouTubeService()
    success = service.exchange_code_for_token(code)
    
    if success:
        return {"message": "Autenticação realizada com sucesso!"}
    else:
        raise HTTPException(status_code=400, detail="Falha ao autenticar com o YouTube. Verifique o código.")


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
                # Convert relative path to absolute
                banner_path = generated_image_url
                if banner_path.startswith("/"):
                    banner_path = "c:/dev/TRAE/codexia/app" + banner_path
                
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
        ("publish_at", "TIMESTAMP"),
        ("auto_post", "BOOLEAN DEFAULT FALSE"),
        ("youtube_video_id", "TEXT"),
        ("uploaded_at", "TIMESTAMP"),
        ("voice_style", "TEXT DEFAULT 'human'"),
        ("voice_gender", "TEXT DEFAULT 'female'")
    ]
    
    for col_name, col_type in missing_cols:
        try:
            db.execute(text(f"SELECT {col_name} FROM scheduled_videos LIMIT 1"))
        except Exception:
            print(f"Column {col_name} missing in save_schedule. Attempting to add...")
            try:
                db.rollback()
                db.execute(text(f"ALTER TABLE scheduled_videos ADD COLUMN {col_name} {col_type}"))
                db.commit()
                print(f"Column {col_name} added successfully.")
            except Exception as e:
                print(f"Failed to auto-fix DB for {col_name}: {e}")
                # Continue anyway, maybe it was a transient error

    count = 0
    saved_ids = []
    try:
        for day in plan:
            theme_day = day.get('theme_of_day', 'Geral')
            day_date_str = day.get('date') # YYYY-MM-DD
            
            # Processar lista unificada de vídeos e shorts
            for vid in day.get('videos', []):
                time_str = vid.get('time', '12:00')
                # Prefer video-specific date, fallback to day date
                date_str = vid.get('date', day_date_str)
                
                # Calcular data/hora do agendamento
                scheduled_dt = datetime.now()
                if date_str:
                    try:
                        # Tenta combinar data do plano com horário sugerido
                        scheduled_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                    except Exception as e:
                        print(f"Erro ao parsear data {date_str} {time_str}: {e}")
                        # Fallback se falhar parser
                        pass

                new_video = ScheduledVideo(
                    theme=theme_day,
                    title=vid.get('title'),
                    description=vid.get('concept'),
                    status="queued", # Já marcamos como na fila
                    video_type=vid.get('type', 'video'),
                    script_data=json.dumps(vid),
                    scheduled_for=scheduled_dt,
                    auto_post=vid.get('auto_post', False), # Support auto_post from plan
                    voice_style=vid.get('voice_style', 'human'),
                    voice_gender=vid.get('voice_gender', 'female')
                )
                db.add(new_video)
                db.flush() # Para pegar o ID
                saved_ids.append(new_video.id)
                count += 1
            
            # Manter suporte legado caso a IA separe (opcional, mas seguro)
            for vid in day.get('shorts', []):
                 # Mesma lógica para shorts legados
                 time_str = vid.get('time', '12:00')
                 date_str = vid.get('date', day_date_str)
                 
                 scheduled_dt = datetime.now()
                 if date_str:
                     try:
                         scheduled_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                     except:
                         pass

                 new_video = ScheduledVideo(
                    theme=theme_day,
                    title=vid.get('title'),
                    description=vid.get('concept'),
                    status="queued",
                    video_type='short',
                    script_data=json.dumps(vid),
                    scheduled_for=scheduled_dt,
                    voice_style=vid.get('voice_style', 'human'),
                    voice_gender=vid.get('voice_gender', 'female')
                )
                 db.add(new_video)
                 db.flush()
                 saved_ids.append(new_video.id)
                 count += 1
                 
        db.commit()
        
        # OTIMIZAÇÃO DE MEMÓRIA: Não iniciar processamento em paralelo.
        # Deixar o MonitorService pegar um por um a cada minuto.
        # for vid_id in saved_ids:
        #    background_tasks.add_task(process_scheduled_video, vid_id)
            
        return {"status": "success", "saved_items": count}
    except Exception as e:
        print(f"Erro ao salvar agendamento: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/schedule/{video_id}/generate")
def generate_scheduled_video(video_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    
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

@router.delete("/schedule/{video_id}")
def delete_scheduled_video(video_id: int, db: Session = Depends(get_db)):
    video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    
    # Opcional: deletar arquivo físico se existir
    if video.video_url:
        try:
            # Caminho relativo para absoluto
            abs_path = os.path.join("c:/dev/TRAE/codexia/app", video.video_url.lstrip('/'))
            if os.path.exists(abs_path):
                os.remove(abs_path)
        except Exception as e:
            print(f"Erro ao deletar arquivo: {e}")

    db.delete(video)
    db.commit()
    return {"status": "deleted"}

@router.get("/schedule")
def get_schedule(db: Session = Depends(get_db)):
    return db.query(ScheduledVideo).order_by(ScheduledVideo.id.desc()).all()

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
        
        # O path retornado é relativo para web (/static/...), precisamos do absoluto para upload
        abs_video_path = "c:/dev/TRAE/codexia/app" + video_path 
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
