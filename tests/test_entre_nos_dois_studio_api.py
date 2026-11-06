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

    def test_overview_disallows_live_generation_and_secrets(self):
        self.assertIn('"auto_publish": False', self.source)
        self.assertIn('"production_ready": False', self.source)
        self.assertIn('"audio_generated": False', self.source)
        self.assertIn('"lip_sync_generated": False', self.source)
        self.assertNotIn('api_key', self.source)
        self.assertNotIn('provider_voice_id":', self.source)


if __name__ == "__main__":
    unittest.main()
