from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Type


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
            duration = max(0.01, end - start)
        except Exception:
            duration = 1.0
        weights.append(duration)
    return weights


def _redistribute_canonical_text(
    timeline: List[Dict[str, Any]],
    canonical_text: str,
) -> List[Dict[str, Any]]:
    """Mantém os tempos medidos no áudio e troca somente a fonte do texto.

    A transcrição continua definindo *quando* cada bloco aparece. O conteúdo da
    legenda, porém, vem exclusivamente do mesmo texto canônico enviado ao TTS.
    Isso impede que pequenas diferenças do ASR (pontuação, flexões, palavras
    omitidas etc.) criem uma segunda versão do roteiro.
    """
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
            # Quando há palavras suficientes, evita blocos vazios no meio.
            remaining_blocks = len(out) - idx - 1
            max_for_this = total_words - remaining_blocks
            if total_words >= len(out):
                end_idx = max(cursor + 1, min(max_for_this, end_idx))
        item["caption"] = " ".join(words[cursor:end_idx]).strip()
        item["text_source"] = "canonical_narration"
        cursor = end_idx

    # Defesa final contra arredondamentos futuros.
    if cursor < total_words and out:
        tail = " ".join(words[cursor:]).strip()
        current = _collapse_ws(out[-1].get("caption") or "")
        out[-1]["caption"] = f"{current} {tail}".strip()

    return out


def _persist_integrity_audit(generator: Any, audit: Dict[str, Any]) -> None:
    task_id = _task_id_from_generator(generator)
    if not task_id:
        return
    try:
        from app.services.task_manager import merge_task_result

        merge_task_result(task_id, {"canonical_narration_integrity": dict(audit)})
    except Exception:
        pass


def install_canonical_caption_source_patch(video_generator_cls: Optional[Type[Any]] = None) -> Type[Any]:
    """Faz TTS e legenda compartilharem uma única fonte textual oficial.

    O patch é deliberadamente pequeno e não altera geração de áudio, imagem,
    roteiro ou timing. Ele atua somente depois que a timeline de legenda já foi
    criada, preservando os timestamps obtidos da transcrição e substituindo o
    texto reconhecido pelo texto canônico entregue ao TTS.
    """
    if video_generator_cls is None:
        from app.services.video_generator import VideoGenerator

        video_generator_cls = VideoGenerator

    if getattr(video_generator_cls, "_codexia_canonical_caption_source_installed", False):
        return video_generator_cls

    original_builder = getattr(video_generator_cls, "_build_caption_timeline_details", None)
    if not callable(original_builder):
        return video_generator_cls

    def canonical_caption_timeline_details(
        self: Any,
        narration: str,
        duration: float,
        audio_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        details = original_builder(self, narration, duration, audio_path=audio_path)
        if not isinstance(details, dict):
            return details

        timeline = details.get("timeline")
        if not isinstance(timeline, list) or not timeline:
            return details

        canonical_text = _normalize_with_generator(self, narration)
        before_text = _timeline_joined_text(timeline)
        before_normalized = _normalize_with_generator(self, before_text)

        # Se já é idêntico, não mexemos em nada além da auditoria.
        changed = before_normalized != canonical_text
        if changed:
            remapped = _redistribute_canonical_text(timeline, canonical_text)
            if remapped:
                details = dict(details)
                details["timeline"] = remapped
                details["canonical_text_source"] = "final_narration_sent_to_tts"
                details["timing_source"] = str(details.get("source") or "unknown")
                details["canonical_text_remapped"] = True

        final_timeline = details.get("timeline") if isinstance(details.get("timeline"), list) else timeline
        after_text = _timeline_joined_text(final_timeline)
        after_normalized = _normalize_with_generator(self, after_text)
        audit = {
            "version": 1,
            "checked_at": _utc_iso(),
            "task_id": _task_id_from_generator(self),
            "canonical_text_source": "final_narration_sent_to_tts",
            "timing_source": str(details.get("source") or "unknown"),
            "canonical_text_sha256": _sha256(canonical_text),
            "caption_text_before_sha256": _sha256(before_normalized),
            "caption_text_after_sha256": _sha256(after_normalized),
            "captions_matched_before": before_normalized == canonical_text,
            "captions_match_after": after_normalized == canonical_text,
            "remapped_from_transcription_text": bool(changed),
            "canonical_word_count": len(canonical_text.split()),
            "caption_block_count": len(final_timeline),
        }
        self._codexia_canonical_narration_integrity = audit
        _persist_integrity_audit(self, audit)

        # Fail-closed apenas para defeito interno: após o remapeamento não pode
        # existir uma segunda versão textual. Isso acontece antes do render.
        if after_normalized != canonical_text:
            raise RuntimeError(
                "Falha interna de integridade: legenda não pôde ser vinculada ao texto canônico da narração."
            )
        return details

    video_generator_cls._build_caption_timeline_details = canonical_caption_timeline_details
    video_generator_cls._codexia_canonical_caption_source_installed = True
    return video_generator_cls
