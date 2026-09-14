import os
import unittest
from unittest.mock import Mock, patch

from app.routers.cinematic_campaign import _estimate_components, _rebalance_plan_to_budget
from app.services.cinematic_compositor import CinematicCompositor
from app.services.cinematic_director import CinematicDirector, CinematicDirectorError
from app.services.cinematic_video_provider import CinematicVideoProvider


class CinematicDirectorTests(unittest.TestCase):
    def test_story_profile_caps_motion_and_budget(self):
        profile = CinematicDirector._profile("story", 10, 999)
        self.assertEqual(profile["kind"], "story")
        self.assertAlmostEqual(profile["target_motion_ratio"], 0.28)
        self.assertLessEqual(profile["default_budget_brl"], 120.0)

    def test_devotional_profile_is_economical(self):
        profile = CinematicDirector._profile("devotional", 10, 30)
        self.assertEqual(profile["kind"], "devotional")
        self.assertAlmostEqual(profile["target_motion_ratio"], 0.10)
        self.assertLessEqual(profile["default_budget_brl"], 45.0)

    def test_current_sonnet_5_pricing_is_used_by_guard(self):
        with patch.dict(os.environ, {}, clear=True):
            cost = CinematicDirector._estimate_cost_usd(
                {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
            )
        self.assertAlmostEqual(cost, 12.0)

    def test_anthropic_director_disables_default_thinking_and_has_output_headroom(self):
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "content": [
                {"type": "thinking", "thinking": "summary"},
                {"type": "text", "text": "{}"},
            ],
            "usage": {"input_tokens": 100, "output_tokens": 200},
            "stop_reason": "end_turn",
        }
        with patch.dict(os.environ, {}, clear=True), patch(
            "app.services.cinematic_director.requests.post", return_value=response
        ) as post:
            text, usage = CinematicDirector()._call_anthropic("key", "system", "prompt")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["output_config"], {"effort": "medium"})
        self.assertGreaterEqual(body["max_tokens"], 32000)
        self.assertEqual(text, "{}")
        self.assertEqual(usage["output_tokens"], 200)

    def test_anthropic_empty_text_reports_stop_reason_instead_of_generic_empty_error(self):
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "content": [{"type": "thinking", "thinking": "too long"}],
            "usage": {"input_tokens": 100, "output_tokens": 36000},
            "stop_reason": "max_tokens",
        }
        with patch("app.services.cinematic_director.requests.post", return_value=response):
            with self.assertRaises(CinematicDirectorError) as ctx:
                CinematicDirector()._call_anthropic("key", "system", "prompt")
        self.assertIn("max_tokens", str(ctx.exception))
        self.assertNotIn("resposta vazia", str(ctx.exception).lower())

    def test_openrouter_accepts_typed_content_parts(self):
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "choices": [
                {
                    "message": {"content": [{"type": "text", "text": "{}"}]},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        with patch("app.services.cinematic_director.requests.post", return_value=response) as post:
            text, _usage = CinematicDirector()._call_openrouter("key", "system", "prompt")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["reasoning"]["effort"], "low")
        self.assertEqual(text, "{}")

    def test_normalize_plan_forces_tier_c_to_still_and_caps_motion(self):
        plan = {
            "scenes": [
                {"tier": "A", "target_seconds": 12, "generative_video_seconds": 15, "recommended_provider": "kling"},
                {"tier": "C", "target_seconds": 12, "generative_video_seconds": 15, "recommended_provider": "veo"},
                {"tier": "B", "target_seconds": 12, "generative_video_seconds": 15, "recommended_provider": "runway"},
            ]
        }
        normalized = CinematicDirector._normalize_plan(
            plan,
            content_type="story",
            duration_minutes=1,
            budget_brl=85,
        )
        self.assertEqual(normalized["motion_budget_seconds"], 17)
        self.assertLessEqual(normalized["motion_planned_seconds"], 17)
        self.assertEqual(normalized["scenes"][1]["tier"], "C")
        self.assertEqual(normalized["scenes"][1]["generative_video_seconds"], 0)
        self.assertEqual(normalized["scenes"][1]["recommended_provider"], "still")

    def test_budget_guard_demotes_premium_motion_and_stays_under_ceiling(self):
        scenes = []
        for index in range(1, 13):
            scenes.append(
                {
                    "index": index,
                    "tier": "A",
                    "purpose": "climax" if index in {10, 11} else "story",
                    "recommended_provider": "kling",
                    "generative_video_seconds": 14,
                }
            )
        for index in range(13, 31):
            scenes.append(
                {
                    "index": index,
                    "tier": "C",
                    "purpose": "story",
                    "recommended_provider": "still",
                    "generative_video_seconds": 0,
                }
            )
        plan = {"scenes": scenes, "quality_checks": {}}
        with patch.dict(os.environ, {"CODEXIA_USD_BRL": "5.12"}, clear=False):
            guarded = _rebalance_plan_to_budget(
                plan,
                content_type="story",
                duration_minutes=10,
                budget_brl=85,
            )
        guard = guarded["budget_guard"]
        self.assertTrue(guard["budget_respected"])
        self.assertLessEqual(guard["estimated_brl"], 85.0)
        self.assertLess(guard["premium_motion_seconds"], guard["original_premium_motion_seconds"])
        self.assertGreater(guard["economy_motion_seconds"], 0)
        self.assertTrue(guard["rebalanced"])
        self.assertTrue(guarded["quality_checks"]["budget_respected"])

    def test_budget_guard_preserves_total_motion_when_economy_floor_fits(self):
        plan = {
            "scenes": [
                {
                    "index": i,
                    "tier": "A" if i <= 8 else "B",
                    "purpose": "climax" if i == 8 else "story",
                    "recommended_provider": "kling" if i <= 8 else "runway",
                    "generative_video_seconds": 12,
                }
                for i in range(1, 15)
            ]
        }
        original_motion = sum(s["generative_video_seconds"] for s in plan["scenes"])
        with patch.dict(os.environ, {"CODEXIA_USD_BRL": "5.12"}, clear=False):
            guarded = _rebalance_plan_to_budget(
                plan,
                content_type="story",
                duration_minutes=10,
                budget_brl=85,
            )
        self.assertEqual(guarded["budget_guard"]["total_motion_seconds"], original_motion)
        self.assertLessEqual(guarded["budget_guard"]["estimated_brl"], 85.0)

    def test_estimate_components_matches_mixed_motion_rates(self):
        components = _estimate_components(
            content_type="story",
            duration_minutes=10,
            motion_seconds=168,
            premium_motion_seconds=56,
            estimated_images=30,
            voice="elevenlabs",
        )
        expected_motion = (112 * 0.05) + (56 * 0.11)
        self.assertAlmostEqual(components["motion"], expected_motion, places=6)
        self.assertGreater(components["recovery_reserve"], 0)


class CinematicCompositorTests(unittest.TestCase):
    def test_scene_windows_ignore_opening_and_endcard(self):
        report = {
            "scene_timeline": [
                {"kind": "opening", "start": 0, "end": 4},
                {"kind": "scene", "scene_index": 1, "visual_start": 4, "visual_end": 13},
                {"kind": "scene", "scene_index": 2, "start": 13, "duration": 8},
                {"kind": "endcard", "start": 21, "end": 25},
            ]
        }
        windows = CinematicCompositor.scene_windows_from_render_report(report)
        self.assertEqual([w["scene_index"] for w in windows], [1, 2])
        self.assertEqual(windows[0]["start"], 4.0)
        self.assertEqual(windows[0]["end"], 13.0)
        self.assertEqual(windows[1]["end"], 21.0)


class CinematicProviderTests(unittest.TestCase):
    def test_status_does_not_call_network_and_handles_missing_keys(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = CinematicVideoProvider(settings=None)
            status = provider.status()
        self.assertIn("runway", status)
        self.assertIn("veo", status)
        self.assertIn("kling", status)
        self.assertFalse(status["runway"]["configured"])
        self.assertFalse(status["veo"]["configured"])
        self.assertFalse(status["kling"]["configured"])


if __name__ == "__main__":
    unittest.main()
