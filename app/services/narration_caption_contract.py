"""Acceptance rules for recoverable estimated caption timelines."""
from __future__ import annotations


_ESTIMATED_MEASURED_TEXT_SOURCES = {
    "text_fallback_from_measured_audio",
    "text_fallback_shifted_by_opening_silence",
}


def measured_text_timeline_is_reviewable(
    *,
    source: str,
    timing_source: str,
    alignment_quality: str,
    captions_synced: bool,
    text_matches: bool,
) -> bool:
    """Allow review, but not automatic publication, with exact estimated captions.

    This path is deliberately narrower than verified ASR/acoustic alignment:
    it requires captions to be generated from the exact text sent to TTS and
    the final sync check to confirm that their measured span fits the audio.
    """
    return bool(
        str(source or "").strip() in _ESTIMATED_MEASURED_TEXT_SOURCES
        and str(timing_source or "").strip() == "measured_audio_duration"
        and str(alignment_quality or "").strip() == "estimated_from_measured_audio_duration"
        and captions_synced is True
        and text_matches is True
    )
