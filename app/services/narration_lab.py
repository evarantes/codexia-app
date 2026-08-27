"""Isolated narration samples with hard cost and rendering boundaries.

This service deliberately does not import the video pipeline, task manager or
image providers. A request can create one short audio sample and its metadata;
it can never enqueue or render a video.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from filelock import FileLock, Timeout

from app.config import AUDIO_OUTPUT_DIR
from app.services.ai_generator import AIContentGenerator
from app.services.narration_contract_guard import (
    NarrationContractError,
    validate_narration_text,
)
from app.services.provider_config import resolve_global_provider_settings


NARRATION_LAB_MAX_CHARS = 1200
NARRATION_LAB_MAX_SAMPLES_PER_USER = 20
NARRATION_LAB_RETENTION_SECONDS = 7 * 24 * 60 * 60
NARRATION_LAB_SAMPLE_ID_RE = re.compile(r"^[a-f0-9]{32}$")

OPENAI_VOICES = (
    ("auto", "Automática (recomendada)"),
    ("alloy", "Alloy"),
    ("ash", "Ash"),
    ("ballad", "Ballad"),
    ("coral", "Coral"),
    ("echo", "Echo"),
    ("fable", "Fable"),
    ("nova", "Nova"),
    ("onyx", "Onyx"),
    ("sage", "Sage"),
    ("shimmer", "Shimmer"),
    ("verse", "Verse"),
)

ELEVENLABS_VOICES = (
    ("auto", "Automática (recomendada)"),
    ("nova", "Feminina natural"),
    ("onyx", "Masculina profunda"),
    ("shimmer", "Feminina jovem"),
    ("echo", "Masculina jovem"),
    ("fable", "Narrativa"),
)

EDGE_VOICES = (
    ("auto", "Automática (recomendada)"),
    ("pt-BR-AntonioNeural", "Antônio — masculina"),
    ("pt-BR-FranciscaNeural", "Francisca — feminina"),
)

VOICE_STYLES = (
    ("human", "Humana e natural"),
    ("soft_prayer", "Suave / oração"),
    ("solemn", "Solene"),
    ("energetic", "Enérgica"),
)


class NarrationLabError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400, code: str = "NARRATION_LAB_ERROR"):
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code or "NARRATION_LAB_ERROR")


def _truthy_env(*names: str) -> bool:
    for name in names:
        if str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "sim", "on", "enabled"}:
            return True
    return False


def _voice_options(items: tuple) -> List[Dict[str, str]]:
    return [{"id": voice_id, "label": label} for voice_id, label in items]


class NarrationLabService:
    def __init__(self, output_root: Optional[str] = None):
        base = Path(output_root or AUDIO_OUTPUT_DIR)
        self.output_root = base / "narration_lab"
        self.output_root.mkdir(parents=True, exist_ok=True)

    def provider_options(self, settings: Any = None) -> Dict[str, Any]:
        resolved = resolve_global_provider_settings(settings)
        openai_configured = bool((resolved.get("openai_api_key") or {}).get("value"))
        elevenlabs_configured = bool((resolved.get("elevenlabs_api_key") or {}).get("value"))
        custom_voice_id = str((resolved.get("elevenlabs_voice_id") or {}).get("value") or "").strip()
        custom_voice_name = str((resolved.get("elevenlabs_voice_name") or {}).get("value") or "").strip()
        paid_disabled = _truthy_env(
            "CODEXIA_DISABLE_PAID_AI",
            "DISABLE_PAID_AI",
            "NO_PAID_AI",
            "FINANCIAL_GUARDIAN_NO_PAID_MODE",
        )
        no_credit = bool(getattr(settings, "openai_no_credit", False)) or _truthy_env(
            "CODEXIA_OPENAI_NO_CREDIT", "OPENAI_NO_CREDIT"
        )

        eleven_voices = list(_voice_options(ELEVENLABS_VOICES))
        if custom_voice_id:
            eleven_voices.insert(
                1,
                {
                    "id": "my_voice",
                    "label": f"Minha voz — {custom_voice_name}" if custom_voice_name else "Minha voz configurada",
                },
            )

        return {
            "max_text_chars": NARRATION_LAB_MAX_CHARS,
            "retention_days": int(NARRATION_LAB_RETENTION_SECONDS / 86400),
            "video_rendering_enabled": False,
            "styles": _voice_options(VOICE_STYLES),
            "providers": [
                {
                    "id": "edge_tts",
                    "label": "Edge TTS",
                    "paid": False,
                    "configured": True,
                    "ready": True,
                    "message": "Gratuito; gera somente a amostra de áudio.",
                    "voices": _voice_options(EDGE_VOICES),
                },
                {
                    "id": "openai_tts",
                    "label": "OpenAI TTS",
                    "paid": True,
                    "configured": openai_configured,
                    "ready": bool(openai_configured and not paid_disabled),
                    "no_credit_flag": no_credit,
                    "message": (
                        "Modo sem consumo pago está ativo."
                        if paid_disabled
                        else "Chave OpenAI não configurada."
                        if not openai_configured
                        else "Marcada como sem saldo nas Configurações; a amostra pode confirmar a recarga."
                        if no_credit
                        else "Configurada; cada nova amostra pode consumir créditos."
                    ),
                    "voices": _voice_options(OPENAI_VOICES),
                },
                {
                    "id": "elevenlabs",
                    "label": "ElevenLabs",
                    "paid": True,
                    "configured": elevenlabs_configured,
                    "ready": bool(elevenlabs_configured and not paid_disabled),
                    "custom_voice_configured": bool(custom_voice_id),
                    "message": (
                        "Modo sem consumo pago está ativo."
                        if paid_disabled
                        else "Chave ElevenLabs não configurada."
                        if not elevenlabs_configured
                        else "Configurada; cada nova amostra pode consumir créditos."
                    ),
                    "voices": eleven_voices,
                },
            ],
        }

    def _user_dir(self, user_id: int) -> Path:
        safe_id = max(0, int(user_id or 0))
        user_dir = self.output_root / str(safe_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    def _normalize_provider(self, provider: Any) -> str:
        raw = str(provider or "").strip().lower()
        aliases = {"openai": "openai_tts", "edge": "edge_tts", "11labs": "elevenlabs"}
        normalized = aliases.get(raw, raw)
        if normalized not in {"edge_tts", "openai_tts", "elevenlabs"}:
            raise NarrationLabError(
                "Selecione Edge TTS, OpenAI TTS ou ElevenLabs.",
                code="PROVIDER_NOT_SUPPORTED",
            )
        return normalized

    def _normalize_style(self, style: Any) -> str:
        raw = str(style or "human").strip().lower()
        aliases = {"soft": "soft_prayer", "prayer": "soft_prayer", "solene": "solemn", "energetic": "energetic"}
        normalized = aliases.get(raw, raw)
        supported = {item[0] for item in VOICE_STYLES}
        if normalized not in supported:
            raise NarrationLabError("Estilo de voz inválido.", code="VOICE_STYLE_INVALID")
        return normalized

    def _normalize_gender(self, gender: Any) -> str:
        raw = str(gender or "female").strip().lower()
        aliases = {"feminina": "female", "masculina": "male", "f": "female", "m": "male"}
        normalized = aliases.get(raw, raw)
        if normalized not in {"female", "male"}:
            raise NarrationLabError("Selecione voz feminina ou masculina.", code="VOICE_GENDER_INVALID")
        return normalized

    def _resolve_edge_voice(self, voice: Any, gender: str) -> str:
        raw = str(voice or "auto").strip()
        allowed = {item[0] for item in EDGE_VOICES}
        if raw not in allowed:
            raise NarrationLabError("Voz Edge TTS inválida.", code="VOICE_INVALID")
        if raw == "auto":
            return "pt-BR-AntonioNeural" if gender == "male" else "pt-BR-FranciscaNeural"
        return raw

    def _resolve_premium_voice(
        self,
        ai: AIContentGenerator,
        *,
        provider: str,
        voice: Any,
        style: str,
        gender: str,
    ) -> Dict[str, Any]:
        raw = str(voice or "auto").strip()
        allowed = {item[0] for item in (OPENAI_VOICES if provider == "openai_tts" else ELEVENLABS_VOICES)}
        if provider == "elevenlabs" and str(ai.elevenlabs_voice_id or "").strip():
            allowed.add("my_voice")
        if raw not in allowed:
            raise NarrationLabError("Voz incompatível com o provedor escolhido.", code="VOICE_INVALID")
        if raw == "auto":
            voice_hint = ai._automatic_voice_hint(
                voice_style=style,
                voice_gender=gender,
                preferred_provider=provider,
            ) or ("onyx" if gender == "male" else "nova")
        else:
            voice_hint = raw

        if provider == "elevenlabs":
            meta = dict(ai._resolve_elevenlabs_voice_selection(voice_hint))
            if not str(meta.get("voice_id_used") or "").strip():
                raise NarrationLabError(
                    "A voz ElevenLabs selecionada não possui voice_id configurado.",
                    code="ELEVENLABS_VOICE_NOT_CONFIGURED",
                )
            return {"voice_hint": voice_hint, **meta}
        return {
            "voice_hint": voice_hint,
            "requested_voice_hint": raw,
            "effective_voice_hint": voice_hint,
            "voice_id_used": None,
            "voice_name_used": voice_hint,
            "voice_selection_source": "narration_lab_explicit" if raw != "auto" else "narration_lab_automatic",
        }

    def _voice_settings(self, style: str, gender: str) -> Dict[str, Any]:
        stability = 0.74
        style_amount = 0.16
        if style == "soft_prayer":
            stability, style_amount = 0.88, 0.09
        elif style == "solemn":
            stability, style_amount = 0.84, 0.14
        elif style == "energetic":
            stability, style_amount = 0.62, 0.27
        if gender == "female":
            style_amount = min(0.32, style_amount + 0.02)
        return {
            "stability": stability,
            "similarity_boost": 0.90,
            "style": style_amount,
            "use_speaker_boost": True,
        }

    def _edge_prosody(self, style: str, gender: str) -> Dict[str, str]:
        if style == "soft_prayer":
            return {"rate": "-10%", "pitch": "-2Hz" if gender == "male" else "-1Hz", "volume": "-6%"}
        if style == "solemn":
            return {"rate": "-6%", "pitch": "-3Hz" if gender == "male" else "-2Hz", "volume": "+0%"}
        if style == "energetic":
            return {"rate": "+8%", "pitch": "+1Hz" if gender == "male" else "+2Hz", "volume": "+4%"}
        return {"rate": "+0%", "pitch": "+0Hz", "volume": "+0%"}

    def _generate_edge_audio(self, text: str, output_path: Path, voice: str, style: str, gender: str) -> None:
        try:
            import edge_tts
        except Exception as exc:
            raise NarrationLabError(
                "Edge TTS não está disponível neste servidor.",
                status_code=503,
                code="EDGE_TTS_UNAVAILABLE",
            ) from exc

        prosody = self._edge_prosody(style, gender)

        async def _save() -> None:
            communicate = edge_tts.Communicate(text, voice, **prosody)
            await communicate.save(str(output_path))

        try:
            asyncio.run(_save())
        except Exception as exc:
            raise NarrationLabError(
                f"Edge TTS não conseguiu gerar a amostra: {str(exc)[:240]}",
                status_code=502,
                code="EDGE_TTS_FAILED",
            ) from exc

    def _probe_audio_file(self, path: Path) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "ok": False,
            "audio_duration_sec": 0.0,
            "audio_size_bytes": 0,
            "error": None,
        }
        if not path.is_file():
            result["error"] = "file_not_found"
            return result
        try:
            result["audio_size_bytes"] = int(path.stat().st_size or 0)
        except OSError as exc:
            result["error"] = f"file_stat_failed: {exc}"
            return result
        if not shutil.which("ffprobe"):
            result["error"] = "ffprobe_not_available"
            return result
        try:
            completed = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_type,duration",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except Exception as exc:
            result["error"] = f"ffprobe_failed: {type(exc).__name__}"
            return result
        if completed.returncode != 0:
            result["error"] = "ffprobe_rejected_audio"
            return result
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError:
            result["error"] = "ffprobe_invalid_json"
            return result
        streams = payload.get("streams") if isinstance(payload, dict) else []
        audio_streams = [
            item for item in (streams or [])
            if isinstance(item, dict) and str(item.get("codec_type") or "").lower() == "audio"
        ]
        durations: List[float] = []
        for stream in audio_streams:
            try:
                durations.append(float(stream.get("duration") or 0.0))
            except (TypeError, ValueError):
                pass
        try:
            format_duration = float(((payload.get("format") or {}).get("duration")) or 0.0)
        except (TypeError, ValueError, AttributeError):
            format_duration = 0.0
        duration = max([format_duration, *durations, 0.0])
        result["audio_duration_sec"] = round(duration, 3)
        result["ok"] = bool(audio_streams and duration > 0.2 and result["audio_size_bytes"] > 500)
        if not result["ok"]:
            result["error"] = "required_audio_checks_failed"
        return result

    def _safe_metadata(self, metadata: Dict[str, Any], *, cache_hit: bool) -> Dict[str, Any]:
        public = dict(metadata or {})
        public.pop("file_path", None)
        public["cache_hit"] = bool(cache_hit)
        public["charged_new_generation"] = bool(public.get("paid_provider") and not cache_hit)
        public["audio_url"] = f"/youtube/narration-lab/audio/{public.get('sample_id')}"
        return public

    def _read_metadata(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def _write_metadata(self, path: Path, metadata: Dict[str, Any]) -> None:
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, path)

    def _cleanup_user_dir(self, user_dir: Path) -> None:
        now = time.time()
        records: List[tuple[float, Path, Dict[str, Any]]] = []
        for metadata_path in user_dir.glob("*.json"):
            metadata = self._read_metadata(metadata_path) or {}
            try:
                created_at = float(metadata.get("created_at_epoch") or metadata_path.stat().st_mtime)
            except (OSError, TypeError, ValueError):
                created_at = 0.0
            records.append((created_at, metadata_path, metadata))
        records.sort(key=lambda item: item[0], reverse=True)
        for index, (created_at, metadata_path, metadata) in enumerate(records):
            expired = created_at > 0 and (now - created_at) > NARRATION_LAB_RETENTION_SECONDS
            excess = index >= NARRATION_LAB_MAX_SAMPLES_PER_USER
            if not expired and not excess:
                continue
            sample_id = str(metadata.get("sample_id") or metadata_path.stem)
            for target in (
                metadata_path,
                user_dir / f"{sample_id}.mp3",
                user_dir / f"{sample_id}.lock",
            ):
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass

    def generate(self, payload: Dict[str, Any], *, user_id: int) -> Dict[str, Any]:
        provider = self._normalize_provider(payload.get("provider"))
        style = self._normalize_style(payload.get("voice_style"))
        gender = self._normalize_gender(payload.get("voice_gender"))
        paid_provider = provider in {"openai_tts", "elevenlabs"}
        if paid_provider and not bool(payload.get("confirm_paid_generation")):
            raise NarrationLabError(
                "Confirme o uso de créditos antes de gerar uma amostra paga.",
                status_code=409,
                code="PAID_CONFIRMATION_REQUIRED",
            )

        try:
            clean_text = validate_narration_text(payload.get("text"), label="amostra de narração")
        except NarrationContractError as exc:
            raise NarrationLabError(str(exc), code="NARRATION_CONTRACT_BLOCKED") from exc
        if len(clean_text) < 20:
            raise NarrationLabError(
                "Digite pelo menos 20 caracteres para comparar a voz com segurança.",
                code="TEXT_TOO_SHORT",
            )
        if len(clean_text) > NARRATION_LAB_MAX_CHARS:
            raise NarrationLabError(
                f"A amostra aceita no máximo {NARRATION_LAB_MAX_CHARS} caracteres para limitar o custo.",
                code="TEXT_TOO_LONG",
            )

        ai: Optional[AIContentGenerator] = None
        voice_meta: Dict[str, Any]
        if provider == "edge_tts":
            edge_voice = self._resolve_edge_voice(payload.get("voice"), gender)
            voice_meta = {
                "voice_hint": edge_voice,
                "requested_voice_hint": str(payload.get("voice") or "auto"),
                "effective_voice_hint": edge_voice,
                "voice_id_used": edge_voice,
                "voice_name_used": edge_voice,
                "voice_selection_source": "narration_lab_edge",
            }
        else:
            ai = AIContentGenerator()
            ai._load_config()
            if ai._paid_ai_disabled():
                raise NarrationLabError(
                    "O modo sem consumo pago está ativo; nenhuma chamada paga foi feita.",
                    status_code=409,
                    code="PAID_AI_DISABLED",
                )
            if provider == "openai_tts" and not str(ai.api_key or "").strip():
                raise NarrationLabError(
                    "A chave da OpenAI não está configurada; nenhuma chamada paga foi feita.",
                    status_code=409,
                    code="OPENAI_NOT_CONFIGURED",
                )
            if provider == "elevenlabs" and not str(ai.elevenlabs_key or "").strip():
                raise NarrationLabError(
                    "A chave da ElevenLabs não está configurada; nenhuma chamada paga foi feita.",
                    status_code=409,
                    code="ELEVENLABS_NOT_CONFIGURED",
                )
            voice_meta = self._resolve_premium_voice(
                ai,
                provider=provider,
                voice=payload.get("voice"),
                style=style,
                gender=gender,
            )

        fingerprint_payload = {
            "contract_version": 1,
            "provider": provider,
            "voice": voice_meta.get("effective_voice_hint"),
            "voice_id": voice_meta.get("voice_id_used"),
            "style": style,
            "gender": gender,
            "text": clean_text,
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        user_dir = self._user_dir(user_id)
        audio_path = user_dir / f"{fingerprint}.mp3"
        metadata_path = user_dir / f"{fingerprint}.json"
        lock = FileLock(str(user_dir / f"{fingerprint}.lock"))

        def _build_metadata(
            probe: Dict[str, Any],
            diagnostics: Dict[str, Any],
            *,
            created_at_epoch: Optional[float] = None,
        ) -> Dict[str, Any]:
            timestamp = float(created_at_epoch or time.time())
            return {
                "sample_id": fingerprint,
                "created_at_epoch": timestamp,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp)),
                "provider_requested": provider,
                "provider_used": provider,
                "fallback_used": False,
                "provider_fallback_allowed": False,
                "paid_provider": paid_provider,
                "spoken_text_sent_to_tts": clean_text,
                "text_char_count": len(clean_text),
                "text_word_count": len(re.findall(r"\w+", clean_text, flags=re.UNICODE)),
                "voice_style": style,
                "voice_gender": gender,
                "requested_voice_hint": voice_meta.get("requested_voice_hint"),
                "effective_voice_hint": voice_meta.get("effective_voice_hint"),
                "voice_id_used": voice_meta.get("voice_id_used"),
                "voice_name_used": voice_meta.get("voice_name_used"),
                "voice_selection_source": voice_meta.get("voice_selection_source"),
                "audio_duration_sec": probe.get("audio_duration_sec"),
                "audio_size_bytes": probe.get("audio_size_bytes"),
                "attempts": diagnostics.get("attempts") or [],
                "rendered_video": False,
                "queued_video_task": False,
                "generated_images": 0,
                "generated_mp4_count": 0,
                "file_path": str(audio_path),
            }

        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise NarrationLabError(
                "Esta mesma amostra já está sendo gerada. Aguarde a conclusão para evitar cobrança duplicada.",
                status_code=409,
                code="SAMPLE_ALREADY_IN_PROGRESS",
            ) from exc

        temp_audio: Optional[Path] = None
        try:
            cached_metadata = self._read_metadata(metadata_path)
            if cached_metadata and audio_path.is_file():
                cached_probe = self._probe_audio_file(audio_path)
                if cached_probe.get("ok"):
                    return self._safe_metadata(cached_metadata, cache_hit=True)
            elif audio_path.is_file():
                # Se o processo caiu depois de preservar o MP3 e antes de salvar
                # o JSON, reconstrói somente os metadados. Nunca repete a chamada
                # paga para o mesmo fingerprint.
                recovered_probe = self._probe_audio_file(audio_path)
                if recovered_probe.get("ok"):
                    recovered_metadata = _build_metadata(
                        recovered_probe,
                        {
                            "attempts": [{
                                "provider": "preserved_narration_lab_audio",
                                "status": "success",
                                "reason": "MP3 preservado recuperado sem nova chamada de TTS.",
                            }]
                        },
                        created_at_epoch=audio_path.stat().st_mtime,
                    )
                    self._write_metadata(metadata_path, recovered_metadata)
                    return self._safe_metadata(recovered_metadata, cache_hit=True)

            temp_audio = user_dir / f".{fingerprint}.{uuid.uuid4().hex}.tmp.mp3"
            diagnostics: Dict[str, Any] = {
                "provider_used": provider,
                "fallback_used": False,
                "attempts": [],
            }
            if provider == "edge_tts":
                self._generate_edge_audio(
                    clean_text,
                    temp_audio,
                    str(voice_meta.get("voice_hint") or ""),
                    style,
                    gender,
                )
                diagnostics["attempts"] = [{"provider": "edge_tts", "status": "success"}]
            else:
                assert ai is not None
                diagnostics = ai.generate_audio_with_diagnostics(
                    clean_text,
                    voice=str(voice_meta.get("voice_hint") or ""),
                    voice_settings=self._voice_settings(style, gender),
                    preferred_provider=provider,
                    allow_provider_fallback=False,
                )
                audio_content = diagnostics.pop("audio_content", None)
                if diagnostics.get("provider_used") != provider or not isinstance(audio_content, (bytes, bytearray)) or not audio_content:
                    attempts = diagnostics.get("attempts") or []
                    reason = next(
                        (str(item.get("reason") or "") for item in reversed(attempts) if isinstance(item, dict) and item.get("reason")),
                        "O provedor não retornou áudio.",
                    )
                    raise NarrationLabError(
                        f"{reason} Confira chave, saldo e voz configurada; nenhum fallback pago foi acionado.",
                        status_code=502,
                        code="TTS_PROVIDER_FAILED",
                    )
                temp_audio.write_bytes(bytes(audio_content))

            probe = self._probe_audio_file(temp_audio)
            if not probe.get("ok"):
                raise NarrationLabError(
                    f"O provedor respondeu, mas o arquivo de áudio não passou na validação ({probe.get('error')}).",
                    status_code=502,
                    code="AUDIO_VALIDATION_FAILED",
                )
            os.replace(temp_audio, audio_path)
            temp_audio = None

            metadata = _build_metadata(probe, diagnostics)
            self._write_metadata(metadata_path, metadata)
            self._cleanup_user_dir(user_dir)
            return self._safe_metadata(metadata, cache_hit=False)
        finally:
            if temp_audio is not None:
                try:
                    temp_audio.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                lock.release()
            except Exception:
                pass

    def list_samples(self, *, user_id: int, limit: int = 12) -> List[Dict[str, Any]]:
        user_dir = self._user_dir(user_id)
        self._cleanup_user_dir(user_dir)
        items: List[Dict[str, Any]] = []
        for metadata_path in user_dir.glob("*.json"):
            metadata = self._read_metadata(metadata_path)
            if not metadata:
                continue
            sample_id = str(metadata.get("sample_id") or "")
            if not NARRATION_LAB_SAMPLE_ID_RE.fullmatch(sample_id):
                continue
            if not (user_dir / f"{sample_id}.mp3").is_file():
                continue
            items.append(self._safe_metadata(metadata, cache_hit=True))
        items.sort(key=lambda item: float(item.get("created_at_epoch") or 0.0), reverse=True)
        return items[: max(1, min(20, int(limit or 12)))]

    def audio_path(self, *, user_id: int, sample_id: str) -> Path:
        normalized = str(sample_id or "").strip().lower()
        if not NARRATION_LAB_SAMPLE_ID_RE.fullmatch(normalized):
            raise NarrationLabError("Amostra não encontrada.", status_code=404, code="SAMPLE_NOT_FOUND")
        user_dir = self._user_dir(user_id)
        audio_path = user_dir / f"{normalized}.mp3"
        metadata_path = user_dir / f"{normalized}.json"
        if not audio_path.is_file() or not metadata_path.is_file():
            raise NarrationLabError("Amostra não encontrada.", status_code=404, code="SAMPLE_NOT_FOUND")
        return audio_path


narration_lab_service = NarrationLabService()
