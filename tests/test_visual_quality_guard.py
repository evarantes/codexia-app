from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from app.services import visual_quality_guard as guard


class _FakeGenerator:
    def __init__(self):
        self.calls = []
        self.ai_service = type("AI", (), {"ai_task_id": None})()

    def _ensure_image_for_scene(self, prompt, **kwargs):
        self.calls.append(str(prompt))
        path = kwargs.get("_path")
        if path:
            return path
        raise RuntimeError("test path missing")

    def create_video_from_plan(self, plan, *args, **kwargs):
        return {"render_report": {"scene_visuals": []}, "file_path": "/tmp/fake.mp4"}


def _review(approve: bool, *, code: str = "", score: float = 9.0):
    issues = []
    critical = []
    if not approve:
        code = code or "abnormal_eyes"
        issues = [{"code": code, "severity": "critical", "message": "defeito crítico"}]
        critical = [code]
    return {
        "status": "reviewed",
        "approve": bool(approve),
        "score": score,
        "issues": issues,
        "critical_issue_codes": critical,
        "summary": "ok" if approve else "rejeitada",
    }


class VisualQualityGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.image_path = str(Path(self.tmp.name) / "scene.jpg")
        Image.new("RGB", (1280, 720), (120, 100, 90)).save(self.image_path, "JPEG")

    def tearDown(self):
        self.tmp.cleanup()
        for key in [
            "ENABLE_STRICT_VISUAL_REJECT",
            "ENABLE_VISUAL_CRITIC_AI",
            "VISUAL_QA_MAX_RETRIES",
            "VISUAL_QA_FAIL_CLOSED",
        ]:
            os.environ.pop(key, None)

    def _patched_class(self):
        class Fake(_FakeGenerator):
            pass

        return guard.install_visual_quality_guard_patch(Fake)

    def test_default_rollout_is_non_blocking_and_does_not_retry(self):
        cls = self._patched_class()
        instance = cls()
        with mock.patch.object(guard, "review_generated_image", return_value=_review(False)):
            result = instance._ensure_image_for_scene("prompt original", _path=self.image_path)
        self.assertEqual(result, self.image_path)
        self.assertEqual(instance.calls, ["prompt original"])

    def test_strict_mode_regenerates_only_rejected_generated_image(self):
        os.environ["ENABLE_STRICT_VISUAL_REJECT"] = "true"
        os.environ["VISUAL_QA_MAX_RETRIES"] = "1"
        cls = self._patched_class()
        instance = cls()
        reviews = [_review(False, code="abnormal_eyes", score=3.0), _review(True, score=9.2)]
        with mock.patch.object(guard, "review_generated_image", side_effect=reviews):
            result = instance._ensure_image_for_scene("Jesus consolando uma família", _path=self.image_path)
        self.assertEqual(result, self.image_path)
        self.assertEqual(len(instance.calls), 2)
        self.assertEqual(instance.calls[0], "Jesus consolando uma família")
        self.assertIn("QUALITY RETRY 1", instance.calls[1])
        self.assertIn("abnormal_eyes", instance.calls[1])
        self.assertEqual(len(instance._codexia_visual_guard_events), 2)

    def test_exhausted_retries_remain_fail_open_by_default(self):
        os.environ["ENABLE_STRICT_VISUAL_REJECT"] = "true"
        os.environ["VISUAL_QA_MAX_RETRIES"] = "1"
        cls = self._patched_class()
        instance = cls()
        with mock.patch.object(guard, "review_generated_image", return_value=_review(False, code="malformed_face", score=2.0)):
            result = instance._ensure_image_for_scene("prompt", _path=self.image_path)
        self.assertEqual(result, self.image_path)
        self.assertEqual(len(instance.calls), 2)

    def test_fail_closed_is_explicit_and_only_after_selective_retry(self):
        os.environ["ENABLE_STRICT_VISUAL_REJECT"] = "true"
        os.environ["VISUAL_QA_MAX_RETRIES"] = "1"
        os.environ["VISUAL_QA_FAIL_CLOSED"] = "true"
        cls = self._patched_class()
        instance = cls()
        with mock.patch.object(guard, "review_generated_image", return_value=_review(False, code="extra_limbs", score=1.5)):
            with self.assertRaisesRegex(RuntimeError, "extra_limbs"):
                instance._ensure_image_for_scene("prompt", _path=self.image_path)
        self.assertEqual(len(instance.calls), 2)

    def test_ai_disabled_review_is_local_and_cost_free(self):
        result = guard.review_generated_image(
            object(),
            self.image_path,
            visual_prompt="scene",
            narration_context="context",
        )
        self.assertTrue(result["approve"])
        self.assertEqual(result["status"], "ai_critic_disabled")
        self.assertIsNone(result["model"])

    def test_final_result_contains_visual_critic_audit(self):
        cls = self._patched_class()
        instance = cls()
        instance._codexia_visual_guard_events = [
            {"attempt": 0, "review": _review(True, score=9.0), "image_path": self.image_path}
        ]
        # create wrapper starts a new execution, therefore no stale events survive.
        result = instance.create_video_from_plan({"title": "teste"})
        self.assertIn("visual_quality_critic", result)
        self.assertEqual(result["visual_quality_critic"]["event_count"], 0)
        self.assertIn("visual_quality_critic", result["render_report"])


if __name__ == "__main__":
    unittest.main()
