from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from app.services.intelligent_cost_optimizer import proportional_visual_index


ProgressCallback = Optional[Callable[[int, str], None]]


def _clean_existing_paths(values: Iterable[Any]) -> List[str]:
    paths: List[str] = []
    for value in values or []:
        path = os.path.abspath(str(value or "").strip())
        if not path or path in paths:
            continue
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 1000:
                paths.append(path)
        except Exception:
            continue
    return paths


def _safe_duration(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except Exception:
        return 0.0


def build_visual_segments(
    *,
    selected_images: Iterable[Any],
    official_scene_timeline: Sequence[Dict[str, Any]],
    target_duration: float,
    endcard_image: str = "",
) -> List[Dict[str, Any]]:
    """Build a deterministic still-image timeline for recovery-only rendering.

    Story entries keep the original narrative order. When there are fewer images
    than story entries, adjacent groups reuse the same image proportionally.
    Opening/closing use the first/last preserved image, while an optional locally
    generated branded endcard is used only for the final silent end-screen.
    """
    images = _clean_existing_paths(selected_images)
    if not images:
        return []

    timeline = [item for item in (official_scene_timeline or []) if isinstance(item, dict)]
    story_entries = [item for item in timeline if str(item.get("kind") or "").strip().lower() == "story"]
    story_count = max(1, len(story_entries))
    story_index_by_identity = {id(item): idx for idx, item in enumerate(story_entries)}
    endcard = os.path.abspath(str(endcard_image or "").strip()) if endcard_image else ""
    if not (endcard and os.path.isfile(endcard)):
        endcard = ""

    segments: List[Dict[str, Any]] = []
    cursor = 0.0
    total = max(0.1, float(target_duration or 0.0))

    for item in timeline:
        kind = str(item.get("kind") or "story").strip().lower()
        start = _safe_duration(item.get("scene_start"))
        end = _safe_duration(item.get("scene_end"))
        if end <= start:
            continue
        if start > total:
            break
        end = min(total, end)
        duration = end - start
        if duration <= 0:
            continue

        if kind == "story":
            story_idx = story_index_by_identity.get(id(item), 0)
            image_idx = proportional_visual_index(story_idx, len(images), story_count)
            image_path = images[image_idx]
        elif kind == "endcard" and endcard:
            image_path = endcard
        elif kind == "opening":
            image_path = images[0]
        else:
            image_path = images[-1]

        if start > cursor + 0.01:
            gap_image = segments[-1]["image_path"] if segments else images[0]
            segments.append({
                "image_path": gap_image,
                "duration": start - cursor,
                "kind": "gap_hold",
            })
        segments.append({
            "image_path": image_path,
            "duration": duration,
            "kind": kind,
        })
        cursor = end

    if not segments:
        segments = [{"image_path": images[0], "duration": total, "kind": "fallback_hold"}]
        cursor = total

    if cursor < total - 0.01:
        tail_image = endcard or segments[-1]["image_path"]
        segments.append({
            "image_path": tail_image,
            "duration": total - cursor,
            "kind": "tail_hold",
        })

    merged: List[Dict[str, Any]] = []
    for item in segments:
        duration = max(0.04, _safe_duration(item.get("duration")))
        path = str(item.get("image_path") or "").strip()
        if not path:
            continue
        if merged and merged[-1]["image_path"] == path:
            merged[-1]["duration"] += duration
            merged[-1]["kind"] = f"{merged[-1].get('kind', '')}+{item.get('kind', '')}".strip("+")
        else:
            merged.append({"image_path": path, "duration": duration, "kind": item.get("kind") or "story"})
    return merged


def _srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds or 0.0) * 1000.0)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt_text(captions: Sequence[Dict[str, Any]], *, max_duration: float) -> str:
    blocks: List[str] = []
    limit = max(0.1, float(max_duration or 0.0))
    index = 1
    for item in captions or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("caption") or item.get("text") or "").strip()
        text = text.replace("\r", " ").replace("\x00", "").strip()
        if not text:
            continue
        start = max(0.0, _safe_duration(item.get("start")))
        end = min(limit, _safe_duration(item.get("end")))
        if end <= start:
            continue
        blocks.append(
            f"{index}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{text}\n"
        )
        index += 1
    return "\n".join(blocks).strip() + ("\n" if blocks else "")


def _ffconcat_quote(path: str) -> str:
    return os.path.abspath(path).replace("'", "'\\''")


