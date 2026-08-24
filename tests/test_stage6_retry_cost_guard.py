from __future__ import annotations

import unittest

from scripts import apply_stage6_retry_cost_guard as guard


class Stage6RetryCostGuardTests(unittest.TestCase):
    def test_patch_blocks_paid_fallback_after_stage6_when_mp4_is_not_recoverable(self):
        source = guard.YOUTUBE.read_text(encoding="utf-8")
        patched = guard.patch_youtube(source)
        self.assertIn(guard.MARKER, patched)
        self.assertIn('"stage_6_render" in stage6_text', patched)
        self.assertIn("A recuperação foi interrompida antes de qualquer nova narração ou imagem paga.", patched)
        self.assertIn('"checked_without_paid_calls": True', patched)
        self.assertIn("Use Corrigir com ativos para revisar e autorizar explicitamente uma nova tentativa", patched)
        compile(patched, str(guard.YOUTUBE), "exec")

    def test_patch_is_idempotent(self):
        source = guard.YOUTUBE.read_text(encoding="utf-8")
        first = guard.patch_youtube(source)
        second = guard.patch_youtube(first)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
