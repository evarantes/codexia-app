"""Teste estrutural isolado; imports completos são cobertos pelo CI geral."""
import ast
import unittest
from pathlib import Path


class StudioOverviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path("app/routers/entre_nos_dois.py").read_text(encoding="utf-8")
        cls.module = ast.parse(source)
        cls.source = source

    def test_overview_is_read_only_and_admin_guarded(self):
        handlers = [n for n in self.module.body if isinstance(n, ast.FunctionDef) and n.name == "overview"]
        self.assertEqual(len(handlers), 1)
        decorators = handlers[0].decorator_list
        self.assertEqual(len(decorators), 1)
        self.assertIsInstance(decorators[0], ast.Call)
        self.assertIsInstance(decorators[0].func, ast.Attribute)
        self.assertEqual(decorators[0].func.attr, "get")
        self.assertIn('get_current_admin_user', self.source)
        self.assertIn('Depends(get_current_admin_user)', self.source)
        self.assertNotIn('@router.post', self.source)
        self.assertNotIn('@router.delete', self.source)

    def test_voice_check_admin_only_and_no_tts_action(self):
        voice_check = next(n for n in self.module.body if isinstance(n, ast.FunctionDef) and n.name == "verify_voices")
        source = ast.get_source_segment(self.source, voice_check)
        self.assertIn('Depends(get_current_admin_user)', source)
        self.assertIn('run_voice_check(', source)
        self.assertNotIn('generate_pilot_audio(', source)
        self.assertNotIn('submit_first_sync3_scene(', source)
        self.assertNotIn('api_key=', source)

    def test_overview_disallows_live_generation_and_secrets(self):
        self.assertIn('"auto_publish": False', self.source)
        self.assertIn('"production_ready": False', self.source)
        self.assertIn('"audio_generated": False', self.source)
        self.assertIn('"lip_sync_generated": False', self.source)
        overview = next(n for n in self.module.body if isinstance(n, ast.FunctionDef) and n.name == "overview")
        overview_source = ast.get_source_segment(self.source, overview)
        self.assertNotIn('api_key', overview_source)
        self.assertNotIn('provider_voice_id":', overview_source)
        readiness = next(n for n in self.module.body if isinstance(n, ast.FunctionDef) and n.name == "readiness")
        self.assertIn('Depends(get_current_admin_user)', ast.get_source_segment(self.source, readiness))


if __name__ == "__main__":
    unittest.main()
