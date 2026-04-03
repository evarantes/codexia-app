import requests
import os
from app.database import SessionLocal
from app.models import Settings


GRAPH_API_VERSION = "v20.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


class FacebookService:
    def __init__(self):
        self._load_config()

    def _load_config(self):
        db = SessionLocal()
        settings = db.query(Settings).first()
        db.close()

        self.page_id = None
        self.access_token = None

        if settings:
            self.page_id = settings.facebook_page_id
            self.access_token = settings.facebook_access_token

        if not self.page_id:
            self.page_id = os.getenv("FACEBOOK_PAGE_ID")
        if not self.access_token:
            self.access_token = os.getenv("FACEBOOK_ACCESS_TOKEN")

    def _is_configured(self):
        token = (self.access_token or "").strip()
        page = (self.page_id or "").strip()
        if not token or not page:
            return False
        if token in ("seu_token_de_acesso_da_pagina", "YOUR_TOKEN"):
            return False
        return True

    def post_to_feed(self, message, link=None):
        """Publica texto (e link opcional) na página do Facebook via Graph API."""
        self._load_config()

        if not self._is_configured():
            print("[FACEBOOK MOCK] Credenciais não configuradas. Simulando postagem.")
            return {"id": "mock_post_id_12345", "status": "published_mock"}

        url = f"{GRAPH_API_BASE}/{self.page_id}/feed"
        payload = {
            "message": message,
            "access_token": self.access_token,
        }
        if link:
            payload["link"] = link

        try:
            response = requests.post(url, data=payload, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Erro ao postar no Facebook: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"Detalhes do erro: {e.response.text}")
            return {"error": str(e)}

    def post_video(self, video_path, title="", description=""):
        """Publica um vídeo na página do Facebook (Reels/Feed)."""
        self._load_config()

        if not self._is_configured():
            print("[FACEBOOK MOCK] Credenciais não configuradas. Simulando upload de vídeo.")
            return {"id": "mock_video_id_12345", "status": "published_mock"}

        if not video_path or not os.path.exists(video_path):
            return {"error": f"Arquivo de vídeo não encontrado: {video_path}"}

        url = f"{GRAPH_API_BASE}/{self.page_id}/videos"
        payload = {"access_token": self.access_token}
        if title:
            payload["title"] = title
        if description:
            payload["description"] = description

        try:
            with open(video_path, "rb") as f:
                files = {"source": (os.path.basename(video_path), f, "video/mp4")}
                response = requests.post(url, data=payload, files=files, timeout=300)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Erro ao publicar vídeo no Facebook: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"Detalhes: {e.response.text}")
            return {"error": str(e)}

    def post_reels(self, video_path, description=""):
        """Publica um Reel na página do Facebook."""
        self._load_config()

        if not self._is_configured():
            print("[FACEBOOK MOCK] Credenciais não configuradas. Simulando upload de Reel.")
            return {"id": "mock_reel_id_12345", "status": "published_mock"}

        if not video_path or not os.path.exists(video_path):
            return {"error": f"Arquivo de vídeo não encontrado: {video_path}"}

        init_url = f"{GRAPH_API_BASE}/{self.page_id}/video_reels"
        try:
            init_resp = requests.post(
                init_url,
                data={
                    "upload_phase": "start",
                    "access_token": self.access_token,
                },
                timeout=30,
            )
            init_resp.raise_for_status()
            video_id = init_resp.json().get("video_id")
            if not video_id:
                return {"error": "Facebook não retornou video_id para o Reel."}

            upload_url = f"https://rupload.facebook.com/video-upload/{GRAPH_API_VERSION}/{video_id}"
            file_size = os.path.getsize(video_path)
            with open(video_path, "rb") as f:
                upload_resp = requests.post(
                    upload_url,
                    headers={
                        "Authorization": f"OAuth {self.access_token}",
                        "offset": "0",
                        "file_size": str(file_size),
                    },
                    data=f,
                    timeout=300,
                )
            upload_resp.raise_for_status()

            publish_resp = requests.post(
                init_url,
                data={
                    "upload_phase": "finish",
                    "access_token": self.access_token,
                    "video_id": video_id,
                    "description": description or "",
                },
                timeout=30,
            )
            publish_resp.raise_for_status()
            result = publish_resp.json()
            result["video_id"] = video_id
            return result
        except requests.exceptions.RequestException as e:
            print(f"Erro ao publicar Reel no Facebook: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"Detalhes: {e.response.text}")
            return {"error": str(e)}

    def get_post_metrics(self, post_id):
        """Busca métricas de um post (requer permissões específicas)."""
        self._load_config()
        if not self._is_configured():
            return {"likes": 0, "comments": 0, "reach": 0}

        try:
            url = f"{GRAPH_API_BASE}/{post_id}"
            params = {
                "fields": "likes.summary(true),comments.summary(true),shares",
                "access_token": self.access_token,
            }
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            return {
                "likes": data.get("likes", {}).get("summary", {}).get("total_count", 0),
                "comments": data.get("comments", {}).get("summary", {}).get("total_count", 0),
                "shares": data.get("shares", {}).get("count", 0) if data.get("shares") else 0,
            }
        except Exception as e:
            print(f"Erro ao buscar métricas do Facebook: {e}")
            return {"likes": 0, "comments": 0, "shares": 0}
