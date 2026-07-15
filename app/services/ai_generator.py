import os
import re
import time
import uuid
import base64
import openai
import requests
from pathlib import Path
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
from app.database import SessionLocal
from app.models import Settings
from app.services.global_settings_service import (
    backfill_settings_from_legacy,
    build_global_settings_service,
    get_or_create_latest_settings,
)
from sqlalchemy.exc import OperationalError, SQLAlchemyError

load_dotenv()

PLACEHOLDER_MARKERS = (
    "sua_chave",
    "your_api_key",
    "your-key",
    "placeholder",
    "changeme",
    "change_me",
    "replace_me",
    "insira_sua_chave",
    "coloque_sua_chave",
)

PLACEHOLDER_VALUES = {
    "sk-...",
    "sk-or-...",
    "api_key",
    "token",
}


def _normalize_secret_value(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    compact = lowered.replace(" ", "").replace("-", "_")
    if lowered in PLACEHOLDER_VALUES or compact in PLACEHOLDER_VALUES:
        return None
    if any(marker in compact for marker in PLACEHOLDER_MARKERS):
        return None
    return raw


def _extract_setting_value(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("value")
    return value

class AIContentGenerator:
    def __init__(self):
        self.api_key = None
        self.gemini_key = None
        self.deepseek_key = None
        self.groq_key = None
        self.anthropic_key = None
        self.mistral_key = None
        self.openrouter_key = None
        self.openrouter_model = None
        self.edenai_key = None
        self.leonardo_key = None
        self.leonardo_model_id = None
        self.elevenlabs_key = None
        self.elevenlabs_voice_id = None
        self.elevenlabs_voice_name = None
        self.voice_provider = "elevenlabs"
        self.default_voice = None
        self.default_language = "pt-BR"
        self.provider = "openai"
        self.hf_token = os.getenv("HUGGINGFACE_TOKEN")

    def _load_config(self):
        # Tenta carregar do banco primeiro, depois do .env
        db = SessionLocal()
        settings = None
        try:
            settings = db.query(Settings).order_by(Settings.id.desc()).first()
            if settings is None:
                settings = get_or_create_latest_settings(db)
            settings = backfill_settings_from_legacy(db, settings=settings)
        except OperationalError as e:
            print(f"AVISO: Falha ao carregar Settings do banco (migração pendente?): {e}")
        except SQLAlchemyError as e:
            print(f"AVISO: Falha ao carregar Settings do banco (erro SQL): {e}")
        except Exception as e:
            print(f"AVISO: Falha ao carregar Settings do banco: {e}")
        finally:
            db.close()

        self.api_key = None
        self.gemini_key = None
        self.deepseek_key = None
        self.groq_key = None
        self.anthropic_key = None
        self.mistral_key = None
        self.openrouter_key = None
        self.openrouter_model = None
        self.edenai_key = None
        self.leonardo_key = None
        self.leonardo_model_id = None
        self.elevenlabs_key = None
        self.elevenlabs_voice_id = None
        self.elevenlabs_voice_name = None
        self.voice_provider = "elevenlabs"
        self.default_voice = None
        self.default_language = "pt-BR"
        self.provider = "openrouter"
        self.hf_token = os.getenv("HUGGINGFACE_TOKEN") # Para MusicGen

        if settings:
            self.gemini_key = _normalize_secret_value(settings.gemini_api_key)
            self.deepseek_key = _normalize_secret_value(settings.deepseek_api_key)
            self.groq_key = _normalize_secret_value(settings.groq_api_key)
            self.anthropic_key = _normalize_secret_value(settings.anthropic_api_key)
            self.mistral_key = _normalize_secret_value(settings.mistral_api_key)
            self.openrouter_key = _normalize_secret_value(settings.openrouter_api_key)
            self.openrouter_model = getattr(settings, "openrouter_model", None)
            self.leonardo_key = _normalize_secret_value(getattr(settings, "leonardo_api_key", None))
            self.leonardo_model_id = getattr(settings, "leonardo_model_id", None)
            self.provider = settings.ai_provider or "openrouter"
        try:
            global_settings = build_global_settings_service(db=db, settings=settings)
            ai_settings = global_settings.get_ai_provider_settings()
            bible_video_settings = global_settings.get_bible_video_factory_settings()
            self.api_key = _normalize_secret_value(_extract_setting_value(ai_settings.get("openai_api_key"))) or self.api_key
            self.edenai_key = _normalize_secret_value(_extract_setting_value(ai_settings.get("edenai_api_key"))) or self.edenai_key
            self.elevenlabs_key = _normalize_secret_value(_extract_setting_value(ai_settings.get("elevenlabs_api_key"))) or self.elevenlabs_key
            self.elevenlabs_voice_id = _extract_setting_value(ai_settings.get("elevenlabs_voice_id")) or self.elevenlabs_voice_id
            self.elevenlabs_voice_name = _extract_setting_value(ai_settings.get("elevenlabs_voice_name")) or self.elevenlabs_voice_name
            self.voice_provider = str((bible_video_settings or {}).get("voice_provider") or self.voice_provider or "elevenlabs").strip().lower() or "elevenlabs"
            self.default_voice = (bible_video_settings or {}).get("default_voice") or self.default_voice
            self.default_language = str((bible_video_settings or {}).get("default_language") or self.default_language or "pt-BR").strip() or "pt-BR"
        except Exception as e:
            print(f"AVISO: Falha ao carregar configuracao central de voz: {e}")

        # Fallback to env vars
        if not self.api_key: self.api_key = _normalize_secret_value(os.getenv("OPENAI_API_KEY"))
        if not self.gemini_key: self.gemini_key = _normalize_secret_value(os.getenv("GEMINI_API_KEY"))
        if not self.deepseek_key: self.deepseek_key = _normalize_secret_value(os.getenv("DEEPSEEK_API_KEY"))
        if not self.groq_key: self.groq_key = _normalize_secret_value(os.getenv("GROQ_API_KEY"))
        if not self.anthropic_key: self.anthropic_key = _normalize_secret_value(os.getenv("ANTHROPIC_API_KEY"))
        if not self.mistral_key: self.mistral_key = _normalize_secret_value(os.getenv("MISTRAL_API_KEY"))
        if not self.openrouter_key: self.openrouter_key = _normalize_secret_value(os.getenv("OPENROUTER_API_KEY"))
        if not self.openrouter_model: self.openrouter_model = os.getenv("OPENROUTER_MODEL")
        if not self.edenai_key: self.edenai_key = _normalize_secret_value(os.getenv("EDENAI_API_KEY"))
        if not self.leonardo_key: self.leonardo_key = _normalize_secret_value(os.getenv("LEONARDO_API_KEY"))
        if not self.leonardo_model_id: self.leonardo_model_id = os.getenv("LEONARDO_MODEL_ID")
        if not self.elevenlabs_key: self.elevenlabs_key = _normalize_secret_value(os.getenv("ELEVENLABS_API_KEY"))
        if not self.elevenlabs_voice_id: self.elevenlabs_voice_id = os.getenv("ELEVENLABS_VOICE_ID")
        if not self.default_language: self.default_language = os.getenv("DEFAULT_LANGUAGE") or "pt-BR"

    def _normalize_voice_provider(self, provider: Optional[str]) -> str:
        raw = str(provider or "").strip().lower()
        aliases = {
            "eleven": "elevenlabs",
            "11labs": "elevenlabs",
            "eden_ai": "edenai",
            "openai": "openai_tts",
            "openai-tts": "openai_tts",
            "edge": "edge_tts",
        }
        return aliases.get(raw, raw or "elevenlabs")

    def _tts_provider_order(self, preferred_provider: Optional[str] = None) -> List[str]:
        configured = self._normalize_voice_provider(preferred_provider or self.voice_provider)
        premium_order = ["elevenlabs", "edenai", "openai_tts"]
        if configured == "edenai":
            premium_order = ["edenai", "elevenlabs", "openai_tts"]
        elif configured == "openai_tts":
            premium_order = ["openai_tts", "elevenlabs", "edenai"]
        elif configured in {"edge_tts", "gtts"}:
            premium_order = ["elevenlabs", "edenai", "openai_tts"]
        unique: List[str] = []
        for item in premium_order:
            if item not in unique:
                unique.append(item)
        return unique

    def _looks_like_voice_id(self, value: Optional[str]) -> bool:
        raw = str(value or "").strip()
        return bool(raw) and raw.isalnum() and len(raw) >= 10

    def _normalize_voice_choice(self, value: Optional[str]) -> str:
        return str(value or "").strip()

    def _is_automatic_voice_choice(self, value: Optional[str]) -> bool:
        raw = self._normalize_voice_choice(value).lower()
        return raw in {"", "auto", "automatic", "automatica", "automático", "automatico", "padrão", "padrao"}

    def _is_custom_elevenlabs_voice_choice(self, value: Optional[str]) -> bool:
        raw = self._normalize_voice_choice(value)
        lowered = raw.lower()
        custom_voice_id = str(self.elevenlabs_voice_id or "").strip()
        custom_voice_name = str(self.elevenlabs_voice_name or "").strip().lower()
        aliases = {"my_voice", "myvoice", "minha_voz", "minhavoz", "custom"}
        if custom_voice_name:
            aliases.add(custom_voice_name)
        if custom_voice_id:
            aliases.add(custom_voice_id.lower())
        return lowered in aliases

    def _automatic_voice_hint(
        self,
        voice_style: Optional[str] = None,
        voice_gender: Optional[str] = None,
        preferred_provider: Optional[str] = None,
    ) -> Optional[str]:
        style = str(voice_style or "human").strip().lower()
        gender = str(voice_gender or "female").strip().lower()
        configured_provider = self._normalize_voice_provider(preferred_provider or self.voice_provider)

        if style in ["robotic", "robotica", "robótica"]:
            return None

        if configured_provider == "elevenlabs":
            if style in ["child", "infantil"]:
                return "echo" if gender == "male" else "shimmer"
            if style in ["angelic", "angelical"]:
                return "fable"
            if style in ["soft", "soft_prayer", "soft-relaxing", "suave", "suave_relaxante"]:
                return "echo" if gender == "male" else "nova"
            return "onyx" if gender == "male" else "nova"

        if style in ["child", "infantil"]:
            return "echo" if gender == "male" else "shimmer"
        if style in ["angelic", "angelical"]:
            return "fable"
        if style in ["soft", "soft_prayer", "soft-relaxing", "suave", "suave_relaxante"]:
            return "echo" if gender == "male" else "nova"
        return "onyx" if gender == "male" else "nova"

    def select_tts_voice_hint(
        self,
        voice_style: Optional[str] = None,
        voice_gender: Optional[str] = None,
        preferred_provider: Optional[str] = None,
        explicit_voice: Optional[str] = None,
    ) -> Optional[str]:
        self._load_config()
        explicit = self._normalize_voice_choice(explicit_voice)
        configured_provider = self._normalize_voice_provider(preferred_provider or self.voice_provider)
        configured_default = self._normalize_voice_choice(self.default_voice)

        if explicit and not self._is_automatic_voice_choice(explicit):
            if configured_provider == "elevenlabs" and self._is_custom_elevenlabs_voice_choice(explicit):
                return "my_voice"
            return explicit

        if configured_default and not self._is_automatic_voice_choice(configured_default):
            if configured_provider == "elevenlabs" and self._is_custom_elevenlabs_voice_choice(configured_default):
                return "my_voice"
            return configured_default

        return self._automatic_voice_hint(
            voice_style=voice_style,
            voice_gender=voice_gender,
            preferred_provider=preferred_provider,
        )

    def _resolve_elevenlabs_voice_selection(self, voice_hint: Optional[str]) -> Dict[str, Any]:
        raw_hint = self._normalize_voice_choice(voice_hint)
        hint = raw_hint.lower()
        custom_voice_id = str(self.elevenlabs_voice_id or "").strip()
        custom_voice_name = str(self.elevenlabs_voice_name or "").strip() or None
        env_voice_male = os.getenv("ELEVENLABS_VOICE_ID_MALE", "").strip()
        env_voice_female = os.getenv("ELEVENLABS_VOICE_ID_FEMALE", "").strip()
        env_voice_default = os.getenv("ELEVENLABS_VOICE_ID", "").strip()

        voice_map = {
            "nova": env_voice_female or "EXAVITQu4vr4xnSDxMaL",
            "shimmer": env_voice_female or "EXAVITQu4vr4xnSDxMaL",
            "onyx": env_voice_male or "VR6AewLTigWG4xSOukaG",
            "echo": env_voice_male or "VR6AewLTigWG4xSOukaG",
            "fable": env_voice_female or "EXAVITQu4vr4xnSDxMaL",
        }

        explicit_voice_id = ""
        if raw_hint.startswith("voice_id:"):
            explicit_voice_id = raw_hint.split(":", 1)[1].strip()
        elif raw_hint.startswith("elevenlabs:"):
            explicit_voice_id = raw_hint.split(":", 1)[1].strip()
        elif self._looks_like_voice_id(raw_hint):
            explicit_voice_id = raw_hint

        if explicit_voice_id:
            return {
                "requested_voice_hint": raw_hint or None,
                "effective_voice_hint": "explicit_voice_id",
                "voice_id_used": explicit_voice_id,
                "voice_name_used": custom_voice_name if explicit_voice_id == custom_voice_id else None,
                "voice_selection_source": "request_explicit_voice_id",
            }

        if self._is_custom_elevenlabs_voice_choice(raw_hint) and custom_voice_id:
            return {
                "requested_voice_hint": raw_hint or None,
                "effective_voice_hint": "my_voice",
                "voice_id_used": custom_voice_id,
                "voice_name_used": custom_voice_name,
                "voice_selection_source": "settings_elevenlabs_voice_id",
            }

        resolved_voice_id = env_voice_default or voice_map.get(hint, env_voice_female or "EXAVITQu4vr4xnSDxMaL")
        return {
            "requested_voice_hint": raw_hint or None,
            "effective_voice_hint": hint or "nova",
            "voice_id_used": resolved_voice_id or None,
            "voice_name_used": None,
            "voice_selection_source": "env_or_provider_default",
        }

    def _generate_audio_openai_tts(self, text: str, voice_hint: str = "nova", voice_settings: Optional[Dict[str, Any]] = None):
        if not self.api_key or not text or not text.strip():
            return None
        try:
            voice = (voice_hint or self.default_voice or "nova").strip() or "nova"
            if voice.lower() in {"my_voice", "myvoice", "minha_voz", "minhavoz"}:
                voice = "nova"
            model = (os.getenv("OPENAI_TTS_MODEL") or "gpt-4o-mini-tts").strip() or "gpt-4o-mini-tts"
            client = openai.OpenAI(api_key=(self.api_key or "").strip(), timeout=180.0)
            response = client.audio.speech.create(
                model=model,
                voice=voice,
                input=text[:4096],
            )
            if hasattr(response, "read"):
                data = response.read()
            else:
                data = getattr(response, "content", None)
            if isinstance(data, (bytes, bytearray)) and data:
                return bytes(data)
            return None
        except Exception as e:
            print(f"OpenAI TTS error: {e}")
            return None

    def generate_audio_with_diagnostics(self, text, voice="onyx", voice_settings: Optional[Dict[str, Any]] = None, preferred_provider: Optional[str] = None) -> Dict[str, Any]:
        self._load_config()
        configured_provider = self._normalize_voice_provider(preferred_provider or self.voice_provider)
        attempts: List[Dict[str, Any]] = []
        provider_order = self._tts_provider_order(preferred_provider=preferred_provider)
        diagnostics: Dict[str, Any] = {
            "configured_provider": configured_provider,
            "provider_order": provider_order,
            "provider_used": None,
            "fallback_used": False,
            "attempts": attempts,
            "audio_content": None,
            "default_language": self.default_language,
            "default_voice": self.default_voice,
            "requested_voice_hint": str(voice or "").strip() or None,
            "effective_voice_hint": str(voice or "").strip() or None,
            "voice_id_used": None,
            "voice_name_used": None,
            "voice_selection_source": None,
        }

        def _add_attempt(provider: str, status: str, reason: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
            item: Dict[str, Any] = {"provider": provider, "status": status}
            if reason:
                item["reason"] = str(reason)[:500]
            if details:
                item["details"] = details
            attempts.append(item)

        providers = {
            "edenai": {
                "available": bool((self.edenai_key or "").strip()),
                "reason": "edenai_api_key ausente nas configuracoes centrais e no ambiente.",
                "fn": self._generate_audio_edenai_elevenlabs,
            },
            "elevenlabs": {
                "available": bool((self.elevenlabs_key or "").strip()),
                "reason": "elevenlabs_api_key ausente nas configuracoes centrais e no ambiente.",
                "fn": self._generate_audio_elevenlabs,
            },
            "openai_tts": {
                "available": bool((self.api_key or "").strip()),
                "reason": "openai_api_key ausente nas configuracoes centrais e no ambiente.",
                "fn": self._generate_audio_openai_tts,
            },
        }

        if configured_provider not in providers and configured_provider not in {"edge_tts", "gtts"}:
            _add_attempt(configured_provider, "skipped", "Provider configurado nao e suportado pelo pipeline premium atual.")

        if configured_provider in {"edge_tts", "gtts"}:
            _add_attempt(configured_provider, "skipped", "Provider configurado nao e premium; providers premium serao tentados primeiro.")

        for idx, provider in enumerate(provider_order):
            provider_meta = providers.get(provider)
            if not provider_meta:
                _add_attempt(provider, "skipped", "Provider nao mapeado para TTS premium.")
                continue
            if not provider_meta["available"]:
                _add_attempt(provider, "skipped", provider_meta["reason"])
                continue
            try:
                provider_voice_meta: Dict[str, Any] = {}
                if provider == "elevenlabs":
                    provider_voice_meta = self._resolve_elevenlabs_voice_selection(voice)
                elif provider == "openai_tts":
                    provider_voice_meta = {
                        "requested_voice_hint": str(voice or "").strip() or None,
                        "effective_voice_hint": str((voice or self.default_voice or "nova")).strip() or "nova",
                        "voice_id_used": None,
                        "voice_name_used": str((voice or self.default_voice or "nova")).strip() or "nova",
                        "voice_selection_source": "request_or_provider_default",
                    }
                audio_content = provider_meta["fn"](text, voice, voice_settings=voice_settings)
                if audio_content:
                    diagnostics["provider_used"] = provider
                    diagnostics["fallback_used"] = bool(idx > 0 or provider != configured_provider)
                    diagnostics["audio_content"] = audio_content
                    for key, value in provider_voice_meta.items():
                        diagnostics[key] = value
                    _add_attempt(provider, "success", "Audio gerado com sucesso.", provider_voice_meta or None)
                    return diagnostics
                _add_attempt(provider, "failed", "Provider retornou resposta vazia.")
            except Exception as e:
                _add_attempt(provider, "failed", str(e))

        diagnostics["error_summary"] = "Nenhum provider premium conseguiu gerar audio."
        return diagnostics

    def _has_text_provider(self) -> bool:
        return bool((self.openrouter_key or "").strip() or (self.api_key or "").strip())

    def _generate_text(self, prompt, system_prompt=None, temperature=0.7, json_mode=False):
        """Gera texto via OpenRouter com fallback para OpenAI direto."""
        self._load_config()
        if not self._has_text_provider():
            return "{}" if json_mode else "Conteúdo gerado por IA (Simulação - Sem Chave)"

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        class _ProviderHTTPError(Exception):
            def __init__(self, status_code: int, target_url: str, body_preview: str = "", model_id: str = ""):
                self.status_code = int(status_code or 0)
                self.target_url = str(target_url or "")
                self.body_preview = str(body_preview or "")
                self.model_id = str(model_id or "")
                super().__init__(f"HTTP {self.status_code} em {self.target_url}: {self.body_preview}".strip())

        def _error_status_code(exc: Exception) -> Optional[int]:
            status = getattr(exc, "status_code", None)
            if status is None:
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
            try:
                return int(status) if status is not None else None
            except Exception:
                return None

        def _is_retryable_status(status_code: Optional[int]) -> bool:
            return int(status_code or 0) in {429, 502, 503, 504}

        def _retry_delay_seconds(attempt_number: int) -> int:
            return 2 if int(attempt_number or 1) <= 1 else 3

        def _call_with_retry(call_fn):
            max_attempts = 3
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    text = call_fn()
                    return text
                except Exception as exc:
                    last_exc = exc
                    status_code = _error_status_code(exc)
                    retryable = _is_retryable_status(status_code)
                    if retryable and attempt < max_attempts:
                        delay_seconds = _retry_delay_seconds(attempt)
                        time.sleep(delay_seconds)
                        continue
                    raise last_exc

        def _http_chat(url: str, api_key: str, extra_headers: Optional[Dict[str, str]], model_id: str, allow_json_mode: bool) -> str:
            hdrs = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            if isinstance(extra_headers, dict):
                for k, v in extra_headers.items():
                    if isinstance(k, str) and k and isinstance(v, str) and v:
                        hdrs[k] = v
            payload: Dict[str, Any] = {
                "model": model_id,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": 4096,
            }
            if allow_json_mode:
                payload["response_format"] = {"type": "json_object"}
            r = requests.post(url, json=payload, headers=hdrs, timeout=180)
            if int(getattr(r, "status_code", 0) or 0) >= 400:
                body = ""
                try:
                    body = (r.text or "")[:900]
                except Exception:
                    body = ""
                body = " ".join(str(body).split())
                raise _ProviderHTTPError(r.status_code, url, body_preview=body, model_id=model_id)
            try:
                data = r.json()
            except Exception:
                raw = ""
                try:
                    raw = (r.text or "")[:900]
                except Exception:
                    raw = ""
                raw = " ".join(str(raw).split())
                raise Exception(f"Resposta não-JSON em {url}: {raw}".strip())
            text = ""
            try:
                choices = data.get("choices") if isinstance(data, dict) else None
                if isinstance(choices, list) and choices:
                    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
                    if isinstance(msg, dict):
                        text = msg.get("content") or ""
            except Exception:
                text = ""
            text = str(text or "").strip()
            if not text:
                raise Exception(f"Resposta vazia do modelo {model_id}")
            return text

        def _extract_content(response) -> str:
            try:
                content = response.choices[0].message.content
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = []
                    for item in content:
                        if isinstance(item, dict):
                            txt = item.get("text")
                            if isinstance(txt, str) and txt.strip():
                                parts.append(txt.strip())
                    return "\n".join(parts).strip()
            except Exception:
                pass
            return ""

        def _call_chat(client, model_id: str, allow_json_mode: bool):
            kwargs = {
                "model": model_id,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": 4096,
            }
            if allow_json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            response = client.chat.completions.create(**kwargs)
            text = _extract_content(response)
            if not str(text or "").strip():
                raise Exception(f"Resposta vazia do modelo {model_id}")
            return text

        errors = []
        raw_model = (self.openrouter_model or "").strip()
        raw_model_norm = raw_model.lower()

        def _try_openrouter() -> Optional[str]:
            if not (self.openrouter_key or "").strip():
                return None
            or_client = None
            try:
                or_client = openai.OpenAI(
                    api_key=self.openrouter_key,
                    base_url="https://openrouter.ai/api/v1",
                    default_headers={"HTTP-Referer": "https://codexia.com", "X-Title": "Codexia"},
                    timeout=180.0,
                )
            except TypeError:
                try:
                    or_client = openai.OpenAI(api_key=self.openrouter_key, base_url="https://openrouter.ai/api/v1")
                except Exception as e:
                    errors.append(f"OpenRouter[client]: {e}")
                    or_client = None
            except Exception as e:
                errors.append(f"OpenRouter[client]: {e}")
                or_client = None
            candidate_models = []
            if raw_model and raw_model_norm not in {"auto", "automático", "automatico", "melhor", "best", "openrouter/auto"}:
                candidate_models.append(raw_model)
            candidate_models.extend(["openai/gpt-4o-mini", "openrouter/auto"])
            seen = set()
            http_headers = {"HTTP-Referer": "https://codexia.com", "X-Title": "Codexia"}
            for model_id in candidate_models:
                if not model_id or model_id in seen:
                    continue
                seen.add(model_id)
                try:
                    try:
                        if or_client is not None:
                            text = _call_with_retry(
                                lambda: _call_chat(or_client, model_id, allow_json_mode=bool(json_mode)),
                            )
                        else:
                            text = _call_with_retry(
                                lambda: _http_chat(
                                    "https://openrouter.ai/api/v1/chat/completions",
                                    self.openrouter_key,
                                    http_headers,
                                    model_id,
                                    allow_json_mode=bool(json_mode),
                                ),
                            )
                        return text
                    except Exception:
                        if or_client is not None:
                            text = _call_with_retry(
                                lambda: _call_chat(or_client, model_id, allow_json_mode=False),
                            )
                        else:
                            text = _call_with_retry(
                                lambda: _http_chat(
                                    "https://openrouter.ai/api/v1/chat/completions",
                                    self.openrouter_key,
                                    http_headers,
                                    model_id,
                                    allow_json_mode=False,
                                ),
                            )
                        return text
                except Exception as e:
                    errors.append(f"OpenRouter[{model_id}]: {e}")
            return None

        def _try_openai() -> Optional[str]:
            if not (self.api_key or "").strip():
                return None
            oa_client = None
            try:
                oa_client = openai.OpenAI(api_key=self.api_key, timeout=180.0)
            except TypeError:
                try:
                    oa_client = openai.OpenAI(api_key=self.api_key)
                except Exception as e:
                    errors.append(f"OpenAI[client]: {e}")
                    oa_client = None
            except Exception as e:
                errors.append(f"OpenAI[client]: {e}")
                oa_client = None
            preferred = (os.getenv("OPENAI_TEXT_MODEL") or "").strip()
            candidate_models = [m for m in [preferred, "gpt-4o-mini", "gpt-4.1-mini", "gpt-4o", "gpt-4.1"] if m]
            seen = set()
            for model_id in candidate_models:
                if not model_id or model_id in seen:
                    continue
                seen.add(model_id)
                try:
                    try:
                        if oa_client is not None:
                            text = _call_with_retry(
                                lambda: _call_chat(oa_client, model_id, allow_json_mode=bool(json_mode)),
                            )
                        else:
                            text = _call_with_retry(
                                lambda: _http_chat(
                                    "https://api.openai.com/v1/chat/completions",
                                    self.api_key,
                                    None,
                                    model_id,
                                    allow_json_mode=bool(json_mode),
                                ),
                            )
                        return text
                    except Exception:
                        if oa_client is not None:
                            text = _call_with_retry(
                                lambda: _call_chat(oa_client, model_id, allow_json_mode=False),
                            )
                        else:
                            text = _call_with_retry(
                                lambda: _http_chat(
                                    "https://api.openai.com/v1/chat/completions",
                                    self.api_key,
                                    None,
                                    model_id,
                                    allow_json_mode=False,
                                ),
                            )
                        return text
                except Exception as e:
                    errors.append(f"OpenAI[{model_id}]: {e}")
            return None

        prov = (self.provider or "").strip().lower()
        if prov in {"openai"}:
            text = _try_openai()
            if text is not None:
                return text
            text = _try_openrouter()
            if text is not None:
                return text
        elif prov in {"openrouter"}:
            text = _try_openrouter()
            if text is not None:
                return text
            text = _try_openai()
            if text is not None:
                return text
        else:
            text = _try_openrouter()
            if text is not None:
                return text
            text = _try_openai()
            if text is not None:
                return text

        raise Exception(" | ".join(errors) if errors else "Nenhum provedor de texto disponível.")

    def generate_book_section(self, section_type, context_text, title, existing_content=None):
        """Generates specific book sections like synopsis, epigraph, preface. Can rewrite existing content."""
        self._load_config()
        # Verify if any key is available
        if not self.openrouter_key:
             return "Conteúdo gerado por IA (Simulação - Sem Chave)"

        base_prompt = f"Escreva um texto para {section_type} do livro '{title}'. Contexto: {context_text}..."
        
        if existing_content and len(existing_content.strip()) > 50:
            # Rewrite mode
            base_prompt = f"""
            ATUE COMO UM EDITOR E REESCREVA a seção '{section_type}' do livro '{title}'.
            
            CONTEXTO E INSTRUÇÕES:
            {context_text}
            
            CONTEÚDO ORIGINAL (Use como base, mas aplique as instruções acima):
            {existing_content}
            
            IMPORTANTE:
            1. Mantenha a essência do conteúdo original, mas adapte conforme as novas instruções.
            2. Se as instruções pedirem para corrigir algo (ex: número de páginas, referências), FAÇA A CORREÇÃO.
            3. Retorne APENAS o novo texto reescrito.
            """

        prompts = {
            "synopsis": f"Escreva uma sinopse instigante para a quarta capa do livro '{title}'. Baseado neste contexto: {context_text}...",
            "epigraph": f"Sugira uma epígrafe (citação curta e profunda) que combine com o tema do livro '{title}'. Contexto: {context_text}...",
            "preface": f"Escreva um prefácio curto para o livro '{title}', introduzindo o tema e preparando o leitor. Contexto: {context_text}...",
            "dedication": f"Sugira uma dedicatória genérica e emocionante para o livro '{title}'.",
            "introduction": f"Escreva uma introdução envolvente para o livro '{title}', apresentando os conceitos principais. Contexto: {context_text}...",
            "epilogue": f"Escreva um epílogo conclusivo para o livro '{title}', amarrando as pontas soltas e oferecendo uma reflexão final. Contexto: {context_text}...",
            "conclusion": f"Escreva uma conclusão resumida para o livro '{title}', recapitulando os pontos principais. Contexto: {context_text}...",
            "chapter": f"Escreva o conteúdo completo para o capítulo '{title}'. Mantenha o estilo do livro. Contexto: {context_text}..."
        }
        
        # If rewriting, use base_prompt constructed above. Otherwise use specific prompt from dict or fallback.
        if existing_content and len(existing_content.strip()) > 50:
            prompt = base_prompt
        else:
            prompt = prompts.get(section_type, base_prompt)

        try:
            content = self._generate_text(prompt)
            if not content:
                return "Erro: Nenhuma IA configurada."
            return content
        except Exception as e:
            print(f"Erro ao gerar seção {section_type}: {e}")
            return f"Erro ao gerar {section_type}: {str(e)}"

    def generate_full_book_draft(self, title: str, idea: str, num_chapters: int, style: str = "didático", num_pages: int = 50):
        """Generates a full book structure and content based on an idea"""
        self._load_config()
        
        if not self.openrouter_key:
            # Mock response
            return {
                "dedication": "Aos sonhadores.",
                "acknowledgments": "Agradeço à IA.",
                "introduction": "Esta é uma introdução gerada automaticamente.",
                "preface": "Um prefácio curto.",
                "epigraph": "O conhecimento é poder.",
                "chapters": [
                    {"title": f"Capítulo {i+1}", "content": f"Conteúdo simulado do capítulo {i+1} sobre {idea}..."} 
                    for i in range(num_chapters)
                ],
                "cover_url": "https://placehold.co/400x600?text=Capa+Simulada"
            }

        # Estimate word count based on pages (approx 250-300 words per page)
        total_words = num_pages * 250
        words_per_chapter = max(300, int(total_words / max(1, num_chapters)))

        # 1. Generate Outline
        outline_prompt = f"""
        Atue como um autor best-seller. Crie o planejamento de um livro completo.
        Título: {title}
        Ideia Central: {idea}
        Número de Capítulos: {num_chapters}
        Estimativa de Páginas: {num_pages} (aprox. {total_words} palavras no total)
        Estilo: {style}

        Retorne APENAS um JSON com a seguinte estrutura:
        {{
            "dedication": "Sugestão de dedicatória",
            "epigraph": "Sugestão de epígrafe",
            "chapters": [
                {{"title": "Título do Cap 1", "summary": "Breve resumo do que abordar neste capítulo"}},
                {{"title": "Título do Cap 2", "summary": "Breve resumo do que abordar neste capítulo"}}
            ]
        }}
        """

        try:
            import json
            
            # Using unified generator
            content = self._generate_text(outline_prompt, json_mode=True)
            if not content:
                 raise Exception("Falha na geração do outline (resposta vazia)")
                 
            content = content.replace("```json", "").replace("```", "").strip()
            structure = json.loads(content)
            
            # 2. Generate Cover (Parallel if possible, but sequential here for simplicity)
            # We generate 1 suggestion
            try:
                cover_urls = self.generate_cover_options(title, idea, n=1)
                structure["cover_url"] = cover_urls[0] if cover_urls else None
            except Exception as e:
                print(f"Erro ao gerar capa: {e}")
                structure["cover_url"] = None

            # 3. Generate Content for each chapter
            # Note: For a real production app, this should be done in background or streamed.
            # Here we do it sequentially but keep it concise to avoid timeout.
            final_chapters = []
            
            for i, chap in enumerate(structure.get("chapters", [])):
                chap_title = chap.get("title", f"Capítulo {i+1}")
                chap_summary = chap.get("summary", "")
                
                content_prompt = f"""
                Escreva o conteúdo completo do Capítulo {i+1} de {len(structure.get("chapters", []))}: '{chap_title}' do livro '{title}'.
                Contexto do capítulo: {chap_summary}
                Estilo: {style}
                Meta de tamanho: Aprox. {words_per_chapter} palavras.
                
                IMPORTANTE: 
                1. NÃO repita o título "Capítulo {i+1}" ou o nome do capítulo no início do texto. Comece diretamente o conteúdo.
                2. Mantenha a coerência com os capítulos anteriores e posteriores.
                3. Escreva de forma envolvente, detalhada e bem estruturada. Use parágrafos claros.
                """
                
                chap_content = self._generate_text(content_prompt)
                
                final_chapters.append({
                    "title": chap_title,
                    "content": chap_content or "Conteúdo não gerado."
                })
            
            structure["chapters"] = final_chapters
            
            # Fill other sections if missing
            if "introduction" not in structure:
                structure["introduction"] = self.generate_book_section("introduction", idea, title)
            if "preface" not in structure:
                structure["preface"] = self.generate_book_section("preface", idea, title)
            if "acknowledgments" not in structure:
                structure["acknowledgments"] = self.generate_book_section("acknowledgments", idea, title)

            return structure

        except Exception as e:
            error_msg = str(e)
            print(f"Erro ao gerar livro: {error_msg}")
            
            # Tratamento amigável para erro de cota
            if "insufficient_quota" in error_msg or "429" in error_msg:
                raise Exception(
                    "Créditos da IA esgotados. Verifique sua cota na OpenAI ou Gemini."
                )
            
            raise e

    def analyze_manuscript_structure(self, text_sample):
        """Analyzes text to identify potential structure (chapters) and extracts content"""
        self._load_config()
        
        import re
        
        # Structure to hold results
        structure = {
            "dedication": "",
            "acknowledgments": "",
            "introduction": "",
            "preface": "",
            "epigraph": "",
            "chapters": []
        }

        # Regex patterns for section headers
        # Order matters: check for specific sections first
        patterns = [
            (r'(?i)^(?:dedicatória|dedication)\s*$', 'dedication'),
            (r'(?i)^(?:agradecimentos|acknowledgments)\s*$', 'acknowledgments'),
            (r'(?i)^(?:introdução|introduction)\s*$', 'introduction'),
            (r'(?i)^(?:prefácio|preface)\s*$', 'preface'),
            (r'(?i)^(?:epígrafe|epigraph)\s*$', 'epigraph'),
            # Broadest chapter matching:
            # 1. "Capítulo 1" or "Chapter 1" (standard), allowing leading symbols like emojis/bullets
            (r'(?i)^[\W_]*(?:cap[ií]tulo|chapter)\s+([0-9IVX]+)(?:[\s:-]+(.*))?', 'chapter')
        ]
        
        lines = text_sample.split('\n')
        
        current_section_type = None 
        current_content = []
        current_title = ""
        
        def save_section():
            nonlocal current_section_type, current_content, current_title
            
            content_str = "\n".join(current_content).strip()
            if not content_str:
                return

            if current_section_type == 'chapter':
                structure['chapters'].append({
                    "title": current_title,
                    "content": content_str
                })
            elif current_section_type in structure:
                structure[current_section_type] = content_str
            
            # Reset content but keep type until new header found (actually type resets on new header)
        
        skip_next = False
        
        for i, line in enumerate(lines):
            if skip_next:
                skip_next = False
                continue
                
            line = line.strip()
            
            # Check for header
            is_header = False
            
            # Skip very long lines for header check (headers are usually short)
            if len(line) < 100 and line:
                for pattern, type_name in patterns:
                    match = re.match(pattern, line)
                    if match:
                        # Found a new header!
                        # 1. Save previous section
                        save_section()
                        
                        # 2. Start new section
                        current_section_type = type_name
                        current_content = []
                        is_header = True
                        
                        if type_name == 'chapter':
                            # Extract chapter title
                            chap_num = match.group(1)
                            title_suffix = match.group(2) if match.lastindex >= 2 else ""
                            
                            if title_suffix and title_suffix.strip():
                                clean_suffix = title_suffix.strip().lstrip(":-").strip()
                                current_title = f"Capítulo {chap_num}: {clean_suffix}"
                            elif i + 1 < len(lines) and len(lines[i+1].strip()) < 100 and lines[i+1].strip():
                                # Check next line for title
                                current_title = f"Capítulo {chap_num}: {lines[i+1].strip()}"
                                skip_next = True # Consume next line as title
                            else:
                                current_title = f"Capítulo {chap_num}"
                        else:
                            current_title = line.title()
                        
                        break
            
            if not is_header:
                current_content.append(line)
        
        # Save last section
        save_section()
        
        # Fallback: if no chapters found but we have content
        if not structure['chapters'] and not any([structure[k] for k in structure if k != 'chapters']):
             # If completely failed to find structure, return whole text as chapter 1
             structure['chapters'].append({"title": "Conteúdo Completo", "content": text_sample})

        return structure


    def generate_ad_copy(self, book_title: str, synopsis: str, style: str = "cliffhanger"):
        # Recarrega config a cada chamada para pegar atualizações
        self._load_config()

        if not self.openrouter_key:
            return self._mock_response(book_title, style)

        prompt = self._build_prompt(book_title, synopsis, style)
        
        try:
            return self._generate_text(prompt, system_prompt="Você é um especialista em copywriting para venda de livros. Crie textos persuasivos, emocionantes e com alto potencial de conversão.") or "Erro na geração."
        except Exception as e:
            print(f"Erro na IA: {e}")
            return self._mock_response(book_title, style, error=str(e))

    def generate_cover_options(self, title: str, context: str, author: str = "", subtitle: str = "", n: int = 3):
        self._load_config()

        print(f"DEBUG: Generating covers for '{title}' with context: {context[:100]}...")
        if not self.edenai_key:
            colors = ["1e293b", "4f46e5", "059669"]
            return [f"https://placehold.co/400x600/{color}/ffffff?text={title[:10]}...%0A{author}" for i, color in enumerate(colors[:n])]

        import json

        title_display = title.strip() if title else "Livro"
        author_display = author.strip() if author else ""
        subtitle_display = subtitle.strip() if subtitle else ""

        prompt_gen_prompt = f"""
        Crie {n} descrições visuais artísticas e EXCLUSIVAS para a capa do livro '{title_display}'.
        Contexto/Mensagem Central: {context[:500]}

        Retorne APENAS um JSON:
        {{
          "prompts": ["descrição 1", "descrição 2", "descrição 3"]
        }}
        """

        try:
            raw = self._generate_text(prompt_gen_prompt, json_mode=True) or "{}"
            raw = raw.replace("```json", "").replace("```", "").strip()
            prompts_data = json.loads(raw) if raw else {}
            prompts = prompts_data.get("prompts", []) if isinstance(prompts_data, dict) else []
        except Exception as e:
            print(f"Error generating cover prompts: {e}")
            prompts = []

        while len(prompts) < n:
            prompts.append(f"Ilustração conceitual para a capa do livro, tema: {context[:120]}")

        def edenai_image_url(text_prompt: str, resolution: str):
            url = "https://api.edenai.run/v2/image/generation/"
            headers = {"Authorization": f"Bearer {self.edenai_key}", "Content-Type": "application/json"}
            payload = {
                "providers": "openai/dall-e-3,stabilityai/stable-diffusion-xl-1024-v1-0",
                "text": text_prompt,
                "resolution": resolution,
                "num_images": 1,
                "response_as_dict": True,
                "attributes_as_list": False,
            }
            r = requests.post(url, headers=headers, json=payload, timeout=120)
            if r.status_code >= 400:
                raise Exception(f"Eden AI HTTP {r.status_code}: {r.text[:240]}")
            data = r.json() or {}
            for provider in ["openai", "stabilityai"]:
                provider_payload = data.get(provider) or {}
                items = provider_payload.get("items") or []
                if items and isinstance(items[0], dict):
                    item0 = items[0]
                    for k in ["image_resource_url", "image_url", "url", "image"]:
                        v = item0.get(k)
                        if isinstance(v, str) and v.strip().startswith("http"):
                            return v.strip()
            return None

        image_urls = []
        for p in prompts[:n]:
            cover_prompt = (
                f"Capa de livro, arte digital 2D plana, sem mockup 3D. "
                f"Título: \"{title_display}\". "
                + (f"Subtítulo: \"{subtitle_display}\". " if subtitle_display else "")
                + (f"Autor: \"{author_display}\". " if author_display else "")
                + f"Descrição visual: {p}. "
                "Sem marcas d'água, sem texto extra."
            )
            try:
                url = edenai_image_url(cover_prompt, resolution="1024x1792")
                image_urls.append(url or "https://placehold.co/400x600?text=Capa+Indispon%C3%ADvel")
            except Exception as e_img:
                print(f"Error generating cover image: {e_img}")
                image_urls.append("https://placehold.co/400x600?text=Cover+Error")

        while len(image_urls) < n:
            image_urls.append("https://placehold.co/400x600?text=Cover+Error")

        return image_urls

    def generate_music_placeholder(self, prompt: str):
        """Gera música a partir de um prompt (Placeholder)"""
        # Implementação futura com MusicGen/HuggingFace
        print(f"Solicitação de música recebida: {prompt}")
        return None

    def generate_video_script(self, book_title: str, synopsis: str, style: str = "drama"):
        self._load_config()
        
        # Se não tiver chave, retorna mock
        if not self.openrouter_key:
            return {
                "title": f"Trailer: {book_title}",
                "scenes": [
                    {"text": f"Conheça a história de {book_title}", "image_prompt": "capa do livro misteriosa"},
                    {"text": "Um segredo que pode mudar tudo...", "image_prompt": "pessoa olhando para o horizonte com suspense"},
                    {"text": "Disponível agora!", "image_prompt": "livro em cima de uma mesa de madeira"}
                ],
                "music_mood": style
            }

        prompt = f"""
        Crie um Roteiro de Vídeo Curto (TikTok/Reels) para o livro '{book_title}'.
        Sinopse: '{synopsis}'.
        Estilo: {style}.
        
        Retorne APENAS um JSON válido com a seguinte estrutura, sem explicações adicionais:
        {{
            "title": "Título do Vídeo",
            "scenes": [
                {{"text": "Frase narrada da cena 1", "image_prompt": "Descrição visual artística e altamente detalhada da cena 1 em inglês, focada em criar uma ilustração digital única e original, sem texto na imagem"}},
                {{"text": "Frase narrada da cena 2", "image_prompt": "Descrição visual artística e altamente detalhada da cena 2 em inglês, focada em criar uma ilustração digital única e original, sem texto na imagem"}}
            ],
            "music_mood": "{style}"
        }}
        Máximo de 4 cenas.
        """

        try:
            content = self._generate_text(
                prompt, 
                system_prompt="Você é um roteirista de vídeo especialista em trailers de livros. Retorne apenas JSON.",
                json_mode=True
            )
            
            import json
            if not content:
                 raise Exception("Resposta vazia da IA")

            # Tenta limpar markdown se houver
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
                
            return json.loads(content.strip())
        except Exception as e:
            print(f"Erro ao gerar script de vídeo: {e}")
            return {
                "title": f"Trailer: {book_title}",
                "scenes": [
                    {"text": f"Descubra {book_title}", "image_prompt": "book cover artistic"},
                    {"text": "Uma história incrível espera por você", "image_prompt": "fantasy world landscape"},
                    {"text": "Leia agora!", "image_prompt": "person reading a book happily"}
                ],
                "music_mood": style
            }

    def generate_short_script_from_prompt(self, prompt: str):
        """Gera roteiro de YouTube Short (vertical, ~30-60s) a partir de um único prompt."""
        self._load_config()
        if not self.openrouter_key:
            return {
                "title": "Short gerado",
                "description": "Um short criado automaticamente com base na mensagem do vídeo. #shorts",
                "scenes": [
                    {"text": "Um momento que inspira.", "image_prompt": "cinematic inspiring scene"},
                    {"text": "Vale a pena persistir.", "image_prompt": "person overcoming challenge"},
                    {"text": "Inscreva-se para mais!", "image_prompt": "call to action minimal"}
                ],
                "music_mood": "drama"
            }
        system = (
            "Você é um roteirista de YouTube Shorts e Reels. Crie roteiros curtos, impactantes, "
            "com frases de efeito. Cada cena deve ter 1-2 frases no máximo (5-15 segundos de fala). "
            "Retorne APENAS um JSON válido, sem explicações."
        )
        user_prompt = f"""
        Crie um roteiro de YouTube Short (vídeo vertical, 30-60 segundos no total) com base neste pedido:

        "{prompt}"

        Regras:
        - Título: uma frase chamativa (máx. 60 caracteres).
        - Descrição: 2-4 linhas com CTA e hashtags relevantes (inclua #shorts).
        - Cenas: entre 3 e 5 cenas. Cada cena: "text" (frase narrada, curta) e "image_prompt" (descrição visual em inglês para gerar imagem com IA, sem texto na imagem).
        - Estilo: dinâmico, adequado para Shorts/Reels, gancho no início.

        Retorne APENAS este JSON (sem markdown, sem texto extra):
        {{
            "title": "Título do Short",
            "description": "Descrição do short com hashtags",
            "scenes": [
                {{"text": "Frase da cena 1", "image_prompt": "descrição visual artística da cena 1"}},
                {{"text": "Frase da cena 2", "image_prompt": "descrição visual artística da cena 2"}}
            ],
            "music_mood": "drama"
        }}
        """
        try:
            content = self._generate_text(
                user_prompt,
                system_prompt=system,
                json_mode=True
            )
            if not content:
                raise Exception("Resposta vazia da IA")
            import json
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
            data = json.loads(content.strip())
            if not data.get("scenes"):
                data["scenes"] = [
                    {"text": "Um momento inspirador.", "image_prompt": "cinematic inspiring scene"},
                    {"text": "Persista e conquiste.", "image_prompt": "person overcoming challenge"}
                ]
            desc = (data.get("description") or "").strip()
            if not desc:
                try:
                    desc_prompt = (
                        f"Crie uma descrição curta (2-4 linhas) para um YouTube Short com base neste pedido:\n\n"
                        f"\"{prompt}\"\n\n"
                        "Regras: inclua CTA (inscreva-se/curta/compartilhe), inclua hashtags relevantes e #shorts. "
                        "Retorne apenas o texto da descrição (sem aspas, sem markdown)."
                    )
                    gen_desc = (self._generate_text(desc_prompt, system_prompt="Você é um copywriter de YouTube. Retorne só o texto.", json_mode=False) or "").strip()
                    if gen_desc:
                        data["description"] = gen_desc[:1200]
                except Exception:
                    pass
            return data
        except Exception as e:
            print(f"Erro ao gerar script de Short: {e}")
            return {
                "title": "Short inspirador",
                "description": "Um short criado automaticamente com base na mensagem do vídeo. #shorts",
                "scenes": [
                    {"text": "Um momento que inspira.", "image_prompt": "cinematic inspiring scene"},
                    {"text": "Vale a pena persistir.", "image_prompt": "person overcoming challenge"},
                    {"text": "Inscreva-se para mais!", "image_prompt": "call to action minimal"}
                ],
                "music_mood": "drama"
            }

    def generate_motivational_script(self, topic, duration_minutes=5):
        """Gera um roteiro longo para vídeo motivacional"""
        self._load_config()
        if not self.openrouter_key:
            return self._mock_response(topic, "motivational_long", duration=duration_minutes)

        # Estimate word count: approx 150 words per minute
        target_word_count = duration_minutes * 150
        min_word_count = max(120, int(duration_minutes * 135))
        max_word_count = max(min_word_count + 80, int(duration_minutes * 165))
        min_scenes = max(5, duration_minutes * 2) # At least 2 scenes per minute
        niche = (os.getenv("YOUTUBE_NICHE") or os.getenv("CHANNEL_NICHE") or os.getenv("CONTENT_NICHE") or "").strip()
        if not niche:
            niche = "reflexão, espiritualidade e mensagens cristãs (sem sensacionalismo falso)"

        prompt = f"""
        Crie um Roteiro de Vídeo Motivacional Profundo de {duration_minutes} minutos sobre '{topic}'.
        Nicho do canal: {niche}.
        Estilo: Inspirador, profundo, humano, com narrativa poderosa.
        Meta de Palavras: ideal em torno de {target_word_count} palavras.
        Faixa aceitável: entre {min_word_count} e {max_word_count} palavras.
        
        O roteiro deve ser estruturado para manter a retenção e COBRIR O TEMPO SOLICITADO.
        Divida em pelo menos {min_scenes} cenas/partes para garantir dinamismo.
        IMPORTANTE: não entregue um roteiro curto. Se ficar abaixo de {min_word_count} palavras, o vídeo não alcançará os {duration_minutes} minutos pedidos.
        Estrutura sugerida: Gancho (0-30s), Problema (dor), Virada, Desenvolvimento (longo), Aplicação prática, Conclusão/CTA.

        DIRETRIZES IMPORTANTES (retenção e engajamento):
        - Primeiros 30 segundos: vá direto na dor/sentimento do espectador (ex: "Se você se sente cansado hoje, esta mensagem é para você...").
        - Evite introduções longas. Sem vinhetas, sem "bem-vindo ao canal" no início.
        - Ritmo: crie quebras de padrão frequentes via legendas curtas e mudanças visuais (ângulo/luz/ambiente). Use uma linguagem visual variada por cena.
        - CTA: inclua uma pergunta direta para comentários no meio ou no final (ex: "Qual parte falou mais com você hoje?").
        - Título: misture emoção + o que as pessoas buscam. Pode ser poético, mas deve conter um termo pesquisável e opcional "(Reflexão)".
        
        Retorne APENAS um JSON válido com a estrutura:
        {{
            "title": "Título Impactante (SEO + emoção)",
            "description": "Descrição otimizada para YouTube com hashtags",
            "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
            "scenes": [
                {{
                    "text": "Texto EXATO da narração (sem 'Cena 1:', sem 'Narrador:').",
                    "caption": "Legenda curta e impactante (até 8 palavras).",
                    "image_prompt": "Descrição visual em inglês, fotorealista e cinematográfica, sem texto na imagem, variando ângulo/ambiente/iluminação de cena para cena."
                }},
                ...
            ],
            "music_mood": "epic_cinematic"
        }}
        """
        
        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um roteirista de vídeos motivacionais virais. Seus roteiros são longos, profundos e respeitam o tempo solicitado.",
                temperature=0.8,
                json_mode=True
            )
            
            import json
            if not content:
                raise Exception("Resposta vazia da IA")

            # Limpeza básica de markdown json
            content = content.replace("```json", "").replace("```", "")
            return json.loads(content)
        except Exception as e:
            print(f"Erro ao gerar roteiro motivacional: {e}")
            return self._mock_response(topic, "motivational_long", error=str(e), duration=duration_minutes)

    def generate_script_from_text(self, text, duration_minutes=5):
        """Estrutura um texto existente em formato de roteiro de vídeo"""
        self._load_config()
        if not self.openrouter_key:
            return self._mock_response("História do Usuário", "motivational_long")
        niche = (os.getenv("YOUTUBE_NICHE") or os.getenv("CHANNEL_NICHE") or os.getenv("CONTENT_NICHE") or "").strip()
        if not niche:
            niche = "reflexão, espiritualidade e mensagens cristãs (sem sensacionalismo falso)"

        prompt = f"""
        Atue como um Editor de Vídeo Profissional.
        Nicho do canal: {niche}.
        Eu tenho uma história/texto pronto e quero transformá-lo em um vídeo narrado de aproximadamente {duration_minutes} minutos.
        
        TEXTO ORIGINAL:
        "{text}"
        
        Sua tarefa:
        1. Reestruture o começo para ter um gancho forte nos primeiros 30 segundos (direto na dor/sentimento), mantendo a mensagem do texto.
        2. Divida este texto em cenas lógicas para narração. MANTENHA O SENTIDO ORIGINAL E A MAIORIA DO TEXTO; ajuste apenas para fluidez e retenção.
        3. Para cada cena, crie:
           - 'caption' (legenda curta e dinâmica, até 8 palavras)
           - 'image_prompt' visual, artístico e detalhado em inglês (sem texto na imagem), variando ângulo/ambiente/iluminação a cada cena.
        3. Defina um título e descrição para o YouTube.
        4. Inclua CTA com pergunta direta para comentários no meio ou no final.
        
        Retorne APENAS um JSON válido com a estrutura:
        {{
            "title": "Título Sugerido",
            "description": "Descrição para YouTube",
            "tags": ["tag1", "tag2"],
            "scenes": [
                {{"text": "Trecho da narração da cena 1...", "caption": "Legenda curta...", "image_prompt": "Descrição visual detalhada em inglês..."}},
                {{"text": "Trecho da narração da cena 2...", "caption": "Legenda curta...", "image_prompt": "Descrição visual detalhada em inglês..."}}
            ],
            "music_mood": "emotional_cinematic"
        }}
        """
        
        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um editor de vídeo profissional. Retorne apenas JSON.",
                temperature=0.7,
                json_mode=True
            )
            
            import json
            if not content:
                 raise Exception("Resposta vazia da IA")

            content = content.replace("```json", "").replace("```", "")
            return json.loads(content)
        except Exception as e:
            print(f"Erro ao estruturar roteiro do texto: {e}")
            return self._mock_response("História do Usuário", "motivational_long", error=str(e))

    def generate_story_or_devotional_text(
        self,
        instruction: str,
        kind: str = "story",
        duration_min_minutes: int = 10,
        duration_max_minutes: Optional[int] = None,
    ) -> str:
        self._load_config()
        if not self._has_text_provider():
            title = "História" if kind == "story" else ("Devocional" if kind == "devotional" else "Reflexão com Oração")
            return f"{title} (Simulação - Sem Chave)\n\n{instruction}".strip()

        kind_norm = (kind or "story").strip().lower()
        if kind_norm not in {"story", "devotional", "prayer"}:
            kind_norm = "story"
        safe_kind = "história" if kind_norm == "story" else ("devocional" if kind_norm == "devotional" else "reflexão com oração")
        min_m = max(1, int(duration_min_minutes or 1))
        max_m = int(duration_max_minutes) if duration_max_minutes else min_m
        if max_m < min_m:
            max_m = min_m

        # Usando 140 palavras por minuto (ritmo de narração calmo e envolvente)
        min_words = min_m * 140
        max_words = max_m * 160
        niche = (os.getenv("YOUTUBE_NICHE") or os.getenv("CHANNEL_NICHE") or os.getenv("CONTENT_NICHE") or "").strip()
        if not niche:
            niche = "reflexão, espiritualidade e mensagens cristãs (sem sensacionalismo falso)"

        extra_guidance = ""
        if kind_norm == "prayer":
            extra_guidance = """
        DIRETRIZES ESPECIAIS DE ORAÇÃO:
        - O texto deve combinar reflexão, consolo, esperança e uma oração guiada natural.
        - O tom deve ser suave, acolhedor, relaxante e espiritual, ajudando a pessoa a desacelerar e encontrar paz.
        - Inclua momentos de respiração, silêncio interior, entrega a Deus, confiança, descanso e tranquilidade.
        - Evite sensacionalismo, medo, condenação dura, culpa excessiva ou linguagem agressiva.
        - Feche com uma oração de paz, proteção e descanso, em linguagem simples e profundamente confortadora.
        """

        prompt = f"""
        Escreva um(a) {safe_kind} ORIGINAL em português (pt-BR), para ser NARRADO em vídeo de longa duração.
        Nicho do canal: {niche}.
        
        IMPORTANTE: O vídeo deve ter no mínimo {min_m} minutos. Para isso, você DEVE escrever um texto longo e detalhado.
        NÃO resuma. Seja descritivo, use exemplos, analogias e aprofunde-se nos detalhes para garantir a extensão necessária.

        INSTRUÇÕES DO USUÁRIO (respeite exatamente):
        {instruction}

        DIRETRIZES DE RETENÇÃO:
        - Comece com um gancho magnético nos primeiros 30 segundos, tocando diretamente na dor/sentimento do espectador.
        - Evite introdução longa, sem vinheta, sem apresentação do canal no início.
        - Inclua pelo menos 2 perguntas diretas ao longo do texto para estimular reflexão e comentários.
        - Finalize com uma CTA clara (curtir/inscrever-se) e uma pergunta curta para comentários.
        {extra_guidance}

        REGRAS DE EXTENSÃO:
        - Objetivo: texto para narração contínua.
        - Duração alvo do vídeo: {min_m} a {max_m} minutos.
        - Tamanho alvo: entre {min_words} e {max_words} palavras.
        - Escreva pelo menos {min_words} palavras. Se o texto for muito curto, o vídeo ficará incompleto.

        ESTILO:
        - Escreva em parágrafos, com ritmo natural e envolvente.
        - Sem marcações técnicas, sem JSON, sem listas, sem títulos de seção.
        - Não escreva "Cena 1" / "Narrador:" / "Roteiro:".

        Retorne APENAS o texto final completo.
        """

        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um escritor e roteirista de narração. Entregue apenas o texto final em português, sem JSON.",
                temperature=0.8,
                json_mode=False,
            )
            if not content:
                raise Exception("Resposta vazia da IA")
            return (self._normalize_narration_text(content) or content).strip()
        except Exception as e:
            print(f"Erro ao gerar {safe_kind}: {e}")
            raise

    def _normalize_narration_text(self, raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            return ""
        if "```" in text:
            t = text
            if "```json" in t:
                try:
                    t = t.split("```json", 1)[1]
                except Exception:
                    t = t
            try:
                t = t.split("```", 1)[1] if t.strip().startswith("```") else t
            except Exception:
                t = t
            try:
                t = t.rsplit("```", 1)[0]
            except Exception:
                t = t
            text = t.strip() or text

        if not (text.startswith("{") or text.startswith("[")):
            return text

        try:
            import json
            data = json.loads(text)
        except Exception:
            return text

        def pick_str(d: dict, keys: list) -> Optional[str]:
            for k in keys:
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return None

        parts: List[str] = []
        if isinstance(data, dict):
            title = pick_str(data, ["title", "titulo"])
            if title:
                parts.append(title)

            script_full = pick_str(data, ["script_full", "script", "content", "text", "narration_text", "narration"])
            if script_full:
                parts.append(script_full)

            sections = data.get("sections")
            if isinstance(sections, list):
                for s in sections:
                    if isinstance(s, dict):
                        seg = pick_str(s, ["content", "text", "narration", "narration_text"])
                        if seg:
                            parts.append(seg)

            scenes = data.get("scenes")
            if isinstance(scenes, list):
                for s in scenes:
                    if isinstance(s, dict):
                        seg = pick_str(s, ["text", "narration", "narration_text", "content"])
                        if seg:
                            parts.append(seg)

        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                elif isinstance(item, dict):
                    v = pick_str(item, ["content", "text", "narration", "narration_text"])
                    if v:
                        parts.append(v)

        out = "\n\n".join([p for p in (parts or []) if isinstance(p, str) and p.strip()]).strip()
        return out or text

    def generate_strong_title_from_text(self, text: str, kind: str = "story", max_len: int = 80) -> str:
        self._load_config()
        safe_kind = (kind or "story").strip().lower()
        if safe_kind not in {"story", "devotional", "prayer"}:
            safe_kind = "story"

        base = (text or "").strip()
        if not base:
            return "Devocional" if safe_kind == "devotional" else ("Reflexão com Oração" if safe_kind == "prayer" else "História")

        def _clean_title_line(s: str) -> str:
            import re as _re
            t = (s or "").strip()
            if not t:
                return ""
            t = t.replace("**", "").replace("`", "").strip()
            if "\n" in t:
                t = t.split("\n", 1)[0].strip()
            if t.startswith(("“", "”", '"', "'", "‘", "’")) and t.endswith(("“", "”", '"', "'", "‘", "’")) and len(t) >= 2:
                t = t[1:-1].strip()
            t = _re.sub(r"\s+", " ", t).strip()
            t = t.rstrip(" .,!?:;—–-").strip()
            return t

        def _truncate(t: str, limit: int) -> str:
            s = (t or "").strip()
            if not s:
                return ""
            if len(s) <= limit:
                return s
            cut = s[:limit].rstrip()
            if " " in cut:
                cut = cut.rsplit(" ", 1)[0].rstrip()
            return cut.strip()

        def _heuristic_title(src: str) -> str:
            import re as _re
            t = (src or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            first = ""
            for ln in t.split("\n"):
                s = (ln or "").strip()
                if s:
                    first = s
                    break
            first = _clean_title_line(first)
            flat = _re.sub(r"\s+", " ", t).strip()
            parts = _re.split(r"(?<=[.!?])\s+", flat)
            sentence = _clean_title_line(parts[0] if parts else flat)

            candidate = first if first else sentence
            cand_low = f" {candidate.lower()} "
            looks_like_sentence = any(x in cand_low for x in [" é ", " uma ", " um ", " são ", " foi ", " eram ", " estava ", " estamos "]) or len(candidate) > int(max_len or 80)

            low = (t or "").lower()
            if safe_kind == "devotional":
                templates = [
                    ("amor", "O Amor de Deus Que Transforma Tudo"),
                    ("perd", "O Perdão de Deus Que Liberta"),
                    ("esper", "A Esperança que Deus Renova Hoje"),
                    ("propós", "O Propósito de Deus Para Sua Vida"),
                    ("propos", "O Propósito de Deus Para Sua Vida"),
                    ("oração", "A Oração Que Muda Tudo"),
                    ("orar", "A Oração Que Muda Tudo"),
                    ("ansied", "Deus Vai Acalmar o Seu Coração"),
                    ("medo", "Deus Vai Acalmar o Seu Coração"),
                    ("cura", "A Cura Que Deus Começa em Você"),
                    ("milagr", "O Milagre que Deus Faz no Silêncio"),
                ]
                chosen = next((tpl for key, tpl in templates if key in low), "")
                if looks_like_sentence or not candidate:
                    candidate = chosen or "Deus Está Falando com Você Hoje"
                if "deus" not in (candidate or "").lower():
                    candidate = f"Deus: {candidate}"
            elif safe_kind == "prayer":
                templates = [
                    ("ansied", "Oração para Acalmar a Ansiedade"),
                    ("medo", "Oração para Vencer o Medo"),
                    ("paz", "Oração de Paz para o Seu Coração"),
                    ("sono", "Oração para Dormir em Paz"),
                    ("descans", "Oração para Descansar na Presença de Deus"),
                    ("cura", "Oração de Cura e Esperança"),
                    ("fam", "Oração de Proteção pela Família"),
                ]
                chosen = next((tpl for key, tpl in templates if key in low), "")
                if looks_like_sentence or not candidate:
                    candidate = chosen or "Oração de Paz e Reflexão com Deus"
            else:
                templates = [
                    ("recome", "O Recomeço Que Mudou Tudo"),
                    ("perd", "Quando Tudo Parece Perdido, a Virada Chega"),
                    ("amor", "O Amor Que Voltou Quando Ninguém Esperava"),
                    ("segred", "O Segredo Que Mudou a Vida Dele"),
                    ("milagr", "O Dia em que Tudo Mudou"),
                ]
                chosen = next((tpl for key, tpl in templates if key in low), "")
                if looks_like_sentence or not candidate:
                    candidate = chosen or "A Virada Que Mudou Tudo"

            candidate = _clean_title_line(candidate)
            candidate = _truncate(candidate, max(40, min(120, int(max_len or 80))))
            return candidate or ("Devocional" if safe_kind == "devotional" else ("Reflexão com Oração" if safe_kind == "prayer" else "História"))

        if not self._has_text_provider():
            return _heuristic_title(base)

        role = "devocional" if safe_kind == "devotional" else ("reflexão com oração" if safe_kind == "prayer" else "história")
        prompt = (
            "Crie UM título forte, impactante e chamativo em português (pt-BR) para um vídeo de YouTube.\n"
            f"Baseie-se na mensagem do texto ({role}).\n\n"
            "Regras obrigatórias:\n"
            f"- 45 a {int(max_len or 80)} caracteres (se possível)\n"
            "- Sem aspas, sem emojis, sem hashtags\n"
            "- Sem ponto final\n"
            "- Linguagem natural (pt-BR)\n"
            + ("- Mencione Deus/Fé de forma respeitosa\n" if safe_kind in {"devotional", "prayer"} else "")
            + "\nTEXTO BASE (trecho):\n"
            + base[:2200]
            + "\n\nRetorne APENAS o título."
        )

        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um copywriter especialista em títulos virais para YouTube. Retorne apenas uma linha com o título.",
                temperature=0.65,
                json_mode=False,
            )
            title = _clean_title_line(str(content or ""))
            if not title:
                return _heuristic_title(base)
            title = _truncate(title, max(40, min(120, int(max_len or 80))))
            return title or _heuristic_title(base)
        except Exception:
            return _heuristic_title(base)

    def improve_story_or_devotional_text(
        self,
        original_text: str,
        instruction: str,
        kind: str = "story",
        duration_min_minutes: int = 10,
        duration_max_minutes: Optional[int] = None,
    ) -> str:
        self._load_config()
        if not self._has_text_provider():
            return (original_text or "").strip() or "Texto (Simulação - Sem Chave)"

        kind_norm = (kind or "story").strip().lower()
        if kind_norm not in {"story", "devotional", "prayer"}:
            kind_norm = "story"
        safe_kind = "história" if kind_norm == "story" else ("devocional" if kind_norm == "devotional" else "reflexão com oração")
        min_m = max(1, int(duration_min_minutes or 1))
        max_m = int(duration_max_minutes) if duration_max_minutes else min_m
        if max_m < min_m:
            max_m = min_m

        # Usando 140 palavras por minuto (ritmo de narração calmo e envolvente)
        min_words = min_m * 140
        max_words = max_m * 160
        niche = (os.getenv("YOUTUBE_NICHE") or os.getenv("CHANNEL_NICHE") or os.getenv("CONTENT_NICHE") or "").strip()
        if not niche:
            niche = "reflexão, espiritualidade e mensagens cristãs (sem sensacionalismo falso)"

        extra_guidance = ""
        if kind_norm == "prayer":
            extra_guidance = """
        REGRAS ESPECIAIS:
        - Intensifique o clima de paz, acolhimento, oração e descanso.
        - Preserve uma linguagem suave, profundamente confortadora e relaxante.
        - Inclua trechos naturais de oração guiada e meditação espiritual.
        - Evite qualquer clima sombrio, ameaçador ou acusatório.
        """

        prompt = f"""
        Você é um editor profissional de textos para narração em vídeo de longa duração.
        Nicho do canal: {niche}.
        Reescreva, MELHORE e EXPANDA o(a) {safe_kind} abaixo para atingir a duração desejada.
        
        IMPORTANTE: O vídeo deve ter no mínimo {min_m} minutos. Se o texto original for curto, você DEVE expandi-lo com detalhes, exemplos e descrições ricas. NÃO resuma.

        INSTRUÇÕES DO USUÁRIO (respeite exatamente):
        {instruction}

        DIRETRIZES DE RETENÇÃO:
        - Ajuste os primeiros parágrafos para ter um gancho magnético (0-30s) direto na dor/sentimento do espectador.
        - Inclua pelo menos 2 perguntas diretas ao longo do texto para estimular reflexão e comentários.
        - Finalize com CTA clara e pergunta curta para comentários.
        {extra_guidance}

        Duração alvo do vídeo: entre {min_m} e {max_m} minutos.
        Tamanho alvo: entre {min_words} e {max_words} palavras (aprox. 140-160 palavras por minuto).

        TEXTO ORIGINAL:
        {original_text}

        REGRAS:
        - Retorne APENAS o texto final completo (sem explicações, sem JSON, sem listas).
        - Escreva pelo menos {min_words} palavras.
        - Não inclua nomes de marcas, links, nem instruções técnicas.
        """

        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um editor de textos para narração. Entregue apenas o texto final em português, sem JSON.",
                temperature=0.7,
                json_mode=False,
            )
            if not content:
                raise Exception("Resposta vazia da IA")
            return (self._normalize_narration_text(content) or content).strip()
        except Exception as e:
            print(f"Erro ao melhorar {safe_kind}: {e}")
            return (original_text or "").strip()

    def generate_youtube_content_factory_strategy(self, idea: str, channel_name: str = "Herdeiros das Promessas") -> Dict[str, Any]:
        self._load_config()
        theme = (idea or "").strip()
        if not theme:
            return {"error": "idea_required"}

        system = (
            "Você é Diretor de Estratégia e Criação para um canal cristão (fé/relacionado à Bíblia). "
            "Seu objetivo é maximizar CTR e retenção (principalmente nos primeiros 2 minutos) sem sensacionalismo falso. "
            "Use mistério e curiosidade bíblica com respeito, e foque em temas como mistérios bíblicos, curiosidades, escatologia, promessas."
        )
        prompt = f"""
Gere uma estratégia completa para um vídeo do canal "{channel_name}" a partir desta ideia bruta:

IDEIA BRUTA:
{theme}

RETORNE APENAS JSON VÁLIDO com esta estrutura:
{{
  "viralizacao": {{
    "potencial": "alto|medio|baixo",
    "nota": 0,
    "justificativa": "..."
  }},
  "titulos": ["...", "...", "...", "...", "..."],
  "roteiro": {{
    "gancho_0_30s": "...",
    "retencao_30_120s": "...",
    "corpo": "...",
    "cta_inscricao": "..."
  }},
  "thumbnail": {{
    "texto": "...",
    "imagem_prompt": "..."
  }},
  "seo": {{
    "tags": ["..."],
    "descricao": "...",
    "timestamps": ["00:00 ...", "00:45 ...", "02:10 ..."]
  }}
}}

REGRAS IMPORTANTES:
- Não use markdown.
- "nota" deve ser número inteiro de 0 a 100.
- O texto da thumbnail deve ser curto (2 a 5 palavras) e legível.
- A descrição deve incluir CTA e hashtags (sem exagero).
- O prompt de imagem deve ser em INGLÊS, estilo: Epic Christian Digital Art, cinematográfico, dramático, iluminação épica, sem texto na imagem.
""".strip()

        try:
            raw = self._generate_text(prompt, system_prompt=system, temperature=0.7, json_mode=True) or ""
            raw = raw.replace("```json", "").replace("```", "").strip()
            import json
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                return {"error": "invalid_response"}
            return data
        except Exception as e:
            return {"error": str(e)}

    def generate_story_image_prompts(
        self,
        story_text: str,
        n: int = 4,
        kind: str = "story",
        story_context: str = "",
        story_title: str = "",
        scene_number: int = 1,
        previous_scene_text: str = "",
    ) -> list:
        self._load_config()
        try:
            count = int(n or 1)
        except Exception:
            count = 1
        count = max(1, min(12, count))

        text = (story_text or "").strip()
        if not text:
            return []
        director = self._build_biblical_story_director(
            story_title=story_title,
            story_context=story_context,
            scene_text=text,
        )
        director_block = self._biblical_director_prompt_block(director, text)
        scene_card = self._build_cinematic_scene_card(
            director=director,
            scene_text=text,
            scene_number=scene_number,
            previous_scene_text=previous_scene_text,
        )
        scene_card_block = self._cinematic_scene_card_prompt_block(scene_card)

        safe_kind = (kind or "story").strip().lower()
        if safe_kind not in {"story", "devotional", "prayer"}:
            safe_kind = "story"
        kind_pt = "história" if safe_kind == "story" else ("devocional" if safe_kind == "devotional" else "reflexão com oração")
        prayer_visual_rule = (
            "Angelical and peaceful Christian meditation atmosphere, soft heavenly light, serene prayerful mood, contemplative composition, relaxing and family-friendly. "
            if safe_kind == "prayer" else ""
        )

        if not self.openrouter_key:
            base = text.replace("\n", " ").strip()[:320]
            styles = [
                "cinematic lighting, shallow depth of field",
                "dramatic atmosphere, volumetric light",
                "soft warm light, film still composition",
                "moody color grading, high detail",
            ]
            prompts = []
            for i in range(count):
                prompts.append(
                    self._compose_biblical_safe_prompt(
                        director,
                        text,
                        scene_card=scene_card,
                        base_prompt=(
                            f"Photorealistic cinematic photography inspired by this {kind_pt} message: {base}. "
                            f"{prayer_visual_rule}{styles[i % len(styles)]}. Realistic humans (no dolls), natural skin, pleasant mood, no horror, no monsters, no gore. No text, no watermark, no logo."
                        ),
                    )
                )
            return prompts

        import json

        prompt = f"""
        Crie {count} prompts de imagem DISTINTOS em INGLÊS, para gerar imagens por IA,
        com base no texto abaixo (um(a) {kind_pt} para narração).

        TEXTO (resumo/ideia central):
        {text[:2200]}

        REGRAS:
        - Cada prompt deve ser uma descrição visual rica (sem texto na imagem).
        - Varie composição, ângulo de câmera, cenário e momento (para evitar imagens repetidas).
        - Não inclua nomes de marcas, logos, marcas d'água nem "text overlay".
        - Estilo preferido: fotografia cinematográfica fotorrealista, iluminação natural e agradável, clima esperançoso e sereno.
        - Pessoas: aparência humana realista, proporções naturais, expressão serena (evitar "doll-like", "uncanny", "creepy").
        - Proibido: terror, monstros, gore, sangue, mutilação, olhos deformados, rosto desfigurado, assustador, grotesco, distópico, apocalíptico, sombrio.
        - Se for reflexão com oração: priorize atmosfera angelical, contemplativa, relaxante, luz celestial suave, paz interior, reverência cristã, sem exageros visuais.
        - DIRETOR BÍBLICO / NARRATIVO OBRIGATÓRIO:
        {director_block}
        - FICHA CINEMATOGRÁFICA DA CENA:
        {scene_card_block}
        - O prompt final deve nascer da ficha cinematográfica da cena, não apenas do texto bruto.
        - Diga exatamente quem aparece em cena e quem não pode aparecer.
        - Evite Jesus genérico; represente a ação específica da cena.
        - Retorne APENAS um JSON válido:
          {{ "prompts": ["...", "..."] }}
        """

        try:
            raw = self._generate_text(
                prompt,
                system_prompt="Você é um diretor de arte. Gere apenas JSON no formato solicitado.",
                temperature=0.6,
                json_mode=True,
            ) or "{}"
            raw = raw.replace("```json", "").replace("```", "").strip()
            data = json.loads(raw) if raw else {}
            prompts = data.get("prompts") if isinstance(data, dict) else None
            if not isinstance(prompts, list):
                prompts = []
        except Exception:
            prompts = []

        while len(prompts) < count:
            base = text.replace("\n", " ").strip()[:320]
            prompts.append(
                self._compose_biblical_safe_prompt(
                    director,
                    text,
                    scene_card=scene_card,
                    base_prompt=(
                        f"Photorealistic cinematic photography inspired by this {kind_pt} message: {base}. "
                        f"{prayer_visual_rule}Realistic humans (no dolls), pleasant mood, no horror, no monsters, no gore. No text, no watermark, no logo."
                    ),
                )
            )

        clean = []
        for p in prompts[:count]:
            if isinstance(p, str) and p.strip():
                clean.append(self._validate_biblical_visual_prompt(p.strip(), director, text, scene_card=scene_card)[:900])
        while len(clean) < count:
            clean.append(
                self._compose_biblical_safe_prompt(
                    director,
                    text,
                    scene_card=scene_card,
                    base_prompt=(
                        f"Photorealistic cinematic photography inspired by this {kind_pt} message. "
                        f"{prayer_visual_rule}Pleasant mood, no horror, no monsters, no gore. No text."
                    ),
                )[:900]
            )
        return clean[:count]

    def _visual_global_style(self) -> str:
        return "Cinematic photo-realistic, 8K, divine chiaroscuro, god rays, golden glow, celestial white highlights, deep blue atmosphere, warm fire tones, vibrant natural colors, epic perspective, deeply emotional expressions, high detail, high-definition, divine light, golden illumination, heavenly atmosphere, reverent scene, cinematic lighting, epic composition, inspiring, uplifting, photorealistic art, holy presence, single coherent scene, realistic anatomy, natural human faces, modest biblical wardrobe, live-action look"

    def _visual_global_negative(self) -> str:
        return "(terror, horror, scary, disturbing, gore, blood, zombie, dark spirits, creepy, unsettling, death, monstrosity, distorted faces, menacing, evil appearance, nightmares, intense fear, non-divine context, unholy, abstract chaos, surreal nightmare, fused faces, extra limbs, deformed anatomy, melted skin, corpse-like face, skull imagery)"

    def _normalize_for_rules(self, text: str) -> str:
        import unicodedata
        t = (text or "").strip().lower()
        t = unicodedata.normalize("NFKD", t)
        t = "".join(ch for ch in t if not unicodedata.combining(ch))
        return t

    def _infer_scene_emotions(self, text: str) -> List[str]:
        norm = self._normalize_for_rules(text or "")
        mapping = [
            ("faith", ["fe", "faith", "cre", "believe", "confianca", "confiança"]),
            ("hope", ["esperanca", "esperança", "hope"]),
            ("fear", ["medo", "fear", "temor", "assustada", "assustado"]),
            ("compassion", ["compaixao", "compaixão", "misericordia", "misericórdia", "mercy"]),
            ("urgency", ["multidao", "multidão", "crowd", "pressa", "urgencia", "urgência"]),
            ("reverence", ["adoracao", "adoração", "reverencia", "reverência", "worship"]),
            ("wonder", ["milagre", "miracle", "assombro", "wonder"]),
            ("shame", ["vergonha", "shame"]),
        ]
        detected: List[str] = []
        for label, terms in mapping:
            if any(term in norm for term in terms):
                detected.append(label)
        if not detected:
            detected.append("reverence")
        return detected[:4]

    def _build_biblical_story_director(
        self,
        story_title: str = "",
        story_context: str = "",
        scene_text: str = "",
    ) -> Dict[str, Any]:
        combined = " ".join(
            part.strip()
            for part in [story_title or "", story_context or "", scene_text or ""]
            if str(part or "").strip()
        )
        norm = self._normalize_for_rules(combined)
        scene_norm = self._normalize_for_rules(scene_text or "")
        director: Dict[str, Any] = {
            "story_id": "generic_biblical_story",
            "main_story": (story_title or "Biblical story").strip() or "Biblical story",
            "main_characters": ["Jesus", "biblical people relevant to the narration"],
            "allowed_characters": ["Jesus", "supporting biblical people relevant to the exact scene"],
            "forbidden_characters": ["Roman soldiers outside the narrated context"],
            "location": "ancient biblical setting matching the narration",
            "biblical_period": "1st century biblical world when relevant",
            "event_narrative": (scene_text or story_context or story_title or "biblical narrative moment").strip(),
            "important_objects": ["objects explicitly mentioned in the narration"],
            "emotions": self._infer_scene_emotions(scene_text or story_context or story_title),
            "forbidden_events": [
                "crucifixion when not explicitly narrated",
                "resurrection when not explicitly narrated",
                "Last Supper when not explicitly narrated",
                "ascension when not explicitly narrated",
                "nativity when not explicitly narrated",
            ],
            "forbidden_objects": [
                "cross when not explicitly narrated",
                "crown of thorns when not explicitly narrated",
                "Golgotha when not explicitly narrated",
            ],
            "chronology_rule": "Do not depict biblical events outside the chronology of the narrated story.",
        }

        if any(term in norm for term in ["fluxo de sangue", "issue of blood", "hemorrhage", "hemorrhoissa"]):
            director.update({
                "story_id": "woman_with_blood_flow",
                "main_story": "Woman with the issue of blood",
                "main_characters": ["Jesus", "woman with the issue of blood", "disciples", "crowd"],
                "allowed_characters": ["Jesus", "woman with the issue of blood", "disciples", "crowd"],
                "forbidden_characters": ["Roman soldiers", "Jesus in a later passion scene", "apostles at the Last Supper"],
                "location": "crowded ancient streets in Galilee or Capernaum",
                "biblical_period": "during Jesus' public ministry before the crucifixion",
                "important_objects": ["lower garment hem", "ancient robes", "dusty street"],
                "forbidden_events": [
                    "crucifixion",
                    "Golgotha",
                    "crown of thorns",
                    "Last Supper",
                    "resurrection",
                    "ascension",
                    "nativity of Jesus",
                ],
                "forbidden_objects": [
                    "cross",
                    "execution stakes",
                    "Roman spears",
                    "thorns crown",
                ],
            })
            if any(term in scene_norm for term in ["orla", "vestes", "garment", "cloak", "mantle", "touch"]):
                director["event_narrative"] = "the woman reaches from below and touches the lower hem of Jesus' tunic with her hand in faith while Jesus walks through the surrounding crowd"
        elif any(term in norm for term in ["mulher samaritana", "samaritana", "woman of samaria", "john 4", "joao 4", "joão 4", "poco de jaco", "poço de jaco", "jacob's well", "samaria"]):
            director.update({
                "story_id": "samaritan_woman",
                "main_story": "Jesus and the Samaritan woman at Jacob's well",
                "main_characters": ["Jesus", "Samaritan woman"],
                "allowed_characters": ["Jesus", "Samaritan woman", "disciples when the narration mentions them"],
                "forbidden_characters": ["Roman soldiers", "Jesus in a later passion scene", "disciples at the Last Supper", "infant Jesus"],
                "location": "Jacob's well in Samaria",
                "biblical_period": "during Jesus' public ministry before the crucifixion",
                "important_objects": ["water jar", "stone well", "ancient road", "midday sunlight"],
                "forbidden_events": [
                    "crucifixion",
                    "Golgotha",
                    "Last Supper",
                    "resurrection",
                    "ascension",
                    "nativity of Jesus",
                ],
                "forbidden_objects": [
                    "cross",
                    "crown of thorns",
                    "Roman execution scene",
                ],
            })
            if any(term in scene_norm for term in ["poco", "poço", "agua", "água", "jarro", "water", "well"]):
                director["event_narrative"] = "Jesus speaks with the Samaritan woman beside Jacob's well"

        return director

    def _biblical_director_prompt_block(self, director: Dict[str, Any], scene_text: str) -> str:
        return (
            f"- História principal: {director.get('main_story')}\n"
            f"- Evento exato da cena: {(scene_text or director.get('event_narrative') or '').strip()}\n"
            f"- Personagens permitidos: {', '.join(director.get('allowed_characters') or [])}\n"
            f"- Personagens proibidos: {', '.join(director.get('forbidden_characters') or [])}\n"
            f"- Objetos importantes: {', '.join(director.get('important_objects') or [])}\n"
            f"- Objetos proibidos: {', '.join(director.get('forbidden_objects') or [])}\n"
            f"- Eventos proibidos: {', '.join(director.get('forbidden_events') or [])}\n"
            f"- Local: {director.get('location')}\n"
            f"- Período bíblico: {director.get('biblical_period')}\n"
            f"- Emoções da cena: {', '.join(director.get('emotions') or [])}\n"
            f"- Regra cronológica: {director.get('chronology_rule')}"
        )

    def _infer_cinematic_camera(self, director: Dict[str, Any], scene_text: str) -> str:
        norm = self._normalize_for_rules(scene_text or "")
        story_id = str(director.get("story_id") or "")
        if story_id == "woman_with_blood_flow":
            if any(term in norm for term in ["orla", "vestes", "garment", "mantle", "touch", "toca"]):
                return "medium shot with partial close-up on the woman's hand touching the lower hem of Jesus' tunic while keeping Jesus and the surrounding crowd visible"
            return "handheld medium shot moving through the crowd, emphasizing the woman reaching toward the lower hem of Jesus' tunic"
        if story_id == "samaritan_woman":
            return "medium two-shot at eye level, gently framing both faces and the stone well"
        if any(term in norm for term in ["multidao", "multidão", "crowd"]):
            return "medium shot with layered depth through the crowd"
        if any(term in norm for term in ["conversa", "dialog", "fala", "speaks"]):
            return "medium two-shot with natural eye-level perspective"
        return "medium cinematic shot with clear subject isolation"

    def _infer_cinematic_lighting(self, director: Dict[str, Any], scene_text: str) -> str:
        norm = self._normalize_for_rules(scene_text or "")
        story_id = str(director.get("story_id") or "")
        if story_id == "samaritan_woman" or any(term in norm for term in ["poco", "poço", "well", "midday", "meio-dia"]):
            return "natural midday sunlight with soft warm highlights and realistic stone reflections"
        if story_id == "woman_with_blood_flow" or any(term in norm for term in ["multidao", "multidão", "crowd", "street", "rua"]):
            return "warm natural daylight with soft dust diffusion and gentle highlights on the main action"
        if any(term in norm for term in ["milagre", "miracle", "fe", "faith"]):
            return "soft natural light with subtle divine warmth, never surreal"
        return "natural cinematic daylight with realistic contrast and serene atmosphere"

    def _infer_visual_focus(self, director: Dict[str, Any], scene_text: str) -> str:
        norm = self._normalize_for_rules(scene_text or "")
        story_id = str(director.get("story_id") or "")
        if story_id == "woman_with_blood_flow":
            return "partial close-up on the woman's hand touching the lower hem of Jesus' tunic"
        if story_id == "samaritan_woman":
            return "the exchange between Jesus and the Samaritan woman beside the well and water jar"
        if any(term in norm for term in ["mao", "mão", "hand", "touch", "toca"]):
            return "the exact physical action described in the narration"
        if any(term in norm for term in ["conversa", "dialog", "fala", "speaks"]):
            return "the facial interaction and body language of the conversation"
        return "the main narrated action, centered and visually unambiguous"

    def _build_scene_continuity_note(self, director: Dict[str, Any], previous_scene_text: str, scene_text: str) -> str:
        previous_clean = (previous_scene_text or "").strip()
        if previous_clean:
            return (
                "Maintain wardrobe, geography, and character continuity from the previous scene while advancing the action. "
                f"Previous scene reference: {previous_clean[:180]}"
            )
        story_id = str(director.get("story_id") or "")
        if story_id == "woman_with_blood_flow":
            return "Establish continuity in the same ancient crowded street, keeping Jesus, the woman, and the surrounding crowd coherent."
        if story_id == "samaritan_woman":
            return "Keep continuity at Jacob's well with the same stone setting, Jesus, the woman, and the water jar."
        return "Establish this scene as a coherent continuation of the same biblical story world."

    def _infer_negative_scene_direction(self, director: Dict[str, Any], scene_text: str) -> str:
        story_id = str(director.get("story_id") or "")
        if story_id == "woman_with_blood_flow":
            return (
                "Do not depict Jesus placing his hand on the woman's head or blessing her with a frontal gesture unless the narration explicitly says so. "
                "The initiative must come from the woman's hand touching the lower hem of Jesus' tunic, with the crowd still present around them."
            )
        return ""

    def _build_cinematic_scene_card(
        self,
        director: Dict[str, Any],
        scene_text: str,
        scene_number: int = 1,
        previous_scene_text: str = "",
    ) -> Dict[str, Any]:
        scene_event = (scene_text or director.get("event_narrative") or "").strip()
        emotions = director.get("emotions") or ["reverence"]
        dominant_emotion = emotions[0] if emotions else "reverence"
        return {
            "main_story": director.get("main_story"),
            "scene_number": max(1, int(scene_number or 1)),
            "location": director.get("location"),
            "biblical_period": director.get("biblical_period"),
            "characters_present": list(director.get("allowed_characters") or []),
            "forbidden_characters": list(director.get("forbidden_characters") or []),
            "primary_action": (
                "the woman's hand touches the lower hem of Jesus' tunic while he walks through the crowd"
                if str(director.get("story_id") or "") == "woman_with_blood_flow"
                else scene_event
            ),
            "dominant_emotion": dominant_emotion,
            "important_objects": list(director.get("important_objects") or []),
            "camera_framing": self._infer_cinematic_camera(director, scene_event),
            "lighting": self._infer_cinematic_lighting(director, scene_event),
            "visual_focus": self._infer_visual_focus(director, scene_event),
            "continuity_with_previous_scene": self._build_scene_continuity_note(director, previous_scene_text, scene_event),
            "negative_scene_direction": self._infer_negative_scene_direction(director, scene_event),
            "forbidden_objects": list(director.get("forbidden_objects") or []),
            "forbidden_events": list(director.get("forbidden_events") or []),
        }

    def _cinematic_scene_card_prompt_block(self, scene_card: Dict[str, Any]) -> str:
        return (
            f"- História principal: {scene_card.get('main_story')}\n"
            f"- Número da cena: {scene_card.get('scene_number')}\n"
            f"- Local: {scene_card.get('location')}\n"
            f"- Período bíblico: {scene_card.get('biblical_period')}\n"
            f"- Personagens presentes: {', '.join(scene_card.get('characters_present') or [])}\n"
            f"- Personagens proibidos: {', '.join(scene_card.get('forbidden_characters') or [])}\n"
            f"- Ação principal: {scene_card.get('primary_action')}\n"
            f"- Emoção dominante: {scene_card.get('dominant_emotion')}\n"
            f"- Objetos importantes: {', '.join(scene_card.get('important_objects') or [])}\n"
            f"- Enquadramento/câmera: {scene_card.get('camera_framing')}\n"
            f"- Iluminação: {scene_card.get('lighting')}\n"
            f"- Foco visual: {scene_card.get('visual_focus')}\n"
            f"- Continuidade com a cena anterior: {scene_card.get('continuity_with_previous_scene')}\n"
            f"- Direção negativa: {scene_card.get('negative_scene_direction')}\n"
            f"- Objetos proibidos: {', '.join(scene_card.get('forbidden_objects') or [])}\n"
            f"- Eventos proibidos: {', '.join(scene_card.get('forbidden_events') or [])}"
        )

    def _compose_biblical_safe_prompt(
        self,
        director: Dict[str, Any],
        scene_text: str,
        base_prompt: str = "",
        scene_card: Optional[Dict[str, Any]] = None,
    ) -> str:
        base = (base_prompt or "").strip()
        scene_event = (scene_text or director.get("event_narrative") or "").strip()
        allowed = ", ".join(director.get("allowed_characters") or [])
        forbidden_characters = ", ".join(director.get("forbidden_characters") or [])
        important_objects = ", ".join(director.get("important_objects") or [])
        forbidden_events = ", ".join(director.get("forbidden_events") or [])
        forbidden_objects = ", ".join(director.get("forbidden_objects") or [])
        emotions = ", ".join(director.get("emotions") or [])
        scene_card = scene_card or self._build_cinematic_scene_card(director, scene_event)
        card_characters = ", ".join(scene_card.get("characters_present") or [])
        card_objects = ", ".join(scene_card.get("important_objects") or [])
        contextual = (
            f"Biblical cinematic scene from {director.get('main_story')}. "
            f"Scene number: {scene_card.get('scene_number')}. "
            f"Exact scene event: {scene_event}. "
            f"Characters on screen: {card_characters or allowed}. "
            f"Forbidden characters: {forbidden_characters}. "
            f"Setting: {director.get('location')}, {director.get('biblical_period')}. "
            f"Primary action: {scene_card.get('primary_action')}. "
            f"Visual focus: {scene_card.get('visual_focus')}. "
            f"Camera framing: {scene_card.get('camera_framing')}. "
            f"Lighting: {scene_card.get('lighting')}. "
            f"Continuity with previous scene: {scene_card.get('continuity_with_previous_scene')}. "
            f"Negative scene direction: {scene_card.get('negative_scene_direction')}. "
            f"Important objects when relevant: {card_objects or important_objects}. "
            f"Emotional tone: {scene_card.get('dominant_emotion') or emotions}. "
            f"Do not show out-of-chronology events such as {forbidden_events}. "
            f"Do not include forbidden objects such as {forbidden_objects}. "
            "No cross or crucifixion unless the narration is explicitly about that moment. "
            "No text, no watermark, no logo."
        ).strip()
        merged = f"{base}. {contextual}" if base else contextual
        return self._sanitize_and_contextualize_image_prompt(merged)

    def _find_biblical_prompt_violations(self, prompt: str, director: Dict[str, Any]) -> List[str]:
        norm = self._normalize_for_rules(prompt or "")
        if not norm:
            return []
        checks = {
            "crucifixion": ["crucifixion", "crucified", "cross", "cruz", "golgotha", "calvary", "crown of thorns", "coroa de espinhos"],
            "last_supper": ["last supper", "ultima ceia", "última ceia"],
            "resurrection": ["resurrection", "ressurreicao", "ressurreição", "empty tomb"],
            "ascension": ["ascension", "ascensao", "ascensão", "jesus ascending"],
            "nativity": ["nativity", "nascimento de jesus", "manger", "baby jesus"],
        }
        violations: List[str] = []
        for label, terms in checks.items():
            if any(term in norm for term in terms):
                violations.append(label)
        return violations

    def _validate_biblical_visual_prompt(
        self,
        prompt: str,
        director: Dict[str, Any],
        scene_text: str,
        scene_card: Optional[Dict[str, Any]] = None,
    ) -> str:
        cleaned = (prompt or "").strip()
        if not cleaned:
            return self._compose_biblical_safe_prompt(director, scene_text, scene_card=scene_card)
        violations = self._find_biblical_prompt_violations(cleaned, director)
        if violations:
            cleaned = self._compose_biblical_safe_prompt(director, scene_text, base_prompt=cleaned[:220], scene_card=scene_card)
        else:
            cleaned = self._compose_biblical_safe_prompt(director, scene_text, base_prompt=cleaned, scene_card=scene_card)
        return cleaned[:900]

    def _extract_storyboard_beats(self, text: str) -> List[str]:
        raw = str(text or "").replace("\r", "\n").strip()
        if not raw:
            return []
        parts = re.split(r'(?<=[.!?])\s+|\n+', raw)
        beats = [str(part or "").strip(" -") for part in parts if str(part or "").strip(" -")]
        if len(beats) > 1:
            return beats
        if len(raw) <= 260:
            return [raw]
        midpoint = max(1, len(raw) // 2)
        candidates = [". ", "; ", ", ", " "]
        split_at = midpoint
        for marker in candidates:
            pos = raw.find(marker, midpoint)
            if pos > 40:
                split_at = pos + len(marker.strip())
                break
        left = raw[:split_at].strip(" ,.;:-")
        right = raw[split_at:].strip(" ,.;:-")
        return [item for item in [left, right] if item]

    def _build_scene_caption_text(self, scene_text: str) -> str:
        beats = self._extract_storyboard_beats(scene_text)
        if not beats:
            return ""
        caption = beats[0]
        if len(caption) <= 150:
            return caption
        trimmed = caption[:147].rsplit(" ", 1)[0].strip(" ,.;:-")
        return trimmed or caption[:150]

    def _scene_signature_tokens(self, text: str, prompt: str = "") -> List[str]:
        norm = self._normalize_for_rules(f"{text or ''} {prompt or ''}")
        tokens = [token for token in re.split(r"[^a-z0-9]+", norm) if len(token) >= 4]
        unique_tokens: List[str] = []
        seen = set()
        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            unique_tokens.append(token)
        return unique_tokens[:24]

    def _scene_similarity_ratio(self, previous_scene: Optional[Dict[str, Any]], current_scene: Dict[str, Any]) -> float:
        if not isinstance(previous_scene, dict) or not isinstance(current_scene, dict):
            return 0.0
        prev_tokens = set(
            self._scene_signature_tokens(
                str(previous_scene.get("text") or ""),
                str(previous_scene.get("image_prompt") or previous_scene.get("prompt_cinematic") or ""),
            )
        )
        curr_tokens = set(
            self._scene_signature_tokens(
                str(current_scene.get("text") or ""),
                str(current_scene.get("image_prompt") or current_scene.get("prompt_cinematic") or ""),
            )
        )
        if not prev_tokens or not curr_tokens:
            return 0.0
        return len(prev_tokens.intersection(curr_tokens)) / float(max(1, len(prev_tokens.union(curr_tokens))))

    def _merge_storyboard_scenes(self, left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(left or {})
        merged["text"] = " ".join(
            part.strip()
            for part in [str((left or {}).get("text") or ""), str((right or {}).get("text") or "")]
            if part and part.strip()
        ).strip()
        merged["caption"] = str((left or {}).get("caption") or (right or {}).get("caption") or "").strip()
        merged["image_prompt"] = str((left or {}).get("image_prompt") or (right or {}).get("image_prompt") or "").strip()
        return merged

    def _rebalance_storyboard_scenes(
        self,
        scenes: List[Dict[str, Any]],
        target_min: int = 8,
        target_max: int = 15,
    ) -> List[Dict[str, Any]]:
        rebalanced = [dict(scene or {}) for scene in (scenes or []) if isinstance(scene, dict) and str(scene.get("text") or "").strip()]
        target_min = max(1, int(target_min or 8))
        target_max = max(target_min, int(target_max or 15))
        while len(rebalanced) < target_min:
            best_idx = -1
            best_split: List[str] = []
            for idx, scene in enumerate(rebalanced):
                beats = self._extract_storyboard_beats(str(scene.get("text") or ""))
                if len(beats) >= 2:
                    candidate = [beats[0].strip(), " ".join(beats[1:]).strip()]
                    if all(candidate):
                        if len(" ".join(candidate)) > len(" ".join(best_split or [])):
                            best_idx = idx
                            best_split = candidate
            if best_idx < 0 or len(best_split) < 2:
                break
            original = dict(rebalanced[best_idx])
            left = dict(original)
            right = dict(original)
            left["text"] = best_split[0]
            right["text"] = best_split[1]
            left["caption"] = self._build_scene_caption_text(left["text"])
            right["caption"] = self._build_scene_caption_text(right["text"])
            rebalanced[best_idx:best_idx + 1] = [left, right]
        while len(rebalanced) > target_max:
            merge_idx = None
            merge_score = None
            for idx in range(len(rebalanced) - 1):
                pair_len = len(str(rebalanced[idx].get("text") or "")) + len(str(rebalanced[idx + 1].get("text") or ""))
                if merge_score is None or pair_len < merge_score:
                    merge_score = pair_len
                    merge_idx = idx
            if merge_idx is None:
                break
            merged = self._merge_storyboard_scenes(rebalanced[merge_idx], rebalanced[merge_idx + 1])
            rebalanced[merge_idx:merge_idx + 2] = [merged]
        return rebalanced

    def _build_cinematic_story_continuity_anchor(self, title: str, scenes: List[Dict[str, Any]]) -> str:
        first_text = str((scenes[0] or {}).get("text") or "").strip() if scenes else ""
        anchor_parts = [
            str(title or "").strip(),
            first_text[:180],
            "Maintain wardrobe, geography, mood, lighting logic, and recurring characters across the full video.",
        ]
        return " ".join(part for part in anchor_parts if part).strip()

    def _infer_scene_motion_effect(
        self,
        scene_card: Dict[str, Any],
        scene_text: str,
        scene_number: int,
        total_scenes: int,
    ) -> str:
        scene_norm = self._normalize_for_rules(str(scene_text or ""))
        card_norm = self._normalize_for_rules(
            " ".join(
                [
                    str(scene_card.get("camera_framing") or ""),
                    str(scene_card.get("visual_focus") or ""),
                    str(scene_card.get("primary_action") or ""),
                ]
            )
        )
        if any(term in scene_norm for term in ["crowd", "multidao", "street", "rua", "atravessa", "passando", "meio"]):
            return "parallax"
        if any(term in scene_norm for term in ["dialog", "conversa", "disse", "falou", "procura", "olha", "well", "poco"]):
            return "drift"
        if any(term in scene_norm for term in ["milagre", "cura", "alivio", "paz", "filha", "peace", "heal", "healing"]):
            return "slow_zoom"
        if any(term in scene_norm for term in ["touch", "toca", "orla", "veste", "mao", "hand"]) or any(term in card_norm for term in ["close", "hem", "detail", "touch"]):
            return "push_in"
        if any(term in scene_norm for term in ["miracle", "wonder", "revelation", "revelacao"]) or any(term in card_norm for term in ["wonder", "revelation"]):
            return "dolly_in"
        if scene_number >= max(1, total_scenes - 1):
            return "slow_zoom"
        return "depth_movement" if scene_number % 2 == 0 else "slow_zoom"

    def _compose_cinematic_prompt_from_scene_card(
        self,
        director: Dict[str, Any],
        scene_card: Dict[str, Any],
        scene_text: str,
        continuity_anchor: str = "",
        movement_hint: str = "",
        variation_note: str = "",
    ) -> str:
        characters = ", ".join(scene_card.get("characters_present") or director.get("allowed_characters") or [])
        important_objects = ", ".join(scene_card.get("important_objects") or director.get("important_objects") or [])
        base_prompt = (
            f"Photorealistic cinematic film still. {scene_card.get('location')}. {scene_card.get('biblical_period')}. "
            f"Characters on screen: {characters}. Primary action: {scene_card.get('primary_action')}. "
            f"Emotion: {scene_card.get('dominant_emotion')}. Camera framing: {scene_card.get('camera_framing')}. "
            f"Lighting: {scene_card.get('lighting')}. Visual focus: {scene_card.get('visual_focus')}. "
            f"Important objects: {important_objects}. Continuity anchor: {continuity_anchor or scene_card.get('continuity_with_previous_scene')}. "
            f"Camera movement feeling: {movement_hint or 'slow cinematic movement'}. "
            f"{variation_note.strip()} "
            "Single coherent scene, natural anatomy, realistic biblical wardrobe, no text, no watermark, no logo."
        ).strip()
        return self._compose_biblical_safe_prompt(
            director,
            scene_text,
            base_prompt=base_prompt,
            scene_card=scene_card,
        )[:900]

    def _build_cinematic_storyboard_qc(self, scenes: List[Dict[str, Any]]) -> Dict[str, Any]:
        analyses: List[Dict[str, Any]] = []
        problematic: List[int] = []
        repeated: List[int] = []
        previous_scene: Optional[Dict[str, Any]] = None
        for idx, scene in enumerate(scenes):
            scene_number = int(scene.get("scene_number") or idx + 1)
            prompt_text = str(scene.get("image_prompt") or scene.get("prompt_cinematic") or "").strip()
            caption_text = str(scene.get("caption") or "").strip()
            motion_hint = str(scene.get("camera_movement") or scene.get("motion_effect") or "").strip()
            scene_card = scene.get("scene_card") if isinstance(scene.get("scene_card"), dict) else {}
            issues: List[str] = []
            prompt_words = len([word for word in prompt_text.split() if word.strip()])
            prompt_score = 100 if prompt_words >= 32 else max(55, min(100, 55 + (prompt_words * 2)))
            caption_score = 100 if caption_text and len(caption_text) <= 160 else 72 if caption_text else 58
            continuity_score = 92 if str(scene_card.get("continuity_with_previous_scene") or "").strip() else 70
            motion_score = 96 if motion_hint else 64
            repetition_ratio = self._scene_similarity_ratio(previous_scene, scene)
            repetition_penalty = 0
            if prompt_words < 22:
                issues.append("prompt_short")
            if not caption_text:
                issues.append("caption_missing")
            elif len(caption_text) > 180:
                issues.append("caption_too_long")
            if not motion_hint:
                issues.append("camera_motion_missing")
            if repetition_ratio >= 0.74:
                repetition_penalty = 24
                repeated.append(scene_number)
                issues.append("repetitive_visual")
            scene_score = max(
                0,
                min(
                    100,
                    int(round(((prompt_score * 0.4) + (continuity_score * 0.2) + (motion_score * 0.2) + (caption_score * 0.2)) - repetition_penalty)),
                ),
            )
            status = "pass" if scene_score >= 78 and not issues else "needs_regeneration"
            if status != "pass":
                problematic.append(scene_number)
            analysis = {
                "scene_number": scene_number,
                "scene_score": scene_score,
                "prompt_score": int(prompt_score),
                "continuity_score": int(continuity_score),
                "motion_score": int(motion_score),
                "caption_score": int(caption_score),
                "repetition_ratio": round(repetition_ratio, 3),
                "issues": issues,
                "status": status,
            }
            scene["scene_qc"] = analysis
            scene["scene_qc_status"] = status
            analyses.append(analysis)
            previous_scene = scene
        average_score = round(sum(item["scene_score"] for item in analyses) / float(max(1, len(analyses))), 2)
        return {
            "scene_count": len(scenes),
            "average_scene_score": average_score,
            "problematic_scene_numbers": problematic,
            "repeated_scene_numbers": repeated,
            "regeneration_required": bool(problematic),
            "scene_analyses": analyses,
        }

    def build_cinematic_engine_v2_plan(
        self,
        plan: Dict[str, Any],
        target_scene_count: Optional[int] = None,
        min_scene_count: int = 8,
        max_scene_count: int = 15,
    ) -> Dict[str, Any]:
        if not isinstance(plan, dict):
            return plan
        raw_scenes = plan.get("scenes") or []
        if not isinstance(raw_scenes, list) or not raw_scenes:
            return plan

        title = str(plan.get("title") or "Video").strip() or "Video"
        story_context = str(plan.get("story_context") or plan.get("description") or "").strip()
        normalized_scenes: List[Dict[str, Any]] = []
        for raw_scene in raw_scenes:
            if isinstance(raw_scene, str):
                scene_dict = {"text": raw_scene.strip(), "image_prompt": ""}
            elif isinstance(raw_scene, dict):
                scene_dict = dict(raw_scene)
            else:
                continue
            scene_text = str(
                scene_dict.get("text")
                or scene_dict.get("narration")
                or scene_dict.get("narration_text")
                or ""
            ).strip()
            if not scene_text:
                continue
            scene_dict["text"] = scene_text
            scene_dict["image_prompt"] = str(scene_dict.get("image_prompt") or scene_dict.get("visual_prompt") or "").strip()
            scene_dict["caption"] = str(scene_dict.get("caption") or scene_dict.get("on_screen_text") or "").strip()
            normalized_scenes.append(scene_dict)

        if not normalized_scenes:
            return plan

        desired_count = int(target_scene_count or len(normalized_scenes) or min_scene_count)
        desired_count = max(int(min_scene_count or 8), min(int(max_scene_count or 15), desired_count))
        normalized_scenes = self._rebalance_storyboard_scenes(
            normalized_scenes,
            target_min=min_scene_count,
            target_max=max_scene_count,
        )
        if len(normalized_scenes) > desired_count:
            normalized_scenes = self._rebalance_storyboard_scenes(
                normalized_scenes,
                target_min=desired_count,
                target_max=desired_count,
            )

        continuity_anchor = self._build_cinematic_story_continuity_anchor(title, normalized_scenes)
        enhanced_scenes: List[Dict[str, Any]] = []
        for idx, scene in enumerate(normalized_scenes, start=1):
            previous_scene_text = str((enhanced_scenes[-1] or {}).get("text") or "").strip() if enhanced_scenes else ""
            scene_text = str(scene.get("text") or "").strip()
            scene_director = scene.get("scene_director") if isinstance(scene.get("scene_director"), dict) else None
            if not scene_director:
                scene_director = self._build_biblical_story_director(
                    story_title=title,
                    story_context=story_context or continuity_anchor,
                    scene_text=scene_text,
                )
            scene_card = scene.get("scene_card") if isinstance(scene.get("scene_card"), dict) else None
            if not scene_card:
                scene_card = self._build_cinematic_scene_card(
                    director=scene_director,
                    scene_text=scene_text,
                    scene_number=idx,
                    previous_scene_text=previous_scene_text,
                )
            movement_hint = str(scene.get("camera_movement") or scene.get("motion_effect") or "").strip()
            if not movement_hint:
                movement_hint = self._infer_scene_motion_effect(scene_card, scene_text, idx, len(normalized_scenes))
            cinematic_prompt = str(scene.get("prompt_cinematic") or scene.get("image_prompt") or "").strip()
            if len(cinematic_prompt.split()) < 22:
                cinematic_prompt = self._compose_cinematic_prompt_from_scene_card(
                    scene_director,
                    scene_card,
                    scene_text,
                    continuity_anchor=continuity_anchor,
                    movement_hint=movement_hint,
                )
            enhanced_scene = dict(scene)
            enhanced_scene.update(
                {
                    "scene_number": idx,
                    "title": str(scene.get("title") or f"Cena {idx}").strip(),
                    "caption": str(scene.get("caption") or "").strip() or self._build_scene_caption_text(scene_text),
                    "image_prompt": cinematic_prompt[:900],
                    "prompt_cinematic": cinematic_prompt[:900],
                    "scene_director": scene_director,
                    "scene_card": scene_card,
                    "camera_movement": movement_hint,
                    "motion_effect": movement_hint,
                    "visual_continuity_anchor": continuity_anchor,
                }
            )
            enhanced_scenes.append(enhanced_scene)

        qc_before = self._build_cinematic_storyboard_qc(enhanced_scenes)
        regenerated_scene_numbers: List[int] = []
        for scene_number in qc_before.get("problematic_scene_numbers") or []:
            idx = max(0, int(scene_number) - 1)
            if idx >= len(enhanced_scenes):
                continue
            scene = enhanced_scenes[idx]
            previous_text = str((enhanced_scenes[idx - 1] or {}).get("text") or "").strip() if idx > 0 else ""
            variation_note = (
                "Advance the dramatic beat with a new visual angle, do not repeat the previous scene composition. "
                f"Previous scene reference: {previous_text[:160]}"
            ).strip()
            scene["image_prompt"] = self._compose_cinematic_prompt_from_scene_card(
                scene.get("scene_director") if isinstance(scene.get("scene_director"), dict) else {},
                scene.get("scene_card") if isinstance(scene.get("scene_card"), dict) else {},
                str(scene.get("text") or ""),
                continuity_anchor=continuity_anchor,
                movement_hint=str(scene.get("camera_movement") or ""),
                variation_note=variation_note,
            )[:900]
            scene["prompt_cinematic"] = scene["image_prompt"]
            scene["qc_regenerated"] = True
            regenerated_scene_numbers.append(int(scene_number))

        qc_after = self._build_cinematic_storyboard_qc(enhanced_scenes)
        plan["scenes"] = enhanced_scenes
        plan["cinematic_engine_v2"] = {
            "enabled": True,
            "version": "sprint1",
            "target_scene_count": desired_count,
            "actual_scene_count": len(enhanced_scenes),
            "continuity_anchor": continuity_anchor,
            "regenerated_scene_numbers": regenerated_scene_numbers,
            "quality_control": qc_after,
            "quality_control_before_regeneration": qc_before,
            "premium_ending_ready": True,
        }
        return plan

    def _visual_negative_for_text(self, text: str) -> str:
        base = self._visual_global_negative()
        norm = self._normalize_for_rules(text or "")
        is_ezekiel_bones = any(k in norm for k in ["ezequiel", "ezekiel", "vale de ossos secos", "ossos secos", "valley of dry bones", "dry bones"])
        extra_common = [
            "no explicit blood",
            "no excessive blood",
            "no gore",
            "no horror",
            "no macabre",
            "no grotesque",
            "no corpse-like faces",
            "no melted skin",
            "no fused faces",
            "no extra limbs",
            "no deformed anatomy",
            "no abstract collage",
            "no surreal nightmare imagery",
            "no terrifying symbols",
            "no demonic symbols",
            "no evil expression",
            "no pure malice",
            "no fully black eyes",
            "no unsettling atmosphere",
            "no creepy",
            "no skulls",
            "no skeletons",
            "no bones",
            "no corpse",
            "no decomposition",
            "no rot",
        ]
        if is_ezekiel_bones:
            extra_common = [x for x in extra_common if x not in {"no skulls", "no skeletons", "no bones"}]
            extra_common.append("if bones appear, focus on reconstitution and life, no decomposition")

        extra = ", ".join(extra_common)
        return f"{base}, {extra}".strip().strip(",")

    def _sanitize_and_contextualize_image_prompt(self, prompt: str) -> str:
        import re
        raw = (prompt or "").strip()
        if not raw:
            return raw

        t = raw
        t = re.sub(r"(?is)\bnegative\s*prompt\s*:\s*.*$", "", t).strip()
        t = re.sub(r"\s+", " ", t).strip()

        norm = self._normalize_for_rules(t)
        positive_visual_context = re.sub(r"(?i)\bdo not show\b[^.]*\.", " ", t)
        positive_visual_context = re.sub(r"(?i)\bdo not include\b[^.]*\.", " ", positive_visual_context)
        positive_visual_context = re.sub(r"(?i)\bno cross or crucifixion unless\b[^.]*\.", " ", positive_visual_context)
        positive_visual_context = re.sub(r"(?i)\bforbidden characters\b[^.]*\.", " ", positive_visual_context)
        positive_visual_context = re.sub(r"(?i)\bpersonagens proibidos\b[^.]*\.", " ", positive_visual_context)
        positive_visual_context = re.sub(r"(?i)\bnegative\s*prompt\b[^.]*\.", " ", positive_visual_context)
        positive_visual_context = re.sub(r"(?i)\bbefore the crucifixion\b", " ", positive_visual_context)
        positive_visual_context = re.sub(r"(?i)\bcrucified jesus\b", " ", positive_visual_context)
        positive_visual_context = re.sub(r"\s+", " ", positive_visual_context).strip()
        norm_positive = self._normalize_for_rules(positive_visual_context)
        is_revelation = any(k in norm for k in [
            "apocalipse", "revelation", "joao", "john",
            "candeeiro", "candelabro", "lampstand", "seven lamp",
            "cabelos brancos", "white hair", "wool",
            "espada saindo da boca", "sword from his mouth", "sword from mouth",
            "olhos em chamas", "eyes like fire", "flames of fire", "eyes of fire",
            "vestes de gloria", "robe of glory", "glorious robe",
            "filho do homem", "son of man",
        ])
        has_jesus_reference = any(k in norm_positive for k in [
            "jesus", "jesus christ", "cristo", "christ",
        ])
        has_passion_reference = any(k in norm_positive for k in [
            "crucificacao", "crucifixion", "crucificado", "crucified",
            "coroa de espinhos", "crown of thorns", "cross", "cruz",
            "calvario", "calvary", "golgota", "golgotha",
        ])
        is_christ_passion = has_jesus_reference and has_passion_reference

        banned_words = [
            "monster", "monstrosity", "demon", "demonic", "devil", "satanic",
            "horror", "terror", "scary", "creepy", "macabre", "disturbing", "unholy",
            "evil", "menacing", "nightmare", "grotesque", "corpse", "skull", "skeleton",
            "mutated", "mutation", "deformed", "distorted", "melted", "twisted",
        ]
        if any(w in norm for w in banned_words):
            for w in banned_words:
                if w in norm:
                    t = re.sub(rf"(?i)\b{re.escape(w)}\b", "", t)
            t = re.sub(r"\s+", " ", t).strip()

        if is_revelation:
            t = (
                f"Glorious cinematic sacred vision inspired by Revelation 1. {t}. "
                "Interpret all visionary symbols as divine glory: hair white like wool as majestic and luminous, "
                "eyes like flames as gentle radiant divine light (not frightening), "
                "and 'sword from the mouth' as a symbolic beam of light shaped like a blade (not literal metal, not grotesque). "
                "Include seven golden lampstands when relevant, golden illumination, heavenly atmosphere, god rays, "
                "epic composition, vibrant colors, uplifting and inspiring mood, modest biblical attire, wide shot."
            ).strip()
        else:
            if any(k in norm for k in ["olhos", "eyes"]) and any(k in norm for k in ["fogo", "fire", "flame"]):
                t = (
                    f"{t}. Interpret any 'eyes of fire' as gentle divine light reflecting like flames, holy and uplifting, not scary or menacing."
                ).strip()
            if any(k in norm for k in ["espada", "sword"]) and any(k in norm for k in ["boca", "mouth"]):
                t = (
                    f"{t}. Interpret any 'sword from the mouth' symbolically as a radiant beam of light shaped like a blade, not grotesque."
                ).strip()
            if is_christ_passion:
                t = (
                    f"{t}. Depict Jesus Christ and the crucifixion with reverence, dignity, compassion, and biblical respect. "
                    "A crown of thorns and the cross are allowed when relevant, but avoid gore, blood emphasis, mutilation, horror, body distortion, or shocking close-ups. "
                    "Prefer medium or wide shots, peaceful sacred expression, natural anatomy, modest biblical clothing, warm divine light, and an atmosphere of sacrifice, love, hope, and redemption."
                ).strip()

        style = self._visual_global_style()
        t_low = t.lower()
        if "reverent" not in t_low and "holy" not in t_low and "heavenly" not in t_low:
            t = f"{t}. Reverent holy scene, uplifting, inspiring."
        if "cinematic" not in t_low and "photorealistic" not in t_low:
            t = f"{t}. {style}"
        else:
            t = f"{t}. {style}"

        t = (
            f"{t}. Do not depict horror, terror, monsters, demons, disturbing atmosphere, or evil appearance. "
            "Avoid close-up threatening faces; if faces appear, keep them serene, natural, and peaceful. "
            "Avoid surreal abstraction, chaotic collage, fused people, duplicated faces, warped hands, extra fingers, broken anatomy, grayscale nightmare aesthetics, or corpse-like textures."
        )
        return re.sub(r"\s+", " ", t).strip()

    def generate_semantic_visual_prompts_from_lyrics(self, lyrics: str, caption_slots: list, title: str = "", options: Optional[Dict[str, Any]] = None) -> list:
        self._load_config()
        import re
        try:
            count = int(len(caption_slots or []))
        except Exception:
            count = 0
        count = max(1, min(60, count))

        raw_lyrics = (lyrics or "").strip()
        clean_lyrics = raw_lyrics
        clean_lyrics = re.sub(r"(?m)^\s*\[\s*style\s*:[^\]]*\]\s*$", "", clean_lyrics).strip()
        clean_lyrics = re.sub(r"(?m)^\s*\[\s*break\s*:[^\]]*\]\s*$", "", clean_lyrics).strip()
        clean_lyrics = re.sub(r"(?m)^\s*\[\s*rhythmic\s+clapping\s*\]\s*$", "", clean_lyrics).strip()
        clean_lyrics = re.sub(r"\n{3,}", "\n\n", clean_lyrics).strip()

        try:
            from app.services.openai_image_module import OpenAIImageModule
        except Exception:
            OpenAIImageModule = None

        opts = dict(options or {})
        if OpenAIImageModule is None:
            style = self._visual_global_style()
            out = []
            while len(out) < count:
                out.append(self._sanitize_and_contextualize_image_prompt(f"Photorealistic cinematic film still. {style}. Safe, uplifting, no text.")[:900])
            return out[:count]

        mod = OpenAIImageModule(ai_service=self)
        sections = mod.split_lyrics_into_sections(clean_lyrics or raw_lyrics)
        allocated = mod._allocate_scenes(sections, count)
        prompt_language = mod._coerce_prompt_language((opts.get("prompt_language") or "auto"), clean_lyrics or raw_lyrics)
        semantic = mod.interpretar_letra(clean_lyrics or raw_lyrics, allocated, prompt_language=prompt_language) or {}
        global_semantic = semantic if isinstance(semantic, dict) else {}
        scene_semantics = global_semantic.get("cenas") if isinstance(global_semantic, dict) else None
        if not isinstance(scene_semantics, list):
            scene_semantics = []

        safe_title = (title or "").strip()
        if safe_title and isinstance(global_semantic, dict) and not global_semantic.get("titulo"):
            global_semantic["titulo"] = safe_title

        prompts = []
        for i, s in enumerate(allocated[:count]):
            ss = scene_semantics[i] if i < len(scene_semantics) and isinstance(scene_semantics[i], dict) else {}
            if not ss.get("trecho_titulo"):
                ss["trecho_titulo"] = (s.get("title") or f"Trecho {i+1}").strip()
            if not ss.get("descricao_cena"):
                ss["descricao_cena"] = (s.get("text") or "").strip()[:900]
            dalle_prompt = mod.build_dalle_prompt(global_semantic, ss, opts, prompt_language)
            prompts.append(self._sanitize_and_contextualize_image_prompt(dalle_prompt)[:900])

        while len(prompts) < count:
            style = self._visual_global_style()
            prompts.append(self._sanitize_and_contextualize_image_prompt(f"Photorealistic cinematic film still. {style}. Safe, uplifting, no text.")[:900])
        return prompts[:count]

    def enrich_scenes_with_image_prompts(self, plan: dict) -> dict:
        """
        Gera image_prompt profissionais com base na narração de cada cena, para a IA
        criar imagens próprias e montar o vídeo de forma profissional (YouTube Auto etc).
        Atualiza apenas cenas que não têm image_prompt ou têm um muito curto/genérico.
        """
        self._load_config()
        if not self.openrouter_key:
            return plan

        scenes = plan.get("scenes") or []
        if not scenes or not isinstance(scenes, list):
            return plan

        # Identifica cenas que precisam de image_prompt (vazio ou curto < 40 chars)
        need_prompts = []
        for i, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                continue
            text = (scene.get("text") or "").strip()
            current = (scene.get("image_prompt") or "").strip()
            if text and (not current or len(current) < 40):
                need_prompts.append((i, text[:500]))

        if not need_prompts:
            return plan

        import json
        # Uma única chamada: gera um image_prompt detalhado por cena a partir da narração
        scene_cards: Dict[int, Dict[str, Any]] = {}
        scene_directors: Dict[int, Dict[str, Any]] = {}
        scenes_desc_parts: List[str] = []
        for order, (scene_idx, text) in enumerate(need_prompts, start=1):
            previous_scene_text = ""
            if scene_idx > 0 and scene_idx - 1 < len(scenes) and isinstance(scenes[scene_idx - 1], dict):
                previous_scene_text = str(scenes[scene_idx - 1].get("text") or "").strip()
            scene_director = self._build_biblical_story_director(
                story_title=str(plan.get("title") or "Vídeo"),
                story_context=str(plan.get("story_context") or ""),
                scene_text=text,
            )
            scene_card = self._build_cinematic_scene_card(
                director=scene_director,
                scene_text=text,
                scene_number=order,
                previous_scene_text=previous_scene_text,
            )
            scene_directors[scene_idx] = scene_director
            scene_cards[scene_idx] = scene_card
            scenes_desc_parts.append(
                f"Cena {order} (narração): {text}\n"
                f"Ficha cinematográfica:\n{self._cinematic_scene_card_prompt_block(scene_card)}"
            )
        scenes_desc = "\n\n".join(scenes_desc_parts)
        title = plan.get("title") or "Vídeo"
        story_context = plan.get("story_context") or "\n".join(
            str(scene.get("text") or "").strip()
            for scene in scenes
            if isinstance(scene, dict) and str(scene.get("text") or "").strip()
        )
        director = self._build_biblical_story_director(
            story_title=str(title or ""),
            story_context=str(story_context or ""),
            scene_text=scenes_desc,
        )

        style = self._visual_global_style()
        neg = self._visual_negative_for_text(scenes_desc)

        prompt = f"""
        Você é um diretor de arte e diretor de fotografia para vídeos narrados (YouTube, Shorts). Seu trabalho é criar descrições visuais para gerar imagens com IA que ilustrem exatamente o que está sendo dito, com continuidade narrativa.

        Título do vídeo: {title}

        Narrações por cena:
        {scenes_desc}

        DIRETOR BÍBLICO / NARRATIVO:
        {self._biblical_director_prompt_block(director, scenes_desc)}

        Para CADA cena acima, crie UMA descrição visual (image_prompt) em INGLÊS com as regras:
        - Faça internamente uma leitura exegética: protagonista, cenário, ação/emoção principal; diferencie metáfora vs literal.
        - Interpretação contextual: jamais interpretar passagens bíblicas de forma literal e sombria; traduza descrições visionárias para glória divina, transcendência e luz cinematográfica, nunca para terror.
        - Representar fielmente a ideia e o clima da narração, evitando genericidade.
        - Cada prompt deve nascer da ficha cinematográfica da própria cena.
        - Diga explicitamente quem aparece e quem não aparece.
        - Evite Jesus genérico quando a cena exige uma ação específica.
        - Estilo obrigatório: {style}.
        - Estética e tom: santidade, adoração, esperança; luz celestial (god rays), brilho dourado, contraste (chiaroscuro) para glória, não para medo; paleta dourado/branco celestial/azul profundo/tons quentes.
        - Composição obrigatória: uma única cena coerente, sem colagem abstrata, sem múltiplos rostos fundidos, sem sobreposição caótica de pessoas, sem anatomia quebrada.
        - Pessoas: humanas realistas (evitar bonecos/uncanny), proporções naturais, expressão serena.
        - Paisagens: realistas, sem aparência de IA assustadora, cores naturais, clima agradável.
        - Bloqueio global obrigatório: {neg}.
        - Proibido: macabro, terror, gore, símbolos de horror, olhos totalmente pretos, expressões de pura maldade; vestes indecentes.
        - Se mencionar Jesus Cristo, cruz, coroa de espinhos, crucificação ou Calvário: mostrar reverência e dignidade; pode haver sofrimento respeitoso, mas sem sangue em destaque, sem mutilação, sem choque visual, sem body horror.
        - Se narrar morte/inferno/trevas: represente por sombras, desertos ou abismos distantes, com a luz vencendo as trevas.
        - Proibido: texto na imagem, marcas d'água, logos.
        - Uma frase detalhada (30-80 palavras): cenário, iluminação, atmosfera, composição.
        - PROIBIDO: foto de banco de imagens, logos, marcas, text, watermark, personagens famosos.
        - Se a narração for abstrata, use metáforas visuais claras que expressem o sentido da mensagem.

        Retorne APENAS um JSON válido com um array "image_prompts" na mesma ordem das cenas:
        {{ "image_prompts": ["descrição visual cena 1...", "descrição visual cena 2...", ...] }}
        """

        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você gera apenas JSON com o array image_prompts. Cada item é uma descrição visual em inglês para gerar imagem com IA.",
                temperature=0.6,
                json_mode=True
            )
            if not content:
                return plan
            content = content.replace("```json", "").replace("```", "").strip()
            data = json.loads(content)
            prompts_list = data.get("image_prompts") or []
            if not isinstance(prompts_list, list):
                return plan
            for k, (scene_idx, scene_text) in enumerate(need_prompts):
                if k < len(prompts_list) and scene_idx < len(scenes):
                    prompt_text = (prompts_list[k] or "").strip()
                    if prompt_text and isinstance(scenes[scene_idx], dict):
                        scene_director = scene_directors.get(scene_idx) or self._build_biblical_story_director(
                            story_title=str(title or ""),
                            story_context=str(story_context or ""),
                            scene_text=scene_text,
                        )
                        scene_card = scene_cards.get(scene_idx) or self._build_cinematic_scene_card(
                            director=scene_director,
                            scene_text=scene_text,
                            scene_number=k + 1,
                        )
                        scenes[scene_idx]["image_prompt"] = self._validate_biblical_visual_prompt(
                            prompt_text,
                            scene_director,
                            str(scenes[scene_idx].get("text") or ""),
                            scene_card=scene_card,
                        )[:500]
            plan["scenes"] = scenes
        except Exception as e:
            print(f"Erro ao enriquecer image_prompts com IA: {e}")
        return plan

    def generate_visual_plan_for_music(self, title, concept, duration_seconds):
        """Generates a visual-only script synchronized with music duration"""
        self._load_config()
        
        # Calculate roughly how many scenes (approx 6-10 seconds per scene)
        num_scenes = max(5, duration_seconds // 8)
        
        prompt = f"""
        Create a visual script for a music video titled "{title}".
        Concept/Theme: {concept}
        Duration: {duration_seconds} seconds.
        
        Please generate {num_scenes} visual scenes that flow well with the music.
        The scenes should be highly descriptive and photorealistic.
        There is NO narration, just music.
        
        Rules for image_prompt:
        - Photorealistic, cinematic, 4k, professional photography, live-action style.
        - NO cartoon, illustration, or pixel art.
        
        Return valid JSON in this format:
        {{
            "scenes": [
                {{
                    "image_prompt": "Detailed description of the scene...",
                    "duration": 8,
                    "transition": "fade"
                }},
                ...
            ]
        }}
        """
        
        try:
            content = self._generate_text(prompt, system_prompt="You are a professional music video director. Return only JSON.", json_mode=True)
            if not content: return {"scenes": []}
            
            content = content.replace("```json", "").replace("```", "").strip()
            return json.loads(content)
        except Exception as e:
            print(f"Error generating visual plan for music: {e}")
            # Fallback
            return {
                "scenes": [
                    {
                        "image_prompt": f"Cinematic shot representing {title} - {concept}, photorealistic, 4k",
                        "duration": duration_seconds,
                        "transition": "fade"
                    }
                ]
            }

    def analyze_channel_strategy(self, stats, current_description):
        """Analisa estratégia do canal"""
        self._load_config()
        
        prompt = f"""
        Atue como um Especialista em Crescimento de YouTube (YouTube Strategist).
        Analise os dados deste canal:
        - Inscritos: {stats.get('subscribers')}
        - Views: {stats.get('views')}
        - Vídeos: {stats.get('videos')}
        - Descrição Atual: "{current_description}"
        
        Forneça um plano de ação curto e direto para alavancar este canal.
        Sugira um novo TÍTULO (Nome do Canal) otimizado e uma nova descrição otimizada.
        
        Retorne JSON:
        {{
            "analysis": "Sua análise...",
            "action_plan": ["Passo 1", "Passo 2", "Passo 3"],
            "title_suggestion": "Novo Nome Sugerido",
            "description_suggestion": "Nova descrição sugerida...",
            "banner_prompt": "Descrição visual para o banner do canal..."
        }}
        """
        
        if not self.openrouter_key:
            return {
                "analysis": "Simulação: O canal tem potencial mas precisa de consistência.",
                "action_plan": ["Postar 2x por semana", "Melhorar Thumbnails", "Focar em Shorts"],
                "title_suggestion": "Codexia - Livros & Mente",
                "description_suggestion": "Canal oficial sobre livros e desenvolvimento pessoal. Inscreva-se para transformar sua vida.",
                "banner_prompt": "Uma biblioteca mística com luz dourada, estilo digital art, alta qualidade, 4k"
            }
            
        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um estrategista de YouTube. Retorne apenas JSON.",
                json_mode=True
            )
            
            import json
            if not content:
                 raise Exception("Resposta vazia da IA")

            content = content.replace("```json", "").replace("```", "")
            return json.loads(content)
        except Exception as e:
            print(f"Erro na análise do canal: {e}")
            return {"error": str(e)}

    def generate_banner_image(self, prompt_text: str) -> str:
        prompt_text = (prompt_text or "").strip()
        if not prompt_text:
            return None
        return self.generate_image(
            f"{prompt_text}. YouTube Channel Banner, wide 16:9 aspect ratio, professional design, no text.",
            aspect_ratio="16:9",
        )

    def generate_monitor_report(self, stats):
        """Gera relatório curto de monitoramento"""
        self._load_config()
        
        prompt = f"""
        Analise o status atual do canal (Monitoramento em Tempo Real):
        - Inscritos: {stats.get('subscribers')}
        - Views: {stats.get('views')}
        - Vídeos: {stats.get('videos')}
        
        Forneça:
        1. Uma análise curta de 1 frase sobre o desempenho atual.
        2. Uma sugestão estratégica imediata (1 frase).
        
        Retorne JSON:
        {{
            "analysis": "...",
            "strategy": "..."
        }}
        """
        
        try:
            content = self._generate_text(prompt, json_mode=True)
            if not content:
                raise Exception("No content generated")
                
            import json
            content = content.replace("```json", "").replace("```", "").strip()
            return json.loads(content)
        except Exception as e:
            print(f"Error generating monitor report: {e}")
            return {
                "analysis": "Monitoramento simulado (Erro IA): Canal estável.",
                "strategy": "Continue postando regularmente para aumentar engajamento."
            }

    def generate_auto_insights(self, stats, recent_videos):
        """
        Gera insights automáticos sobre o canal, analisando impacto por vídeo
        e sugerindo novos conteúdos baseados nos melhores desempenhos.
        """
        self._load_config()
        
        import json
        videos_json = json.dumps(recent_videos, indent=2, default=str)
        
        prompt = f"""
        Atue como um Especialista Sênior em YouTube Analytics e Estratégia de Conteúdo.
        Nicho do canal (obrigatório respeitar): {(os.getenv("YOUTUBE_NICHE") or os.getenv("CHANNEL_NICHE") or os.getenv("CONTENT_NICHE") or "reflexão, espiritualidade e mensagens cristãs").strip()}.
        
        DADOS DO CANAL:
        - Nome: {stats.get('title')}
        - Inscritos: {stats.get('subscribers')}
        - Total Views: {stats.get('views')}
        - Total Vídeos: {stats.get('videos')}
        
        VÍDEOS RECENTES (Performance):
        {videos_json}
        
        SUA MISSÃO:
        1. Analise a evolução de cada vídeo recente e seu impacto no canal (quais trouxeram mais views/engajamento).
        2. Identifique o vídeo de MELHOR resultado (o "Campeão").
        3. Gere listas de ideias de vídeos longos e shorts baseados no campeão e também em dores/perguntas do público do nicho.
        4. Gere um plano de conteúdo semanal AUTOMÁTICO focado em ALAVANCAR esse sucesso.
        5. Inclua títulos com SEO + emoção (poesia com termo pesquisável) e CTA que estimule comentários.
        
        Retorne APENAS um JSON válido com a seguinte estrutura:
        {{
            "summary": "Resumo geral da saúde do canal e tendências identificadas.",
            "video_impact_analysis": [
                {{"video_title": "Título do Vídeo", "impact": "Análise curta do impacto"}}
            ],
            "best_video": {{
                "title": "Título do Melhor Vídeo",
                "reason": "Por que foi o melhor"
            }},
            "long_video_ideas": [
                {{"title": "Título Ideia 1", "concept": "Conceito..."}},
                {{"title": "Título Ideia 2", "concept": "Conceito..."}}
            ],
            "shorts_ideas": [
                {{"title": "Título Short 1", "concept": "Conceito..."}},
                {{"title": "Título Short 2", "concept": "Conceito..."}}
            ],
            "weekly_plan": [
                {{
                    "day": "Segunda-feira",
                    "theme": "Continuação do Sucesso",
                    "videos": [
                        {{
                            "title": "Título Sugerido",
                            "concept": "Explicação do conceito",
                            "time": "18:00",
                            "type": "video",
                            "auto_post": true
                        }}
                    ]
                }},
                {{
                    "day": "Quarta-feira",
                    "theme": "Short Viral",
                    "videos": [
                        {{
                            "title": "Título do Short",
                            "concept": "Hook rápido",
                            "time": "12:00",
                            "type": "short",
                            "auto_post": true
                        }}
                    ]
                }}
            ]
        }}
        """
        
        try:
            content = self._generate_text(
                prompt, 
                system_prompt="Você é um estrategista de YouTube focado em dados e crescimento viral.",
                json_mode=True
            )
            
            if not content:
                raise Exception("Resposta vazia da IA")
                
            clean_content = content.replace("```json", "").replace("```", "").strip()
            return json.loads(clean_content)
            
        except Exception as e:
            print(f"Erro ao gerar auto insights: {e}")
            # Mock fallback para não quebrar o frontend
            return {
                "summary": "Não foi possível gerar a análise detalhada neste momento.",
                "video_impact_analysis": [],
                "best_video": {"title": "N/A", "reason": "Erro na análise"},
                "long_video_ideas": [],
                "shorts_ideas": [],
                "weekly_plan": []
            }

    def generate_topic_suggestions(self, stats: dict, recent_videos: list, recent_comments: list, hours: int = 72) -> Dict[str, Any]:
        self._load_config()
        niche = (os.getenv("YOUTUBE_NICHE") or os.getenv("CHANNEL_NICHE") or os.getenv("CONTENT_NICHE") or "").strip()
        if not niche:
            niche = "reflexão, espiritualidade e mensagens cristãs (sem sensacionalismo falso)"
        try:
            hrs = int(hours or 72)
        except Exception:
            hrs = 72
        hrs = max(12, min(24 * 14, hrs))
        if not self.openrouter_key:
            return {
                "summary": "Sugestões simuladas (IA não configurada).",
                "niche": niche,
                "hours_window": hrs,
                "long_video_ideas": [],
                "shorts_ideas": [],
                "notes": [],
            }

        import json
        payload = {
            "stats": stats or {},
            "recent_videos": recent_videos or [],
            "recent_comments": recent_comments or [],
        }
        prompt = f"""
Atue como estrategista de conteúdo (YouTube + redes) para um canal do nicho: {niche}.

Objetivo: sugerir temas que tendem a performar bem nas próximas {hrs} horas, sem inventar fatos específicos. Use padrões de dores do público, termos pesquisáveis e o histórico do canal.

DADOS (use como base):
{json.dumps(payload, ensure_ascii=False)}

Regras:
- Sugira ideias coerentes com o nicho e com a mensagem (reflexão/espiritualidade).
- Crie títulos SEO + emoção (pode ser poético, mas inclua termo pesquisável e opcional "(Reflexão)").
- Para cada ideia, inclua um gancho (0-30s) direto na dor/sentimento do espectador.
- Inclua CTA com pergunta para comentários.

Retorne APENAS JSON válido:
{{
  "summary": "Resumo curto do que parece em alta no nicho e por quê (2-4 frases).",
  "hours_window": {hrs},
  "long_video_ideas": [
    {{"title": "...", "concept": "...", "hook_0_30s": "...", "cta_question": "..."}}
  ],
  "shorts_ideas": [
    {{"title": "...", "concept": "...", "hook_0_3s": "...", "cta_question": "..."}}
  ],
  "notes": ["..."]
}}
""".strip()

        raw = self._generate_text(
            prompt,
            system_prompt="Você é um estrategista de conteúdo e copywriter focado em retenção e SEO. Responda apenas JSON válido.",
            temperature=0.7,
            json_mode=True,
        )
        raw = (raw or "").replace("```json", "").replace("```", "").strip()
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {"summary": raw}
        if not isinstance(data, dict):
            return {"summary": "Resposta inválida", "raw": raw}
        data.setdefault("hours_window", hrs)
        return data

    def generate_monetization_insights(self, progress_data):
        """
        Gera insights focados em atingir a monetização do YouTube.
        """
        self._load_config()

        subs = progress_data.get('subscribers', 0)
        subs_target = progress_data.get('subscribers_target', 1000)
        hours = progress_data.get('estimated_watch_hours', 0)
        hours_target = progress_data.get('watch_hours_target', 4000)
        subs_pct = progress_data.get('subscribers_progress_pct', 0)
        hours_pct = progress_data.get('watch_hours_progress_pct', 0)
        subs_missing = max(0, subs_target - subs)
        hours_missing = max(0, hours_target - hours)

        prompt = f"""Atue como um Consultor Expert de Monetização do YouTube (YPP).

DADOS DO CANAL:
- Inscritos atuais: {subs} / Meta: {subs_target} (progresso: {subs_pct}%, faltam: {subs_missing})
- Horas de exibição estimadas: {hours} / Meta: {hours_target} (progresso: {hours_pct}%, faltam: {hours_missing})

REGRAS IMPORTANTES:
- Os valores de subscribers_missing e watch_hours_missing DEVEM ser calculados exatamente: {subs_missing} e {hours_missing} respectivamente.
- Se o canal JÁ atingiu uma meta, indique 0 faltante e parabenize.
- A estimativa de tempo deve ser realista (baseada no ritmo atual de crescimento).
- As ações semanais devem ser ESPECÍFICAS e ACIONÁVEIS (não genéricas).
- A estratégia deve priorizar o gap MAIOR (se faltam mais horas, foque em watch time; se faltam mais inscritos, foque em crescimento).

Retorne APENAS JSON válido com esta estrutura EXATA:
{{
    "summary": "Análise detalhada da situação atual do canal em relação à monetização (2-3 frases).",
    "gap_analysis": {{
        "subscribers_missing": {subs_missing},
        "watch_hours_missing": {hours_missing},
        "estimated_time_to_monetize": "Estimativa realista baseada no ritmo atual (ex: 2-3 meses)"
    }},
    "strategy_suggestion": "Estratégia principal detalhada para fechar o gap mais crítico.",
    "weekly_actions": [
        "Ação específica 1 com detalhes de implementação",
        "Ação específica 2 com detalhes de implementação",
        "Ação específica 3 com detalhes de implementação",
        "Ação específica 4 com detalhes de implementação",
        "Ação específica 5 com detalhes de implementação"
    ]
}}"""

        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um consultor especialista em monetização do YouTube Partner Program. Responda sempre em português do Brasil com dados precisos.",
                json_mode=True
            )

            if not content:
                raise Exception("Resposta vazia da IA")

            clean_content = content.replace("```json", "").replace("```", "").strip()
            result = json.loads(clean_content)

            if "gap_analysis" in result:
                result["gap_analysis"]["subscribers_missing"] = subs_missing
                result["gap_analysis"]["watch_hours_missing"] = hours_missing

            return result

        except Exception as e:
            print(f"Erro ao gerar insights de monetização: {e}")

            if subs >= subs_target and hours >= hours_target:
                summary = f"Parabéns! Seu canal já atingiu os requisitos de monetização: {subs} inscritos e ~{hours} horas de exibição."
                strategy = "Mantenha a consistência e solicite a revisão do YPP se ainda não o fez."
                time_est = "Elegível agora!"
            elif subs_pct > hours_pct:
                summary = f"Seu canal tem {subs} inscritos ({subs_pct}%) e ~{hours} horas de exibição ({hours_pct}%). O gap principal são as horas de exibição."
                strategy = "Foque em vídeos longos (10-20 min) e lives para aumentar as horas de exibição."
                time_est = "Depende do ritmo de publicação"
            else:
                summary = f"Seu canal tem {subs} inscritos ({subs_pct}%) e ~{hours} horas de exibição ({hours_pct}%). O gap principal são os inscritos."
                strategy = "Foque em Shorts virais e colaborações para crescer a base de inscritos."
                time_est = "Depende do ritmo de publicação"

            return {
                "summary": summary,
                "gap_analysis": {
                    "subscribers_missing": subs_missing,
                    "watch_hours_missing": hours_missing,
                    "estimated_time_to_monetize": time_est
                },
                "strategy_suggestion": strategy,
                "weekly_actions": [
                    f"Publicar pelo menos 3 vídeos longos (10+ min) para aumentar horas de exibição" if hours_pct < subs_pct else "Publicar 5 Shorts por semana para ganhar inscritos",
                    "Responder todos os comentários para aumentar engajamento e retenção",
                    "Criar thumbnails chamativas com CTR acima de 5%",
                    "Analisar Analytics para identificar vídeos com maior retenção e replicar o formato",
                    "Promover o canal em comunidades relevantes e redes sociais"
                ]
            }

    def transcribe_audio_segments(self, audio_path: str, language: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        info = self.transcribe_audio_segments_detailed(audio_path=audio_path, language=language)
        segs = info.get("segments") if isinstance(info, dict) else None
        return segs if isinstance(segs, list) else None

    def transcribe_audio_segments_detailed(self, audio_path: str, language: Optional[str] = None) -> Dict[str, Any]:
        self._load_config()
        api_key = (self.api_key or "").strip() if self.api_key else ""
        if not api_key:
            return {"segments": None, "error": "missing_api_key"}
        if not audio_path or not os.path.exists(audio_path):
            return {"segments": None, "error": "file_not_found"}
        client = openai.OpenAI(api_key=api_key)
        def _extract_openai_error(e: Exception) -> Dict[str, Any]:
            info: Dict[str, Any] = {}
            status = getattr(e, "status_code", None)
            if status is None:
                resp = getattr(e, "response", None)
                status = getattr(resp, "status_code", None)
            if status is not None:
                info["status"] = status

            body = getattr(e, "body", None)
            if body is None:
                resp = getattr(e, "response", None)
                try:
                    body = resp.json() if resp is not None else None
                except Exception:
                    body = None

            if isinstance(body, str) and body.strip():
                try:
                    import json
                    body = json.loads(body)
                except Exception:
                    pass

            if isinstance(body, dict):
                err = body.get("error")
                if isinstance(err, dict):
                    if err.get("type") is not None:
                        info["type"] = err.get("type")
                    if err.get("code") is not None:
                        info["code"] = err.get("code")
                    if err.get("message") is not None:
                        info["message"] = err.get("message")

            if not info.get("message"):
                info["message"] = str(e)
            return info

        try:
            with open(audio_path, "rb") as f:
                kwargs: Dict[str, Any] = {
                    "model": "whisper-1",
                    "file": f,
                    "timestamp_granularities": ["word", "segment"],
                }
                if language:
                    kwargs["language"] = language
                try:
                    res = client.audio.transcriptions.create(**kwargs)
                except TypeError:
                    kwargs.pop("timestamp_granularities", None)
                    res = client.audio.transcriptions.create(**kwargs)
        except Exception as e:
            return {"segments": None, "error": _extract_openai_error(e)}

        segments = None
        if hasattr(res, "segments"):
            segments = getattr(res, "segments")
        elif isinstance(res, dict):
            segments = res.get("segments")
        if not isinstance(segments, list):
            return {"segments": None, "error": "no_segments"}

        out: List[Dict[str, Any]] = []
        for s in segments:
            start = None
            end = None
            text = None
            words_raw = None
            if isinstance(s, dict):
                start = s.get("start")
                end = s.get("end")
                text = s.get("text")
                words_raw = s.get("words")
            elif hasattr(s, "model_dump"):
                try:
                    d = s.model_dump()
                    if isinstance(d, dict):
                        start = d.get("start")
                        end = d.get("end")
                        text = d.get("text")
                        words_raw = d.get("words")
                except Exception:
                    start = getattr(s, "start", None)
                    end = getattr(s, "end", None)
                    text = getattr(s, "text", None)
                    words_raw = getattr(s, "words", None)
            else:
                start = getattr(s, "start", None)
                end = getattr(s, "end", None)
                text = getattr(s, "text", None)
                words_raw = getattr(s, "words", None)
            try:
                start_f = float(start)
                end_f = float(end)
            except Exception:
                continue
            t = str(text or "").strip()
            if not t:
                continue
            words_out: Optional[List[Dict[str, Any]]] = None
            if isinstance(words_raw, list) and words_raw:
                w_items: List[Dict[str, Any]] = []
                for w in words_raw:
                    if not isinstance(w, dict):
                        continue
                    try:
                        ws = float(w.get("start"))
                        we = float(w.get("end"))
                    except Exception:
                        continue
                    ww = str(w.get("word") or w.get("text") or "").strip()
                    if not ww or we <= ws:
                        continue
                    w_items.append({"start": ws, "end": we, "word": ww})
                if w_items:
                    words_out = w_items
            out.append({"start": start_f, "end": end_f, "text": t, "words": words_out})
        return {"segments": out or None, "error": None if out else "empty_segments"}

    def generate_hotmart_suggestions(self, book_data):
        """
        Analisa um livro e gera sugestões otimizadas para publicação na Hotmart:
        - Título otimizado para vendas
        - Descrição persuasiva
        - Preço sugerido baseado no mercado
        - Categoria adequada
        - Tags relevantes
        - Copy de vendas
        """
        self._load_config()
        import json
        
        prompt = f"""
        Você é um especialista em marketing digital e vendas de produtos digitais na Hotmart.
        
        LIVRO PARA ANÁLISE:
        - Título: {book_data.get('title', 'Sem título')}
        - Autor: {book_data.get('author', 'Desconhecido')}
        - Sinopse: {book_data.get('synopsis', 'Sem sinopse')}
        - Preço Atual: R$ {book_data.get('price', 0)}
        - Capítulos: {', '.join(book_data.get('chapters', [])) if book_data.get('chapters') else 'Não informado'}
        
        SUA MISSÃO:
        1. Analise o conteúdo do livro e sugira um TÍTULO otimizado para vendas (pode ser diferente do original, mas mantendo a essência).
        2. Crie uma DESCRIÇÃO persuasiva e otimizada para conversão (máximo 2000 caracteres).
        3. Sugira um PREÇO competitivo baseado no mercado brasileiro de produtos digitais similares.
        4. Identifique a CATEGORIA mais adequada na Hotmart (ex: Educação, Negócios, Desenvolvimento Pessoal, etc.).
        5. Liste 5-10 TAGS relevantes para SEO e descoberta.
        6. Crie um COPY DE VENDAS curto (2-3 parágrafos) destacando os principais benefícios.
        7. Sugira um SUBTÍTULO chamativo.
        
        Retorne APENAS um JSON válido:
        {{
            "optimized_title": "Título otimizado para vendas",
            "subtitle": "Subtítulo chamativo",
            "description": "Descrição completa e persuasiva do produto...",
            "sales_copy": "Copy de vendas destacando benefícios...",
            "suggested_price": 97.00,
            "category": "Educação",
            "tags": ["tag1", "tag2", "tag3"],
            "key_benefits": [
                "Benefício 1",
                "Benefício 2",
                "Benefício 3"
            ],
            "target_audience": "Descrição do público-alvo",
            "marketing_notes": "Observações importantes para marketing"
        }}
        """
        
        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um especialista em marketing digital e vendas de produtos digitais na Hotmart.",
                json_mode=True
            )
            
            if not content:
                raise Exception("Resposta vazia da IA")
                
            clean_content = content.replace("```json", "").replace("```", "").strip()
            suggestions = json.loads(clean_content)
            
            return suggestions
            
        except Exception as e:
            print(f"Erro ao gerar sugestões Hotmart: {e}")
            
            # Fallback com dados básicos
            return {
                "optimized_title": book_data.get('title', 'Sem título'),
                "subtitle": f"Por {book_data.get('author', 'Autor')}",
                "description": book_data.get('synopsis', 'Sem descrição disponível.'),
                "sales_copy": f"Descubra {book_data.get('title', 'este livro')} e transforme sua vida.",
                "suggested_price": book_data.get('price', 97.00),
                "category": "Educação",
                "tags": ["livro", "digital", "educação"],
                "key_benefits": [
                    "Conteúdo de qualidade",
                    "Acesso imediato",
                    "Suporte ao cliente"
                ],
                "target_audience": "Pessoas interessadas em desenvolvimento pessoal",
                "marketing_notes": "Configure as sugestões manualmente se necessário."
            }

    def generate_hotmart_suggestions_sync(self, book_data, changed_field, new_value, current_form):
        """
        Regenera campos relacionados quando o usuário altera manualmente um campo.
        Mantém consistência entre título, descrição, copy de vendas, etc.
        """
        self._load_config()
        import json
        
        # Mapeia qual campo foi alterado e quais devem ser atualizados
        field_dependencies = {
            "name": ["sales_copy", "description", "subtitle"],  # Se título muda, atualiza copy, descrição e subtítulo
            "description": ["sales_copy", "key_benefits"],  # Se descrição muda, atualiza copy e benefícios
            "subtitle": ["sales_copy"],  # Se subtítulo muda, atualiza copy
            "price": [],  # Preço não afeta outros campos
            "category": ["tags"],  # Se categoria muda, pode atualizar tags
            "tags": []  # Tags não afetam outros campos
        }
        
        fields_to_update = field_dependencies.get(changed_field, [])
        
        if not fields_to_update:
            return {}  # Nenhum campo precisa ser atualizado
        
        prompt = f"""
        Você é um especialista em marketing digital e vendas de produtos digitais na Hotmart.
        
        CONTEXTO DO LIVRO:
        - Título ATUAL (alterado pelo usuário): {current_form.get('name') or book_data.get('title')}
        - Autor: {book_data.get('author', 'Desconhecido')}
        - Descrição ATUAL: {current_form.get('description') or book_data.get('synopsis', '')}
        - Subtítulo ATUAL: {current_form.get('subtitle', '')}
        - Preço: R$ {current_form.get('price') or book_data.get('price', 0)}
        - Categoria: {current_form.get('category', '')}
        
        CAMPO ALTERADO:
        - Campo: {changed_field}
        - Novo Valor: {new_value}
        
        SUA MISSÃO:
        Atualize APENAS os seguintes campos para manter consistência com a alteração feita:
        {', '.join(fields_to_update)}
        
        IMPORTANTE:
        - Use o título "{current_form.get('name') or book_data.get('title')}" em TODOS os textos gerados
        - Mantenha o tom e estilo profissional
        - Garanta que todos os textos mencionem o título correto
        - Se o campo alterado foi o título, atualize o copy de vendas para usar o novo título
        
        Retorne APENAS um JSON válido com os campos atualizados:
        {{
            "sales_copy": "Novo copy de vendas usando o título correto...",
            "description": "Nova descrição se necessário...",
            "subtitle": "Novo subtítulo se necessário...",
            "key_benefits": ["Benefício 1", "Benefício 2", "Benefício 3"]
        }}
        
        Inclua APENAS os campos que estão na lista: {fields_to_update}
        """
        
        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um especialista em marketing digital e vendas de produtos digitais na Hotmart. Mantenha consistência entre todos os textos.",
                json_mode=True
            )
            
            if not content:
                raise Exception("Resposta vazia da IA")
                
            clean_content = content.replace("```json", "").replace("```", "").strip()
            updated_fields = json.loads(clean_content)
            
            # Retorna apenas os campos que devem ser atualizados
            result = {}
            for field in fields_to_update:
                if field in updated_fields:
                    result[field] = updated_fields[field]
            
            return result
            
        except Exception as e:
            print(f"Erro ao sincronizar campos Hotmart: {e}")
            return {}

    def _build_prompt(self, title, synopsis, style):
        if style == "cliffhanger":
            return f"Crie um anúncio curto e misterioso para o livro '{title}'. Sinopse: {synopsis}. Termine com um gancho forte."
        elif style == "storytelling":
            return f"Conte uma história curta e emocionante baseada no livro '{title}'. Sinopse: {synopsis}. Foque na jornada do herói."
        else: # direct
            return f"Crie um anúncio de vendas direto e persuasivo para o livro '{title}'. Sinopse: {synopsis}. Liste 3 benefícios e faça uma oferta irresistível."

    def generate_content_plan(self, theme, duration_type="days", duration_value=7, start_date=None, videos_per_day=1, shorts_per_day=0, video_duration=5):
        """Gera plano de conteúdo personalizado"""
        self._load_config()
        
        from datetime import datetime, timedelta
        import json
        
        if not start_date:
            start_date_obj = datetime.now() + timedelta(days=1)
        else:
            try:
                start_date_obj = datetime.strptime(start_date, "%Y-%m-%d")
            except:
                start_date_obj = datetime.now() + timedelta(days=1)
                
        total_days = int(duration_value)
        if duration_type == "weeks":
            total_days = total_days * 7
        elif duration_type == "months":
            total_days = total_days * 30
            
        # Limit total days to 30 for safety in this iteration to avoid timeouts/context limits
        if total_days > 31:
            total_days = 31

        prompt = f"""
        Crie um planejamento de conteúdo para um canal do YouTube sobre o tema '{theme}'.
        Período: {total_days} dias, começando em {start_date_obj.strftime('%d/%m/%Y')}.
        
        Para CADA dia ({total_days} dias), eu preciso EXATAMENTE de:
        1. {videos_per_day} Vídeo(s) Longo(s) (type="video") com duração de {video_duration} min.
        2. {shorts_per_day} Vídeo(s) Curto(s) (type="short") com duração de 1 min.
        
        IMPORTANTE: As datas devem ser sequenciais a partir de {start_date_obj.strftime('%Y-%m-%d')}.
        Respeite rigorosamente a quantidade de vídeos e shorts por dia solicitada.
        
        Retorne APENAS um JSON válido com a estrutura:
        {{
            "plan": [
                {{
                    "date": "YYYY-MM-DD",
                    "theme_of_day": "Tema do dia",
                    "videos": [
                        {{
                            "title": "Título",
                            "concept": "Ideia do vídeo",
                            "time": "HH:MM",
                            "type": "video",
                            "duration": {video_duration}
                        }},
                        {{
                            "title": "Título do Short",
                            "concept": "Ideia do short",
                            "time": "HH:MM",
                            "type": "short",
                            "duration": 1
                        }}
                    ]
                }}
            ]
        }}
        """
        
        try:
            content = self._generate_text(prompt, json_mode=True)
            if not content:
                 raise Exception("Resposta vazia da IA ou nenhum provedor configurado")

            content = content.replace("```json", "").replace("```", "").strip()
            return json.loads(content)
            
        except Exception as e:
            error_msg = str(e)
            print(f"Erro ao gerar plano: {error_msg}")
            
            # Mock fallback
            mock_plan = []
            for i in range(total_days):
                current_date = start_date_obj + timedelta(days=i)
                day_videos = []
                
                # Mock Videos
                for v in range(int(videos_per_day)):
                    hour = 8 + (v * 4) # 8, 12, 16...
                    if hour > 22: hour = 22
                    day_videos.append({
                        "title": f"Vídeo {v+1}: {theme} {i+1}", 
                        "concept": f"Conceito vídeo {v+1}", 
                        "time": f"{hour:02d}:00", 
                        "type": "video",
                        "duration": video_duration
                    })
                
                # Mock Shorts
                for s in range(int(shorts_per_day)):
                    hour = 10 + (s * 2) # 10, 12, 14...
                    if hour > 23: hour = 23
                    day_videos.append({
                        "title": f"Short {s+1}: {theme}", 
                        "concept": "Curiosidade rápida", 
                        "time": f"{hour:02d}:30", 
                        "type": "short",
                        "duration": 1
                    })

                mock_plan.append({
                    "day": i + 1,
                    "date": current_date.strftime('%Y-%m-%d'),
                    "theme_of_day": f"Tema do Dia {i+1}: {theme}",
                    "videos": day_videos
                })
            
            return {"plan": mock_plan}

    def _mock_response(self, title, style, error=None, duration=None, **kwargs):
        base_msg = f"⚠️ MODO SIMULAÇÃO (Vá em Configurações e adicione sua chave OpenAI)\n\n"
        if error:
            base_msg += f"Erro detectado: {error}\n\n"
            
        if style == "cliffhanger":
            return base_msg + f"🔥 [Simulação] O mistério de '{title}' vai te prender..."
        elif style == "storytelling":
            return base_msg + f"📖 [Simulação] Quando escrevi '{title}', eu queria..."
        elif style == "motivational_long":
            import json
            
            # Simple scaling of scenes based on duration if provided
            num_scenes = 3
            if duration:
                try:
                    num_scenes = max(3, int(duration) * 2)
                except:
                    pass
            
            scenes = []
            scenes.append({"text": f"Bem-vindo a este vídeo sobre {title}. A vida é cheia de desafios...", "image_prompt": "Mountain peak sunrise"})
            
            for i in range(num_scenes - 2):
                scenes.append({"text": f"O passo {i+1} é acreditar em si mesmo e nunca desistir, pois a persistência é a chave.", "image_prompt": f"Motivational scene {i+1} nature landscape"})
                
            scenes.append({"text": "Acredite em si mesmo e conquiste seus sonhos.", "image_prompt": "Lion looking at horizon"})

            return {
                "title": f"Motivação: {title} (Vídeo Épico)",
                "description": "Vídeo motivacional gerado automaticamente.",
                "scenes": scenes,
                "music_mood": "epic"
            }
        else:
            return base_msg + f"🎬 [Simulação] Roteiro para '{title}'..."

    def generate_image(self, prompt, aspect_ratio: str = "9:16", providers: list = None, status_callback=None):
        """
        Gera imagem usando APENAS OpenAI Images API.
        Se falhar, levanta exceção e não tenta nenhum outro provedor.
        """
        self._load_config()
        raw_prompt = (prompt or "").strip()
        if not raw_prompt:
            return None
        raw_prompt = self._sanitize_and_contextualize_image_prompt(raw_prompt)

        def notify(message: str):
            if status_callback:
                try:
                    status_callback(message)
                except Exception:
                    pass

        if not (self.api_key or "").strip():
            raise Exception("OpenAI não configurada (OPENAI_API_KEY ausente).")

        size = "1024x1024"

        neg = self._visual_negative_for_text(raw_prompt)
        full_prompt = (
            f"{raw_prompt}. "
            "Cinematic, epic, high quality, dramatic lighting, professional composition. "
            "Photorealistic cinematic photography. "
            "No text, no letters, no numbers, no captions, no subtitles, no signage, no watermarks, no logos. "
            f"Negative prompt: {neg}."
        ).strip()

        base_dir = Path("generated_assets/openai_images")
        base_dir.mkdir(parents=True, exist_ok=True)
        filename = f"img_{uuid.uuid4().hex}.png"
        out_path = base_dir / filename

        def _extract_openai_error_message(err: Exception) -> str:
            raw = ""
            try:
                raw = str(err or "").strip()
            except Exception:
                raw = ""
            try:
                body = getattr(err, "body", None)
                if body:
                    raw = f"{raw} | body={body}"
            except Exception:
                pass
            low = raw.lower()
            if not raw:
                return "Erro desconhecido ao chamar a OpenAI Images API."
            if "api key" in low or "invalid_api_key" in low or "incorrect api key" in low or "unauthorized" in low or "401" in low:
                return "Falha na autenticação da OpenAI. Verifique a OpenAI API Key em Configurações."
            if "insufficient_quota" in low or "quota" in low or "billing" in low or "credit" in low:
                return "A OpenAI recusou a geração por falta de saldo/quota. Verifique faturamento e créditos da conta."
            if "rate limit" in low or "429" in low or "too many requests" in low:
                return "Limite de requisições da OpenAI atingido. Aguarde um pouco e tente novamente."
            if "content_policy" in low or "safety" in low or "policy" in low or "moderation" in low:
                return "A OpenAI bloqueou o prompt pela política de conteúdo. Ajuste a descrição da imagem."
            if "model" in low and ("not found" in low or "does not exist" in low or "unsupported" in low):
                return "O modelo de imagem da OpenAI não está disponível nessa conta/SDK. Verifique o acesso ao `gpt-image-1`."
            return f"Falha ao gerar imagem na OpenAI: {raw[:500]}"

        notify("Gerando imagem com OpenAI...")
        try:
            if hasattr(openai, "OpenAI"):
                client = openai.OpenAI(api_key=(self.api_key or "").strip())
                result = client.images.generate(
                    model="gpt-image-1",
                    prompt=full_prompt,
                    size=size,
                )
                item0 = result.data[0] if result and getattr(result, "data", None) else None
                image_base64 = getattr(item0, "b64_json", None) if item0 is not None else None
            else:
                raise Exception("SDK OpenAI desatualizado. Requer openai>=1.0.0.")
        except Exception as e:
            print("OPENAI IMAGE ERROR RAW:", repr(e))
            raise Exception(_extract_openai_error_message(e))

        try:
            image_base64 = (image_base64 or "").strip() if isinstance(image_base64, str) else ""
            if not image_base64:
                raise Exception("OpenAI não retornou b64_json na imagem.")
            image_bytes = base64.b64decode(image_base64)
            with open(out_path, "wb") as f:
                f.write(image_bytes)
            if not out_path.exists() or out_path.stat().st_size < 1024:
                raise Exception("OpenAI não retornou bytes válidos para a imagem.")
            return f"/generated_assets/openai_images/{filename}"
        except Exception as e:
            print("OPENAI IMAGE ERROR RAW:", repr(e))
            raise Exception(f"Falha ao processar a imagem retornada pela OpenAI: {str(e)[:500]}")

    def generate_audio(self, text, voice="onyx", voice_settings: Optional[Dict[str, Any]] = None):
        """Gera áudio usando Eden AI (ElevenLabs) com fallback opcional."""
        diagnostics = self.generate_audio_with_diagnostics(text, voice=voice, voice_settings=voice_settings)
        return diagnostics.get("audio_content")

    def _generate_audio_edenai_elevenlabs(self, text: str, voice_hint: str = "onyx", voice_settings: Optional[Dict[str, Any]] = None):
        if not (self.edenai_key or "").strip() or not text or not text.strip():
            return None
        try:
            hint = (voice_hint or "").strip().lower()
            custom_voice_id = (self.elevenlabs_voice_id or "").strip()
            env_voice_male = os.getenv("ELEVENLABS_VOICE_ID_MALE", "").strip()
            env_voice_female = os.getenv("ELEVENLABS_VOICE_ID_FEMALE", "").strip()
            env_voice_default = os.getenv("ELEVENLABS_VOICE_ID", "").strip()

            voice_map = {
                "nova": env_voice_female or "EXAVITQu4vr4xnSDxMaL",
                "shimmer": env_voice_female or "EXAVITQu4vr4xnSDxMaL",
                "onyx": env_voice_male or "VR6AewLTigWG4xSOukaG",
                "echo": env_voice_male or "VR6AewLTigWG4xSOukaG",
                "fable": env_voice_female or "EXAVITQu4vr4xnSDxMaL",
            }

            if hint in {"my_voice", "myvoice", "minha_voz", "minhavoz", "custom"} and custom_voice_id:
                voice_id = custom_voice_id
            else:
                voice_id = env_voice_default or voice_map.get(hint)

            headers = {"Authorization": f"Bearer {self.edenai_key.strip()}"}
            payload = {
                "providers": "elevenlabs",
                "text": text[:5000],
                "language": "pt-BR",
            }
            if voice_id:
                payload["voice_id"] = voice_id
                payload["voice"] = voice_id
            if isinstance(voice_settings, dict) and voice_settings:
                payload["settings"] = {"elevenlabs": {"voice_settings": voice_settings}}

            r = requests.post(
                "https://api.edenai.run/v2/audio/text_to_speech",
                headers=headers,
                json=payload,
                timeout=120,
            )
            if r.status_code >= 400:
                print(f"Eden AI TTS HTTP {r.status_code}: {(r.text or '')[:240]}")
                return None
            data = r.json() if (r.headers.get("content-type") or "").startswith("application/json") else {}

            def extract_url(obj):
                if isinstance(obj, dict):
                    for k in ("audio_resource_url", "audio_url", "url"):
                        v = obj.get(k)
                        if isinstance(v, str) and v.startswith("http"):
                            return v
                return None

            provider_payload = data.get("elevenlabs") if isinstance(data, dict) else None
            audio_url = extract_url(provider_payload) or extract_url(data)
            if not audio_url:
                return None

            rr = requests.get(audio_url, timeout=120)
            if rr.status_code >= 400 or not rr.content:
                return None
            return rr.content
        except Exception as e:
            print(f"Eden AI TTS error: {e}")
            return None

    def _generate_audio_elevenlabs(self, text: str, voice_hint: str = "onyx", voice_settings: Optional[Dict[str, Any]] = None):
        """Gera áudio usando ElevenLabs API (vozes ultra-realistas)."""
        if not self.elevenlabs_key or not text or not text.strip():
            return None
        try:
            voice_meta = self._resolve_elevenlabs_voice_selection(voice_hint)
            voice_id = str(voice_meta.get("voice_id_used") or "").strip()
            if not voice_id:
                return None
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            headers = {"xi-api-key": self.elevenlabs_key, "Content-Type": "application/json"}
            settings = {
                "stability": 0.35,
                "similarity_boost": 0.85,
                "style": 0.35,
                "use_speaker_boost": True,
            }
            if isinstance(voice_settings, dict):
                for k in ("stability", "similarity_boost", "style", "use_speaker_boost"):
                    if k in voice_settings:
                        settings[k] = voice_settings[k]

            payload = {
                "text": text[:5000],
                "model_id": "eleven_multilingual_v2",
                "voice_settings": settings,
            }
            r = requests.post(url, json=payload, headers=headers, timeout=120)
            if r.status_code == 200:
                return r.content
            print(f"ElevenLabs TTS HTTP {r.status_code}: {r.text[:240]}")
        except Exception as e:
            print(f"ElevenLabs TTS error: {e}")
        return None

    def generate_song_lyrics(self, theme: str, message: str, language: str = "pt-BR", style: str = "", genre: str = ""):
        self._load_config()
        lang = (language or "pt-BR").strip()
        theme = (theme or "").strip()
        message = (message or "").strip()
        style = (style or "").strip()
        genre = (genre or "").strip()

        if not theme or not message:
            return {"title": "Música", "lyrics": ""}

        if not self.openrouter_key:
            title = f"{theme.title()} - Recomeçar"
            lyrics = (
                f"Verso 1\n"
                f"No silêncio eu me encontrei\n"
                f"Quando tudo parecia não ter fim\n"
                f"Guardei no peito o que eu sonhei\n"
                f"E fiz da queda um novo sim\n\n"
                f"Pré-Refrão\n"
                f"Eu ouvi a vida me chamar\n"
                f"Pra levantar e continuar\n\n"
                f"Refrão\n"
                f"{message}\n"
                f"Eu vou seguir sem olhar pra trás\n"
                f"Se a tempestade vem, eu faço paz\n"
                f"{message}\n\n"
                f"Verso 2\n"
                f"Se o medo tenta me prender\n"
                f"Eu lembro quem eu decidi ser\n"
                f"Cada cicatriz me faz crescer\n"
                f"E a esperança volta a aparecer\n\n"
                f"Refrão\n"
                f"{message}\n"
                f"Eu vou seguir sem olhar pra trás\n"
                f"Se a tempestade vem, eu faço paz\n"
                f"{message}\n\n"
                f"Ponte\n"
                f"Eu não nasci pra desistir\n"
                f"Eu nasci pra renascer\n\n"
                f"Refrão Final\n"
                f"{message}\n"
                f"Eu vou seguir sem olhar pra trás\n"
                f"Se a tempestade vem, eu faço paz\n"
                f"{message}\n"
            )
            return {"title": title, "lyrics": lyrics}

        combined = f"{style} {genre}".strip().lower()
        combined = combined.replace("ç", "c").replace("ã", "a").replace("á", "a").replace("à", "a").replace("â", "a")
        combined = combined.replace("é", "e").replace("ê", "e")
        combined = combined.replace("í", "i")
        combined = combined.replace("ó", "o").replace("ô", "o").replace("õ", "o")
        combined = combined.replace("ú", "u")

        extra_rules = ""
        if any(k in combined for k in ["pentecostal", "corinho", "corinho de fogo", "fogo no pe", "fogo no pe'"]):
            extra_rules = (
                "\nRegras específicas do estilo (Corinho / Pentecostal):\n"
                "- Linguagem de culto congregacional (igreja pequena), direta e simples.\n"
                "- Frases curtas e rítmicas, fáceis de cantar em grupo.\n"
                "- Refrão com chamada-e-resposta (com repetições) e energia alta.\n"
                "- Evite romantização/sofrência e gírias seculares.\n"
            )
        if any(k in combined for k in ["corinho tradicional", "culto de oracao", "culto de oração"]):
            extra_rules += (
                "- Puxada de 'marcha pentecostal' (rápida), com temática de oração, vitória, fogo e comunhão.\n"
                "- Evite totalmente sonoridade sertaneja/country (banjo, viola caipira, rodeio, sofrência).\n"
            )
        if "pentecostal raiz" in combined:
            extra_rules += (
                "- Clima 'raiz' e acústico (violão/pandeiro/bateria), mantendo simplicidade e impacto.\n"
                "- Evite elementos modernos/eletro e metálicos; foque no percussivo.\n"
            )

        prompt = f"""
Crie uma letra de música ORIGINAL baseada no tema e na mensagem.

Tema: {theme}
Mensagem: {message}
Idioma: {lang}
Estilo: {style or 'livre'}
Gênero: {genre or 'livre'}

Regras:
- Letra com estrutura clara: Verso 1, Pré-Refrão, Refrão, Verso 2, Refrão, Ponte, Refrão Final.
- Sem palavrões.
- Sem citar marcas, artistas ou músicas existentes.
- Sem usar markdown.
- Refrão deve repetir a mensagem de forma memorável.
{extra_rules}

Retorne APENAS um JSON válido no formato:
{{
  "title": "Título curto e memorável",
  "lyrics": "Letra completa com quebras de linha"
}}
"""
        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um compositor profissional. Retorne somente JSON válido.",
                temperature=0.85,
                json_mode=True
            )
            if not content:
                raise Exception("Resposta vazia da IA")
            import json
            clean = content.replace("```json", "").replace("```", "").strip()
            data = json.loads(clean)
            title = (data.get("title") or "Música").strip()[:120]
            lyrics = (data.get("lyrics") or "").strip()
            if not lyrics:
                raise Exception("Letra vazia")
            return {"title": title, "lyrics": lyrics}
        except Exception as e:
            print(f"Erro ao gerar letra: {e}")
            title = f"{theme.title()} - Mensagem"
            lyrics = f"Verso 1\n{theme}\n\nRefrão\n{message}\n"
            return {"title": title, "lyrics": lyrics}

    def improve_song_lyrics(self, lyrics: str, instruction: str, language: str = "pt-BR", style: str = "", genre: str = ""):
        self._load_config()
        original = (lyrics or "").strip()
        req = (instruction or "").strip()
        lang = (language or "pt-BR").strip()
        style = (style or "").strip()
        genre = (genre or "").strip()

        if not original or not req:
            return {"lyrics": original}

        if not self.openrouter_key:
            return {"lyrics": original}

        combined = f"{style} {genre}".strip().lower()
        combined = combined.replace("ç", "c").replace("ã", "a").replace("á", "a").replace("à", "a").replace("â", "a")
        combined = combined.replace("é", "e").replace("ê", "e")
        combined = combined.replace("í", "i")
        combined = combined.replace("ó", "o").replace("ô", "o").replace("õ", "o")
        combined = combined.replace("ú", "u")

        extra_rules = ""
        if any(k in combined for k in ["pentecostal", "corinho", "corinho de fogo", "fogo no pe", "fogo no pe'"]):
            extra_rules = (
                "\nRegras específicas do estilo (Corinho / Pentecostal):\n"
                "- Linguagem congregacional, simples e direta.\n"
                "- Frases curtas, rítmicas e repetíveis.\n"
                "- Refrão forte com chamada-e-resposta e energia alta.\n"
                "- Evite romantização/sofrência e gírias seculares.\n"
            )
        if any(k in combined for k in ["corinho tradicional", "culto de oracao", "culto de oração"]):
            extra_rules += (
                "- Puxada de marcha pentecostal, temática de oração/vitória.\n"
                "- Evite totalmente sonoridade sertaneja/country e linguagem associada.\n"
            )
        if "pentecostal raiz" in combined:
            extra_rules += (
                "- Clima 'raiz' e acústico (violão/pandeiro/bateria), simplicidade e impacto.\n"
            )

        import json
        prompt = f"""
Você é um revisor e compositor profissional de letras de música cristã em {lang}.

OBJETIVO:
Aplicar o pedido de melhoria do usuário na letra existente, mantendo o sentido, coerência, ritmo cantável e contexto teológico.

PEDIDO DO USUÁRIO (execute com precisão):
{req}

LETRA ORIGINAL:
{original[:5200]}

REGRAS:
- Preserve o tema, a mensagem e o contexto bíblico/teológico.
- Preserve a estrutura (Verso/Refrão/Ponte etc.) e as quebras de linha. Não transforme em prosa.
- Faça a escansão (métrica/ritmo das sílabas) internamente quando o pedido mencionar contagem de sílabas, sílabas tônicas, métrica ou "8 sílabas", e ajuste a(s) frase(s) para cumprir exatamente.
- Se o pedido mencionar rima rica/consoante, garanta rimas ricas nas linhas relevantes sem forçar palavras estranhas.
- Se o pedido mencionar substituir ou ajustar uma frase específica, altere apenas o mínimo necessário no restante da letra para manter fluidez e coerência.
- Não inclua marcas, links, nomes de artistas, nem explicações.
{extra_rules}

Retorne APENAS um JSON válido no formato:
{{ "lyrics": "Letra completa melhorada com quebras de linha" }}
"""
        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um compositor e revisor. Retorne somente JSON válido no formato solicitado.",
                temperature=0.55,
                json_mode=True,
            )
            if not content:
                raise Exception("Resposta vazia da IA")
            clean = content.replace("```json", "").replace("```", "").strip()
            data = json.loads(clean) if clean else {}
            improved = (data.get("lyrics") or "").strip() if isinstance(data, dict) else ""
            if not improved:
                raise Exception("Letra vazia")
            return {"lyrics": improved}
        except Exception as e:
            print(f"Erro ao melhorar letra: {e}")
            return {"lyrics": original}

    def lyrics_to_music_prompt(self, lyrics: str, title: str = "", genre: str = ""):
        """Converte letra em prompt para geração de música instrumental (MusicGen)."""
        self._load_config()
        if not self.openrouter_key:
            return f"Emotional instrumental music, {genre or 'pop ballad'}. Cinematic, no lyrics."
        prompt = f"""Com base nesta letra, crie UM prompt em inglês para música INSTRUMENTAL (sem voz). Uma frase curta (até 80 palavras).
Título: {title or 'Sem título'}
Gênero: {genre or 'qualquer'}
Letra: {lyrics[:1200]}
Retorne APENAS o prompt, sem aspas."""
        try:
            out = self._generate_text(prompt, system_prompt="You output only the music prompt.", temperature=0.7)
            return (out or "").strip()[:300] or f"Emotional instrumental, {genre or 'cinematic'}. No lyrics."
        except Exception as e:
            print(f"Erro ao gerar prompt de música: {e}")
            return f"Emotional instrumental music, {genre or 'pop'}. Cinematic, no lyrics."

    def lyrics_to_clip_scenes(self, lyrics: str, title: str = ""):
        """Converte letra em cenas (texto + image_prompt) para clipe."""
        self._load_config()
        import re
        lines = [l.strip() for l in (lyrics or "").strip().split("\n") if l.strip()]
        label_re = re.compile(r"^(verso|refr[aã]o|pr[eé]-?refr[aã]o|ponte|intro|outro|coro|bridge|chorus)\b", re.IGNORECASE)
        lines = [l for l in lines if not label_re.match(l)]
        if not lines:
            return [{"text": title or "Música", "image_prompt": "abstract music visual"}]
        scenes = []
        for block in lines:
            if self.openrouter_key:
                prompt = f"""Lyric line (Portuguese): "{block[:260]}". Song title: {title or 'Song'}. Create ONE image prompt in English for a photorealistic cinematic music video scene that matches the lyric literally. No text in image. One sentence."""
                try:
                    ip = self._generate_text(prompt, system_prompt="Output only the image prompt.", temperature=0.7)
                    image_prompt = (ip or "").strip()[:250] or "cinematic music video scene"
                except Exception:
                    image_prompt = "cinematic music video scene"
            else:
                image_prompt = "cinematic music video scene"
            scenes.append({"text": block, "image_prompt": image_prompt})
        return scenes if scenes else [{"text": title or "Música", "image_prompt": "abstract music visual"}]

    def generate_music(self, prompt):
        """Gera música usando Hugging Face (MusicGen)"""
        # Se não tiver token, tenta sem (pode falhar por rate limit)
        # URL atualizada conforme erro 410
        API_URL = "https://router.huggingface.co/models/facebook/musicgen-small"
        # Fallback URL antiga se necessário
        # API_URL = "https://api-inference.huggingface.co/models/facebook/musicgen-small"
        
        headers = {}
        if self.hf_token:
            headers["Authorization"] = f"Bearer {self.hf_token}"
        
        # Otimiza o prompt para música de fundo
        music_prompt = f"Background music, {prompt}. High quality, cinematic, ambient, no lyrics, loopable."
        
        try:
            payload = {"inputs": music_prompt}
            response = requests.post(API_URL, headers=headers, json=payload)
            
            if response.status_code == 200:
                return response.content
            else:
                print(f"Erro HF MusicGen: {response.status_code} - {response.text}")
                return None
        except Exception as e:
            print(f"Erro ao gerar música: {e}")
            return None
