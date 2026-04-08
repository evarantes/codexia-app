from apscheduler.schedulers.background import BackgroundScheduler
from app.services.video_processing import process_scheduled_video
from app.database import SessionLocal, SQLALCHEMY_DATABASE_URL
from app.models import ChannelReport, ScheduledVideo, CommunityComment, SystemNotification, ChannelInsight
import datetime
import logging
import json
import os

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MonitorService:
    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.job = None
        self.queue_job = None
        self.upload_job = None
        self.comments_job = None
        self.insights_job = None

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
            # Run upload check every 5 minutes; first run after 2 min to avoid overload at startup (Coolify/Render)
            self.upload_job = self.scheduler.add_job(
                self.check_scheduled_uploads,
                'interval',
                minutes=5,
                next_run_time=datetime.datetime.now() + datetime.timedelta(minutes=2)
            )
            # Backup SQLite 1x por dia (só quando banco é SQLite)
            if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
                from app.services.backup_service import run_sqlite_backup
                self.scheduler.add_job(run_sqlite_backup, "cron", hour=3, minute=0)
                logger.info("Backup SQLite diário agendado (03:00).")
            
            # Executar verificação de integridade de arquivos (Self-Healing)
            # Rodar imediatamente no startup
            self.check_file_integrity()
            # E agendar para rodar a cada 30 minutos para pegar arquivos deletados (ephemeral storage)
            self.scheduler.add_job(self.check_file_integrity, 'interval', minutes=30)

            self.comments_job = self.scheduler.add_job(
                self.check_new_comments,
                "interval",
                minutes=10,
                max_instances=1,
                next_run_time=datetime.datetime.now() + datetime.timedelta(minutes=2)
            )
            self.insights_job = self.scheduler.add_job(
                self.check_subscriber_insights,
                "interval",
                minutes=10,
                max_instances=1,
                next_run_time=datetime.datetime.now() + datetime.timedelta(minutes=2)
            )
            
            self.scheduler.start()
            logger.info("Monitoramento do canal, processador de fila e agendador de uploads iniciados.")

    def _reset_stuck_videos(self):
        """Reseta vídeos que ficaram presos em 'processing' devido a reinicialização do servidor"""
        db = SessionLocal()
        try:
            stuck_videos = db.query(ScheduledVideo).filter(ScheduledVideo.status == "processing").all()
            if stuck_videos:
                logger.warning(f"Encontrados {len(stuck_videos)} vídeos presos em 'processing'. Verificando retries...")
                for video in stuck_videos:
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
                    
                from app.config import absolute_path_for_video
                abs_path = absolute_path_for_video(video.video_url)
                
                if not os.path.exists(abs_path):
                    # Tentar recuperação inteligente (Auto-Heal) se houver script em cache
                    has_script_cache = False
                    try:
                        if video.script_data:
                            s_data = json.loads(video.script_data)
                            if s_data.get("scenes") and isinstance(s_data.get("scenes"), list):
                                has_script_cache = True
                    except:
                        pass

                    if has_script_cache:
                        logger.warning(f"Arquivo sumiu para vídeo {video.id}. Reenfileirando para recuperação GRATUITA (via cache).")
                        video.status = "queued"
                        video.progress = 0
                        # Não adiciona msg de erro pois será recuperado automaticamente
                    else:
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
            logger.error(f"Erro na verificação de integridade: {e}")
        finally:
            db.close()

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Monitoramento do canal parado.")

    def process_video_queue(self):
        """Verifica se há vídeos na fila e inicia processamento"""
        db = SessionLocal()
        try:
            # 1. Check if any video is currently processing (to avoid overload)
            processing = db.query(ScheduledVideo).filter(ScheduledVideo.status == "processing").first()
            if processing:
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
            next_video = db.query(ScheduledVideo).filter(ScheduledVideo.status == "queued").order_by(ScheduledVideo.id.asc()).first()
            
            if next_video:
                logger.info(f"Iniciando processamento do vídeo agendado {next_video.id}...")
                # We call the processor directly (synchronously in this thread)
                # Since we use max_instances=1, this won't overlap with itself.
                process_scheduled_video(next_video.id)
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
                from app.services.youtube_service import YouTubeService
                yt_service = YouTubeService()
                for video in videos_to_upload:
                    logger.info(f"Iniciando upload automático do vídeo {video.id} ({video.title})...")
                    try:
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
                            except:
                                pass

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

        db = SessionLocal()
        try:
            yt = YouTubeService()
            if not yt.service:
                return

            videos = yt.get_recent_videos_performance(max_results=8) or []
            new_items = []
            for v in videos[:5]:
                yid = (v or {}).get("videoId")
                if not yid:
                    continue
                try:
                    raw = yt.list_video_comments(yid, max_results=80)
                except Exception:
                    continue

                for c in raw or []:
                    cid = (c or {}).get("youtube_comment_id")
                    if not cid:
                        continue
                    exists = db.query(CommunityComment).filter(CommunityComment.youtube_comment_id == cid).first()
                    if exists:
                        continue

                    published_at = None
                    try:
                        s = (c.get("published_at") or "").strip()
                        if s:
                            s = s.replace("Z", "+00:00")
                            published_at = datetime.datetime.fromisoformat(s)
                    except Exception:
                        published_at = None

                    item = CommunityComment(
                        youtube_comment_id=cid,
                        youtube_parent_id=c.get("youtube_parent_id"),
                        youtube_video_id=c.get("youtube_video_id") or yid,
                        author=c.get("author"),
                        text=(c.get("text") or "").strip(),
                        like_count=int(c.get("like_count", 0) or 0),
                        published_at=published_at,
                        status="new",
                    )
                    db.add(item)
                    new_items.append(item)

                    if len(new_items) <= 5:
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

            if new_items:
                db.commit()
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
