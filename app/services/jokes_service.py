"""
Serviço de geração de piadas e vídeos para o Canal de Piadas.
Cria vídeos de 10+ minutos com avatar animado narrando piadas curtas.
"""
import os
import uuid
import json
import time
import threading
import asyncio
import textwrap
import math
from typing import List, Optional, Tuple

from app.config import VIDEO_OUTPUT_DIR, VIDEO_URL_PREFIX, STATIC_DIR


THEME_LABELS = {
    "geral": "Piadas em Geral",
    "portugues": "Piadas com Português",
    "religioso": "Piadas Religiosas",
    "gospel": "Piadas Gospel",
    "crianca": "Piadas Infantis",
    "animais": "Piadas de Animais",
    "trabalho": "Piadas de Trabalho",
    "escola": "Piadas de Escola",
    "casal": "Piadas de Casal",
    "politico": "Piadas Políticas",
}

THEME_PROMPTS = {
    "geral": "piadas engraçadas e leves adequadas para toda a família",
    "portugues": "piadas clássicas com português como personagem principal (o famoso português ingênuo), no estilo humor brasileiro",
    "religioso": "piadas religiosas respeitosas e leves, sem ofender, adequadas para ambiente familiar",
    "gospel": "piadas cristãs e gospel, edificantes e engraçadas, adequadas para comunidade cristã",
    "crianca": "piadas infantis simples e inocentes para crianças de até 12 anos",
    "animais": "piadas engraçadas com animais como personagens principais",
    "trabalho": "piadas sobre situações cotidianas do trabalho e escritório",
    "escola": "piadas sobre alunos, professores e situações escolares",
    "casal": "piadas leves e engraçadas sobre casamento e relacionamentos, sem baixaria",
    "politico": "piadas sobre política e situações governamentais, no estilo humor saudável",
}

AVATAR_PROMPT = (
    "A friendly cartoon comedian avatar character for a jokes channel. "
    "The character is a cheerful Brazilian man in his 40s, wearing a colorful casual shirt, "
    "with an expressive face and a big smile showing teeth, microphone in hand, "
    "standing in a slight performance pose. Style: flat illustration, vibrant colors, "
    "clean lines, white background, bust/portrait format, no text."
)


