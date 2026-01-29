"""
Rotas para gerar música a partir de letra e clipe (vídeo) da música.
"""
import os
import uuid
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from app.services.ai_generator import AIContentGenerator
from app.services.video_generator import VideoGenerator
from app.routers.auth import get_current_user
from app.models import User

router = APIRouter(prefix="/music", tags=["music"])


class GenerateMusicRequest(BaseModel):
    lyrics: str
    title: str = "Música"
    genre: str = ""


class GenerateClipRequest(BaseModel):
    lyrics: str
    title: str = "Música"
    music_filename: Optional[str] = None  # Se não enviar, usa o último gerado na sessão (frontend guarda)


@router.post("/generate")
def generate_music_from_lyrics(request: GenerateMusicRequest, user: User = Depends(get_current_user)):
    """
    Gera música instrumental a partir da letra (MusicGen).
    A voz cantada exigiria integração com Suno/Udio; por ora só instrumental.
    """
    if not request.lyrics or not request.lyrics.strip():
        raise HTTPException(status_code=400, detail="Envie a letra da música.")
    try:
        ai = AIContentGenerator()
        music_prompt = ai.lyrics_to_music_prompt(request.lyrics, request.title, request.genre)
        raw_audio = ai.generate_music(music_prompt)
        if not raw_audio:
            raise HTTPException(
                status_code=503,
                detail="Não foi possível gerar a música. Verifique o token do Hugging Face em Configurações (opcional)."
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
            "message": "Música instrumental gerada. Use 'Gerar Clipe' para criar o vídeo."
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
        # Tenta o último arquivo .wav na pasta (gerado recentemente)
        music_dir = "app/static/music"
        if os.path.exists(music_dir):
            wavs = [f for f in os.listdir(music_dir) if f.endswith(".wav") and f.startswith("song_")]
            wavs.sort(key=lambda f: os.path.getmtime(os.path.join(music_dir, f)), reverse=True)
            if wavs:
                music_filename = wavs[0]
    if not music_filename:
        raise HTTPException(
            status_code=400,
            detail="Gere a música primeiro (botão 'Gerar Música') ou informe music_filename."
        )
    music_path = os.path.join("app/static/music", music_filename)
    if not os.path.exists(music_path):
        raise HTTPException(status_code=404, detail="Arquivo de música não encontrado. Gere a música novamente.")
    try:
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
