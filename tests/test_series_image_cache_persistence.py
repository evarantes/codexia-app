import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Configurar o banco local antes de importar os serviços da aplicação.
_TEST_DB_ROOT = tempfile.mkdtemp(prefix="series-image-cache-db-")
os.environ["APP_ENV"] = "development"
os.environ["ENABLE_SQLITE_DEV"] = "true"
os.environ["SQLITE_DB_PATH"] = os.path.join(_TEST_DB_ROOT, "test.sqlite3")
os.environ.pop("DATABASE_URL", None)

from app.services.ai_generator import AIContentGenerator
from app.services.ai_router import AIRouter


class _FakeImageRouter:
    def __init__(self):
        self.kwargs = None

    def generate_image(self, **kwargs):
        self.kwargs = dict(kwargs)
        output_dir = Path(str(kwargs["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        result = output_dir / "series-image.png"
        result.write_bytes(b"x" * 1500)
        return str(result)


class SeriesImageCachePersistenceTests(unittest.TestCase):
    def _generator(self):
        generator = AIContentGenerator()
        generator.ai_router = _FakeImageRouter()
        generator._load_config = lambda: None
        generator._paid_ai_disabled = lambda: False
        generator.api_key = "sk-test"
        return generator

    def test_series_uses_persistent_directory_and_can_reclaim_missing_cache(self):
        with tempfile.TemporaryDirectory(prefix="series-images-") as tmp:
            generator = self._generator()
            generator.set_operation_context(
                user_id=1,
                task_id="series-task",
                source_module="youtube_series",
            )
            with patch("app.services.ai_generator.IMAGES_OUTPUT_DIR", tmp):
                result = generator.generate_image("Cinematic sunrise")

            self.assertTrue(os.path.isfile(result))
            self.assertTrue(os.path.abspath(result).startswith(os.path.abspath(tmp)))
            self.assertTrue(generator.ai_router.kwargs["reclaim_missing_completed_file"])

    def test_story_keeps_existing_image_output_contract(self):
        generator = self._generator()
        generator.set_operation_context(
            user_id=1,
            task_id="story-task",
            source_module="story",
        )
        result = generator.generate_image("Cinematic sunrise")

        self.assertEqual(result, "/generated_assets/openai_images/series-image.png")
        self.assertEqual(
            generator.ai_router.kwargs["output_dir"],
            "generated_assets/openai_images",
        )
        self.assertFalse(generator.ai_router.kwargs["reclaim_missing_completed_file"])

    def test_youtube_auto_uses_persistent_directory_and_reclaims_stale_cache(self):
        with tempfile.TemporaryDirectory(prefix="youtube-auto-images-") as tmp:
            generator = self._generator()
            generator.set_operation_context(
                user_id=1,
                task_id="youtube-auto-task",
                source_module="youtube_auto",
            )
            with patch("app.services.ai_generator.IMAGES_OUTPUT_DIR", tmp):
                result = generator.generate_image("Cinematic biblical scene")

            self.assertTrue(os.path.isfile(result))
            self.assertTrue(os.path.abspath(result).startswith(os.path.abspath(tmp)))
            self.assertEqual(generator.ai_router.kwargs["output_dir"], tmp)
            self.assertTrue(generator.ai_router.kwargs["reclaim_missing_completed_file"])

    def test_completed_image_cache_requires_physical_file(self):
        with tempfile.TemporaryDirectory(prefix="series-cache-") as tmp:
            valid = Path(tmp) / "valid.png"
            valid.write_bytes(b"x" * 1500)
            missing = Path(tmp) / "missing.png"
            too_small = Path(tmp) / "small.png"
            too_small.write_bytes(b"x" * 100)

            self.assertTrue(AIRouter._completed_image_result_is_usable({"path": str(valid)}))
            self.assertFalse(AIRouter._completed_image_result_is_usable({"path": str(missing)}))
            self.assertFalse(AIRouter._completed_image_result_is_usable({"path": str(too_small)}))
            self.assertFalse(AIRouter._completed_image_result_is_usable({}))


if __name__ == "__main__":
    unittest.main()
