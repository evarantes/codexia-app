import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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
    def test_uploaded_settings_logo_reaches_renderer_branding_profile(self):
        with tempfile.TemporaryDirectory(prefix="renderer-logo-") as tmp:
            logo_path = os.path.join(tmp, "official-channel-logo.png")
            from PIL import Image

            logo = Image.new("RGBA", (320, 160), (30, 40, 90, 255))
            logo.save(logo_path, format="PNG")
            logo.close()
            resolver = SimpleNamespace(
                resolve_official_channel_logo=lambda: {
                    "selected_value": logo_path,
                    "selected_source": "settings_path",
                }
            )
            with patch(
                "app.services.global_settings_service.build_global_settings_service",
                return_value=resolver,
            ):
                branding = VideoGenerator(output_dir=tmp)._resolve_channel_branding(
                    {"channel_name": "HERDEIROS DAS PROMESSAS"}
                )

            self.assertEqual(branding["logo_path"], logo_path)
            self.assertEqual(branding["logo_source"], "settings_path")
            self.assertTrue(branding["future_ready"]["logo"])

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

    def test_cinematic_opening_is_short_immediate_and_keeps_current_channel_brand(self):
        generator = VideoGenerator()
        plan = {
            "title": "Estudo 1 — Quando o desafio aparece",
            "channel_name": "HERDEIROS DAS PROMESSAS",
        }
        narration = generator.prepare_final_narration_text(
            plan,
            [{"text": "A fé permanece firme quando surgem desafios."}],
        )

        self.assertEqual(narration["channel_name"], "HERDEIROS DAS PROMESSAS")
        self.assertLessEqual(narration["intro_opening_hold_sec"], 0.8)
        self.assertLessEqual(len(narration["opening_text"].split()), 14)
        self.assertIn("quando o desafio aparece", narration["opening_text"].lower())
        self.assertEqual(narration["end_screen_target_duration_sec"], 4.0)

    def test_cinematic_closing_uses_one_clear_cta_instead_of_four_commands(self):
        generator = VideoGenerator()
        narration = generator.prepare_final_narration_text(
            {
                "title": "Uma mensagem de esperança",
                "channel_name": "HERDEIROS DAS PROMESSAS",
            },
            [{"text": "A esperança renasce a cada manhã."}],
        )
        closing = narration["closing_text"].lower()

        self.assertIn("herdeiros das promessas", closing)
        self.assertIn("inscreva-se", closing)
        self.assertNotIn("curta este vídeo", closing)
        self.assertNotIn("comente", closing)
        self.assertNotIn("compartilhe", closing)
        self.assertNotIn("ative o sininho", closing)

    def test_endcard_uses_explicit_bible_reference_without_inventing_a_verse(self):
        generator = VideoGenerator()
        closing = generator._resolve_contextual_closing(
            {
                "title": "Esperança durante a tempestade",
                "verse_reference": "Salmos 46:1",
            }
        )

        self.assertEqual(closing["kind"], "verse")
        self.assertEqual(closing["reference"], "Salmos 46:1")
        self.assertEqual(closing["text"], "")
        self.assertEqual(closing["lines"], ["MEDITE EM SALMOS 46:1"])

    def test_endcard_prefers_explicit_verse_text_and_keeps_reference_visible(self):
        generator = VideoGenerator()
        closing = generator._resolve_contextual_closing(
            {
                "bible_verse": {
                    "text": "Uma mensagem bíblica curta já fornecida pelo roteiro.",
                    "reference": "Referência 1:2",
                }
            }
        )

        self.assertEqual(closing["kind"], "verse")
        self.assertEqual(closing["source"], "explicit_scripture")
        self.assertEqual(closing["lines"][0], "MEDITE EM REFERÊNCIA 1:2")
        self.assertIn("mensagem bíblica", " ".join(closing["lines"]).lower())
        self.assertLessEqual(len(closing["lines"]), 3)

    def test_endcard_builds_cost_free_contextual_reflection_when_verse_is_absent(self):
        generator = VideoGenerator()
        closing = generator._resolve_contextual_closing(
            {
                "title": "Quando o desafio aparece",
                "scenes": [{"text": "A incerteza chegou, mas a fé permaneceu."}],
            }
        )

        self.assertEqual(closing["kind"], "reflection")
        self.assertEqual(closing["source"], "rule_based_context")
        self.assertEqual(closing["lines"][0], "PARA REFLETIR")
        self.assertIn("desafio", closing["text"].lower())
        self.assertLessEqual(len(closing["lines"]), 3)

    def test_contextual_endcard_renders_inside_safe_area_with_channel_cta(self):
        generator = VideoGenerator()
        closing = generator._resolve_contextual_closing(
            {
                "title": "Deus permanece durante o desafio",
                "channel_name": "HERDEIROS DAS PROMESSAS",
            }
        )
        branding = {
            "channel_name": "HERDEIROS DAS PROMESSAS",
            "channel_title_lines": ["HERDEIROS DAS PROMESSAS", "ONDE A FÉ SE TORNA ATITUDE"],
            "final_message_lines": closing["lines"],
            "contextual_closing": closing,
            "endcard_cta_text": "INSCREVA-SE E CONTINUE CONOSCO",
            "primary_color": "#F6E7B0",
            "secondary_color": "#FFFFFF",
        }
        layout_report = {}

        frame = generator._build_cinematic_endcard_frame(
            branding,
            background_path=None,
            size=(1280, 720),
            layout_report=layout_report,
        )

        self.assertEqual(frame.shape, (720, 1280, 3))
        self.assertTrue(layout_report["text_fits"])
        self.assertFalse(layout_report["overflow_detected"])
        self.assertEqual(
            layout_report["sections"]["closing_phrase"]["lines"],
            ["INSCREVA-SE E CONTINUE CONOSCO"],
        )
        self.assertEqual(layout_report["contextual_closing"]["kind"], "reflection")

    def test_long_static_scene_is_split_into_cost_free_visual_beats(self):
        generator = VideoGenerator()
        beats = generator._plan_cinematic_visual_beats(22.625)

        self.assertEqual(len(beats), 4)
        self.assertAlmostEqual(sum(item["duration"] for item in beats), 22.625, places=5)
        self.assertTrue(all(item["duration"] <= 7.0 for item in beats))
        self.assertEqual(beats[0]["start"], 0.0)
        self.assertEqual(beats[-1]["end"], 22.625)

    def test_long_video_uses_bounded_visual_compositions_to_reduce_peak_memory(self):
        generator = VideoGenerator()

        short_hold = generator._memory_safe_visual_hold_seconds(4 * 60)
        long_hold = generator._memory_safe_visual_hold_seconds(10 * 60)
        long_beats = generator._plan_cinematic_visual_beats(10 * 60, max_hold_sec=long_hold)

        self.assertEqual(short_hold, 7.0)
        self.assertGreater(long_hold, short_hold)
        self.assertLessEqual(len(long_beats), 36)
        self.assertAlmostEqual(sum(item["duration"] for item in long_beats), 600.0, places=3)

    def test_caption_overlay_is_cropped_before_moviepy_keeps_it_in_memory(self):
        generator = VideoGenerator()
        overlay = generator.create_text_overlay(
            "Uma mensagem curta para acompanhar a narração.",
            size=(1280, 720),
        )

        clip = generator._clip_from_rgba(overlay, 2.0, crop_transparent=True)

        self.assertLess(clip.size[0] * clip.size[1], 1280 * 720 * 0.35)
        self.assertLess(clip.img.nbytes, overlay.nbytes * 0.35)
        if hasattr(clip, "close"):
            clip.close()


if __name__ == "__main__":
    unittest.main()
