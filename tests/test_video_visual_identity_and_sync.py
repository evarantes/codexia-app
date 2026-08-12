import os
import tempfile
import unittest

from app.services.ai_generator import AIContentGenerator
from app.services.video_generator import VideoGenerator


class _CapturingImageService:
    def __init__(self, image_path: str):
        self.image_path = image_path
        self.prompts = []

    def generate_image(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        return self.image_path


class VideoVisualIdentityAndSyncTests(unittest.TestCase):
    def test_paid_scene_prompt_locks_jesus_identity_and_facial_hair_continuity(self):
        with tempfile.TemporaryDirectory(prefix="visual-identity-") as tmp:
            image_path = os.path.join(tmp, "scene.png")
            with open(image_path, "wb") as handle:
                handle.write(b"image" * 400)
            service = _CapturingImageService(image_path)
            generator = VideoGenerator(output_dir=tmp, ai_service=service)

            result = generator._ensure_image_for_scene(
                "Jesus comforts a lonely person while a woman watches in the background",
                "Jesus encontra quem se sente sozinho.",
                max_rounds=4,
            )

            self.assertEqual(result, image_path)
            self.assertEqual(len(service.prompts), 1)
            prompt = service.prompts[0]
            self.assertIn("Identity lock for Jesus Christ", prompt)
            self.assertIn("never gender-swap Jesus", prompt)
            self.assertIn("Do not accidentally copy a moustache or beard", prompt)
            self.assertTrue(generator._last_image_prompt_debug["jesus_identity_lock_applied"])

    def test_global_image_sanitizer_keeps_identity_guard_at_the_front(self):
        service = AIContentGenerator()
        prompt = service._sanitize_and_contextualize_image_prompt(
            "Jesus comforts a woman in a first-century village"
        )

        self.assertTrue(prompt.startswith("Identity lock for Jesus Christ:"))
        self.assertIn("Character identity lock:", prompt)
        self.assertIn("Do not accidentally copy a moustache or beard", prompt)

    def test_generic_devotional_does_not_inject_jesus_when_text_does_not_name_him(self):
        service = AIContentGenerator()
        director = service._build_biblical_story_director(
            story_title="Uma mensagem de esperança",
            story_context="Uma pessoa encontra consolo durante uma noite difícil.",
            scene_text="Amanhece e ela volta a ter esperança.",
        )

        self.assertNotIn("Jesus", director["allowed_characters"])

    def test_named_character_disappearing_forces_a_new_visual(self):
        generator = VideoGenerator()
        transition = generator._build_visual_transition_decision(
            {"characters": ["Jesus"], "environment": [], "action": [], "emotion": [], "time": [], "weather": [], "lighting": [], "viewpoint": [], "style": []},
            {"characters": [], "environment": [], "action": [], "emotion": [], "time": [], "weather": [], "lighting": [], "viewpoint": [], "style": []},
        )

        self.assertTrue(transition["should_generate_new"])
        self.assertIn("personagem", transition["changed_dimensions"])

    def test_prepared_images_follow_contiguous_visual_groups_instead_of_round_robin(self):
        generator = VideoGenerator()
        paths = ["image-1.png", "image-2.png", "image-3.png", "image-4.png"]

        mapped = [
            generator._selected_image_for_visual_group(paths, group_id)
            for group_id in [0, 0, 1, 1, 2, 2, 3, 3]
        ]

        self.assertEqual(
            mapped,
            [
                "image-1.png", "image-1.png",
                "image-2.png", "image-2.png",
                "image-3.png", "image-3.png",
                "image-4.png", "image-4.png",
            ],
        )

    def test_short_audio_anchored_scene_is_not_stretched_by_visual_minimum(self):
        generator = VideoGenerator()
        info = generator._resolve_scene_visual_duration(
            {
                "scene_start": 8.0,
                "scene_end": 9.0,
                "caption_blocks": [{"start": 0.0, "end": 1.0, "caption": "Breve."}],
            },
            minimum_duration=2.8,
        )

        self.assertTrue(info["audio_anchored"])
        self.assertEqual(info["duration"], 1.0)

    def test_audio_anchored_scene_timeline_does_not_accumulate_synthetic_delay(self):
        generator = VideoGenerator()
        scenes = [
            {"text": "Cena um."},
            {"text": "Cena dois."},
            {"text": "Cena três."},
        ]
        sync = {
            "scene_timelines": [
                [{"block_index": 0, "caption": "Cena um.", "global_start": 5.0, "global_end": 10.0}],
                [{"block_index": 1, "caption": "Cena dois.", "global_start": 10.0, "global_end": 15.0}],
                [{"block_index": 2, "caption": "Cena três.", "global_start": 15.0, "global_end": 20.0}],
            ]
        }

        timeline = generator._build_official_scene_timeline(
            scenes=scenes,
            scene_caption_sync=sync,
            planned_scene_durations=[5.0, 5.0, 5.0],
            opening_text="Abertura.",
            opening_image="opening.png",
            title_duration=5.0,
            initial_opening_silence_sec=1.0,
            cta_text="Encerramento.",
            closing_image="closing.png",
            pause_before_cta_sec=1.0,
            cta_duration=3.0,
            end_duration=2.0,
            timeline_source="real_segments_aligned_to_narration",
        )

        story = [item for item in timeline if item["kind"] == "story"]
        self.assertEqual([item["scene_start"] for item in story], [5.0, 10.0, 15.0])
        self.assertEqual([item["scene_end"] for item in story], [10.0, 15.0, 20.0])
        self.assertEqual([item["audio_start"] for item in story], [5.0, 10.0, 15.0])
        self.assertTrue(all(item["synthetic_timeline_shift_sec"] == 0.0 for item in story))
        self.assertEqual(
            [item["caption_blocks"][0]["global_start"] for item in story],
            [5.0, 10.0, 15.0],
        )
        closing = next(item for item in timeline if item["kind"] == "closing")
        self.assertEqual(closing["scene_start"], 20.0)
        self.assertEqual(closing["audio_start"], 21.0)


if __name__ == "__main__":
    unittest.main()
