"""Regressão do contrato visual estrito da produção História/Devocional.

Cobre o caso que bloqueou a execução: uma solicitação legada chega com 8
imagens, mas uma produção de 10 minutos precisa cumprir o mínimo de 20 antes
de entrar no Quality Gate.
"""

import unittest

from app.routers.youtube import _apply_director_visual_target
from app.services.intelligent_cost_optimizer import minimum_visual_count_for_duration
from app.services.video_generator import VideoGenerator


class VisualTargetContractTests(unittest.TestCase):
    def test_ten_minute_quality_floor_is_twenty(self):
        self.assertEqual(minimum_visual_count_for_duration(10), 20)

    def test_director_promotes_legacy_eight_image_default(self):
        payload = {
            "duration": 10,
            "image_mode": "multiple",
            "image_count": 8,
            "director_quality_required": True,
        }

        normalized = _apply_director_visual_target(payload)

        self.assertIs(normalized, payload)
        self.assertEqual(normalized["expected_image_count"], 20)
        self.assertEqual(normalized["strict_visual_target_count"], 20)
        self.assertTrue(normalized["strict_visual_quality_required"])

    def test_reused_eight_images_cannot_lower_strict_target(self):
        generator = VideoGenerator.__new__(VideoGenerator)
        scenes = [
            {"text": f"scene {index}", "_estimated_narration_sec": 30.0}
            for index in range(20)
        ]

        target = generator._target_visual_count(
            scenes,
            {
                "expected_image_count": 20,
                "strict_visual_target_count": 20,
            },
            selected_image_count=8,
        )

        self.assertEqual(target, 20)


if __name__ == "__main__":
    unittest.main()
