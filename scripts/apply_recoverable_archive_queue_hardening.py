from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app/routers/youtube.py"
INDEX = ROOT / "app/static/index.html"

MARKER_BACKEND = "CODEXIA_RECOVERABLE_ARCHIVE_QUEUE_V1"
MARKER_UI = "CODEXIA_RECOVERABLE_ARCHIVE_UI_V1"


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1)


def _insert_before_once(text: str, anchor: str, insertion: str, label: str) -> str:
    if insertion in text:
        return text
    count = text.count(anchor)
    if count != 1:
        raise PatchError(f"{label}: âncora esperada 1 vez, encontrada {count}")
    return text.replace(anchor, insertion + anchor, 1)


BACKEND_INSERT = r'''# CODEXIA_RECOVERABLE_ARCHIVE_QUEUE_V1
def _task_is_explicitly_discarded_snapshot(task: Dict[str, Any], result_obj: Optional[Dict[str, Any]] = None) -> bool:
    result = result_obj if isinstance(result_obj, dict) else (
        task.get("result") if isinstance(task.get("result"), dict) else {}
    )
    message = str(task.get("message") or "").strip().lower()
    return bool(result.get("discarded")) or "descartad" in message


@router.get("/tasks/recoverable")
def list_recoverable_story_video_tasks(limit: int = 30, _admin=Depends(get_current_admin_user)):
    """Lista trabalhos fora da fila que ainda podem reutilizar o mesmo task_id e seus ativos.

    Tarefas explicitamente descartadas não aparecem. Cancelamento/encerramento do
    servidor apenas arquiva a execução; não autoriza nova mídia paga.
    """
    db = SessionLocal()
    try:
        lim = max(1, min(100, int(limit or 30)))
        rows = (
            db.query(VideoTask)
            .filter(VideoTask.status.in_(["failed", "cancelled"]))
            .order_by(VideoTask.updated_at.desc(), VideoTask.created_at.desc())
            .limit(max(lim * 4, lim))
            .all()
        )
        items: List[Dict[str, Any]] = []
        for row in rows:
            result_obj = _video_task_result_obj(row) or {}
            if not _is_story_video_generation_task(result_obj):
                continue
            task_snapshot = {
                "task_id": str(row.id),
                "status": str(row.status or ""),
                "progress": int(row.progress or 0),
                "message": str(row.message or ""),
                "result": result_obj,
            }
            if _task_is_explicitly_discarded_snapshot(task_snapshot, result_obj):
                continue
            payload = _video_task_result_payload(result_obj)
            if not payload:
                continue

            diagnostic: Dict[str, Any] = {}
            try:
                from app.services.production_manifest_diagnostics import build_manifest_diagnostic
                diagnostic = build_manifest_diagnostic(str(row.id)) or {}
            except Exception as exc:
                diagnostic = {
                    "manifest_found": False,
                    "recommendation": f"Diagnóstico de ativos indisponível: {type(exc).__name__}",
                }

            images = diagnostic.get("images") if isinstance(diagnostic.get("images"), dict) else {}
            audio = diagnostic.get("audio") if isinstance(diagnostic.get("audio"), dict) else {}
            items.append({
                "task_id": str(row.id),
                "title": _video_task_title_from_row(row),
                "status": str(row.status or ""),
                "progress": int(row.progress or 0),
                "message": str(row.message or ""),
                "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
                "updated_at": row.updated_at.isoformat() if getattr(row, "updated_at", None) else None,
                "manifest_found": bool(diagnostic.get("manifest_found")),
                "script_preserved": bool(diagnostic.get("script_preserved")),
                "images_valid": int(images.get("valid") or 0),
                "images_expected": int(images.get("expected") or 0),
                "images_missing": int(images.get("missing") or 0),
                "audio_found": bool(audio.get("found")),
                "audio_reusable": bool(audio.get("reusable")),
                "video_preserved": bool(diagnostic.get("video_preserved")),
                "checkpoint": str(diagnostic.get("max_recoverable_checkpoint") or "starting"),
                "planned_action": str(diagnostic.get("planned_action") or "blocked"),
                "requires_explicit_paid_confirmation": bool(diagnostic.get("requires_explicit_paid_confirmation")),
                "automatic_paid_recovery_allowed": False,
                "recommendation": str(diagnostic.get("recommendation") or ""),
                "can_retry": True,
                "recoverable": True,
            })
            if len(items) >= lim:
                break
        return {"count": len(items), "items": items}
    finally:
        db.close()


'''

