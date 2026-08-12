import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/codexia_test")

from app.database import Base  # noqa: E402
from app.models import ScheduledVideo, Settings, Tenant, User, VideoTask  # noqa: E402
import app.services.financial_guardian_service as fg_module  # noqa: E402
from app.services.financial_guardian.youtube_observability import (  # noqa: E402
    youtube_financial_guardian_observability_service,
)


class YouTubeGuardianObservabilityTests(unittest.TestCase):
    def setUp(self):
        fg_module._schema_ready = False
        manifest_path = fg_module._manifest_path()
        if manifest_path.exists():
            manifest_path.unlink()

        self.temp_dir = Path(tempfile.mkdtemp(prefix="yt-guardian-observability-"))
        self.db_path = self.temp_dir / "guardian.sqlite"
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()

        tenant = Tenant(name="Tenant Observability", slug="tenant-observability")
        self.db.add(tenant)
        self.db.flush()
        self.user = User(
            tenant_id=tenant.id,
            email="observability@codexia.test",
            name="Guardian Observability",
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

    def test_simulated_scenarios_feed_overview_timeline_and_ledger_without_duplicates(self):
        service = youtube_financial_guardian_observability_service

        scenario_a = service.simulate_scenario(self.db, user=self.user, scenario_code="A")
        scenario_e = service.simulate_scenario(self.db, user=self.user, scenario_code="E")
        scenario_g = service.simulate_scenario(self.db, user=self.user, scenario_code="G")

        self.assertEqual(scenario_a["status"], "completed")
        self.assertEqual(scenario_e["status"], "failed")
        self.assertEqual(scenario_g["scenario_code"], "G")
        self.assertTrue(scenario_g["no_paid_calls_confirmed"])

        timeline = service.build_timeline(self.db, user=self.user, task_id=scenario_a["task_id"])
        self.assertTrue(timeline["found"])
        self.assertTrue(any(item["event_type"] == "VIDEO_PUBLISHED" for item in timeline["events"]))
        self.assertTrue(any(item["event_type"] == "PRE_ESTIMATE" for item in timeline["events"]))

        overview = service.build_overview(self.db, user=self.user, period="current_month")
        self.assertEqual(overview["source_type"], "youtube_auto")
        self.assertEqual(overview["dashboard"]["data_scope"], "actual")
        self.assertEqual(overview["dashboard"]["jobs_total"], 0)
        self.assertEqual(overview["dashboard"]["actual_cost_total"], 0.0)
        self.assertEqual(overview["ledger_summary"]["entries_count"], 0)
        self.assertTrue(overview["simulation_summary"]["separated_from_actual"])
        self.assertGreaterEqual(overview["simulation_summary"]["jobs_total"], 3)
        self.assertGreaterEqual(overview["simulation_summary"]["ledger_entries_total"], 4)
        self.assertGreater(overview["simulation_summary"]["actual_cost_total"], 0.0)
        self.assertEqual(overview["health"]["status"], "Sem dados")
        self.assertIn(
            overview["progress_indicator"]["label"],
            {"Progresso comprovado", "Progresso parcial", "Sem progresso mensurável", "Regressão"},
        )

        before_count = len(service.build_timeline(self.db, user=self.user, task_id=scenario_a["task_id"])["events"])
        service.simulate_scenario(self.db, user=self.user, scenario_code="A")
        after_count = len(service.build_timeline(self.db, user=self.user, task_id=scenario_a["task_id"])["events"])
        self.assertEqual(before_count, after_count)

    def test_real_costs_budget_and_manual_revenue_remain_outside_simulations(self):
        service = youtube_financial_guardian_observability_service
        self.db.add(Settings(
            user_id=self.user.id,
            per_video_spend_limit=1.0,
            daily_spend_limit=2.0,
            monthly_spend_limit=20.0,
        ))
        self.db.add(VideoTask(
            id="yt-real-cost-1",
            user_id=self.user.id,
            status="completed",
            progress=100,
            message="Vídeo real concluído",
            result_json=fg_module._json_dumps({
                "kind": "youtube_story_video",
                "payload": {"topic": "Vídeo real"},
                "financial_guardian": {
                    "source_type": "youtube_auto",
                    "estimated_cost": 0.75,
                    "actual_cost": 0.5,
                },
            }),
        ))
        self.db.commit()
        service.simulate_scenario(self.db, user=self.user, scenario_code="G")
        service.save_ledger_entry(
            self.db,
            user=self.user,
            payload={
                "entry_kind": "revenue",
                "category": "YouTube",
                "currency": "BRL",
                "amount": 3.0,
                "description": "Receita real informada manualmente",
            },
        )

        overview = service.build_overview(self.db, user=self.user, period="current_month")

        self.assertEqual(overview["dashboard"]["jobs_total"], 1)
        self.assertEqual(overview["dashboard"]["actual_cost_total"], 0.5)
        self.assertEqual(overview["dashboard"]["revenue_total"], 3.0)
        self.assertEqual(overview["ledger_summary"]["entries_count"], 1)
        self.assertEqual(overview["budget"]["status"], "within_budget")
        self.assertEqual(overview["budget"]["daily_spent"], 0.5)
        self.assertEqual(overview["budget"]["daily_remaining"], 1.5)
        self.assertEqual(overview["simulation_summary"]["jobs_total"], 1)
        self.assertGreaterEqual(overview["simulation_summary"]["ledger_entries_total"], 4)

    def test_preestimate_and_manual_ledger_entry_are_available(self):
        service = youtube_financial_guardian_observability_service
        estimate = service.estimate_preproduction(
            user=self.user,
            payload={
                "topic": "Salmo 91",
                "story_content": "Aquele que habita no esconderijo do Altissimo.",
                "duration": 9,
                "kind": "story",
                "aspect_ratio": "16:9",
                "selected_images": ["/tmp/existing-image.png"],
            },
        )
        self.assertEqual(estimate["source_type"], "youtube_auto")
        self.assertGreaterEqual(estimate["scenes_predicted"], 4)
        self.assertGreaterEqual(estimate["expected_cost"], 0.0)

        saved = service.save_ledger_entry(
            self.db,
            user=self.user,
            payload={
                "entry_kind": "expense",
                "category": "Desenvolvimento",
                "currency": "BRL",
                "amount": 123.45,
                "description": "Custo simulado de desenvolvimento",
                "occurred_at": "2026-07-24T12:00:00",
                "metadata": {"manual": True},
            },
        )
        self.assertTrue(saved["id"])

        ledger = service.list_ledger_entries(self.db, user=self.user)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["category"], "Desenvolvimento")

    def test_overview_exposes_content_registry_artifact_library_and_shorts_without_new_store(self):
        service = youtube_financial_guardian_observability_service
        fg_module.financial_guardian_service.ensure_schema(self.db)

        image_path = self.temp_dir / "scene-01.png"
        audio_path = self.temp_dir / "narration.mp3"
        video_path = self.temp_dir / "video-final.mp4"
        subtitle_path = self.temp_dir / "video-final.srt"
        for path in (image_path, audio_path, video_path, subtitle_path):
            path.write_text("stub", encoding="utf-8")

        now = datetime.utcnow()
        fingerprint = "fp-salmo-23"
        base_payload = {
            "topic": "Salmo 23",
            "mode": "topic",
            "kind": "story",
            "auto_upload": False,
            "content_fingerprint": fingerprint,
            "internal_title": "salmo 23",
            "youtube_title": "Salmo 23",
            "narrated_title": "Salmo 23",
        }
        base_result = {
            "kind": "youtube_story_video",
            "payload": base_payload,
            "title_control": {
                "internal_title": "salmo 23",
                "youtube_title": "Salmo 23",
                "narrated_title": "Salmo 23",
            },
            "script": {
                "internal_title": "salmo 23",
                "youtube_title": "Salmo 23",
                "narrated_title": "Salmo 23",
                "selected_images": [str(image_path)],
            },
            "file_path": str(video_path),
            "video_url": str(video_path),
            "audio_generation": {
                "output_path": str(audio_path),
            },
            "render_report": {
                "scene_visuals": [{"image_path": str(image_path), "source": "generated"}],
                "official_audio_transcription": {
                    "srt_path": str(subtitle_path),
                    "segments": [{"text": "O Senhor e o meu pastor"}],
                },
            },
            "cost_control": {
                "reused_video": True,
            },
            "financial_guardian": {
                "source_type": "youtube_auto",
                "estimated_cost": 1.2,
                "actual_cost": 0.7,
            },
        }

        latest_task = VideoTask(
            id="yt-registry-latest",
            user_id=self.user.id,
            status="completed",
            progress=100,
            message="Video gerado",
            result_json=fg_module._json_dumps(base_result),
            created_at=now - timedelta(minutes=10),
            updated_at=now - timedelta(minutes=5),
        )
        older_task = VideoTask(
            id="yt-registry-older",
            user_id=self.user.id,
            status="completed",
            progress=100,
            message="Video anterior",
            result_json=fg_module._json_dumps({
                **base_result,
                "file_path": "",
                "video_url": "",
                "audio_generation": {},
                "render_report": {},
            }),
            created_at=now - timedelta(days=1),
            updated_at=now - timedelta(days=1),
        )
        self.db.add_all([latest_task, older_task])
        self.db.flush()

        self.db.execute(text(
            f"""
            INSERT INTO {fg_module._AUDIT_TABLE} (
                user_id, job_id, source_type, context_id, scope_key, event_type, stage, severity,
                estimated_cost, actual_cost, context_json, details_json, created_at
            ) VALUES (
                :user_id, NULL, 'youtube_auto', :context_id, :scope_key, 'production_completed', 'upload_completed', 'info',
                :estimated_cost, :actual_cost, :context_json, :details_json, :created_at
            )
            """
        ), {
            "user_id": int(self.user.id),
            "context_id": latest_task.id,
            "scope_key": f"user:{int(self.user.id)}",
            "estimated_cost": 1.2,
            "actual_cost": 0.7,
            "context_json": fg_module._json_dumps({
                "context_id": latest_task.id,
                "title": "Salmo 23",
                "status": "completed",
            }),
            "details_json": fg_module._json_dumps({
                "upload_result": {"id": "yt-123"},
            }),
            "created_at": now - timedelta(minutes=4),
        })
        self.db.execute(text(
            f"""
            INSERT INTO {fg_module._CACHE_TABLE} (
                user_id, job_id, source_type, context_id, scope_key, asset_kind, cache_key, file_hash, file_path,
                hit_count, last_used_at, created_at, updated_at, meta_json
            ) VALUES (
                :user_id, NULL, 'youtube_auto', :context_id, :scope_key, 'image', :cache_key, :file_hash, :file_path,
                :hit_count, :last_used_at, :created_at, :updated_at, :meta_json
            )
            """
        ), {
            "user_id": int(self.user.id),
            "context_id": latest_task.id,
            "scope_key": f"user:{int(self.user.id)}",
            "cache_key": "cache-scene-01",
            "file_hash": "hash-scene-01",
            "file_path": str(image_path),
            "hit_count": 3,
            "last_used_at": now - timedelta(minutes=3),
            "created_at": now - timedelta(minutes=6),
            "updated_at": now - timedelta(minutes=3),
            "meta_json": fg_module._json_dumps({"scene_number": 1}),
        })

        parent_video = ScheduledVideo(
            user_id=self.user.id,
            theme="Salmo 23",
            title="Salmo 23 completo",
            description="Video longo",
            scheduled_for=now,
            status="published",
            video_type="video",
            video_url=str(video_path),
            progress=100,
            youtube_video_id="yt-parent-1",
            uploaded_at=now - timedelta(minutes=20),
            updated_at=now - timedelta(minutes=20),
        )
        self.db.add(parent_video)
        self.db.flush()
        short_published = ScheduledVideo(
            user_id=self.user.id,
            theme="Salmo 23 short",
            title="Salmo 23 corte 1",
            description="Short publicado",
            scheduled_for=now,
            status="published",
            video_type="short",
            parent_video_id=parent_video.id,
            video_url=str(video_path),
            progress=100,
            youtube_video_id="yt-short-1",
            uploaded_at=now - timedelta(minutes=2),
            updated_at=now - timedelta(minutes=2),
        )
        short_failed = ScheduledVideo(
            user_id=self.user.id,
            theme="Salmo 23 short 2",
            title="Salmo 23 corte 2",
            description="Short falhou",
            scheduled_for=now,
            status="failed",
            video_type="short",
            parent_video_id=parent_video.id,
            video_url=str(video_path),
            progress=0,
            updated_at=now - timedelta(minutes=1),
        )
        self.db.add_all([short_published, short_failed])
        self.db.commit()

        overview = service.build_overview(self.db, user=self.user, period="current_month")

        registry = overview["content_registry"]
        self.assertEqual(registry["items_total"], 1)
        self.assertEqual(registry["published_total"], 1)
        self.assertEqual(registry["duplicate_groups"], 1)
        self.assertEqual(registry["recent_items"][0]["duplicate_versions"], 2)
        self.assertEqual(registry["recent_items"][0]["content_fingerprint"], fingerprint)

        artifact_library = overview["artifact_library"]
        self.assertGreaterEqual(artifact_library["scripts_ready_total"], 1)
        self.assertEqual(artifact_library["image_cache_rows_total"], 1)
        self.assertEqual(artifact_library["image_cache_hits_total"], 3)
        self.assertGreaterEqual(artifact_library["audio_available_total"], 1)
        self.assertGreaterEqual(artifact_library["videos_available_total"], 1)
        self.assertGreaterEqual(artifact_library["transcriptions_ready_total"], 1)

        shorts_summary = overview["shorts_summary"]
        self.assertEqual(shorts_summary["shorts_total"], 2)
        self.assertEqual(shorts_summary["published_total"], 1)
        self.assertEqual(shorts_summary["failed_total"], 1)
        self.assertEqual(shorts_summary["recent_items"][0]["parent_title"], "Salmo 23 completo")


if __name__ == "__main__":
    unittest.main()
