from apscheduler.schedulers.background import BackgroundScheduler
from app.services.video_processing import process_scheduled_video
from app.database import SessionLocal
from app.models import ChannelReport, ScheduledVideo, CommunityComment, SystemNotification, ChannelInsight, VideoTask
import datetime
import logging
import json
import os
import shutil
import sys
import multiprocessing
import requests
from app.redis_client import conn as redis_conn, queue as rq_queue
try:
    from rq import Worker
    _RQ_AVAILABLE = True
except Exception:
    Worker = None
    _RQ_AVAILABLE = False

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_SCHEDULED_DISPATCHING_STATUS = "dispatching"
_SCHEDULED_DISPATCHING_TIMEOUT_SECONDS = 300


def _is_startup_database_bootstrap_error(exc: Exception) -> bool:
    if isinstance(exc, UnicodeDecodeError):
        return True
    message = str(exc or "").lower()
    if not message:
        return False
    return (
        ("utf-8" in message and "codec can't decode" in message)
        or "could not connect to server" in message
        or "connection refused" in message
        or "connection to server" in message
    )

def _load_scheduled_processing_policy(video):
    from app.routers.youtube import _load_scheduled_video_payload, _scheduled_video_processing_policy

    payload = _load_scheduled_video_payload(video)
    policy = _scheduled_video_processing_policy(video, payload)
    return payload, policy


def _recover_stale_dispatching_videos(db, *, stale_after_seconds: int = _SCHEDULED_DISPATCHING_TIMEOUT_SECONDS) -> int:
    now = datetime.datetime.now()
    recovered = 0
    stuck_items = (
        db.query(ScheduledVideo)
        .filter(ScheduledVideo.status == _SCHEDULED_DISPATCHING_STATUS)
        .all()
    )
    for video in stuck_items:
        updated_at = getattr(video, "updated_at", None) or getattr(video, "scheduled_for", None) or now
        age_seconds = max(0.0, float((now - updated_at).total_seconds()))
        if age_seconds < max(30, int(stale_after_seconds)):
            continue
        video.status = "queued"
        video.progress = 0
        recovered += 1
        logger.warning(f"Vídeo {video.id} ficou preso em dispatching por {int(age_seconds)}s. Retornando para queued.")
    return recovered


def _claim_video_for_dispatch(db, video_id: int) -> bool:
    updated = (
        db.query(ScheduledVideo)
        .filter(
            ScheduledVideo.id == video_id,
            ScheduledVideo.status == "queued",
        )
        .update(
            {
                ScheduledVideo.status: _SCHEDULED_DISPATCHING_STATUS,
                ScheduledVideo.updated_at: datetime.datetime.now(),
            },
            synchronize_session=False,
        )
    )
    return bool(updated)

def _append_auto_block_note(video, payload, policy, context: str):
    payload = dict(payload or {})
    reason = str(policy.get("reason") or "scheduled_video_source_not_allowed").strip() or "scheduled_video_source_not_allowed"
    source = str(policy.get("source") or payload.get("source") or "legacy_schedule").strip() or "legacy_schedule"
    note = f"[AUTO_BLOCKED]: {context}. source={source}. Motivo: {reason}."

    payload["_monitor_blocked"] = True
    payload["_monitor_blocked_reason"] = reason
    payload["_monitor_blocked_source"] = source
    payload["_monitor_blocked_context"] = context
    payload["_monitor_blocked_at"] = datetime.datetime.now().isoformat()
    try:
        video.script_data = json.dumps(payload)
    except Exception:
        pass

    if note not in (video.description or ""):
        current_desc = (video.description or "").strip()
        video.description = (current_desc + "\n\n" + note).strip() if current_desc else note

def _normalize_blocked_scheduled_video(video, payload, policy, context: str):
    _append_auto_block_note(video, payload, policy, context)
    has_asset = bool((video.video_url or "").strip() or payload.get("video_url"))
    status = str(video.status or "").strip().lower()
    if has_asset:
        if status != "published":
            video.status = "completed"
        try:
            video.progress = max(int(video.progress or 0), 100)
        except Exception:
            video.progress = 100
    elif status == "processing":
        video.status = "failed"
        video.progress = 0

