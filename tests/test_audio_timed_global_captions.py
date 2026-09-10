from __future__ import annotations

import inspect
import unittest

from app.services.video_generator import VideoGenerator
from scripts.apply_audio_timed_global_captions import (
    MARKER,
    check_text,
    patch_renderer,
)


class AudioTimedGlobalCaptionTests(unittest.TestCase):
    def test_word_boundaries_keep_silence_outside_caption_window(self):
        generator = VideoGenerator.__new__(VideoGenerator)
        timeline = generator._caption_timeline_from_segments(
            [
                {
                    "words": [
                        {"word": "A", "start": 2.0, "end": 2.35},
                        {"word": "fé", "start": 2.45, "end": 2.85},
                        {"word": "permanece", "start": 3.0, "end": 3.65},
                        {"word": "firme", "start": 7.0, "end": 7.55},
                    ]
                }
            ],
            12.0,
            narration="A fé permanece firme",
        )

        self.assertTrue(timeline)
        self.assertAlmostEqual(float(timeline[0]["start"]), 2.0, places=3)
        self.assertAlmostEqual(float(timeline[-1]["end"]), 7.55, places=3)
        self.assertLess(float(timeline[-1]["end"]), 12.0)

    def test_segment_boundaries_are_not_stretched_to_file_duration(self):
        generator = VideoGenerator.__new__(VideoGenerator)
        timeline = generator._caption_timeline_from_segments(
            [
                {"start": 1.5, "end": 3.0, "text": "Primeiro trecho."},
                {"start": 5.0, "end": 7.25, "text": "Segundo trecho."},
            ],
            10.0,
            narration="Primeiro trecho. Segundo trecho.",
        )

        self.assertEqual(float(timeline[0]["start"]), 1.5)
        self.assertEqual(float(timeline[-1]["end"]), 7.25)
        self.assertLess(float(timeline[-1]["end"]), 10.0)

    def test_renderer_has_one_global_caption_composite_and_no_local_copies(self):
        source = inspect.getsource(VideoGenerator.create_video_from_plan)
        self.assertIn(MARKER, source)
        self.assertIn('"caption_render_mode"] = "global_audio_timeline"', source)
        self.assertIn("opening_caption_overlays = []", source)
        self.assertIn("expanded_scene_timeline: List[Dict[str, Any]] = []", source)
        self.assertIn("closing_caption_overlays = []", source)
        self.assertEqual(
            source.count("global_caption_overlays = self._caption_overlay_clips_for_window"),
            1,
        )
        self.assertEqual(source.count("self._caption_overlay_clips_for_window("), 1)

    def test_patch_is_idempotent_and_has_required_contract(self):
        from pathlib import Path

        source = Path("app/services/video_generator.py").read_text(encoding="utf-8")
        transformed = patch_renderer(source)
        self.assertEqual(transformed, source)
        check_text(transformed)


if __name__ == "__main__":
    unittest.main()
