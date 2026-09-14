from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from app.config import VIDEO_OUTPUT_DIR, VIDEO_URL_PREFIX


class CinematicCompositorError(RuntimeError):
    pass


class CinematicCompositor:
    """Post-process a canonical Codexia render with true generative motion.

    The canonical pipeline remains responsible for narration, timing, music,
    captions, branding and validation.  This compositor replaces only selected
    visual windows with muted AI-generated clips and keeps the base audio.

    Existing burned captions would be hidden under a full-screen replacement.
    To preserve them, we read the canonical SRT and re-burn only subtitle events
    that overlap replacement windows.  Outside those windows the untouched base
    captions remain exactly as rendered by the canonical pipeline.
    """

    def __init__(self, output_dir: Optional[str] = None):
        self.output_dir = str(output_dir or VIDEO_OUTPUT_DIR)
        os.makedirs(self.output_dir, exist_ok=True)

    @staticmethod
    def _ffmpeg() -> str:
        path = shutil.which("ffmpeg")
        if not path:
            raise CinematicCompositorError("FFmpeg não está disponível no servidor.")
        return path

    @staticmethod
    def _ffprobe() -> str:
        path = shutil.which("ffprobe")
        if not path:
            raise CinematicCompositorError("FFprobe não está disponível no servidor.")
        return path

    @classmethod
    def _probe_video(cls, path: str) -> Dict[str, Any]:
        if not path or not os.path.isfile(path):
            raise CinematicCompositorError(f"Vídeo base não encontrado: {path}")
        cmd = [
            cls._ffprobe(), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration:format=duration",
            "-of", "default=noprint_wrappers=1", path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 0:
            raise CinematicCompositorError("FFprobe não conseguiu ler o vídeo base.")
        parsed: Dict[str, str] = {}
        for line in (result.stdout or "").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                parsed[key.strip()] = value.strip()
        try:
            width = int(parsed.get("width") or 1920)
            height = int(parsed.get("height") or 1080)
        except Exception:
            width, height = 1920, 1080
        try:
            duration = float(parsed.get("duration") or 0.0)
        except Exception:
            duration = 0.0
        return {"width": width, "height": height, "duration": duration}

    @staticmethod
    def _number(item: Dict[str, Any], keys: Sequence[str], default: float = 0.0) -> float:
        for key in keys:
            try:
                value = item.get(key)
                if value is not None and str(value).strip() != "":
                    return float(value)
            except Exception:
                continue
        return float(default)

    @classmethod
    def scene_windows_from_render_report(cls, render_report: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Normalize the official scene timeline produced by VideoGenerator.

        Field names are intentionally tolerant because old successful tasks can
        have timeline entries from earlier pipeline revisions.
        """
        raw = render_report.get("scene_timeline") if isinstance(render_report, dict) else None
        if not isinstance(raw, list):
            return []
        out: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or item.get("type") or "scene").strip().lower()
            if kind in {"opening", "intro", "closing", "cta", "endcard", "end_card", "outro"}:
                continue
            start = cls._number(item, ("visual_start", "start", "clip_start", "timeline_start", "audio_start"), 0.0)
            end = cls._number(item, ("visual_end", "end", "clip_end", "timeline_end", "audio_end"), start)
            if end <= start:
                duration = cls._number(item, ("duration", "duration_sec", "visual_duration"), 0.0)
                end = start + max(0.0, duration)
            if end <= start:
                continue
            try:
                explicit_index = int(item.get("scene_index") or item.get("index") or 0)
            except Exception:
                explicit_index = 0
            out.append({
                "scene_index": explicit_index or (len(out) + 1),
                "start": round(max(0.0, start), 3),
                "end": round(max(start, end), 3),
                "kind": kind,
                "raw": item,
            })
        return out

    @staticmethod
    def _download_clip(url: str, directory: str, index: int) -> str:
        raw = str(url or "").strip()
        if not raw.lower().startswith(("https://", "http://")):
            if os.path.isfile(raw):
                return os.path.abspath(raw)
            raise CinematicCompositorError("O clipe de movimento precisa ser arquivo local ou URL HTTP(S).")
        destination = os.path.join(directory, f"motion_{index:03d}.mp4")
        try:
            with requests.get(raw, stream=True, timeout=(20, 180)) as response:
                response.raise_for_status()
                total = 0
                with open(destination, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > 250 * 1024 * 1024:
                            raise CinematicCompositorError("Clipe de movimento excede 250 MB.")
                        handle.write(chunk)
        except CinematicCompositorError:
            raise
        except Exception as exc:
            raise CinematicCompositorError(f"Falha ao baixar clipe de movimento: {exc}") from exc
        if not os.path.isfile(destination) or os.path.getsize(destination) < 1024:
            raise CinematicCompositorError("Clipe de movimento baixado está vazio.")
        return destination

    @staticmethod
    def _srt_timestamp_to_seconds(value: str) -> float:
        match = re.match(r"\s*(\d+):(\d+):(\d+)[,.](\d+)\s*", str(value or ""))
        if not match:
            return 0.0
        h, m, s, ms = [int(x) for x in match.groups()]
        return (h * 3600.0) + (m * 60.0) + s + (ms / (1000.0 if len(str(ms)) >= 3 else 100.0))

    @classmethod
    def _parse_srt(cls, path: str) -> List[Tuple[float, float, str]]:
        if not path or not os.path.isfile(path):
            return []
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip())
        events: List[Tuple[float, float, str]] = []
        for block in blocks:
            lines = [line for line in block.split("\n") if line.strip()]
            if len(lines) < 2:
                continue
            time_idx = next((i for i, line in enumerate(lines) if "-->" in line), -1)
            if time_idx < 0:
                continue
            left, right = [part.strip() for part in lines[time_idx].split("-->", 1)]
            start = cls._srt_timestamp_to_seconds(left)
            end = cls._srt_timestamp_to_seconds(right)
            caption = "\n".join(lines[time_idx + 1 :]).strip()
            if caption and end > start:
                events.append((start, end, caption))
        return events

    @staticmethod
    def _seconds_to_srt(value: float) -> str:
        ms_total = max(0, int(round(float(value) * 1000.0)))
        h, remainder = divmod(ms_total, 3_600_000)
        m, remainder = divmod(remainder, 60_000)
        s, ms = divmod(remainder, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    @classmethod
    def _write_replacement_srt(cls, original_srt: str, windows: Sequence[Tuple[float, float]], output_path: str) -> Optional[str]:
        events = cls._parse_srt(original_srt)
        if not events:
            return None
        kept: List[Tuple[float, float, str]] = []
        for start, end, caption in events:
            if any(end > win_start and start < win_end for win_start, win_end in windows):
                kept.append((start, end, caption))
        if not kept:
            return None
        lines: List[str] = []
        for idx, (start, end, caption) in enumerate(kept, start=1):
            lines.extend([
                str(idx),
                f"{cls._seconds_to_srt(start)} --> {cls._seconds_to_srt(end)}",
                caption,
                "",
            ])
        Path(output_path).write_text("\n".join(lines), encoding="utf-8")
        return output_path

    @staticmethod
    def _escape_subtitles_path(path: str) -> str:
        # ffmpeg subtitles filter parses ':' and backslashes specially.
        return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    def compose(
        self,
        *,
        base_video_path: str,
        motion_clips: Sequence[Dict[str, Any]],
        render_report: Optional[Dict[str, Any]] = None,
        output_filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        base = os.path.abspath(str(base_video_path or "").strip())
        info = self._probe_video(base)
        width, height = int(info["width"]), int(info["height"])
        base_duration = float(info.get("duration") or 0.0)
        timeline = self.scene_windows_from_render_report(render_report or {})
        by_index = {int(item["scene_index"]): item for item in timeline}

        prepared: List[Dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="codexia-cinematic-") as temp_dir:
            for idx, request in enumerate(motion_clips or [], start=1):
                if not isinstance(request, dict):
                    continue
                try:
                    scene_index = int(request.get("scene_index") or idx)
                except Exception:
                    scene_index = idx
                window = by_index.get(scene_index)
                explicit_start = self._number(request, ("start", "start_sec"), -1.0)
                explicit_end = self._number(request, ("end", "end_sec"), -1.0)
                if explicit_start >= 0 and explicit_end > explicit_start:
                    start, end = explicit_start, explicit_end
                elif window:
                    start, end = float(window["start"]), float(window["end"])
                else:
                    raise CinematicCompositorError(f"Não encontrei a janela temporal da cena {scene_index} no render_report.")
                if base_duration > 0:
                    start = min(start, max(0.0, base_duration - 0.05))
                    end = min(end, base_duration)
                requested_motion = self._number(request, ("motion_seconds", "duration_seconds"), 0.0)
                if requested_motion > 0:
                    end = min(end, start + requested_motion)
                if end - start < 0.2:
                    continue
                source = request.get("clip_path") or request.get("output_url") or request.get("url")
                clip_path = self._download_clip(str(source or ""), temp_dir, idx)
                clip_info = self._probe_video(clip_path)
                clip_duration = float(clip_info.get("duration") or 0.0)
                if clip_duration > 0:
                    end = min(end, start + clip_duration)
                if end - start < 0.2:
                    continue
                prepared.append({"scene_index": scene_index, "start": start, "end": end, "path": clip_path})

            if not prepared:
                raise CinematicCompositorError("Nenhum clipe de movimento válido foi informado.")

            output_name = str(output_filename or f"cinematic_{uuid.uuid4().hex}.mp4").strip()
            if not output_name.lower().endswith(".mp4"):
                output_name += ".mp4"
            output_name = os.path.basename(output_name)
            output_path = os.path.join(self.output_dir, output_name)

            cmd: List[str] = [self._ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", base]
            for item in prepared:
                cmd.extend(["-i", item["path"]])

            filter_parts: List[str] = []
            current = "[0:v]"
            for input_idx, item in enumerate(prepared, start=1):
                start, end = float(item["start"]), float(item["end"])
                duration = max(0.2, end - start)
                clip_label = f"clip{input_idx}"
                out_label = f"mix{input_idx}"
                filter_parts.append(
                    f"[{input_idx}:v]setpts=PTS-STARTPTS,"
                    f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height},fps=30,trim=duration={duration:.3f},"
                    f"setpts=PTS-STARTPTS+{start:.3f}/TB[{clip_label}]"
                )
                filter_parts.append(
                    f"{current}[{clip_label}]overlay=0:0:eof_action=pass:enable='between(t,{start:.3f},{end:.3f})'[{out_label}]"
                )
                current = f"[{out_label}]"

            report = render_report or {}
            srt_path = ""
            try:
                srt_path = str(((report.get("srt") or {}).get("path") or "")).strip()
            except Exception:
                srt_path = ""
            replacement_windows = [(float(item["start"]), float(item["end"])) for item in prepared]
            filtered_srt = self._write_replacement_srt(
                srt_path,
                replacement_windows,
                os.path.join(temp_dir, "replacement-captions.srt"),
            ) if srt_path else None
            final_label = "vfinal"
            if filtered_srt:
                escaped = self._escape_subtitles_path(filtered_srt)
                filter_parts.append(
                    f"{current}subtitles='{escaped}':force_style='Alignment=2,MarginV=55,Outline=3,Shadow=1'[{final_label}]"
                )
            else:
                filter_parts.append(f"{current}null[{final_label}]")

            cmd.extend([
                "-filter_complex", ";".join(filter_parts),
                "-map", f"[{final_label}]",
                "-map", "0:a?",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                "-shortest",
                output_path,
            ])
            timeout = max(300.0, min(7200.0, (base_duration or 600.0) * 8.0))
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
            if result.returncode != 0:
                try:
                    if os.path.exists(output_path):
                        os.remove(output_path)
                except Exception:
                    pass
                raise CinematicCompositorError("FFmpeg falhou na composição cinematográfica: " + str(result.stderr or "")[-1200:])
            if not os.path.isfile(output_path) or os.path.getsize(output_path) < 50 * 1024:
                raise CinematicCompositorError("A composição cinematográfica não gerou um MP4 válido.")
            final_info = self._probe_video(output_path)
            if base_duration > 0 and abs(float(final_info.get("duration") or 0.0) - base_duration) > 1.0:
                raise CinematicCompositorError(
                    f"Duração do pós-processamento divergiu do vídeo base: base={base_duration:.2f}s final={float(final_info.get('duration') or 0.0):.2f}s."
                )
            return {
                "file_path": output_path,
                "video_url": f"{VIDEO_URL_PREFIX}/{output_name}",
                "base_duration_sec": round(base_duration, 3),
                "final_duration_sec": round(float(final_info.get("duration") or 0.0), 3),
                "motion_windows": [
                    {"scene_index": item["scene_index"], "start": round(item["start"], 3), "end": round(item["end"], 3)}
                    for item in prepared
                ],
                "captions_restored_over_motion": bool(filtered_srt),
                "audio_source": "canonical_base_video",
            }