DISCARD_OLD = r'''    status = str((task.get("status") or "")).strip().lower()
    if status == "cancelled":
        return {
            "message": "Tarefa já estava descartada.",
            "task_id": task_id,
            "status": "cancelled",
            "discarded": True,
        }
    if status != "failed":
        raise HTTPException(
            status_code=409,
            detail="Somente tarefas com falha podem ser descartadas. Use Cancelar para tarefas em andamento.",
        )

    discarded = request_cancel_task(task_id, message="Tarefa falhada descartada pelo usuário.")'''

DISCARD_NEW = r'''    status = str((task.get("status") or "")).strip().lower()
    result_obj = task.get("result") if isinstance(task.get("result"), dict) else {}
    if status == "cancelled" and _task_is_explicitly_discarded_snapshot(task, result_obj):
        return {
            "message": "Tarefa já estava descartada.",
            "task_id": task_id,
            "status": "cancelled",
            "discarded": True,
        }
    if status not in {"failed", "cancelled"}:
        raise HTTPException(
            status_code=409,
            detail="Somente tarefas falhadas ou arquivadas podem ser descartadas. Use Cancelar para tarefas em andamento.",
        )

    merge_task_result(task_id, {
        "discarded": True,
        "discarded_task_id": str(task_id),
        "discarded_at": datetime.utcnow().isoformat(),
    })
    discarded = request_cancel_task(task_id, message="Tarefa descartada definitivamente da recuperação pelo usuário.")'''

CANCEL_ALL_OLD = r'''        rows = db.query(VideoTask).filter(VideoTask.status.in_(["pending", "processing", "failed"])).all()
        for r in rows:
            status = str(r.status or "").strip().lower()
            result_obj = _video_task_result_obj(r)
            if status in {"pending", "processing"} or (
                status == "failed" and _is_story_video_generation_task(result_obj)
            ):
                task_ids_to_cancel.append(str(r.id))'''

CANCEL_ALL_NEW = r'''        # Encerrar o servidor não equivale a descartar histórico recuperável.
        # Apenas execuções ativas saem da fila; falhas antigas permanecem visíveis
        # em /tasks/recoverable para eventual retomada da MESMA tarefa.
        rows = db.query(VideoTask).filter(VideoTask.status.in_(["pending", "processing"])).all()
        for r in rows:
            task_ids_to_cancel.append(str(r.id))'''

CANCEL_MESSAGE_OLD = r'''        "message": "Produção encerrada, falhas antigas descartadas e séries ativas pausadas.",'''
CANCEL_MESSAGE_NEW = r'''        "message": "Execuções ativas encerradas e arquivadas; falhas antigas preservadas no histórico recuperável e séries ativas pausadas.",'''

RETRY_OLD = r'''        status = str((task.get("status") or "")).lower()
        progress = task.get("progress")'''
RETRY_NEW = r'''        status = str((task.get("status") or "")).lower()
        if status == "cancelled":
            archived_result = task.get("result") if isinstance(task.get("result"), dict) else {}
            if _task_is_explicitly_discarded_snapshot(task, archived_result):
                raise HTTPException(
                    status_code=409,
                    detail="Esta tarefa foi descartada da recuperação e não pode ser recolocada na fila.",
                )
        progress = task.get("progress")'''

UI_STATE_OLD = r'''                    ytQueueFactoryBusy: false,
                    ytQueueActionId: null,
                    ytStoryTaskPollFails: {},'''
UI_STATE_NEW = r'''                    ytQueueFactoryBusy: false,
                    ytQueueActionId: null,
                    // CODEXIA_RECOVERABLE_ARCHIVE_UI_V1
                    ytRecoverableTasksLoading: false,
                    ytRecoverableTasksLastLoadAt: 0,
                    ytRecoverableTasks: [],
                    ytRecoverableActionId: null,
                    ytStoryTaskPollFails: {},'''

