from __future__ import annotations

import unittest

from scripts import apply_stale_factory_lock_recovery as compat


class _FakeRedis:
    def __init__(self, ttl_value: int, eval_result: int = 1):
        self.ttl_value = int(ttl_value)
        self.eval_result = int(eval_result)
        self.eval_calls = []

    def ttl(self, key):
        return self.ttl_value

    def eval(self, script, key_count, key, recoverable_ttl):
        self.eval_calls.append((script, key_count, key, int(recoverable_ttl)))
        return self.eval_result


class StaleFactoryLockRecoveryTests(unittest.TestCase):
    def _runtime(self, *, ttl: int, live_work: bool, eval_result: int = 1):
        redis = _FakeRedis(ttl, eval_result=eval_result)
        ns = {
            "conn": redis,
            "FACTORY_LOCK_KEY": "codexia:video_factory:single_worker_lock",
            "_cancel_all_active": lambda: False,
            "RQ_AVAILABLE": False,
            "Worker": None,
        }
        exec(compat.HELPERS, ns)
        # Isola a decisão de remoção: os testes abaixo validam a política de TTL
        # e o fail-closed sem precisar de PostgreSQL/RQ reais.
        ns["_factory_lock_has_live_work"] = lambda: bool(live_work)
        return ns, redis

    def test_patch_is_idempotent_and_compiles(self):
        source = compat.YOUTUBE.read_text(encoding="utf-8")
        first = compat.patch_youtube(source)
        second = compat.patch_youtube(first)
        self.assertEqual(first, second)
        self.assertIn(compat.MARKER, first)
        self.assertIn("def _factory_lock_has_live_work()", first)
        self.assertIn("def _recover_stale_factory_lock_if_safe()", first)
        self.assertIn("stale_factory_lock_recovered = _recover_stale_factory_lock_if_safe()", first)
        self.assertIn("if not _recover_stale_factory_lock_if_safe():", first)
        compile(first, str(compat.YOUTUBE), "exec")

    def test_fresh_lock_is_never_deleted(self):
        ns, redis = self._runtime(ttl=(4 * 60 * 60) - 60, live_work=False)
        self.assertFalse(ns["_recover_stale_factory_lock_if_safe"]())
        self.assertEqual(redis.eval_calls, [])

    def test_old_lock_with_live_work_is_never_deleted(self):
        ns, redis = self._runtime(ttl=8_000, live_work=True)
        self.assertFalse(ns["_recover_stale_factory_lock_if_safe"]())
        self.assertEqual(redis.eval_calls, [])

    def test_old_orphan_lock_can_be_deleted_atomically(self):
        ns, redis = self._runtime(ttl=8_000, live_work=False, eval_result=1)
        self.assertTrue(ns["_recover_stale_factory_lock_if_safe"]())
        self.assertEqual(len(redis.eval_calls), 1)
        _, key_count, key, threshold = redis.eval_calls[0]
        self.assertEqual(key_count, 1)
        self.assertEqual(key, "codexia:video_factory:single_worker_lock")
        self.assertEqual(threshold, (4 * 60 * 60) - (10 * 60))

    def test_atomic_delete_failure_keeps_lock_busy(self):
        ns, redis = self._runtime(ttl=8_000, live_work=False, eval_result=0)
        self.assertFalse(ns["_recover_stale_factory_lock_if_safe"]())
        self.assertEqual(len(redis.eval_calls), 1)

    def test_guard_is_fail_closed_on_db_and_rq_errors(self):
        self.assertIn("except Exception:\n        # Em dúvida, preserve o lock.\n        return True", compat.HELPERS)
        self.assertIn("Se não conseguimos provar que os workers estão ociosos, preserve", compat.HELPERS)
        self.assertIn("if _factory_lock_has_live_work():\n        return False", compat.HELPERS)


if __name__ == "__main__":
    unittest.main()
