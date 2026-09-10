#!/usr/bin/env python3
"""Make burned-in captions a single global audio-timed overlay.

The renderer historically attached caption clips to each visual scene. Scene
durations can be stretched/reused for visual pacing, so those local clips may
drift or be shown after the CTA even when the audio transcript timestamps are
correct. This deterministic patch keeps scene metadata for planning, but
composites the caption timeline exactly once over the concatenated video.
"""
from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RENDER_TARGET = ROOT / "app/services/video_generator.py"
MARKER = "CODEXIA_AUDIO_TIMED_GLOBAL_CAPTIONS_V1"


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho original, encontrado {count}")
    return text.replace(old, new, 1)


def patch_caption_boundaries(text: str) -> str:
    """Preserve real first/last speech timestamps from ASR/WordBoundary."""
    old_word = '''        if timeline:\n            timeline[0]["start"] = 0.0\n            timeline[-1]["end"] = total_duration\n            return self._realign_caption_timeline_to_narration(timeline, narration)'''
    new_word = '''        if timeline:\n            # CODEXIA_AUDIO_TIMED_GLOBAL_CAPTIONS_V1\n            # Não estique o primeiro/último bloco até os limites do arquivo:\n            # o áudio pode conter silêncio de abertura ou de endcard. Os\n            # timestamps reais das palavras são a autoridade da legenda.\n            return self._realign_caption_timeline_to_narration(timeline, narration)'''
    text = _replace_once(text, old_word, new_word, label="caption/word-boundaries")

    old_segment = '''        if approx:\n            approx[0]["start"] = 0.0\n            approx[-1]["end"] = total_duration\n        return self._realign_caption_timeline_to_narration(approx, narration)'''
    new_segment = '''        # Preserve the segment timestamps as returned by the audio\n        # transcriber; do not turn trailing silence into a subtitle.\n        return self._realign_caption_timeline_to_narration(approx, narration)'''
    text = _replace_once(text, old_segment, new_segment, label="caption/segment-boundaries")
    return text


def patch_approved_timeline_sanitize(text: str) -> str:
    old = '''                if shifted_approved_timed:\n                    caption_timeline_details = {\n                        "timeline": shifted_approved_timed,'''
    new = '''                if shifted_approved_timed:\n                    # Approved Edge-TTS boundaries may contain encoder padding.\n                    # Normalize once, while keeping the last spoken word end.\n                    shifted_approved_timed = self._sanitize_caption_timeline(\n                        shifted_approved_timed,\n                        actual_total_audio_dur,\n                    )\n                    caption_timeline_details = {\n                        "timeline": shifted_approved_timed,'''
    return _replace_once(text, old, new, label="caption/approved-boundaries")


def patch_opening_overlay(text: str) -> str:
    old = '''            opening_caption_overlays = self._caption_overlay_clips_for_window(\n                full_caption_timeline,\n                0.0,\n                opening_visual_duration,\n                video_size,\n            )\n            opening_overlays.extend(opening_caption_overlays)\n            render_report["visual_plan"]["opening_caption_suppressed"] = False\n            render_report["visual_plan"]["opening_caption_blocks"] = len(opening_caption_overlays)'''
    new = '''            # CODEXIA_AUDIO_TIMED_GLOBAL_CAPTIONS_V1\n            # Captions are composited once after all scenes are concatenated.\n            # Never attach a second local copy to the opening clip.\n            opening_caption_overlays = []\n            render_report["visual_plan"]["opening_caption_suppressed"] = True\n            render_report["visual_plan"]["opening_caption_blocks"] = 0\n            render_report["visual_plan"]["caption_render_mode"] = "global_audio_timeline"'''
    return _replace_once(text, old, new, label="caption/opening-local-overlay")


