import inspect
import unittest

import app
import app.redis_client as redis_client
import app.tasks as worker_tasks


class CX33QueueFailClosedTests(unittest.TestCase):
    def test_unavailable_queue_never_executes_function_inline(self):
        queue = redis_client.UnavailableQueue("redis timeout")
        called = {"value": False}

        def expensive_job():
            called["value"] = True

        with self.assertRaises(redis_client.QueueUnavailableError):
            queue.enqueue(expensive_job)
        self.assertFalse(called["value"])

    def test_production_inline_fallback_is_explicitly_forbidden(self):
        source = inspect.getsource(redis_client._inline_fallback_allowed)
        self.assertIn('app_env in {"production", "prod"}', source)
        self.assertIn("return False", source)

    def test_redis_timeouts_are_not_hardcoded_to_one_second(self):
        source = inspect.getsource(redis_client)
        self.assertIn("REDIS_CONNECT_TIMEOUT_SECONDS", source)
        self.assertIn("REDIS_SOCKET_TIMEOUT_SECONDS", source)
        self.assertNotIn("socket_timeout=1, socket_connect_timeout=1", source)

    def test_factory_lock_uses_short_renewable_lease(self):
        source = inspect.getsource(worker_tasks)
        self.assertIn("VIDEO_FACTORY_LOCK_TTL_SECONDS", source)
        self.assertIn("lock.extend(ttl_seconds, replace_ttl=True)", source)
        self.assertNotIn("timeout=4 * 60 * 60", source)

    def test_retry_failure_does_not_fallback_to_inline_execution(self):
        source = inspect.getsource(worker_tasks.process_job_task)
        self.assertIn("queue.enqueue_in", source)
        self.assertIn("execução inline bloqueada", source)
        self.assertNotIn("factory.process_job(job)\n                    return", source)

    def test_video_task_terminal_status_mapping(self):
        self.assertEqual(app._canonical_status_for_video_task("failed"), "failed")
        self.assertEqual(app._canonical_status_for_video_task("cancelled"), "cancelled")
        self.assertEqual(app._canonical_status_for_video_task("pending"), "queued")
        self.assertEqual(app._canonical_status_for_video_task("processing"), "processing")
        self.assertEqual(app._canonical_status_for_video_task("completed"), "completed")

    def test_state_sync_updates_unified_row_by_task_id(self):
        source = inspect.getsource(app._sync_video_task_state_to_unified)
        self.assertIn("UPDATE unified_videos", source)
        self.assertIn("WHERE task_id = :task_id", source)
        self.assertIn("last_error", source)
        self.assertIn("progress = :progress", source)


if __name__ == "__main__":
    unittest.main()
