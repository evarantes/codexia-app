from __future__ import annotations

# Revalidation marker: this suite must stay green with PR #80 already in main.
import os
import unittest
from unittest.mock import patch

from app.services.render_performance import ffmpeg_thread_decision


class RenderPerformanceTests(unittest.TestCase):
    def test_two_threads_when_resources_are_comfortable(self):
        decision = ffmpeg_thread_decision(
            available_mb=2400,
            cpu_count=4,
            swap_ratio=0.25,
            max_threads=2,
        )
        self.assertEqual(decision["selected_threads"], 2)
        self.assertEqual(decision["reason"], "resources_allow_2_threads")

    def test_falls_back_to_one_thread_under_memory_pressure(self):
        decision = ffmpeg_thread_decision(
            available_mb=900,
            cpu_count=4,
            swap_ratio=0.20,
            max_threads=2,
        )
        self.assertEqual(decision["selected_threads"], 1)
        self.assertIn("available_memory_below", decision["reason"])

    def test_falls_back_to_one_thread_when_swap_is_high(self):
        decision = ffmpeg_thread_decision(
            available_mb=2500,
            cpu_count=4,
            swap_ratio=0.82,
            max_threads=2,
        )
        self.assertEqual(decision["selected_threads"], 1)
        self.assertIn("swap_above", decision["reason"])

    def test_first_version_never_exceeds_two_threads(self):
        decision = ffmpeg_thread_decision(
            available_mb=8000,
            cpu_count=16,
            swap_ratio=0.0,
            max_threads=12,
        )
        self.assertEqual(decision["selected_threads"], 2)
        self.assertEqual(decision["max_threads"], 2)

    def test_environment_can_force_safe_one_thread(self):
        with patch.dict(os.environ, {"CODEXIA_FFMPEG_MAX_THREADS": "1"}, clear=False):
            decision = ffmpeg_thread_decision(
                available_mb=8000,
                cpu_count=8,
                swap_ratio=0.0,
            )
        self.assertEqual(decision["selected_threads"], 1)


if __name__ == "__main__":
    unittest.main()
