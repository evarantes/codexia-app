import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ENABLE_SQLITE_DEV", "true")
os.environ.setdefault("APP_ENV", "development")
if os.environ.get("DATABASE_URL", "").startswith("postgresql://"):
    del os.environ["DATABASE_URL"]

from app.services.video_resource_guard import (
    evaluate_series_video_resources,
    resource_guard_message,
    series_video_resource_requirements,
)


_ENV_NAMES = {
    "SERIES_RESOURCE_GUARD_ENABLED": "",
    "SERIES_MIN_TOTAL_MEMORY_MB": "",
    "SERIES_MIN_AVAILABLE_MEMORY_MB": "",
    "SERIES_MIN_FREE_DISK_GB": "",
    "SERIES_MAX_SWAP_USED_PERCENT": "",
    "SERIES_MAX_LOAD_PER_CPU": "",
}


def _snapshot(**overrides):
    base = {
        "total_memory_mb": 8192.0,
        "available_memory_mb": 5120.0,
        "swap_total_mb": 2048.0,
        "swap_used_percent": 10.0,
        "disk_path": "/data",
        "disk_free_gb": 40.0,
        "disk_used_percent": 50.0,
        "load_1m": 1.0,
        "cpu_count": 4,
        "load_per_cpu": 0.25,
    }
    base.update(overrides)
    return base


class SeriesVideoResourceGuardTests(unittest.TestCase):
    def test_ten_minute_video_is_blocked_on_four_gb_vps(self):
        with patch.dict(os.environ, _ENV_NAMES, clear=False):
            report = evaluate_series_video_resources(
                10,
                snapshot=_snapshot(
                    total_memory_mb=3814.0,
                    available_memory_mb=2048.0,
                    swap_used_percent=75.0,
                    disk_free_gb=14.0,
                ),
            )

        self.assertFalse(report["allowed"])
        joined = " ".join(report["reasons"])
        self.assertIn("RAM total insuficiente", joined)
        self.assertIn("Memória disponível insuficiente", joined)
        self.assertIn("Espaço livre insuficiente", joined)

    def test_ten_minute_video_passes_with_safe_margin(self):
        with patch.dict(os.environ, _ENV_NAMES, clear=False):
            report = evaluate_series_video_resources(10, snapshot=_snapshot())

        self.assertTrue(report["allowed"])
        self.assertEqual(report["reasons"], [])
        self.assertEqual(report["requirements"]["min_total_memory_mb"], 6144.0)
        self.assertEqual(report["requirements"]["min_free_disk_gb"], 15.0)

    def test_guard_can_be_explicitly_disabled_for_controlled_environment(self):
        env = dict(_ENV_NAMES)
        env["SERIES_RESOURCE_GUARD_ENABLED"] = "false"
        with patch.dict(os.environ, env, clear=False):
            report = evaluate_series_video_resources(
                10,
                snapshot=_snapshot(
                    total_memory_mb=512.0,
                    available_memory_mb=64.0,
                    swap_used_percent=99.0,
                    disk_free_gb=0.1,
                    load_per_cpu=99.0,
                ),
            )

        self.assertFalse(report["enabled"])
        self.assertTrue(report["allowed"])

    def test_duration_changes_requirements_without_touching_pipeline(self):
        with patch.dict(os.environ, _ENV_NAMES, clear=False):
            short = series_video_resource_requirements(2)
            long = series_video_resource_requirements(10)

        self.assertEqual(short["min_total_memory_mb"], 0.0)
        self.assertGreater(long["min_available_memory_mb"], short["min_available_memory_mb"])
        self.assertGreater(long["min_free_disk_gb"], short["min_free_disk_gb"])

    def test_block_message_explains_that_series_is_preserved(self):
        message = resource_guard_message({"reasons": ["RAM insuficiente."]})
        self.assertIn("aguardando recursos seguros", message)
        self.assertIn("permanece preservada", message)


class SeriesVideoExecutorProtectionTests(unittest.TestCase):
    def test_series_payload_is_detected_from_canonical_fields(self):
        from app.routers import youtube

        self.assertTrue(youtube._is_youtube_series_payload({"source_module": "youtube_series"}))
        self.assertTrue(youtube._is_youtube_series_payload({"series_context": {"series_id": 3}}))
        self.assertFalse(youtube._is_youtube_series_payload({"source_module": "story"}))

    def test_blocked_preflight_does_not_start_any_executor(self):
        from app.routers import youtube

        payload = {
            "source_module": "youtube_series",
            "series_context": {"series_id": 3},
            "duration": 10,
        }
        with patch.object(youtube, "_maybe_enable_render_only_flags", return_value=payload), \
             patch.object(youtube, "_series_resource_preflight", return_value={"allowed": False}), \
             patch.object(youtube, "_start_isolated_video_generation") as isolated, \
             patch.object(youtube.threading, "Thread") as thread:
            youtube._dispatch_video_generation_task(payload, "task-protected")

        isolated.assert_not_called()
        thread.assert_not_called()

    def test_series_uses_isolated_process_even_without_redis(self):
        from app.routers import youtube

        payload = {
            "source_module": "youtube_series",
            "series_context": {"series_id": 3},
            "duration": 5,
        }
        with patch.dict(
            os.environ,
            {
                "USE_RQ_FOR_VIDEO_GENERATION": "false",
                "VIDEO_GENERATION_EXECUTOR": "thread",
            },
            clear=False,
        ), patch.object(youtube, "conn", None), \
             patch.object(youtube, "_maybe_enable_render_only_flags", return_value=payload), \
             patch.object(youtube, "_series_resource_preflight", return_value={"allowed": True}), \
             patch.object(youtube, "_start_isolated_video_generation", return_value=True) as isolated, \
             patch.object(youtube.threading, "Thread") as thread:
            youtube._dispatch_video_generation_task(payload, "task-isolated")

        isolated.assert_called_once_with(payload, "task-isolated")
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
