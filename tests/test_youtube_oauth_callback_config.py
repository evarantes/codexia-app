import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


class YouTubeOAuthCallbackConfigTests(unittest.TestCase):
    def test_docker_startup_derives_public_callback_from_base_url(self):
        env = os.environ.copy()
        env.pop("YOUTUBE_OAUTH_REDIRECT_URI", None)
        env["BASE_URL"] = "https://codexia.example/"
        script = (
            'if [ -z "${YOUTUBE_OAUTH_REDIRECT_URI:-}" ] && [ -n "${BASE_URL:-}" ]; '
            'then export YOUTUBE_OAUTH_REDIRECT_URI="${BASE_URL%/}/youtube/auth/callback"; fi; '
            'printf "%s" "$YOUTUBE_OAUTH_REDIRECT_URI"'
        )
        result = subprocess.run(
            ["sh", "-c", script],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout,
            "https://codexia.example/youtube/auth/callback",
        )

    def test_docker_startup_preserves_explicit_redirect_override(self):
        env = os.environ.copy()
        env["BASE_URL"] = "https://codexia.example"
        env["YOUTUBE_OAUTH_REDIRECT_URI"] = "https://override.example/oauth/callback"
        script = (
            'if [ -z "${YOUTUBE_OAUTH_REDIRECT_URI:-}" ] && [ -n "${BASE_URL:-}" ]; '
            'then export YOUTUBE_OAUTH_REDIRECT_URI="${BASE_URL%/}/youtube/auth/callback"; fi; '
            'printf "%s" "$YOUTUBE_OAUTH_REDIRECT_URI"'
        )
        result = subprocess.run(
            ["sh", "-c", script],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout, "https://override.example/oauth/callback")

    def test_dockerfile_contains_oauth_startup_guard(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn('[ -z "${YOUTUBE_OAUTH_REDIRECT_URI:-}" ]', dockerfile)
        self.assertIn('[ -n "${BASE_URL:-}" ]', dockerfile)
        self.assertIn('${BASE_URL%/}/youtube/auth/callback', dockerfile)

    def test_service_uses_explicit_runtime_redirect(self):
        from app.services.youtube_service import default_oauth_redirect_uri

        expected = "https://codexia.example/youtube/auth/callback"
        with patch.dict(os.environ, {"YOUTUBE_OAUTH_REDIRECT_URI": expected}, clear=False):
            self.assertEqual(default_oauth_redirect_uri(), expected)


if __name__ == "__main__":
    unittest.main()
