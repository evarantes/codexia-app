import os
import uuid
import requests
import gc
import threading
import asyncio
import re
import time

from app.config import VIDEO_OUTPUT_DIR, VIDEO_URL_PREFIX


class VideoGenerator:
    def __init__(self, output_dir=None, ai_service=None):
        self.output_dir = output_dir or VIDEO_OUTPUT_DIR
        os.makedirs(self.output_dir, exist_ok=True)
        self.music_dir = "app/static/music"
        os.makedirs(self.music_dir, exist_ok=True)
        self.ai_service = ai_service
        self.MUSIC_CREDITS = {
            "drama": "Music: Impact Prelude by Kevin MacLeod\nFree download: https://filmmusic.io/song/3900-impact-prelude\nLicense (CC BY 4.0): https://filmmusic.io/standard-license",
            "epic": "Music: Impact Andante by Kevin MacLeod\nFree download: https://filmmusic.io/song/3898-impact-andante\nLicense (CC BY 4.0): https://filmmusic.io/standard-license",
            "happy": "Music: Carefree by Kevin MacLeod\nFree download: https://filmmusic.io/song/3476-carefree\nLicense (CC BY 4.0): https://filmmusic.io/standard-license"
        }
        # self._ensure_fallback_music() removido do init para evitar delay no startup

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

    def create_text_image(self, text, size=(1080, 1920), bg_color=(20, 20, 20), text_color=(255, 255, 255), bg_image_path=None):
        """Cria uma imagem com texto centralizado usando Pillow, opcionalmente com imagem de fundo"""
        from PIL import Image, ImageDraw, ImageFont, ImageEnhance
        import textwrap
        import numpy as np
        
        if bg_image_path and os.path.exists(bg_image_path):
            try:
                img = Image.open(bg_image_path).convert('RGB')
                # Resize and crop to fill
                img_ratio = img.width / img.height
                target_ratio = size[0] / size[1]
                
                if img_ratio > target_ratio:
                    # Imagem mais larga que o alvo, corta as laterais
                    new_height = size[1]
                    new_width = int(new_height * img_ratio)
                    img = img.resize((new_width, new_height), Image.LANCZOS)
                    left = (new_width - size[0]) / 2
                    img = img.crop((left, 0, left + size[0], size[1]))
                else:
                    # Imagem mais alta que o alvo, corta topo/base
                    new_width = size[0]
                    new_height = int(new_width / img_ratio)
                    img = img.resize((new_width, new_height), Image.LANCZOS)
                    top = (new_height - size[1]) / 2
                    img = img.crop((0, top, size[0], top + size[1]))
                
                # Escurecer moderadamente a imagem para legibilidade do texto
                enhancer = ImageEnhance.Brightness(img)
                img = enhancer.enhance(0.8) # mantém legibilidade sem escurecer demais
            except Exception as e:
                print(f"Erro ao carregar imagem de fundo: {e}")
                img = Image.new('RGB', size, color=bg_color)
        else:
            img = Image.new('RGB', size, color=bg_color)

        d = ImageDraw.Draw(img)
        
        # Tenta carregar fonte com tamanho adequado para evitar texto minúsculo.
        try:
            font_size = max(34, min(72, int(size[0] * 0.055)))
            font_candidates = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "DejaVuSans-Bold.ttf",
                "arial.ttf",
            ]
            font = None
            for font_path in font_candidates:
                try:
                    font = ImageFont.truetype(font_path, font_size)
                    break
                except Exception:
                    continue
            if font is None:
                raise OSError("no truetype font found")
        except Exception:
            font = ImageFont.load_default()

        # Quebra o texto
        # Aumentado width para 40 caracteres para ocupar menos altura
        lines = textwrap.wrap(text, width=40) 
        
        # Calcula altura total do bloco de texto
        line_height = max(42, int(getattr(font, "size", 40) * 1.25))
        text_block_height = len(lines) * line_height
        
        # Posiciona no terço inferior (Subtitle style), mas com limite
        # Garante que não suba muito para o meio
        # Fixamos a base do texto a 100px do fundo
        margin_bottom = 150
        y_text = size[1] - text_block_height - margin_bottom
        
        # Se o texto for muito longo e subir demais, cortamos o topo (fallback)
        # Mas idealmente o texto deve ser curto.
        # Vamos desenhar o fundo preto
        
        # Fundo transparente (background box removido)
        
        for line in lines:
            bbox = d.textbbox((0, 0), line, font=font)
            text_width = bbox[2] - bbox[0]
            
            x = (size[0] - text_width) / 2
            # Desenha texto com leve sombra/outline para legibilidade extra
            # Outline
            for off in [(1,1), (-1,-1), (1,-1), (-1,1)]:
                d.text((x+off[0], y_text+off[1]), line, font=font, fill=(0,0,0))
                
            d.text((x, y_text), line, font=font, fill=text_color)
            y_text += line_height

        return np.array(img)

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

    def download_image(self, url, retries=3):
        import time
        try:
            import imghdr
        except ImportError:
            imghdr = None
        
        for attempt in range(retries):
            try:
                print(f"Baixando imagem de: {url[:50]}... (Tentativa {attempt+1}/{retries})")
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8'
                }
                # Verify=False somente se absolutamente necessário (risco de segurança, mas útil para debug em alguns envs)
                response = requests.get(url, headers=headers, stream=True, timeout=20)
                
                if response.status_code == 200:
                    filename = f"temp_{uuid.uuid4()}.png"
                    filepath = os.path.join(self.output_dir, filename)
                    
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
            
            filename = f"fallback_local_{uuid.uuid4()}.png"
            filepath = os.path.join(self.output_dir, filename)
            img.save(filepath)
            return filepath
        except Exception as e:
            print(f"Erro ao gerar fundo local: {e}")
            return None

    def _ensure_image_for_scene(
        self,
        prompt,
        text_fallback,
        aspect_ratio="9:16",
        status_callback=None,
        max_rounds=4,
        allow_non_ai_fallback=False
    ):
        """
        Garante imagem por IA com múltiplas tentativas/provedores.
        Se allow_non_ai_fallback=False, retorna None ao esgotar tentativas.
        """
        width, height = (720, 1280) if aspect_ratio == "9:16" else (1280, 720)
        rounds = max(1, min(12, int(max_rounds or 4)))

        def notify(msg):
            if status_callback:
                try:
                    status_callback(msg)
                except Exception:
                    pass

        # Garante prompt
        if not prompt and text_fallback:
             prompt = f"Exclusive AI illustration representing this narration: {text_fallback[:220]}"

        base_prompt = (prompt or "").strip()
        if not base_prompt:
            notify("Sem prompt de imagem válido para esta cena.")
            return None

        providers = ["openai_dalle3", "pollinations_flux", "pollinations_turbo", "pollinations"]
        final_prompt = (
            f"{base_prompt}. Must align with the narration context. "
            "Exclusive original artwork, no stock photo, no text, no watermark."
        )

        for round_idx in range(1, rounds + 1):
            notify(f"Tentativa de imagem {round_idx}/{rounds} em múltiplas IAs...")
            last_provider = None
            for provider in providers:
                last_provider = provider
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

            if round_idx < rounds:
                wait_s = min(10, 2 + round_idx * 2)
                why = "provedores ocupados/instáveis"
                if last_provider:
                    why = f"última tentativa ({last_provider}) sem imagem válida"
                notify(f"Aguardando {wait_s}s para nova rodada ({why}).")
                time.sleep(wait_s)

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

    def create_video_from_plan(self, plan, cover_image_path=None, aspect_ratio="9:16", progress_callback=None, voice_style=None, voice_gender=None, music_file_path=None):
        """Gera vídeo complexo com áudio e cenas a partir do plano da IA"""
        # Lazy imports: moviepy 1.x usa .editor, moviepy 2.x exporta direto de moviepy
        try:
            from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip, CompositeAudioClip, concatenate_audioclips, AudioClip
        except ImportError:
            from moviepy import ImageClip, concatenate_videoclips, AudioFileClip, CompositeAudioClip, concatenate_audioclips, AudioClip
        import numpy as np

        if progress_callback:
            progress_callback(0, "Iniciando composição do vídeo...")
            
        clips = []
        final_clip = None
        bg_music = None
        allow_non_ai_fallback = os.getenv("ALLOW_NON_AI_IMAGE_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}
        
        try:
            title = plan.get('title', 'Vídeo Sem Título')
            raw_scenes = plan.get('scenes', [])
            
            # Validação extra: Se 'scenes' não for lista, tenta corrigir ou usa lista vazia
            if not isinstance(raw_scenes, list):
                print(f"ALERTA: 'scenes' não é lista. Tipo: {type(raw_scenes)}. Valor: {raw_scenes}")
                if isinstance(raw_scenes, str):
                    # Pode ser que a IA retornou uma string única como cena
                    raw_scenes = [{"text": raw_scenes, "image_prompt": ""}]
                else:
                    raw_scenes = []

            # PROCESSAMENTO DE CENAS LONGAS: Quebra automática de texto (Apenas se NÃO for modo música)
            scenes = []
            if not music_file_path:
                for scene in raw_scenes:
                    scene_text = ""
                    scene_prompt = ""
                    
                    if isinstance(scene, str):
                        scene_text = scene
                    else:
                        scene_text = scene.get('text', '')
                        scene_prompt = scene.get('image_prompt', '')
                    
                    # Se o texto for muito longo (> 200 caracteres), quebra em múltiplas cenas
                    if len(scene_text) > 200:
                        import re
                        # Quebra por pontuação final (. ! ?) mantendo a pontuação
                        # Regex: split por (.+espaço, !+espaço, ?+espaço)
                        parts = re.split(r'(?<=[.!?])\s+', scene_text)
                        
                        for part in parts:
                            if part.strip():
                                scenes.append({"text": part.strip(), "image_prompt": scene_prompt})
                    else:
                        # Adiciona como está (normalizando para dicionário)
                        scenes.append({"text": scene_text, "image_prompt": scene_prompt})
            else:
                # No modo música, usamos as cenas como vieram (já quebradas por tempo)
                scenes = raw_scenes

            # Enriquecimento: IA gera image_prompts profissionais com base na narração (imagens próprias para vídeo profissional)
            # Skip enrichment if music mode (handled by generator)
            if self.ai_service and scenes and not music_file_path:
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
                        allow_non_ai_fallback=allow_non_ai_fallback
                    )
                    
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
                        threads=4,
                        preset='ultrafast'
                    )
                    
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
            
            start_bg_path = cover_image_path if cover_image_path and os.path.exists(cover_image_path) else None
            img_title = self.create_text_image(clean_title, size=video_size, bg_color=(50, 0, 100), bg_image_path=start_bg_path)
            
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
                if progress_callback:
                    # Progresso proporcional entre 10% e 80%
                    scene_progress = 10 + int((i / total_scenes) * 70)
                    progress_callback(scene_progress, f"Processando cena {i+1} de {total_scenes}...")
                    
                if isinstance(scene, str):
                    text = scene
                    # Auto-generate prompt for text-only scenes to ensure visuals
                    image_prompt = f"Cinematic digital art representing: {text[:100]}"
                else:
                    text = scene.get('text', '')
                    image_prompt = scene.get('image_prompt', '')
                    
                    # Fallback: Se não houver prompt de imagem, cria um baseado no texto
                    if not image_prompt and text:
                        print(f"Aviso: Cena {i+1} sem image_prompt. Criando a partir do texto.")
                        image_prompt = f"Cinematic digital art representing: {text[:100]}"

                # Limpeza de segurança para evitar metadados no vídeo
                clean_text = self._clean_text(text)
                
                # GARANTIA DE IMAGEM (Substitui lógica antiga)
                def _scene_status(message, scene_idx=i, total=total_scenes, pct=scene_progress):
                    if progress_callback:
                        progress_callback(pct, f"Cena {scene_idx+1}/{total}: {message}")

                bg_image_path = self._ensure_image_for_scene(
                    image_prompt,
                    text_fallback=clean_text,
                    aspect_ratio=aspect_ratio,
                    status_callback=_scene_status,
                    allow_non_ai_fallback=allow_non_ai_fallback
                )

                if not bg_image_path:
                    bg_image_path = self._generate_fallback_background(video_size)
                    if bg_image_path and progress_callback:
                        progress_callback(scene_progress, f"Cena {i+1}/{total_scenes}: IA de imagem indisponível; usando fundo local.")
                if not bg_image_path:
                    raise Exception(f"Falha ao gerar imagem da cena {i+1}.")

                # Fallback colors
                bg_colors = [(30, 30, 30), (0, 30, 60), (60, 0, 30), (30, 60, 0)]
                bg_color = bg_colors[i % len(bg_colors)]
                
                # Gerar Audio da cena
                audio_path = self.generate_audio(clean_text, voice_style=voice_style, voice_gender=voice_gender)
                
                # Gerar Imagem
                img_scene = self.create_text_image(clean_text, size=video_size, bg_color=bg_color, bg_image_path=bg_image_path)
                
                # Cria clip da cena
                # ImageClip precisa estar disponível no escopo (garantido pelo import no início da função)
                clip_scene = ImageClip(img_scene)
                
                if audio_path:
                    audio_clip_scene = AudioFileClip(audio_path)
                    clip_scene = clip_scene.with_duration(audio_clip_scene.duration + 0.5)
                    clip_scene = clip_scene.with_audio(audio_clip_scene)
                else:
                    # FALLBACK DE ÁUDIO CRÍTICO
                    print(f"AVISO: Cena {i+1} sem áudio gerado. Mantendo duração padrão.")
                    clip_scene = clip_scene.with_duration(5)
                
                # APLICAÇÃO DE EFEITOS VISUAIS (Ken Burns)
                # Só aplica se tivermos uma imagem real de fundo (não cor sólida gerada por código)
                if bg_image_path:
                    clip_scene = self._apply_ken_burns(clip_scene, video_size)
                    
                clips.append(clip_scene)
                
                # Limpeza de imagens temporárias
                if bg_image_path and "temp_" in bg_image_path:
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
            img_end = self.create_text_image(end_text, size=video_size, bg_color=(0, 100, 50), bg_image_path=end_bg_path)
            
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
                ffmpeg_params=["-preset", "ultrafast"],
                **logger_kw
            )
            
            abs_path = os.path.abspath(output_path)
            print(f"Vídeo salvo com sucesso em: {abs_path} (Size: {os.path.getsize(output_path)} bytes)")
            
            if progress_callback:
                progress_callback(100, "Vídeo renderizado com sucesso!")
            
            return {"video_url": f"{VIDEO_URL_PREFIX}/{filename}", "music_credit": used_music_credit}
            
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

    def create_music_video(self, music_path, scenes, title="Música", aspect_ratio="9:16"):
        """Gera clipe (vídeo) com a música como áudio e cenas baseadas na letra. Sem TTS."""
        try:
            from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip, CompositeAudioClip, concatenate_audioclips
        except ImportError:
            from moviepy import ImageClip, concatenate_videoclips, AudioFileClip, CompositeAudioClip, concatenate_audioclips
        
        if not os.path.exists(music_path):
            raise FileNotFoundError(f"Arquivo de música não encontrado: {music_path}")
        video_size = (720, 1280) if aspect_ratio == "9:16" else (1280, 720)
        allow_non_ai_fallback = os.getenv("ALLOW_NON_AI_IMAGE_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}
        clips = []
        try:
            audio_clip = AudioFileClip(music_path)
            total_duration = audio_clip.duration
            n = max(1, len(scenes))
            segment_duration = total_duration / n
            for i, scene in enumerate(scenes):
                text = scene.get("text", "") if isinstance(scene, dict) else str(scene)
                image_prompt = scene.get("image_prompt", "") if isinstance(scene, dict) else ""
                # Usa Pexels/Pixabay primeiro, depois IA (Música e Clipe)
                def _clip_status(message, scene_idx=i, total=n):
                    # progresso local simples para dar feedback no log da API
                    print(f"[Clip][Cena {scene_idx+1}/{total}] {message}")

                bg_image_path = self._ensure_image_for_scene(
                    image_prompt,
                    text_fallback=text[:80],
                    aspect_ratio=aspect_ratio,
                    status_callback=_clip_status,
                    allow_non_ai_fallback=allow_non_ai_fallback
                ) if image_prompt else None
                if image_prompt and not bg_image_path:
                    bg_image_path = self._generate_fallback_background(video_size)
                    if bg_image_path:
                        print(f"[Clip][Cena {i+1}/{n}] IA de imagem indisponível; usando fundo local.")
                if image_prompt and not bg_image_path:
                    raise Exception(f"Falha ao gerar imagem da cena {i+1}.")
                bg_colors = [(30, 30, 30), (0, 30, 60), (60, 0, 30)]
                bg_color = bg_colors[i % len(bg_colors)]
                img = self.create_text_image(self._clean_text(text), size=video_size, bg_color=bg_color, bg_image_path=bg_image_path)
                clip = ImageClip(img).with_duration(segment_duration)
                clips.append(clip)
                if bg_image_path and "temp_" in bg_image_path:
                    try:
                        os.remove(bg_image_path)
                    except Exception:
                        pass
                gc.collect()
            final = concatenate_videoclips(clips)
            final = final.with_audio(audio_clip)
            filename = f"clip_{uuid.uuid4().hex[:8]}.mp4"
            output_path = os.path.join(self.output_dir, filename)
            final.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac", logger=None)
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
