from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.config import (
    AUDIO_OUTPUT_DIR,
    IMAGES_OUTPUT_DIR,
    VIDEO_OUTPUT_DIR,
    absolute_path_for_audio,
    absolute_path_for_image,
    absolute_path_for_video,
)


_LOCK = threading.RLock()
_SCHEMA_VERSION = 1
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
_SCRIPT_KEYS = {"script", "seeded_script", "storyboard", "editorial_script"}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_task_id(task_id: Any) -> str:
    raw = str(task_id or "").strip()
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)[:180]


def _root_dir() -> Path:
    configured = str(os.getenv("CODEXIA_PRODUCTION_MANIFEST_DIR") or "").strip()
    if configured:
        root = Path(configured)
    elif os.path.isdir("/data"):
        root = Path("/data/media/production_manifests")
    else:
        root = Path("app/static/production_manifests")
    root.mkdir(parents=True, exist_ok=True)
    return root


def manifest_dir(task_id: Any) -> Path:
    safe = _safe_task_id(task_id)
    if not safe:
        raise ValueError("task_id inválido para manifesto")
    path = _root_dir() / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def manifest_path(task_id: Any) -> Path:
    return manifest_dir(task_id) / "manifest.json"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if not path.is_file():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def load_manifest(task_id: Any) -> Dict[str, Any]:
    with _LOCK:
        return _read_json(manifest_path(task_id))


def _parse_iso_epoch(value: Any) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return float(dt.timestamp())
    except Exception:
        return 0.0


def _extension(value: str) -> str:
    clean = str(value or "").split("?", 1)[0].split("#", 1)[0]
    return Path(clean).suffix.lower()


def _kind_for_value(value: str, key_hint: str = "") -> str:
    ext = _extension(value)
    hint = str(key_hint or "").lower()
    if ext in _IMAGE_EXTS or "image" in hint or "visual" in hint or "frame" in hint:
        return "image"
    if ext in _AUDIO_EXTS or "audio" in hint or "voice" in hint or "tts" in hint:
        return "audio"
    if ext in _VIDEO_EXTS or "video" in hint or "render" in hint:
        return "video"
    return "other"


def _resolve_existing_path(value: Any, kind: str = "") -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw_no_query = raw.split("?", 1)[0].split("#", 1)[0]
    if raw_no_query.startswith(("http://", "https://")):
        raw_no_query = "/" + raw_no_query.split("/", 3)[-1] if raw_no_query.count("/") >= 3 else raw_no_query
    try:
        if os.path.isfile(raw_no_query):
            return os.path.abspath(raw_no_query)
    except Exception:
        pass
    resolvers = []
    if kind == "image":
        resolvers = [absolute_path_for_image]
    elif kind == "audio":
        resolvers = [absolute_path_for_audio]
    elif kind == "video":
        resolvers = [absolute_path_for_video]
    else:
        resolvers = [absolute_path_for_image, absolute_path_for_audio, absolute_path_for_video]
    for resolver in resolvers:
        try:
            candidate = str(resolver(raw_no_query) or "").strip()
        except Exception:
            candidate = ""
        try:
            if candidate and os.path.isfile(candidate):
                return os.path.abspath(candidate)
        except Exception:
            continue
    return ""


def _sample_hash(path: str) -> str:
    try:
        size = os.path.getsize(path)
        h = hashlib.sha256()
        h.update(str(size).encode("ascii"))
        with open(path, "rb") as fh:
            if size <= 16 * 1024 * 1024:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
            else:
                h.update(fh.read(1024 * 1024))
                fh.seek(max(0, size - 1024 * 1024))
                h.update(fh.read(1024 * 1024))
        return h.hexdigest()
    except Exception:
        return ""


def _durable_copy(task_id: str, source: str, kind: str) -> str:
    try:
        source_path = Path(source)
        if not source_path.is_file():
            return ""
        assets_dir = manifest_dir(task_id) / "assets" / kind
        assets_dir.mkdir(parents=True, exist_ok=True)
        name_hash = hashlib.sha1(str(source_path).encode("utf-8")).hexdigest()[:10]
        target = assets_dir / f"{name_hash}_{source_path.name}"
        if target.is_file() and target.stat().st_size == source_path.stat().st_size:
            return str(target)
        try:
            os.link(source_path, target)
        except FileExistsError:
            pass
        except Exception:
            # Copiar imagens/áudio é barato. Para vídeo grande, preferimos manter
            # a referência original quando hardlink não for possível, evitando
            # duplicar muitos GB sem necessidade.
            if kind != "video" or source_path.stat().st_size <= 256 * 1024 * 1024:
                shutil.copy2(source_path, target)
            else:
                return str(source_path)
        return str(target if target.is_file() else source_path)
    except Exception:
        return source


