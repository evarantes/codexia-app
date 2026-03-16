"""
Rotas para gerar música a partir de letra e clipe (vídeo) da música.
Com Suno API: música com voz cantada. Sem Suno: instrumental (MusicGen).
"""
import os
import uuid
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from app.services.suno_service import generate_song_with_vocals
from app.services.ai_generator import AIContentGenerator
from app.routers.auth import get_current_user
from app.models import User

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
        # 1. Tentar Suno (voz cantada) se a chave estiver configurada
        result = generate_song_with_vocals(
            lyrics=request.lyrics.strip(),
            title=request.title or "Música",
            style=request.genre or "Pop",
            vocal_gender=request.vocal_gender,
        )
        if result.get("success") and result.get("music_url"):
            return {
                "music_url": result["music_url"],
                "music_filename": result["music_filename"],
                "message": "Música com voz cantada gerada (Suno). Use 'Gerar Clipe' para criar o vídeo.",
                "with_vocals": True,
            }
        if result.get("success") is False and "não configurada" not in (result.get("error") or "").lower():
            raise HTTPException(status_code=503, detail=result.get("error", "Suno falhou."))

        # 2. Fallback: instrumental (MusicGen)
        ai = AIContentGenerator()
        music_prompt = ai.lyrics_to_music_prompt(request.lyrics, request.title, request.genre)
        raw_audio = ai.generate_music(music_prompt)
        if not raw_audio:
            raise HTTPException(
                status_code=503,
                detail="Não foi possível gerar a música. Configure a Suno API Key em Configurações para voz cantada, ou o token Hugging Face para instrumental."
            )
        music_dir = "app/static/music"
        os.makedirs(music_dir, exist_ok=True)
        filename = f"song_{uuid.uuid4().hex[:10]}.wav"
        path = os.path.join(music_dir, filename)
        with open(path, "wb") as f:
            f.write(raw_audio)
        return {
            "music_url": f"/static/music/{filename}",
            "music_filename": filename,
            "message": "Música instrumental gerada. Para voz cantada, configure a Suno API Key em Configurações.",
            "with_vocals": False,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar música: {str(e)}")


@router.post("/clip")
def generate_music_clip(request: GenerateClipRequest, user: User = Depends(get_current_user)):
    """
    Gera clipe (vídeo) da música: cenas baseadas na letra + áudio da música gerada.
    """
    if not request.lyrics or not request.lyrics.strip():
        raise HTTPException(status_code=400, detail="Envie a letra da música.")
    music_filename = request.music_filename
    if not music_filename:
        music_dir = "app/static/music"
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
    music_path = os.path.join("app/static/music", music_filename)
    if not os.path.exists(music_path):
        raise HTTPException(status_code=404, detail="Arquivo de música não encontrado. Gere a música novamente.")
    try:
        from app.services.video_generator import VideoGenerator
        ai = AIContentGenerator()
        video_gen = VideoGenerator(ai_service=ai)
        scenes = ai.lyrics_to_clip_scenes(request.lyrics, request.title)
        result = video_gen.create_music_video(music_path, scenes, title=request.title, aspect_ratio="9:16")
        return {
            "video_url": result["video_url"],
            "message": "Clipe gerado com sucesso."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar clipe: {str(e)}")
