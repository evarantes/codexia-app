import unittest
from pathlib import Path

from scripts import apply_lightweight_stage6_recovery


class LongFormFfmpegRenderHardeningTests(unittest.TestCase):
    def test_long_form_runtime_selects_bounded_ffmpeg_renderer(self):
        block = apply_lightweight_stage6_recovery.VIDEO_FAST_BLOCK

        self.assertIn("VIDEO_LONG_FORM_FFMPEG_MIN_SECONDS", block)
        self.assertIn("VIDEO_LONG_FORM_FFMPEG_RENDER", block)
        self.assertIn('"long_form_bounded_ffmpeg"', block)
        self.assertIn('"ffmpeg_long_form_v1"', block)
        self.assertIn('"average_image_duration_sec": round(', block)
        self.assertIn('"timeline_source": str(caption_timeline_source or "unknown")', block)

    def test_ffmpeg_timeout_does_not_depend_on_stdout_activity(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "services"
            / "lightweight_recovery_renderer.py"
        ).read_text(encoding="utf-8")

        self.assertIn("output_queue.get(timeout=1.0)", source)
        self.assertIn("if now - process_started > max_runtime", source)
        self.assertIn("process.poll()", source)
        self.assertNotIn("for raw_line in process.stdout", source)


if __name__ == "__main__":
    unittest.main()