def patch_scene_overlay(text: str) -> str:
    old = '''                scene_caption_timeline = list(scene_timeline_entry.get("caption_blocks") or [])\n                expanded_scene_timeline = []\n                for item in scene_caption_timeline:\n                    expanded_scene_timeline.extend(\n                        self._expand_caption_item_for_overlay(\n                            item,\n                            size=video_size,\n                            max_lines=2,\n                            reserved_bottom_ratio=CAPTION_SAFE_AREA_BOTTOM_RATIO,\n                        )\n                    )'''
    new = '''                # CODEXIA_AUDIO_TIMED_GLOBAL_CAPTIONS_V1\n                # Keep caption_blocks in the official timeline for diagnostics,\n                # but do not render a local copy. Local scene coordinates drift\n                # whenever visual pacing stretches or reuses a scene.\n                scene_caption_timeline = list(scene_timeline_entry.get("caption_blocks") or [])\n                expanded_scene_timeline: List[Dict[str, Any]] = []'''
    return _replace_once(text, old, new, label="caption/scene-local-overlay")


def patch_closing_overlay(text: str) -> str:
    old = '''                closing_caption_overlays = self._caption_overlay_clips_for_window(\n                    full_caption_timeline,\n                    closing_audio_start,\n                    closing_audio_end,\n                    video_size,\n                )\n                if closing_caption_overlays:\n                    clip_cta = CompositeVideoClip([clip_cta] + closing_caption_overlays, size=video_size)\n                clips.append(clip_cta)\n                render_report["cta_rendered"] = True\n                render_report["visual_plan"]["cta_visual_mode"] = "cinematic_background_bridge"\n                render_report["visual_plan"]["closing_caption_blocks"] = len(closing_caption_overlays)'''
    new = '''                # CODEXIA_AUDIO_TIMED_GLOBAL_CAPTIONS_V1\n                # The CTA is covered by the same global audio timeline as every\n                # other word; a local CTA copy would create duplicate subtitles.\n                closing_caption_overlays = []\n                clips.append(clip_cta)\n                render_report["cta_rendered"] = True\n                render_report["visual_plan"]["cta_visual_mode"] = "cinematic_background_bridge"\n                render_report["visual_plan"]["closing_caption_blocks"] = 0'''
    return _replace_once(text, old, new, label="caption/closing-local-overlay")


def patch_global_composite(text: str) -> str:
    old = '''            self._assert_clip_not_none(final_clip, "final_clip_after_concat")\n\n            try:\n                final_dur = float(getattr(final_clip, "duration", 0) or 0)'''
    new = '''            self._assert_clip_not_none(final_clip, "final_clip_after_concat")\n\n            # CODEXIA_AUDIO_TIMED_GLOBAL_CAPTIONS_V1\n            # Render one subtitle layer in final-video coordinates. This keeps\n            # every caption tied to the official audio timestamps regardless of\n            # scene reuse, visual beats, fades, or CTA placement. The window is\n            # bounded by both the audio and the already-concatenated video, so a\n            # caption can never extend the video or leak into the silent endcard.\n            try:\n                concatenated_duration = float(getattr(final_clip, "duration", 0) or 0.0)\n            except Exception:\n                concatenated_duration = 0.0\n            global_caption_overlays = []\n            caption_overlay_window_end = min(\n                max(0.0, float(actual_total_audio_dur or 0.0)),\n                max(0.0, concatenated_duration),\n            )\n            if caption_overlay_window_end > 0.0:\n                global_caption_overlays = self._caption_overlay_clips_for_window(\n                    full_caption_timeline,\n                    0.0,\n                    caption_overlay_window_end,\n                    video_size,\n                )\n            if global_caption_overlays:\n                final_clip = CompositeVideoClip(\n                    [final_clip] + global_caption_overlays,\n                    size=video_size,\n                )\n                if concatenated_duration > 0.0:\n                    final_clip = self._set_clip_duration(final_clip, concatenated_duration)\n            render_report["visual_plan"]["caption_render_mode"] = "global_audio_timeline"\n            render_report["visual_plan"]["global_caption_overlay_count"] = len(global_caption_overlays)\n            render_report["visual_plan"]["global_caption_window_end_sec"] = round(\n                caption_overlay_window_end,\n                3,\n            )\n\n            try:\n                final_dur = float(getattr(final_clip, "duration", 0) or 0)'''
    return _replace_once(text, old, new, label="caption/global-composite")


