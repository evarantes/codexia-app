import unittest

from app.services.visual_quality_repair import (
    build_auto_visual_repair_plan,
    rendered_visual_diversity_report,
)


def test_reused_path_count_does_not_fail_a_varied_render():
    report = {
        "visual_plan": {
            "requested_image_count": 8,
            "reused_image_count": 3,
            "average_image_duration_sec": 8,
        },
        "scene_visuals": [
            {"image_path": f"/images/scene-{index}.png", "final_visual_duration_sec": 8}
            for index in range(8)
        ],
    }

    result = rendered_visual_diversity_report(report)

    assert result["passed"] is True
    assert result["unique_rendered_image_count"] == 8
    assert result["path_reuse_count_is_advisory"] == 3


def test_one_repeated_image_fails_even_when_legacy_average_is_low():
    report = {
        "visual_plan": {
            "requested_image_count": 8,
            "reused_image_count": 1,
            "average_image_duration_sec": 8,
        },
        "scene_visuals": [
            {"image_path": "/images/scene-1.png", "final_visual_duration_sec": 8}
            for _ in range(8)
        ],
    }

    result = rendered_visual_diversity_report(report)

    assert result["passed"] is False
    assert result["unique_rendered_image_count"] == 1
    assert result["minimum_unique_image_count"] == 6


def test_auto_repair_forces_multiple_images_and_preserves_narration():
    original = {
        "title": "Teste",
        "scenes": [{"text": f"Cena {index}"} for index in range(8)],
        "selected_images": ["/images/scene-1.png"],
        "seed_audio_path": "/audio/narration.mp3",
        "seed_narration_text": "Narração aprovada",
    }
    report = {
        "visual_plan": {"requested_image_count": 8},
        "scene_visuals": [
            {"image_path": "/images/scene-1.png", "final_visual_duration_sec": 40}
        ],
        "audio_generation": {"output_path": "/audio/narration.mp3"},
    }

    result = build_auto_visual_repair_plan(
        original,
        report,
        expected_image_count=8,
        task_id="task-1",
        attempt=1,
        max_new_image_calls=4,
        reuse_audio=True,
    )

    assert result["ok"] is True
    assert result["plan"]["image_mode"] == "multiple"
    assert result["plan"]["single_bg"] is False
    assert result["plan"]["allow_image_reuse"] is False
    assert result["plan"]["seed_audio_path"] == "/audio/narration.mp3"
    assert result["plan"]["_partial_image_recovery"]["max_new_image_calls"] == 4
    assert original["selected_images"] == ["/images/scene-1.png"]

def test_auto_repair_replaces_only_visuals_not_used_in_a_repetitive_render():
    original = {
        "title": "Teste",
        "scenes": [{"text": f"Cena {index}"} for index in range(8)],
        "selected_images": [f"/images/candidate-{index}.png" for index in range(8)],
        "seed_audio_path": "/audio/narration.mp3",
        "seed_narration_text": "Narração aprovada",
    }
    report = {
        "visual_plan": {"requested_image_count": 8},
        "scene_visuals": [
            {"image_path": "/images/scene-1.png", "final_visual_duration_sec": 8}
            for _ in range(8)
        ],
        "audio_generation": {"output_path": "/audio/narration.mp3"},
    }

    result = build_auto_visual_repair_plan(
        original,
        report,
        expected_image_count=8,
        task_id="task-repeated-visuals",
        attempt=1,
        max_new_image_calls=12,
        reuse_audio=True,
        replace_repeated_visuals=True,
    )

    assert result["ok"] is True
    assert result["plan"]["selected_images"] == ["/images/scene-1.png"]
    assert result["missing_image_count"] == 7
    assert result["max_new_image_calls"] == 7
    assert result["plan"]["_partial_image_recovery"]["existing_image_count"] == 1
    assert result["plan"]["_partial_image_recovery"]["max_new_image_calls"] == 7
    assert result["plan"]["seed_audio_path"] == "/audio/narration.mp3"
    assert result["plan"]["automatic_visual_repair"]["replacing_repeated_visuals"] is True
    assert original["selected_images"] == [f"/images/candidate-{index}.png" for index in range(8)]

class VisualQualityRepairRegressionTests(unittest.TestCase):
    def test_repeated_render_replaces_unused_candidates_and_preserves_audio(self):
        original = {
            "title": "Teste",
            "scenes": [{"text": f"Cena {index}"} for index in range(8)],
            "selected_images": [f"/images/candidate-{index}.png" for index in range(8)],
            "seed_audio_path": "/audio/narration.mp3",
            "seed_narration_text": "Narração aprovada",
        }
        report = {
            "visual_plan": {"requested_image_count": 8},
            "scene_visuals": [
                {"image_path": "/images/scene-1.png", "final_visual_duration_sec": 8}
                for _ in range(8)
            ],
            "audio_generation": {"output_path": "/audio/narration.mp3"},
        }

        result = build_auto_visual_repair_plan(
            original,
            report,
            expected_image_count=8,
            task_id="task-repeated-visuals",
            attempt=1,
            max_new_image_calls=12,
            reuse_audio=True,
            replace_repeated_visuals=True,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected_images"], ["/images/scene-1.png"])
        self.assertEqual(result["missing_image_count"], 7)
        self.assertEqual(result["max_new_image_calls"], 7)
        self.assertEqual(result["plan"]["seed_audio_path"], "/audio/narration.mp3")
        self.assertTrue(result["plan"]["automatic_visual_repair"]["replacing_repeated_visuals"])
        self.assertEqual(
            original["selected_images"],
            [f"/images/candidate-{index}.png" for index in range(8)],
        )
