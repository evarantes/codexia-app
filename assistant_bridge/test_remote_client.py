"""Contract tests for the cross-installation client; never contact a live server."""
from unittest.mock import Mock, patch
import os
from assistant_bridge.remote_client import remote_music_capability

def test_missing_configuration_fails_closed():
    with patch.dict(os.environ, {"CODEXIA_MAIN_INTERNAL_URL": "", "CODEXIA_INTERNAL_SERVICE_TOKEN": ""}):
        assert remote_music_capability()["suno_available"] is False

def test_rejects_insecure_http_without_request():
    with patch.dict(os.environ, {"CODEXIA_MAIN_INTERNAL_URL": "http://example.com", "CODEXIA_INTERNAL_SERVICE_TOKEN": "x" * 40}):
        with patch("assistant_bridge.remote_client.requests.get") as get:
            assert remote_music_capability()["configured"] is False
            get.assert_not_called()

def test_authenticated_https_request_does_not_expose_key():
    response = Mock()
    response.json.return_value = {"suno_available": True, "provider": "suno"}
    with patch.dict(os.environ, {"CODEXIA_MAIN_INTERNAL_URL": "https://codexia.example", "CODEXIA_INTERNAL_SERVICE_TOKEN": "s" * 40}):
        with patch("assistant_bridge.remote_client.requests.get", return_value=response) as get:
            assert remote_music_capability() == {"configured": True, "suno_available": True}
            args, kwargs = get.call_args
            assert args[0] == "https://codexia.example/api/internal/codexia/music-capability"
            assert kwargs["allow_redirects"] is False
            assert kwargs["timeout"] == 8
            assert kwargs["headers"]["Authorization"] == "Bearer " + "s" * 40

def test_connection_failure_does_not_leak_token():
    import requests
    with patch.dict(os.environ, {"CODEXIA_MAIN_INTERNAL_URL": "https://codexia.example", "CODEXIA_INTERNAL_SERVICE_TOKEN": "s" * 40}):
        with patch("assistant_bridge.remote_client.requests.get", side_effect=requests.ConnectionError("secret")):
            result = remote_music_capability()
            assert result["suno_available"] is False
            assert "secret" not in str(result)
