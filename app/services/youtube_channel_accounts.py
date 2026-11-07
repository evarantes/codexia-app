"""Secure additional YouTube channel connections for Codexia projects."""
from __future__ import annotations

import json
import re
from typing import Any

from app.database import SessionLocal
from app.models import ProjectYouTubeChannelBinding, YouTubeChannelAccount


class YouTubeChannelAccountError(ValueError):
    pass


def _project_key(value: str) -> str:
    key = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", key):
        raise YouTubeChannelAccountError("Projeto inválido.")
    return key


def save_connected_channel(*, user_id: int, project_key: str, credentials: Any) -> dict:
    """Verify OAuth access, encrypt the refresh token, and bind the channel."""
    key = _project_key(project_key)
    if type(user_id) is not int or user_id <= 0:
        raise YouTubeChannelAccountError("Usuário inválido.")
    refresh_token = str(getattr(credentials, "refresh_token", None) or "").strip()
    if not refresh_token:
        raise YouTubeChannelAccountError("O Google não retornou um token de renovação.")
    try:
        from googleapiclient.discovery import build
        from app.services.google_drive_archive import encrypt_drive_secret

        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        result = youtube.channels().list(part="id,snippet", mine=True, maxResults=1).execute()
        channels = result.get("items") or []
        if not channels:
            raise YouTubeChannelAccountError("A conta autorizada não possui um canal do YouTube acessível.")
        channel = channels[0]
        channel_id = str(channel.get("id") or "").strip()
        snippet = channel.get("snippet") or {}
        title = str(snippet.get("title") or "").strip()
        if not channel_id or not title:
            raise YouTubeChannelAccountError("O YouTube não retornou os dados do canal.")
        thumbnails = snippet.get("thumbnails") or {}
        thumbnail_url = next(
            (str((thumbnails.get(size) or {}).get("url") or "") for size in ("high", "medium", "default")
             if (thumbnails.get(size) or {}).get("url")),
            None,
        )
        encrypted = encrypt_drive_secret(refresh_token)
        scopes = getattr(credentials, "scopes", None) or []
        db = SessionLocal()
        try:
            account = (
                db.query(YouTubeChannelAccount)
                .filter(
                    YouTubeChannelAccount.user_id == user_id,
                    YouTubeChannelAccount.channel_id == channel_id,
                )
                .first()
            )
            if account is None:
                account = YouTubeChannelAccount(
                    user_id=user_id,
                    channel_id=channel_id,
                    channel_title=title,
                    refresh_token_encrypted=encrypted,
                )
                db.add(account)
                db.flush()
            account.channel_title = title
            account.thumbnail_url = thumbnail_url
            account.refresh_token_encrypted = encrypted
            account.scopes_json = json.dumps(list(scopes), ensure_ascii=False)
            account.is_active = True

            binding = (
                db.query(ProjectYouTubeChannelBinding)
                .filter(
                    ProjectYouTubeChannelBinding.user_id == user_id,
                    ProjectYouTubeChannelBinding.project_key == key,
                )
                .first()
            )
            if binding is None:
                binding = ProjectYouTubeChannelBinding(
                    user_id=user_id,
                    project_key=key,
                    youtube_channel_account_id=account.id,
                )
                db.add(binding)
            else:
                binding.youtube_channel_account_id = account.id
            db.commit()
            db.refresh(account)
            return {
                "id": int(account.id),
                "channel_id": account.channel_id,
                "channel_title": account.channel_title,
                "thumbnail_url": account.thumbnail_url,
                "project_key": key,
                "linked": True,
            }
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except YouTubeChannelAccountError:
        raise
    except Exception as exc:
        raise YouTubeChannelAccountError(
            "Não foi possível validar e salvar a conexão adicional do YouTube."
        ) from exc


def link_channel(*, user_id: int, project_key: str, account_id: int) -> dict:
    key = _project_key(project_key)
    db = SessionLocal()
    try:
        account = (
            db.query(YouTubeChannelAccount)
            .filter(
                YouTubeChannelAccount.id == int(account_id),
                YouTubeChannelAccount.user_id == int(user_id),
                YouTubeChannelAccount.is_active.is_(True),
            )
            .first()
        )
        if account is None:
            raise YouTubeChannelAccountError("Canal não encontrado para este usuário.")
        binding = (
            db.query(ProjectYouTubeChannelBinding)
            .filter(
                ProjectYouTubeChannelBinding.user_id == int(user_id),
                ProjectYouTubeChannelBinding.project_key == key,
            )
            .first()
        )
        if binding is None:
            binding = ProjectYouTubeChannelBinding(
                user_id=int(user_id), project_key=key, youtube_channel_account_id=account.id,
            )
            db.add(binding)
        else:
            binding.youtube_channel_account_id = account.id
        db.commit()
        return {
            "linked": True, "project_key": key, "channel_id": account.channel_id,
            "channel_title": account.channel_title,
        }
    except YouTubeChannelAccountError:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise YouTubeChannelAccountError("Não foi possível vincular este canal ao projeto.") from exc
    finally:
        db.close()


def list_project_channels(*, user_id: int, project_key: str) -> dict:
    key = _project_key(project_key)
    db = SessionLocal()
    try:
        accounts = (
            db.query(YouTubeChannelAccount)
            .filter(
                YouTubeChannelAccount.user_id == int(user_id),
                YouTubeChannelAccount.is_active.is_(True),
            )
            .order_by(YouTubeChannelAccount.channel_title.asc(), YouTubeChannelAccount.id.asc())
            .all()
        )
        binding = (
            db.query(ProjectYouTubeChannelBinding)
            .filter(
                ProjectYouTubeChannelBinding.user_id == int(user_id),
                ProjectYouTubeChannelBinding.project_key == key,
            )
            .first()
        )
        selected = int(binding.youtube_channel_account_id) if binding else None
        return {
            "project_key": key,
            "selected_account_id": selected,
            "channels": [
                {
                    "id": int(item.id),
                    "channel_id": item.channel_id,
                    "channel_title": item.channel_title,
                    "thumbnail_url": item.thumbnail_url,
                    "selected": int(item.id) == selected,
                }
                for item in accounts
            ],
        }
    finally:
        db.close()
