import os
import threading
from datetime import timedelta
from uuid import uuid4

from app.database import SessionLocal
from app.models import Job
from app.redis_client import conn, queue
from app.services.video_factory import VideoFactory

FACTORY_LOCK_KEY = "codexia:video_factory:single_worker_lock"


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name) or "").strip() or default)
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _factory_lock_ttl_seconds() -> int:
    # Lease curta e renovável. Se o worker morrer, o lock desaparece em poucos
    # minutos em vez de bloquear a fábrica por 4 horas.
    return _env_int("VIDEO_FACTORY_LOCK_TTL_SECONDS", 180, 60, 900)


def _factory_lock_heartbeat_seconds(ttl_seconds: int) -> int:
    configured = _env_int("VIDEO_FACTORY_LOCK_HEARTBEAT_SECONDS", 30, 10, 300)
    return max(10, min(configured, max(10, ttl_seconds // 3)))


def _start_lock_heartbeat(lock, ttl_seconds: int):
    stop_event = threading.Event()
    interval = _factory_lock_heartbeat_seconds(ttl_seconds)

    def _beat():
        while not stop_event.wait(interval):
            try:
                # redis-py Lock mantém token de propriedade; extend falha se o
                # lock já não pertencer a este executor.
                lock.extend(ttl_seconds, replace_ttl=True)
            except Exception as exc:
                print(f"Factory lock heartbeat stopped: {type(exc).__name__}: {exc}")
                break

    thread = threading.Thread(
        target=_beat,
        name="codexia-factory-lock-heartbeat",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def process_job_task(job_id: int):
    """Tarefa executada pelo Worker."""
    db = SessionLocal()
    lock = None
    heartbeat_stop = None
    heartbeat_thread = None
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

        # Serializa a fábrica com uma lease curta e renovável. A antiga lease de
        # 4 horas deixava o servidor falsamente ocupado após morte do executor.
        if conn:
            try:
                ttl_seconds = _factory_lock_ttl_seconds()
                lock = conn.lock(
                    FACTORY_LOCK_KEY,
                    timeout=ttl_seconds,
                    blocking_timeout=1,
                    thread_local=False,
                )
                if not lock.acquire(blocking=False):
                    lock = None
                    if (job.status or "").lower() != "pending":
                        job.status = "pending"
                    line = "Aguardando execução sequencial..."
                    logs = job.logs or ""
                    if not logs.endswith(line + "\n"):
                        job.logs = logs + f"{line}\n"
                    db.commit()
                    try:
                        queue.enqueue_in(
                            timedelta(seconds=20),
                            process_job_task,
                            job.id,
                            job_id=f"video_job_retry_{job.id}_{uuid4().hex[:8]}",
                        )
                    except Exception as retry_exc:
                        # Fail-closed: jamais executar o job inline quando a fila
                        # estiver indisponível. O monitor/UI poderá reenfileirar.
                        print(
                            "Não foi possível reagendar job no RQ; execução inline bloqueada: "
                            f"{type(retry_exc).__name__}: {retry_exc}"
                        )
                    return
                heartbeat_stop, heartbeat_thread = _start_lock_heartbeat(lock, ttl_seconds)
            except Exception as e:
                print(f"Erro ao adquirir lock global da fábrica: {e}")
                lock = None
                # Se Redis estava configurado mas falhou, não prossegue sem lock.
                # Isso evita que o CPX22 execute trabalho pesado em paralelo.
                if conn is not None:
                    return

        print(f"Worker Processing Job {job_id} - Step: {job.step}")
        factory.process_job(job)
    except Exception as e:
        print(f"Error in job {job_id}: {e}")
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None and heartbeat_thread.is_alive():
            heartbeat_thread.join(timeout=2)
        if lock:
            try:
                lock.release()
            except Exception:
                pass
        db.close()
