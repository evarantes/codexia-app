from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Book, Post
from app.services.video_generator import VideoGenerator
from app.services.ai_generator import AIContentGenerator
import uuid

router = APIRouter(prefix="/video", tags=["Video"])

class VideoRequest(BaseModel):
    title: str
    script: List[str]

class AutoVideoRequest(BaseModel):
    book_id: int
    style: str = "drama"

class CreateVideoRequest(BaseModel):
    mode: str = "manual" # manual, topic, story
    title: str
    content: str # Script lines (manual), Topic (topic), or Story Prompt (story)
    duration: int = 1
    voice_style: Optional[str] = "human"
    voice_gender: Optional[str] = "female"

@router.post("/create")
def create_video(request: CreateVideoRequest):
    try:
        ai_service = AIContentGenerator()
        video_gen = VideoGenerator(ai_service=ai_service)
        
        script_plan = {}
        
        if request.mode == "manual":
            script_plan = {
                "title": request.title,
                "scenes": [{"text": line} for line in request.content.split('\n') if line.strip()]
            }
            aspect_ratio = "16:9"
        elif request.mode == "topic":
            script_plan = ai_service.generate_motivational_script(request.content, request.duration)
            script_plan["title"] = request.title
            aspect_ratio = "16:9"
        elif request.mode == "story":
            script_plan = ai_service.generate_video_script(request.title, request.content, "story")
            aspect_ratio = "16:9"
        elif request.mode == "short":
            # YouTube Short por prompt: um único prompt → roteiro curto → vídeo vertical 9:16
            script_plan = ai_service.generate_short_script_from_prompt(request.content)
            script_plan["title"] = request.title or script_plan.get("title", "Short")
            aspect_ratio = "9:16"
        else:
            script_plan = ai_service.generate_video_script(request.title, request.content, "drama")
            aspect_ratio = "16:9"
            
        # Generate Video (9:16 para Short, 16:9 para os demais)
        result = video_gen.create_video_from_plan(
            script_plan,
            aspect_ratio=aspect_ratio,
            voice_style=request.voice_style,
            voice_gender=request.voice_gender
        )
        
        return {"video_url": result["video_url"], "script": script_plan, "music_credit": result.get("music_credit")}
        
    except Exception as e:
        print(f"Erro ao criar vídeo ({request.mode}): {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate")
def generate_video(request: VideoRequest):
    try:
        filename = f"{uuid.uuid4()}.mp4"
        # Instancia sob demanda para evitar problemas de startup
        local_video_gen = VideoGenerator()
        video_url = local_video_gen.generate_simple_video(request.title, request.script, filename)
        return {"video_url": video_url}
    except Exception as e:
        print(f"Erro ao gerar vídeo: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate-auto")
def generate_auto_video(request: AutoVideoRequest, db: Session = Depends(get_db)):
    try:
        book = db.query(Book).filter(Book.id == request.book_id).first()
        if not book:
            raise HTTPException(status_code=404, detail="Book not found")
        
        # 1. Gerar Roteiro com IA
        ai_service = AIContentGenerator()
        script_plan = ai_service.generate_video_script(book.title, book.synopsis, request.style)
        
        # 2. Gerar Vídeo
        # Instancia VideoGenerator passando ai_service para gerar imagens
        video_gen = VideoGenerator(ai_service=ai_service)
        
        # Resolve caminho da capa se existir
        cover_path = None
        if book.cover_image_url:
            # Se for url relativa, converte para caminho absoluto
            if book.cover_image_url.startswith("/static"):
                cover_path = f"app{book.cover_image_url}"
            else:
                # Se for URL externa, precisaria baixar. Por enquanto assume local se começar com /static
                # TODO: Implementar download de capa externa se necessário
                pass

        result = video_gen.create_video_from_plan(script_plan, cover_image_path=cover_path)
        
        return {"video_url": result["video_url"], "script": script_plan, "music_credit": result.get("music_credit")}
    except Exception as e:
        print(f"Erro ao gerar vídeo automático: {e}")
        raise HTTPException(status_code=500, detail=str(e))
