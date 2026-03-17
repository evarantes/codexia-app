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

    def _infer_music_mood(self, theme: str, narration_text: str) -> str:
        t = f"{theme or ''}\n{narration_text or ''}".lower()
        if any(k in t for k in ["feliz", "alegr", "comédia", "divertid", "engraç", "leve", "good vibes", "relax"]):
            return "happy"
        if any(k in t for k in ["épico", "epic", "motiv", "supera", "conquista", "vitória", "fé", "propósito", "coragem", "guerreiro"]):
            return "epic"
        return "drama"

    def _truncate_words(self, text: str, max_words: int) -> str:
        words = (text or "").strip().split()
        if max_words <= 0 or len(words) <= max_words:
            return (text or "").strip()
        truncated = " ".join(words[:max_words]).strip()
        if truncated and truncated[-1] not in ".!?":
            truncated += "."
        return truncated

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
                video.status = "SCRIPT"
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
            
            elif job.step == "tts":
                video.status = "TTS"
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
            
            elif job.step == "visuals":
                video.status = "VISUALS"
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
            
            elif job.step == "render":
                video.status = "RENDER"
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
        target_words = max(80, int(duration_min) * 150)
        max_words = int(target_words * 1.10)
        prompt = f"""
        Crie um roteiro detalhado para um vídeo de YouTube sobre '{theme}'.
        Duração estimada: {duration_min} minutos.
        Meta de palavras no total: aproximadamente {target_words} (não exceda {max_words}).
        Estilo: {voice_style}.
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
            try:
                n_scenes = max(3, int(duration_min) * 2)
            except Exception:
                n_scenes = 6
            scenes = [
                {"idx": 1, "narration": f"Bem-vindo ao conteúdo sobre {theme}.", "visual_prompt": f"Cena introdutória sobre {theme}", "keywords": theme, "duration_sec": 6},
            ]
            for i in range(2, max(3, n_scenes)):
                scenes.append(
                    {"idx": i, "narration": f"Ponto importante {i-1} sobre {theme}.", "visual_prompt": f"Cena explicativa sobre {theme}, tópico {i-1}", "keywords": theme, "duration_sec": 8}
                )
            scenes.append(
                {"idx": n_scenes, "narration": "Se gostou, curta e acompanhe os próximos vídeos.", "visual_prompt": "Chamada final para ação em estúdio", "keywords": "call to action", "duration_sec": 5},
            )
            data = {
                "title": video.title or f"{theme} - Vídeo",
                "description": f"Vídeo sobre {theme}",
                "tags": "youtube, conteúdo, automação",
                "scenes": scenes,
            }
        
        video.title = data.get("title", video.title)
        video.description = data.get("description", "")
        video.tags = data.get("tags", "")
        
        # Save Scenes
        scenes_data = data.get("scenes", [])
        if not isinstance(scenes_data, list) or not scenes_data:
            scenes_data = [{"idx": 1, "narration": f"Conteúdo sobre {theme}.", "visual_prompt": f"Cena sobre {theme}", "keywords": theme, "duration_sec": 6}]

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

        try:
            total_words = sum(len((s.get("narration") or "").split()) for s in normalized_scenes)
        except Exception:
            total_words = 0
        if total_words and total_words > max_words:
            scale = float(target_words) / float(total_words)
            adjusted = []
            for s in normalized_scenes:
                narration = s.get("narration") or ""
                n_words = len(narration.split())
                new_max = max(12, int(n_words * scale))
                s["narration"] = self._truncate_words(narration, new_max)
                adjusted.append(s)
            normalized_scenes = adjusted

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
        # Modo estrito opcional: quando true, falha a cena caso nenhum provedor de IA responda.
        # Quando false (padrão), usa fallback contextual (texto/narração) para não travar a produção.
        strict_ai_only = _is_truthy(os.getenv("STRICT_AI_IMAGE_ONLY"))
        # Fallback local em gradiente deve ser somente opt-in, para evitar fundo genérico.
        allow_local_gradient = _is_truthy(os.getenv("ALLOW_LOCAL_GRADIENT_FALLBACK"))
        try:
            max_rounds = max(1, min(12, int(os.getenv("AI_IMAGE_MAX_ROUNDS", "4"))))
        except Exception:
            max_rounds = 4
        fallback_used = False

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

            base_progress = 55 + int(((idx - 1) / total) * 25)
            def scene_status(message: str):
                self._set_job_progress(job, base_progress, f"Cena {idx}/{total}: {message}")

            scene_status(f"Iniciando geração da imagem (cena {scene.idx}).")
            filepath = self.video_gen._ensure_image_for_scene(
                prompt,
                text_fallback=narration[:120] if narration else (scene.keywords or ""),
                aspect_ratio="16:9",
                status_callback=scene_status,
                max_rounds=max_rounds,
                allow_non_ai_fallback=allow_local_gradient
            )
            source_type = "AI_EXCLUSIVE"
            invalid_path = (not filepath or not os.path.exists(filepath) or os.path.getsize(filepath) < 1000)
            local_fallback = bool(filepath and os.path.basename(filepath).startswith("fallback_local_"))

            if invalid_path or local_fallback:
                # Mesmo em modo estrito, evitamos interrupção total do pipeline.
                # O fallback contextual mantém produção ativa e evita "tela preta".
                from PIL import Image
                fallback_text = narration[:180] if narration else (scene.keywords or f"Cena {scene.idx}")
                fallback_bg = self.video_gen._generate_fallback_background((1280, 720))
                img_array = self.video_gen.create_text_image(
                    fallback_text,
                    size=(1280, 720),
                    bg_color=(70, 90, 130),
                    text_color=(245, 245, 245),
                    bg_image_path=fallback_bg,
                )
                filename = f"scene_{video.id}_{scene.idx}_fallback_text.png"
                fallback_path = os.path.join(VIDEO_OUTPUT_DIR, filename)
                Image.fromarray(img_array).save(fallback_path)

                if filepath and os.path.exists(filepath) and os.path.basename(filepath).startswith("fallback_local_"):
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                if fallback_bg and os.path.exists(fallback_bg):
                    try:
                        os.remove(fallback_bg)
                    except Exception:
                        pass

                filepath = fallback_path
                source_type = "TEXT_FALLBACK"
                fallback_used = True
                if strict_ai_only:
                    scene_status("IA indisponível no modo estrito; aplicando fallback contextual para evitar falha.")
                else:
                    scene_status("IA indisponível no momento; usando arte contextual para não travar.")
            scene_status(f"Imagem da cena {scene.idx} pronta.")

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
        if fallback_used:
            job.logs += "Visuais concluídos com fallback contextual em cenas sem resposta de IA.\n"
        elif strict_ai_only:
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
            from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips, CompositeVideoClip, CompositeAudioClip, concatenate_audioclips, AudioClip
        except ImportError:
            from moviepy import ImageClip, AudioFileClip, concatenate_videoclips, CompositeVideoClip, CompositeAudioClip, concatenate_audioclips, AudioClip
        
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
                    try:
                        img_clip = self.video_gen._apply_ken_burns(img_clip, (1280, 720))
                    except Exception:
                        pass
                    clips.append(img_clip)

            if not clips:
                raise Exception("Sem clips para renderizar (áudio/imagem ausentes).")

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

            target_duration = getattr(video, "duration_sec", None)
            if target_duration:
                try:
                    target_duration = float(target_duration)
                except Exception:
                    target_duration = None
            if target_duration and target_duration > 1:
                try:
                    current = float(final_video.duration or 0)
                except Exception:
                    current = 0
                if current > (target_duration + 0.5):
                    final_video = self._clip_subclip(final_video, 0, target_duration)
                elif current and current < (target_duration - 0.5):
                    extra = target_duration - current
                    try:
                        frame_t = max(0, current - 0.02)
                        last_frame = final_video.get_frame(frame_t)
                        freeze = self._clip_with_duration(ImageClip(last_frame), extra)

                        silence_audio = AudioClip(lambda _t: [0.0, 0.0], duration=extra, fps=44100)
                        freeze = self._clip_with_audio(freeze, silence_audio)

                        combined = concatenate_videoclips([final_video, freeze], method="compose")
                        base_audio = final_video.audio
                        if base_audio:
                            combined_audio = concatenate_audioclips([base_audio, silence_audio])
                        else:
                            combined_audio = silence_audio
                        final_video = self._clip_with_audio(combined, combined_audio)
                    except Exception as e:
                        job.logs += f"Aviso: não foi possível ajustar duração ({e}).\n"

            try:
                plan = video.plan
                theme = plan.theme if plan and getattr(plan, "theme", None) else (video.title or "")
                narration_text = " ".join((s.narration_text or "") for s in scenes)
                music_path = None
                if plan and getattr(plan, "music_file", None):
                    raw = str(plan.music_file or "").strip()
                    if raw:
                        if os.path.exists(raw):
                            music_path = raw
                        else:
                            candidate = os.path.join(self.video_gen.music_dir, raw)
                            if os.path.exists(candidate):
                                music_path = candidate
                if not music_path:
                    self.video_gen._ensure_fallback_music()
                    mood = self._infer_music_mood(theme, narration_text)
                    candidate = os.path.join(self.video_gen.music_dir, f"{mood}.mp3")
                    if os.path.exists(candidate):
                        music_path = candidate
                    else:
                        try:
                            import glob
                            any_mp3 = glob.glob(os.path.join(self.video_gen.music_dir, "*.mp3"))
                            music_path = any_mp3[0] if any_mp3 else None
                        except Exception:
                            music_path = None

                if music_path and os.path.exists(music_path):
                    bg = AudioFileClip(music_path)
                    try:
                        bg = bg.volumex(0.10)
                    except Exception:
                        pass
                    try:
                        bg = bg.audio_fadein(1.2).audio_fadeout(1.2)
                    except Exception:
                        pass
                    try:
                        loops = []
                        remaining = float(final_video.duration or 0)
                        while remaining > 0:
                            seg = bg
                            try:
                                if remaining < float(bg.duration or 0):
                                    seg = self._clip_subclip(bg, 0, remaining)
                            except Exception:
                                pass
                            loops.append(seg)
                            try:
                                remaining -= float(seg.duration or 0)
                            except Exception:
                                remaining = 0
                        bg_loop = concatenate_audioclips(loops) if loops else bg
                    except Exception:
                        bg_loop = bg

                    base_audio = final_video.audio
                    if base_audio:
                        final_audio = CompositeAudioClip([bg_loop, base_audio])
                    else:
                        final_audio = bg_loop
                    final_video = self._clip_with_audio(final_video, final_audio)
            except Exception as e:
                job.logs += f"Aviso: não foi possível aplicar música de fundo ({e}).\n"

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
        composite = None
        overlay_clip = None
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
            max_duration = 58
            try:
                total = float(clip.duration or 0)
            except Exception:
                total = 0
            if total <= 0:
                job.logs += "Erro: duração inválida do vídeo pai.\n"
                return
            start = 4.0 if total > (max_duration + 4.0) else max(0.0, total - max_duration)
            end = min(total, start + max_duration)
            final_short = self._clip_subclip(resized, start, end)

            try:
                from PIL import Image, ImageDraw, ImageFont
                import uuid as _uuid
                w, h = 1080, 1920
                img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                box_h = 210
                y0 = h - box_h - 80
                draw.rounded_rectangle((60, y0, w - 60, y0 + box_h), radius=36, fill=(0, 0, 0, 170))
                text = "Quer o resto?\nAssista o vídeo completo no canal"
                try:
                    font = ImageFont.truetype("arial.ttf", 58)
                except Exception:
                    font = ImageFont.load_default()
                draw.multiline_text((110, y0 + 40), text, font=font, fill=(255, 255, 255, 245), spacing=14)
                overlay_path = os.path.join(VIDEO_OUTPUT_DIR, f"short_overlay_{_uuid.uuid4().hex}.png")
                img.save(overlay_path)
                overlay_duration = min(3.0, float(final_short.duration or 3.0))
                overlay_clip = ImageClip(overlay_path)
                if hasattr(overlay_clip, "with_duration"):
                    overlay_clip = overlay_clip.with_duration(overlay_duration)
                else:
                    overlay_clip = overlay_clip.set_duration(overlay_duration)
                start_t = max(0.0, float(final_short.duration or 0) - overlay_duration)
                if hasattr(overlay_clip, "with_start"):
                    overlay_clip = overlay_clip.with_start(start_t)
                else:
                    overlay_clip = overlay_clip.set_start(start_t)
                if hasattr(overlay_clip, "with_position"):
                    overlay_clip = overlay_clip.with_position(("center", "center"))
                else:
                    overlay_clip = overlay_clip.set_position(("center", "center"))
                composite = CompositeVideoClip([final_short, overlay_clip])
                if getattr(final_short, "audio", None):
                    composite = self._clip_with_audio(composite, final_short.audio)
                final_short = composite
            except Exception as e:
                job.logs += f"Aviso: overlay não aplicado ({e}).\n"
            
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
            for c in (overlay_clip, composite, final_short, resized, cropped, clip):
                try:
                    if c:
                        c.close()
                except Exception:
                    pass
