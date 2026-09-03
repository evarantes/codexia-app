from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DurationSecondsSupportTests(unittest.TestCase):
    def test_frontend_exposes_seconds_and_preserves_fractional_minutes(self):
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        self.assertIn("Segundos — teste rápido", html)
        self.assertIn("Minutos — produção normal", html)
        self.assertIn(":min=\"ytStoryDurationUnit === 'seconds' ? 5 : 1\"", html)
        self.assertIn("Math.round(durationRaw * 60) / 60", html)
        self.assertIn("const requestedMin = requestedMinSeconds / 60", html)
        self.assertIn("duration_unit: durationUnit", html)

    def test_backend_keeps_exact_second_target(self):
        router = (ROOT / "app/routers/youtube.py").read_text(encoding="utf-8")
        self.assertIn("duration: float = 5", router)
        self.assertIn("duration_min: Optional[float] = None", router)
        self.assertIn("duration_max: Optional[float] = None", router)
        self.assertIn('duration_unit: str = "minutes"', router)
        self.assertIn("max(5.0 / 60.0, min(60.0, float(requested_minutes)))", router)
        self.assertIn('script["duration_min_sec"] = int(round(requested_min_minutes * 60))', router)
        self.assertIn('script["target_duration_sec"] = int(round(requested_minutes * 60))', router)

    def test_story_generation_allows_subminute_word_ranges(self):
        editor = (ROOT / "app/services/story_review_editor.py").read_text(encoding="utf-8")
        generator = (ROOT / "app/services/ai_generator.py").read_text(encoding="utf-8")
        self.assertIn("max(5.0 / 60.0", editor)
        self.assertIn("min_words = max(8", editor)
        self.assertIn("duration_min_minutes: float = 10", editor)
        self.assertIn("max(5.0 / 60.0", generator)
        self.assertIn("min_words = max(8", generator)

    def test_api_and_worker_apply_same_seconds_contract(self):
        docker_api = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        docker_worker = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
        for source in (docker_api, docker_worker):
            self.assertIn("apply_duration_seconds_support.py --apply", source)
            self.assertIn("apply_duration_seconds_support.py --check", source)


if __name__ == "__main__":
    unittest.main()
