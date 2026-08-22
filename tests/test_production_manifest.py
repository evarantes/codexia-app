from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.services import production_manifest as pm


def _script(scene_count: int = 4):
    return {
        "title": "Teste",
        "scenes": [
            {"text": f"Cena {idx + 1}", "image_prompt": f"Imagem {idx + 1}"}
            for idx in range(scene_count)
        ],
    }


def _write_image(path: Path, color=(40, 80, 120)) -> None:
    base = Image.new("RGB", (160, 96), color)
    noise = Image.effect_noise(base.size, 60).convert("RGB")
    Image.blend(base, noise, 0.35).save(path, format="PNG")


class ProductionManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "manifests"
        self.images = base / "images"
        self.audio = base / "audio"
        self.videos = base / "videos"
        for path in (self.root, self.images, self.audio, self.videos):
            path.mkdir(parents=True, exist_ok=True)
        self.patchers = [
            patch.dict(os.environ, {"CODEXIA_PRODUCTION_MANIFEST_DIR": str(self.root)}, clear=False),
            patch.object(pm, "IMAGES_OUTPUT_DIR", str(self.images)),
            patch.object(pm, "AUDIO_OUTPUT_DIR", str(self.audio)),
            patch.object(pm, "VIDEO_OUTPUT_DIR", str(self.videos)),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def test_manifest_persists_task_assets_and_failure_checkpoint(self):
        image = self.images / "scene_001.png"
        _write_image(image)
        snapshot = {
            "task_id": "task-123",
            "status": "processing",
            "progress": 35,
            "message": "3/8 Gerando imagens...",
            "created_at": "2026-08-21T12:00:00+00:00",
            "result": {
                "payload": {"duration": 10, "image_count": 4},
                "script": _script(4),
                "selected_images": [str(image)],
            },
        }
        manifest = pm.sync_task_snapshot("task-123", snapshot)
        self.assertEqual(manifest["task_id"], "task-123")
        self.assertEqual(manifest["stage"], "stage_3_images")
        self.assertEqual(manifest["expected_image_count"], 4)
        self.assertTrue(any(a["kind"] == "image" and a["exists"] for a in manifest["artifacts"]))
        durable = next(a["durable_path"] for a in manifest["artifacts"] if a["kind"] == "image")
        self.assertTrue(os.path.isfile(durable))

        failed = dict(snapshot)
        failed.update({"status": "failed", "progress": 89, "message": "Falha no controle final de qualidade"})
        manifest2 = pm.sync_task_snapshot("task-123", failed)
        self.assertEqual(manifest2["status"], "failed")
        self.assertEqual(manifest2["progress"], 89)
        self.assertGreaterEqual(len(manifest2["checkpoints"]), 2)
        self.assertTrue(os.path.isfile(durable))

    def test_filesystem_checkpoint_only_claims_new_files(self):
        old = self.images / "old.png"
        _write_image(old, (20, 20, 20))
        os.utime(old, (1, 1))
        base = {
            "task_id": "task-new",
            "status": "pending",
            "progress": 0,
            "message": "Aguardando início...",
            "result": {"payload": {"duration": 5}, "script": _script(2)},
        }
        first = pm.sync_task_snapshot("task-new", base)
        self.assertFalse(any(Path(a.get("original_path", "")).name == "old.png" for a in first["artifacts"]))

        fresh = self.images / "fresh.png"
        _write_image(fresh, (30, 30, 30))
        second_snapshot = dict(base)
        second_snapshot.update({"status": "processing", "progress": 30, "message": "Gerando imagem (router)..."})
        second = pm.sync_task_snapshot("task-new", second_snapshot)
        self.assertTrue(any(Path(a.get("original_path", "")).name == "fresh.png" for a in second["artifacts"]))

    def test_partial_recovery_requires_second_resume_confirmation(self):
        audio = self.audio / "voice.mp3"
        audio.write_bytes(b"a" * 4096)
        image = self.images / "scene_001.png"
        _write_image(image)
        snapshot = {
            "task_id": "task-recovery",
            "status": "paused",
            "progress": 40,
            "message": "Produção pausada; ativos preservados.",
            "result": {
                "payload": {"duration": 10, "production_mode": "balanced"},
                "script": _script(4),
                "selected_images": [str(image)],
                "audio_checkpoint": {
                    "output_path": str(audio),
                    "duration_seconds": 600.0,
                },
            },
        }
        pm.sync_task_snapshot("task-recovery", snapshot)
        pm.record_artifact("task-recovery", str(audio), kind="audio", source="tts_immediate")
        with patch.object(pm, "_probe_duration", side_effect=lambda path: 600.0 if str(path).endswith("voice.mp3") else 0.0), patch.object(
            pm, "_image_cost_estimate", side_effect=lambda missing, duration, mode: (0.12 * missing, 0.62 * missing)
        ):
            plan = pm.build_recovery_plan("task-recovery")
            self.assertEqual(plan["action"], "regenerate_missing_images")
            self.assertTrue(plan["script_ok"])
            self.assertTrue(plan["audio_ok"])
            self.assertEqual(plan["valid_image_count"], 1)
            self.assertEqual(plan["expected_image_count"], 4)
            self.assertEqual(plan["missing_image_count"], 3)

            first = pm.confirm_or_prepare_partial_recovery("task-recovery", {"duration": 10})
            self.assertFalse(first["allow"])
            self.assertEqual(first["reason"], "confirmation_required")
            message = pm.recovery_confirmation_message(first["plan"])
            self.assertIn("clique Retomar novamente", message)
            self.assertIn("Nenhuma chamada paga foi feita ainda", message)

            second = pm.confirm_or_prepare_partial_recovery("task-recovery", {"duration": 10})
            self.assertTrue(second["allow"])
            patched = second["payload"]
            self.assertTrue(patched["_recovery_generate_missing_images_only"])
            self.assertEqual(patched["_recovery_missing_image_count"], 3)
            self.assertTrue(patched["reuse_audio_from"]["output_path"].endswith("voice.mp3"))
            self.assertEqual(len(patched["selected_images"]), 1)
            self.assertIsInstance(patched["seeded_script"], dict)

    def test_stale_selected_paths_are_remapped_to_durable_worker_files(self):
        original_paths = []
        for index in range(1, 4):
            path = self.images / f"scene_{index:03d}.png"
            _write_image(path, (index * 30, 40, 90))
            original_paths.append(str(path))
        extra = self.images / "cover_extra.png"
        _write_image(extra, (220, 180, 20))
        snapshot = {
            "task_id": "task-remap",
            "status": "failed",
            "progress": 85,
            "message": "Falha na renderização",
            "result": {
                "payload": {"duration": 10, "image_count": 3},
                "script": _script(3),
                "selected_images": original_paths,
            },
        }
        manifest = pm.sync_task_snapshot("task-remap", snapshot)
        for path in original_paths:
            Path(path).unlink()

        resolved = pm.resolve_recovery_image_paths(
            "task-remap",
            original_paths,
            expected_count=3,
        )

        self.assertTrue(resolved["complete"])
        self.assertEqual(len(resolved["paths"]), 3)
        self.assertEqual(resolved["matched_reference_count"], 3)
        self.assertFalse(resolved["ambiguous_fallback"])
        self.assertTrue(all(Path(path).is_file() for path in resolved["paths"]))
        self.assertTrue(all("production_manifests" not in path or "assets" in path for path in resolved["paths"]))
        self.assertEqual(manifest["expected_image_count"], 3)

    def test_more_unordered_candidates_than_slots_are_not_chosen_arbitrarily(self):
        for name, color in (
            ("alpha.png", (20, 40, 60)),
            ("beta.png", (50, 70, 90)),
            ("gamma.png", (80, 100, 120)),
        ):
            _write_image(self.images / name, color)
        snapshot = {
            "task_id": "task-ambiguous",
            "status": "failed",
            "progress": 35,
            "message": "Falha após imagens",
            "result": {
                "payload": {"duration": 5, "image_count": 2},
                "script": _script(2),
            },
        }
        pm.sync_task_snapshot("task-ambiguous", snapshot)

        resolved = pm.resolve_recovery_image_paths("task-ambiguous", [], expected_count=2)

        self.assertFalse(resolved["complete"])
        self.assertTrue(resolved["ambiguous_fallback"])
        self.assertEqual(resolved["paths"], [])
        self.assertFalse(resolved["paid_calls_performed"])

    def test_untrusted_audio_requires_explicit_second_confirmation(self):
        image = self.images / "scene_001.png"
        _write_image(image)
        audio = self.audio / "legacy.mp3"
        audio.write_bytes(b"a" * 4096)
        snapshot = {
            "task_id": "task-untrusted-audio",
            "status": "failed",
            "progress": 85,
            "message": "Falha na renderização",
            "result": {
                "payload": {"duration": 10, "image_count": 1},
                "script": _script(1),
                "selected_images": [str(image)],
                "audio_checkpoint": {"output_path": str(audio), "duration_seconds": 600.0},
            },
        }
        pm.sync_task_snapshot("task-untrusted-audio", snapshot)
        with patch.object(pm, "_probe_duration", return_value=600.0):
            plan = pm.build_recovery_plan("task-untrusted-audio")
            self.assertEqual(plan["action"], "rebuild_untrusted_audio")
            self.assertTrue(plan["audio_found"])
            self.assertFalse(plan["audio_reusable"])

            first = pm.confirm_or_prepare_partial_recovery("task-untrusted-audio", {"duration": 10})
            self.assertFalse(first["allow"])
            self.assertEqual(first["reason"], "confirmation_required")

            second = pm.confirm_or_prepare_partial_recovery("task-untrusted-audio", {"duration": 10})
            self.assertTrue(second["allow"])
            payload = second["payload"]
            self.assertNotIn("reuse_audio_from", payload)
            self.assertTrue(payload["seeded_script"]["_manifest_recovery_policy"]["rebuild_audio"])
            self.assertFalse(payload["force_render_only"])

    def test_no_paid_confirmation_when_script_or_audio_missing(self):
        snapshot = {
            "task_id": "task-blocked",
            "status": "failed",
            "progress": 20,
            "message": "Falha antes da mídia.",
            "result": {"payload": {"duration": 10}},
        }
        pm.sync_task_snapshot("task-blocked", snapshot)
        plan = pm.build_recovery_plan("task-blocked")
        self.assertEqual(plan["action"], "blocked")
        decision = pm.confirm_or_prepare_partial_recovery("task-blocked", {"duration": 10})
        self.assertFalse(decision["allow"])
        self.assertEqual(decision["reason"], "blocked")


if __name__ == "__main__":
    unittest.main()
