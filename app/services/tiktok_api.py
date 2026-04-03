"""
Serviço de integração com TikTok via Content Posting API.
Requer um TikTok Developer App com acesso ao Content Posting API.
Fluxo: Upload vídeo → criar post com o vídeo.
"""
import requests
import os
import time
from app.database import SessionLocal
from app.models import Settings


TIKTOK_API_BASE = "https://open.tiktokapis.com/v2"


class TikTokService:
    def __init__(self):
        self._load_config()

    def _load_config(self):
        db = SessionLocal()
        settings = db.query(Settings).first()
        db.close()

        self.access_token = None

        if settings:
            self.access_token = getattr(settings, "tiktok_access_token", None)

        if not self.access_token:
            self.access_token = os.getenv("TIKTOK_ACCESS_TOKEN")

    def _is_configured(self):
        token = (self.access_token or "").strip()
        return bool(token)

    def _headers(self):
        return {
            "Authorization": f"Bearer {(self.access_token or '').strip()}",
            "Content-Type": "application/json; charset=UTF-8",
        }

    def publish_video(self, video_path, title="", privacy_level="SELF_ONLY"):
        """
        Publica um vídeo no TikTok via Content Posting API (Direct Post).
        privacy_level: SELF_ONLY, MUTUAL_FOLLOW_FRIENDS, FOLLOWER_OF_CREATOR, PUBLIC_TO_EVERYONE
        """
        self._load_config()

        if not self._is_configured():
            print("[TIKTOK MOCK] Credenciais não configuradas. Simulando publicação.")
            return {"publish_id": "mock_tt_12345", "status": "published_mock"}

        if not video_path or not os.path.exists(video_path):
            return {"error": f"Arquivo de vídeo não encontrado: {video_path}"}

        file_size = os.path.getsize(video_path)

        try:
            init_url = f"{TIKTOK_API_BASE}/post/publish/video/init/"
            init_payload = {
                "post_info": {
                    "title": (title or "")[:150],
                    "privacy_level": privacy_level,
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": file_size,
                    "chunk_size": file_size,
                    "total_chunk_count": 1,
                },
            }
            init_resp = requests.post(
                init_url, headers=self._headers(), json=init_payload, timeout=30
            )
            init_resp.raise_for_status()
            init_data = init_resp.json().get("data", {})
            publish_id = init_data.get("publish_id")
            upload_url = init_data.get("upload_url")

            if not publish_id or not upload_url:
                return {"error": f"TikTok não retornou publish_id/upload_url: {init_resp.text[:300]}"}

            with open(video_path, "rb") as f:
                video_bytes = f.read()

            upload_headers = {
                "Content-Type": "video/mp4",
                "Content-Length": str(file_size),
                "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
            }
            upload_resp = requests.put(
                upload_url, headers=upload_headers, data=video_bytes, timeout=300
            )
            if upload_resp.status_code not in (200, 201):
                return {"error": f"Falha no upload do vídeo ao TikTok: HTTP {upload_resp.status_code}"}

            for _ in range(30):
                time.sleep(5)
                status_resp = requests.post(
                    f"{TIKTOK_API_BASE}/post/publish/status/fetch/",
                    headers=self._headers(),
                    json={"publish_id": publish_id},
                    timeout=15,
                )
                if status_resp.status_code != 200:
                    continue
                status_data = status_resp.json().get("data", {})
                status = (status_data.get("status") or "").upper()
                if status == "PUBLISH_COMPLETE":
                    return {
                        "publish_id": publish_id,
                        "status": "published",
                        "tiktok_video_id": status_data.get("publicaly_available_post_id", [""])[0] if isinstance(status_data.get("publicaly_available_post_id"), list) else "",
                    }
                if status in ("FAILED",):
                    fail_reason = status_data.get("fail_reason", "desconhecido")
                    return {"error": f"TikTok rejeitou o vídeo: {fail_reason}", "publish_id": publish_id}

            return {"publish_id": publish_id, "status": "processing"}
        except requests.exceptions.RequestException as e:
            print(f"Erro ao publicar vídeo no TikTok: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"Detalhes: {e.response.text}")
            return {"error": str(e)}

    def publish_video_by_url(self, video_url, title="", privacy_level="SELF_ONLY"):
        """
        Publica um vídeo no TikTok usando URL pública (Pull from URL).
        """
        self._load_config()

        if not self._is_configured():
            print("[TIKTOK MOCK] Credenciais não configuradas. Simulando publicação.")
            return {"publish_id": "mock_tt_url_12345", "status": "published_mock"}

        try:
            url = f"{TIKTOK_API_BASE}/post/publish/video/init/"
            payload = {
                "post_info": {
                    "title": (title or "")[:150],
                    "privacy_level": privacy_level,
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                },
                "source_info": {
                    "source": "PULL_FROM_URL",
                    "video_url": video_url,
                },
            }
            resp = requests.post(url, headers=self._headers(), json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            return {
                "publish_id": data.get("publish_id"),
                "status": "processing",
            }
        except requests.exceptions.RequestException as e:
            print(f"Erro ao publicar vídeo no TikTok por URL: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"Detalhes: {e.response.text}")
            return {"error": str(e)}
