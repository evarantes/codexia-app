import os
import tempfile
from pathlib import Path
import unittest


os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("ENABLE_SQLITE_DEV", "1")
_tmp_dir = Path(tempfile.mkdtemp(prefix="codexia-fg-tests-"))
_db_path = (_tmp_dir / "financial_guardian.sqlite").resolve()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_db_path}")

from app.database import Base, engine  # noqa: E402
from app.models import Settings  # noqa: E402

Base.metadata.create_all(engine)

from app.services.ai_generator import AIContentGenerator  # noqa: E402
from app.services.financial_guardian_service import (  # noqa: E402
    build_image_cache_key,
    evaluate_budget_guard,
    evaluate_recovery_loop,
)


class FinancialGuardianServiceTests(unittest.TestCase):
    def test_budget_guard_allows_when_within_limits(self):
        decision = evaluate_budget_guard(
            estimated_cost=3.5,
            spent_today=4.0,
            spent_month=12.0,
            per_video_limit=5.0,
            daily_limit=10.0,
            monthly_limit=20.0,
        )

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["status"], "allowed")
        self.assertEqual(decision["projected_today"], 7.5)
        self.assertEqual(decision["projected_month"], 15.5)

    def test_budget_guard_blocks_multiple_limit_breaches(self):
        decision = evaluate_budget_guard(
            estimated_cost=7.0,
            spent_today=5.0,
            spent_month=19.0,
            per_video_limit=6.0,
            daily_limit=10.0,
            monthly_limit=25.0,
        )

        self.assertFalse(decision["allowed"])
        self.assertIn("limite por vídeo", decision["reason"])
        self.assertIn("limite diário", decision["reason"])
        self.assertIn("limite mensal", decision["reason"])

    def test_recovery_loop_blocks_when_score_delta_is_too_small(self):
        decision = evaluate_recovery_loop(
            stage="captions_render",
            attempt_number=2,
            before_score=89.0,
            after_score=89.4,
            min_score_delta=1.0,
            max_attempts=3,
        )

        self.assertTrue(decision["stop"])
        self.assertEqual(decision["score_delta"], 0.4)
        self.assertIn("abaixo do mínimo", decision["reason"])

    def test_recovery_loop_allows_when_improvement_is_real(self):
        decision = evaluate_recovery_loop(
            stage="pronunciation_tts_render",
            attempt_number=1,
            before_score=86.0,
            after_score=88.0,
            min_score_delta=1.0,
            max_attempts=2,
        )

        self.assertFalse(decision["stop"])
        self.assertEqual(decision["score_delta"], 2.0)

    def test_image_cache_key_is_stable_and_sensitive_to_prompt(self):
        key_a = build_image_cache_key(
            aspect_ratio="16:9",
            scene_number=1,
            image_prompt="Moses opens the sea",
            scene_text="The sea opens before the people.",
        )
        key_b = build_image_cache_key(
            aspect_ratio="16:9",
            scene_number=1,
            image_prompt="Moses opens the sea",
            scene_text="The sea opens before the people.",
        )
        key_c = build_image_cache_key(
            aspect_ratio="16:9",
            scene_number=1,
            image_prompt="Moses crosses the desert",
            scene_text="The sea opens before the people.",
        )

        self.assertEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_c)

    def test_no_paid_mode_skips_premium_tts_providers(self):
        previous_disable = os.environ.get("CODEXIA_DISABLE_PAID_AI")
        previous_openai = os.environ.get("OPENAI_API_KEY")
        try:
            os.environ["CODEXIA_DISABLE_PAID_AI"] = "1"
            os.environ["OPENAI_API_KEY"] = "sk-test"
            generator = AIContentGenerator()
            diagnostics = generator.generate_audio_with_diagnostics("texto de teste", preferred_provider="openai_tts")

            self.assertIsNone(diagnostics["provider_used"])
            self.assertEqual(diagnostics["error_summary"], "Nenhum provider premium conseguiu gerar audio.")
            self.assertTrue(any("Modo sem consumo pago ativo" in (attempt.get("reason") or "") for attempt in diagnostics["attempts"]))
        finally:
            if previous_disable is None:
                os.environ.pop("CODEXIA_DISABLE_PAID_AI", None)
            else:
                os.environ["CODEXIA_DISABLE_PAID_AI"] = previous_disable
            if previous_openai is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = previous_openai

    def test_no_paid_mode_blocks_paid_image_generation(self):
        previous_disable = os.environ.get("CODEXIA_DISABLE_PAID_AI")
        try:
            os.environ["CODEXIA_DISABLE_PAID_AI"] = "1"
            generator = AIContentGenerator()
            with self.assertRaises(Exception) as ctx:
                generator.generate_image("Moses opens the sea", aspect_ratio="16:9")
            self.assertIn("Modo sem consumo pago ativo", str(ctx.exception))
        finally:
            if previous_disable is None:
                os.environ.pop("CODEXIA_DISABLE_PAID_AI", None)
            else:
                os.environ["CODEXIA_DISABLE_PAID_AI"] = previous_disable


if __name__ == "__main__":
    unittest.main()
