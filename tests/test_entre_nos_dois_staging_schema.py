import os
import unittest
from unittest.mock import patch

from scripts.entre_nos_dois_staging_schema import ensure_isolated_database


class StagingSchemaGuardTests(unittest.TestCase):
    def test_staging_only(self):
        with patch.dict(os.environ, {
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql://u:p@studio-db:5432/codexia_staging",
        }):
            with self.assertRaises(RuntimeError):
                ensure_isolated_database()

    def test_rejects_production_and_spoofed_hosts(self):
        for url in (
            "postgresql://u:p@prod-db:5432/codexia_staging",
            "postgresql://studio-db@prod-db:5432/codexia_staging",
            "postgresql://u:p@studio-db:5432/codexia_prod",
            "sqlite:///codexia_staging",
        ):
            with self.subTest(url=url), patch.dict(os.environ, {
                "APP_ENV": "staging", "DATABASE_URL": url,
            }):
                with self.assertRaises(RuntimeError):
                    ensure_isolated_database()

    def test_accepts_only_exact_staging_db(self):
        with patch.dict(os.environ, {
            "APP_ENV": "staging",
            "DATABASE_URL": "postgresql://u:p@studio-db:5432/codexia_staging",
        }):
            ensure_isolated_database()


if __name__ == "__main__":
    unittest.main()
