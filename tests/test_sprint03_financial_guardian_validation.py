import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/codexia_test")

from app.database import Base  # noqa: E402
from app.models import Tenant, User  # noqa: E402
from app.modules.bible_video_factory.models import BibleVideoJob, BibleVideoMetric  # noqa: E402
import app.services.financial_guardian_service as fg_module  # noqa: E402
from app.services.financial_guardian_service import (  # noqa: E402
    build_image_cache_key,
    evaluate_recovery_loop,
    financial_guardian_service,
)


ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "artifacts" / "sprint03_guardian_validation"


class Sprint03FinancialGuardianValidationTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        if ARTIFACT_DIR.exists():
            shutil.rmtree(ARTIFACT_DIR)
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

        manifest_path = fg_module._manifest_path()
        if manifest_path.exists():
            manifest_path.unlink()
        fg_module._schema_ready = False

        self.temp_dir = Path(tempfile.mkdtemp(prefix="guardian-validation-"))
        self.db_path = self.temp_dir / "validation.sqlite"
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        Base.metadata.create_all(self.engine)
        self.db = self.Session()

        tenant = Tenant(name="Tenant Teste", slug="tenant-teste")
        self.db.add(tenant)
        self.db.flush()
        self.user = User(
            tenant_id=tenant.id,
            email="guardian@codexia.test",
            name="Guardiao",
            hashed_password="hash",
            is_active=True,
            is_admin=True,
            role="admin",
        )
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)

        self.results = []

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_evidence(self, slug, payload):
        path = ARTIFACT_DIR / f"{slug}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def _record_result(self, slug, title, payload):
        path = self._write_evidence(slug, payload)
        self.results.append({"slug": slug, "title": title, "path": str(path), "payload": payload})
        return payload

    def _create_job(self, **overrides):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        payload = {
            "user_id": self.user.id,
            "title": "Job de Validacao",
            "job_type": "episode",
            "platform": "youtube",
            "aspect_ratio": "16:9",
            "kanban_stage": "idea",
            "status": "queued",
            "approval_status": "pending",
            "progress": 0,
            "estimated_cost": 0.0,
            "actual_cost": 0.0,
            "created_at": now,
            "updated_at": now,
        }
        payload.update(overrides)
        job = BibleVideoJob(**payload)
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def _create_metric(self, **overrides):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        payload = {
            "user_id": self.user.id,
            "platform": "youtube",
            "view_count": 0,
            "ctr": 0.0,
            "retention": 0.0,
            "subscribers_gained": 0,
            "likes": 0,
            "comments": 0,
            "created_at": now,
            "updated_at": now,
        }
        payload.update(overrides)
        metric = BibleVideoMetric(**payload)
        self.db.add(metric)
        self.db.commit()
        self.db.refresh(metric)
        return metric

    def _create_image_file(self, name, content):
        image_path = self.temp_dir / name
        image_path.write_bytes(content)
        return str(image_path)

    def test_validacao_funcional_completa_do_guardiao_financeiro(self):
        config = SimpleNamespace(
            per_video_spend_limit=10.0,
            daily_spend_limit=20.0,
            monthly_spend_limit=40.0,
            max_quality_recovery_attempts=2,
            min_quality_recovery_score_delta=1.0,
        )

        historico = self._create_job(
            title="Historico do dia",
            status="ready",
            estimated_cost=4.5,
            actual_cost=7.0,
        )
        job_ok = self._create_job(
            title="Video validado",
            status="queued",
            estimated_cost=6.0,
        )
        decision_ok = financial_guardian_service.evaluate_job_preflight(self.db, job=job_ok, config=config)
        self.assertTrue(decision_ok["allowed"])
        self.assertEqual(decision_ok["estimated_cost"], 6.0)
        self._record_result(
            "01_estimativa_pre_geracao",
            "Estimativa de custo antes da geração",
            {
                "regra": "estimativa de custo antes da geração",
                "job_id": job_ok.id,
                "estimated_cost": job_ok.estimated_cost,
                "preflight": decision_ok,
            },
        )

        job_block = self._create_job(
            title="Video bloqueado",
            status="queued",
            estimated_cost=14.0,
        )
        decision_block = financial_guardian_service.evaluate_job_preflight(self.db, job=job_block, config=config)
        self.assertFalse(decision_block["allowed"])
        self.assertIn("limite por vídeo", decision_block["reason"])
        self.assertIn("limite diário", decision_block["reason"])
        self._record_result(
            "02_bloqueio_orcamento",
            "Bloqueio por orçamento configurado",
            {
                "regra": "bloqueio por orçamento configurado",
                "job_id": job_block.id,
                "preflight": decision_block,
            },
        )

        financial_guardian_service.record_event(
            self.db,
            user_id=self.user.id,
            job_id=job_ok.id,
            event_type="production_started",
            stage="scenes_generated",
            estimated_cost=job_ok.estimated_cost,
            actual_cost=job_ok.actual_cost,
            details={"simulado": True},
        )
        plan_base = {
            "scenes": [
                {
                    "text": "O mar se abre diante do povo.",
                    "image_prompt": "Moses opens the sea at sunset",
                    "caption": "Legenda A",
                }
            ]
        }
        image_path = self._create_image_file("scene-01.bin", b"fake-image-scene-01")
        cache_store = financial_guardian_service.cache_images_from_result(
            self.db,
            job=job_ok,
            plan=plan_base,
            image_paths=[image_path],
        )
        hydrated = financial_guardian_service.hydrate_plan_with_cached_images(self.db, job=job_ok, plan=plan_base)
        recovery_check = evaluate_recovery_loop(
            stage="captions_render",
            attempt_number=2,
            before_score=89.0,
            after_score=89.2,
            min_score_delta=config.min_quality_recovery_score_delta,
            max_attempts=config.max_quality_recovery_attempts,
        )
        self.assertTrue(recovery_check["stop"])
        financial_guardian_service.record_event(
            self.db,
            user_id=self.user.id,
            job_id=job_ok.id,
            event_type="recovery_loop_blocked",
            stage="captions_render",
            severity="warning",
            estimated_cost=job_ok.estimated_cost,
            actual_cost=job_ok.actual_cost,
            details=recovery_check,
        )
        job_ok.actual_cost = 6.0
        job_ok.status = "ready"
        self.db.commit()
        audit = financial_guardian_service.get_job_audit(self.db, job_ok.id)
        event_types = [event["event_type"] for event in audit["events"]]
        self.assertEqual(
            event_types,
            [
                "preflight_allowed",
                "production_started",
                "image_cache_stored",
                "image_cache_applied",
                "recovery_loop_blocked",
            ],
        )
        self._record_result(
            "03_auditoria_etapas",
            "Auditoria registrando todas as etapas",
            {
                "regra": "auditoria registrando todas as etapas",
                "job_id": job_ok.id,
                "event_types": event_types,
                "audit": audit,
            },
        )

        self.assertEqual(cache_store["stored_assets"], 1)
        self.assertEqual(hydrated["selected_images"], [image_path])
        self._record_result(
            "04_cache_imagens",
            "Cache de imagens funcionando",
            {
                "regra": "cache de imagens funcionando",
                "cache_store": cache_store,
                "hydrated_plan": hydrated,
            },
        )

        hash_reuse_plan = {
            "scenes": [
                {
                    "text": "O mar se abre diante do povo.",
                    "image_prompt": "Moses opens the sea at sunset",
                    "caption": "Legenda A",
                }
            ]
        }
        hash_reuse_job = self._create_job(title="Reuso por hash", estimated_cost=6.0)
        hash_before = build_image_cache_key(
            aspect_ratio=hash_reuse_job.aspect_ratio,
            scene_number=1,
            image_prompt=hash_reuse_plan["scenes"][0]["image_prompt"],
            scene_text=hash_reuse_plan["scenes"][0]["text"],
        )
        hash_hydrated = financial_guardian_service.hydrate_plan_with_cached_images(
            self.db,
            job=hash_reuse_job,
            plan=hash_reuse_plan,
        )
        hash_after = hash_hydrated["financial_guardian"]["image_cache_hits"][0]["cache_key"]
        self.assertEqual(hash_before, hash_after)
        self.assertEqual(hash_hydrated["selected_images"], [image_path])
        self._record_result(
            "05_reutilizacao_por_hash",
            "Reutilização por hash",
            {
                "regra": "reutilização por hash",
                "expected_hash": hash_before,
                "reused_hash": hash_after,
                "selected_images": hash_hydrated["selected_images"],
            },
        )

        caption_changed_plan = {
            "scenes": [
                {
                    "text": "O mar se abre diante do povo.",
                    "image_prompt": "Moses opens the sea at sunset",
                    "caption": "Legenda B alterada",
                }
            ]
        }
        caption_job = self._create_job(title="Legenda alterada", estimated_cost=6.0)
        caption_hydrated = financial_guardian_service.hydrate_plan_with_cached_images(
            self.db,
            job=caption_job,
            plan=caption_changed_plan,
        )
        self.assertEqual(caption_hydrated["selected_images"], [image_path])
        self._record_result(
            "06_sem_regenerar_com_legenda_alterada",
            "Não regenerar imagem quando apenas legenda mudar",
            {
                "regra": "não regenerar imagem quando apenas legenda mudar",
                "selected_images": caption_hydrated["selected_images"],
                "caption_original": plan_base["scenes"][0]["caption"],
                "caption_nova": caption_changed_plan["scenes"][0]["caption"],
            },
        )

        fail_job = self._create_job(title="Persistencia falha", estimated_cost=5.0)
        fail_plan = {
            "scenes": [
                {
                    "text": "Abraao observa as estrelas.",
                    "image_prompt": "Abraham looking at the stars in the desert",
                    "caption": "Legenda persistencia",
                }
            ]
        }
        fail_image_path = self._create_image_file("scene-fail.bin", b"fake-image-scene-persist-fail")
        fail_cache_key = build_image_cache_key(
            aspect_ratio=fail_job.aspect_ratio,
            scene_number=1,
            image_prompt=fail_plan["scenes"][0]["image_prompt"],
            scene_text=fail_plan["scenes"][0]["text"],
        )
        financial_guardian_service.cache_images_from_result(
            self.db,
            job=fail_job,
            plan=fail_plan,
            image_paths=[fail_image_path],
        )
        self.db.rollback()
        manifest_entry = financial_guardian_service._find_manifest_entry(
            user_id=self.user.id,
            asset_kind="image",
            cache_key=fail_cache_key,
        )
        db_row_after_rollback = self.db.execute(
            text(
                """
                SELECT COUNT(*) AS total
                FROM codexia_asset_generation_cache
                WHERE user_id = :user_id AND cache_key = :cache_key
                """
            ),
            {"user_id": self.user.id, "cache_key": fail_cache_key},
        ).scalar()
        retry_job = self._create_job(title="Persistencia falha retry", estimated_cost=5.0)
        retry_plan = {
            "scenes": [
                {
                    "text": "Abraao observa as estrelas.",
                    "image_prompt": "Abraham looking at the stars in the desert",
                    "caption": "Legenda alterada no retry",
                }
            ]
        }
        retry_hydrated = financial_guardian_service.hydrate_plan_with_cached_images(
            self.db,
            job=retry_job,
            plan=retry_plan,
        )
        self.assertEqual(db_row_after_rollback, 0)
        self.assertIsNotNone(manifest_entry)
        self.assertEqual(retry_hydrated["selected_images"], [fail_image_path])
        self._record_result(
            "07_sem_regenerar_apos_falha_persistencia",
            "Não regenerar imagem quando persistência falhar",
            {
                "regra": "não regenerar imagem quando persistência falhar",
                "cache_rows_after_rollback": db_row_after_rollback,
                "manifest_entry": manifest_entry,
                "selected_images": retry_hydrated["selected_images"],
            },
        )

        self._record_result(
            "08_auto_recovery_sem_ganho_real",
            "Interrupção automática de Auto Recovery sem ganho real",
            {
                "regra": "interrupção automática de Auto Recovery sem ganho real",
                "decision": recovery_check,
            },
        )

        self._create_metric(job_id=job_ok.id, view_count=5000, retention=46.2, subscribers_gained=25, ctr=4.1, likes=300, comments=42)
        job_report = financial_guardian_service.build_job_financial_report(self.db, job_id=job_ok.id)
        self.assertTrue(job_report["found"])
        self.assertEqual(job_report["estimated_cost"], 6.0)
        self.assertEqual(job_report["actual_cost"], 6.0)
        self._record_result(
            "09_relatorio_financeiro_por_video",
            "Geração do relatório financeiro por vídeo",
            {
                "regra": "geração do relatório financeiro por vídeo",
                "report": job_report,
            },
        )

        daily_report = financial_guardian_service.build_daily_financial_report(self.db, user_id=self.user.id)
        self.assertGreaterEqual(daily_report["jobs_count"], 2)
        self.assertGreaterEqual(daily_report["event_count"], 1)
        self._record_result(
            "10_relatorio_financeiro_diario",
            "Geração do relatório financeiro diário",
            {
                "regra": "geração do relatório financeiro diário",
                "report": daily_report,
            },
        )

        dashboard = financial_guardian_service.build_user_dashboard(self.db, user_id=self.user.id)
        self.assertGreaterEqual(dashboard["efficiency"]["image_cache_assets"], 1)
        self.assertGreaterEqual(dashboard["efficiency"]["image_cache_hits"], 1)
        self._record_result(
            "11_painel_eficiencia",
            "Geração do painel de eficiência",
            {
                "regra": "geração do painel de eficiência",
                "panel": dashboard["efficiency"],
            },
        )

        self.assertGreater(dashboard["roi"]["actual_cost_total"], 0)
        self.assertGreater(dashboard["roi"]["views_total"], 0)
        self.assertGreater(dashboard["roi"]["roi_proxy"], 0)
        self._record_result(
            "12_painel_roi",
            "Geração do painel de ROI",
            {
                "regra": "geração do painel de ROI",
                "panel": dashboard["roi"],
            },
        )

        admin_dashboard = financial_guardian_service.build_admin_dashboard(self.db)
        summary_lines = [
            "# Sprint 03 - Validação Completa do Guardião Financeiro",
            "",
            "Todos os cenários abaixo foram executados com mocks e simulações locais, sem chamadas reais para OpenAI.",
            "",
        ]
        for item in self.results:
            summary_lines.append(f"- OK: {item['title']} -> `{Path(item['path']).name}`")
        summary_lines.extend(
            [
                "",
                "## Painel Admin Simulado",
                "",
                "```json",
                json.dumps(admin_dashboard, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            ]
        )
        (ARTIFACT_DIR / "validation_report.md").write_text("\n".join(summary_lines), encoding="utf-8")

        self.assertGreaterEqual(admin_dashboard["summary"]["preflight_blocked"], 1)
        self.assertGreaterEqual(admin_dashboard["summary"]["estimated_savings"], 0.0)


if __name__ == "__main__":
    unittest.main()
