import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.worker_release_health import (
    WORKER_RELEASE_HEALTH_VERSION,
    WORKER_RELEASE_KEY,
    current_release_commit,
    publish_worker_release,
    read_worker_release,
    worker_release_health_snapshot,
)


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def setex(self, key, ttl, value):
        self.values[key] = value
        self.ttls[key] = ttl

    def get(self, key):
        value = self.values.get(key)
        return value.encode("utf-8") if isinstance(value, str) else value


class WorkerReleaseHealthTests(unittest.TestCase):
    def test_release_commit_uses_same_environment_precedence_as_api(self):
        with patch.dict(
            os.environ,
            {
                "RENDER_GIT_COMMIT": "render-sha",
                "SOURCE_COMMIT": "source-sha",
                "COMMIT_SHA": "commit-sha",
                "GIT_COMMIT": "git-sha",
            },
            clear=False,
        ):
            self.assertEqual(current_release_commit(), "render-sha")

    def test_publish_and_read_round_trip_with_short_lived_key(self):
        redis = _FakeRedis()
        payload = publish_worker_release(
            redis,
            commit="abc123",
            started_at="2026-09-08T00:00:00+00:00",
            ttl_seconds=90,
        )

        self.assertEqual(redis.ttls[WORKER_RELEASE_KEY], 90)
        self.assertEqual(payload["commit"], "abc123")
        self.assertEqual(read_worker_release(redis)["commit"], "abc123")
        self.assertEqual(
            json.loads(redis.values[WORKER_RELEASE_KEY])["protocol_version"],
            WORKER_RELEASE_HEALTH_VERSION,
        )

    def test_health_snapshot_requires_exact_nonempty_match(self):
        ok = worker_release_health_snapshot(
            app_commit="same-sha",
            worker_payload={
                "commit": "same-sha",
                "last_seen_at": "now",
                "started_at": "before",
                "protocol_version": WORKER_RELEASE_HEALTH_VERSION,
            },
        )
        mismatch = worker_release_health_snapshot(
            app_commit="new-sha",
            worker_payload={"commit": "old-sha"},
        )
        unavailable = worker_release_health_snapshot(
            app_commit="new-sha",
            worker_payload={},
        )

        self.assertEqual(ok["status"], "ok")
        self.assertTrue(ok["same_version"])
        self.assertEqual(mismatch["status"], "version_mismatch")
        self.assertFalse(mismatch["same_version"])
        self.assertEqual(unavailable["status"], "worker_unavailable")
        self.assertFalse(unavailable["online"])

    def test_runtime_wires_worker_heartbeat_and_public_health_route(self):
        root = Path(__file__).resolve().parents[1]
        worker = (root / "app/worker.py").read_text(encoding="utf-8")
        main = (root / "app/main.py").read_text(encoding="utf-8")

        self.assertIn("worker_conn = create_rq_worker_connection()", worker)
        self.assertIn("start_worker_release_heartbeat(worker_conn)", worker)
        self.assertIn('@app.get("/health/worker")', main)
        self.assertIn("worker_release_health_snapshot", main)


if __name__ == "__main__":
    unittest.main()
