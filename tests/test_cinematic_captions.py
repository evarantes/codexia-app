from __future__ import annotations

import os
import unittest

import numpy as np

from app.services.cinematic_captions import apply_presentation_rollout, install_cinematic_caption_patch


class _DummyGenerator:
    def _caption_layout_metrics(self, text, size=(100, 100), max_lines=2, reserved_bottom_ratio=0.0, safe_area_override=None):
        return {"fits": True, "font_size_used": 99, "lines": [str(text)]}

    def _split_caption_units(self, text, max_words=8, max_chars=54):
        return [f"{max_words}:{max_chars}:{text}"]

    def create_text_overlay(self, text, *args, **kwargs):
        arr = np.zeros((100, 100, 4), dtype=np.uint8)
        arr[10:15, 10:15, 3] = 255
        return arr


class CinematicCaptionTests(unittest.TestCase):
    def tearDown(self):
        for key in ("ENABLE_SCENE_DIRECTOR", "ENABLE_CINEMATIC_CAPTIONS"):
            os.environ.pop(key, None)

    def test_presentation_rollout_defaults_on_but_respects_explicit_rollback(self):
        state = apply_presentation_rollout()
        self.assertTrue(state["scene_director_enabled"])
        self.assertTrue(state["cinematic_captions_enabled"])

        os.environ["ENABLE_CINEMATIC_CAPTIONS"] = "false"
        state = apply_presentation_rollout()
        self.assertFalse(state["cinematic_captions_enabled"])

    def test_caption_patch_uses_shorter_blocks_and_lower_overlay(self):
        cls = type("DummyPremiumGenerator", (_DummyGenerator,), {})
        install_cinematic_caption_patch(cls)
        generator = cls()

        # Padrão final mais discreto: no máximo 6 palavras / 40 caracteres por bloco.
        units = generator._split_caption_units("mensagem")
        self.assertEqual(units, ["6:40:mensagem"])

        arr = generator.create_text_overlay("texto", vertical_anchor="bottom")
        original_top = 10
        active_rows = np.where(arr[:, :, 3].max(axis=1) > 0)[0]
        self.assertGreater(int(active_rows.min()), original_top)

    def test_explicit_disable_preserves_original_caption_behavior(self):
        os.environ["ENABLE_CINEMATIC_CAPTIONS"] = "false"
        cls = type("DummyRollbackGenerator", (_DummyGenerator,), {})
        install_cinematic_caption_patch(cls)
        generator = cls()
        self.assertEqual(generator._split_caption_units("mensagem"), ["8:54:mensagem"])
        arr = generator.create_text_overlay("texto", vertical_anchor="bottom")
        active_rows = np.where(arr[:, :, 3].max(axis=1) > 0)[0]
        self.assertEqual(int(active_rows.min()), 10)


if __name__ == "__main__":
    unittest.main()