UI_FETCH_TAIL_OLD = r'''                    } finally {
                        this.ytActiveVideoTasksLoading = false;
                    }
                },
                openStoryTaskFromQueue(item) {'''
UI_FETCH_TAIL_NEW = r'''                    } finally {
                        this.ytActiveVideoTasksLoading = false;
                        try { await this.fetchRecoverableVideoTasks({ silent: true }); } catch (e) {}
                    }
                },
                async fetchRecoverableVideoTasks({ silent = false } = {}) {
                    const now = Date.now();
                    const last = Number(this.ytRecoverableTasksLastLoadAt || 0);
                    if (silent && (now - last) < 30000) return;
                    this.ytRecoverableTasksLastLoadAt = now;
                    if (!silent) this.ytRecoverableTasksLoading = true;
                    try {
                        const res = await this.authFetch(`/youtube/tasks/recoverable?limit=40&_=${Date.now()}`);
                        const data = await res.json().catch(() => ({}));
                        if (!res.ok) throw new Error(data.detail || data.message || 'Falha ao carregar trabalhos recuperáveis.');
                        this.ytRecoverableTasks = Array.isArray(data.items) ? data.items : [];
                    } catch (e) {
                        if (!silent) alert('Erro ao carregar trabalhos recuperáveis: ' + (e.message || e));
                    } finally {
                        if (!silent) this.ytRecoverableTasksLoading = false;
                    }
                },
                async openRecoverableTask(item) {
                    const taskId = String((item && item.task_id) || '').trim();
                    if (!taskId) return;
                    this.ytStoryTaskId = taskId;
                    try { localStorage.setItem('ytStoryTaskId', taskId); } catch (e) {}
                    await this.pollStoryTask(taskId);
                    await this.diagnoseStoryTask();
                },
                async restoreRecoverableTask(item) {
                    const taskId = String((item && item.task_id) || '').trim();
                    if (!taskId || this.ytRecoverableActionId) return;
                    const title = String((item && item.title) || 'esta produção');
                    if (!confirm(`Recolocar "${title}" na fila usando a MESMA tarefa e os ativos preservados?\n\nMídia paga não será regenerada automaticamente; qualquer custo faltante continuará exigindo confirmação explícita.`)) return;
                    this.ytRecoverableActionId = taskId;
                    try {
                        const res = await this.authFetch(`/youtube/task/${encodeURIComponent(taskId)}/retry`, { method: 'POST' });
                        const data = await res.json().catch(() => ({}));
                        if (!res.ok) throw new Error(data.detail || data.message || 'Falha ao recolocar a tarefa na fila.');
                        this.ytStoryTaskId = taskId;
                        try { localStorage.setItem('ytStoryTaskId', taskId); } catch (e) {}
                        await this.fetchRecoverableVideoTasks({ silent: false });
                        await this.fetchActiveVideoTasks({ silent: false, autoOpen: false });
                        this.pollStoryTask(taskId);
                        alert('Tarefa recolocada na fila com reaproveitamento dos ativos preservados.');
                    } catch (e) {
                        alert('Erro ao recuperar tarefa: ' + (e.message || e));
                    } finally {
                        this.ytRecoverableActionId = null;
                    }
                },
                async discardRecoverableTask(item) {
                    const taskId = String((item && item.task_id) || '').trim();
                    if (!taskId || this.ytRecoverableActionId) return;
                    const title = String((item && item.title) || 'esta produção');
                    if (!confirm(`Retirar "${title}" do histórico recuperável?\n\nEla continuará no histórico técnico/auditoria, mas deixará de poder ser recolocada pela interface.`)) return;
                    this.ytRecoverableActionId = taskId;
                    try {
                        const res = await this.authFetch(`/youtube/task/${encodeURIComponent(taskId)}/discard`, { method: 'POST' });
                        const data = await res.json().catch(() => ({}));
                        if (!res.ok) throw new Error(data.detail || data.message || 'Falha ao descartar recuperação.');
                        await this.fetchRecoverableVideoTasks({ silent: false });
                    } catch (e) {
                        alert('Erro ao descartar recuperação: ' + (e.message || e));
                    } finally {
                        this.ytRecoverableActionId = null;
                    }
                },
                openStoryTaskFromQueue(item) {'''

