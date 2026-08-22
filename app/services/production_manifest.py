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


def _extract_selected_image_references(result: Any, payload: Dict[str, Any], script: Dict[str, Any]) -> List[str]:
    """Preserve the original scene-image order even when paths later go stale."""
    references: List[str] = []

    def _add(value: Any) -> None:
        if not isinstance(value, str):
            return
        item = value.strip()
        if item and item not in references:
            references.append(item)

    def _add_list(value: Any) -> None:
        if not isinstance(value, list):
            return
        for item in value:
            if isinstance(item, str):
                _add(item)
                continue
            if not isinstance(item, dict):
                continue
            for key in (
                "image_path",
                "generated_image_path",
                "source_path",
                "background_image_path",
                "selected_image_path",
                "path",
                "image_url",
                "url",
                "storage_key",
            ):
                _add(item.get(key))

    sources = [payload, result if isinstance(result, dict) else {}, script]
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("selected_images", "rendered_images", "images", "custom_image_paths"):
            _add_list(source.get(key))

    if isinstance(result, dict):
        report = result.get("render_report") if isinstance(result.get("render_report"), dict) else {}
        _add_list(report.get("scene_visuals"))
        visual_plan = report.get("visual_plan") if isinstance(report.get("visual_plan"), dict) else {}
        for key in ("selected_images", "rendered_images", "images"):
            _add_list(visual_plan.get(key))
        storyboard = result.get("storyboard") if isinstance(result.get("storyboard"), dict) else {}
        _add_list(storyboard.get("scenes"))
    return references


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
        selected_image_references = _extract_selected_image_references(result, payload, script)
        if selected_image_references:
            existing["selected_image_references"] = selected_image_references
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


def _usable_image_file(path_value: Any, *, min_bytes: int = 1000) -> bool:
    path = str(path_value or "").strip()
    try:
        if not path or not os.path.isfile(path) or os.path.getsize(path) < int(min_bytes or 1):
            return False
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
        return int(width or 0) >= 32 and int(height or 0) >= 32
    except Exception:
        return False


