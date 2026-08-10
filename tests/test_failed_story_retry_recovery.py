import inspect
import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.routers.youtube import (
    _dispatch_task_result,
    _load_latest_recoverable_story_video_task,
    retry_task,
)


class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.rows


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self.rows)


class FailedStoryRetryRecoveryTests(unittest.TestCase):
    def test_latest_failed_story_with_payload_is_recoverable(self):
        payload = {"mode": "story", "story_content": "Texto", "duration": 3}
        row = SimpleNamespace(
            id="task-1",
            status="failed",
            progress=78,
            message="Falha de validação",
            result_json=json.dumps({"kind": "youtube_story_video", "payload": payload}),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        self.assertIs(_load_latest_recoverable_story_video_task(_FakeDb([row])), row)

    def test_dispatch_metadata_preserves_expensive_assets(self):
        prior = {
            "payload": {"mode": "story"},
            "script": {"scenes": [{"text": "Cena"}]},
            "render_report": {"audio_generation": {"output_path": "/data/audio.mp3"}},
        }
        with patch("app.routers.youtube.get_task", return_value={"result": prior}):
            merged = _dispatch_task_result("task-1", {"mode": "story", "force_reuse_assets": True}, "thread")
        self.assertEqual(merged["script"], prior["script"])
        self.assertEqual(merged["render_report"], prior["render_report"])
        self.assertTrue(merged["payload"]["force_reuse_assets"])

    def test_retry_uses_canonical_dispatch_and_not_raw_legacy_thread(self):
        source = inspect.getsource(retry_task)
        self.assertIn("_dispatch_video_generation_task(payload, task_id)", source)
        self.assertIn('"pipeline": "unified_video_pipeline"', source)
        self.assertNotIn("threading.Thread(target=process_video_generation", source)

    def test_frontend_keeps_failed_task_link_and_blocks_duplicate_generate(self):
        html = (Path(__file__).parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8")
        poll_story = html.split("async pollStoryTask(taskId)", 1)[1].split("async generateStoryShorts", 1)[0]
        failed_branch = poll_story.split("if (status === 'failed') {", 1)[1].split("if (status === 'cancelled')", 1)[0]
        self.assertIn("localStorage.setItem('ytStoryTaskId'", failed_branch)
        self.assertIn("this.ytStoryTaskId = String(taskId)", failed_branch)
        self.assertNotIn("localStorage.removeItem('ytStoryTaskId')", failed_branch)
        self.assertIn("Esta solicitação pode ser recuperada", html)


if __name__ == "__main__":
    unittest.main()