def build_concat_text(segments: Sequence[Dict[str, Any]]) -> str:
    lines = ["ffconcat version 1.0"]
    valid: List[Tuple[str, float]] = []
    for item in segments or []:
        path = str(item.get("image_path") or "").strip()
        duration = _safe_duration(item.get("duration"))
        if not path or duration <= 0:
            continue
        valid.append((path, duration))
        lines.append(f"file '{_ffconcat_quote(path)}'")
        lines.append(f"duration {duration:.6f}")
    if valid:
        lines.append(f"file '{_ffconcat_quote(valid[-1][0])}'")
    return "\n".join(lines) + "\n"


def _escape_subtitles_filter_path(path: str) -> str:
    value = os.path.abspath(path).replace("\\", "/")
    return value.replace(":", "\\:").replace("'", "\\'")


def _ffprobe_duration(path: str) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not os.path.isfile(path):
        return 0.0
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                os.path.abspath(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return 0.0
        return max(0.0, float((result.stdout or "0").strip() or 0.0))
    except Exception:
        return 0.0


def _local_music_candidate(music_dir: str, mood: str) -> str:
    root = os.path.abspath(str(music_dir or "").strip())
    if not root or not os.path.isdir(root):
        return ""
    preferred = os.path.join(root, f"{str(mood or 'drama').strip().lower()}.mp3")
    try:
        if os.path.isfile(preferred) and os.path.getsize(preferred) > 1000:
            return preferred
    except Exception:
        pass
    try:
        for candidate in sorted(Path(root).glob("*.mp3")):
            if candidate.is_file() and candidate.stat().st_size > 1000:
                return str(candidate.resolve())
    except Exception:
        pass
    return ""


def build_ffmpeg_command(
    *,
    concat_path: str,
    srt_path: str,
    audio_path: str,
    output_path: str,
    target_duration: float,
    video_size: Tuple[int, int],
    local_music_path: str = "",
    music_volume: float = 0.025,
    threads: int = 2,
) -> List[str]:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    width, height = int(video_size[0]), int(video_size[1])
    total = max(0.1, float(target_duration or 0.0))
    safe_threads = max(1, min(2, int(threads or 1)))
    subtitle_path = _escape_subtitles_filter_path(srt_path)
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps=24,format=yuv420p,"
        f"subtitles='{subtitle_path}':"
        "force_style='FontName=DejaVu Sans,FontSize=28,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=36'"
    )

    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel", "warning",
        "-f", "concat",
        "-safe", "0",
        "-i", os.path.abspath(concat_path),
        "-i", os.path.abspath(audio_path),
    ]

    music = os.path.abspath(local_music_path) if local_music_path else ""
    tail_padding = max(0.0, total - _ffprobe_duration(audio_path))
    filter_parts = [
        f"[0:v]{video_filter}[v]",
        f"[1:a]apad=pad_dur={tail_padding + 1.0:.3f}[narr]",
    ]
    audio_map = "[narr]"

    if music and os.path.isfile(music):
        volume = max(0.0, min(0.20, float(music_volume or 0.0)))
        fade_start = max(0.0, total - 1.25)
        command += ["-stream_loop", "-1", "-i", music]
        filter_parts.append(
            f"[2:a]volume={volume:.4f},atrim=0:{total:.3f},"
            f"afade=t=out:st={fade_start:.3f}:d=1.25[music]"
        )
        filter_parts.append("[narr][music]amix=inputs=2:duration=longest:dropout_transition=2[a]")
        audio_map = "[a]"

    command += [
        "-filter_complex", ";".join(filter_parts),
        "-map", "[v]",
        "-map", audio_map,
        "-t", f"{total:.3f}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "stillimage",
        "-b:v", "1800k",
        "-threads", str(safe_threads),
        "-c:a", "aac",
        "-b:a", "160k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-nostats",
        os.path.abspath(output_path),
    ]
    return command


