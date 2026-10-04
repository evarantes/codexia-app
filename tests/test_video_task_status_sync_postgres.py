"""PostgreSQL regression for VideoTask -> UnifiedVideo status mirroring.

Runs only against the isolated PostgreSQL database used by GitHub Actions.
"""

import unittest
import uuid

from app.database import SessionLocal, engine
from app.models import UnifiedVideo, VideoTask


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
            db.add_all([task, unified])
            db.flush()

            # The after_flush hook runs raw SQL. Expiring forces a PostgreSQL
            # read after that statement and proves the transaction is healthy.
            db.expire(unified)
            self.assertEqual(unified.status, "queued")
            self.assertEqual(unified.progress, 41)
            self.assertEqual(unified.last_message, "Status sync regression")
            self.assertIsNone(unified.last_error)
        finally:
            db.rollback()
            db.close()


if __name__ == "__main__":
    unittest.main()
