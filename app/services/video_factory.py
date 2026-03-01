import json
import os
import subprocess
import uuid
import requests
from datetime import datetime, timedelta
from pathlib import Path
from sqlalchemy.orm import Session
from app.models import ContentPlan, Video, Scene, Job, Asset
from app.services.ai_generator import AIContentGenerator
from app.services.video_generator import VideoGenerator
from app.services.stock_service import StockService
from app.services.storage import StorageService
from app.config import VIDEO_OUTPUT_DIR
from app.redis_client import queue

class VideoFactory:
    def __init__(self, db: Session):
        self.db = db
        self.ai = AIContentGenerator()
        self.video_gen = VideoGenerator(output_dir=VIDEO_OUTPUT_DIR, ai_service=self.ai)
        self.stock = StockService()
        self.storage = StorageService()

    def create_plan(self, plan_data: dict, user_id: int):
        """Cria o plano de conteúdo e os vídeos agendados."""
        try:
            start_date = datetime.strptime(plan_data['start_date'], '%Y-%m-%d')
        except:
            start_date = datetime.now()

        plan = ContentPlan(
            user_id=user_id,
            mode=plan_data.get('mode', 'theme'),
            theme=plan_data.get('theme'),
            start_date=start_date,
            days=int(plan_data.get('days', 1)),
            videos_per_day=int(plan_data.get('videos_per_day', 1)),
            shorts_per_day=int(plan_data.get('shorts_per_day', 0)),
            duration_min=int(plan_data.get('duration_min', 5)),
            voice_style=plan_data.get('voice_style', 'human'),
            voice_gender=plan_data.get('voice_gender', 'female'),
            music_file=plan_data.get('music_file'),
            status="confirmed"
        )
        self.db.add(plan)
        self.db.commit()
        self.db.refresh(plan)

        current_date = plan.start_date
        for day in range(plan.days):
            # Long Videos
            for v_idx in range(plan.videos_per_day):
                video = Video(
                    plan_id=plan.id,
                    type="LONG",
                    title=f"{plan.theme} - Dia {day+1} (Gerando...)",
                    status="queued",
                    duration_sec=plan.duration_min * 60,
                    scheduled_at=current_date.replace(hour=10 + v_idx*2, minute=0, second=0)
                )
                self.db.add(video)
                self.db.commit()
                self.db.refresh(video)
                
                # Enfileira Job de Script
                self._add_job(video.id, "script")

                # Shorts (Placeholder - serão ativados após o vídeo longo ficar pronto)
                for s_idx in range(plan.shorts_per_day):
                    short = Video(
                        plan_id=plan.id,
                        type="SHORT",
                        title=f"Short {day+1} (Aguardando Vídeo Longo)",
                        status="queued", # Fica queued mas sem job por enquanto
                        parent_video_id=video.id,
                        scheduled_at=current_date.replace(hour=14 + s_idx, minute=0, second=0)
                    )
                    self.db.add(short)
            
            current_date += timedelta(days=1)
        
        self.db.commit()
        return plan

    def _add_job(self, video_id: int, step: str):
        job = Job(video_id=video_id, step=step, status="pending", progress=0)
        self.db.add(job)
        self.db.commit()
        
        # Enfileirar no Redis se disponível
        if queue:
            try:
                # Import local para evitar ciclo
                from app.tasks import process_job_task
                queue.enqueue(process_job_task, job.id)
                print(f"Job {job.id} enfileirado no Redis.")
            except Exception as e:
                print(f"Erro ao enfileirar job {job.id}: {e}")

    def process_next_job(self):
        """Pega o próximo job pendente e executa. Chamado pelo Worker/Cron (Legado/MVP)."""
        job = self.db.query(Job).filter(Job.status == "pending").order_by(Job.created_at.asc()).first()
        if not job:
            return False
        self.process_job(job)
        return True

    def process_job(self, job: Job):
        """Executa um job específico."""
        print(f"[Factory] Processando Job {job.id} - Step: {job.step} para Video {job.video_id}")
        job.status = "processing"
        job.logs = f"Iniciado em {datetime.now()}\n"
        self.db.commit()

        try:
            video = self.db.query(Video).get(job.video_id)
            if not video:
                raise Exception("Vídeo não encontrado")

            if job.step == "script":
                self._step_script(video, job)
                # Next: TTS
                self._add_job(video.id, "tts")
                video.status = "SCRIPT"
            
            elif job.step == "tts":
                self._step_tts(video, job)
                # Next: Visuals
                self._add_job(video.id, "visuals")
                video.status = "TTS"
            
            elif job.step == "visuals":
                self._step_visuals(video, job)
                # Next: Render
                self._add_job(video.id, "render")
                video.status = "VISUALS"
            
            elif job.step == "render":
                self._step_render(video, job)
                video.status = "READY"
                # Trigger Shorts generation
                shorts = self.db.query(Video).filter(Video.parent_video_id == video.id).all()
                for short in shorts:
                    self._add_job(short.id, "shorts_extract")
            
            elif job.step == "shorts_extract":
                self._step_shorts_extract(video, job)
                video.status = "READY"

            job.status = "completed"
            job.progress = 100
            job.logs += f"Concluído em {datetime.now()}\n"
            self.db.commit()

        except Exception as e:
            print(f"[Factory] Erro no Job {job.id}: {e}")
            job.status = "failed"
            job.logs += f"ERRO: {str(e)}\n"
            video = self.db.query(Video).get(job.video_id)
            if video:
                video.status = "ERROR"
            self.db.commit()

    def _step_script(self, video: Video, job: Job):
        plan = video.plan
        prompt = f"""
        Crie um roteiro detalhado para um vídeo de YouTube sobre '{plan.theme}'.
        Duração estimada: {plan.duration_min} minutos.
        Estilo: {plan.voice_style}.
        Estrutura:
        1. Gancho (0-30s)
        2. Introdução
        3. Conteúdo Principal (dividido em tópicos)
        4. Conclusão e CTA
        
        Saída ESTRITAMENTE em JSON no formato:
        {{
            "title": "Título chamativo",
            "description": "Descrição para YouTube com hashtags",
            "tags": "tag1, tag2, tag3",
            "scenes": [
                {{
                    "idx": 1,
                    "narration": "Texto exato para narrar...",
                    "visual_prompt": "Descrição da imagem/cena para IA...",
                    "keywords": "keyword1, keyword2",
                    "duration_sec": 5
                }}
            ]
        }}
        """
        
        response = self.ai._generate_text(prompt, json_mode=True)
        # Limpeza básica do JSON
        response = response.replace("```json", "").replace("```", "").strip()
        data = json.loads(response)
        
        video.title = data.get("title", video.title)
        video.description = data.get("description", "")
        video.tags = data.get("tags", "")
        
        # Save Scenes
        for s in data.get("scenes", []):
            scene = Scene(
                video_id=video.id,
                idx=s.get("idx"),
                narration_text=s.get("narration"),
                visual_prompt=s.get("visual_prompt"),
                keywords=s.get("keywords"),
                duration_sec=s.get("duration_sec", 5)
            )
            self.db.add(scene)
        
        self.db.commit()
        job.logs += "Roteiro gerado e cenas salvas.\n"

    def _step_tts(self, video: Video, job: Job):
        plan = video.plan
        scenes = self.db.query(Scene).filter(Scene.video_id == video.id).order_by(Scene.idx).all()
        
        for scene in scenes:
            audio_path = self.video_gen.generate_audio(
                scene.narration_text, 
                voice_style=plan.voice_style, 
                voice_gender=plan.voice_gender
            )
            if audio_path:
                asset = Asset(
                    video_id=video.id,
                    kind="AUDIO",
                    storage_key=audio_path,
                    meta_json=json.dumps({"scene_idx": scene.idx})
                )
                self.db.add(asset)
        
        self.db.commit()
        job.logs += f"{len(scenes)} áudios gerados.\n"

    def _step_visuals(self, video: Video, job: Job):
        job.logs += "Iniciando geração de visuais...\n"
        scenes = self.db.query(Scene).filter(Scene.video_id == video.id).order_by(Scene.idx).all()
        
        for scene in scenes:
            try:
                filepath = None
                source_type = "TEXT_PLACEHOLDER"
                
                # 1. Tentar Stock (Pexels/Pixabay) - keywords ou visual_prompt
                query = (scene.keywords or scene.visual_prompt or "").strip()
                if query:
                    job.logs += f"Buscando stock (Pexels/Pixabay) para cena {scene.idx}: {query[:50]}...\n"
                    stock_url = self.stock.search_image(query)
                    if stock_url:
                        try:
                            # Download image
                            response = requests.get(stock_url, timeout=10)
                            if response.status_code == 200:
                                filename = f"scene_{video.id}_{scene.idx}_{uuid.uuid4().hex[:6]}.jpg"
                                filepath = os.path.join(VIDEO_OUTPUT_DIR, filename)
                                with open(filepath, 'wb') as f:
                                    f.write(response.content)
                                source_type = "STOCK"
                                job.logs += f"Stock encontrado para cena {scene.idx}.\n"
                        except Exception as e:
                            job.logs += f"Erro ao baixar stock: {e}\n"
                
                # 2. Fallback: Gerar Imagem com Texto
                if not filepath:
                    job.logs += f"Gerando imagem fallback para cena {scene.idx}\n"
                    # Usando o create_text_image do VideoGenerator
                    img_array = self.video_gen.create_text_image(
                        scene.narration_text[:100], # Preview do texto
                        size=(1920, 1080)
                    )
                    
                    # Salvar imagem
                    from PIL import Image
                    filename = f"scene_{video.id}_{scene.idx}.png"
                    filepath = os.path.join(VIDEO_OUTPUT_DIR, filename)
                    Image.fromarray(img_array).save(filepath)
                    source_type = "TEXT_GEN"

                # 3. Upload to S3 (optional backup)
                s3_key = self.storage.upload_file(filepath)
                
                asset = Asset(
                    video_id=video.id,
                    kind="IMAGE",
                    storage_key=filepath,
                    meta_json=json.dumps({"scene_idx": scene.idx, "source": source_type, "s3_url": s3_key})
                )
                self.db.add(asset)
            except Exception as e:
                job.logs += f"Aviso: Falha nos visuais da cena {scene.idx}: {e}\n"
            
        self.db.commit()
        job.logs += "Visuais gerados.\n"

    def _step_render(self, video: Video, job: Job):
        """Renderiza o vídeo final unificando a lógica com VideoGenerator."""
        job.logs += "Iniciando renderização unificada...\n"
        self.db.commit()

        try:
            # 1. Preparar o plano para o VideoGenerator
            scenes = self.db.query(Scene).filter(Scene.video_id == video.id).order_by(Scene.idx).all()
            plan = {
                "title": video.title,
                "scenes": [
                    {
                        "text": s.narration_text,
                        "image_prompt": s.visual_prompt,
                        "duration": s.duration_sec
                    } for s in scenes
                ],
                "music_mood": "drama" # Default mood
            }

            # 2. Callback de progresso para atualizar o Job
            def progress_cb(pct, msg):
                job.progress = pct
                job.logs += f"[{pct}%] {msg}\n"
                self.db.commit()

            # 3. Executar renderização via VideoGenerator (Unificação)
            # Isso garante Ken Burns, limpeza de texto, e otimizações de memória
            result = self.video_gen.create_video_from_plan(
                plan,
                aspect_ratio="16:9" if video.type == "LONG" else "9:16",
                progress_callback=progress_cb,
                voice_style=video.plan.voice_style,
                voice_gender=video.plan.voice_gender,
                music_file_path=video.plan.music_file
            )

            if result and "file_path" in result:
                output_path = result["file_path"]
                video_url = result.get("video_url")
                
                # Salvar Asset FINAL
                asset = Asset(
                    video_id=video.id,
                    kind="FINAL",
                    storage_key=output_path
                )
                self.db.add(asset)
                
                # Atualizar Video
                # youtube_video_id deve ser nulo até a publicação real
                video.youtube_video_id = None 
                # Salvar a URL para visualização no frontend
                video.description = (video.description or "") + f"\n\nURL_VIDEO: {video_url}"
                video.status = "READY"
                self.db.commit()
                job.logs += f"Render concluído com sucesso: {output_path}\n"
            else:
                # Fallback para o modo antigo se o novo falhar (ou retornar apenas URL)
                video_url = result.get("video_url")
                if video_url:
                    job.logs += f"Vídeo gerado (URL): {video_url}\n"
                    video.status = "READY"
                    self.db.commit()
                else:
                    raise Exception("Falha ao obter path do vídeo renderizado.")

        except Exception as e:
            job.logs += f"Erro crítico no step_render: {str(e)}\n"
            raise e

    def _step_shorts_extract(self, video: Video, job: Job):
        # Pega o vídeo pai
        parent = video.parent_video
        if not parent:
            job.logs += "Erro: Sem vídeo pai.\n"
            return

        parent_asset = self.db.query(Asset).filter(Asset.video_id == parent.id, Asset.kind == "FINAL").first()
        if not parent_asset:
            job.logs += "Erro: Vídeo pai não tem render final.\n"
            return
            
        # Crop 9:16 e pega 60s aleatórios ou baseados em cenas
        # MVP: Pega 0-60s e corta o centro
        
        try:
            from moviepy.editor import VideoFileClip
        except ImportError:
            from moviepy import VideoFileClip

        clip = VideoFileClip(parent_asset.storage_key)
        # Corta centro 1080x1920 (ou redimensiona)
        # Se for 1920x1080 (landscape), crop center 607x1080 then resize or just crop
        
        # Crop para 9:16
        w, h = clip.size
        target_ratio = 9/16
        
        # Se for landscape, a altura é o limitante se quisermos preencher tudo, mas perderemos laterais
        # Melhor estratégia simples: Cortar um quadrado central ou retângulo vertical
        
        new_w = h * target_ratio # 1080 * 9/16 = 607.5
        if new_w > w:
            new_w = w
            
        x_center = w / 2
        y_center = h / 2
        
        cropped = clip.crop(x1=x_center - new_w/2, y1=0, width=new_w, height=h)
        resized = cropped.resize((1080, 1920))
        
        # Pega subclip de 60s
        duration = min(clip.duration, 60)
        final_short = resized.subclip(0, duration)
        
        output_filename = f"short_{video.id}.mp4"
        output_path = os.path.join(VIDEO_OUTPUT_DIR, output_filename)
        
        final_short.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")
        
        asset = Asset(
            video_id=video.id,
            kind="FINAL",
            storage_key=output_path
        )
        self.db.add(asset)
        self.db.commit()
        job.logs += f"Short renderizado: {output_path}\n"