def render_lightweight_recovery_video(
    *,
    output_path: str,
    selected_images: Iterable[Any],
    audio_path: str,
    captions: Sequence[Dict[str, Any]],
    official_scene_timeline: Sequence[Dict[str, Any]],
    target_duration: float,
    video_size: Tuple[int, int],
    endcard_image: str = "",
    music_dir: str = "app/static/music",
    music_mood: str = "drama",
    music_volume: float = 0.025,
    progress_callback: ProgressCallback = None,
) -> Dict[str, Any]:
    """Render recovery-only media using local assets and FFmpeg.

    No AI/music provider and no download are called here. Images, narration,
    captions and optional music must already exist on disk.
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg não está disponível para o render leve de recuperação.")
    if not audio_path or not os.path.isfile(audio_path) or os.path.getsize(audio_path) <= 1000:
        raise RuntimeError("Áudio preservado inválido para o render leve de recuperação.")

    total = max(0.1, float(target_duration or 0.0))
    segments = build_visual_segments(
        selected_images=selected_images,
        official_scene_timeline=official_scene_timeline,
        target_duration=total,
        endcard_image=endcard_image,
    )
    if not segments:
        raise RuntimeError("Nenhuma imagem local válida disponível para o render leve de recuperação.")

    output_abs = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_abs) or ".", exist_ok=True)
    local_music = _local_music_candidate(music_dir, music_mood)

    if progress_callback:
        progress_callback(90, "6/8 Render leve confirmado — preparando FFmpeg local...")

    started = time.time()
    with tempfile.TemporaryDirectory(prefix="codexia-light-recovery-", dir=os.path.dirname(output_abs) or None) as tmp:
        concat_path = os.path.join(tmp, "visuals.ffconcat")
        srt_path = os.path.join(tmp, "captions.srt")
        with open(concat_path, "w", encoding="utf-8") as fh:
            fh.write(build_concat_text(segments))
        with open(srt_path, "w", encoding="utf-8") as fh:
            fh.write(build_srt_text(captions, max_duration=total))

        command = build_ffmpeg_command(
            concat_path=concat_path,
            srt_path=srt_path,
            audio_path=audio_path,
            output_path=output_abs,
            target_duration=total,
            video_size=video_size,
            local_music_path=local_music,
            music_volume=music_volume,
            threads=2,
        )

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        max_runtime = max(900.0, min(3600.0, total * 4.0))
        last_emit = 0.0
        output_tail: List[str] = []
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = (raw_line or "").strip()
                if line:
                    output_tail.append(line)
                    output_tail = output_tail[-40:]
                now = time.time()
                if now - started > max_runtime:
                    process.terminate()
                    raise RuntimeError(f"Render leve excedeu o limite seguro de {int(max_runtime)}s.")
                if line.startswith("out_time_ms="):
                    try:
                        elapsed_media = float(line.split("=", 1)[1]) / 1_000_000.0
                    except Exception:
                        elapsed_media = 0.0
                    if progress_callback and now - last_emit >= 1.5:
                        ratio = max(0.0, min(1.0, elapsed_media / total))
                        pct = 90 + int(ratio * 9.0)
                        progress_callback(
                            min(99, pct),
                            f"6/8 Render leve FFmpeg — {elapsed_media:.0f}s/{total:.0f}s codificados...",
                        )
                        last_emit = now
            return_code = process.wait(timeout=30)
        except Exception:
            try:
                process.terminate()
                process.wait(timeout=10)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            raise

    if return_code != 0:
        raise RuntimeError(
            "FFmpeg falhou no render leve de recuperação. " + " | ".join(output_tail[-8:])
        )
    if not os.path.isfile(output_abs) or os.path.getsize(output_abs) < 50 * 1024:
        raise RuntimeError("Render leve terminou sem produzir um MP4 utilizável.")

    obtained = _ffprobe_duration(output_abs)
    tolerance = max(1.0, min(4.0, total * 0.01))
    if obtained <= 0 or abs(obtained - total) > tolerance:
        raise RuntimeError(
            f"Duração inválida no render leve: obtida={obtained:.2f}s alvo={total:.2f}s."
        )

    if progress_callback:
        progress_callback(100, "Vídeo renderizado com sucesso pelo modo leve de recuperação.")

    return {
        "file_path": output_abs,
        "duration_sec": round(obtained, 3),
        "render_seconds": round(max(0.0, time.time() - started), 3),
        "visual_segment_count": len(segments),
        "caption_count": len([
            item for item in captions or []
            if isinstance(item, dict) and str(item.get("caption") or item.get("text") or "").strip()
        ]),
        "music_source": "local_existing_file" if local_music else "none",
        "music_path": local_music,
        "paid_provider_calls": 0,
        "external_downloads": 0,
        "renderer": "ffmpeg_concat_subtitles_v1",
        "motion_policy": "static_preserved_visuals_hard_cuts",
    }
