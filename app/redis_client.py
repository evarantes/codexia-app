import os
import sys

import redis


class QueueUnavailableError(RuntimeError):
    """Erro explícito quando a fila RQ não está disponível.

    Em produção o Codexia deve falhar fechado: uma indisponibilidade temporária
    do Redis nunca pode transformar uma tarefa pesada em execução inline no
    processo web (CPX22).
    """


class UnavailableQueue:
    def __init__(self, reason: str):
        self.reason = str(reason or "Redis/RQ indisponível")

    def _raise(self):
        raise QueueUnavailableError(
            "Fila de processamento indisponível. A produção não foi executada "
            f"no servidor web. Motivo: {self.reason}"
        )

    def enqueue(self, *args, **kwargs):
        self._raise()

    def enqueue_in(self, *args, **kwargs):
        self._raise()


class DevelopmentInlineQueue:
    """Fallback apenas para desenvolvimento explícito/Windows.

    Nunca é usado automaticamente em produção.
    """

    def enqueue(self, func, *args, **kwargs):
        print(f"DevelopmentInlineQueue: Executing {func.__name__} inline (development only)")
        return func(*args, **kwargs)

    def enqueue_in(self, _delta, func, *args, **kwargs):
        return self.enqueue(func, *args, **kwargs)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name) or "").strip() or default)
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _inline_fallback_allowed() -> bool:
    app_env = str(os.getenv("APP_ENV") or "").strip().lower()
    explicit = str(os.getenv("ALLOW_INLINE_VIDEO_GENERATION") or "").strip().lower()
    if explicit in {"0", "false", "no", "off"}:
        return False
    # Mesmo no Windows, produção nunca executa vídeo pesado inline.
    if app_env in {"production", "prod"}:
        return False
    return bool(sys.platform == "win32" or explicit in {"1", "true", "yes", "on"})


queue = None
conn = None

try:
    if sys.platform == "win32":
        raise ImportError("RQ does not support Windows due to fork() dependency.")

    from rq import Queue

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    connect_timeout = _env_int("REDIS_CONNECT_TIMEOUT_SECONDS", 5, 2, 60)
    socket_timeout = _env_int("REDIS_SOCKET_TIMEOUT_SECONDS", 10, 3, 120)
    health_interval = _env_int("REDIS_HEALTH_CHECK_INTERVAL_SECONDS", 30, 5, 300)

    conn = redis.from_url(
        redis_url,
        socket_connect_timeout=connect_timeout,
        socket_timeout=socket_timeout,
        socket_keepalive=True,
        health_check_interval=health_interval,
        retry_on_timeout=True,
    )
    # Valida autenticação e leitura antes de expor a conexão ao restante do app.
    conn.ping()

    try:
        default_timeout = int((os.getenv("RQ_DEFAULT_TIMEOUT") or "").strip() or "14400")
    except Exception:
        default_timeout = 14400
    default_timeout = max(600, min(24 * 60 * 60, default_timeout))
    queue = Queue("default", connection=conn, default_timeout=default_timeout)
except Exception as e:
    reason = f"{type(e).__name__}: {e}"
    conn = None
    if _inline_fallback_allowed():
        print(f"Warning: Redis/RQ initialization failed: {reason}. Development inline fallback enabled.")
        queue = DevelopmentInlineQueue()
    else:
        print(
            "ERROR: Redis/RQ initialization failed. Heavy worker execution is fail-closed; "
            f"no inline production will run. Reason: {reason}"
        )
        queue = UnavailableQueue(reason)
