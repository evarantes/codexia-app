import json
import os
import gc
import datetime
from app.database import SessionLocal
from app.models import ScheduledVideo
from app.services.ai_generator import AIContentGenerator

_PROCESSABLE_QUEUE_STATUSES = ("queued", "dispatching")

def _load_scheduled_processing_policy(video):
    from app.routers.youtube import _load_scheduled_video_payload, _scheduled_video_processing_policy

    payload = _load_scheduled_video_payload(video)
    policy = _scheduled_video_processing_policy(video, payload)
    return payload, policy

def _record_processing_refusal(video, payload, policy, trigger_mode: str):
    payload = dict(payload or {})
    reason = str(policy.get("reason") or "scheduled_video_source_not_allowed").strip() or "scheduled_video_source_not_allowed"
    source = str(policy.get("source") or payload.get("source") or "legacy_schedule").strip() or "legacy_schedule"
    note = f"[AUTO_BLOCKED]: Processamento {trigger_mode} recusado para source={source}. Motivo: {reason}."

    payload["_processing_refused"] = True
    payload["_processing_refused_reason"] = reason
    payload["_processing_refused_source"] = source
    payload["_processing_refused_trigger_mode"] = trigger_mode
    payload["_processing_refused_at"] = datetime.datetime.now().isoformat()
    try:
        video.script_data = json.dumps(payload)
    except Exception:
        pass

    current_desc = (video.description or "").strip()
    if note not in current_desc:
        video.description = (current_desc + "\n\n" + note).strip() if current_desc else note


def _claim_scheduled_video_for_processing(db, video_id: int) -> bool:
    updated = (
        db.query(ScheduledVideo)
        .filter(
            ScheduledVideo.id == video_id,
            ScheduledVideo.status.in_(_PROCESSABLE_QUEUE_STATUSES),
        )
        .update(
            {
                ScheduledVideo.status: "processing",
                ScheduledVideo.updated_at: datetime.datetime.now(),
            },
            synchronize_session=False,
        )
    )
    if updated:
        db.commit()
        return True
    db.rollback()
    return False

