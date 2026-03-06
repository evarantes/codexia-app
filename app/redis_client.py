import os
import redis
try:
    from rq import Queue
    RQ_AVAILABLE = True
except Exception:
    # No Windows, RQ pode falhar devido ao fork()
    RQ_AVAILABLE = False
    Queue = None

redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
try:
    conn = redis.from_url(redis_url)
    if RQ_AVAILABLE and Queue is not None:
        queue = Queue('default', connection=conn)
    else:
        queue = None
except Exception as e:
    print(f"Warning: Redis connection failed: {e}. Worker functionality will be disabled.")
    conn = None
    queue = None
