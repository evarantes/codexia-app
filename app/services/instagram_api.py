"""
Serviço de integração com Instagram via Graph API (Instagram Graph API).
Requer uma conta Instagram Business/Creator vinculada a uma Facebook Page.
Usa o mesmo Facebook Access Token (com permissões instagram_basic, instagram_content_publish).
"""
import requests
import os
import time
from app.database import SessionLocal
from app.models import Settings


GRAPH_API_VERSION = "v20.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


class InstagramService:
    def __init__(self):
        self._load_config()

    def _load_config(self):
        db = SessionLocal()
        settings = db.query(Settings).first()
        db.close()

        self.ig_user_id = None
        self.access_token = None

        if settings:
            self.ig_user_id = getattr(settings, "instagram_user_id", None)
            self.access_token = getattr(settings, "instagram_access_token", None) or settings.facebook_access_token

        if not self.ig_user_id:
            self.ig_user_id = os.getenv("INSTAGRAM_USER_ID")
        if not self.access_token:
            self.access_token = os.getenv("INSTAGRAM_ACCESS_TOKEN") or os.getenv("FACEBOOK_ACCESS_TOKEN")

    def _is_configured(self):
        token = (self.access_token or "").strip()
        ig_id = (self.ig_user_id or "").strip()
        return bool(token and ig_id)

    def publish_reels(self, video_url, caption=""):
        """
        Publica um Reel no Instagram via Graph API (Container-based publishing).
        video_url deve ser uma URL pública acessível pelo servidor do Facebook.
        """
        self._load_config()

        if not self._is_configured():
            return {"error": "Instagram não configurado. Adicione o User ID e Access Token em Configurações."}

        try:
            container_url = f"{GRAPH_API_BASE}/{self.ig_user_id}/media"
            container_resp = requests.post(
                container_url,
                data={
                    "media_type": "REELS",
                    "video_url": video_url,
                    "caption": caption or "",
                    "access_token": self.access_token,
                },
                timeout=60,
            )
            container_resp.raise_for_status()
            creation_id = container_resp.json().get("id")
            if not creation_id:
                return {"error": "Instagram não retornou creation_id do container."}

            for _ in range(30):
                time.sleep(5)
                status_resp = requests.get(
                    f"{GRAPH_API_BASE}/{creation_id}",
                    params={"fields": "status_code", "access_token": self.access_token},
                    timeout=15,
                )
                status_data = status_resp.json()
                status_code = (status_data.get("status_code") or "").upper()
                if status_code == "FINISHED":
                    break
                if status_code == "ERROR":
                    return {"error": f"Erro no processamento do container: {status_data}"}

            publish_url = f"{GRAPH_API_BASE}/{self.ig_user_id}/media_publish"
            publish_resp = requests.post(
                publish_url,
                data={
                    "creation_id": creation_id,
                    "access_token": self.access_token,
                },
                timeout=30,
            )
            publish_resp.raise_for_status()
            result = publish_resp.json()
            result["creation_id"] = creation_id
            return result
        except requests.exceptions.RequestException as e:
            print(f"Erro ao publicar Reel no Instagram: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"Detalhes: {e.response.text}")
            return {"error": str(e)}

    def publish_image(self, image_url, caption=""):
        """Publica uma imagem no feed do Instagram."""
        self._load_config()

        if not self._is_configured():
            return {"error": "Instagram não configurado. Adicione o User ID e Access Token em Configurações."}

        try:
            container_url = f"{GRAPH_API_BASE}/{self.ig_user_id}/media"
            container_resp = requests.post(
                container_url,
                data={
                    "image_url": image_url,
                    "caption": caption or "",
                    "access_token": self.access_token,
                },
                timeout=30,
            )
            container_resp.raise_for_status()
            creation_id = container_resp.json().get("id")
            if not creation_id:
                return {"error": "Instagram não retornou creation_id."}

            publish_url = f"{GRAPH_API_BASE}/{self.ig_user_id}/media_publish"
            publish_resp = requests.post(
                publish_url,
                data={
                    "creation_id": creation_id,
                    "access_token": self.access_token,
                },
                timeout=30,
            )
            publish_resp.raise_for_status()
            return publish_resp.json()
        except requests.exceptions.RequestException as e:
            print(f"Erro ao publicar imagem no Instagram: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"Detalhes: {e.response.text}")
            return {"error": str(e)}
