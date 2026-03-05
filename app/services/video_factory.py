import json
import os
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from sqlalchemy.orm import Session
from sqlalchemy import case, func
from app.models import ContentPlan, Video, Scene, Job, Asset
from app.services.ai_generator import AIContentGenerator
from app.services.video_generator import VideoGenerator
from app.services.storage import StorageService
from app.config import VIDEO_OUTPUT_DIR
from app.redis_client import queue
from app.database import SessionLocal

def _is_truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

class VideoFactory:
    def __init__(self, db: Session):
        self.db = db
        self.ai = AIContentGenerator()
        self.video_gen = VideoGenerator(output_dir=VIDEO_OUTPUT_DIR, ai_service=self.ai)
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
        # Dispara no máximo 1 pipeline LONG global.
        # Se já houver LONG pendente/processando, os novos ficam em queued e serão
        # liberados quando o atual finalizar render.
        has_active_long = (
            self.db.query(Job)
            .join(Video, Video.id == Job.video_id)
            .filter(
                Job.status.in_(["pending", "processing"]),
                Video.type == "LONG",
                Video.parent_video_id == None,
            )
            .first()
        )
        if not has_active_long:
            self._enqueue_next_long_video()
        return plan

    def _add_job(self, video_id: int, step: str):
        # Evita jobs duplicados da mesma etapa para o mesmo vídeo.
        existing = (
            self.db.query(Job)
            .filter(
                Job.video_id == video_id,
                Job.step == step,
                Job.status.in_(["pending", "processing"])
            )
            .order_by(Job.id.desc())
            .first()
        )
        if existing:
            job = existing
        else:
            job = Job(video_id=video_id, step=step, status="pending", progress=0)
            self.db.add(job)
            self.db.commit()
        
        # Enfileirar no Redis se disponível
        if queue:
            try:
                # Import local para evitar ciclo
                from app.tasks import process_job_task
                queue.enqueue(process_job_task, job.id, job_id=f"video_job_{job.id}")
                print(f"Job {job.id} enfileirado no Redis.")
            except Exception as e:
                print(f"Erro ao enfileirar job {job.id}: {e}")

    def _clip_with_duration(self, clip, duration):
        """Compatibilidade MoviePy 1.x/2.x."""
        if hasattr(clip, "with_duration"):
            return clip.with_duration(duration)
        return clip.set_duration(duration)

    def _clip_with_audio(self, clip, audio_clip):
        """Compatibilidade MoviePy 1.x/2.x."""
        if hasattr(clip, "with_audio"):
            return clip.with_audio(audio_clip)
        return clip.set_audio(audio_clip)

    def _clip_subclip(self, clip, start_t, end_t):
        """Compatibilidade MoviePy 1.x/2.x."""
        if hasattr(clip, "subclip"):
            return clip.subclip(start_t, end_t)
        if hasattr(clip, "subclipped"):
            return clip.subclipped(start_t, end_t)
        raise AttributeError("Clip sem subclip/subclipped")

    def _clip_crop(self, clip, **kwargs):
        """Compatibilidade MoviePy 1.x/2.x para crop."""
        if hasattr(clip, "crop"):
            return clip.crop(**kwargs)
        if hasattr(clip, "cropped"):
            return clip.cropped(**kwargs)
        raise AttributeError("Clip sem crop/cropped")

    def _clip_resize(self, clip, size):
        """Compatibilidade MoviePy 1.x/2.x para resize."""
        if hasattr(clip, "resized"):
            return clip.resized(size)
        if hasattr(clip, "resize"):
            return clip.resize(size)
        raise AttributeError("Clip sem resize/resized")

    def _set_job_progress(self, job: Job, progress: int, log_line: str = None):
        """Atualiza progresso do job sem permitir regressão visual."""
        try:
            p = max(0, min(100, int(progress)))
        except Exception:
            p = 0
        current = int(job.progress or 0)
        if p > current:
            job.progress = p
        if log_line:
            job.logs = (job.logs or "") + f"{log_line}\n"
        self.db.commit()

    def _video_control_state(self, video_id: int) -> str:
        """Retorna estado de controle do vídeo: PAUSE, CANCEL ou vazio."""
        video = self.db.query(Video).get(video_id)
        if not video:
            return "CANCEL"
        status = (video.status or "").strip().upper()
        if status.startswith("CANCELLED") or status.startswith("CANCELED"):
            return "CANCEL"
        if status.startswith("PAUSED"):
            return "PAUSE"
        return ""

    def process_next_job(self):
        """Pega o próximo job pendente e executa. Um vídeo por vez."""
        # Não iniciar se já existe job em processamento (defesa extra além do lock)
        if self.db.query(Job).filter(Job.status == "processing").first():
            return False
        # Prioriza concluir um vídeo antes de iniciar outro:
        # 1) menor video_id
        # 2) ordem natural das etapas
        step_priority = case(
            (Job.step == "script", 1),
            (Job.step == "tts", 2),
            (Job.step == "visuals", 3),
            (Job.step == "render", 4),
            (Job.step == "shorts_extract", 5),
            else_=99
        )
        job = (
            self.db.query(Job)
            .join(Video, Video.id == Job.video_id)
            .filter(
                Job.status == "pending",
                ~func.upper(func.trim(Video.status)).like("PAUSED%"),
                ~func.upper(func.trim(Video.status)).like("CANCELLED%"),
                ~func.upper(func.trim(Video.status)).like("CANCELED%"),
            )
            .order_by(Job.video_id.asc(), step_priority.asc(), Job.created_at.asc())
            .first()
        )
        if not job:
            return False
        self.process_job(job)
        return True

    def _enqueue_next_long_video(self):
        """Enfileira o próximo LONG queued globalmente (um pipeline por vez)."""
        active_job_exists = (
            self.db.query(Job.id)
            .filter(
                Job.video_id == Video.id,
                Job.status.in_(["pending", "processing"])
            )
            .exists()
        )
        next_video = (
            self.db.query(Video)
            .filter(
                Video.parent_video_id == None,
                Video.type == "LONG",
                func.upper(func.trim(Video.status)) == "QUEUED",
                ~active_job_exists,
            )
            .order_by(Video.id.asc())
            .first()
        )
        if not next_video:
            return

        self._add_job(next_video.id, "script")

    def process_job(self, job: Job):
        """Executa um job específico."""
        print(f"[Factory] Processando Job {job.id} - Step: {job.step} para Video {job.video_id}")
        # Se o vídeo foi pausado/cancelado antes do worker iniciar, não executa.
        pre_control = self._video_control_state(job.video_id)
        if pre_control == "CANCEL":
            job.status = "cancelled"
            job.logs = (job.logs or "") + "Cancelado antes de iniciar execução.\n"
            self.db.commit()
            return
        if pre_control == "PAUSE":
            job.status = "paused"
            job.logs = (job.logs or "") + "Pausado antes de iniciar execução.\n"
            self.db.commit()
            return

        job.status = "processing"
        job.progress = max(int(job.progress or 0), 5)
        job.logs = f"Iniciado em {datetime.now()}\n"
        self.db.commit()

        enqueue_next_long = False
        try:
            video = self.db.query(Video).get(job.video_id)
            if not video:
                raise Exception("Vídeo não encontrado")

            if job.step == "script":
                self._set_job_progress(job, 10, "Gerando roteiro...")
                self._step_script(video, job)
                control = self._video_control_state(video.id)
                if control == "CANCEL":
                    video.status = "CANCELLED"
                elif control == "PAUSE":
                    video.status = "PAUSED"
                else:
                    # Next: TTS
                    self._add_job(video.id, "tts")
                    video.status = "SCRIPT"
            
            elif job.step == "tts":
                self._set_job_progress(job, 35, "Gerando narração (TTS)...")
                self._step_tts(video, job)
                control = self._video_control_state(video.id)
                if control == "CANCEL":
                    video.status = "CANCELLED"
                elif control == "PAUSE":
                    video.status = "PAUSED"
                else:
                    # Next: Visuals
                    self._add_job(video.id, "visuals")
                    video.status = "TTS"
            
            elif job.step == "visuals":
                self._set_job_progress(job, 55, "Gerando visuais...")
                self._step_visuals(video, job)
                control = self._video_control_state(video.id)
                if control == "CANCEL":
                    video.status = "CANCELLED"
                elif control == "PAUSE":
                    video.status = "PAUSED"
                else:
                    # Next: Render
                    self._add_job(video.id, "render")
                    video.status = "VISUALS"
            
            elif job.step == "render":
                self._set_job_progress(job, 75, "Renderizando vídeo final...")
                self._step_render(video, job)
                control = self._video_control_state(video.id)
                if control == "CANCEL":
                    video.status = "CANCELLED"
                elif control == "PAUSE":
                    video.status = "PAUSED"
                else:
                    video.status = "READY"
                    # Trigger Shorts generation
                    shorts = self.db.query(Video).filter(Video.parent_video_id == video.id).all()
                    for short in shorts:
                        self._add_job(short.id, "shorts_extract")
                    # Após concluir o LONG atual, libera o próximo LONG globalmente.
                    enqueue_next_long = ((video.type or "").upper() == "LONG")
            
            elif job.step == "shorts_extract":
                self._set_job_progress(job, 85, "Gerando short derivado...")
                self._step_shorts_extract(video, job)
                control = self._video_control_state(video.id)
                if control == "CANCEL":
                    video.status = "CANCELLED"
                elif control == "PAUSE":
                    video.status = "PAUSED"
                else:
                    video.status = "READY"

            final_video_status = (video.status or "").strip().upper()
            job.status = "completed"
            if final_video_status.startswith("PAUSED"):
                job.logs += f"Etapa concluída e produção pausada em {datetime.now()}\n"
            elif final_video_status.startswith("CANCELLED") or final_video_status.startswith("CANCELED"):
                job.logs += f"Etapa finalizada e produção cancelada em {datetime.now()}\n"
            else:
                job.progress = 100
                job.logs += f"Concluído em {datetime.now()}\n"
            self.db.commit()
            if enqueue_next_long:
                try:
                    self._enqueue_next_long_video()
                except Exception as e:
                    print(f"[Factory] Aviso ao liberar próximo LONG: {e}")

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
        theme = plan.theme if plan and getattr(plan, "theme", None) else (video.title or "Tema")
        duration_min = plan.duration_min if plan and getattr(plan, "duration_min", None) else 3
        voice_style = plan.voice_style if plan and getattr(plan, "voice_style", None) else "human"
        target_sec = duration_min * 60
        min_scenes = max(6, duration_min * 2)  # pelo menos 2 cenas por minuto
        prompt = f"""
        Crie um roteiro detalhado para um vídeo de YouTube sobre '{theme}'.
        OBRIGATÓRIO: O vídeo deve ter aproximadamente {duration_min} MINUTOS quando narrado em voz alta (~{target_sec} segundos no total).
        Use entre {min_scenes} e {min_scenes + 6} cenas. Cada cena deve ter "narration" com 1 a 3 frases (ao ler em voz alta, ~30-90 segundos por cena).
        Estilo: {voice_style}.
        Estrutura: Gancho (primeiras cenas) → Introdução → Conteúdo Principal (várias cenas) → Conclusão e CTA.
        Para cada cena, "keywords" e "visual_prompt" devem descrever uma CENA ou IMAGEM concreta (ex: "pessoa orando", "Bíblia aberta", "natureza") para buscar foto de banco.
        
        Saída ESTRITAMENTE em JSON:
        {{
            "title": "Título chamativo",
            "description": "Descrição para YouTube com hashtags",
            "tags": "tag1, tag2, tag3",
            "scenes": [
                {{
                    "idx": 1,
                    "narration": "Texto exato para narrar nesta cena (1-3 frases).",
                    "visual_prompt": "Descrição visual da cena para buscar imagem (ex: pastor pregando, paisagem)",
                    "keywords": "palavras para busca de foto stock em inglês",
                    "duration_sec": 45
                }}
            ]
        }}
        A soma de duration_sec de todas as cenas deve ser aproximadamente {target_sec}.
        """

        data = None
        try:
            response = self.ai._generate_text(prompt, json_mode=True)
            if response and isinstance(response, str):
                # Limpeza básica do JSON
                clean = response.replace("```json", "").replace("```", "").strip()
                data = json.loads(clean)
        except Exception as e:
            job.logs += f"Aviso: IA indisponível/retorno inválido no script ({e}). Usando fallback.\n"

        if not data or not isinstance(data, dict):
            sec_per_scene = max(30, target_sec // min_scenes)
            data = {
                "title": video.title or f"{theme} - Vídeo",
                "description": f"Vídeo sobre {theme}",
                "tags": "youtube, conteúdo, automação",
                "scenes": [
                    {"idx": i, "narration": f"Cena {i} sobre {theme}.", "visual_prompt": f"Cena sobre {theme}", "keywords": theme, "duration_sec": sec_per_scene}
                    for i in range(1, min_scenes + 1)
                ],
            }
        
        video.title = data.get("title", video.title)
        video.description = data.get("description", "")
        video.tags = data.get("tags", "")
        
        # Save Scenes
        scenes_data = data.get("scenes", [])
        if not isinstance(scenes_data, list) or not scenes_data:
            sec_per = max(30, target_sec // 6)
            scenes_data = [{"idx": i, "narration": f"Conteúdo sobre {theme} (parte {i}).", "visual_prompt": f"Cena sobre {theme}", "keywords": theme, "duration_sec": sec_per} for i in range(1, 7)]

        normalized_scenes = []
        for i, s in enumerate(scenes_data, start=1):
            if not isinstance(s, dict):
                s = {"narration": str(s)}
            normalized_scenes.append({
                "idx": int(s.get("idx") or i),
                "narration": (s.get("narration") or "").strip() or f"Cena {i} sobre {theme}.",
                "visual_prompt": (s.get("visual_prompt") or "").strip(),
                "keywords": (s.get("keywords") or "").strip(),
                "duration_sec": s.get("duration_sec", 5),
            })

        # Garante coerência visual com a narração por cena (prompts exclusivos da IA).
        try:
            enrich_payload = {
                "title": video.title or f"{theme} - Vídeo",
                "scenes": [
                    {"text": s["narration"], "image_prompt": s["visual_prompt"]}
                    for s in normalized_scenes
                ],
            }
            enriched = self.ai.enrich_scenes_with_image_prompts(enrich_payload) or {}
            enriched_scenes = enriched.get("scenes") or []
            if isinstance(enriched_scenes, list):
                for i, item in enumerate(enriched_scenes):
                    if i >= len(normalized_scenes) or not isinstance(item, dict):
                        continue
                    prompt = (item.get("image_prompt") or "").strip()
                    if prompt:
                        normalized_scenes[i]["visual_prompt"] = prompt[:500]
        except Exception as e:
            job.logs += f"Aviso: não foi possível enriquecer prompts visuais ({e}).\n"

        for s in normalized_scenes:
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
        job.logs += f"Roteiro gerado e {len(normalized_scenes)} cenas salvas.\n"

    def _step_tts(self, video: Video, job: Job):
        plan = video.plan
        scenes = self.db.query(Scene).filter(Scene.video_id == video.id).order_by(Scene.idx).all()
        if not scenes:
            raise Exception("Nenhuma cena encontrada para gerar áudio.")

        generated_audio = 0
        total = max(1, len(scenes))
        for idx, scene in enumerate(scenes, start=1):
            audio_path = self.video_gen.generate_audio(
                scene.narration_text, 
                voice_style=(plan.voice_style if plan else "human"),
                voice_gender=(plan.voice_gender if plan else "female")
            )
            if audio_path:
                asset = Asset(
                    video_id=video.id,
                    kind="AUDIO",
                    storage_key=audio_path,
                    meta_json=json.dumps({"scene_idx": scene.idx})
                )
                self.db.add(asset)
                generated_audio += 1
            # 35% -> 70% durante TTS
            self._set_job_progress(job, 35 + int((idx / total) * 35))
        
        self.db.commit()
        if generated_audio == 0:
            raise Exception("Falha ao gerar narração: nenhum áudio foi criado.")
        job.logs += f"{generated_audio}/{len(scenes)} áudios gerados.\n"

    def _step_visuals(self, video: Video, job: Job):
        job.logs += "Iniciando geração de visuais exclusivos por IA...\n"
        scenes = self.db.query(Scene).filter(Scene.video_id == video.id).order_by(Scene.idx).all()
        total = max(1, len(scenes))
        strict_ai_only = _is_truthy(os.getenv("STRICT_AI_IMAGE_ONLY"))
        job.logs += f"Total de cenas a processar: {total}\n"
        for idx, scene in enumerate(scenes, start=1):
            narration = (scene.narration_text or "").strip()
            visual_prompt = (scene.visual_prompt or "").strip()
            if not visual_prompt:
                visual_prompt = f"Illustrate the meaning of this narration: {narration[:220]}"
            prompt = (
                f"{visual_prompt}. "
                f"This image must clearly match the narrated message: \"{narration[:260]}\". "
                "Original AI-generated artwork, exclusive composition, no stock photo, no text, no watermark."
            )

            job.logs += f"\n=== CENA {idx}/{total} ===\n"
            job.logs += f"Narração: {narration[:100]}...\n"
            job.logs += f"Prompt visual: {visual_prompt[:80]}...\n"
            job.logs += f"Gerando imagem única por IA...\n"
            filepath = self.video_gen._ensure_image_for_scene(
                prompt,
                text_fallback=narration[:120] if narration else (scene.keywords or ""),
                aspect_ratio="16:9"
            )
            source_type = "AI_EXCLUSIVE"
            invalid_path = (not filepath or not os.path.exists(filepath) or os.path.getsize(filepath) < 1000)
            local_fallback = bool(filepath and os.path.basename(filepath).startswith("fallback_local_"))

            if invalid_path or local_fallback:
                if local_fallback:
                    job.logs += f"Resultado: Fundo gradiente gerado. Tentando buscar imagem real de qualidade...\n"
                else:
                    job.logs += f"Resultado: Falha na geração por IA. Tentando buscar imagem real...\n"
                
                if strict_ai_only:
                    raise Exception(
                        f"Falha ao gerar imagem exclusiva por IA para cena {scene.idx}. "
                        "Verifique as credenciais/serviço de IA e tente novamente."
                    )

                # Fallback 1: tentar Pexels/Pixabay (fotos reais) antes de texto
                filepath = None
                plan = video.plan
                theme = (plan.theme if plan and getattr(plan, "theme", None) else None) or (video.title or "").split(" - ")[0].strip() or "story"
                for query in [(scene.keywords or "").strip(), (scene.visual_prompt or "").strip(), theme]:
                    if query and len(query) >= 2:
                        try:
                            stock_url = self.stock.search_image(query[:80] if len(query) > 80 else query)
                            if stock_url:
                                r = requests.get(stock_url, timeout=10)
                                if r.status_code == 200:
                                    fn = f"scene_{video.id}_{scene.idx}_{uuid.uuid4().hex[:6]}.jpg"
                                    filepath = os.path.join(VIDEO_OUTPUT_DIR, fn)
                                    with open(filepath, 'wb') as f:
                                        f.write(r.content)
                                    source_type = "STOCK"
                                    job.logs += f"Stock (Pexels/Pixabay) para cena {scene.idx}.\n"
                                    break
                        except Exception:
                            pass

                # Fallback 2: resiliente com texto em fundo colorido (quando stock não encontrou)
                if not filepath:
                    from PIL import Image
                    fallback_text = narration[:180] if narration else (scene.keywords or f"Cena {scene.idx}")
                    img_array = self.video_gen.create_text_image(
                        fallback_text,
                        size=(1280, 720),
                        bg_color=(35, 35, 45),
                        text_color=(245, 245, 245),
                    )
                    filename = f"scene_{video.id}_{scene.idx}_fallback_text.png"
                    fallback_path = os.path.join(VIDEO_OUTPUT_DIR, filename)
                    Image.fromarray(img_array).save(fallback_path)
                    filepath = fallback_path
                    source_type = "TEXT_FALLBACK"
                    job.logs += (
                        f"Cena {scene.idx}: Usando fundo colorido (IA e Stock falharam).\n"
                    )
            else:
                job.logs += f"Resultado final: Imagem única gerada com sucesso por IA.\n"

            s3_key = self.storage.upload_file(filepath)
            asset = Asset(
                video_id=video.id,
                kind="IMAGE",
                storage_key=filepath,
                meta_json=json.dumps({"scene_idx": scene.idx, "source": source_type, "s3_url": s3_key})
            )
            self.db.add(asset)
            self._set_job_progress(job, 55 + int((idx / total) * 25))
            
        self.db.commit()
        if strict_ai_only:
            job.logs += "Visuais gerados por IA (modo estrito).\n"
        else:
            job.logs += "Visuais gerados por IA com fallback resiliente quando necessário.\n"

    def _step_render(self, video: Video, job: Job):
        # Montar FFmpeg command
        # 1. Listar assets ordenados
        assets = self.db.query(Asset).filter(Asset.video_id == video.id).all()
        audio_assets = {json.loads(a.meta_json)['scene_idx']: a for a in assets if a.kind == 'AUDIO'}
        image_assets = {json.loads(a.meta_json)['scene_idx']: a for a in assets if a.kind == 'IMAGE'}
        
        # Criar arquivo de input para concatenação (demuxer) é complexo com áudio/vídeo sync
        # Melhor usar moviepy para MVP ou comando complexo filter_complex
        
        # MVP Rápido: Usar MoviePy (já que o usuário permitiu e está instalado)
        # moviepy 1.x usa .editor, moviepy 2.x exporta direto de moviepy
        try:
            from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips, CompositeVideoClip
        except ImportError:
            from moviepy import ImageClip, AudioFileClip, concatenate_videoclips, CompositeVideoClip
        
        clips = []
        final_video = None
        scenes = self.db.query(Scene).filter(Scene.video_id == video.id).order_by(Scene.idx).all()
        
        try:
            for scene in scenes:
                audio_asset = audio_assets.get(scene.idx)
                image_asset = image_assets.get(scene.idx)
                
                if audio_asset and image_asset:
                    audio_clip = AudioFileClip(audio_asset.storage_key)
                    duration = audio_clip.duration
                    
                    img_clip = ImageClip(image_asset.storage_key)
                    img_clip = self._clip_resize(img_clip, (1280, 720))  # 720p = render ~2x mais rápido
                    img_clip = self._clip_with_duration(img_clip, duration)
                    img_clip = self._clip_with_audio(img_clip, audio_clip)
                    clips.append(img_clip)

            if not clips:
                raise Exception("Sem clips para renderizar (áudio/imagem ausentes).")

            final_video = concatenate_videoclips(clips, method="compose")
            output_filename = f"final_{video.id}.mp4"
            output_path = os.path.join(VIDEO_OUTPUT_DIR, output_filename)
            
            self._set_job_progress(job, 75, "Escrevendo arquivo de vídeo...")
            write_logger = None
            try:
                import proglog
                def _up(percent):
                    self._set_job_progress(job, percent, "Escrevendo arquivo de vídeo...")
                class RenderLogger(proglog.ProgressBarLogger):
                    def bars_callback(self, bar, attr, value, old_value=None):
                        super().bars_callback(bar, attr, value, old_value)
                        if bar not in self.bars or not self.bars[bar].get("total"):
                            return
                        total = self.bars[bar]["total"]
                        if total and value is not None:
                            pct = 75 + int(24 * (value / total))
                            try:
                                _up(min(99, pct))
                            except Exception:
                                pass
                write_logger = RenderLogger()
            except Exception:
                pass

            # Heartbeat: atualiza progresso 75->95 a cada 15s caso proglog não dispare (ex: MoviePy 2.x)
            stop_event = threading.Event()
            job_id = job.id

            def _progress_heartbeat():
                db = None
                try:
                    db = SessionLocal()
                    interval = 15
                    while not stop_event.wait(interval):
                        try:
                            j = db.query(Job).get(job_id)
                            if not j or (j.status or "").lower() != "processing":
                                return
                            current = int(j.progress or 75)
                            p = min(95, current + 5)
                            if p > current:
                                j.progress = p
                                db.commit()
                        except Exception:
                            if db:
                                db.rollback()
                finally:
                    if db:
                        db.close()

            heartbeat = threading.Thread(target=_progress_heartbeat, daemon=True)
            heartbeat.start()
            try:
                kw = {"logger": write_logger} if write_logger else {}
                final_video.write_videofile(
                    output_path, fps=20, codec="libx264", audio_codec="aac",
                    threads=4, ffmpeg_params=["-preset", "ultrafast", "-crf", "28"], **kw
                )
            finally:
                stop_event.set()

            # Save Asset
            asset = Asset(
                video_id=video.id,
                kind="FINAL",
                storage_key=output_path
            )
            self.db.add(asset)
            
            # Update Video
            video.youtube_video_id = output_path # Temporário: guarda o path
            self.db.commit()
            job.logs += f"Render concluído: {output_path}\n"
        finally:
            try:
                if final_video:
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

        clip = None
        cropped = None
        resized = None
        final_short = None
        try:
            clip = VideoFileClip(parent_asset.storage_key)
            self._set_job_progress(job, 90, "Processando corte para short...")
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
            
            cropped = self._clip_crop(clip, x1=x_center - new_w/2, y1=0, width=new_w, height=h)
            resized = self._clip_resize(cropped, (1080, 1920))
            
            # Pega subclip de 60s
            duration = min(clip.duration, 60)
            final_short = self._clip_subclip(resized, 0, duration)
            
            output_filename = f"short_{video.id}.mp4"
            output_path = os.path.join(VIDEO_OUTPUT_DIR, output_filename)
            
            final_short.write_videofile(
                output_path, fps=20, codec="libx264", audio_codec="aac",
                threads=4, ffmpeg_params=["-preset", "ultrafast", "-crf", "28"]
            )
            
            asset = Asset(
                video_id=video.id,
                kind="FINAL",
                storage_key=output_path
            )
            self.db.add(asset)
            self.db.commit()
            job.logs += f"Short renderizado: {output_path}\n"
        finally:
            for c in (final_short, resized, cropped, clip):
                try:
                    if c:
                        c.close()
                except Exception:
                    pass
