from __future__ import annotations

import unittest
from pathlib import Path

from scripts.apply_caption_integrity_self_heal import (
    FATAL_MESSAGE,
    SELF_HEAL_MARKER,
    check_text,
    patch_renderer,
)


class CaptionIntegrityHardeningTests(unittest.TestCase):
    def test_legacy_fatal_validator_is_replaced_and_patch_is_idempotent(self):
        source = '''            if normalized_caption_text != normalized_tts_text:\n                raise Exception("Falha de validacao: legenda-base difere do texto enviado ao TTS.")\n'''
        transformed = patch_renderer(source)

        self.assertIn(SELF_HEAL_MARKER, transformed)
        self.assertNotIn(f'raise Exception("{FATAL_MESSAGE}")', transformed)
        self.assertIn("_codexia_force_canonical_caption_timeline", transformed)
        self.assertIn('"canonical_single_block"', transformed)
        self.assertEqual(patch_renderer(transformed), transformed)
        check_text(transformed)

    def test_ci_workspace_renderer_has_no_fail_closed_caption_mismatch(self):
        # A etapa de hardening do Video pipeline CI roda antes da suíte completa.
        # Este teste garante que o artefato efetivamente validado é o mesmo que
        # será construído no Docker/Coolify.
        renderer = Path("app/services/video_generator.py").read_text(encoding="utf-8")
        self.assertIn(SELF_HEAL_MARKER, renderer)
        self.assertNotIn(f'raise Exception("{FATAL_MESSAGE}")', renderer)
        check_text(renderer)


if __name__ == "__main__":
    unittest.main()
