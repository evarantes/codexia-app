from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app.services.youtube_narration_gate import YouTubeNarrationGateService


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg necessário para o smoke do modo logo")
class YouTubeLogoNarrationTestModeTests(unittest.TestCase):
    def test_logo_mode_reuses_existing_audio_and_generates_zero_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = YouTubeNarrationGateService(output_root=str(root))
            user_id = 7
            preview_id = "a" * 32
            user_dir = service._user_dir(user_id)
            audio = user_dir / f"{preview_id}.mp3"
            logo = root / "logo.png"
            Image.new("RGB", (640, 360), (20, 20, 20)).save(logo)
            # Keep this fixture cheap, but long enough for the production
            # validity guard (> 50 KB) to exercise a real MP4 instead of
            # failing only because a 1.2 s synthetic clip is too small.
            proc = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=5.0",
                    "-c:a", "libmp3lame", str(audio),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = service.generate_logo_test_video(
                preview_id=preview_id,
                user_id=user_id,
                logo_path=str(logo),
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "logo_only_narration_test")
            self.assertEqual(result["images_generated"], 0)
            self.assertFalse(result["thumbnail_generated"])
            self.assertTrue(result["audio_reused_exactly"])
            video = service.logo_test_video_path(preview_id=preview_id, user_id=user_id)
            self.assertTrue(video.is_file())
            self.assertGreater(video.stat().st_size, 50_000)

    def test_logo_mode_never_generates_audio_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = YouTubeNarrationGateService(output_root=str(root))
            logo = root / "logo.png"
            Image.new("RGB", (320, 180), (0, 0, 0)).save(logo)
            with self.assertRaises(Exception):
                service.generate_logo_test_video(
                    preview_id="b" * 32,
                    user_id=3,
                    logo_path=str(logo),
                )


if __name__ == "__main__":
    unittest.main()
