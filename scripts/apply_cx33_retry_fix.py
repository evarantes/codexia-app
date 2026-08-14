from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app" / "routers" / "youtube.py"
INDEX = ROOT / "app" / "static" / "index.html"
TEST = ROOT / "tests" / "test_cx33_retry_dispatch_regression.py"


def replace_regex(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{label}: esperado 1 match, encontrado {count}")
    return updated


def replace_exact(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: esperado 1 match, encontrado {count}")
    return text.replace(old, new, 1)


youtube = YOUTUBE.read_text(encoding="utf-8")

rq_workers = '''def _rq_workers_online() -> bool:
    """Retorna True somente para worker RQ com heartbeat recente.

    RQ devolve ``last_heartbeat`` timezone-aware em versões atuais. O app usava
    ``datetime.utcnow()`` (naive), o que fazia a subtração falhar silenciosamente
    e classificava o CX33 vivo como offline.
    """
    if not conn or not RQ_AVAILABLE or Worker is None:
        return False
    try:
        try:
            workers = list(Worker.all(connection=conn))
        except TypeError:
            workers = list(Worker.all(conn))
        if not workers:
            return False

        now = datetime.now(timezone.utc)
        for worker in workers:
            try:
                heartbeat = getattr(worker, "last_heartbeat", None)
                if not heartbeat:
                    continue
                if isinstance(heartbeat, str):
                    heartbeat = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
                if getattr(heartbeat, "tzinfo", None) is None:
                    heartbeat = heartbeat.replace(tzinfo=timezone.utc)
                else:
                    heartbeat = heartbeat.astimezone(timezone.utc)
                age_seconds = (now - heartbeat).total_seconds()
                if -5 <= age_seconds <= 120:
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False

'''

youtube = replace_regex(
    youtube,
    r"def _rq_workers_online\(\) -> bool:\n.*?(?=def _is_video_factory_busy\(\) -> bool:)",
    rq_workers,
    "rq worker heartbeat",
)

dispatch = '''def _dispatch_video_generation_task(payload: Dict[str, Any], task_id: str):
    """Enfileira vídeo pesado no RQ e nunca cai para execução local em produção.

    O servidor principal (CPX22) pode coordenar/monitorar a fila, mas a geração
    pesada pertence ao worker dedicado (CX33). Se Redis/RQ/worker estiver
    indisponível, a tarefa é preservada como pendente em vez de iniciar thread ou
    processo local.
    """
    payload = _maybe_enable_render_only_flags(payload, task_id)
    requires_isolation = _requires_isolated_video_process(payload, task_id)
    resource_report = _series_resource_preflight(payload, task_id)
    if resource_report is not None and not bool(resource_report.get("allowed")):
        return

    app_env = str(os.getenv("APP_ENV") or "").strip().lower()
    production = app_env in {"production", "prod"}
    use_rq_raw = str(os.getenv("USE_RQ_FOR_VIDEO_GENERATION") or "").strip()
    if use_rq_raw:
        use_rq = use_rq_raw.lower() in {"1", "true", "yes", "on"}
    else:
        use_rq = conn is not None and _rq_workers_online()

    current = get_task(task_id) or {}
    try:
        current_progress = max(0, min(100, int(current.get("progress") or 0)))
    except Exception:
        current_progress = 0
    preserved_progress = max(1, current_progress)

    worker_online = bool(conn is not None and _rq_workers_online())
    if use_rq and worker_online:
        try:
            rq_queue.enqueue(
                process_video_generation_payload,
                payload,
                task_id,
                job_timeout=_rq_video_timeout_seconds(),
            )
            update_task(
                task_id,
                status="processing",
                progress=preserved_progress,
                message="Enfileirado no worker de vídeo CX33; aguardando/confirmando execução...",
                result=_dispatch_task_result(task_id, payload, "rq"),
            )
            return
        except Exception as exc:
            if production:
                update_task(
                    task_id,
                    status="pending",
                    progress=preserved_progress,
                    message=(
                        "Falha ao enfileirar no worker CX33/RQ. A tarefa foi preservada e NÃO será "
                        f"executada no servidor principal. Detalhe: {str(exc)[:180]}"
                    ),
                    result=_dispatch_task_result(
                        task_id,
                        payload,
                        "rq_enqueue_failed",
                        retryable=True,
                    ),
                )
                return

    if production:
        update_task(
            task_id,
            status="pending",
            progress=preserved_progress,
            message=(
                "Worker de vídeo CX33/RQ indisponível. A produção foi preservada e NÃO será "
                "executada no servidor principal. Restabeleça o worker e reinicie/retome a tarefa."
            ),
            result=_dispatch_task_result(
                task_id,
                payload,
                "rq_worker_unavailable",
                retryable=True,
            ),
        )
        return

    # Fallback local mantido apenas para desenvolvimento/homologação explícita.
    allow_inline_raw = os.getenv("ALLOW_INLINE_VIDEO_GENERATION")
    if allow_inline_raw is None or not str(allow_inline_raw).strip():
        allow_inline = True
    else:
        allow_inline = str(allow_inline_raw).strip().lower() in {"1", "true", "yes", "on"}
    if not allow_inline:
        update_task(
            task_id,
            status="pending",
            progress=preserved_progress,
            message="Aguardando worker RQ; execução local desativada.",
        )
        return

    executor = (os.getenv("VIDEO_GENERATION_EXECUTOR") or "thread").strip().lower()
    if executor not in {"auto", "thread", "process"}:
        executor = "thread"
    if requires_isolation:
        executor = "process"

    use_process = executor == "process" and (conn is not None or requires_isolation)
    if use_process:
        if _start_isolated_video_generation(payload, task_id):
            return
        if requires_isolation:
            return

    update_task(
        task_id,
        status="processing",
        progress=preserved_progress,
        message="Iniciando geração local de desenvolvimento...",
        result=_dispatch_task_result(task_id, payload, "thread"),
    )
    thread = threading.Thread(target=process_video_generation_payload, args=(payload, task_id), daemon=True)
    thread.start()

'''

youtube = replace_regex(
    youtube,
    r"def _dispatch_video_generation_task\(payload: Dict\[str, Any\], task_id: str\):\n.*?(?=def _kick_story_video_task_queue\(\) -> Optional\[str\]:)",
    dispatch,
    "fail-closed dispatcher",
)

old_reset = '''        reset = reset_task_for_retry(
            task_id,
            progress=1,
            message="Recuperando a mesma tarefa pelo pipeline unificado...",
        )'''
new_reset = '''        resume_progress = max(1, progress_n)
        reset = reset_task_for_retry(
            task_id,
            progress=resume_progress,
            message="Retomada preparada com reaproveitamento dos ativos; aguardando worker CX33...",
        )'''
youtube = replace_exact(youtube, old_reset, new_reset, "retry preserve progress")

old_transition = '''                status="processing",
                step="recovery",
                progress=1,
                message="Recuperando a mesma tarefa e reutilizando os ativos disponíveis.",'''
new_transition = '''                status="pending",
                step="queued_recovery",
                progress=resume_progress,
                message="Retomada preparada, reutilizando os ativos disponíveis e aguardando worker CX33.",'''
youtube = replace_exact(youtube, old_transition, new_transition, "retry unified pending")

old_diag = '''            status = str((t.get("status") or "")).lower()
            msg = str((t.get("message") or ""))
            runtime_state = str(runtime.get("state") or "")'''
new_diag = '''            status = str((t.get("status") or "")).lower()
            msg = str((t.get("message") or ""))
            if status == "failed":
                report["recommendations"].append(
                    f"Tarefa falhou em {int(t.get('progress') or 0)}%: {msg or 'sem mensagem técnica registrada.'}"
                )
            elif status == "pending" and msg:
                report["recommendations"].append(f"Tarefa aguardando execução: {msg}")
            runtime_state = str(runtime.get("state") or "")'''
youtube = replace_exact(youtube, old_diag, new_diag, "diagnostic failed summary")

youtube = youtube.replace(
    'report["recommendations"].append("USE_RQ_FOR_VIDEO_GENERATION está ativo, mas não há workers RQ. Desative USE_RQ_FOR_VIDEO_GENERATION ou suba um worker.")',
    'report["recommendations"].append("O worker RQ/CX33 não está disponível. A produção pesada permanecerá preservada e não será executada no servidor principal; restabeleça o worker CX33/Redis e reinicie a tarefa.")',
    1,
)

YOUTUBE.write_text(youtube, encoding="utf-8")

index = INDEX.read_text(encoding="utf-8")
old_panel = '''                        <div v-if="ytStoryAssistReport" class="mt-3 text-xs bg-white border rounded p-3">
                            <div class="font-bold text-gray-800 mb-1">Diagnóstico</div>
                            <div v-if="ytStoryAssistReport.recommendations && ytStoryAssistReport.recommendations.length" class="text-gray-700">
                                <div v-for="(r, idx) in ytStoryAssistReport.recommendations" :key="'rec-'+idx">- {{ r }}</div>
                            </div>
                            <div v-if="ytStoryAssistReport.ai && ytStoryAssistReport.ai.acoes_recomendadas && ytStoryAssistReport.ai.acoes_recomendadas.length" class="mt-2 text-gray-700">
                                <div class="font-semibold">IA</div>
                                <div v-for="(r, idx) in ytStoryAssistReport.ai.acoes_recomendadas" :key="'airec-'+idx">- {{ r }}</div>
                            </div>
                        </div>'''
new_panel = '''                        <div v-if="ytStoryAssistReport" class="mt-3 text-xs bg-white border rounded p-3">
                            <div class="font-bold text-gray-800 mb-2">Diagnóstico</div>
                            <div v-if="ytStoryAssistReport.task" class="mb-2 p-2 rounded bg-gray-50 border text-gray-700">
                                <div><strong>Status real:</strong> {{ ytStoryAssistReport.task.status || 'desconhecido' }} <span v-if="ytStoryAssistReport.task.progress !== undefined">• {{ Number(ytStoryAssistReport.task.progress || 0) }}%</span></div>
                                <div v-if="ytStoryAssistReport.task.message" class="mt-1"><strong>Mensagem:</strong> {{ ytStoryAssistReport.task.message }}</div>
                                <div v-if="ytStoryAssistReport.task.runtime && ytStoryAssistReport.task.runtime.label" class="mt-1"><strong>Executor:</strong> {{ ytStoryAssistReport.task.runtime.label }}</div>
                            </div>
                            <div v-if="ytStoryAssistReport.checks && ytStoryAssistReport.checks.length" class="mb-2 text-gray-700">
                                <div class="font-semibold mb-1">Verificações</div>
                                <div v-for="(check, idx) in ytStoryAssistReport.checks" :key="'check-'+idx" class="flex gap-1 items-start">
                                    <span :class="check.ok ? 'text-green-700' : 'text-red-700'">{{ check.ok ? '✓' : '✗' }}</span>
                                    <span><strong>{{ check.name }}:</strong> {{ check.value !== undefined && check.value !== null ? check.value : (check.ok ? 'OK' : 'Falha') }}</span>
                                </div>
                            </div>
                            <div v-if="ytStoryAssistReport.recommendations && ytStoryAssistReport.recommendations.length" class="text-gray-700">
                                <div class="font-semibold mb-1">Conclusão / ação recomendada</div>
                                <div v-for="(r, idx) in ytStoryAssistReport.recommendations" :key="'rec-'+idx">- {{ r }}</div>
                            </div>
                            <div v-if="ytStoryAssistReport.ai && ytStoryAssistReport.ai.acoes_recomendadas && ytStoryAssistReport.ai.acoes_recomendadas.length" class="mt-2 text-gray-700">
                                <div class="font-semibold">IA</div>
                                <div v-for="(r, idx) in ytStoryAssistReport.ai.acoes_recomendadas" :key="'airec-'+idx">- {{ r }}</div>
                            </div>
                            <div v-if="(!ytStoryAssistReport.checks || !ytStoryAssistReport.checks.length) && (!ytStoryAssistReport.recommendations || !ytStoryAssistReport.recommendations.length) && !ytStoryAssistReport.task" class="text-gray-500 italic">
                                Nenhuma informação diagnóstica foi retornada. Atualize a página e tente novamente.
                            </div>
                        </div>'''
index = replace_exact(index, old_panel, new_panel, "diagnostic panel")
INDEX.write_text(index, encoding="utf-8")

TEST.write_text(r'''from datetime import datetime, timezone

import pytest

from app.routers import youtube


class _WorkerRow:
    def __init__(self, heartbeat):
        self.last_heartbeat = heartbeat


def test_rq_worker_online_accepts_timezone_aware_heartbeat(monkeypatch):
    class FakeWorker:
        @classmethod
        def all(cls, *args, **kwargs):
            return [_WorkerRow(datetime.now(timezone.utc))]

    monkeypatch.setattr(youtube, "conn", object())
    monkeypatch.setattr(youtube, "RQ_AVAILABLE", True)
    monkeypatch.setattr(youtube, "Worker", FakeWorker)

    assert youtube._rq_workers_online() is True


def test_production_never_falls_back_to_local_when_worker_offline(monkeypatch):
    updates = []
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(youtube, "conn", object())
    monkeypatch.setattr(youtube, "_rq_workers_online", lambda: False)
    monkeypatch.setattr(youtube, "_series_resource_preflight", lambda payload, task_id: {"allowed": True})
    monkeypatch.setattr(youtube, "_requires_isolated_video_process", lambda payload, task_id: False)
    monkeypatch.setattr(youtube, "_maybe_enable_render_only_flags", lambda payload, task_id: payload)
    monkeypatch.setattr(youtube, "get_task", lambda task_id: {"progress": 57})
    monkeypatch.setattr(youtube, "update_task", lambda task_id, **kwargs: updates.append(kwargs))

    class ForbiddenThread:
        def __init__(self, *args, **kwargs):
            raise AssertionError("produção local não pode iniciar no app principal")

    monkeypatch.setattr(youtube.threading, "Thread", ForbiddenThread)
    youtube._dispatch_video_generation_task({"duration": 3}, "task-1")

    assert updates
    assert updates[-1]["status"] == "pending"
    assert updates[-1]["progress"] == 57
    assert "NÃO será" in updates[-1]["message"]


def test_production_enqueues_when_cx33_worker_is_online(monkeypatch):
    enqueued = []
    updates = []

    class Queue:
        def enqueue(self, *args, **kwargs):
            enqueued.append((args, kwargs))

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(youtube, "conn", object())
    monkeypatch.setattr(youtube, "rq_queue", Queue())
    monkeypatch.setattr(youtube, "_rq_workers_online", lambda: True)
    monkeypatch.setattr(youtube, "_series_resource_preflight", lambda payload, task_id: {"allowed": True})
    monkeypatch.setattr(youtube, "_requires_isolated_video_process", lambda payload, task_id: False)
    monkeypatch.setattr(youtube, "_maybe_enable_render_only_flags", lambda payload, task_id: payload)
    monkeypatch.setattr(youtube, "get_task", lambda task_id: {"progress": 20})
    monkeypatch.setattr(youtube, "update_task", lambda task_id, **kwargs: updates.append(kwargs))

    youtube._dispatch_video_generation_task({"duration": 3}, "task-2")

    assert len(enqueued) == 1
    assert updates[-1]["status"] == "processing"
    assert updates[-1]["progress"] == 20
    assert "CX33" in updates[-1]["message"]


def test_diagnostic_panel_renders_checks_and_task_message():
    html = (youtube.Path("app/static/index.html")).read_text(encoding="utf-8")
    assert "ytStoryAssistReport.checks" in html
    assert "ytStoryAssistReport.task.message" in html
    assert "Status real:" in html
''', encoding="utf-8")

print("CX33 retry/diagnostics patch aplicado com sucesso")
