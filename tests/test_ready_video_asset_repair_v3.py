from __future__ import annotations

import unittest
from pathlib import Path

from app.services.ready_video_repair_gate import confirmed_ready_video_repair_budget


ROOT = Path(__file__).resolve().parents[1]


class ReadyVideoAssetRepairV3Tests(unittest.TestCase):
    def _payload(self):
        budget = {
            "enabled": True,
            "expected_image_count": 40,
            "existing_image_count": 10,
            "missing_image_count": 30,
            "estimated_image_cost_usd": 1.2,
            "estimated_image_cost_brl": 0.0,
            "plan_hash": "repair:task-pastor:40:10:30",
        }
        return {
            "repair_mode": True,
            "repair_regenerate_audio": True,
            "repair_exclude_video": True,
            "repair_complete_visuals": True,
            "repair_source_scheduled_video_id": 45,
            "repair_image_budget": dict(budget),
            "seeded_script": {
                "repair_complete_visuals": True,
                "_partial_image_recovery": dict(budget),
            },
        }

    def test_exact_confirmed_budget_bypasses_only_legacy_confirmation(self):
        self.assertTrue(confirmed_ready_video_repair_budget(self._payload()))

    def test_plan_hash_change_fails_closed(self):
        payload = self._payload()
        payload["seeded_script"]["_partial_image_recovery"]["plan_hash"] = "repair:changed"
        self.assertFalse(confirmed_ready_video_repair_budget(payload))

    def test_missing_count_change_fails_closed(self):
        payload = self._payload()
        payload["repair_image_budget"]["missing_image_count"] = 29
        self.assertFalse(confirmed_ready_video_repair_budget(payload))

    def test_inconsistent_expected_count_fails_closed(self):
        payload = self._payload()
        payload["repair_image_budget"]["expected_image_count"] = 41
        payload["seeded_script"]["_partial_image_recovery"]["expected_image_count"] = 41
        self.assertFalse(confirmed_ready_video_repair_budget(payload))

    def test_missing_editorial_repair_flags_fails_closed(self):
        for key in (
            "repair_mode",
            "repair_regenerate_audio",
            "repair_exclude_video",
            "repair_complete_visuals",
        ):
            payload = self._payload()
            payload[key] = False
            self.assertFalse(confirmed_ready_video_repair_budget(payload), key)

    def test_runtime_patch_skips_legacy_guard_only_after_strict_gate(self):
        router = (ROOT / "app/routers/youtube.py").read_text(encoding="utf-8")
        self.assertIn("CODEXIA_READY_VIDEO_ASSET_REPAIR_V3", router)
        self.assertIn("confirmed_ready_video_repair_budget(payload)", router)
        self.assertIn('payload.pop("_recovery_block_paid_regeneration", None)', router)
        self.assertIn('payload.pop("_recovery_missing_assets", None)', router)
        self.assertIn("else:\n            payload = _maybe_enable_render_only_flags(payload, task_id)", router)


if __name__ == "__main__":
    unittest.main()
