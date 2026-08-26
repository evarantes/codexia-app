import unittest

from scripts import apply_narration_cta_finish_hardening as hardening


class NarrationCtaFinishHardeningTests(unittest.TestCase):
    def test_narration_guard_rejects_code_and_strips_safe_markdown(self):
        source = '''import re\n\n_STRUCTURAL_PATTERNS = (\n    ("code_fence", re.compile(r"```|~~~")),\n)\n\ndef sanitize_narration_text(text):\n    raw = str(text)\n    raw = _SAFE_SSML_CONTAINER_TAG.sub(" ", raw)\n    return raw\n'''
        patched = hardening.patch_narration_guard(source)
        self.assertIn('"inline_code"', patched)
        self.assertIn('"source_code_assignment"', patched)
        self.assertIn('"source_code_declaration"', patched)
        self.assertIn('"source_code_arrow"', patched)
        self.assertIn('"sql_statement"', patched)
        self.assertIn('^\\s{0,3}#{1,6}\\s+', patched)
        self.assertEqual(hardening.patch_narration_guard(patched), patched)

    def test_cta_endcard_is_visible_for_four_to_six_seconds(self):
        source = '''            end_screen_target_duration_sec = float(_end_screen_configured if _end_screen_configured is not None else 1.2)\n            end_clip_duration = min(1.6, max(0.8, round(end_screen_target_duration_sec, 2)))\n            cta_ok = (0.8 <= float(end_clip_duration or 0.0) <= 1.6) if closing_has_narration else True\n'''
        patched = hardening.patch_video(source)
        self.assertIn('else 5.0)', patched)
        self.assertIn('min(6.0, max(4.0', patched)
        self.assertIn('4.0 <= float(end_clip_duration or 0.0) <= 6.0', patched)
        self.assertEqual(hardening.patch_video(patched), patched)

    def test_retry_error_expansion_cannot_generate_if_const_syntax_error(self):
        source = '''async function retry() {\n    if (!res.ok) const retryDetail = data && data.detail;\n        const retryDetailMessage = (retryDetail && typeof retryDetail === 'object')\n            ? retryDetail.message\n            : retryDetail;\n        throw new Error(retryDetailMessage || data.message || 'Falha ao recolocar a tarefa na fila.');\n}\n'''
        patched = hardening.patch_index(source)
        self.assertNotIn('if (!res.ok) const retryDetail', patched)
        self.assertIn('if (!res.ok) { const retryDetail', patched)
        self.assertIn("Falha ao recolocar a tarefa na fila.'); }", patched)
        self.assertEqual(hardening.patch_index(patched), patched)

    def test_plan_error_expansion_is_braced_too(self):
        source = '''async function plan() {\n    if (!planRes.ok) const planDetail = planData && planData.detail;\n        const planDetailMessage = (planDetail && typeof planDetail === 'object')\n            ? planDetail.message\n            : planDetail;\n        throw new Error(planDetailMessage || planData.message || 'Falha ao analisar alternativas de recuperação.');\n}\n'''
        patched = hardening.patch_index(source)
        self.assertNotIn('if (!planRes.ok) const planDetail', patched)
        self.assertIn('if (!planRes.ok) { const planDetail', patched)
        self.assertIn("Falha ao analisar alternativas de recuperação.'); }", patched)


if __name__ == '__main__':
    unittest.main()
