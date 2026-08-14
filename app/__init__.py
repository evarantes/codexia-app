"""Inicialização transversal do Codexia.

Este módulo mantém a linha de auditoria ``unified_videos`` sincronizada com a
fonte operacional ``video_tasks``. A sincronização usa SQL na mesma transação
do VideoTask, portanto uma falha/interrupção detectada pelo monitor não deixa a
UI canônica presa em ``queued / 0%``.
"""

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
