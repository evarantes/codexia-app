from __future__ import annotations

import os
import unittest
from pathlib import Path

from app.services.channel_excellence_guard import install_channel_excellence_guard_patch


ROOT = Path(__file__).resolve().parents[1]


class _RetryDurationGenerator:
    render_calls = 0

    def generate_audio(self, text, lang="pt", *args, **kwargs):
        return {"text": text, "lang": lang}

    def _default_opening_text(self, channel_name, *, plan=None):
        return str((plan or {}).get("title") or "")

    def _default_reflection_text(self, plan=None, scenes=None):
        return ""

    def _default_closing_text(self, channel_name):
        return ""

    def _target_visual_count(self, scenes, plan=None, *, ai_available=True):
        return max(1, len(list(scenes or [])))

    def _build_visual_transition_decision(self, previous_profile, current_profile):
        return {"should_generate_new": True}

    def _resolve_contextual_closing(self, plan=None):
        return {"kind": "custom", "lines": ["Leve esta esperança com você."]}

    def create_video_from_plan(self, plan, *args, **kwargs):
        type(self).render_calls += 1
        return {
            "file_path": "/tmp/fake.mp4",
            "render_report": {
                "narration_plan": {
                    "opening_text": str(plan.get("title") or ""),
                    "closing_text": "",
                },
                "visual_plan": {
                    "generated_image_count": 8,
                    "reused_image_count": 0,
                    "average_image_duration_sec": 7.5,
                },
            },
        }


class DurationRetryConfirmationTests(unittest.TestCase):
    def setUp(self):
        os.environ["ENABLE_DURATION_SANITY_PREFLIGHT"] = "true"
        os.environ["ENABLE_FINAL_VIDEO_QUALITY_GATE"] = "true"

    def tearDown(self):
        os.environ.pop("ENABLE_DURATION_SANITY_PREFLIGHT", None)
        os.environ.pop("ENABLE_FINAL_VIDEO_QUALITY_GATE", None)

    def test_retry_of_duration_failure_is_explicit_confirmation(self):
        cls = type("DurationApprovedByRetry", (_RetryDurationGenerator,), {"render_calls": 0})
        install_channel_excellence_guard_patch(cls)
        text = " ".join(["esperança"] * 300)
        result = cls().create_video_from_plan({
            "title": "Jesus Está Presente",
            "duration_min": 1,
            "duration_max": 1,
            "duration_max_sec": 60,
            "target_duration_sec": 60,
            "force_reuse_assets": True,
            "scenes": [{"text": text}],
        })
        self.assertEqual(cls.render_calls, 1)
        report = result["channel_excellence_guard"]["duration_preflight"]
        self.assertFalse(report["passed"])
        self.assertTrue(report["overridden_by_user"])
        self.assertEqual(report["approval_source"], "retry_after_duration_warning")

    def test_frontend_exposes_post_editorial_duration_confirmation(self):
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        self.assertIn("Continuar assim mesmo", html)
        self.assertIn("isDurationWarningRetry", html)
        self.assertIn("Nenhuma mídia paga foi gerada nesta tentativa.", html)
        self.assertIn("Confirmar duração do roteiro?", html)


if __name__ == "__main__":
    unittest.main()
