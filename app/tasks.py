from app.database import SessionLocal
from app.services.video_factory import VideoFactory
from app.models import Job
from app.redis_client import conn, queue
from datetime import timedelta
from uuid import uuid4

FACTORY_LOCK_KEY = "codexia:video_factory:single_worker_lock"

def process_job_task(job_id: int):
    """Tarefa executada pelo Worker"""
    db = SessionLocal()
    lock = None
    try:
        factory = VideoFactory(db)
        job = db.query(Job).get(job_id)
        if not job:
            print(f"Job {job_id} not found")
            return

        # Evita reprocessar jobs já finalizados (pode acontecer por retries/dup no Redis).
        if job.status in ("completed", "failed", "paused", "cancelled"):
            return

        # Se outro worker já está com este job em execução, ignora duplicata.
        if job.status == "processing":
            return

        # Serializa toda a fábrica: um job por vez para reduzir consumo e travamentos.
        if conn:
            try:
                lock = conn.lock(FACTORY_LOCK_KEY, timeout=4 * 60 * 60, blocking_timeout=1)
                if not lock.acquire(blocking=False):
                    if (job.status or "").lower() != "pending":
                        job.status = "pending"
                    line = "Aguardando execução sequencial..."
                    logs = job.logs or ""
                    if not logs.endswith(line + "\n"):
                        job.logs = logs + ("" if not logs else "") + f"{line}\n"
                    db.commit()
                    if queue:
                        try:
                            queue.enqueue_in(
                                timedelta(seconds=20),
                                process_job_task,
                                job.id,
                                job_id=f"video_job_retry_{job.id}_{uuid4().hex[:8]}"
                            )
                        except Exception:
                            queue.enqueue(process_job_task, job.id, job_id=f"video_job_retry_{job.id}_{uuid4().hex[:8]}")
                    return
            except Exception as e:
                print(f"Erro ao adquirir lock global da fábrica: {e}")
                lock = None

        print(f"Worker Processing Job {job_id} - Step: {job.step}")
        factory.process_job(job)
    except Exception as e:
        print(f"Error in job {job_id}: {e}")
    finally:
        if lock:
            try:
                lock.release()
            except Exception:
                pass
        db.close()
