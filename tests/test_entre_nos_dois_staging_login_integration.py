"""Exercise the isolated staging app's actual login, forced password change,
and protected Studio route using a mock database. No external requests.
"""
from __future__ import annotations

import importlib
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class FakeQuery:
    def __init__(self, user):
        self.user = user
    def filter(self, *args, **kwargs):
        return self
    def first(self):
        return self.user


class FakeDB:
    def __init__(self, user):
        self.user = user
        self.commits = 0
    def query(self, _):
        return FakeQuery(self.user)
    def add(self, _):
        pass
    def commit(self):
        self.commits += 1


class StagingAuthIntegrationTests(unittest.TestCase):
    def test_first_login_requires_password_change_before_studio(self):
        with patch.dict(os.environ, {"APP_ENV": "staging"}):
            staging = importlib.import_module("app.studio_staging")
        from app.database import get_db
        from app.models import User
        from app.routers.auth import get_password_hash
        initial = "initial-staging-passphrase-123"
        next_password = "fresh-staging-passphrase-456"
        user = User(
            id=42, email="staging-admin@example.test", is_admin=True,
            is_active=True, role="admin", must_change_password=True,
            hashed_password=get_password_hash(initial),
        )
        db = FakeDB(user)
        staging.app.dependency_overrides[get_db] = lambda: db
        self.addCleanup(staging.app.dependency_overrides.pop, get_db, None)
        with TestClient(staging.app) as client:
            login = client.post("/token", data={"username": user.email, "password": initial})
            self.assertEqual(login.status_code, 200)
            self.assertTrue(login.json()["must_change_password"])
            token = login.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            blocked = client.get("/studio/entre-nos-dois/overview", headers=headers)
            self.assertEqual(blocked.status_code, 403)
            changed = client.post(
                "/staging/change-password", headers=headers,
                json={"current_password": initial, "new_password": next_password},
            )
            self.assertEqual(changed.status_code, 200)
            self.assertFalse(user.must_change_password)
            self.assertEqual(db.commits, 1)
            available = client.get("/studio/entre-nos-dois/overview", headers=headers)
            self.assertEqual(available.status_code, 200)
            self.assertFalse(available.json()["auto_publish"])
            self.assertEqual(client.post(
                "/token", data={"username": user.email, "password": initial}
            ).status_code, 401)
            self.assertEqual(client.post(
                "/token", data={"username": user.email, "password": next_password}
            ).status_code, 200)


if __name__ == "__main__":
    unittest.main()
