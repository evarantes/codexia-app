import tempfile
import unittest

from app.services.ai_router import AIOperationBlocked, classify_openai_image_error
from app.services.video_generator import VideoGenerator


class _FakeImageService:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def generate_image(self, *_args, **_kwargs):
        self.calls += 1
        raise self.error


class OpenAIImageCostGuardTests(unittest.TestCase):
    def test_insufficient_quota_is_actionable_and_non_retryable(self):
        failure = classify_openai_image_error(
            Exception("Error code: 429 - {'error': {'code': 'insufficient_quota'}}"),
            model="gpt-image-1-mini",
        )

        self.assertEqual(failure.code, "OPENAI_NO_CREDIT")
        self.assertFalse(failure.retryable)
        self.assertIn("créditos", failure.action_required)
        self.assertIn("billing", failure.billing_url)

    def test_auth_model_rate_limit_and_policy_have_distinct_codes(self):
        cases = [
            (Exception("401 invalid_api_key"), "OPENAI_AUTH_ERROR", False),
            (Exception("model_not_found"), "OPENAI_MODEL_UNAVAILABLE", False),
            (Exception("Your organization must be verified to use this model"), "OPENAI_ORG_VERIFICATION_REQUIRED", False),
            (Exception("429 rate_limit_exceeded"), "OPENAI_RATE_LIMIT", True),
            (Exception("content_policy_violation"), "OPENAI_CONTENT_POLICY", False),
        ]
        for error, expected_code, retryable in cases:
            with self.subTest(expected_code=expected_code):
                failure = classify_openai_image_error(error, model="gpt-image-1-mini")
                self.assertEqual(failure.code, expected_code)
                self.assertEqual(failure.retryable, retryable)

    def test_scene_generation_makes_only_one_paid_call_and_preserves_error(self):
        expected = AIOperationBlocked(
            "OPENAI_NO_CREDIT",
            "OpenAI sem saldo/quota para gerar imagens.",
            provider="openai",
            retryable=False,
            action_required="Adicionar créditos.",
            model="gpt-image-1-mini",
        )
        fake = _FakeImageService(expected)
        with tempfile.TemporaryDirectory(prefix="openai-image-guard-") as tmp:
            generator = VideoGenerator(output_dir=tmp, ai_service=fake)
            with self.assertRaises(AIOperationBlocked) as raised:
                generator._ensure_image_for_scene(
                    "Cinematic sunrise",
                    "Uma nova manhã",
                    max_rounds=4,
                    allow_non_ai_fallback=False,
                )

        self.assertEqual(fake.calls, 1)
        self.assertIs(raised.exception, expected)
        self.assertEqual(raised.exception.code, "OPENAI_NO_CREDIT")


if __name__ == "__main__":
    unittest.main()
