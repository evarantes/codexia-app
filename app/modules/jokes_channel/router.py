import os
import json
import base64
import uuid
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.modules.jokes_channel.models import JokeAvatar, JokeTheme, Joke, JokeCompilation
from app.modules.jokes_channel.service import JokesChannelService, JOKE_THEMES_DEFAULT
from app.config import VIDEO_OUTPUT_DIR

router = APIRouter(prefix="/jokes", tags=["jokes-channel"])
service = JokesChannelService()


# ─── Request / Response Models ───

class GenerateJokesRequest(BaseModel):
    theme: str
    quantity: int = 10

class ManualJokeRequest(BaseModel):
    theme_id: Optional[int] = None
    title: Optional[str] = None
    content: str

class JokeUpdateRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    status: Optional[str] = None

class AvatarRequest(BaseModel):
    name: str = "Risadão"
    description: Optional[str] = None
    voice_gender: str = "male"

class AvatarUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    voice_gender: Optional[str] = None

class CompilationRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    theme_id: Optional[int] = None
    avatar_id: Optional[int] = None
    joke_ids: List[int] = []
    target_duration_min: int = 10

class CompilationReviewRequest(BaseModel):
    action: str  # approve | reject
    feedback: Optional[str] = None

class ThemeRequest(BaseModel):
    name: str
    description: Optional[str] = None
    icon: Optional[str] = "fa-laugh"


# ─── Temas ───

@router.get("/themes")
def list_themes(db: Session = Depends(get_db)):
    themes = db.query(JokeTheme).filter(JokeTheme.is_active == True).order_by(JokeTheme.name).all()
    return [{"id": t.id, "name": t.name, "description": t.description, "icon": t.icon} for t in themes]

@router.post("/themes")
def create_theme(req: ThemeRequest, db: Session = Depends(get_db)):
    theme = JokeTheme(name=req.name, description=req.description, icon=req.icon)
    db.add(theme)
    db.commit()
    db.refresh(theme)
    return {"id": theme.id, "name": theme.name, "message": "Tema criado com sucesso."}

@router.post("/themes/seed")
def seed_default_themes(db: Session = Depends(get_db)):
    """Popula os temas padrão se não existirem."""
    existing = db.query(JokeTheme).count()
    if existing > 0:
        return {"message": f"Já existem {existing} temas. Nenhum adicionado."}
    for t in JOKE_THEMES_DEFAULT:
        db.add(JokeTheme(name=t["name"], description=t["description"], icon=t["icon"]))
    db.commit()
    return {"message": f"{len(JOKE_THEMES_DEFAULT)} temas padrão adicionados."}

@router.delete("/themes/{theme_id}")
def delete_theme(theme_id: int, db: Session = Depends(get_db)):
    theme = db.query(JokeTheme).get(theme_id)
    if not theme:
        raise HTTPException(status_code=404, detail="Tema não encontrado.")
    theme.is_active = False
    db.commit()
    return {"message": "Tema desativado."}


# ─── Avatar ───

@router.get("/avatar")
def get_avatar(db: Session = Depends(get_db)):
    avatar = db.query(JokeAvatar).filter(JokeAvatar.is_active == True).first()
    if not avatar:
        return {"id": None, "name": None, "message": "Nenhum avatar configurado."}
    return {
        "id": avatar.id,
        "name": avatar.name,
        "description": avatar.description,
        "image_url": avatar.image_url,
        "has_image": bool(avatar.image_url or avatar.image_base64),
        "voice_gender": avatar.voice_gender,
        "created_at": str(avatar.created_at) if avatar.created_at else None
    }

@router.post("/avatar")
async def create_or_update_avatar(req: AvatarRequest, db: Session = Depends(get_db)):
    existing = db.query(JokeAvatar).filter(JokeAvatar.is_active == True).first()
    if existing:
        existing.name = req.name
        existing.description = req.description
        existing.voice_gender = req.voice_gender
        db.commit()
        db.refresh(existing)
        avatar = existing
    else:
        avatar = JokeAvatar(
            name=req.name,
            description=req.description,
            voice_gender=req.voice_gender
        )
        db.add(avatar)
        db.commit()
        db.refresh(avatar)
    return {"id": avatar.id, "name": avatar.name, "message": "Avatar salvo com sucesso."}

