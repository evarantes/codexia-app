import json
import unittest
from unittest.mock import patch

from app.models import VideoTask
from app.services import task_manager


class _RetryQuery:
    def __init__(self, owner):
        self.owner = owner

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        self.owner.query_attempts += 1
        if self.owner.fail_queries > 0:
            self.owner.fail_queries -= 1
            raise RuntimeError("simulated aborted PostgreSQL transaction")
        return self.owner.row


class _RetryDb:
    def __init__(self, row=None, fail_queries=0):
        self.row = row
        self.fail_queries = int(fail_queries)
        self.query_attempts = 0
        self.rollback_calls = 0
        self.added = []
        self.commits = 0
        self.closed = False
        self.is_active = True

    def query(self, _model):
        return _RetryQuery(self)

    def rollback(self):
        self.rollback_calls += 1

    def execute(self, *_args, **_kwargs):
        return None

    def add(self, row):
        self.added.append(row)
        self.row = row

    def flush(self):
        return None

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class TaskRecoveryTransactionBoundaryTests(unittest.TestCase):
    def _patch_task_side_effects(self):
        return (
            patch.object(task_manager, "_ensure_task_support_tables"),
            patch.object(task_manager, "_sync_task_aux_state"),
            patch.object(task_manager, "_task_aux_meta", return_value={}),
            patch.object(task_manager, "_redis_set"),
            patch.object(task_manager, "_control_set"),
        )

    def test_reset_retries_after_aborted_task_lookup(self):
        row = VideoTask(
            id="task-retry",
            status="failed",
            progress=42,
            message="falha preservada",
            result_json=json.dumps({"payload": {"mode": "story"}}),
        )
        db = _RetryDb(row=row, fail_queries=1)
        patches = self._patch_task_side_effects()
        with (
            patch.object(task_manager, "SessionLocal", return_value=db),
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
        ):
            recovered = task_manager.reset_task_for_retry(
                "task-retry",
                progress=43,
                message="recuperação preparada",
            )

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["task_id"], "task-retry")
        self.assertEqual(recovered["status"], "processing")
        self.assertEqual(recovered["progress"], 43)
        self.assertEqual(db.query_attempts, 2)
        self.assertGreaterEqual(db.rollback_calls, 1)
        self.assertEqual(db.commits, 1)

    def test_reset_repairs_missing_row_from_same_task_snapshot(self):
        db = _RetryDb(row=None)
        snapshot = {
            "task_id": "task-ghost",
            "status": "failed",
            "progress": 78,
            "message": "falha preservada",
            "result": {
                "payload": {"mode": "story", "story_content": "Roteiro preservado"},
                "script": {"scenes": [{"text": "Cena preservada"}]},
            },
        }
        patches = self._patch_task_side_effects()
        with (
            patch.object(task_manager, "SessionLocal", return_value=db),
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
        ):
            recovered = task_manager.reset_task_for_retry(
                "task-ghost",
                progress=79,
                message="recuperação preparada",
                snapshot=snapshot,
            )

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["task_id"], "task-ghost")
        self.assertEqual(recovered["status"], "processing")
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.added[0].id, "task-ghost")
        persisted = json.loads(db.added[0].result_json)
        self.assertEqual(persisted["payload"]["story_content"], "Roteiro preservado")
        self.assertEqual(db.commits, 1)


if __name__ == "__main__":
    unittest.main()
