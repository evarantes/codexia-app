"""Narração supervisionada do YouTube Auto sobre o núcleo canônico v1.

A regra é deliberadamente simples:
1. o núcleo transforma a entrada em um artefato de fala seguro;
2. o TTS recebe exatamente ``artifact.spoken_text``;
3. a aprovação grava hash + versão do núcleo;
4. o vídeo final só pode reutilizar esse MP3 aprovado, nunca sintetizar em silêncio.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict

from app.config import AUDIO_OUTPUT_DIR
from app.services.narration_core import (
    NARRATION_CORE_NAMESPACE,
    NARRATION_CORE_VERSION,
    NarrationArtifact,
    NarrationCoreError,
    build_narration_artifact,
    narration_fingerprint,
    require_current_core,
)


MAX_TEXT_CHARS = 30000
SUPPORTED_VOICES = {"pt-BR-FranciscaNeural", "pt-BR-AntonioNeural"}


class YouTubeNarrationGateError(RuntimeError):
    def __init__(self, message: str, *, code: str = "YOUTUBE_NARRATION_GATE_ERROR", status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class YouTubeNarrationGateService:
    def __init__(self, output_root: str | None = None):
        # Namespace físico novo: áudios legados não entram no cache desta arquitetura.
        self.output_root = Path(output_root or AUDIO_OUTPUT_DIR) / "youtube_narration_core_v1"
        self.output_root.mkdir(parents=True, exist_ok=True)

    def _user_dir(self, user_id: int) -> Path:
        path = self.output_root / str(max(0, int(user_id or 0)))
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _artifact(text: Any) -> NarrationArtifact:
        raw = str(text or "") if not isinstance(text, (dict, list)) else text
        if isinstance(raw, str) and len(raw) > MAX_TEXT_CHARS:
            raise YouTubeNarrationGateError(
                f"A narração excede o limite de {MAX_TEXT_CHARS} caracteres.",
                code="TEXT_TOO_LONG",
            )
        try:
            artifact = build_narration_artifact(raw)
        except NarrationCoreError as exc:
            raise YouTubeNarrationGateError(
                f"Narração bloqueada antes do TTS: {exc}",
                code="NARRATION_CORE_BLOCKED",
                status_code=422,
            ) from exc
        if not artifact.spoken_text:
            raise YouTubeNarrationGateError("O texto da narração está vazio.", code="TEXT_REQUIRED")
        if len(artifact.spoken_text) > MAX_TEXT_CHARS:
            raise YouTubeNarrationGateError(
                f"A fala segura excede o limite de {MAX_TEXT_CHARS} caracteres.",
                code="TEXT_TOO_LONG",
            )
        return artifact

    @staticmethod
    def _voice(value: Any, gender: Any = None) -> str:
        raw = str(value or "auto").strip()
        if raw == "auto":
            return "pt-BR-AntonioNeural" if str(gender or "").lower() == "male" else "pt-BR-FranciscaNeural"
        if raw not in SUPPORTED_VOICES:
            raise YouTubeNarrationGateError("Voz Edge TTS inválida.", code="VOICE_INVALID")
        return raw

    @staticmethod
    def _duration(path: Path) -> float:
        if not shutil.which("ffprobe"):
            return 0.0
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            return max(0.0, float((result.stdout or "0").strip() or 0)) if result.returncode == 0 else 0.0
        except Exception:
            return 0.0

    @staticmethod
    def _safe_preview_id(preview_id: Any) -> str:
        safe_id = str(preview_id or "").strip().lower()
        if len(safe_id) != 32 or any(ch not in "0123456789abcdef" for ch in safe_id):
            raise YouTubeNarrationGateError("Identificador de narração inválido.", code="PREVIEW_ID_INVALID")
        return safe_id

    @staticmethod
    def _read_meta(path: Path) -> Dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _write_meta(path: Path, meta: Dict[str, Any]) -> None:
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)

    def generate(self, *, text: Any, user_id: int, voice: Any = "auto", voice_gender: Any = "female") -> Dict[str, Any]:
        artifact = self._artifact(text)
        spoken = artifact.spoken_text
        selected_voice = self._voice(voice, voice_gender)
        preview_id = narration_fingerprint(
            spoken_text=spoken,
            voice=selected_voice,
            provider="edge_tts",
        )
        user_dir = self._user_dir(user_id)
        mp3_path = user_dir / f"{preview_id}.mp3"
        meta_path = user_dir / f"{preview_id}.json"
        old_meta = self._read_meta(meta_path) if meta_path.is_file() else {}
        cache_hit = False
        if mp3_path.is_file() and mp3_path.stat().st_size > 512 and old_meta:
            try:
                require_current_core(old_meta)
                cache_hit = (
                    str(old_meta.get("text_sha256") or "") == artifact.text_sha256
                    and str(old_meta.get("voice") or "") == selected_voice
                )
            except NarrationCoreError:
                cache_hit = False

        if not cache_hit:
            # Nunca sobrescreve um MP3 parcialmente gerado/legado no lugar.
            mp3_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            try:
                import edge_tts
            except Exception as exc:
                raise YouTubeNarrationGateError(
                    "Edge TTS não está disponível no servidor.",
                    code="EDGE_TTS_UNAVAILABLE",
                    status_code=503,
                ) from exc

            async def _save() -> None:
                communicator = edge_tts.Communicate(
                    spoken,
                    selected_voice,
                    rate="+0%",
                    pitch="+0Hz",
                    volume="+0%",
                )
                await communicator.save(str(mp3_path))

            try:
                asyncio.run(_save())
            except Exception as exc:
                mp3_path.unlink(missing_ok=True)
                raise YouTubeNarrationGateError(
                    f"Falha ao gerar a narração: {str(exc)[:240]}",
                    code="EDGE_TTS_FAILED",
                    status_code=502,
                ) from exc

        if not mp3_path.is_file() or mp3_path.stat().st_size <= 512:
            raise YouTubeNarrationGateError("O áudio gerado é inválido.", code="AUDIO_INVALID", status_code=502)

        meta = {
            "preview_id": preview_id,
            "text_sha256": artifact.text_sha256,
            "spoken_text_sent_to_tts": spoken,
            "narration_core_version": NARRATION_CORE_VERSION,
            "narration_core_namespace": NARRATION_CORE_NAMESPACE,
            "removed_technical_blocks": artifact.removed_technical_blocks,
            "source_kind": artifact.source_kind,
            "voice": selected_voice,
            "provider": "edge_tts",
            "audio_size_bytes": int(mp3_path.stat().st_size),
            "audio_duration_sec": self._duration(mp3_path),
            "cache_hit": bool(cache_hit),
            "approved": bool(cache_hit and old_meta.get("approved")),
        }
        self._write_meta(meta_path, meta)
        return {
            **meta,
            "audio_url": f"/youtube/narration-lab/production-preview/audio/{preview_id}",
        }

    def approve(self, *, preview_id: str, expected_text: Any, user_id: int) -> Dict[str, Any]:
        safe_id = self._safe_preview_id(preview_id)
        artifact = self._artifact(expected_text)
        user_dir = self._user_dir(user_id)
        mp3_path = user_dir / f"{safe_id}.mp3"
        meta_path = user_dir / f"{safe_id}.json"
        if not mp3_path.is_file() or not meta_path.is_file():
            raise YouTubeNarrationGateError("Narração não encontrada ou expirada.", code="PREVIEW_NOT_FOUND", status_code=404)
        meta = self._read_meta(meta_path)
        if not meta:
            raise YouTubeNarrationGateError("Metadados da narração estão inválidos.", code="PREVIEW_METADATA_INVALID")
        try:
            require_current_core(meta)
        except NarrationCoreError as exc:
            raise YouTubeNarrationGateError(str(exc), code="OLD_NARRATION_CORE", status_code=409) from exc
        if str(meta.get("text_sha256") or "") != artifact.text_sha256:
            raise YouTubeNarrationGateError(
                "O texto foi alterado depois da geração do áudio. Gere e aprove uma nova narração.",
                code="TEXT_CHANGED_AFTER_PREVIEW",
                status_code=409,
            )
        expected_id = narration_fingerprint(
            spoken_text=artifact.spoken_text,
            voice=str(meta.get("voice") or ""),
            provider=str(meta.get("provider") or "edge_tts"),
        )
        if expected_id != safe_id:
            raise YouTubeNarrationGateError(
                "O áudio não corresponde ao núcleo/voz/texto atual. Gere uma nova narração.",
                code="PREVIEW_FINGERPRINT_MISMATCH",
                status_code=409,
            )
        meta["approved"] = True
        self._write_meta(meta_path, meta)
        return {
            "approved": True,
            "preview_id": safe_id,
            "text_sha256": artifact.text_sha256,
            "narration_core_version": NARRATION_CORE_VERSION,
            "narration_core_namespace": NARRATION_CORE_NAMESPACE,
            "reuse_audio_from": {
                "output_path": str(mp3_path.resolve()),
                "source": "youtube_narration_core_v1_approved",
                "preview_id": safe_id,
                "provider": "edge_tts",
                "voice": meta.get("voice"),
                "text_sha256": artifact.text_sha256,
                "narration_core_version": NARRATION_CORE_VERSION,
                "narration_core_namespace": NARRATION_CORE_NAMESPACE,
            },
        }

    def audio_path(self, *, preview_id: str, user_id: int) -> Path:
        safe_id = self._safe_preview_id(preview_id)
        user_dir = self._user_dir(user_id)
        path = user_dir / f"{safe_id}.mp3"
        meta_path = user_dir / f"{safe_id}.json"
        if not path.is_file() or not meta_path.is_file():
            raise YouTubeNarrationGateError("Áudio não encontrado.", code="PREVIEW_NOT_FOUND", status_code=404)
        meta = self._read_meta(meta_path)
        try:
            require_current_core(meta)
        except NarrationCoreError as exc:
            raise YouTubeNarrationGateError(str(exc), code="OLD_NARRATION_CORE", status_code=409) from exc
        return path

    def generate_logo_test_video(self, *, preview_id: str, user_id: int, logo_path: str) -> Dict[str, Any]:
        """Render barato de homologação; usa exatamente o MP3 do núcleo aprovado."""
        safe_id = self._safe_preview_id(preview_id)
        audio_path = self.audio_path(preview_id=safe_id, user_id=user_id)
        logo = Path(str(logo_path or "")).expanduser().resolve()
        if not logo.is_file() or logo.stat().st_size <= 256:
            raise YouTubeNarrationGateError(
                "Logo oficial do canal não encontrado. Configure/envie o logo em Configurações.",
                code="OFFICIAL_LOGO_NOT_FOUND",
                status_code=422,
            )
        if not shutil.which("ffmpeg"):
            raise YouTubeNarrationGateError("FFmpeg não está disponível no servidor.", code="FFMPEG_UNAVAILABLE", status_code=503)

        user_dir = self._user_dir(user_id)
        output = user_dir / f"{safe_id}-logo-test.mp4"
        newest_input = max(audio_path.stat().st_mtime, logo.stat().st_mtime)
        cache_hit = bool(output.is_file() and output.stat().st_size > 50_000 and output.stat().st_mtime >= newest_input)
        if not cache_hit:
            tmp = user_dir / f"{safe_id}-logo-test.tmp.mp4"
            tmp.unlink(missing_ok=True)
            command = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-loop", "1", "-i", str(logo),
                "-i", str(audio_path),
                "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
                "-pix_fmt", "yuv420p", "-r", "24",
                "-c:a", "aac", "-b:a", "128k",
                "-shortest", "-movflags", "+faststart", str(tmp),
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=15 * 60, check=False)
            if result.returncode != 0 or not tmp.is_file() or tmp.stat().st_size <= 50_000:
                tmp.unlink(missing_ok=True)
                raise YouTubeNarrationGateError(
                    f"Falha ao renderizar vídeo-teste com o logo: {(result.stderr or 'erro desconhecido')[-500:]}",
                    code="LOGO_TEST_RENDER_FAILED",
                    status_code=502,
                )
            os.replace(tmp, output)

        return {
            "ok": True,
            "preview_id": safe_id,
            "mode": "logo_only_narration_test",
            "images_generated": 0,
            "thumbnail_generated": False,
            "audio_reused_exactly": True,
            "narration_core_version": NARRATION_CORE_VERSION,
            "audio_path": str(audio_path),
            "audio_duration_sec": self._duration(audio_path),
            "video_duration_sec": self._duration(output),
            "cache_hit": cache_hit,
            "video_url": f"/youtube/narration-lab/production-preview/logo-test/{safe_id}",
        }

    def logo_test_video_path(self, *, preview_id: str, user_id: int) -> Path:
        safe_id = self._safe_preview_id(preview_id)
        path = self._user_dir(user_id) / f"{safe_id}-logo-test.mp4"
        if not path.is_file() or path.stat().st_size <= 50_000:
            raise YouTubeNarrationGateError("Vídeo-teste não encontrado.", code="LOGO_TEST_NOT_FOUND", status_code=404)
        return path


youtube_narration_gate_service = YouTubeNarrationGateService()
