import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/codexia_test")

from app.database import Base  # noqa: E402
from app.models import ScheduledVideo, SeriesEpisode, SeriesPlan, Tenant, User  # noqa: E402
from app.services.youtube_series_service import youtube_series_service  # noqa: E402


class YouTubeSeriesServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="yt-series-service-"))
        self.db_path = self.temp_dir / "series.sqlite"
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()

        tenant = Tenant(name="Tenant Series", slug="tenant-series")
        self.db.add(tenant)
        self.db.flush()
        self.user = User(
            tenant_id=tenant.id,
            email="series@codexia.test",
            name="Series Tester",
            hashed_password="hash",
            is_active=True,
            is_admin=True,
            role="admin",
        )
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_series(self, **overrides):
        payload = {
            "name": "Jornada da Fé",
            "main_theme": "Como manter a fé nos dias difíceis",
            "objective": "Conduzir o público em sete passos de fortalecimento espiritual.",
            "target_audience": "Adultos cristãos",
            "content_type": "reflection",
            "start_date": "2026-08-01",
            "end_date": "2026-08-05",
            "publication_time": "19:00",
            "timezone": "America/Sao_Paulo",
            "production_lead_days": 1,
            "production_time": "06:00",
            "duration_minutes": 10,
            "visibility": "unlisted",
            "tone": "acolhedor",
            "narration_style": "human",
            "continuity_level": "high",
            "hook_intensity": "medium",
            "use_biblical_references": True,
            "cta_subscribe": True,
            "cta_next_episode": True,
            "status": "draft",
        }
        payload.update(overrides)
        with patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}):
            return youtube_series_service.create_series(self.db, user=self.user, payload=payload)

    def test_series_of_five_days_calculates_dates_episode_count_and_lead_time(self):
        detail = self._create_series(status="active")

        self.assertEqual(detail["total_episodes"], 5)
        self.assertEqual(len(detail["episodes"]), 5)
        self.assertEqual(detail["episodes"][0]["episode_number"], 1)
        self.assertEqual(detail["episodes"][-1]["episode_number"], 5)

        first_pub = datetime.fromisoformat(detail["episodes"][0]["publication_datetime"])
        first_prod = datetime.fromisoformat(detail["episodes"][0]["production_datetime"])
        self.assertEqual((first_pub.date() - first_prod.date()).days, 1)
        self.assertEqual(first_prod.hour, 9)  # 06:00 America/Sao_Paulo -> 09:00 UTC in August
        self.assertEqual(first_pub.hour, 22)  # 19:00 America/Sao_Paulo -> 22:00 UTC in August

    def test_multiple_series_remain_independent_lines(self):
        with patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}):
            self._create_series(name="Jornada da Fé", main_theme="Fé", start_date="2026-08-01", end_date="2026-08-03")
            self._create_series(name="Superando o Medo", main_theme="Medo", start_date="2026-08-05", end_date="2026-08-09")
            self._create_series(name="Orações da Manhã", main_theme="Oração", start_date="2026-08-01", end_date="2026-08-31")
            listing = youtube_series_service.list_series(self.db, user=self.user)

        self.assertEqual(listing["count"], 3)
        names = {item["name"] for item in listing["items"]}
        self.assertIn("Jornada da Fé", names)
        self.assertIn("Superando o Medo", names)
        self.assertIn("Orações da Manhã", names)

    def test_approval_creates_publish_queue_entry_without_regeneration(self):
        detail = self._create_series(status="active")
        episode_id = detail["episodes"][0]["id"]
        episode = self.db.query(SeriesEpisode).filter(SeriesEpisode.id == episode_id).first()
        self.assertIsNotNone(episode)
        episode.task_id = "task-approved"
        episode.status = "awaiting_review"
        self.db.commit()

        with patch("app.services.youtube_series_service.get_task", return_value={
            "status": "completed",
            "result": {
                "title": "Episódio aprovado",
                "description": "Descrição pronta",
                "video_url": "/media/videos/approved.mp4",
                "cost_control": {"estimated_cost": 1.2, "actual_cost": 0.8},
            },
        }), patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}):
            result = youtube_series_service.approve_episode(self.db, user=self.user, episode_id=episode_id)

        updated = next(ep for ep in result["episodes"] if ep["id"] == episode_id)
        queue_item = self.db.query(ScheduledVideo).filter(ScheduledVideo.id == updated["scheduled_video_id"]).first()

        self.assertEqual(updated["status"], "scheduled")
        self.assertIsNotNone(queue_item)
        self.assertTrue(bool(queue_item.auto_post))
        self.assertEqual(queue_item.status, "completed")
        self.assertEqual(queue_item.scheduled_for, self.db.query(SeriesEpisode).get(episode_id).publication_datetime)

    def test_rejection_by_image_creates_new_version_with_script_and_audio_reuse(self):
        audio_path = self.temp_dir / "audio.mp3"
        audio_path.write_text("audio", encoding="utf-8")
        detail = self._create_series(status="active")
        episode_id = detail["episodes"][0]["id"]
        episode = self.db.query(SeriesEpisode).filter(SeriesEpisode.id == episode_id).first()
        episode.task_id = "task-before-reject"
        episode.status = "awaiting_review"
        self.db.commit()

        fake_old_task = {
            "status": "completed",
            "result": {
                "title": "Versão 1",
                "video_url": "/media/videos/v1.mp4",
                "script": {
                    "selected_images": ["/media/img-1.png", "/media/img-2.png"],
                    "scenes": [{"text": "Cena 1"}],
                },
                "render_report": {
                    "audio_generation": {"output_path": str(audio_path)},
                    "official_audio_transcription": {"srt_path": "/media/sub.srt"},
                },
                "cost_control": {"estimated_cost": 1.1, "actual_cost": 0.7},
            },
        }

        with patch("app.services.youtube_series_service.get_task", side_effect=lambda task_id: fake_old_task if str(task_id) == "task-before-reject" else {"status": "pending", "result": {}}), \
             patch("app.services.youtube_series_service.claim_video_task", return_value={"task_id": "task-correction", "task": {"result": {}}}), \
             patch("app.services.youtube_series_service.update_task", return_value=None), \
             patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}):
            result = youtube_series_service.reject_episode(
                self.db,
                user=self.user,
                episode_id=episode_id,
                reasons=["image"],
                feedback="A imagem principal não representa a narração.",
            )

        updated = next(ep for ep in result["episodes"] if ep["id"] == episode_id)
        correction = updated["review"]
        plan = self.db.query(SeriesEpisode).filter(SeriesEpisode.id == episode_id).first()
        correction_plan = plan.correction_plan_json

        self.assertEqual(updated["current_version"], 2)
        self.assertEqual(updated["task_id"], "task-correction")
        self.assertIn(updated["status"], {"in_production", "awaiting_review"})
        self.assertEqual(correction["latest_decision"], "rejected")
        self.assertIn("script", correction_plan)
        self.assertIn("audio", correction_plan)
        self.assertIn("image", correction_plan)

    def test_scheduler_blocks_publication_when_no_approval_exists(self):
        detail = self._create_series(status="active")
        episode_id = detail["episodes"][0]["id"]
        series_id = detail["id"]
        episode = self.db.query(SeriesEpisode).filter(SeriesEpisode.id == episode_id).first()
        episode.status = "awaiting_review"
        episode.publication_datetime = datetime.utcnow() - timedelta(hours=2)
        self.db.commit()

        with patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}):
            summary = youtube_series_service.sync_series_scheduler(self.db, now=datetime.utcnow())
            result = youtube_series_service.get_series_detail(self.db, user=self.user, series_id=series_id)

        updated = next(ep for ep in result["episodes"] if ep["id"] == episode_id)
        self.assertGreaterEqual(summary["blocked"], 1)
        self.assertEqual(updated["status"], "publication_blocked")
        self.assertEqual(result["status"], "pending_issue")

    def test_scheduler_blocks_same_cycle_when_task_finishes_after_publication_deadline(self):
        detail = self._create_series(status="active")
        episode_id = detail["episodes"][0]["id"]
        series_id = detail["id"]
        episode = self.db.query(SeriesEpisode).filter(SeriesEpisode.id == episode_id).first()
        episode.status = "in_production"
        episode.task_id = "task-finished-late"
        episode.publication_datetime = datetime.utcnow() - timedelta(minutes=30)
        self.db.commit()

        with patch("app.services.youtube_series_service.get_task", return_value={"status": "completed", "result": {}}), \
             patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}):
            summary = youtube_series_service.sync_series_scheduler(self.db, now=datetime.utcnow())
            result = youtube_series_service.get_series_detail(self.db, user=self.user, series_id=series_id)

        updated = next(ep for ep in result["episodes"] if ep["id"] == episode_id)
        self.assertGreaterEqual(summary["synced"], 1)
        self.assertGreaterEqual(summary["blocked"], 1)
        self.assertEqual(updated["status"], "publication_blocked")
        self.assertEqual(result["status"], "pending_issue")

    def test_editorial_plan_preserves_continuity_and_last_episode_closure(self):
        detail = self._create_series(end_date="2026-08-03")
        episodes = detail["episodes"]

        self.assertTrue(episodes[1]["previous_episode_hook"])
        self.assertIn("próximo", episodes[0]["next_episode_hook"].lower())
        self.assertIn("concl", episodes[-1]["next_episode_hook"].lower())

    def test_scheduler_is_idempotent_for_due_episode(self):
        detail = self._create_series(status="active")
        series_id = detail["id"]
        episode = self.db.query(SeriesEpisode).filter(SeriesEpisode.series_id == series_id).order_by(SeriesEpisode.episode_number.asc()).first()
        episode.production_datetime = datetime.utcnow() - timedelta(minutes=5)
        episode.status = "awaiting_production"
        self.db.commit()

        call_counter = {"count": 0}

        def fake_enqueue(db, user, series, episode, correction_feedback=None, initial_result=None, force_regenerate=False):
            call_counter["count"] += 1
            episode.task_id = "task-due"
            episode.status = "in_production"
            return {"task_id": "task-due", "content_fingerprint": "fp-task-due"}

        with patch.object(youtube_series_service, "_enqueue_episode_generation", side_effect=fake_enqueue), \
             patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}):
            youtube_series_service.sync_series_scheduler(self.db, now=datetime.utcnow())
            youtube_series_service.sync_series_scheduler(self.db, now=datetime.utcnow())

        self.assertEqual(call_counter["count"], 1)


if __name__ == "__main__":
    unittest.main()
