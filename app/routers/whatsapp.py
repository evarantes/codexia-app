import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import absolute_path_for_video
from app.database import get_db
from app.models import ScheduledVideo, User
from app.routers.auth import get_current_user


router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


def _bridge_url() -> str:
    return (os.getenv("WHATSAPP_BRIDGE_URL") or "http://localhost:3030").strip().rstrip("/")


def _bridge_request(method: str, path: str, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = _bridge_url()
    if not base:
        raise HTTPException(status_code=503, detail="WHATSAPP_BRIDGE_URL não configurado.")
    url = f"{base}{path}"
    try:
        r = requests.request(method.upper(), url, json=json_body, timeout=20)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Falha ao conectar no WhatsApp Bridge: {str(e)}")
    data = None
    try:
        data = r.json()
    except Exception:
        data = {}
    if not r.ok:
        msg = (data.get("error") if isinstance(data, dict) else None) or f"Erro do WhatsApp Bridge ({r.status_code})."
        raise HTTPException(status_code=502, detail=msg)
    return data if isinstance(data, dict) else {}


class WhatsAppSendNowRequest(BaseModel):
    to: str
    message: str
    video_url: Optional[str] = None


class WhatsAppRecipient(BaseModel):
    to: str
    name: Optional[str] = None
    message: Optional[str] = None


class WhatsAppScheduleRequest(BaseModel):
    scheduled_for: str
    title: Optional[str] = None
    message: str
    video_url: Optional[str] = None
    recipients: List[WhatsAppRecipient]


@router.get("/status")
def whatsapp_status(user: User = Depends(get_current_user)):
    _ = user
    return _bridge_request("GET", "/status")


@router.get("/qr")
def whatsapp_qr(user: User = Depends(get_current_user)):
    _ = user
    return _bridge_request("GET", "/qr")


@router.post("/send-now")
def whatsapp_send_now(request: WhatsAppSendNowRequest, user: User = Depends(get_current_user)):
    _ = user
    media_path = None
    if request.video_url:
        abs_path = absolute_path_for_video(request.video_url)
        if abs_path and os.path.exists(abs_path):
            media_path = abs_path
        else:
            raise HTTPException(status_code=400, detail="Arquivo do vídeo não encontrado no servidor.")
    payload = {"to": request.to, "message": request.message, "media_path": media_path}
    return _bridge_request("POST", "/send", json_body=payload)


@router.post("/schedule-send")
def whatsapp_schedule_send(request: WhatsAppScheduleRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    raw = (request.scheduled_for or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="scheduled_for é obrigatório.")
    try:
        raw = raw.replace("Z", "+00:00")
    except Exception:
        pass
    scheduled_for = None
    try:
        scheduled_for = datetime.fromisoformat(raw)
    except Exception:
        try:
            scheduled_for = datetime.strptime(raw, "%Y-%m-%d %H:%M")
        except Exception:
            scheduled_for = None
    if not scheduled_for:
        raise HTTPException(status_code=400, detail="scheduled_for inválido.")

    recs = []
    for r in request.recipients or []:
        to = (r.to or "").strip()
        if not to:
            continue
        recs.append({"to": to, "name": (r.name or "").strip() or None, "message": (r.message or "").strip() or None})
    if not recs:
        raise HTTPException(status_code=400, detail="Informe ao menos 1 destinatário.")

    sv = ScheduledVideo(
        user_id=getattr(user, "id", None),
        theme="WhatsApp",
        title=(request.title or "Envio WhatsApp")[:200],
        description=(request.message or "")[:4000],
        scheduled_for=scheduled_for,
        status="completed",
        video_type="video",
        parent_video_id=None,
        script_data=json.dumps(
            {"platform": "whatsapp", "message": request.message, "recipients": recs},
            ensure_ascii=False,
        ),
        video_url=(request.video_url or None),
        progress=100,
        auto_post=True,
    )
    db.add(sv)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Falha ao salvar agendamento do WhatsApp.")
    try:
        db.refresh(sv)
    except Exception:
        pass

    return {"status": "scheduled", "scheduled_video_id": getattr(sv, "id", None), "scheduled_for": scheduled_for.isoformat()}

