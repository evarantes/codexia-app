from __future__ import annotations

import datetime
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app.models import CommunityComment, Settings
from app.services.youtube_service import YouTubeService


def _now_utc() -> datetime.datetime:
    return datetime.datetime.utcnow()


def _safe_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _build_reply_text(template: str, author: Optional[str]) -> str:
    name = (author or "").strip()
    if not name:
        name = "amigo"

    text = (template or "").strip()
    if not text:
        text = "Obrigado por assistir e comentar! Se ainda não é inscrito, se inscreva no canal e ative o sininho."

    out = text.replace("{author}", name)
    out = " ".join(out.split()).strip()
    if len(out) > 900:
        out = out[:900].rstrip()
    return out


def _get_settings(db: Session) -> Settings:
    settings = db.query(Settings).order_by(Settings.id.desc()).first()
    if not settings:
        settings = Settings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def auto_thank_comments(
    db: Session,
    *,
    backfill: bool = False,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    settings = _get_settings(db)

    enabled = bool(getattr(settings, "youtube_auto_thanks_enabled", False))
    if not enabled and not backfill:
        return {"enabled": False, "replied": 0, "skipped": 0, "errors": 0}

    yt = YouTubeService()
    if not yt.service:
        return {"enabled": enabled, "replied": 0, "skipped": 0, "errors": 1, "error": yt.auth_error or "YouTube não conectado"}

    template = (getattr(settings, "youtube_auto_thanks_template", None) or "").strip()
    max_per_run = _safe_int(getattr(settings, "youtube_auto_thanks_max_per_run", None), 15)
    cooldown_hours = _safe_int(getattr(settings, "youtube_auto_thanks_cooldown_hours", None), 72)

    if limit is None:
        limit = max_per_run
    limit = max(1, min(200, int(limit)))

    cooldown = datetime.timedelta(hours=max(0, min(24 * 30, cooldown_hours)))

    q = db.query(CommunityComment).filter(CommunityComment.youtube_parent_id == None)  # noqa: E711
    if backfill:
        q = q.filter(CommunityComment.status != "replied")
    else:
        q = q.filter(CommunityComment.status == "new")

    items = q.order_by(CommunityComment.published_at.asc().nullslast(), CommunityComment.created_at.asc()).limit(limit * 5).all()

    replied = 0
    skipped = 0
    errors = 0
    seen_authors: Dict[str, datetime.datetime] = {}

    def _author_last_reply(author: str) -> Optional[datetime.datetime]:
        if not author:
            return None
        if author in seen_authors:
            return seen_authors[author]
        last = (
            db.query(CommunityComment)
            .filter(CommunityComment.author == author)
            .filter(CommunityComment.reply_sent_at != None)  # noqa: E711
            .order_by(CommunityComment.reply_sent_at.desc())
            .first()
        )
        dt = getattr(last, "reply_sent_at", None) if last else None
        if dt:
            seen_authors[author] = dt
        return dt

    for item in items:
        if replied >= limit:
            break
        try:
            cid = (getattr(item, "youtube_comment_id", None) or "").strip()
            if not cid:
                skipped += 1
                continue

            if getattr(item, "reply_sent_at", None) is not None or str(getattr(item, "status", "")).lower() == "replied":
                skipped += 1
                continue

            author = (getattr(item, "author", None) or "").strip()
            last_rep = _author_last_reply(author) if author else None
            if last_rep and cooldown.total_seconds() > 0:
                if (_now_utc() - last_rep) < cooldown:
                    skipped += 1
                    continue

            reply_text = _build_reply_text(template, author)
            yt.reply_to_comment(cid, reply_text)

            item.status = "replied"
            item.reply_text = reply_text
            item.reply_sent_at = _now_utc()
            replied += 1
            if author:
                seen_authors[author] = item.reply_sent_at
        except Exception:
            errors += 1
            continue

    if replied:
        db.commit()

    return {"enabled": enabled, "replied": replied, "skipped": skipped, "errors": errors}

