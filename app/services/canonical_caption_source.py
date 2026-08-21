from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Type


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _collapse_ws(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_with_generator(generator: Any, value: Any) -> str:
    normalizer = getattr(generator, "_normalize_tts_text", None)
    if callable(normalizer):
        try:
            return _collapse_ws(normalizer(str(value or "")))
        except Exception:
            pass
    return _collapse_ws(value)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_collapse_ws(value).encode("utf-8")).hexdigest()


def _task_id_from_generator(generator: Any) -> Optional[str]:
    ai_service = getattr(generator, "ai_service", None)
    task_id = getattr(ai_service, "ai_task_id", None) if ai_service is not None else None
    return str(task_id).strip() if task_id else None


def _canonical_text_from_tts_checkpoint(generator: Any, narration_fallback: str) -> Tuple[str, str]:
    """Retorna o texto oficial mais próximo do áudio realmente produzido."""
    task_id = _task_id_from_generator(generator)
    if task_id:
        try:
            from app.services.task_manager import get_task

            task = get_task(task_id) or {}
            result = task.get("result") if isinstance(task.get("result"), dict) else {}
            checkpoint = result.get("audio_checkpoint") if isinstance(result.get("audio_checkpoint"), dict) else {}
            text = str(checkpoint.get("final_text_sent_to_tts") or "").strip()
            if text:
                return _normalize_with_generator(generator, text), "audio_checkpoint.final_text_sent_to_tts"

            audio_generation = result.get("audio_generation") if isinstance(result.get("audio_generation"), dict) else {}
            text = str(audio_generation.get("final_text_sent_to_tts") or "").strip()
            if text:
                return _normalize_with_generator(generator, text), "audio_generation.final_text_sent_to_tts"

            render_report = result.get("render_report") if isinstance(result.get("render_report"), dict) else {}
            render_audio = render_report.get("audio_generation") if isinstance(render_report.get("audio_generation"), dict) else {}
            text = str(render_audio.get("final_text_sent_to_tts") or "").strip()
            if text:
                return _normalize_with_generator(generator, text), "render_report.audio_generation.final_text_sent_to_tts"
        except Exception:
            pass

    return _normalize_with_generator(generator, narration_fallback), "renderer.final_narration_text"


def _timeline_joined_text(timeline: Any) -> str:
    if not isinstance(timeline, list):
        return ""
    return " ".join(
        str(item.get("caption") or "").strip()
        for item in timeline
        if isinstance(item, dict) and str(item.get("caption") or "").strip()
    ).strip()


def _caption_weights(timeline: List[Dict[str, Any]]) -> List[float]:
    weights: List[float] = []
    for item in timeline:
        caption = _collapse_ws(item.get("caption") or "")
        word_count = len(caption.split())
        if word_count > 0:
            weights.append(float(word_count))
            continue
        try:
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or 0.0)
            weights.append(max(0.01, end - start))
        except Exception:
            weights.append(1.0)
    return weights


def _redistribute_canonical_text(
    timeline: List[Dict[str, Any]],
    canonical_text: str,
) -> List[Dict[str, Any]]:
    """Preserva os timestamps e substitui somente o conteúdo reconhecido pelo ASR."""
    if not timeline:
        return []
    words = _collapse_ws(canonical_text).split()
    if not words:
        return deepcopy(timeline)

    out = [deepcopy(item) for item in timeline if isinstance(item, dict)]
    if not out:
        return []

    weights = _caption_weights(out)
    total_weight = sum(weights) or float(len(out))
    total_words = len(words)
    cursor = 0
    cumulative = 0.0

    for idx, item in enumerate(out):
        if idx == len(out) - 1:
            end_idx = total_words
        else:
            cumulative += weights[idx]
            end_idx = int(round(total_words * cumulative / total_weight))
            end_idx = max(cursor, min(total_words, end_idx))
            remaining_blocks = len(out) - idx - 1
            max_for_this = total_words - remaining_blocks
            if total_words >= len(out):
                end_idx = max(cursor + 1, min(max_for_this, end_idx))
        item["caption"] = " ".join(words[cursor:end_idx]).strip()
        item["text_source"] = "canonical_narration"
        cursor = end_idx

    if cursor < total_words and out:
        tail = " ".join(words[cursor:]).strip()
        current = _collapse_ws(out[-1].get("caption") or "")
        out[-1]["caption"] = f"{current} {tail}".strip()
    return out