def process_scheduled_video(video_id: int, trigger_mode: str = "auto"):
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
    
    # Adicionar Lock Global para impedir concorrência entre fila e frontend
    from app.redis_client import conn
    from filelock import FileLock, Timeout
    _lock_dir = "/data" if os.path.isdir("/data") else os.path.expanduser("~")
    _FACTORY_LOCK_PATH = os.path.join(_lock_dir, ".codexia_factory.lock")
    FACTORY_LOCK_KEY = "codexia:video_factory:single_worker_lock"
    
    redis_lock = None
    file_lock = None
    if conn:
        try:
            redis_lock = conn.lock(FACTORY_LOCK_KEY, timeout=4 * 60 * 60, blocking_timeout=1)
            if not redis_lock.acquire(blocking=False):
                print(f"Vídeo agendado {video_id} abortado (Redis): já existe uma geração de vídeo rodando no servidor.")
                return
        except Exception:
            redis_lock = None

    if not conn or not redis_lock:
        try:
            file_lock = FileLock(_FACTORY_LOCK_PATH, timeout=0)
            file_lock.acquire()
        except Timeout:
            print(f"Vídeo agendado {video_id} abortado (FileLock): já existe uma geração de vídeo rodando no servidor.")
            return
        except Exception:
            file_lock = None

    # Lazy import para reduzir uso de memória no startup (moviepy/PIL/numpy são pesados)
    from app.services.video_generator import VideoGenerator

    # Re-instanciar DB session pois estamos em thread separada
    db = SessionLocal()
    video = None
    try:
        video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
        if not video:
            return

        script_data, processing_policy = _load_scheduled_processing_policy(video)
        if not processing_policy.get("auto_process_eligible"):
            _record_processing_refusal(video, script_data, processing_policy, trigger_mode)
            db.commit()
            print(
                f"Vídeo agendado {video_id} recusado. "
                f"source={processing_policy.get('source')} "
                f"reason={processing_policy.get('reason')} "
                f"trigger={trigger_mode}"
            )
            return
            
        # Double-check status to avoid race conditions if called from multiple places
        if video.status == "processing":
            print(f"Video {video_id} já está sendo processado.")
            return

        # CRITICAL SAFETY: Prevent reprocessing of already completed videos (saves OpenAI cost)
        if video.status in ("completed", "ready", "published"):
             print(f"Video {video_id} já está concluído ({video.status}). Ignorando reprocessamento.")
             return
        
        # Check if file exists to recover from "stuck" state without cost
        if video.video_url:
             try:
                 from app.config import absolute_path_for_video
                 abs_path = absolute_path_for_video(video.video_url)
                 if os.path.exists(abs_path):
                     print(f"Vídeo {video_id} já possui arquivo gerado em {abs_path}. Recuperando status para 'completed'.")
                     video.status = "completed"
                     video.progress = 100
                     db.commit()
                     return
             except Exception as e:
                 print(f"Erro ao verificar arquivo existente: {e}")

        if not _claim_scheduled_video_for_processing(db, video_id):
            latest = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
            latest_status = getattr(latest, "status", None)
            print(f"Vídeo agendado {video_id} não pôde ser reservado pelo worker. Status atual: {latest_status}")
            return
        video = db.query(ScheduledVideo).filter(ScheduledVideo.id == video_id).first()
        if not video:
            return
        
        # Recuperar dados do script (safe load)
        if not isinstance(script_data, dict):
            script_data = {}
        
        ai_service = AIContentGenerator()
        video_service = VideoGenerator(ai_service=ai_service)
        
        topic = video.title
        concept = video.description or ""
        
        # Limpar créditos de música antigos do conceito/descrição para não contaminar o prompt
        if "Music:" in concept:
            concept = concept.split("Music:")[0].strip()
        if "http" in concept: # Remove URLs comuns em créditos
            concept = concept.split("http")[0].strip()
            
        # Gerar roteiro detalhado
        # Se for short, 1 min. Se video, 5 min (padrão solicitado pelo user antes)
        # Prioridade: Duração solicitada > Tipo Short (1min) > Padrão (3min)
        duration = 3 # Padrão
        
        if script_data.get('duration'):
             try:
                 duration = int(script_data.get('duration'))
             except:
                 pass
        elif video.video_type == 'short':
             duration = 1
        
        final_script = None
        
        # OTIMIZAÇÃO DE CUSTO: Verificar se já existe script gerado (recuperação de falha/crash)
        if script_data.get("scenes") and isinstance(script_data.get("scenes"), list):
             print(f"Usando script em cache (DB) para video {video_id}. Economizando chamada OpenAI.")
             final_script = script_data
        
        # MODO VIDEO CLIP MUSICAL
        elif video.music_file_path and os.path.exists(video.music_file_path):
             print(f"Gerando plano visual para música: {video.music_file_path}")
             try:
                 # Obter duração da música
                 try:
                    from moviepy.editor import AudioFileClip
                 except ImportError:
                    from moviepy import AudioFileClip
                 
                 audioclip = AudioFileClip(video.music_file_path)
                 music_duration = int(audioclip.duration)
                 audioclip.close()
                 
                 final_script = ai_service.generate_visual_plan_for_music(topic, concept, music_duration)
                 final_script["title"] = topic # Ensure title is present
                 
                 # Salvar cache
                 if final_script and "scenes" in final_script:
                     script_data.update(final_script)
                     video.script_data = json.dumps(script_data)
                     db.commit()
             except Exception as e:
                 print(f"Erro ao processar modo música: {e}")
                 raise e

        else:
             print(f"Gerando script para video {video_id}: {topic}")
             final_script = ai_service.generate_motivational_script(f"{topic}. Conceito: {concept}", duration)
             
             # Salvar script gerado no banco imediatamente para evitar regerar em caso de crash
             if final_script and "scenes" in final_script:
                 try:
                     # Merge generated script into existing plan data
                     script_data.update(final_script)
                     video.script_data = json.dumps(script_data)
                     db.commit()
                     print(f"Script salvo em cache para video {video_id}")
                 except Exception as e:
                     print(f"Erro ao salvar cache do script: {e}")

        if not final_script or not isinstance(final_script.get("scenes"), list) or len(final_script.get("scenes", [])) == 0:
            raise ValueError(
                "IA não retornou roteiro válido. Configure OPENAI_API_KEY ou GEMINI_API_KEY em Configurações."
            )

        try:
            target_sec = int(duration) * 60 if video.video_type != "short" else 60
            if isinstance(final_script, dict) and target_sec > 0:
                final_script["target_duration_sec"] = target_sec
        except Exception:
            pass

        # ENRICHMENT: IA gera image_prompts profissionais com base na narração (imagens próprias para vídeo profissional)
        # Skip enrichment for music videos as they are already visual-focused
        if not video.music_file_path:
            print("Enriquecendo cenas com descrições visuais geradas pela IA (narração → imagem)...")
            final_script = ai_service.enrich_scenes_with_image_prompts(final_script)
        
        # Gerar vídeo
        def progress_callback(p, m):
            try:
                # p is 0-100; nunca diminuir progresso (evita mostrar 95% -> 24% se o logger/enviar valor errado)
                new_p = int(p)
                if new_p >= (video.progress or 0):
                    video.progress = new_p
                    video.updated_at = datetime.datetime.now()
                    db.commit()
            except:
                pass
            
        ratio = "9:16" if video.video_type == 'short' else "16:9"
        
        print(f"Renderizando video {video_id}...")
        result = video_service.create_video_from_plan(
            final_script, 
            aspect_ratio=ratio, 
            progress_callback=progress_callback,
            voice_style=video.voice_style,
            voice_gender=video.voice_gender,
            music_file_path=video.music_file_path # Pass music file if exists
        )
        video_path = result["video_url"]

        try:
            used_images = result.get("used_images") if isinstance(result, dict) else None
            if isinstance(used_images, list):
                cleaned = []
                for v in used_images:
                    if isinstance(v, str) and v.strip() and v.strip().startswith("/static/"):
                        cleaned.append(v.strip())
                if cleaned:
                    script_data["rendered_images"] = cleaned[:60]
                    video.script_data = json.dumps(script_data)
        except Exception:
            pass
        
        # Adicionar créditos ao script_data se possível ou salvar na descrição do vídeo
        if result.get("music_credit"):
            credit = f"\n\n{result['music_credit']}"
            if not video.description:
                video.description = ""
            if credit not in video.description:
                video.description += credit
        
        video.status = "AWAITING_PUBLISH"
        video.progress = 100
        video.video_url = video_path # path relativo /static/videos/...
        db.commit()
        print(f"Video {video_id} concluído: {video_path}")
        
    except Exception as e:
        import traceback
        error_msg = f"{str(e)}\n{traceback.format_exc()}"
        print(f"Erro ao gerar video agendado {video_id}: {error_msg}")
        if video:
            video.status = "failed"
            video.progress = 0
            # Append error to description for visibility in UI
            current_desc = video.description or ""
            # Avoid duplicating error messages
            if "[ERRO]" not in current_desc:
                video.description = f"{current_desc}\n\n[ERRO]: {error_msg}"[:5000] # Increased limit for traceback
            db.commit()
    finally:
        db.close()
        gc.collect()
        
        # Release locks
        try:
            if redis_lock:
                redis_lock.release()
        except Exception:
            pass
        try:
            if file_lock:
                file_lock.release()
        except Exception:
            pass
