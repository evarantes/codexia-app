"""Inicialização transversal do Codexia.

Este módulo mantém a linha de auditoria ``unified_videos`` sincronizada com a
fonte operacional ``video_tasks``. A sincronização usa SQL na mesma transação
do VideoTask, portanto uma falha/interrupção detectada pelo monitor não deixa a
UI canônica presa em ``queued / 0%``.
"""

import json

from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session


def _canonical_status_for_video_task(status: str):
    normalized = str(status or "").strip().lower()
    if normalized == "failed":
        return "failed"
    if normalized == "cancelled":
        return "cancelled"
    if normalized in {"pending", "queued", "paused", "pause_requested"}:
        return "queued"
    # Processing e completed precisam considerar o estado detalhado/review da
    # própria linha UnifiedVideo; são tratados por CASE no SQL abaixo.
    if normalized in {"processing", "completed", "ready", "rendered_upload_failed"}:
        return normalized
    return None


def _openai_credit_recovery_result(obj):
    """Detecta retry humano após falha OPENAI_NO_CREDIT.

    O retry seguro do YouTube Auto troca a tarefa de ``failed`` para
    ``processing`` e usa a mensagem ``Retomada preparada...``. Esse gesto do
    usuário passa a ser a confirmação de que o saldo foi recarregado. Não
    liberamos o bloqueio em nenhuma transição automática e não repetimos a
    chamada se a recarga ainda não estiver disponível.
    """
    try:
        if str(getattr(obj, "__tablename__", "") or "") != "video_tasks":
            return None
        state = inspect(obj)
        history = state.attrs.status.history
        previous = {str(value or "").strip().lower() for value in (history.deleted or [])}
        current = str(getattr(obj, "status", "") or "").strip().lower()
        message = str(getattr(obj, "message", "") or "").strip().lower()
        if "failed" not in previous or current not in {"processing", "pending"}:
            return None
        if "retomada preparada" not in message and "reaproveitamento" not in message:
            return None

        raw = str(getattr(obj, "result_json", "") or "").strip()
        if not raw:
            return None
        result = json.loads(raw)
        if not isinstance(result, dict):
            return None
        provider_error = result.get("provider_error")
        if not isinstance(provider_error, dict):
            return None
        provider = str(provider_error.get("provider") or "").strip().lower()
        code = str(provider_error.get("code") or "").strip().upper()
        if provider != "openai" or code != "OPENAI_NO_CREDIT":
            return None
        return result
    except Exception:
        return None


@event.listens_for(Session, "before_flush")
def _resume_after_openai_credit_recharge(session, _flush_context, _instances):
    """Libera ``openai_no_credit`` somente quando o usuário reinicia a mesma tarefa.

    A falha continua protegida por padrão. Depois de recarregar a OpenAI, o
    clique em ``Reiniciar tarefa/Reiniciar agora`` é a confirmação humana que
    autoriza uma nova tentativa, preservando payload, imagens, áudio e demais
    checkpoints já salvos pela recuperação canônica.
    """
    try:
        candidates = list(session.dirty)
    except Exception:
        candidates = []

    for obj in candidates:
        result = _openai_credit_recovery_result(obj)
        if not isinstance(result, dict):
            continue

        user_id = getattr(obj, "user_id", None)
        try:
            if user_id is None:
                session.execute(
                    text(
                        """
                        UPDATE settings
                        SET openai_no_credit = :disabled
                        WHERE id = (SELECT id FROM settings ORDER BY id DESC LIMIT 1)
                        """
                    ),
                    {"disabled": False},
                )
            else:
                session.execute(
                    text(
                        """
                        UPDATE settings
                        SET openai_no_credit = :disabled
                        WHERE id = (
                            SELECT id FROM settings
                            WHERE user_id = :user_id
                            ORDER BY id DESC
                            LIMIT 1
                        )
                        """
                    ),
                    {"disabled": False, "user_id": int(user_id)},
                )
        except Exception:
            # Não mascara o retry: se a configuração não puder ser liberada,
            # a próxima chamada continuará bloqueada e a tarefa falhará de
            # forma recuperável, sem entrar em repetição automática.
            continue

        # O alerta antigo não deve permanecer vermelho enquanto a mesma tarefa
        # já está sendo retomada. Mantemos uma trilha de auditoria no resultado.
        result.pop("provider_error", None)
        recovery = result.get("provider_credit_recovery")
        if not isinstance(recovery, dict):
            recovery = {}
        recovery.update(
            {
                "provider": "openai",
                "credit_recharge_confirmed": True,
                "confirmation_source": "explicit_same_task_retry",
                "same_task": True,
                "reuse_assets": True,
            }
        )
        result["provider_credit_recovery"] = recovery
        try:
            obj.result_json = json.dumps(result, ensure_ascii=False)
        except Exception:
            pass


@event.listens_for(Session, "after_flush")
def _sync_video_task_state_to_unified(session, _flush_context):
    touched = []
    try:
        candidates = list(session.new) + list(session.dirty)
    except Exception:
        candidates = []

    for obj in candidates:
        try:
            if str(getattr(obj, "__tablename__", "") or "") != "video_tasks":
                continue
            task_id = str(getattr(obj, "id", "") or "").strip()
            status = str(getattr(obj, "status", "") or "").strip().lower()
            canonical = _canonical_status_for_video_task(status)
            if not task_id or canonical is None:
                continue
            touched.append(
                {
                    "task_id": task_id,
                    "task_status": status,
                    "canonical": canonical,
                    "progress": max(0, min(100, int(getattr(obj, "progress", 0) or 0))),
                    "message": str(getattr(obj, "message", "") or "")[:1000] or None,
                }
            )
        except Exception:
            continue

    if not touched:
        return

    try:
        bind = session.get_bind()
        if bind is None or not inspect(bind).has_table("unified_videos"):
            return
    except Exception:
        return

    for item in touched:
        status = item["task_status"]
        # Não reduz o estado detalhado de uma execução viva: se o pipeline já
        # está em processing_images/processing_audio/rendering, preserva-o.
        if status == "processing":
            status_expr = "CASE WHEN status IN ('queued','failed') THEN 'processing_script' ELSE status END"
        elif status in {"completed", "ready"}:
            status_expr = "CASE WHEN review_required THEN 'awaiting_review' ELSE 'approved' END"
        elif status == "rendered_upload_failed":
            status_expr = "CASE WHEN review_required THEN 'awaiting_review' ELSE 'failed' END"
        else:
            status_expr = ":canonical_status"

        params = {
            "task_id": item["task_id"],
            "canonical_status": item["canonical"],
            "progress": item["progress"],
            "message": item["message"],
            "last_error": item["message"] if status == "failed" else None,
        }
        try:
            session.execute(
                text(
                    f"""
                    UPDATE unified_videos
                    SET status = {status_expr},
                        progress = :progress,
                        last_message = COALESCE(:message, last_message),
                        last_error = CASE
                            WHEN :canonical_status = 'failed' THEN COALESCE(:last_error, last_error)
                            WHEN :canonical_status IN ('queued','cancelled') THEN NULL
                            ELSE last_error
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE task_id = :task_id
                    """
                ),
                params,
            )
        except Exception:
            # Não mascara o fluxo principal se a linha de auditoria ainda não
            # existir (ex.: bootstrap/migration). A próxima transição canônica
            # fará a reconciliação normal.
            continue
