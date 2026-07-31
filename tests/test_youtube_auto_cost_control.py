import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/codexia_test")

from app.database import Base  # noqa: E402
from app.models import Tenant, User, VideoTask  # noqa: E402
import app.services.task_manager as task_manager  # noqa: E402
from app.services.task_manager import claim_video_task  # noqa: E402
from app.services.youtube_auto_identity import (  # noqa: E402
    build_video_content_fingerprint,
    sanitize_narrated_title,
)


class YouTubeAutoCostControlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="youtube-auto-cost-")
        self.db_path = os.path.join(self.temp_dir, "youtube-auto.sqlite")
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        Base.metadata.create_all(self.engine)
        self._create_support_tables()

        self.original_session_local = task_manager.SessionLocal
        task_manager.SessionLocal = self.Session
        task_manager._task_schema_ready = False
        task_manager.video_tasks.clear()

        db = self.Session()
        tenant = Tenant(name="Tenant Teste", slug="tenant-youtube-auto")
        db.add(tenant)
        db.flush()
        user = User(
            tenant_id=tenant.id,
            email="youtube-auto@codexia.test",
            name="YouTube Auto",
            hashed_password="hash",
            is_active=True,
            is_admin=True,
            role="admin",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        self.user_id = user.id
        db.close()

    def tearDown(self):
        task_manager.SessionLocal = self.original_session_local
        task_manager.video_tasks.clear()
        self.engine.dispose()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_support_tables(self):
        with self.engine.begin() as conn:
            conn.execute(text(
                """
                CREATE TABLE video_task_dedupe (
                    idempotency_key VARCHAR(255) PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    task_id VARCHAR(255) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    request_payload_json TEXT NULL,
                    result_json TEXT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    expires_at DATETIME NULL,
                    completed_at DATETIME NULL
                )
                """
            ))
            conn.execute(text(
                """
                CREATE TABLE video_task_leases (
                    task_id VARCHAR(255) PRIMARY KEY,
                    executor_id VARCHAR(255) NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    started_at DATETIME NOT NULL,
                    heartbeat_at DATETIME NOT NULL,
                    expires_at DATETIME NULL,
                    lease_expires_at DATETIME NULL
                )
                """
            ))
            conn.execute(text(
                """
                CREATE TABLE video_task_locks (
                    lock_key VARCHAR(255) PRIMARY KEY,
                    owner_id VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    expires_at DATETIME NOT NULL
                )
                """
            ))

    def _payload(self, **extra):
        base = {
            "topic": "Quando a Esperança Parece Perdida (Reflexão)",
            "duration": 5,
            "auto_upload": False,
            "mode": "topic",
            "kind": "story",
            "voice_style": "human",
            "voice_gender": "female",
            "aspect_ratio": "16:9",
            "image_mode": "multiple",
        }
        base.update(extra)
        fingerprint = build_video_content_fingerprint(base)
        base["content_fingerprint"] = fingerprint["content_fingerprint"]
        base["internal_title"] = fingerprint["internal_title"]
        base["youtube_title"] = fingerprint["youtube_title"]
        base["narrated_title"] = fingerprint["narrated_title"]
        return base

    def test_content_fingerprint_ignores_non_content_variations_and_strips_title_markers(self):
        payload_a = self._payload(auto_upload=False)
        payload_b = self._payload(
            auto_upload=True,
            override_title="Quando a Esperança Parece Perdida (Mensagem)",
        )

        fingerprint_a = build_video_content_fingerprint(payload_a)
        fingerprint_b = build_video_content_fingerprint(payload_b)

        self.assertEqual(fingerprint_a["content_fingerprint"], fingerprint_b["content_fingerprint"])
        self.assertEqual(fingerprint_a["narrated_title"], "Quando a Esperança Parece Perdida")
        self.assertEqual(sanitize_narrated_title("Salmo 91 (Devocional)"), "Salmo 91")

    def test_claim_video_task_reuses_equivalent_content_fingerprint(self):
        payload = self._payload()
        base_result = {
            "payload": payload,
            "kind": "youtube_story_video",
        }

        first = claim_video_task(
            idempotency_key="ytv1:first-request-hash",
            request_hash="first-request-hash",
            payload=payload,
            dedupe_window_seconds=21600,
            user_id=self.user_id,
            initial_result=base_result,
        )

        reused_task_ids = set()
        for idx in range(2, 6):
            repeated = claim_video_task(
                idempotency_key=f"ytv1:variant-{idx}",
                request_hash=f"variant-{idx}",
                payload=self._payload(auto_upload=bool(idx % 2)),
                dedupe_window_seconds=21600,
                user_id=self.user_id,
                initial_result=base_result,
            )
            self.assertFalse(repeated["created_new_task"])
            self.assertTrue(repeated["reused_existing_task"])
            self.assertTrue(repeated["duplicate_prevented"])
            self.assertEqual(repeated["matched_by"], "content_fingerprint")
            reused_task_ids.add(repeated["task_id"])

        self.assertEqual(reused_task_ids, {first["task_id"]})

        db = self.Session()
        try:
            self.assertEqual(db.query(VideoTask).count(), 1)
            dedupe_rows = db.execute(text("SELECT COUNT(*) FROM video_task_dedupe")).scalar()
            self.assertEqual(int(dedupe_rows or 0), 5)
            row = db.query(VideoTask).first()
            payload_saved = json.loads(row.result_json)
            self.assertEqual(
                payload_saved["payload"]["narrated_title"],
                "Quando a Esperança Parece Perdida",
            )
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
