"""Safety checks for the intentionally limited Entre Nós Dois staging ASGI app."""
import ast
import unittest
from pathlib import Path


class StudioOnlyStagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path("app/studio_staging.py").read_text(encoding="utf-8")
        cls.compose = Path("docker-compose.entre-nos-dois-staging.yml").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_does_not_import_main_or_start_workers(self):
        for text in (
            "app.main", "monitor_service", "run_migrations(", "create_admin_master",
            "youtube.router", "music.router", "StaticFiles(",
        ):
            self.assertNotIn(text, self.source)
        self.assertIn("app.include_router(entre_nos_dois.router)", self.source)

    def test_staging_only_and_no_documentation_endpoints(self):
        self.assertIn('!= "staging"', self.source)
        for term in ("docs_url=None", "redoc_url=None", "openapi_url=None"):
            self.assertIn(term, self.source)

    def test_compose_launches_only_studio_after_isolated_migrations(self):
        self.assertIn("uvicorn app.studio_staging:app", self.compose)
        self.assertIn("python scripts/apply_migrations.py &&", self.compose)
        self.assertNotIn("uvicorn app.main:app", self.compose)

    def test_login_is_only_for_active_admin_and_expires(self):
        self.assertIn("not user.is_active or not user.is_admin", self.source)
        self.assertIn("expires_delta=timedelta(hours=1)", self.source)
        self.assertIn("app.dependency_overrides[get_current_admin_user] = staging_admin", self.source)
        self.assertIn("if user.must_change_password:", self.source)
        self.assertIn("admin: User = Depends(get_current_user)", self.source)
        self.assertIn("admin.must_change_password = False", self.source)
        self.assertIn('@app.post("/token")', self.source)

    def test_public_surface_is_narrow(self):
        endpoints = [n for n in self.tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        route_paths = [
            a.args[0].value
            for n in endpoints for a in n.decorator_list
            if isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute)
            and a.func.attr in ("post", "get") and a.args and isinstance(a.args[0], ast.Constant)
        ]
        self.assertEqual(sorted(route_paths), ["/", "/health", "/staging/change-password", "/token"])


if __name__ == "__main__":
    unittest.main()
