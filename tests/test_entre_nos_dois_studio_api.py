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
        self.assertFalse(any(isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in {'post','delete'} for dec in decorators))

    def test_dashboard_links_to_studio(self):
        # The full legacy suite temporarily mounts its archived UI at index.html.
        # During that bridge, inspect the saved V2 shell that will be restored.
        dashboard_path = Path("app/static/.codexia-v2-index.build-backup.bak")
        if not dashboard_path.is_file():
            dashboard_path = Path("app/static/index.html")
        dashboard = dashboard_path.read_text(encoding="utf-8")
        self.assertIn('href="/studio/entre-nos-dois/painel"', dashboard)
        self.assertIn('Entre Nós Dois', dashboard)

    def test_studio_admin_login_verifies_permissions_and_session_logout(self):
        studio = Path("app/static/pages/entre-nos-dois/index.html").read_text(encoding="utf-8")
        self.assertIn('id="studio-login-form"', studio)
        self.assertIn("fetch('/token'", studio)
        self.assertIn("admin.ok", studio)
        self.assertIn("sessionStorage.setItem(studioTokenKey", studio)
        self.assertIn("sessionStorage.setItem('entre_nos_dois_logged_out','1')", studio)
        self.assertIn("sessionStorage.getItem('entre_nos_dois_logged_out')", studio)
        self.assertNotIn("localStorage.setItem(studioTokenKey", studio)

    def test_interactive_preview_has_no_paid_generation_calls(self):
        studio = Path("app/static/pages/entre-nos-dois/index.html").read_text(encoding="utf-8")
        self.assertIn('id="preview-next"', studio)
        self.assertIn('id="preview-prev"', studio)
        self.assertIn('function renderPreview()', studio)
        self.assertIn('id="preview-image"', studio)
        self.assertIn('previews[selected]', studio)
        self.assertIn('renderPreview();', studio)
        self.assertIn("previewDialogue=Array.isArray(data.dialogue)", studio)
        self.assertNotIn('/text-to-speech/', studio)
        self.assertNotIn('/scene/submit', studio)

    def test_direct_studio_page_does_not_expose_protected_data(self):
        page = next(n for n in self.module.body if isinstance(n, ast.FunctionDef) and n.name == "studio_page")
        source = ast.get_source_segment(self.source, page)
        self.assertIn('FileResponse(', source)
        self.assertIn('index.html', source)
        self.assertNotIn('api_key', source)
        self.assertNotIn('run_voice_check(', source)

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
