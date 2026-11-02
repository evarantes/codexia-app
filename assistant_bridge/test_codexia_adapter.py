"""Contract tests for existing Codexia music/clip API payload translation.

Run: pytest assistant_bridge/test_codexia_adapter.py
No external services or paid calls.
"""
import pytest
from pydantic import ValidationError
from assistant_bridge.codexia_adapter import music_payload, clip_payload, eligible_assets
from assistant_bridge.router import ProductionRequest


def test_generate_music_contract():
    brief = ProductionRequest(title="Exemplo", lyrics="Letra original", style="Piseiro",
                              voice="male", aspect_ratio="16:9").model_dump()
    assert music_payload(brief) == {
        "title": "Exemplo", "lyrics": "Letra original", "genre": "Piseiro",
        "vocal_gender": "m",
    }


def test_clip_never_auto_publishes():
    brief = ProductionRequest(title="Exemplo", lyrics="Letra original", style="Piseiro",
                              aspect_ratio="9:16").model_dump()
    result = clip_payload(brief, "song_123.mp3")
    assert result["auto_upload_youtube"] is False
    assert result["aspect_ratio"] == "9:16"
    assert result["captions_enabled"] is True


@pytest.mark.parametrize("name", ["", "../secret.mp3", "/tmp/a.mp3", "folder\\x.mp3"])
def test_clip_rejects_nonbasename(name):
    brief = ProductionRequest(title="Exemplo", lyrics="Letra", style="Piseiro").model_dump()
    with pytest.raises(ValueError):
        clip_payload(brief, name)


def test_asset_rights_filter():
    brief = {"assets": [
        {"url": "https://example.org/a.jpg", "rights_confirmed": True},
        {"url": "https://example.org/b.jpg", "rights_confirmed": False},
    ]}
    assert [x["url"] for x in eligible_assets(brief)] == ["https://example.org/a.jpg"]


def test_required_fields():
    with pytest.raises(ValidationError):
        ProductionRequest(title="", lyrics="", style="")
