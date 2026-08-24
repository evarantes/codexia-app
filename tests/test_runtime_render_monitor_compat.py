from __future__ import annotations

import unittest

from scripts import apply_runtime_render_monitor_compat as compat


class RuntimeRenderMonitorCompatTests(unittest.TestCase):
    def test_patch_prevents_generic_300s_monitor_from_preempting_final_render(self):
        source = compat.YOUTUBE.read_text(encoding="utf-8")
        patched = compat.patch_youtube(source)

        self.assertIn(compat.MARKER, patched)
        self.assertIn("def _runtime_effective_interruption_seconds", patched)
        self.assertIn("VIDEO_RUNTIME_RENDER_INTERRUPTION_SECONDS", patched)
        self.assertIn("VIDEO_RENDER_HARD_STALL_SECONDS", patched)
        self.assertIn('"stage_6_render" in stage_text', patched)
        self.assertIn("render_limit = max(900, min(7200, raw_render_limit)) + 120", patched)
        self.assertGreaterEqual(
            patched.count("_runtime_effective_interruption_seconds(task, telemetry_obj)"),
            1,
        )
        self.assertNotIn(compat.OLD_CONDITION, patched)
        compile(patched, str(compat.YOUTUBE), "exec")

    def test_patch_is_idempotent(self):
        source = compat.YOUTUBE.read_text(encoding="utf-8")
        first = compat.patch_youtube(source)
        second = compat.patch_youtube(first)
        self.assertEqual(first, second)

    def test_normal_runtime_limit_remains_300_seconds_contract(self):
        self.assertIn('or "300"', compat.BASE_HELPER)
        self.assertIn("return max(120, min(30 * 60, raw))", compat.BASE_HELPER)
        self.assertIn("if not is_final_render", compat.HELPER)
        self.assertIn("return int(normal_limit)", compat.HELPER)


if __name__ == "__main__":
    unittest.main()
