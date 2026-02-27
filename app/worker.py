import os
import sys

# Adiciona o diretório raiz ao PYTHONPATH para imports funcionarem
sys.path.append(os.getcwd())

from rq import Worker, Queue, Connection
from dotenv import load_dotenv
from app.redis_client import conn

load_dotenv()

listen = ['default']

if __name__ == '__main__':
    if not conn:
        print("Redis connection not available. Exiting.")
        exit(1)
        
    print(f"Starting Worker... Listening on {listen}")
    with Connection(conn):
        worker = Worker(list(map(Queue, listen)))
        worker.work()
