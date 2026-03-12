import json
import os
import uuid
import time
import asyncio
import threading
from datetime import datetime

from app.services.ai_generator import AIContentGenerator
from app.config import VIDEO_OUTPUT_DIR, VIDEO_URL_PREFIX


JOKE_THEMES_DEFAULT = [
    {"name": "Português", "description": "Piadas clássicas de português", "icon": "fa-flag"},
    {"name": "Religiosa", "description": "Piadas religiosas leves e respeitosas", "icon": "fa-church"},
    {"name": "Gospel", "description": "Humor gospel saudável", "icon": "fa-bible"},
    {"name": "Trocadilho", "description": "Piadas de trocadilho e jogo de palavras", "icon": "fa-quote-left"},
    {"name": "Animais", "description": "Piadas com animais engraçados", "icon": "fa-dog"},
    {"name": "Escola", "description": "Piadas de escola e professores", "icon": "fa-graduation-cap"},
    {"name": "Família", "description": "Piadas de família e dia a dia", "icon": "fa-home"},
    {"name": "Profissões", "description": "Piadas sobre profissões diversas", "icon": "fa-briefcase"},
    {"name": "Médico", "description": "Piadas de consultório médico", "icon": "fa-stethoscope"},
    {"name": "Caipira", "description": "Piadas caipiras e do interior", "icon": "fa-tractor"},
    {"name": "Loira", "description": "Piadas clássicas de loira (sem ofensas)", "icon": "fa-user"},
    {"name": "Criança", "description": "Piadas inocentes de crianças", "icon": "fa-child"},
    {"name": "Casamento", "description": "Piadas sobre casamento e relacionamentos", "icon": "fa-ring"},
    {"name": "Advogado", "description": "Piadas sobre advogados e tribunais", "icon": "fa-gavel"},
    {"name": "Variado", "description": "Piadas de temas variados e mistos", "icon": "fa-random"},
]


