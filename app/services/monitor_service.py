from apscheduler.schedulers.background import BackgroundScheduler
from app.services.youtube_service import YouTubeService
from app.services.ai_generator import AIContentGenerator
from app.services.video_processing import process_scheduled_video
from app.database import SessionLocal
from app.models import ChannelReport, ScheduledVideo
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

    def start(self):
        if not self.job:
            # Run every 10 minutes
            self.job = self.scheduler.add_job(self.check_channel_status, 'interval', minutes=10)
            # Run video queue check every 1 minute, starting immediately
            self.queue_job = self.scheduler.add_job(
                self.process_video_queue, 
                'interval', 
                minutes=1, 
                max_instances=1,
                next_run_time=datetime.datetime.now()
            )
            # Run upload check every 5 minutes, starting immediately (catch up on missed uploads)
            self.upload_job = self.scheduler.add_job(
                self.check_scheduled_uploads, 
                'interval', 
                minutes=5,
                next_run_time=datetime.datetime.now()
            )
            
            # Executar verificação de integridade de arquivos (Self-Healing)
            self.check_file_integrity()
            
            self.scheduler.start()
            logger.info("Monitoramento do canal, processador de fila e agendador de uploads iniciados.")

    def check_file_integrity(self):
        """Verifica se os arquivos de vídeos 'completos' realmente existem no disco.
           Se não existirem (ex: Render reiniciou), marca como 'queued' para regenerar."""
        logger.info("Verificando integridade dos arquivos de vídeo...")
        db = SessionLocal()
        try:
            # Pega vídeos marcados como prontos mas ainda não upados
            videos = db.query(ScheduledVideo).filter(
                ScheduledVideo.status == "completed",
                ScheduledVideo.uploaded_at == None
            ).all()
            
            restored_count = 0
            for video in videos:
                if not video.video_url:
                    continue
                    
                # Caminho relativo seguro (remove primeira barra se houver)
                rel_path = video.video_url.lstrip('/')
                if rel_path.startswith("static"):
                     # Ajuste para estrutura do projeto: app/static/...
                     rel_path = os.path.join("app", rel_path)
                
                abs_path = os.path.join(os.getcwd(), rel_path)
                
                if not os.path.exists(abs_path):
                    logger.warning(f"Arquivo sumiu para vídeo {video.id} ({video.title}). Reiniciando geração...")
                    video.status = "queued"
                    video.progress = 0
                    restored_count += 1
            
            if restored_count > 0:
                db.commit()
                logger.info(f"Recuperação: {restored_count} vídeos retornados para a fila de geração.")
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
                # Optional: Check if stuck (e.g. > 1 hour) and reset?
                # For now, just wait.
                logger.info(f"Fila ocupada: Vídeo {processing.id} está processando.")
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
                yt_service = YouTubeService()
                for video in videos_to_upload:
                    logger.info(f"Iniciando upload automático do vídeo {video.id} ({video.title})...")
                    try:
                        # Construct absolute path (Platform Independent)
                        # video.video_url is usually "/static/videos/..."
                        rel_path = video.video_url.lstrip('/')
                        if rel_path.startswith("static"):
                             rel_path = os.path.join("app", rel_path)
                             
                        abs_video_path = os.path.join(os.getcwd(), rel_path)
                        
                        if not os.path.exists(abs_video_path):
                            logger.error(f"Arquivo de vídeo não encontrado: {abs_video_path}")
                            # Se o arquivo não existe, marcamos para regenerar (queued) em vez de ignorar
                            logger.info(f"Tentando recuperar vídeo {video.id} reenviando para fila...")
                            video.status = "queued"
                            video.progress = 0
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
                        video_id = yt_service.upload_video(
                            abs_video_path,
                            title=video.title,
                            description=video.description or "Vídeo gerado automaticamente por Codexia.",
                            tags=tags
                        )
                        
                        if video_id:
                            video.uploaded_at = datetime.datetime.now()
                            video.youtube_video_id = video_id
                            video.status = "published"
                            logger.info(f"Vídeo {video.id} publicado com sucesso! ID: {video_id}")
                        else:
                            logger.error(f"Falha no upload do vídeo {video.id}")
                            
                        db.commit()
                        
                    except Exception as e:
                        logger.error(f"Erro ao fazer upload do vídeo {video.id}: {e}")
                        
        except Exception as e:
            logger.error(f"Erro no verificador de uploads: {e}")
        finally:
            db.close()

    def check_channel_status(self):
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

monitor_service = MonitorService()
