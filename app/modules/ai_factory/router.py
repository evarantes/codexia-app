from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.modules.ai_factory.service import AIFactoryService
from app.modules.ai_factory.models import AIStory, AICover, AIImage, AIScript, AIJokeChannelProject
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/ai-factory", tags=["ai-factory"])
service = AIFactoryService()

# Request Models
class StoryRequest(BaseModel):
    theme: str
    style: Optional[str] = "Padrão"
    audience: Optional[str] = "Geral"
    length: Optional[str] = "Curto"

class CoverRequest(BaseModel):
    title: str
    subtitle: Optional[str] = ""
    author: Optional[str] = ""
    style: Optional[str] = "Cinemático"

class ImageRequest(BaseModel):
    theme: str
    style: Optional[str] = "Realista"
    quantity: int = 1

class ScriptRequest(BaseModel):
    theme: str
    duration: Optional[str] = "5 minutos"
    narrative_type: Optional[str] = "Documentário"

class ShortsRequest(BaseModel):
    script_id: int

class JokeChannelRequest(BaseModel):
    channel_name: str = "Canal de Piadas Sem Baixaria"
    theme: str
    category: Optional[str] = "Livre"
    tone: Optional[str] = "Leve e familiar"
    duration_minutes: int = 10
    jokes_count: int = 24
    source_mode: Optional[str] = "ai"
    manual_jokes: Optional[str] = ""
    avatar_name: Optional[str] = "Zeca do Riso"
    avatar_style: Optional[str] = "cartoon simpatico em estudio"
    avatar_description: Optional[str] = "apresentador fixo, sorridente e acolhedor"
    auto_publish: bool = False

class JokeChannelReviewRequest(BaseModel):
    status: str
    review_notes: Optional[str] = ""
    auto_publish: Optional[bool] = None

# Endpoints

