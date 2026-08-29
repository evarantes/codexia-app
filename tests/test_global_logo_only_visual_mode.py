import os
import tempfile
import unittest
from pathlib import Path


class GlobalLogoOnlyVisualModeTests(unittest.TestCase):
    def test_payload_normalization_is_opt_in_and_fail_closed(self):
        from app.services.logo_only_visual_mode import apply_logo_only_to_payload

        original = {"topic": "teste", "logo_only_visuals": False, "image_mode": "multiple"}
        normal = apply_logo_only_to_payload(original)
        self.assertEqual(normal["image_mode"], "multiple")
        self.assertNotIn("disable_ai_image_generation", normal)

        with tempfile.TemporaryDirectory() as tmp:
            logo = Path(tmp) / "logo.png"
            logo.write_bytes(b"not-a-real-png-but-existing-for-contract-test")
            previous = os.environ.get("OFFICIAL_CHANNEL_LOGO_PATH")
            os.environ["OFFICIAL_CHANNEL_LOGO_PATH"] = str(logo)
            try:
                guarded = apply_logo_only_to_payload({"topic": "teste", "logo_only_visuals": True})
            finally:
                if previous is None:
                    os.environ.pop("OFFICIAL_CHANNEL_LOGO_PATH", None)
                else:
                    os.environ["OFFICIAL_CHANNEL_LOGO_PATH"] = previous

        self.assertTrue(guarded["logo_only_visuals"])
        self.assertEqual(guarded["image_mode"], "single")
        self.assertEqual(guarded["image_count"], 1)
        self.assertTrue(guarded["disable_ai_image_generation"])
        self.assertTrue(guarded["disable_ai_thumbnail_generation"])
        self.assertEqual(len(guarded["selected_images"]), 1)
        self.assertEqual(guarded["selected_images"], guarded["custom_image_paths"])
        self.assertEqual(guarded["thumbnail_path"], guarded["selected_images"][0])

    def test_context_override_returns_official_logo_without_provider(self):
        from app.services.logo_only_visual_mode import image_provider_override, logo_only_visual_context

        self.assertIsNone(image_provider_override())
        with tempfile.TemporaryDirectory() as tmp:
            logo = Path(tmp) / "logo.png"
            logo.write_bytes(b"logo")
            with logo_only_visual_context(True, logo_path=str(logo), logo_url="/static/uploads/logo.png"):
                self.assertEqual(image_provider_override(), "/static/uploads/logo.png")
        self.assertIsNone(image_provider_override())

    def test_hardening_contract_is_present_after_ci_apply(self):
        root = Path(__file__).resolve().parents[1]
        expected = {
            "app/main.py": ["CODEXIA_GLOBAL_LOGO_ONLY_VISUALS_V1:middleware", "X-Codexia-Logo-Only-Visuals"],
            "app/services/ai_generator.py": ["CODEXIA_GLOBAL_LOGO_ONLY_VISUALS_V1:provider:ai_generator.py", "image_provider_override"],
            "app/services/ai_router.py": ["CODEXIA_GLOBAL_LOGO_ONLY_VISUALS_V1:provider:ai_router.py", "image_provider_override"],
            "app/services/image_storyboard_service.py": ["CODEXIA_GLOBAL_LOGO_ONLY_VISUALS_V1:thumbnail", "images_generated"],
            "app/routers/youtube.py": ["logo_only_visuals: bool = False", "CODEXIA_GLOBAL_LOGO_ONLY_VISUALS_V1:dispatch"],
            "app/services/unified_video_pipeline.py": ["logo_only_visuals: bool = False", "CODEXIA_GLOBAL_LOGO_ONLY_VISUALS_V1:unified-builder"],
            "app/services/video_generator.py": ["CODEXIA_GLOBAL_LOGO_ONLY_VISUALS_V1:opening", "CODEXIA_GLOBAL_LOGO_ONLY_VISUALS_V1:music", "ai_image_generation_disabled"],
        }
        for rel, needles in expected.items():
            text = (root / rel).read_text(encoding="utf-8")
            for needle in needles:
                self.assertIn(needle, text, f"{needle} ausente em {rel}")

    def test_ui_uses_checkbox_and_not_separate_economic_test_button(self):
        root = Path(__file__).resolve().parents[1]
        js = (root / "app/static/youtube_logo_test_mode.js").read_text(encoding="utf-8")
        self.assertIn("Usar apenas a logo do canal", js)
        self.assertIn("Não gerar imagens nem thumbnail por IA", js)
        self.assertIn("logo_only_visuals", js)
        self.assertIn("X-Codexia-Logo-Only-Visuals", js)
        self.assertNotIn("Testar vídeo somente com o logo", js)
        self.assertNotIn("Teste econômico de narração", js)

    def test_global_script_is_loaded_by_every_static_html_page(self):
        root = Path(__file__).resolve().parents[1]
        pages = [root / "app/static/index.html"]
        pages.extend(sorted((root / "app/static/pages").rglob("*.html")))
        checked = 0
        for page in pages:
            text = page.read_text(encoding="utf-8")
            if "</body>" not in text:
                continue
            checked += 1
            self.assertIn('/static/youtube_logo_test_mode.js', text, str(page))
        self.assertGreater(checked, 1)


if __name__ == "__main__":
    unittest.main()
