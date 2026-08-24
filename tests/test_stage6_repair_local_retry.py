from __future__ import annotations

import unittest

from scripts import apply_stage6_repair_local_retry as patch


class Stage6RepairLocalRetryTests(unittest.TestCase):
    def _source(self) -> str:
        return "\n\n".join(
            [
                "# CODEXIA_READY_VIDEO_ASSET_REPAIR_V3",
                patch.RAW_REPAIR_GUARD_OLD,
                patch.PROMOTE_GUARD_OLD,
                patch.SOURCES_OLD,
                patch.VISUAL_COUNTS_OLD,
                patch.CONFIRMED_OLD,
            ]
        )

    def test_stage6_retry_becomes_local_only_and_idempotent(self) -> None:
        transformed = patch.patch_youtube(self._source())
        self.assertIn(patch.MARKER, transformed)
        self.assertIn('_repair_stage6_recovery_only', transformed)
        self.assertIn('current_stage_hint == "stage_6_render"', transformed)
        self.assertIn('retry_payload_source["script"]', transformed)
        self.assertIn('"retry_payload": 0', transformed)
        self.assertIn('payload["repair_regenerate_audio"] = False', transformed)
        self.assertIn('payload["repair_exclude_video"] = False', transformed)
        self.assertIn('payload["repair_complete_visuals"] = False', transformed)
        self.assertNotIn(patch.RAW_REPAIR_GUARD_OLD, transformed)
        self.assertNotIn(patch.PROMOTE_GUARD_OLD, transformed)
        self.assertNotIn(patch.CONFIRMED_OLD, transformed)
        self.assertEqual(transformed, patch.patch_youtube(transformed))

    def test_initial_repair_remains_editorial_not_render_only(self) -> None:
        transformed = patch.patch_youtube(self._source())
        self.assertIn('elif ready_repair_confirmed:', transformed)
        self.assertIn('payload["force_render_only"] = False', transformed)
        self.assertIn('payload.pop("_recovery_block_paid_regeneration", None)', transformed)

    def test_stage6_render_only_sanitizes_paid_media_flags_only_after_inventory(self) -> None:
        transformed = patch.patch_youtube(self._source())
        inventory_pos = transformed.index('payload = _maybe_enable_render_only_flags(payload, task_id)')
        sanitize_pos = transformed.index('payload["repair_regenerate_audio"] = False')
        self.assertLess(inventory_pos, sanitize_pos)
        self.assertIn('not bool(payload.get("_recovery_block_paid_regeneration"))', transformed)


if __name__ == "__main__":
    unittest.main()