UI_CARD_ANCHOR = r'''                            <div class="flex-1">
                                <label class="block font-bold mb-2">Shorts (História/Devocional)</label>'''
UI_CARD_INSERT = r'''                            <div class="mt-4 mb-4 bg-amber-50 border border-amber-200 rounded p-4">
                                <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-3 mb-3">
                                    <div>
                                        <div class="font-bold text-amber-950"><i class="fas fa-box-archive mr-2"></i>Trabalhos armazenados / recuperáveis</div>
                                        <div class="text-xs text-amber-800 mt-1">Produções fora da fila que ainda mantêm o mesmo task_id. Recolocar tenta reutilizar roteiro, áudio e imagens antes de qualquer nova geração.</div>
                                    </div>
                                    <button @click="fetchRecoverableVideoTasks({ silent: false })" :disabled="ytRecoverableTasksLoading" class="px-3 py-2 rounded border border-amber-300 bg-white hover:bg-amber-100 disabled:opacity-50 text-sm whitespace-nowrap">
                                        <i v-if="ytRecoverableTasksLoading" class="fas fa-spinner fa-spin mr-1"></i>
                                        <span>{{ ytRecoverableTasksLoading ? 'Atualizando...' : 'Atualizar histórico' }}</span>
                                    </button>
                                </div>
                                <div v-if="ytRecoverableTasksLoading && !ytRecoverableTasks.length" class="text-sm text-amber-800">Carregando trabalhos preservados...</div>
                                <div v-else-if="!ytRecoverableTasks.length" class="text-sm text-gray-600 italic">Nenhum trabalho recuperável armazenado no momento.</div>
                                <div v-else class="space-y-3">
                                    <div v-for="item in ytRecoverableTasks" :key="'recoverable-'+item.task_id" class="bg-white border border-amber-200 rounded p-3">
                                        <div class="flex flex-col lg:flex-row lg:items-start lg:justify-between gap-3">
                                            <div class="min-w-0 flex-1">
                                                <div class="font-semibold text-gray-900 truncate">{{ item.title || 'Produção sem título' }}</div>
                                                <div class="text-xs text-gray-600 mt-1">{{ String(item.status || '').toUpperCase() }} • {{ Number(item.progress || 0) }}% • {{ item.checkpoint || 'starting' }}</div>
                                                <div class="text-xs text-gray-500 mt-1 line-clamp-2">{{ item.message || item.recommendation || 'Sem detalhe registrado.' }}</div>
                                                <div class="flex flex-wrap gap-2 mt-2 text-xs">
                                                    <span class="px-2 py-1 rounded" :class="item.script_preserved ? 'bg-green-50 text-green-800' : 'bg-gray-100 text-gray-600'">Roteiro {{ item.script_preserved ? '✓' : '—' }}</span>
                                                    <span class="px-2 py-1 rounded" :class="Number(item.images_valid || 0) > 0 ? 'bg-green-50 text-green-800' : 'bg-gray-100 text-gray-600'">Imagens {{ Number(item.images_valid || 0) }}/{{ Number(item.images_expected || 0) || '?' }}</span>
                                                    <span class="px-2 py-1 rounded" :class="item.audio_reusable ? 'bg-green-50 text-green-800' : 'bg-gray-100 text-gray-600'">Áudio {{ item.audio_reusable ? '✓' : (item.audio_found ? 'revisar' : '—') }}</span>
                                                    <span class="px-2 py-1 rounded bg-blue-50 text-blue-800">Pago automático: BLOQUEADO</span>
                                                </div>
                                            </div>
                                            <div class="flex flex-wrap gap-2 lg:justify-end">
                                                <button @click="openRecoverableTask(item)" class="px-3 py-2 rounded border bg-white hover:bg-gray-100 text-sm"><i class="fas fa-stethoscope mr-1"></i>Diagnosticar</button>
                                                <button @click="restoreRecoverableTask(item)" :disabled="Boolean(ytRecoverableActionId)" class="px-3 py-2 rounded bg-green-600 hover:bg-green-700 text-white text-sm disabled:opacity-50"><i class="fas fa-rotate mr-1"></i>Recolocar na fila</button>
                                                <button @click="discardRecoverableTask(item)" :disabled="Boolean(ytRecoverableActionId)" class="px-3 py-2 rounded border border-red-200 bg-red-50 hover:bg-red-100 text-red-700 text-sm disabled:opacity-50"><i class="fas fa-box-archive mr-1"></i>Descartar recuperação</button>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </div>

'''

