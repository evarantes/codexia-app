"""Fail-closed media validation backed by the system ``ffprobe`` binary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, Iterable


def _positive_float(value: Any) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 else 0.0


def _maximum_stream_duration(streams: Iterable[Dict[str, Any]], codec_type: str) -> float:
    duration = 0.0
    for stream in streams:
        if str(stream.get("codec_type") or "").lower() != codec_type:
            continue
        duration = max(duration, _positive_float(stream.get("duration")))
        tags = stream.get("tags")
        if isinstance(tags, dict):
            duration = max(duration, _positive_float(tags.get("DURATION")))
    return duration


def probe_media_file(path: str, timeout_seconds: int = 20) -> Dict[str, Any]:
    """Return verified stream and duration metadata for a local media file.

    Missing ``ffprobe``, malformed output, timeouts and corrupt files are all
    failures. File size is reported for diagnostics but is never used as a
    substitute for stream validation.
    """

    absolute_path = os.path.abspath(str(path or "")) if path else ""
    result: Dict[str, Any] = {
        "ok": False,
        "path": absolute_path,
        "file_exists": bool(absolute_path and os.path.isfile(absolute_path)),
        "file_size_bytes": 0,
        "probe_available": bool(shutil.which("ffprobe")),
        "video_stream": False,
        "audio_stream": False,
        "video_duration": 0.0,
        "audio_duration": 0.0,
        "format_duration": 0.0,
        "error": None,
    }

    if not result["file_exists"]:
        result["error"] = "file_not_found"
        return result

    try:
        result["file_size_bytes"] = int(os.path.getsize(absolute_path) or 0)
    except OSError as exc:
        result["error"] = f"file_stat_failed: {exc}"
        return result

    if not result["probe_available"]:
        result["error"] = "ffprobe_not_available"
        return result

    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,duration:stream_tags=DURATION",
                "-of",
                "json",
                absolute_path,
            ],
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_seconds or 20)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        result["error"] = "ffprobe_timeout"
        return result
    except Exception as exc:
        result["error"] = f"ffprobe_execution_failed: {type(exc).__name__}: {str(exc)[:160]}"
        return result

    if completed.returncode != 0:
        stderr = str(completed.stderr or "").strip().replace("\n", " ")
        result["error"] = f"ffprobe_rejected_file: {stderr[:200]}"
        return result

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        result["error"] = f"ffprobe_invalid_json: {exc}"
        return result

    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        result["error"] = "ffprobe_missing_streams"
        return result

    stream_dicts = [stream for stream in streams if isinstance(stream, dict)]
    result["video_stream"] = any(
        str(stream.get("codec_type") or "").lower() == "video" for stream in stream_dicts
    )
    result["audio_stream"] = any(
        str(stream.get("codec_type") or "").lower() == "audio" for stream in stream_dicts
    )

    format_obj = payload.get("format") if isinstance(payload, dict) else None
    format_duration = _positive_float(format_obj.get("duration")) if isinstance(format_obj, dict) else 0.0
    video_duration = _maximum_stream_duration(stream_dicts, "video")
    audio_duration = _maximum_stream_duration(stream_dicts, "audio")

    # MP4 files may omit per-stream duration while still reporting a verified
    # container duration. Only use that ffprobe-derived value for an existing
    # stream; never estimate duration from file size.
    if result["video_stream"] and video_duration <= 0:
        video_duration = format_duration
    if result["audio_stream"] and audio_duration <= 0:
        audio_duration = format_duration

    result["format_duration"] = round(format_duration, 6)
    result["video_duration"] = round(video_duration, 6)
    result["audio_duration"] = round(audio_duration, 6)
    result["ok"] = bool(
        result["file_size_bytes"] > 100 * 1024
        and result["video_stream"]
        and result["audio_stream"]
        and video_duration > 0.5
        and audio_duration > 0.5
    )
    if not result["ok"]:
        result["error"] = "required_media_checks_failed"
    return result


def media_durations_match(probe: Dict[str, Any], minimum_tolerance: float = 0.5) -> bool:
    """Require audio and video durations to remain within a small tolerance."""

    video_duration = _positive_float(probe.get("video_duration"))
    audio_duration = _positive_float(probe.get("audio_duration"))
    if video_duration <= 0 or audio_duration <= 0:
        return False
    tolerance = max(float(minimum_tolerance), audio_duration * 0.03)
    return abs(video_duration - audio_duration) <= tolerance