class JokesChannelService:
    def __init__(self):
        self.ai_service = AIContentGenerator()
        self.output_dir = VIDEO_OUTPUT_DIR
        os.makedirs(self.output_dir, exist_ok=True)

    def _reload_ai(self):
        self.ai_service = AIContentGenerator()

    async def generate_jokes(self, theme: str, quantity: int = 10, existing_jokes: list = None) -> list:
        """Gera piadas exclusivas via IA com base no tema, evitando repetições."""
        self._reload_ai()

        avoid_text = ""
        if existing_jokes:
            samples = existing_jokes[:5]
            avoid_text = "\n".join([f"- {j}" for j in samples])
            avoid_text = f"\n\nEVITE piadas parecidas com estas já existentes:\n{avoid_text}"

        prompt = f"""Você é um comediante brasileiro especialista em piadas curtas, limpas e engraçadas.
Gere {quantity} piadas CURTAS e ENGRAÇADAS sobre o tema: "{theme}".

REGRAS OBRIGATÓRIAS:
- Piadas 100% LIMPAS, sem baixaria, sem palavrões, sem duplo sentido vulgar
- Cada piada deve ter entre 2 e 6 linhas no máximo
- Devem ser rápidas, com uma punchline clara
- Formato: pergunta-resposta OU narrativa curta com desfecho engraçado
- Variadas entre si (não repetir estrutura)
- Em português brasileiro coloquial e natural
{avoid_text}

Retorne EXCLUSIVAMENTE um JSON válido no formato:
[
    {{"title": "título curto opcional", "content": "texto completo da piada"}},
    ...
]"""

        response = self.ai_service._generate_text(prompt, json_mode=True)
        try:
            response = response.replace("```json", "").replace("```", "").strip()
            jokes = json.loads(response)
            if isinstance(jokes, dict) and "jokes" in jokes:
                jokes = jokes["jokes"]
            if not isinstance(jokes, list):
                jokes = [jokes]
            return jokes
        except json.JSONDecodeError:
            return [{"title": "Piada Gerada", "content": response}]

    async def generate_avatar_image(self, description: str = None) -> dict:
        """Gera imagem do avatar comediante usando DALL-E."""
        self._reload_ai()

        prompt_desc = description or "um comediante brasileiro simpático e carismático"
        image_prompt = (
            f"Personagem cartoon 3D estilo Pixar: {prompt_desc}. "
            "Fundo sólido colorido vibrante, expressão alegre e acolhedora, "
            "vestido casualmente com camisa colorida. "
            "Estilo limpo, profissional, adequado para canal de humor familiar no YouTube. "
            "Sem texto na imagem."
        )

        try:
            urls = self.ai_service.generate_cover_options(
                title="Avatar Comediante",
                context=image_prompt,
                n=1
            )
            if urls:
                return {"image_url": urls[0], "prompt_used": image_prompt}
        except Exception as e:
            print(f"Erro ao gerar avatar: {e}")

        return {"image_url": None, "prompt_used": image_prompt, "error": "Falha ao gerar imagem do avatar."}

    def _generate_tts_for_joke(self, text: str, voice_gender: str = "male") -> str | None:
        """Gera áudio TTS para uma piada."""
        if not text or not text.strip():
            return None

        from app.services.video_generator import VideoGenerator
        vg = VideoGenerator(output_dir=self.output_dir, ai_service=self.ai_service)
        return vg.generate_audio(text, lang='pt', voice_style='human', voice_gender=voice_gender)

    def _get_audio_duration(self, audio_path: str) -> float:
        """Obtém duração do áudio em segundos."""
        try:
            from mutagen.mp3 import MP3
            audio = MP3(audio_path)
            return audio.info.length
        except Exception:
            pass
        try:
            from moviepy.editor import AudioFileClip
            clip = AudioFileClip(audio_path)
            dur = clip.duration
            clip.close()
            return dur
        except Exception:
            return 5.0

    def generate_compilation_video(
        self,
        jokes: list,
        avatar_image_path: str | None,
        title: str = "Piadas do Dia",
        voice_gender: str = "male",
        progress_callback=None
    ) -> dict:
        """
        Gera vídeo compilação de piadas com avatar.
        Retorna dict com video_url, duration_sec, etc.
        """
        from PIL import Image, ImageDraw, ImageFont
        import textwrap

        try:
            from moviepy.editor import (
                ImageClip, AudioFileClip, CompositeVideoClip,
                concatenate_videoclips, ColorClip, TextClip
            )
        except ImportError:
            return {"error": "MoviePy não instalado. Execute: pip install moviepy"}

        video_id = str(uuid.uuid4())[:8]
        temp_clips = []
        audio_files = []
        total = len(jokes)

        VINHETA_DUR = 1.5
        PAUSE_DUR = 0.8
        VIDEO_W, VIDEO_H = 1920, 1080
        FPS = 24
        BG_COLOR = (25, 25, 60)

        def _update_progress(pct):
            if progress_callback:
                try:
                    progress_callback(pct)
                except Exception:
                    pass

        _update_progress(5)

        avatar_clip = None
        if avatar_image_path and os.path.exists(avatar_image_path):
            try:
                avatar_img = Image.open(avatar_image_path).convert("RGBA")
                avatar_size = 300
                avatar_img = avatar_img.resize((avatar_size, avatar_size), Image.LANCZOS)
                avatar_tmp = os.path.join(self.output_dir, f"avatar_{video_id}.png")
                avatar_img.save(avatar_tmp)
                avatar_clip = ImageClip(avatar_tmp).resize(height=avatar_size)
            except Exception as e:
                print(f"Erro ao carregar avatar: {e}")

        def create_joke_frame(joke_text: str, joke_num: int, total_num: int) -> str:
            """Cria frame visual com texto da piada e fundo."""
            img = Image.new('RGB', (VIDEO_W, VIDEO_H), color=BG_COLOR)
            d = ImageDraw.Draw(img)

            try:
                font_candidates = [
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                    "DejaVuSans-Bold.ttf", "arial.ttf",
                ]
                font = None
                for fp in font_candidates:
                    if os.path.exists(fp):
                        font = ImageFont.truetype(fp, 42)
                        break
                if not font:
                    font = ImageFont.load_default()
            except Exception:
                font = ImageFont.load_default()

            try:
                font_small = ImageFont.truetype(font.path, 28)
            except Exception:
                font_small = font

            header = f"Piada {joke_num}/{total_num}"
            d.text((VIDEO_W // 2, 50), header, fill=(255, 215, 0), font=font_small, anchor="mt")

            text_x = 450 if avatar_clip else 100
            text_max_w = VIDEO_W - text_x - 80
            chars_per_line = max(30, int(text_max_w / 24))
            wrapped = textwrap.fill(joke_text, width=chars_per_line)
            lines = wrapped.split('\n')

            total_text_h = len(lines) * 55
            start_y = max(150, (VIDEO_H - total_text_h) // 2)

            for i, line in enumerate(lines):
                y = start_y + i * 55
                d.text((text_x, y), line, fill=(255, 255, 255), font=font)

            frame_path = os.path.join(self.output_dir, f"joke_frame_{video_id}_{joke_num}.png")
            img.save(frame_path)
            return frame_path

        def create_intro_frame() -> str:
            img = Image.new('RGB', (VIDEO_W, VIDEO_H), color=(20, 20, 80))
            d = ImageDraw.Draw(img)
            try:
                font_candidates = [
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                ]
                font = None
                for fp in font_candidates:
                    if os.path.exists(fp):
                        font = ImageFont.truetype(fp, 64)
                        break
                if not font:
                    font = ImageFont.load_default()
            except Exception:
                font = ImageFont.load_default()

            d.text((VIDEO_W // 2, VIDEO_H // 2 - 50), title, fill=(255, 215, 0), font=font, anchor="mm")

            try:
                font_sub = ImageFont.truetype(font.path, 36)
            except Exception:
                font_sub = font
            d.text((VIDEO_W // 2, VIDEO_H // 2 + 40), "Prepare-se para rir!", fill=(200, 200, 255), font=font_sub, anchor="mm")

            path = os.path.join(self.output_dir, f"intro_{video_id}.png")
            img.save(path)
            return path

        try:
            _update_progress(10)

            intro_path = create_intro_frame()
            intro_clip = ImageClip(intro_path).set_duration(3.0)
            temp_clips.append(intro_clip)

            for idx, joke_data in enumerate(jokes):
                joke_text = joke_data if isinstance(joke_data, str) else joke_data.get("content", str(joke_data))
                joke_num = idx + 1
                pct = 10 + int((idx / total) * 70)
                _update_progress(pct)

                print(f"Processando piada {joke_num}/{total}: {joke_text[:40]}...")

                audio_path = self._generate_tts_for_joke(joke_text, voice_gender)
                if audio_path:
                    audio_files.append(audio_path)
                    duration = self._get_audio_duration(audio_path) + PAUSE_DUR
                else:
                    duration = max(5.0, len(joke_text) * 0.07) + PAUSE_DUR

                frame_path = create_joke_frame(joke_text, joke_num, total)

                img_clip = ImageClip(frame_path).set_duration(duration)

                if avatar_clip:
                    av = avatar_clip.set_duration(duration).set_position((60, VIDEO_H // 2 - 150))
                    scene = CompositeVideoClip([img_clip, av], size=(VIDEO_W, VIDEO_H))
                else:
                    scene = img_clip

                if audio_path:
                    try:
                        a_clip = AudioFileClip(audio_path)
                        scene = scene.set_audio(a_clip)
                    except Exception as e:
                        print(f"Erro ao anexar áudio da piada {joke_num}: {e}")

                vinheta = ColorClip(
                    size=(VIDEO_W, VIDEO_H),
                    color=BG_COLOR,
                    duration=VINHETA_DUR
                )
                temp_clips.extend([scene, vinheta])

            _update_progress(85)

            final = concatenate_videoclips(temp_clips, method="compose")
            output_filename = f"piadas_{video_id}_{int(time.time())}.mp4"
            output_path = os.path.join(self.output_dir, output_filename)

            final.write_videofile(
                output_path,
                fps=FPS,
                codec="libx264",
                audio_codec="aac",
                threads=2,
                preset="ultrafast",
                logger=None
            )

            actual_duration = final.duration

            final.close()
            for c in temp_clips:
                try:
                    c.close()
                except Exception:
                    pass

            _update_progress(95)

            for f in audio_files:
                try:
                    os.remove(f)
                except Exception:
                    pass

            import glob as g
            for pattern in [f"joke_frame_{video_id}_*", f"intro_{video_id}*", f"avatar_{video_id}*"]:
                for f in g.glob(os.path.join(self.output_dir, pattern)):
                    try:
                        os.remove(f)
                    except Exception:
                        pass

            video_url = f"{VIDEO_URL_PREFIX}/{output_filename}"
            _update_progress(100)

            return {
                "video_url": video_url,
                "video_path": output_path,
                "duration_sec": actual_duration,
                "total_jokes": total,
                "filename": output_filename
            }

        except Exception as e:
            print(f"Erro ao gerar vídeo de compilação: {e}")
            import traceback
            traceback.print_exc()
            for c in temp_clips:
                try:
                    c.close()
                except Exception:
                    pass
            return {"error": str(e)}

    async def generate_compilation_title(self, theme: str) -> dict:
        """Gera título e descrição para a compilação de piadas."""
        self._reload_ai()
        prompt = f"""Crie um título criativo e uma descrição para um vídeo de compilação de piadas no YouTube.
Tema das piadas: {theme}

REGRAS:
- O título deve ser chamativo, com emojis, máximo 80 caracteres
- A descrição deve ter 2-3 linhas, convidativa, com CTA para se inscrever
- Adicione 5-8 hashtags relevantes

Retorne EXCLUSIVAMENTE JSON:
{{"title": "...", "description": "...", "tags": "..."}}"""

        response = self.ai_service._generate_text(prompt, json_mode=True)
        try:
            response = response.replace("```json", "").replace("```", "").strip()
            return json.loads(response)
        except json.JSONDecodeError:
            return {
                "title": f"Melhores Piadas de {theme} - Rir é o Melhor Remédio! 😂",
                "description": f"As melhores piadas de {theme}! Inscreva-se para mais vídeos de humor!",
                "tags": f"piadas,humor,{theme.lower()},comedia,engraçado"
            }
