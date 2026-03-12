"""
Router do Canal de Piadas.
Gerencia: configuração do canal, banco de piadas, episódios e geração de vídeo.
"""
import json
import threading
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import JokesChannel, JokeItem, JokesEpisode, Settings
from app.routers.auth import get_current_user
from app.services.ai_generator import AIContentGenerator

router = APIRouter(prefix="/jokes-channel", tags=["Canal de Piadas"])


# ─── Schemas ─────────────────────────────────────────────────────────────────

class ChannelCreate(BaseModel):
    name: str = "Canal de Piadas"
    description: Optional[str] = None
    default_theme: str = "geral"
    voice_gender: str = "male"
    avatar_prompt: Optional[str] = None
    background_music: str = "comedy"


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    default_theme: Optional[str] = None
    voice_gender: Optional[str] = None
    background_music: Optional[str] = None


class JokeCreate(BaseModel):
    theme: str = "geral"
    title: Optional[str] = None
    text: str
    punchline: Optional[str] = None
    source: str = "manual"


class JokeGenerateRequest(BaseModel):
    theme: str = "geral"
    count: int = 20


class EpisodeCreate(BaseModel):
    title: str
    theme: str = "geral"
    joke_ids: List[int] = []
    generate_jokes: bool = False
    joke_count: int = 20


class EpisodeApprove(BaseModel):
    approved: bool = True


# ─── Canal ───────────────────────────────────────────────────────────────────