def patch_sync_validation(text: str) -> str:
    old = '''                video_sync_target = float(target_video_duration or actual_total_audio_dur)\n                audio_video_diff = abs(final_dur - video_sync_target)\n                audio_caption_diff = abs(caption_duration - actual_total_audio_dur)'''
    new = '''                video_sync_target = float(target_video_duration or actual_total_audio_dur)\n                audio_video_diff = abs(final_dur - video_sync_target)\n                # CODEXIA_AUDIO_TIMED_GLOBAL_CAPTIONS_V1\n                # A transcrição termina na última palavra falada e o arquivo\n                # pode ter silêncio de padding. Só um excesso de legenda é\n                # erro; silêncio depois da última palavra é permitido.\n                caption_overflow_sec = max(\n                    0.0,\n                    float(caption_duration or 0.0) - float(actual_total_audio_dur or 0.0),\n                )\n                trailing_audio_silence_sec = max(\n                    0.0,\n                    float(actual_total_audio_dur or 0.0) - float(caption_duration or 0.0),\n                )\n                audio_caption_diff = caption_overflow_sec'''
    text = _replace_once(text, old, new, label="caption/sync-one-sided-boundary")

    old_dict = '''                    "captions_duration_sec": round(float(caption_duration or 0.0), 2),\n                    "video_duration_sec": round(float(final_dur or 0.0), 2),'''
    new_dict = '''                    "captions_duration_sec": round(float(caption_duration or 0.0), 2),\n                    "spoken_audio_end_sec": round(float(caption_duration or 0.0), 2),\n                    "audio_trailing_silence_sec": round(float(trailing_audio_silence_sec or 0.0), 2),\n                    "video_duration_sec": round(float(final_dur or 0.0), 2),'''
    return _replace_once(text, old_dict, new_dict, label="caption/sync-report-boundary")


def patch_renderer(text: str) -> str:
    if MARKER in text:
        return text
    text = patch_caption_boundaries(text)
    text = patch_approved_timeline_sanitize(text)
    text = patch_opening_overlay(text)
    text = patch_scene_overlay(text)
    text = patch_closing_overlay(text)
    text = patch_global_composite(text)
    text = patch_sync_validation(text)
    return text


def check_text(text: str) -> None:
    required = (
        MARKER,
        '"caption_render_mode"] = "global_audio_timeline"',
        'global_caption_overlays = self._caption_overlay_clips_for_window',
        'opening_caption_overlays = []',
        'expanded_scene_timeline: List[Dict[str, Any]] = []',
        'closing_caption_overlays = []',
        'do not turn trailing silence into a subtitle',
        'caption_overflow_sec = max(',
        '"audio_trailing_silence_sec"',
    )
    missing = [needle for needle in required if needle not in text]
    if missing:
        raise PatchError(f"hardening global de legendas incompleto: {missing}")


def apply(*, write: bool) -> int:
    original = RENDER_TARGET.read_text(encoding="utf-8")
    transformed = patch_renderer(original)
    if patch_renderer(transformed) != transformed:
        raise PatchError("transformação não idempotente")
    check_text(transformed)
    if write and transformed != original:
        RENDER_TARGET.write_text(transformed, encoding="utf-8")
    print(
        "Audio-timed global captions: "
        + ("alterado" if transformed != original else "já aplicado")
        + "."
    )
    return int(transformed != original)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply e/ou --check")
    try:
        if args.apply:
            apply(write=True)
        if args.check:
            check_text(RENDER_TARGET.read_text(encoding="utf-8"))
    except PatchError as exc:
        print(f"ERRO AUDIO-TIMED GLOBAL CAPTIONS: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