def _shift_timeline(timeline: List[Dict[str, Any]], offset: float, duration: float) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    offset = max(0.0, float(offset or 0.0))
    duration = max(offset + 0.1, float(duration or 0.0))
    for raw in timeline or []:
        if not isinstance(raw, dict):
            continue
        item = deepcopy(raw)
        try:
            start = max(0.0, float(item.get("start") or 0.0)) + offset
            end = max(start, float(item.get("end") or start)) + offset
        except Exception:
            continue
        item["start"] = round(min(duration, start), 3)
        item["end"] = round(min(duration, max(start, end)), 3)
        item["source"] = item.get("source") or "canonical_text_timing"
        out.append(item)
    return out


def _single_canonical_block(canonical_text: str, duration: float, opening_silence_sec: float = 0.0) -> List[Dict[str, Any]]:
    start = max(0.0, float(opening_silence_sec or 0.0))
    end = max(start + 0.1, float(duration or 0.0))
    return [{
        "caption": canonical_text,
        "start": round(start, 3),
        "end": round(end, 3),
        "source": "canonical_single_block",
        "text_source": "canonical_narration",
    }]


def _persist_integrity_audit(generator: Any, audit: Dict[str, Any]) -> None:
    task_id = _task_id_from_generator(generator)
    if not task_id:
        return
    try:
        from app.services.task_manager import merge_task_result

        merge_task_result(task_id, {"canonical_narration_integrity": dict(audit)})
    except Exception:
        pass


