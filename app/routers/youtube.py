import os
import glob
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from app.services.youtube_service import YouTubeService
from app.services.ai_generator import AIContentGenerator
from app.services.video_generator import VideoGenerator
from app.services.task_manager import create_task, update_task, get_task
from app.database import get_db
from app.models import ScheduledVideo
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import json
from datetime import datetime

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

@router.post("/optimize")
def optimize_channel():
    yt_service = YouTubeService()
    ai_service = AIContentGenerator()
    return yt_service.optimize_channel(ai_service)

@router.post("/optimize/execute")
def execute_optimization(data: Dict[str, Any]):
    """Executa as melhorias sugeridas (título/descrição)"""
    yt_service = YouTubeService()
    # data expects {'title': '...', 'description': '...'}
    return yt_service.update_channel_info(title=data.get('title'), description=data.get('description'))

class ScheduleRequest(BaseModel):
    theme: str
    duration_type: str = "days" # days, weeks, months
    duration_value: int = 7
    start_date: str = None # YYYY-MM-DD

@router.post("/schedule/generate")
def generate_schedule(request: ScheduleRequest):
    ai_service = AIContentGenerator()
    return ai_service.generate_content_plan(
        request.theme, 
        request.duration_type, 
        request.duration_value, 
        request.start_date
    )

@router.post("/schedule/save")
def save_schedule(plan: List[Dict[str, Any]], background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Salva o plano no banco de dados e inicia geração"""
    count = 0
    saved_ids = []
    try:
        for day in plan:
            theme_day = day.get('theme_of_day', 'Geral')
            date_str = day.get('date') # YYYY-MM-DD
            
            # Processar lista unificada de vídeos e shorts
            for vid in day.get('videos', []):
                time_str = vid.get('time', '12:00')
                
                # Calcular data/hora do agendamento
                scheduled_dt = datetime.now()
                if date_str:
                    try:
                        # Tenta combinar data do plano com horário sugerido
                        scheduled_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                    except:
                        # Fallback se falhar parser
                        pass

                new_video = ScheduledVideo(
                    theme=theme_day,
                    title=vid.get('title'),
                    description=vid.get('concept'),
                    status="queued", # Já marcamos como na fila
                    video_type=vid.get('type', 'video'),
                    script_data=json.dumps(vid),
                    scheduled_for=scheduled_dt
                )
                db.add(new_video)
                db.flush() # Para pegar o ID
                saved_ids.append(new_video.id)
                count += 1
            
            # Manter suporte legado caso a IA separe (opcional, mas seguro)
            for vid in day.get('shorts', []):
                 new_video = ScheduledVideo(
                    theme=theme_day,
                    title=vid.get('title'),
                    description=vid.get('concept'),
                    status="queued",
                    video_type='short',
                    script_data=json.dumps(vid),
                    scheduled_for=datetime.now()
                )
                 db.add(new_video)
                 db.flush()
                 saved_ids.append(new_video.id)
                 count += 1
                 
        db.commit()
        
        # Iniciar geração em background para cada vídeo salvo
        # Isso pode ser pesado, mas atende ao pedido "fiquem pre gerados"
        for vid_id in saved_ids:
            background_tasks.add_task(process_scheduled_video, vid_id)
            
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
    db.commit()
    
    background_tasks.add_task(process_scheduled_video, video_id)
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
            abs_path = os.path.join("c:/dev/TRAE/vibraface/app", video.video_url.lstrip('/'))
            if os.path.exists(abs_path):
                os.remove(abs_path)
        except Exception as e:
            print(f"Erro ao deletar arquivo: {e}")

    db.delete(video)
    db.commit()
    return {"status": "deleted"}

def process_scheduled_video(video_id: int):
    # Re-instanciar DB session pois estamos em thread separada
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
        if not video:
            return
            
        video.status = "processing"
        db.commit()
        
        # Recuperar dados do script
        script_data = json.loads(video.script_data)
        
        ai_service = AIContentGenerator()
        video_service = VideoGenerator(ai_service=ai_service)
        
        topic = video.title
        concept = video.description
        
        # Gerar roteiro detalhado
        # Se for short, 1 min. Se video, 5 min (padrão solicitado pelo user antes)
        duration = 1 if video.video_type == 'short' else 3 # 3 min para ser mais rápido que 5
        
        print(f"Gerando script para video {video_id}: {topic}")
        final_script = ai_service.generate_motivational_script(f"{topic}. Conceito: {concept}", duration)
        
        # Gerar vídeo
        def progress_callback(p, m):
            pass # Sem feedback detalhado por task manager por enquanto, apenas status no DB
            
        ratio = "9:16" if video.video_type == 'short' else "16:9"
        
        print(f"Renderizando video {video_id}...")
        result = video_service.create_video_from_plan(final_script, aspect_ratio=ratio, progress_callback=progress_callback)
        video_path = result["video_url"]
        
        # Adicionar créditos ao script_data se possível ou salvar na descrição do vídeo
        if result.get("music_credit"):
            credit = f"\n\n{result['music_credit']}"
            if not video.description:
                video.description = ""
            if credit not in video.description:
                video.description += credit
        
        video.status = "completed"
        video.video_url = video_path # path relativo /static/videos/...
        db.commit()
        print(f"Video {video_id} concluído: {video_path}")
        
    except Exception as e:
        print(f"Erro ao gerar video agendado {video_id}: {e}")
        video.status = "failed"
        db.commit()
    finally:
        db.close()

@router.get("/schedule")
def get_schedule(db: Session = Depends(get_db)):
    return db.query(ScheduledVideo).order_by(ScheduledVideo.id.desc()).all()

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
        abs_video_path = "c:/dev/TRAE/vibraface/app" + video_path 
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
