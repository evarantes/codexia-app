"""Static safety gates for the opt-in standalone Studio staging stack."""
import unittest
from pathlib import Path


class StudioStagingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = Path("docker-compose.entre-nos-dois-staging.yml").read_text(encoding="utf-8")

    def test_isolated_resources_and_local_only_listener(self):
        for required in (
            "name: codexia-entre-nos-dois-staging",
            "127.0.0.1:18080:8000",
            "studio_private:/data/studio-private",
            "studio_db_data:/var/lib/postgresql/data",
            "CODEXIA_STUDIO_PRIVATE_DIR: /data/studio-private",
            "APP_ENV: staging",
        ):
            self.assertIn(required, self.compose)

    def test_rejects_default_passwords_and_live_provider_keys(self):
        self.assertIn("CODEXIA_STAGING_DB_PASSWORD:?", self.compose)
        self.assertIn("CODEXIA_STAGING_SECRET_KEY:?", self.compose)
        self.assertNotIn("YOUTUBE_REFRESH_TOKEN:", self.compose)
        self.assertNotIn("FAL_KEY:", self.compose)
        self.assertNotIn("ELEVENLABS_API_KEY:", self.compose)
        self.assertNotIn("8000:8000", self.compose.replace("127.0.0.1:18080:8000", ""))

    def test_no_prod_db_or_volume_names(self):
        self.assertNotIn("postgres_data:", self.compose)
        self.assertNotIn("codexia_media:", self.compose)
        self.assertNotIn("container_name:", self.compose)


if __name__ == "__main__":
    unittest.main()
