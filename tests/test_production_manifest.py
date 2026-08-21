from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.services import production_manifest as pm


@pytest.fixture()
def manifest_env(tmp_path, monkeypatch):
    root = tmp_path / "manifests"
    images = tmp_path / "images"
    audio = tmp_path / "audio"
    videos = tmp_path / "videos"
    for path in (root, images, audio, videos):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEXIA_PRODUCTION_MANIFEST_DIR", str(root))
    monkeypatch.setattr(pm, "IMAGES_OUTPUT_DIR", str(images))
    monkeypatch.setattr(pm, "AUDIO_OUTPUT_DIR", str(audio))
    monkeypatch.setattr(pm, "VIDEO_OUTPUT_DIR", str(videos))
    return {"root": root, "images": images, "audio": audio, "videos": videos}


def _script(scene_count: int = 4):
    return {
        "title": "Teste",
        "scenes": [
            {"text": f"Cena {idx + 1}", "image_prompt": f"Imagem {idx + 1}"}
            for idx in range(scene_count)
        ],
    }


def test_manifest_persists_task_assets_and_failure_checkpoint(manifest_env):
    image = manifest_env["images"] / "scene_001.png"
    image.write_bytes(b"x" * 4096)
    snapshot = {
        "task_id": "task-123",
        "status": "processing",
        "progress": 35,
        "message": "3/8 Gerando imagens...",
        "created_at": "2026-08-21T12:00:00+00:00",
        "result": {
            "payload": {"duration": 10, "image_count": 4},
            "script": _script(4),
            "selected_images": [str(image)],
        },
    }
    manifest = pm.sync_task_snapshot("task-123", snapshot)
    assert manifest["task_id"] == "task-123"
    assert manifest["stage"] == "stage_3_images"
    assert manifest["expected_image_count"] == 4
    assert any(a["kind"] == "image" and a["exists"] for a in manifest["artifacts"])
    durable = next(a["durable_path"] for a in manifest["artifacts"] if a["kind"] == "image")
    assert os.path.isfile(durable)

    failed = dict(snapshot)
    failed.update({"status": "failed", "progress": 89, "message": "Falha no controle final de qualidade"})
    manifest2 = pm.sync_task_snapshot("task-123", failed)
    assert manifest2["status"] == "failed"
    assert manifest2["progress"] == 89
    assert len(manifest2["checkpoints"]) >= 2
    assert os.path.isfile(durable)


def test_filesystem_checkpoint_only_claims_new_files(manifest_env):
    old = manifest_env["images"] / "old.png"
    old.write_bytes(b"o" * 4096)
    os.utime(old, (1, 1))
    base = {
        "task_id": "task-new",
        "status": "pending",
        "progress": 0,
        "message": "Aguardando início...",
        "result": {"payload": {"duration": 5}, "script": _script(2)},
    }
    first = pm.sync_task_snapshot("task-new", base)
    assert not any(Path(a.get("original_path", "")).name == "old.png" for a in first["artifacts"])

    fresh = manifest_env["images"] / "fresh.png"
    fresh.write_bytes(b"f" * 4096)
    second_snapshot = dict(base)
    second_snapshot.update({"status": "processing", "progress": 30, "message": "Gerando imagem (router)..."})
    second = pm.sync_task_snapshot("task-new", second_snapshot)
    assert any(Path(a.get("original_path", "")).name == "fresh.png" for a in second["artifacts"])


def test_partial_recovery_requires_second_resume_confirmation(manifest_env, monkeypatch):
    audio = manifest_env["audio"] / "voice.mp3"
    audio.write_bytes(b"a" * 4096)
    image = manifest_env["images"] / "scene_001.png"
    image.write_bytes(b"i" * 4096)
    snapshot = {
        "task_id": "task-recovery",
        "status": "paused",
        "progress": 40,
        "message": "Produção pausada; ativos preservados.",
        "result": {
            "payload": {"duration": 10, "production_mode": "balanced"},
            "script": _script(4),
            "selected_images": [str(image)],
            "audio_checkpoint": {
                "output_path": str(audio),
                "duration_seconds": 600.0,
            },
        },
    }
    pm.sync_task_snapshot("task-recovery", snapshot)
    monkeypatch.setattr(pm, "_probe_duration", lambda path: 600.0 if str(path).endswith("voice.mp3") else 0.0)
    monkeypatch.setattr(pm, "_image_cost_estimate", lambda missing, duration, mode: (0.12 * missing, 0.62 * missing))

    plan = pm.build_recovery_plan("task-recovery")
    assert plan["action"] == "regenerate_missing_images"
    assert plan["script_ok"] is True
    assert plan["audio_ok"] is True
    assert plan["valid_image_count"] == 1
    assert plan["expected_image_count"] == 4
    assert plan["missing_image_count"] == 3

    first = pm.confirm_or_prepare_partial_recovery("task-recovery", {"duration": 10})
    assert first["allow"] is False
    assert first["reason"] == "confirmation_required"
    message = pm.recovery_confirmation_message(first["plan"])
    assert "clique Retomar novamente" in message
    assert "Nenhuma chamada paga foi feita ainda" in message

    second = pm.confirm_or_prepare_partial_recovery("task-recovery", {"duration": 10})
    assert second["allow"] is True
    patched = second["payload"]
    assert patched["_recovery_generate_missing_images_only"] is True
    assert patched["_recovery_missing_image_count"] == 3
    assert patched["reuse_audio_from"]["output_path"].endswith("voice.mp3")
    assert len(patched["selected_images"]) == 1
    assert isinstance(patched["seeded_script"], dict)


def test_no_paid_confirmation_when_script_or_audio_missing(manifest_env):
    snapshot = {
        "task_id": "task-blocked",
        "status": "failed",
        "progress": 20,
        "message": "Falha antes da mídia.",
        "result": {"payload": {"duration": 10}},
    }
    pm.sync_task_snapshot("task-blocked", snapshot)
    plan = pm.build_recovery_plan("task-blocked")
    assert plan["action"] == "blocked"
    decision = pm.confirm_or_prepare_partial_recovery("task-blocked", {"duration": 10})
    assert decision["allow"] is False
    assert decision["reason"] == "blocked"