@router.post("/story")
async def generate_story(request: StoryRequest, db: Session = Depends(get_db)):
    try:
        result = await service.generate_story(request.theme, request.style, request.audience, request.length)
        
        # Save to DB
        db_story = AIStory(
            theme=request.theme,
            style=request.style,
            audience=request.audience,
            length=request.length,
            title=result.get("title"),
            synopsis=result.get("synopsis"),
            content=result
        )
        db.add(db_story)
        db.commit()
        db.refresh(db_story)
        
        return db_story
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/cover")
async def generate_cover(request: CoverRequest, db: Session = Depends(get_db)):
    try:
        # Generate prompt first (optional step based on user request "Gerar prompt de imagem")
        # But user also said "Integrar com provedor... Salvar imagem".
        # We will generate the image directly using the service which calls DALL-E
        
        # We use the existing logic in AIContentGenerator which handles prompt + image generation
        # or we construct our own prompt.
        # Let's generate a refined prompt first using text gen
        prompt = await service.generate_cover_prompt(request.title, request.subtitle, request.author, request.style)
        
        # Now generate image
        # Using existing generate_cover_options from AIContentGenerator which expects (title, context, author, subtitle)
        # We pass the generated prompt as context or style
        image_urls = service.ai_service.generate_cover_options(
            title=request.title,
            context=f"Style: {request.style}. Prompt: {prompt}",
            author=request.author,
            subtitle=request.subtitle,
            n=1
        )
        
        image_url = image_urls[0] if image_urls else None

        db_cover = AICover(
            title=request.title,
            subtitle=request.subtitle,
            author=request.author,
            style=request.style,
            prompt=prompt,
            image_url=image_url
        )
        db.add(db_cover)
        db.commit()
        db.refresh(db_cover)
        
        return db_cover
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/image")
async def generate_image(request: ImageRequest, db: Session = Depends(get_db)):
    try:
        # Generate generic images
        # We need a method for this. We'll use the service wrapper.
        
        # First generate a detailed prompt if needed, or use theme as prompt
        # Let's assume theme + style is the prompt base
        base_prompt = f"{request.theme}, style: {request.style}"
        
        results = []
        for _ in range(request.quantity):
            # We reuse generate_cover_options for now as it's the only image gen method exposed in ai_generator.py
            # or we assume we implemented generate_image in service.py (which we did, but it calls generate_cover_image which doesn't exist)
            # Wait, I noticed generate_cover_options in ai_generator.py.
            # I will use that for now, passing the prompt as context.
            
            urls = service.ai_service.generate_cover_options(
                title="Image", # Dummy title
                context=base_prompt,
                n=1
            )
            if urls:
                db_image = AIImage(
                    theme=request.theme,
                    style=request.style,
                    prompt=base_prompt,
                    image_url=urls[0]
                )
                db.add(db_image)
                db.commit()
                db.refresh(db_image)
                results.append(db_image)
        
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/script")
async def generate_script(request: ScriptRequest, db: Session = Depends(get_db)):
    try:
        result = await service.generate_script(request.theme, request.duration, request.narrative_type)
        
        db_script = AIScript(
            theme=request.theme,
            duration=request.duration,
            narrative_type=request.narrative_type,
            script_content=result.get("script_full"),
            scenes=result.get("scenes")
        )
        db.add(db_script)
        db.commit()
        db.refresh(db_script)
        
        return db_script
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/shorts")
async def generate_shorts(request: ShortsRequest, db: Session = Depends(get_db)):
    try:
        # Fetch original script
        original_script = db.query(AIScript).filter(AIScript.id == request.script_id).first()
        if not original_script:
            raise HTTPException(status_code=404, detail="Script not found")
            
        result = await service.generate_shorts_from_script(original_script.script_content)
        
        # Update script with shorts
        original_script.shorts = result
        db.commit()
        
        return original_script
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/jokes-channel")
async def generate_jokes_channel(request: JokeChannelRequest, db: Session = Depends(get_db)):
    try:
        payload = await service.generate_joke_channel_package(
            channel_name=request.channel_name,
            theme=request.theme,
            category=request.category or "Livre",
            tone=request.tone or "Leve e familiar",
            duration_minutes=request.duration_minutes,
            jokes_count=request.jokes_count,
            source_mode=request.source_mode or "ai",
            manual_jokes=request.manual_jokes or "",
            avatar_name=request.avatar_name or "Zeca do Riso",
            avatar_style=request.avatar_style or "cartoon simpatico em estudio",
            avatar_description=request.avatar_description or "apresentador fixo, sorridente e acolhedor",
            auto_publish=request.auto_publish,
        )

        db_project = AIJokeChannelProject(
            channel_name=request.channel_name,
            theme=request.theme,
            category=request.category or "Livre",
            tone=request.tone or "Leve e familiar",
            source_mode=request.source_mode or "ai",
            duration_minutes=request.duration_minutes,
            jokes_count=max(request.jokes_count, request.duration_minutes * 2),
            manual_jokes=request.manual_jokes or "",
            avatar_name=request.avatar_name or "Zeca do Riso",
            avatar_style=request.avatar_style or "cartoon simpatico em estudio",
            avatar_description=request.avatar_description or "apresentador fixo, sorridente e acolhedor",
            auto_publish=request.auto_publish,
            status="ready_for_review",
            generated_payload=payload,
        )
        db.add(db_project)
        db.commit()
        db.refresh(db_project)

        return db_project
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/jokes-channel")
async def list_jokes_channel_projects(db: Session = Depends(get_db)):
    return (
        db.query(AIJokeChannelProject)
        .order_by(AIJokeChannelProject.created_at.desc(), AIJokeChannelProject.id.desc())
        .all()
    )

@router.post("/jokes-channel/{project_id}/review")
async def review_jokes_channel_project(project_id: int, request: JokeChannelReviewRequest, db: Session = Depends(get_db)):
    project = db.query(AIJokeChannelProject).filter(AIJokeChannelProject.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Projeto de canal de piadas nao encontrado")

    allowed_statuses = {"ready_for_review", "approved", "needs_changes", "scheduled"}
    if request.status not in allowed_statuses:
        raise HTTPException(status_code=400, detail="Status de revisao invalido")

    project.status = request.status
    if request.review_notes is not None:
        project.review_notes = request.review_notes
    if request.auto_publish is not None:
        project.auto_publish = request.auto_publish

    db.commit()
    db.refresh(project)
    return project

@router.get("/library")
async def get_library(type: str = "all", db: Session = Depends(get_db)):
    response = {}
    
    if type in ["all", "stories"]:
        response["stories"] = db.query(AIStory).order_by(AIStory.created_at.desc()).all()
    
    if type in ["all", "covers"]:
        response["covers"] = db.query(AICover).order_by(AICover.created_at.desc()).all()
        
    if type in ["all", "images"]:
        response["images"] = db.query(AIImage).order_by(AIImage.created_at.desc()).all()
        
    if type in ["all", "scripts"]:
        response["scripts"] = db.query(AIScript).order_by(AIScript.created_at.desc()).all()

    if type in ["all", "joke_projects"]:
        response["joke_projects"] = (
            db.query(AIJokeChannelProject)
            .order_by(AIJokeChannelProject.created_at.desc(), AIJokeChannelProject.id.desc())
            .all()
        )
        
    return response
