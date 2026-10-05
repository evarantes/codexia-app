"""PostgreSQL regression for VideoTask -> UnifiedVideo status mirroring.

Runs only against the isolated PostgreSQL database used by GitHub Actions.
"""

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import text

from app.database import SessionLocal, engine
from app.models import UnifiedVideo, VideoTask
import app.services.task_manager as task_manager
import app.services.financial_guardian_service as financial_guardian_module
from app.services.financial_guardian import FinancialContext
from app.services.financial_guardian_service import FinancialGuardianService


_IS_ISOLATED_POSTGRES_CI = (
    engine.dialect.name == "postgresql"
    and engine.url.database == "codexia_ci"
)


@unittest.skipUnless(
    _IS_ISOLATED_POSTGRES_CI,
    "requires the isolated PostgreSQL CI database",
)
class VideoTaskStatusSyncPostgresTests(unittest.TestCase):
    def test_pending_task_sync_keeps_transaction_usable(self):
        db = SessionLocal()
        task_id = f"status-sync-{uuid.uuid4()}"
        idempotency_key = f"status-sync:{uuid.uuid4()}"
        task = VideoTask(
            id=task_id,
            status="pending",
            progress=41,
            message="Status sync regression",
        )
        unified = UnifiedVideo(
            idempotency_key=idempotency_key,
            task_id=task_id,
            source_module="status_sync_test",
            source_id=task_id,
            status="approved",
            progress=99,
            last_error="old error",
        )
        try:
            # Create the task first to satisfy unified_videos.task_id's FK.
            db.add(task)
            db.flush()
            db.add(unified)
            db.flush()

            # Now touch the task so the after_flush hook updates the existing
            # audit row. Expiring forces a PostgreSQL read after the raw SQL.
            task.progress = 42
            db.flush()
            db.expire(unified)
            self.assertEqual(unified.status, "queued")
            self.assertEqual(unified.progress, 42)
            self.assertEqual(unified.last_message, "Status sync regression")
            self.assertIsNone(unified.last_error)
        finally:
            db.rollback()
            db.close()

    def test_processing_status_persists_when_completed_at_is_null(self):
        task_id = f"dedupe-sync-{uuid.uuid4()}"
        idempotency_key = f"dedupe-sync:{uuid.uuid4()}"
        setup_db = SessionLocal()
        try:
            task = VideoTask(
                id=task_id,
                status="pending",
                progress=0,
                message="Queued",
            )
            setup_db.add(task)
            setup_db.flush()
            task_manager._upsert_dedupe_row(
                setup_db,
                idempotency_key=idempotency_key,
                request_hash=str(uuid.uuid4()),
                task_id=task_id,
                status="pending",
                payload={"test": "postgres-null-completed-at"},
            )
            setup_db.commit()
        finally:
            setup_db.rollback()
            setup_db.close()

        try:
            updated = task_manager.update_task(
                task_id,
                status="processing",
                progress=17,
                message="Renderizando vídeo",
            )
            self.assertIsInstance(updated, dict)
            self.assertEqual(updated["status"], "processing")
            self.assertEqual(updated["progress"], 17)

            verify_db = SessionLocal()
            try:
                dedupe = verify_db.execute(
                    text(
                        "SELECT status, completed_at FROM video_task_dedupe "
                        "WHERE task_id = :task_id"
                    ),
                    {"task_id": task_id},
                ).mappings().one()
                self.assertEqual(dedupe["status"], "processing")
                self.assertIsNone(dedupe["completed_at"])
            finally:
                verify_db.close()

            completed = task_manager.update_task(
                task_id,
                status="completed",
                progress=100,
                message="Concluído",
            )
            self.assertIsInstance(completed, dict)
            self.assertEqual(completed["status"], "completed")

            verify_db = SessionLocal()
            try:
                dedupe = verify_db.execute(
                    text(
                        "SELECT status, completed_at FROM video_task_dedupe "
                        "WHERE task_id = :task_id"
                    ),
                    {"task_id": task_id},
                ).mappings().one()
                self.assertEqual(dedupe["status"], "completed")
                self.assertIsNotNone(dedupe["completed_at"])
            finally:
                verify_db.close()
        finally:
            cleanup_db = SessionLocal()
            try:
                cleanup_db.execute(
                    text("DELETE FROM video_task_dedupe WHERE task_id = :task_id"),
                    {"task_id": task_id},
                )
                cleanup_db.execute(
                    text("DELETE FROM video_tasks WHERE id = :task_id"),
                    {"task_id": task_id},
                )
                cleanup_db.commit()
            finally:
                cleanup_db.rollback()
                cleanup_db.close()


@unittest.skipUnless(
    _IS_ISOLATED_POSTGRES_CI,
    "requires the isolated PostgreSQL CI database",
)
class FinancialGuardianCachePostgresTests(unittest.TestCase):
    def test_image_cache_insert_reuses_string_binds_with_explicit_postgres_types(self):
        context_id = f"cache-scope-type:{uuid.uuid4()}"
        with tempfile.TemporaryDirectory(prefix="guardian-postgres-cache-") as tmp:
            image_path = Path(tmp) / "scene.png"
            image_path.write_bytes(b"image cache regression fixture")
            context = FinancialContext(
                source_type="youtube_auto",
                context_id=context_id,
                user_id=987654321,
                metadata={},
            )
            db = SessionLocal()
            try:
                with patch.object(
                    financial_guardian_module,
                    "_MANIFEST_DIR",
                    Path(tmp) / "manifest",
                ), patch.object(financial_guardian_module, "_schema_ready", False):
                    result = FinancialGuardianService().cache_images_from_context_result(
                        db,
                        context=context,
                        plan={
                            "scenes": [
                                {
                                    "image_prompt": "A quiet shoreline at dawn",
                                    "text": "The day begins beside the sea.",
                                }
                            ]
                        },
                        image_paths=[str(image_path)],
                    )
                    self.assertEqual(result["db_stored_assets"], 1)
                    self.assertFalse(result["persistence_degraded"])
                    db.commit()

                rows = db.execute(
                    text(
                        "SELECT scope_key, asset_kind FROM codexia_asset_generation_cache "
                        "WHERE context_id = :context_id"
                    ),
                    {"context_id": context_id},
                ).mappings().all()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["scope_key"], "user:987654321")
                self.assertEqual(rows[0]["asset_kind"], "image")
            finally:
                db.rollback()
                db.close()

                cleanup_db = SessionLocal()
                try:
                    cleanup_db.execute(
                        text(
                            "DELETE FROM codexia_asset_generation_cache "
                            "WHERE context_id = :context_id"
                        ),
                        {"context_id": context_id},
                    )
                    cleanup_db.execute(
                        text(
                            "DELETE FROM codexia_financial_audit_events "
                            "WHERE context_id = :context_id"
                        ),
                        {"context_id": context_id},
                    )
                    cleanup_db.commit()
                finally:
                    cleanup_db.rollback()
                    cleanup_db.close()


if __name__ == "__main__":
    unittest.main()
