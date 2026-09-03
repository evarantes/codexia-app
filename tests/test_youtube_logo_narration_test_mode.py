from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app.services.narration_core import NARRATION_CORE_NAMESPACE, NARRATION_CORE_VERSION
from app.services.youtube_narration_gate import YouTubeNarrationGateService


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg necessário para o smoke do modo logo")
class YouTubeLogoNarrationTestModeTests(unittest.TestCase):
    def test_logo_mode_reuses_existing_core_audio_and_generates_zero_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = YouTubeNarrationGateService(output_root=str(root))
            user_id = 7
            preview_id = "a" * 32
            user_dir = service._user_dir(user_id)
            audio = user_dir / f"{preview_id}.mp3"
            metadata = user_dir / f"{preview_id}.json"
            logo = root / "logo.png"
            Image.new("RGB", (640, 360), (20, 20, 20)).save(logo)
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
            metadata.write_text(
                json.dumps(
                    {
                        "preview_id": preview_id,
                        "narration_core_version": NARRATION_CORE_VERSION,
                        "narration_core_namespace": NARRATION_CORE_NAMESPACE,
                    }
                ),
                encoding="utf-8",
            )
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
            self.assertEqual(result["narration_core_version"], NARRATION_CORE_VERSION)
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
