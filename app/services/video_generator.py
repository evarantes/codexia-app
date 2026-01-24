import os
import uuid
import requests
import gc
from gtts import gTTS
from moviepy import ImageClip, concatenate_videoclips, AudioFileClip, CompositeAudioClip, concatenate_audioclips
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
import textwrap
import numpy as np

class VideoGenerator:
    def __init__(self, output_dir="app/static/videos", ai_service=None):
        self.output_dir = output_dir
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
                
                # Escurecer a imagem para legibilidade do texto
                enhancer = ImageEnhance.Brightness(img)
                img = enhancer.enhance(0.4) # 40% de brilho original
            except Exception as e:
                print(f"Erro ao carregar imagem de fundo: {e}")
                img = Image.new('RGB', size, color=bg_color)
        else:
            img = Image.new('RGB', size, color=bg_color)

        d = ImageDraw.Draw(img)
        
        # Tenta carregar uma fonte padrão, senão usa padrão
        try:
            # Tenta arial ou outra fonte do sistema se possível
            # Reduzido tamanho para 40 para ser mais elegante
            font = ImageFont.truetype("arial.ttf", 40)
        except:
            font = ImageFont.load_default()

        # Quebra o texto
        # Aumentado width para 40 caracteres para ocupar menos altura
        lines = textwrap.wrap(text, width=40) 
        
        # Calcula altura total do bloco de texto
        line_height = 50 # Reduzido line height
        text_block_height = len(lines) * line_height
        
        # Posiciona no terço inferior (Subtitle style), mas com limite
        # Garante que não suba muito para o meio
        # Fixamos a base do texto a 100px do fundo
        margin_bottom = 150
        y_text = size[1] - text_block_height - margin_bottom
        
        # Se o texto for muito longo e subir demais, cortamos o topo (fallback)
        # Mas idealmente o texto deve ser curto.
        # Vamos desenhar o fundo preto
        
        # Desenha fundo semi-transparente para o texto
        # Precisamos de uma imagem RGBA para transparência
        overlay = Image.new('RGBA', size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        
        # Padding
        padding = 20
        # Calcula largura máxima do texto
        max_width = 0
        for line in lines:
            bbox = d.textbbox((0, 0), line, font=font)
            w = bbox[2] - bbox[0]
            if w > max_width: max_width = w
            
        # Box background
        # Centralizado horizontalmente
        box_left = (size[0] - max_width) / 2 - padding
        box_top = y_text - padding
        box_right = (size[0] + max_width) / 2 + padding
        box_bottom = y_text + text_block_height + padding
        
        draw.rectangle([box_left, box_top, box_right, box_bottom], fill=(0, 0, 0, 160)) # Black with alpha
        
        # Compoe overlay na imagem original
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, overlay)
        img = img.convert("RGB")
        d = ImageDraw.Draw(img) # Novo draw object
        
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

    def generate_audio(self, text, lang='pt'):
        """Gera arquivo de áudio usando OpenAI (Human-like) ou gTTS (Fallback)"""
        if not text.strip(): return None
        
        # 1. Tentar OpenAI TTS (Qualidade Humana)
        if self.ai_service:
            audio_content = self.ai_service.generate_audio(text, voice="onyx") # Onyx é uma voz masculina profunda e narrativa
            if audio_content:
                filename = f"{uuid.uuid4()}.mp3"
                path = os.path.join(self.output_dir, filename)
                with open(path, "wb") as f:
                    f.write(audio_content)
                return path

        # 2. Fallback gTTS (Robótico, mas funciona sempre)
        try:
            tts = gTTS(text=text, lang=lang)
            filename = f"{uuid.uuid4()}.mp3"
            path = os.path.join(self.output_dir, filename)
            tts.save(path)
            return path
        except Exception as e:
            print(f"Erro no TTS: {e}")
            return None

    def download_image(self, url):
        try:
            response = requests.get(url, stream=True)
            if response.status_code == 200:
                filename = f"temp_{uuid.uuid4()}.png"
                filepath = os.path.join(self.output_dir, filename)
                with open(filepath, 'wb') as f:
                    for chunk in response.iter_content(1024):
                        f.write(chunk)
                return filepath
        except Exception as e:
            print(f"Erro ao baixar imagem: {e}")
        return None

    def create_video_from_plan(self, plan, cover_image_path=None, aspect_ratio="9:16", progress_callback=None):
        """Gera vídeo complexo com áudio e cenas a partir do plano da IA"""
        if progress_callback:
            progress_callback(0, "Iniciando composição do vídeo...")
            
        clips = []
        final_clip = None
        bg_music = None
        
        try:
            title = plan.get('title', 'Vídeo Sem Título')
            scenes = plan.get('scenes', [])
            
            # Otimização de memória: Reduzir resolução para 720p para evitar OOM em tiers gratuitos
            if aspect_ratio == "16:9":
                video_size = (1280, 720) # Antes: 1920, 1080
            else:
                video_size = (720, 1280) # Antes: 1080, 1920

            # 1. Slide de Título (Com capa se disponível)
            if progress_callback:
                progress_callback(5, "Criando slide de título...")
                
            title_audio_path = self.generate_audio(title)
            
            start_bg_path = cover_image_path if cover_image_path and os.path.exists(cover_image_path) else None
            img_title = self.create_text_image(title, size=video_size, bg_color=(50, 0, 100), bg_image_path=start_bg_path)
            
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
                    
                text = scene.get('text', '')
                image_prompt = scene.get('image_prompt', '')
                
                # Tentar gerar imagem com IA
                bg_image_path = None
                if self.ai_service and image_prompt:
                    print(f"Gerando imagem para cena {i+1}...")
                    # Otimiza prompt para aspect ratio
                    prompt_suffix = f". Aspect ratio {aspect_ratio}."
                    image_url = self.ai_service.generate_image(image_prompt + prompt_suffix)
                    if image_url:
                        bg_image_path = self.download_image(image_url)

                # Fallback colors
                bg_colors = [(30, 30, 30), (0, 30, 60), (60, 0, 30), (30, 60, 0)]
                bg_color = bg_colors[i % len(bg_colors)]
                
                # Gerar Audio da cena
                audio_path = self.generate_audio(text)
                
                # Gerar Imagem
                img = self.create_text_image(text, size=video_size, bg_color=bg_color, bg_image_path=bg_image_path)
                clip = ImageClip(img)
                
                if audio_path:
                    audio_clip_scene = AudioFileClip(audio_path)
                    clip = clip.with_duration(audio_clip_scene.duration + 0.5)
                    clip = clip.with_audio(audio_clip_scene)
                else:
                    clip = clip.with_duration(4)
                    
                clips.append(clip)
                
                # Limpar imagem temporária se foi baixada
                if bg_image_path and "temp_" in bg_image_path:
                    try:
                        os.remove(bg_image_path)
                    except:
                        pass
                
                # Forçar coleta de lixo a cada cena para evitar pico
                gc.collect()
                
            # 3. Slide Final (CTA)
            if progress_callback:
                progress_callback(85, "Criando slide final...")
                
            end_text = "Inscreva-se no Canal!\nLink na Bio."
            audio_end_path = self.generate_audio("Inscreva-se no canal e ative o sininho.")
            
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

            # Output
            if progress_callback:
                progress_callback(95, "Renderizando arquivo final...")
                
            filename = f"{uuid.uuid4()}.mp4"
            output_path = os.path.join(self.output_dir, filename)
            
            # Escreve o arquivo
            # threads=1 para reduzir uso de memória durante encoding
            final_clip.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac", threads=1)
            
            if progress_callback:
                progress_callback(100, "Vídeo renderizado com sucesso!")
            
            return {"video_url": f"/static/videos/{filename}", "music_credit": used_music_credit}
            
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