CANCEL_CONFIRM_OLD = r'''                    if (!confirm('Encerrar todas as produções do servidor, descartar falhas antigas e pausar séries ativas?')) return;'''
CANCEL_CONFIRM_NEW = r'''                    if (!confirm('Encerrar as execuções ativas do servidor e pausar séries?\n\nFalhas antigas NÃO serão descartadas. Tarefas encerradas continuarão no histórico recuperável para possível retomada.')) return;'''

CANCEL_ALERT_OLD = r'''                        alert(`Produção encerrada. ${cancelledTasks} tarefa(s) removida(s) da fila e ${pausedSeries} série(s) pausada(s).`);'''
CANCEL_ALERT_NEW = r'''                        await this.fetchRecoverableVideoTasks({ silent: false });
                        alert(`Produção encerrada. ${cancelledTasks} tarefa(s) ativa(s) foram arquivadas para recuperação e ${pausedSeries} série(s) pausada(s).`);'''


def apply() -> None:
    youtube = YOUTUBE.read_text(encoding="utf-8")
    youtube = _insert_before_once(
        youtube,
        '@router.post("/task/{task_id}/discard")\n',
        BACKEND_INSERT,
        "recoverable backend endpoint",
    )
    youtube = _replace_once(youtube, DISCARD_OLD, DISCARD_NEW, "discard archived task")
    youtube = _replace_once(youtube, CANCEL_ALL_OLD, CANCEL_ALL_NEW, "cancel_all preserves failures")
    youtube = _replace_once(youtube, CANCEL_MESSAGE_OLD, CANCEL_MESSAGE_NEW, "cancel_all message")
    youtube = _replace_once(youtube, RETRY_OLD, RETRY_NEW, "retry cancelled archive guard")
    YOUTUBE.write_text(youtube, encoding="utf-8")

    index = INDEX.read_text(encoding="utf-8")
    index = _replace_once(index, UI_STATE_OLD, UI_STATE_NEW, "recoverable UI state")
    index = _replace_once(index, UI_FETCH_TAIL_OLD, UI_FETCH_TAIL_NEW, "recoverable UI actions")
    index = _insert_before_once(index, UI_CARD_ANCHOR, UI_CARD_INSERT, "recoverable UI card")
    index = _replace_once(index, CANCEL_CONFIRM_OLD, CANCEL_CONFIRM_NEW, "safe cancel confirmation")
    index = _replace_once(index, CANCEL_ALERT_OLD, CANCEL_ALERT_NEW, "safe cancel result")
    INDEX.write_text(index, encoding="utf-8")


def check() -> None:
    youtube = YOUTUBE.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    backend_checks = (
        MARKER_BACKEND in youtube,
        '@router.get("/tasks/recoverable")' in youtube,
        'VideoTask.status.in_(["pending", "processing"])' in youtube,
        'falhas antigas preservadas no histórico recuperável' in youtube,
        'status not in {"failed", "cancelled"}' in youtube,
        'Esta tarefa foi descartada da recuperação' in youtube,
    )
    if not all(backend_checks):
        raise PatchError("contrato backend do histórico recuperável não aplicado")
    if 'VideoTask.status.in_(["pending", "processing", "failed"])' in youtube:
        raise PatchError("cancel_all ainda descarta tarefas failed")

    ui_checks = (
        MARKER_UI in index,
        'Trabalhos armazenados / recuperáveis' in index,
        'fetchRecoverableVideoTasks' in index,
        'restoreRecoverableTask' in index,
        'Descartar recuperação' in index,
        'Falhas antigas NÃO serão descartadas' in index,
    )
    if not all(ui_checks):
        raise PatchError("contrato UI do histórico recuperável não aplicado")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.apply:
        apply()
    if args.check:
        check()
    if not args.apply and not args.check:
        parser.error("use --apply e/ou --check")


if __name__ == "__main__":
    main()
