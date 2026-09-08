"""Natural pt-BR supervised narration gate.

This v2 gate keeps the approved Portuguese text canonical while moving pauses,
breath windows and caption timing into a separate performance layer. The final
video still reuses the frozen MP3; no TTS is allowed during render.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

from app.services.narration_core import (
    NARRATION_CORE_NAMESPACE,
    NARRATION_CORE_VERSION,
    NarrationCoreError,
    narration_fingerprint,
    require_current_core,
)
from app.services.narrative_structure_standard import (
    audit_canonical_narration,
    compose_canonical_narration,
)
from app.services.production_job_store import ProductionJobStoreError
from app.services.ptbr_narration_performance import (
    PTBR_EDGE_PITCH,
    PTBR_EDGE_RATE,
    PTBR_EDGE_VOLUME,
    PTBR_NARRATION_PERFORMANCE_NAMESPACE,
    PTBR_NARRATION_PERFORMANCE_VERSION,
    PtBrNarrationPerformanceError,
    synthesize_edge_ptbr_performance,
)
from app.services.youtube_narration_gate import (
    YouTubeNarrationGateError,
    YouTubeNarrationGateService,
)


class YouTubeNarrationGateV2Service(YouTubeNarrationGateService):
    @staticmethod
    def _require_current_performance(meta: Dict[str, Any]) -> None:
        try:
            version = int(meta.get("narration_performance_version") or 0)
        except Exception:
            version = 0
        namespace = str(meta.get("narration_performance_namespace") or "").strip()
        if (
            version != PTBR_NARRATION_PERFORMANCE_VERSION
            or namespace != PTBR_NARRATION_PERFORMANCE_NAMESPACE
            or meta.get("caption_alignment_exact") is not True
            or not isinstance(meta.get("caption_timeline"), list)
            or not meta.get("caption_timeline")
        ):
            raise YouTubeNarrationGateError(
                "Esta narração foi criada antes do padrão natural/sincronizado atual. "
                "Gere uma nova prévia antes de aprovar ou renderizar.",
                code="OLD_NARRATION_PERFORMANCE",
                status_code=409,
            )

    def generate(
        self,
        *,
        text: Any,
        user_id: int,
        voice: Any = "auto",
        voice_gender: Any = "female",
        production_job_id: Any = None,
        theme: Any = "",
    ) -> Dict[str, Any]:
        source_artifact = self._artifact(text)
        safe_review_text = self._safe_review_text(text, source_artifact)
        canonical_input = compose_canonical_narration({}, fallback_text=safe_review_text)
        narration_contract = audit_canonical_narration(canonical_input)
        if not narration_contract.get("valid"):
            raise YouTubeNarrationGateError(
                "O roteiro precisa conter, em blocos separados, apresentação do canal, gancho, "
                "desenvolvimento, verdade central, transformação, aplicação, clímax, reflexão "
                "e o CTA completo ao final.",
                code="NARRATIVE_CONTRACT_INCOMPLETE",
                status_code=422,
            )

        artifact = self._artifact(canonical_input)
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
                self._require_current_performance(old_meta)
                cache_hit = bool(
                    str(old_meta.get("text_sha256") or "") == artifact.text_sha256
                    and str(old_meta.get("voice") or "") == selected_voice
                )
            except (NarrationCoreError, YouTubeNarrationGateError):
                cache_hit = False

        performance: Dict[str, Any] = {}
        if cache_hit:
            performance = {
                "caption_timeline": list(old_meta.get("caption_timeline") or []),
                "segment_count": int(old_meta.get("performance_segment_count") or 0),
                "pause_total_sec": float(old_meta.get("performance_pause_total_sec") or 0.0),
                "performance_version": int(old_meta.get("narration_performance_version") or 0),
                "performance_namespace": str(old_meta.get("narration_performance_namespace") or ""),
                "rate": str(old_meta.get("performance_rate") or PTBR_EDGE_RATE),
                "pitch": str(old_meta.get("performance_pitch") or PTBR_EDGE_PITCH),
                "volume": str(old_meta.get("performance_volume") or PTBR_EDGE_VOLUME),
                "caption_alignment_exact": old_meta.get("caption_alignment_exact") is True,
            }
        else:
            mp3_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            try:
                performance = asyncio.run(
                    synthesize_edge_ptbr_performance(
                        spoken_text=spoken,
                        review_text=canonical_input,
                        voice=selected_voice,
                        output_path=mp3_path,
                    )
                )
            except PtBrNarrationPerformanceError as exc:
                mp3_path.unlink(missing_ok=True)
                raise YouTubeNarrationGateError(
                    f"Falha no padrão natural de narração: {str(exc)[:320]}",
                    code="PTBR_NARRATION_PERFORMANCE_FAILED",
                    status_code=502,
                ) from exc
            except Exception as exc:
                mp3_path.unlink(missing_ok=True)
                raise YouTubeNarrationGateError(
                    f"Falha ao gerar a narração: {str(exc)[:240]}",
                    code="EDGE_TTS_FAILED",
                    status_code=502,
                ) from exc

        caption_timeline = list(performance.get("caption_timeline") or [])
        if not mp3_path.is_file() or mp3_path.stat().st_size <= 512:
            raise YouTubeNarrationGateError(
                "O áudio gerado é inválido.",
                code="AUDIO_INVALID",
                status_code=502,
            )
        if not caption_timeline or performance.get("caption_alignment_exact") is not True:
            mp3_path.unlink(missing_ok=True)
            raise YouTubeNarrationGateError(
                "O áudio foi gerado, mas a legenda não pôde ser associada palavra por palavra. "
                "A versão foi descartada para evitar legenda inexata.",
                code="CAPTION_ALIGNMENT_NOT_EXACT",
                status_code=502,
            )

        meta = {
            "preview_id": preview_id,
            "text_sha256": artifact.text_sha256,
            "review_script_text": canonical_input,
            "spoken_text_sent_to_tts": spoken,
            "narration_core_version": NARRATION_CORE_VERSION,
            "narration_core_namespace": NARRATION_CORE_NAMESPACE,
            "narration_performance_version": PTBR_NARRATION_PERFORMANCE_VERSION,
            "narration_performance_namespace": PTBR_NARRATION_PERFORMANCE_NAMESPACE,
            "removed_technical_blocks": source_artifact.removed_technical_blocks,
            "source_kind": source_artifact.source_kind,
            "voice": selected_voice,
            "provider": "edge_tts",
            "language": "pt-BR",
            "audio_size_bytes": int(mp3_path.stat().st_size),
            "audio_duration_sec": self._duration(mp3_path),
            "narration_contract": narration_contract,
            "caption_timeline": caption_timeline,
            "caption_timing_source": "edge_tts_word_boundaries_exact_ptbr_v2",
            "caption_alignment_exact": True,
            "performance_segment_count": int(performance.get("segment_count") or 0),
            "performance_pause_total_sec": float(performance.get("pause_total_sec") or 0.0),
            "performance_rate": str(performance.get("rate") or PTBR_EDGE_RATE),
            "performance_pitch": str(performance.get("pitch") or PTBR_EDGE_PITCH),
            "performance_volume": str(performance.get("volume") or PTBR_EDGE_VOLUME),
            "ptbr_text_integrity": {
                "approved_spelling_preserved": True,
                "accentuation_source": "approved_script",
                "pseudo_phonetic_rewrites": False,
                "word_order_preserved": True,
            },
            "cache_hit": bool(cache_hit),
            "approved": bool(cache_hit and old_meta.get("approved")),
        }
        self._write_meta(meta_path, meta)

        try:
            job = self.job_store.register_preview(
                user_id=user_id,
                source_mp3=mp3_path,
                source_meta=meta_path,
                preview_id=preview_id,
                theme=str(theme or ""),
                job_id=str(production_job_id or "").strip() or None,
            )
        except ProductionJobStoreError as exc:
            raise YouTubeNarrationGateError(
                str(exc),
                code="PRODUCTION_JOB_ERROR",
                status_code=409,
            ) from exc

        return {
            **meta,
            "production_job_id": job["job_id"],
            "production_job_status": job.get("status"),
            "audio_url": f"/youtube/narration-lab/production-preview/audio/{preview_id}",
        }

    def approve(
        self,
        *,
        preview_id: str,
        expected_text: Any,
        user_id: int,
        production_job_id: Any = None,
    ) -> Dict[str, Any]:
        safe_id = self._safe_preview_id(preview_id)
        meta_path = self._user_dir(user_id) / f"{safe_id}.json"
        meta = self._read_meta(meta_path)
        if not meta:
            raise YouTubeNarrationGateError(
                "Metadados da narração estão inválidos.",
                code="PREVIEW_METADATA_INVALID",
                status_code=409,
            )
        self._require_current_performance(meta)
        return super().approve(
            preview_id=safe_id,
            expected_text=expected_text,
            user_id=user_id,
            production_job_id=production_job_id,
        )

    def audio_path(self, *, preview_id: str, user_id: int) -> Path:
        path = super().audio_path(preview_id=preview_id, user_id=user_id)
        meta = self._read_meta(path.with_suffix(".json"))
        self._require_current_performance(meta)
        return path


youtube_narration_gate_service = YouTubeNarrationGateV2Service()

__all__ = [
    "YouTubeNarrationGateError",
    "YouTubeNarrationGateV2Service",
    "youtube_narration_gate_service",
]
