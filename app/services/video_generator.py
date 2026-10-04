Warning: truncated output (original token count: 100454)
Total output lines: 7992

import os
import uuid
import requests
import gc
import threading
import asyncio
import re
import time
import difflib
import unicodedata
import math
import hashlib
from contextlib import contextmanager
from typing import Optional, Callable, List, Dict, Any

from app.config import VIDEO_OUTPUT_DIR, VIDEO_URL_PREFIX, STATIC_DIR
from app.services.media_probe import duration_sync_tolerance_seconds
from app.services.narrative_structure_standard import (
    CHANNEL_PRESENTATION_TEXT,
    DEFAULT_NARRATED_CTA_TEXT,
)
from app.services.recovery_image_budget import RecoveryImageCallBudget
from app.services.recovery_image_budget import RecoveryImageBudgetExceeded
from app.services.safe_text_layout import SafeTextLayout
from app.services.narration_duration_feedback import (
    calibrated_body_duration_target,
    expansion_word_range,
    planning_duration_bounds,
)

CAPTION_SAFE_AREA_X_RATIO = 0.06
CAPTION_SAFE_AREA_TOP_RATIO = 0.08
CAPTION_SAFE_AREA_BOTTOM_RATIO = 0.08
DEFAULT_SCENE_TRANSITION_SEC = 0.30
DEFAULT_SCENE_AUDIO_MARGIN_SEC = 0.40
# Abertura visual global: título, logo e nome do canal sem fala/legenda.
DEFAULT_OPENING_SILENCE_SEC = 4.0
DEFAULT_SCENE_IMAGE_LEAD_SEC = 0.30
DEFAULT_SCENE_CAPTION_LEAD_SEC = 0.20
DEFAULT_CINEMATIC_END_SCREEN_SEC = 4.0
DEFAULT_MAX_CINEMATIC_VISUAL_HOLD_SEC = 7.0
REAL_AUDIO_CAPTION_TIMELINE_SOURCES = {
    "official_audio_transcript",
    "approved_edge_tts_word_boundaries",
    "local_audio_activity_alignment",
}


def _bounded_timeout_seconds(env_name: str, default: int) -> int:
    try:
        raw = int((os.getenv(env_name) or "").strip() or str(default))
    except Exception:
        raw = int(default)
    return max(30, min(180, raw))


