import unittest

from app.services.narration_duration_feedback import (
    calibrated_body_duration_target,
    expansion_word_range,
    planning_duration_bounds,
)


class NarrationDurationFeedbackTests(unittest.TestCase):
    def test_exact_target_has_a_feasible_planning_band(self):
        lower, upper = planning_duration_bounds(60, 60, 60)
        self.assertAlmostEqual(lower, 58.8)
        self.assertAlmostEqual(upper, 61.2)
        self.assertLessEqual(lower, upper)

    def test_measured_short_audio_increases_body_target_using_real_pace(self):
        target = calibrated_body_duration_target(
            estimated_body_sec=40,
            actual_audio_sec=54,
            fixed_audio_sec=12,
            desired_audio_sec=58.8,
        )
        self.assertGreater(target, 40)
        self.assertAlmostEqual(target, 44.57142857)

    def test_small_duration_deficit_uses_a_proportional_word_range(self):
        min_words, max_words, target_words = expansion_word_range(
            current_words=125,
            estimated_body_sec=47,
            target_body_sec=52.5,
            words_per_minute=150,
        )

        self.assertEqual((min_words, max_words, target_words), (142, 152, 146))
        self.assertLess(target_words, 205)


if __name__ == "__main__":
    unittest.main()
