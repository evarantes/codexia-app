import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch, call

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


if not os.environ.get("ENABLE_SQLITE_DEV"):
    os.environ["ENABLE_SQLITE_DEV"] = "true"
if not os.environ.get("APP_ENV"):
    os.environ["APP_ENV"] = "development"
# Override DATABASE_URL explicito postgres de setup? → SQLite dev explicit.
if os.environ.get("DATABASE_URL", "").startswith("postgresql://"):
    del os.environ["DATABASE_URL"]
from app.database import Base  # noqa: E402
from app.models import ScheduledVideo, SeriesEpisode, SeriesPlan, Tenant, User, UnifiedVideoStatus  # noqa: E402
from app.services.youtube_series_service import youtube_series_service  # noqa: E402


# ====== HELPERS â€” paridade de contrato HistÃ³ria/Devocional vs SÃ©ries ======

def _story_build_request_payload():
    """Payload equivalente ao mÃ³dulo HistÃ³ria/Devocional (Texto â†’ VÃ­deo)."""
    return {
        "source_module": "story",
        "source_id": "task:abc123",
        "idempotency_key": "story:topic:xyz:v1",
        "content_type": "devotional",
        "topic": "Como manter a fÃ© nos dias difÃ­ceis",
        "script_text": None,
        "duration_minutes": 10,
        "aspect_ratio": "16:9",
        "image_count": 8,
        "text_provider": "configured",
        "image_provider": "configured",
        "voice_provider": "configured",
        "voice_id": "human",
        "music_enabled": False,
        "visibility": "unlisted",
        "auto_publish": False,
        "review_required": True,
        "user_id": 1,
        "force_regenerate": False,
        "override_title": None,
        "override_description": None,
        "request_hash": "story-hash-xyz",
        "legacy_payload": {"mode": "topic"},
    }


