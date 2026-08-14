import os
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ.setdefault("ENABLE_SQLITE_DEV", "true")
os.environ.setdefault("APP_ENV", "development")
if os.environ.get("DATABASE_URL", "").startswith("postgresql://"):
    del os.environ["DATABASE_URL"]

from app.services.video_resource_guard import (
    evaluate_runtime_resource_health,
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


class VideoRuntimeResourceHealthTests(unittest.TestCase):
    def test_runtime_health_reports_normal_resources(self):
        report = evaluate_runtime_resource_health(_snapshot())

        self.assertEqual(report["level"], "ok")
        self.assertEqual(report["reasons"], [])

    def test_runtime_health_warns_without_blocking_pipeline(self):
        report = evaluate_runtime_resource_health(
            _snapshot(available_memory_mb=700.0, swap_used_percent=88.0)
        )

        self.assertEqual(report["level"], "warning")
        self.assertIn("Pouca memória", " ".join(report["reasons"]))
        self.assertNotIn("allowed", report)

    def test_runtime_health_marks_critical_pressure(self):
        report = evaluate_runtime_resource_health(
            _snapshot(available_memory_mb=300.0, swap_used_percent=97.0, disk_free_gb=1.0)
        )

        self.assertEqual(report["level"], "critical")
        joined = " ".join(report["reasons"])
        self.assertIn("Memória disponível crítica", joined)
        self.assertIn("Swap em nível crítico", joined)
        self.assertIn("Espaço livre crítico", joined)


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
                "ALLOW_INLINE_VIDEO_GENERATION": "true",
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

    def test_regular_ten_minute_video_is_also_isolated_from_web_process(self):
        from app.routers import youtube

        payload = {
            "source_module": "story",
            "mode": "story",
            "duration": 10,
        }
        with patch.dict(
            os.environ,
            {
                "USE_RQ_FOR_VIDEO_GENERATION": "false",
                "ALLOW_INLINE_VIDEO_GENERATION": "true",
                "VIDEO_GENERATION_EXECUTOR": "thread",
                "VIDEO_ISOLATED_PROCESS_MINUTES": "5",
            },
            clear=False,
        ), patch.object(youtube, "conn", None), \
             patch.object(youtube, "_maybe_enable_render_only_flags", return_value=payload), \
             patch.object(youtube, "_series_resource_preflight", return_value=None), \
             patch.object(youtube, "_start_isolated_video_generation", return_value=True) as isolated, \
             patch.object(youtube.threading, "Thread") as thread:
            youtube._dispatch_video_generation_task(payload, "task-long-regular")

        isolated.assert_called_once_with(payload, "task-long-regular")
        thread.assert_not_called()

    def test_short_regular_video_keeps_existing_lightweight_executor(self):
        from app.routers import youtube

        payload = {"source_module": "story", "mode": "story", "duration": 2}
        with patch.dict(
            os.environ,
            {
                "USE_RQ_FOR_VIDEO_GENERATION": "false",
                "ALLOW_INLINE_VIDEO_GENERATION": "true",
                "VIDEO_GENERATION_EXECUTOR": "thread",
                "VIDEO_ISOLATED_PROCESS_MINUTES": "5",
            },
            clear=False,
        ), patch.object(youtube, "conn", None), \
             patch.object(youtube, "_maybe_enable_render_only_flags", return_value=payload), \
             patch.object(youtube, "_series_resource_preflight", return_value=None), \
             patch.object(youtube, "_start_isolated_video_generation") as isolated, \
             patch.object(youtube, "update_task"), \
             patch.object(youtube.threading, "Thread") as thread:
            youtube._dispatch_video_generation_task(payload, "task-short-regular")

        isolated.assert_not_called()
        thread.assert_called_once()


class VideoRuntimeTelemetryTests(unittest.TestCase):
    def test_runtime_view_distinguishes_active_from_interrupted(self):
        from app.routers import youtube

        now = datetime.utcnow()
        base = {
            "task_id": "task-runtime",
            "status": "processing",
            "progress": 48,
            "message": "3/8 Preparando imagens",
            "updated_at": now.isoformat(),
            "result": {
                "pipeline_stage": "stage_3_images",
                "runtime_telemetry": {
                    "heartbeat_at": now.isoformat(),
                    "stage_changed_at": (now - timedelta(minutes=2)).isoformat(),
                    "resource_health": {"level": "ok", "snapshot": _snapshot()},
                },
            },
        }
        with patch.object(youtube, "get_task_execution_lease", return_value=None):
            active = youtube._runtime_view_for_task(base)
            interrupted_task = dict(base)
            interrupted_task["result"] = dict(base["result"])
            interrupted_task["result"]["runtime_telemetry"] = dict(base["result"]["runtime_telemetry"])
            interrupted_task["result"]["runtime_telemetry"]["heartbeat_at"] = (now - timedelta(minutes=3)).isoformat()
            interrupted = youtube._runtime_view_for_task(interrupted_task)

        self.assertEqual(active["state"], "working")
        self.assertEqual(interrupted["state"], "possibly_interrupted")
        self.assertGreaterEqual(active["stage_unchanged_seconds"], 119)

    def test_runtime_monitor_persists_heartbeat_and_resource_snapshot(self):
        from app.routers import youtube

        updated = threading.Event()
        written = []
        task = {
            "task_id": "task-monitor",
            "status": "processing",
            "progress": 48,
            "message": "3/8 Preparando imagens",
            "result": {"pipeline_stage": "stage_3_images"},
        }

        def _record_update(_task_id, result_patch=None, **kwargs):
            written.append({"result": result_patch or kwargs.get("result") or {}})
            updated.set()

        with patch.object(youtube, "get_task", return_value=task), \
             patch.object(youtube, "merge_task_result", side_effect=_record_update), \
             patch.object(youtube, "heartbeat_task_execution_lease", return_value=True), \
             patch("app.services.video_resource_guard.capture_resource_snapshot", return_value=_snapshot()), \
             patch.dict(os.environ, {"VIDEO_RUNTIME_HEARTBEAT_SECONDS": "5"}, clear=False):
            stop, thread = youtube._start_video_runtime_monitor("task-monitor", "executor-test")
            self.assertTrue(updated.wait(1.5))
            stop.set()
            thread.join(timeout=1.5)

        self.assertTrue(written)
        telemetry = written[0]["result"]["runtime_telemetry"]
        self.assertEqual(telemetry["stage"], "stage_3_images")
        self.assertEqual(telemetry["resource_health"]["level"], "ok")
        self.assertEqual(telemetry["executor_id"], "executor-test")

    def test_legacy_task_with_expired_executor_becomes_recoverable(self):
        from app.routers import youtube

        now = datetime.utcnow()
        legacy = {
            "task_id": "task-legacy-oom",
            "status": "processing",
            "progress": 48,
            "message": "3/8 Preparando imagens e cenas...",
            "created_at": (now - timedelta(hours=8)).isoformat(),
            "updated_at": (now - timedelta(minutes=20)).isoformat(),
            "executor_heartbeat_at": (now - timedelta(minutes=20)).isoformat(),
            "result": {"pipeline_stage": "stage_3_images"},
        }
        failed = dict(legacy)
        failed["status"] = "failed"
        failed["message"] = "A execução antiga perdeu o executor."

        with patch.object(youtube, "get_task", side_effect=[legacy, failed]), \
             patch.object(youtube, "update_task") as update, \
             patch.object(youtube, "get_task_execution_lease", return_value=None):
            response = youtube.get_task_status("task-legacy-oom")

        update.assert_called_once()
        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["runtime"]["state"], "finished")


if __name__ == "__main__":
    unittest.main()
