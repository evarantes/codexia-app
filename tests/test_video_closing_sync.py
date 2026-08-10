import tempfile
import unittest

from app.services.media_probe import (
    duration_sync_tolerance_seconds,
    media_durations_match,
)
from app.services.video_generator import VideoGenerator


class VideoClosingSyncTests(unittest.TestCase):
    def test_tolerance_is_proportional_and_capped(self):
        self.assertEqual(duration_sync_tolerance_seconds(2.0), 0.5)
        self.assertAlmostEqual(duration_sync_tolerance_seconds(174.0), 1.74)
        self.assertEqual(duration_sync_tolerance_seconds(600.0), 3.0)

    def test_media_duration_validation_uses_same_tolerance(self):
        base = {"video_duration": 175.7, "audio_duration": 174.0}
        self.assertTrue(media_durations_match(base))
        self.assertFalse(
            media_durations_match({"video_duration": 176.0, "audio_duration": 174.0})
        )

    def test_short_clip_freezes_last_frame_to_audio_target(self):
        try:
            from moviepy import AudioClip, ColorClip
        except ImportError:
            from moviepy.editor import AudioClip, ColorClip

        with tempfile.TemporaryDirectory() as tmp:
            generator = VideoGenerator(output_dir=tmp)
            clip = ColorClip(size=(16, 16), color=(20, 30, 40), duration=172.8)
            audio = AudioClip(lambda t: 0, duration=174.35, fps=8000)
            clip = generator._set_clip_audio(clip, audio)
            synced = None
            try:
                synced, report = generator._synchronize_video_clip_duration(clip, 174.35)
                self.assertEqual(report["action"], "freeze_last_frame")
                self.assertAlmostEqual(float(synced.duration), 174.35, places=3)
                self.assertIsNotNone(synced.get_frame(174.30))
            finally:
                if synced is not None:
                    synced.close()
                clip.close()
                audio.close()

    def test_long_clip_is_trimmed_to_audio_target(self):
        try:
            from moviepy import ColorClip
        except ImportError:
            from moviepy.editor import ColorClip

        with tempfile.TemporaryDirectory() as tmp:
            generator = VideoGenerator(output_dir=tmp)
            clip = ColorClip(size=(16, 16), color=(20, 30, 40), duration=176.0)
            synced = None
            try:
                synced, report = generator._synchronize_video_clip_duration(clip, 174.35)
                self.assertEqual(report["action"], "trim_video")
                self.assertAlmostEqual(float(synced.duration), 174.35, places=3)
            finally:
                if synced is not None:
                    synced.close()
                clip.close()


if __name__ == "__main__":
    unittest.main()
