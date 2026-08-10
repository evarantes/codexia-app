import os
import json
import time
import uuid
import base64
import hashlib
import secrets
from typing import Optional, Tuple, Dict, Any
import logging
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from app.database import SessionLocal
from app.models import Settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtubepartner",
]

_OAUTH_STATE_STORE: Dict[str, Dict[str, Any]] = {}

def default_oauth_redirect_uri() -> str:
    raw = (os.getenv("YOUTUBE_OAUTH_REDIRECT_URI") or "").strip()
    if raw:
        return raw
    port = str((os.getenv("PORT") or os.getenv("APP_PORT") or "8010")).strip() or "8010"
    return f"http://127.0.0.1:{port}/youtube/auth/callback"

def _cleanup_expired_state_store() -> None:
    now = time.time()
    for k in list(_OAUTH_STATE_STORE.keys()):
        entry = _OAUTH_STATE_STORE.get(k) or {}
        if float(entry.get("created_at") or 0) + 600.0 < now:
            try:
                del _OAUTH_STATE_STORE[k]
            except Exception:
                pass

def oauth_pop_state(state: str) -> Optional[Dict[str, Any]]:
    _cleanup_expired_state_store()
    entry = _OAUTH_STATE_STORE.pop(state, None)
    if not entry:
        return None
    if float(entry.get("created_at") or 0) + 600.0 < time.time():
        return None
    return entry


def oauth_peek_state(state: str) -> Optional[Dict[str, Any]]:
    """Consulta um state válido sem consumi-lo antes de validar o redirect URI."""
    _cleanup_expired_state_store()
    entry = _OAUTH_STATE_STORE.get(str(state or "").strip())
    if not entry:
        return None
    if float(entry.get("created_at") or 0) + 600.0 < time.time():
        return None
    return dict(entry)

def pkce_verifier_and_challenge() -> Tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge

def _presence(value: str) -> str:
    return "PRESENT" if str(value or "").strip() else "EMPTY"

_YT_CACHE = {
    "my_channel_id": None,
    "my_channel_id_expires_at": 0.0,
    "my_channel": None,
    "my_channel_expires_at": 0.0,
}

def _yt_cache_get(key: str):
    try:
        expires_at = float(_YT_CACHE.get(f"{key}_expires_at") or 0.0)
        if expires_at and time.time() < expires_at:
            return _YT_CACHE.get(key)
    except Exception:
        return None
    return None

def _yt_cache_set(key: str, value, ttl_seconds: int):
    try:
        _YT_CACHE[key] = value
        _YT_CACHE[f"{key}_expires_at"] = float(time.time() + max(1, int(ttl_seconds)))
    except Exception:
        pass

def _parse_http_error_reason(e: Exception) -> Optional[str]:
    try:
        if isinstance(e, HttpError) and getattr(e, "content", None):
            raw = e.content.decode("utf-8", errors="ignore") if isinstance(e.content, (bytes, bytearray)) else str(e.content)
            data = json.loads(raw) if raw else {}
            err = (data or {}).get("error") or {}
            errors = err.get("errors") or []
            if errors and isinstance(errors[0], dict):
                reason = errors[0].get("reason")
                if reason:
                    return str(reason)
    except Exception:
        return None
    return None

