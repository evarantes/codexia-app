from __future__ import annotations

import datetime
import hashlib
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


def _pick_verse(comment_text: str, seed: str) -> Tuple[str, str]:
    low = (comment_text or "").strip().lower()
    candidates = [
        ("Salmos 46:1", "Deus é o nosso refúgio e fortaleza, socorro bem presente na angústia."),
        ("Filipenses 4:6-7", "Não andem ansiosos... e a paz de Deus guardará o coração e a mente em Cristo Jesus."),
        ("Isaías 41:10", "Não temas, porque eu sou contigo; não te assombres, porque eu sou o teu Deus."),
        ("Mateus 11:28", "Vinde a mim, todos os que estais cansados e sobrecarregados, e eu vos aliviarei."),
        ("Romanos 8:28", "Todas as coisas cooperam para o bem daqueles que amam a Deus."),
        ("Salmos 23:1", "O Senhor é o meu pastor; nada me faltará."),
        ("Josué 1:9", "Sê forte e corajoso... porque o Senhor teu Deus é contigo por onde quer que andares."),
        ("Provérbios 3:5-6", "Confia no Senhor de todo o teu coração... e ele endireitará os teus caminhos."),
        ("1 Pedro 5:7", "Lancem sobre ele toda a vossa ansiedade, porque ele tem cuidado de vós."),
        ("Romanos 15:13", "O Deus da esperança vos encha de toda alegria e paz na fé."),
        ("Isaías 40:31", "Os que esperam no Senhor renovam as suas forças."),
        ("João 3:16", "Porque Deus amou o mundo de tal maneira que deu o seu Filho unigênito..."),
        ("1 João 1:9", "Se confessarmos os nossos pecados, ele é fiel e justo para nos perdoar."),
    ]

    thematic: Optional[Tuple[str, str]] = None
    if any(k in low for k in ["ansied", "ansioso", "preocupa", "afli", "press"]):
        thematic = ("1 Pedro 5:7", "Lancem sobre ele toda a vossa ansiedade, porque ele tem cuidado de vós.")
    elif any(k in low for k in ["medo", "temor", "pavor"]):
        thematic = ("Isaías 41:10", "Não temas, porque eu sou contigo; não te assombres, porque eu sou o teu Deus.")
    elif any(k in low for k in ["cans", "sobrecar", "esgot", "exaust"]):
        thematic = ("Mateus 11:28", "Vinde a mim, todos os que estais cansados e sobrecarregados, e eu vos aliviarei.")
    elif any(k in low for k in ["perd", "pecad", "culpa"]):
        thematic = ("1 João 1:9", "Se confessarmos os nossos pecados, ele é fiel e justo para nos perdoar.")
    elif any(k in low for k in ["amor", "amou", "amei"]):
        thematic = ("João 3:16", "Porque Deus amou o mundo de tal maneira que deu o seu Filho unigênito...")
    elif any(k in low for k in ["esper", "futuro", "amanhã"]):
        thematic = ("Romanos 15:13", "O Deus da esperança vos encha de toda alegria e paz na fé.")
    elif any(k in low for k in ["paz", "calma"]):
        thematic = ("Filipenses 4:6-7", "Não andem ansiosos... e a paz de Deus guardará o coração e a mente em Cristo Jesus.")
    elif any(k in low for k in ["força", "forte", "coragem", "coraj"]):
        thematic = ("Josué 1:9", "Sê forte e corajoso... porque o Senhor teu Deus é contigo por onde quer que andares.")

    if thematic:
        return thematic

    s = (seed or "").strip() or "seed"
    idx = int(hashlib.md5(s.encode("utf-8")).hexdigest(), 16) % max(1, len(candidates))
    return candidates[idx]


def _build_reply_text(template: str, author: Optional[str], comment_text: str, seed: str) -> str:
    name = (author or "").strip()
    if not name:
        name = "amigo"

    text = (template or "").strip()
    if not text:
        text = "Obrigado por assistir e comentar, {author}! Se ainda não é inscrito, se inscreva no canal e ative o sininho.\n\n{verse}"

    verse_ref, verse_txt = _pick_verse(comment_text or "", seed)
    verse_block = f"Versículo para meditar: {verse_txt} ({verse_ref})"

    out = (
        text.replace("{author}", name)
        .replace("{verse}", verse_block)
        .replace("{verse_ref}", verse_ref)
        .replace("{verse_text}", verse_txt)
    )
    out = " ".join(out.split()).strip()
    if "{verse}" not in (template or "") and "versículo" not in out.lower():
        out = f"{out}\n\n{verse_block}".strip()
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

            reply_text = _build_reply_text(template, author, getattr(item, "text", None) or "", cid)
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
