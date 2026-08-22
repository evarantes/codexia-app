from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.production_manifest_diagnostics import build_manifest_diagnostic, enrich_video_diagnostic_report


class ProductionManifestDiagnosticsTests(unittest.TestCase):
    def _base_plan(self):
        return {
            "action": "rerender_without_paid_media",
            "script_ok": True,
            "audio_ok": True,
            "images_ok": True,
            "video_ok": False,
            "valid_image_count": 48,
            "expected_image_count": 48,
            "missing_image_count": 0,
            "audio_path": "/data/audio.mp3",
            "estimated_new_cost_usd": 0,
            "estimated_new_cost_brl": 0,
        }

    def test_legacy_audio_is_never_reused_automatically(self):
        manifest = {
            "schema_version": 1,
            "artifacts": [
                {"kind": "audio", "exists": True, "source": "filesystem_checkpoint"},
                {"kind": "image", "exists": True, "source": "filesystem_checkpoint"},
            ],
        }
        with patch("app.services.production_manifest_diagnostics.load_manifest", return_value=manifest), patch(
            "app.services.production_manifest_diagnostics.build_recovery_plan", return_value=self._base_plan()
        ):
            result = build_manifest_diagnostic("task-old")

        self.assertTrue(result["audio"]["found"])
        self.assertFalse(result["audio"]["reusable"])
        self.assertEqual(result["audio"]["trust"], "legacy_unverified")
        self.assertEqual(result["effective_action"], "rebuild_untrusted_audio_then_recover")
        self.assertFalse(result["automatic_paid_recovery_allowed"])

    def test_guarded_audio_can_be_reported_as_reusable(self):
        manifest = {
            "schema_version": 1,
            "artifacts": [
                {"kind": "audio", "exists": True, "source": "tts_immediate"},
                {"kind": "image", "exists": True, "source": "renderer_immediate"},
            ],
        }
        with patch("app.services.production_manifest_diagnostics.load_manifest", return_value=manifest), patch(
            "app.services.production_manifest_diagnostics.build_recovery_plan", return_value=self._base_plan()
        ):
            result = build_manifest_diagnostic("task-new")

        self.assertTrue(result["audio"]["reusable"])
        self.assertEqual(result["audio"]["trust"], "narration_contract_v1")
        self.assertEqual(result["effective_action"], "rerender_without_paid_media")

    def test_diagnostic_enrichment_is_read_only_and_human_readable(self):
        manifest = {
            "schema_version": 1,
            "artifacts": [{"kind": "audio", "exists": True, "source": "filesystem_checkpoint"}],
        }
        plan = self._base_plan()
        plan.update({"valid_image_count": 42, "expected_image_count": 48, "missing_image_count": 6, "images_ok": False})
        with patch("app.services.production_manifest_diagnostics.load_manifest", return_value=manifest), patch(
            "app.services.production_manifest_diagnostics.build_recovery_plan", return_value=plan
        ):
            report = enrich_video_diagnostic_report({"checks": [], "recommendations": []}, task_id="task-old")

        names = {item["name"]: item for item in report["checks"]}
        self.assertEqual(names["Imagens preservadas"]["value"], "42/48")
        self.assertIn("NÃO confiável", names["Áudio preservado"]["value"])
        self.assertEqual(names["Recuperação paga automática"]["value"], "BLOQUEADA")
        self.assertFalse(report["production_manifest"]["automatic_paid_recovery_allowed"])

    def test_missing_manifest_is_explicit(self):
        with patch("app.services.production_manifest_diagnostics.load_manifest", return_value={}):
            result = build_manifest_diagnostic("missing")
        self.assertFalse(result["manifest_found"])
        self.assertFalse(result["automatic_paid_recovery_allowed"])

    def test_diagnostic_uses_recoverable_checkpoint_progress_instead_of_stale_failure_value(self):
        manifest = {
            "schema_version": 1,
            "artifacts": [
                {"kind": "video", "exists": True, "source": "render_immediate"},
                {"kind": "audio", "exists": True, "source": "tts_immediate"},
            ],
        }
        plan = self._base_plan()
        plan.update({"action": "review_existing_render", "video_ok": True})
        report = {
            "task": {"status": "failed", "progress": 20, "message": "Falha tardia"},
            "checks": [],
            "recommendations": ["Tarefa falhou em 20%: Falha tardia"],
        }
        with patch("app.services.production_manifest_diagnostics.load_manifest", return_value=manifest), patch(
            "app.services.production_manifest_diagnostics.build_recovery_plan", return_value=plan
        ):
            enriched = enrich_video_diagnostic_report(report, task_id="task-late")

        self.assertEqual(enriched["task"]["recorded_progress"], 20)
        self.assertEqual(enriched["task"]["progress"], 85)
        self.assertEqual(enriched["task"]["progress_source"], "production_manifest_checkpoint")
        self.assertNotIn("Tarefa falhou em 20%: Falha tardia", enriched["recommendations"])
        self.assertIn("85% recuperável", enriched["recommendations"][0])


if __name__ == "__main__":
    unittest.main()
