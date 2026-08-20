from __future__ import annotations

import os
import unittest
from pathlib import Path

from app.services.channel_excellence_guard import install_channel_excellence_guard_patch


ROOT = Path(__file__).resolve().parents[1]


class _DurationGenerator:
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


class DurationConfirmationTests(unittest.TestCase):
    def setUp(self):
        os.environ["ENABLE_DURATION_SANITY_PREFLIGHT"] = "true"
        os.environ["ENABLE_FINAL_VIDEO_QUALITY_GATE"] = "true"

    def tearDown(self):
        os.environ.pop("ENABLE_DURATION_SANITY_PREFLIGHT", None)
        os.environ.pop("ENABLE_FINAL_VIDEO_QUALITY_GATE", None)

    def _generator_cls(self, name: str):
        cls = type(name, (_DurationGenerator,), {"render_calls": 0})
        install_channel_excellence_guard_patch(cls)
        return cls

    def test_extreme_overrun_without_confirmation_still_blocks_before_render(self):
        cls = self._generator_cls("DurationBlockedWithoutApproval")
        text = " ".join(["esperança"] * 300)
        with self.assertRaisesRegex(RuntimeError, "Continuar assim mesmo"):
            cls().create_video_from_plan({
                "title": "Jesus Está Presente",
                "duration_min": 1,
                "duration_max": 1,
                "duration_max_sec": 60,
                "target_duration_sec": 60,
                "scenes": [{"text": text}],
            })
        self.assertEqual(cls.render_calls, 0)

    def test_extreme_overrun_with_user_confirmation_proceeds_and_is_audited(self):
        cls = self._generator_cls("DurationApprovedByUser")
        text = " ".join(["esperança"] * 300)
        result = cls().create_video_from_plan({
            "title": "Jesus Está Presente",
            "duration_min": 1,
            "duration_max": 1,
            "duration_max_sec": 60,
            "target_duration_sec": 60,
            "duration_override_approved": True,
            "scenes": [{"text": text}],
        })
        self.assertEqual(cls.render_calls, 1)
        report = result["channel_excellence_guard"]["duration_preflight"]
        self.assertFalse(report["passed"])
        self.assertTrue(report["overridden_by_user"])
        self.assertEqual(report["approval_source"], "user_confirmation")

    def test_frontend_uses_requested_range_and_warns_before_submit(self):
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        self.assertIn("const requestedMin = Math.max(1", html)
        self.assertIn("const requestedMax = Math.max(requestedMin", html)
        self.assertIn("Aviso de duração do vídeo", html)
        self.assertIn("duration_override_approved: durationOverrideApproved", html)
        self.assertIn("duration_min: requestedMin", html)
        self.assertIn("duration_max: requestedMax", html)
        self.assertNotIn(
            "const duration = Number(this.ytStoryPredictedDurationMinutesValue || this.ytStoryVideoDuration",
            html,
        )

    def test_backend_contract_accepts_and_propagates_duration_confirmation(self):
        router = (ROOT / "app/routers/youtube.py").read_text(encoding="utf-8")
        self.assertIn("duration_min: Optional[int] = None", router)
        self.assertIn("duration_max: Optional[int] = None", router)
        self.assertIn("duration_override_approved: bool = False", router)
        self.assertIn('script["duration_override_approved"] = duration_override_approved', router)
        self.assertIn('script["duration_max_sec"] = int(requested_max_minutes * 60)', router)


if __name__ == "__main__":
    unittest.main()
