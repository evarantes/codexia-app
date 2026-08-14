import os
import sys

# Adiciona o diretório raiz ao PYTHONPATH para imports funcionarem
sys.path.append(os.getcwd())

from rq import Worker, Queue
from dotenv import load_dotenv
from app.redis_client import conn

load_dotenv()

from app.database import DATABASE_DISPLAY
from app.services.audio_checkpoint import install_audio_checkpoint_patch

# O worker CX33 precisa persistir o MP3 assim que o TTS termina, antes de
# qualquer crítica/validação/render posterior. A instalação é idempotente.
install_audio_checkpoint_patch()

listen = ['default']

if __name__ == '__main__':
    if not conn:
        print("Redis connection not available. Exiting.")
        exit(1)
        
    print(f"Starting Worker... Listening on {listen} | Banco: {DATABASE_DISPLAY}")
    queues = [Queue(name, connection=conn) for name in listen]
    worker = Worker(queues, connection=conn)
    worker.work()