def _series_expected_request_fields():
    """Campos que TODO UnifiedVideoRequest de sÃ©rie deve ter (mesma assinatura histÃ³ria)."""
    return {
        "source_module",
        "source_id",
        "idempotency_key",
        "content_type",
        "topic",
        "script_text",
        "duration_minutes",
        "aspect_ratio",
        "image_count",
        "text_provider",
        "image_provider",
        "voice_provider",
        "voice_id",
        "music_enabled",
        "visibility",
        "auto_publish",
        "review_required",
        "user_id",
        "force_regenerate",
        "override_title",
        "override_description",
        "request_hash",
        "seeded_script",
        "selected_images",
        "reuse_audio_from",
        "legacy_payload",
    }


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
            "name": "Jornada da FÃ©",
            "main_theme": "Como manter a fÃ© nos dias difÃ­ceis",
            "objective": "Conduzir o pÃºblico em sete passos de fortalecimento espiritual.",
            "target_audience": "Adultos cristÃ£os",
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
            self._create_series(name="Jornada da FÃ©", main_theme="FÃ©", start_date="2026-08-01", end_date="2026-08-03")
            self._create_series(name="Superando o Medo", main_theme="Medo", start_date="2026-08-05", end_date="2026-08-09")
            self._create_series(name="OraÃ§Ãµes da ManhÃ£", main_theme="OraÃ§Ã£o", start_date="2026-08-01", end_date="2026-08-31")
            listing = youtube_series_service.list_series(self.db, user=self.user)

        self.assertEqual(listing["count"], 3)
        names = {item["name"] for item in listing["items"]}
        self.assertIn("Jornada da FÃ©", names)
        self.assertIn("Superando o Medo", names)
        self.assertIn("OraÃ§Ãµes da ManhÃ£", names)

    def test_archiving_series_cancels_unpublished_episodes_and_removes_it_from_list(self):
        detail = self._create_series(status="active", end_date="2026-08-01")
        series_id = detail["id"]
        episode = self.db.query(SeriesEpisode).filter(SeriesEpisode.series_id == series_id).first()
        episode.status = "failed"
        episode.task_id = "task-series-failed"
        self.db.commit()

        with patch(
            "app.services.youtube_series_service.request_cancel_task",
            return_value={"status": "cancelled"},
        ) as cancel_task:
            archived = youtube_series_service.update_series_status(
                self.db,
                user=self.user,
                series_id=series_id,
                status="cancelled",
            )

        self.db.refresh(episode)
        series = self.db.query(SeriesPlan).filter(SeriesPlan.id == series_id).first()
        listing = youtube_series_service.list_series(self.db, user=self.user)
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["status"], "cancelled")
        self.assertEqual(episode.status, "cancelled")
        self.assertIsNotNone(series.archived_at)
        self.assertEqual(listing["count"], 0)
        cancel_task.assert_called_once_with(
            "task-series-failed",
            message="Cancelado pelo usuário ao arquivar a série.",
        )

    def test_server_shutdown_pauses_active_series_and_cancels_linked_episode(self):
        detail = self._create_series(status="active", end_date="2026-08-01")
        series_id = detail["id"]
        episode = self.db.query(SeriesEpisode).filter(SeriesEpisode.series_id == series_id).first()
        episode.status = "failed"
        episode.task_id = "task-server-stop"
        self.db.commit()

        result = youtube_series_service.pause_for_server_shutdown(
            self.db,
            cancelled_task_ids=["task-server-stop"],
        )

        self.db.refresh(episode)
        series = self.db.query(SeriesPlan).filter(SeriesPlan.id == series_id).first()
        self.assertEqual(series.status, "paused")
        self.assertEqual(episode.status, "cancelled")
        self.assertEqual(result["paused_series"], 1)
        self.assertEqual(result["cancelled_series_episodes"], 1)

    def test_approval_creates_publish_queue_entry_without_regeneration(self):
        detail = self._create_series(status="active")
        episode_id = detail["episodes"][0]["id"]
        episode = self.db.query(SeriesEpisode).filter(SeriesEpisode.id == episode_id).first()
        self.assertIsNotNone(episode)
        episode.task_id = "task-approved"
        episode.status = "awaiting_review"
        self.db.commit()

        fake_uv = MagicMock()
        fake_uv.id = 1
        fake_uv.task_id = "task-approved"
        fake_uv.idempotency_key = "yts:series:1:episode:1:v1"
        fake_uv.source_module = "youtube_series"
        fake_uv.source_id = f"episode:{int(episode_id)}"
        fake_uv.status = UnifiedVideoStatus.AWAITING_REVIEW
        fake_uv.video_url = "/media/videos/approved.mp4"
        fake_uv.video_path = None
        fake_uv.script_json = json.dumps({"title": "EpisÃ³dio aprovado", "description": "DescriÃ§Ã£o pronta"})
        fake_uv.title = "EpisÃ³dio aprovado"
        fake_uv.description = "DescriÃ§Ã£o pronta"
        fake_uv.youtube_video_id = None
        fake_uv.youtube_url = None
        fake_uv.estimated_cost = 1.2
        fake_uv.actual_cost = 0.8
        fake_uv.call_count_text = 1
        fake_uv.call_count_image = 8
        fake_uv.call_count_audio = 1
        fake_uv.result_json = None

        fake_pub_result = {
            "already_uploaded": False,
            "uploaded": False,
            "youtube_video_id": None,
            "youtube_url": None,
            "skipped_reason": "auto_publish=False",
        }

        def fake_transition(db, idempotency_key_or_task_id, *, status, step=None, progress=None, message=None, merge_result=None):
            fake_uv.status = status
            return fake_uv

        with patch("app.services.youtube_series_service.get_task", return_value={
            "status": "completed",
            "result": {
                "title": "EpisÃ³dio aprovado",
                "description": "DescriÃ§Ã£o pronta",
                "video_url": "/media/videos/approved.mp4",
                "cost_control": {"estimated_cost": 1.2, "actual_cost": 0.8},
            },
        }), patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}), \
            patch("app.services.youtube_series_service._require_unified_pipeline") as mock_require, \
            patch.object(youtube_series_service, "_find_unified_video_by_episode", return_value=fake_uv):

            uvpsvc = MagicMock()
            uvpsvc.transition_status = fake_transition
            uvpsvc.publish_if_ready = MagicMock(return_value=fake_pub_result)
            uvpsvc.ensure_schema = MagicMock()
            mock_require.return_value = uvpsvc

            result = youtube_series_service.approve_episode(self.db, user=self.user, episode_id=episode_id)

        updated = next(ep for ep in result["episodes"] if ep["id"] == episode_id)
        queue_item = self.db.query(ScheduledVideo).filter(ScheduledVideo.id == updated["scheduled_video_id"]).first()

        self.assertEqual(updated["status"], "approved")
        self.assertIsNotNone(queue_item)
        self.assertTrue(bool(queue_item.auto_post))
        self.assertEqual(queue_item.status, "completed")
        self.assertEqual(queue_item.scheduled_for, self.db.query(SeriesEpisode).get(episode_id).publication_datetime)

        with patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}), \
             patch.object(youtube_series_service, "_enqueue_episode_generation", return_value={"task_id": "noop", "content_fingerprint": "noop"}):
            youtube_series_service.sync_series_scheduler(self.db, now=datetime.utcnow())
            refreshed = youtube_series_service.get_series_detail(self.db, user=self.user, series_id=detail["id"])
        refreshed_ep = next(ep for ep in refreshed["episodes"] if ep["id"] == episode_id)
        self.assertEqual(refreshed_ep["status"], "scheduled")

    def test_rejection_by_image_creates_new_version_with_seeds_in_unified_video_request(self):
        """RejeiÃ§Ã£o NÃƒO usa pipeline legado: usa seeded_script / selected_images / reuse_audio_from no UReq."""
        audio_path = self.temp_dir / "audio.mp3"
        audio_path.write_text("audio", encoding="utf-8")
        image_1 = self.temp_dir / "img-1.png"
        image_2 = self.temp_dir / "img-2.png"
        image_1.write_bytes(b"image-1")
        image_2.write_bytes(b"image-2")
        detail = self._create_series(status="active")
        episode_id = detail["episodes"][0]["id"]
        episode = self.db.query(SeriesEpisode).filter(SeriesEpisode.id == episode_id).first()
        episode.task_id = "task-before-reject"
        episode.status = "awaiting_review"
        self.db.commit()

        uv_result_dict = {
            "script": {
                "selected_images": [str(image_1), str(image_2)],
                "scenes": [{"text": "Cena 1"}, {"text": "Cena 2"}],
            },
            "images": [str(image_1), str(image_2)],
            "audio_generation": {"output_path": str(audio_path)},
            "official_audio_transcription": {"srt_path": "/media/sub.srt"},
        }

        fake_uv = MagicMock()
        fake_uv.id = 2
        fake_uv.task_id = "task-before-reject"
        fake_uv.idempotency_key = "yts:series:1:episode:1:v1"
        fake_uv.source_module = "youtube_series"
        fake_uv.source_id = f"episode:{int(episode_id)}"
        fake_uv.status = UnifiedVideoStatus.AWAITING_REVIEW
        fake_uv.result_json = uv_result_dict
        fake_uv.script_json = json.dumps(uv_result_dict["script"])
        fake_uv.images_json = json.dumps(uv_result_dict["images"])
        fake_uv.audio_path = str(audio_path)

        captured_requests: Dict[str, Any] = {}

        def fake_submit_or_reuse(db, *, request, kick_queue_callback=None, legacy_initial_result=None, user=None):
            captured_requests["last_request"] = request
            captured_requests["legacy_initial_result"] = legacy_initial_result
            captured_requests["kick_callback_was_none_or_callable"] = (
                kick_queue_callback is None or callable(kick_queue_callback)
            )
            fake_result = MagicMock()
            fake_result.task_id = "task-correction-uvp"
            fake_result.message = "ok"
            fake_result.reused_existing = False
            fake_result.reused_completed = False
            fake_result.queue_position = 1
            fake_result.already_processing = False
            fake_result.idempotency_key = str(request.idempotency_key)
            fake_result.video_url = None
            fake_result.youtube_video_id = None
            fake_result.providers = {}
            fake_result.unified_video_id = 999
            return fake_result

        with patch("app.services.youtube_series_service.get_task", return_value={"status": "completed", "result": {}}), \
             patch("app.services.youtube_series_service.update_task", return_value=None), \
             patch("app.services.youtube_series_service._require_unified_pipeline") as mock_require, \
             patch.object(youtube_series_service, "_find_unified_video_by_episode", return_value=fake_uv), \
             patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}):

            uvpsvc = MagicMock()
            uvpsvc.submit_or_reuse = fake_submit_or_reuse
            uvpsvc.ensure_schema = MagicMock()
            mock_require.return_value = uvpsvc

            result = youtube_series_service.reject_episode(
                self.db,
                user=self.user,
                episode_id=episode_id,
                reasons=["image"],
                feedback="A imagem principal nÃ£o representa a narraÃ§Ã£o.",
            )

        updated = next(ep for ep in result["episodes"] if ep["id"] == episode_id)
        correction = updated["review"]
        plan = self.db.query(SeriesEpisode).filter(SeriesEpisode.id == episode_id).first()
        correction_plan = plan.correction_plan_json

        self.assertEqual(updated["current_version"], 2)
        self.assertEqual(updated["task_id"], "task-correction-uvp")
        self.assertIn(updated["status"], {"in_production", "awaiting_review"})
        self.assertEqual(correction["latest_decision"], "rejected")
        self.assertIn("script", correction_plan)
        self.assertIn("audio", correction_plan)
        self.assertIn("image", correction_plan)

        req = captured_requests.get("last_request")
        self.assertIsNotNone(req, "submit_or_reuse DEVE ser chamado pelo pipeline unificado")
        self.assertIsNone(
            captured_requests.get("legacy_initial_result"),
            "SÃ©ries NÃƒO deve passar initial_result legado (seeds via campos do UReq)."
        )

        # ===== VALIDAÃ‡ÃƒO DA PARIDADE: mesmos campos do UnifiedVideoRequest usado por HistÃ³ria =====
        req_fields = {f for f in dir(req) if not f.startswith("_") and not callable(getattr(req, f))}
        for expected in _series_expected_request_fields():
            self.assertIn(expected, req_fields, f"SÃ©ries: campo {expected} ausente no UReq (HistÃ³ria tem).")

        # ===== VALIDAÃ‡ÃƒO DOS SEEDS: script/audio carregados, SOMENTE imagem alvo Ã© regenerada =====
        self.assertIsNotNone(req.seeded_script, "seeded_script deve ser passado ao UReq (reutiliza script).")
        self.assertIsInstance(req.selected_images, list, "selected_images deve ser list com slots por cena.")
        self.assertGreaterEqual(len(req.selected_images), 1, "selected_images deve conter slots para cada cena.")
        self.assertIsNotNone(req.reuse_audio_from, "reuse_audio_from deve carregar output_path + srt da geraÃ§Ã£o anterior.")
        self.assertTrue(req.force_regenerate, "force_regenerate=True quando imagem estÃ¡ em regenerated_components.")

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
        self.assertIn("proximo", episodes[0]["next_episode_hook"].lower()
                      .replace("ó", "o").replace("õ", "o").replace("â", "a").replace("ã", "a").replace("ç", "c"))
        self.assertIn("concl", episodes[-1]["next_episode_hook"].lower()
                      .replace("ó", "o").replace("õ", "o").replace("â", "a").replace("ã", "a").replace("ç", "c"))

    def test_scheduler_is_idempotent_for_due_episode(self):
        detail = self._create_series(status="active")
        series_id = detail["id"]
        episode = self.db.query(SeriesEpisode).filter(SeriesEpisode.series_id == series_id).order_by(SeriesEpisode.episode_number.asc()).first()
        episode.production_datetime = datetime.utcnow() - timedelta(minutes=5)
        episode.status = "awaiting_production"
        self.db.commit()

        call_counter = {"count": 0}

        def fake_enqueue(db, user, series, episode, correction_feedback=None, initial_result=None, force_regenerate=False, **_):
            call_counter["count"] += 1
            episode.task_id = "task-due"
            episode.status = "in_production"
            return {"task_id": "task-due", "content_fingerprint": "fp-task-due"}

        with patch.object(youtube_series_service, "_enqueue_episode_generation", side_effect=fake_enqueue), \
             patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}):
            youtube_series_service.sync_series_scheduler(self.db, now=datetime.utcnow())
            youtube_series_service.sync_series_scheduler(self.db, now=datetime.utcnow())

        self.assertEqual(call_counter["count"], 1)

    def test_failed_provider_task_is_preserved_and_not_requeued(self):
        detail = self._create_series(status="active", end_date="2026-08-01")
        series_id = detail["id"]
        episode = self.db.query(SeriesEpisode).filter(SeriesEpisode.series_id == series_id).first()
        episode.status = "in_production"
        episode.task_id = "task-openai-no-credit"
        episode.production_datetime = datetime.utcnow() - timedelta(minutes=5)
        self.db.commit()

        provider_error = {
            "provider": "openai",
            "code": "OPENAI_NO_CREDIT",
            "message": "OpenAI sem saldo/quota para gerar imagens.",
            "retryable": False,
            "action_required": "Adicionar créditos na OpenAI.",
            "model": "gpt-image-1-mini",
        }
        failed_task = {
            "status": "failed",
            "message": provider_error["message"],
            "result": {"provider_error": provider_error},
        }

        with patch("app.services.youtube_series_service.get_task", return_value=failed_task), \
             patch.object(youtube_series_service, "_enqueue_episode_generation") as enqueue, \
             patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}):
            youtube_series_service.sync_series_scheduler(self.db, now=datetime.utcnow())
            result = youtube_series_service.get_series_detail(self.db, user=self.user, series_id=series_id)

        self.db.refresh(episode)
        serialized = result["episodes"][0]
        self.assertEqual(episode.status, "failed")
        self.assertEqual(episode.task_id, "task-openai-no-credit")
        self.assertEqual(serialized["task_error"], provider_error["message"])
        self.assertEqual(serialized["provider_error"]["code"], "OPENAI_NO_CREDIT")
        enqueue.assert_not_called()


