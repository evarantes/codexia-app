from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FinalRenderRecoveryTests(unittest.TestCase):
    def test_built_router_recovers_final_render_before_paid_retry(self):
        router = (ROOT / "app/routers/youtube.py").read_text(encoding="utf-8")
        self.assertIn("CODEXIA_FINAL_RENDER_RECOVERY_V1_START", router)
        self.assertIn("_recovery_try_promote_final_render(payload, task_id)", router)
        self.assertIn("probe_media_file(path)", router)
        self.assertIn("media_durations_match(probe)", router)
        self.assertIn("recovered_final_render_frames", router)
        self.assertIn("transition_to_awaiting_review_if_valid", router)

        salvage_pos = router.index("_recovery_try_promote_final_render(payload, task_id)")
        paid_block_pos = router.index('if bool(payload.get("_recovery_block_paid_regeneration"))', salvage_pos)
        self.assertLess(salvage_pos, paid_block_pos)

    def test_duration_guard_rejects_short_render_for_ten_minute_task(self):
        from app.routers.youtube import _recovery_final_video_duration_plausible

        self.assertFalse(_recovery_final_video_duration_plausible(55.0, 10))
        self.assertFalse(_recovery_final_video_duration_plausible(300.0, 10))
        self.assertTrue(_recovery_final_video_duration_plausible(600.0, 10))
        self.assertTrue(_recovery_final_video_duration_plausible(622.0, 10))
        self.assertFalse(_recovery_final_video_duration_plausible(1200.0, 10))

    def test_explicit_candidate_collector_finds_nested_video_references(self):
        from app.routers.youtube import _recovery_final_video_explicit_candidates

        task_result = {
            "render": {"file_path": "/data/media/videos/a.mp4"},
            "video_url": "/media/videos/b.mp4",
        }
        unified = {
            "_unified_recovery_meta": {"video_path": "/data/media/videos/c.mp4"},
            "nested": {"final_video_path": "/data/media/videos/d.mp4"},
        }
        found = _recovery_final_video_explicit_candidates(task_result, unified)
        self.assertEqual(
            found,
            [
                "/data/media/videos/a.mp4",
                "/media/videos/b.mp4",
                "/data/media/videos/d.mp4",
                "/data/media/videos/c.mp4",
            ],
        )

    def test_runtime_build_applies_final_render_recovery_after_checkpoint_v3(self):
        for filename in ("Dockerfile", "Dockerfile.worker"):
            content = (ROOT / filename).read_text(encoding="utf-8")
            checkpoint = content.index("apply_recovery_checkpoint_hardening.py --check")
            final_recovery = content.index("apply_final_render_recovery.py --apply")
            self.assertLess(checkpoint, final_recovery)
            self.assertIn("apply_final_render_recovery.py --check", content)


if __name__ == "__main__":
    unittest.main()
