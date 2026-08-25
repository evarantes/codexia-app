from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import VIDEO_OUTPUT_DIR
from app.database import SessionLocal
from app.models import VideoTask
from app.services.task_manager import (
    acquire_task_execution_lease,
    finalize_task_once,
    get_task,
    get_task_execution_lease,
    heartbeat_task_execution_lease,
    merge_task_result,
    release_task_execution_lease,
    update_task,
)

router = APIRouter(prefix="/local-worker/v1", tags=["Local Render Worker"])

_TOKEN_ENV = "CODEXIA_LOCAL_WORKER_TOKEN"
_LEASE_TTL_SECONDS = 180
_MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024
_HEARTBEATS: Dict[str, Dict[str, Any]] = {}


class HeartbeatRequest(BaseModel):
    worker_id: str = Field(min_length=3, max_length=120)
    version: str = Field(default="phase1", max_length=64)
    inventory: Dict[str, Any] = Field(default_factory=dict)
    busy_task_id: Optional[str] = None


class LeaseRequest(BaseModel):
    worker_id: str = Field(min_length=3, max_length=120)


class LeaseHeartbeatRequest(BaseModel):
    worker_id: str = Field(min_length=3, max_length=120)


class FailureRequest(BaseModel):
    worker_id: str = Field(min_length=3, max_length=120)
    error: str = Field(min_length=1, max_length=2000)


