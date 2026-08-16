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
from app.services.visual_quality_shadow import install_visual_quality_shadow_patch
from app.services.visual_quality_guard import install_visual_quality_guard_patch
from app.services.visual_quality_rollout import apply_visual_quality_observe_rollout

# O worker CX33 precisa persistir o MP3 assim que o TTS termina, antes de
# qualquer crítica/validação/render posterior. A instalação é idempotente.
video_generator_cls = install_audio_checkpoint_patch()

# Fase 1: observação local não bloqueante, sem chamadas pagas.
install_visual_quality_shadow_patch(video_generator_cls)

# Rollout controlado da Fase 2: liga SOMENTE o crítico multimodal em modo
# observação quando não houver override explícito no ambiente. Não ativa
# rejeição estrita, fail-closed nem regeneração automática.
visual_rollout = apply_visual_quality_observe_rollout()

# Fase 2: crítico visual + retry seletivo na MESMA classe canônica.
# Neste rollout, o crítico observa e registra notas; retries continuam
# desligados enquanto ENABLE_STRICT_VISUAL_REJECT não for explicitamente true.
install_visual_quality_guard_patch(video_generator_cls)

listen = ['default']

if __name__ == '__main__':
    if not conn:
        print("Redis connection not available. Exiting.")
        exit(1)

    print(
        f"Starting Worker... Listening on {listen} | Banco: {DATABASE_DISPLAY} | "
        f"VisualCritic={visual_rollout['ai_critic_enabled']} | "
        f"StrictVisualReject={visual_rollout['strict_visual_reject']}"
    )
    queues = [Queue(name, connection=conn) for name in listen]
    worker = Worker(queues, connection=conn)
    worker.work()
