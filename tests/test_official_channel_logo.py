import asyncio
import os
import tempfile
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from PIL import Image

from app.routers import settings as settings_router
from app.services.global_settings_service import GlobalSettingsService


class _Upload:
    def __init__(self, content: bytes, *, content_type: str = "image/png", filename: str = "logo.png"):
        self._content = content
        self.content_type = content_type
        self.filename = filename

    async def read(self, size: int = -1) -> bytes:
        return self._content if size < 0 else self._content[:size]


class _Db:
    def __init__(self):
        self.commits = 0

    def add(self, _value):
        return None

    def commit(self):
        self.commits += 1

    def refresh(self, _value):
        return None


def _png_bytes(size=(480, 240), alpha=True) -> bytes:
    mode = "RGBA" if alpha else "RGB"
    color = (20, 30, 50, 0) if alpha else (20, 30, 50)
    image = Image.new(mode, size, color)
    output = BytesIO()
    image.save(output, format="PNG")
    image.close()
    return output.getvalue()


class OfficialChannelLogoTests(unittest.TestCase):
    def test_upload_normalizes_logo_persists_path_and_can_remove_it(self):
        with tempfile.TemporaryDirectory(prefix="official-logo-") as tmp:
            settings = SimpleNamespace(official_channel_logo_path=None, official_channel_logo_url=None)
            db = _Db()
            current_user = SimpleNamespace(id=7)
            with (
                patch.object(settings_router, "BRANDING_OUTPUT_DIR", tmp),
                patch.object(settings_router, "_get_or_create_settings_row", return_value=settings),
            ):
                result = asyncio.run(
                    settings_router.upload_official_channel_logo(
                        file=_Upload(_png_bytes()),
                        db=db,
                        current_user=current_user,
                    )
                )

                self.assertTrue(result["success"])
                self.assertEqual(result["width"], 480)
                self.assertEqual(result["height"], 240)
                self.assertEqual(settings.official_channel_logo_path, result["official_channel_logo_path"])
                self.assertTrue(os.path.isfile(settings.official_channel_logo_path))
                self.assertEqual(
                    os.path.commonpath([settings.official_channel_logo_path, tmp]),
                    os.path.abspath(tmp),
                )
                with Image.open(settings.official_channel_logo_path) as saved:
                    self.assertEqual(saved.format, "PNG")
                    self.assertEqual(saved.mode, "RGBA")

                removed = settings_router.delete_official_channel_logo(db=db, current_user=current_user)
                self.assertTrue(removed["success"])
                self.assertTrue(removed["removed"])
                self.assertIsNone(settings.official_channel_logo_path)
                self.assertGreaterEqual(db.commits, 2)

    def test_upload_rejects_invalid_type_and_oversized_payload(self):
        settings = SimpleNamespace(official_channel_logo_path=None, official_channel_logo_url=None)
        with patch.object(settings_router, "_get_or_create_settings_row", return_value=settings):
            with self.assertRaises(HTTPException) as invalid_type:
                asyncio.run(
                    settings_router.upload_official_channel_logo(
                        file=_Upload(b"not-an-image", content_type="text/plain"),
                        db=_Db(),
                        current_user=SimpleNamespace(id=1),
                    )
                )
            self.assertEqual(invalid_type.exception.status_code, 400)

            oversized = b"x" * (settings_router.OFFICIAL_LOGO_MAX_UPLOAD_BYTES + 1)
            with self.assertRaises(HTTPException) as too_large:
                asyncio.run(
                    settings_router.upload_official_channel_logo(
                        file=_Upload(oversized),
                        db=_Db(),
                        current_user=SimpleNamespace(id=1),
                    )
                )
            self.assertEqual(too_large.exception.status_code, 413)

    def test_global_resolver_prefers_existing_local_logo_then_url_fallback(self):
        with tempfile.TemporaryDirectory(prefix="official-logo-resolver-") as tmp:
            logo_path = os.path.join(tmp, "logo.png")
            with open(logo_path, "wb") as handle:
                handle.write(_png_bytes())
            settings = SimpleNamespace(
                official_channel_logo_path=logo_path,
                official_channel_logo_url="https://example.test/logo.png",
            )
            service = GlobalSettingsService(settings=settings)

            local = service.resolve_official_channel_logo()
            self.assertEqual(local["selected_value"], logo_path)
            self.assertEqual(local["selected_source"], "settings_path")
            self.assertTrue(local["path_exists"])

            os.unlink(logo_path)
            fallback = service.resolve_official_channel_logo()
            self.assertEqual(fallback["selected_value"], "https://example.test/logo.png")
            self.assertEqual(fallback["selected_source"], "settings_url")
            self.assertFalse(fallback["path_exists"])

    def test_settings_ui_has_computer_picker_preview_and_no_editable_local_path(self):
        index_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "index.html")
        with open(index_path, "r", encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("Buscar logo no computador", source)
        self.assertIn('accept="image/png,image/jpeg,image/webp"', source)
        self.assertIn("uploadOfficialChannelLogo", source)
        self.assertIn("loadOfficialChannelLogoPreview", source)
        self.assertNotIn('v-model="settings.official_channel_logo_path"', source)


if __name__ == "__main__":
    unittest.main()