@router.get("/channel")
def get_channel(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    user_id = getattr(current_user, "id", None)
    ch = db.query(JokesChannel).filter(JokesChannel.user_id == user_id).first()
    if not ch:
        return {"channel": None}
    return {"channel": _channel_to_dict(ch)}


@router.post("/channel")
def create_or_update_channel(
    data: ChannelCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user_id = getattr(current_user, "id", None)
    ch = db.query(JokesChannel).filter(JokesChannel.user_id == user_id).first()
    if ch:
        ch.name = data.name
        ch.description = data.description
        ch.default_theme = data.default_theme
        ch.voice_gender = data.voice_gender
        ch.background_music = data.background_music
        if data.avatar_prompt:
            ch.avatar_prompt = data.avatar_prompt
        ch.updated_at = datetime.utcnow()
    else:
        ch = JokesChannel(
            user_id=user_id,
            name=data.name,
            description=data.description,
            default_theme=data.default_theme,
            voice_gender=data.voice_gender,
            avatar_prompt=data.avatar_prompt,
            background_music=data.background_music,
        )
        db.add(ch)
    db.commit()
    db.refresh(ch)
    return {"channel": _channel_to_dict(ch)}


@router.post("/channel/generate-avatar")
def generate_avatar(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Gera (ou regenera) o avatar do canal via DALL-E."""
    user_id = getattr(current_user, "id", None)
    ch = db.query(JokesChannel).filter(JokesChannel.user_id == user_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Canal não encontrado. Crie o canal primeiro.")

    try:
        ai_service = AIContentGenerator()
        from app.services.jokes_service import JokesVideoService
        svc = JokesVideoService(ai_service=ai_service)
        url, b64 = svc.generate_avatar(ch.avatar_prompt)
        if not url:
            raise HTTPException(status_code=500, detail="Falha ao gerar avatar via IA.")
        ch.avatar_url = url
        ch.avatar_base64 = b64
        ch.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(ch)
        return {"avatar_url": url, "channel": _channel_to_dict(ch)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Piadas ──────────────────────────────────────────────────────────────────

@router.get("/jokes")
def list_jokes(
    theme: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user_id = getattr(current_user, "id", None)
    q = db.query(JokeItem).filter(JokeItem.user_id == user_id)
    if theme:
        q = q.filter(JokeItem.theme == theme)
    if status:
        q = q.filter(JokeItem.status == status)
    jokes = q.order_by(JokeItem.created_at.desc()).limit(limit).all()
    return {"jokes": [_joke_to_dict(j) for j in jokes]}


@router.post("/jokes")
def add_joke(
    data: JokeCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user_id = getattr(current_user, "id", None)
    ch = db.query(JokesChannel).filter(JokesChannel.user_id == user_id).first()
    joke = JokeItem(
        channel_id=ch.id if ch else None,
        user_id=user_id,
        theme=data.theme,
        title=data.title or "",
        text=data.text,
        punchline=data.punchline or "",
        source=data.source,
        status="approved",
    )
    db.add(joke)
    db.commit()
    db.refresh(joke)
    return {"joke": _joke_to_dict(joke)}


@router.post("/jokes/generate")
def generate_jokes(
    data: JokeGenerateRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Gera `count` piadas via IA e salva no banco."""
    user_id = getattr(current_user, "id", None)
    ch = db.query(JokesChannel).filter(JokesChannel.user_id == user_id).first()

    try:
        ai_service = AIContentGenerator()
        from app.services.jokes_service import JokesVideoService
        svc = JokesVideoService(ai_service=ai_service)
        jokes_data = svc.generate_jokes_ai(theme=data.theme, count=min(data.count, 30))

        created = []
        for jd in jokes_data:
            joke = JokeItem(
                channel_id=ch.id if ch else None,
                user_id=user_id,
                theme=jd.get("theme", data.theme),
                title=jd.get("title", "Piada"),
                text=jd.get("text", ""),
                source="ai",
                status="approved",
            )
            db.add(joke)
            db.flush()
            created.append(_joke_to_dict(joke))

        db.commit()
        return {"jokes": created, "count": len(created)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/jokes/{joke_id}")
def update_joke(
    joke_id: int,
    data: JokeCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user_id = getattr(current_user, "id", None)
    joke = db.query(JokeItem).filter(JokeItem.id == joke_id, JokeItem.user_id == user_id).first()
    if not joke:
        raise HTTPException(status_code=404, detail="Piada não encontrada.")
    joke.theme = data.theme
    joke.title = data.title or joke.title
    joke.text = data.text
    joke.punchline = data.punchline or joke.punchline
    db.commit()
    db.refresh(joke)
    return {"joke": _joke_to_dict(joke)}


@router.delete("/jokes/{joke_id}")
def delete_joke(
    joke_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user_id = getattr(current_user, "id", None)
    joke = db.query(JokeItem).filter(JokeItem.id == joke_id, JokeItem.user_id == user_id).first()
    if not joke:
        raise HTTPException(status_code=404, detail="Piada não encontrada.")
    db.delete(joke)
    db.commit()
    return {"ok": True}


# ─── Episódios ───────────────────────────────────────────────────────────────

@router.get("/episodes")
def list_episodes(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user_id = getattr(current_user, "id", None)
    episodes = (
        db.query(JokesEpisode)
        .filter(JokesEpisode.user_id == user_id)
        .order_by(JokesEpisode.created_at.desc())
        .all()
    )
    return {"episodes": [_episode_to_dict(ep) for ep in episodes]}


@router.post("/episodes")
def create_episode(
    data: EpisodeCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Cria um novo episódio e inicia a geração do vídeo em background."""
    user_id = getattr(current_user, "id", None)
    ch = db.query(JokesChannel).filter(JokesChannel.user_id == user_id).first()

    # Coleta piadas
    jokes_list = []
    if data.joke_ids:
        jokes_objs = (
            db.query(JokeItem)
            .filter(JokeItem.id.in_(data.joke_ids), JokeItem.user_id == user_id)
            .all()
        )
        jokes_list = [{"title": j.title or "", "text": j.text, "theme": j.theme} for j in jokes_objs]
    elif data.generate_jokes:
        # Gera piadas via IA automaticamente
        try:
            ai_service = AIContentGenerator()
            from app.services.jokes_service import JokesVideoService
            svc = JokesVideoService(ai_service=ai_service)
            jokes_data = svc.generate_jokes_ai(theme=data.theme, count=data.joke_count)
            jokes_list = jokes_data
            # Salva no banco
            for jd in jokes_data:
                joke = JokeItem(
                    channel_id=ch.id if ch else None,
                    user_id=user_id,
                    theme=jd.get("theme", data.theme),
                    title=jd.get("title", "Piada"),
                    text=jd.get("text", ""),
                    source="ai",
                    status="used",
                )
                db.add(joke)
            db.flush()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Erro ao gerar piadas: {e}")

    if not jokes_list:
        raise HTTPException(status_code=400, detail="Nenhuma piada selecionada para o episódio.")

    episode = JokesEpisode(
        channel_id=ch.id if ch else None,
        user_id=user_id,
        title=data.title,
        theme=data.theme,
        jokes_json=json.dumps(jokes_list, ensure_ascii=False),
        status="generating",
        progress=0,
    )
    db.add(episode)
    db.commit()
    db.refresh(episode)

    # Dispara geração em background
    background_tasks.add_task(
        _generate_episode_video,
        episode_id=episode.id,
        channel_name=ch.name if ch else "Canal de Piadas",
        avatar_path=ch.avatar_url if ch else None,
        voice_gender=ch.voice_gender if ch else "male",
        jokes_list=jokes_list,
        theme=data.theme,
    )

    return {"episode": _episode_to_dict(episode)}


@router.get("/episodes/{episode_id}")
def get_episode(
    episode_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user_id = getattr(current_user, "id", None)
    ep = db.query(JokesEpisode).filter(
        JokesEpisode.id == episode_id, JokesEpisode.user_id == user_id
    ).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Episódio não encontrado.")
    return {"episode": _episode_to_dict(ep)}


@router.put("/episodes/{episode_id}/approve")
def approve_episode(
    episode_id: int,
    data: EpisodeApprove,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Aprova ou rejeita um episódio após revisão."""
    user_id = getattr(current_user, "id", None)
    ep = db.query(JokesEpisode).filter(
        JokesEpisode.id == episode_id, JokesEpisode.user_id == user_id
    ).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Episódio não encontrado.")
    if ep.status not in ("review", "approved", "failed"):
        raise HTTPException(status_code=400, detail=f"Episódio está em status '{ep.status}', não pode ser revisado agora.")

    ep.status = "approved" if data.approved else "draft"
    ep.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(ep)
    return {"episode": _episode_to_dict(ep)}


@router.post("/episodes/{episode_id}/publish")
def publish_episode(
    episode_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Publica o episódio aprovado (marca como publicado)."""
    user_id = getattr(current_user, "id", None)
    ep = db.query(JokesEpisode).filter(
        JokesEpisode.id == episode_id, JokesEpisode.user_id == user_id
    ).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Episódio não encontrado.")
    if ep.status != "approved":
        raise HTTPException(status_code=400, detail="Episódio precisa ser aprovado antes de publicar.")

    ep.status = "published"
    ep.published_at = datetime.utcnow()
    ep.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(ep)
    return {"episode": _episode_to_dict(ep), "message": "Episódio marcado como publicado!"}


@router.delete("/episodes/{episode_id}")
def delete_episode(
    episode_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user_id = getattr(current_user, "id", None)
    ep = db.query(JokesEpisode).filter(
        JokesEpisode.id == episode_id, JokesEpisode.user_id == user_id
    ).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Episódio não encontrado.")
    db.delete(ep)
    db.commit()
    return {"ok": True}


@router.post("/episodes/{episode_id}/regenerate")
def regenerate_episode(
    episode_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Regera o vídeo de um episódio existente."""
    user_id = getattr(current_user, "id", None)
    ep = db.query(JokesEpisode).filter(
        JokesEpisode.id == episode_id, JokesEpisode.user_id == user_id
    ).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Episódio não encontrado.")

    ch = db.query(JokesChannel).filter(JokesChannel.user_id == user_id).first()
    jokes_list = json.loads(ep.jokes_json) if ep.jokes_json else []

    if not jokes_list:
        raise HTTPException(status_code=400, detail="Sem piadas no episódio para regerar.")

    ep.status = "generating"
    ep.progress = 0
    ep.error_log = None
    ep.updated_at = datetime.utcnow()
    db.commit()

    background_tasks.add_task(
        _generate_episode_video,
        episode_id=ep.id,
        channel_name=ch.name if ch else "Canal de Piadas",
        avatar_path=ch.avatar_url if ch else None,
        voice_gender=ch.voice_gender if ch else "male",
        jokes_list=jokes_list,
        theme=ep.theme,
    )

    return {"episode": _episode_to_dict(ep)}


# ─── Background: Geração de Vídeo ────────────────────────────────────────────

def _generate_episode_video(
    episode_id: int,
    channel_name: str,
    avatar_path: Optional[str],
    voice_gender: str,
    jokes_list: list,
    theme: str,
):
    """Função executada em background para gerar o vídeo do episódio."""
    from app.database import SessionLocal
    db = SessionLocal()

    def _update_progress(pct: int, msg: str = ""):
        try:
            ep = db.query(JokesEpisode).filter(JokesEpisode.id == episode_id).first()
            if ep:
                ep.progress = pct
                if msg:
                    ep.error_log = msg
                db.commit()
        except Exception:
            pass

    try:
        ai_service = AIContentGenerator()
        from app.services.jokes_service import JokesVideoService
        svc = JokesVideoService(ai_service=ai_service)

        result = svc.create_jokes_video(
            episode_id=episode_id,
            channel_name=channel_name,
            jokes=jokes_list,
            avatar_path=avatar_path,
            voice_gender=voice_gender,
            theme=theme,
            on_progress=_update_progress,
        )

        ep = db.query(JokesEpisode).filter(JokesEpisode.id == episode_id).first()
        if ep:
            ep.video_url = result["video_url"]
            ep.duration_sec = result.get("duration_sec", 0)
            ep.status = "review"
            ep.progress = 100
            ep.error_log = None
            ep.updated_at = datetime.utcnow()
            db.commit()
        print(f"[JokesVideo] Episódio {episode_id} gerado: {result['video_url']}")

    except Exception as e:
        import traceback
        err = traceback.format_exc()
        print(f"[JokesVideo] Erro ao gerar episódio {episode_id}: {err}")
        try:
            ep = db.query(JokesEpisode).filter(JokesEpisode.id == episode_id).first()
            if ep:
                ep.status = "failed"
                ep.error_log = str(e)
                ep.updated_at = datetime.utcnow()
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


# ─── Helpers de serialização ─────────────────────────────────────────────────

def _channel_to_dict(ch: JokesChannel) -> dict:
    return {
        "id": ch.id,
        "name": ch.name,
        "description": ch.description,
        "default_theme": ch.default_theme,
        "voice_gender": ch.voice_gender,
        "avatar_url": ch.avatar_url,
        "has_avatar": bool(ch.avatar_url),
        "avatar_prompt": ch.avatar_prompt,
        "background_music": ch.background_music,
        "created_at": ch.created_at.isoformat() if ch.created_at else None,
        "updated_at": ch.updated_at.isoformat() if ch.updated_at else None,
    }


def _joke_to_dict(j: JokeItem) -> dict:
    return {
        "id": j.id,
        "theme": j.theme,
        "title": j.title,
        "text": j.text,
        "punchline": j.punchline,
        "source": j.source,
        "status": j.status,
        "created_at": j.created_at.isoformat() if j.created_at else None,
    }


def _episode_to_dict(ep: JokesEpisode) -> dict:
    jokes = []
    if ep.jokes_json:
        try:
            jokes = json.loads(ep.jokes_json)
        except Exception:
            pass
    return {
        "id": ep.id,
        "title": ep.title,
        "theme": ep.theme,
        "jokes": jokes,
        "joke_count": len(jokes),
        "video_url": ep.video_url,
        "thumbnail_url": ep.thumbnail_url,
        "duration_sec": ep.duration_sec,
        "status": ep.status,
        "progress": ep.progress,
        "error_log": ep.error_log,
        "youtube_video_id": ep.youtube_video_id,
        "published_at": ep.published_at.isoformat() if ep.published_at else None,
        "created_at": ep.created_at.isoformat() if ep.created_at else None,
        "updated_at": ep.updated_at.isoformat() if ep.updated_at else None,
    }