def _artifact_entry(task_id: str, path: str, kind: str, source: str) -> Dict[str, Any]:
    durable = _durable_copy(task_id, path, kind)
    chosen = durable if durable and os.path.isfile(durable) else path
    try:
        size = int(os.path.getsize(chosen) or 0)
    except Exception:
        size = 0
    try:
        mtime = float(os.path.getmtime(chosen) or 0.0)
    except Exception:
        mtime = 0.0
    return {
        "kind": kind,
        "original_path": path,
        "durable_path": chosen,
        "size_bytes": size,
        "sha256_sample": _sample_hash(chosen),
        "mtime_epoch": round(mtime, 3),
        "source": source,
        "first_seen_at": _utc_iso(),
        "exists": bool(chosen and os.path.isfile(chosen) and size > 0),
    }


def _walk_explicit_paths(value: Any, *, key_hint: str = "", depth: int = 0) -> Iterable[Tuple[str, str, str]]:
    if depth > 10:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            key_norm = str(key or "").lower()
            if isinstance(child, str):
                kind = _kind_for_value(child, key_norm)
                if kind != "other":
                    path = _resolve_existing_path(child, kind)
                    if path:
                        yield path, kind, f"result:{key_norm}"
            elif isinstance(child, (dict, list, tuple)):
                yield from _walk_explicit_paths(child, key_hint=key_norm, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            if isinstance(child, str):
                kind = _kind_for_value(child, key_hint)
                if kind != "other":
                    path = _resolve_existing_path(child, kind)
                    if path:
                        yield path, kind, f"result:{key_hint or 'list'}"
            elif isinstance(child, (dict, list, tuple)):
                yield from _walk_explicit_paths(child, key_hint=key_hint, depth=depth + 1)


def _filesystem_candidates(after_epoch: float) -> Iterable[Tuple[str, str, str]]:
    roots = (
        (str(IMAGES_OUTPUT_DIR), "image", _IMAGE_EXTS),
        (str(AUDIO_OUTPUT_DIR), "audio", _AUDIO_EXTS),
        (str(VIDEO_OUTPUT_DIR), "video", _VIDEO_EXTS),
    )
    cutoff = max(0.0, float(after_epoch or 0.0) - 2.0)
    for root, kind, extensions in roots:
        try:
            if not os.path.isdir(root):
                continue
            for item in os.scandir(root):
                if not item.is_file():
                    continue
                if Path(item.name).suffix.lower() not in extensions:
                    continue
                try:
                    mtime = float(item.stat().st_mtime or 0.0)
                except Exception:
                    continue
                if mtime >= cutoff:
                    yield os.path.abspath(item.path), kind, "filesystem_checkpoint"
        except Exception:
            continue


def _stage_from_snapshot(snapshot: Dict[str, Any]) -> str:
    message = str(snapshot.get("message") or "").strip().lower()
    progress = int(snapshot.get("progress") or 0)
    if "revis" in message or "editorial" in message:
        return "stage_1_editorial"
    if "narra" in message or "tts" in message or "voice" in message or "áudio" in message or "audio" in message:
        return "stage_2_voice"
    if "imagem" in message or "visual" in message:
        return "stage_3_images"
    if "compos" in message or "montag" in message:
        return "stage_5_compose"
    if "render" in message or progress >= 85:
        return "stage_6_render"
    if progress >= 70:
        return "stage_5_compose"
    if progress >= 35:
        return "stage_3_images"
    if progress >= 18:
        return "stage_2_voice"
    if progress >= 8:
        return "stage_1_editorial"
    return "starting"


def _extract_payload(result: Any) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    payload = result.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _extract_script(result: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(result, dict):
        for key in ("script", "seeded_script", "storyboard"):
            candidate = result.get(key)
            if isinstance(candidate, dict) and candidate:
                return dict(candidate)
    candidate = payload.get("seeded_script") if isinstance(payload, dict) else None
    return dict(candidate) if isinstance(candidate, dict) else {}


def _expected_image_count(result: Any, payload: Dict[str, Any], script: Dict[str, Any]) -> int:
    candidates: List[int] = []
    for source in (payload, result if isinstance(result, dict) else {}, script):
        if not isinstance(source, dict):
            continue
        for key in ("expected_image_count", "image_count", "scene_count"):
            try:
                value = int(source.get(key) or 0)
            except Exception:
                value = 0
            if value > 0:
                candidates.append(value)
        scenes = source.get("scenes")
        if isinstance(scenes, list) and scenes:
            candidates.append(len(scenes))
    if isinstance(result, dict):
        report = result.get("render_report") if isinstance(result.get("render_report"), dict) else {}
        visuals = report.get("scene_visuals") if isinstance(report.get("scene_visuals"), list) else []
        if visuals:
            candidates.append(len(visuals))
    return max(candidates or [0])


def _merge_artifacts(existing: List[Dict[str, Any]], incoming: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for item in existing or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("durable_path") or item.get("original_path") or "").strip()
        if key:
            merged[key] = dict(item)
    for item in incoming:
        key = str(item.get("durable_path") or item.get("original_path") or "").strip()
        if not key:
            continue
        prior = merged.get(key) or {}
        combined = dict(prior)
        combined.update(item)
        if prior.get("first_seen_at"):
            combined["first_seen_at"] = prior["first_seen_at"]
        merged[key] = combined
    return sorted(merged.values(), key=lambda x: (str(x.get("kind") or ""), str(x.get("durable_path") or "")))


def sync_task_snapshot(task_id: Any, snapshot: Any) -> Dict[str, Any]:
    """Persist a durable, append-only-ish manifest whenever task state changes.

    This function is intentionally fail-safe: callers may invoke it from the
    task manager hot path and any persistence problem must never break video
    generation itself.
    """
    task_key = _safe_task_id(task_id)
    if not task_key or not isinstance(snapshot, dict):
        return {}
    with _LOCK:
        path = manifest_path(task_key)
        existing = _read_json(path)
        now_epoch = time.time()
        result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
        payload = _extract_payload(result)
        script = _extract_script(result, payload)
        if not existing:
            existing = {
                "schema_version": _SCHEMA_VERSION,
                "task_id": task_key,
                "created_at": str(snapshot.get("created_at") or _utc_iso()),
                "manifest_created_at": _utc_iso(),
                "scan_cursor_epoch": now_epoch,
                "artifacts": [],
                "checkpoints": [],
            }
        prior_cursor = float(existing.get("scan_cursor_epoch") or now_epoch)
        incoming_entries: List[Dict[str, Any]] = []
        seen_paths = set()
        for explicit_path, kind, source in _walk_explicit_paths(result):
            if explicit_path in seen_paths:
                continue
            seen_paths.add(explicit_path)
            incoming_entries.append(_artifact_entry(task_key, explicit_path, kind, source))

        # Only newly-created files are discovered heuristically. The cursor is
        # initialized at manifest creation, so an old failed task can never
        # accidentally claim media produced by later tasks.
        status = str(snapshot.get("status") or "").strip().lower()
        if status in {"pending", "processing", "pause_requested", "paused", "failed", "awaiting_review", "completed"}:
            for discovered_path, kind, source in _filesystem_candidates(prior_cursor):
                if discovered_path in seen_paths:
                    continue
                seen_paths.add(discovered_path)
                incoming_entries.append(_artifact_entry(task_key, discovered_path, kind, source))

        artifacts = _merge_artifacts(existing.get("artifacts") or [], incoming_entries)
        existing["artifacts"] = artifacts
        existing["scan_cursor_epoch"] = now_epoch
        existing["updated_at"] = _utc_iso()
        existing["status"] = status or existing.get("status")
        existing["progress"] = int(snapshot.get("progress") or 0)
        existing["message"] = str(snapshot.get("message") or "")[:2000]
        existing["stage"] = _stage_from_snapshot(snapshot)
        existing["payload"] = payload or existing.get("payload") or {}
        existing["expected_duration_minutes"] = int(float((payload or {}).get("duration") or (payload or {}).get("duration_minutes") or existing.get("expected_duration_minutes") or 0))
        if script:
            existing["script"] = script
            try:
                (manifest_dir(task_key) / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
        expected_images = _expected_image_count(result, payload, script or existing.get("script") or {})
        if expected_images > 0:
            existing["expected_image_count"] = expected_images

        checkpoint = {
            "at": _utc_iso(),
            "status": existing.get("status"),
            "progress": existing.get("progress"),
            "stage": existing.get("stage"),
            "message": existing.get("message"),
            "artifact_counts": {
                "images": sum(1 for a in artifacts if a.get("kind") == "image" and a.get("exists")),
                "audio": sum(1 for a in artifacts if a.get("kind") == "audio" and a.get("exists")),
                "video": sum(1 for a in artifacts if a.get("kind") == "video" and a.get("exists")),
            },
        }
        checkpoints = [c for c in (existing.get("checkpoints") or []) if isinstance(c, dict)]
        last = checkpoints[-1] if checkpoints else {}
        signature = (checkpoint["status"], checkpoint["progress"], checkpoint["stage"], checkpoint["message"])
        last_signature = (last.get("status"), last.get("progress"), last.get("stage"), last.get("message"))
        if signature != last_signature:
            checkpoints.append(checkpoint)
            checkpoints = checkpoints[-200:]
        existing["checkpoints"] = checkpoints
        _atomic_write_json(path, existing)
        return dict(existing)


def _probe_duration(path: str) -> float:
    try:
        from app.services.media_probe import probe_media_file
        data = probe_media_file(path)
        for key in ("format_duration", "duration", "audio_duration", "video_duration"):
            try:
                value = float((data or {}).get(key) or 0.0)
            except Exception:
                value = 0.0
            if value > 0.1:
                return value
    except Exception:
        return 0.0
    return 0.0


def _valid_artifacts(manifest: Dict[str, Any], kind: str) -> List[Dict[str, Any]]:
    valid = []
    for item in manifest.get("artifacts") or []:
        if not isinstance(item, dict) or item.get("kind") != kind:
            continue
        path = str(item.get("durable_path") or item.get("original_path") or "").strip()
        try:
            if path and os.path.isfile(path) and os.path.getsize(path) >= 1000:
                entry = dict(item)
                entry["resolved_path"] = path
                valid.append(entry)
        except Exception:
            continue
    return valid


def _plan_hash(plan: Dict[str, Any]) -> str:
    stable = {
        "task_id": plan.get("task_id"),
        "action": plan.get("action"),
        "script_ok": plan.get("script_ok"),
        "audio_ok": plan.get("audio_ok"),
        "valid_image_count": plan.get("valid_image_count"),
        "expected_image_count": plan.get("expected_image_count"),
        "missing_image_count": plan.get("missing_image_count"),
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def _image_cost_estimate(missing_count: int, duration_minutes: float, mode: str) -> Tuple[float, float]:
    missing = max(0, int(missing_count or 0))
    if missing <= 0:
        return 0.0, 0.0
    unit = 0.0
    try:
        from app.services.video_cost_estimator import estimate_video_cost
        estimate = estimate_video_cost(max(0.25, float(duration_minutes or 1.0)), mode=str(mode or "balanced"), regeneration_rate=0.0)
        unit = max(0.0, float(getattr(estimate, "image_unit_cost_usd", 0.0) or 0.0))
    except Exception:
        unit = 0.0
    usd = round(unit * missing, 6)
    try:
        brl_rate = max(0.01, float(os.getenv("CODEXIA_USD_BRL") or 5.20))
    except Exception:
        brl_rate = 5.20
    return usd, round(usd * brl_rate, 2)


def build_recovery_plan(task_id: Any, payload_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    task_key = _safe_task_id(task_id)
    manifest = load_manifest(task_key)
    payload = dict(payload_override or manifest.get("payload") or {})
    script = manifest.get("script") if isinstance(manifest.get("script"), dict) else {}
    images = _valid_artifacts(manifest, "image")
    audio = _valid_artifacts(manifest, "audio")
    videos = _valid_artifacts(manifest, "video")

    target_minutes = float(payload.get("duration") or payload.get("duration_minutes") or manifest.get("expected_duration_minutes") or 0.0)
    target_seconds = max(60.0, target_minutes * 60.0) if target_minutes > 0 else 0.0
    expected_images = int(manifest.get("expected_image_count") or _expected_image_count({}, payload, script) or len(images) or 0)
    valid_image_count = len(images)
    missing_images = max(0, expected_images - valid_image_count) if expected_images > 0 else 0

    audio_choice: Optional[Dict[str, Any]] = None
    audio_duration = 0.0
    for item in sorted(audio, key=lambda x: float(x.get("mtime_epoch") or 0.0), reverse=True):
        duration = _probe_duration(str(item.get("resolved_path") or ""))
        if duration <= 0:
            continue
        if target_seconds and not (target_seconds * 0.60 <= duration <= target_seconds * 1.80):
            continue
        audio_choice = item
        audio_duration = duration
        break

    video_choice: Optional[Dict[str, Any]] = None
    for item in sorted(videos, key=lambda x: float(x.get("mtime_epoch") or 0.0), reverse=True):
        duration = _probe_duration(str(item.get("resolved_path") or ""))
        if duration <= 0:
            continue
        if audio_duration > 0 and abs(duration - audio_duration) > max(4.0, audio_duration * 0.08):
            continue
        video_choice = dict(item)
        video_choice["duration_sec"] = round(duration, 3)
        break

    script_ok = bool(script and isinstance(script.get("scenes"), list) and script.get("scenes"))
    audio_ok = audio_choice is not None
    images_ok = bool(valid_image_count > 0 and (expected_images <= 0 or valid_image_count >= expected_images))
    video_ok = video_choice is not None

    if video_ok:
        action = "review_existing_render"
    elif script_ok and audio_ok and images_ok:
        action = "rerender_without_paid_media"
    elif script_ok and audio_ok and missing_images > 0:
        action = "regenerate_missing_images"
    else:
        action = "blocked"

    mode = str(payload.get("production_mode") or payload.get("mode") or "balanced")
    cost_usd, cost_brl = _image_cost_estimate(missing_images, target_minutes or 1.0, mode)
    plan = {
        "schema_version": _SCHEMA_VERSION,
        "task_id": task_key,
        "action": action,
        "script_ok": script_ok,
        "audio_ok": audio_ok,
        "images_ok": images_ok,
        "video_ok": video_ok,
        "valid_image_count": valid_image_count,
        "expected_image_count": expected_images,
        "missing_image_count": missing_images,
        "audio_duration_sec": round(audio_duration, 3),
        "target_duration_minutes": round(target_minutes, 3),
        "estimated_new_cost_usd": cost_usd,
        "estimated_new_cost_brl": cost_brl,
        "new_paid_calls_required": bool(action == "regenerate_missing_images" and missing_images > 0),
        "existing_image_paths": [str(item.get("resolved_path") or "") for item in images],
        "audio_path": str((audio_choice or {}).get("resolved_path") or ""),
        "video_path": str((video_choice or {}).get("resolved_path") or ""),
        "generated_at": _utc_iso(),
    }
    plan["plan_hash"] = _plan_hash(plan)
    return plan


def recovery_payload_patch(task_id: Any, payload: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    task_key = _safe_task_id(task_id)
    manifest = load_manifest(task_key)
    patched = dict(payload or {})
    script = manifest.get("script") if isinstance(manifest.get("script"), dict) else {}
    if script:
        patched["seeded_script"] = dict(script)
    existing_images = [str(x) for x in plan.get("existing_image_paths") or [] if str(x or "").strip()]
    if existing_images:
        patched["selected_images"] = existing_images
    audio_path = str(plan.get("audio_path") or "").strip()
    if audio_path:
        patched["reuse_audio_from"] = {
            "output_path": audio_path,
            "final_audio_path": audio_path,
            "audio_path": audio_path,
            "duration_seconds": float(plan.get("audio_duration_sec") or 0.0),
            "final_audio_duration_sec": float(plan.get("audio_duration_sec") or 0.0),
            "source": "production_manifest",
        }
    patched["force_reuse_assets"] = True
    patched["_production_manifest_recovery"] = True
    patched["_recovery_plan_hash"] = str(plan.get("plan_hash") or "")
    if plan.get("action") == "rerender_without_paid_media":
        patched["force_render_only"] = True
        patched.pop("_recovery_block_paid_regeneration", None)
        patched.pop("_recovery_missing_assets", None)
    elif plan.get("action") == "regenerate_missing_images":
        patched["force_render_only"] = False
        patched["_recovery_generate_missing_images_only"] = True
        patched["_recovery_missing_image_count"] = int(plan.get("missing_image_count") or 0)
        patched.pop("_recovery_block_paid_regeneration", None)
        patched.pop("_recovery_missing_assets", None)
    return patched


def confirm_or_prepare_partial_recovery(task_id: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Require an explicit second Resume click before any paid recovery.

    First call records a short-lived confirmation request and blocks. A second
    call for the exact same plan consumes the confirmation and returns a payload
    that reuses script/audio and allows only the missing-image stage.
    """
    task_key = _safe_task_id(task_id)
    plan = build_recovery_plan(task_key, payload_override=payload)
    if plan.get("action") != "regenerate_missing_images":
        return {"allow": False, "plan": plan, "reason": str(plan.get("action") or "blocked")}
    with _LOCK:
        manifest = load_manifest(task_key)
        pending = manifest.get("pending_paid_recovery") if isinstance(manifest.get("pending_paid_recovery"), dict) else {}
        now = time.time()
        same_plan = str(pending.get("plan_hash") or "") == str(plan.get("plan_hash") or "")
        not_expired = float(pending.get("expires_epoch") or 0.0) >= now
        if same_plan and not_expired:
            manifest["pending_paid_recovery"] = {}
            manifest["last_paid_recovery_confirmation"] = {
                "plan_hash": plan.get("plan_hash"),
                "confirmed_at": _utc_iso(),
                "missing_image_count": plan.get("missing_image_count"),
                "estimated_new_cost_usd": plan.get("estimated_new_cost_usd"),
                "estimated_new_cost_brl": plan.get("estimated_new_cost_brl"),
            }
            _atomic_write_json(manifest_path(task_key), manifest)
            return {
                "allow": True,
                "plan": plan,
                "payload": recovery_payload_patch(task_key, payload, plan),
                "reason": "user_confirmed_second_resume_click",
            }

        manifest["pending_paid_recovery"] = {
            "plan_hash": plan.get("plan_hash"),
            "requested_at": _utc_iso(),
            "expires_epoch": now + 10 * 60,
            "missing_image_count": plan.get("missing_image_count"),
            "estimated_new_cost_usd": plan.get("estimated_new_cost_usd"),
            "estimated_new_cost_brl": plan.get("estimated_new_cost_brl"),
        }
        _atomic_write_json(manifest_path(task_key), manifest)
        return {"allow": False, "plan": plan, "reason": "confirmation_required"}


def recovery_confirmation_message(plan: Dict[str, Any]) -> str:
    count = int(plan.get("missing_image_count") or 0)
    usd = float(plan.get("estimated_new_cost_usd") or 0.0)
    brl = float(plan.get("estimated_new_cost_brl") or 0.0)
    cost = f"US$ {usd:.4f} / aprox. R$ {brl:.2f}" if usd > 0 else "custo do provedor a confirmar"
    return (
        "Recuperação parcial disponível. Roteiro e áudio estão preservados; "
        f"faltam {count} imagem(ns). Custo preventivo estimado para gerar somente o que falta: {cost}. "
        "Nenhuma chamada paga foi feita ainda. Para confirmar esse custo e continuar, clique Retomar novamente; "
        "para desistir, use Cancelar."
    )


def recovery_ready_message(plan: Dict[str, Any]) -> str:
    return (
        "Recuperação confirmada: roteiro e áudio preservados serão reutilizados; "
        f"somente {int(plan.get('missing_image_count') or 0)} imagem(ns) ausente(s) poderão ser geradas. "
        "O manifesto permanente continuará registrando cada novo ativo e checkpoint."
    )


__all__ = [
    "build_recovery_plan",
    "confirm_or_prepare_partial_recovery",
    "load_manifest",
    "manifest_path",
    "recovery_confirmation_message",
    "recovery_payload_patch",
    "recovery_ready_message",
    "sync_task_snapshot",
]
