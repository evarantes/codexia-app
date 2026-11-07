"""In-process FastAPI integration tests for the actual Studio routes.

Uses a disposable directory and dependency overrides; never hits a provider,
a real database, a paid endpoint, or a live Codexia instance.
"""
import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from fastapi.responses import Response

from app.routers import entre_nos_dois
from app.routers.auth import get_current_admin_user


class EntreNosDoisHTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        env = patch.dict(os.environ, {"CODEXIA_STUDIO_PRIVATE_DIR": self.dir.name})
        env.start()
        self.addCleanup(env.stop)
        self.app = FastAPI()
        self.app.include_router(entre_nos_dois.router)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        pic = io.BytesIO()
        Image.new("RGB", (48, 48), (120, 80, 60)).save(pic, format="JPEG")
        self.valid_image = pic.getvalue()
        self.drive_assets = {}
        def store_asset(*, user_id, kind, filename, body, metadata=None):
            self.drive_assets[(user_id, kind, filename)] = {
                "kind": kind, "filename": filename, "body": body, "sha256": "test", "size": len(body),
                **(metadata or {}),
            }
        def find_asset(*, user_id, kind, filename):
            entry = self.drive_assets.get((user_id, kind, filename))
            return ({key: value for key, value in entry.items() if key != "body"} | {"test_body": entry["body"]}) if entry else None
        self.store_mock = patch("app.services.entre_nos_dois_references.store_asset_bytes", side_effect=store_asset)
        self.find_refs_mock = patch("app.services.entre_nos_dois_references.find_asset", side_effect=find_asset)
        self.find_router_mock = patch("app.routers.entre_nos_dois.find_asset", side_effect=find_asset)
        self.stream_mock = patch(
            "app.routers.entre_nos_dois.stream_asset",
            side_effect=lambda entry, **kwargs: Response(
                content=entry["test_body"], media_type=kwargs["media_type"],
                headers={"Cache-Control": "private, no-store"},
            ),
        )
        for mocked in (self.store_mock, self.find_refs_mock, self.find_router_mock, self.stream_mock):
            mocked.start()
            self.addCleanup(mocked.stop)

    def login_as(self, user_id):
        self.app.dependency_overrides[get_current_admin_user] = lambda: SimpleNamespace(id=user_id)
        self.addCleanup(self.app.dependency_overrides.clear)

    def test_anonymous_cannot_access_any_private_images_or_data(self):
        base = "/studio/entre-nos-dois"
        for path in ("/overview", "/readiness", "/references", "/references/Helena/image"):
            self.assertIn(self.client.get(base + path).status_code, (401, 403))
        self.assertIn(
            self.client.put(base + "/references/Helena", content=self.valid_image).status_code,
            (401, 403),
        )
        self.assertIn(self.client.post(base + "/verify-voices").status_code, (401, 403))

    def test_admin_upload_preview_and_user_separation(self):
        self.login_as(42)
        base = "/studio/entre-nos-dois"
        before = self.client.get(base + "/references")
        self.assertEqual(before.status_code, 200)
        self.assertIs(before.json()["hosts"]["Helena"], False)
        response = self.client.put(
            base + "/references/Helena",
            content=self.valid_image,
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["private"], True)
        self.assertNotIn("path", response.json())
        status = self.client.get(base + "/references")
        self.assertIs(status.json()["hosts"]["Helena"], True)
        preview = self.client.get(base + "/references/Helena/image")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.headers["content-type"], "image/png")
        self.assertIn("no-store", preview.headers["cache-control"])
        with Image.open(io.BytesIO(preview.content)) as image:
            self.assertEqual(image.size, (48, 48))
        self.app.dependency_overrides[get_current_admin_user] = lambda: SimpleNamespace(id=43)
        self.assertIs(self.client.get(base + "/references").json()["hosts"]["Helena"], False)
        self.assertEqual(self.client.get(base + "/references/Helena/image").status_code, 404)

    def test_reject_invalid_and_oversized_image_without_save(self):
        self.login_as(42)
        url = "/studio/entre-nos-dois/references/Gabriel"
        self.assertEqual(self.client.put(url, content=b"not-an-image").status_code, 400)
        self.assertEqual(
            self.client.put(url, content=b"x" * (8 * 1024 * 1024 + 1)).status_code, 413
        )
        self.assertIs(self.client.get("/studio/entre-nos-dois/references").json()["hosts"]["Gabriel"], False)

    def test_overview_remains_non_billable(self):
        self.login_as(42)
        response = self.client.get("/studio/entre-nos-dois/overview")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["auto_publish"])
        self.assertFalse(response.json()["production_ready"])
        self.assertEqual(len(response.json()["dialogue"]), 4)


if __name__ == "__main__":
    unittest.main()
