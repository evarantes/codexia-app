import os
import uuid
import requests
import gc
import threading
import asyncio
import re
import time
import difflib
import unicodedata
from typing import Optional, Callable

from app.config import VIDEO_OUTPUT_DIR, VIDEO_URL_PREFIX, STATIC_DIR


class VideoGenerator:
    def __init__(self, output_dir=None, ai_service=None):
        self.output_dir = output_dir or VIDEO_OUTPUT_DIR
        os.makedirs(self.output_dir, exist_ok=True)
        self.music_dir = "app/static/music"
        os.makedirs(self.music_dir, exist_ok=True)
        self.generated_dir = os.path.join(str(STATIC_DIR), "generated")
        os.makedirs(self.generated_dir, exist_ok=True)
        self.ai_service = ai_service
        self.MUSIC_CREDITS = {
            "drama": "Music: Impact Prelude by Kevin MacLeod\nFree download: https://filmmusic.io/song/3900-impact-prelude\nLicense (CC BY 4.0): https://filmmusic.io/standard-license",
            "epic": "Music: Impact Andante by Kevin MacLeod\nFree download: https://filmmusic.io/song/3898-impact-andante\nLicense (CC BY 4.0): https://filmmusic.io/standard-license",
            "happy": "Music: Carefree by Kevin MacLeod\nFree download: https://filmmusic.io/song/3476-carefree\nLicense (CC BY 4.0): https://filmmusic.io/standard-license"
        }
        # self._ensure_fallback_music() removido do init para evitar delay no startup

    def _ffprobe_duration_seconds(self, path: str) -> float:
        try:
            import subprocess
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if r.returncode != 0:
                return 0.0
            s = (r.stdout or "").strip()
            try:
                return float(s) if s else 0.0
            except Exception:
                return 0.0
        except Exception:
            return 0.0

    def _ensure_playable_mp4(self, path: str) -> str:
        try:
            if not path or not os.path.exists(path):
                raise Exception("Arquivo de vídeo não encontrado.")
            if os.path.getsize(path) < 1024 * 50:
                raise Exception("Arquivo de vídeo muito pequeno (provável falha no render).")
        except Exception:
            raise

        dur = self._ffprobe_duration_seconds(path)
        if dur >= 0.5:
            return path

        try:
            import subprocess
            fixed = f"{path}.fixed.mp4"
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", path, "-c", "copy", "-movflags", "+faststart", "-pix_fmt", "yuv420p", fixed],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if r.returncode == 0 and os.path.exists(fixed) and os.path.getsize(fixed) > 1024 * 50:
                dur2 = self._ffprobe_duration_seconds(fixed)
                if dur2 >= 0.5:
                    try:
                        os.replace(fixed, path)
                    except Exception:
                        return fixed
                    return path
        except Exception:
            pass

        raise Exception("Vídeo gerado inválido (duração 0s). Verifique ffmpeg e armazenamento /data.")

    def _ensure_fallback_music(self):
        """Baixa músicas de fallback se a pasta estiver vazia"""
        try:
            import glob
            if not glob.glob(os.path.join(self.music_dir, "*.mp3")):
                print("Baixando músicas de fallback...")
                music_urls = {
                    "drama.mp3": "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Impact%20Prelude.mp3",
                    "epic.mp3": "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Impact%20Andante.mp3",
                    "happy.mp3": "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Carefree.mp3"
                }
                
                for filename, url in music_urls.items():
                    try:
                        print(f"Baixando {filename}...")
                        response = requests.get(url, timeout=30)
                        if response.status_code == 200:
                            with open(os.path.join(self.music_dir, filename), 'wb') as f:
                                f.write(response.content)
                    except Exception as e:
                        print(f"Erro ao baixar {filename}: {e}")
        except Exception as e:
            print(f"Erro no setup de músicas: {e}")

    def create_text_image(self, text, size=(1080, 1920), bg_color=(20, 20, 20), text_color=(255, 255, 255), bg_image_path=None, footer_text: Optional[str] = None):
        from PIL import Image, ImageEnhance
        import numpy as np

        bg = None
        if bg_image_path and os.path.exists(bg_image_path):
            try:
                bg = Image.open(bg_image_path).convert("RGB")
            except Exception as e:
                print(f"Erro ao carregar imagem de fundo: {e}")
                bg = None
        if bg is None:
            bg = Image.new("RGB", size, color=bg_color)
        else:
            img_ratio = bg.width / max(1, bg.height)
            target_ratio = size[0] / max(1, size[1])
            if img_ratio > target_ratio:
                new_height = size[1]
                new_width = int(new_height * img_ratio)
                bg = bg.resize((new_width, new_height), Image.LANCZOS)
                left = int((new_width - size[0]) / 2)
                bg = bg.crop((left, 0, left + size[0], size[1]))
            else:
                new_width = size[0]
                new_height = int(new_width / max(0.0001, img_ratio))
                bg = bg.resize((new_width, new_height), Image.LANCZOS)
                top = int((new_height - size[1]) / 2)
                bg = bg.crop((0, top, size[0], top + size[1]))
            try:
                bg = ImageEnhance.Brightness(bg).enhance(0.8)
            except Exception:
                pass

        overlay = self.create_text_overlay(text, size=size, text_color=text_color, footer_text=footer_text)
        base = bg.convert("RGBA")
        try:
            base.alpha_composite(Image.fromarray(overlay, mode="RGBA"))
        except Exception:
            base = base.convert("RGB")
            return np.array(base)
        return np.array(base.convert("RGB"))

    def create_text_overlay(self, text, size=(1080, 1920), text_color=(255, 255, 255), footer_text: Optional[str] = None):
        from PIL import Image, ImageDraw, ImageFont
        import numpy as np

        text = (text or "").strip()
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        font_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "DejaVuSans-Bold.ttf",
            "arial.ttf",
        ]

        def measure(s: str, f):
            try:
                return draw.textlength(s, font=f)
            except Exception:
                b = draw.textbbox((0, 0), s, font=f)
                return float(b[2] - b[0])

        def wrap_words(s: str, f, max_w: int):
            words = [w for w in (s or "").split() if w]
            lines = []
            cur = ""
            for w in words:
                test = w if not cur else f"{cur} {w}"
                if measure(test, f) <= max_w:
                    cur = test
                    continue
                if cur:
                    lines.append(cur)
                    cur = w
                    continue
                acc = ""
                for ch in w:
                    test2 = acc + ch
                    if measure(test2, f) <= max_w:
                        acc = test2
                    else:
                        if acc:
                            lines.append(acc)
                        acc = ch
                cur = acc
            if cur:
                lines.append(cur)
            return lines

        w, h = size
        margin_x = int(w * 0.07)
        margin_bottom = int(h * 0.10)
        max_w = max(120, w - 2 * margin_x)
        max_h = max(120, int(h * 0.42))

        base_size = max(26, min(78, int(w * 0.060)))
        min_size = max(18, min(34, int(w * 0.030)))

        chosen_font = None
        chosen_lines = []
        chosen_line_h = 0

        for fs in range(base_size, min_size - 1, -2):
            font = None
            for fp in font_candidates:
                try:
                    font = ImageFont.truetype(fp, fs)
                    break
                except Exception:
                    continue
            if font is None:
                font = ImageFont.load_default()

            lines = wrap_words(text, font, max_w)
            try:
                line_h = int(getattr(font, "size", fs) * 1.20)
            except Exception:
                line_h = int(fs * 1.20)
            total_h = len(lines) * line_h
            if lines and total_h <= max_h:
                chosen_font = font
                chosen_lines = lines
                chosen_line_h = line_h
                break

        if chosen_font is None:
            font = None
            for fp in font_candidates:
                try:
                    font = ImageFont.truetype(fp, min_size)
                    break
                except Exception:
                    continue
            if font is None:
                font = ImageFont.load_default()
            lines = wrap_words(text, font, max_w)
            line_h = int(getattr(font, "size", min_size) * 1.20)
            max_lines = max(1, int(max_h / max(1, line_h)))
            if len(lines) > max_lines:
                keep = lines[:max_lines]
                last = keep[-1]
                ell = "..."
                while last and measure(last + ell, font) > max_w:
                    last = last[:-1].rstrip()
                keep[-1] = (last + ell).strip() if last else ell
                lines = keep
            chosen_font = font
            chosen_lines = lines
            chosen_line_h = line_h

        text_block_h = len(chosen_lines) * chosen_line_h
        y = h - margin_bottom - text_block_h
        y = max(int(h * 0.08), y)

        outline = (0, 0, 0, 255)
        fill = (int(text_color[0]), int(text_color[1]), int(text_color[2]), 255)
        for line in chosen_lines:
            b = draw.textbbox((0, 0), line, font=chosen_font)
            tw = b[2] - b[0]
            x = int((w - tw) / 2)
            for off in [(2, 2), (-2, -2), (2, -2), (-2, 2), (0, 2), (2, 0), (-2, 0), (0, -2)]:
                draw.text((x + off[0], y + off[1]), line, font=chosen_font, fill=outline)
            draw.text((x, y), line, font=chosen_font, fill=fill)
            y += chosen_line_h

        footer = (footer_text or "").strip()
        if footer:
            footer_fs = max(14, min(34, int(w * 0.028)))
            footer_font = None
            for fp in font_candidates:
                try:
                    footer_font = ImageFont.truetype(fp, footer_fs)
                    break
                except Exception:
                    continue
            if footer_font is None:
                footer_font = ImageFont.load_default()

            try:
                fb = draw.textbbox((0, 0), footer, font=footer_font)
                ftw = fb[2] - fb[0]
                fth = fb[3] - fb[1]
            except Exception:
                ftw = int(measure(footer, footer_font))
                fth = int(footer_fs * 1.2)

            pad_x = int(w * 0.03)
            pad_y = int(max(8, h * 0.010))
            fx = int((w - ftw) / 2)
            fy = int(h - pad_y - fth - int(h * 0.02))

            rect = (
                max(0, fx - pad_x),
                max(0, fy - int(pad_y * 0.7)),
                min(w, fx + ftw + pad_x),
                min(h, fy + fth + int(pad_y * 0.7)),
            )
            draw.rectangle(rect, fill=(0, 0, 0, 150))
            for off in [(1, 1), (-1, -1), (1, -1), (-1, 1)]:
                draw.text((fx + off[0], fy + off[1]), footer, font=footer_font, fill=(0, 0, 0, 255))
            draw.text((fx, fy), footer, font=footer_font, fill=(255, 255, 255, 230))

        return np.array(img)

    def _make_caption(self, narration: str):
        t = (narration or "").strip()
        if not t:
            return ""
        t = re.sub(r"\s+", " ", t)
        parts = re.split(r"(?<=[.!?])\s+", t)
        cap = ""
        for p in parts:
            if not p:
                continue
            if len((cap + " " + p).strip()) <= 180:
                cap = (cap + " " + p).strip()
                if len(cap) >= 120:
                    break
            else:
                break
        if not cap:
            cap = t[:180].rstrip()
        return cap

    def review_plan(self, plan: dict):
        if not isinstance(plan, dict):
            return plan
        scenes = plan.get("scenes") or []
        if not isinstance(scenes, list) or not scenes:
            return plan
        notes = []
        for i, s in enumerate(scenes):
            if not isinstance(s, dict):
                continue
            txt = (s.get("text") or "").strip()
            clean = self._clean_text(txt)
            cap = (s.get("caption") or s.get("on_screen_text") or "").strip()
            if not cap:
                cap = clean if len(clean) <= 220 else self._make_caption(clean)
                s["caption"] = cap
                notes.append(f"caption_auto:cena_{i+1}")
            elif len(cap) > 220:
                s["caption"] = self._make_caption(cap)
                notes.append(f"caption_trunc:cena_{i+1}")
        plan["scenes"] = scenes
        if notes:
            plan["review_notes"] = notes
        return plan

    def _clean_text(self, text):
        """Limpa o texto de metadados, instruções de roteiro e markdown"""
        if not text: return ""
        
        # 1. Remove Markdown Bold (**text**) -> text (keep content, remove markers)
        text = text.replace("**", "")
        
        # 2. Remove Script Prefixes
        # "Narrador:", "Cena 1:", "Imagem:"
        text = re.sub(r'^(Narrador|Narrator|Cena|Scene|Imagem|Visual)(\s+\d+)?\s*[:.-]\s*', '', text, flags=re.IGNORECASE)
        
        # 3. Remove Instructions in Brackets [Visual: ...] or [Sound: ...]
        text = re.sub(r'\[.*?\]', '', text)
        
        # 4. Remove Instructions in Parentheses that look like metadata
        # Removes (Music: ...), (Visual: ...), (Tone: ...)
        text = re.sub(r'\((Music|Visual|Sound|Tone|Credit|Source).*?\)', '', text, flags=re.IGNORECASE)
        
        # 5. Remove explicit credits lines
        text = re.sub(r'^Music:.*$', '', text, flags=re.MULTILINE|re.IGNORECASE)
        text = re.sub(r'^Credits:.*$', '', text, flags=re.MULTILINE|re.IGNORECASE)

        return text.strip()

    def generate_audio(self, text, lang='pt', voice_style=None, voice_gender=None):
        """Gera arquivo de áudio usando OpenAI (Human-like), Edge-TTS (Natural Free) ou gTTS (Fallback)"""
        if not text or not text.strip(): 
            print("Aviso: Texto vazio para generate_audio")
            return None
        
        # Limpeza de segurança para evitar leitura de metadados
        clean_text = self._clean_text(text)
        if not clean_text: 
            print("Aviso: Texto ficou vazio após limpeza em generate_audio")
            return None

        style = (voice_style or "human").lower()
        gender = (voice_gender or "female").lower()
        
        print(f"Gerando áudio para: '{clean_text[:30]}...' (Style: {style}, Gender: {gender})")
        
        openai_voice = "onyx"
        if style in ["my_voice", "myvoice", "minha_voz", "minhavoz"]:
            openai_voice = "my_voice"
        elif style in ["human", "humana"]:
            openai_voice = "onyx" if gender == "male" else "nova"
        elif style in ["child", "infantil"]:
            openai_voice = "echo" if gender == "male" else "shimmer"
        elif style in ["angelic", "angelical"]:
            openai_voice = "fable"
        elif style in ["robotic", "robotica", "robótica"]:
            openai_voice = None

        # 1. ElevenLabs/OpenAI TTS (ai_service tenta ElevenLabs primeiro, depois OpenAI)
        # Importante: não depende de OPENAI_API_KEY para usar ElevenLabs.
        if openai_voice and self.ai_service and hasattr(self.ai_service, "generate_audio"):
            try:
                print(f"Tentando TTS premium ({openai_voice})...")
                audio_content = self.ai_service.generate_audio(clean_text, voice=openai_voice)
                if audio_content:
                    filename = f"{uuid.uuid4()}.mp3"
                    path = os.path.join(self.output_dir, filename)
                    with open(path, "wb") as f:
                        f.write(audio_content)
                    print(f"TTS premium sucesso: {path}")
                    return path
            except Exception as e:
                print(f"TTS premium falhou, tentando fallback: {e}")

        # 3. Edge TTS (Qualidade Natural Gratuita - Microsoft)
        if style not in ["robotic", "robotica", "robótica"]:
            try:
                print("Tentando Edge TTS...")
                import edge_tts
                import asyncio
                import threading
                
                if lang == 'pt':
                    if gender == "male":
                        voice = "pt-BR-AntonioNeural"
                    else:
                        voice = "pt-BR-FranciscaNeural"
                else:
                    if gender == "male":
                        voice = "en-US-ChristopherNeural"
                    else:
                        voice = "en-US-JennyNeural"

                filename = f"{uuid.uuid4()}.mp3"
                path = os.path.join(self.output_dir, filename)

                async def _run_edge_tts():
                    communicate = edge_tts.Communicate(clean_text, voice)
                    await communicate.save(path)
                    
                t = threading.Thread(target=lambda: asyncio.run(_run_edge_tts()))
                t.start()
                t.join(timeout=15) # Aumentado timeout para 15s

                if os.path.exists(path) and os.path.getsize(path) > 500: # Check > 500 bytes
                    print(f"Edge TTS sucesso: {path}")
                    return path
                else:
                    print(f"Edge TTS gerou arquivo vazio ou falhou (Size check failed). Path: {path}")
            except Exception as e:
                 print(f"Edge TTS falhou: {e}")

        # 4. Fallback gTTS (Robótico)
        try:
            from gtts import gTTS
            print("Tentando Fallback gTTS (Robótico)...")
            tts = gTTS(text=clean_text, lang=lang)
            filename = f"{uuid.uuid4()}.mp3"
            path = os.path.join(self.output_dir, filename)
            tts.save(path)
            
            # Verificação de segurança
            if os.path.exists(path) and os.path.getsize(path) > 100:
                print(f"gTTS sucesso: {path}")
                return path
            else:
                 print("gTTS gerou arquivo vazio.")
                 return None
        except Exception as e:
            print(f"Erro no TTS Final (gTTS): {e}")
            return None

    def download_image(self, url, retries=3, timeout=20):
        import time
        try:
            import imghdr
        except ImportError:
            imghdr = None
        
        if "pollinations.ai" in (url or ""):
            timeout = max(timeout, 60)
        
        for attempt in range(retries):
            try:
                print(f"Baixando imagem de: {url[:50]}... (Tentativa {attempt+1}/{retries}, timeout={timeout}s)")
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8'
                }
                response = requests.get(url, headers=headers, stream=True, timeout=timeout)
                
                if response.status_code == 200:
                    filename = f"genimg_{uuid.uuid4().hex}.png"
                    filepath = os.path.join(self.generated_dir, filename)
                    
                    with open(filepath, 'wb') as f:
                        for chunk in response.iter_content(4096):
                            f.write(chunk)
                
                    # Verificação de tamanho
                    file_size = os.path.getsize(filepath)
                    if file_size < 1000: # 1KB mínimo
                        print(f"AVISO: Imagem muito pequena ({file_size} bytes). Ignorando.")
                        try: os.remove(filepath)
                        except: pass
                        continue
                        
                    # Verificação de tipo de arquivo (Header)
                    try:
                        img_type = None
                        if imghdr:
                            img_type = imghdr.what(filepath)
                            
                        if not img_type and not filepath.lower().endswith('.svg'):
                            # Tenta abrir com PIL para confirmar
                            from PIL import Image
                            try:
                                with Image.open(filepath) as img:
                                    img.verify()
                            except:
                                print(f"AVISO: Arquivo baixado não é imagem válida. Ignorando.")
                                try: os.remove(filepath)
                                except: pass
                                continue
                    except:
                        pass
                    
                    return filepath
                elif response.status_code in [502, 503, 504, 429]:
                    print(f"Erro temporário ({response.status_code}). Retentando em 2s...")
                    time.sleep(2)
                    continue
                else:
                    print(f"Falha ao baixar imagem. Status: {response.status_code}")
                    # Se for 403/404, não adianta tentar muito
                    if response.status_code in [403, 404]:
                        break
            except Exception as e:
                print(f"Erro ao baixar imagem: {e}")
                time.sleep(1)
        
        return None

    def _generate_fallback_background(self, size):
        """Gera um fundo gradiente/texturizado localmente quando tudo falha"""
        try:
            from PIL import Image, ImageDraw
            import random
            
            width, height = size
            # Cria imagem base
            img = Image.new('RGB', (width, height), color=(20, 20, 20))
            draw = ImageDraw.Draw(img)
            
            # Cores com contraste e luminosidade média para evitar aspecto de tela preta.
            color_top = (random.randint(90, 150), random.randint(90, 160), random.randint(120, 210))
            color_bottom = (random.randint(30, 90), random.randint(30, 90), random.randint(60, 130))
            
            # Desenha gradiente vertical (linha por linha para simplicidade sem numpy)
            # Para performance em 1080p, desenhamos em baixa resolução e redimensionamos
            small_h = 256
            small_w = int(width * (small_h / height))
            small_img = Image.new('RGB', (small_w, small_h))
            small_draw = ImageDraw.Draw(small_img)
            
            for y in range(small_h):
                ratio = y / small_h
                r = int(color_top[0] + (color_bottom[0] - color_top[0]) * ratio)
                g = int(color_top[1] + (color_bottom[1] - color_top[1]) * ratio)
                b = int(color_top[2] + (color_bottom[2] - color_top[2]) * ratio)
                small_draw.line([(0, y), (small_w, y)], fill=(r, g, b))
            
            # Redimensiona para tamanho final com suavização
            img = small_img.resize((width, height), Image.BICUBIC)
            
            filename = f"genbg_{uuid.uuid4().hex}.png"
            filepath = os.path.join(self.generated_dir, filename)
            img.save(filepath)
            return filepath
        except Exception as e:
            print(f"Erro ao gerar fundo local: {e}")
            return None

    def _pollinations_direct_url(self, prompt, aspect_ratio="9:16"):
        """Gera URL direta do Pollinations como último recurso (não requer API key)."""
        import urllib.parse
        width, height = (720, 1280) if aspect_ratio == "9:16" else (1280, 720)
        safe_prompt = urllib.parse.quote(
            f"{prompt} photorealistic cinematic photography natural lighting pleasant mood "
            "realistic humans natural skin texture proportional anatomy "
            "avoid close-up portraits "
            "no horror no monsters no zombies no undead no gore no blood "
            "no creepy no uncanny no doll-like "
            "no deformed no disfigured no mutated no bad anatomy no extra limbs "
            "no bad hands no extra fingers no melted face no distorted faces "
            "high detail sharp focus no text no watermark no logo"
        )
        seed = uuid.uuid4()
        return f"https://image.pollinations.ai/prompt/{safe_prompt}?width={width}&height={height}&nologo=true&seed={seed}&enhance=false&model=flux"

    def _ensure_image_for_scene(
        self,
        prompt,
        text_fallback,
        aspect_ratio="9:16",
        status_callback=None,
        max_rounds=2,
        allow_non_ai_fallback=False
    ):
        """
        Garante imagem por IA com múltiplas tentativas e todos os provedores disponíveis.
        Sempre tenta obter uma imagem real de IA — nunca retorna None sem esgotar
        todas as opções, incluindo Pollinations direto como último recurso gratuito.
        """
        width, height = (720, 1280) if aspect_ratio == "9:16" else (1280, 720)
        rounds = max(1, min(6, int(max_rounds or 2)))

        def notify(msg):
            if status_callback:
                try:
                    status_callback(msg)
                except Exception:
                    pass

        if not prompt and text_fallback:
            prompt = f"Photorealistic cinematic photography representing this narration: {text_fallback[:220]}"

        base_prompt = (prompt or "").strip()
        if not base_prompt:
            notify("Sem prompt de imagem válido para esta cena.")
            return None

        providers = [
            "edenai", "openai_direct", "leonardo",
            "pollinations_flux", "pollinations_turbo", "pollinations",
        ]
        final_prompt = (
            f"{base_prompt}. Must align with the narration context. "
            "Photorealistic cinematic photography, natural lighting, pleasant mood. "
            "Realistic humans (no dolls), natural skin, proportional anatomy. "
            "Avoid close-up portraits. "
            "No sci-fi, no futuristic, no cyberpunk, no robots, no androids, no cyborgs, no machinery, no laboratory, no wires. "
            "No horror, no monsters, no zombies, no undead, no gore, no blood. "
            "No creepy, no uncanny, no doll-like. "
            "No deformed, no disfigured, no mutated, no bad anatomy, no extra limbs, no bad hands, no extra fingers, no melted face, no distorted faces. "
            "No dystopian, no apocalyptic. "
            "No text, no watermark, no logo."
        )

        for round_idx in range(1, rounds + 1):
            notify(f"Tentando gerar imagem ({round_idx}/{rounds})...")
            for provider in providers:
                url = None
                if self.ai_service:
                    try:
                        url = self.ai_service.generate_image(
                            final_prompt,
                            aspect_ratio=aspect_ratio,
                            providers=[provider],
                            status_callback=notify
                        )
                    except Exception as e:
                        notify(f"Falha no provedor {provider}: {str(e)[:120]}")
                        url = None

                if not url:
                    continue

                path = self.download_image(url, retries=2)
                if path and os.path.exists(path) and os.path.getsize(path) > 1000:
                    notify(f"Imagem gerada com sucesso ({provider}).")
                    return path

                notify(f"Resposta inválida de {provider}; tentando próximo provedor.")

        notify("Provedores configurados falharam. Tentando Pollinations direto como último recurso...")
        for attempt in range(3):
            try:
                direct_url = self._pollinations_direct_url(base_prompt, aspect_ratio)
                path = self.download_image(direct_url, retries=2, timeout=90)
                if path and os.path.exists(path) and os.path.getsize(path) > 1000:
                    notify(f"Imagem obtida via Pollinations direto (tentativa {attempt+1}).")
                    return path
            except Exception as e:
                notify(f"Pollinations direto tentativa {attempt+1} falhou: {str(e)[:100]}")
            import time
            time.sleep(2)

        notify("Não foi possível gerar imagem personalizada após todas as tentativas.")
        if allow_non_ai_fallback:
            notify("Aplicando fallback local por configuração do ambiente.")
            return self._generate_fallback_background((width, height))
        return None

    def _set_clip_duration(self, clip, duration):
        """Compatível com MoviePy 1.x (set_duration) e 2.x (with_duration)."""
        if hasattr(clip, "with_duration"):
            return clip.with_duration(duration)
        return clip.set_duration(duration)

    def _set_clip_audio(self, clip, audio_clip):
        """Compatível com MoviePy 1.x (set_audio) e 2.x (with_audio)."""
        if hasattr(clip, "with_audio"):
            return clip.with_audio(audio_clip)
        return clip.set_audio(audio_clip)

    def _clip_from_rgba(self, rgba_arr, duration):
        try:
            from moviepy.editor import ImageClip
        except Exception:
            from moviepy import ImageClip
        rgb = rgba_arr[:, :, :3]
        alpha = (rgba_arr[:, :, 3].astype("float32") / 255.0)
        base = ImageClip(rgb)
        mask = None
        try:
            mask = ImageClip(alpha, ismask=True)
        except Exception:
            try:
                mask = ImageClip(alpha)
            except Exception:
                mask = None
        if mask is not None:
            if hasattr(base, "with_mask"):
                base = base.with_mask(mask)
            else:
                base = base.set_mask(mask)
        base = self._set_clip_duration(base, duration)
        if mask is not None:
            try:
                mask = self._set_clip_duration(mask, duration)
            except Exception:
                pass
        return base

    def _subclip(self, clip, start_t, end_t):
        """Compatível com MoviePy 1.x (subclip) e 2.x (subclipped)."""
        if hasattr(clip, "subclip"):
            return clip.subclip(start_t, end_t)
        if hasattr(clip, "subclipped"):
            return clip.subclipped(start_t, end_t)
        raise AttributeError("Objeto de clip sem subclip/subclipped")

    def _apply_ken_burns(self, clip, size, zoom_factor=1.15):
        """
        Aplica efeito suave de zoom (Ken Burns) em um ImageClip.
        """
        try:
            w, h = size
            # Função de transformação para zoom
            def resize_func(t):
                # Zoom linear de 1.0 até zoom_factor ao longo da duração do clip
                current_zoom = 1 + (zoom_factor - 1) * (t / clip.duration)
                return current_zoom

            # Aplica o resize animado e centraliza
            # Nota: Isso pode ser custoso para processar. Se der timeout, simplificar.
            # Alternativa mais leve: Apenas um crop variável se a imagem for maior que o vídeo
            zoomed = clip.resized(resize_func) if hasattr(clip, "resized") else clip.resize(resize_func)
            if hasattr(zoomed, "with_position"):
                return zoomed.with_position('center')
            return zoomed.set_position('center')
        except Exception as e:
            print(f"Erro ao aplicar Ken Burns: {e}")
            return clip

    def _resolve_input_image_path(self, value: str) -> str:
        v = (value or "").strip()
        if not v:
            return ""
        v = v.replace("\\", "/").split("?", 1)[0].split("#", 1)[0].strip()
        if not v:
            return ""

        if v.startswith("/static/"):
            rel = v.replace("/static/", "", 1).lstrip("/")
            candidate = os.path.join("app", "static", rel)
            if os.path.exists(candidate):
                return candidate

        if v.startswith("static/"):
            candidate = os.path.join("app", v)
            if os.path.exists(candidate):
                return candidate

        if v.startswith("app/static/"):
            candidate = v
            if os.path.exists(candidate):
                return candidate

        if os.path.isabs(v) and os.path.exists(v):
            return v

        if v.startswith("http://") or v.startswith("https://"):
            try:
                path = self.download_image(v, retries=1)
                if path and os.path.exists(path) and os.path.getsize(path) > 1000:
                    return path
            except Exception:
                return ""

        candidate = os.path.join("app", "static", v.lstrip("/"))
        if os.path.exists(candidate):
            return candidate
        return ""

    def create_video_from_plan(self, plan, cover_image_path=None, aspect_ratio="9:16", progress_callback=None, voice_style=None, voice_gender=None, music_file_path=None):
        """Gera vídeo complexo com áudio e cenas a partir do plano da IA"""
        # Lazy imports: moviepy 1.x usa .editor, moviepy 2.x exporta direto de moviepy
        try:
            from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip, CompositeVideoClip, CompositeAudioClip, concatenate_audioclips, AudioClip
        except ImportError:
            from moviepy import ImageClip, concatenate_videoclips, AudioFileClip, CompositeVideoClip, CompositeAudioClip, concatenate_audioclips, AudioClip
        import numpy as np

        if progress_callback:
            progress_callback(0, "Iniciando composição do vídeo...")
            
        clips = []
        final_clip = None
        bg_music = None
        allow_non_ai_fallback = os.getenv("ALLOW_NON_AI_IMAGE_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}
        image_max_rounds = int((os.getenv("IMAGE_MAX_ROUNDS") or "2").strip() or "2")
        image_cache = {}
        cached_temp_paths = set()
        fallback_bg_path = None
        use_single_bg = (os.getenv("VIDEO_SINGLE_BG") or "true").strip().lower() in {"1", "true", "yes", "on"}
        video_bg_path = None
        video_bg_paths = []
        video_bg_frame = None
        used_image_urls = []
        used_image_url_set = set()

        def _track_image_path(p: str):
            try:
                if not p or not isinstance(p, str):
                    return
                pp = os.path.abspath(p)
                static_root = os.path.abspath(os.path.join("app", "static"))
                if not pp.startswith(static_root):
                    return
                rel = pp[len(static_root):].lstrip(os.sep).replace(os.sep, "/")
                if not rel:
                    return
                url = f"/static/{rel}"
                if url not in used_image_url_set:
                    used_image_url_set.add(url)
                    used_image_urls.append(url)
            except Exception:
                return
        
        try:
            title = plan.get('title', 'Vídeo Sem Título')
            try:
                plan = self.review_plan(plan)
            except Exception:
                pass
            raw_scenes = plan.get('scenes', [])
            
            # Validação extra: Se 'scenes' não for lista, tenta corrigir ou usa lista vazia
            if not isinstance(raw_scenes, list):
                print(f"ALERTA: 'scenes' não é lista. Tipo: {type(raw_scenes)}. Valor: {raw_scenes}")
                if isinstance(raw_scenes, str):
                    # Pode ser que a IA retornou uma string única como cena
                    raw_scenes = [{"text": raw_scenes, "image_prompt": ""}]
                else:
                    raw_scenes = []

            def _materialize_scenes(raw_list):
                scenes_local = []
                if music_file_path:
                    return raw_list if isinstance(raw_list, list) else []
                if not isinstance(raw_list, list):
                    return []
                for scene in raw_list:
                    scene_text = ""
                    scene_prompt = ""

                    if isinstance(scene, str):
                        scene_text = scene
                    elif isinstance(scene, dict):
                        scene_text = scene.get('text', '')
                        scene_prompt = scene.get('image_prompt', '')
                    else:
                        scene_text = str(scene)

                    scene_text = (scene_text or "").strip()
                    if not scene_text:
                        continue

                    if not scene_prompt and scene_text:
                        scene_prompt = f"Photorealistic cinematic photography representing: {scene_text[:140]}"

                    split_threshold = int((os.getenv("SCENE_TEXT_SPLIT_THRESHOLD") or "320").strip() or "320")
                    target_chars = int((os.getenv("SCENE_TEXT_TARGET_CHARS") or "240").strip() or "240")
                    target_chars = max(160, min(800, target_chars))

                    if len(scene_text) > split_threshold:
                        parts = re.split(r'(?<=[.!?])\s+', scene_text)
                        buf = ""
                        for part in parts:
                            p = (part or "").strip()
                            if not p:
                                continue
                            if not buf:
                                buf = p
                                continue
                            if len(buf) + 1 + len(p) <= target_chars:
                                buf = f"{buf} {p}"
                                continue
                            scenes_local.append({"text": buf.strip(), "image_prompt": scene_prompt})
                            buf = p
                        if buf.strip():
                            scenes_local.append({"text": buf.strip(), "image_prompt": scene_prompt})
                    else:
                        scenes_local.append({"text": scene_text, "image_prompt": scene_prompt})
                return scenes_local

            scenes = _materialize_scenes(raw_scenes)
            if not scenes and not music_file_path:
                alt_raw = plan.get("blocks") or plan.get("segments") or plan.get("parts") or plan.get("chapters") or []
                if isinstance(alt_raw, list) and alt_raw:
                    raw_scenes = alt_raw
                    scenes = _materialize_scenes(raw_scenes)

            if not scenes and not music_file_path:
                base_text = ""
                for k in (
                    "roteiro",
                    "script",
                    "text",
                    "content",
                    "story_content",
                    "narration_text",
                    "narration",
                    "raw_text",
                    "raw_script",
                    "full_text",
                ):
                    v = plan.get(k)
                    if isinstance(v, str) and v.strip():
                        base_text = v.strip()
                        break
                if base_text:
                    raw_scenes = [{"text": base_text, "image_prompt": plan.get("image_prompt") or ""}]
                    scenes = _materialize_scenes(raw_scenes)

            if not scenes and not music_file_path:
                fallback_text = ""
                try:
                    desc = (plan.get("description") or "").strip() if isinstance(plan, dict) else ""
                except Exception:
                    desc = ""
                if isinstance(title, str) and title.strip():
                    fallback_text = title.strip()
                if desc:
                    fallback_text = (fallback_text + "\n\n" + desc).strip()
                if not fallback_text:
                    fallback_text = "Conteúdo em preparação."
                scenes = [{"text": fallback_text, "image_prompt": plan.get("image_prompt") if isinstance(plan, dict) else ""}]

            # Enriquecimento: IA gera image_prompts profissionais com base na narração (imagens próprias para vídeo profissional)
            # Skip enrichment if music mode (handled by generator) or if images were preselected
            selected_raw_pre = plan.get("selected_images") or plan.get("images") or []
            has_preselected_images = isinstance(selected_raw_pre, list) and any(isinstance(x, str) and x.strip() for x in selected_raw_pre)
            if self.ai_service and scenes and not music_file_path and not has_preselected_images:
                try:
                    enriched = self.ai_service.enrich_scenes_with_image_prompts({"title": title, "scenes": scenes})
                    if enriched and enriched.get("scenes"):
                        scenes = enriched["scenes"]
                except Exception as e:
                    print(f"Aviso: enriquecimento de image_prompts falhou, usando prompts existentes: {e}")

            # Otimização de memória: Reduzir resolução para 720p para evitar OOM em tiers gratuitos
            if aspect_ratio == "16:9":
                video_size = (1280, 720) # Antes: 1920, 1080
            else:
                video_size = (720, 1280) # Antes: 1080, 1920

            selected_image_paths = []
            selected_primary_path = None
            selected_raw = plan.get("selected_images") or plan.get("images") or []
            if isinstance(selected_raw, list):
                for item in selected_raw:
                    if not isinstance(item, str):
                        continue
                    p = self._resolve_input_image_path(item)
                    if p and os.path.exists(p):
                        selected_image_paths.append(p)
            if selected_image_paths:
                selected_primary_path = selected_image_paths[0]
                if not music_file_path:
                    use_single_bg = False

            if use_single_bg and scenes and not music_file_path:
                try:
                    first_txt = ""
                    s0 = scenes[0]
                    if isinstance(s0, dict):
                        first_txt = (s0.get("text") or "").strip()
                    else:
                        first_txt = str(s0).strip()

                    pool_size_raw = (os.getenv("VIDEO_BG_POOL_SIZE") or "").strip()
                    try:
                        pool_size = int(pool_size_raw) if pool_size_raw else 3
                    except Exception:
                        pool_size = 3
                    pool_size = max(1, min(5, pool_size))

                    base_for_bg = ((title or "") + "\n\n" + (first_txt or "")).strip()
                    if isinstance(plan, dict) and isinstance(plan.get("description"), str) and plan.get("description").strip():
                        base_for_bg = (base_for_bg + "\n\n" + plan.get("description").strip()).strip()

                    prompts = []
                    bg_prompt = plan.get("background_prompt") if isinstance(plan, dict) else None
                    if isinstance(bg_prompt, str) and bg_prompt.strip():
                        prompts = [bg_prompt.strip()]
                    elif self.ai_service and hasattr(self.ai_service, "generate_image_prompts_from_text"):
                        try:
                            prompts = self.ai_service.generate_image_prompts_from_text(
                                base_for_bg or title or first_txt or "",
                                count=pool_size,
                                kind=(plan.get("kind") if isinstance(plan, dict) else None),
                            )
                        except Exception:
                            prompts = []
                    if not prompts:
                        prompts = [
                            f"{title}. Photorealistic cinematic background representing the story. Warm natural light, pleasant mood. {first_txt[:220]}",
                            f"{title}. Photorealistic cinematic landscape illustrating the message. Realistic, peaceful, uplifting atmosphere. {first_txt[:220]}",
                            f"{title}. Photorealistic cinematic scene with realistic people, kind expressions, hopeful mood. {first_txt[:220]}",
                        ][:pool_size]

                    def _bg_status(message: str):
                        if progress_callback:
                            progress_callback(8, f"Fundo do vídeo: {message}")

                    video_bg_paths = []
                    for pidx, ptxt in enumerate(prompts[:pool_size]):
                        _bg_status(f"Gerando fundo {pidx+1}/{pool_size}...")
                        path = self._ensure_image_for_scene(
                            ptxt,
                            text_fallback=(first_txt or title)[:220],
                            aspect_ratio=aspect_ratio,
                            status_callback=_bg_status,
                            max_rounds=image_max_rounds,
                            allow_non_ai_fallback=allow_non_ai_fallback,
                        )
                        if not path:
                            try:
                                direct_url = self._pollinations_direct_url(
                                    ptxt or f"Photorealistic cinematic background for: {title}",
                                    aspect_ratio,
                                )
                                path = self.download_image(direct_url, retries=2, timeout=90)
                                if not (path and os.path.exists(path) and os.path.getsize(path) > 1000):
                                    path = None
                            except Exception:
                                path = None
                        if path:
                            video_bg_paths.append(path)
                            _track_image_path(path)

                    if not video_bg_paths:
                        video_bg_path = self._generate_fallback_background(video_size)
                        if video_bg_path:
                            video_bg_paths = [video_bg_path]
                            _track_image_path(video_bg_path)

                    if video_bg_paths:
                        video_bg_path = video_bg_paths[0]
                        if len(video_bg_paths) == 1:
                            video_bg_frame = self.create_text_image(
                                "",
                                size=video_size,
                                bg_color=(20, 20, 20),
                                text_color=(255, 255, 255),
                                bg_image_path=video_bg_path,
                            )
                        else:
                            video_bg_frame = None
                except Exception:
                    video_bg_path = None
                    video_bg_frame = None
                    video_bg_paths = []

            # --- MODO MÚSICA ---
            if music_file_path and os.path.exists(music_file_path):
                if progress_callback:
                    progress_callback(10, "Modo Música: Preparando áudio e imagens...")
                
                # Carregar áudio principal
                main_audio = AudioFileClip(music_file_path)
                
                # Gerar clips visuais sincronizados
                for i, scene in enumerate(scenes):
                    if progress_callback:
                        progress_callback(10 + int((i / len(scenes)) * 60), f"Gerando cena {i+1}/{len(scenes)}...")
                    
                    # Duration comes from the plan
                    duration = scene.get('duration', 5)
                    image_prompt = scene.get('image_prompt', '')
                    if not image_prompt:
                         image_prompt = f"Scene for {title}, photorealistic, cinematic"

                    def _music_status(message, scene_idx=i, total=len(scenes)):
                        if progress_callback:
                            progress_callback(
                                10 + int((scene_idx / max(1, total)) * 60),
                                f"Cena {scene_idx+1}/{total}: {message}"
                            )

                    img_path = self._ensure_image_for_scene(
                        image_prompt,
                        text_fallback=title,
                        aspect_ratio=aspect_ratio,
                        status_callback=_music_status,
                        max_rounds=image_max_rounds,
                        allow_non_ai_fallback=allow_non_ai_fallback
                    )
                    
                    if not img_path:
                        try:
                            direct_url = self._pollinations_direct_url(
                                image_prompt or f"Cinematic scene for music video: {title}",
                                aspect_ratio
                            )
                            img_path = self.download_image(direct_url, retries=2, timeout=90)
                            if not (img_path and os.path.exists(img_path) and os.path.getsize(img_path) > 1000):
                                img_path = None
                        except Exception:
                            img_path = None
                    if not img_path:
                        img_path = self._generate_fallback_background(video_size)
                        if img_path and progress_callback:
                            progress_callback(
                                10 + int((i / max(1, len(scenes))) * 60),
                                f"Cena {i+1}/{len(scenes)}: IA de imagem indisponível; usando fundo local."
                            )

                    if img_path and os.path.exists(img_path):
                        # Criar clip
                        clip = self._set_clip_duration(ImageClip(img_path), duration)
                        clip = self._apply_ken_burns(clip, video_size)
                        clips.append(clip)
                    else:
                        raise Exception("Falha ao gerar imagem da cena musical.")
                
                # Concatenar clips visuais
                if clips:
                    transition_sec = 0.25
                    if len(clips) > 1:
                        faded = []
                        for idx, c in enumerate(clips):
                            if idx > 0 and hasattr(c, "crossfadein"):
                                try:
                                    c = c.crossfadein(transition_sec)
                                except Exception:
                                    pass
                            faded.append(c)
                        clips = faded
                        try:
                            final_video = concatenate_videoclips(clips, method="compose", padding=-transition_sec)
                        except Exception:
                            final_video = concatenate_videoclips(clips, method="compose")
                    else:
                        final_video = concatenate_videoclips(clips, method="compose")
                    
                    # Ajustar áudio: Se vídeo for menor que áudio, corta áudio. Se vídeo for maior, loop ou corta vídeo.
                    # Vamos cortar o vídeo para bater com o áudio ou vice-versa.
                    # Preferência: Vídeo segue o áudio (mas o plano visual já foi calculado para bater aprox.)
                    
                    if final_video.duration > main_audio.duration:
                        final_video = self._subclip(final_video, 0, main_audio.duration)
                    else:
                        # Se vídeo ficou menor (arredondamentos), corta o áudio
                        main_audio = self._subclip(main_audio, 0, final_video.duration)
                        
                    final_video = self._set_clip_audio(final_video, main_audio)
                    
                    # Exportar
                    output_filename = f"music_video_{uuid.uuid4().hex}.mp4"
                    output_path = os.path.join(self.output_dir, output_filename)
                    
                    if progress_callback:
                        progress_callback(80, "Renderizando vídeo final...")
                        
                    final_video.write_videofile(
                        output_path,
                        fps=24,
                        codec='libx264',
                        audio_codec='aac',
                        threads=1,
                        ffmpeg_params=["-preset", "ultrafast", "-movflags", "+faststart", "-pix_fmt", "yuv420p"]
                    )
                    output_path = self._ensure_playable_mp4(output_path)
                    
                    # Cleanup
                    try:
                        main_audio.close()
                        final_video.close()
                        for c in clips: c.close()
                    except:
                        pass
                        
                    return {"video_url": f"{VIDEO_URL_PREFIX}/{output_filename}", "file_path": output_path}
                    
                else:
                    raise Exception("Nenhuma cena visual gerada para o clipe musical.")

            # --- MODO NORMAL (NARRADO) ---
            # 1. Slide de Título (Com capa se disponível)
            if progress_callback:
                progress_callback(5, "Criando slide de título...")
                
            # Limpeza do título para evitar mostrar créditos ou URLs
            clean_title = title
            if "Music:" in clean_title:
                clean_title = clean_title.split("Music:")[0].strip()
            if "http" in clean_title:
                clean_title = clean_title.split("http")[0].strip()
            # Limita tamanho do título no slide
            if len(clean_title) > 100:
                clean_title = clean_title[:97] + "..."

            title_audio_path = self.generate_audio(clean_title, voice_style=voice_style, voice_gender=voice_gender)
            
            start_bg_path = selected_primary_path if selected_primary_path and os.path.exists(selected_primary_path) else None
            if not start_bg_path:
                start_bg_path = cover_image_path if cover_image_path and os.path.exists(cover_image_path) else None
            if not start_bg_path and video_bg_path and os.path.exists(video_bg_path):
                start_bg_path = video_bg_path
            _track_image_path(start_bg_path)
            img_title = self.create_text_image(clean_title, size=video_size, bg_color=(20, 20, 20), bg_image_path=start_bg_path)
            
            clip_title = ImageClip(img_title)
            
            if title_audio_path:
                audio_clip = AudioFileClip(title_audio_path)
                # Adiciona um pouco de tempo extra
                clip_title = clip_title.with_duration(audio_clip.duration + 1.5)
                clip_title = clip_title.with_audio(audio_clip)
            else:
                clip_title = clip_title.with_duration(3)
                
            clips.append(clip_title)
            
            # 2. Cenas
            total_scenes = len(scenes)
            for i, scene in enumerate(scenes):
                scene_progress = 10 + int((i / total_scenes) * 70)
                if progress_callback:
                    progress_callback(scene_progress, f"Processando cena {i+1} de {total_scenes}...")
                    
                if isinstance(scene, str):
                    text = scene
                    # Auto-generate prompt for text-only scenes to ensure visuals
                    image_prompt = f"Photorealistic cinematic photography representing: {text[:100]}"
                else:
                    text = scene.get('text', '')
                    image_prompt = scene.get('image_prompt', '')
                    
                    # Fallback: Se não houver prompt de imagem, cria um baseado no texto
                    if not image_prompt and text:
                        print(f"Aviso: Cena {i+1} sem image_prompt. Criando a partir do texto.")
                        image_prompt = f"Photorealistic cinematic photography representing: {text[:100]}"

                # Limpeza de segurança para evitar metadados no vídeo
                clean_text = self._clean_text(text)
                
                # GARANTIA DE IMAGEM (Substitui lógica antiga)
                def _scene_status(message, scene_idx=i, total=total_scenes, pct=scene_progress):
                    if progress_callback:
                        progress_callback(pct, f"Cena {scene_idx+1}/{total}: {message}")

                bg_image_path = None
                prompt_key = None
                if selected_image_paths:
                    bg_image_path = selected_image_paths[i % len(selected_image_paths)]
                elif use_single_bg and video_bg_paths:
                    try:
                        import random
                        bg_image_path = random.choice(video_bg_paths)
                    except Exception:
                        bg_image_path = video_bg_paths[0]
                else:
                    prompt_key = (
                        str(aspect_ratio).strip(),
                        (image_prompt or "").strip().lower() or clean_text[:220].strip().lower(),
                    )
                    cached = image_cache.get(prompt_key)
                    if cached and os.path.exists(cached):
                        bg_image_path = cached
                    else:
                        bg_image_path = self._ensure_image_for_scene(
                            image_prompt,
                            text_fallback=clean_text,
                            aspect_ratio=aspect_ratio,
                            status_callback=_scene_status,
                            max_rounds=image_max_rounds,
                            allow_non_ai_fallback=allow_non_ai_fallback
                        )

                if not bg_image_path:
                    if progress_callback:
                        progress_callback(scene_progress, f"Cena {i+1}/{total_scenes}: Tentando Pollinations direto como último recurso...")
                    try:
                        direct_url = self._pollinations_direct_url(
                            image_prompt or f"Cinematic illustration for: {clean_text[:140]}",
                            aspect_ratio
                        )
                        bg_image_path = self.download_image(direct_url, retries=2, timeout=90)
                        if bg_image_path and os.path.exists(bg_image_path) and os.path.getsize(bg_image_path) > 1000:
                            _track_image_path(bg_image_path)
                        else:
                            bg_image_path = None
                    except Exception:
                        bg_image_path = None

                if not bg_image_path:
                    if not fallback_bg_path or not os.path.exists(fallback_bg_path):
                        fallback_bg_path = self._generate_fallback_background(video_size)
                    bg_image_path = fallback_bg_path
                    if bg_image_path and progress_callback:
                        progress_callback(scene_progress, f"Cena {i+1}/{total_scenes}: IA de imagem indisponível; usando fundo local.")
                if not bg_image_path:
                    raise Exception(f"Falha ao gerar imagem da cena {i+1}.")
                try:
                    if prompt_key and not (use_single_bg and video_bg_path):
                        image_cache[prompt_key] = bg_image_path
                except Exception:
                    pass
                _track_image_path(bg_image_path)

                # Fallback colors
                bg_colors = [(30, 30, 30), (0, 30, 60), (60, 0, 30), (30, 60, 0)]
                bg_color = bg_colors[i % len(bg_colors)]
                
                # Gerar Audio da cena
                audio_path = self.generate_audio(clean_text, voice_style=voice_style, voice_gender=voice_gender)
                
                screen_text = ""
                if isinstance(scene, dict):
                    screen_text = (scene.get("caption") or scene.get("on_screen_text") or "").strip()
                if not screen_text:
                    screen_text = self._make_caption(clean_text)

                if use_single_bg and video_bg_frame is not None:
                    bg_frame = video_bg_frame
                else:
                    bg_frame = self.create_text_image("", size=video_size, bg_color=bg_color, bg_image_path=bg_image_path)

                bg_clip = ImageClip(bg_frame)
                if audio_path:
                    audio_clip_scene = AudioFileClip(audio_path)
                    scene_dur = float(audio_clip_scene.duration or 0) + 0.5
                else:
                    audio_clip_scene = None
                    scene_dur = 5

                if hasattr(bg_clip, "with_duration"):
                    bg_clip = bg_clip.with_duration(scene_dur)
                else:
                    bg_clip = bg_clip.set_duration(scene_dur)
                bg_clip = self._apply_ken_burns(bg_clip, video_size, zoom_factor=1.08)

                overlay_arr = self.create_text_overlay(screen_text, size=video_size, text_color=(255, 255, 255))
                overlay_clip = self._clip_from_rgba(overlay_arr, scene_dur)
                clip_scene = CompositeVideoClip([bg_clip, overlay_clip], size=video_size)
                
                if audio_clip_scene:
                    if hasattr(clip_scene, "with_audio"):
                        clip_scene = clip_scene.with_audio(audio_clip_scene)
                    else:
                        clip_scene = clip_scene.set_audio(audio_clip_scene)
                else:
                    print(f"AVISO: Cena {i+1} sem áudio gerado. Mantendo duração padrão.")
                    
                clips.append(clip_scene)
                
                # Limpeza de imagens temporárias
                if bg_image_path and "temp_" in bg_image_path and bg_image_path not in cached_temp_paths:
                    try:
                        os.remove(bg_image_path)
                    except Exception:
                        pass
                        
                # Memory Cleanup entre cenas
                gc.collect()
                
            # 3. Slide Final (CTA)
            if progress_callback:
                progress_callback(85, "Criando slide final...")
                
            end_text = "Inscreva-se no Canal!\nLink na Bio."
            audio_end_path = self.generate_audio("Inscreva-se no canal e ative o sininho.", voice_style=voice_style, voice_gender=voice_gender)
            
            end_bg_path = cover_image_path if cover_image_path and os.path.exists(cover_image_path) else None
            if not end_bg_path and video_bg_path and os.path.exists(video_bg_path):
                end_bg_path = video_bg_path
            _track_image_path(end_bg_path)
            img_end = self.create_text_image(end_text, size=video_size, bg_color=(20, 20, 20), bg_image_path=end_bg_path)
            
            clip_end = ImageClip(img_end)
            
            if audio_end_path:
                audio_clip_end = AudioFileClip(audio_end_path)
                clip_end = clip_end.with_duration(audio_clip_end.duration + 1)
                clip_end = clip_end.with_audio(audio_clip_end)
            else:
                clip_end = clip_end.with_duration(3)
                
            clips.append(clip_end)
            
            # Concatenar todos
            transition_sec = 0.25
            if len(clips) > 1:
                faded = []
                for idx, c in enumerate(clips):
                    if idx > 0 and hasattr(c, "crossfadein"):
                        try:
                            c = c.crossfadein(transition_sec)
                        except Exception:
                            pass
                    faded.append(c)
                clips = faded
                try:
                    final_clip = concatenate_videoclips(clips, method="compose", padding=-transition_sec)
                except Exception:
                    final_clip = concatenate_videoclips(clips, method="compose")
            else:
                final_clip = concatenate_videoclips(clips, method="compose")
            
            # 4. Adicionar Música de Fundo
            if progress_callback:
                progress_callback(90, "Adicionando trilha sonora...")
                
            music_mood = plan.get('music_mood', 'drama')
            music_path = None
            used_music_credit = None
            
            # Tenta gerar música exclusiva com IA
            if self.ai_service:
                print(f"Gerando música exclusiva para mood: {music_mood}...")
                music_content = self.ai_service.generate_music(f"{music_mood} style, inspired by {title}")
                if music_content:
                    filename = f"music_{uuid.uuid4()}.wav" 
                    generated_music_path = os.path.join(self.output_dir, filename)
                    with open(generated_music_path, "wb") as f:
                        f.write(music_content)
                    music_path = generated_music_path
            
            # Se falhou ou não tem IA, usa biblioteca local
            if not music_path or not os.path.exists(music_path):
                 self._ensure_fallback_music()
                 local_path = os.path.join("app/static/music", f"{music_mood}.mp3")
                 if os.path.exists(local_path):
                     music_path = local_path
                 else:
                     try:
                         import glob
                         mp3_files = glob.glob("app/static/music/*.mp3")
                         if mp3_files:
                             music_path = mp3_files[0]
                             print(f"Usando música fallback genérica: {music_path}")
                     except Exception as e:
                         print(f"Erro ao procurar fallback de música: {e}")
            
            if music_path and os.path.exists(music_path):
                if not used_music_credit:
                    filename = os.path.basename(music_path).lower()
                    for key, credit in self.MUSIC_CREDITS.items():
                        if key in filename:
                            used_music_credit = credit
                            break

                try:
                    bg_music = AudioFileClip(music_path)
                    
                    if bg_music.duration < final_clip.duration:
                        num_loops = int(final_clip.duration / bg_music.duration) + 1
                        bg_music = concatenate_audioclips([bg_music] * num_loops)
                    
                    bg_music = bg_music.with_duration(final_clip.duration)
                    bg_music = bg_music.with_volume_scaled(0.1)
                    
                    if final_clip.audio:
                        final_audio = CompositeAudioClip([bg_music, final_clip.audio])
                    else:
                        final_audio = bg_music
                        
                    final_clip = final_clip.with_audio(final_audio)
                except Exception as e:
                    print(f"Erro ao adicionar música de fundo: {e}")

            target_duration = plan.get("target_duration_sec")
            if target_duration:
                try:
                    target_duration = float(target_duration)
                except Exception:
                    target_duration = None
            if target_duration and target_duration > 1 and final_clip:
                try:
                    current = float(final_clip.duration or 0)
                except Exception:
                    current = 0
                if current > (target_duration + 0.5):
                    final_clip = self._subclip(final_clip, 0, target_duration)
                elif current and current < (target_duration - 0.5):
                    extra = target_duration - current
                    try:
                        frame_t = max(0, current - 0.02)
                        last_frame = final_clip.get_frame(frame_t)
                        freeze = self._set_clip_duration(ImageClip(last_frame), extra)

                        def _silence(_t):
                            return np.array([0.0, 0.0])

                        silence_audio = AudioClip(_silence, duration=extra, fps=44100)
                        freeze = self._set_clip_audio(freeze, silence_audio)

                        combined = concatenate_videoclips([final_clip, freeze], method="compose")
                        base_audio = final_clip.audio
                        if base_audio:
                            combined_audio = concatenate_audioclips([base_audio, silence_audio])
                        else:
                            combined_audio = silence_audio
                        final_clip = self._set_clip_audio(combined, combined_audio)
                    except Exception as e:
                        print(f"Aviso: não foi possível ajustar duração para {target_duration}s: {e}")

            # Output
            if progress_callback:
                progress_callback(95, "Renderizando arquivo final...")
                
            filename = f"{uuid.uuid4()}.mp4"
            output_path = os.path.join(self.output_dir, filename)
            
            # Logger customizado: durante write_videofile (etapa mais longa) pinga 95→99
            # para o progress_callback atualizar o DB e evitar timeout do monitor
            write_logger = None
            if progress_callback:
                try:
                    import proglog
                    class RenderProgressLogger(proglog.ProgressBarLogger):
                        def __init__(self, callback):
                            super().__init__()
                            self._cb = callback
                        def bars_callback(self, bar, attr, value, old_value=None):
                            super().bars_callback(bar, attr, value, old_value)
                            if not self._cb or bar not in self.bars:
                                return
                            total = self.bars[bar].get("total")
                            if total and value is not None:
                                pct = 95 + int(4 * (value / total))
                                try:
                                    self._cb(min(99, pct), "Renderizando arquivo final...")
                                except Exception:
                                    pass
                    write_logger = RenderProgressLogger(progress_callback)
                except Exception:
                    pass
            logger_kw = {"logger": write_logger} if write_logger else {}
            
            # Escreve o arquivo
            # threads=1 + preset ultrafast para reduzir memória e tempo (evita OOM no Render)
            print(f"Renderizando vídeo para: {output_path}")
            final_clip.write_videofile(
                output_path, fps=24, codec="libx264", audio_codec="aac", threads=1,
                ffmpeg_params=["-preset", "ultrafast", "-movflags", "+faststart", "-pix_fmt", "yuv420p"],
                **logger_kw
            )
            output_path = self._ensure_playable_mp4(output_path)
            
            abs_path = os.path.abspath(output_path)
            print(f"Vídeo salvo com sucesso em: {abs_path} (Size: {os.path.getsize(output_path)} bytes)")
            
            if progress_callback:
                progress_callback(100, "Vídeo renderizado com sucesso!")
            
            return {"video_url": f"{VIDEO_URL_PREFIX}/{filename}", "music_credit": used_music_credit, "used_images": used_image_urls}
            
        except Exception as e:
            print(f"Erro na geração do vídeo: {e}")
            raise e
        finally:
            # Resource Cleanup
            print("Limpando recursos de memória...")
            try:
                if final_clip:
                    final_clip.close()
                if bg_music:
                    bg_music.close()
                for clip in clips:
                    try:
                        clip.close()
                        if clip.audio:
                            clip.audio.close()
                    except:
                        pass
            except Exception as e:
                print(f"Erro ao limpar recursos: {e}")
                
            # Force GC
            gc.collect()
            try:
                for p in list(cached_temp_paths):
                    try:
                        if p and os.path.exists(p):
                            os.remove(p)
                    except Exception:
                        pass
            except Exception:
                pass

    def create_music_video(self, music_path, scenes=None, title="Música", aspect_ratio="9:16", lyrics: Optional[str] = None, author_text: Optional[str] = None, watermark_enabled: bool = True, sync_mode: str = "auto", progress_callback: Optional[Callable[[int, str], None]] = None):
        """Gera clipe (vídeo) com a música como áudio e cenas baseadas na letra. Sem TTS."""
        try:
            from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip, CompositeAudioClip, concatenate_audioclips
        except ImportError:
            from moviepy import ImageClip, concatenate_videoclips, AudioFileClip, CompositeAudioClip, concatenate_audioclips
        
        if not os.path.exists(music_path):
            raise FileNotFoundError(f"Arquivo de música não encontrado: {music_path}")
        video_size = (720, 1280) if aspect_ratio == "9:16" else (1280, 720)
        allow_non_ai_fallback = False
        clips = []
        try:
            audio_clip = AudioFileClip(music_path)
            total_duration = audio_clip.duration
            if progress_callback:
                try:
                    progress_callback(5, "Preparando áudio...")
                except Exception:
                    pass
            if scenes is None:
                scenes = []

            lyric_text = lyrics
            if isinstance(scenes, dict):
                lyric_text = lyric_text or scenes.get("lyrics")
                scenes = scenes.get("scenes") or []
            if not isinstance(scenes, list):
                scenes = []

            credit = None
            if watermark_enabled:
                at = (author_text or "").strip()
                if at:
                    credit = f"Autor: {at} • © {time.strftime('%Y')}"
                else:
                    credit = f"© {time.strftime('%Y')}"

            def _normalize(s: str) -> str:
                t = (s or "").strip().lower()
                if not t:
                    return ""
                t = unicodedata.normalize("NFKD", t)
                t = "".join(ch for ch in t if not unicodedata.combining(ch))
                t = re.sub(r"[^a-z0-9\s]", " ", t)
                t = re.sub(r"\s+", " ", t).strip()
                return t

            def _is_label_line(line: str) -> bool:
                if not line:
                    return False
                return bool(re.match(r"^(verso|refr[aã]o|pr[eé]-?refr[aã]o|ponte|intro|outro|coro|bridge|chorus)\b", line.strip(), flags=re.IGNORECASE))

            def _clean_lyrics_lines(lyrics_raw: str):
                raw = [l.strip() for l in (lyrics_raw or "").splitlines() if l.strip()]
                out = []
                for l in raw:
                    if _is_label_line(l):
                        continue
                    l2 = re.sub(r"^\[.*?\]\s*", "", l).strip()
                    if not l2:
                        continue
                    out.append(l2)
                return out

            lyrics_lines = _clean_lyrics_lines(lyric_text or "")

            max_scenes_env = os.getenv("MUSIC_CLIP_MAX_SCENES", "").strip()
            try:
                max_scenes = int(max_scenes_env) if max_scenes_env else 18
            except Exception:
                max_scenes = 18
            max_scenes = max(6, min(max_scenes, 40))

            strict_sync = (sync_mode or "auto").strip().lower() in {"perfect", "precise", "strict"}
            if strict_sync and not lyrics_lines:
                raise Exception("Para sincronização perfeita, informe a letra da música.")

            segments = None
            transcribe_error = None
            if self.ai_service and lyrics_lines:
                try:
                    if hasattr(self.ai_service, "transcribe_audio_segments_detailed"):
                        info = self.ai_service.transcribe_audio_segments_detailed(music_path, language="pt")
                        if isinstance(info, dict):
                            segments = info.get("segments")
                            transcribe_error = info.get("error")
                        else:
                            segments = None
                    else:
                        segments = self.ai_service.transcribe_audio_segments(music_path, language="pt")
                except Exception as e:
                    segments = None
                    transcribe_error = str(e)

            if strict_sync and not segments:
                if transcribe_error == "missing_api_key":
                    raise Exception("Para sincronização perfeita, configure a OpenAI API Key (openai_api_key em Configurações) para transcrever o áudio e sincronizar com a letra.")
                if transcribe_error == "file_not_found":
                    raise Exception("Para sincronização perfeita, o arquivo de áudio não foi encontrado no servidor.")
                if transcribe_error:
                    raise Exception(f"Para sincronização perfeita, falhou ao transcrever o áudio na OpenAI: {transcribe_error}")
                raise Exception("Para sincronização perfeita, não foi possível transcrever o áudio na OpenAI.")
            if progress_callback:
                try:
                    progress_callback(18, "Sincronizando letra e áudio...")
                except Exception:
                    pass

            def _merge_segments(segs, strict: bool = False):
                blocks = []
                cur = None
                for s in (segs or []):
                    if not isinstance(s, dict):
                        continue
                    try:
                        st = float(s.get("start"))
                        en = float(s.get("end"))
                    except Exception:
                        continue
                    if en <= st:
                        continue
                    txt = str(s.get("text") or "").strip()
                    if not txt:
                        continue
                    if cur is None:
                        cur = {"start": max(0.0, st), "end": en, "text": txt}
                    else:
                        cur["end"] = en
                        cur["text"] = (cur["text"] + " " + txt).strip()
                    dur = float(cur["end"]) - float(cur["start"])
                    if strict:
                        if dur >= 1.8 or len(cur["text"]) >= 44:
                            blocks.append(cur)
                            cur = None
                            continue
                    if dur >= 3.6 or len(cur["text"]) >= 84:
                        blocks.append(cur)
                        cur = None
                if cur is not None:
                    blocks.append(cur)
                if blocks:
                    if not strict:
                        if blocks[0]["start"] > 0.25:
                            blocks[0]["start"] = 0.0
                        if blocks[-1]["end"] < (total_duration - 0.2):
                            blocks[-1]["end"] = float(total_duration)
                return blocks

            def _align_blocks_to_lyrics(blocks, lines):
                if not blocks or not lines:
                    return {}
                b = blocks[:]
                l = lines[:]
                if len(b) > 120:
                    b = b[:120]
                    b[-1]["end"] = float(total_duration)
                if len(l) > 180:
                    l = l[:180]
                m = len(b)
                n = len(l)
                skip_block = 0.55
                skip_line = 0.25
                neg_inf = -1e18
                dp = [[neg_inf] * (n + 1) for _ in range(m + 1)]
                prev = [[None] * (n + 1) for _ in range(m + 1)]
                dp[0][0] = 0.0

                score_cache = {}

                def _score(i, j):
                    key = (i, j)
                    if key in score_cache:
                        return score_cache[key]
                    s1 = _normalize(b[i]["text"])
                    s2 = _normalize(l[j])
                    if not s1 or not s2:
                        sc = 0.0
                    else:
                        sc = difflib.SequenceMatcher(None, s1, s2).ratio()
                    score_cache[key] = sc
                    return sc

                for i in range(m + 1):
                    for j in range(n + 1):
                        base = dp[i][j]
                        if base <= neg_inf / 2:
                            continue
                        if i < m:
                            cand = base - skip_block
                            if cand > dp[i + 1][j]:
                                dp[i + 1][j] = cand
                                prev[i + 1][j] = (i, j, "skip_block")
                        if j < n:
                            cand = base - skip_line
                            if cand > dp[i][j + 1]:
                                dp[i][j + 1] = cand
                                prev[i][j + 1] = (i, j, "skip_line")
                        if i < m and j < n:
                            sc = _score(i, j)
                            cand = base + sc
                            if cand > dp[i + 1][j + 1]:
                                dp[i + 1][j + 1] = cand
                                prev[i + 1][j + 1] = (i, j, "match")

                best_j = 0
                best_val = neg_inf
                for j in range(n + 1):
                    if dp[m][j] > best_val:
                        best_val = dp[m][j]
                        best_j = j

                mapping = {}
                i = m
                j = best_j
                while i > 0 or j > 0:
                    step = prev[i][j]
                    if not step:
                        break
                    pi, pj, action = step
                    if action == "match":
                        mapping[pi] = pj
                    i, j = pi, pj
                return mapping

            timeline = []
            if strict_sync and segments and isinstance(segments, list) and lyrics_lines:
                lead_ms_raw = (os.getenv("MUSIC_CLIP_CAPTION_LEAD_MS") or "").strip()
                try:
                    lead_ms = float(lead_ms_raw) if lead_ms_raw else 80.0
                except Exception:
                    lead_ms = 80.0
                lead_sec = max(-0.9, min(0.9, lead_ms / 1000.0))

                blocks = _merge_segments(segments, strict=True)
                mapping = _align_blocks_to_lyrics(blocks, lyrics_lines)

                line_spans = {}
                for b_idx, line_idx in (mapping or {}).items():
                    if b_idx is None or line_idx is None:
                        continue
                    if b_idx < 0 or b_idx >= len(blocks):
                        continue
                    if line_idx < 0 or line_idx >= len(lyrics_lines):
                        continue
                    b = blocks[b_idx]
                    st = float(b.get("start") or 0.0)
                    en = float(b.get("end") or 0.0)
                    if en <= st:
                        continue
                    span = line_spans.get(line_idx)
                    if not span:
                        line_spans[line_idx] = {"start": st, "end": en}
                    else:
                        span["start"] = min(float(span["start"]), st)
                        span["end"] = max(float(span["end"]), en)

                spans = [None] * len(lyrics_lines)
                for li, sp in line_spans.items():
                    if not sp:
                        continue
                    sst = float(sp.get("start") or 0.0)
                    een = float(sp.get("end") or 0.0)
                    if lead_sec > 0.0:
                        sst = max(0.0, sst - lead_sec)
                        een = max(sst + 0.25, een - lead_sec)
                    spans[li] = {"start": sst, "end": een}

                def _fill_range(start_idx: int, end_idx: int, start_t: float, end_t: float):
                    count = (end_idx - start_idx) + 1
                    if count <= 0:
                        return
                    start_t = max(0.0, float(start_t or 0.0))
                    end_t = min(float(total_duration), float(end_t or 0.0))
                    if end_t <= start_t:
                        seg = 0.8
                        t0 = start_t
                        for k in range(count):
                            spans[start_idx + k] = {"start": t0, "end": min(float(total_duration), t0 + seg)}
                            t0 = min(float(total_duration), t0 + seg)
                        return
                    step = (end_t - start_t) / float(count)
                    t0 = start_t
                    for k in range(count):
                        st = t0
                        en = end_t if k == (count - 1) else (st + step)
                        spans[start_idx + k] = {"start": st, "end": en}
                        t0 = en

                first_known = None
                last_known = None
                for i in range(len(spans)):
                    if spans[i] is not None:
                        first_known = i
                        break
                for i in range(len(spans) - 1, -1, -1):
                    if spans[i] is not None:
                        last_known = i
                        break

                if first_known is None or last_known is None:
                    n = min(max_scenes, len(lyrics_lines))
                    if n <= 0:
                        n = 1
                    seg_dur = float(total_duration) / float(n)
                    t = 0.0
                    for i in range(n):
                        start = t
                        end = float(total_duration) if i == (n - 1) else min(float(total_duration), start + seg_dur)
                        t = end
                        cap = lyrics_lines[min(i, len(lyrics_lines) - 1)]
                        timeline.append({"start": start, "end": end, "caption": cap})
                else:
                    intro_end = float(spans[first_known]["start"])
                    if intro_end > 0.35:
                        timeline.append({"start": 0.0, "end": intro_end, "caption": ""})
                    if last_known < (len(spans) - 1):
                        _fill_range(last_known + 1, len(spans) - 1, float(spans[last_known]["end"]), float(total_duration))
                    for i in range(first_known, last_known + 1):
                        if spans[i] is not None:
                            continue
                        j = i - 1
                        while j >= 0 and spans[j] is None:
                            j -= 1
                        k = i + 1
                        while k < len(spans) and spans[k] is None:
                            k += 1
                        if j >= 0 and k < len(spans) and spans[j] is not None and spans[k] is not None:
                            _fill_range(i, k - 1, float(spans[j]["end"]), float(spans[k]["start"]))

                    for idx, cap in enumerate(lyrics_lines):
                        if idx < first_known:
                            continue
                        sp = spans[idx]
                        if not sp:
                            continue
                        timeline.append({"start": float(sp["start"]), "end": float(sp["end"]), "caption": cap})
            elif lyrics_lines:
                n = min(max_scenes, len(lyrics_lines))
                if n <= 0:
                    n = 1
                seg_dur = float(total_duration) / float(n)
                t = 0.0
                for i in range(n):
                    start = t
                    end = float(total_duration) if i == (n - 1) else min(float(total_duration), start + seg_dur)
                    t = end
                    cap = lyrics_lines[min(i, len(lyrics_lines) - 1)]
                    timeline.append({"start": start, "end": end, "caption": cap})
            else:
                n = max(1, len(scenes)) if scenes else 1
                seg_dur = float(total_duration) / float(n)
                for i in range(n):
                    start = float(i) * seg_dur
                    end = float(total_duration) if i == (n - 1) else float(i + 1) * seg_dur
                    sc = scenes[i] if i < len(scenes) else {}
                    cap = sc.get("text") if isinstance(sc, dict) else str(sc)
                    timeline.append({"start": start, "end": end, "caption": str(cap or "").strip()})

            limit = max_scenes
            if strict_sync and timeline and not str(timeline[0].get("caption") or "").strip():
                limit = min(60, max_scenes + 1)
            if len(timeline) > limit:
                timeline = timeline[:limit]
                timeline[-1]["end"] = float(total_duration)

            sum_prev = 0.0
            for it in timeline:
                dur = max(0.6, float(it["end"]) - float(it["start"]))
                it["duration"] = dur
                sum_prev += dur
            if timeline:
                drift = float(total_duration) - float(sum_prev)
                if abs(drift) > 0.35:
                    timeline[-1]["duration"] = max(0.8, float(timeline[-1]["duration"]) + drift)
            if progress_callback:
                try:
                    progress_callback(22, "Planejando cenas...")
                except Exception:
                    pass

            def _prompt_for_caption(caption: str) -> str:
                cap = (caption or "").strip()
                if not cap:
                    return "cinematic music video scene"
                norm_full = _normalize(lyric_text or "")
                norm_cap = _normalize(cap)
                is_christian = any(k in norm_full for k in [
                    "jesus", "cristo", "cruz", "calvario", "golgota", "ressuscitou", "ressurreicao",
                    "tumulo", "sepulcro", "sangue", "redencao", "salvacao", "mestre",
                    "coroa", "espinhos", "pregos", "cravo", "veu",
                    "pecado", "sacrificio", "agonia", "penalidade", "perdao", "paz", "vitoria",
                ]) or any(k in norm_cap for k in ["jesus", "cristo", "cruz", "calvario", "golgota", "ressuscitou", "sepulcro", "redencao", "salvacao", "pecado", "sacrificio"])

                def _scene_hint() -> str:
                    if any(k in norm_cap for k in ["calvario", "golgota"]):
                        return "A man carrying a wooden cross on a dusty road toward a hill outside ancient Jerusalem, Roman era, wide shot, dramatic sky, reverent."
                    if ("cruz" in norm_cap) and any(k in norm_cap for k in ["vitoria", "venceu", "morte", "paz"]):
                        return "A cross silhouette on a hill at sunrise, rays of light breaking through clouds, hope and victory, wide cinematic landscape."
                    if ("cruz" in norm_cap) and any(k in norm_cap for k in ["castigo", "paz", "perdao", "salvacao", "redencao"]):
                        return "A wooden cross on a hill with soft golden light, peaceful atmosphere, symbolism of salvation and redemption, wide cinematic landscape."
                    if any(k in norm_cap for k in ["pregos", "cravo", "cravos"]):
                        return "Close shot of iron nails and a wooden beam on the ground, symbolic and reverent, no violence, soft dramatic light."
                    if ("coroa" in norm_cap and "espinh" in norm_cap):
                        return "A crown of thorns resting on rough stone, warm backlight, reverent still life, cinematic."
                    if ("veu" in norm_cap and any(k in norm_cap for k in ["rasgou", "rasgado", "rasgando"])):
                        return "Inside the ancient temple, a large curtain tearing from top to bottom, a beam of light shining through, awe and reverence."
                    if any(k in norm_cap for k in ["tumulo", "sepulcro", "ressuscitou", "vazio"]):
                        return "An empty tomb with the stone rolled away at dawn, gentle golden light, peaceful and triumphant, biblical era."
                    if "multidao" in norm_cap or "gritava" in norm_cap:
                        return "A crowd in ancient Jerusalem watching in tension on a stone street, Roman era, wide shot, cinematic."
                    if any(k in norm_cap for k in ["pecado", "pena", "castigo", "condenacao"]):
                        return "A person kneeling in darkness with a soft beam of light and a cross in the distance, symbolism of forgiveness and grace, cinematic."
                    if "inferno" in norm_cap:
                        return "Chains breaking in a dark cave as bright light forms a cross shape, symbolism of victory over darkness, cinematic, non-graphic."
                    if any(k in norm_cap for k in ["liberto", "liberdade", "cura", "libertacao", "adoracao"]):
                        return "A modern worship scene with hands raised and a cross in the background, soft light, uplifting, cinematic, no text."
                    return ""

                base_style = "Photorealistic cinematic photography, natural lighting, wide shot, high detail, reverent, inspiring."
                if is_christian:
                    hint = _scene_hint() or "Reverent Christian biblical imagery about the crucifixion and resurrection, symbolic and non-graphic."
                    llm_scene = None
                    try:
                        if self.ai_service and hasattr(self.ai_service, "_load_config"):
                            try:
                                self.ai_service._load_config()
                            except Exception:
                                pass
                        can_llm = bool(getattr(self.ai_service, "openrouter_key", None)) and hasattr(self.ai_service, "_generate_text")
                        if can_llm:
                            sys_p = "Você é um diretor de fotografia. Gere apenas UMA frase em inglês descrevendo uma tomada cinematográfica para um videoclipe, alinhada à letra. Proibido gore/violência gráfica. Sem texto na imagem."
                            user_p = f"LETRA (pt-BR): {cap}\nGere a tomada (1 frase), estilo fotorealista, era bíblica quando aplicável."
                            out = (self.ai_service._generate_text(user_p, system_prompt=sys_p, temperature=0.3, json_mode=False) or "").strip()
                            if out and ("Simulação" not in out) and len(out) <= 260:
                                llm_scene = out
                    except Exception:
                        llm_scene = None
                    scene = llm_scene or hint
                    return f"{scene} {base_style} No text, no watermark, no logo."

                return f"Scene inspired by this lyric line (Portuguese): {cap[:180]}. {base_style} No text, no watermark, no logo."

            for i, it in enumerate(timeline):
                caption = (it.get("caption") or "").strip()
                duration = float(it.get("duration") or 0)
                if duration <= 0:
                    continue
                if i == 0 and strict_sync and not caption:
                    norm_full = _normalize(lyric_text or "")
                    is_christian_song = any(k in norm_full for k in ["jesus", "cristo", "cruz", "calvario", "golgota", "ressuscitou", "redencao", "salvacao", "sangue", "pecado", "sacrificio"])
                    opening = (
                        f"Cinematic opening shot for a Christian music video titled '{(title or 'Song')[:80]}'. "
                        "A wooden cross silhouette on a hill at sunrise, gentle rays of light, reverent, hopeful, non-graphic."
                        if is_christian_song
                        else f"Cinematic opening shot for a music video titled '{(title or 'Song')[:80]}', symbolic, inspiring, wide shot, non-graphic."
                    )
                    image_prompt = opening + " No text, no watermark, no logo."
                else:
                    image_prompt = _prompt_for_caption(caption)
                if progress_callback:
                    try:
                        total = max(1, len(timeline))
                        p = 25 + int((float(i) / float(total)) * 60.0)
                        progress_callback(min(90, max(0, p)), f"Gerando cena {i+1}/{total}...")
                    except Exception:
                        pass

                def _clip_status(message, scene_idx=i, total=len(timeline)):
                    print(f"[Clip][Cena {scene_idx+1}/{total}] {message}")

                bg_image_path = self._ensure_image_for_scene(
                    image_prompt,
                    text_fallback=caption[:120],
                    aspect_ratio=aspect_ratio,
                    status_callback=_clip_status,
                    allow_non_ai_fallback=allow_non_ai_fallback
                )
                if not bg_image_path:
                    raise Exception(f"Falha ao gerar imagem da cena {i+1}.")
                bg_colors = [(30, 30, 30), (0, 30, 60), (60, 0, 30)]
                bg_color = bg_colors[i % len(bg_colors)]
                if i == 0 and strict_sync and not caption:
                    title_text = (title or "Música").strip()
                    if title_text and len(title_text) > 110:
                        title_text = title_text[:107] + "..."
                    if watermark_enabled and (author_text or "").strip():
                        title_screen = f"{title_text}\n\n{(author_text or '').strip()}"
                    else:
                        title_screen = title_text
                    img = self.create_text_image(self._clean_text(title_screen), size=video_size, bg_color=bg_color, bg_image_path=bg_image_path, footer_text=credit)
                else:
                    img = self.create_text_image(self._clean_text(caption), size=video_size, bg_color=bg_color, bg_image_path=bg_image_path, footer_text=credit)
                clip = ImageClip(img)
                clip = self._set_clip_duration(clip, duration)
                clips.append(clip)
                if progress_callback:
                    try:
                        total = max(1, len(timeline))
                        p = 30 + int((float(i + 1) / float(total)) * 60.0)
                        progress_callback(min(92, max(0, p)), f"Cena {i+1}/{total} pronta.")
                    except Exception:
                        pass
                if bg_image_path and "temp_" in bg_image_path:
                    try:
                        os.remove(bg_image_path)
                    except Exception:
                        pass
                gc.collect()
            if progress_callback:
                try:
                    progress_callback(95, "Renderizando vídeo...")
                except Exception:
                    pass
            final = concatenate_videoclips(clips)
            final = self._set_clip_audio(final, audio_clip)
            filename = f"clip_{uuid.uuid4().hex[:8]}.mp4"
            output_path = os.path.join(self.output_dir, filename)
            ffmpeg_params = ["-preset", "ultrafast", "-movflags", "+faststart", "-pix_fmt", "yuv420p"]
            at = (author_text or "").strip()
            if watermark_enabled and at:
                year = time.strftime("%Y")
                ffmpeg_params += [
                    "-metadata", f"artist={at}",
                    "-metadata", f"copyright=© {year} {at}",
                    "-metadata", "comment=Video criado no Codexia",
                ]
            final.write_videofile(
                output_path,
                fps=24,
                codec="libx264",
                audio_codec="aac",
                threads=1,
                ffmpeg_params=ffmpeg_params,
                logger=None
            )
            output_path = self._ensure_playable_mp4(output_path)
            for c in clips:
                try:
                    c.close()
                except Exception:
                    pass
            final.close()
            audio_clip.close()
            return {"video_url": f"{VIDEO_URL_PREFIX}/{filename}"}
        except Exception as e:
            for c in clips:
                try:
                    c.close()
                except Exception:
                    pass
            raise e

    def generate_simple_video(self, title, script_lines, output_filename="video.mp4"):
        # Mantendo compatibilidade com código antigo se necessário
        plan = {
            "title": title,
            "scenes": [{"text": line} for line in script_lines if line.strip()]
        }
        result = self.create_video_from_plan(plan)
        # Mantém compatibilidade retornando apenas URL se for o esperado por chamadas antigas diretas
        # Mas vamos atualizar os chamadores para lidar com dict
        return result["video_url"]
