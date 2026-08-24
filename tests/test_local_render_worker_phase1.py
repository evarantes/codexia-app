from pathlib import Path
import unittest

from local_worker.agent import LocalRenderAgent, WorkerError


ROOT = Path(__file__).resolve().parents[1]


class LocalRenderWorkerPhase1Tests(unittest.TestCase):
    def test_agent_rejects_plain_http(self):
        with self.assertRaisesRegex(WorkerError, "HTTPS"):
            LocalRenderAgent(base_url="http://127.0.0.1:8000", token="x" * 32)

    def test_agent_requires_dedicated_nontrivial_token(self):
        with self.assertRaisesRegex(WorkerError, "Token"):
            LocalRenderAgent(base_url="https://codexia.example", token="short")

    def test_agent_has_no_database_or_redis_client_imports(self):
        source = (ROOT / "local_worker" / "agent.py").read_text(encoding="utf-8")
        forbidden = (
            "psycopg",
            "psycopg2",
            "sqlalchemy",
            "redis.Redis",
            "from app.database",
            "from app.redis_client",
        )
        for token in forbidden:
            self.assertNotIn(token, source)

    def test_agent_is_outbound_only_and_has_no_listener(self):
        source = (ROOT / "local_worker" / "agent.py").read_text(encoding="utf-8")
        forbidden = ("socket.bind(", ".listen(", "HTTPServer(", "uvicorn.run(", "Flask(")
        for token in forbidden:
            self.assertNotIn(token, source)
        self.assertIn("requests.Session()", source)
        self.assertIn("https://", source)

    def test_agent_never_calls_paid_media_or_publish_endpoints(self):
        source = (ROOT / "local_worker" / "agent.py").read_text(encoding="utf-8").lower()
        forbidden_paths = (
            "/images/generations",
            "openai.images",
            "text-to-speech",
            "elevenlabs",
            "suno",
            "/youtube/upload",
            "/publish",
        )
        for token in forbidden_paths:
            self.assertNotIn(token, source)

    def test_server_fails_closed_without_explicit_local_worker_opt_in(self):
        from app.routers.local_render_worker import _eligible_payload

        base = {"force_render_only": True, "force_reuse_assets": True, "auto_upload": False}
        self.assertIsNone(_eligible_payload({"payload": base}))
        allowed = dict(base, local_render_worker_allowed=True)
        self.assertEqual(_eligible_payload({"payload": allowed}), allowed)
        self.assertIsNone(_eligible_payload({"payload": dict(allowed, auto_upload=True)}))

    def test_server_uses_existing_execution_lease_and_never_exposes_arbitrary_paths(self):
        source = (ROOT / "app" / "routers" / "local_render_worker.py").read_text(encoding="utf-8")
        self.assertIn("acquire_task_execution_lease", source)
        self.assertIn("heartbeat_task_execution_lease", source)
        self.assertIn("release_task_execution_lease", source)
        self.assertIn('"_path"', source)
        self.assertIn('if k != "_path"', source)
        self.assertIn("asset_index", source)

    def test_one_video_at_a_time_and_resource_limits_are_hard_coded(self):
        source = (ROOT / "local_worker" / "agent.py").read_text(encoding="utf-8")
        self.assertIn("self._single_job_lock = threading.Lock()", source)
        self.assertIn("self._single_job_lock.acquire(blocking=False)", source)
        self.assertIn("max_ram_percent", source)
        self.assertIn("min_free_disk_gb", source)
        self.assertIn("min(2", source)

    def test_hardened_build_wires_router(self):
        source = (ROOT / "scripts" / "apply_ready_video_asset_repair_v3.py").read_text(encoding="utf-8")
        patcher = (ROOT / "scripts" / "apply_local_render_worker_phase1.py").read_text(encoding="utf-8")
        self.assertIn("local_worker_phase1.apply()", source)
        self.assertIn("local_worker_phase1.check()", source)
        self.assertIn("app.include_router(local_render_worker.router)", patcher)


if __name__ == "__main__":
    unittest.main()
