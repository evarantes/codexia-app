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
from app.services.scene_director_shadow import install_scene_director_shadow_patch
from app.services.scene_director_active import install_scene_director_active_patch
from app.services.cinematic_captions import apply_presentation_rollout, install_cinematic_caption_patch
from app.services.channel_excellence_guard import apply_channel_excellence_rollout, install_channel_excellence_guard_patch
from app.services.final_video_presentation_guard import install_final_video_presentation_guard
from app.services.narrative_editor import install_narrative_editor_patch

# O worker CX33 precisa persistir o MP3 assim que o TTS termina, antes de
# qualquer crítica/validação/render posterior. A instalação é idempotente.
video_generator_cls = install_audio_checkpoint_patch()

# Fase 1: observação local não bloqueante, sem chamadas pagas.
install_visual_quality_shadow_patch(video_generator_cls)

# Rollout controlado da Fase 2: crítico visual ativo e, por padrão, no máximo
# uma regeneração seletiva SOMENTE quando o guard reprovar defeito crítico.
# VISUAL_QA_FAIL_CLOSED permanece false, portanto QA nunca derruba o pipeline.
visual_rollout = apply_visual_quality_observe_rollout()
install_visual_quality_guard_patch(video_generator_cls)

# Auditoria de variedade permanece para comparação antes/depois.
install_scene_director_shadow_patch(video_generator_cls)

# Direção visual + legenda premium.
presentation_rollout = apply_presentation_rollout()
install_scene_director_active_patch(video_generator_cls)
install_cinematic_caption_patch(video_generator_cls)

# Pacote de excelência: guard de pronúncia, narração aprovada, unicidade visual
# e trava final de qualidade.
excellence_rollout = apply_channel_excellence_rollout()
install_channel_excellence_guard_patch(video_generator_cls)

# Endcard premium é instalado depois do patch antigo para ter precedência e
# jamais reaproveitar a última cena como encerramento.
install_final_video_presentation_guard(video_generator_cls)

# Editor Narrativo fica como camada externa: revisa título/texto primeiro.
# Se a IA editorial falhar, preserva o plano e continua (fail-open).
install_narrative_editor_patch(video_generator_cls)

listen = ['default']

if __name__ == '__main__':
    if not conn:
        print("Redis connection not available. Exiting.")
        exit(1)

    print(
        f"Starting Worker... Listening on {listen} | Banco: {DATABASE_DISPLAY} | "
        f"VisualCritic={visual_rollout['ai_critic_enabled']} | "
        f"StrictVisualReject={visual_rollout['strict_visual_reject']} | "
        f"VisualRetries={visual_rollout['max_retries']} | "
        f"VisualFailClosed={visual_rollout['fail_closed']} | "
        f"SceneDirector={presentation_rollout['scene_director_enabled']} | "
        f"CinematicCaptions={presentation_rollout['cinematic_captions_enabled']} | "
        f"ChannelExcellence={excellence_rollout['enabled']} | "
        f"FinalQualityGate={excellence_rollout['final_quality_gate']} | "
        f"NarrativeEditor={str(os.getenv('ENABLE_NARRATIVE_EDITOR') or 'true').lower() not in {'0','false','no','off','nao','não'}}"
    )
    queues = [Queue(name, connection=conn) for name in listen]
    worker = Worker(queues, connection=conn)
    worker.work()
