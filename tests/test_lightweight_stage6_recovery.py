import math
import os
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from PIL import Image

from app.services.intelligent_cost_optimizer import (
    build_sparse_visual_optimization_plan,
    validate_optimization_confirmation,
)
from app.services.lightweight_recovery_renderer import (
    build_concat_text,
    build_ffmpeg_command,
    build_srt_text,
    build_visual_segments,
    render_lightweight_recovery_video,
)


class LightweightStage6RecoveryTests(unittest.TestCase):
    def _image(self, root: str, name: str, value: int) -> str:
        path = os.path.join(root, name)
        # Gere um quadro determinístico com detalhe visual realista para o smoke.
        # O renderer rejeita ativos <1 KB e MP4 final <50 KB; um PNG chapado pode
        # comprimir artificialmente abaixo desses guards, algo que não representa
        # as imagens preservadas de produção. Mantemos os guards intactos.
        width, height = 320, 180
        data = bytearray(width * height * 3)
        for idx in range(width * height):
            x = idx % width
            y = idx // width
            seed = (x * 37 + y * 53 + value * 17 + (x * y) * 3) & 0xFF
            base = idx * 3
            data[base] = seed
            data[base + 1] = (seed * 3 + value * 11) & 0xFF
            data[base + 2] = (seed * 7 + x + y) & 0xFF
        Image.frombytes("RGB", (width, height), bytes(data)).save(path)
        self.assertGreater(os.path.getsize(path), 1000)
        return path

    def _wav(self, root: str, seconds: float = 2.0) -> str:
        path = os.path.join(root, "narration.wav")
        rate = 16000
        frames = int(rate * seconds)
        with wave.open(path, "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(rate)
            for idx in range(frames):
                sample = int(1000 * math.sin(2.0 * math.pi * 220.0 * idx / rate))
                fh.writeframesraw(struct.pack("<h", sample))
        return path

    def test_lightweight_renderer_change_is_bound_to_confirmation_hash(self):
        common = dict(
            task_id="task-stage6",
            title="Vídeo de recuperação",
            target_visual_count=3,
            valid_image_paths=["/data/a.png", "/data/b.png", "/data/c.png"],
            script={"title": "Vídeo de recuperação", "scenes": [{"narration": "Texto completo."}]},
            audio_path="/data/audio.mp3",
        )
        original = build_sparse_visual_optimization_plan(**common, lightweight_recovery=False)
        lightweight = build_sparse_visual_optimization_plan(**common, lightweight_recovery=True)

        self.assertFalse(original["requires_confirmation"])
        self.assertTrue(lightweight["requires_confirmation"])
        self.assertTrue(lightweight["lightweight_recovery"])
        self.assertEqual(lightweight["render_strategy"], "ffmpeg_lightweight_recovery_v1")
        self.assertEqual(lightweight["paid_tts_calls"], 0)
        self.assertEqual(lightweight["external_music_provider_calls"], 0)
        self.assertTrue(lightweight["preserve_captions"])
        self.assertNotEqual(original["plan_hash"], lightweight["plan_hash"])
        self.assertTrue(validate_optimization_confirmation(lightweight, lightweight["plan_hash"]))
        self.assertFalse(validate_optimization_confirmation(lightweight, original["plan_hash"]))

    def test_visual_mapping_preserves_order_and_uses_local_endcard(self):
        with tempfile.TemporaryDirectory() as tmp:
            images = [self._image(tmp, f"img-{idx}.png", 30 + idx * 30) for idx in range(3)]
            endcard = self._image(tmp, "end.png", 180)
            timeline = [
                {"kind": "opening", "scene_start": 0.0, "scene_end": 0.5},
                {"kind": "story", "scene_start": 0.5, "scene_end": 1.0},
                {"kind": "story", "scene_start": 1.0, "scene_end": 1.5},
                {"kind": "story", "scene_start": 1.5, "scene_end": 2.0},
                {"kind": "story", "scene_start": 2.0, "scene_end": 2.5},
                {"kind": "story", "scene_start": 2.5, "scene_end": 3.0},
                {"kind": "endcard", "scene_start": 3.0, "scene_end": 3.5},
            ]
            segments = build_visual_segments(
                selected_images=images,
                official_scene_timeline=timeline,
                target_duration=3.5,
                endcard_image=endcard,
            )
            used = [item["image_path"] for item in segments]
            self.assertEqual(used[0], os.path.abspath(images[0]))
            self.assertEqual(used[-1], os.path.abspath(endcard))
            self.assertTrue(all(item["duration"] > 0 for item in segments))
            self.assertAlmostEqual(sum(item["duration"] for item in segments), 3.5, places=2)

    def test_srt_and_ffconcat_are_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = self._image(tmp, "img.png", 90)
            srt = build_srt_text(
                [
                    {"start": 0.1, "end": 0.8, "caption": "Primeira legenda."},
                    {"start": 0.8, "end": 1.6, "caption": "Segunda legenda."},
                ],
                max_duration=2.0,
            )
            self.assertIn("00:00:00,100 --> 00:00:00,800", srt)
            self.assertIn("Primeira legenda.", srt)
            concat = build_concat_text([{"image_path": image, "duration": 2.0}])
            self.assertTrue(concat.startswith("ffconcat version 1.0"))
            self.assertEqual(concat.count("file '"), 2)
            self.assertIn("duration 2.000000", concat)

    def test_ffmpeg_command_has_no_network_provider_and_caps_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            concat = os.path.join(tmp, "visuals.ffconcat")
            srt = os.path.join(tmp, "captions.srt")
            audio = self._wav(tmp, 1.0)
            output = os.path.join(tmp, "out.mp4")
            Path(concat).write_text("ffconcat version 1.0\n", encoding="utf-8")
            Path(srt).write_text("", encoding="utf-8")
            command = build_ffmpeg_command(
                concat_path=concat,
                srt_path=srt,
                audio_path=audio,
                output_path=output,
                target_duration=1.0,
                video_size=(320, 180),
                threads=8,
            )
            joined = " ".join(command)
            self.assertIn("-threads 2", joined)
            self.assertIn("subtitles=", joined)
            self.assertNotIn("http://", joined)
            self.assertNotIn("https://", joined)
            self.assertNotIn("huggingface", joined.lower())

    def test_real_ffmpeg_smoke_uses_only_local_assets(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg/ffprobe indisponível")
        filters = subprocess_filters = None
        try:
            import subprocess
            filters = subprocess.run(
                [shutil.which("ffmpeg"), "-hide_banner", "-filters"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            subprocess_filters = (filters.stdout or "") + (filters.stderr or "")
        except Exception:
            self.skipTest("não foi possível consultar filtros do ffmpeg")
        if "subtitles" not in (subprocess_filters or ""):
            self.skipTest("ffmpeg sem filtro subtitles/libass")

        with tempfile.TemporaryDirectory() as tmp:
            image_a = self._image(tmp, "a.png", 40)
            image_b = self._image(tmp, "b.png", 120)
            audio = self._wav(tmp, 2.0)
            music_dir = os.path.join(tmp, "empty-music")
            os.makedirs(music_dir, exist_ok=True)
            output = os.path.join(tmp, "recovery.mp4")
            timeline = [
                {"kind": "opening", "scene_start": 0.0, "scene_end": 0.3},
                {"kind": "story", "scene_start": 0.3, "scene_end": 1.1},
                {"kind": "story", "scene_start": 1.1, "scene_end": 2.0},
            ]
            result = render_lightweight_recovery_video(
                output_path=output,
                selected_images=[image_a, image_b],
                audio_path=audio,
                captions=[{"start": 0.3, "end": 1.5, "caption": "Legenda preservada."}],
                official_scene_timeline=timeline,
                target_duration=2.0,
                video_size=(320, 180),
                music_dir=music_dir,
            )
            self.assertTrue(os.path.isfile(output))
            self.assertGreater(os.path.getsize(output), 50 * 1024)
            self.assertEqual(result["paid_provider_calls"], 0)
            self.assertEqual(result["external_downloads"], 0)
            self.assertEqual(result["music_source"], "none")
            self.assertAlmostEqual(result["duration_sec"], 2.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