# ==============================================================================
# ==============================================================================
# CLASSE DE TESTES QUE PROVA A PARIDADE:
#   SÃ‰RIES PROGRAMADAS  == EXATAMENTE ==  HISTÃ“RIA / DEVOCIONAL
#
# VerificaÃ§Ãµes arquiteturais:
#   1. SÃ©ries NÃƒO importa/usa nenhum serviÃ§o legado de geraÃ§Ã£o.
#   2. SÃ©ries usa submit_or_reuse com EXATAMENTE os mesmos parÃ¢metros do Story.
#   3. AprovaÃ§Ã£o usa transition_status + publish_if_ready (mesmos do Story review).
#   4. publish_if_ready usa upload_callable (YouTubeService.upload_video),
#      NÃƒO faz upload secundÃ¡rio dentro do approve_episode.
#   5. IdempotÃªncia versionada: episode.current_version ++ â†’ :vN no ik.
# ==============================================================================
# ==============================================================================

class SeriesPipelineParityTests(unittest.TestCase):
    """PROVA: mÃ³dulo SÃ©ries percorre EXATAMENTE o mesmo fluxo do mÃ³dulo HistÃ³ria/Devocional."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="yt-series-parity-"))
        self.db_path = self.temp_dir / "parity.sqlite"
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()

        tenant = Tenant(name="Tenant Parity", slug="tenant-parity")
        self.db.add(tenant)
        self.db.flush()
        self.user = User(
            tenant_id=tenant.id,
            email="parity@codexia.test",
            name="Parity Tester",
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

    def _create_active_series_with_episode(self):
        payload = {
            "name": "Serie Paridade Historia vs Series",
            "main_theme": "Como confiar no Senhor em tempos de crise",
            "objective": "Teste de paridade de pipeline.",
            "target_audience": "Adultos cristaos",
            "content_type": "reflection",
            "start_date": "2026-08-10",
            "end_date": "2026-08-10",
            "publication_time": "20:00",
            "timezone": "America/Sao_Paulo",
            "production_lead_days": 1,
            "production_time": "08:00",
            "duration_minutes": 10,
            "visibility": "unlisted",
            "tone": "acolhedor",
            "narration_style": "human",
            "continuity_level": "high",
            "hook_intensity": "medium",
            "use_biblical_references": True,
            "cta_subscribe": True,
            "cta_next_episode": True,
            "status": "active",
        }
        with patch.object(youtube_series_service, "_episode_planned_cost", return_value={"estimated_cost": 1.0}):
            detail = youtube_series_service.create_series(self.db, user=self.user, payload=payload)
        # Garante IDs numéricos preenchidos antes do retorno
        self.db.flush()
        series = self.db.query(SeriesPlan).filter(SeriesPlan.id == int(detail["id"])).first()
        self.assertIsNotNone(series, "SeriesPlan não persistido.")
        episode = self.db.query(SeriesEpisode).filter(SeriesEpisode.series_id == int(series.id)).first()
        self.assertIsNotNone(episode, "SeriesEpisode não persistido.")
        self.assertIsNotNone(getattr(series, "id", None), "series.id NÃO pode ser None (quebra idempotency_key).")
        self.assertIsNotNone(getattr(episode, "episode_number", None),
                             "episode.episode_number NÃO pode ser None.")
        return series, episode, detail

    # ------------------------------------------------------------------
    # 1. ARQUITETURA: SEM DUPLICIDADE â€” NÃƒO EXPORTA SERVIÃ‡OS LEGADOS
    # ------------------------------------------------------------------
    def test_series_service_module_does_not_import_legacy_pipeline_symbols(self):
        """youtube_series_service NÃƒO pode importar claim_video_task / AIContentGenerator / VoiceoverService / VideoFactory."""
        import importlib
        import inspect
        ytss_mod = importlib.import_module("app.services.youtube_series_service")
        source = inspect.getsource(ytss_mod)
        forbidden_top_level = [
            "claim_video_task",  # NÃƒO tem 2Âº pipeline
            "AIContentGenerator",
            "VoiceoverService",
            "VideoFactory",
            "AiContent",
        ]
        for symbol in forbidden_top_level:
            pattern = f"from {symbol}|import {symbol}"
            import_line = f"import {symbol}"
            from_line = f"from app"
            self.assertNotIn(
                import_line, source,
                f"youtube_series_service.py NÃƒO DEVE importar '{symbol}' (seria pipeline duplicado). "
                "Apenas app/services/unified_video_pipeline.py deve instanciar serviÃ§os de geraÃ§Ã£o."
            )
        # TambÃ©m garante que, no bloco de imports de task_manager, claim_video_task nÃ£o esteja
        self.assertFalse(
            getattr(ytss_mod, "claim_video_task", None) is not None and callable(getattr(ytss_mod, "claim_video_task", None)),
            "claim_video_task NÃƒO deve estar disponÃ­vel em youtube_series_service (removido do bloco de imports)."
        )

    def test_series_service_requires_unified_pipeline_before_any_generation(self):
        """_require_unified_pipeline existe e levanta RuntimeError se UVP nÃ£o carregar."""
        import app.services.youtube_series_service as ytss_mod
        self.assertTrue(
            callable(getattr(ytss_mod, "_require_unified_pipeline", None)),
            "_require_unified_pipeline deve ser a funÃ§Ã£o de guarda (nÃ£o _get_unified_video_pipeline com fallback)."
        )
        # Simula UVP ausente e confirma RuntimeError
        saved_ok, saved_uvp, saved_factory, saved_ureq = (
            ytss_mod._UVP_OK,
            ytss_mod._UVP,
            ytss_mod._unified_video_pipeline_factory,
            ytss_mod._UReq,
        )
        try:
            ytss_mod._UVP_OK = False
            ytss_mod._UVP = None
            ytss_mod._unified_video_pipeline_factory = None
            ytss_mod._UReq = None
            with self.assertRaises(RuntimeError):
                ytss_mod._require_unified_pipeline("test_guard")
        finally:
            ytss_mod._UVP_OK, ytss_mod._UVP, ytss_mod._unified_video_pipeline_factory, ytss_mod._UReq = (
                saved_ok, saved_uvp, saved_factory, saved_ureq
            )

    # ------------------------------------------------------------------
    # 2. PARIDADE submit_or_reuse: Mesmos parÃ¢metros que HistÃ³ria
    # ------------------------------------------------------------------
    def test_series_submit_or_reuse_called_with_same_signature_as_story_module(self):
        """SÃ©ries chama submit_or_reuse(db, request=UnifiedVideoRequest, kick_queue_callback, legacy_initial_result, user)."""
        series, episode, detail = self._create_active_series_with_episode()

        captured: Dict[str, Any] = {}

        def fake_submit(*args, **kwargs):
            captured["args_count"] = len(args)
            captured["kwargs_keys"] = set(kwargs.keys())
            captured["request_instance_type"] = type(kwargs.get("request")).__name__
            captured["source_module"] = getattr(kwargs.get("request"), "source_module", None)
            captured["source_id_prefix"] = str(getattr(kwargs.get("request"), "source_id", "")).split(":")[0] or None
            captured["kick_callback_type"] = (
                "callable" if callable(kwargs.get("kick_queue_callback")) or kwargs.get("kick_queue_callback") is None else "invalid"
            )
            captured["user_passed"] = kwargs.get("user") is not None
            captured["has_legacy_initial_result"] = "legacy_initial_result" in captured["kwargs_keys"]
            captured["legacy_initial_result_value"] = kwargs.get("legacy_initial_result")
            fake_result = MagicMock()
            fake_result.task_id = "task-parity-001"
            fake_result.message = "queued"
            fake_result.reused_existing = False
            fake_result.reused_completed = False
            fake_result.queue_position = 1
            fake_result.already_processing = False
            fake_result.idempotency_key = str(getattr(kwargs.get("request"), "idempotency_key", ""))
            fake_result.video_url = None
            fake_result.youtube_video_id = None
            fake_result.providers = {}
            fake_result.unified_video_id = 123
            return fake_result

        with patch("app.services.youtube_series_service.update_task", return_value=None), \
             patch("app.services.youtube_series_service._require_unified_pipeline") as mock_req:
            uvpsvc = MagicMock()
            uvpsvc.submit_or_reuse = fake_submit
            uvpsvc.ensure_schema = MagicMock()
            mock_req.return_value = uvpsvc

            youtube_series_service._enqueue_episode_generation(
                self.db,
                user=self.user,
                series=series,
                episode=episode,
            )

        # ======== Exatamente os mesmos 5 parÃ¢metros que o mÃ³dulo story ========
        story_expected_kwargs = {"db", "request", "kick_queue_callback", "legacy_initial_result", "user"}
        # O primeiro arg posicional Ã© db (self.db via positional), os restantes sÃ£o kwargs
        self.assertEqual(captured["args_count"], 1, "submit_or_reuse deve receber 'db' como 1Âº arg posicional.")
        # db = args[0], restante via kwargs
        self.assertTrue(
            story_expected_kwargs.issubset(captured["kwargs_keys"] | {"db"}),
            f"SÃ©ries deve chamar submit_or_reuse com mesma assinatura que story: {story_expected_kwargs}. "
            f"Obtido: {captured['kwargs_keys']}"
        )

        self.assertEqual(captured["source_module"], "youtube_series",
                         "source_module=youtube_series (diferencia registros no unified_videos sem duplicar lÃ³gica).")
        self.assertEqual(captured["source_id_prefix"], "episode",
                         "source_id='episode:{id}' â€” padrÃ£o Ãºnico para encontrar UnifiedVideo pelo episÃ³dio.")
        self.assertEqual(captured["kick_callback_type"], "callable",
                         "kick_queue_callback deve ser callable (ou None), como no mÃ³dulo story.")
        self.assertTrue(captured["user_passed"], "O usuÃ¡rio autenticado deve ser repassado ao submit_or_reuse (igual story).")
        self.assertTrue(captured["has_legacy_initial_result"],
                        "legacy_initial_result deve ser passado explicitamente (igual story, mesmo quando None).")
        # Em SÃ©ries: legacy_initial_result Ã© sempre None (seeds via UReq)
        self.assertIsNone(
            captured.get("legacy_initial_result_value"),
            "SÃ©ries NÃƒO deve carregar 'base_result' em legacy_initial_result. Seeds sÃ£o campos do UnifiedVideoRequest."
        )

    def test_series_idempotency_key_versioned_by_current_version(self):
        """episode.current_version â†’ ':vN' no idempotency_key (reprocessamento de nova versÃ£o gera nova task, nÃ£o reutiliza errado)."""
        series, episode, detail = self._create_active_series_with_episode()
        captured_key: Dict[str, Any] = {}

        def fake_submit(db, *, request, **_):
            captured_key["ikey"] = str(request.idempotency_key)
            captured_key["source_id"] = str(request.source_id)
            fake_result = MagicMock()
            fake_result.task_id = "task-vcheck"
            fake_result.message = "ok"
            fake_result.reused_existing = False
            fake_result.reused_completed = False
            fake_result.queue_position = 1
            fake_result.already_processing = False
            fake_result.idempotency_key = str(request.idempotency_key)
            fake_result.video_url = None
            fake_result.youtube_video_id = None
            fake_result.providers = {}
            fake_result.unified_video_id = 1
            return fake_result

        with patch("app.services.youtube_series_service.update_task", return_value=None), \
             patch("app.services.youtube_series_service._require_unified_pipeline") as mock_req:
            uvpsvc = MagicMock()
            uvpsvc.submit_or_reuse = fake_submit
            uvpsvc.ensure_schema = MagicMock()
            mock_req.return_value = uvpsvc

            episode.current_version = 3
            youtube_series_service._enqueue_episode_generation(self.db, user=self.user, series=series, episode=episode)

        self.assertIn(":v3", captured_key["ikey"],
                      f"idempotency_key deve terminar com :v{int(episode.current_version or 1)} (3 nesta simulaÃ§Ã£o). "
                      f"Obtido: {captured_key['ikey']}")

    # ------------------------------------------------------------------
    # 3. AprovaÃ§Ã£o: transition_status â†’ APPROVED + publish_if_ready ÃšNICO
    # ------------------------------------------------------------------
    def test_approve_episode_uses_unified_pipeline_exclusive_methods_no_secondary_upload(self):
        """approve_episode chama: transition_status â†’ APPROVED + publish_if_ready(upload_callable).
        NÃƒO pode existir um YouTubeService.upload_video FORA do upload_callable (upload secundÃ¡rio)."""
        series, episode, detail = self._create_active_series_with_episode()
        series.auto_approval = True
        episode.task_id = "task-parity-approve"
        episode.status = "awaiting_review"
        episode.current_version = 1
        self.db.commit()

        fake_uv = MagicMock()
        fake_uv.id = 7
        fake_uv.task_id = "task-parity-approve"
        fake_uv.idempotency_key = "yts:series:parity:episode:1:v1"
        fake_uv.source_module = "youtube_series"
        fake_uv.source_id = f"episode:{int(episode.id)}"
        fake_uv.status = UnifiedVideoStatus.AWAITING_REVIEW
        fake_uv.video_url = "/media/parity.mp4"
        fake_uv.video_path = str(self.temp_dir / "parity.mp4")
        fake_uv.title = "EpisÃ³dio Paridade"
        fake_uv.description = "DescriÃ§Ã£o paridade"
        fake_uv.youtube_video_id = None
        fake_uv.youtube_url = None
        fake_uv.script_json = json.dumps({"title": fake_uv.title, "description": fake_uv.description})
        fake_uv.estimated_cost = 0.0
        fake_uv.actual_cost = 0.0
        fake_uv.call_count_text = 0
        fake_uv.call_count_image = 0
        fake_uv.call_count_audio = 0
        fake_uv.result_json = None

        captured_calls: Dict[str, Any] = {"transition": None, "publish": None, "yt_upload_calls": 0}

        def fake_transition(db, idempotency_key_or_task_id, *, status, step=None, progress=None, message=None, merge_result=None):
            captured_calls["transition"] = {
                "id": idempotency_key_or_task_id,
                "status": status,
                "progress": progress,
                "has_merge_result": merge_result is not None,
            }
            fake_uv.status = status
            return fake_uv

        def fake_publish(db, idempotency_key_or_task_id, *, upload_callable, upload_metadata=None, visibility_override=None):
            captured_calls["publish"] = {
                "id": idempotency_key_or_task_id,
                "upload_callable_callable": callable(upload_callable),
                "has_upload_metadata": upload_metadata is not None,
                "has_visibility_override": visibility_override is not None,
            }
            # Call upload_callable inside publish_if_ready exactly once (just like real impl would when needed)
            vp = fake_uv.video_path or (self.temp_dir / "parity.mp4")
            Path(vp).write_bytes(b"fake-mp4")
            upload_res = upload_callable(str(vp), {
                "title": "X", "description": "Y", "tags": ["parity"], "visibility": "unlisted"
            })
            captured_calls["last_upload_callable_result"] = upload_res
            return {"uploaded": True, "youtube_video_id": "upload-via-callable",
                    "youtube_url": "https://www.youtube.com/watch?v=upload-via-callable"}

        class _FakeYouTubeService:
            def __init__(s):
                s.service = object()

            def upload_video(s, file_path, title, description, tags=None, category_id="27", thumbnail_path=None):
                captured_calls["yt_upload_calls"] += 1
                return {"id": f"yt-{captured_calls['yt_upload_calls']}",
                        "youtube_video_id": f"yt-{captured_calls['yt_upload_calls']}"}

        with patch("app.services.youtube_series_service.get_task", return_value={"status": "completed", "result": {
            "title": fake_uv.title, "description": fake_uv.description,
            "video_url": fake_uv.video_url,
        }}), patch("app.services.youtube_series_service._require_unified_pipeline") as mock_req, \
             patch.object(youtube_series_service, "_find_unified_video_by_episode", return_value=fake_uv), \
             patch("app.services.youtube_service.YouTubeService", side_effect=lambda: _FakeYouTubeService()):
            uvpsvc = MagicMock()
            uvpsvc.transition_status = fake_transition
            uvpsvc.publish_if_ready = fake_publish
            mock_req.return_value = uvpsvc

            youtube_series_service.approve_episode(self.db, user=self.user, episode_id=int(episode.id))

        self.assertIsNotNone(captured_calls["transition"], "transition_status DEVE ser chamado no approve.")
        self.assertEqual(captured_calls["transition"]["status"], UnifiedVideoStatus.APPROVED,
                         f"transition_status DEVE levar para APPROVED â€” status: {UnifiedVideoStatus.APPROVED}.")
        self.assertIsNotNone(captured_calls["publish"], "publish_if_ready DEVE ser chamado no approve.")
        self.assertTrue(captured_calls["publish"]["upload_callable_callable"],
                        "publish_if_ready DEVE receber upload_callable callable.")

        # upload_callable chama YouTubeService.upload_video â†’ contagem deve ser 1
        # Se o approve_episode chamasse YouTubeService.upload_video FORA do callable (upload duplicado),
        # a contagem seria 2 ou mais.
        self.assertEqual(captured_calls["yt_upload_calls"], 1,
                         "DEVE haver EXATAMENTE 1 YouTubeService.upload_video POR approve_episode, "
                         "DENTRO do publish_if_ready via upload_callable. Nenhum upload secundÃ¡rio fora do UVP.")

    # ------------------------------------------------------------------
    # 4. Request fields: 1:1 com UnifiedVideoRequest (igual Story)
    # ------------------------------------------------------------------
    def test_series_unified_video_request_has_every_story_field(self):
        """SÃ©ries UReq âŠ‡ Story UReq. Todo campo que HistÃ³ria pode passar, SÃ©ries tambÃ©m pode."""
        series, episode, detail = self._create_active_series_with_episode()

        captured_req: Dict[str, Any] = {}

        def fake_submit(db, *, request, **_):
            captured_req["req"] = request
            fake_result = MagicMock()
            fake_result.task_id = "t-fields"
            fake_result.message = "ok"
            fake_result.reused_existing = False
            fake_result.reused_completed = False
            fake_result.queue_position = 1
            fake_result.already_processing = False
            fake_result.idempotency_key = str(request.idempotency_key)
            fake_result.video_url = None
            fake_result.youtube_video_id = None
            fake_result.providers = {}
            fake_result.unified_video_id = 7
            return fake_result

        with patch("app.services.youtube_series_service.update_task", return_value=None), \
             patch("app.services.youtube_series_service._require_unified_pipeline") as mock_req:
            uvpsvc = MagicMock()
            uvpsvc.submit_or_reuse = fake_submit
            uvpsvc.ensure_schema = MagicMock()
            mock_req.return_value = uvpsvc
            youtube_series_service._enqueue_episode_generation(self.db, user=self.user, series=series, episode=episode)

        req = captured_req["req"]
        story_payload = _story_build_request_payload()
        missing = []
        for key in story_payload.keys():
            if not hasattr(req, key):
                missing.append(key)
        self.assertEqual(missing, [],
                         f"SÃ©ries deve expor TODOS os campos que o payload de HistÃ³ria espera. "
                         f"Campos ausentes no UnifiedVideoRequest de sÃ©rie: {missing}")


if __name__ == "__main__":
    unittest.main()
