"""
Rotas para gerar música a partir de letra e clipe (vídeo) da música.
Com Suno API: música com voz cantada. Sem Suno: instrumental (MusicGen).
"""
import os
import uuid
import threading
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from app.database import get_db
from app.services.suno_service import (
    get_suno_api_key,
    create_suno_task,
    poll_suno_task,
    download_suno_audio,
)
from app.services.ai_generator import AIContentGenerator
from app.routers.auth import get_current_user
from app.models import User, VideoTask, SavedMusic
from app.services.task_manager import create_task, update_task, get_task
from app.config import MUSIC_OUTPUT_DIR, MUSIC_URL_PREFIX, absolute_path_for_music

router = APIRouter(prefix="/music", tags=["music"])


class GenerateMusicRequest(BaseModel):
    lyrics: str
    title: str = "Música"
    genre: str = ""
    vocal_gender: Optional[str] = None  # "m" ou "f" para Suno


class GenerateClipRequest(BaseModel):
    lyrics: str
    title: str = "Música"
    music_filename: Optional[str] = None


class GenerateLyricsRequest(BaseModel):
    theme: str
    message: str
    language: str = "pt-BR"
    style: str = ""
    genre: str = ""


class SaveMusicRequest(BaseModel):
    title: str = "Música"
    lyrics: Optional[str] = None
    genre: Optional[str] = None
    vocal_gender: Optional[str] = None
    with_vocals: Optional[bool] = None
    music_url: Optional[str] = None
    music_filename: Optional[str] = None
    clip_url: Optional[str] = None
    clip_filename: Optional[str] = None


