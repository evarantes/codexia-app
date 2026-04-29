from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Book, Post
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
    storyboard_quantity: int = 15
    storyboard_images: Optional[List[str]] = None

@router.post("/create")
def create_video(request: CreateVideoRequest):
    # Lazy import para reduzir memória no startup (moviepy/PIL/numpy)
    from app.services.video_generator import VideoGenerator
    from app.services.image_storyboard_service import generate_storyboard_images
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

        storyboard = None
        provided = request.storyboard_images if isinstance(request.storyboard_images, list) else None
        provided_urls = [str(u or "").strip() for u in (provided or []) if isinstance(u, str) and str(u or "").strip()]
        if provided_urls:
            script_plan["selected_images"] = provided_urls
        else:
            full_text = "\n".join(
                [
                    (script_plan.get("title") or request.title or "").strip(),
                    "\n".join([str((s or {}).get("text") or "").strip() for s in (script_plan.get("scenes") or []) if isinstance(s, dict)]),
                ]
            ).strip()
            storyboard = generate_storyboard_images(full_text, quantity=request.storyboard_quantity)
            storyboard_urls = [str(it.get("url") or "").strip() for it in (storyboard.get("images") or []) if isinstance(it, dict)]
            storyboard_urls = [u for u in storyboard_urls if u]
            if storyboard_urls:
                script_plan["selected_images"] = storyboard_urls
            
        # Generate Video (9:16 para Short, 16:9 para os demais)
        result = video_gen.create_video_from_plan(
            script_plan,
            aspect_ratio=aspect_ratio,
            voice_style=request.voice_style,
            voice_gender=request.voice_gender
        )
        
        return {"video_url": result["video_url"], "script": script_plan, "music_credit": result.get("music_credit"), "storyboard": storyboard}
        
    except Exception as e:
        print(f"Erro ao criar vídeo ({request.mode}): {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate")
def generate_video(request: VideoRequest):
    from app.services.video_generator import VideoGenerator
    from app.services.image_storyboard_service import generate_storyboard_images
    try:
        filename = f"{uuid.uuid4()}.mp4"
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
            "scenes": [{"text": line} for line in request.script if line.strip()]
        }

        full_text = "\n".join(
            [
                (script_plan.get("title") or request.title or "").strip(),
                "\n".join([str((s or {}).get("text") or "").strip() for s in (script_plan.get("scenes") or []) if isinstance(s, dict)]),
            ]
        ).strip()
        storyboard = generate_storyboard_images(full_text, quantity=15)
        storyboard_urls = [str(it.get("url") or "").strip() for it in (storyboard.get("images") or []) if isinstance(it, dict)]
        storyboard_urls = [u for u in storyboard_urls if u]
        if storyboard_urls:
            script_plan["selected_images"] = storyboard_urls
        
        # Usa o pipeline moderno (create_video_from_plan) em vez do legado
        result = local_video_gen.create_video_from_plan(
            script_plan,
            aspect_ratio="16:9",
            voice_style="human", # Default melhor
            voice_gender="female"
        )
        
        return {"video_url": result["video_url"]}
    except Exception as e:
        print(f"Erro ao gerar vídeo: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate-auto")
def generate_auto_video(request: AutoVideoRequest, db: Session = Depends(get_db)):
    from app.services.video_generator import VideoGenerator
    from app.services.image_storyboard_service import generate_storyboard_images
    try:
        book = db.query(Book).filter(Book.id == request.book_id).first()
        if not book:
            raise HTTPException(status_code=404, detail="Book not found")
        
        ai_service = AIContentGenerator()
        script_plan = ai_service.generate_video_script(book.title, book.synopsis, request.style)
        video_gen = VideoGenerator(ai_service=ai_service)

        full_text = "\n".join(
            [
                (script_plan.get("title") or book.title or "").strip(),
                "\n".join([str((s or {}).get("text") or "").strip() for s in (script_plan.get("scenes") or []) if isinstance(s, dict)]),
            ]
        ).strip()
        storyboard = generate_storyboard_images(full_text, quantity=15)
        storyboard_urls = [str(it.get("url") or "").strip() for it in (storyboard.get("images") or []) if isinstance(it, dict)]
        storyboard_urls = [u for u in storyboard_urls if u]
        if storyboard_urls:
            script_plan["selected_images"] = storyboard_urls
        
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
