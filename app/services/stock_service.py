import os
import requests
import random
import time
from app.database import SessionLocal
from app.models import Settings

class StockService:
    def __init__(self):
        db = SessionLocal()
        settings = db.query(Settings).first()
        db.close()
        
        self.pexels_api_key = None
        self.pixabay_api_key = None
        self.edenai_api_key = None
        
        if settings:
            self.pexels_api_key = settings.pexels_api_key
            self.pixabay_api_key = settings.pixabay_api_key
            self.edenai_api_key = getattr(settings, "edenai_api_key", None)
            
        # Fallback to env vars
        if not self.pexels_api_key: self.pexels_api_key = os.getenv('PEXELS_API_KEY')
        if not self.pixabay_api_key: self.pixabay_api_key = os.getenv('PIXABAY_API_KEY')
        if not self.edenai_api_key: self.edenai_api_key = os.getenv("EDENAI_API_KEY")

    def _edenai_headers(self):
        key = (self.edenai_api_key or "").strip()
        if not key:
            return None
        return {"Authorization": f"Bearer {key}"}

    def search_image(self, query: str, orientation: str = "landscape"):
        """Search for stock images based on query"""
        headers = self._edenai_headers()
        if headers:
            try:
                provider = (os.getenv("EDENAI_IMAGE_PROVIDER") or "openai").strip()
                resolution = "1792x1024" if orientation == "landscape" else "1024x1792"
                payload = {
                    "providers": provider,
                    "text": (query or "").strip(),
                    "resolution": resolution,
                }
                r = requests.post(
                    "https://api.edenai.run/v2/image/generation",
                    headers=headers,
                    json=payload,
                    timeout=120,
                )
                if r.status_code == 200:
                    data = r.json() if (r.headers.get("content-type") or "").startswith("application/json") else {}
                    provider_payload = data.get(provider) if isinstance(data, dict) else None
                    if not isinstance(provider_payload, dict):
                        provider_payload = None
                        if isinstance(data, dict):
                            for _, v in data.items():
                                if isinstance(v, dict) and (
                                    v.get("image_resource_url") or v.get("image_url") or v.get("url")
                                ):
                                    provider_payload = v
                                    break
                    if isinstance(provider_payload, dict):
                        url = (
                            provider_payload.get("image_resource_url")
                            or provider_payload.get("image_url")
                            or provider_payload.get("url")
                        )
                        if isinstance(url, str) and url.strip():
                            return url.strip()
            except Exception as e:
                print(f"Eden AI image error: {e}")

        # Pexels First
        if self.pexels_api_key:
            try:
                url = f"https://api.pexels.com/v1/search?query={query}&per_page=5&orientation={orientation}"
                headers = {"Authorization": self.pexels_api_key}
                response = requests.get(url, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    if data.get('photos'):
                        photo = random.choice(data['photos'])
                        return photo['src']['original']  # Return URL
            except Exception as e:
                print(f"Pexels Error: {e}")

        # Pixabay Fallback
        if self.pixabay_api_key:
            try:
                pixabay_orientation = "horizontal" if orientation == "landscape" else "vertical"
                url = f"https://pixabay.com/api/?key={self.pixabay_api_key}&q={query}&image_type=photo&orientation={pixabay_orientation}"
                response = requests.get(url)
                if response.status_code == 200:
                    data = response.json()
                    if data.get('hits'):
                        hit = random.choice(data['hits'])
                        return hit['largeImageURL']
            except Exception as e:
                print(f"Pixabay Error: {e}")

        return None

    def search_video(self, query: str, orientation: str = "landscape"):
        """Gera um vídeo curto por texto via Eden AI (fallback futuro para stock)."""
        headers = self._edenai_headers()
        if not headers:
            return None

        provider = (os.getenv("EDENAI_VIDEO_PROVIDER") or "amazon").strip()
        try:
            payload = {"providers": provider, "text": (query or "").strip()}
            r = requests.post(
                "https://api.edenai.run/v2/video/generation_async",
                headers=headers,
                json=payload,
                timeout=120,
            )
            if r.status_code >= 400:
                return None
            data = r.json() if (r.headers.get("content-type") or "").startswith("application/json") else {}
            public_id = None
            if isinstance(data, dict):
                public_id = (data.get("public_id") or data.get("id") or "").strip()
            if not public_id:
                return None

            deadline = time.time() + 90
            last = None
            while time.time() < deadline:
                rr = requests.get(
                    f"https://api.edenai.run/v2/video/generation_async/{public_id}/",
                    headers=headers,
                    params={"response_as_dict": "true", "show_original_response": "false"},
                    timeout=120,
                )
                if rr.status_code >= 400:
                    break
                last = rr.json() if (rr.headers.get("content-type") or "").startswith("application/json") else {}
                provider_payload = last.get(provider) if isinstance(last, dict) else None
                if isinstance(provider_payload, dict):
                    status = (provider_payload.get("status") or "").lower()
                    if status in {"succeeded", "success"}:
                        for k in ("video_resource_url", "video_url", "url"):
                            v = provider_payload.get(k)
                            if isinstance(v, str) and v.startswith("http"):
                                return v
                    if status in {"failed", "error"}:
                        return None
                time.sleep(2)
            if isinstance(last, dict):
                provider_payload = last.get(provider)
                if isinstance(provider_payload, dict):
                    for k in ("video_resource_url", "video_url", "url"):
                        v = provider_payload.get(k)
                        if isinstance(v, str) and v.startswith("http"):
                            return v
            return None
        except Exception:
            return None
