from fastapi import APIRouter, Request, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Sale, Customer, Book, Settings
from app.services.email_service import EmailService
from app.services.payment import PaymentService
from app.config import BASE_URL, VIDEO_OUTPUT_DIR, VIDEO_URL_PREFIX, absolute_path_for_video
from pydantic import BaseModel
import datetime
import json
import os
import re
import requests
import tempfile
from typing import Any, Dict, List, Optional, Tuple

router = APIRouter(prefix="/webhook", tags=["Webhook"])

class WebhookPayload(BaseModel):
    action: str
    data: dict

_WA_LAST_VIDEO_BY_NUMBER: Dict[str, Dict[str, str]] = {}
_WA_LAST_CHANNEL_BY_NUMBER: Dict[str, str] = {}
_WA_LAST_LIST_BY_NUMBER: Dict[str, List[Dict[str, str]]] = {}
_TG_LAST_VIDEO_BY_CHAT: Dict[str, Dict[str, str]] = {}
_TG_LAST_CHANNEL_BY_CHAT: Dict[str, str] = {}
_TG_LAST_LIST_BY_CHAT: Dict[str, List[Dict[str, str]]] = {}

def _wa_config(db: Session) -> Dict[str, Any]:
    settings = db.query(Settings).first()
    phone_number_id = (getattr(settings, "whatsapp_phone_number_id", None) if settings else None) or os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    access_token = (getattr(settings, "whatsapp_access_token", None) if settings else None) or os.getenv("WHATSAPP_ACCESS_TOKEN")
    verify_token = (getattr(settings, "whatsapp_verify_token", None) if settings else None) or os.getenv("WHATSAPP_VERIFY_TOKEN")
    allowed_numbers_raw = (getattr(settings, "whatsapp_allowed_numbers", None) if settings else None) or os.getenv("WHATSAPP_ALLOWED_NUMBERS")
    allowed_numbers: Optional[List[str]] = None
    if allowed_numbers_raw and str(allowed_numbers_raw).strip():
        allowed_numbers = [re.sub(r"\D+", "", part) for part in str(allowed_numbers_raw).split(",")]
        allowed_numbers = [n for n in allowed_numbers if n]
    return {
        "phone_number_id": str(phone_number_id).strip() if phone_number_id else "",
        "access_token": str(access_token).strip() if access_token else "",
        "verify_token": str(verify_token).strip() if verify_token else "",
        "allowed_numbers": allowed_numbers,
    }

def _wa_is_allowed(cfg: Dict[str, Any], from_number: str) -> bool:
    allowed = cfg.get("allowed_numbers")
    if not allowed:
        return True
    n = re.sub(r"\D+", "", from_number or "")
    return n in set(allowed)

def _wa_post(cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
    phone_number_id = (cfg.get("phone_number_id") or "").strip()
    access_token = (cfg.get("access_token") or "").strip()
    if not phone_number_id or not access_token:
        return
    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        requests.post(url, headers=headers, json=payload, timeout=20)
    except Exception:
        return

def _wa_send_text(cfg: Dict[str, Any], to_number: str, text: str) -> None:
    _wa_post(cfg, {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"preview_url": True, "body": text},
    })

def _wa_send_menu(cfg: Dict[str, Any], to_number: str) -> None:
    _wa_post(cfg, {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "Codexia"},
            "body": {"text": "Escolha uma opção:"},
            "action": {
                "button": "Menu",
                "sections": [
                    {
                        "title": "Vídeos",
                        "rows": [
                            {"id": "menu_generate_video", "title": "Gerar vídeo", "description": "Cria um novo vídeo por comando"},
                            {"id": "menu_list_videos", "title": "Últimos vídeos", "description": "Lista vídeos recentes para avaliação"},
                            {"id": "menu_publish_last", "title": "Publicar último", "description": "Publica o último vídeo gerado"},
                        ],
                    },
                    {
                        "title": "Ajuda",
                        "rows": [
                            {"id": "menu_help", "title": "Como usar", "description": "Exemplos de comandos"},
                        ],
                    },
                ],
            },
        },
    })

