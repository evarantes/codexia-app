import requests
import os
from app.database import SessionLocal
from app.models import Settings

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

        # Fallback para .env
        if not self.page_id:
            self.page_id = os.getenv("FACEBOOK_PAGE_ID")
        if not self.access_token:
            self.access_token = os.getenv("FACEBOOK_ACCESS_TOKEN")

    def post_to_feed(self, message, link=None):
        """
        Publica um texto e link na página do Facebook.
        """
        self._load_config() # Reload config

        if not self.access_token or not self.page_id or self.access_token == "seu_token_de_acesso_da_pagina":
            print("[FACEBOOK MOCK] Credenciais não configuradas. Simulando postagem.")
            return {"id": "mock_post_id_12345", "status": "published_mock"}

        url = f"{self.base_url}/{self.page_id}/feed"
        payload = {
            "message": message,
            "access_token": self.access_token
        }
        if link:
            payload["link"] = link

        try:
            response = requests.post(url, data=payload)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Erro ao postar no Facebook: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"Detalhes do erro: {e.response.text}")
            return {"error": str(e)}

    def get_post_metrics(self, post_id):
        """
        Busca curtidas, comentários e alcance.
        """
        # Simplificação para demonstração, pois métricas reais exigem permissões específicas
        return {"likes": 0, "comments": 0, "reach": 0}