@router.put("/avatar/{avatar_id}")
async def update_avatar(avatar_id: int, req: AvatarUpdateRequest, db: Session = Depends(get_db)):
    avatar = db.query(JokeAvatar).get(avatar_id)
    if not avatar:
        raise HTTPException(status_code=404, detail="Avatar não encontrado.")
    if req.name is not None:
        avatar.name = req.name
    if req.description is not None:
        avatar.description = req.description
    if req.voice_gender is not None:
        avatar.voice_gender = req.voice_gender
    db.commit()
    return {"id": avatar.id, "message": "Avatar atualizado."}

@router.post("/avatar/generate-image")
async def generate_avatar_image(db: Session = Depends(get_db)):
    """Gera imagem do avatar via IA (DALL-E)."""
    avatar = db.query(JokeAvatar).filter(JokeAvatar.is_active == True).first()
    desc = avatar.description if avatar else None
    result = await service.generate_avatar_image(desc)
    if result.get("image_url") and avatar:
        avatar.image_url = result["image_url"]
        db.commit()
    return result

@router.post("/avatar/upload-image")
async def upload_avatar_image(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload manual de imagem do avatar."""
    avatar = db.query(JokeAvatar).filter(JokeAvatar.is_active == True).first()
    if not avatar:
        avatar = JokeAvatar(name="Risadão")
        db.add(avatar)
        db.commit()
        db.refresh(avatar)

    contents = await file.read()
    ext = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "png"
    filename = f"avatar_{avatar.id}_{uuid.uuid4().hex[:8]}.{ext}"
    filepath = os.path.join(VIDEO_OUTPUT_DIR, filename)
    os.makedirs(VIDEO_OUTPUT_DIR, exist_ok=True)
    with open(filepath, "wb") as f:
        f.write(contents)

    avatar.image_url = f"/media/videos/{filename}"
    b64 = base64.b64encode(contents).decode("utf-8")
    avatar.image_base64 = f"data:image/{ext};base64,{b64}"
    db.commit()

    return {"id": avatar.id, "image_url": avatar.image_url, "message": "Imagem do avatar enviada."}


# ─── Piadas ───

@router.get("/list")
def list_jokes(
    status: Optional[str] = None,
    theme_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    q = db.query(Joke)
    if status:
        q = q.filter(Joke.status == status)
    if theme_id:
        q = q.filter(Joke.theme_id == theme_id)
    total = q.count()
    jokes = q.order_by(Joke.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "jokes": [
            {
                "id": j.id,
                "title": j.title,
                "content": j.content,
                "source": j.source,
                "status": j.status,
                "theme": {"id": j.theme.id, "name": j.theme.name} if j.theme else None,
                "duration_sec": j.duration_sec,
                "created_at": str(j.created_at) if j.created_at else None
            }
            for j in jokes
        ]
    }

@router.post("/generate")
async def generate_jokes_ai(req: GenerateJokesRequest, db: Session = Depends(get_db)):
    """Gera piadas via IA e salva no banco."""
    theme_obj = None
    if req.theme:
        theme_obj = db.query(JokeTheme).filter(JokeTheme.name == req.theme).first()

    existing = [j.content for j in db.query(Joke).filter(Joke.status != "rejected").limit(20).all()]
    jokes_raw = await service.generate_jokes(req.theme, req.quantity, existing)

    saved = []
    for j in jokes_raw:
        content = j.get("content", str(j)) if isinstance(j, dict) else str(j)
        title = j.get("title", "") if isinstance(j, dict) else ""
        joke = Joke(
            theme_id=theme_obj.id if theme_obj else None,
            title=title,
            content=content,
            source="ai",
            status="draft"
        )
        db.add(joke)
        saved.append(joke)

    db.commit()
    for j in saved:
        db.refresh(j)

    return {
        "generated": len(saved),
        "jokes": [
            {"id": j.id, "title": j.title, "content": j.content, "status": j.status}
            for j in saved
        ]
    }

@router.post("/manual")
def add_manual_joke(req: ManualJokeRequest, db: Session = Depends(get_db)):
    """Adiciona uma piada manualmente."""
    joke = Joke(
        theme_id=req.theme_id,
        title=req.title,
        content=req.content,
        source="manual",
        status="draft"
    )
    db.add(joke)
    db.commit()
    db.refresh(joke)
    return {"id": joke.id, "message": "Piada adicionada com sucesso."}

@router.put("/joke/{joke_id}")
def update_joke(joke_id: int, req: JokeUpdateRequest, db: Session = Depends(get_db)):
    joke = db.query(Joke).get(joke_id)
    if not joke:
        raise HTTPException(status_code=404, detail="Piada não encontrada.")
    if req.title is not None:
        joke.title = req.title
    if req.content is not None:
        joke.content = req.content
    if req.status is not None:
        joke.status = req.status
    db.commit()
    return {"id": joke.id, "message": "Piada atualizada."}

@router.post("/joke/{joke_id}/approve")
def approve_joke(joke_id: int, db: Session = Depends(get_db)):
    joke = db.query(Joke).get(joke_id)
    if not joke:
        raise HTTPException(status_code=404, detail="Piada não encontrada.")
    joke.status = "approved"
    db.commit()
    return {"id": joke.id, "status": "approved"}

@router.post("/joke/{joke_id}/reject")
def reject_joke(joke_id: int, db: Session = Depends(get_db)):
    joke = db.query(Joke).get(joke_id)
    if not joke:
        raise HTTPException(status_code=404, detail="Piada não encontrada.")
    joke.status = "rejected"
    db.commit()
    return {"id": joke.id, "status": "rejected"}

@router.delete("/joke/{joke_id}")
def delete_joke(joke_id: int, db: Session = Depends(get_db)):
    joke = db.query(Joke).get(joke_id)
    if not joke:
        raise HTTPException(status_code=404, detail="Piada não encontrada.")
    db.delete(joke)
    db.commit()
    return {"message": "Piada excluída."}

@router.post("/approve-all")
def approve_all_drafts(db: Session = Depends(get_db)):
    """Aprova todas as piadas em rascunho."""
    count = db.query(Joke).filter(Joke.status == "draft").update({"status": "approved"})
    db.commit()
    return {"approved": count, "message": f"{count} piadas aprovadas."}


# ─── Compilação / Vídeo ───

@router.get("/compilations")
def list_compilations(
    status: Optional[str] = None,
    limit: int = 20,
    db: Session = Depends(get_db)
):
    q = db.query(JokeCompilation)
    if status:
        q = q.filter(JokeCompilation.status == status)
    comps = q.order_by(JokeCompilation.created_at.desc()).limit(limit).all()
    return [
        {
            "id": c.id,
            "title": c.title,
            "description": c.description,
            "status": c.status,
            "progress": c.progress,
            "total_jokes": c.total_jokes,
            "actual_duration_sec": c.actual_duration_sec,
            "video_url": c.video_url,
            "thumbnail_url": c.thumbnail_url,
            "theme": {"id": c.theme.id, "name": c.theme.name} if c.theme else None,
            "avatar": {"id": c.avatar.id, "name": c.avatar.name} if c.avatar else None,
            "youtube_video_id": c.youtube_video_id,
            "published_at": str(c.published_at) if c.published_at else None,
            "created_at": str(c.created_at) if c.created_at else None,
            "error_message": c.error_message
        }
        for c in comps
    ]

@router.post("/compilation/create")
async def create_compilation(req: CompilationRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Cria compilação e inicia geração do vídeo em background."""
    if req.joke_ids:
        jokes = db.query(Joke).filter(Joke.id.in_(req.joke_ids)).all()
    else:
        jokes = db.query(Joke).filter(Joke.status == "approved").order_by(Joke.created_at.desc()).all()

    if not jokes:
        raise HTTPException(status_code=400, detail="Nenhuma piada disponível para compilação. Gere ou aprove piadas primeiro.")

    min_jokes = max(5, req.target_duration_min * 2)
    jokes_list = jokes[:min_jokes * 3]

    avatar = None
    if req.avatar_id:
        avatar = db.query(JokeAvatar).get(req.avatar_id)
    if not avatar:
        avatar = db.query(JokeAvatar).filter(JokeAvatar.is_active == True).first()

    theme = None
    if req.theme_id:
        theme = db.query(JokeTheme).get(req.theme_id)

    title = req.title or f"Compilação de Piadas{' - ' + theme.name if theme else ''}"
    description = req.description or f"As melhores piadas selecionadas para você! Prepare-se para rir!"

    jokes_data = [{"id": j.id, "content": j.content, "title": j.title} for j in jokes_list]

    comp = JokeCompilation(
        title=title,
        description=description,
        avatar_id=avatar.id if avatar else None,
        theme_id=req.theme_id,
        jokes_json=jokes_data,
        total_jokes=len(jokes_list),
        target_duration_min=req.target_duration_min,
        status="generating",
        progress=0
    )
    db.add(comp)
    db.commit()
    db.refresh(comp)

    for j in jokes_list:
        j.status = "used"
    db.commit()

    background_tasks.add_task(
        _generate_compilation_background,
        comp.id,
        jokes_data,
        avatar,
        title,
        avatar.voice_gender if avatar else "male"
    )

    return {
        "id": comp.id,
        "title": comp.title,
        "total_jokes": len(jokes_list),
        "status": "generating",
        "message": "Compilação criada! Vídeo sendo gerado em background."
    }


def _generate_compilation_background(
    compilation_id: int,
    jokes_data: list,
    avatar,
    title: str,
    voice_gender: str
):
    """Task de background para gerar o vídeo da compilação."""
    db = SessionLocal()
    try:
        comp = db.query(JokeCompilation).get(compilation_id)
        if not comp:
            return

        def progress_cb(pct):
            try:
                comp.progress = pct
                db.commit()
            except Exception:
                pass

        avatar_path = None
        if avatar and avatar.image_url:
            from app.config import absolute_path_for_video
            avatar_path = absolute_path_for_video(avatar.image_url)
            if not os.path.isfile(avatar_path or ""):
                avatar_path = None

        svc = JokesChannelService()
        result = svc.generate_compilation_video(
            jokes=jokes_data,
            avatar_image_path=avatar_path,
            title=title,
            voice_gender=voice_gender,
            progress_callback=progress_cb
        )

        if result.get("error"):
            comp.status = "failed"
            comp.error_message = result["error"]
            comp.progress = 0
        else:
            comp.video_url = result.get("video_url")
            comp.actual_duration_sec = result.get("duration_sec")
            comp.status = "review"
            comp.progress = 100
            comp.error_message = None

        db.commit()
    except Exception as e:
        try:
            comp = db.query(JokeCompilation).get(compilation_id)
            if comp:
                comp.status = "failed"
                comp.error_message = str(e)
                comp.progress = 0
                db.commit()
        except Exception:
            pass
        print(f"Erro ao gerar compilação {compilation_id}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()


@router.get("/compilation/{comp_id}")
def get_compilation(comp_id: int, db: Session = Depends(get_db)):
    comp = db.query(JokeCompilation).get(comp_id)
    if not comp:
        raise HTTPException(status_code=404, detail="Compilação não encontrada.")
    return {
        "id": comp.id,
        "title": comp.title,
        "description": comp.description,
        "status": comp.status,
        "progress": comp.progress,
        "total_jokes": comp.total_jokes,
        "actual_duration_sec": comp.actual_duration_sec,
        "video_url": comp.video_url,
        "jokes": comp.jokes_json,
        "theme": {"id": comp.theme.id, "name": comp.theme.name} if comp.theme else None,
        "avatar": {"id": comp.avatar.id, "name": comp.avatar.name} if comp.avatar else None,
        "error_message": comp.error_message,
        "youtube_video_id": comp.youtube_video_id,
        "published_at": str(comp.published_at) if comp.published_at else None,
        "created_at": str(comp.created_at) if comp.created_at else None
    }

@router.post("/compilation/{comp_id}/review")
def review_compilation(comp_id: int, req: CompilationReviewRequest, db: Session = Depends(get_db)):
    """Aprova ou rejeita uma compilação após revisão."""
    comp = db.query(JokeCompilation).get(comp_id)
    if not comp:
        raise HTTPException(status_code=404, detail="Compilação não encontrada.")
    if comp.status != "review":
        raise HTTPException(status_code=400, detail=f"Compilação não está em revisão (status: {comp.status}).")

    if req.action == "approve":
        comp.status = "approved"
        db.commit()
        return {"id": comp.id, "status": "approved", "message": "Compilação aprovada! Pronta para publicar."}
    elif req.action == "reject":
        comp.status = "rejected"
        comp.error_message = req.feedback
        db.commit()
        return {"id": comp.id, "status": "rejected", "message": "Compilação rejeitada."}
    else:
        raise HTTPException(status_code=400, detail="Ação inválida. Use 'approve' ou 'reject'.")

@router.post("/compilation/{comp_id}/publish")
async def publish_compilation(comp_id: int, db: Session = Depends(get_db)):
    """Publica o vídeo da compilação no YouTube."""
    comp = db.query(JokeCompilation).get(comp_id)
    if not comp:
        raise HTTPException(status_code=404, detail="Compilação não encontrada.")
    if comp.status not in ("approved", "review"):
        raise HTTPException(status_code=400, detail=f"Compilação precisa estar aprovada para publicar (status: {comp.status}).")
    if not comp.video_url:
        raise HTTPException(status_code=400, detail="Vídeo não encontrado.")

    try:
        from app.services.youtube_service import YouTubeService
        yt = YouTubeService()

        from app.config import absolute_path_for_video
        video_path = absolute_path_for_video(comp.video_url)
        if not os.path.isfile(video_path):
            raise HTTPException(status_code=400, detail=f"Arquivo de vídeo não encontrado: {video_path}")

        tags_str = comp.description or ""
        tags = [t.strip() for t in tags_str.split(",") if t.strip()][:10]
        if not tags:
            tags = ["piadas", "humor", "comedia", "engraçado"]

        yt_result = yt.upload_video(
            file_path=video_path,
            title=comp.title[:100],
            description=comp.description or comp.title,
            tags=tags,
            category_id="23"
        )

        comp.youtube_video_id = yt_result.get("id")
        comp.published_at = datetime.utcnow()
        comp.status = "published"
        db.commit()

        return {
            "id": comp.id,
            "youtube_video_id": comp.youtube_video_id,
            "status": "published",
            "message": "Vídeo publicado no YouTube com sucesso!"
        }
    except HTTPException:
        raise
    except Exception as e:
        comp.status = "approved"
        comp.error_message = f"Erro ao publicar: {str(e)}"
        db.commit()
        raise HTTPException(status_code=500, detail=f"Erro ao publicar no YouTube: {str(e)}")

@router.post("/compilation/{comp_id}/retry")
async def retry_compilation(comp_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Regenera o vídeo de uma compilação que falhou."""
    comp = db.query(JokeCompilation).get(comp_id)
    if not comp:
        raise HTTPException(status_code=404, detail="Compilação não encontrada.")
    if comp.status not in ("failed", "rejected"):
        raise HTTPException(status_code=400, detail="Só é possível retentar compilações com falha ou rejeitadas.")

    comp.status = "generating"
    comp.progress = 0
    comp.error_message = None
    db.commit()

    avatar = comp.avatar
    background_tasks.add_task(
        _generate_compilation_background,
        comp.id,
        comp.jokes_json or [],
        avatar,
        comp.title,
        avatar.voice_gender if avatar else "male"
    )

    return {"id": comp.id, "status": "generating", "message": "Regenerando vídeo..."}

@router.delete("/compilation/{comp_id}")
def delete_compilation(comp_id: int, db: Session = Depends(get_db)):
    comp = db.query(JokeCompilation).get(comp_id)
    if not comp:
        raise HTTPException(status_code=404, detail="Compilação não encontrada.")

    if comp.video_url:
        try:
            from app.config import absolute_path_for_video
            path = absolute_path_for_video(comp.video_url)
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass

    db.delete(comp)
    db.commit()
    return {"message": "Compilação excluída."}


# ─── Geração Automática de Título ───

@router.post("/generate-title")
async def generate_title(theme: str = "Variado"):
    result = await service.generate_compilation_title(theme)
    return result


# ─── Stats ───

@router.get("/stats")
def jokes_stats(db: Session = Depends(get_db)):
    total_jokes = db.query(Joke).count()
    draft_jokes = db.query(Joke).filter(Joke.status == "draft").count()
    approved_jokes = db.query(Joke).filter(Joke.status == "approved").count()
    total_comps = db.query(JokeCompilation).count()
    pending_review = db.query(JokeCompilation).filter(JokeCompilation.status == "review").count()
    published = db.query(JokeCompilation).filter(JokeCompilation.status == "published").count()

    return {
        "total_jokes": total_jokes,
        "draft_jokes": draft_jokes,
        "approved_jokes": approved_jokes,
        "total_compilations": total_comps,
        "pending_review": pending_review,
        "published": published
    }