def _require_worker_token(authorization: Optional[str] = Header(default=None)) -> None:
    expected = str(os.getenv(_TOKEN_ENV) or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Local worker desabilitado: token não configurado.")
    supplied = str(authorization or "").strip()
    prefix = "Bearer "
    if not supplied.startswith(prefix):
        raise HTTPException(status_code=401, detail="Token do worker ausente.")
    candidate = supplied[len(prefix):].strip()
    if not candidate or not hmac.compare_digest(candidate, expected):
        raise HTTPException(status_code=401, detail="Token do worker inválido.")


def _safe_existing_file(path_value: Any) -> Optional[str]:
    raw = str(path_value or "").strip()
    if not raw:
        return None
    try:
        resolved = str(Path(raw).resolve(strict=True))
    except Exception:
        return None
    return resolved if os.path.isfile(resolved) and os.path.getsize(resolved) > 0 else None


def _task_result(task: Dict[str, Any]) -> Dict[str, Any]:
    result = task.get("result") if isinstance(task, dict) else None
    return result if isinstance(result, dict) else {}


def _eligible_payload(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    # Fail closed: the normal server queue never becomes local-worker work by accident.
    if not bool(payload.get("local_render_worker_allowed")):
        return None
    if not bool(payload.get("force_render_only")):
        return None
    if not bool(payload.get("force_reuse_assets")):
        return None
    if bool(payload.get("auto_upload")):
        return None
    return payload


def _collect_assets(task_id: str, result: Dict[str, Any], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    assets: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add(kind: str, value: Any, media_type: str) -> None:
        path = _safe_existing_file(value)
        if not path or path in seen:
            return
        seen.add(path)
        assets.append({
            "index": len(assets),
            "kind": kind,
            "filename": os.path.basename(path),
            "media_type": media_type,
            "bytes": os.path.getsize(path),
            "sha256": _sha256_file(path),
            "_path": path,
        })

    seeded = payload.get("seeded_script") if isinstance(payload.get("seeded_script"), dict) else {}
    for source in (payload.get("selected_images"), seeded.get("selected_images"), result.get("selected_images")):
        if isinstance(source, list):
            for value in source:
                add("image", value, "image/*")

    audio_sources = [payload.get("reuse_audio_from"), result.get("audio_checkpoint")]
    report = result.get("render_report") if isinstance(result.get("render_report"), dict) else {}
    audio_sources.append(report.get("audio_generation"))
    for item in audio_sources:
        if not isinstance(item, dict):
            continue
        for key in ("output_path", "final_audio_path", "audio_path"):
            if item.get(key):
                add("audio", item.get(key), "audio/*")
                break

    for source in (payload, seeded, result):
        if not isinstance(source, dict):
            continue
        for key in ("srt_path", "caption_srt_path", "subtitles_path"):
            if source.get(key):
                add("captions", source.get(key), "application/x-subrip")
                break

    return assets


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(task_id: str, worker_id: str) -> Dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada.")
    result = _task_result(task)
    payload = _eligible_payload(result)
    if payload is None:
        raise HTTPException(status_code=409, detail="Tarefa não autorizada para render local.")
    assets = _collect_assets(task_id, result, payload)
    images = [a for a in assets if a["kind"] == "image"]
    audio = [a for a in assets if a["kind"] == "audio"]
    if not images or not audio:
        raise HTTPException(status_code=409, detail="Ativos preservados insuficientes para render local.")
    public_assets = []
    for asset in assets:
        item = {k: v for k, v in asset.items() if k != "_path"}
        item["download_url"] = f"/local-worker/v1/tasks/{task_id}/assets/{asset['index']}"
        public_assets.append(item)
    return {
        "task_id": task_id,
        "worker_id": worker_id,
        "mode": "render_only",
        "publish": False,
        "paid_media_calls_allowed": False,
        "regenerate_images": False,
        "regenerate_tts": False,
        "preserve_full_text": True,
        "preserve_full_narration": True,
        "assets": public_assets,
        "render": {
            "width": int(payload.get("local_render_width") or 1280),
            "height": int(payload.get("local_render_height") or 720),
            "fps": int(payload.get("local_render_fps") or 30),
            "ffmpeg_threads": min(2, max(1, int(payload.get("local_render_threads") or 2))),
            "video_codec_preference": ["h264_qsv", "libx264"],
        },
    }


@router.post("/heartbeat", dependencies=[Depends(_require_worker_token)])
def worker_heartbeat(body: HeartbeatRequest):
    now = time.time()
    _HEARTBEATS[body.worker_id] = {
        "worker_id": body.worker_id,
        "version": body.version,
        "inventory": body.inventory,
        "busy_task_id": body.busy_task_id,
        "last_seen_epoch": now,
    }
    if body.busy_task_id:
        heartbeat_task_execution_lease(body.busy_task_id, body.worker_id, ttl_seconds=_LEASE_TTL_SECONDS)
    return {"ok": True, "server_epoch": now, "lease_ttl_seconds": _LEASE_TTL_SECONDS}


@router.post("/lease", dependencies=[Depends(_require_worker_token)])
def lease_next_task(body: LeaseRequest):
    # Phase 1 deliberately claims only explicitly opted-in render-only jobs.
    db = SessionLocal()
    try:
        rows = (
            db.query(VideoTask)
            .filter(VideoTask.status.in_(["pending", "processing", "cancelled", "failed"]))
            .order_by(VideoTask.updated_at.asc(), VideoTask.created_at.asc())
            .limit(100)
            .all()
        )
        candidate_ids = [str(row.id) for row in rows]
    finally:
        db.close()

    for task_id in candidate_ids:
        task = get_task(task_id)
        result = _task_result(task or {})
        if _eligible_payload(result) is None:
            continue
        lease = acquire_task_execution_lease(task_id, body.worker_id, ttl_seconds=_LEASE_TTL_SECONDS)
        if not bool(lease.get("acquired")):
            continue
        try:
            manifest = _manifest(task_id, body.worker_id)
        except Exception:
            release_task_execution_lease(task_id, body.worker_id)
            continue
        update_task(task_id, status="processing", progress=max(1, int((task or {}).get("progress") or 1)), message=f"Render local reservado para {body.worker_id}.")
        merge_task_result(task_id, {"local_worker": {"worker_id": body.worker_id, "lease": lease, "phase": "leased"}})
        return {"leased": True, "lease": lease, "manifest": manifest}
    return {"leased": False}


@router.post("/tasks/{task_id}/heartbeat", dependencies=[Depends(_require_worker_token)])
def task_heartbeat(task_id: str, body: LeaseHeartbeatRequest):
    ok = heartbeat_task_execution_lease(task_id, body.worker_id, ttl_seconds=_LEASE_TTL_SECONDS)
    if not ok:
        raise HTTPException(status_code=409, detail="Lease inválido ou expirado.")
    merge_task_result(task_id, {"local_worker": {"worker_id": body.worker_id, "phase": "rendering", "heartbeat_at": time.time()}})
    return {"ok": True}


@router.get("/tasks/{task_id}/assets/{asset_index}", dependencies=[Depends(_require_worker_token)])
def download_task_asset(task_id: str, asset_index: int, x_worker_id: Optional[str] = Header(default=None)):
    worker_id = str(x_worker_id or "").strip()
    lease = get_task_execution_lease(task_id)
    if not worker_id or not lease or str(lease.get("executor_id") or "") != worker_id:
        raise HTTPException(status_code=403, detail="Worker não possui lease desta tarefa.")
    task = get_task(task_id)
    result = _task_result(task or {})
    payload = _eligible_payload(result)
    if payload is None:
        raise HTTPException(status_code=409, detail="Tarefa não autorizada para render local.")
    assets = _collect_assets(task_id, result, payload)
    if asset_index < 0 or asset_index >= len(assets):
        raise HTTPException(status_code=404, detail="Ativo não encontrado.")
    asset = assets[asset_index]
    return FileResponse(asset["_path"], filename=asset["filename"], media_type=asset["media_type"])


@router.post("/tasks/{task_id}/complete", dependencies=[Depends(_require_worker_token)])
async def complete_task(task_id: str, worker_id: str, file: UploadFile = File(...)):
    lease = get_task_execution_lease(task_id)
    if not lease or str(lease.get("executor_id") or "") != str(worker_id or ""):
        raise HTTPException(status_code=409, detail="Lease inválido ou pertencente a outro executor.")
    filename = os.path.basename(str(file.filename or "render.mp4"))
    if not filename.lower().endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Somente MP4 é aceito.")
    output_dir = Path(VIDEO_OUTPUT_DIR or "app/static/videos").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    final_name = f"local_{task_id}_{uuid.uuid4().hex[:10]}.mp4"
    temp_path = output_dir / f".{final_name}.upload"
    final_path = output_dir / final_name
    total = 0
    try:
        with open(temp_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="MP4 excede o limite de upload.")
                out.write(chunk)
        if total < 1024:
            raise HTTPException(status_code=400, detail="MP4 final vazio ou inválido.")
        os.replace(temp_path, final_path)
        result = _task_result(get_task(task_id) or {})
        result = dict(result)
        result["video_url"] = f"/media/videos/{final_name}"
        result["file_path"] = str(final_path)
        result["local_worker"] = {
            "worker_id": worker_id,
            "phase": "completed",
            "bytes": total,
            "sha256": _sha256_file(str(final_path)),
            "published": False,
            "paid_media_calls": 0,
        }
        finalized = finalize_task_once(task_id, status="completed", progress=100, message="Render local concluído; aguardando revisão/publicação manual.", result=result)
        return {"ok": True, "task_id": task_id, "video_url": result["video_url"], "finalized": finalized.get("finalized_now", False)}
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass
        release_task_execution_lease(task_id, worker_id)


@router.post("/tasks/{task_id}/failed", dependencies=[Depends(_require_worker_token)])
def fail_task(task_id: str, body: FailureRequest):
    lease = get_task_execution_lease(task_id)
    if not lease or str(lease.get("executor_id") or "") != body.worker_id:
        raise HTTPException(status_code=409, detail="Lease inválido ou pertencente a outro executor.")
    merge_task_result(task_id, {"local_worker": {"worker_id": body.worker_id, "phase": "failed", "error": body.error}})
    update_task(task_id, status="failed", message=f"Render local falhou: {body.error[:500]}")
    release_task_execution_lease(task_id, body.worker_id)
    return {"ok": True}