class JokesVideoService:
    def __init__(self, ai_service=None):
        self.ai_service = ai_service
        self.output_dir = VIDEO_OUTPUT_DIR
        os.makedirs(self.output_dir, exist_ok=True)
        self._avatars_dir = os.path.join(str(STATIC_DIR), "jokes_avatars")
        os.makedirs(self._avatars_dir, exist_ok=True)

    # ─── IA: Geração de Piadas ────────────────────────────────────────────────

    def generate_jokes_ai(self, theme: str, count: int = 20) -> List[dict]:
        """Gera `count` piadas no tema especificado via IA."""
        theme_desc = THEME_PROMPTS.get(theme, THEME_PROMPTS["geral"])
        prompt = (
            f"Gere exatamente {count} piadas originais e engraçadas sobre {theme_desc}. "
            "As piadas devem ser curtas (máximo 6 linhas cada), limpas, sem conteúdo adulto ou ofensivo, "
            "e adequadas para vídeo no YouTube. Cada piada deve ter uma configuração (setup) e um golpe final (punchline) claro. "
            "Retorne SOMENTE um JSON com uma lista de objetos, cada um com os campos: "
            '"title" (título curto da piada), "text" (a piada completa incluindo setup e punchline). '
            "Exemplo: [{\"title\": \"O Médico\", \"text\": \"Por que o médico foi ao banco?...\\nPara checar a pressão!\"}, ...]"
            f"\n\nGere as {count} piadas agora:"
        )
        system = (
            "Você é um roteirista de humor brasileiro especialista em piadas limpas e engraçadas para YouTube. "
            "Retorne APENAS JSON válido sem markdown, sem explicações."
        )
        try:
            raw = self.ai_service._generate_text(prompt, system_prompt=system, temperature=0.9, json_mode=False)
            # Extrai JSON da resposta
            raw = raw.strip()
            start = raw.find("[")
            end = raw.rfind("]") + 1
            if start >= 0 and end > start:
                raw = raw[start:end]
            jokes_data = json.loads(raw)
            result = []
            for item in jokes_data:
                if isinstance(item, dict) and "text" in item:
                    result.append({
                        "title": item.get("title", "Piada"),
                        "text": item.get("text", "").strip(),
                        "theme": theme,
                        "source": "ai",
                    })
            return result
        except Exception as e:
            print(f"[JokesService] Erro ao gerar piadas via IA: {e}")
            return []

    # ─── Avatar ───────────────────────────────────────────────────────────────

    def generate_avatar(self, custom_prompt: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """
        Gera imagem do avatar via DALL-E e retorna (url_relativa, base64_string).
        Salva o arquivo em app/static/jokes_avatars/.
        """
        prompt = custom_prompt or AVATAR_PROMPT
        try:
            image_data = self.ai_service.generate_image(prompt, size="1024x1024")
            if not image_data:
                return None, None

            filename = f"avatar_{uuid.uuid4().hex[:8]}.png"
            filepath = os.path.join(self._avatars_dir, filename)

            if isinstance(image_data, bytes):
                with open(filepath, "wb") as f:
                    f.write(image_data)
            elif isinstance(image_data, str) and image_data.startswith("http"):
                import requests
                resp = requests.get(image_data, timeout=30)
                resp.raise_for_status()
                with open(filepath, "wb") as f:
                    f.write(resp.content)
            elif isinstance(image_data, str) and image_data.startswith("data:"):
                import base64
                b64 = image_data.split(",", 1)[1]
                with open(filepath, "wb") as f:
                    f.write(base64.b64decode(b64))
            else:
                return None, None

            url = f"/static/jokes_avatars/{filename}"
            # Lê base64 para persistência
            import base64
            with open(filepath, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            return url, b64

        except Exception as e:
            print(f"[JokesService] Erro ao gerar avatar: {e}")
            return None, None

    # ─── Geração de Vídeo ─────────────────────────────────────────────────────

    def create_jokes_video(
        self,
        episode_id: int,
        channel_name: str,
        jokes: List[dict],
        avatar_path: Optional[str],
        voice_gender: str = "male",
        theme: str = "geral",
        on_progress=None,
    ) -> dict:
        """
        Gera o vídeo completo do episódio de piadas.
        Retorna dict com video_url e duration_sec.
        """
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
        import numpy as np

        try:
            from moviepy import (
                ImageClip, AudioFileClip,
                concatenate_videoclips, VideoClip,
            )
            from moviepy.audio.AudioClip import concatenate_audioclips
        except ImportError:
            from moviepy.editor import (
                ImageClip, AudioFileClip,
                concatenate_videoclips,
            )
            from moviepy.audio.AudioClip import concatenate_audioclips

        SIZE = (720, 1280)  # Vertical HD (compatível com YouTube Shorts e economiza memória/tempo)
        FPS = 24
        TOTAL_JOKES = len(jokes)

        def _progress(pct, msg=""):
            if on_progress:
                on_progress(pct, msg)
            print(f"[JokesVideo] {pct}% - {msg}")

        _progress(2, "Preparando recursos...")

        # Carrega avatar
        avatar_img = self._load_avatar_pil(avatar_path, target_height=int(SIZE[1] * 0.40))

        # Cria TTS para cada piada
        audio_paths = []
        _progress(5, "Gerando áudio das piadas...")
        for i, joke in enumerate(jokes):
            pct = 5 + int((i / TOTAL_JOKES) * 35)
            _progress(pct, f"Gerando TTS para piada {i+1}/{TOTAL_JOKES}...")
            audio = self._generate_joke_tts(joke["text"], voice_gender)
            audio_paths.append(audio)

        # Monta slides de vídeo
        clips = []

        # ── Intro ──
        _progress(40, "Criando slide de abertura...")
        intro_clip = self._make_intro_clip(channel_name, theme, SIZE, FPS, duration=4)
        clips.append(intro_clip)

        # ── Cada Piada ──
        for i, (joke, audio_path) in enumerate(zip(jokes, audio_paths)):
            pct = 42 + int((i / TOTAL_JOKES) * 45)
            _progress(pct, f"Montando cena da piada {i+1}/{TOTAL_JOKES}...")
            joke_clip = self._make_joke_clip(
                joke=joke,
                avatar_img=avatar_img,
                audio_path=audio_path,
                size=SIZE,
                fps=FPS,
                joke_number=i + 1,
                total_jokes=TOTAL_JOKES,
            )
            clips.append(joke_clip)

            # Micro-transição entre piadas (frame colorido)
            if i < TOTAL_JOKES - 1:
                trans = self._make_transition_clip(SIZE, FPS, duration=1.0)
                clips.append(trans)

        # ── Outro ──
        _progress(88, "Criando slide de encerramento...")
        outro_clip = self._make_outro_clip(channel_name, SIZE, FPS, duration=5)
        clips.append(outro_clip)

        _progress(90, "Concatenando vídeo final...")
        final_clip = concatenate_videoclips(clips, method="chain")

        # Adiciona música de fundo leve
        final_clip = self._mix_background_music(final_clip, SIZE)

        filename = f"jokes_ep_{episode_id}_{uuid.uuid4().hex[:6]}.mp4"
        out_path = os.path.join(self.output_dir, filename)

        _progress(92, "Renderizando vídeo (pode demorar alguns minutos)...")
        final_clip.write_videofile(
            out_path,
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            temp_audiofile=os.path.join(self.output_dir, f"_tmp_audio_{episode_id}.m4a"),
            remove_temp=True,
            logger=None,
            threads=2,
        )
        final_clip.close()
        for c in clips:
            try:
                c.close()
            except Exception:
                pass

        duration = float(final_clip.duration) if hasattr(final_clip, "duration") and final_clip.duration else 0
        video_url = f"{VIDEO_URL_PREFIX}/{filename}"
        _progress(100, "Vídeo gerado com sucesso!")
        return {"video_url": video_url, "duration_sec": duration, "filename": filename}

    # ─── Helpers de Clip ─────────────────────────────────────────────────────

    def _load_avatar_pil(self, avatar_path: Optional[str], target_height: int = 700):
        """Carrega e redimensiona avatar PIL. Retorna PIL Image ou None."""
        from PIL import Image
        try:
            if not avatar_path:
                return None
            # avatar_path pode ser URL relativa (/static/...) ou caminho no disco
            if avatar_path.startswith("/static/"):
                disk_path = os.path.join(str(STATIC_DIR), avatar_path[len("/static/"):])
            else:
                disk_path = avatar_path
            if not os.path.exists(disk_path):
                return None
            img = Image.open(disk_path).convert("RGBA")
            ratio = target_height / img.height
            new_w = int(img.width * ratio)
            img = img.resize((new_w, target_height), Image.LANCZOS)
            return img
        except Exception as e:
            print(f"[JokesService] Erro ao carregar avatar: {e}")
            return None

    def _generate_joke_tts(self, text: str, voice_gender: str = "male") -> Optional[str]:
        """Gera TTS da piada e retorna caminho do arquivo de áudio."""
        try:
            from app.services.video_generator import VideoGenerator
            vg = VideoGenerator(output_dir=self.output_dir, ai_service=self.ai_service)
            path = vg.generate_audio(text, lang="pt", voice_style="human", voice_gender=voice_gender)
            return path
        except Exception as e:
            print(f"[JokesService] Erro TTS: {e}")
            return None

    def _make_intro_clip(self, channel_name: str, theme: str, size: tuple, fps: int, duration: float = 4.0):
        """Cria slide de abertura animado."""
        try:
            from moviepy import ImageClip
        except ImportError:
            from moviepy.editor import ImageClip

        frame = self._draw_intro_frame(channel_name, theme, size)
        clip = ImageClip(frame, duration=duration).with_fps(fps)
        return clip

    def _make_outro_clip(self, channel_name: str, size: tuple, fps: int, duration: float = 5.0):
        """Cria slide de encerramento com CTA."""
        try:
            from moviepy import ImageClip
        except ImportError:
            from moviepy.editor import ImageClip

        frame = self._draw_outro_frame(channel_name, size)
        clip = ImageClip(frame, duration=duration).with_fps(fps)
        return clip

    def _make_transition_clip(self, size: tuple, fps: int, duration: float = 0.8):
        """Cria um frame de transição colorido entre piadas."""
        try:
            from moviepy import ImageClip
        except ImportError:
            from moviepy.editor import ImageClip

        import numpy as np
        from PIL import Image, ImageDraw
        img = Image.new("RGB", size, color=(255, 200, 0))
        draw = ImageDraw.Draw(img)
        # Linha de risada
        font = self._get_font(80)
        stars = "✦ ✦ ✦ ✦ ✦"
        w, h = size
        bbox = draw.textbbox((0, 0), stars, font=font)
        tw = bbox[2] - bbox[0]
        draw.text(((w - tw) // 2, h // 2 - 60), stars, font=font, fill=(80, 40, 0))
        frame = np.array(img)
        return ImageClip(frame, duration=duration).with_fps(fps)

    def _make_joke_clip(
        self,
        joke: dict,
        avatar_img,
        audio_path: Optional[str],
        size: tuple,
        fps: int,
        joke_number: int,
        total_jokes: int,
    ):
        """
        Cria o clip de uma piada: fundo colorido + avatar animado + texto + áudio TTS.
        Usa 2 frames-chave (avatar neutro / falando) para economizar memória.
        """
        try:
            from moviepy import AudioFileClip, VideoClip
        except ImportError:
            from moviepy.editor import AudioFileClip, VideoClip
        import numpy as np

        # Duração base: audio ou mínimo 5s
        audio_clip = None
        audio_duration = 6.0
        if audio_path and os.path.exists(audio_path):
            try:
                audio_clip = AudioFileClip(audio_path)
                audio_duration = audio_clip.duration
            except Exception as e:
                print(f"[JokesService] Erro ao carregar áudio: {e}")

        total_duration = max(audio_duration + 1.5, 6.0)

        # Pré-computa apenas 2 frames-chave para economizar memória
        frame_neutral, frame_talking = self._build_joke_keyframes(
            joke_text=joke["text"],
            avatar_img=avatar_img,
            size=size,
            joke_number=joke_number,
            total_jokes=total_jokes,
        )

        # make_frame seleciona frame com base no tempo (troca 3.5x/s durante a fala)
        def make_frame(t):
            is_talking = t < audio_duration + 0.3
            use_talking = is_talking and (int(t * 7) % 2 == 0)
            return frame_talking if use_talking else frame_neutral

        clip = VideoClip(make_frame=make_frame, duration=total_duration).with_fps(fps)

        if audio_clip:
            clip = clip.with_audio(audio_clip)

        return clip

    def _build_joke_keyframes(
        self,
        joke_text: str,
        avatar_img,
        size: tuple,
        joke_number: int,
        total_jokes: int,
    ):
        """
        Cria 2 frames numpy (neutro, falando) para o clip de piada.
        Mantém a memória baixa: apenas 2 frames em vez de fps*duração frames.
        """
        from PIL import Image, ImageDraw
        import numpy as np

        W, H = size
        bg_colors = [
            (30, 30, 60), (20, 60, 30), (60, 20, 40),
            (60, 40, 10), (20, 40, 60), (50, 15, 50),
        ]
        bg_color = bg_colors[(joke_number - 1) % len(bg_colors)]

        avatar_normal = avatar_img
        avatar_talking = self._create_talking_variant(avatar_img) if avatar_img else None

        font_joke = self._get_font(38)
        font_counter = self._get_font(28)
        lines = textwrap.wrap(joke_text, width=34)[:10]

        def _build_frame(current_avatar, sound_wave: bool):
            img = Image.new("RGB", size, color=bg_color)
            draw = ImageDraw.Draw(img)
            self._draw_bg_gradient(draw, W, H, bg_color)

            if current_avatar:
                av_w, av_h = current_avatar.size
                ax = (W - av_w) // 2
                ay = int(H * 0.08)
                if current_avatar.mode == "RGBA":
                    bg_layer = Image.new("RGB", current_avatar.size, bg_color)
                    bg_layer.paste(current_avatar, mask=current_avatar.split()[3])
                    img.paste(bg_layer, (ax, ay))
                else:
                    img.paste(current_avatar, (ax, ay))
                if sound_wave:
                    self._draw_sound_waves(draw, ax + av_w // 2, ay + av_h // 2, 0.1)

            counter_text = f"#{joke_number}/{total_jokes}"
            draw.text((W - 140, 40), counter_text, font=font_counter, fill=(200, 200, 200))
            self._draw_text_box(draw, lines, W, H, int(H * 0.52), font_joke)
            self._draw_progress_bar(draw, W, H, joke_number, total_jokes)
            return np.array(img)

        frame_neutral = _build_frame(avatar_normal, sound_wave=False)
        frame_talking = _build_frame(avatar_talking or avatar_normal, sound_wave=True)
        return frame_neutral, frame_talking

    def _create_talking_variant(self, avatar_img):
        """Cria variante do avatar com indicador de boca aberta (overlay simples)."""
        from PIL import Image, ImageDraw
        if not avatar_img:
            return None
        try:
            variant = avatar_img.copy()
            draw = ImageDraw.Draw(variant)
            W, H = variant.size
            # Desenha uma elipse oval na área inferior central (boca)
            mouth_cx = W // 2
            mouth_cy = int(H * 0.70)
            mouth_w = int(W * 0.12)
            mouth_h = int(H * 0.05)
            draw.ellipse(
                [mouth_cx - mouth_w, mouth_cy - mouth_h,
                 mouth_cx + mouth_w, mouth_cy + mouth_h],
                fill=(180, 80, 80),
                outline=(100, 40, 40),
                width=2,
            )
            return variant
        except Exception:
            return avatar_img

    def _draw_bg_gradient(self, draw, W: int, H: int, base_color: tuple):
        """Desenha um gradiente vertical no fundo."""
        r, g, b = base_color
        step = H // 20
        for i in range(20):
            factor = 1.0 + (i / 20) * 0.3
            cr = min(255, int(r * factor))
            cg = min(255, int(g * factor))
            cb = min(255, int(b * factor))
            y0 = i * step
            y1 = y0 + step + 1
            draw.rectangle([(0, y0), (W, y1)], fill=(cr, cg, cb))

    def _draw_sound_waves(self, draw, cx: int, cy: int, t: float):
        """Desenha ondas de som animadas ao redor do avatar."""
        from PIL import ImageDraw
        num_waves = 3
        for i in range(num_waves):
            phase = (t * 2 + i * 0.5) % 1.0
            radius = int(50 + phase * 80)
            alpha_factor = max(0, 1.0 - phase)
            color = (255, 220, 50, int(180 * alpha_factor))
            try:
                draw.ellipse(
                    [cx - radius, cy - radius, cx + radius, cy + radius],
                    outline=(255, 220, 50),
                    width=max(1, int(3 * alpha_factor)),
                )
            except Exception:
                pass

    def _draw_text_box(self, draw, lines: list, W: int, H: int, top: int, font):
        """Desenha caixa de texto semi-transparente com a piada."""
        from PIL import Image, ImageDraw
        if not lines:
            return
        line_h = 52
        padding = 30
        box_h = len(lines) * line_h + padding * 2
        box_y = top
        # Fundo escuro semi-transparente (simulado desenhando retângulo escurecido)
        draw.rectangle(
            [(40, box_y), (W - 40, box_y + box_h)],
            fill=(0, 0, 0),
            outline=(255, 220, 50),
            width=3,
        )
        y = box_y + padding
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            tw = bbox[2] - bbox[0]
            x = (W - tw) // 2
            # Sombra
            draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0))
            draw.text((x, y), line, font=font, fill=(255, 255, 220))
            y += line_h

    def _draw_progress_bar(self, draw, W: int, H: int, current: int, total: int):
        """Desenha barra de progresso do episódio no rodapé."""
        bar_h = 8
        bar_y = H - 30
        bar_w = int((current / total) * (W - 80))
        draw.rectangle([(40, bar_y), (W - 40, bar_y + bar_h)], fill=(60, 60, 60))
        draw.rectangle([(40, bar_y), (40 + bar_w, bar_y + bar_h)], fill=(255, 200, 50))

    def _draw_intro_frame(self, channel_name: str, theme: str, size: tuple, extra=None) -> "np.ndarray":
        """Cria frame de abertura do episódio."""
        from PIL import Image, ImageDraw
        import numpy as np

        W, H = size
        img = Image.new("RGB", size, color=(20, 15, 50))
        draw = ImageDraw.Draw(img)

        # Fundo gradiente roxo/azul
        for i in range(H):
            ratio = i / H
            r = int(20 + ratio * 30)
            g = int(15 + ratio * 20)
            b = int(50 + ratio * 60)
            draw.line([(0, i), (W, i)], fill=(r, g, b))

        # Estrelas decorativas
        import random
        rng = random.Random(42)
        for _ in range(60):
            x = rng.randint(0, W)
            y = rng.randint(0, int(H * 0.5))
            r2 = rng.randint(2, 5)
            draw.ellipse([x - r2, y - r2, x + r2, y + r2], fill=(255, 255, 200))

        # Microfone / ícone
        font_icon = self._get_font(160)
        icon = "🎙️"
        try:
            bbox = draw.textbbox((0, 0), icon, font=font_icon)
            tw = bbox[2] - bbox[0]
            draw.text(((W - tw) // 2, int(H * 0.15)), icon, font=font_icon)
        except Exception:
            pass

        # Nome do canal
        font_title = self._get_font(72)
        for line in textwrap.wrap(channel_name.upper(), width=18):
            bbox = draw.textbbox((0, 0), line, font=font_title)
            tw = bbox[2] - bbox[0]
            x = (W - tw) // 2
            draw.text((x + 3, int(H * 0.42) + 3), line, font=font_title, fill=(0, 0, 0))
            draw.text((x, int(H * 0.42)), line, font=font_title, fill=(255, 220, 50))

        theme_label = THEME_LABELS.get(theme, "Piadas")
        font_sub = self._get_font(46)
        sub_text = f"— {theme_label} —"
        bbox = draw.textbbox((0, 0), sub_text, font=font_sub)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) // 2, int(H * 0.58)), sub_text, font=font_sub, fill=(200, 200, 255))

        # Barra inferior
        draw.rectangle([(0, H - 80), (W, H)], fill=(255, 200, 50))
        cta = "Fique até o fim! 😄"
        font_cta = self._get_font(40)
        bbox = draw.textbbox((0, 0), cta, font=font_cta)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) // 2, H - 65), cta, font=font_cta, fill=(20, 20, 20))

        return np.array(img)

    def _draw_outro_frame(self, channel_name: str, size: tuple) -> "np.ndarray":
        """Cria frame de encerramento com CTA."""
        from PIL import Image, ImageDraw
        import numpy as np

        W, H = size
        img = Image.new("RGB", size, color=(20, 60, 20))
        draw = ImageDraw.Draw(img)

        # Fundo gradiente verde
        for i in range(H):
            ratio = i / H
            r = int(20 + ratio * 10)
            g = int(60 + ratio * 40)
            b = int(20 + ratio * 10)
            draw.line([(0, i), (W, i)], fill=(r, g, b))

        font_big = self._get_font(80)
        font_med = self._get_font(50)
        font_small = self._get_font(38)

        lines_cta = [
            ("Gostou? 😄", int(H * 0.18), (255, 255, 100)),
            ("Curta e Compartilhe!", int(H * 0.30), (255, 255, 255)),
            ("Se inscreva para", int(H * 0.44), (200, 255, 200)),
            ("mais piadas!", int(H * 0.54), (200, 255, 200)),
            (channel_name, int(H * 0.67), (255, 220, 50)),
            ("Até a próxima! 👋", int(H * 0.80), (200, 200, 255)),
        ]

        for text, y_pos, color in lines_cta:
            font = font_big if y_pos < H * 0.25 else (font_med if y_pos < H * 0.65 else font_small)
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            x = (W - tw) // 2
            draw.text((x + 2, y_pos + 2), text, font=font, fill=(0, 0, 0))
            draw.text((x, y_pos), text, font=font, fill=color)

        # Rodapé
        draw.rectangle([(0, H - 100), (W, H)], fill=(255, 200, 50))
        sub_text = "Ative o sino 🔔 para não perder!"
        font_sub = self._get_font(36)
        bbox = draw.textbbox((0, 0), sub_text, font=font_sub)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) // 2, H - 80), sub_text, font=font_sub, fill=(20, 20, 20))

        return np.array(img)

    def _mix_background_music(self, clip, size: tuple):
        """Adiciona música de fundo suave ao vídeo (se disponível)."""
        try:
            from moviepy.audio.AudioClip import concatenate_audioclips, AudioArrayClip
            import numpy as np

            music_candidates = []
            music_dir = os.path.join(str(STATIC_DIR), "music")
            if os.path.isdir(music_dir):
                for f in os.listdir(music_dir):
                    if f.endswith(".mp3"):
                        music_candidates.append(os.path.join(music_dir, f))

            if not music_candidates:
                return clip

            music_path = music_candidates[0]

            try:
                from moviepy import AudioFileClip
            except ImportError:
                from moviepy.editor import AudioFileClip

            music = AudioFileClip(music_path)
            # Loop até a duração do vídeo
            if music.duration < clip.duration:
                repeats = math.ceil(clip.duration / music.duration)
                music = concatenate_audioclips([music] * repeats)
            # Corta para a duração do vídeo
            try:
                music = music.subclipped(0, clip.duration)
            except AttributeError:
                music = music.subclip(0, clip.duration)
            music = music.with_volume_scaled(0.08)

            if clip.audio:
                try:
                    from moviepy.audio.AudioClip import CompositeAudioClip
                except ImportError:
                    try:
                        from moviepy import CompositeAudioClip
                    except ImportError:
                        from moviepy.editor import CompositeAudioClip
                final_audio = CompositeAudioClip([clip.audio, music])
                return clip.with_audio(final_audio)
            else:
                return clip.with_audio(music)
        except Exception as e:
            print(f"[JokesService] Música de fundo ignorada: {e}")
            return clip

    def _get_font(self, size: int):
        """Retorna fonte PIL no tamanho especificado."""
        from PIL import ImageFont
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "DejaVuSans-Bold.ttf",
            "arial.ttf",
        ]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()
