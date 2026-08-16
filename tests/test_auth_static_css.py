import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"


class AuthStaticCssTests(unittest.TestCase):
    def test_login_uses_local_auth_css_without_tailwind_cdn(self):
        html = (STATIC / "login.html").read_text(encoding="utf-8")
        self.assertIn('href="/static/auth.css"', html)
        self.assertNotIn("cdn.tailwindcss.com", html)

    def test_reset_password_uses_local_auth_css_without_tailwind_cdn(self):
        html = (STATIC / "reset-password.html").read_text(encoding="utf-8")
        self.assertIn('href="/static/auth.css"', html)
        self.assertNotIn("cdn.tailwindcss.com", html)

    def test_local_auth_css_contains_layout_and_form_fallbacks(self):
        css = (STATIC / "auth.css").read_text(encoding="utf-8")
        for required in (
            ".min-h-screen",
            ".max-w-md",
            ".bg-indigo-700",
            ".bg-indigo-600",
            ".rounded-lg",
            ".shadow-xl",
            ".w-full",
            ".focus\\:ring-2:focus",
        ):
            self.assertIn(required, css)


if __name__ == "__main__":
    unittest.main()