@router.post("/lyrics")
def generate_lyrics(request: GenerateLyricsRequest, user: User = Depends(get_current_user)):
    if not request.theme or not request.theme.strip():
        raise HTTPException(status_code=400, detail="Informe o tema.")
    if not request.message or not request.message.strip():
        raise HTTPException(status_code=400, detail="Informe a mensagem.")
    try:
        ai = AIContentGenerator()
        result = ai.generate_song_lyrics(
            theme=request.theme.strip(),
            message=request.message.strip(),
            language=(request.language or "pt-BR").strip(),
            style=(request.style or "").strip(),
            genre=(request.genre or "").strip(),
        )
        if not result or not isinstance(result, dict) or not result.get("lyrics"):
            raise HTTPException(status_code=503, detail="Não foi possível gerar a letra agora.")
        return {
            "title": result.get("title") or "Música",
            "lyrics": result.get("lyrics"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar letra: {str(e)}")


@router.post("/generate")
def generate_music_from_lyrics(request: GenerateMusicRequest, user: User = Depends(get_current_user)):
    """
    Gera música a partir da letra. Se Suno API Key estiver em Configurações: música com voz cantada.
    Senão: instrumental (MusicGen / Hugging Face).
    """
    if not request.lyrics or not request.lyrics.strip():
        raise HTTPException(status_code=400, detail="Envie a letra da música.")
    try:
        api_key = get_suno_api_key()
        if api_key:
            task_id = create_task(user_id=user.id)
            update_task(
                task_id,
                status="processing",
                progress=5,
                message="Enviando para o Suno...",
                result={"provider": "suno", "title": request.title or "Música"},
            )

            lyrics = request.lyrics.strip()
            title = request.title or "Música"
            style = request.genre or "Pop"
            vocal_gender = request.vocal_gender

            created = create_suno_task(
                api_key=api_key,
                lyrics=lyrics,
                title=title,
                style=style,
                vocal_gender=vocal_gender,
            )
            if not created.get("success"):
                update_task(task_id, status="failed", progress=100, message=created.get("error") or "Suno falhou.")
                raise HTTPException(status_code=503, detail=created.get("error", "Suno falhou."))
            suno_task_id = str(created.get("task_id") or "")
            update_task(
                task_id,
                status="processing",
                progress=12,
                message="Suno processando...",
                result={"provider": "suno", "suno_task_id": suno_task_id, "title": title},
            )

            def _run():
                try:
                    update_task(task_id, status="processing", progress=20, message="Aguardando áudio do Suno...")
                    polled = poll_suno_task(api_key=api_key, task_id=suno_task_id, max_wait_seconds=12 * 60, step_seconds=6)
                    if not polled.get("success"):
                        update_task(task_id, status="failed", progress=100, message=polled.get("error") or "Suno falhou.")
                        return
                    audio_url = str(polled.get("audio_url") or "")
                    if not audio_url:
                        update_task(task_id, status="failed", progress=100, message="Suno não retornou URL do áudio.")
                        return
                    update_task(task_id, status="processing", progress=85, message="Baixando áudio...")
                    dl = download_suno_audio(audio_url)
                    if not dl.get("success"):
                        update_task(task_id, status="failed", progress=100, message=dl.get("error") or "Falha ao baixar áudio.")
                        return
                    update_task(
                        task_id,
                        status="completed",
                        progress=100,
                        message="Música com voz cantada gerada (Suno).",
                        result={
                            "provider": "suno",
                            "suno_task_id": suno_task_id,
                            "audio_url": audio_url,
                            "music_url": dl.get("music_url"),
                            "music_filename": dl.get("music_filename"),
                            "with_vocals": True,
                            "title": title,
                        },
                    )
                except Exception as e:
                    update_task(task_id, status="failed", progress=100, message=str(e))

            threading.Thread(target=_run, daemon=True).start()
            return {
                "task_id": task_id,
                "message": "Geração iniciada (Suno). Aguarde a finalização.",
                "with_vocals": True,
            }

        # 2. Fallback: instrumental (MusicGen)
        ai = AIContentGenerator()
        music_prompt = ai.lyrics_to_music_prompt(request.lyrics, request.title, request.genre)
        raw_audio = ai.generate_music(music_prompt)
        if not raw_audio:
            raise HTTPException(
                status_code=503,
                detail="Não foi possível gerar a música. Configure a Suno API Key em Configurações para voz cantada, ou o token Hugging Face para instrumental."
            )
        music_dir = MUSIC_OUTPUT_DIR
        os.makedirs(music_dir, exist_ok=True)
        filename = f"song_{uuid.uuid4().hex[:10]}.wav"
        path = os.path.join(music_dir, filename)
        with open(path, "wb") as f:
            f.write(raw_audio)
        return {
            "music_url": f"{MUSIC_URL_PREFIX}/{filename}",
            "music_filename": filename,
            "message": "Música instrumental gerada. Para voz cantada, configure a Suno API Key em Configurações.",
            "with_vocals": False,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar música: {str(e)}")

@router.get("/task/{task_id}")
def get_music_task(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada.")
    if row.user_id and row.user_id != user.id and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Sem permissão para acessar esta tarefa.")
    data = get_task(task_id)
    if not data:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada.")
    return data


@router.post("/saved")
def save_music_item(request: SaveMusicRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    has_any = bool((request.lyrics and request.lyrics.strip()) or (request.music_url and request.music_url.strip()) or (request.clip_url and request.clip_url.strip()))
    if not has_any:
        raise HTTPException(status_code=400, detail="Informe ao menos a letra, a música ou o clipe para salvar.")
    item = SavedMusic(
        user_id=user.id,
        title=(request.title or "Música")[:200],
        lyrics=(request.lyrics.strip() if request.lyrics and request.lyrics.strip() else None),
        genre=(request.genre.strip() if request.genre and request.genre.strip() else None),
        vocal_gender=(request.vocal_gender.strip() if request.vocal_gender and request.vocal_gender.strip() else None),
        with_vocals=bool(request.with_vocals) if request.with_vocals is not None else False,
        music_url=(request.music_url.strip() if request.music_url and request.music_url.strip() else None),
        music_filename=(request.music_filename.strip() if request.music_filename and request.music_filename.strip() else None),
        clip_url=(request.clip_url.strip() if request.clip_url and request.clip_url.strip() else None),
        clip_filename=(request.clip_filename.strip() if request.clip_filename and request.clip_filename.strip() else None),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {
        "id": item.id,
        "title": item.title,
        "lyrics": item.lyrics,
        "genre": item.genre,
        "vocal_gender": item.vocal_gender,
        "with_vocals": item.with_vocals,
        "music_url": item.music_url,
        "music_filename": item.music_filename,
        "clip_url": item.clip_url,
        "clip_filename": item.clip_filename,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


@router.get("/saved")
def list_saved_music(limit: int = 50, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        lim = int(limit)
    except Exception:
        lim = 50
    lim = max(1, min(lim, 200))
    q = db.query(SavedMusic).filter(SavedMusic.user_id == user.id).order_by(SavedMusic.created_at.desc()).limit(lim)
    items = q.all()
    return {
        "items": [
            {
                "id": i.id,
                "title": i.title,
                "lyrics": i.lyrics,
                "genre": i.genre,
                "vocal_gender": i.vocal_gender,
                "with_vocals": i.with_vocals,
                "music_url": i.music_url,
                "music_filename": i.music_filename,
                "clip_url": i.clip_url,
                "clip_filename": i.clip_filename,
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in items
        ]
    }


@router.get("/saved/{item_id}")
def get_saved_music(item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.query(SavedMusic).filter(SavedMusic.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    if item.user_id and item.user_id != user.id and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Sem permissão para acessar este item.")
    return {
        "id": item.id,
        "title": item.title,
        "lyrics": item.lyrics,
        "genre": item.genre,
        "vocal_gender": item.vocal_gender,
        "with_vocals": item.with_vocals,
        "music_url": item.music_url,
        "music_filename": item.music_filename,
        "clip_url": item.clip_url,
        "clip_filename": item.clip_filename,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


@router.delete("/saved/{item_id}")
def delete_saved_music(item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.query(SavedMusic).filter(SavedMusic.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    if item.user_id and item.user_id != user.id and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Sem permissão para excluir este item.")
    db.delete(item)
    db.commit()
    return {"success": True}


@router.post("/clip")
def generate_music_clip(request: GenerateClipRequest, user: User = Depends(get_current_user)):
    """
    Gera clipe (vídeo) da música: cenas baseadas na letra + áudio da música gerada.
    """
    if not request.lyrics or not request.lyrics.strip():
        raise HTTPException(status_code=400, detail="Envie a letra da música.")
    music_filename = request.music_filename
    if not music_filename:
        music_dir = MUSIC_OUTPUT_DIR
        if os.path.exists(music_dir):
            songs = [f for f in os.listdir(music_dir) if f.startswith("song_") and (f.endswith(".wav") or f.endswith(".mp3"))]
            songs.sort(key=lambda f: os.path.getmtime(os.path.join(music_dir, f)), reverse=True)
            if songs:
                music_filename = songs[0]
    if not music_filename:
        raise HTTPException(
            status_code=400,
            detail="Gere a música primeiro (botão 'Gerar Música') ou informe music_filename."
        )
    music_path = absolute_path_for_music(music_filename)
    if not os.path.exists(music_path):
        raise HTTPException(status_code=404, detail="Arquivo de música não encontrado. Gere a música novamente.")
    try:
        from app.services.video_generator import VideoGenerator
        ai = AIContentGenerator()
        video_gen = VideoGenerator(ai_service=ai)
        result = video_gen.create_music_video(music_path, title=request.title, aspect_ratio="9:16", lyrics=request.lyrics)
        return {
            "video_url": result["video_url"],
            "message": "Clipe gerado com sucesso."
        }
    except Exception as e:
        msg = str(e)
        if "Sincronização perfeita requer transcrição do áudio" in msg:
            raise HTTPException(status_code=400, detail=msg)
        raise HTTPException(status_code=500, detail=f"Erro ao gerar clipe: {msg}")
