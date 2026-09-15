from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, Optional

import requests

from app.config import VIDEO_OUTPUT_DIR, VIDEO_URL_PREFIX
from app.models import Settings


class CinematicProviderError(RuntimeError):
    pass


class CinematicVideoProvider:
    """Low-level async adapters for Runway, Veo and Kling/fal.

    These methods only submit/poll. They never spend credits on GET/status calls.
    Generation is triggered only from explicit production/scene endpoints.
    Completed Veo videos are copied to Codexia's persistent video volume so the
    browser can preview them and the hybrid compositor can reuse the exact clip.
    """

    RUNWAY_BASE = "https://api.dev.runwayml.com/v1"
    GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
    FAL_BASE = "https://queue.fal.run"

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings

    @staticmethod
    def _env(*names: str) -> str:
        for name in names:
            value = str(os.getenv(name) or "").strip()
            if value:
                return value
        return ""

    def _gemini_key(self) -> str:
        return str(getattr(self.settings, "gemini_api_key", None) or os.getenv("GEMINI_API_KEY") or "").strip()

    def status(self) -> Dict[str, Any]:
        runway = bool(self._env("RUNWAYML_API_SECRET", "RUNWAY_API_KEY"))
        veo = bool(self._gemini_key())
        kling = bool(self._env("FAL_KEY"))
        return {
            "runway": {"configured": runway, "scale_model": self._env("RUNWAY_SCALE_MODEL") or "gen4_turbo", "premium_model": self._env("RUNWAY_PREMIUM_MODEL") or "gen4.5"},
            "veo": {"configured": veo, "scale_model": self._env("VEO_SCALE_MODEL") or "veo-3.1-lite-generate-preview", "premium_model": self._env("VEO_PREMIUM_MODEL") or "veo-3.1-fast-generate-preview"},
            "kling": {"configured": kling, "scale_model": "fal-ai/kling-video/v3/turbo/standard/image-to-video", "premium_model": "fal-ai/kling-video/v3/standard/image-to-video"},
        }

    def submit_runway(self, *, prompt: str, image_uri: Optional[str], duration: int = 5, premium: bool = False, ratio: str = "1280:720") -> Dict[str, Any]:
        key = self._env("RUNWAYML_API_SECRET", "RUNWAY_API_KEY")
        if not key:
            raise CinematicProviderError("Runway não configurado: defina RUNWAYML_API_SECRET no Coolify.")
        model = (self._env("RUNWAY_PREMIUM_MODEL") or "gen4.5") if premium else (self._env("RUNWAY_SCALE_MODEL") or "gen4_turbo")
        body: Dict[str, Any] = {
            "model": model,
            "promptText": str(prompt or "")[:1000],
            "ratio": ratio,
            "duration": 10 if int(duration) >= 10 else 5,
        }
        if image_uri:
            body["promptImage"] = image_uri
        elif model == "gen4_turbo":
            body["model"] = self._env("RUNWAY_PREMIUM_MODEL") or "gen4.5"
        response = requests.post(
            f"{self.RUNWAY_BASE}/image_to_video",
            headers={"Authorization": f"Bearer {key}", "X-Runway-Version": "2024-11-06", "Content-Type": "application/json"},
            json=body,
            timeout=60,
        )
        if not response.ok:
            raise CinematicProviderError(f"Runway HTTP {response.status_code}: {response.text[:500]}")
        data = response.json()
        return {"provider": "runway", "job_id": data.get("id"), "status": data.get("status") or "PENDING", "raw": data}

    def poll_runway(self, job_id: str) -> Dict[str, Any]:
        key = self._env("RUNWAYML_API_SECRET", "RUNWAY_API_KEY")
        if not key:
            raise CinematicProviderError("Runway não configurado.")
        response = requests.get(
            f"{self.RUNWAY_BASE}/tasks/{job_id}",
            headers={"Authorization": f"Bearer {key}", "X-Runway-Version": "2024-11-06"},
            timeout=45,
        )
        if not response.ok:
            raise CinematicProviderError(f"Runway status HTTP {response.status_code}: {response.text[:500]}")
        data = response.json()
        output = data.get("output") or []
        return {"provider": "runway", "job_id": job_id, "status": data.get("status"), "output_url": output[0] if output else None, "raw": data}

    @staticmethod
    def _veo_duration_value(duration: int) -> int:
        """Clamp Codexia scene duration to numeric values accepted by Veo 3.1."""
        value = int(duration or 8)
        return 8 if value >= 8 else 6 if value >= 6 else 4

    def submit_veo(self, *, prompt: str, image_base64: Optional[str] = None, image_mime: str = "image/png", duration: int = 8, premium: bool = False, aspect_ratio: str = "16:9") -> Dict[str, Any]:
        key = self._gemini_key()
        if not key:
            raise CinematicProviderError("Veo não configurado: cadastre a GEMINI_API_KEY.")
        model = (self._env("VEO_PREMIUM_MODEL") or "veo-3.1-fast-generate-preview") if premium else (self._env("VEO_SCALE_MODEL") or "veo-3.1-lite-generate-preview")
        instance: Dict[str, Any] = {"prompt": str(prompt or "")[:4000]}
        if image_base64:
            instance["image"] = {"inlineData": {"mimeType": image_mime, "data": image_base64}}
        parameters: Dict[str, Any] = {
            "durationSeconds": self._veo_duration_value(duration),
            "aspectRatio": "9:16" if aspect_ratio == "9:16" else "16:9",
            "resolution": "720p",
        }
        body = {"instances": [instance], "parameters": parameters}
        response = requests.post(
            f"{self.GEMINI_BASE}/models/{model}:predictLongRunning",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=body,
            timeout=60,
        )
        if not response.ok:
            raise CinematicProviderError(f"Veo HTTP {response.status_code}: {response.text[:500]}")
        data = response.json()
        operation_name = str(data.get("name") or "").strip()
        if not operation_name:
            raise CinematicProviderError("Veo aceitou a requisição, mas não retornou o identificador da operação.")
        return {"provider": "veo", "job_id": operation_name, "status": "PENDING", "model": model, "raw": data}

    @staticmethod
    def _veo_local_names(job_id: str) -> tuple[str, str, str]:
        digest = hashlib.sha256(str(job_id or "").encode("utf-8")).hexdigest()[:20]
        filename = f"veo_pilot_{digest}.mp4"
        return filename, os.path.join(VIDEO_OUTPUT_DIR, filename), f"{VIDEO_URL_PREFIX}/{filename}"

    def _persist_veo_video(self, *, job_id: str, video_uri: str, api_key: str) -> Dict[str, str]:
        filename, final_path, local_url = self._veo_local_names(job_id)
        try:
            if os.path.isfile(final_path) and os.path.getsize(final_path) > 32 * 1024:
                return {"filename": filename, "file_path": final_path, "video_url": local_url}
        except Exception:
            pass
        os.makedirs(VIDEO_OUTPUT_DIR, exist_ok=True)
        temp_path = final_path + ".part"
        total = 0
        try:
            with requests.get(
                str(video_uri),
                headers={"x-goog-api-key": api_key},
                stream=True,
                allow_redirects=True,
                timeout=(20, 240),
            ) as response:
                if not response.ok:
                    raise CinematicProviderError(f"Veo download HTTP {response.status_code}: {response.text[:400]}")
                with open(temp_path, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > 300 * 1024 * 1024:
                            raise CinematicProviderError("O clipe Veo excedeu o limite de segurança de 300 MB.")
                        handle.write(chunk)
            if total < 32 * 1024:
                raise CinematicProviderError("O Veo concluiu, mas o arquivo recebido está vazio ou incompleto.")
            os.replace(temp_path, final_path)
        except Exception:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass
            raise
        return {"filename": filename, "file_path": final_path, "video_url": local_url}

    def poll_veo(self, job_id: str) -> Dict[str, Any]:
        key = self._gemini_key()
        if not key:
            raise CinematicProviderError("Veo não configurado.")
        response = requests.get(f"{self.GEMINI_BASE}/{job_id.lstrip('/')}", headers={"x-goog-api-key": key}, timeout=45)
        if not response.ok:
            raise CinematicProviderError(f"Veo status HTTP {response.status_code}: {response.text[:500]}")
        data = response.json()
        done = bool(data.get("done"))
        if done and data.get("error"):
            err = data.get("error") or {}
            message = str(err.get("message") or err)[:700]
            return {"provider": "veo", "job_id": job_id, "status": "FAILED", "output_url": None, "error": message, "raw": data}
        video_uri = None
        if done:
            try:
                video_uri = data["response"]["generateVideoResponse"]["generatedSamples"][0]["video"]["uri"]
            except Exception:
                try:
                    video_uri = data["response"]["generatedVideos"][0]["video"]["uri"]
                except Exception:
                    video_uri = None
        if not done:
            return {"provider": "veo", "job_id": job_id, "status": "PENDING", "output_url": None, "raw": data}
        if not video_uri:
            return {"provider": "veo", "job_id": job_id, "status": "FAILED", "output_url": None, "error": "Veo terminou sem retornar URI de vídeo.", "raw": data}
        persisted = self._persist_veo_video(job_id=job_id, video_uri=video_uri, api_key=key)
        return {
            "provider": "veo",
            "job_id": job_id,
            "status": "SUCCEEDED",
            "output_url": persisted["video_url"],
            "file_path": persisted["file_path"],
            "filename": persisted["filename"],
            "remote_output_url": video_uri,
            "raw": data,
        }

    def submit_kling(self, *, prompt: str, start_image_url: str, duration: int = 5, premium: bool = True) -> Dict[str, Any]:
        key = self._env("FAL_KEY")
        if not key:
            raise CinematicProviderError("Kling não configurado: defina FAL_KEY no Coolify.")
        model_path = "fal-ai/kling-video/v3/standard/image-to-video" if premium else "fal-ai/kling-video/v3/turbo/standard/image-to-video"
        response = requests.post(
            f"{self.FAL_BASE}/{model_path}",
            headers={"Authorization": f"Key {key}", "Content-Type": "application/json"},
            json={
                "prompt": str(prompt or "")[:2500],
                "start_image_url": start_image_url,
                "duration": str(max(3, min(15, int(duration or 5)))),
                "generate_audio": False,
            },
            timeout=60,
        )
        if not response.ok:
            raise CinematicProviderError(f"Kling/fal HTTP {response.status_code}: {response.text[:500]}")
        data = response.json()
        request_id = data.get("request_id") or data.get("requestId")
        return {
            "provider": "kling",
            "job_id": request_id,
            "status": data.get("status") or "IN_QUEUE",
            "status_url": data.get("status_url"),
            "response_url": data.get("response_url"),
            "model_path": model_path,
            "raw": data,
        }

    def poll_kling(self, *, job_id: str, status_url: Optional[str] = None, response_url: Optional[str] = None, model_path: Optional[str] = None) -> Dict[str, Any]:
        key = self._env("FAL_KEY")
        if not key:
            raise CinematicProviderError("Kling não configurado.")
        model = model_path or "fal-ai/kling-video/v3/standard/image-to-video"
        status_endpoint = status_url or f"{self.FAL_BASE}/{model}/requests/{job_id}/status"
        status_resp = requests.get(status_endpoint, headers={"Authorization": f"Key {key}"}, timeout=45)
        if not status_resp.ok:
            raise CinematicProviderError(f"Kling status HTTP {status_resp.status_code}: {status_resp.text[:500]}")
        status_data = status_resp.json()
        status = str(status_data.get("status") or "").upper()
        if status not in {"COMPLETED", "SUCCEEDED"}:
            return {"provider": "kling", "job_id": job_id, "status": status or "PENDING", "raw": status_data}
        result_endpoint = response_url or f"{self.FAL_BASE}/{model}/requests/{job_id}"
        result_resp = requests.get(result_endpoint, headers={"Authorization": f"Key {key}"}, timeout=45)
        if not result_resp.ok:
            raise CinematicProviderError(f"Kling result HTTP {result_resp.status_code}: {result_resp.text[:500]}")
        result = result_resp.json()
        video = result.get("video") or {}
        return {"provider": "kling", "job_id": job_id, "status": "SUCCEEDED", "output_url": video.get("url"), "raw": result}

    def _fallback_text_to_video_provider(self) -> Optional[str]:
        if self._gemini_key():
            return "veo"
        if self._env("RUNWAYML_API_SECRET", "RUNWAY_API_KEY"):
            return "runway"
        return None

    def submit(self, *, provider: str, prompt: str, image_uri: Optional[str] = None, image_base64: Optional[str] = None, duration: int = 5, premium: bool = False, aspect_ratio: str = "16:9") -> Dict[str, Any]:
        provider = str(provider or "runway").strip().lower()
        requested_provider = provider
        if provider == "kling" and (not image_uri or str(image_uri).startswith("data:")):
            provider = self._fallback_text_to_video_provider() or "kling"
        if provider == "runway":
            result = self.submit_runway(prompt=prompt, image_uri=image_uri, duration=duration, premium=premium, ratio="720:1280" if aspect_ratio == "9:16" else "1280:720")
        elif provider == "veo":
            result = self.submit_veo(prompt=prompt, image_base64=image_base64, duration=duration, premium=premium, aspect_ratio=aspect_ratio)
        elif provider == "kling":
            if not image_uri or str(image_uri).startswith("data:"):
                raise CinematicProviderError("Kling/fal requer uma URL pública da imagem inicial e não há Veo/Runway configurado para fallback.")
            result = self.submit_kling(prompt=prompt, start_image_url=image_uri, duration=duration, premium=premium)
        else:
            raise CinematicProviderError(f"Provedor de vídeo não suportado: {provider}")
        if requested_provider != provider:
            result["requested_provider"] = requested_provider
            result["fallback_provider"] = provider
            result["fallback_reason"] = "O provedor solicitado exige imagem inicial; foi usado um provedor texto-para-vídeo configurado."
        return result
