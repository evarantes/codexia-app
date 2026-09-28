import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services import unified_video_pipeline as uvp


class _Db:
    def __init__(self):
        self.rollback_calls = 0

    def rollback(self):
        self.rollback_calls += 1


class SubmitTransactionBoundaryTests(unittest.TestCase):
    def test_submit_retries_once_after_aborted_transaction(self):
        service = object.__new__(uvp.UnifiedVideoPipelineService)
        db = _Db()
        request = SimpleNamespace(idempotency_key="test:submit-retry")
        expected = object()
        calls = []

        def submit_once(*_args, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("current transaction is aborted, commands ignored")
            return expected

        with (
            patch.object(uvp, "acquire_distributed_lock", return_value={}),
            patch.object(uvp, "release_distributed_lock"),
            patch.object(service, "_submit_or_reuse_locked", side_effect=submit_once),
        ):
            result = service.submit_or_reuse(db, request=request)

        self.assertIs(result, expected)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(db.rollback_calls, 1)


if __name__ == "__main__":
    unittest.main()
