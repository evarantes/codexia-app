from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import List, Optional, Literal
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Book

router = APIRouter(prefix="/video", tags=["Video"])

class VideoRequest(BaseModel):
    title: str
    script: List[str]

class AutoVideoRequest(BaseModel):
    book_id: int
    style: str = "drama"

class CreateVideoRequest(BaseModel):
    mode: Literal["manual", "topic", "story", "short"] = "manual"
    title: str
    content: str # Script lines (manual), Topic (topic), or Story Prompt (story)
    duration: int = Field(default=1, ge=1, le=15)
    voice_style: Optional[str] = "human"
    voice_gender: Optional[str] = "female"

def _extract_script_lines(content: str) -> List[str]:
    return [line.strip() for line in (content or "").split('\n') if line and line.strip()]

@router.post("/create")
def create_video(request: CreateVideoRequest):
    # Lazy import para reduzir memória no startup (moviepy/PIL/numpy)
    from app.services.ai_generator import AIContentGenerator
    from app.services.video_generator import VideoGenerator
    try:
        ai_service = AIContentGenerator()
        video_gen = VideoGenerator(ai_service=ai_service)
        
        script_plan = {}
        content = (request.content or "").strip()
        
        if request.mode == "manual":
            script_lines = _extract_script_lines(content)
            if not script_lines:
                raise HTTPException(status_code=400, detail="No modo manual, informe ao menos uma linha de roteiro.")
            script_plan = {
                "title": request.title,
                "scenes": [{"text": line} for line in script_lines]
            }
            aspect_ratio = "16:9"
        elif request.mode == "topic":
            if not content:
                raise HTTPException(status_code=400, detail="Informe um tema para gerar o vídeo.")
            script_plan = ai_service.generate_motivational_script(content, request.duration)
            script_plan["title"] = request.title
            aspect_ratio = "16:9"
        elif request.mode == "story":
            if not content:
                raise HTTPException(status_code=400, detail="Informe o conteúdo da história para gerar o vídeo.")
            script_plan = ai_service.generate_video_script(request.title, content, "story")
            aspect_ratio = "16:9"
        elif request.mode == "short":
            # YouTube Short por prompt: um único prompt → roteiro curto → vídeo vertical 9:16
            if not content:
                raise HTTPException(status_code=400, detail="Informe o prompt do Short para gerar o vídeo.")
            script_plan = ai_service.generate_short_script_from_prompt(content)
            script_plan["title"] = request.title or script_plan.get("title", "Short")
            aspect_ratio = "9:16"

        if not script_plan.get("scenes"):
            raise HTTPException(status_code=400, detail="Não foi possível gerar cenas para o vídeo. Ajuste o conteúdo e tente novamente.")
            
        # Generate Video (9:16 para Short, 16:9 para os demais)
        result = video_gen.create_video_from_plan(
            script_plan,
            aspect_ratio=aspect_ratio,
            voice_style=request.voice_style,
            voice_gender=request.voice_gender
        )
        
        return {"video_url": result["video_url"], "script": script_plan, "music_credit": result.get("music_credit")}
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Erro ao criar vídeo ({request.mode}): {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate")
def generate_video(request: VideoRequest):
    from app.services.video_generator import VideoGenerator
    from app.services.ai_generator import AIContentGenerator
    try:
        script_lines = [line.strip() for line in request.script if line and line.strip()]
        if not script_lines:
            raise HTTPException(status_code=400, detail="Informe ao menos uma linha de roteiro para gerar o vídeo.")

        # Tenta inicializar IA para melhores imagens/audio
        try:
            ai_service = AIContentGenerator()
        except Exception as e:
            print(f"Aviso: Não foi possível iniciar AI Service: {e}")
            ai_service = None
            
        local_video_gen = VideoGenerator(ai_service=ai_service)
        
        # Se tiver IA, podemos tentar enriquecer o script ou usar create_video_from_plan
        # Mas para manter compatibilidade com "simple video", passamos apenas o script
        # O VideoGenerator internamente pode usar o ai_service se implementado em generate_simple_video
        # Mas generate_simple_video (legacy) talvez não use. 
        # Vamos verificar se podemos usar create_video_from_plan que é mais robusto.
        
        # Converte script simples para plano de cenas
        script_plan = {
            "title": request.title,
            "scenes": [{"text": line} for line in script_lines]
        }
        
        # Usa o pipeline moderno (create_video_from_plan) em vez do legado
        result = local_video_gen.create_video_from_plan(
            script_plan,
            aspect_ratio="16:9",
            voice_style="human", # Default melhor
            voice_gender="female"
        )
        
        return {"video_url": result["video_url"]}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Erro ao gerar vídeo: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate-auto")
def generate_auto_video(request: AutoVideoRequest, db: Session = Depends(get_db)):
    from app.services.ai_generator import AIContentGenerator
    from app.services.video_generator import VideoGenerator
    try:
        book = db.query(Book).filter(Book.id == request.book_id).first()
        if not book:
            raise HTTPException(status_code=404, detail="Book not found")
        
        ai_service = AIContentGenerator()
        script_plan = ai_service.generate_video_script(book.title, book.synopsis, request.style)
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
