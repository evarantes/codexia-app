from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.services.ready_video_repair import build_repair_preview


ROOT = Path(__file__).resolve().parents[1]


class ReadyVideoAssetRepairV4Tests(unittest.TestCase):
    def test_zero_new_images_still_requires_paid_audio_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = []
            refs = []
            for idx in range(38):
                path = root / f"img_{idx:02d}.png"
                path.write_bytes((b"x" * 1600) + bytes([idx % 255]))
                refs.append(str(path))
                artifacts.append({
                    "kind": "image",
                    "original_path": str(path),
                    "durable_path": str(path),
                    "exists": True,
                })
            script = {"scenes": [{"text": f"Cena {idx}"} for idx in range(38)]}
            manifest = {
                "expected_duration_minutes": 9.3,
                "expected_image_count": 38,
                "selected_image_references": refs,
                "artifacts": artifacts,
                "script": script,
            }
            result = {
                "payload": {"duration": 9.3, "seeded_script": script},
                "script": script,
            }
            old_audio_unit = os.environ.get("YOUTUBE_AUTO_TTS_MINUTE_COST_UNIT")
            old_usd_brl = os.environ.get("CODEXIA_USD_BRL")
            os.environ["YOUTUBE_AUTO_TTS_MINUTE_COST_UNIT"] = "0.012"
            os.environ["CODEXIA_USD_BRL"] = "5.20"
            try:
                preview = build_repair_preview(
                    task_id="task-video-45",
                    title="Vídeo 45",
                    task_result=result,
                    payload=result["payload"],
                    manifest=manifest,
                    image_cost_unit=0.04,
                    seconds_per_image=15,
                )
            finally:
                if old_audio_unit is None:
                    os.environ.pop("YOUTUBE_AUTO_TTS_MINUTE_COST_UNIT", None)
                else:
                    os.environ["YOUTUBE_AUTO_TTS_MINUTE_COST_UNIT"] = old_audio_unit
                if old_usd_brl is None:
                    os.environ.pop("CODEXIA_USD_BRL", None)
                else:
                    os.environ["CODEXIA_USD_BRL"] = old_usd_brl

            self.assertEqual(preview["existing_image_count"], 38)
            self.assertEqual(preview["required_unique_image_count"], 38)
            self.assertEqual(preview["missing_image_count"], 0)
            self.assertTrue(preview["regenerate_audio"])
            self.assertTrue(preview["paid_audio_calls_require_confirmation"])
            self.assertGreater(preview["estimated_new_audio_cost_usd"], 0)
            self.assertGreater(preview["estimated_new_audio_cost_brl"], 0)

    def test_runtime_requires_audio_confirmation_server_side_and_ui(self):
        router = (ROOT / "app/routers/youtube.py").read_text(encoding="utf-8")
        index = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        service = (ROOT / "app/services/ready_video_repair.py").read_text(encoding="utf-8")
        self.assertIn("CODEXIA_READY_VIDEO_ASSET_REPAIR_V4", service)
        self.assertIn("paid_audio_confirmation_required", router)
        self.assertIn('get("confirm_paid_audio")', router)
        self.assertIn("Estimativa preventiva da narração", index)
        self.assertIn("confirm_paid_audio: true", index)


if __name__ == "__main__":
    unittest.main()
