from __future__ import annotations

import os
import re
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
    opening_image: str = "",
    story_image: str = "",
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
    opening = os.path.abspath(str(opening_image or "").strip()) if opening_image else ""
    if not (opening and os.path.isfile(opening)):
        opening = ""
    story = os.path.abspath(str(story_image or "").strip()) if story_image else ""
    if not (story and os.path.isfile(story)):
        story = ""

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
            if story:
                image_path = story
            else:
                story_idx = story_index_by_identity.get(id(item), 0)
                image_idx = proportional_visual_index(story_idx, len(images), story_count)
                image_path = images[image_idx]
        elif kind == "endcard" and endcard:
            image_path = endcard
        elif kind == "opening":
            image_path = opening or images[0]
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


def _split_caption_for_two_lines(
    value: Any,
    *,
    max_chars_per_line: int = 40,
    max_lines: int = 2,
) -> List[str]:
    """Split a caption without changing, dropping or cutting any word."""
    text = re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()
    if not text:
        return []
    words = text.split(" ")
    blocks: List[str] = []
    lines: List[str] = []
    current: List[str] = []

    def flush_line() -> None:
        nonlocal current
        if current:
            lines.append(" ".join(current))
            current = []

    def flush_block() -> None:
        nonlocal lines
        flush_line()
        if lines:
            blocks.append("\n".join(lines[:max_lines]))
            lines = []

    for word in words:
        candidate = " ".join([*current, word]) if current else word
        if current and len(candidate) > max_chars_per_line:
            flush_line()
            if len(lines) >= max_lines:
                flush_block()
        current.append(word)

        # Prefer natural phrase boundaries once the block is readable. This
        # reduces the chance of a caption changing in the middle of a thought.
        visible_words = sum(len(line.split()) for line in lines) + len(current)
        if re.search(r"[.!?…;:]$", word) and visible_words >= 3:
            flush_block()

    flush_block()
    return blocks


def build_srt_text(
    captions: Sequence[Dict[str, Any]],
    *,
    max_duration: float,
    opening_silence_sec: float = 0.0,
) -> str:
    blocks: List[str] = []
    limit = max(0.1, float(max_duration or 0.0))
    prepared: List[Dict[str, Any]] = []
    for raw in captions or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("caption") or raw.get("text") or "").strip()
        if not text:
            continue
        start = max(0.0, _safe_duration(raw.get("start")))
        end = _safe_duration(raw.get("end"))
        if end > start:
            prepared.append({"text": text, "start": start, "end": end})

    desired_first = max(0.0, float(opening_silence_sec or 0.0))
    earliest = min((item["start"] for item in prepared), default=desired_first)
    shift = max(0.0, desired_first - earliest) if earliest < desired_first - 0.05 else 0.0
    index = 1
    for item in prepared:
        text = str(item["text"]).replace("\r", " ").strip()
        start = max(desired_first, float(item["start"]) + shift)
        end = min(limit, float(item["end"]) + shift)
        if end <= start:
            continue
        chunks = _split_caption_for_two_lines(text)
        if not chunks:
            continue
        weights = [max(1, len(re.sub(r"\s+", "", chunk))) for chunk in chunks]
        weight_total = max(1, sum(weights))
        cursor = start
        for chunk_index, chunk in enumerate(chunks):
            if chunk_index == len(chunks) - 1:
                chunk_end = end
            else:
                chunk_end = min(
                    end,
                    cursor + ((end - start) * weights[chunk_index] / weight_total),
                )
            if chunk_end <= cursor:
                continue
            blocks.append(
                f"{index}\n{_srt_timestamp(cursor)} --> {_srt_timestamp(chunk_end)}\n{chunk}\n"
            )
            index += 1
            cursor = chunk_end
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