def _tg_config(db: Session) -> Dict[str, Any]:
    settings = db.query(Settings).first()
    bot_token = (getattr(settings, "telegram_bot_token", None) if settings else None) or os.getenv("TELEGRAM_BOT_TOKEN")
    allowed_chat_ids_raw = (getattr(settings, "telegram_allowed_chat_ids", None) if settings else None) or os.getenv("TELEGRAM_ALLOWED_CHAT_IDS")
    allowed_chat_ids: Optional[List[str]] = None
    if allowed_chat_ids_raw and str(allowed_chat_ids_raw).strip():
        allowed_chat_ids = [str(part).strip() for part in str(allowed_chat_ids_raw).split(",")]
        allowed_chat_ids = [c for c in allowed_chat_ids if c]
    return {
        "bot_token": str(bot_token).strip() if bot_token else "",
        "allowed_chat_ids": allowed_chat_ids,
    }

def _tg_is_allowed(cfg: Dict[str, Any], chat_id: str) -> bool:
    allowed = cfg.get("allowed_chat_ids")
    if not allowed:
        return True
    return str(chat_id) in set(allowed)

def _tg_api_post(cfg: Dict[str, Any], method: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    token = (cfg.get("bot_token") or "").strip()
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None

def _tg_send_text(cfg: Dict[str, Any], chat_id: str, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": False}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    _tg_api_post(cfg, "sendMessage", payload)

def _tg_send_menu(cfg: Dict[str, Any], chat_id: str) -> None:
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "Gerar vídeo", "callback_data": "tg_generate"},
                {"text": "Últimos vídeos", "callback_data": "tg_list"},
            ],
            [
                {"text": "Publicar último", "callback_data": "tg_publish_last"},
                {"text": "Ajuda", "callback_data": "tg_help"},
            ],
        ]
    }
    _tg_send_text(cfg, chat_id, "Codexia — escolha uma opção:", reply_markup=reply_markup)

def _tg_get_file_url(cfg: Dict[str, Any], file_id: str) -> Optional[str]:
    token = (cfg.get("bot_token") or "").strip()
    if not token or not file_id:
        return None
    res = _tg_api_post(cfg, "getFile", {"file_id": file_id})
    file_path = (((res or {}).get("result") or {}) if isinstance(res, dict) else {}).get("file_path")
    if not file_path:
        return None
    return f"https://api.telegram.org/file/bot{token}/{file_path}"

def _tg_download_file(cfg: Dict[str, Any], file_id: str) -> Optional[str]:
    url = _tg_get_file_url(cfg, file_id)
    if not url:
        return None
    try:
        r = requests.get(url, timeout=120)
        if r.status_code != 200 or not r.content:
            return None
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
            tmp.write(r.content)
            return tmp.name
    except Exception:
        return None

def _edenai_key(db: Session) -> str:
    settings = db.query(Settings).first()
    key = (getattr(settings, "edenai_api_key", None) if settings else None) or os.getenv("EDENAI_API_KEY")
    return str(key).strip() if key else ""

def _wa_download_media(cfg: Dict[str, Any], media_id: str) -> Optional[Tuple[str, str]]:
    access_token = (cfg.get("access_token") or "").strip()
    if not access_token or not media_id:
        return None
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        meta = requests.get(f"https://graph.facebook.com/v20.0/{media_id}", headers=headers, timeout=20).json()
        url = (meta or {}).get("url")
        mime = (meta or {}).get("mime_type") or ""
        if not url:
            return None
        r = requests.get(url, headers=headers, timeout=60)
        if r.status_code != 200 or not r.content:
            return None
        suffix = ".ogg"
        if "mpeg" in mime or "mp3" in mime:
            suffix = ".mp3"
        elif "wav" in mime:
            suffix = ".wav"
        elif "mp4" in mime:
            suffix = ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(r.content)
            return tmp.name, mime
    except Exception:
        return None

def _edenai_speech_to_text(edenai_key: str, file_path: str) -> Optional[str]:
    key = (edenai_key or "").strip()
    if not key or not file_path or not os.path.isfile(file_path):
        return None
    headers = {"Authorization": f"Bearer {key}"}
    try:
        with open(file_path, "rb") as f:
            files = {"file": f}
            data = {"providers": "openai,google", "language": "pt-BR"}
            r = requests.post("https://api.edenai.run/v2/audio/speech_to_text", headers=headers, data=data, files=files, timeout=120)
        if r.status_code != 200:
            return None
        payload = r.json() or {}
        for provider_key in ["openai", "google"]:
            block = payload.get(provider_key) or {}
            txt = block.get("text") or block.get("transcription") or block.get("transcript")
            if txt and isinstance(txt, str):
                return txt.strip()
        txt = payload.get("text") or payload.get("transcription")
        if txt and isinstance(txt, str):
            return txt.strip()
        return None
    except Exception:
        return None

