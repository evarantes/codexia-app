"""
Integração com Suno API para gerar música com voz cantada a partir de letra.
Documentação: https://docs.sunoapi.org/
"""
import os
import time
import uuid
import requests
from typing import Optional, List, Dict, Any
from app.database import SessionLocal
from app.models import Settings


SUNO_BASE = "https://api.sunoapi.org/api/v1"
# callBackUrl é obrigatório na API; pode ser placeholder se usar apenas polling
CALLBACK_PLACEHOLDER = "https://example.com/suno-callback"


def get_suno_api_key() -> Optional[str]:
    key = os.getenv("SUNO_API_KEY")
    if key and key.strip():
        return key.strip()
    db = SessionLocal()
    try:
        s = db.query(Settings).first()
        if s and s.suno_api_key and s.suno_api_key.strip():
            return s.suno_api_key.strip()
    finally:
        db.close()
    return None


def generate_song_with_vocals(
    lyrics: str,
    title: str = "Música",
    style: str = "Pop",
    model: str = "V4_5ALL",
    vocal_gender: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Gera música com voz cantada via Suno.
    Retorna {"success": True, "task_id": "...", "audio_url": "...", "local_path": "..."}
    ou {"success": False, "error": "..."}.
    Faz polling até concluir (timeout ~4 min).
    """
    api_key = get_suno_api_key()
    if not api_key:
        return {"success": False, "error": "Chave Suno API não configurada. Configure em Configurações (Suno API Key) ou variável SUNO_API_KEY."}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # Custom mode: prompt = letra (cantada), style, title
    body = {
        "customMode": True,
        "instrumental": False,
        "model": model,
        "prompt": lyrics[:5000],
        "style": (style or "Pop")[:1000],
        "title": (title or "Música")[:100],
        "callBackUrl": CALLBACK_PLACEHOLDER,
    }
    if vocal_gender in ("m", "f"):
        body["vocalGender"] = vocal_gender

    try:
        r = requests.post(f"{SUNO_BASE}/generate", headers=headers, json=body, timeout=30)
        data = r.json()
        if r.status_code != 200 or data.get("code") != 200:
            return {"success": False, "error": data.get("msg", r.text) or f"HTTP {r.status_code}"}
        task_id = data.get("data", {}).get("taskId")
        if not task_id:
            return {"success": False, "error": "Suno não retornou taskId."}
    except Exception as e:
        return {"success": False, "error": str(e)}

    # Poll até SUCCESS ou FAILED (máx ~4 min)
    max_wait = 260
    step = 8
    elapsed = 0
    while elapsed < max_wait:
        time.sleep(step)
        elapsed += step
        try:
            status_r = requests.get(
                f"{SUNO_BASE}/generate/record-info",
                params={"taskId": task_id},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
            )
            status_data = status_r.json()
            if status_r.status_code != 200:
                continue
            rec = status_data.get("data", {})
            status = rec.get("status")
            if status == "FAILED":
                return {"success": False, "error": rec.get("errorMessage", "Geração falhou no Suno.")}
            if status == "SUCCESS":
                resp = rec.get("response", {})
                tracks = resp.get("data") or []
                if not tracks:
                    return {"success": False, "error": "Suno não retornou áudio."}
                first = tracks[0]
                audio_url = first.get("audio_url") or first.get("audioUrl") or first.get("url")
                if not audio_url:
                    return {"success": False, "error": "URL do áudio não encontrada na resposta."}
                # Baixar e salvar localmente
                music_dir = "app/static/music"
                os.makedirs(music_dir, exist_ok=True)
                filename = f"song_{uuid.uuid4().hex[:10]}.mp3"
                path = os.path.join(music_dir, filename)
                dl = requests.get(audio_url, timeout=60)
                if dl.status_code != 200:
                    return {"success": False, "error": "Falha ao baixar áudio do Suno."}
                with open(path, "wb") as f:
                    f.write(dl.content)
                return {
                    "success": True,
                    "task_id": task_id,
                    "audio_url": audio_url,
                    "music_url": f"/static/music/{filename}",
                    "music_filename": filename,
                }
        except Exception as e:
            if elapsed >= max_wait - step:
                return {"success": False, "error": f"Timeout ou erro ao verificar status: {e}"}
            continue

    return {"success": False, "error": "Timeout aguardando a geração da música no Suno."}