def _normalized_path_key(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    raw = raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return raw


def _artifact_aliases(item: Dict[str, Any]) -> Tuple[set, set]:
    full: set = set()
    names: set = set()
    for key in ("original_path", "durable_path", "resolved_path"):
        value = _normalized_path_key(item.get(key))
        if not value:
            continue
        full.add(value)
        name = Path(value).name.lower()
        if not name:
            continue
        names.add(name)
        names.add(re.sub(r"^[0-9a-f]{10}_", "", name))
    return full, names


def _scene_number_from_artifact(item: Dict[str, Any]) -> int:
    names: List[str] = []
    for key in ("original_path", "durable_path", "resolved_path"):
        name = Path(_normalized_path_key(item.get(key))).name.lower()
        if name:
            names.append(re.sub(r"^[0-9a-f]{10}_", "", name))
    patterns = (
        r"(?:scene|cena|image|imagem|frame|visual|group|grupo)[_-]?(\d{1,3})(?:\D|$)",
        r"^(\d{1,3})[_-]",
    )
    for name in names:
        for pattern in patterns:
            match = re.search(pattern, name)
            if match:
                try:
                    return int(match.group(1))
                except Exception:
                    continue
    return 0


def resolve_recovery_image_paths(
    task_id: Any,
    selected_values: Optional[Iterable[Any]] = None,
    *,
    expected_count: int = 0,
) -> Dict[str, Any]:
    """Map stale selected_images references to worker-local durable files.

    Exact original-path and filename matches are preferred. Extra artifacts are
    never selected arbitrarily when more candidates exist than scene slots.
    This function is read-only and never invokes an external provider.
    """
    task_key = _safe_task_id(task_id)
    manifest = load_manifest(task_key) if task_key else {}
    try:
        target = int(expected_count or manifest.get("expected_image_count") or 0)
    except Exception:
        target = 0
    target = max(0, target)

    raw_references = list(selected_values or [])
    if not raw_references:
        stored = manifest.get("selected_image_references")
        raw_references = list(stored) if isinstance(stored, list) else []
    if not raw_references:
        for source in (
            manifest.get("payload") if isinstance(manifest.get("payload"), dict) else {},
            manifest.get("script") if isinstance(manifest.get("script"), dict) else {},
        ):
            for key in ("selected_images", "rendered_images", "images", "custom_image_paths"):
                values = source.get(key) if isinstance(source, dict) else None
                if isinstance(values, list) and values:
                    raw_references.extend(values)

    candidates: List[Dict[str, Any]] = []
    invalid_candidate_count = 0
    seen_candidate_keys: set = set()
    for artifact in _valid_artifacts(manifest, "image"):
        path = str(artifact.get("resolved_path") or "").strip()
        if not _usable_image_file(path):
            invalid_candidate_count += 1
            continue
        entry = dict(artifact)
        entry["resolved_path"] = os.path.abspath(path)
        content_key = str(entry.get("sha256_sample") or "").strip() or _sample_hash(path) or os.path.realpath(path)
        if content_key in seen_candidate_keys:
            continue
        seen_candidate_keys.add(content_key)
        entry["_content_key"] = content_key
        full_aliases, name_aliases = _artifact_aliases(entry)
        entry["_full_aliases"] = full_aliases
        entry["_name_aliases"] = name_aliases
        entry["_scene_number"] = _scene_number_from_artifact(entry)
        candidates.append(entry)

    candidates.sort(
        key=lambda item: (
            float(item.get("mtime_epoch") or 0.0),
            str(item.get("first_seen_at") or ""),
            str(item.get("original_path") or item.get("resolved_path") or ""),
        )
    )

    chosen: List[str] = []
    chosen_keys: set = set()
    matched_reference_count = 0
    direct_reference_count = 0
    ambiguous_reference_count = 0

    def _choose(entry: Dict[str, Any]) -> bool:
        content_key = str(entry.get("_content_key") or "")
        path = str(entry.get("resolved_path") or "")
        if not path or content_key in chosen_keys:
            return False
        chosen.append(path)
        chosen_keys.add(content_key)
        return True

    for raw in raw_references:
        if target and len(chosen) >= target:
            break
        reference = _normalized_path_key(raw)
        if not reference:
            continue
        direct = _resolve_existing_path(reference, "image")
        if direct and _usable_image_file(direct):
            direct_key = _sample_hash(direct) or os.path.realpath(direct)
            direct_entry = {
                "resolved_path": os.path.abspath(direct),
                "_content_key": direct_key,
            }
            if _choose(direct_entry):
                direct_reference_count += 1
                matched_reference_count += 1
            continue

        full_matches = [item for item in candidates if reference in item.get("_full_aliases", set())]
        matches = full_matches
        if not matches:
            name = Path(reference).name.lower()
            normalized_name = re.sub(r"^[0-9a-f]{10}_", "", name)
            matches = [
                item
                for item in candidates
                if name in item.get("_name_aliases", set())
                or normalized_name in item.get("_name_aliases", set())
            ]
        unique_matches = {str(item.get("_content_key") or ""): item for item in matches}
        if len(unique_matches) == 1:
            if _choose(next(iter(unique_matches.values()))):
                matched_reference_count += 1
        elif len(unique_matches) > 1:
            ambiguous_reference_count += 1

    fallback_count = 0
    ambiguous_fallback = False
    needed = max(0, target - len(chosen)) if target else 0
    remaining = [item for item in candidates if str(item.get("_content_key") or "") not in chosen_keys]
    if target and needed > 0:
        if len(remaining) == needed:
            for entry in remaining:
                if _choose(entry):
                    fallback_count += 1
        elif len(remaining) > needed:
            slots: Dict[int, List[Dict[str, Any]]] = {}
            for entry in remaining:
                scene_number = int(entry.get("_scene_number") or 0)
                if 1 <= scene_number <= target:
                    slots.setdefault(scene_number, []).append(entry)
            used_scene_numbers = {
                int(item.get("_scene_number") or 0)
                for item in candidates
                if str(item.get("_content_key") or "") in chosen_keys
            }
            slot_candidates = [
                items[0]
                for scene_number, items in sorted(slots.items())
                if scene_number not in used_scene_numbers and len(items) == 1
            ]
            if len(slot_candidates) == needed:
                for entry in slot_candidates:
                    if _choose(entry):
                        fallback_count += 1
            else:
                ambiguous_fallback = True
    elif not target and not chosen:
        for entry in remaining:
            if _choose(entry):
                fallback_count += 1

    if target and len(chosen) > target:
        chosen = chosen[:target]

    return {
        "task_id": task_key,
        "paths": chosen,
        "expected_count": target,
        "selected_reference_count": len([x for x in raw_references if str(x or "").strip()]),
        "matched_reference_count": matched_reference_count,
        "direct_reference_count": direct_reference_count,
        "fallback_count": fallback_count,
        "candidate_count": len(candidates),
        "invalid_candidate_count": invalid_candidate_count,
        "ambiguous_reference_count": ambiguous_reference_count,
        "ambiguous_fallback": ambiguous_fallback,
        "complete": bool(chosen and (target <= 0 or len(chosen) >= target)),
        "paid_calls_performed": False,
        "strategy": "manifest_original_path_then_unique_scene_slot_v1",
    }


def _plan_hash(plan: Dict[str, Any]) -> str:
    stable = {
        "task_id": plan.get("task_id"),
        "action": plan.get("action"),
        "script_ok": plan.get("script_ok"),
        "audio_ok": plan.get("audio_ok"),
        "valid_image_count": plan.get("valid_image_count"),
        "expected_image_count": plan.get("expected_image_count"),
        "missing_image_count": plan.get("missing_image_count"),
        "audio_reusable": plan.get("audio_reusable"),
        "audio_rebuild_required": plan.get("audio_rebuild_required"),
        "estimated_new_cost_usd": plan.get("estimated_new_cost_usd"),
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


def _audio_cost_estimate(duration_minutes: float) -> Tuple[float, float]:
    minutes = max(0.0, float(duration_minutes or 0.0))
    try:
        unit_usd = max(0.0, float(os.getenv("YOUTUBE_AUTO_TTS_MINUTE_COST_UNIT") or 0.0120))
    except Exception:
        unit_usd = 0.0120
    usd = round(minutes * unit_usd, 6)
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
    audio = _valid_artifacts(manifest, "audio")
    videos = _valid_artifacts(manifest, "video")

    target_minutes = float(payload.get("duration") or payload.get("duration_minutes") or manifest.get("expected_duration_minutes") or 0.0)
    target_seconds = max(60.0, target_minutes * 60.0) if target_minutes > 0 else 0.0
    expected_images = int(manifest.get("expected_image_count") or _expected_image_count({}, payload, script) or 0)
    selected_references = payload.get("selected_images") if isinstance(payload.get("selected_images"), list) else None
    image_resolution = resolve_recovery_image_paths(
        task_key,
        selected_references,
        expected_count=expected_images,
    )
    existing_image_paths = [str(path) for path in image_resolution.get("paths") or [] if str(path or "").strip()]
    if expected_images <= 0:
        expected_images = len(existing_image_paths)
    valid_image_count = len(existing_image_paths)
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
    audio_found = audio_choice is not None
    audio_reusable = bool(
        audio_choice
        and str(audio_choice.get("source") or "").strip().lower() == "tts_immediate"
    )
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
    audio_ok = audio_reusable
    images_ok = bool(valid_image_count > 0 and (expected_images <= 0 or valid_image_count >= expected_images))
    video_ok = video_choice is not None

    if video_ok:
        action = "review_existing_render"
    elif script_ok and audio_ok and images_ok:
        action = "rerender_without_paid_media"
    elif script_ok and audio_ok and missing_images > 0:
        action = "regenerate_missing_images"
    elif script_ok and images_ok:
        action = "rebuild_untrusted_audio" if audio_found else "rebuild_missing_audio"
    elif script_ok and missing_images > 0:
        action = "rebuild_audio_and_missing_images"
    else:
        action = "blocked"

    mode = str(payload.get("production_mode") or payload.get("mode") or "balanced")
    image_cost_usd, image_cost_brl = _image_cost_estimate(missing_images, target_minutes or 1.0, mode)
    rebuilds_audio = action in {"rebuild_untrusted_audio", "rebuild_missing_audio", "rebuild_audio_and_missing_images"}
    audio_cost_usd, audio_cost_brl = _audio_cost_estimate(target_minutes or 1.0) if rebuilds_audio else (0.0, 0.0)
    cost_usd = round(image_cost_usd + audio_cost_usd, 6)
    cost_brl = round(image_cost_brl + audio_cost_brl, 2)
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
        "audio_found": audio_found,
        "audio_reusable": audio_reusable,
        "audio_trust": "narration_contract_v1" if audio_reusable else ("legacy_unverified" if audio_found else "missing"),
        "audio_rebuild_required": rebuilds_audio,
        "target_duration_minutes": round(target_minutes, 3),
        "estimated_image_cost_usd": image_cost_usd,
        "estimated_image_cost_brl": image_cost_brl,
        "estimated_audio_cost_usd": audio_cost_usd,
        "estimated_audio_cost_brl": audio_cost_brl,
        "estimated_new_cost_usd": cost_usd,
        "estimated_new_cost_brl": cost_brl,
        "new_paid_calls_required": bool(
            (action in {"regenerate_missing_images", "rebuild_audio_and_missing_images"} and missing_images > 0)
            or rebuilds_audio
        ),
        "paid_operations": [
            operation
            for operation, enabled in (
                ("generate_missing_images", missing_images > 0 and action in {"regenerate_missing_images", "rebuild_audio_and_missing_images"}),
                ("rebuild_narration", rebuilds_audio),
            )
            if enabled
        ],
        "existing_image_paths": existing_image_paths,
        "image_resolution": {key: value for key, value in image_resolution.items() if key != "paths"},
        "audio_path": str((audio_choice or {}).get("resolved_path") or "") if audio_reusable else "",
        "audio_candidate_path": str((audio_choice or {}).get("resolved_path") or ""),
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
    if audio_path and bool(plan.get("audio_reusable")):
        patched["reuse_audio_from"] = {
            "output_path": audio_path,
            "final_audio_path": audio_path,
            "audio_path": audio_path,
            "duration_seconds": float(plan.get("audio_duration_sec") or 0.0),
            "final_audio_duration_sec": float(plan.get("audio_duration_sec") or 0.0),
            "source": "production_manifest",
        }
    else:
        patched.pop("reuse_audio_from", None)
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
    elif plan.get("action") in {"rebuild_untrusted_audio", "rebuild_missing_audio", "rebuild_audio_and_missing_images"}:
        patched["force_render_only"] = False
        rebuilds_images = plan.get("action") == "rebuild_audio_and_missing_images"
        patched["_recovery_generate_missing_images_only"] = bool(rebuilds_images)
        patched["_recovery_missing_image_count"] = int(plan.get("missing_image_count") or 0) if rebuilds_images else 0
        seeded = patched.get("seeded_script") if isinstance(patched.get("seeded_script"), dict) else {}
        if seeded:
            seeded = dict(seeded)
            seeded["_manifest_recovery_policy"] = {
                "version": 1,
                "rebuild_audio": True,
                "reuse_images": bool(existing_images),
                "generate_missing_images_only": bool(rebuilds_images),
                "existing_image_count": len(existing_images),
                "expected_image_count": int(plan.get("expected_image_count") or 0),
                "missing_image_count": int(plan.get("missing_image_count") or 0),
                "plan_hash": str(plan.get("plan_hash") or ""),
                "paid_confirmation_consumed": True,
            }
            if rebuilds_images:
                seeded["_partial_image_recovery"] = {
                    "enabled": True,
                    "existing_image_count": len(existing_images),
                    "expected_image_count": int(plan.get("expected_image_count") or 0),
                    "missing_image_count": int(plan.get("missing_image_count") or 0),
                    "estimated_image_cost_usd": float(plan.get("estimated_image_cost_usd") or 0.0),
                    "estimated_image_cost_brl": float(plan.get("estimated_image_cost_brl") or 0.0),
                    "plan_hash": str(plan.get("plan_hash") or ""),
                }
            patched["seeded_script"] = seeded
        patched.pop("_recovery_block_paid_regeneration", None)
        patched.pop("_recovery_missing_assets", None)
    return patched


def confirm_or_prepare_partial_recovery(task_id: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Require an explicit second Resume click before any paid recovery.

    First call records a short-lived confirmation request and blocks. A second
    call for the exact same plan consumes the confirmation and returns a payload
    that reuses every trusted asset and permits only the confirmed paid stages.
    """
    task_key = _safe_task_id(task_id)
    plan = build_recovery_plan(task_key, payload_override=payload)
    paid_actions = {
        "regenerate_missing_images",
        "rebuild_untrusted_audio",
        "rebuild_missing_audio",
        "rebuild_audio_and_missing_images",
    }
    if plan.get("action") not in paid_actions:
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
                "audio_rebuild_required": bool(plan.get("audio_rebuild_required")),
                "paid_operations": list(plan.get("paid_operations") or []),
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
            "audio_rebuild_required": bool(plan.get("audio_rebuild_required")),
            "paid_operations": list(plan.get("paid_operations") or []),
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
    operations: List[str] = []
    if bool(plan.get("audio_rebuild_required")):
        operations.append("reconstruir somente a narração")
    if count > 0:
        operations.append(f"gerar somente {count} imagem(ns) ausente(s)")
    operation_label = " e ".join(operations) or "executar a recuperação necessária"
    preserved = "Roteiro e imagens válidas serão preservados" if bool(plan.get("audio_rebuild_required")) else "Roteiro e áudio confiável serão preservados"
    return (
        f"Recuperação parcial disponível. {preserved}; será necessário {operation_label}. "
        f"Custo preventivo máximo estimado: {cost}. Nenhuma chamada paga foi feita ainda. "
        "Para confirmar esse custo e continuar, clique Retomar novamente; para desistir, use Cancelar."
    )


def recovery_ready_message(plan: Dict[str, Any]) -> str:
    actions: List[str] = []
    if bool(plan.get("audio_rebuild_required")):
        actions.append("a narração será reconstruída")
    missing = int(plan.get("missing_image_count") or 0)
    if missing > 0:
        actions.append(f"somente {missing} imagem(ns) ausente(s) poderão ser geradas")
    return (
        "Recuperação confirmada: roteiro e ativos válidos serão reutilizados; "
        + (" e ".join(actions) if actions else "nenhuma nova mídia paga será gerada")
        + ". O manifesto permanente continuará registrando cada novo ativo e checkpoint."
    )


__all__ = [
    "build_recovery_plan",
    "confirm_or_prepare_partial_recovery",
    "load_manifest",
    "manifest_path",
    "recovery_confirmation_message",
    "recovery_payload_patch",
    "recovery_ready_message",
    "resolve_recovery_image_paths",
    "sync_task_snapshot",
]
