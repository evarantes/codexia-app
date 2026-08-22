from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ManifestAssetRecoveryHardeningTests(unittest.TestCase):
    def test_runtime_remaps_selected_images_before_renderer_validation(self):
        source = (ROOT / "app/services/video_generator.py").read_text(encoding="utf-8")
        marker = source.index("CODEXIA_MANIFEST_ASSET_PATH_RECOVERY_V1")
        selected_loop = source.index("if isinstance(selected_raw, list):", marker)
        forced_failure = source.index("Nenhuma chamada paga foi realizada.", marker)

        self.assertLess(marker, selected_loop)
        self.assertLess(selected_loop, forced_failure)
        self.assertIn("resolve_recovery_image_paths(", source[marker:forced_failure])
        self.assertIn('plan["selected_images"] = list(recovered_paths)', source[marker:forced_failure])
        self.assertIn('render_report["manifest_asset_recovery"]', source[marker:forced_failure])

    def test_retry_checkpoint_rejects_legacy_audio_before_dispatch(self):
        source = (ROOT / "app/routers/youtube.py").read_text(encoding="utf-8")
        marker = source.index("CODEXIA_MANIFEST_CHECKPOINT_TRUST_V1")
        block_end = source.index("render_only = bool(script_ok and images_ok and audio_ok)", marker)
        block = source[marker:block_end]

        self.assertIn("build_recovery_plan(task_id, payload_override=payload)", block)
        self.assertIn('manifest_checkpoint_plan.get("audio_reusable")', block)
        self.assertIn('"rebuild_untrusted_audio"', block)
        self.assertIn('payload.pop("reuse_audio_from", None)', block)

    def test_api_and_worker_builds_apply_same_recovery_contract(self):
        for filename in ("Dockerfile", "Dockerfile.worker"):
            content = (ROOT / filename).read_text(encoding="utf-8")
            apply_pos = content.index("apply_manifest_asset_recovery_hardening.py --apply")
            check_pos = content.index("apply_manifest_asset_recovery_hardening.py --check")
            adaptive_pos = content.index("apply_adaptive_render_threads_hardening.py --apply")
            self.assertLess(apply_pos, check_pos, filename)
            self.assertLess(check_pos, adaptive_pos, filename)


if __name__ == "__main__":
    unittest.main()