@contextmanager
def _narration_activity_pulse(
    callback: Optional[Callable[[str], None]],
    message: str,
    *,
    interval_seconds: Optional[float] = None,
):
    """Mantém o heartbeat da tarefa vivo enquanto um provedor bloqueia a thread."""
    if not callback:
        yield
        return
    stop_event = threading.Event()
    try:
        interval = float(
            interval_seconds
            if interval_seconds is not None
            else (os.getenv("NARRATION_HEARTBEAT_SECONDS") or "15")
        )
    except Exception:
        interval = 15.0
    interval = max(0.01 if interval_seconds is not None else 5.0, min(60.0, interval))

    def _emit() -> None:
        try:
            callback(message)
        except Exception:
            pass

    def _run() -> None:
        while not stop_event.wait(interval):
            _emit()

    _emit()
    thread = threading.Thread(target=_run, name="narration-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=min(1.0, interval + 0.1))

class VideoGenerator:
    def __init__(self, output_dir=None, ai_service=None):
        self.output_dir = output_dir or VIDEO_OUTPUT_DIR
        os.makedirs(self.output_dir, exist_ok=True)
        self.music_dir = "app/static/music"
        os.makedirs(self.music_dir, exist_ok=True)
        self.generated_dir = os.path.join(str(STATIC_DIR), "generated")
        os.makedirs(self.generated_dir, exist_ok=True)
        self.ai_service = ai_service
        self.MUSIC_CREDITS = {
            "drama": "Music: Impact Prelude by Kevin MacLeod\nFree download: https://filmmusic.io/song/3900-impact-prelude\nLicense (CC BY 4.0): https://filmmusic.io/standard-license",
            "epic": "Music: Impact Andante by Kevin MacLeod\nFree download: https://filmmusic.io/song/3898-impact-andante\nLicense (CC BY 4.0): https://filmmusic.io/standard-license",
            "happy": "Music: Carefree by Kevin MacLeod\nFree download: https://filmmusic.io/song/3476-carefree\nLicense (CC BY 4.0): https://filmmusic.io/standard-license"
        }
        self._last_tts_debug: Dict[str, Any] = {}
        self._last_image_prompt_debug: Dict[str, Any] = {}
        # self._ensure_fallback_music() removido do init para evitar delay no startup

    #region debug-point youtube-finalize-stuck
    def _dbg_event(self, hypothesis_id: str, msg: str, data: Optional[Dict[str, Any]] = None):
        try:
            import json as _json
            import urllib.request as _urlreq

            env_path = os.path.join(".dbg", "youtube-finalize-stuck.env")
            url = "http://127.0.0.1:7777/event"
            session_id = "youtube-finalize-stuck"
            if os.path.exists(env_path):
                try:
                    with open(env_path, "r", encoding="utf-8") as f:
                        for line in f.read().splitlines():
                            if line.startswith("DEBUG_SERVER_URL="):
                                url = line.split("=", 1)[1].strip() or url
                            elif line.startswith("DEBUG_SESSION_ID="):
                                session_id = line.split("=", 1)[1].strip() or session_id
                except Exception:
                    pass

            run_id = str(os.getenv("DEBUG_RUN_ID") or "pre").strip() or "pre"
            payload = {
                "sessionId": session_id,
                "runId": run_id,
                "hypothesisId": str(hypothesis_id or "").strip() or "NA",
                "location": "app/services/video_generator.py",
                "msg": str(msg or ""),
                "data": data or {},
            }
            req = _urlreq.Request(
                url,
                data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            _urlreq.urlopen(req, timeout=0.25).read()
        except Exception:
            pass
    #endregion

    def _summarize_tts_failure(self, tts_debug: Optional[Dict[str, Any]]) -> str:
        info = dict(tts_debug or {})
        configured = str(info.get("configured_provider") or "desconhecido").strip()
        used = str(info.get("provider_used") or "").strip()
        attempts = info.get("attempts") or []
        parts = [f"Provider configurado: {configured}."]
        if used:
            parts.append(f"Provider usado: {used}.")
        if attempts:
            items = []
            for attempt in attempts[:6]:
                provider = str(attempt.get("provider") or "desconhecido").strip()
                status = str(attempt.get("status") or "unknown").strip()
                reason = str(attempt.get("reason") or "").strip()
                items.append(f"{provider}={status}" + (f" ({reason})" if reason else ""))
            if items:
                parts.append("Tentativas: " + "; ".join(items) + ".")
        summary = str(info.get("error_summary") or "").strip()
        if summary:
            parts.append(summary)
        return " ".join(part for part in parts if part).strip()

    def _ffprobe_duration_seconds(self, path: str) -> float:
        try:
            import subprocess
            abs_path = os.path.abspath(path)
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", abs_path],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if r.returncode != 0:
                return 0.0
            s = (r.stdout or "").strip()
            try:
                return float(s) if s else 0.0
            except Exception:
                return 0.0
        except Exception:
            return 0.0

    def _prepare_approved_audio_for_visual_opening(
        self,
        source_path: str,
        silence_seconds: float,
    ) -> str:
        """Create a local render derivative with silence before approved speech.

        The frozen MP3 remains untouched and is still the sole narration source.
        This operation only delays that verified audio; it never calls TTS or
        changes the spoken text.
        """
        source = os.path.abspath(str(source_path or "").strip())
        silence = max(0.0, float(silence_seconds or 0.0))
        if silence <= 0:
            return source
        if not source or not os.path.isfile(source) or os.path.getsize(source) <= 1000:
            raise RuntimeError("MP3 aprovado indisponível para aplicar a abertura visual.")

        try:
            import shutil
            import subprocess

            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("FFmpeg não está disponível para preparar a abertura visual.")
            output = os.path.join(
                self.output_dir,
                f"approved_narration_opening_{uuid.uuid4().hex}.mp3",
            )
            delay_ms = max(1, int(round(silence * 1000.0)))
            source_duration = float(self._ffprobe_duration_seconds(source) or 0.0)
            timeout = max(120.0, min(1800.0, (source_duration * 2.0) + 60.0))
            result = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-i", source,
                    "-map", "0:a:0",
                    "-af", f"adelay={delay_ms}:all=1",
                    "-vn",
                    "-c:a", "libmp3lame",
                    "-b:a", "192k",
                    output,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if result.returncode != 0 or not os.path.isfile(output) or os.path.getsize(output) <= 1000:
                raise RuntimeError(
                    "FFmpeg não conseguiu aplicar a abertura silenciosa: "
                    + str(result.stderr or "erro desconhecido")[-500:]
                )
            obtained = float(self._ffprobe_duration_seconds(output) or 0.0)
            expected = source_duration + silence if source_duration > 0 else silence
            if obtained <= 0 or (expected > 0 and obtained < expected - 0.35):
                raise RuntimeError(
                    f"Áudio preparado ficou curto: obtido={obtained:.2f}s esperado={expected:.2f}s."
                )
            return output
        except Exception:
            try:
                if "output" in locals() and output and os.path.isfile(output):
                    os.remove(output)
            except Exception:
                pass
            raise

    def _is_ffprobe_available(self) -> bool:
        try:
            import shutil
            return bool(shutil.which("ffprobe"))
        except Exception:
            return False

    def _ffprobe_stream_duration_seconds(self, path: str) -> float:
        try:
            import subprocess
            abs_path = os.path.abspath(path)
            r = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    abs_path,
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if r.returncode != 0:
                return 0.0
            s = (r.stdout or "").strip()
            return float(s) if s else 0.0
        except Exception:
            return 0.0

    def _measure_rendered_video_duration_seconds(self, path: str, attempts: int = 4, retry_delay_sec: float = 0.6) -> float:
        abs_path = os.path.abspath(path or "")
        if not abs_path or not os.path.exists(abs_path):
            return 0.0

        def _moviepy_duration() -> float:
            try:
                try:
                    from moviepy.editor import VideoFileClip
                except ImportError:
                    from moviepy import VideoFileClip
                clip = VideoFileClip(abs_path)
                try:
                    return float(getattr(clip, "duration", 0) or 0.0)
                finally:
                    clip.close()
            except Exception:
                return 0.0

        attempts = max(1, int(attempts or 1))
        retry_delay_sec = max(0.0, float(retry_delay_sec or 0.0))
        measured = 0.0
        for attempt_idx in range(attempts):
            for probe in (
                self._ffprobe_duration_seconds,
                self._ffprobe_stream_duration_seconds,
                lambda candidate: _moviepy_duration(),
            ):
                try:
                    duration = float(probe(abs_path) or 0.0)
                except Exception:
                    duration = 0.0
                if duration > 0.1:
                    return duration
                measured = max(measured, duration)
            if attempt_idx < attempts - 1 and retry_delay_sec > 0:
                time.sleep(retry_delay_sec * float(attempt_idx + 1))
        return measured

    def _ensure_playable_mp4(self, path: str) -> str:
        try:
            if not path or not os.path.exists(path):
                raise Exception("Arquivo de vídeo não encontrado.")
            if os.path.getsize(path) < 1024 * 50:
                raise Exception("Arquivo de vídeo muito pequeno (provável falha no render).")
        except Exception:
            raise

        dur = self._measure_rendered_video_duration_seconds(path)
        if dur >= 0.5:
            return path

        try:
            import subprocess
            fixed = f"{path}.fixed.mp4"
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", path, "-c", "copy", "-movflags", "+faststart", "-pix_fmt", "yuv420p", fixed],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if r.returncode == 0 and os.path.exists(fixed) and os.path.getsize(fixed) > 1024 * 50:
                dur2 = self._measure_rendered_video_duration_seconds(fixed)
                if dur2 >= 0.5:
                    try:
                        os.replace(fixed, path)
                    except Exception:
                        return fixed
                    return path
        except Exception:
            pass

        raise Exception("Vídeo gerado inválido (duração 0s). Verifique ffmpeg e armazenamento /data.")

    def _ensure_fallback_music(self):
        """Baixa músicas de fallback se a pasta estiver vazia"""
        try:
            import glob
            if not glob.glob(os.path.join(self.music_dir, "*.mp3")):
                print("Baixando músicas de fallback...")
                music_urls = {
                    "drama.mp3": "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Impact%20Prelude.mp3",
                    "epic.mp3": "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Impact%20Andante.mp3",
                    "happy.mp3": "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Carefree.mp3"
                }
                
                for filename, url in music_urls.items():
                    try:
                        print(f"Baixando {filename}...")
                        response = requests.get(url, timeout=30)
                        if response.status_code == 200:
                            with open(os.path.join(self.music_dir, filename), 'wb') as f:
                                f.write(response.content)
                    except Exception as e:
                        print(f"Erro ao baixar {filename}: {e}")
        except Exception as e:
            print(f"Erro no setup de músicas: {e}")

    def create_text_image(self, text, size=(1080, 1920), bg_color=(20, 20, 20), text_color=(255, 255, 255), bg_image_path=None, footer_text: Optional[str] = None):
        from PIL import Image, ImageEnhance
        import numpy as np

        bg = None
        if bg_image_path and os.path.exists(bg_image_path):
            try:
                bg = Image.open(bg_image_path).convert("RGB")
            except Exception as e:
                print(f"Erro ao carregar imagem de fundo: {e}")
                bg = None
        if bg is None:
            try:
                fallback_bg_path = self._generate_fallback_background(size)
                if fallback_bg_path and os.path.exists(fallback_bg_path):
                    bg = Image.open(fallback_bg_path).convert("RGB")
            except Exception:
                bg = None
        if bg is None:
            bg = Image.new("RGB", size, color=bg_color)
        else:
            img_ratio = bg.width / max(1, bg.height)
            target_ratio = size[0] / max(1, size[1])
            if img_ratio > target_ratio:
                new_height = size[1]
                new_width = int(new_height * img_ratio)
                bg = bg.resize((new_width, new_height), Image.LANCZOS)
                left = int((new_width - size[0]) / 2)
                bg = bg.crop((left, 0, left + size[0], size[1]))
            else:
                new_width = size[0]
                new_height = int(new_width / max(0.0001, img_ratio))
                bg = bg.resize((new_width, new_height), Image.LANCZOS)
                top = int((new_height - size[1]) / 2)
                bg = bg.crop((0, top, size[0], top + size[1]))
            try:
                bg = ImageEnhance.Brightness(bg).enhance(0.8)
            except Exception:
                pass

        overlay = self.create_text_overlay(text, size=size, text_color=text_color, footer_text=footer_text)
        base = bg.convert("RGBA")
        try:
            base.alpha_composite(Image.fromarray(overlay, mode="RGBA"))
        except Exception:
            base = base.convert("RGB")
            return np.array(base)
        return np.array(base.convert("RGB"))

    def _hex_to_rgb(self, value: Any, default=(255, 255, 255)):
        raw = str(value or "").strip().lstrip("#")
        if len(raw) == 3:
            raw = "".join(ch * 2 for ch in raw)
        if len(raw) != 6:
            return default
        try:
            return tuple(int(raw[idx:idx + 2], 16) for idx in (0, 2, 4))
        except Exception:
            return default

    def _fit_image_within(self, image, max_width: int, max_height: int):
        from PIL import Image

        width = max(1, int(getattr(image, "width", max_width) or max_width))
        height = max(1, int(getattr(image, "height", max_height) or max_height))
        scale = min(float(max_width) / float(width), float(max_height) / float(height), 1.0)
        resized = image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS)
        return resized

    def _build_logo_overlay(self, logo_path: str, size, *, duration: float, position: str = "top_center", opacity: float = 0.92, width_ratio: float = 0.18):
        if not logo_path or not os.path.exists(logo_path):
            return None
        try:
            from PIL import Image
            import numpy as np
        except Exception:
            return None

        try:
            base = Image.new("RGBA", size, (0, 0, 0, 0))
            logo = Image.open(logo_path).convert("RGBA")
            max_width = max(60, int(size[0] * float(width_ratio or 0.18)))
            max_height = max(60, int(size[1] * 0.12))
            logo = self._fit_image_within(logo, max_width, max_height)

            alpha = logo.getchannel("A")
            alpha = alpha.point(lambda px: int(max(0, min(255, px * float(opacity or 1.0)))))
            logo.putalpha(alpha)

            x = int((size[0] - logo.width) / 2)
            y = int(size[1] * 0.06)
            position_norm = str(position or "").strip().lower()
            if position_norm == "top_right":
                x = int(size[0] - logo.width - (size[0] * 0.06))
            elif position_norm == "top_left":
                x = int(size[0] * 0.06)
            elif position_norm == "center":
                y = int((size[1] - logo.height) / 2)

            base.alpha_composite(logo, (max(0, x), max(0, y)))
            overlay_clip = self._clip_from_rgba(np.array(base, dtype=np.uint8), duration)
            return overlay_clip
        except Exception:
            return None

    def _resolve_closing_background_image(
        self,
        branding: Dict[str, Any],
        *,
        opening_visual: Optional[Dict[str, Any]] = None,
        last_scene_image_path: Optional[str] = None,
        cover_image_path: Optional[str] = None,
        selected_primary_path: Optional[str] = None,
        video_bg_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        candidates = [
            ("branding_closing_image", branding.get("closing_image_path")),
            ("branding_opening_image", branding.get("opening_image_path")),
            ("opening_visual", (opening_visual or {}).get("path") if isinstance(opening_visual, dict) else None),
            ("last_scene_image", last_scene_image_path),
            ("cover_image", cover_image_path),
            ("selected_primary", selected_primary_path),
            ("video_background", video_bg_path),
        ]
        for source, value in candidates:
            path = self._resolve_input_image_path(str(value or "").strip())
            if path and os.path.exists(path):
                return {"path": path, "source": source}
        return {"path": None, "source": "fallback_background"}

    def _build_cinematic_endcard_frame(
        self,
        branding: Dict[str, Any],
        *,
        background_path: Optional[str],
        size,
        layout_report: Optional[Dict[str, Any]] = None,
    ):
        from PIL import Image, ImageDraw
        import numpy as np

        primary_color = self._hex_to_rgb(branding.get("primary_color"), default=(246, 231, 176))
        secondary_color = self._hex_to_rgb(branding.get("secondary_color"), default=(255, 255, 255))
        base_rgb = self.create_text_image("", size=size, bg_color=(16, 16, 16), bg_image_path=background_path, footer_text=None)
        base = Image.fromarray(base_rgb).convert("RGBA")
        w, h = size

        # Darken background for better CTA readability without using plain color.
        scrim = Image.new("RGBA", size, (6, 8, 12, 120))
        base.alpha_composite(scrim)

        gradient = Image.new("RGBA", size, (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(gradient)
        for idx in range(h):
            alpha = int(145 * (idx / max(1, h)))
            gdraw.line([(0, idx), (w, idx)], fill=(5, 8, 12, alpha))
        base.alpha_composite(gradient)

        draw = ImageDraw.Draw(base)
        safe_margin_x = int(w * 0.08)
        safe_margin_y = int(h * 0.08)
        layout_engine = self._build_safe_text_layout(
            size=size,
            safe_area={"top": 0.08, "bottom": 0.08, "left": 0.08, "right": 0.08},
        )

        def _area_from_pixels(left_px: int, top_px: int, right_px: int, bottom_px: int) -> Dict[str, float]:
            return {
                "left": max(0.0, min(1.0, float(left_px) / max(1.0, float(w)))),
                "top": max(0.0, min(1.0, float(top_px) / max(1.0, float(h)))),
                "right": max(0.0, min(1.0, float(w - right_px) / max(1.0, float(w)))),
                "bottom": max(0.0, min(1.0, float(h - bottom_px) / max(1.0, float(h)))),
            }

        logo_path = str(branding.get("logo_path") or "").strip()
        logo_bottom = int(h * 0.12)
        if logo_path and os.path.exists(logo_path):
            try:
                logo = Image.open(logo_path).convert("RGBA")
                logo = self._fit_image_within(logo, max(90, int(w * 0.24)), max(90, int(h * 0.14)))
                base.alpha_composite(logo, (int((w - logo.width) / 2), int(h * 0.10)))
                logo_bottom = int(h * 0.10) + logo.height
            except Exception:
                logo_bottom = int(h * 0.12)

        title_lines = [
            str(line or "").strip()
            for line in list(branding.get("channel_title_lines") or [])
            if str(line or "").strip()
        ][:2]
        if not title_lines:
            fallback_name = str(branding.get("channel_name") or "").strip()
            if fallback_name:
                title_lines = [fallback_name]

        report_payload: Dict[str, Any] = {
            "resolution": f"{w}x{h}",
            "safe_area": {"top": 0.08, "bottom": 0.08, "left": 0.08, "right": 0.08},
            "channel_title_lines": title_lines,
            "sections": {},
        }

        title_block_bottom = max(safe_margin_y, logo_bottom + int(h * 0.04))
        if title_lines:
            title_gap = max(8, int(h * 0.014))
            title_primary_area = _area_from_pixels(
                safe_margin_x,
                title_block_bottom,
                w - safe_margin_x,
                title_block_bottom + int(h * 0.12),
            )
            title_primary_layout = layout_engine.fit_text_block(
                fixed_lines=[title_lines[0]],
                area=title_primary_area,
                preferred_font_size=max(38, min(74, int(w * 0.052))),
                min_font_size=max(24, min(34, int(w * 0.026))),
                max_lines=1,
                line_spacing_ratio=1.10,
            )
            if not title_primary_layout.get("fits"):
                raise ValueError("Endcard layout failure: channel name does not fit safe area.")
            title_primary_layout = layout_engine.render_text_block(
                draw=draw,
                layout=title_primary_layout,
                fill=(primary_color[0], primary_color[1], primary_color[2], 255),
                shadow=(0, 0, 0, 180),
            )
            report_payload["sections"]["channel_name"] = {
                "text_fits": bool(title_primary_layout.get("fits")),
                "overflow_detected": bool(title_primary_layout.get("overflow_detected")),
                "font_size_used": int(title_primary_layout.get("font_size_used") or 0),
                "line_count": int(title_primary_layout.get("line_count") or 0),
                "lines": list(title_primary_layout.get("lines") or []),
            }
            title_block_bottom = max(
                title_block_bottom,
                max((box.get("y", 0) + box.get("height", 0)) for box in title_primary_layout.get("rendered_boxes") or [{"y": title_block_bottom, "height": int(h * 0.08)}]),
            )

            if len(title_lines) > 1:
                slogan_top = title_block_bottom + title_gap
                slogan_area = _area_from_pixels(
                    safe_margin_x,
                    slogan_top,
                    w - safe_margin_x,
                    slogan_top + int(h * 0.10),
                )
                slogan_layout = layout_engine.fit_text_block(
                    fixed_lines=[title_lines[1]],
                    area=slogan_area,
                    preferred_font_size=max(28, min(58, int(w * 0.040))),
                    min_font_size=max(20, min(30, int(w * 0.022))),
                    max_lines=1,
                    line_spacing_ratio=1.10,
                )
                if not slogan_layout.get("fits"):
                    raise ValueError("Endcard layout failure: channel slogan does not fit safe area.")
                slogan_layout = layout_engine.render_text_block(
                    draw=draw,
                    layout=slogan_layout,
                    fill=(secondary_color[0], secondary_color[1], secondary_color[2], 255),
                    shadow=(0, 0, 0, 180),
                )
                report_payload["sections"]["channel_slogan"] = {
                    "text_fits": bool(slogan_layout.get("fits")),
                    "overflow_detected": bool(slogan_layout.get("overflow_detected")),
                    "font_size_used": int(slogan_layout.get("font_size_used") or 0),
                    "line_count": int(slogan_layout.get("line_count") or 0),
                    "lines": list(slogan_layout.get("lines") or []),
                }
                title_block_bottom = max(
                    title_block_bottom,
                    max((box.get("y", 0) + box.get("height", 0)) for box in slogan_layout.get("rendered_boxes") or [{"y": slogan_top, "height": int(h * 0.06)}]),
                )

        lines = list(branding.get("final_message_lines") or [])[:3]
        cta_top = max(int(h * 0.44), title_block_bottom + int(h * 0.07))
        cta_area = _area_from_pixels(
            safe_margin_x,
            cta_top,
            w - safe_margin_x,
            min(h - safe_margin_y - int(h * 0.12), cta_top + int(h * 0.26)),
        )
        cta_layout = layout_engine.fit_text_block(
            fixed_lines=lines,
            area=cta_area,
            preferred_font_size=max(28, min(54, int(w * 0.041))),
            min_font_size=max(20, min(32, int(w * 0.025))),
            max_lines=max(1, len(lines)),
            line_spacing_ratio=1.22,
        )
        if not cta_layout.get("fits"):
            raise ValueError("Endcard layout failure: CTA block does not fit safe area.")
        cta_layout = layout_engine.render_text_block(
            draw=draw,
            layout=cta_layout,
            fill=(secondary_color[0], secondary_color[1], secondary_color[2], 255),
            shadow=(0, 0, 0, 180),
        )
        report_payload["sections"]["cta"] = {
            "text_fits": bool(cta_layout.get("fits")),
            "overflow_detected": bool(cta_layout.get("overflow_detected")),
            "font_size_used": int(cta_layout.get("font_size_used") or 0),
            "line_count": int(cta_layout.get("line_count") or 0),
            "lines": list(cta_layout.get("lines") or []),
        }

        subtitle = str(branding.get("endcard_cta_text") or "INSCREVA-SE E CONTINUE CONOSCO").strip()
        subtitle_area = _area_from_pixels(
            safe_margin_x,
            h - safe_margin_y - int(h * 0.10),
            w - safe_margin_x,
            h - safe_margin_y,
        )
        subtitle_layout = layout_engine.fit_text_block(
            fixed_lines=[subtitle],
            area=subtitle_area,
            preferred_font_size=max(18, min(28, int(w * 0.022))),
            min_font_size=max(16, min(24, int(w * 0.018))),
            max_lines=1,
            line_spacing_ratio=1.10,
        )
        if not subtitle_layout.get("fits"):
            raise ValueError("Endcard layout failure: closing subtitle does not fit safe area.")
        subtitle_layout = layout_engine.render_text_block(
            draw=draw,
            layout=subtitle_layout,
            fill=(235, 235, 235, 255),
            shadow=(0, 0, 0, 180),
        )
        report_payload["sections"]["closing_phrase"] = {
            "text_fits": bool(subtitle_layout.get("fits")),
            "overflow_detected": bool(subtitle_layout.get("overflow_detected")),
            "font_size_used": int(subtitle_layout.get("font_size_used") or 0),
            "line_count": int(subtitle_layout.get("line_count") or 0),
            "lines": list(subtitle_layout.get("lines") or []),
        }
        report_payload["contextual_closing"] = dict(branding.get("contextual_closing") or {})

        if isinstance(layout_report, dict):
            layout_report.clear()
            overall_overflow = any(
                bool((section or {}).get("overflow_detected"))
                for section in (report_payload.get("sections") or {}).values()
            )
            overall_fits = all(
                bool((section or {}).get("text_fits"))
                for section in (report_payload.get("sections") or {}).values()
            )
            report_payload["text_fits"] = overall_fits
            report_payload["overflow_detected"] = overall_overflow
            report_payload["font_size_used"] = max(
                int((section or {}).get("font_size_used") or 0)
                for section in (report_payload.get("sections") or {}).values()
            )
            report_payload["line_count"] = sum(
                int((section or {}).get("line_count") or 0)
                for section in (report_payload.get("sections") or {}).values()
            )
            layout_report.update(report_payload)

        return np.array(base.convert("RGB"))

    def _apply_audio_fadeout(self, clip, duration: float = 0.8):
        if clip is None:
            return None
        try:
            clip_duration = float(getattr(clip, "duration", 0.0) or 0.0)
        except Exception:
            clip_duration = 0.0
        fade = max(0.0, min(float(duration or 0.0), clip_duration * 0.35))
        if fade <= 0:
            return clip
        try:
            if hasattr(clip, "audio_fadeout"):
                return clip.audio_fadeout(fade)
            try:
                from moviepy.editor import afx
            except ImportError:
                from moviepy import afx
            return clip.fx(afx.audio_fadeout, fade)
        except Exception:
            return clip

    def _apply_scene_transition_style(self, clip, transition_sec: float = DEFAULT_SCENE_TRANSITION_SEC):
        if clip is None:
            return None
        return self._apply_soft_fade(
            clip,
            fade_in_sec=max(0.12, float(transition_sec or DEFAULT_SCENE_TRANSITION_SEC) * 0.70),
            fade_out_sec=max(0.16, float(transition_sec or DEFAULT_SCENE_TRANSITION_SEC)),
        )

    def _caption_font_candidates(self) -> List[str]:
        return [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "DejaVuSans-Bold.ttf",
            "arial.ttf",
        ]

    def _load_caption_font(self, font_size: int):
        from PIL import ImageFont

        for fp in self._caption_font_candidates():
            try:
                return ImageFont.truetype(fp, font_size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _build_safe_text_layout(
        self,
        size=(1080, 1920),
        *,
        safe_area: Optional[Dict[str, float]] = None,
    ) -> SafeTextLayout:
        return SafeTextLayout(
            size=size,
            font_loader=self._load_caption_font,
            safe_area=safe_area,
        )

    def _measure_caption_text_width(self, draw, text: str, font) -> float:
        try:
            return float(draw.textlength(text, font=font))
        except Exception:
            bbox = draw.textbbox((0, 0), text, font=font)
            return float(bbox[2] - bbox[0])

    def _wrap_caption_words(self, text: str, draw, font, max_width: int) -> List[str]:
        words = [w for w in str(text or "").split() if w]
        lines: List[str] = []
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if self._measure_caption_text_width(draw, candidate, font) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = word
                continue
            current = word
        if current:
            lines.append(current)
        return lines

    def _caption_layout_metrics(
        self,
        text: str,
        size=(1080, 1920),
        max_lines: int = 2,
        reserved_bottom_ratio: float = 0.0,
        safe_area_override: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        w, h = size
        margin_x = int(w * CAPTION_SAFE_AREA_X_RATIO)
        base_size = max(24, min(72, int(w * 0.055)))
        min_size = max(14, min(28, int(w * 0.022)))
        safe_area = {
            "top": CAPTION_SAFE_AREA_TOP_RATIO,
            "bottom": max(CAPTION_SAFE_AREA_BOTTOM_RATIO, float(reserved_bottom_ratio or 0.0)),
            "left": CAPTION_SAFE_AREA_X_RATIO,
            "right": CAPTION_SAFE_AREA_X_RATIO,
        }
        if isinstance(safe_area_override, dict):
            safe_area.update({k: float(v) for k, v in safe_area_override.items() if v is not None})
        layout = self._build_safe_text_layout(
            size=size,
            safe_area=safe_area,
        )
        metrics = layout.fit_text_block(
            text=re.sub(r"\s+", " ", str(text or "").strip()),
            area=safe_area,
            preferred_font_size=base_size,
            min_font_size=min_size,
            max_lines=max_lines,
            line_spacing_ratio=1.20,
        )
        return {
            "fits": bool(metrics.get("fits")),
            "font": metrics.get("font"),
            "lines": list(metrics.get("lines") or []),
            "line_h": int(metrics.get("line_height") or 0),
            "font_size_used": int(metrics.get("font_size_used") or 0),
            "overflow_detected": bool(metrics.get("overflow_detected")),
            "layout": metrics,
        }

    def _split_caption_text_for_overlay(
        self,
        text: str,
        size=(1080, 1920),
        max_lines: int = 2,
        reserved_bottom_ratio: float = 0.0,
    ) -> List[str]:
        cleaned = re.sub(r"\s+", " ", str(text or "").strip())
        if not cleaned:
            return []
        if self._caption_layout_metrics(
            cleaned,
            size=size,
            max_lines=max_lines,
            reserved_bottom_ratio=reserved_bottom_ratio,
        ).get("fits"):
            return [cleaned]

        words = [w for w in cleaned.split() if w]
        if len(words) <= 1:
            return [cleaned]

        chunks: List[str] = []
        current_words: List[str] = []
        idx = 0
        while idx < len(words):
            candidate_words = current_words + [words[idx]]
            candidate = " ".join(candidate_words).strip()
            if self._caption_layout_metrics(
                candidate,
                size=size,
                max_lines=max_lines,
                reserved_bottom_ratio=reserved_bottom_ratio,
            ).get("fits"):
                current_words = candidate_words
                idx += 1
                continue
            if current_words:
                chunks.append(" ".join(current_words).strip())
                current_words = []
                continue
            chunks.append(words[idx])
            idx += 1

        if current_words:
            chunks.append(" ".join(current_words).strip())

        return [chunk for chunk in chunks if chunk] or [cleaned]

    def _expand_caption_item_for_overlay(
        self,
        item: Dict[str, Any],
        size=(1080, 1920),
        max_lines: int = 2,
        reserved_bottom_ratio: float = 0.0,
    ) -> List[Dict[str, Any]]:
        caption = re.sub(r"\s+", " ", str((item or {}).get("caption") or "").strip())
        try:
            start = float((item or {}).get("start") or 0.0)
            end = float((item or {}).get("end") or 0.0)
        except Exception:
            return []
        if not caption or end <= start:
            return []

        chunks = self._split_caption_text_for_overlay(
            caption,
            size=size,
            max_lines=max_lines,
            reserved_bottom_ratio=reserved_bottom_ratio,
        )
        if len(chunks) <= 1:
            clone = dict(item or {})
            clone["caption"] = caption
            clone["start"] = round(start, 3)
            clone["end"] = round(end, 3)
            return [clone]

        total_duration = max(0.01, end - start)
        weights = [max(1, len(chunk.split())) for chunk in chunks]
        total_weight = max(1, sum(weights))
        expanded: List[Dict[str, Any]] = []
        cursor = start
        for idx, chunk in enumerate(chunks):
            if idx == len(chunks) - 1:
                chunk_end = end
            else:
                portion = weights[idx] / total_weight
                chunk_duration = max(0.35, total_duration * portion)
                remaining_min = 0.35 * max(0, len(chunks) - idx - 1)
                chunk_end = min(end - remaining_min, cursor + chunk_duration)
            …80454 tokens truncated… self._assert_clip_not_none(c, "clips_list_item", {"clip_index": ci})
                try:
                    d = float(getattr(c, "duration", 0) or 0)
                except Exception:
                    d = 0
                if d <= 0:
                    raise Exception(f"Clip com duração inválida (<=0): index={ci} type={type(c).__name__}")
            preflight_env = (os.getenv("VIDEO_PREFLIGHT_VALIDATE") or "1").strip().lower()
            if preflight_env not in {"0", "false", "no", "off"}:
                try:
                    max_pf = int((os.getenv("VIDEO_PREFLIGHT_MAX_CLIPS") or "120").strip() or "120")
                except Exception:
                    max_pf = 120
                max_pf = max(0, min(max_pf, 220))
                if max_pf and len(clips) <= max_pf:
                    for ci, c in enumerate(list(clips)):
                        try:
                            dur = float(getattr(c, "duration", 0) or 0)
                        except Exception:
                            dur = 0
                        ts = [0.0]
                        if dur > 0.25:
                            ts.append(max(0.0, dur - 0.05))
                        for tt in ts:
                            try:
                                c.get_frame(tt)
                            except Exception as ex:
                                raise Exception(f"Preflight falhou: clip_index={ci} t={tt} type={type(c).__name__} err={ex}")
            if len(clips) > 1:
                try:
                    method = "compose" if len(clips) < 15 else "chain"
                    final_clip = concatenate_videoclips(clips, method=method)
                except Exception:
                    final_clip = concatenate_videoclips(clips, method="compose")
            else:
                final_clip = concatenate_videoclips(clips, method="compose")
            self._assert_clip_not_none(final_clip, "final_clip_after_concat")

            # CODEXIA_AUDIO_TIMED_GLOBAL_CAPTIONS_V1
            # Render one subtitle layer in final-video coordinates. This keeps
            # every caption tied to the official audio timestamps regardless of
            # scene reuse, visual beats, fades, or CTA placement. The window is
            # bounded by both the audio and the already-concatenated video, so a
            # caption can never extend the video or leak into the silent endcard.
            try:
                concatenated_duration = float(getattr(final_clip, "duration", 0) or 0.0)
            except Exception:
                concatenated_duration = 0.0
            global_caption_overlays = []
            caption_overlay_window_end = min(
                max(0.0, float(actual_total_audio_dur or 0.0)),
                max(0.0, concatenated_duration),
            )
            if caption_overlay_window_end > 0.0:
                global_caption_overlays = self._caption_overlay_clips_for_window(
                    full_caption_timeline,
                    0.0,
                    caption_overlay_window_end,
                    video_size,
                )
            if global_caption_overlays:
                final_clip = CompositeVideoClip(
                    [final_clip] + global_caption_overlays,
                    size=video_size,
                )
                if concatenated_duration > 0.0:
                    final_clip = self._set_clip_duration(final_clip, concatenated_duration)
            render_report["visual_plan"]["caption_render_mode"] = "global_audio_timeline"
            render_report["visual_plan"]["global_caption_overlay_count"] = len(global_caption_overlays)
            render_report["visual_plan"]["global_caption_window_end_sec"] = round(
                caption_overlay_window_end,
                3,
            )

            try:
                final_dur = float(getattr(final_clip, "duration", 0) or 0)
            except Exception:
                final_dur = 0
            if final_dur <= 0:
                raise Exception("final_clip com duração inválida (<=0) após concatenação.")

            if not music_file_path:
                narration_audio_track = main_audio_clip
                if silent_cinematic_tail_sec > 0:
                    try:
                        silence_tail = AudioClip(
                            lambda t: 0,
                            duration=float(silent_cinematic_tail_sec),
                            fps=int(getattr(main_audio_clip, "fps", 44100) or 44100),
                        )
                        narration_audio_track = concatenate_audioclips([main_audio_clip, silence_tail])
                    except Exception:
                        narration_audio_track = main_audio_clip
                final_clip = self._set_clip_audio(final_clip, narration_audio_track)
                try:
                    final_dur = float(getattr(final_clip, "duration", 0) or 0)
                except Exception:
                    final_dur = final_dur

            if getattr(final_clip, "audio", None) is not None:
                ad = float(getattr(final_clip.audio, "duration", 0) or 0)
                if ad > 0:
                    expected_video_duration = float(target_video_duration or ad)
                    final_clip, duration_sync_repair = self._synchronize_video_clip_duration(
                        final_clip,
                        expected_video_duration,
                    )
                    render_report["duration_sync_repair"] = duration_sync_repair
                    final_dur = float(getattr(final_clip, "duration", 0) or 0)

            render_report["final_video_duration_sec"] = round(float(final_dur or 0.0), 2)

            if not music_file_path:
                try:
                    final_dur = float(getattr(final_clip, "duration", 0) or 0.0)
                except Exception:
                    final_dur = 0.0
                caption_duration = 0.0
                if full_caption_timeline:
                    try:
                        caption_duration = float(full_caption_timeline[-1].get("end") or 0.0)
                    except Exception:
                        caption_duration = 0.0
                video_sync_target = float(target_video_duration or actual_total_audio_dur)
                audio_video_diff = abs(final_dur - video_sync_target)
                # CODEXIA_AUDIO_TIMED_GLOBAL_CAPTIONS_V1
                # A transcrição termina na última palavra falada e o arquivo
                # pode ter silêncio de padding. Só um excesso de legenda é
                # erro; silêncio depois da última palavra é permitido.
                caption_overflow_sec = max(
                    0.0,
                    float(caption_duration or 0.0) - float(actual_total_audio_dur or 0.0),
                )
                trailing_audio_silence_sec = max(
                    0.0,
                    float(actual_total_audio_dur or 0.0) - float(caption_duration or 0.0),
                )
                audio_caption_diff = caption_overflow_sec
                video_sync_tolerance = duration_sync_tolerance_seconds(video_sync_target)
                sync_validation = {
                    "planned_text_duration_sec": round(float(estimated_total_duration or 0.0), 2),
                    "audio_duration_sec": round(float(actual_total_audio_dur or 0.0), 2),
                    "captions_duration_sec": round(float(caption_duration or 0.0), 2),
                    "spoken_audio_end_sec": round(float(caption_duration or 0.0), 2),
                    "audio_trailing_silence_sec": round(float(trailing_audio_silence_sec or 0.0), 2),
                    "video_duration_sec": round(float(final_dur or 0.0), 2),
                    "video_sync_target_sec": round(float(video_sync_target or 0.0), 2),
                    "audio_caption_diff_sec": round(audio_caption_diff, 2),
                    "audio_video_diff_sec": round(audio_video_diff, 2),
                    "captions_synced_with_audio": bool(audio_caption_diff <= 0.25),
                    "video_sync_tolerance_sec": round(video_sync_tolerance, 3),
                    "video_synced_with_audio": bool(audio_video_diff <= video_sync_tolerance),
                    "video_extends_past_narration_for_cinematic_closing": bool(silent_cinematic_tail_sec > 0),
                    "cinematic_closing_tail_sec": round(float(silent_cinematic_tail_sec or 0.0), 2),
                    "has_automatic_opening": bool((planning_meta.get("opening_text") or "").strip()),
                    "has_automatic_closing": bool((planning_meta.get("closing_text") or "").strip()) or bool(silent_cinematic_tail_sec > 0),
                    "opening_hook_starts_sec": round(float(initial_opening_silence_sec or 0.0), 2),
                    "opening_visual_only_duration_sec": round(float(initial_opening_silence_sec or 0.0), 2),
                    "opening_visual_only_is_4s": bool(abs(float(initial_opening_silence_sec or 0.0) - 4.0) <= 0.05),
                    "endcard_duration_sec": round(float(end_clip_duration or 0.0), 2),
                    "timeline_source": caption_timeline_source,
                    "uses_official_scene_timeline": True,
                    "official_scene_timeline_count": len(official_scene_timeline),
                    "scene_image_lead_sec": round(DEFAULT_SCENE_IMAGE_LEAD_SEC, 2),
                    "scene_caption_lead_sec": round(DEFAULT_SCENE_CAPTION_LEAD_SEC, 2),
                    "scene_post_audio_margin_sec": round(DEFAULT_SCENE_AUDIO_MARGIN_SEC, 2),
                }
                sync_validation["caption_block_sync"] = scene_caption_sync.get("block_sync_report") or {}
                sync_validation["timeline_report"] = render_report.get("timeline_report") or {}
                render_report["sync_validation"] = sync_validation
                render_report["narration_completed"] = bool(sync_validation["captions_synced_with_audio"] and sync_validation["video_synced_with_audio"])
                render_report["story_completed"] = True
                if not sync_validation["captions_synced_with_audio"]:
                    raise Exception("Falha de validacao: legenda nao terminou junto com o audio final.")
                if not sync_validation["video_synced_with_audio"]:
                    raise Exception("Falha de validacao: video nao terminou junto com o audio final.")
                if not sync_validation["has_automatic_opening"]:
                    raise Exception("Falha de validacao: abertura automatica ausente.")
                if not sync_validation["has_automatic_closing"]:
                    raise Exception("Falha de validacao: encerramento automatico ausente.")
                if not sync_validation["opening_visual_only_is_4s"]:
                    raise Exception("Falha de validacao: a abertura visual sem fala/legenda deve durar 4 segundos.")

            try:
                final_clip.get_frame(0.0)
                if final_dur > 0.25:
                    final_clip.get_frame(max(0.0, final_dur - 0.05))
            except Exception as e:
                raise Exception(f"Preflight falhou no final_clip (get_frame): {e}")
            
            # 4. Adicionar Música de Fundo
            if progress_callback:
                progress_callback(90, "Adicionando trilha sonora...")
            
            # Limpeza agressiva de memória antes da renderização final
            gc.collect()
                
            music_mood = plan.get('music_mood', 'drama')
            music_prompt = (plan.get("music_prompt") or "").strip() if isinstance(plan, dict) else ""
            fallback_music_mood = (plan.get("music_mood_fallback") or music_mood) if isinstance(plan, dict) else music_mood
            music_path = None
            used_music_credit = None
            music_provider_used = "none"
            music_generation_error = ""

            # Preferência portável da Fábrica: quando Eleven Music está
            # configurado, gera uma trilha instrumental própria e adequada ao
            # episódio. Se o provedor premium não estiver disponível, o fluxo
            # continua para o gerador já existente e, por fim, para a biblioteca
            # local — a trilha nunca deve derrubar uma produção de vídeo.
            try:
                from app.services.fabrica_pipeline import generate_eleven_music_track

                music_plan = dict(plan) if isinstance(plan, dict) else {}
                configured_elevenlabs_key = str(
                    getattr(self.ai_service, "elevenlabs_key", "")
                    or getattr(self.ai_service, "elevenlabs_api_key", "")
                    or ""
                ).strip() if self.ai_service else ""
                if configured_elevenlabs_key:
                    # The settings-backed AI service is the source of truth in
                    # Codexia; do not expose the key in the render report.
                    music_plan["elevenlabs_api_key"] = configured_elevenlabs_key
                generated_music = generate_eleven_music_track(
                    music_plan,
                    self.output_dir,
                    target_video_duration,
                    progress_callback=progress_callback,
                )
                if isinstance(generated_music, dict) and os.path.exists(str(generated_music.get("path") or "")):
                    music_path = str(generated_music.get("path"))
                    music_provider_used = str(generated_music.get("provider") or "Eleven Music")
                    render_report["visual_plan"]["background_music_cached"] = bool(generated_music.get("cached"))
                    render_report["visual_plan"]["background_music_prompt"] = str(generated_music.get("prompt") or "")[:1000]
            except Exception as exc:
                music_generation_error = f"{type(exc).__name__}: {str(exc)[:240]}"

            # Tenta gerar música exclusiva com IA
            if not music_path and self.ai_service:
                print(f"Gerando música exclusiva para mood: {music_mood}...")
                music_brief = music_prompt or f"{music_mood} style, inspired by {title}"
                try:
                    music_content = self.ai_service.generate_music(music_brief)
                    if music_content:
                        filename = f"music_{uuid.uuid4()}.wav"
                        generated_music_path = os.path.join(self.output_dir, filename)
                        with open(generated_music_path, "wb") as f:
                            f.write(music_content)
                        music_path = generated_music_path
                        music_provider_used = "AIContentGenerator MusicGen"
                except Exception as exc:
                    music_generation_error = f"{type(exc).__name__}: {str(exc)[:240]}"
            
            # Se falhou ou não tem IA, usa biblioteca local
            if not music_path or not os.path.exists(music_path):
                 self._ensure_fallback_music()
                 local_path = os.path.join("app/static/music", f"{fallback_music_mood}.mp3")
                 if os.path.exists(local_path):
                     music_path = local_path
                     music_provider_used = "biblioteca_local"
                 else:
                     try:
                         import glob
                         mp3_files = glob.glob("app/static/music/*.mp3")
                         if mp3_files:
                             music_path = mp3_files[0]
                             music_provider_used = "biblioteca_local_generica"
                             print(f"Usando música fallback genérica: {music_path}")
                     except Exception as e:
                         print(f"Erro ao procurar fallback de música: {e}")
            
            if music_path and os.path.exists(music_path):
                if not used_music_credit:
                    filename = os.path.basename(music_path).lower()
                    for key, credit in self.MUSIC_CREDITS.items():
                        if key in filename:
                            used_music_credit = credit
                            break

                try:
                    bg_music = AudioFileClip(music_path)
                    self._assert_clip_not_none(bg_music, "bg_music_clip", {"path": music_path})
                    has_voice_audio = bool(final_clip and getattr(final_clip, "audio", None))
                    try:
                        bg_volume_raw = ""
                        if isinstance(plan, dict) and plan.get("bg_music_volume") is not None:
                            bg_volume_raw = str(plan.get("bg_music_volume")).strip()
                        if not bg_volume_raw:
                            bg_volume_raw = (os.getenv("VIDEO_BG_MUSIC_VOLUME") or "").strip()
                        default_bg_volume = 0.025 if (has_voice_audio and prefer_peaceful_music) else (0.035 if has_voice_audio else 0.08)
                        bg_volume = float(bg_volume_raw) if bg_volume_raw else default_bg_volume
                    except Exception:
                        bg_volume = 0.025 if (has_voice_audio and prefer_peaceful_music) else (0.035 if has_voice_audio else 0.08)
                    bg_volume = max(0.0, min(0.2, bg_volume))
                    
                    if bg_music.duration < final_clip.duration:
                        num_loops = int(final_clip.duration / bg_music.duration) + 1
                        bg_music = concatenate_audioclips([bg_music] * num_loops)
                    
                    bg_music = bg_music.with_duration(final_clip.duration)
                    bg_music = bg_music.with_volume_scaled(bg_volume)
                    bg_music = self._apply_audio_fadeout(
                        bg_music,
                        duration=min(1.4, max(0.8, float(end_clip_duration or 0.0) * 0.40)),
                    )
                    
                    if has_voice_audio:
                        final_audio = CompositeAudioClip([bg_music, final_clip.audio])
                    else:
                        final_audio = bg_music
                        
                    final_clip = final_clip.with_audio(final_audio)
                    render_report["visual_plan"]["background_music_fade_out"] = True
                    render_report["visual_plan"]["background_music_provider"] = music_provider_used or "unknown"
                    render_report["visual_plan"]["background_music_generated"] = bool(
                        music_provider_used and music_provider_used not in {"biblioteca_local", "biblioteca_local_generica"}
                    )
                    if music_generation_error:
                        render_report["visual_plan"]["background_music_generation_error"] = music_generation_error
                except Exception as e:
                    print(f"Erro ao adicionar música de fundo: {e}")

            # Output
            filename = f"{uuid.uuid4()}.mp4"
            output_path = os.path.join(self.output_dir, filename)
            try:
                self._dbg_event("H1", "write_videofile start (narrated)", {"output_path": output_path})
            except Exception:
                pass
            output_msg = f"Renderizando arquivo final... output={filename}"
            if progress_callback:
                progress_callback(95, output_msg)
            
            # Logger customizado: durante write_videofile (etapa mais longa) pinga 95→99
            # para o progress_callback atualizar o DB e evitar timeout do monitor
            write_logger = None
            if progress_callback:
                try:
                    import proglog
                    class RenderProgressLogger(proglog.ProgressBarLogger):
                        def __init__(self, callback, message):
                            super().__init__()
                            self._cb = callback
                            self._msg = str(message or "Renderizando arquivo final...")
                        def bars_callback(self, bar, attr, value, old_value=None):
                            super().bars_callback(bar, attr, value, old_value)
                            if not self._cb or bar not in self.bars:
                                return
                            total = self.bars[bar].get("total")
                            if total and value is not None:
                                pct = 95 + int(4 * (value / total))
                                try:
                                    self._cb(min(99, pct), self._msg)
                                except Exception:
                                    pass
                                try:
                                    if value == 1 or value == total or (old_value is not None and int(value) != int(old_value) and int(value) % 25 == 0):
                                        pass
                                except Exception:
                                    pass
                    write_logger = RenderProgressLogger(progress_callback, output_msg)
                except Exception:
                    pass
            logger_kw = {"logger": write_logger} if write_logger else {}
            
            # Escreve o arquivo
            # threads=1 + preset ultrafast para reduzir memória e tempo (evita OOM no Render)
            print(f"Renderizando vídeo para: {output_path}")
            
            # Otimização adicional para vídeos longos: bitrates controlados para evitar arquivos gigantes
            # Para vídeos > 10 min, usamos bitrate menor para economizar RAM e disco
            is_long_video = len(clips) > 25
            bitrate = "1800k" if is_long_video else "3500k"
            
            debug_ctx["stage"] = "write_videofile"
            _render_hb_stop = None
            _render_hb_thread = None
            try:
                import threading as _threading
                _render_hb_stop = _threading.Event()

                def _render_heartbeat():
                    _last_size = -1
                    _start_ts = None
                    _tick = 0
                    while not _render_hb_stop.wait(15):
                        try:
                            _exists = os.path.exists(output_path)
                            _size = os.path.getsize(output_path) if _exists else 0
                            if _start_ts is None:
                                try: import time as _time; _start_ts = _time.time()
                                except Exception: _start_ts = 0
                            try:
                                import time as _time2
                                _elapsed_sec = int(max(0, (_time2.time() - (_start_ts or _time2.time()))))
                            except Exception:
                                _elapsed_sec = 0
                            _h = _elapsed_sec // 3600
                            _m = (_elapsed_sec % 3600) // 60
                            _s = _elapsed_sec % 60
                            if _h > 0:
                                _elapsed_str = f"{_h:d}h{_m:02d}m{_s:02d}s"
                            elif _m > 0:
                                _elapsed_str = f"{_m:d}m{_s:02d}s"
                            else:
                                _elapsed_str = f"{_s:d}s"
                            _mb = round(_size / (1024 * 1024), 1) if _size else 0
                            if progress_callback:
                                _base_pct = 95 if _size <= 0 else 96
                                _tick += 1
                                _swing = (_tick % 30)
                                if _size > 0 and _elapsed_sec > 60:
                                    _base_pct = 97 if (_swing < 15) else 98
                                elif _size > 0 and _elapsed_sec > 20:
                                    _base_pct = 96 if (_swing < 15) else 97
                                _msg = (f"6/8 Renderizando arquivo final... "
                                        f"({_elapsed_str} decorridos; arquivo: ~{_mb} MB)")
                                try:
                                    progress_callback(_base_pct, _msg)
                                except Exception:
                                    pass
                            _last_size = _size
                        except Exception as _hb_err:
                            pass

                _render_hb_thread = _threading.Thread(target=_render_heartbeat, daemon=True)
                _render_hb_thread.start()
            except Exception as _hb_start_err:
                pass

            try:
                final_clip.write_videofile(
                    output_path, 
                    fps=24, 
                    codec="libx264", 
                    audio_codec="aac", 
                    threads=1, # IMPORTANTE: 1 thread usa MUITO menos RAM que múltiplas
                    bitrate=bitrate,
                    ffmpeg_params=[
                        "-preset", "ultrafast", 
                        "-movflags", "+faststart", 
                        "-pix_fmt", "yuv420p",
                        "-tune", "stillimage" if is_long_video else "film"
                    ],
                    **logger_kw
                )
                try:
                    if _render_hb_stop:
                        _render_hb_stop.set()
                except Exception:
                    pass
            except Exception as e:
                try:
                    if _render_hb_stop:
                        _render_hb_stop.set()
                except Exception:
                    pass
                try:
                    import traceback as _tb
                    self._dbg_event("H1", "write_videofile exception (narrated)", {
                        "output_path": output_path,
                        "error": str(e),
                        "traceback": _tb.format_exc()[-4000:],
                        "exists": bool(os.path.exists(output_path)),
                        "size": int(os.path.getsize(output_path)) if os.path.exists(output_path) else 0,
                    })
                except Exception:
                    pass
                raise
            try:
                self._dbg_event("H1", "write_videofile done (narrated)", {
                    "output_path": output_path,
                    "exists": bool(os.path.exists(output_path)),
                    "size": int(os.path.getsize(output_path)) if os.path.exists(output_path) else 0,
                })
            except Exception:
                pass
            self._dbg_event("H1", "_ensure_playable_mp4 start (narrated)", {"output_path": output_path})
            output_path = self._ensure_playable_mp4(output_path)
            try:
                self._dbg_event("H1", "_ensure_playable_mp4 done (narrated)", {
                    "output_path": output_path,
                    "exists": bool(os.path.exists(output_path)),
                    "size": int(os.path.getsize(output_path)) if os.path.exists(output_path) else 0,
                })
            except Exception:
                pass
            
            
            abs_path = os.path.abspath(output_path)
            print(f"Vídeo salvo com sucesso em: {abs_path} (Size: {os.path.getsize(output_path)} bytes)")
            
            if progress_callback:
                progress_callback(100, "Vídeo renderizado com sucesso!")

            render_report["visual_plan"]["recovery_image_budget"] = recovery_image_budget.snapshot()
            render_report["visual_plan"]["generated_image_count"] = len({
                item.get("image_path") for item in render_report["scene_visuals"] if item.get("image_path")
            })
            render_report["visual_plan"]["generated_new_images"] = len({
                item.get("image_path")
                for item in render_report["scene_visuals"]
                if str(item.get("source") or "").startswith(("generated", "cached"))
            })
            render_report["visual_plan"]["reused_scene_numbers"] = [
                int(item.get("scene_number") or 0)
                for item in render_report["scene_visuals"]
                if bool(item.get("reused"))
            ]
            image_duration_map: Dict[str, float] = {}
            image_usage_counts: Dict[str, int] = {}
            for item in render_report["scene_visuals"]:
                path = str(item.get("image_path") or "").strip()
                if not path:
                    continue
                image_duration_map[path] = image_duration_map.get(path, 0.0) + float(item.get("final_visual_duration_sec") or 0.0)
                image_usage_counts[path] = image_usage_counts.get(path, 0) + 1
            render_report["visual_plan"]["average_image_duration_sec"] = round(
                (sum(image_duration_map.values()) / max(1, len(image_duration_map))),
                2,
            ) if image_duration_map else 0.0
            render_report["visual_plan"]["reused_image_count"] = sum(
                1 for count in image_usage_counts.values() if count > 1
            )
            render_report["duration_plan"]["obtained_duration_sec"] = round(
                float(self._measure_rendered_video_duration_seconds(output_path) or 0.0),
                2,
            )
            render_report["duration_plan"]["title_duration_sec"] = round(float(title_clip_duration or 0.0), 2)
            render_report["duration_plan"]["end_duration_sec"] = round(float(end_clip_duration or 0.0), 2)
            requested_duration_final = float(render_report["duration_plan"].get("requested_duration_target_sec") or 0.0)
            obtained_duration_final = float(render_report["duration_plan"].get("obtained_duration_sec") or 0.0)
            if requested_duration_final > 0:
                diff_pct = abs(obtained_duration_final - requested_duration_final) / requested_duration_final
            else:
                diff_pct = 0.0
            render_report["duration_plan"]["tolerance_pct"] = 5.0
            render_report["duration_plan"]["within_tolerance"] = bool(requested_duration_final <= 0 or diff_pct <= 0.05)
            render_report["duration_plan"]["difference_sec"] = round(obtained_duration_final - requested_duration_final, 2)
            render_report["duration_plan"]["estimated_full_narration_duration_sec"] = round(float(planning_meta.get("estimated_total_duration_sec") or 0.0), 2)
            render_report["duration_plan"]["actual_audio_duration_sec"] = round(float(actual_total_audio_dur or 0.0), 2)
            render_report["duration_plan"]["above_requested_range_sec"] = round(max(0.0, obtained_duration_final - max_requested_duration), 2) if max_requested_duration > 0 else 0.0
            render_report["duration_plan"]["below_requested_range_sec"] = round(max(0.0, min_requested_duration - obtained_duration_final), 2) if min_requested_duration > 0 else 0.0
            render_report["duration_plan"]["within_requested_range"] = bool(
                (min_requested_duration <= 0 or obtained_duration_final >= min_requested_duration)
                and (max_requested_duration <= 0 or obtained_duration_final <= max_requested_duration)
            )
            if not render_report["duration_plan"].get("range_decision"):
                render_report["duration_plan"]["range_decision"] = (
                    "within_requested_range"
                    if render_report["duration_plan"]["within_requested_range"]
                    else "keep_complete_narration_outside_range"
                )
            if not render_report["duration_plan"].get("range_decision_reason"):
                render_report["duration_plan"]["range_decision_reason"] = (
                    "Narracao completa ficou dentro da faixa solicitada."
                    if render_report["duration_plan"]["within_requested_range"]
                    else "Duracao final ficou fora da faixa de referencia, mas o sistema manteve a narracao completa para nao cortar o audio."
                )
            render_report["video_url"] = f"{VIDEO_URL_PREFIX}/{filename}"
            render_report["file_path"] = output_path
            # ====== sync_validation: áudio ↔ vídeo (item 2) ======
            obtained_duration_final_sec = float(render_report["duration_plan"].get("obtained_duration_sec") or 0.0)
            final_audio_track_duration_sec = float(target_video_duration or actual_total_audio_dur)
            delta_av = abs(obtained_duration_final_sec - final_audio_track_duration_sec)
            tolerance_target = duration_sync_tolerance_seconds(final_audio_track_duration_sec)
            tolerance_ok = (final_audio_track_duration_sec <= 0) or (delta_av <= tolerance_target)
            # scenes_ok: nenhuma cena visual foi criada com duração 0 ou abaixo do mínimo
            scenes_ok = True
            visual_pacing_ok = True
            try:
                scene_durations = [
                    float(item.get("final_visual_duration_sec") or 0.0)
                    for item in (render_report.get("scene_visuals") or [])
                ]
                if scene_durations:
                    scenes_ok = all(d > 0.1 for d in scene_durations)
                max_visual_holds = [
                    float(item.get("max_visual_hold_sec") or item.get("final_visual_duration_sec") or 0.0)
                    for item in (render_report.get("scene_visuals") or [])
                ]
                if max_visual_holds:
                    visual_pacing_ok = all(
                        hold <= (cinematic_visual_hold_sec + 0.05)
                        for hold in max_visual_holds
                    )
            except Exception:
                scenes_ok = True
                visual_pacing_ok = True
            # captions_ok: última legenda NÃO ultrapassa a duração do áudio
            captions_ok = True
            last_caption_end = 0.0
            try:
                for cap in (full_caption_timeline or []):
                    try:
                        e = float(cap.get("end") or 0.0)
                        last_caption_end = max(last_caption_end, e)
                    except Exception:
                        pass
                if last_caption_end > 0 and actual_total_audio_dur > 0:
                    captions_ok = last_caption_end <= (actual_total_audio_dur + 0.25)
            except Exception:
                captions_ok = True
            # cta_ok: end_screen duração está entre 3 e 6 segundos
            cta_ok = (3.0 <= float(end_clip_duration or 0.0) <= 6.0) if closing_has_narration else True
            sync_validation = {
                "audio_duration_sec": round(float(final_audio_track_duration_sec or 0.0), 3),
                "narration_duration_sec": round(float(actual_total_audio_dur or 0.0), 3),
                "silent_endcard_duration_sec": round(float(silent_cinematic_tail_sec or 0.0), 3),
                "video_duration_sec": round(float(obtained_duration_final_sec or 0.0), 3),
                "delta_sec": round(float(delta_av), 3),
                "tolerance_target_sec": tolerance_target,
                "tolerance_ok": bool(tolerance_ok),
                "scenes_ok": bool(scenes_ok),
                "visual_pacing_ok": bool(visual_pacing_ok),
                "max_visual_hold_target_sec": round(cinematic_visual_hold_sec, 3),
                "captions_ok": bool(captions_ok),
                "cta_ok": bool(cta_ok),
                "last_caption_end_sec": round(float(last_caption_end), 3) if last_caption_end else 0.0,
            }
            render_report["sync_validation"] = sync_validation
            # ====== Legenda SRT exportada (item 3) ======
            srt_path = ""
            try:
                _srt_name = (os.path.splitext(filename)[0]) + ".srt"
                srt_path = os.path.join(OUTPUT_DIR, _srt_name) if os.path.isabs(OUTPUT_DIR or "") else os.path.abspath(os.path.join(str(OUTPUT_DIR or "videos"), _srt_name))
                srt_lines: List[str] = []
                def _fmt_srt_ts(t: float) -> str:
                    h = int(t // 3600)
                    m = int((t % 3600) // 60)
                    s = t - (h * 3600 + m * 60)
                    secs = int(s)
                    ms = int(round((s - secs) * 1000, 0))
                    if ms == 1000:
                        secs += 1
                        ms = 0
                    return f"{h:02d}:{m:02d}:{secs:02d},{ms:03d}"
                subtitle_index = 1
                for cap in (full_caption_timeline or []):
                    text = str(cap.get("caption") or "").strip()
                    if not text:
                        continue
                    try:
                        srt_start = float(cap.get("start") or 0.0)
                        srt_end = float(cap.get("end") or 0.0)
                    except Exception:
                        continue
                    if srt_end <= srt_start:
                        srt_end = srt_start + 0.2
                    # Garante 2 linhas no máximo (separa por frases se necessário)
                    words = text.split()
                    if len(words) > 14:
                        mid = len(words) // 2
                        first = " ".join(words[:mid])
                        second = " ".join(words[mid:])
                        display_text = f"{first}\n{second}"
                    else:
                        display_text = text
                    srt_lines.append(str(subtitle_index))
                    srt_lines.append(f"{_fmt_srt_ts(max(0.0, srt_start))} --> {_fmt_srt_ts(srt_end)}")
                    srt_lines.append(display_text)
                    srt_lines.append("")
                    subtitle_index += 1
                if srt_lines:
                    with open(srt_path, "w", encoding="utf-8", errors="replace") as fsrt:
                        fsrt.write("\n".join(srt_lines))
                    if not os.path.exists(srt_path):
                        srt_path = ""
                else:
                    srt_path = ""
            except Exception as _srt_err:
                srt_path = ""
                render_report["srt_error"] = f"{type(_srt_err).__name__}: {str(_srt_err)[:200]}"
            if srt_path and os.path.exists(srt_path):
                _srt_url = f"{VIDEO_URL_PREFIX}/{os.path.basename(srt_path)}"
                render_report["srt"] = {
                    "path": srt_path,
                    "url": _srt_url,
                    "entries": int(subtitle_index - 1),
                    "source": str(caption_timeline_source or "unknown"),
                    "pt_br": True,
                }
            else:
                render_report["srt"] = {"path": "", "url": "", "entries": 0, "source": str(caption_timeline_source or "unknown"), "error": "not_exported"}

            return {
                "video_url": f"{VIDEO_URL_PREFIX}/{filename}",
                "file_path": output_path,
                "music_credit": used_music_credit,
                "used_images": used_image_urls,
                "render_report": render_report,
                "sync_validation": sync_validation,
                "srt_path": srt_path if (srt_path and os.path.exists(srt_path)) else "",
            }
            
        except Exception as e:
                raise e

    def generate_simple_video(self, title, script_lines, output_filename="video.mp4"):
        # Mantendo compatibilidade com código antigo se necessário
        plan = {
            "title": title,
            "scenes": [{"text": line} for line in script_lines if line.strip()]
        }
        result = self.create_video_from_plan(plan)
        # Mantém compatibilidade retornando apenas URL se for o esperado por chamadas antigas diretas
        # Mas vamos atualizar os chamadores para lidar com dict
        return result["video_url"]
