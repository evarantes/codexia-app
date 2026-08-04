import os
import shutil
import tempfile
import unittest
import hashlib
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import Session as _SASession

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/codexia_test")

from app.database import Base
from app.models import Settings
from app.services import youtube_service as yt_module


class _FakeRequest:
    pass


class _FakeCredentials:
    def __init__(self, *, client_id: str, client_secret: str, refresh_token: str, valid: bool, expired: bool):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.valid = valid
        self.expired = expired

    def refresh(self, _request):
        if str(self.refresh_token or "").strip() == "old-invalid":
            raise Exception("invalid_grant: Token has been expired or revoked.")
        self.valid = True
        self.expired = False

    @classmethod
    def from_authorized_user_info(cls, info, scopes=None):
        return cls(
            client_id=str(info.get("client_id") or ""),
            client_secret=str(info.get("client_secret") or ""),
            refresh_token=str(info.get("refresh_token") or ""),
            valid=False,
            expired=True,
        )

    @classmethod
    def from_authorized_user_file(cls, filename, scopes):
        return cls(
            client_id="file-client",
            client_secret="file-secret",
            refresh_token="file-refresh",
            valid=True,
            expired=False,
        )


class _FakeFlow:
    def __init__(self, credentials):
        self.credentials = credentials
        self.redirect_uri = None
        self.code_verifier = "verifier"

    def authorization_url(self, **_kwargs):
        return "http://example/auth", None

    def fetch_token(self, code):
        return None


class YouTubeOAuthFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="yt-oauth-"))
        self.db_path = self.temp_dir / "oauth.sqlite"
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        Base.metadata.create_all(self.engine)

        self.db = self.Session()
        settings = Settings(
            youtube_client_id="db-client",
            youtube_client_secret="db-secret",
            youtube_refresh_token="",
        )
        self.db.add(settings)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _session_local(self):
        return self.Session()

    def test_exchange_persists_refresh_token_and_new_instance_authenticates(self):
        self.db.query(Settings).update({Settings.youtube_refresh_token: "old-valid"})
        self.db.commit()
        old_token = self.db.query(Settings).first().youtube_refresh_token
        old_fp = hashlib.sha256(old_token.encode("utf-8")).hexdigest()

        flow = _FakeFlow(_FakeCredentials(
            client_id="db-client",
            client_secret="db-secret",
            refresh_token="new-refresh",
            valid=True,
            expired=False,
        ))

        with patch.object(yt_module, "SessionLocal", self.Session), \
             patch.object(yt_module, "Request", _FakeRequest), \
             patch.object(yt_module, "build", return_value=object()), \
             patch.object(yt_module, "Credentials", _FakeCredentials), \
             patch.object(yt_module.InstalledAppFlow, "from_client_config", return_value=flow):
            svc = yt_module.YouTubeService()
            ok, msg = svc.exchange_code_for_token("code")

        self.assertTrue(ok, msg)
        refreshed = self.db.query(Settings).first()
        self.assertEqual(refreshed.youtube_refresh_token, "new-refresh")
        new_fp = hashlib.sha256(refreshed.youtube_refresh_token.encode("utf-8")).hexdigest()
        self.assertNotEqual(old_fp, new_fp)

        with patch.object(yt_module, "SessionLocal", self.Session), \
             patch.object(yt_module, "Request", _FakeRequest), \
             patch.object(yt_module, "build", return_value=object()), \
             patch.object(yt_module, "Credentials", _FakeCredentials):
            verify = yt_module.YouTubeService()
            self.assertIsNotNone(verify.service)
            self.assertIsNone(verify.auth_error)
            self.assertEqual(verify.auth_source, "database")

    def test_exchange_fails_when_google_returns_no_refresh_token(self):
        flow = _FakeFlow(_FakeCredentials(
            client_id="db-client",
            client_secret="db-secret",
            refresh_token="",
            valid=True,
            expired=False,
        ))

        self.db.query(Settings).update({Settings.youtube_refresh_token: "old-invalid"})
        self.db.commit()

        with patch.object(yt_module, "SessionLocal", self.Session), \
             patch.object(yt_module, "Request", _FakeRequest), \
             patch.object(yt_module, "build", return_value=object()), \
             patch.object(yt_module, "Credentials", _FakeCredentials), \
             patch.object(yt_module.InstalledAppFlow, "from_client_config", return_value=flow):
            svc = yt_module.YouTubeService()
            ok, msg = svc.exchange_code_for_token("code")

        self.assertFalse(ok)
        self.assertIn("não forneceu", msg.lower())
        refreshed = self.db.query(Settings).first()
        self.assertEqual(refreshed.youtube_refresh_token, "old-invalid")

    def test_exchange_fails_when_db_commit_raises_and_rolls_back(self):
        flow = _FakeFlow(_FakeCredentials(
            client_id="db-client",
            client_secret="db-secret",
            refresh_token="new-refresh",
            valid=True,
            expired=False,
        ))

        self.db.query(Settings).update({Settings.youtube_refresh_token: "old-valid"})
        self.db.commit()

        calls = {"rollback": 0}
        original_rollback = _SASession.rollback

        def commit_fail(self):
            raise Exception("commit failed")

        def rollback_spy(self):
            calls["rollback"] += 1
            return original_rollback(self)

        with patch.object(yt_module, "SessionLocal", self.Session), \
             patch.object(yt_module, "Request", _FakeRequest), \
             patch.object(yt_module, "build", return_value=object()), \
             patch.object(yt_module, "Credentials", _FakeCredentials), \
             patch.object(yt_module.InstalledAppFlow, "from_client_config", return_value=flow), \
             patch.object(_SASession, "commit", commit_fail), \
             patch.object(_SASession, "rollback", rollback_spy):
            svc = yt_module.YouTubeService()
            ok, msg = svc.exchange_code_for_token("code")

        self.assertFalse(ok)
        self.assertIn("falha ao salvar", msg.lower())
        self.assertGreaterEqual(calls["rollback"], 1)

        refreshed = self.db.query(Settings).first()
        self.assertEqual(refreshed.youtube_refresh_token, "old-valid")

    def test_load_credentials_invalid_grant_marks_auth_error(self):
        self.db.query(Settings).update({Settings.youtube_refresh_token: "old-invalid"})
        self.db.commit()

        with patch.object(yt_module, "SessionLocal", self.Session), \
             patch.object(yt_module, "Request", _FakeRequest), \
             patch.object(yt_module, "build", return_value=object()), \
             patch.object(yt_module, "Credentials", _FakeCredentials):
            svc = yt_module.YouTubeService()
            self.assertIsNone(svc.service)
            self.assertTrue("invalid_grant" in str(svc.auth_error or ""))

    def test_load_credentials_deduplicates_equivalent_candidates(self):
        self.db.query(Settings).update({Settings.youtube_refresh_token: "new-refresh"})
        self.db.commit()

        call_counter = {"count": 0}
        original_from_info = _FakeCredentials.from_authorized_user_info

        def wrapped_from_info(info, scopes=None):
            call_counter["count"] += 1
            return original_from_info(info, scopes=scopes)

        with patch.object(yt_module, "SessionLocal", self.Session), \
             patch.object(yt_module, "Request", _FakeRequest), \
             patch.object(yt_module, "build", return_value=object()), \
             patch.object(yt_module, "Credentials", _FakeCredentials), \
             patch.object(_FakeCredentials, "from_authorized_user_info", side_effect=wrapped_from_info):
            svc = yt_module.YouTubeService()
            self.assertIsNotNone(svc.service)

        self.assertEqual(call_counter["count"], 2)


if __name__ == "__main__":
    unittest.main()
