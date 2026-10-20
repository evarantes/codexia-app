"""Regression tests: visual beat limits must never truncate narration."""
import os
import unittest
from unittest.mock import patch

from app.services.scene_director_active import _expand_scenes_for_cinematic_variety, _scene_text


class SceneDirectorNarrationPreservationTests(unittest.TestCase):
    def test_long_single_scene_keeps_every_word(self):
        narration = " ".join(f"palavra{i}" for i in range(1500))
        with patch.dict(os.environ, {"VIDEO_MAX_UNIQUE_SCENES": "8", "ENABLE_REAL_VISUAL_BEATS": "true"}):
            result, report = _expand_scenes_for_cinematic_variety(
                [{"text": narration, "image_prompt": "paisagem"}]
            )
        self.assertLessEqual(len(result), 8)
        self.assertEqual(" ".join(_scene_text(s) for s in result), narration)
        self.assertEqual(report["after"], len(result))

    def test_reserves_space_for_later_source_scenes(self):
        scenes = [{"text": " ".join(f"bloco{i}palavra{j}" for j in range(180)), "image_prompt": "teste"} for i in range(6)]
        with patch.dict(os.environ, {"VIDEO_MAX_UNIQUE_SCENES": "8", "ENABLE_REAL_VISUAL_BEATS": "true"}):
            result, _ = _expand_scenes_for_cinematic_variety(scenes)
        self.assertLessEqual(len(result), 8)
        self.assertEqual(
            " ".join(_scene_text(s) for s in result),
            " ".join(_scene_text(s) for s in scenes),
        )

    def test_more_original_scenes_than_budget_are_not_dropped(self):
        scenes = [{"text": f"cena {i}", "image_path": f"/manual/{i}.png"} for i in range(12)]
        with patch.dict(os.environ, {"VIDEO_MAX_UNIQUE_SCENES": "8", "ENABLE_REAL_VISUAL_BEATS": "true"}):
            result, report = _expand_scenes_for_cinematic_variety(scenes)
        self.assertEqual(result, scenes)
        self.assertEqual(report["skipped_reason"], "source_scene_count_at_or_above_budget")

    def test_manual_image_is_preserved(self):
        scenes = [{"text": "Narração aprovada", "image_path": "/manual/approved.png"}]
        with patch.dict(os.environ, {"VIDEO_MAX_UNIQUE_SCENES": "8", "ENABLE_REAL_VISUAL_BEATS": "true"}):
            result, _ = _expand_scenes_for_cinematic_variety(scenes)
        self.assertEqual(result[0]["image_path"], scenes[0]["image_path"])
        self.assertEqual(_scene_text(result[0]), scenes[0]["text"])


if __name__ == "__main__":
    unittest.main()
