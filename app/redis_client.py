import os
import redis
import sys

# RQ (Redis Queue) requires fork(), which is not available on Windows.
# We'll use a dummy queue implementation for Windows or when RQ fails.
queue = None
conn = None

try:
    if sys.platform == 'win32':
        raise ImportError("RQ does not support Windows due to fork() dependency.")
        
    from rq import Queue
    
    redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
    conn = redis.from_url(redis_url)
    queue = Queue('default', connection=conn)
except Exception as e:
    print(f"Warning: Redis/RQ initialization failed: {e}. Worker functionality will be disabled/mocked.")
    
    # Mock Queue for Windows/Fallback
    class MockQueue:
        def enqueue(self, func, *args, **kwargs):
            print(f"MockQueue: Executing {func.__name__} immediately (No Redis)")
            return func(*args, **kwargs)
            
    queue = MockQueue()
