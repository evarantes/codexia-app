"""Local integration smoke test for the story/devotional scheduling pipeline.

The test spends nothing on external providers. It does require the same
``ffmpeg``/``ffprobe`` system dependencies used by the production container and
creates a genuinely playable video/audio MP4 fixture at runtime.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEMP_ROOT = Path(tempfile.mkdtemp(prefix="codexia-real-smoke-"))
SQLITE_PATH = TEMP_ROOT / "smoke.sqlite3"
os.environ["APP_ENV"] = "development"
os.environ["ENABLE_SQLITE_DEV"] = "true"
os.environ["SQLITE_DB_PATH"] = str(SQLITE_PATH)
os.environ["SECRET_KEY"] = "codexia-smoke-only-secret"
os.environ["VIDEO_TASK_STALE_MINUTES"] = "30"
os.environ["YOUTUBE_CONTENT_REUSE_WINDOW_HOURS"] = "48"


def _require_media_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise RuntimeError(
            "Smoke real exige dependências do container de produção: " + ", ".join(missing)
        )


def _create_real_mp4(path: Path, duration_seconds: float = 3.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=640x360:rate=24:duration={duration_seconds}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=44100:duration={duration_seconds}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-b:v",
        "900k",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou: {str(completed.stderr or '')[-1000:]}")


def _create_large_corrupt_file(path: Path, size_bytes: int = 5 * 1024 * 1024) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41")
        handle.write(b"\x00" * max(0, size_bytes - handle.tell()))


def _force_task_stale(db, video_task_model, task_id: str):
    row = db.query(video_task_model).filter(video_task_model.id == task_id).first()
    assert row is not None, f"task não encontrada: {task_id}"
    stale_time = (datetime.now(UTC) - timedelta(minutes=35)).replace(tzinfo=None)
    row.created_at = stale_time
    row.updated_at = stale_time
    db.commit()
    db.refresh(row)
    return row


def main() -> int:
    _require_media_tools()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import Base, SessionLocal, engine
    from app.models import ScheduledVideo, VideoTask
    from app.routers.youtube import (
        _cleanup_story_video_task_queue,
        _find_reusable_completed_task_by_content,
        _payload_content_hash,
        router as youtube_router,
    )
    from app.services.media_probe import media_durations_match, probe_media_file
    from app.services.task_manager import (
        _ensure_task_support_tables,
        create_task,
        get_task,
        update_task,
    )

    Base.metadata.create_all(bind=engine)
    _ensure_task_support_tables()

    real_mp4 = TEMP_ROOT / "real-video-with-audio.mp4"
    corrupt_mp4 = TEMP_ROOT / "large-but-corrupt.mp4"
    _create_real_mp4(real_mp4)
    _create_large_corrupt_file(corrupt_mp4)

    real_probe = probe_media_file(str(real_mp4))
    corrupt_probe = probe_media_file(str(corrupt_mp4))
    assert real_probe["ok"], real_probe
    assert real_probe["video_stream"] and real_probe["audio_stream"], real_probe
    assert media_durations_match(real_probe), real_probe
    assert not corrupt_probe["ok"], corrupt_probe
    print("[OK] ffprobe aceitou MP4 reproduzível com áudio e rejeitou arquivo grande corrompido")

    with patch("app.services.media_probe.shutil.which", return_value=None):
        unavailable_probe = probe_media_file(str(real_mp4))
    assert not unavailable_probe["ok"]
    assert unavailable_probe["error"] == "ffprobe_not_available"
    print("[OK] ausência de ffprobe reprova em modo fail-closed")

    db = SessionLocal()
    try:
        db.query(ScheduledVideo).delete(synchronize_session=False)
        db.commit()

        task_id = create_task(
            initial_status="processing",
            progress=0,
            message="Início",
            result={"kind": "youtube_story_video", "payload": {"mode": "story"}},
        )
        first_update = get_task(task_id).get("updated_at")
        time.sleep(1.05)
        update_task(task_id, progress=0, message="Início")
        second_update = get_task(task_id).get("updated_at")
        assert first_update != second_update
        print("[OK] update_task renova updated_at mesmo sem alterar os demais valores")

        payload = {
            "override_title": "Smoke real de recuperação",
            "story_content": "Conteúdo local sem chamada a provedores externos.",
            "duration": 1,
            "kind": "devotional",
            "mode": "story",
        }
        content_hash = _payload_content_hash(payload)
        completed_id = create_task(
            initial_status="completed",
            progress=100,
            message="Concluída",
            result={
                "kind": "youtube_story_video",
                "payload": payload,
                "content_hash": content_hash,
                "video_url": "/static/videos/real-video-with-audio.mp4",
                "file_path": str(real_mp4),
                "final_validation": {
                    "ok": True,
                    "checks": {
                        "video_stream": real_probe["video_stream"],
                        "audio_stream": real_probe["audio_stream"],
                        "duration_valid": media_durations_match(real_probe),
                    },
                },
            },
        )
        reused = _find_reusable_completed_task_by_content(db, payload)
        assert reused and reused["task_id"] == completed_id, reused
        print("[OK] dedupe reutilizou somente artefato confirmado pelo ffprobe")

        api = FastAPI()
        api.include_router(youtube_router)
        request_payload = {
            "video_url": "/static/videos/real-video-with-audio.mp4",
            "video_path": str(real_mp4),
            "task_id": completed_id,
            "title": "Smoke real de idempotência",
            "description": "Três chamadas HTTP reais; nenhum insert manual de fallback.",
            "kind": "devotional",
        }
        with TestClient(api) as client:
            responses = [
                client.post("/youtube/schedule/from_generated", json=request_payload)
                for _ in range(3)
            ]
        assert [response.status_code for response in responses] == [200, 200, 200], [
            response.text for response in responses
        ]
        response_data = [response.json() for response in responses]
        assert response_data[0]["reused_existing"] is False, response_data
        assert response_data[1]["reused_existing"] is True, response_data
        assert response_data[2]["reused_existing"] is True, response_data
        db.expire_all()
        assert db.query(ScheduledVideo).count() == 1
        print("[OK] endpoint HTTP real chamado 3x criou exatamente 1 ScheduledVideo")

        stale_valid_id = create_task(
            initial_status="processing",
            progress=86,
            message="6/8 Renderizando...",
            result={
                "kind": "youtube_story_video",
                "payload": payload,
                "file_path": str(real_mp4),
                "video_path": str(real_mp4),
                "video_url": "/static/videos/real-video-with-audio.mp4",
            },
        )
        valid_row = _force_task_stale(db, VideoTask, stale_valid_id)
        valid_cleanup = _cleanup_story_video_task_queue(db, rows=[valid_row])
        valid_after = get_task(stale_valid_id)
        assert valid_after["status"] == "completed", valid_cleanup
        assert valid_after["result"]["final_validation"]["recovered"] is True
        print("[OK] watchdog recuperou tarefa somente após validação real do MP4")

        corrupt_payload = {**payload, "override_title": "Arquivo corrompido"}
        stale_corrupt_id = create_task(
            initial_status="processing",
            progress=86,
            message="6/8 Renderizando...",
            result={
                "kind": "youtube_story_video",
                "payload": corrupt_payload,
                "file_path": str(corrupt_mp4),
                "video_path": str(corrupt_mp4),
                "video_url": "/static/videos/large-but-corrupt.mp4",
            },
        )
        corrupt_row = _force_task_stale(db, VideoTask, stale_corrupt_id)
        corrupt_cleanup = _cleanup_story_video_task_queue(db, rows=[corrupt_row])
        corrupt_after = get_task(stale_corrupt_id)
        assert corrupt_after["status"] == "failed", corrupt_cleanup
        assert _find_reusable_completed_task_by_content(db, corrupt_payload) is None

        with TestClient(api) as client:
            rejected = client.post(
                "/youtube/schedule/from_generated",
                json={
                    "video_url": "/static/videos/large-but-corrupt.mp4",
                    "video_path": str(corrupt_mp4),
                    "task_id": stale_corrupt_id,
                    "title": "Não deve agendar",
                },
            )
        assert rejected.status_code == 422, rejected.text
        db.expire_all()
        assert db.query(ScheduledVideo).count() == 1
        print("[OK] arquivo corrompido de 5 MB falhou no watchdog e foi bloqueado pelo endpoint")

        print("\nTODOS OS CHECKS REAIS PASSARAM — sem IA/TTS pagos e sem fallback por tamanho")
        return 0
    finally:
        db.close()
        engine.dispose()
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