def _build_logo_only_brand_frames(
    *,
    logo_path: str,
    output_dir: str,
    video_size: Tuple[int, int],
    opening_title: str,
    channel_name: str,
) -> Tuple[str, str, str]:
    """Build exact-size opening, story and endcard frames for logo-only video."""
    from PIL import Image, ImageDraw, ImageFont

    width, height = int(video_size[0]), int(video_size[1])
    if width <= 0 or height <= 0:
        raise RuntimeError("Dimensão inválida para os quadros de identidade visual.")
    logo = Image.open(logo_path).convert("RGBA")

    def font(size: int, *, bold: bool = False):
        candidates = (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        )
        for candidate in candidates:
            try:
                return ImageFont.truetype(candidate, max(12, int(size)))
            except Exception:
                continue
        return ImageFont.load_default()

    def background() -> Image.Image:
        frame = Image.new("RGB", (width, height), (15, 17, 39))
        draw = ImageDraw.Draw(frame)
        for y in range(height):
            ratio = y / max(1, height - 1)
            color = (
                int(24 + (16 * ratio)),
                int(25 + (10 * ratio)),
                int(68 + (28 * ratio)),
            )
            draw.line((0, y, width, y), fill=color)
        return frame

    def wrap_pixels(draw: Any, value: str, selected_font: Any, max_width: int, max_lines: int) -> List[str]:
        words = re.sub(r"\s+", " ", str(value or "")).strip().split()
        lines: List[str] = []
        current: List[str] = []
        for word in words:
            candidate = " ".join([*current, word])
            bbox = draw.textbbox((0, 0), candidate, font=selected_font)
            if current and (bbox[2] - bbox[0]) > max_width:
                lines.append(" ".join(current))
                current = [word]
                if len(lines) >= max_lines:
                    break
            else:
                current.append(word)
        if current and len(lines) < max_lines:
            lines.append(" ".join(current))
        return lines[:max_lines]

    opening = background()
    opening_logo = logo.copy()
    opening_logo.thumbnail((int(width * 0.24), int(height * 0.25)), Image.Resampling.LANCZOS)
    opening.paste(
        opening_logo,
        ((width - opening_logo.width) // 2, int(height * 0.10)),
        opening_logo,
    )
    opening_draw = ImageDraw.Draw(opening)
    title_font = font(max(28, int(height * 0.064)), bold=True)
    title_lines = wrap_pixels(opening_draw, opening_title, title_font, int(width * 0.78), 3)
    line_height = int(getattr(title_font, "size", 36) * 1.22)
    title_y = int(height * 0.43)
    for line_index, line in enumerate(title_lines):
        bbox = opening_draw.textbbox((0, 0), line, font=title_font, stroke_width=2)
        opening_draw.text(
            ((width - (bbox[2] - bbox[0])) / 2, title_y + line_index * line_height),
            line,
            font=title_font,
            fill=(255, 255, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0),
        )
    channel_font = font(max(18, int(height * 0.035)), bold=True)
    channel = re.sub(r"\s+", " ", str(channel_name or "HERDEIROS DAS PROMESSAS")).strip().upper()
    channel_bbox = opening_draw.textbbox((0, 0), channel, font=channel_font)
    opening_draw.text(
        ((width - (channel_bbox[2] - channel_bbox[0])) / 2, int(height * 0.84)),
        channel,
        font=channel_font,
        fill=(218, 210, 255),
    )

    story = background()
    story_logo = logo.copy()
    story_logo.thumbnail((int(width * 0.18), int(height * 0.15)), Image.Resampling.LANCZOS)
    logo_x = max(24, int(width * 0.035))
    # The caption safe area occupies the bottom centre. Keep the fixed logo
    # above it and in the lower-left corner so the two never overlap.
    logo_y = max(24, height - story_logo.height - max(36, int(height * 0.25)))
    story.paste(story_logo, (logo_x, logo_y), story_logo)

    endcard = background()
    endcard_logo = logo.copy()
    endcard_logo.thumbnail((int(width * 0.22), int(height * 0.22)), Image.Resampling.LANCZOS)
    endcard.paste(
        endcard_logo,
        ((width - endcard_logo.width) // 2, int(height * 0.12)),
        endcard_logo,
    )
    endcard_draw = ImageDraw.Draw(endcard)
    end_channel_font = font(max(21, int(height * 0.045)), bold=True)
    end_channel_lines = wrap_pixels(
        endcard_draw,
        channel,
        end_channel_font,
        int(width * 0.82),
        2,
    )
    end_channel_y = int(height * 0.47)
    for line_index, line in enumerate(end_channel_lines):
        bbox = endcard_draw.textbbox((0, 0), line, font=end_channel_font)
        endcard_draw.text(
            ((width - (bbox[2] - bbox[0])) / 2, end_channel_y + line_index * int(end_channel_font.size * 1.18)),
            line,
            font=end_channel_font,
            fill=(255, 255, 255),
        )
    end_cta_font = font(max(15, int(height * 0.026)), bold=True)
    end_cta_lines = wrap_pixels(
        endcard_draw,
        "CURTA • INSCREVA-SE • ATIVE O SININHO • COMPARTILHE • COMENTE",
        end_cta_font,
        int(width * 0.84),
        2,
    )
    end_cta_y = int(height * 0.69)
    for line_index, line in enumerate(end_cta_lines):
        bbox = endcard_draw.textbbox((0, 0), line, font=end_cta_font)
        endcard_draw.text(
            ((width - (bbox[2] - bbox[0])) / 2, end_cta_y + line_index * int(end_cta_font.size * 1.25)),
            line,
            font=end_cta_font,
            fill=(218, 210, 255),
        )

    opening_path = os.path.join(output_dir, "logo_only_opening.png")
    story_path = os.path.join(output_dir, "logo_only_story.png")
    endcard_path = os.path.join(output_dir, "logo_only_endcard.png")
    opening.save(opening_path, format="PNG")
    story.save(story_path, format="PNG")
    endcard.save(endcard_path, format="PNG")
    return opening_path, story_path, endcard_path


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
    # libass scales SRT styles from its 288px script coordinate system. Fixed
    # style values therefore scale naturally with the output height; deriving
    # them from ``height`` here would apply the scale twice and create giant
    # captions on 720p/1080p videos.
    caption_font_size = 18
    caption_margin = 24
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps=24,format=yuv420p,"
        f"subtitles='{subtitle_path}':original_size={width}x{height}:"
        f"force_style='FontName=DejaVu Sans,FontSize={caption_font_size},PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BackColour=&H64000000,BorderStyle=1,"
        f"Outline=2,Shadow=1,Alignment=2,MarginV={caption_margin}'"
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
    opening_silence_sec: float = 0.0,
    logo_only_visuals: bool = False,
    opening_title: str = "",
    channel_name: str = "Herdeiros das Promessas",
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
    local_images = _clean_existing_paths(selected_images)
    if not local_images:
        raise RuntimeError("Nenhuma imagem local válida disponível para o render leve de recuperação.")

    output_abs = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_abs) or ".", exist_ok=True)
    local_music = _local_music_candidate(music_dir, music_mood)

    if progress_callback:
        progress_callback(
            90,
            "6/8 Render rápido com a identidade do canal — preparando FFmpeg local..."
            if logo_only_visuals
            else "6/8 Render leve confirmado — preparando FFmpeg local...",
        )

    started = time.time()
    with tempfile.TemporaryDirectory(prefix="codexia-light-recovery-", dir=os.path.dirname(output_abs) or None) as tmp:
        opening_frame = ""
        story_frame = ""
        brand_endcard_frame = ""
        if logo_only_visuals:
            opening_frame, story_frame, brand_endcard_frame = _build_logo_only_brand_frames(
                logo_path=local_images[0],
                output_dir=tmp,
                video_size=video_size,
                opening_title=opening_title,
                channel_name=channel_name,
            )
        segments = build_visual_segments(
            # Every logo-only frame has the exact output resolution and RGB
            # pixel format. Feeding the raw logo (often square/RGBA) into the
            # same concat stream can corrupt frames when dimensions change.
            selected_images=[opening_frame] if logo_only_visuals else local_images,
            official_scene_timeline=official_scene_timeline,
            target_duration=total,
            endcard_image=brand_endcard_frame if logo_only_visuals else endcard_image,
            opening_image=opening_frame,
            story_image=story_frame,
        )
        if not segments:
            raise RuntimeError("Nenhuma imagem local válida disponível para o render leve de recuperação.")
        concat_path = os.path.join(tmp, "visuals.ffconcat")
        srt_path = os.path.join(tmp, "captions.srt")
        with open(concat_path, "w", encoding="utf-8") as fh:
            fh.write(build_concat_text(segments))
        srt_text = build_srt_text(
            captions,
            max_duration=total,
            opening_silence_sec=opening_silence_sec,
        )
        if not srt_text.strip():
            raise RuntimeError("Legenda canônica vazia; o render foi bloqueado.")
        with open(srt_path, "w", encoding="utf-8") as fh:
            fh.write(srt_text)

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
        max_runtime = max(180.0, min(1800.0, (total * 1.5) + 120.0))
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
            process.stdout.close()
        except Exception:
            try:
                process.terminate()
                process.wait(timeout=10)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            if process.stdout is not None:
                process.stdout.close()
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
        progress_callback(
            100,
            "Vídeo renderizado com sucesso pelo modo rápido da identidade do canal."
            if logo_only_visuals
            else "Vídeo renderizado com sucesso pelo modo leve de recuperação.",
        )

    render_seconds = max(0.0, time.time() - started)
    return {
        "file_path": output_abs,
        "duration_sec": round(obtained, 3),
        "render_seconds": round(render_seconds, 3),
        "render_realtime_factor": round(render_seconds / max(0.1, obtained), 4),
        "visual_segment_count": len(segments),
        "caption_count": srt_text.count(" --> "),
        "caption_max_lines": 2,
        "caption_max_chars_per_line": 40,
        "captions_begin_after_opening_sec": round(max(0.0, float(opening_silence_sec or 0.0)), 3),
        "logo_only_visuals": bool(logo_only_visuals),
        "opening_visual_only_sec": round(max(0.0, float(opening_silence_sec or 0.0)), 3),
        "fixed_logo_position": "bottom_left_above_captions" if logo_only_visuals else "not_applicable",
        "music_source": "local_existing_file" if local_music else "none",
        "music_path": local_music,
        "paid_provider_calls": 0,
        "external_downloads": 0,
        "renderer": "ffmpeg_static_brand_v3" if logo_only_visuals else "ffmpeg_concat_subtitles_v1",
        "motion_policy": "static_preserved_visuals_hard_cuts",
    }
