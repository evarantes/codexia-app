import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/codexia_test")

from app.database import Base  # noqa: E402
from app.models import Tenant, User, VideoTask  # noqa: E402
import app.services.financial_guardian_service as fg_module  # noqa: E402
from app.services.financial_guardian import youtube_auto_financial_adapter  # noqa: E402
from app.services.financial_guardian_service import financial_guardian_service  # noqa: E402


class FinancialGuardianYouTubeAdapterTests(unittest.TestCase):
    def setUp(self):
        manifest_path = fg_module._manifest_path()
        if manifest_path.exists():
            manifest_path.unlink()
        fg_module._schema_ready = False

        self.temp_dir = Path(tempfile.mkdtemp(prefix="guardian-youtube-"))
        self.db_path = self.temp_dir / "youtube.sqlite"
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()

        tenant = Tenant(name="Tenant Guardian", slug="tenant-guardian")
        self.db.add(tenant)
        self.db.flush()
        self.user = User(
            tenant_id=tenant.id,
            email="youtube@codexia.test",
            name="Guardian YouTube",
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

    def _create_task(self, task_id: str, payload: dict, status: str = "processing") -> VideoTask:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        row = VideoTask(
            id=task_id,
            user_id=self.user.id,
            status=status,
            progress=0,
            message="Teste",
            result_json=json.dumps({"payload": payload, "kind": "youtube_story_video"}, ensure_ascii=False),
            created_at=now,
            updated_at=now,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _create_image_file(self, name: str, content: bytes) -> str:
        path = self.temp_dir / name
        path.write_bytes(content)
        return str(path)

    def test_youtube_auto_adapter_supports_preflight_cache_reports_and_dashboards(self):
        payload = {
            "topic": "Salmo 23 para dormir em paz",
            "duration": 8,
            "auto_upload": False,
            "mode": "story",
            "kind": "prayer",
            "story_content": "O Senhor e meu pastor e nada me faltara.",
            "aspect_ratio": "16:9",
        }
        self._create_task("yt-task-1", payload, status="completed")
        context_store = youtube_auto_financial_adapter.build_context(
            task_id="yt-task-1",
            payload=payload,
            user_id=self.user.id,
            status="processing",
        )
        config = SimpleNamespace(
            per_video_spend_limit=5.0,
            daily_spend_limit=50.0,
            monthly_spend_limit=100.0,
        )

        preflight_ok = financial_guardian_service.evaluate_context_preflight(
            self.db,
            context=context_store,
            config=config,
            adapter=youtube_auto_financial_adapter,
        )
        self.assertTrue(preflight_ok["allowed"])

        plan = {
            "title": "Salmo 23",
            "scenes": [
                {
                    "text": "O Senhor e meu pastor e nada me faltara.",
                    "image_prompt": "Pastor guiding sheep at golden sunset",
                }
            ],
        }
        image_path = self._create_image_file("scene-01.bin", b"youtube-image-01")
        cache_store = financial_guardian_service.cache_images_from_context_result(
            self.db,
            context=context_store,
            plan=plan,
            image_paths=[image_path],
        )
        financial_guardian_service.record_context_event(
            self.db,
            context=context_store,
            event_type="production_completed",
            stage="render_completed",
            estimated_cost=context_store.estimated_cost,
            actual_cost=context_store.actual_cost,
            details={"cache_summary": cache_store},
        )
        self.db.commit()

        self._create_task("yt-task-2", payload, status="completed")
        context_reuse = youtube_auto_financial_adapter.build_context(
            task_id="yt-task-2",
            payload=payload,
            user_id=self.user.id,
            status="processing",
        )
        preflight_reuse = financial_guardian_service.evaluate_context_preflight(
            self.db,
            context=context_reuse,
            config=config,
            adapter=youtube_auto_financial_adapter,
        )
        self.assertTrue(preflight_reuse["allowed"])
        hydrated = financial_guardian_service.hydrate_plan_with_cached_images_for_context(
            self.db,
            context=context_reuse,
            plan=plan,
        )
        self.assertEqual(hydrated["selected_images"], [image_path])
        financial_guardian_service.record_context_event(
            self.db,
            context=context_reuse,
            event_type="production_completed",
            stage="render_completed",
            estimated_cost=context_reuse.estimated_cost,
            actual_cost=max(0.0, context_reuse.actual_cost - 0.01),
            details={"estimated_savings": 0.01},
        )
        self.db.commit()

        report = financial_guardian_service.build_context_financial_report(
            self.db,
            source_type="youtube_auto",
            context_id="yt-task-2",
            adapter=youtube_auto_financial_adapter,
        )
        self.assertTrue(report["found"])
        self.assertEqual(report["title"], "Salmo 23 para dormir em paz")
        self.assertGreaterEqual(report["cache_hits"], 1)

        daily = financial_guardian_service.build_daily_financial_report_for_source(
            self.db,
            source_type="youtube_auto",
            user_id=self.user.id,
            adapter=youtube_auto_financial_adapter,
        )
        self.assertGreaterEqual(daily["jobs_count"], 2)
        self.assertGreaterEqual(daily["event_count"], 4)

        dashboard = financial_guardian_service.build_user_dashboard_for_source(
            self.db,
            source_type="youtube_auto",
            user_id=self.user.id,
            adapter=youtube_auto_financial_adapter,
        )
        self.assertGreaterEqual(dashboard["finance"]["preflight_passed"], 2)
        self.assertGreaterEqual(dashboard["efficiency"]["image_cache_assets"], 1)
        self.assertGreaterEqual(dashboard["efficiency"]["image_cache_hits"], 1)

        self._create_task("yt-task-3", payload, status="failed")
        blocked = financial_guardian_service.evaluate_context_preflight(
            self.db,
            context=youtube_auto_financial_adapter.build_context(
                task_id="yt-task-3",
                payload=payload,
                user_id=self.user.id,
                status="queued",
            ),
            config=SimpleNamespace(
                per_video_spend_limit=0.01,
                daily_spend_limit=0.02,
                monthly_spend_limit=0.02,
            ),
            adapter=youtube_auto_financial_adapter,
        )
        self.assertFalse(blocked["allowed"])
        self.assertIn("limite por vídeo", blocked["reason"])


if __name__ == "__main__":
    unittest.main()