class YouTubeService:
    def __init__(self):
        self.credentials = None
        self.service = None
        self.auth_source = None
        self.auth_error = None
        self._cached_my_channel_id = None
        self._load_credentials()

    def _get_my_channel_id(self):
        cached = _yt_cache_get("my_channel_id")
        if cached:
            self._cached_my_channel_id = cached
            return cached
        if self._cached_my_channel_id:
            _yt_cache_set("my_channel_id", self._cached_my_channel_id, ttl_seconds=6 * 3600)
            return self._cached_my_channel_id
        if not self.service:
            return None
        try:
            request = self.service.channels().list(part="id", mine=True)
            response = request.execute()
            items = response.get("items") or []
            if items:
                self._cached_my_channel_id = items[0].get("id")
                if self._cached_my_channel_id:
                    _yt_cache_set("my_channel_id", self._cached_my_channel_id, ttl_seconds=6 * 3600)
                return self._cached_my_channel_id
        except Exception:
            return None
        return None

    def _read_env_youtube_creds(self):
        client_id = (os.getenv("YOUTUBE_CLIENT_ID") or "").strip()
        client_secret = (os.getenv("YOUTUBE_CLIENT_SECRET") or "").strip()
        refresh_token = (os.getenv("YOUTUBE_REFRESH_TOKEN") or "").strip()
        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }

    def _read_db_youtube_creds(self, settings):
        return {
            "client_id": (getattr(settings, "youtube_client_id", None) or "").strip() if settings else "",
            "client_secret": (getattr(settings, "youtube_client_secret", None) or "").strip() if settings else "",
            "refresh_token": (getattr(settings, "youtube_refresh_token", None) or "").strip() if settings else "",
        }

    def _compose_info(self, base):
        return {
            "client_id": base.get("client_id"),
            "client_secret": base.get("client_secret"),
            "refresh_token": base.get("refresh_token"),
            "token_uri": "https://oauth2.googleapis.com/token",
        }

    def _has_full_creds(self, data):
        return bool((data.get("client_id") or "").strip() and (data.get("client_secret") or "").strip() and (data.get("refresh_token") or "").strip())

    def _oauth_client_id_secret(self, settings=None):
        db_creds = self._read_db_youtube_creds(settings)
        env_creds = self._read_env_youtube_creds()
        client_id = db_creds.get("client_id") or env_creds.get("client_id")
        client_secret = db_creds.get("client_secret") or env_creds.get("client_secret")
        return (client_id or "").strip(), (client_secret or "").strip()

    def _load_credentials(self):
        """Carrega credenciais do banco ou arquivo"""
        settings = None
        logger.info(
            "YouTube OAuth: load_credentials start token.json=%s client_secret.json=%s",
            "PRESENT" if os.path.exists("token.json") else "ABSENT",
            "PRESENT" if os.path.exists("client_secret.json") else "ABSENT",
        )

        # 1. Tentar carregar do Banco de Dados
        try:
            db = SessionLocal()
            settings = db.query(Settings).first()
            db.close()

            if settings and settings.youtube_refresh_token and settings.youtube_client_id and settings.youtube_client_secret:
                try:
                    info = {
                        "client_id": settings.youtube_client_id,
                        "client_secret": settings.youtube_client_secret,
                        "refresh_token": settings.youtube_refresh_token,
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                    # Não passar SCOPES aqui para evitar erro de invalid_scope se o token tiver escopos diferentes
                    self.credentials = Credentials.from_authorized_user_info(info, scopes=None)
                    self.auth_source = "database"
                except Exception as e:
                    logger.warning("YouTube OAuth: erro ao montar credenciais do banco (%s)", type(e).__name__)
                    self.auth_error = f"Erro ao ler credenciais do banco: {e}"
        except Exception as e:
            logger.warning("YouTube OAuth: erro ao acessar banco para credenciais (%s)", type(e).__name__)
            self.auth_error = f"Erro ao acessar banco para credenciais: {e}"
            settings = None

        db_creds = self._read_db_youtube_creds(settings)
        env_creds = self._read_env_youtube_creds()
        logger.info(
            "YouTube OAuth: sources db(client_id=%s client_secret=%s refresh_token=%s) env(client_id=%s client_secret=%s refresh_token=%s)",
            _presence(db_creds.get("client_id")),
            _presence(db_creds.get("client_secret")),
            _presence(db_creds.get("refresh_token")),
            _presence(env_creds.get("client_id")),
            _presence(env_creds.get("client_secret")),
            _presence(env_creds.get("refresh_token")),
        )
        mixed_db_env = {
            "client_id": db_creds.get("client_id") or env_creds.get("client_id"),
            "client_secret": db_creds.get("client_secret") or env_creds.get("client_secret"),
            "refresh_token": db_creds.get("refresh_token") or env_creds.get("refresh_token"),
        }
        mixed_env_db = {
            "client_id": env_creds.get("client_id") or db_creds.get("client_id"),
            "client_secret": env_creds.get("client_secret") or db_creds.get("client_secret"),
            "refresh_token": env_creds.get("refresh_token") or db_creds.get("refresh_token"),
        }

        potential = []
        if self._has_full_creds(db_creds):
            potential.append(("database", db_creds))
        if self._has_full_creds(env_creds):
            potential.append(("environment", env_creds))
        if self._has_full_creds(mixed_db_env):
            potential.append(("mixed_db_env", mixed_db_env))
        if self._has_full_creds(mixed_env_db):
            potential.append(("mixed_env_db", mixed_env_db))

        candidates = []
        seen = set()
        for source, raw in potential:
            key = (
                (raw.get("client_id") or "").strip(),
                (raw.get("client_secret") or "").strip(),
                (raw.get("refresh_token") or "").strip(),
            )
            if key in seen:
                continue
            seen.add(key)
            candidates.append((source, raw))

        logger.info("YouTube OAuth: candidates=%s", [c[0] for c in candidates])
        last_error = None
        for source, raw in candidates:
            try:
                creds = Credentials.from_authorized_user_info(self._compose_info(raw), scopes=None)
                if creds.refresh_token and (not creds.valid or creds.expired):
                    creds.refresh(Request())
                service = build('youtube', 'v3', credentials=creds)
                self.credentials = creds
                self.service = service
                self.auth_source = source
                self.auth_error = None
                logger.info(
                    "YouTube OAuth: selected source=%s refresh_token=%s valid=%s expired=%s",
                    source,
                    _presence(getattr(creds, "refresh_token", None)),
                    bool(getattr(creds, "valid", False)),
                    bool(getattr(creds, "expired", False)),
                )
                return
            except Exception as e:
                last_error = f"{source}: {e}"
                logger.warning(
                    "Erro ao validar credenciais YouTube (%s): %s (%s) http_reason=%s",
                    source,
                    str(e).splitlines()[0][:220],
                    type(e).__name__,
                    _parse_http_error_reason(e),
                )

        # Fallback para arquivo local (desenvolvimento)
        if os.path.exists('token.json'):
            try:
                creds = Credentials.from_authorized_user_file('token.json', SCOPES)
                if creds.refresh_token and (not creds.valid or creds.expired):
                    creds.refresh(Request())
                self.credentials = creds
                self.service = build('youtube', 'v3', credentials=creds)
                self.auth_source = "token_file"
                self.auth_error = None
                logger.info(
                    "YouTube OAuth: selected source=token_file refresh_token=%s valid=%s expired=%s",
                    _presence(getattr(creds, "refresh_token", None)),
                    bool(getattr(creds, "valid", False)),
                    bool(getattr(creds, "expired", False)),
                )
                return
            except Exception as e:
                logger.warning("Erro ao carregar token.json: %s (%s)", str(e).splitlines()[0][:220], type(e).__name__)
                last_error = f"token_file: {e}"

        self.credentials = None
        self.service = None
        self.auth_error = last_error or "Credenciais do YouTube ausentes (banco/ENV/token.json)."
        logger.warning("YouTube OAuth: no valid credentials auth_error=%s", str(self.auth_error or "").splitlines()[0][:220])

    def list_video_comments(self, youtube_video_id: str, max_results: int = 100):
        """Lista comentários (threads) de um vídeo. Retorna lista achatada de comentários (top-level e replies)."""
        if not self.service:
            raise RuntimeError(self.auth_error or "Serviço do YouTube não inicializado.")
        my_channel_id = self._get_my_channel_id()
        items = []
        try:
            page_token = None
            while True:
                req = self.service.commentThreads().list(
                    part="snippet,replies",
                    videoId=youtube_video_id,
                    maxResults=min(max_results, 100),
                    order="time",
                    textFormat="plainText",
                    pageToken=page_token
                )
                resp = req.execute()
                for th in resp.get("items", []):
                    top = th.get("snippet", {}).get("topLevelComment", {})
                    s = top.get("snippet", {}) if top else {}
                    if top:
                        author_channel_id = ((s.get("authorChannelId") or {}) if isinstance(s, dict) else {}).get("value")
                        is_owner = bool(author_channel_id and my_channel_id and author_channel_id == my_channel_id)
                        items.append({
                            "youtube_comment_id": top.get("id"),
                            "youtube_parent_id": None,
                            "youtube_video_id": youtube_video_id,
                            "author": s.get("authorDisplayName"),
                            "author_channel_id": author_channel_id,
                            "author_is_channel_owner": is_owner,
                            "text": s.get("textDisplay") or s.get("textOriginal"),
                            "like_count": s.get("likeCount", 0),
                            "published_at": s.get("publishedAt"),
                        })
                    for rep in (th.get("replies", {}) or {}).get("comments", []) or []:
                        rs = rep.get("snippet", {}) if rep else {}
                        author_channel_id = ((rs.get("authorChannelId") or {}) if isinstance(rs, dict) else {}).get("value")
                        is_owner = bool(author_channel_id and my_channel_id and author_channel_id == my_channel_id)
                        items.append({
                            "youtube_comment_id": rep.get("id"),
                            "youtube_parent_id": top.get("id") if top else None,
                            "youtube_video_id": youtube_video_id,
                            "author": rs.get("authorDisplayName"),
                            "author_channel_id": author_channel_id,
                            "author_is_channel_owner": is_owner,
                            "text": rs.get("textDisplay") or rs.get("textOriginal"),
                            "like_count": rs.get("likeCount", 0),
                            "published_at": rs.get("publishedAt"),
                        })
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as e:
            raise RuntimeError(f"Erro YouTube API (comments): {e}")
        return items

    def list_channel_comments(self, max_results: int = 200):
        if not self.service:
            raise RuntimeError(self.auth_error or "Serviço do YouTube não inicializado.")
        my_channel_id = self._get_my_channel_id()
        if not my_channel_id:
            raise RuntimeError("Não foi possível descobrir o channelId do canal autenticado.")
        items = []
        try:
            page_token = None
            while True:
                req = self.service.commentThreads().list(
                    part="snippet,replies",
                    allThreadsRelatedToChannelId=my_channel_id,
                    maxResults=min(max_results, 100),
                    order="time",
                    textFormat="plainText",
                    pageToken=page_token,
                )
                resp = req.execute()
                for th in resp.get("items", []):
                    top = th.get("snippet", {}).get("topLevelComment", {})
                    s = top.get("snippet", {}) if top else {}
                    video_id = (th.get("snippet", {}) or {}).get("videoId")
                    if top:
                        author_channel_id = ((s.get("authorChannelId") or {}) if isinstance(s, dict) else {}).get("value")
                        is_owner = bool(author_channel_id and my_channel_id and author_channel_id == my_channel_id)
                        items.append({
                            "youtube_comment_id": top.get("id"),
                            "youtube_parent_id": None,
                            "youtube_video_id": video_id,
                            "author": s.get("authorDisplayName"),
                            "author_channel_id": author_channel_id,
                            "author_is_channel_owner": is_owner,
                            "text": s.get("textDisplay") or s.get("textOriginal"),
                            "like_count": s.get("likeCount", 0),
                            "published_at": s.get("publishedAt"),
                        })
                    for rep in (th.get("replies", {}) or {}).get("comments", []) or []:
                        rs = rep.get("snippet", {}) if rep else {}
                        author_channel_id = ((rs.get("authorChannelId") or {}) if isinstance(rs, dict) else {}).get("value")
                        is_owner = bool(author_channel_id and my_channel_id and author_channel_id == my_channel_id)
                        items.append({
                            "youtube_comment_id": rep.get("id"),
                            "youtube_parent_id": top.get("id") if top else None,
                            "youtube_video_id": video_id,
                            "author": rs.get("authorDisplayName"),
                            "author_channel_id": author_channel_id,
                            "author_is_channel_owner": is_owner,
                            "text": rs.get("textDisplay") or rs.get("textOriginal"),
                            "like_count": rs.get("likeCount", 0),
                            "published_at": rs.get("publishedAt"),
                        })
                if len(items) >= int(max_results):
                    break
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as e:
            raise RuntimeError(f"Erro YouTube API (channel comments): {e}")
        return items[: int(max_results)]

    def reply_to_comment(self, parent_comment_id: str, text: str):
        """Responde a um comentário (parentId = comentário top-level)."""
        if not self.service:
            raise RuntimeError(self.auth_error or "Serviço do YouTube não inicializado.")
        try:
            body = {
                "snippet": {
                    "parentId": parent_comment_id,
                    "textOriginal": text
                }
            }
            req = self.service.comments().insert(part="snippet", body=body)
            resp = req.execute()
            return resp
        except HttpError as e:
            raise RuntimeError(f"Erro ao responder comentário: {e}")

    def _build_flow_from_creds_or_file(self, redirect_uri: str) -> Tuple[Optional[InstalledAppFlow], Optional[str], Optional[str]]:
        """Monta o Google OAuth flow com um redirect_uri EXATO (não OOB)."""
        redirect_uri = (redirect_uri or "").strip() or default_oauth_redirect_uri()
        db = SessionLocal()
        settings = db.query(Settings).first()
        db.close()
        client_id, client_secret = self._oauth_client_id_secret(settings)
        used_json_file = False
        if client_id and client_secret:
            try:
                client_config = {
                    "installed": {
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": [redirect_uri],
                    }
                }
                flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
                flow.redirect_uri = redirect_uri
                return flow, None, None
            except Exception as e:
                logger.warning("YouTube OAuth: failed to create flow from db/env (%s)", type(e).__name__)
        if os.path.exists("client_secret.json"):
            try:
                flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
                flow.redirect_uri = redirect_uri
                used_json_file = True
                client_id_override = None
                client_secret_override = None
                try:
                    with open("client_secret.json", "r") as f:
                        data = json.load(f)
                    installed = data.get("installed", data.get("web", {}))
                    client_id_override = str(installed.get("client_id") or "").strip() or None
                    client_secret_override = str(installed.get("client_secret") or "").strip() or None
                except Exception:
                    pass
                return flow, client_id_override, client_secret_override
            except Exception as e:
                logger.warning("YouTube OAuth: failed to create flow from client_secret.json (%s)", type(e).__name__)
        return None, None, None

    def get_auth_url_with_state(self, redirect_uri: Optional[str] = None) -> Dict[str, Any]:
        """Novo fluxo moderno: retorna {auth_url, state, code_verifier, redirect_uri}.
        Não usa mais OOB. Gera state aleatório e PKCE, e guarda em _OAUTH_STATE_STORE.
        """
        redirect_uri = (redirect_uri or "").strip() or default_oauth_redirect_uri()
        flow, _, _ = self._build_flow_from_creds_or_file(redirect_uri)
        if flow is None:
            raise FileNotFoundError(
                "Credenciais do YouTube não configuradas. Use Settings.youtube_client_id / youtube_client_secret "
                "ou variáveis YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET ou arquivo client_secret.json."
            )
        state = uuid.uuid4().hex + secrets.token_hex(8)
        verifier, challenge = pkce_verifier_and_challenge()
        flow.code_verifier = verifier
        try:
            auth_url, _ = flow.authorization_url(
                prompt="consent",
                access_type="offline",
                include_granted_scopes="true",
                state=state,
                code_challenge=challenge,
                code_challenge_method="S256",
            )
        except TypeError:
            auth_url, _ = flow.authorization_url(
                prompt="consent",
                access_type="offline",
                include_granted_scopes="true",
                state=state,
            )
        _cleanup_expired_state_store()
        _OAUTH_STATE_STORE[state] = {
            "created_at": time.time(),
            "state": state,
            "code_verifier": verifier,
            "redirect_uri": flow.redirect_uri or redirect_uri,
            "challenge": challenge,
        }
        try:
            if os.path.exists("youtube_pkce.txt"):
                os.remove("youtube_pkce.txt")
        except Exception:
            pass
        return {
            "auth_url": auth_url,
            "state": state,
            "redirect_uri": flow.redirect_uri or redirect_uri,
        }

    def get_auth_url(self):
        """Compatibilidade antiga. Usa o novo fluxo com redirect_uri moderno (não OOB)."""
        result = self.get_auth_url_with_state()
        return result["auth_url"]

    def exchange_code_for_token_with_state(self, code: str, state: str, redirect_uri: Optional[str] = None) -> Tuple[bool, str, Dict[str, Any]]:
        """Novo fluxo: valida state, troca code por token, salva, valida API real.
        Retorna (ok, mensagem, dict_safe_com_metadados_sem_segredos)."""
        audit: Dict[str, Any] = {
            "http_status": None,
            "error": None,
            "error_description": None,
            "redirect_uri": None,
            "refresh_token_present": False,
            "state_valid": False,
            "state_consumed": False,
            "pkce_preserved": False,
            "redirect_uri_consistent": False,
            "persisted": False,
            "service_verified": False,
        }
        code = str(code or "").strip()
        state = str(state or "").strip()
        if not code:
            audit["error"] = "missing_code"
            audit["error_description"] = "Parâmetro 'code' não fornecido pelo Google."
            return False, (audit["error_description"] or "Código de autorização ausente."), audit
        if not state:
            audit["error"] = "missing_state"
            audit["error_description"] = "Parâmetro 'state' não fornecido no callback."
            return False, (audit["error_description"] or "State ausente."), audit
        stored = oauth_peek_state(state)
        if not stored:
            audit["error"] = "invalid_state"
            audit["error_description"] = (
                "State OAuth inválido ou expirado. Tente conectar novamente (botão Conectar YouTube)."
            )
            return False, (audit["error_description"] or "State inválido/expirado."), audit
        audit["state_valid"] = True
        stored_verifier = str(stored.get("code_verifier") or "").strip()
        stored_redirect_uri = str(stored.get("redirect_uri") or "").strip()
        effective_redirect_uri = (redirect_uri or "").strip() or stored_redirect_uri or default_oauth_redirect_uri()
        audit["redirect_uri"] = effective_redirect_uri
        if effective_redirect_uri and stored_redirect_uri and effective_redirect_uri != stored_redirect_uri:
            audit["error"] = "redirect_uri_mismatch"
            audit["error_description"] = (
                f"redirect_uri inconsistente entre auth_url e callback "
                f"(stored={stored_redirect_uri} callback={effective_redirect_uri})."
            )
            return False, (audit["error_description"] or "redirect_uri divergente."), audit
        audit["redirect_uri_consistent"] = True
        # Consome somente depois de state + redirect URI passarem. Assim, um
        # callback malformado não invalida a tentativa correta ainda em curso.
        stored = oauth_pop_state(state)
        if not stored:
            audit["state_valid"] = False
            audit["error"] = "invalid_state"
            audit["error_description"] = "State OAuth já consumido ou expirado. Conecte novamente."
            return False, audit["error_description"], audit
        audit["state_consumed"] = True
        flow, cid_override, csec_override = self._build_flow_from_creds_or_file(effective_redirect_uri)
        if flow is None:
            audit["error"] = "credentials_missing"
            audit["error_description"] = (
                "Credenciais do YouTube não configuradas durante o exchange. "
                "Verifique Settings.youtube_client_id / youtube_client_secret ou ENV."
            )
            return False, (audit["error_description"] or "Credenciais ausentes."), audit
        if stored_verifier:
            flow.code_verifier = stored_verifier
            audit["pkce_preserved"] = True
        http_status = None
        google_error = None
        google_error_desc = None
        try:
            flow.fetch_token(code=code)
            http_status = 200
        except Exception as e:
            msg_parts = str(e).splitlines()
            msg = msg_parts[0][:500] if msg_parts else type(e).__name__
            try:
                raw = getattr(e, "error_details", None) or getattr(e, "response", None)
                if isinstance(raw, dict):
                    google_error = str(raw.get("error") or google_error or "").strip() or None
                    google_error_desc = str(raw.get("error_description") or "").strip() or None
                    if google_error:
                        pass
                if hasattr(e, "args") and e.args:
                    head = str(e.args[0])
                    if "400" in head:
                        http_status = 400
                    elif "401" in head:
                        http_status = 401
            except Exception:
                pass
            if http_status is None:
                http_status = 400
            audit["http_status"] = http_status
            audit["error"] = google_error or "fetch_token_failed"
            audit["error_description"] = google_error_desc or msg
            return False, (
                f"Falha na troca do authorization code pelo token (HTTP {http_status}). "
                f"Google error={audit['error']}. "
                f"Detalhe: {audit['error_description']}"
            ), audit
        audit["http_status"] = http_status
        self.credentials = flow.credentials
        refresh_token = (getattr(self.credentials, "refresh_token", None) or "").strip()
        audit["refresh_token_present"] = bool(refresh_token)
        if not refresh_token:
            audit["error"] = "refresh_token_missing"
            audit["error_description"] = (
                "Google não forneceu um refresh_token novo. "
                "Acesse myaccount.google.com/permissions, revogue o Codexia, "
                "e clique novamente em Conectar YouTube (prompt=consent obrigatório)."
            )
            return False, (audit["error_description"] or "refresh_token ausente."), audit
        try:
            self._save_credentials_to_db(cid_override, csec_override)
            audit["persisted"] = True
        except Exception as e:
            audit["error"] = "persist_failed"
            audit["error_description"] = f"Erro ao salvar credenciais no banco: {type(e).__name__}"
            return False, (audit["error_description"] or "Falha ao persistir no DB."), audit
        try:
            verification = YouTubeService()
            if not getattr(verification, "service", None) or (
                "invalid_grant" in str(getattr(verification, "auth_error", "") or "")
            ):
                audit["error"] = "verification_invalid_grant"
                audit["error_description"] = (
                    "Credenciais persistidas, mas a validação imediata retornou invalid_grant. "
                    "Revogue e refaça a conexão."
                )
                return False, (audit["error_description"] or "invalid_grant na verificação final."), audit
            audit["service_verified"] = True
        except Exception as e:
            audit["error"] = "verification_exception"
            audit["error_description"] = f"Erro na validação final do serviço: {type(e).__name__}"
            return False, (audit["error_description"] or "Erro na validação final."), audit
        return True, "Conexão com o YouTube concluída com sucesso!", audit

    def exchange_code_for_token(self, code):
        """Fallback compat com fluxo manual antigo (manualmente colar código no Swagger).
        Hoje — OOB foi descontinuado pelo Google, então isso quase sempre retorna erro.
        Mantido como fallback claramente identificado. [FALLBACK TEMPORÁRIO]
        """
        # Nós NÃO temos state neste caminho. Tentamos usar o state store se houver
        # apenas um state vivo (ambiente de dev/teste single-user). Caso contrário,
        # falhamos com mensagem clara orientando a usar o fluxo novo.
        _cleanup_expired_state_store()
        states = list(_OAUTH_STATE_STORE.keys())
        if len(states) != 1:
            return False, (
                "[FALLBACK TEMPORÁRIO — FLUXO MANUAL DESCONTINUADO PELO GOOGLE (OOB removido)]. "
                "Use o fluxo novo: clique em Conectar YouTube, autorize no Google, e deixe o callback automático "
                "/youtube/auth/callback processar. Não cole mais códigos manualmente."
            )
        state = states[0]
        return self.exchange_code_for_token_with_state(code=code, state=state)[:2]

    def _save_credentials_to_db(self, client_id=None, client_secret=None):
        """Salva as credenciais atuais no banco de dados"""
        if not self.credentials:
            return

        db = SessionLocal()
        try:
            settings = db.query(Settings).first()
            if not settings:
                settings = Settings()
                db.add(settings)

            refresh_token = (getattr(self.credentials, "refresh_token", None) or "").strip()
            if not refresh_token:
                logger.warning("YouTube OAuth: save aborted refresh_token=EMPTY (não sobrescrevendo token anterior)")
                raise RuntimeError(
                    "Google não forneceu um novo refresh token. A autenticação não pode ser concluída."
                )

            settings.youtube_refresh_token = refresh_token

            # Atualizar client_id/secret se fornecidos ou presentes nas credenciais
            current_client_id = getattr(self.credentials, 'client_id', None) or client_id
            if current_client_id:
                settings.youtube_client_id = current_client_id.strip()
            
            current_client_secret = getattr(self.credentials, 'client_secret', None) or client_secret
            if current_client_secret:
                settings.youtube_client_secret = current_client_secret.strip()

            db.commit()
            logger.info(
                "YouTube OAuth: credentials saved db(client_id=%s client_secret=%s refresh_token=%s)",
                _presence(getattr(settings, "youtube_client_id", None)),
                _presence(getattr(settings, "youtube_client_secret", None)),
                _presence(getattr(settings, "youtube_refresh_token", None)),
            )
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning("YouTube OAuth: save failed %s (%s)", str(e).splitlines()[0][:220], type(e).__name__)
            raise
        finally:
            db.close()


    def _get_my_channel(self, use_cache: bool = True):
        """Helper para buscar o canal autenticado"""
        if not self.service:
            return None
            
        try:
            if use_cache:
                cached = _yt_cache_get("my_channel")
                if cached:
                    return cached
            request = self.service.channels().list(
                part="snippet,statistics,brandingSettings,contentDetails",
                mine=True
            )
            response = request.execute()
            
            if response['items']:
                ch = response['items'][0]
                if use_cache:
                    _yt_cache_set("my_channel", ch, ttl_seconds=180)
                return ch
        except Exception as e:
            print(f"Erro no helper _get_my_channel: {e}")
            raise e
        return None

    def get_recent_videos_stats(self, limit=10):
        """Busca estatísticas dos vídeos recentes"""
        if not self.service:
            return []
        
        try:
            # 1. Get Uploads Playlist ID
            channel = self._get_my_channel()
            if not channel:
                return []
                
            uploads_playlist_id = channel['contentDetails']['relatedPlaylists']['uploads']
            
            # 2. Get Videos from Playlist
            playlist_items = self.service.playlistItems().list(
                part="snippet,contentDetails",
                playlistId=uploads_playlist_id,
                maxResults=limit
            ).execute()
            
            if not playlist_items.get('items'):
                return []
                
            video_ids = [item['contentDetails']['videoId'] for item in playlist_items['items']]
            
            # 3. Get Video Stats
            videos_response = self.service.videos().list(
                part="statistics,snippet",
                id=','.join(video_ids)
            ).execute()
            
            videos = []
            for item in videos_response['items']:
                stats = item['statistics']
                videos.append({
                    "id": item['id'],
                    "title": item['snippet']['title'],
                    "published_at": item['snippet']['publishedAt'],
                    "views": int(stats.get('viewCount', 0)),
                    "likes": int(stats.get('likeCount', 0)),
                    "comments": int(stats.get('commentCount', 0))
                })
            
            return videos
            
        except Exception as e:
            print(f"Error fetching recent videos: {e}")
            return []

    def get_channel_stats(self):
        """Retorna estatísticas do canal conectado"""
        if not self.service:
            # Tentar descobrir POR QUE falhou
            reason = "Autenticação falhou"
            if not self.credentials:
                reason = "Credenciais não encontradas no banco"
            elif not self.credentials.valid:
                reason = "Credenciais inválidas ou expiradas"
                
            return {"connected": False, "error": reason}
        
        try:
            channel = self._get_my_channel(use_cache=True)
            if channel:
                stats = channel['statistics']
                snippet = channel['snippet']
                return {
                    "connected": True,
                    "title": snippet['title'],
                    "subscribers": stats['subscriberCount'],
                    "views": stats['viewCount'],
                    "videos": stats['videoCount'],
                    "thumbnail": snippet['thumbnails']['default']['url']
                }
            return {"connected": False, "error": "Nenhum canal encontrado"}
        except HttpError as e:
            reason = _parse_http_error_reason(e)
            if (e.resp.status == 403) and reason in {"quotaExceeded", "dailyLimitExceeded", "userRateLimitExceeded"}:
                return {
                    "connected": False,
                    "error": "Limite diário da API do YouTube excedido (quotaExceeded). Aguarde a renovação da quota ou solicite aumento no Google Cloud Console.",
                }
            if e.resp.status == 403 and "accessNotConfigured" in str(e):
                return {
                    "connected": False, 
                    "error": "A API 'YouTube Data API v3' não está ativada no Google Cloud. Ative-a no Console do Google Cloud."
                }
            return {"connected": False, "error": f"Erro do Google: {str(e)}"}
        except Exception as e:
            return {"connected": False, "error": f"Erro ao buscar canal: {str(e)}"}

    def set_thumbnail(self, youtube_video_id: str, thumbnail_path: str):
        """Define a thumbnail de um vídeo já enviado (youtube.thumbnails.set)."""
        if not self.service:
            return {"error": self.auth_error or "Canal não conectado ao YouTube.", "status": "not_connected"}
        vid = (youtube_video_id or "").strip()
        path = (thumbnail_path or "").strip()
        if not vid:
            return {"error": "youtube_video_id é obrigatório.", "status": "invalid"}
        if not path or not os.path.exists(path):
            return {"error": "Arquivo de thumbnail não encontrado.", "status": "file_not_found", "path": path}

        try:
            media = MediaFileUpload(path, mimetype="image/png", resumable=False)
            req = self.service.thumbnails().set(videoId=vid, media_body=media)
            resp = req.execute()
            return {"status": "ok", "response": resp}
        except Exception as e:
            return {"error": str(e), "status": "failed"}

    def upload_video(self, file_path, title, description, tags=None, category_id="27", thumbnail_path: Optional[str] = None):  # 27 = Education
        """Faz upload de um vídeo para o YouTube (opcional: seta thumbnail)."""
        if tags is None:
            tags = []
        if not self.service:
            reason = self.auth_error or "Canal não conectado ao YouTube."
            print(f"[YOUTUBE_DISCONNECTED] Upload não iniciado: {reason}")
            return {"error": reason, "status": "not_connected"}
            
        try:
            body = {
                'snippet': {
                    'title': title,
                    'description': description,
                    'tags': tags,
                    'categoryId': category_id
                },
                'status': {
                    'privacyStatus': 'unlisted',
                    'selfDeclaredMadeForKids': False,
                }
            }

            media = MediaFileUpload(file_path, chunksize=-1, resumable=True)

            request = self.service.videos().insert(
                part=','.join(body.keys()),
                body=body,
                media_body=media
            )

            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    print(f"Upload progresso: {int(status.progress() * 100)}%")

            if thumbnail_path and isinstance(response, dict):
                youtube_id = response.get("id")
                if youtube_id and isinstance(youtube_id, str):
                    thumb_res = self.set_thumbnail(youtube_id, thumbnail_path)
                    response["thumbnail_set"] = bool(isinstance(thumb_res, dict) and thumb_res.get("status") == "ok")
                    if isinstance(thumb_res, dict) and thumb_res.get("error"):
                        response["thumbnail_error"] = thumb_res.get("error")
            return response
        except Exception as e:
            print(f"Erro no upload para YouTube: {e}")
            return {"error": str(e)}

    def optimize_channel(self, ai_service):
        """Analisa e otimiza o canal usando IA"""
        stats = self.get_channel_stats()
        
        # Se stats tem descrição, usa. Senão, mock.
        current_description = stats.get('description', "Canal sobre livros e motivação.")
        
        analysis = ai_service.analyze_channel_strategy(stats, current_description)
        
        # Retorna a análise para o usuário confirmar a execução
        return analysis

    def upload_channel_banner(self, image_url):
        """Faz upload de uma imagem para banner do canal"""
        if not self.service:
            return None
            
        import requests
        from io import BytesIO
        from googleapiclient.http import MediaIoBaseUpload
        
        try:
            print(f"Baixando banner de {image_url}...")
            response = requests.get(image_url)
            if response.status_code != 200:
                print("Erro ao baixar imagem do banner")
                return None
            
            image_data = BytesIO(response.content)
            # Google exige mimetype image/png ou image/jpeg
            media = MediaIoBaseUpload(image_data, mimetype='image/png', resumable=True)
            
            print("Enviando banner para YouTube...")
            request = self.service.channelBanners().insert(
                body={},
                media_body=media
            )
            response = request.execute()
            print(f"Banner enviado. URL: {response.get('url')}")
            return response.get('url')
        except Exception as e:
            print(f"Erro ao fazer upload do banner: {e}")
            return None

    def update_channel_info(self, title=None, description=None, banner_external_url=None):
        """Atualiza título e descrição do canal"""
        if not self.service:
            return {"error": "Canal não conectado. Vá em Configurações > YouTube e conecte seu canal primeiro."}
        
        try:
            # 1. Get current channel info using helper
            item = self._get_my_channel()
            if not item:
                return {"error": "Channel not found"}
                
            channel_id = item['id']
            
            # Prepare update parts
            parts = []
            body = {"id": channel_id}
            
            # Update Branding Settings (Banner, and legacy title/desc)
            branding_settings = item.get('brandingSettings', {})
            if 'channel' not in branding_settings:
                branding_settings['channel'] = {}
            if 'image' not in branding_settings:
                branding_settings['image'] = {}
            
            branding_updated = False
            if title:
                branding_settings['channel']['title'] = title
                branding_updated = True
            if description:
                branding_settings['channel']['description'] = description
                branding_updated = True
            if banner_external_url:
                branding_settings['image']['bannerExternalUrl'] = banner_external_url
                branding_updated = True
                
            if branding_updated:
                parts.append("brandingSettings")
                body["brandingSettings"] = branding_settings
            
            # Update Snippet (Main Title/Description)
            snippet = item.get('snippet', {})
            snippet_updated = False
            if title:
                snippet['title'] = title
                snippet_updated = True
            if description:
                snippet['description'] = description
                snippet_updated = True
            
            if snippet_updated:
                parts.append("snippet")
                body["snippet"] = snippet
                
            if not parts:
                return {"message": "Nada a atualizar"}

            # Execute update
            update_request = self.service.channels().update(
                part=",".join(parts),
                body=body
            )
            update_response = update_request.execute()
            return update_response

        except Exception as e:
            print(f"Error updating channel: {e}")
            return {"error": str(e)}

    def get_recent_videos_performance(self, max_results: int = 20):
        """
        Retorna uma lista simplificada de vídeos recentes com métricas básicas
        (título, data de publicação, views, likes se disponíveis).
        Usa apenas escopos já configurados (readonly).
        """
        if not self.service:
            print("[YouTubeService] get_recent_videos_performance: serviço não conectado, retornando lista vazia.")
            return []

        try:
            channel = self._get_my_channel()
            if not channel:
                return []

            uploads_playlist_id = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
            if not uploads_playlist_id:
                # Fallback simples: usar search.list por canal
                search_req = self.service.search().list(
                    part="snippet",
                    channelId=channel["id"],
                    maxResults=max_results,
                    order="date",
                    type="video"
                )
                search_res = search_req.execute()
                items = search_res.get("items", [])
                videos = []
                for item in items:
                    snippet = item.get("snippet", {})
                    videos.append({
                        "videoId": item["id"]["videoId"],
                        "title": snippet.get("title"),
                        "publishedAt": snippet.get("publishedAt"),
                        "viewCount": None,
                        "likeCount": None
                    })
                return videos

            # Caso padrão: playlist "uploads"
            playlist_items_req = self.service.playlistItems().list(
                part="contentDetails,snippet",
                playlistId=uploads_playlist_id,
                maxResults=max_results
            )
            playlist_items_res = playlist_items_req.execute()
            items = playlist_items_res.get("items", [])

            video_ids = [it["contentDetails"]["videoId"] for it in items if it.get("contentDetails")]
            if not video_ids:
                return []

            videos_req = self.service.videos().list(
                part="snippet,statistics",
                id=",".join(video_ids)
            )
            videos_res = videos_req.execute()
            videos = []
            for item in videos_res.get("items", []):
                snippet = item.get("snippet", {})
                stats = item.get("statistics", {})
                videos.append({
                    "videoId": item.get("id"),
                    "title": snippet.get("title"),
                    "publishedAt": snippet.get("publishedAt"),
                    "viewCount": int(stats.get("viewCount", 0)) if stats.get("viewCount") is not None else 0,
                    "likeCount": int(stats.get("likeCount", 0)) if stats.get("likeCount") is not None else 0,
                    "commentCount": int(stats.get("commentCount", 0)) if stats.get("commentCount") is not None else 0
                })
            # Ordena por views desc
            videos.sort(key=lambda v: v.get("viewCount", 0), reverse=True)
            return videos
        except Exception as e:
            print(f"[YouTubeService] Erro ao buscar performance de vídeos: {e}")
            return []

    def get_subscriber_insights(self, days: int = 14, max_results: int = 20):
        if not self.credentials:
            return {
                "days": days,
                "subscriber_sources": [],
                "best_videos": [],
                "totals": {"subscribersGained": 0, "subscribersLost": 0},
                "error": self.auth_error or "Credenciais não disponíveis."
            }

        if not self.service:
            return {
                "days": days,
                "subscriber_sources": [],
                "best_videos": [],
                "totals": {"subscribersGained": 0, "subscribersLost": 0},
                "error": self.auth_error or "Serviço do YouTube não inicializado."
            }

        from datetime import datetime, timedelta
        end_date = datetime.utcnow().date()
        start_date = end_date - timedelta(days=max(1, int(days)))

        try:
            analytics = build("youtubeAnalytics", "v2", credentials=self.credentials)

            totals_resp = analytics.reports().query(
                ids="channel==MINE",
                startDate=start_date.isoformat(),
                endDate=end_date.isoformat(),
                metrics="subscribersGained,subscribersLost"
            ).execute()

            totals = {"subscribersGained": 0, "subscribersLost": 0}
            try:
                row = (totals_resp.get("rows") or [[0, 0]])[0]
                totals["subscribersGained"] = int(row[0] or 0)
                totals["subscribersLost"] = int(row[1] or 0)
            except Exception:
                pass

            sources_resp = analytics.reports().query(
                ids="channel==MINE",
                startDate=start_date.isoformat(),
                endDate=end_date.isoformat(),
                metrics="subscribersGained",
                dimensions="trafficSourceType",
                sort="-subscribersGained",
                maxResults=min(int(max_results), 25)
            ).execute()

            subscriber_sources = []
            for r in (sources_resp.get("rows") or []):
                try:
                    subscriber_sources.append({"source": r[0], "subscribersGained": int(r[1] or 0)})
                except Exception:
                    continue

            best_videos_resp = analytics.reports().query(
                ids="channel==MINE",
                startDate=start_date.isoformat(),
                endDate=end_date.isoformat(),
                metrics="subscribersGained,views,likes,comments",
                dimensions="video",
                sort="-subscribersGained",
                maxResults=min(int(max_results), 50)
            ).execute()

            rows = best_videos_resp.get("rows") or []
            video_ids = [r[0] for r in rows if r and r[0]]

            meta_by_id = {}
            if video_ids:
                try:
                    meta_resp = self.service.videos().list(
                        part="snippet,contentDetails",
                        id=",".join(video_ids[:50])
                    ).execute()
                    for it in (meta_resp.get("items") or []):
                        vid = it.get("id")
                        snippet = it.get("snippet") or {}
                        cd = it.get("contentDetails") or {}
                        meta_by_id[vid] = {
                            "title": snippet.get("title"),
                            "publishedAt": snippet.get("publishedAt"),
                            "duration": cd.get("duration"),
                        }
                except Exception:
                    meta_by_id = {}

            best_videos = []
            for r in rows:
                if not r or len(r) < 5:
                    continue
                vid = r[0]
                best_videos.append({
                    "videoId": vid,
                    "title": meta_by_id.get(vid, {}).get("title"),
                    "publishedAt": meta_by_id.get(vid, {}).get("publishedAt"),
                    "duration": meta_by_id.get(vid, {}).get("duration"),
                    "subscribersGained": int(r[1] or 0),
                    "views": int(r[2] or 0),
                    "likes": int(r[3] or 0),
                    "comments": int(r[4] or 0),
                })

            return {
                "days": days,
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "totals": totals,
                "subscriber_sources": subscriber_sources,
                "best_videos": best_videos,
            }
        except HttpError as e:
            return {
                "days": days,
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "subscriber_sources": [],
                "best_videos": [],
                "totals": {"subscribersGained": 0, "subscribersLost": 0},
                "error": str(e),
            }
        except Exception as e:
            return {
                "days": days,
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "subscriber_sources": [],
                "best_videos": [],
                "totals": {"subscribersGained": 0, "subscribersLost": 0},
                "error": str(e),
            }

    def get_monetization_progress(self):
        """
        Retorna um resumo simples de progresso rumo à monetização (estimado),
        usando apenas stats básicos disponíveis sem Analytics.
        """
        stats = self.get_channel_stats()
        try:
            subscribers = int(stats.get("subscribers", 0) or 0)
        except Exception:
            subscribers = 0
        try:
            total_views = int(stats.get("views", 0) or 0)
        except Exception:
            total_views = 0
        # Estimativa bem simplificada de horas de exibição:
        # assumindo ~3min de watch médio por view em vídeos longos.
        # horas ≈ (views * 3min) / 60
        estimated_watch_hours = (total_views * 3) / 60.0
        data = {
            "subscribers": subscribers,
            "subscribers_target": 1000,
            "total_views": total_views,
            "estimated_watch_hours": round(estimated_watch_hours, 1),
            "watch_hours_target": 4000,
            # valores percentuais para exibição
            "subscribers_progress_pct": min(100, round(subscribers / 10.0, 1)) if subscribers else 0,
            "watch_hours_progress_pct": min(100, round((estimated_watch_hours / 4000.0) * 100, 1)) if estimated_watch_hours else 0,
            "raw_stats": stats,
        }
        return data
