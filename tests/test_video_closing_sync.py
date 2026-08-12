import tempfile
import unittest
import inspect

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

    def test_subclip_clamps_marginal_child_audio_rounding_difference(self):
        try:
            from moviepy import AudioClip, ColorClip
        except ImportError:
            from moviepy.editor import AudioClip, ColorClip

        with tempfile.TemporaryDirectory() as tmp:
            generator = VideoGenerator(output_dir=tmp)
            audio = AudioClip(lambda t: 0, duration=140.94, fps=8000)
            clip = ColorClip(
                size=(16, 16),
                color=(20, 30, 40),
                duration=141.0,
            )
            clip = generator._set_clip_audio(clip, audio)
            trimmed = None
            try:
                # O MoviePy mostra ambos como 140.94, mas rejeita a diferença
                # interna acima de 1e-8 quando propaga o corte ao áudio.
                trimmed = generator._subclip(clip, 0, 140.940001)
                self.assertAlmostEqual(float(trimmed.duration), 140.94, places=6)
                self.assertEqual(
                    generator._last_subclip_clamp_debug["limiting_component"],
                    "audio",
                )
                self.assertAlmostEqual(
                    generator._last_subclip_clamp_debug["overshoot_sec"],
                    0.000001,
                    places=9,
                )
            finally:
                if trimmed is not None:
                    trimmed.close()
                clip.close()
                audio.close()

    def test_subclip_does_not_hide_a_real_duration_mismatch(self):
        try:
            from moviepy import AudioClip, ColorClip
        except ImportError:
            from moviepy.editor import AudioClip, ColorClip

        with tempfile.TemporaryDirectory() as tmp:
            generator = VideoGenerator(output_dir=tmp)
            audio = AudioClip(lambda t: 0, duration=10.0, fps=8000)
            clip = ColorClip(size=(16, 16), color=(20, 30, 40), duration=12.0)
            clip = generator._set_clip_audio(clip, audio)
            try:
                with self.assertRaises(ValueError):
                    generator._subclip(clip, 0, 11.0)
                self.assertIsNone(generator._last_subclip_clamp_debug)
            finally:
                clip.close()
                audio.close()

    def test_cinematic_endcard_extends_audio_track_with_matching_silence(self):
        source = inspect.getsource(VideoGenerator.create_video_from_plan)
        self.assertIn("silent_cinematic_tail_sec = end_clip_duration", source)
        self.assertIn("final_audio_track_duration_sec = float(target_video_duration", source)
        self.assertIn('"silent_endcard_duration_sec"', source)
        self.assertIn('"cta_visual_mode"] = "cinematic_background_bridge"', source)


if __name__ == "__main__":
    unittest.main()