def _tg_handle_list(cfg: Dict[str, Any], chat_id: str) -> None:
    videos = _list_recent_videos(limit=5)
    _TG_LAST_LIST_BY_CHAT[str(chat_id)] = videos
    if not videos:
        _tg_send_text(cfg, chat_id, "Nenhum vídeo encontrado ainda.")
        return
    lines = ["Últimos vídeos:"]
    for i, it in enumerate(videos, start=1):
        lines.append(f"{i}) {_safe_abs_url(it.get('url') or '')}")
    lines.append("")
    lines.append("Para publicar: \"publicar último vídeo\" ou \"publicar <nome>.mp4\"")
    _tg_send_text(cfg, chat_id, "\n".join(lines))

def _tg_handle_help(cfg: Dict[str, Any], chat_id: str) -> None:
    _tg_send_text(cfg, chat_id, "\n".join([
        "Exemplos:",
        "- menu",
        "- gerar um vídeo motivacional de 10 minutos para o YouTube",
        "- últimos vídeos",
        "- publicar último vídeo",
        "- publicar 123abc.mp4",
        "Você também pode mandar um áudio com o comando.",
    ]))


def _submit_canonical_chat_video(*, channel: str, recipient_id: str, theme: str, minutes: int, voice_style: str, voice_gender: str):
    """Entrega comandos de mensageria ao pipeline História/Devocional."""
    from app.database import SessionLocal
    from app.services.unified_video_pipeline import build_unified_video_request, unified_video_pipeline

    payload = {
        "topic": theme,
        "duration": minutes,
        "mode": "topic",
        "kind": "custom",
        "aspect_ratio": "16:9",
        "voice_style": voice_style,
        "voice_gender": voice_gender,
        "review_required": True,
        "channel": channel,
        "recipient_id": recipient_id,
    }
    db = SessionLocal()
    try:
        request = build_unified_video_request(
            payload,
            source_module=f"{channel}_webhook",
            source_id=f"{channel}:{recipient_id}:{theme}:{minutes}",
        )
        try:
            from app.routers.youtube import _kick_story_video_task_queue_async

            kick = _kick_story_video_task_queue_async if callable(_kick_story_video_task_queue_async) else None
        except Exception:
            kick = None
        return unified_video_pipeline().submit_or_reuse(
            db,
            request=request,
            kick_queue_callback=kick,
            legacy_initial_result={
                "source_module": f"{channel}_webhook",
                "pipeline": "unified_video_pipeline",
                "payload": payload,
            },
        )
    finally:
        db.close()


