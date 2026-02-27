import os
import redis
from rq import Queue

redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
try:
    conn = redis.from_url(redis_url)
    queue = Queue('default', connection=conn)
except Exception as e:
    print(f"Warning: Redis connection failed: {e}. Worker functionality will be disabled.")
    conn = None
    queue = None
