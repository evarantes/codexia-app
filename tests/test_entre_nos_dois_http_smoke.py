import unittest
from unittest.mock import patch

from scripts.entre_nos_dois_smoke import run


class StudioSmokeTests(unittest.TestCase):
    def test_unauthenticated_guard_and_admin_readiness(self):
        responses = {
            "/studio/entre-nos-dois/painel": (200, "<html>Studio</html>"),
            "/studio/entre-nos-dois/overview": (200, '{"program_title":"Entre Nós Dois","auto_publish":false}'),
            "/studio/entre-nos-dois/readiness": (200, '{"paid_execution_enabled":false,"auto_publish":false}'),
            "/studio/entre-nos-dois/references": (200, '{"hosts":{"Gabriel":false,"Helena":false}}'),
            "/studio/entre-nos-dois/references/Helena/image": (401, "{}"),
        }

        def fake_fetch(base, path, token=None):
            if path != "/studio/entre-nos-dois/painel" and not token:
                return 401, "{}"
            return responses[path]

        with patch("scripts.entre_nos_dois_smoke.fetch", side_effect=fake_fetch) as fetch:
            result = run("http://localhost:8000", admin_token="test-only")
        self.assertTrue(result["all_passed"])
        self.assertEqual(fetch.call_count, 8)
        self.assertFalse(result["paid_calls_made"])

    def test_exposed_private_endpoint_is_fatal(self):
        with patch("scripts.entre_nos_dois_smoke.fetch", return_value=(200, "{}")):
            result = run("http://localhost:8000")
        self.assertFalse(result["all_passed"])

    def test_no_credentials_in_url(self):
        with self.assertRaises(ValueError):
            run("https://user:secret@example.com")
 

if __name__ == "__main__":
    unittest.main()
