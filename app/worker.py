35
import os
import sys

# Adiciona o diretório raiz ao PYTHONPATH para imports funcionarem
sys.path.append(os.getcwd())

try:
    from rq import Worker, Queue
    RQ_AVAILABLE = True
except Exception:
    # No Windows, RQ pode falhar devido ao fork()
    RQ_AVAILABLE = False
    Worker = None
    Queue = None

from dotenv import load_dotenv
from app.redis_client import conn

load_dotenv()

listen = ['default']

if __name__ == '__main__':
    if not conn or not RQ_AVAILABLE or Worker is None or Queue is None:
        print("Redis connection or RQ not available. Exiting.")
        exit(1)
        
    print(f"Starting Worker... Listening on {listen}")
    queues = [Queue(name, connection=conn) for name in listen]
    worker = Worker(queues, connection=conn)
    worker.work()
