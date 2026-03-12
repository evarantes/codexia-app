"""
Fábrica de vídeos do Canal de Piadas.
- Avatar fixo em todas as cenas (imagem estática)
- TTS por piada
- Render: avatar + áudio sincronizado
- Futuro: lip sync (SadTalker/Wav2Lip) quando use_lip_sync=True
"""
import os
import json
import threading
from pathlib import Path
from sqlalchemy.orm import Session
from app.models import JokesChannel, JokesVideo, Joke
from app.services.video_generator import VideoGenerator
from app.services.ai_generator import AIContentGenerator
from app.services.storage import StorageService
from app.config import VIDEO_OUTPUT_DIR


def _clip_with_duration(clip, duration):
    if hasattr(clip, "with_duration"):
        return clip.with_duration(duration)
    return clip.set_duration(duration)


def _clip_with_audio(clip, audio_clip):
    if hasattr(clip, "with_audio"):
        return clip.with_audio(audio_clip)
    return clip.set_audio(audio_clip)


def _clip_resize(clip, size):
    if hasattr(clip, "resized"):
        return clip.resized(size)
    return clip.resize(size)


class JokesFactory:
    def __init__(self, db: Session):
        self.db = db
        self.ai = AIContentGenerator()
        self.video_gen = VideoGenerator(output_dir=VIDEO_OUTPUT_DIR, ai_service=self.ai)
        self.storage = StorageService()
        os.makedirs(VIDEO_OUTPUT_DIR, exist_ok=True)

    def _get_avatar_path(self, channel: JokesChannel) -> str:
        """Retorna path da imagem do avatar ou gera fallback."""
        if channel.avatar_image_path and os.path.exists(channel.avatar_image_path):
            return channel.avatar_image_path
        # Fallback: imagem com texto "Avatar" (placeholder até o usuário fazer upload)
        from PIL import Image
        import numpy as np
        size = (1280, 720)
        img_array = self.video_gen.create_text_image(
            "Avatar",
            size=size,
            bg_color=(40, 44, 52),
            text_color=(200, 200, 200),
        )
        fallback_path = os.path.join(VIDEO_OUTPUT_DIR, "jokes_avatar_fallback.png")
        Image.fromarray(img_array).save(fallback_path)
        return fallback_path

    def generate_video(self, video: JokesVideo, progress_callback=None) -> str:
        """
        Gera o vídeo completo: avatar fixo + TTS por piada.
        Retorna o path do vídeo gerado.
        """
        channel = video.channel
        jokes = self.db.query(Joke).filter(Joke.video_id == video.id).order_by(Joke.idx).all()
        if not jokes:
            raise ValueError("Nenhuma piada no vídeo.")

        avatar_path = self._get_avatar_path(channel)
        voice_style = channel.voice_style or "human"
        voice_gender = channel.voice_gender or "male"

        try:
            from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
        except ImportError:
            from moviepy import ImageClip, AudioFileClip, concatenate_videoclips

        clips = []
        total = len(jokes)

        for idx, joke in enumerate(jokes, start=1):
            if progress_callback:
                progress_callback(int(30 + (idx / total) * 50), f"Gerando áudio da piada {idx}/{total}...")

            text = (joke.setup or "") + " " + (joke.punchline or "")
            text = text.strip()
            if not text:
                continue

            audio_path = self.video_gen.generate_audio(
                text,
                voice_style=voice_style,
                voice_gender=voice_gender,
            )
            if not audio_path or not os.path.exists(audio_path):
                continue

            audio_clip = AudioFileClip(audio_path)
            duration = audio_clip.duration

            if progress_callback:
                progress_callback(int(30 + (idx / total) * 50), f"Montando cena {idx}/{total}...")

            img_clip = ImageClip(avatar_path)
            img_clip = _clip_resize(img_clip, (1280, 720))
            img_clip = _clip_with_duration(img_clip, duration)
            img_clip = _clip_with_audio(img_clip, audio_clip)
            clips.append(img_clip)

        if not clips:
            raise ValueError("Nenhum clip gerado.")

        if progress_callback:
            progress_callback(85, "Renderizando vídeo final...")

        final_video = concatenate_videoclips(clips, method="compose")
        output_filename = f"jokes_{video.id}.mp4"
        output_path = os.path.join(VIDEO_OUTPUT_DIR, output_filename)

        try:
            final_video.write_videofile(
                output_path,
                fps=24,
                codec="libx264",
                audio_codec="aac",
                threads=4,
                ffmpeg_params=["-preset", "ultrafast", "-crf", "28"],
            )
        finally:
            try:
                final_video.close()
            except Exception:
                pass
            for c in clips:
                try:
                    if getattr(c, "audio", None):
                        c.audio.close()
                    c.close()
                except Exception:
                    pass

        return output_path
