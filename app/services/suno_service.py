"""
Integração com Suno API para gerar música com voz cantada a partir de letra.
Documentação: https://docs.sunoapi.org/
"""
import os
import time
import uuid
import requests
from typing import Optional, List, Dict, Any, Tuple
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


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}

def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []

def _find_first_audio_url(obj: Any) -> Optional[str]:
    stack = [obj]
    seen: set = set()
    while stack:
        cur = stack.pop()
        try:
            cur_id = id(cur)
        except Exception:
            cur_id = None
        if cur_id is not None:
            if cur_id in seen:
                continue
            seen.add(cur_id)

        if isinstance(cur, dict):
            for k in ("audio_url", "audioUrl", "audio", "url", "audioURL", "audio_url_mp3", "audio_mp3_url"):
                v = cur.get(k)
                if isinstance(v, str) and v.strip().startswith(("http://", "https://")):
                    return v.strip()
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            for v in cur:
                if isinstance(v, (dict, list)):
                    stack.append(v)
    return None

def _parse_status_payload(status_data: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    data = _as_dict(status_data.get("data"))
    status = str((data.get("status") or data.get("state") or "")).strip()
    return status.upper(), data

def create_suno_task(
    api_key: str,
    lyrics: str,
    title: str = "Música",
    style: str = "Pop",
    model: str = "V4_5ALL",
    vocal_gender: Optional[str] = None,
) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "customMode": True,
        "instrumental": False,
        "model": model,
        "prompt": (lyrics or "")[:5000],
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
        task_id = _as_dict(data.get("data")).get("taskId")
        if not task_id:
            return {"success": False, "error": "Suno não retornou taskId."}
        return {"success": True, "task_id": str(task_id)}
    except Exception as e:
        return {"success": False, "error": str(e)}

def poll_suno_task(
    api_key: str,
    task_id: str,
    max_wait_seconds: int = 12 * 60,
    step_seconds: int = 6,
) -> Dict[str, Any]:
    try:
        max_wait = max(30, int(max_wait_seconds))
    except Exception:
        max_wait = 12 * 60
    try:
        step = max(2, int(step_seconds))
    except Exception:
        step = 6

    elapsed = 0
    last_payload: Dict[str, Any] = {}
    while elapsed < max_wait:
        time.sleep(step)
        elapsed += step
        try:
            status_r = requests.get(
                f"{SUNO_BASE}/generate/record-info",
                params={"taskId": task_id},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=20,
            )
            if status_r.status_code != 200:
                continue
            status_data = status_r.json()
            status, payload = _parse_status_payload(status_data)
            last_payload = payload
            if status == "FAILED":
                return {"success": False, "error": payload.get("errorMessage", "Geração falhou no Suno.")}
            audio_url = _find_first_audio_url(payload)
            if audio_url:
                return {"success": True, "status": status or "SUCCESS", "audio_url": audio_url, "raw": payload}
            if status in ("SUCCESS", "COMPLETED", "DONE"):
                continue
        except Exception:
            continue

    return {"success": False, "error": "Timeout aguardando a geração da música no Suno.", "raw": last_payload}

def download_suno_audio(audio_url: str) -> Dict[str, Any]:
    if not audio_url or not isinstance(audio_url, str):
        return {"success": False, "error": "URL de áudio inválida."}
    music_dir = "app/static/music"
    os.makedirs(music_dir, exist_ok=True)
    filename = f"song_{uuid.uuid4().hex[:10]}.mp3"
    path = os.path.join(music_dir, filename)
    try:
        dl = requests.get(audio_url, timeout=90, stream=True, headers={"User-Agent": "Mozilla/5.0"})
        if dl.status_code != 200:
            return {"success": False, "error": f"Falha ao baixar áudio do Suno (HTTP {dl.status_code})."}
        total = 0
        with open(path, "wb") as f:
            for chunk in dl.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                total += len(chunk)
                if total > 200 * 1024 * 1024:
                    break
        if total < 50 * 1024:
            try:
                os.remove(path)
            except Exception:
                pass
            return {"success": False, "error": "Áudio baixado muito pequeno; provável falha/URL inválida."}
        return {"success": True, "music_url": f"/static/music/{filename}", "music_filename": filename, "bytes": total}
    except Exception as e:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        return {"success": False, "error": str(e)}

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

    created = create_suno_task(
        api_key=api_key,
        lyrics=lyrics,
        title=title,
        style=style,
        model=model,
        vocal_gender=vocal_gender,
    )
    if not created.get("success"):
        return created
    task_id = str(created.get("task_id") or "")
    if not task_id:
        return {"success": False, "error": "Suno não retornou taskId."}
    polled = poll_suno_task(api_key=api_key, task_id=task_id)
    if not polled.get("success"):
        return {"success": False, "error": polled.get("error") or "Suno falhou.", "task_id": task_id}
    audio_url = polled.get("audio_url")
    if not audio_url:
        return {"success": False, "error": "URL do áudio não encontrada na resposta.", "task_id": task_id}
    dl = download_suno_audio(str(audio_url))
    if not dl.get("success"):
        return {"success": False, "error": dl.get("error") or "Falha ao baixar áudio do Suno.", "task_id": task_id, "audio_url": audio_url}
    return {
        "success": True,
        "task_id": task_id,
        "audio_url": audio_url,
        "music_url": dl.get("music_url"),
        "music_filename": dl.get("music_filename"),
    }