def install_canonical_caption_source_patch(video_generator_cls: Type[Any]) -> Type[Any]:
    """Instala a fonte textual canônica com reparo automático de legendas.

    Regra de arquitetura: depois que o TTS termina, o texto associado ao áudio é
    a autoridade. A transcrição serve para timestamps. Divergência de texto nunca
    deve cancelar uma produção paga: primeiro preservamos os tempos do ASR,
    depois reconstruímos a timeline pelo texto canônico e, como último fallback,
    usamos um bloco canônico temporizado. O evento fica auditado para diagnóstico.
    """
    if video_generator_cls is None:
        raise ValueError("video_generator_cls é obrigatório para preservar o executor canônico")
    if getattr(video_generator_cls, "_codexia_canonical_caption_source_installed", False):
        return video_generator_cls

    original_builder = getattr(video_generator_cls, "_build_caption_timeline_details", None)
    if not callable(original_builder):
        return video_generator_cls

    def resolve_canonical_narration_text(self: Any, narration_fallback: str) -> str:
        canonical_text, canonical_source = _canonical_text_from_tts_checkpoint(self, narration_fallback)
        self._codexia_canonical_text_source = canonical_source
        self._codexia_canonical_text_sha256 = _sha256(canonical_text)
        return canonical_text

    def force_canonical_caption_timeline(
        self: Any,
        narration: str,
        duration: float,
        *,
        timeline: Optional[List[Dict[str, Any]]] = None,
        opening_silence_sec: float = 0.0,
    ) -> List[Dict[str, Any]]:
        canonical_text = resolve_canonical_narration_text(self, narration)
        if not canonical_text:
            self._codexia_caption_repair_mode = "no_canonical_text"
            return list(timeline or [])

        # 1) Melhor resultado: mantém os timestamps reais da transcrição.
        remapped = _redistribute_canonical_text(list(timeline or []), canonical_text)
        if remapped and _normalize_with_generator(self, _timeline_joined_text(remapped)) == canonical_text:
            self._codexia_caption_repair_mode = "preserved_asr_timestamps"
            return remapped

        # 2) Reconstrói blocos usando o texto oficial, sem nova chamada externa.
        builder = getattr(self, "_caption_timeline_from_text", None)
        if callable(builder):
            try:
                usable_duration = max(0.1, float(duration or 0.0) - max(0.0, float(opening_silence_sec or 0.0)))
                rebuilt = builder(canonical_text, usable_duration) or []
                rebuilt = _shift_timeline(rebuilt, opening_silence_sec, duration)
                if rebuilt and _normalize_with_generator(self, _timeline_joined_text(rebuilt)) == canonical_text:
                    self._codexia_caption_repair_mode = "canonical_text_timeline"
                    return rebuilt
            except Exception:
                pass

        # 3) Garantia final: conteúdo 100% idêntico ao TTS. O renderer poderá
        # subdividir este bloco para overlay sem mudar o texto-base.
        self._codexia_caption_repair_mode = "canonical_single_block"
        return _single_canonical_block(canonical_text, duration, opening_silence_sec)

    def canonical_caption_timeline_details(
        self: Any,
        narration: str,
        duration: float,
        audio_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        canonical_text = resolve_canonical_narration_text(self, narration)
        canonical_source = str(
            getattr(self, "_codexia_canonical_text_source", "renderer.final_narration_text")
            or "renderer.final_narration_text"
        )
        builder_error = None
        try:
            details = original_builder(self, canonical_text or narration, duration, audio_path=audio_path)
        except Exception as exc:
            builder_error = str(exc)[:700]
            details = {
                "source": "text_fallback",
                "timing_source": "canonical_recovery_after_builder_error",
                "timeline": [],
            }
        if not isinstance(details, dict):
            details = {"source": "text_fallback", "timeline": []}

        timeline = details.get("timeline") if isinstance(details.get("timeline"), list) else []
        before_text = _timeline_joined_text(timeline)
        before_normalized = _normalize_with_generator(self, before_text)

        if canonical_text and before_normalized != canonical_text:
            repaired = force_canonical_caption_timeline(
                self,
                canonical_text,
                duration,
                timeline=timeline,
                opening_silence_sec=0.0,
            )
            details = dict(details)
            details["timeline"] = repaired
            details["canonical_text_source"] = canonical_source
            details["timing_source"] = str(details.get("timing_source") or details.get("source") or "unknown")
            details["canonical_text_remapped"] = True
            details["canonical_self_heal"] = True
            details["canonical_repair_mode"] = str(getattr(self, "_codexia_caption_repair_mode", "unknown"))

        final_timeline = details.get("timeline") if isinstance(details.get("timeline"), list) else []
        after_text = _timeline_joined_text(final_timeline)
        after_normalized = _normalize_with_generator(self, after_text)

        # Mesmo que um normalizador futuro introduza comportamento inesperado,
        # uma divergência residual vira timeline canônica, nunca falha fatal.
        if canonical_text and after_normalized != canonical_text:
            final_timeline = _single_canonical_block(canonical_text, duration, 0.0)
            details = dict(details)
            details["timeline"] = final_timeline
            details["canonical_self_heal"] = True
            details["canonical_repair_mode"] = "canonical_single_block"
            self._codexia_caption_repair_mode = "canonical_single_block"
            after_text = _timeline_joined_text(final_timeline)
            after_normalized = _normalize_with_generator(self, after_text)

        audit = {
            "version": 4,
            "checked_at": _utc_iso(),
            "task_id": _task_id_from_generator(self),
            "canonical_text_source": canonical_source,
            "timing_source": str(details.get("timing_source") or details.get("source") or "unknown"),
            "canonical_text_sha256": _sha256(canonical_text),
            "caption_text_before_sha256": _sha256(before_normalized),
            "caption_text_after_sha256": _sha256(after_normalized),
            "captions_matched_before": before_normalized == canonical_text,
            "captions_match_after": after_normalized == canonical_text,
            "auto_repaired": bool(before_normalized != canonical_text),
            "repair_mode": str(getattr(self, "_codexia_caption_repair_mode", "not_needed")),
            "builder_error": builder_error,
            "canonical_word_count": len(canonical_text.split()),
            "caption_block_count": len(final_timeline),
        }
        self._codexia_canonical_narration_integrity = audit
        _persist_integrity_audit(self, audit)
        return details

    video_generator_cls._codexia_resolve_canonical_narration_text = resolve_canonical_narration_text
    video_generator_cls._codexia_force_canonical_caption_timeline = force_canonical_caption_timeline
    video_generator_cls._build_caption_timeline_details = canonical_caption_timeline_details
    video_generator_cls._codexia_canonical_caption_source_installed = True
    video_generator_cls._codexia_caption_integrity_version = 4
    return video_generator_cls