class MonitorService:
    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.job = None
        self.queue_job = None
        self.story_queue_job = None
        self.series_sync_job = None
        self.task_recovery_job = None
        self.topic_suggestions_job = None
        self.upload_job = None
        self.comments_job = None
        self.insights_job = None
        self.housekeeping_job = None
        self.last_series_sync_at = None
        self.last_series_sync_summary = None
        self.last_series_sync_error = None

    def start(self):
        if not self.job:
            # Startup Recovery: Reset any 'processing' videos to 'queued'
            self._reset_stuck_videos()

            self.job = self.scheduler.add_job(self.check_channel_status, 'interval', minutes=10, max_instances=1)
            # Run video queue check every 1 minute
            # REMOVED next_run_time=now to allow server to startup fully before heavy processing
            self.queue_job = self.scheduler.add_job(
                self.process_video_queue, 
                'interval', 
                minutes=1, 
                max_instances=1
            )
            self.story_queue_job = self.scheduler.add_job(
                self.process_story_video_task_queue,
                "interval",
                minutes=1,
                max_instances=1,
                next_run_time=datetime.datetime.now() + datetime.timedelta(seconds=45),
            )
            self.series_sync_job = self.scheduler.add_job(
                self.sync_youtube_series_scheduler,
                "interval",
                minutes=1,
                max_instances=1,
                next_run_time=datetime.datetime.now() + datetime.timedelta(seconds=55),
            )
            self.task_recovery_job = self.scheduler.add_job(
                self.recover_stalled_story_video_tasks,
                "interval",
                minutes=1,
                max_instances=1,
                next_run_time=datetime.datetime.now() + datetime.timedelta(seconds=75),
            )
            # Run upload check every 5 minutes; first run after 2 min to avoid overload at startup (Coolify/Render)
            self.upload_job = self.scheduler.add_job(
                self.check_scheduled_uploads,
                'interval',
                minutes=5,
                next_run_time=datetime.datetime.now() + datetime.timedelta(minutes=2)
            )
            # Executar verificação de integridade de arquivos (Self-Healing)
            # Rodar imediatamente no startup
            self.check_file_integrity()
            # E agendar para rodar a cada 30 minutos para pegar arquivos deletados (ephemeral storage)
            self.scheduler.add_job(self.check_file_integrity, 'interval', minutes=30)

            try:
                comments_minutes = int((os.getenv("COMMENTS_CHECK_INTERVAL_MINUTES") or "").strip() or "2")
            except Exception:
                comments_minutes = 2
            comments_minutes = max(1, min(60, comments_minutes))
            self.comments_job = self.scheduler.add_job(
                self.check_new_comments,
                "interval",
                minutes=comments_minutes,
                max_instances=1,
                next_run_time=datetime.datetime.now() + datetime.timedelta(minutes=1)
            )
            self.insights_job = self.scheduler.add_job(
                self.check_subscriber_insights,
                "interval",
                minutes=10,
                max_instances=1,
                next_run_time=datetime.datetime.now() + datetime.timedelta(minutes=2)
            )
            self.topic_suggestions_job = self.scheduler.add_job(
                self.check_topic_suggestions,
                "interval",
                hours=12,
                max_instances=1,
                next_run_time=datetime.datetime.now() + datetime.timedelta(minutes=3),
            )

            self.housekeeping_job = self.scheduler.add_job(
                self.run_housekeeping,
                "cron",
                hour=4,
                minute=20,
                max_instances=1,
                next_run_time=datetime.datetime.now() + datetime.timedelta(minutes=4),
            )
            
            self.scheduler.start()
            logger.info("Monitoramento do canal, processador de fila e agendador de uploads iniciados.")

    def process_story_video_task_queue(self):
        try:
            from app.routers.youtube import _kick_story_video_task_queue
            _kick_story_video_task_queue()
        except Exception as e:
            logger.error(f"Erro ao processar fila de vídeos narrados: {e}")

    def sync_youtube_series_scheduler(self):
        self.last_series_sync_at = datetime.datetime.utcnow()
        self.last_series_sync_error = None
        try:
            from app.services.youtube_series_service import youtube_series_service
            db = SessionLocal()
            try:
                self.last_series_sync_summary = youtube_series_service.sync_series_scheduler(db)
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        except Exception as e:
            self.last_series_sync_error = str(e)
            logger.error(f"Erro ao sincronizar séries do YouTube: {e}")

    def recover_stalled_story_video_tasks(self):
        from app.models import VideoTask
        from app.services.task_manager import finalize_task_once, get_task
        from app.config import absolute_path_for_video

        try:
            stall_raw = (os.getenv("YOUTUBE_TASK_STALL_RECOVERY_SECONDS") or "").strip()
            stall_after = int(stall_raw) if stall_raw else 7 * 60
        except Exception:
            stall_after = 7 * 60
        stall_after = max(120, min(60 * 60, int(stall_after)))
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(seconds=stall_after)

        db = SessionLocal()
        try:
            rows = (
                db.query(VideoTask)
                .filter(VideoTask.status == "processing")
                .filter(VideoTask.progress >= 85)
                .filter(VideoTask.updated_at < cutoff)
                .order_by(VideoTask.updated_at.asc())
                .limit(10)
                .all()
            )
            for row in rows:
                task_id = str(getattr(row, "id", "") or "").strip()
                if not task_id:
                    continue
                msg = str(getattr(row, "message", "") or "")
                if "output=" not in msg:
                    continue
                candidate = msg.split("output=", 1)[1].strip().split()[0].strip()
                if not candidate.endswith(".mp4"):
                    continue
                video_url = f"/static/videos/{candidate}"
                abs_path = absolute_path_for_video(video_url)
                if not abs_path or (not os.path.exists(abs_path)):
                    continue
                try:
                    mtime = datetime.datetime.utcfromtimestamp(os.path.getmtime(abs_path))
                except Exception:
                    mtime = None
                if mtime and mtime > (datetime.datetime.utcnow() - datetime.timedelta(seconds=max(60, stall_after // 2))):
                    continue
                try:
                    size_bytes = int(os.path.getsize(abs_path))
                except Exception:
                    size_bytes = 0
                if size_bytes < 750_000:
                    continue

                current = get_task(task_id) or {}
                base_result = current.get("result") if isinstance(current.get("result"), dict) else {}
                merged = dict(base_result or {})
                merged.setdefault("video_url", video_url)
                merged.setdefault("rendering", {})
                if isinstance(merged.get("rendering"), dict):
                    merged["rendering"].setdefault("output_filename", candidate)
                    merged["rendering"].setdefault("output_url", video_url)
                merged["watchdog_recovered"] = True
                finalize_task_once(
                    task_id,
                    status="completed",
                    progress=100,
                    message="Vídeo recuperado automaticamente (watchdog).",
                    result=merged,
                )
        except Exception as e:
            logger.error(f"Erro ao recuperar VideoTasks travadas: {e}")
        finally:
            try:
                db.close()
            except Exception:
                pass

    def check_topic_suggestions(self):
        from app.services.youtube_service import YouTubeService
        from app.services.ai_generator import AIContentGenerator
        try:
            enabled_raw = (os.getenv("TOPIC_SUGGESTIONS_ENABLED") or "").strip().lower()
            if enabled_raw in {"0", "false", "no", "off"}:
                return
        except Exception:
            pass

        db = SessionLocal()
        try:
            last = (
                db.query(ChannelInsight)
                .filter(ChannelInsight.kind == "topic_suggestions")
                .order_by(ChannelInsight.id.desc())
                .first()
            )
            if last and last.created_at and (datetime.datetime.utcnow() - last.created_at) < datetime.timedelta(hours=8):
                return

            yt = YouTubeService()
            if not yt.service:
                return
            stats = yt.get_channel_stats()
            videos = yt.get_recent_videos_performance(max_results=20) or []

            comments_q = (
                db.query(CommunityComment)
                .order_by(CommunityComment.published_at.desc().nullslast(), CommunityComment.created_at.desc().nullslast())
                .limit(80)
                .all()
            )
            comments = []
            for c in comments_q or []:
                t = (c.text or "").strip()
                if not t:
                    continue
                comments.append({
                    "text": t[:500],
                    "like_count": int(getattr(c, "like_count", 0) or 0),
                    "published_at": (c.published_at.isoformat() if getattr(c, "published_at", None) else None),
                    "video_id": (c.youtube_video_id or None),
                })
            ai = AIContentGenerator()
            data = ai.generate_topic_suggestions(stats=stats, recent_videos=videos, recent_comments=comments, hours=72) or {}
            summary = (data.get("summary") if isinstance(data, dict) else None) or "Sugestões de temas atualizadas."

            db.add(ChannelInsight(
                user_id=None,
                kind="topic_suggestions",
                start_date=None,
                end_date=None,
                data_json=json.dumps(data, ensure_ascii=False),
                ai_summary=str(summary)[:1200],
            ))

            top_titles = []
            try:
                for idea in (data.get("long_video_ideas") or [])[:3]:
                    if isinstance(idea, dict) and (idea.get("title") or "").strip():
                        top_titles.append(str(idea.get("title")).strip()[:140])
            except Exception:
                top_titles = []
            payload = {"top_long_titles": top_titles, "hours_window": int(data.get("hours_window") or 72) if isinstance(data, dict) else 72}
            db.add(SystemNotification(
                user_id=None,
                kind="topic_suggestions",
                title="Sugestões de temas",
                message=str(summary)[:900],
                payload_json=json.dumps(payload, ensure_ascii=False),
                status="new",
            ))
            db.commit()
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            logger.error(f"Erro ao gerar sugestões de temas: {e}")
        finally:
            try:
                db.close()
            except Exception:
                pass

    def _reset_stuck_videos(self):
        """Reseta vídeos que ficaram presos em 'processing' devido a reinicialização do servidor"""
        db = SessionLocal()
        try:
            recovered_dispatching = _recover_stale_dispatching_videos(db)
            if recovered_dispatching:
                logger.warning(f"Recovery de startup: {recovered_dispatching} vídeos retornaram de dispatching para queued.")
            stuck_videos = db.query(ScheduledVideo).filter(ScheduledVideo.status == "processing").all()
            if stuck_videos:
                logger.warning(f"Encontrados {len(stuck_videos)} vídeos presos em 'processing'. Verificando retries...")
                for video in stuck_videos:
                    payload, policy = _load_scheduled_processing_policy(video)
                    # Smart Recovery: Check if video file actually exists (maybe DB update failed)
                    recovered = False
                    if video.video_url:
                        try:
                            from app.config import absolute_path_for_video
                            abs_path = absolute_path_for_video(video.video_url)
                            if os.path.exists(abs_path):
                                logger.info(f"Vídeo {video.id} recuperado! Arquivo existe. Marcando como 'completed'.")
                                video.status = "completed"
                                video.progress = 100
                                recovered = True
                        except Exception as e:
                            logger.error(f"Erro ao verificar arquivo para recovery: {e}")
                    
                    if recovered:
                        continue

                    if not policy.get("auto_process_eligible"):
                        _normalize_blocked_scheduled_video(
                            video,
                            payload,
                            policy,
                            "Startup recovery ignorou item bloqueado",
                        )
                        logger.info(
                            f"Vídeo {video.id} bloqueado no recovery de startup. "
                            f"source={policy.get('source')} reason={policy.get('reason')}"
                        )
                        continue

                    # Lógica de Max Retries usando script_data (JSON)
                    try:
                        data = json.loads(video.script_data or "{}")
                        retries = data.get("_crash_retries", 0)
                        
                        if retries >= 3:
                            logger.error(f"Vídeo {video.id} falhou 3 vezes (possível crash recorrente). Marcando como falha.")
                            video.status = "failed"
                            video.progress = 0
                            msg = "\n[ERRO DE SISTEMA]: O vídeo causou falhas repetidas (provavelmente memória insuficiente) e foi cancelado. Tente gerar um vídeo mais curto."
                            if not video.description or "[ERRO DE SISTEMA]" not in video.description:
                                video.description = (video.description or "") + msg
                        else:
                            # Incrementa retry e re-enfileira
                            data["_crash_retries"] = retries + 1
                            video.script_data = json.dumps(data)
                            video.status = "queued"
                            video.progress = 0
                            logger.info(f"Vídeo {video.id} reenfileirado para nova tentativa (Crash #{retries + 1})")
                            
                    except Exception as e:
                        logger.error(f"Erro ao processar retries do vídeo {video.id}: {e}")
                        # Fallback seguro: apenas reseta
                        video.status = "queued"
                        video.progress = 0
                        
                db.commit()
        except Exception as e:
            if _is_startup_database_bootstrap_error(e):
                logger.warning(f"Recovery de startup ignorado por indisponibilidade/conexão do PostgreSQL: {e}")
            else:
                logger.error(f"Erro ao resetar vídeos presos: {e}")
        finally:
            db.close()

    def check_file_integrity(self):
        """Verifica se os arquivos de vídeos 'completos' realmente existem no disco.
           Se não existirem (ex: Render reiniciou), marca como 'failed' — NUNCA reenfileira,
           para não regenerar e gastar OpenAI; o usuário pode excluir ou agendar um novo."""
        logger.info("Verificando integridade dos arquivos de vídeo...")
        db = SessionLocal()
        try:
            # Pega vídeos marcados como prontos mas ainda não upados
            videos = db.query(ScheduledVideo).filter(
                ScheduledVideo.status == "completed",
                ScheduledVideo.uploaded_at == None
            ).all()
            
            failed_count = 0
            for video in videos:
                if not video.video_url:
                    continue
                payload, policy = _load_scheduled_processing_policy(video)
                    
                from app.config import absolute_path_for_video
                abs_path = absolute_path_for_video(video.video_url)
                
                if not os.path.exists(abs_path):
                    # Tentar recuperação inteligente (Auto-Heal) se houver script em cache
                    has_script_cache = False
                    try:
                        if payload.get("scenes") and isinstance(payload.get("scenes"), list):
                                has_script_cache = True
                    except:
                        pass

                    if has_script_cache and policy.get("auto_process_eligible"):
                        logger.warning(f"Arquivo sumiu para vídeo {video.id}. Reenfileirando para recuperação GRATUITA (via cache).")
                        video.status = "queued"
                        video.progress = 0
                        # Não adiciona msg de erro pois será recuperado automaticamente
                    else:
                        if not policy.get("auto_process_eligible"):
                            _append_auto_block_note(
                                video,
                                payload,
                                policy,
                                "Integridade detectou item bloqueado sem permitir reenfileirar",
                            )
                        logger.warning(f"Arquivo sumiu para vídeo {video.id} e SEM cache. Marcando como falha.")
                        video.status = "failed"
                        video.progress = 0
                        msg = "[SISTEMA]: Arquivo de vídeo não encontrado (possível reinício do servidor). Exclua este item ou agende um novo vídeo."
                        if not (video.description and "[SISTEMA]" in video.description):
                            video.description = (video.description or "") + "\n\n" + msg
                    
                    failed_count += 1
            
            if failed_count > 0:
                db.commit()
                logger.info(f"Integridade: {failed_count} vídeos marcados como falha (arquivo ausente).")
            else:
                logger.info("Integridade ok. Todos os vídeos completos possuem arquivos.")
                
        except Exception as e:
            if _is_startup_database_bootstrap_error(e):
                logger.warning(f"Verificação de integridade adiada por indisponibilidade/conexão do PostgreSQL: {e}")
            else:
                logger.error(f"Erro na verificação de integridade: {e}")
        finally:
            db.close()

    def run_housekeeping(self):
        enabled = (os.getenv("CODEXIA_HOUSEKEEPING_ENABLED") or "1").strip().lower() not in ("0", "false", "no")
        if not enabled:
            return

        try:
            retention_days = int((os.getenv("CODEXIA_HOUSEKEEPING_RETENTION_DAYS") or "").strip() or "7")
        except Exception:
            retention_days = 7
        retention_days = max(1, min(365, retention_days))

        try:
            task_retention_days = int((os.getenv("CODEXIA_TASK_RETENTION_DAYS") or "").strip() or "30")
        except Exception:
            task_retention_days = 30
        task_retention_days = max(7, min(365, task_retention_days))

        now = datetime.datetime.utcnow()
        cutoff = now - datetime.timedelta(days=retention_days)
        dirs = [
            os.path.join("app", "static", "temp_uploads"),
            os.path.join("app", "static", "generated"),
            os.path.join("app", "static", "tmp"),
        ]
        if os.path.isdir("/data"):
            dirs.extend([
                os.path.join("/data", "tmp"),
                os.path.join("/data", "media", "tmp"),
                os.path.join("/data", "media", "cache"),
            ])

        removed_files = 0
        removed_bytes = 0
        for base in dirs:
            if not base or not os.path.isdir(base):
                continue
            for root, _, files in os.walk(base):
                for name in files:
                    path = os.path.join(root, name)
                    try:
                        st = os.stat(path)
                        mtime = datetime.datetime.utcfromtimestamp(st.st_mtime)
                        if mtime <= cutoff:
                            removed_bytes += int(st.st_size or 0)
                            os.remove(path)
                            removed_files += 1
                    except Exception:
                        continue

        try:
            tasks_cutoff = now - datetime.timedelta(days=task_retention_days)
            db = SessionLocal()
            try:
                q = (
                    db.query(VideoTask)
                    .filter(VideoTask.updated_at < tasks_cutoff)
                    .filter(VideoTask.status.in_(["completed", "failed", "cancelled"]))
                )
                deleted = q.delete(synchronize_session=False)
                if deleted:
                    db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            finally:
                db.close()
        except Exception:
            pass

        try:
            warn_pct = int((os.getenv("CODEXIA_DISK_WARN_PCT") or "").strip() or "90")
        except Exception:
            warn_pct = 90
        warn_pct = max(50, min(99, warn_pct))

        try:
            du = shutil.disk_usage("/")
            used_pct = int(round((du.used / max(1, du.total)) * 100))
            if used_pct >= warn_pct:
                db = SessionLocal()
                try:
                    last = (
                        db.query(SystemNotification)
                        .filter(SystemNotification.kind == "disk_space")
                        .order_by(SystemNotification.created_at.desc())
                        .first()
                    )
                    should_create = True
                    if last and last.created_at and (datetime.datetime.utcnow() - last.created_at) < datetime.timedelta(hours=12):
                        should_create = False
                    if should_create:
                        msg = f"Disco do servidor está em {used_pct}% de uso. Faça limpeza de imagens/cache do Docker no servidor (Coolify) para evitar erro 500."
                        db.add(SystemNotification(kind="disk_space", title="Espaço em disco baixo", message=msg))
                        db.commit()
                except Exception:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                finally:
                    db.close()
        except Exception:
            pass

        if removed_files:
            mb = removed_bytes / (1024 * 1024)
            logger.info(f"Housekeeping: removidos {removed_files} arquivos antigos (~{mb:.1f}MB) de diretórios temporários.")

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Monitoramento do canal parado.")

    def process_video_queue(self):
        """Verifica se há vídeos na fila e inicia processamento"""
        db = SessionLocal()
        try:
            recovered_dispatching = _recover_stale_dispatching_videos(db)
            if recovered_dispatching:
                db.commit()

            dispatching = db.query(ScheduledVideo).filter(ScheduledVideo.status == _SCHEDULED_DISPATCHING_STATUS).first()
            if dispatching:
                last_update = dispatching.updated_at or dispatching.scheduled_for or datetime.datetime.now()
                logger.info(f"Fila ocupada: Vídeo {dispatching.id} está em dispatching (Atualizado em: {last_update}).")
                return

            # 1. Check if any video is currently processing (to avoid overload)
            processing = db.query(ScheduledVideo).filter(ScheduledVideo.status == "processing").first()
            if processing:
                processing_payload, processing_policy = _load_scheduled_processing_policy(processing)
                if not processing_policy.get("auto_process_eligible"):
                    _normalize_blocked_scheduled_video(
                        processing,
                        processing_payload,
                        processing_policy,
                        "Monitor encontrou item bloqueado em processing",
                    )
                    db.commit()
                    logger.warning(
                        f"Vídeo {processing.id} removido do auto-run por elegibilidade. "
                        f"source={processing_policy.get('source')} reason={processing_policy.get('reason')}"
                    )
                    return

                # Check for timeout (stuck video)
                # If updated_at is missing (legacy), assume it's stuck if we are here (simplification)
                # or rely on a reasonable default if null.
                
                # Timeout: 40 min normalmente; 90 min se já estiver em fase final (95%+), pois write_videofile é pesado
                is_final_render = (processing.progress or 0) >= 90
                timeout_minutes = 90 if is_final_render else 40
                timeout_limit = datetime.timedelta(minutes=timeout_minutes)
                last_update = processing.updated_at or processing.scheduled_for or datetime.datetime.now()
                
                if datetime.datetime.now() - last_update > timeout_limit:
                    logger.warning(f"Vídeo {processing.id} expirou (timeout {timeout_minutes}min). Marcando como falha.")
                    processing.status = "failed"
                    processing.description = (processing.description or "") + "\n\n[SISTEMA]: Processo expirou (timeout de 40min). Tente novamente."
                    db.commit()
                    # Continue to pick next video in next cycle (or now if we want to be aggressive)
                    return
                
                logger.info(f"Fila ocupada: Vídeo {processing.id} está processando (Atualizado em: {last_update}).")
                return

            # 2. Pick next queued video
            queued_candidates = (
                db.query(ScheduledVideo)
                .filter(ScheduledVideo.status == "queued")
                .order_by(ScheduledVideo.id.asc())
                .limit(100)
                .all()
            )
            next_video = None
            for candidate in queued_candidates:
                payload, policy = _load_scheduled_processing_policy(candidate)
                if policy.get("auto_process_eligible"):
                    next_video = candidate
                    break
                _normalize_blocked_scheduled_video(
                    candidate,
                    payload,
                    policy,
                    "Monitor recusou item bloqueado na fila",
                )
                logger.info(
                    f"Vídeo {candidate.id} ignorado pelo monitor. "
                    f"source={policy.get('source')} reason={policy.get('reason')}"
                )
            if queued_candidates:
                db.commit()
            
            if next_video:
                if not _claim_video_for_dispatch(db, next_video.id):
                    db.rollback()
                    logger.info(f"Vídeo {next_video.id} não pôde ser reservado para dispatch. Outro processo assumiu o item.")
                    return
                db.commit()
                logger.info(f"Iniciando processamento do vídeo agendado {next_video.id}...")
                try:
                    workers_ok = False
                    if redis_conn is not None and _RQ_AVAILABLE and Worker is not None:
                        try:
                            workers_ok = Worker.count(redis_conn) > 0
                        except Exception:
                            workers_ok = False

                    is_mock = (rq_queue.__class__.__name__ == "MockQueue")
                    if redis_conn is not None and workers_ok and (not is_mock):
                        try:
                            timeout_raw = (os.getenv("RQ_VIDEO_TIMEOUT") or os.getenv("RQ_DEFAULT_TIMEOUT") or "").strip()
                            job_timeout = int(timeout_raw) if timeout_raw else 14400
                        except Exception:
                            job_timeout = 14400
                        rq_queue.enqueue(process_scheduled_video, next_video.id, "auto", job_timeout=max(600, job_timeout))
                        logger.info(f"Enfileirado para worker: vídeo {next_video.id}.")
                        return

                    allow_inline = (os.getenv("ALLOW_INLINE_VIDEO_GENERATION") or "").strip().lower() in {"1", "true", "yes"}
                    if allow_inline:
                        process_scheduled_video(next_video.id, "auto")
                        return
                    reserved_video = db.query(ScheduledVideo).filter(ScheduledVideo.id == next_video.id).first()
                    if reserved_video and reserved_video.status == _SCHEDULED_DISPATCHING_STATUS:
                        reserved_video.status = "queued"
                        reserved_video.progress = 0
                        db.commit()
                    logger.warning(f"Worker indisponível. Mantendo vídeo {next_video.id} em fila (queued) para evitar travar o servidor web.")
                    return
                except Exception as e:
                    reserved_video = db.query(ScheduledVideo).filter(ScheduledVideo.id == next_video.id).first()
                    if reserved_video and reserved_video.status == _SCHEDULED_DISPATCHING_STATUS:
                        reserved_video.status = "queued"
                        reserved_video.progress = 0
                        db.commit()
                    logger.error(f"Falha ao enfileirar vídeo {next_video.id} no worker: {e}")
                    return
            else:
                pass # Nothing to do
                
        except Exception as e:
            logger.error(f"Erro no processador de fila: {e}")
        finally:
            db.close()

    def check_scheduled_uploads(self):
        """Verifica vídeos prontos e agendados para upload"""
        db = SessionLocal()
        try:
            now = datetime.datetime.now()
            # Videos that are completed, have auto_post=True, scheduled time passed, and not yet uploaded
            videos_to_upload = db.query(ScheduledVideo).filter(
                ScheduledVideo.status == "completed",
                ScheduledVideo.auto_post == True,
                ScheduledVideo.scheduled_for <= now,
                ScheduledVideo.uploaded_at == None
            ).all()
            
            if videos_to_upload:
                yt_service = None
                for video in videos_to_upload:
                    try:
                        payload = {}
                        if video.script_data:
                            try:
                                payload = json.loads(video.script_data or "{}")
                            except Exception:
                                payload = {}
                        platform = (payload.get("platform") if isinstance(payload, dict) else None) or "youtube"
                        platform = str(platform).strip().lower() or "youtube"

                        if platform == "whatsapp":
                            bridge_url = (os.getenv("WHATSAPP_BRIDGE_URL") or "").strip().rstrip("/")
                            if not bridge_url:
                                video.auto_post = False
                                note = "[UPLOAD_ERRO]: WHATSAPP_BRIDGE_URL não configurado. Não foi possível enviar no WhatsApp."
                                if note not in (video.description or ""):
                                    video.description = ((video.description or "").strip() + "\n\n" + note).strip()
                                db.commit()
                                continue

                            base_message = (payload.get("message") if isinstance(payload, dict) else None) or (video.description or "")
                            recs = payload.get("recipients") if isinstance(payload, dict) else None
                            single_to = (payload.get("to") if isinstance(payload, dict) else None)
                            if (not isinstance(recs, list) or not recs) and single_to:
                                recs = [{"to": single_to, "name": payload.get("name"), "message": payload.get("message")}]
                            if not isinstance(recs, list) or not recs:
                                video.auto_post = False
                                note = "[UPLOAD_ERRO]: Nenhum destinatário configurado para WhatsApp."
                                if note not in (video.description or ""):
                                    video.description = ((video.description or "").strip() + "\n\n" + note).strip()
                                db.commit()
                                continue

                            media_path = None
                            if video.video_url:
                                from app.config import absolute_path_for_video
                                abs_video_path = absolute_path_for_video(video.video_url)
                                if abs_video_path and os.path.exists(abs_video_path):
                                    media_path = abs_video_path
                                else:
                                    video.auto_post = False
                                    note = "[UPLOAD_ERRO]: Arquivo do vídeo não encontrado no servidor. Não foi possível enviar no WhatsApp."
                                    if note not in (video.description or ""):
                                        video.description = ((video.description or "").strip() + "\n\n" + note).strip()
                                    db.commit()
                                    continue

                            sent = 0
                            errors = []
                            for r in recs:
                                if not isinstance(r, dict):
                                    continue
                                to = str(r.get("to") or "").strip()
                                if not to:
                                    continue
                                msg = str(r.get("message") or base_message or "").strip()
                                name = str(r.get("name") or "").strip()
                                if name and "{name}" in msg:
                                    try:
                                        msg = msg.replace("{name}", name)
                                    except Exception:
                                        pass
                                try:
                                    resp = requests.post(
                                        f"{bridge_url}/send",
                                        json={"to": to, "message": msg, "media_path": media_path},
                                        timeout=40,
                                    )
                                    ok = bool(resp.ok)
                                    if not ok:
                                        err = None
                                        try:
                                            body = resp.json()
                                            if isinstance(body, dict):
                                                err = body.get("error") or body.get("detail")
                                        except Exception:
                                            err = None
                                        errors.append(err or f"Falha ({resp.status_code}) para {to}")
                                    else:
                                        sent += 1
                                except Exception as e:
                                    errors.append(f"{to}: {str(e)}")

                            if sent > 0 and not errors:
                                video.uploaded_at = datetime.datetime.now()
                                video.youtube_video_id = f"whatsapp:{sent}"
                                video.status = "published"
                                logger.info(f"Envio WhatsApp concluído para {sent} contato(s).")
                            else:
                                video.auto_post = False
                                if sent <= 0:
                                    video.status = "failed"
                                else:
                                    if (video.status or "").lower() not in {"completed", "ready"}:
                                        video.status = "completed"
                                err_txt = "; ".join([e for e in errors if e])[:800]
                                note = f"[UPLOAD_ERRO]: WhatsApp não enviou para todos os destinatários. Enviados: {sent}. Erros: {err_txt or 'desconhecido'}"
                                if note not in (video.description or ""):
                                    video.description = ((video.description or "").strip() + "\n\n" + note).strip()

                            db.commit()
                            continue

                        from app.services.youtube_service import YouTubeService
                        if yt_service is None:
                            yt_service = YouTubeService()

                        try:
                            from app.models import YouTubeAutoAuditEvent
                            db.add(YouTubeAutoAuditEvent(
                                event_type="upload_started",
                                series_id=int(payload.get("series_id")) if isinstance(payload, dict) and payload.get("series_id") else None,
                                episode_id=int(payload.get("episode_id")) if isinstance(payload, dict) and payload.get("episode_id") else None,
                                scheduled_video_id=int(video.id),
                                status_before=str(video.status),
                                status_after="uploading",
                                payload_json=json.dumps({
                                    "platform": platform,
                                    "scheduled_for": (video.scheduled_for.isoformat() if getattr(video, "scheduled_for", None) else None),
                                }, ensure_ascii=False),
                            ))
                            db.commit()
                        except Exception:
                            try:
                                db.rollback()
                            except Exception:
                                pass

                        logger.info(f"Iniciando upload automático do vídeo {video.id} ({video.title})...")
                        from app.config import absolute_path_for_video
                        abs_video_path = absolute_path_for_video(video.video_url)
                        
                        if not os.path.exists(abs_video_path):
                            logger.error(f"Arquivo de vídeo não encontrado: {abs_video_path}")
                            # NUNCA reenfileirar: vídeo em Aguardando Publicação só sai por publicar ou excluir
                            video.status = "failed"
                            video.progress = 0
                            msg = "[UPLOAD_ERRO]: Arquivo de vídeo não encontrado. Não foi possível publicar. Exclua este item ou agende um novo vídeo."
                            if not (video.description and "[UPLOAD_ERRO]" in video.description):
                                video.description = (video.description or "") + "\n\n" + msg
                            db.commit()
                            continue

                        # Parse script data for tags if available
                        tags = ["motivação", "sucesso"]
                        if video.script_data:
                            try:
                                script = json.loads(video.script_data)
                                if "tags" in script:
                                    tags = script["tags"]
                            except Exception as e:
                                logger.error(f"Erro ao ler tags do script_data (scheduled_video_id={video.id}): {e}")

                        # Check for lateness
                        time_diff = now - video.scheduled_for
                        if time_diff.total_seconds() > 600: # 10 minutes late
                            logger.warning(f"EMERGÊNCIA: Upload do vídeo {video.id} está atrasado em {time_diff}. Iniciando imediatamente.")
                        else:
                            logger.info(f"Iniciando upload automático do vídeo {video.id} ({video.title})...")

                        # Upload
                        # video_path must be relative to app root or absolute
                        # We stored relative path in DB like "/static/videos/..."
                        upload_result = yt_service.upload_video(
                            abs_video_path,
                            title=video.title,
                            description=video.description or "Vídeo gerado automaticamente por Codexia.",
                            tags=tags
                        )
                        
                        # Interpretar resultado do upload:
                        # - Sucesso real: dict com 'id' e sem 'error'
                        # - Mock (sem credenciais): dict com 'id' e status 'uploaded_mock'
                        # - Falha: dict com chave 'error' ou resultado vazio
                        is_error = False
                        video_id_value = None
                        if isinstance(upload_result, dict):
                            if upload_result.get("error"):
                                is_error = True
                            elif upload_result.get("status") == "uploaded_mock":
                                is_error = True
                            else:
                                video_id_value = upload_result.get("id") or str(upload_result)
                        else:
                            # Qualquer outro tipo não-vazio é tratado como sucesso e logado como string
                            if upload_result:
                                video_id_value = str(upload_result)
                            else:
                                is_error = True

                        if is_error or not video_id_value:
                            logger.error(f"Falha no upload do vídeo {video.id}: {upload_result}")
                            # Evita loop infinito de auto-post e mantém item pronto para tentativa manual.
                            video.auto_post = False
                            if (video.status or "").lower() not in {"completed", "ready"}:
                                video.status = "completed"
                            if isinstance(upload_result, dict) and upload_result.get("status") == "uploaded_mock":
                                msg = "Canal não conectado ao YouTube. Configure as credenciais em Configurações antes de publicar."
                            else:
                                msg = (upload_result.get("error") if isinstance(upload_result, dict) else str(upload_result)) or "Falha ao enviar para o YouTube."
                            note = f"[UPLOAD_ERRO]: {msg}"
                            if note not in (video.description or ""):
                                video.description = ((video.description or "").strip() + "\n\n" + note).strip()
                        else:
                            video.uploaded_at = datetime.datetime.now()
                            video.youtube_video_id = video_id_value
                            video.status = "published"
                            logger.info(f"Vídeo {video.id} publicado com sucesso! ID: {video_id_value}")
                            try:
                                from app.services.youtube_series_service import youtube_series_service
                                youtube_series_service.update_publication_state_from_schedule(db, scheduled_video_id=int(video.id))
                            except Exception as e:
                                logger.error(f"Erro ao sincronizar publicação do episódio a partir do ScheduledVideo {video.id}: {e}")
                            try:
                                from app.models import YouTubeAutoAuditEvent
                                db.add(YouTubeAutoAuditEvent(
                                    event_type="upload_completed",
                                    series_id=int(payload.get("series_id")) if isinstance(payload, dict) and payload.get("series_id") else None,
                                    episode_id=int(payload.get("episode_id")) if isinstance(payload, dict) and payload.get("episode_id") else None,
                                    scheduled_video_id=int(video.id),
                                    status_before="uploading",
                                    status_after="published",
                                    payload_json=json.dumps({
                                        "youtube_video_id": str(video_id_value),
                                        "uploaded_at": video.uploaded_at.isoformat() if getattr(video, "uploaded_at", None) else None,
                                    }, ensure_ascii=False),
                                ))
                            except Exception:
                                pass
                            
                        db.commit()
                        
                    except Exception as e:
                        logger.error(f"Erro ao fazer upload do vídeo {video.id}: {e}")
                        
        except Exception as e:
            logger.error(f"Erro no verificador de uploads: {e}")
        finally:
            db.close()

    def check_channel_status(self):
        # Lazy import para economizar memória no startup
        from app.services.youtube_service import YouTubeService
        from app.services.ai_generator import AIContentGenerator

        logger.info(f"[{datetime.datetime.now()}] Executando verificação de canal...")
        db = SessionLocal()
        try:
            yt_service = YouTubeService()
            stats = yt_service.get_channel_stats()
            
            # Even if not connected (mock data), we can generate a report for testing purposes if desired,
            # but usually we want real data. For now, let's proceed if stats are returned.
            
            # Analyze with IA
            ai_service = AIContentGenerator()
            report_data = ai_service.generate_monitor_report(stats)
            
            # Save Report
            report = ChannelReport(
                subscribers=int(stats.get('subscribers', 0)),
                views=int(stats.get('views', 0)),
                videos=int(stats.get('videos', 0)),
                analysis_text=report_data.get('analysis', 'Sem análise'),
                strategy_suggestion=report_data.get('strategy', 'Sem sugestão')
            )
            db.add(report)
            db.commit()
            logger.info("Relatório de monitoramento salvo com sucesso.")
            
        except Exception as e:
            logger.error(f"Erro no monitoramento: {e}")
        finally:
            db.close()

    def check_new_comments(self):
        from app.services.youtube_service import YouTubeService
        from app.models import Settings
        from app.services.youtube_auto_responder import auto_thank_comments

        db = SessionLocal()
        try:
            yt = YouTubeService()
            if not yt.service:
                return

            try:
                max_results = int((os.getenv("COMMENTS_CHANNEL_MAX_RESULTS") or "").strip() or "200")
            except Exception:
                max_results = 200
            max_results = max(10, min(500, max_results))

            try:
                initial_lookback_hours = int((os.getenv("COMMENTS_INITIAL_LOOKBACK_HOURS") or "").strip() or "48")
            except Exception:
                initial_lookback_hours = 48
            initial_lookback_hours = max(1, min(24 * 30, initial_lookback_hours))

            settings = db.query(Settings).first()
            if not settings:
                settings = Settings()
                db.add(settings)
                db.commit()

            last_sync = getattr(settings, "youtube_comments_last_sync_at", None)
            if not last_sync:
                last_sync = datetime.datetime.utcnow() - datetime.timedelta(hours=initial_lookback_hours)
                settings.youtube_comments_last_sync_at = last_sync
                db.commit()

            def _parse_utc_naive(ts: str):
                try:
                    s = (ts or "").strip()
                    if not s:
                        return None
                    s = s.replace("Z", "+00:00")
                    dt = datetime.datetime.fromisoformat(s)
                    if dt.tzinfo is not None:
                        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
                    return dt
                except Exception:
                    return None

            new_items = []
            newest_seen = None
            try:
                raw = yt.list_channel_comments(max_results=max_results)
            except Exception as e:
                logger.error(f"Erro ao verificar comentários (YouTube API): {e}")
                return

            reply_by_owner = {}
            for c in raw or []:
                try:
                    if c.get("youtube_parent_id") and c.get("author_is_channel_owner"):
                        pid = c.get("youtube_parent_id")
                        if pid and pid not in reply_by_owner:
                            reply_by_owner[pid] = c
                except Exception:
                    continue

            for pid, rep in reply_by_owner.items():
                try:
                    top = db.query(CommunityComment).filter(CommunityComment.youtube_comment_id == pid).first()
                    if top and top.status != "replied":
                        top.status = "replied"
                        txt = (rep.get("text") or "").strip()
                        if txt:
                            top.reply_text = txt
                        rep_dt = _parse_utc_naive(rep.get("published_at") or "")
                        if rep_dt:
                            top.reply_sent_at = rep_dt
                except Exception:
                    pass

            for c in raw or []:
                cid = (c or {}).get("youtube_comment_id")
                if not cid:
                    continue

                published_dt = _parse_utc_naive((c.get("published_at") or ""))
                if published_dt and published_dt <= last_sync:
                    continue

                exists = db.query(CommunityComment).filter(CommunityComment.youtube_comment_id == cid).first()
                if exists:
                    continue

                item = CommunityComment(
                    youtube_comment_id=cid,
                    youtube_parent_id=c.get("youtube_parent_id"),
                    youtube_video_id=c.get("youtube_video_id"),
                    author=c.get("author"),
                    text=(c.get("text") or "").strip(),
                    like_count=int(c.get("like_count", 0) or 0),
                    published_at=published_dt,
                    status="new",
                )
                if item.youtube_parent_id is None and item.youtube_comment_id in reply_by_owner:
                    item.status = "replied"
                    txt = (reply_by_owner[item.youtube_comment_id].get("text") or "").strip()
                    if txt:
                        item.reply_text = txt
                    rep_dt = _parse_utc_naive(reply_by_owner[item.youtube_comment_id].get("published_at") or "")
                    if rep_dt:
                        item.reply_sent_at = rep_dt
                db.add(item)
                new_items.append(item)
                if published_dt and (newest_seen is None or published_dt > newest_seen):
                    newest_seen = published_dt

                if len(new_items) <= 10:
                    msg = (item.text or "")[:240]
                    payload = {
                        "youtube_video_id": item.youtube_video_id,
                        "youtube_comment_id": item.youtube_comment_id,
                        "youtube_parent_id": item.youtube_parent_id,
                        "author": item.author,
                    }
                    db.add(SystemNotification(
                        user_id=None,
                        kind="youtube_comment",
                        title="Novo comentário no YouTube",
                        message=f"{(item.author or 'Usuário')}: {msg}",
                        payload_json=json.dumps(payload, ensure_ascii=False),
                        status="new",
                    ))

            if newest_seen and (getattr(settings, "youtube_comments_last_sync_at", None) is None or newest_seen > (settings.youtube_comments_last_sync_at or datetime.datetime.min)):
                settings.youtube_comments_last_sync_at = newest_seen

            if new_items or reply_by_owner:
                db.commit()

            try:
                auto_thank_comments(db)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Erro ao verificar comentários: {e}")
        finally:
            db.close()

    def check_subscriber_insights(self):
        from app.services.youtube_service import YouTubeService

        db = SessionLocal()
        try:
            last = db.query(ChannelInsight).filter(ChannelInsight.kind == "subscribers_14d").order_by(ChannelInsight.id.desc()).first()
            if last and last.created_at and (datetime.datetime.utcnow() - last.created_at) < datetime.timedelta(hours=2):
                return

            yt = YouTubeService()
            if not yt.service:
                return

            data = yt.get_subscriber_insights(days=14, max_results=20) or {}
            insight = ChannelInsight(
                user_id=None,
                kind="subscribers_14d",
                start_date=None,
                end_date=None,
                data_json=json.dumps(data, ensure_ascii=False),
                ai_summary=None,
            )
            db.add(insight)

            if not data.get("error"):
                totals = data.get("totals") or {}
                gained = totals.get("subscribersGained", 0)
                lost = totals.get("subscribersLost", 0)
                sources = data.get("subscriber_sources") or []
                top = sources[0]["source"] if sources and isinstance(sources[0], dict) else None
                message = f"Últimos 14 dias: +{gained} ganhos / -{lost} perdas."
                if top:
                    message = f"{message} Top fonte: {top}."
                db.add(SystemNotification(
                    user_id=None,
                    kind="subscriber_insights",
                    title="Insights de inscritos",
                    message=message,
                    payload_json=json.dumps({"days": 14, "totals": totals, "top_source": top}, ensure_ascii=False),
                    status="new",
                ))

            db.commit()
        except Exception as e:
            logger.error(f"Erro ao gerar insights de inscritos: {e}")
        finally:
            db.close()

monitor_service = MonitorService()
