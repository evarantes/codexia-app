import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import ScheduledVideo, UnifiedVideo, UnifiedVideoStatus, VideoTask
from app.routers.youtube import _sync_ready_unified_to_scheduled


class UnifiedReadyQueueSyncTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codexia-ready-sync-")
        self.engine = create_engine(
            f"sqlite:///{Path(self._tmp.name) / 'ready.sqlite3'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)

    def tearDown(self):
        self.engine.dispose()
        self._tmp.cleanup()

    def _create_unified(self, *, task_id: str, status: str) -> int:
        db = self.Session()
        try:
            db.add(VideoTask(id=task_id, status=status, progress=100))
            unified = UnifiedVideo(
                idempotency_key=f"idem-{task_id}",
                task_id=task_id,
                source_module="youtube_story",
                source_id=f"story:{task_id}",
                content_type="devotional",
                topic="Tema de fallback",
                status=status,
                progress=100,
                script_json=json.dumps(
                    {
                        "title": "Deus vê e cuida de você",
                        "description": "Descrição pronta para revisão.",
                    },
                    ensure_ascii=False,
                ),
                result_json=json.dumps(
                    {
                        "kind": "devotional",
                        "video_type": "video",
                        "video_url": "/media/videos/video-pronto.mp4",
                    },
                    ensure_ascii=False,
                ),
                video_path="/data/media/videos/video-pronto.mp4",
                video_url="/media/videos/video-pronto.mp4",
            )
            db.add(unified)
            db.commit()
            return int(unified.id)
        finally:
            db.close()

    def test_awaiting_review_is_backfilled_once_into_ready_queue(self):
        unified_id = self._create_unified(
            task_id="task-awaiting-review",
            status=UnifiedVideoStatus.AWAITING_REVIEW,
        )

        db = self.Session()
        try:
            _sync_ready_unified_to_scheduled(db)
            _sync_ready_unified_to_scheduled(db)

            rows = db.query(ScheduledVideo).filter(
                ScheduledVideo.unified_video_id == unified_id
            ).all()
            self.assertEqual(len(rows), 1, "a sincronização deve ser idempotente")
            scheduled = rows[0]
            self.assertEqual(scheduled.status, "completed")
            self.assertEqual(scheduled.progress, 100)
            self.assertEqual(scheduled.task_id, "task-awaiting-review")
            self.assertEqual(scheduled.pipeline, "unified_video_pipeline")
            self.assertEqual(scheduled.title, "Deus vê e cuida de você")
            self.assertEqual(scheduled.video_url, "/media/videos/video-pronto.mp4")
            self.assertEqual(scheduled.video_path, "/data/media/videos/video-pronto.mp4")
            payload = json.loads(scheduled.script_data)
            self.assertEqual(payload["source"], "unified_video_pipeline")
            self.assertEqual(payload["unified_video_id"], unified_id)
        finally:
            db.close()

    def test_failed_unified_video_never_enters_ready_queue(self):
        unified_id = self._create_unified(
            task_id="task-failed",
            status=UnifiedVideoStatus.FAILED,
        )
        db = self.Session()
        try:
            _sync_ready_unified_to_scheduled(db)
            count = db.query(ScheduledVideo).filter(
                ScheduledVideo.unified_video_id == unified_id
            ).count()
            self.assertEqual(count, 0)
        finally:
            db.close()

    def test_published_state_upgrades_existing_mirror_without_duplicate(self):
        unified_id = self._create_unified(
            task_id="task-published",
            status=UnifiedVideoStatus.AWAITING_REVIEW,
        )
        db = self.Session()
        try:
            _sync_ready_unified_to_scheduled(db)
            unified = db.query(UnifiedVideo).filter(UnifiedVideo.id == unified_id).one()
            unified.status = UnifiedVideoStatus.PUBLISHED
            unified.youtube_video_id = "youtube-123"
            unified.youtube_url = "https://www.youtube.com/watch?v=youtube-123"
            db.commit()

            _sync_ready_unified_to_scheduled(db)
            rows = db.query(ScheduledVideo).filter(
                ScheduledVideo.unified_video_id == unified_id
            ).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, "published")
            self.assertEqual(rows[0].youtube_video_id, "youtube-123")
        finally:
            db.close()

    def test_frontend_treats_awaiting_review_as_terminal_and_sends_identity(self):
        html = (Path(__file__).parents[1] / "app" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        poll_story = html.split("async pollStoryTask(taskId)", 1)[1].split(
            "async generateStoryShorts", 1
        )[0]
        self.assertIn("['completed', 'awaiting_review', 'approved']", poll_story)
        self.assertIn("readyStatuses.includes(status)", poll_story)
        self.assertIn("this.queueGeneratedStoryVideo({ noScroll: true })", poll_story)

        queue_story = html.split("async queueGeneratedStoryVideo(options)", 1)[1].split(
            "async publishGeneratedStoryVideoNow", 1
        )[0]
        self.assertIn("task_id: this.ytStoryTaskId || null", queue_story)
        self.assertIn("unified_video_id:", queue_story)
        self.assertIn("video_path: r.file_path || r.video_path || null", queue_story)
        self.assertIn("s === 'awaiting_review'", html)


if __name__ == "__main__":
    unittest.main()
