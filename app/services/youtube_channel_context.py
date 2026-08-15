"""Contexto persistente do canal YouTube para produção desacoplada da publicação.

O canal pode ficar temporariamente desconectado sem impedir roteiro, imagens, áudio
ou renderização. Quando a API está disponível, salvamos um snapshot seguro em
``channel_insights``; quando não está, a produção pode continuar usando o último
snapshot conhecido.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models import ChannelInsight

SNAPSHOT_KIND = "youtube_channel_snapshot"
_SENSITIVE_FRAGMENTS = ("secret", "token", "credential", "password", "client_id")


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key or "").strip().lower()
    return any(fragment in normalized for fragment in _SENSITIVE_FRAGMENTS)


def _safe_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _safe_value(item, depth + 1)
            for key, item in value.items()
            if not _is_sensitive_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth + 1) for item in value[:100]]
    return str(value)[:500]


def sanitize_channel_snapshot(stats: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Remove qualquer material sensível e normaliza o snapshot para JSON."""
    raw = stats if isinstance(stats, dict) else {}
    payload = {
        str(key): _safe_value(value)
        for key, value in raw.items()
        if not _is_sensitive_key(key)
    }
    payload["cached_at"] = datetime.utcnow().isoformat()
    return payload


def save_channel_snapshot(db: Session, *, user_id: Optional[int], stats: Dict[str, Any]) -> Dict[str, Any]:
    """Salva/atualiza o último snapshot conhecido do canal do usuário."""
    payload = sanitize_channel_snapshot(stats)
    query = db.query(ChannelInsight).filter(ChannelInsight.kind == SNAPSHOT_KIND)
    if user_id is None:
        query = query.filter(ChannelInsight.user_id.is_(None))
    else:
        query = query.filter(ChannelInsight.user_id == int(user_id))
    row = query.order_by(ChannelInsight.id.desc()).first()

    channel_title = str(
        payload.get("channel_title")
        or payload.get("title")
        or payload.get("channel_name")
        or "Canal YouTube"
    ).strip()[:255]
    message = (
        f"Snapshot do canal: {channel_title}. "
        f"Inscritos={payload.get('subscribers', payload.get('subscriber_count', 'n/d'))}; "
        f"visualizações={payload.get('views', payload.get('view_count', 'n/d'))}; "
        f"vídeos={payload.get('videos', payload.get('video_count', 'n/d'))}."
    )[:2000]

    if row is None:
        row = ChannelInsight(
            user_id=(int(user_id) if user_id is not None else None),
            kind=SNAPSHOT_KIND,
            title=channel_title,
            message=message,
            payload_json=json.dumps(payload, ensure_ascii=False),
            status="cached",
        )
        db.add(row)
    else:
        row.title = channel_title
        row.message = message
        row.payload_json = json.dumps(payload, ensure_ascii=False)
        row.status = "cached"
        row.created_at = datetime.utcnow()
    db.commit()
    return payload


def load_channel_snapshot(db: Session, *, user_id: Optional[int]) -> Optional[Dict[str, Any]]:
    query = db.query(ChannelInsight).filter(ChannelInsight.kind == SNAPSHOT_KIND)
    if user_id is None:
        query = query.filter(ChannelInsight.user_id.is_(None))
    else:
        query = query.filter(ChannelInsight.user_id == int(user_id))
    row = query.order_by(ChannelInsight.id.desc()).first()
    if not row or not row.payload_json:
        return None
    try:
        payload = json.loads(row.payload_json)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def channel_context_text_from_snapshot(snapshot: Optional[Dict[str, Any]]) -> str:
    """Gera contexto editorial curto e seguro para o pipeline de produção."""
    data = snapshot if isinstance(snapshot, dict) else {}
    if not data:
        return ""
    title = str(data.get("channel_title") or data.get("title") or data.get("channel_name") or "").strip()
    subscribers = data.get("subscribers", data.get("subscriber_count"))
    views = data.get("views", data.get("view_count"))
    videos = data.get("videos", data.get("video_count"))
    parts = []
    if title:
        parts.append(f"Canal de destino: {title}")
    if subscribers is not None:
        parts.append(f"inscritos conhecidos: {subscribers}")
    if views is not None:
        parts.append(f"visualizações conhecidas: {views}")
    if videos is not None:
        parts.append(f"vídeos conhecidos: {videos}")
    if not parts:
        return ""
    return "Contexto persistido do canal (pode estar desatualizado): " + "; ".join(parts) + "."


def load_channel_context_text(db: Session, *, user_id: Optional[int]) -> str:
    return channel_context_text_from_snapshot(load_channel_snapshot(db, user_id=user_id))
