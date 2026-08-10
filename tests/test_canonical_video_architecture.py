from __future__ import annotations

import ast
import importlib.util
import os
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("ENABLE_SQLITE_DEV", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
BASELINE_MIGRATION = ROOT / "alembic" / "versions" / "000000000001_canonical_schema_baseline.py"
FIRST_LEGACY_MIGRATION = ROOT / "alembic" / "versions" / "1145add67fa0_add_music_file_to_content_plan.py"
MERGE_MIGRATION = ROOT / "alembic" / "versions" / "b1c2d3e4f5a6_merge_story_pipeline_schema_heads.py"


class CanonicalVideoArchitectureTests(unittest.TestCase):
    def test_alembic_has_explicit_non_destructive_baseline(self):
        baseline_spec = importlib.util.spec_from_file_location("canonical_baseline", BASELINE_MIGRATION)
        baseline = importlib.util.module_from_spec(baseline_spec)
        baseline_spec.loader.exec_module(baseline)  # type: ignore[union-attr]
        first_spec = importlib.util.spec_from_file_location("first_legacy_migration", FIRST_LEGACY_MIGRATION)
        first = importlib.util.module_from_spec(first_spec)
        first_spec.loader.exec_module(first)  # type: ignore[union-attr]

        self.assertEqual(baseline.revision, "000000000001")
        self.assertIsNone(baseline.down_revision)
        self.assertEqual(first.down_revision, baseline.revision)

    def test_alembic_has_one_merge_head_based_on_story_pipeline(self):
        spec = importlib.util.spec_from_file_location("canonical_merge", MERGE_MIGRATION)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]

        self.assertEqual(module.revision, "b1c2d3e4f5a6")
        self.assertEqual(
            tuple(module.down_revision),
            ("a9b2c4d6e8f0", "f8a7b2c4d6e0"),
        )

    def test_merge_migration_is_idempotent_and_repairs_pipeline_links(self):
        spec = importlib.util.spec_from_file_location("canonical_merge_runtime", MERGE_MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]

        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE unified_videos (id INTEGER PRIMARY KEY)"))
            connection.execute(
                sa.text(
                    "CREATE TABLE scheduled_videos ("
                    "id INTEGER PRIMARY KEY, task_id VARCHAR(64), unified_video_id INTEGER)"
                )
            )
            connection.execute(sa.text("CREATE TABLE videos (id INTEGER PRIMARY KEY)"))
            connection.execute(sa.text("CREATE TABLE codexia_humor_projects (id INTEGER PRIMARY KEY)"))
            context = MigrationContext.configure(connection)
            module.op = Operations(context)
            module.upgrade()
            module.upgrade()

            inspector = sa.inspect(connection)
            unified_columns = {column["name"] for column in inspector.get_columns("unified_videos")}
            video_columns = {column["name"] for column in inspector.get_columns("videos")}
            humor_columns = {column["name"] for column in inspector.get_columns("codexia_humor_projects")}
            scheduled_indexes = {index["name"] for index in inspector.get_indexes("scheduled_videos")}

        self.assertTrue(
            {"force_reuse_assets", "force_render_only", "review_feedback_json", "render_logs_json"}
            <= unified_columns
        )
        self.assertTrue({"task_id", "unified_video_id", "pipeline"} <= video_columns)
        self.assertTrue({"task_id", "unified_video_id", "pipeline"} <= humor_columns)
        self.assertTrue(
            {"ix_scheduled_videos_task_id", "ix_scheduled_videos_unified_video_id"}
            <= scheduled_indexes
        )

    def test_only_story_executor_can_import_video_generator(self):
        violations = []
        allowed = {APP / "routers" / "youtube.py"}
        for path in sorted(APP.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "app.services.video_generator":
                    if path not in allowed:
                        violations.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            violations,
            [],
            "VideoGenerator só pode ser importado pelo executor História/Devocional: "
            + ", ".join(violations),
        )

    def test_legacy_pipeline_fallback_is_globally_disabled(self):
        from app.config import legacy_pipeline_fallback_allowed

        for module in ["story", "scheduled", "video_factory", "music_clip", "humor_factory"]:
            self.assertFalse(legacy_pipeline_fallback_allowed(module_name=module))

    def test_all_modules_share_the_same_request_builder(self):
        from app.services.unified_video_pipeline import build_unified_video_request

        payload = {
            "topic": "Teste canônico",
            "duration": 4,
            "aspect_ratio": "9:16",
            "music_file_path": "/tmp/audio.mp3",
            "voice_style": "human",
        }
        first = build_unified_video_request(
            payload,
            source_module="scheduled",
            source_id="scheduled:42",
            user_id=7,
        )
        second = build_unified_video_request(
            payload,
            source_module="scheduled",
            source_id="scheduled:42",
            user_id=7,
        )
        self.assertEqual(first.model_dump(), second.model_dump())
        self.assertEqual(first.source_module, "scheduled")
        self.assertEqual(first.source_id, "scheduled:42")
        self.assertEqual(first.aspect_ratio, "9:16")
        self.assertTrue(first.music_enabled)
        self.assertGreaterEqual(len(first.idempotency_key), 8)

    def test_scheduled_queue_delegates_to_unified_pipeline(self):
        source = (APP / "services" / "video_processing.py").read_text(encoding="utf-8")
        self.assertIn("build_unified_video_request", source)
        self.assertIn("unified_video_pipeline().submit_or_reuse", source)
        self.assertNotIn("video_generator import VideoGenerator", source)


if __name__ == "__main__":
    unittest.main()
