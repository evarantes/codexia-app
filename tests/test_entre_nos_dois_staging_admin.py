import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.entre_nos_dois_staging_admin import (
    StagingBootstrapError, bootstrap_admin, ensure_staging_environment,
)


class StagingFirstAdminTests(unittest.TestCase):
    def test_production_environment_is_blocked(self):
        with patch.dict(os.environ, {
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql://user:pass@studio-db:5432/codexia_staging",
        }):
            with self.assertRaises(StagingBootstrapError):
                ensure_staging_environment()

    def test_unverified_database_is_blocked(self):
        with patch.dict(os.environ, {
            "APP_ENV": "staging",
            "DATABASE_URL": "postgresql://user:pass@production-db:5432/codexia",
        }):
            with self.assertRaises(StagingBootstrapError):
                ensure_staging_environment()

    def test_deceptive_database_url_is_blocked(self):
        with patch.dict(os.environ, {
            "APP_ENV": "staging",
            "DATABASE_URL": "postgresql://studio-db@production-db:5432/codexia_staging",
        }):
            with self.assertRaises(StagingBootstrapError):
                ensure_staging_environment()

    def test_password_is_validated_before_db_access(self):
        with patch.dict(os.environ, {
            "APP_ENV": "staging",
            "DATABASE_URL": "postgresql://user:pass@studio-db:5432/codexia_staging",
        }):
            with self.assertRaises(StagingBootstrapError):
                bootstrap_admin("admin@example.com", "short")

    def test_existing_accounts_prevent_bootstrap(self):
        class FakeQuery:
            def first(self):
                return object()

        class FakeSession:
            rolled_back = False
            closed = False

            def query(self, model):
                return FakeQuery()

            def rollback(self):
                self.rolled_back = True

            def close(self):
                self.closed = True

        db = FakeSession()
        with patch.dict(os.environ, {
            "APP_ENV": "staging",
            "DATABASE_URL": "postgresql://user:pass@studio-db:5432/codexia_staging",
        }), patch.dict(sys.modules, {"app.models": SimpleNamespace(User=object())}):
            with self.assertRaises(StagingBootstrapError):
                bootstrap_admin("admin@example.com", "example-secure-passphrase", session_factory=lambda: db)
        self.assertTrue(db.rolled_back)
        self.assertTrue(db.closed)


if __name__ == "__main__":
    unittest.main()