def _refresh_chat_video_from_task(last: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve o artefato validado quando o usuário pede publicação."""
    if str(last.get("video_url") or "").strip():
        return last
    task_id = str(last.get("task_id") or "").strip()
    if not task_id:
        return last
    from app.services.task_manager import get_task

    task = get_task(task_id) or {}
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    video_url = str(
        result.get("video_url")
        or result.get("video_path")
        or result.get("file_path")
        or ""
    ).strip()
    if video_url:
        last["video_url"] = video_url
        last["abs_url"] = _safe_abs_url(video_url)
    return last

def _tg_handle_generate_video(cfg: Dict[str, Any], chat_id: str, cmd: Dict[str, Any]) -> None:
    theme = (cmd.get("theme") or "").strip() or "motivacional"
    minutes = cmd.get("minutes")
    try:
        minutes_val = int(minutes) if minutes is not None else 10
    except Exception:
        minutes_val = 10
    voice_style = (cmd.get("voice_style") or "").strip() or "human"
    voice_gender = (cmd.get("voice_gender") or "").strip() or "female"

    result = _submit_canonical_chat_video(
        channel="telegram",
        recipient_id=str(chat_id),
        theme=theme,
        minutes=minutes_val,
        voice_style=voice_style,
        voice_gender=voice_gender,
    )
    _TG_LAST_VIDEO_BY_CHAT[str(chat_id)] = {
        "video_url": result.video_url or "",
        "abs_url": _safe_abs_url(result.video_url or "") if result.video_url else "",
        "task_id": result.task_id,
    }
    _tg_send_text(
        cfg,
        chat_id,
        f"Vídeo enviado ao pipeline único. Tarefa: {result.task_id}. "
        "Quando a validação terminar, ele ficará disponível para avaliação.",
    )

def _tg_handle_publish(cfg: Dict[str, Any], chat_id: str, cmd: Dict[str, Any]) -> None:
    from app.services.youtube_service import YouTubeService
    from app.services.ai_generator import AIContentGenerator

    channel = (cmd.get("channel") or "").strip()
    if channel:
        _TG_LAST_CHANNEL_BY_CHAT[str(chat_id)] = channel
    channel_display = _TG_LAST_CHANNEL_BY_CHAT.get(str(chat_id)) or channel or "canal conectado"

    filename = (cmd.get("filename") or "").strip()
    video_url = ""
    abs_url = ""
    if filename:
        video_url = f"{VIDEO_URL_PREFIX}/{filename}"
        abs_url = _safe_abs_url(video_url)
    else:
        last = _refresh_chat_video_from_task(_TG_LAST_VIDEO_BY_CHAT.get(str(chat_id)) or {})
        video_url = last.get("video_url") or ""
        abs_url = last.get("abs_url") or ""

    if not video_url:
        _tg_send_text(cfg, chat_id, "Não encontrei um vídeo recente para publicar. Use \"últimos vídeos\" ou gere um vídeo primeiro.")
        return

    path = absolute_path_for_video(video_url)
    if not path or not os.path.isfile(path):
        _tg_send_text(cfg, chat_id, f"Arquivo do vídeo não encontrado no servidor:\n{abs_url}")
        return

    ai = AIContentGenerator()
    title_prompt = f"Crie um título curto e forte para um vídeo do tema: {os.path.basename(path)}"
    title = (ai._generate_text(title_prompt) or "").strip() or "Vídeo Codexia"
    description = "Vídeo gerado automaticamente por Codexia."

    yt = YouTubeService()
    res = yt.upload_video(path, title=title, description=description, tags=[])
    if isinstance(res, dict) and res.get("error"):
        _tg_send_text(cfg, chat_id, f"Falha ao publicar no YouTube ({channel_display}): {res.get('error')}")
        return
    video_id = (res.get("id") if isinstance(res, dict) else None) or (res.get("videoId") if isinstance(res, dict) else None)
    if video_id:
        _tg_send_text(cfg, chat_id, f"Publicado no YouTube ({channel_display}).\nhttps://www.youtube.com/watch?v={video_id}")
        return
    _tg_send_text(cfg, chat_id, f"Upload finalizado no YouTube ({channel_display}). Confira no seu canal.\n{abs_url}")

def _wa_parse_incoming(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            messages = value.get("messages") or []
            for msg in messages:
                out.append({
                    "from": msg.get("from"),
                    "type": msg.get("type"),
                    "text": (msg.get("text") or {}).get("body"),
                    "interactive": msg.get("interactive"),
                    "audio": msg.get("audio"),
                })
    return out

def _wa_pick_action_id(event: Dict[str, Any]) -> Optional[str]:
    it = event.get("interactive") or {}
    lr = it.get("list_reply") or {}
    br = it.get("button_reply") or {}
    return lr.get("id") or br.get("id")

def _list_recent_videos(limit: int = 5) -> List[Dict[str, str]]:
    base_dir = str(VIDEO_OUTPUT_DIR)
    url_prefix = str(VIDEO_URL_PREFIX)
    if not base_dir or not os.path.isdir(base_dir):
        return []
    items: List[Tuple[float, str]] = []
    for name in os.listdir(base_dir):
        if not name.lower().endswith(".mp4"):
            continue
        full = os.path.join(base_dir, name)
        try:
            items.append((os.path.getctime(full), name))
        except Exception:
            continue
    items.sort(key=lambda x: x[0], reverse=True)
    result: List[Dict[str, str]] = []
    for _, name in items[: max(1, limit)]:
        result.append({"filename": name, "url": f"{url_prefix}/{name}"})
    return result

def _safe_abs_url(path: str) -> str:
    p = (path or "").strip()
    if not p:
        return ""
    if p.startswith("http://") or p.startswith("https://"):
        return p
    if p.startswith("/"):
        return f"{BASE_URL}{p}"
    return f"{BASE_URL}/{p}"

def _parse_command_with_ai(text: str) -> Dict[str, Any]:
    from app.services.ai_generator import AIContentGenerator
    ai = AIContentGenerator()
    system_prompt = (
        "Você é um parser de comandos para um bot do WhatsApp chamado Codexia.\n"
        "Extraia a intenção e retorne APENAS um JSON válido.\n"
        "Esquema:\n"
        "{\n"
        "  \"action\": \"menu\"|\"help\"|\"list_videos\"|\"generate_video\"|\"publish_last\"|\"publish_file\"|\"unknown\",\n"
        "  \"theme\": string|null,\n"
        "  \"minutes\": number|null,\n"
        "  \"publish\": boolean|null,\n"
        "  \"channel\": string|null,\n"
        "  \"voice_style\": string|null,\n"
        "  \"voice_gender\": string|null,\n"
        "  \"filename\": string|null\n"
        "}\n"
        "Regras:\n"
        "- Se pedir menu: action=menu.\n"
        "- Se pedir listar vídeos: action=list_videos.\n"
        "- Se pedir gerar vídeo: action=generate_video; theme; minutes; publish; channel.\n"
        "- Se pedir publicar o último: action=publish_last.\n"
        "- Se pedir publicar um arquivo específico: action=publish_file e filename.\n"
        "- Se não entender: action=unknown.\n"
        "Idioma: pt-BR."
    )
    raw = ai._generate_text(text, system_prompt=system_prompt, json_mode=True)
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _parse_command_fallback(text: str) -> Dict[str, Any]:
    t = (text or "").strip().lower()
    if not t:
        return {"action": "unknown"}
    if "menu" in t:
        return {"action": "menu"}
    if "ajuda" in t or "como" in t:
        return {"action": "help"}
    if "último" in t and "public" in t:
        return {"action": "publish_last"}
    if "listar" in t or "ultimos" in t or "últimos" in t:
        return {"action": "list_videos"}
    if "public" in t:
        m = re.search(r"([\\w\\-]+\\.mp4)", t)
        if m:
            return {"action": "publish_file", "filename": m.group(1)}
        return {"action": "publish_last"}
    if "gerar" in t and ("vídeo" in t or "video" in t):
        m = re.search(r"(\\d{1,2})\\s*(min|mins|minutos)", t)
        minutes = int(m.group(1)) if m else None
        return {"action": "generate_video", "theme": None, "minutes": minutes, "publish": ("youtube" in t), "channel": None}
    return {"action": "unknown"}

def _normalize_command_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    m = re.match(r"^\\s*codexia\\s*[-,:]*\\s*(.*)$", raw, flags=re.IGNORECASE)
    return (m.group(1) if m else raw).strip()

def _handle_generate_video(cfg: Dict[str, Any], from_number: str, cmd: Dict[str, Any]) -> None:
    theme = (cmd.get("theme") or "").strip() or "motivacional"
    minutes = cmd.get("minutes")
    try:
        minutes_val = int(minutes) if minutes is not None else 10
    except Exception:
        minutes_val = 10
    voice_style = (cmd.get("voice_style") or "").strip() or "human"
    voice_gender = (cmd.get("voice_gender") or "").strip() or "female"

    result = _submit_canonical_chat_video(
        channel="whatsapp",
        recipient_id=str(from_number),
        theme=theme,
        minutes=minutes_val,
        voice_style=voice_style,
        voice_gender=voice_gender,
    )
    _WA_LAST_VIDEO_BY_NUMBER[from_number] = {
        "video_url": result.video_url or "",
        "abs_url": _safe_abs_url(result.video_url or "") if result.video_url else "",
        "task_id": result.task_id,
    }
    _wa_send_text(
        cfg,
        from_number,
        f"Vídeo enviado ao pipeline único. Tarefa: {result.task_id}. "
        "Quando a validação terminar, ele ficará disponível para avaliação.",
    )

def _handle_publish(cfg: Dict[str, Any], from_number: str, cmd: Dict[str, Any]) -> None:
    from app.services.youtube_service import YouTubeService
    from app.services.ai_generator import AIContentGenerator

    channel = (cmd.get("channel") or "").strip()
    if channel:
        _WA_LAST_CHANNEL_BY_NUMBER[from_number] = channel
    channel_display = _WA_LAST_CHANNEL_BY_NUMBER.get(from_number) or channel or "canal conectado"

    filename = (cmd.get("filename") or "").strip()
    video_url = ""
    abs_url = ""
    if filename:
        video_url = f"{VIDEO_URL_PREFIX}/{filename}"
        abs_url = _safe_abs_url(video_url)
    else:
        last = _refresh_chat_video_from_task(_WA_LAST_VIDEO_BY_NUMBER.get(from_number) or {})
        video_url = last.get("video_url") or ""
        abs_url = last.get("abs_url") or ""

    if not video_url:
        _wa_send_text(cfg, from_number, "Não encontrei um vídeo recente para publicar. Use: \"codexia, últimos vídeos\" ou gere um vídeo primeiro.")
        return

    path = absolute_path_for_video(video_url)
    if not path or not os.path.isfile(path):
        _wa_send_text(cfg, from_number, f"Arquivo do vídeo não encontrado no servidor:\n{abs_url}")
        return

    ai = AIContentGenerator()
    title_prompt = f"Crie um título curto e forte para um vídeo do tema: {os.path.basename(path)}"
    title = (ai._generate_text(title_prompt) or "").strip() or "Vídeo Codexia"
    description = "Vídeo gerado automaticamente por Codexia."

    yt = YouTubeService()
    res = yt.upload_video(path, title=title, description=description, tags=[])
    if isinstance(res, dict) and res.get("error"):
        _wa_send_text(cfg, from_number, f"Falha ao publicar no YouTube ({channel_display}): {res.get('error')}")
        return
    video_id = (res.get("id") if isinstance(res, dict) else None) or (res.get("videoId") if isinstance(res, dict) else None)
    if video_id:
        _wa_send_text(cfg, from_number, f"Publicado no YouTube ({channel_display}).\nhttps://www.youtube.com/watch?v={video_id}")
        return
    _wa_send_text(cfg, from_number, f"Upload finalizado no YouTube ({channel_display}). Confira no seu canal.\n{abs_url}")

def _handle_list(cfg: Dict[str, Any], from_number: str) -> None:
    videos = _list_recent_videos(limit=5)
    _WA_LAST_LIST_BY_NUMBER[from_number] = videos
    if not videos:
        _wa_send_text(cfg, from_number, "Nenhum vídeo encontrado ainda.")
        return
    lines = ["Últimos vídeos:"]
    for i, it in enumerate(videos, start=1):
        lines.append(f"{i}) {_safe_abs_url(it.get('url') or '')}")
    lines.append("")
    lines.append("Para publicar o último: \"codexia, publicar último vídeo\"")
    lines.append("Para publicar um arquivo: \"codexia, publicar <nome>.mp4\"")
    _wa_send_text(cfg, from_number, "\n".join(lines))

def _handle_help(cfg: Dict[str, Any], from_number: str) -> None:
    _wa_send_text(cfg, from_number, "\n".join([
        "Exemplos:",
        "- codexia, me mostre o menu",
        "- codexia, gerar um vídeo motivacional de 10 minutos para o YouTube",
        "- codexia, últimos vídeos",
        "- codexia, publicar último vídeo",
        "- codexia, publicar 123abc.mp4",
    ]))

@router.get("/whatsapp")
def whatsapp_verify(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
    db: Session = Depends(get_db),
):
    cfg = _wa_config(db)
    expected = (cfg.get("verify_token") or "").strip()
    if hub_mode == "subscribe" and hub_verify_token and expected and hub_verify_token == expected:
        return int(hub_challenge or "0")
    raise HTTPException(status_code=403, detail="Invalid verify token")

@router.post("/whatsapp")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    cfg = _wa_config(db)
    payload = await request.json()
    events = _wa_parse_incoming(payload or {})
    for event in events:
        from_number = (event.get("from") or "").strip()
        if not from_number:
            continue
        if not _wa_is_allowed(cfg, from_number):
            _wa_send_text(cfg, from_number, "Acesso não autorizado para este número.")
            continue

        action_id = _wa_pick_action_id(event)
        if action_id == "menu_generate_video":
            _wa_send_text(cfg, from_number, "Envie: \"codexia, gerar um vídeo motivacional de 10 minutos para o YouTube\"")
            continue
        if action_id == "menu_list_videos":
            _handle_list(cfg, from_number)
            continue
        if action_id == "menu_publish_last":
            background_tasks.add_task(_handle_publish, cfg, from_number, {"action": "publish_last"})
            _wa_send_text(cfg, from_number, "Publicando o último vídeo...")
            continue
        if action_id == "menu_help":
            _handle_help(cfg, from_number)
            continue

        text = _normalize_command_text(event.get("text") or "")
        if not text and (event.get("type") == "audio" or event.get("audio")):
            audio = event.get("audio") or {}
            media_id = (audio.get("id") or "").strip()
            downloaded = _wa_download_media(cfg, media_id) if media_id else None
            if downloaded:
                tmp_path, _ = downloaded
                try:
                    transcript = _edenai_speech_to_text(_edenai_key(db), tmp_path)
                finally:
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
                if transcript:
                    text = _normalize_command_text(transcript)
        if not text:
            continue

        cmd = _parse_command_with_ai(text)
        if not cmd or not cmd.get("action") or cmd.get("action") == "unknown":
            cmd = _parse_command_fallback(text)

        action = (cmd.get("action") or "").strip()
        if action == "menu":
            _wa_send_menu(cfg, from_number)
        elif action == "help":
            _handle_help(cfg, from_number)
        elif action == "list_videos":
            _handle_list(cfg, from_number)
        elif action == "generate_video":
            _wa_send_text(cfg, from_number, "Gerando seu vídeo... vou te avisar quando terminar.")
            background_tasks.add_task(_handle_generate_video, cfg, from_number, cmd)
        elif action in {"publish_last", "publish_file"}:
            _wa_send_text(cfg, from_number, "Publicando no YouTube...")
            background_tasks.add_task(_handle_publish, cfg, from_number, cmd)
        else:
            _wa_send_text(cfg, from_number, "Não entendi. Envie: \"codexia, me mostre o menu\"")

    return {"status": "ok"}

@router.post("/telegram")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    cfg = _tg_config(db)
    payload = await request.json()

    callback_query = (payload or {}).get("callback_query")
    if callback_query:
        callback_id = callback_query.get("id")
        data = (callback_query.get("data") or "").strip()
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "").strip()
        if callback_id:
            _tg_api_post(cfg, "answerCallbackQuery", {"callback_query_id": callback_id})
        if not chat_id:
            return {"ok": True}
        if not _tg_is_allowed(cfg, chat_id):
            _tg_send_text(cfg, chat_id, "Acesso não autorizado para este chat.")
            return {"ok": True}
        if data == "tg_generate":
            _tg_send_text(cfg, chat_id, "Envie: \"gerar um vídeo motivacional de 10 minutos para o YouTube\"")
        elif data == "tg_list":
            _tg_handle_list(cfg, chat_id)
        elif data == "tg_publish_last":
            _tg_send_text(cfg, chat_id, "Publicando no YouTube...")
            background_tasks.add_task(_tg_handle_publish, cfg, chat_id, {"action": "publish_last"})
        elif data == "tg_help":
            _tg_handle_help(cfg, chat_id)
        else:
            _tg_send_menu(cfg, chat_id)
        return {"ok": True}

    message = (payload or {}).get("message") or (payload or {}).get("edited_message") or {}
    if not message:
        return {"ok": True}

    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "").strip()
    if not chat_id:
        return {"ok": True}
    if not _tg_is_allowed(cfg, chat_id):
        _tg_send_text(cfg, chat_id, "Acesso não autorizado para este chat.")
        return {"ok": True}

    text = _normalize_command_text((message.get("text") or "").strip())
    if not text and message.get("voice"):
        file_id = ((message.get("voice") or {}).get("file_id") or "").strip()
        tmp_path = _tg_download_file(cfg, file_id) if file_id else None
        if tmp_path:
            try:
                transcript = _edenai_speech_to_text(_edenai_key(db), tmp_path)
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            if transcript:
                text = _normalize_command_text(transcript)

    if not text:
        return {"ok": True}

    low = text.strip().lower()
    if low in {"/start", "/menu", "menu"}:
        _tg_send_menu(cfg, chat_id)
        return {"ok": True}

    cmd = _parse_command_with_ai(text)
    if not cmd or not cmd.get("action") or cmd.get("action") == "unknown":
        cmd = _parse_command_fallback(text)

    action = (cmd.get("action") or "").strip()
    if action == "menu":
        _tg_send_menu(cfg, chat_id)
    elif action == "help":
        _tg_handle_help(cfg, chat_id)
    elif action == "list_videos":
        _tg_handle_list(cfg, chat_id)
    elif action == "generate_video":
        _tg_send_text(cfg, chat_id, "Gerando seu vídeo... vou te avisar quando terminar.")
        background_tasks.add_task(_tg_handle_generate_video, cfg, chat_id, cmd)
    elif action in {"publish_last", "publish_file"}:
        _tg_send_text(cfg, chat_id, "Publicando no YouTube...")
        background_tasks.add_task(_tg_handle_publish, cfg, chat_id, cmd)
    else:
        _tg_send_text(cfg, chat_id, "Não entendi. Envie \"menu\" para ver as opções.")

    return {"ok": True}

@router.post("/mercadopago")
async def mercadopago_webhook(payload: dict, db: Session = Depends(get_db)):
    """
    Recebe notificações do Mercado Pago.
    Em produção, o MP envia um JSON com 'action' e 'data'.
    """
    print(f"[WEBHOOK] Payload recebido: {payload}")

    # Verifica se é uma notificação de pagamento
    if payload.get("type") == "payment":
        payment_id = payload.get("data", {}).get("id")
        
        # Aqui deveríamos consultar a API do Mercado Pago para pegar os detalhes reais
        # Como estamos simulando ou usando sandbox, vamos simular os dados do cliente
        # se não conseguirmos pegar da API.
        
        # Simulação de dados obtidos do pagamento (Mock)
        # Em produção: payment_info = payment_service.get_payment_info(payment_id)
        
        # MOCK DATA para fins de demonstração imediata
        customer_email = "cliente@exemplo.com"
        customer_name = "Cliente Codexia"
        book_id = 1 # Assumindo o primeiro livro por enquanto
        amount = 29.90
        status = "approved"

        # Verificar se o cliente já existe
        customer = db.query(Customer).filter(Customer.email == customer_email).first()
        if not customer:
            customer = Customer(name=customer_name, email=customer_email)
            db.add(customer)
            db.commit()
            db.refresh(customer)
        
        # Registrar a venda
        existing_sale = db.query(Sale).filter(Sale.payment_id == str(payment_id)).first()
        if not existing_sale:
            sale = Sale(
                customer_id=customer.id,
                book_id=book_id,
                amount=amount,
                status=status,
                payment_id=str(payment_id)
            )
            db.add(sale)
            db.commit()
            
            # Disparar Entrega
            book = db.query(Book).filter(Book.id == book_id).first()
            if book and status == "approved":
                # Gerar link de download (BASE_URL em produção)
                if book.file_path:
                    download_link = f"{BASE_URL}{book.file_path}"
                else:
                    download_link = f"{BASE_URL}/download/{book.id}"
                
                # Instancia serviço de email sob demanda
                email_service = EmailService()
                email_service.send_delivery_email(customer.email, customer.name, book.title, download_link)

    return {"status": "received"}

@router.post("/simulate-sale")
def simulate_sale(db: Session = Depends(get_db)):
    """
    Endpoint auxiliar para testar o fluxo sem precisar fazer uma compra real no MP.
    """
    # Criar cliente teste
    customer = db.query(Customer).filter(Customer.email == "teste@codexia.com").first()
    if not customer:
        customer = Customer(name="João Leitor", email="teste@codexia.com", phone="11999999999")
        db.add(customer)
        db.commit()
        db.refresh(customer)
    
    # Pegar um livro
    book = db.query(Book).first()
    if not book:
        return {"error": "Nenhum livro cadastrado para simular venda"}

    # Criar venda
    import uuid
    payment_id = str(uuid.uuid4())
    
    sale = Sale(
        customer_id=customer.id,
        book_id=book.id,
        amount=book.price,
        status="approved",
        payment_id=payment_id
    )
    db.add(sale)
    db.commit()

    # Enviar email
    if book.file_path:
        download_link = f"{BASE_URL}{book.file_path}"
    else:
        download_link = f"{BASE_URL}/download/{book.id}"
    email_service = EmailService()
    email_service.send_delivery_email(customer.email, customer.name, book.title, download_link)

    return {"status": "Venda simulada com sucesso", "customer": customer.name, "book": book.title}
