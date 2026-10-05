import unittest

from app.services.narration_duration_feedback import (
    calibrated_body_duration_target,
    expansion_word_range,
    playback_rate_for_target,
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

    def test_small_real_audio_deficit_can_be_corrected_without_regenerating_tts(self):
        rate = playback_rate_for_target(
            speech_duration_sec=49.45,
            fixed_duration_sec=4.55,
            target_total_sec=58.8,
        )

        self.assertAlmostEqual(rate, 0.911521, places=5)

    def test_large_real_audio_deficit_requires_replanning_instead_of_extreme_slowdown(self):
        rate = playback_rate_for_target(
            speech_duration_sec=35,
            fixed_duration_sec=4.55,
            target_total_sec=58.8,
        )

        self.assertIsNone(rate)


if __name__ == "__main__":
    unittest.main()
