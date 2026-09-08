import os
import unittest
from unittest.mock import patch

from app import redis_client


class _FakeRedisConnection:
    def __init__(self):
        self.ping_calls = 0

    def ping(self):
        self.ping_calls += 1
        return True


class RQWorkerConnectionTests(unittest.TestCase):
    def test_worker_socket_timeout_exceeds_rq_default_dequeue_timeout(self):
        fake = _FakeRedisConnection()
        with patch.dict(
            os.environ,
            {
                "REDIS_URL": "redis://queue.example:6379/0",
                "REDIS_SOCKET_TIMEOUT_SECONDS": "10",
            },
            clear=False,
        ), patch.object(redis_client.redis, "from_url", return_value=fake) as from_url:
            connection = redis_client.create_rq_worker_connection()

        self.assertIs(connection, fake)
        self.assertEqual(fake.ping_calls, 1)
        self.assertEqual(from_url.call_args.args[0], "redis://queue.example:6379/0")
        self.assertGreater(from_url.call_args.kwargs["socket_timeout"], 405)
        self.assertEqual(from_url.call_args.kwargs["socket_timeout"], 500)

    def test_worker_timeout_has_safe_lower_bound(self):
        fake = _FakeRedisConnection()
        with patch.dict(
            os.environ,
            {"RQ_WORKER_REDIS_SOCKET_TIMEOUT_SECONDS": "30"},
            clear=False,
        ), patch.object(redis_client.redis, "from_url", return_value=fake) as from_url:
            redis_client.create_rq_worker_connection()

        self.assertEqual(from_url.call_args.kwargs["socket_timeout"], 420)


if __name__ == "__main__":
    unittest.main()
