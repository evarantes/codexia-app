import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.ai_router import AICapability, AIOperationBlocked, AIPolicy, AIRouter


class _FakeResult:
    def fetchone(self):
        return None


class _FakeSession:
    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.invalid = False
        self.closed = False

    def commit(self):
        self.events.append("commit")
        self.invalid = False

    def rollback(self):
        self.events.append("rollback")
        self.invalid = False

    def execute(self, *_args, **_kwargs):
        if self.invalid:
            raise RuntimeError("session transaction was not rolled back")
        return _FakeResult()

    def close(self):
        self.closed = True
        self.events.append("close")


def _fake_settings():
    return SimpleNamespace(
        openai_api_key="sk-test",
        openai_no_credit=False,
        ai_cb_failure_threshold=3,
        ai_cb_cooldown_seconds=300,
        ai_cb_half_open_max_attempts=1,
    )


def _fake_policy():
    return AIPolicy(
        capability=AICapability.IMAGE_GENERATION,
        primary_provider="openai",
        primary_model="gpt-image-2",
        fallback_enabled=False,
        fallback_provider=None,
        fallback_model=None,
        cache_enabled=False,
        estimated_cost=0.005,
        max_cost=None,
        is_active=True,
    )


def _prepare_router(router, session, events):
    router._get_settings = lambda *_args, **_kwargs: _fake_settings()
    router._load_policy = lambda *_args, **_kwargs: _fake_policy()
    router._openai_capability_allowed = lambda *_args, **_kwargs: True
    router._cb_allow_provider = lambda *_args, **_kwargs: True
    router._should_simulate_failure = lambda *_args, **_kwargs: False
    router._claim_operation = lambda *_args, **_kwargs: events.append("claim")
    router._dry_run_enabled = lambda: False
    return session


class AIRouterImageTransactionTests(unittest.TestCase):
    def test_operation_is_committed_before_paid_provider_request(self):
        events = []
        session = _FakeSession(events)
        router = AIRouter()
        _prepare_router(router, session, events)

        def provider_call(**_kwargs):
            events.append("provider")
            return b"png-image-bytes"

        def record_success(db, **_kwargs):
            events.append("persist_success")
            db.commit()

        router._call_openai_image = provider_call
        router._record_openai_image_success = record_success

        with tempfile.TemporaryDirectory(prefix="image-router-txn-") as output_dir:
            with patch("app.services.ai_router.SessionLocal", return_value=session):
                path = router.generate_image(
                    user_id=7,
                    task_id="task-transaction-1",
                    video_id=None,
                    capability=AICapability.IMAGE_GENERATION,
                    prompt="Cinematic sunrise",
                    output_dir=output_dir,
                )

        self.assertTrue(path.endswith(".png"))
        self.assertLess(events.index("commit"), events.index("provider"))
        self.assertLess(events.index("provider"), events.index("persist_success"))

    def test_provider_failure_rolls_back_before_recording_failure(self):
        events = []
        session = _FakeSession(events)
        router = AIRouter()
        _prepare_router(router, session, events)

        def provider_failure(**_kwargs):
            events.append("provider")
            session.invalid = True
            raise RuntimeError("429 rate_limit_exceeded")

        def record_failure(db, **kwargs):
            self.assertFalse(db.invalid)
            events.append("persist_failure")

        router._call_openai_image = provider_failure
        router._fail_operation = record_failure
        router._cb_on_failure = lambda *_args, **_kwargs: events.append("record_circuit_failure")

        with tempfile.TemporaryDirectory(prefix="image-router-failure-") as output_dir:
            with patch("app.services.ai_router.SessionLocal", return_value=session):
                with self.assertRaises(AIOperationBlocked) as raised:
                    router.generate_image(
                        user_id=7,
                        task_id="task-transaction-2",
                        video_id=None,
                        capability=AICapability.IMAGE_GENERATION,
                        prompt="Cinematic sunrise",
                        output_dir=output_dir,
                    )

        self.assertEqual(raised.exception.code, "OPENAI_RATE_LIMIT")
        self.assertLess(events.index("rollback"), events.index("persist_failure"))
        self.assertIn("record_circuit_failure", events)

    def test_success_persistence_recovers_with_a_fresh_session(self):
        events = []
        first_session = _FakeSession(events)
        recovery_session = _FakeSession(events)
        router = AIRouter()
        _prepare_router(router, first_session, events)
        router._call_openai_image = lambda **_kwargs: b"png-image-bytes"

        def record_success(db, **_kwargs):
            if db is first_session:
                raise RuntimeError("invalid transaction")
            events.append("recovered_success")
            db.commit()

        router._record_openai_image_success = record_success

        with tempfile.TemporaryDirectory(prefix="image-router-recovery-") as output_dir:
            with patch(
                "app.services.ai_router.SessionLocal",
                side_effect=[first_session, recovery_session],
            ):
                path = router.generate_image(
                    user_id=7,
                    task_id="task-transaction-3",
                    video_id=None,
                    capability=AICapability.IMAGE_GENERATION,
                    prompt="Cinematic sunrise",
                    output_dir=output_dir,
                )

        self.assertTrue(path.endswith(".png"))
        self.assertIn("recovered_success", events)
        self.assertTrue(recovery_session.closed)


if __name__ == "__main__":
    unittest.main()
