import os
import redis
from rq import Worker, Queue, Connection
from dotenv import load_dotenv

load_dotenv()

from app.database import DATABASE_DISPLAY

listen = ['default']

redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')

def run_worker():
    try:
        conn = redis.from_url(redis_url)
        with Connection(conn):
            worker = Worker(list(map(Queue, listen)))
            print(f"Worker iniciado, aguardando jobs... Banco: {DATABASE_DISPLAY}")
            worker.work()
    except Exception as e:
        print(f"Erro ao iniciar worker: {e}")

if __name__ == '__main__':
    run_worker()
