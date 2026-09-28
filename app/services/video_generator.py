Warning: truncated output (original token count: 91388)
Total output lines: 7263

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
from app.services.safe_text_layout import SafeTextLayout

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
            chunk_end = max(cursor + 0.01, chunk_end)
            clone = dict(item or {})
            clone["caption"] = chunk
            clone["start"] = round(cursor, 3)
            clone["end"] = round(chunk_end, 3)
            expanded.append(clone)
            cursor = chunk_end
        if expanded:
            expanded[-1]["end"] = round(end, 3)
        return expanded

    def create_text_overlay(
        self,
        text,
        size=(1080, 1920),
        text_color=(255, 255, 255),
        footer_text: Optional[str] = None,
        max_lines: int = 2,
        vertical_anchor: str = "bottom",
        reserved_bottom_ratio: float = 0.0,
        layout_report: Optional[Dict[str, Any]] = None,
        safe_area_override: Optional[Dict[str, float]] = None,
    ):
        from PIL import Image, ImageDraw, ImageFont
        import numpy as np

        text = (text or "").strip()
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        w, h = size
        margin_x = int(w * CAPTION_SAFE_AREA_X_RATIO)
        margin_bottom = max(int(h * CAPTION_SAFE_AREA_BOTTOM_RATIO), int(h * max(0.0, float(reserved_bottom_ratio or 0.0))))
        layout = self._caption_layout_metrics(
            text,
            size=size,
            max_lines=max_lines,
            reserved_bottom_ratio=reserved_bottom_ratio,
            safe_area_override=safe_area_override,
        )
        chosen_font = layout.get("font")
        chosen_lines = list(layout.get("lines") or [])
        chosen_line_h = int(layout.get("line_h") or 0)
        if chosen_font is None:
            chosen_font = self._load_caption_font(max(14, min(28, int(w * 0.022))))
        if chosen_line_h <= 0:
            chosen_line_h = int(getattr(chosen_font, "size", 18) * 1.20)
        if len(chosen_lines) > max_lines:
            chosen_lines = chosen_lines[:max_lines]

        text_block_h = len(chosen_lines) * chosen_line_h
        anchor = str(vertical_anchor or "").strip().lower()
        if anchor == "top":
            y = max(int(h * CAPTION_SAFE_AREA_TOP_RATIO), int(h * CAPTION_SAFE_AREA_TOP_RATIO))
        elif anchor == "center":
            y = int((h - text_block_h) / 2)
            y = max(int(h * CAPTION_SAFE_AREA_TOP_RATIO), y)
        else:
            y = h - margin_bottom - text_block_h
            y = max(int(h * CAPTION_SAFE_AREA_TOP_RATIO), y)

        outline = (0, 0, 0, 255)
        fill = (int(text_color[0]), int(text_color[1]), int(text_color[2]), 255)
        for line in chosen_lines:
            b = draw.textbbox((0, 0), line, font=chosen_font)
            tw = b[2] - b[0]
            x = int((w - tw) / 2)
            draw.text((x + 3, y + 3), line, font=chosen_font, fill=(0, 0, 0, 160))
            for off in [(2, 2), (-2, -2), (2, -2), (-2, 2), (0, 2), (2, 0), (-2, 0), (0, -2)]:
                draw.text((x + off[0], y + off[1]), line, font=chosen_font, fill=outline)
            draw.text((x, y), line, font=chosen_font, fill=fill)
            y += chosen_line_h

        footer = (footer_text or "").strip()
        if footer:
            footer_fs = max(14, min(34, int(w * 0.028)))
            footer_font = None
            for fp in self._caption_font_candidates():
                try:
                    footer_font = ImageFont.truetype(fp, footer_fs)
                    break
                except Exception:
                    continue
            if footer_font is None:
                footer_font = ImageFont.load_default()

            try:
                fb = draw.textbbox((0, 0), footer, font=footer_font)
                ftw = fb[2] - fb[0]
                fth = fb[3] - fb[1]
            except Exception:
                ftw = int(measure(footer, footer_font))
                fth = int(footer_fs * 1.2)

            pad_x = int(w * 0.03)
            pad_y = int(max(8, h * 0.010))
            fx = int((w - ftw) / 2)
            fy = int(h - pad_y - fth - int(h * 0.02))

            rect = (
                max(0, fx - pad_x),
                max(0, fy - int(pad_y * 0.7)),
                min(w, fx + ftw + pad_x),
                min(h, fy + fth + int(pad_y * 0.7)),
            )
            draw.rectangle(rect, fill=(0, 0, 0, 150))
            for off in [(1, 1), (-1, -1), (1, -1), (-1, 1)]:
                draw.text((fx + off[0], fy + off[1]), footer, font=footer_font, fill=(0, 0, 0, 255))
            draw.text((fx, fy), footer, font=footer_font, fill=(255, 255, 255, 230))

        if isinstance(layout_report, dict):
            safe_area = {
                "top": CAPTION_SAFE_AREA_TOP_RATIO,
                "bottom": max(CAPTION_SAFE_AREA_BOTTOM_RATIO, float(reserved_bottom_ratio or 0.0)),
                "left": CAPTION_SAFE_AREA_X_RATIO,
                "right": CAPTION_SAFE_AREA_X_RATIO,
            }
            if isinstance(safe_area_override, dict):
                safe_area.update({k: float(v) for k, v in safe_area_override.items() if v is not None})
            layout_report.clear()
            layout_report.update(
                {
                    "resolution": f"{w}x{h}",
                    "safe_area": safe_area,
                    "text_fits": bool(layout.get("fits")),
                    "overflow_detected": bool(layout.get("overflow_detected")),
                    "font_size_used": int(layout.get("font_size_used") or getattr(chosen_font, "size", 0) or 0),
                    "line_count": len(chosen_lines),
                    "vertical_anchor": anchor,
                    "footer_present": bool(footer),
                    "lines": chosen_lines,
                }
            )

        return np.array(img)

    def _make_caption(self, narration: str):
        t = (narration or "").strip()
        if not t:
            return ""
        t = re.sub(r"\s+", " ", t)
        parts = re.split(r"(?<=[.!?])\s+", t)
        cap = ""
        for p in parts:
            if not p:
                continue
            if len((cap + " " + p).strip()) <= 180:
                cap = (cap + " " + p).strip()
                if len(cap) >= 120:
                    break
            else:
                break
        if not cap:
            cap = t[:180].rstrip()
        return cap

    def _is_meta_instruction_fragment(self, text: str) -> bool:
        normalized = self._fold_text_for_matching(self._clean_text(text))
        if not normalized:
            return True
        if re.search(r"https?://|www\.|@[\w.-]+", normalized):
            return False
        meta_prefixes = (
            "manter", "reforcar", "reforcar", "aumentar", "reduzir", "evitar", "usar", "incluir",
            "mostrar", "destacar", "tom ", "ritmo", "foco", "camera", "camera ", "transicao",
            "transicao ", "tempo ", "duracao", "duracao ", "estilo ", "consistencia", "consistencia ",
        )
        if normalized.startswith(meta_prefixes):
            return True
        words = re.findall(r"[a-z0-9]+", normalized)
        if not words:
            return True
        abstract_only = {
            "suspense", "gancho", "emocao", "emocional", "conflito", "revelacao", "visual",
            "estrutura", "metrica", "introducao", "desenvolvimento", "conclusao", "resumo",
            "objetivo", "dica", "cta", "chamada", "cena", "pergunta",
        }
        if len(words) <= 3 and all(word in abstract_only for word in words):
            return True
        return False

    def _fold_text_for_matching(self, text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        normalized = unicodedata.normalize("NFKD", raw)
        normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
        return normalized.lower().strip()

    def _strip_structural_markers(self, text: str) -> str:
        raw = self._clean_text(text)
        if not raw:
            return ""

        label_pattern = (
            r"(gancho|pergunta|cta|cena(?:\s+\d+)?|observa(?:cao|caoes|coes|caoes|cao|ção|ções)?|"
            r"nota|m[eé]trica|estrutura|introdu[cç][aã]o|desenvolvimento|conclus[aã]o|"
            r"chamada|dica|objetivo|resumo|reflex[aã]o|mensagem|t[ií]tulo|cap[ií]tulo)"
        )

        kept_parts: List[str] = []
        fragments = re.findall(r'[^.!?…\n]+(?:[.!?…]+["”’\']?|$)', raw)
        for fragment in fragments:
            candidate = (fragment or "").strip(" \t-•*")
            if not candidate:
                continue

            while True:
                match = re.match(rf"^\s*{label_pattern}\s*[:\-–—]\s*(.*)$", candidate, flags=re.IGNORECASE)
                if not match:
                    break
                label = self._fold_text_for_matching(match.group(1))
                remainder = (match.group(2) or "").strip()
                if not remainder or self._is_meta_instruction_fragment(remainder):
                    candidate = ""
                    break
                candidate = remainder
                if label.startswith(("observ", "nota", "met", "estrut")) and self._is_meta_instruction_fragment(candidate):
                    candidate = ""
                    break

            if not candidate:
                continue

            candidate = re.sub(rf"(?i)\b{label_pattern}\s*[:\-–—]\s*", "", candidate)
            candidate = re.sub(r"\s+", " ", candidate).strip(" \t-•*")
            if candidate:
                kept_parts.append(candidate)

        merged = " ".join(kept_parts).strip()
        return re.sub(r"\s+", " ", merged).strip()

    def _normalize_tts_text(self, text: str) -> str:
        t = self._strip_structural_markers(text)
        if not t:
            return ""
        t = unicodedata.normalize("NFKC", t)
        t = re.sub(r"https?://\S+|www\.\S+", " ", t, flags=re.IGNORECASE)
        t = re.sub(r"\b[\w.+-]+@[\w.-]+\.\w+\b", " ", t)
        t = re.sub(r"[_*#`~<>|]+", " ", t)
        t = re.sub(r"\s+([,.;:!?])", r"\1", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    def _estimate_narration_seconds(self, text: str) -> float:
        cleaned = self._normalize_tts_text(text)
        if not cleaned:
            return 0.0
        word_count = len(cleaned.split())
        if word_count <= 0:
            return 0.0
        return max(2.5, round(word_count / 2.45, 2))

    def _count_words(self, text: str) -> int:
        try:
            return len(re.findall(r"\w+", str(text or ""), flags=re.UNICODE))
        except Exception:
            return len(str(text or "").split())

    def _estimate_voice_words_per_minute(self, voice_style: Optional[str] = None, voice_gender: Optional[str] = None) -> float:
        style = str(voice_style or "").strip().lower()
        gender = str(voice_gender or "").strip().lower()
        base_wpm = 147.0
        if style in {"soft_prayer", "prayer", "meditation", "calm", "serene"}:
            base_wpm = 128.0
        elif style in {"human", "natural", "warm"}:
            base_wpm = 145.0
        elif style in {"energetic", "fast", "commercial"}:
            base_wpm = 158.0
        if gender == "male":
            base_wpm -= 2.0
        elif gender == "female":
            base_wpm += 1.0
        return max(118.0, min(165.0, base_wpm))

    def _estimate_text_duration_with_voice(self, text: str, voice_style: Optional[str] = None, voice_gender: Optional[str] = None) -> float:
        cleaned = self._normalize_tts_text(text)
        if not cleaned:
            return 0.0
        words = self._count_words(cleaned)
        if words <= 0:
            return 0.0
        wpm = self._estimate_voice_words_per_minute(voice_style=voice_style, voice_gender=voice_gender)
        punctuation_pauses = len(re.findall(r"[.!?;:]", cleaned))
        seconds = (float(words) / max(1.0, wpm)) * 60.0
        seconds += min(6.0, punctuation_pauses * 0.18)
        return round(max(2.0, seconds), 2)

    def _format_duration_hms(self, duration_sec: float) -> str:
        total = max(0, int(round(float(duration_sec or 0.0))))
        minutes, seconds = divmod(total, 60)
        return f"{minutes} min {seconds:02d} s"

    def _resolve_requested_duration_range_sec(self, plan: Optional[Dict[str, Any]]) -> Dict[str, float]:
        plan = plan if isinstance(plan, dict) else {}
        raw_candidates = {
            "min_sec": plan.get("target_duration_min_sec") or plan.get("duration_min_sec"),
            "max_sec": plan.get("target_duration_max_sec") or plan.get("duration_max_sec"),
            "min_min": plan.get("target_duration_min") or plan.get("duration_min"),
            "max_min": plan.get("target_duration_max") or plan.get("duration_max"),
            "target_sec": plan.get("target_duration_sec"),
            "target_min": plan.get("target_duration_min"),
        }

        def _as_float(value: Any) -> Optional[float]:
            try:
                num = float(value)
            except Exception:
                return None
            return num if num > 0 else None

        min_sec = _as_float(raw_candidates["min_sec"])
        max_sec = _as_float(raw_candidates["max_sec"])
        min_min = _as_float(raw_candidates["min_min"])
        max_min = _as_float(raw_candidates["max_min"])
        target_sec = _as_float(raw_candidates["target_sec"])
        target_min = _as_float(raw_candidates["target_min"])

        if min_sec is None and min_min is not None:
            min_sec = min_min * 60.0
        if max_sec is None and max_min is not None:
            max_sec = max_min * 60.0
        if target_sec is None and target_min is not None:
            target_sec = target_min * 60.0
        if min_sec is None and target_sec is not None:
            min_sec = target_sec
        if max_sec is None and target_sec is not None:
            max_sec = target_sec
        if min_sec is None and max_sec is not None:
            min_sec = max_sec
        if max_sec is None and min_sec is not None:
            max_sec = min_sec
        if min_sec is None:
            min_sec = 0.0
        if max_sec is None:
            max_sec = min_sec
        if max_sec and min_sec and max_sec < min_sec:
            max_sec = min_sec
        target = target_sec if target_sec is not None else (max_sec or min_sec or 0.0)
        return {
            "min_sec": round(float(min_sec or 0.0), 2),
            "max_sec": round(float(max_sec or 0.0), 2),
            "target_sec": round(float(target or 0.0), 2),
        }

    def _resolve_channel_name(self, plan: Optional[Dict[str, Any]] = None) -> str:
        plan = plan if isinstance(plan, dict) else {}
        candidates = [
            plan.get("channel_name"),
            os.getenv("YOUTUBE_CHANNEL_NAME"),
            os.getenv("CHANNEL_NAME"),
            os.getenv("SITE_NAME"),
        ]
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value:
                return value[:80]
        try:
            from app.services.youtube_service import YouTubeService
            stats = YouTubeService().get_channel_stats()
            title = str((stats or {}).get("title") or "").strip() if isinstance(stats, dict) else ""
            if title:
                return title[:80]
        except Exception:
            pass
        return "HERDEIROS DAS PROMESSAS"

    def _compact_cinematic_phrase(self, value: Any, *, max_words: int = 14) -> str:
        text = self._normalize_tts_text(str(value or "").strip())
        if not text:
            return ""
        sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].strip()
        words = sentence.split()
        if len(words) <= max_words:
            return sentence
        compact = " ".join(words[: max(1, int(max_words))]).rstrip(" ,;:-—")
        return compact + ("?" if sentence.endswith("?") else ".")

    def _default_opening_text(
        self,
        channel_name: str,
        *,
        plan: Optional[Dict[str, Any]] = None,
    ) -> str:
        # O título/gancho já aparece visualmente nos primeiros quatro segundos.
        # A primeira fala é a apresentação fixa acordada para todo o Codexia.
        return CHANNEL_PRESENTATION_TEXT

    def _default_reflection_text(self, plan: Optional[Dict[str, Any]] = None, scenes: Optional[List[Dict[str, Any]]] = None) -> str:
        plan = plan if isinstance(plan, dict) else {}
        title = str(plan.get("title") or "").strip()
        base_theme = title or "esta mensagem"
        return (
            f"Que a reflexão final sobre {base_theme} nos lembre que Deus continua presente, "
            "cura o coração e responde a quem persevera em fé."
        )

    def _wrap_endcard_message(self, value: Any, *, max_chars: int = 46, max_lines: int = 2) -> List[str]:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if not text:
            return []
        words = text.split()
        lines: List[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and len(candidate) > max_chars:
                lines.append(current)
                current = word
                if len(lines) >= max_lines:
                    break
            else:
                current = candidate
        if len(lines) < max_lines and current:
            lines.append(current)
        consumed_words = sum(len(line.split()) for line in lines)
        if consumed_words < len(words) and lines:
            lines[-1] = lines[-1].rstrip(" ,;:-—.!?") + "…"
        return lines[:max_lines]

    def _contextual_reflection_from_plan(self, plan: Dict[str, Any]) -> str:
        scenes = plan.get("scenes") if isinstance(plan.get("scenes"), list) else []
        context_parts = [str(plan.get("title") or ""), str(plan.get("theme") or "")]
        context_parts.extend(
            str((scene or {}).get("text") or (scene or {}).get("narration") or "")
            for scene in scenes[:4]
            if isinstance(scene, dict)
        )
        normalized = unicodedata.normalize("NFKD", " ".join(context_parts)).encode("ascii", "ignore").decode("ascii").lower()
        rules = [
            (("solidao", "sozinho", "vazio"), "Mesmo quando a solidão pesa, Deus permanece perto e renova a esperança de quem abre o coração."),
            (("medo", "ansiedade", "preocupacao"), "A fé não ignora o medo; ela nos lembra que Deus caminha conosco em cada novo passo."),
            (("perdao", "culpa", "recomeco"), "O perdão abre espaço para um novo começo e nos convida a caminhar com graça e verdade."),
            (("proposito", "chamado", "escolheu"), "Seu propósito amadurece quando a fé se transforma em atitude, serviço e perseverança."),
            (("desafio", "incerteza", "tempestade", "prova"), "Mesmo diante do desafio, Deus continua presente e fortalece quem escolhe avançar pela fé."),
            (("amor", "cura", "coracao"), "O amor de Deus alcança o coração, restaura a esperança e nos ensina a cuidar uns dos outros."),
            (("gratidao", "agradecer", "bencao"), "A gratidão muda o olhar e nos ajuda a reconhecer a presença de Deus também nas pequenas coisas."),
        ]
        for keywords, message in rules:
            if any(keyword in normalized for keyword in keywords):
                return message
        return "Leve esta mensagem com você: Deus permanece presente e fortalece quem escolhe caminhar pela fé."

    def _resolve_contextual_closing(self, plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        plan = plan if isinstance(plan, dict) else {}
        branding = plan.get("branding") if isinstance(plan.get("branding"), dict) else {}

        explicit_message = branding.get("final_message") or plan.get("final_message")
        if explicit_message:
            if isinstance(explicit_message, str):
                lines = [line.strip() for line in re.split(r"[\r\n]+", explicit_message) if line.strip()]
            elif isinstance(explicit_message, (list, tuple)):
                lines = [str(line).strip() for line in explicit_message if str(line or "").strip()]
            else:
                lines = []
            if lines:
                return {
                    "kind": "custom",
                    "source": "explicit_final_message",
                    "text": " ".join(lines),
                    "reference": None,
                    "lines": lines[:3],
                }

        sources: List[Dict[str, Any]] = [branding, plan]
        scenes = plan.get("scenes") if isinstance(plan.get("scenes"), list) else []
        sources.extend(scene for scene in scenes if isinstance(scene, dict))
        verse_text = ""
        verse_reference = ""
        for source in sources:
            structured = source.get("scripture") if isinstance(source.get("scripture"), dict) else {}
            structured_verse = source.get("bible_verse") if isinstance(source.get("bible_verse"), dict) else {}
            for candidate in (structured, structured_verse):
                if not verse_text:
                    verse_text = str(candidate.get("text") or candidate.get("verse") or "").strip()
                if not verse_reference:
                    verse_reference = str(candidate.get("reference") or candidate.get("ref") or "").strip()
            if not verse_text:
                for key in ("closing_verse", "meditation_verse", "verse_text", "scripture_text", "bible_verse"):
                    value = source.get(key)
                    if value and not isinstance(value, dict):
                        verse_text = str(value).strip()
                        break
            if not verse_reference:
                for key in ("verse_reference", "bible_reference", "biblical_reference", "scripture_reference"):
                    value = str(source.get(key) or "").strip()
                    if value:
                        verse_reference = value
                        break
            if verse_text or verse_reference:
                break

        if verse_text or verse_reference:
            compact_verse = self._compact_cinematic_phrase(verse_text, max_words=18)
            label = f"MEDITE EM {verse_reference}" if verse_reference else "VERSÍCULO PARA MEDITAÇÃO"
            lines = [label.upper(), *self._wrap_endcard_message(compact_verse, max_chars=48, max_lines=2)]
            return {
                "kind": "verse",
                "source": "explicit_scripture",
                "text": compact_verse,
                "reference": verse_reference or None,
                "lines": lines[:3],
            }

        reflection = ""
        reflection_source = "rule_based_context"
        for source in (branding, plan):
            for key in ("closing_reflection", "final_reflection", "reflection_text", "meditation_text"):
                value = str(source.get(key) or "").strip()
                if value:
                    reflection = value
                    reflection_source = f"explicit_{key}"
                    break
            if reflection:
                break
        reflection = self._compact_cinematic_phrase(
            reflection or self._contextual_reflection_from_plan(plan),
            max_words=22,
        )
        return {
            "kind": "reflection",
            "source": reflection_source,
            "text": reflection,
            "reference": None,
            "lines": ["PARA REFLETIR", *self._wrap_endcard_message(reflection, max_chars=48, max_lines=2)][:3],
        }

    def _default_closing_text(self, channel_name: str) -> str:
        return DEFAULT_NARRATED_CTA_TEXT

    def _default_channel_slogan(self) -> str:
        return "ONDE A FÉ SE TORNA ATITUDE"

    def _resolve_endcard_channel_lines(
        self,
        channel_name: str,
        channel_slogan: Optional[str] = None,
    ) -> List[str]:
        original_name = re.sub(r"\s+", " ", str(channel_name or "").strip())
        safe_name = original_name
        safe_slogan = re.sub(r"\s+", " ", str(channel_slogan or "").strip())
        if not safe_slogan:
            safe_slogan = self._default_channel_slogan()
        if safe_slogan:
            safe_name = re.sub(
                r"[\s\-|,:;]*!?\s*onde\s+a\s+f[ée]\s+se\s+torna\s+atitude!?\s*$",
                "",
                safe_name,
                flags=re.IGNORECASE,
            ).strip()
        if not safe_name:
            safe_name = "HERDEIROS DAS PROMESSAS"
        lines = [safe_name.upper(), safe_slogan.upper()]
        return lines[:2]

    def _compose_segmented_narration_audio(
        self,
        *,
        main_text: str,
        cta_text: str,
        voice_style: Optional[str] = None,
        voice_gender: Optional[str] = None,
        pause_duration_sec: float = 1.25,
        initial_silence_duration_sec: float = 0.0,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        try:
            from moviepy.editor import AudioFileClip, concatenate_audioclips, AudioClip
        except ImportError:
            from moviepy import AudioFileClip, concatenate_audioclips, AudioClip

        main_audio_path = self.generate_audio(
            main_text,
            voice_style=voice_style,
            voice_gender=voice_gender,
            status_callback=status_callback,
            segment_label="narração principal",
        )
        if not main_audio_path or not os.path.exists(main_audio_path):
            raise Exception("Falha ao gerar o audio principal da narracao.")

        main_audio_clip = AudioFileClip(main_audio_path)
        cta_audio_path = None
        cta_audio_clip = None
        silence_clip = None
        initial_silence_clip = None
        combined_clip = None
        combined_audio_path = main_audio_path
        pause_duration_sec = max(0.0, float(pause_duration_sec or 0.0))
        initial_silence_duration_sec = max(0.0, float(initial_silence_duration_sec or 0.0))
        try:
            fps = int(getattr(main_audio_clip, "fps", 44100) or 44100)
            if initial_silence_duration_sec > 0:
                initial_silence_clip = AudioClip(lambda t: 0, duration=initial_silence_duration_sec, fps=fps)
            if cta_text:
                cta_audio_path = self.generate_audio(
                    cta_text,
                    voice_style=voice_style,
                    voice_gender=voice_gender,
                    status_callback=status_callback,
                    segment_label="encerramento",
                )
                if not cta_audio_path or not os.path.exists(cta_audio_path):
                    raise Exception("Falha ao gerar o audio do CTA.")
                cta_audio_clip = AudioFileClip(cta_audio_path)
                sequence = []
                if initial_silence_clip is not None:
                    sequence.append(initial_silence_clip)
                sequence.append(main_audio_clip)
                if pause_duration_sec > 0:
                    silence_clip = AudioClip(lambda t: 0, duration=pause_duration_sec, fps=fps)
                    sequence.append(silence_clip)
                sequence.append(cta_audio_clip)
                combined_clip = concatenate_audioclips(sequence)
                combined_audio_path = os.path.join(self.output_dir, f"narration_{uuid.uuid4().hex}.mp3")
                combined_clip.write_audiofile(
                    combined_audio_path,
                    fps=fps,
                    nbytes=2,
                    codec="mp3",
                    bitrate="192k",
                    logger=None,
                )
            elif initial_silence_clip is not None:
                combined_clip = concatenate_audioclips([initial_silence_clip, main_audio_clip])
                combined_audio_path = os.path.join(self.output_dir, f"narration_{uuid.uuid4().hex}.mp3")
                combined_clip.write_audiofile(
                    combined_audio_path,
                    fps=fps,
                    nbytes=2,
                    codec="mp3",
                    bitrate="192k",
                    logger=None,
                )

            return {
                "audio_path": combined_audio_path,
                "main_audio_path": main_audio_path,
                "cta_audio_path": cta_audio_path,
                "main_duration_sec": round(float(getattr(main_audio_clip, "duration", 0.0) or 0.0), 2),
                "cta_duration_sec": round(float(getattr(cta_audio_clip, "duration", 0.0) or 0.0), 2) if cta_audio_clip is not None else 0.0,
                "initial_silence_duration_sec": round(initial_silence_duration_sec, 2),
                "pause_duration_sec": round(pause_duration_sec, 2),
            }
        finally:
            for clip in [combined_clip, initial_silence_clip, silence_clip, cta_audio_clip, main_audio_clip]:
                try:
                    if clip is not None:
                        clip.close()
                except Exception:
                    pass

    def _resolve_channel_branding(self, plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        plan = plan if isinstance(plan, dict) else {}
        branding = plan.get("branding") if isinstance(plan.get("branding"), dict) else {}
        channel_name = self._resolve_channel_name(plan)

        def _pick(*values):
            for value in values:
                text = str(value or "").strip()
                if text:
                    return text
            return ""

        logo_info: Dict[str, Any] = {}
        try:
            from app.services.global_settings_service import build_global_settings_service

            logo_info = build_global_settings_service().resolve_official_channel_logo()
        except Exception:
            logo_info = {}

        logo_candidate = _pick(
            branding.get("logo"),
            branding.get("logo_path"),
            branding.get("logo_url"),
            plan.get("channel_logo"),
            plan.get("channel_logo_path"),
            plan.get("channel_logo_url"),
            logo_info.get("selected_value"),
        )
        opening_image_candidate = _pick(
            branding.get("opening_image"),
            branding.get("opening_image_path"),
            branding.get("opening_image_url"),
            plan.get("opening_image"),
            plan.get("opening_image_path"),
            plan.get("opening_image_url"),
        )
        closing_image_candidate = _pick(
            branding.get("closing_image"),
            branding.get("closing_image_path"),
            branding.get("closing_image_url"),
            plan.get("closing_image"),
            plan.get("closing_image_path"),
            plan.get("closing_image_url"),
        )
        channel_slogan = _pick(
            branding.get("channel_slogan"),
            plan.get("channel_slogan"),
            os.getenv("YOUTUBE_CHANNEL_SLOGAN"),
            os.getenv("CHANNEL_SLOGAN"),
        )
        channel_title_lines = self._resolve_endcard_channel_lines(channel_name, channel_slogan=channel_slogan)

        contextual_closing = self._resolve_contextual_closing(plan)
        final_message = list(contextual_closing.get("lines") or [])[:3]
        if not final_message:
            final_message = ["PARA REFLETIR", "LEVE ESTA MENSAGEM COM VOCÊ."]
        endcard_cta_text = _pick(
            branding.get("endcard_cta_text"),
            plan.get("endcard_cta_text"),
            "INSCREVA-SE E CONTINUE CONOSCO",
        )

        primary_color = _pick(branding.get("primary_color"), plan.get("primary_color"), "#F6E7B0")
        secondary_color = _pick(branding.get("secondary_color"), plan.get("secondary_color"), "#FFFFFF")

        return {
            "channel_name": channel_name,
            "channel_slogan": channel_slogan or (channel_title_lines[1] if len(channel_title_lines) > 1 else ""),
            "channel_title_lines": channel_title_lines,
            "logo_candidate": logo_candidate,
            "logo_path": self._resolve_input_image_path(logo_candidate),
            "opening_image_candidate": opening_image_candidate,
            "opening_image_path": self._resolve_input_image_path(opening_image_candidate),
            "closing_image_candidate": closing_image_candidate,
            "closing_image_path": self._resolve_input_image_path(closing_image_candidate),
            "primary_color": primary_color,
            "secondary_color": secondary_color,
            "font": _pick(branding.get("font"), plan.get("font"), "DejaVuSans-Bold"),
            "title_style": _pick(branding.get("title_style"), plan.get("title_style"), "cinematic_minimal"),
            "entry_animation": _pick(branding.get("entry_animation"), plan.get("entry_animation"), "fade"),
            "exit_animation": _pick(branding.get("exit_animation"), plan.get("exit_animation"), "fade_out"),
            "final_message_lines": final_message[:3],
            "contextual_closing": contextual_closing,
            "endcard_cta_text": endcard_cta_text,
            "logo_source": logo_info.get("selected_source"),
            "future_ready": {
                "logo": bool(logo_candidate),
                "opening_image": bool(opening_image_candidate),
                "closing_image": bool(closing_image_candidate),
                "primary_color": primary_color,
                "secondary_color": secondary_color,
                "font": _pick(branding.get("font"), plan.get("font")),
                "title_style": _pick(branding.get("title_style"), plan.get("title_style")),
                "entry_animation": _pick(branding.get("entry_animation"), plan.get("entry_animation")),
                "exit_animation": _pick(branding.get("exit_animation"), plan.get("exit_animation")),
                "final_message": final_message,
            },
        }

    def _redistribute_body_text_to_scenes(self, body_text: str, scenes: List[Dict[str, Any]]) -> List[str]:
        if not scenes:
            return []
        cleaned_body = self._normalize_tts_text(body_text)
        if not cleaned_body:
            return ["" for _ in scenes]
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned_body) if s and s.strip()]
        if not sentences:
            return [cleaned_body] + ["" for _ in scenes[1:]]

        weights: List[int] = []
        for scene in scenes:
            estimated = int(max(1, round(float(scene.get("_estimated_narration_sec") or self._estimate_narration_seconds(scene.get("_tts_text") or scene.get("text") or "")))))
            weights.append(max(1, estimated))
        total_weight = sum(weights) or len(scenes)
        total_words = sum(max(1, self._count_words(sentence)) for sentence in sentences)
        target_words = [max(8, int(round(total_words * (weight / float(total_weight))))) for weight in weights]

        distributed: List[str] = []
        cursor = 0
        for idx, target in enumerate(target_words):
            if idx == len(target_words) - 1:
                chunk_sentences = sentences[cursor:]
            else:
                chunk_sentences = []
                chunk_words = 0
                while cursor < len(sentences):
                    candidate = sentences[cursor]
                    candidate_words = max(1, self._count_words(candidate))
                    if chunk_sentences and chunk_words >= target:
                        break
                    chunk_sentences.append(candidate)
                    chunk_words += candidate_words
                    cursor += 1
                if not chunk_sentences and cursor < len(sentences):
                    chunk_sentences = [sentences[cursor]]
                    cursor += 1
            distributed.append(" ".join(chunk_sentences).strip())

        while len(distributed) < len(scenes):
            distributed.append(distributed[-1] if distributed else "")
        return distributed[:len(scenes)]

    def _condense_body_text_to_fit(self, body_text: str, scenes: List[Dict[str, Any]], target_max_sec: float, voice_style: Optional[str] = None, voice_gender: Optional[str] = None, kind: Optional[str] = None) -> Dict[str, Any]:
        clean_body = self._normalize_tts_text(body_text)
        if not clean_body:
            return {"body_text": "", "scene_texts": ["" for _ in scenes], "used_ai": False, "attempted": False}
        estimated_now = self._estimate_text_duration_with_voice(clean_body, voice_style=voice_style, voice_gender=voice_gender)
        if target_max_sec <= 0 or estimated_now <= target_max_sec:
            return {
                "body_text": clean_body,
                "scene_texts": [self._normalize_tts_text(scene.get("_tts_text") or scene.get("text") or "") for scene in scenes],
                "used_ai": False,
                "attempted": False,
            }

        reduction_ratio = max(0.45, min(0.94, float(target_max_sec) / max(1.0, estimated_now)))
        target_words = max(24, int(self._count_words(clean_body) * reduction_ratio))
        condensed = clean_body
        used_ai = False

        if self.ai_service and hasattr(self.ai_service, "_generate_text"):
            try:
                safe_kind = str(kind or "story").strip().lower() or "story"
                prompt = (
                    f"Reescreva o texto abaixo para narracao em video no formato {safe_kind}, "
                    f"mantendo a mensagem, os personagens e a progressao dramatica, mas reduzindo para cerca de {target_words} palavras. "
                    "Remova repeticoes, trechos redundantes e voltas desnecessarias. "
                    "Nao adicione saudacao, nao adicione CTA, nao use titulos de secao, nao use markdown, nao use listas. "
                    "Retorne apenas o texto final enxuto em portugues.\n\n"
                    f"TEXTO:\n{clean_body[:12000]}"
                )
                ai_result = self.ai_service._generate_text(
                    prompt,
                    system_prompt="Voce e um editor de narracao para YouTube. Entregue apenas o texto final em portugues.",
                    temperature=0.3,
                    json_mode=False,
                )
                normalized = self._normalize_tts_text(ai_result)
                if normalized:
                    condensed = normalized
                    used_ai = True
            except Exception:
                condensed = clean_body

        if condensed == clean_body:
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", clean_body) if s and s.strip()]
            kept: List[str] = []
            current_words = 0
            for idx, sentence in enumerate(sentences):
                sentence_words = max(1, self._count_words(sentence))
                remaining = len(sentences) - idx
                if current_words + sentence_words > target_words and kept and remaining > 1:
                    continue
                kept.append(sentence)
                current_words += sentence_words
                if current_words >= target_words and remaining <= 1:
                    break
            condensed = " ".join(kept).strip() or clean_body
            if self._count_words(condensed) > target_words:
                trimmed_words = condensed.split()[:target_words]
                condensed = " ".join(trimmed_words).strip()
                if condensed and not re.search(r"[.!?]$", condensed):
                    condensed = condensed.rstrip(",;:") + "."

        condensed_estimate = self._estimate_text_duration_with_voice(condensed, voice_style=voice_style, voice_gender=voice_gender)
        if condensed and target_max_sec > 0 and condensed_estimate > target_max_sec:
            trim_ratio = max(0.45, min(0.95, float(target_max_sec) / max(1.0, condensed_estimate)))
            final_target_words = max(18, int(self._count_words(condensed) * trim_ratio))
            final_words = condensed.split()[:final_target_words]
            condensed = " ".join(final_words).strip()
            if condensed and not re.search(r"[.!?]$", condensed):
                condensed = condensed.rstrip(",;:") + "."

        scene_texts = self._redistribute_body_text_to_scenes(condensed, scenes)
        return {
            "body_text": condensed,
            "scene_texts": scene_texts,
            "used_ai": used_ai,
            "attempted": True,
        }

    def prepare_final_narration_text(self, plan: Optional[Dict[str, Any]], scenes: List[Dict[str, Any]], voice_style: Optional[str] = None, voice_gender: Optional[str] = None) -> Dict[str, Any]:
        plan = plan if isinstance(plan, dict) else {}
        kind = str(plan.get("kind") or "story").strip().lower() or "story"
        channel_name = self._resolve_channel_name(plan)
        opening_text = self._default_opening_text(channel_name, plan=plan)
        reflection_text = self._normalize_tts_text(str(plan.get("reflection_text") or "").strip()) or self._default_reflection_text(plan, scenes)
        closing_text = self._default_closing_text(channel_name)
        intro_opening_hold_sec = DEFAULT_OPENING_SILENCE_SEC
        pause_duration_sec = 0.55
        end_screen_target_duration_sec = DEFAULT_CINEMATIC_END_SCREEN_SEC
        cleaned_scene_texts = [self._normalize_tts_text(scene.get("_tts_text") or scene.get("text") or "") for scene in scenes]
        story_text = " ".join(text for text in cleaned_scene_texts if text).strip()
        body_text = " ".join(part for part in [story_text, reflection_text] if part).strip()
        duration_range = self._resolve_requested_duration_range_sec(plan)
        max_total_sec = float(duration_range.get("max_sec") or 0.0)
        min_total_sec = float(duration_range.get("min_sec") or 0.0)

        planning_attempts: List[Dict[str, Any]] = []
        planning_max_total_sec = float(max_total_sec) * 0.96 if max_total_sec > 0 else 0.0
        opening_est = self._estimate_text_duration_with_voice(opening_text, voice_style=voice_style, voice_gender=voice_gender)
        closing_est = self._estimate_text_duration_with_voice(closing_text, voice_style=voice_style, voice_gender=voice_gender)
        reflection_est = self._estimate_text_duration_with_voice(reflection_text, voice_style=voice_style, voice_gender=voice_gender)
        story_est = self._estimate_text_duration_with_voice(story_text, voice_style=voice_style, voice_gender=voice_gender)
        scene_texts = list(cleaned_scene_texts)

        for attempt in range(3):
            body_est = self._estimate_text_duration_with_voice(body_text, voice_style=voice_style, voice_gender=voice_gender)
            total_est = intro_opening_hold_sec + opening_est + body_est + closing_est + pause_duration_sec
            planning_attempts.append({
                "attempt": attempt + 1,
                "body_word_count": self._count_words(body_text),
                "estimated_total_duration_sec": round(total_est, 2),
                "within_requested_range": bool((not min_total_sec or total_est >= min_total_sec) and (not max_total_sec or total_est <= max_total_sec)),
                "within_planning_budget": bool((not min_total_sec or total_est >= min_total_sec) and (not planning_max_total_sec or total_est <= planning_max_total_sec)),
            })
            if not planning_max_total_sec or total_est <= planning_max_total_sec:
                break
            target_body_max_sec = max(8.0, planning_max_total_sec - opening_est - closing_est)
            condensed = self._condense_body_text_to_fit(
                body_text,
                scenes,
                target_max_sec=target_body_max_sec,
                voice_style=voice_style,
                voice_gender=voice_gender,
                kind=kind,
            )
            new_body_text = self._normalize_tts_text(condensed.get("body_text") or "")
            if not new_body_text or new_body_text == body_text:
                break
            body_text = new_body_text
            scene_texts = [self._normalize_tts_text(text) for text in (condensed.get("scene_texts") or [])]
            if len(scene_texts) != len(scenes):
                scene_texts = self._redistribute_body_text_to_scenes(body_text, scenes)

        full_text_parts = [opening_text.strip(), body_text.strip(), closing_text.strip()]
        full_text = " ".join(part for part in full_text_parts if part).strip()
        opening_est = self._estimate_text_duration_with_voice(opening_text, voice_style=voice_style, voice_gender=voice_gender)
        body_est = self._estimate_text_duration_with_voice(body_text, voice_style=voice_style, voice_gender=voice_gender)
        closing_est = self._estimate_text_duration_with_voice(closing_text, voice_style=voice_style, voice_gender=voice_gender)
        story_est = self._estimate_text_duration_with_voice(story_text, voice_style=voice_style, voice_gender=voice_gender)
        reflection_est = self._estimate_text_duration_with_voice(reflection_text, voice_style=voice_style, voice_gender=voice_gender)
        total_est = intro_opening_hold_sec + opening_est + body_est + closing_est + pause_duration_sec

        if len(scene_texts) != len(scenes):
            scene_texts = self._redistribute_body_text_to_scenes(body_text, scenes)

        scene_estimates: List[float] = []
        for idx, scene_text in enumerate(scene_texts):
            scene_est = self._estimate_text_duration_with_voice(scene_text, voice_style=voice_style, voice_gender=voice_gender)
            if scene_est <= 0 and idx < len(scenes):
                scene_est = self._estimate_text_duration_with_voice(scenes[idx].get("_tts_text") or scenes[idx].get("text") or "", voice_style=voice_style, voice_gender=voice_gender)
            scene_estimates.append(max(0.0, scene_est))

        return {
            "channel_name": channel_name,
            "opening_text": opening_text,
            "story_text": story_text,
            "reflection_text": reflection_text,
            "body_text": body_text,
            "cta_text": closing_text,
            "closing_text": closing_text,
            "full_text": full_text,
            "voice_words_per_minute": round(self._estimate_voice_words_per_minute(voice_style=voice_style, voice_gender=voice_gender), 2),
            "char_count": len(full_text),
            "word_count": self._count_words(full_text),
            "opening_duration_est_sec": round(opening_est, 2),
            "story_duration_est_sec": round(story_est, 2),
            "reflection_duration_est_sec": round(reflection_est, 2),
            "body_duration_est_sec": round(body_est, 2),
            "closing_duration_est_sec": round(closing_est, 2),
            "cta_duration_est_sec": round(closing_est, 2),
            "estimated_total_duration_sec": round(total_est, 2),
            "planning_target_max_sec": round(planning_max_total_sec, 2) if planning_max_total_sec > 0 else 0.0,
            "requested_duration_range_sec": duration_range,
            "scene_texts": scene_texts,
            "scene_estimated_durations_sec": [round(value, 2) for value in scene_estimates],
            "planning_attempts": planning_attempts,
            "intro_opening_hold_sec": round(intro_opening_hold_sec, 2),
            "pause_duration_sec": round(pause_duration_sec, 2),
            "end_screen_target_duration_sec": round(end_screen_target_duration_sec, 2),
        }

    def _slice_caption_timeline(self, timeline: List[Dict[str, Any]], start_sec: float, end_sec: float) -> List[Dict[str, Any]]:
        if not isinstance(timeline, list) or end_sec <= start_sec:
            return []
        sliced: List[Dict[str, Any]] = []
        for item in timeline:
            try:
                item_start = float(item.get("start") or 0.0)
                item_end = float(item.get("end") or 0.0)
            except Exception:
                continue
            if item_end <= start_sec or item_start >= end_sec:
                continue
            local_start = max(start_sec, item_start) - start_sec
            local_end = min(end_sec, item_end) - start_sec
            caption = str(item.get("caption") or "").strip()
            if local_end > local_start and caption:
                sliced.append({
                    "start": round(local_start, 3),
                    "end": round(local_end, 3),
                    "caption": caption,
                })
        return sliced

    def _sanitize_caption_timeline(
        self,
        timeline: List[Dict[str, Any]],
        audio_duration: float,
    ) -> List[Dict[str, Any]]:
        """Normaliza a timeline final para impedir legenda fora do áudio ou repetida.

        A timeline é a fonte única do overlay e do SRT. Itens são recortados ao
        áudio real, ordenados e legendas idênticas sobrepostas são fundidas. Uma
        repetição separada por fala real é preservada.
        """
        limit = max(0.0, float(audio_duration or 0.0))
        if limit <= 0 or not isinstance(timeline, list):
            return []
        cleaned: List[Dict[str, Any]] = []
        for raw in timeline:
            if not isinstance(raw, dict):
                continue
            caption = re.sub(r"\s+", " ", str(raw.get("caption") or "")).strip()
            if not caption:
                continue
            try:
                start = max(0.0, min(limit, float(raw.get("start") or 0.0)))
                end = max(0.0, min(limit, float(raw.get("end") or 0.0)))
            except Exception:
                continue
            if end <= start:
                continue
            item = dict(raw)
            item.update({"caption": caption, "start": round(start, 3), "end": round(end, 3)})
            cleaned.append(item)
        cleaned.sort(key=lambda item: (float(item["start"]), float(item["end"])))

        merged: List[Dict[str, Any]] = []
        for item in cleaned:
            if merged:
                previous = merged[-1]
                same_text = self._normalize_tts_text(previous.get("caption")) == self._normalize_tts_text(item.get("caption"))
                overlaps = float(item["start"]) <= float(previous["end"]) + 0.08
                if same_text and overlaps:
                    previous["end"] = round(max(float(previous["end"]), float(item["end"])), 3)
                    continue
            merged.append(item)
        return merged

    def _caption_overlay_clips_for_window(
        self,
        timeline: List[Dict[str, Any]],
        start_sec: float,
        end_sec: float,
        size,
    ) -> List[Any]:
        """Build literal, audio-timed overlays for opening or closing windows."""
        overlays: List[Any] = []
        sliced = self._slice_caption_timeline(timeline, start_sec, end_sec)
        expanded: List[Dict[str, Any]] = []
        for item in sliced:
            expanded.extend(
                self._expand_caption_item_for_overlay(
                    item,
                    size=size,
                    reserved_bottom_ratio=CAPTION_SAFE_AREA_BOTTOM_RATIO,
                )
            )
        for item in expanded:
            caption = str(item.get("caption") or "").strip()
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or 0.0)
            if not caption or end <= start:
                continue
            overlay_arr = self.create_text_overlay(
                caption,
                size=size,
                text_color=(255, 255, 255),
                reserved_bottom_ratio=CAPTION_SAFE_AREA_BOTTOM_RATIO,
            )
            overlay_clip = self._clip_from_rgba(overlay_arr, end - start, crop_transparent=True)
            overlays.append(self._set_clip_start(overlay_clip, start))
        return overlays

    def _clean_image_prompt_seed(self, prompt: str, max_chars: int = 180) -> str:
        cleaned = self._clean_text(prompt)
        if not cleaned:
            return ""
        cleaned = re.sub(r"(?i)\bphotorealistic\b|\bcinematic\b|\bphotography\b|\brepresenting\b", " ", cleaned)
        cleaned = re.sub(r"(?i)\bfocus on this exact moment\s*:\s*", " ", cleaned)
        cleaned = re.sub(r"(?i)\bshow the exact narrated moment\s*:\s*", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.;:-")
        if len(cleaned) > max_chars:
            cleaned = cleaned[:max_chars].rsplit(" ", 1)[0].strip()
        return cleaned

    def _compact_narrative_moment(self, text: str, max_chars: int = 120) -> str:
        cleaned = self._normalize_tts_text(text)
        if not cleaned:
            return ""
        first_sentence = re.split(r"(?<=[.!?])\s+", cleaned)[0].strip()
        compact = first_sentence or cleaned
        if len(compact) > max_chars:
            compact = compact[:max_chars].rsplit(" ", 1)[0].strip()
        return compact

    def _strip_visual_prompt_labels(self, text: str) -> str:
        cleaned = self._clean_text(text)
        if not cleaned:
            return ""
        cleaned = re.sub(
            r"(?i)\b(personagem|ambiente|iluminacao|iluminação|momento(?:\s+narrativo)?|narrativa|continuidade|estilo)\s*:\s*",
            " ",
            cleaned,
        )
        return re.sub(r"\s+", " ", cleaned).strip(" ,.;:-")

    def _normalize_semantic_text(self, text: str) -> str:
        cleaned = self._strip_visual_prompt_labels(text)
        if not cleaned:
            return ""
        return self._fold_text_for_matching(cleaned)

    def _extract_semantic_tags(self, text: str, catalog: Dict[str, List[str]]) -> List[str]:
        normalized = self._normalize_semantic_text(text)
        if not normalized:
            return []
        matches: List[str] = []
        for label, keywords in catalog.items():
            for keyword in keywords:
                keyword_norm = self._normalize_semantic_text(keyword)
                if not keyword_norm:
                    continue
                if re.search(rf"(?<!\w){re.escape(keyword_norm)}(?!\w)", normalized):
                    matches.append(label)
                    break
        return matches

    def _extract_character_tags(self, text: str) -> List[str]:
        if not text:
            return []
        cleaned = self._strip_visual_prompt_labels(text)
        if not cleaned:
            return []
        normalized_full = self._fold_text_for_matching(cleaned)
        known_character_aliases = {
            "jesus": "Jesus",
            "cristo": "Jesus",
            "pedro": "Pedro",
            "paulo": "Paulo",
            "maria": "Maria",
            "jose": "Jose",
            "joao": "Joao",
            "davi": "Davi",
            "daniel": "Daniel",
            "moises": "Moises",
            "abraao": "Abraao",
            "jaco": "Jaco",
            "isaque": "Isaque",
            "elias": "Elias",
            "eliseu": "Eliseu",
            "marta": "Marta",
            "lazaro": "Lazaro",
        }
        tokens = re.findall(
            r"\b[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{1,}(?:\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{1,})?\b",
            cleaned,
        )
        blacklist = {
            "cena", "gancho", "pergunta", "observacao", "nota", "metrica", "estrutura", "introducao",
            "desenvolvimento", "conclusao", "resumo", "chamada", "dica", "objetivo", "momento",
            "narrativa", "continuidade", "estilo", "personagem", "ambiente", "iluminacao",
            "ele", "ela", "eles", "elas", "dele", "dela", "deles", "delas", "se", "ao", "aos",
            "aquela", "aquele", "aquelas", "aqueles", "depois", "antes", "durante", "entao",
            "quando", "enquanto", "apos", "logo", "assim", "mesmo", "mesma", "same", "then",
            "agora", "ali", "aqui", "isso", "isto", "essa", "esse", "essas", "esses",
            "momento narrativo", "historia", "história", "cenario", "cenario visual",
        }
        seen: List[str] = []
        for alias, label in known_character_aliases.items():
            if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized_full) and label not in seen:
                seen.append(label)
        for token in tokens:
            normalized = self._fold_text_for_matching(token)
            parts = [part for part in normalized.split() if part]
            if not parts:
                continue
            if any(part in blacklist for part in parts):
                continue
            if len(parts) == 1 and parts[0] not in known_character_aliases and not re.search(r"\b(?:[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{2,})\b", token):
                continue
            if token not in seen:
                seen.append(token)
        return seen[:3]

    def _build_semantic_scene_profile(self, scene: Dict[str, Any], scene_number: int) -> Dict[str, Any]:
        raw_text = str(scene.get("text") or "").strip()
        clean_text = str(scene.get("_tts_text") or raw_text).strip()
        prompt_seed = self._clean_image_prompt_seed(str(scene.get("image_prompt") or scene.get("visual_prompt") or ""))
        semantic_prompt_seed = self._strip_visual_prompt_labels(prompt_seed)
        source_text = f"{clean_text or raw_text} {semantic_prompt_seed}".strip()

        environment_catalog = {
            "temple": ["templo", "santuario", "sanctuary", "altar"],
            "corridor": ["corredor", "hallway", "passagem", "passage"],
            "chamber": ["camara", "câmara", "chamber", "sala secreta", "secret chamber"],
            "desert": ["deserto", "desert"],
            "sea": ["mar", "oceano", "sea", "shore"],
            "city": ["cidade", "city", "street", "rua", "market", "mercado"],
            "home": ["casa", "home", "room", "quarto"],
            "mountain": ["montanha", "mountain", "hill", "colina"],
        }
        action_catalog = {
            "reading": ["ler", "lendo", "examina", "examinar", "pergaminho", "scroll", "study"],
            "watching": ["olha", "observa", "encara", "watching", "gazes"],
            "walking": ["caminha", "walking", "anda", "passos", "walks"],
            "running": ["corre", "running", "fug", "sprint", "rush"],
            "revealing": ["revela", "revelacao", "revelação", "descobre", "finds", "encontra", "mapa"],
            "crying": ["chora", "cry", "tears", "lagrimas", "lágrimas"],
            "praying": ["ora", "prayer", "rez", "prays"],
            "speaking": ["fala", "diz", "declara", "speaks", "asks"],
        }
        emotion_catalog = {
            "tension": ["tensao", "tensão", "suspense", "medo", "fear", "tense"],
            "hope": ["esperanca", "esperança", "hope", "promessa", "promise"],
            "grief": ["triste", "dor", "luto", "weeps", "cry", "sorrow"],
            "joy": ["alegria", "joy", "rejoice", "celebra"],
            "awe": ["gloria", "glória", "milagre", "awe", "wonder"],
        }
        time_catalog = {
            "night": ["noite", "night", "moon", "midnight"],
            "dawn": ["amanhecer", "sunrise", "dawn"],
            "day": ["dia", "daylight", "afternoon"],
            "sunset": ["entardecer", "sunset", "twilight"],
            "ancient": ["antigo", "ancient", "biblico", "biblical", "era"],
        }
        weather_catalog = {
            "storm": ["storm", "tempest", "rain", "chuva", "thunder"],
            "clear": ["clear", "sunny", "limpo", "calmo"],
            "fog": ["fog", "mist", "neblina"],
        }
        lighting_catalog = {
            "torchlight": ["tocha", "torch", "warm light", "warm torch"],
            "golden": ["golden", "dourad", "sunset glow"],
            "soft": ["soft light", "suave", "diffused"],
            "dark": ["escuro", "sombrio", "shadow", "dark"],
        }
        viewpoint_catalog = {
            "close_up": ["close", "close-up", "detalhe", "detail"],
            "wide": ["wide", "panoramic", "establishing", "amplo"],
            "pov": ["pov", "point of view", "first person"],
            "medium": ["medium shot", "waist", "half body"],
        }
        style_catalog = {
            "cinematic_realism": ["cinematic", "realism", "realistic", "film"],
            "documentary": ["documentary", "docu"],
            "epic": ["epic", "heroic", "grand"],
            "pastoral": ["pastoral", "gentle", "soft"],
        }

        profile = {
            "scene_number": int(scene_number),
            "moment": self._compact_narrative_moment(clean_text or raw_text),
            "characters": self._extract_character_tags(clean_text or raw_text),
   …41388 tokens truncated…_required = bool(
                    isinstance(plan, dict) and plan.get("approved_narration_required")
                )
                main_audio_path = (
                    self._prepare_approved_audio_for_visual_opening(
                        seed_audio_path,
                        initial_opening_silence_sec,
                    )
                    if approved_seed_required
                    else seed_audio_path
                )
                debug_ctx["audio_path"] = main_audio_path
                debug_ctx["tts_provider_configured"] = "seed_reuse"
                debug_ctx["tts_provider_used"] = "seed_reuse"
                debug_ctx["tts_fallback_used"] = False
                try:
                    if seed_audio_text:
                        # The approved script, not the editorial storyboard, is
                        # the literal text authority for captions and reports.
                        final_narration_text = seed_audio_text
                        planning_meta["full_text"] = seed_audio_text
                        render_report["audio_generation"]["final_text_sent_to_tts"] = seed_audio_text
                except Exception:
                    pass
                main_audio_clip = AudioFileClip(main_audio_path)
                self._assert_clip_not_none(main_audio_clip, "seed_audio_clip", {"path": main_audio_path})
                actual_total_audio_dur = float(self._ffprobe_duration_seconds(main_audio_path) or 0.0)
                if actual_total_audio_dur <= 0:
                    actual_total_audio_dur = float(getattr(main_audio_clip, "duration", 0) or 0.0)
                if actual_total_audio_dur <= 0:
                    raise Exception("Áudio reutilizado com duração inválida.")
                render_report["audio_generation"]["provider_used"] = "seed_reuse"
                render_report["audio_generation"]["fallback_used"] = False
                render_report["audio_generation"]["final_audio_duration_sec"] = round(actual_total_audio_dur, 2)
                render_report["audio_generation"]["output_path"] = main_audio_path
                render_report["audio_generation"]["seed_reused"] = True
                render_report["audio_generation"]["approved_source_path"] = seed_audio_path
                render_report["audio_generation"]["approved_source_unchanged"] = True
                render_report["audio_generation"]["opening_silence_applied_locally_sec"] = (
                    round(initial_opening_silence_sec, 2) if approved_seed_required else 0.0
                )
                max_ok = (max_requested_duration <= 0) or (actual_total_audio_dur <= (max_requested_duration * (1.0 + range_tolerance)))
                min_ok = (min_requested_duration <= 0) or (actual_total_audio_dur >= (min_requested_duration * (1.0 - range_tolerance)))
                duration_range_report["actual_audio_duration_sec"] = round(actual_total_audio_dur, 2)
                duration_range_report["above_requested_range_sec"] = round(max(0.0, actual_total_audio_dur - max_requested_duration) if max_requested_duration > 0 else 0.0, 2)
                duration_range_report["below_requested_range_sec"] = round(max(0.0, min_requested_duration - actual_total_audio_dur) if min_requested_duration > 0 else 0.0, 2)
                duration_range_report["within_requested_range"] = bool(max_ok and min_ok)
                duration_range_report["decision"] = "seed_audio_reuse"
                duration_range_report["decision_reason"] = "Áudio reutilizado a partir do seed_audio_path."
            for narration_attempt in range(0 if seed_audio_used else 4):
                segmented_audio = self._compose_segmented_narration_audio(
                    main_text=main_story_narration_text or final_narration_text,
                    cta_text=cta_narration_text,
                    voice_style=voice_style,
                    voice_gender=voice_gender,
                    pause_duration_sec=pause_before_cta_sec,
                    initial_silence_duration_sec=initial_opening_silence_sec,
                    status_callback=_tts_status,
                )
                main_audio_path = segmented_audio.get("audio_path")
                tts_debug = dict(self._last_tts_debug or {})
                tts_debug["final_text_sent_to_tts"] = final_narration_text
                tts_debug["attempt_number"] = narration_attempt + 1
                tts_debug["segmented_audio"] = {
                    "main_audio_path": segmented_audio.get("main_audio_path"),
                    "cta_audio_path": segmented_audio.get("cta_audio_path"),
                    "initial_silence_duration_sec": segmented_audio.get("initial_silence_duration_sec"),
                    "pause_duration_sec": segmented_audio.get("pause_duration_sec"),
                    "main_duration_sec": segmented_audio.get("main_duration_sec"),
                    "cta_duration_sec": segmented_audio.get("cta_duration_sec"),
                }
                render_report["audio_generation"] = tts_debug
                debug_ctx["audio_path"] = main_audio_path
                debug_ctx["title_audio_path"] = segmented_audio.get("main_audio_path")
                debug_ctx["end_audio_path"] = segmented_audio.get("cta_audio_path")
                debug_ctx["tts_provider_configured"] = tts_debug.get("configured_provider")
                debug_ctx["tts_provider_used"] = tts_debug.get("provider_used")
                debug_ctx["tts_fallback_used"] = tts_debug.get("fallback_used")
                if not main_audio_path or not os.path.exists(main_audio_path):
                    raise Exception(
                        "Falha ao gerar o audio final da narracao. "
                        + self._summarize_tts_failure(tts_debug)
                    )
                if main_audio_clip is not None:
                    try:
                        main_audio_clip.close()
                    except Exception:
                        pass
                main_audio_clip = AudioFileClip(main_audio_path)
                self._assert_clip_not_none(main_audio_clip, "main_narration_audio_clip", {"path": main_audio_path})
                actual_total_audio_dur = float(self._ffprobe_duration_seconds(main_audio_path) or 0.0)
                if actual_total_audio_dur <= 0:
                    actual_total_audio_dur = float(getattr(main_audio_clip, "duration", 0) or 0.0)
                if actual_total_audio_dur <= 0:
                    raise Exception("Audio final gerado com duracao invalida.")
                render_report["audio_generation"]["provider_used"] = (
                    render_report["audio_generation"].get("provider_used")
                    or tts_debug.get("provider_used")
                )
                render_report["audio_generation"]["fallback_used"] = bool(
                    render_report["audio_generation"].get("fallback_used")
                )
                render_report["audio_generation"]["final_audio_duration_sec"] = round(actual_total_audio_dur, 2)
                render_report["audio_generation"]["output_path"] = main_audio_path
                render_report["audio_generation"]["initial_opening_silence_sec"] = segmented_audio.get("initial_silence_duration_sec")
                render_report["audio_generation"]["pause_before_cta_sec"] = segmented_audio.get("pause_duration_sec")
                render_report["audio_generation"]["main_narration_duration_sec"] = segmented_audio.get("main_duration_sec")
                render_report["audio_generation"]["cta_duration_sec"] = segmented_audio.get("cta_duration_sec")

                max_ok = (max_requested_duration <= 0) or (actual_total_audio_dur <= (max_requested_duration * (1.0 + range_tolerance)))
                min_ok = (min_requested_duration <= 0) or (actual_total_audio_dur >= (min_requested_duration * (1.0 - range_tolerance)))
                above_requested_range_sec = max(0.0, actual_total_audio_dur - max_requested_duration) if max_requested_duration > 0 else 0.0
                below_requested_range_sec = max(0.0, min_requested_duration - actual_total_audio_dur) if min_requested_duration > 0 else 0.0
                duration_range_report["actual_audio_duration_sec"] = round(actual_total_audio_dur, 2)
                duration_range_report["above_requested_range_sec"] = round(above_requested_range_sec, 2)
                duration_range_report["below_requested_range_sec"] = round(below_requested_range_sec, 2)
                duration_range_report["within_requested_range"] = bool(max_ok and min_ok)
                if max_ok and min_ok:
                    duration_range_report["decision"] = "within_requested_range"
                    duration_range_report["decision_reason"] = "Narracao completa ficou dentro da faixa solicitada."
                    break
                if not max_ok and narration_attempt >= 3:
                    duration_range_report["kept_complete_narration"] = True
                    duration_range_report["decision"] = "keep_complete_narration_outside_range"
                    duration_range_report["decision_reason"] = "Duracao final excedeu a faixa de referencia, mas a narracao foi mantida completa para nao cortar o audio."
                    break
                if not max_ok:
                    duration_range_report["attempted_replanning_after_real_audio"] = True
                else:
                    duration_range_report["kept_complete_narration"] = True
                    duration_range_report["decision"] = "keep_complete_narration_below_range"
                    duration_range_report["decision_reason"] = "Duracao final ficou abaixo da faixa de referencia, mas a narracao foi mantida completa e a timeline segue o audio real."
                    break

                current_body_text = str(planning_meta.get("body_text") or "").strip()
                current_opening = str(planning_meta.get("opening_text") or "").strip()
                current_closing = str(planning_meta.get("closing_text") or "").strip()
                opening_duration_est = self._estimate_text_duration_with_voice(current_opening, voice_style=voice_style, voice_gender=voice_gender)
                closing_duration_est = self._estimate_text_duration_with_voice(current_closing, voice_style=voice_style, voice_gender=voice_gender)
                current_body_estimate = self._estimate_text_duration_with_voice(current_body_text, voice_style=voice_style, voice_gender=voice_gender)
                body_actual_estimate = max(1.0, actual_total_audio_dur - initial_opening_silence_sec - opening_duration_est - closing_duration_est)
                target_body_max_sec = max(8.0, (real_audio_target_max_sec or max_requested_duration or actual_total_audio_dur) - initial_opening_silence_sec - opening_duration_est - closing_duration_est)
                if max_requested_duration > 0 and actual_total_audio_dur > 0:
                    shrink_ratio = max(0.35, min(0.95, float(real_audio_target_max_sec or max_requested_duration) / max(1.0, actual_total_audio_dur)))
                    proportional_target = min(current_body_estimate, body_actual_estimate) * shrink_ratio
                    target_body_max_sec = max(8.0, min(target_body_max_sec, proportional_target))

                condensed = self._condense_body_text_to_fit(
                    current_body_text,
                    scenes,
                    target_max_sec=target_body_max_sec,
                    voice_style=voice_style,
                    voice_gender=voice_gender,
                    kind=plan.get("kind") if isinstance(plan, dict) else None,
                )
                new_body_text = self._normalize_tts_text(condensed.get("body_text") or "")
                if not new_body_text or new_body_text == current_body_text:
                    duration_range_report["kept_complete_narration"] = True
                    duration_range_report["decision"] = "keep_complete_narration_after_failed_replan"
                    duration_range_report["decision_reason"] = "Nao foi possivel resumir mais sem comprometer a narracao; o processo seguiu com o audio completo como referencia oficial."
                    break
                planning_meta["body_text"] = new_body_text
                scene_texts = condensed.get("scene_texts") or self._redistribute_body_text_to_scenes(new_body_text, scenes)
                planning_meta["scene_texts"] = scene_texts
                planning_meta["body_duration_est_sec"] = round(self._estimate_text_duration_with_voice(new_body_text, voice_style=voice_style, voice_gender=voice_gender), 2)
                planning_meta["word_count"] = self._count_words(" ".join([current_opening, new_body_text, current_closing]).strip())
                planning_meta["char_count"] = len(" ".join([current_opening, new_body_text, current_closing]).strip())
                planning_meta["planning_target_max_sec"] = round(real_audio_target_max_sec, 2) if real_audio_target_max_sec > 0 else 0.0
                planning_meta["estimated_total_duration_sec"] = round(
                    float(planning_meta.get("intro_opening_hold_sec") or 0.0)
                    + float(planning_meta.get("opening_duration_est_sec") or 0.0)
                    + float(planning_meta.get("body_duration_est_sec") or 0.0)
                    + float(planning_meta.get("closing_duration_est_sec") or 0.0)
                    + float(planning_meta.get("pause_duration_sec") or 0.0),
                    2,
                )
                planning_meta["full_text"] = " ".join([current_opening, new_body_text, current_closing]).strip()
                render_report["audio_generation"]["replanned_text_sent_to_tts"] = planning_meta["full_text"]
                planning_meta.setdefault("planning_attempts", []).append({
                    "attempt": len(planning_meta.get("planning_attempts") or []) + 1,
                    "body_word_count": self._count_words(new_body_text),
                    "estimated_total_duration_sec": planning_meta.get("estimated_total_duration_sec"),
                    "replanned_after_real_audio": True,
                    "actual_audio_duration_sec": round(actual_total_audio_dur, 2),
                    "target_body_max_sec": round(target_body_max_sec, 2),
                })
                for idx, scene in enumerate(scenes):
                    if idx < len(scene_texts):
                        scene["_tts_text"] = self._normalize_tts_text(scene_texts[idx] or scene.get("_tts_text") or scene.get("text") or "")
                        scene["_estimated_narration_sec"] = self._estimate_text_duration_with_voice(scene["_tts_text"], voice_style=voice_style, voice_gender=voice_gender)
                    if idx < len(render_report["narration_for_tts"]):
                        render_report["narration_for_tts"][idx]["clean_text"] = scene.get("_tts_text") or ""
                        render_report["narration_for_tts"][idx]["estimated_duration_sec"] = round(float(scene.get("_estimated_narration_sec") or 0.0), 2)
                planning_meta["scene_estimated_durations_sec"] = [round(float(scene.get("_estimated_narration_sec") or 0.0), 2) for scene in scenes]
                render_report["narration_plan"] = planning_meta
                final_narration_text = str(planning_meta.get("full_text") or "").strip()
                main_story_narration_text = " ".join(
                    part for part in [
                        str(planning_meta.get("opening_text") or "").strip(),
                        str(planning_meta.get("body_text") or "").strip(),
                    ]
                    if part
                ).strip()
                cta_narration_text = str(planning_meta.get("cta_text") or planning_meta.get("closing_text") or "").strip()

            estimated_total_duration = float(planning_meta.get("estimated_total_duration_sec") or 0.0)
            duration_range_report["estimated_full_narration_duration_sec"] = round(estimated_total_duration, 2)
            if duration_range_report["decision"] == "pending":
                duration_range_report["kept_complete_narration"] = bool(not duration_range_report["within_requested_range"])
                duration_range_report["decision"] = "keep_complete_narration_outside_range" if not duration_range_report["within_requested_range"] else "within_requested_range"
                duration_range_report["decision_reason"] = (
                    "Narracao final ficou fora da faixa de referencia, mas o processo manteve o audio completo como fonte oficial da timeline."
                    if not duration_range_report["within_requested_range"]
                    else "Narracao completa ficou dentro da faixa solicitada."
                )
            planning_meta["duration_range_report"] = duration_range_report
            render_report["narration_plan"] = planning_meta
            closing_has_narration = bool(str(planning_meta.get("closing_text") or "").strip())
            pause_before_cta_sec = float(planning_meta.get("pause_duration_sec") or pause_before_cta_sec or 1.25)
            initial_opening_silence_sec = float(planning_meta.get("intro_opening_hold_sec") or initial_opening_silence_sec or DEFAULT_OPENING_SILENCE_SEC)
            end_screen_target_duration_sec = float(planning_meta.get("end_screen_target_duration_sec") or 5.0)
            opening_est = float(planning_meta.get("opening_duration_est_sec") or 0.0)
            closing_est = float(planning_meta.get("closing_duration_est_sec") or 0.0)
            reflection_est = float(planning_meta.get("reflection_duration_est_sec") or 0.0)
            body_est = float(planning_meta.get("body_duration_est_sec") or 0.0)
            scale_ratio = (actual_total_audio_dur / estimated_total_duration) if estimated_total_duration > 0 else 1.0
            opening_voice_duration = round(max(0.0, opening_est * scale_ratio), 2)
            title_clip_duration = round(max(initial_opening_silence_sec, initial_opening_silence_sec + opening_voice_duration), 2) if opening_est > 0 else round(max(3.4, initial_opening_silence_sec), 2)
            cta_clip_duration = min(6.0, max(3.0, round(closing_est * scale_ratio, 2))) if closing_has_narration else 0.0
            end_clip_duration = min(6.0, max(3.0, round(end_screen_target_duration_sec, 2)))
            voice_closing_duration = cta_clip_duration if closing_has_narration else 0.0
            silent_cinematic_tail_sec = end_clip_duration
            target_video_duration = actual_total_audio_dur + silent_cinematic_tail_sec
            if (title_clip_duration + voice_closing_duration) >= actual_total_audio_dur:
                title_clip_duration = max(initial_opening_silence_sec, min(title_clip_duration, actual_total_audio_dur * 0.28))
                voice_closing_duration = max(0.0, min(voice_closing_duration, actual_total_audio_dur * 0.24))
            reflection_duration_sec = round(max(0.0, reflection_est * scale_ratio), 2) if reflection_est > 0 else 0.0
            body_audio_target = max(0.0, actual_total_audio_dur - title_clip_duration - voice_closing_duration - pause_before_cta_sec)

            approved_word_timeline = (
                list(plan.get("approved_caption_timeline") or [])
                if isinstance(plan, dict) and isinstance(plan.get("approved_caption_timeline"), list)
                else []
            )
            caption_timeline_details: Dict[str, Any]
            if approved_word_timeline:
                voice_duration = max(
                    0.1,
                    actual_total_audio_dur - max(0.0, initial_opening_silence_sec),
                )
                approved_timed = self._caption_timeline_from_segments(
                    [{"words": approved_word_timeline}],
                    voice_duration,
                    narration=final_narration_text,
                )
                shifted_approved_timed = []
                for raw_item in approved_timed or []:
                    item = dict(raw_item)
                    start = float(item.get("start") or 0.0) + initial_opening_silence_sec
                    end = float(item.get("end") or 0.0) + initial_opening_silence_sec
                    item["start"] = round(min(actual_total_audio_dur, start), 3)
                    item["end"] = round(min(actual_total_audio_dur, max(start, end)), 3)
                    item["source"] = "approved_edge_tts_word_boundaries"
                    item["text_source"] = "approved_narration"
                    shifted_approved_timed.append(item)
                if shifted_approved_timed:
                    # Approved Edge-TTS boundaries may contain encoder padding.
                    # Normalize once, while keeping the last spoken word end.
                    shifted_approved_timed = self._sanitize_caption_timeline(
                        shifted_approved_timed,
                        actual_total_audio_dur,
                    )
                    caption_timeline_details = {
                        "timeline": shifted_approved_timed,
                        "source": "approved_edge_tts_word_boundaries",
                        "timing_source": str(
                            (plan or {}).get("approved_caption_timing_source")
                            or "edge_tts_word_boundaries"
                        ),
                    }
                else:
                    caption_timeline_details = self._build_caption_timeline_details(
                        final_narration_text,
                        actual_total_audio_dur,
                        audio_path=main_audio_path,
                    )
            else:
                caption_timeline_details = self._build_caption_timeline_details(
                    final_narration_text,
                    actual_total_audio_dur,
                    audio_path=main_audio_path,
                )
            full_caption_timeline = caption_timeline_details.get("timeline") or []
            caption_timeline_source = str(caption_timeline_details.get("source") or "text_fallback")
            if caption_timeline_source == "text_fallback" and initial_opening_silence_sec > 0 and final_narration_text:
                shifted_timeline = self._caption_timeline_from_text(
                    final_narration_text,
                    max(0.1, actual_total_audio_dur - initial_opening_silence_sec),
                )
                adjusted_timeline = []
                for item in shifted_timeline:
                    try:
                        start = float(item.get("start") or 0.0) + initial_opening_silence_sec
                        end = float(item.get("end") or 0.0) + initial_opening_silence_sec
                    except Exception:
                        continue
                    adjusted = dict(item)
                    adjusted["start"] = round(min(actual_total_audio_dur, start), 3)
                    adjusted["end"] = round(min(actual_total_audio_dur, max(start, end)), 3)
                    adjusted_timeline.append(adjusted)
                if adjusted_timeline:
                    full_caption_timeline = adjusted_timeline
                    caption_timeline_source = "text_fallback_shifted_by_opening_silence"
            if not full_caption_timeline:
                raise Exception("Falha ao gerar a timeline de legendas a partir do audio final.")
            caption_text_joined = " ".join(
                str(item.get("caption") or "").strip()
                for item in full_caption_timeline
                if str(item.get("caption") or "").strip()
            ).strip()
            normalized_tts_text = self._normalize_tts_text(final_narration_text)
            normalized_caption_text = self._normalize_tts_text(caption_text_joined)
            render_report["utf8_audit"] = {
                "final_text_sent_to_tts_unicode_escape": normalized_tts_text.encode("unicode_escape").decode("ascii"),
                "captions_source_text_unicode_escape": normalized_caption_text.encode("unicode_escape").decode("ascii"),
                "texts_identical_after_whitespace_normalization": normalized_caption_text == normalized_tts_text,
                "tts_text_length": len(normalized_tts_text),
                "captions_text_length": len(normalized_caption_text),
            }
            render_report["text_integrity"] = {
                "final_text_sent_to_tts": final_narration_text,
                "captions_source_text": caption_text_joined,
                "tts_contains_non_ascii": bool(re.search(r"[^\x00-\x7F]", final_narration_text)),
                "captions_contain_non_ascii": bool(re.search(r"[^\x00-\x7F]", caption_text_joined)),
                "tts_contains_punctuation": bool(re.search(r"[,.!?;:…\"“”'‘’\-–—]", final_narration_text)),
                "captions_contain_punctuation": bool(re.search(r"[,.!?;:…\"“”'‘’\-–—]", caption_text_joined)),
                "captions_match_narration_source": normalized_caption_text == normalized_tts_text,
            }
            if normalized_caption_text != normalized_tts_text:
                raise Exception("Falha de validacao: legenda-base difere do texto enviado ao TTS.")

            requested_duration = actual_total_audio_dur
            duration_plan = self._plan_scene_visual_durations(
                scenes,
                requested_duration,
                title_duration=title_clip_duration,
                end_duration=voice_closing_duration + pause_before_cta_sec,
                scene_decisions=visual_group_plan.get("scene_decisions") or [],
                transition_duration=0.0,
            )
            planned_scene_durations = duration_plan.get("allocated_scene_durations") or [float(scene.get("_estimated_narration_sec") or 5.0) for scene in scenes]
            render_report["duration_plan"] = duration_plan
            render_report["duration_plan"]["requested_duration_min_sec"] = round(min_requested_duration, 2)
            render_report["duration_plan"]["requested_duration_max_sec"] = round(max_requested_duration, 2)
            render_report["duration_plan"]["requested_duration_target_sec"] = round(target_requested_duration, 2)
            render_report["duration_plan"]["requested_duration_is_reference_only"] = True
            render_report["duration_plan"]["planned_total_audio_duration_sec"] = round(estimated_total_duration, 2)
            render_report["duration_plan"]["actual_audio_duration_sec"] = round(actual_total_audio_dur, 2)
            render_report["duration_plan"]["opening_duration_sec"] = round(title_clip_duration, 2)
            render_report["duration_plan"]["reflection_duration_sec"] = reflection_duration_sec
            render_report["duration_plan"]["pause_before_cta_sec"] = round(pause_before_cta_sec, 2)
            render_report["duration_plan"]["cta_duration_sec"] = round(voice_closing_duration, 2)
            render_report["duration_plan"]["end_duration_sec"] = round(end_clip_duration, 2)
            render_report["duration_plan"]["voice_closing_duration_sec"] = round(voice_closing_duration, 2)
            render_report["duration_plan"]["cinematic_end_pause_sec"] = round(pause_before_cta_sec, 2)
            render_report["duration_plan"]["cinematic_closing_tail_sec"] = round(silent_cinematic_tail_sec, 2)
            render_report["duration_plan"]["target_video_duration_sec"] = round(target_video_duration, 2)
            render_report["duration_plan"]["body_audio_duration_sec"] = round(body_audio_target, 2)
            render_report["requested_duration_sec"] = round(target_requested_duration or max_requested_duration or min_requested_duration or 0.0, 2)
            render_report["estimated_script_duration_sec"] = round(estimated_total_duration, 2)
            render_report["narration_duration_sec"] = round(actual_total_audio_dur, 2)
            render_report["intro_duration_sec"] = round(title_clip_duration, 2)
            render_report["reflection_duration_sec"] = reflection_duration_sec
            render_report["cta_duration_sec"] = round(voice_closing_duration, 2)
            render_report["end_screen_duration_sec"] = round(end_clip_duration, 2)
            render_report["narration_completed"] = False
            render_report["story_completed"] = False
            render_report["cta_rendered"] = False
            render_report["end_screen_rendered"] = False
            render_report["plain_background_detected_at_end"] = False
            render_report["unexpected_extra_video_created"] = False
            render_report["visual_plan"]["caption_max_lines"] = 2
            render_report["visual_plan"]["caption_reserved_bottom_ratio"] = CAPTION_SAFE_AREA_BOTTOM_RATIO
            render_report["visual_plan"]["caption_vertical_anchor"] = "bottom"
            render_report["visual_plan"]["safe_area_left_right_ratio"] = CAPTION_SAFE_AREA_X_RATIO
            render_report["visual_plan"]["safe_area_top_ratio"] = CAPTION_SAFE_AREA_TOP_RATIO
            render_report["visual_plan"]["safe_area_bottom_ratio"] = CAPTION_SAFE_AREA_BOTTOM_RATIO
            render_report["duration_plan"]["initial_opening_silence_sec"] = round(initial_opening_silence_sec, 2)
            render_report["duration_plan"]["opening_voice_duration_sec"] = round(opening_voice_duration, 2)
            render_report["duration_plan"]["scene_audio_margin_sec"] = round(DEFAULT_SCENE_AUDIO_MARGIN_SEC, 2)
            render_report["duration_plan"]["range_decision"] = duration_range_report.get("decision")
            render_report["duration_plan"]["range_decision_reason"] = duration_range_report.get("decision_reason")
            render_report["duration_plan"]["above_requested_range_sec"] = duration_range_report.get("above_requested_range_sec")
            render_report["duration_plan"]["below_requested_range_sec"] = duration_range_report.get("below_requested_range_sec")

            def _opening_status(message: str):
                if progress_callback:
                    progress_callback(9, f"Abertura: {message}")

            opening_visual = self._resolve_opening_background_image(
                title,
                scenes,
                continuity_anchor,
                plan=plan if isinstance(plan, dict) else None,
                selected_primary_path=selected_primary_path,
                cover_image_path=cover_image_path,
                video_bg_path=video_bg_path,
                aspect_ratio=aspect_ratio,
                image_max_rounds=image_max_rounds,
                allow_non_ai_fallback=allow_non_ai_fallback,
                status_callback=_opening_status,
                paid_call_guard=paid_image_call_guard,
                generated_group_paths=generated_group_paths,
                generated_group_sources=generated_group_sources,
                scene_to_group=scene_to_group,
            )
            start_bg_path = opening_visual.get("path") if isinstance(opening_visual, dict) else None
            if branding_profile.get("opening_image_path"):
                start_bg_path = branding_profile.get("opening_image_path")
            _track_image_path(start_bg_path)
            title_footer = f"Canal {planning_meta.get('channel_name')}" if planning_meta.get("channel_name") else None
            img_title = self.create_text_image("", size=video_size, bg_color=(20, 20, 20), bg_image_path=start_bg_path, footer_text=None)
            clip_title = ImageClip(img_title)
            self._assert_clip_not_none(clip_title, "title_slide")
            opening_visual_duration = max(2.0, round(float(title_clip_duration or 0.0), 2))
            title_clip_duration = opening_visual_duration
            clip_title = self._set_clip_duration(clip_title, opening_visual_duration)
            clip_title = self._apply_motion_effect(
                clip_title,
                video_size,
                {"name": "slow_zoom", "zoom_factor": 1.06, "scene_number": 0, "total_scenes": max(1, len(scenes))},
            )
            clip_title = self._apply_soft_fade(
                clip_title,
                fade_in_sec=min(0.60, opening_visual_duration * 0.20),
                fade_out_sec=min(0.35, opening_visual_duration * 0.14),
            )
            opening_overlays = []
            opening_logo = self._build_logo_overlay(
                str(branding_profile.get("logo_path") or "").strip(),
                video_size,
                duration=min(opening_visual_duration, 2.8),
                position="top_center",
                opacity=0.86,
                width_ratio=0.14,
            )
            if opening_logo is not None:
                opening_logo = self._apply_soft_fade(
                    opening_logo,
                    fade_in_sec=min(0.55, opening_visual_duration * 0.18),
                    fade_out_sec=min(0.35, opening_visual_duration * 0.12),
                )
                opening_overlays.append(opening_logo)
            title_overlay_duration = max(2.0, min(opening_visual_duration, 2.6))
            title_overlay = self._build_opening_title_overlay(
                clean_title,
                video_size,
                footer_text=title_footer,
                duration=title_overlay_duration,
            )
            if title_overlay is not None:
                opening_overlays.append(title_overlay)
            # CODEXIA_AUDIO_TIMED_GLOBAL_CAPTIONS_V1
            # Captions are composited once after all scenes are concatenated.
            # Never attach a second local copy to the opening clip.
            opening_caption_overlays = []
            render_report["visual_plan"]["opening_caption_suppressed"] = True
            render_report["visual_plan"]["opening_caption_blocks"] = 0
            render_report["visual_plan"]["caption_render_mode"] = "global_audio_timeline"
            render_report["visual_plan"]["opening_background_source"] = (
                opening_visual.get("source") if isinstance(opening_visual, dict) else "fallback_background"
            )
            render_report["visual_plan"]["opening_background_generated"] = bool(
                isinstance(opening_visual, dict) and opening_visual.get("generated")
            )
            render_report["visual_plan"]["opening_background_generation_attempted"] = bool(
                isinstance(opening_visual, dict) and opening_visual.get("generation_attempted")
            )
            render_report["visual_plan"]["opening_background_generation_error"] = (
                opening_visual.get("generation_error") if isinstance(opening_visual, dict) else None
            )
            render_report["visual_plan"]["opening_background_fallback_reason"] = (
                opening_visual.get("fallback_reason") if isinstance(opening_visual, dict) else None
            )
            render_report["visual_plan"]["opening_visual_duration_sec"] = round(opening_visual_duration, 2)
            render_report["visual_plan"]["opening_title_overlay_duration_sec"] = round(title_overlay_duration, 2)
            render_report["visual_plan"]["opening_title_animation"] = "fade_plus_slow_zoom"
            render_report["visual_plan"]["opening_logo_present"] = bool(opening_logo is not None)
            clip_title = CompositeVideoClip([clip_title] + opening_overlays, size=video_size) if opening_overlays else clip_title
            clips.append(clip_title)

            total_scenes = len(scenes)
            cinematic_visual_hold_sec = self._memory_safe_visual_hold_seconds(actual_total_audio_dur)
            render_report["resource_profile"] = {
                "long_video_memory_mode": bool(actual_total_audio_dur >= 5 * 60),
                "visual_hold_target_sec": round(cinematic_visual_hold_sec, 3),
                "caption_overlays_cropped": True,
            }
            debug_ctx["scene_count"] = int(total_scenes)
            min_scene_visual_duration = 2.2 if total_scenes <= 2 else 2.8
            render_report["duration_plan"]["min_scene_visual_duration_sec"] = round(min_scene_visual_duration, 2)
            final_scene_durations = [
                max(
                    float(planned_scene_durations[i]) if i < len(planned_scene_durations) else float(scene.get("_estimated_narration_sec") or 5.0),
                    min_scene_visual_duration,
                )
                for i, scene in enumerate(scenes)
            ]
            legacy_scene_windows = []
            legacy_scene_cursor = float(title_clip_duration or 0.0)
            for scene_dur in final_scene_durations:
                scene_dur = float(scene_dur or 0.0)
                legacy_scene_windows.append({
                    "start": round(legacy_scene_cursor, 3),
                    "end": round(legacy_scene_cursor + scene_dur, 3),
                })
                legacy_scene_cursor += scene_dur
            scene_caption_sync = self._build_scene_caption_sync_map(
                full_caption_timeline,
                scenes,
                planning_meta,
                legacy_scene_windows=legacy_scene_windows,
                title_duration=title_clip_duration,
                end_duration=voice_closing_duration + pause_before_cta_sec,
                actual_total_audio_dur=actual_total_audio_dur,
                timeline_source=caption_timeline_source,
            )
            render_report["sync_validation"]["caption_timeline_source"] = caption_timeline_source
            render_report["sync_validation"]["caption_block_sync"] = scene_caption_sync.get("block_sync_report") or {}
            official_scene_timeline = self._build_official_scene_timeline(
                scenes=scenes,
                scene_caption_sync=scene_caption_sync,
                planned_scene_durations=planned_scene_durations,
                opening_text=str(planning_meta.get("opening_text") or "").strip(),
                opening_image=start_bg_path or "",
                title_duration=title_clip_duration,
                initial_opening_silence_sec=initial_opening_silence_sec,
                cta_text=cta_narration_text,
                closing_image="",
                pause_before_cta_sec=pause_before_cta_sec,
                cta_duration=voice_closing_duration,
                end_duration=end_clip_duration,
                timeline_source=caption_timeline_source,
                transition_name="fade",
            )
            render_report["scene_timeline"] = official_scene_timeline
            opening_timeline_entry = next((item for item in official_scene_timeline if str(item.get("kind") or "") == "opening"), None)
            closing_timeline_entry = next((item for item in official_scene_timeline if str(item.get("kind") or "") == "closing"), None)
            endcard_timeline_entry = next((item for item in official_scene_timeline if str(item.get("kind") or "") == "endcard"), None)
            if isinstance(opening_timeline_entry, dict):
                title_clip_duration = max(0.0, float(opening_timeline_entry.get("scene_end") or 0.0) - float(opening_timeline_entry.get("scene_start") or 0.0))
            if isinstance(closing_timeline_entry, dict):
                pause_before_cta_sec = max(0.0, float(closing_timeline_entry.get("audio_start") or 0.0) - float(closing_timeline_entry.get("scene_start") or 0.0))
                voice_closing_duration = max(0.0, float(closing_timeline_entry.get("audio_end") or 0.0) - float(closing_timeline_entry.get("audio_start") or 0.0))
            if isinstance(endcard_timeline_entry, dict):
                end_clip_duration = max(0.0, float(endcard_timeline_entry.get("scene_end") or 0.0) - float(endcard_timeline_entry.get("scene_start") or 0.0))
            story_timeline_entries = [item for item in official_scene_timeline if str(item.get("kind") or "") == "story"]
            render_report["timeline_report"] = {
                "official_scene_timeline_enabled": True,
                "scene_count": len(official_scene_timeline),
                "opening_present": bool(opening_timeline_entry),
                "closing_present": bool(closing_timeline_entry),
                "endcard_present": bool(endcard_timeline_entry),
                "timeline_source": caption_timeline_source,
                "image_lead_sec": round(DEFAULT_SCENE_IMAGE_LEAD_SEC, 2),
                "caption_lead_sec": round(DEFAULT_SCENE_CAPTION_LEAD_SEC, 2),
                "scene_audio_margin_sec": round(DEFAULT_SCENE_AUDIO_MARGIN_SEC, 2),
            }
            last_story_scene_clip = None
            last_story_scene_image_path = None

            for i, scene in enumerate(scenes):
                debug_ctx["stage"] = "scene_loop"
                debug_ctx["scene_index"] = int(i)
                scene_progress = 10 + int((i / max(1, total_scenes)) * 70)
                if progress_callback:
                    progress_callback(scene_progress, f"Processando cena {i+1} de {total_scenes}...")

                if isinstance(scene, str):
                    text = scene
                    image_prompt = f"Photorealistic cinematic photography representing: {text[:100]}"
                else:
                    text = scene.get('text', '')
                    image_prompt = scene.get('image_prompt', '')
                    if not image_prompt and text:
                        image_prompt = f"Photorealistic cinematic photography representing: {text[:100]}"

                clean_text = (scene.get("_tts_text") if isinstance(scene, dict) else "") or self._normalize_tts_text(text)

                def _scene_status(message, scene_idx=i, total=total_scenes, pct=scene_progress):
                    if progress_callback:
                        progress_callback(pct, f"Cena {scene_idx+1}/{total}: {message}")

                bg_image_path = None
                prompt_key = None
                reused_from_pool = False
                visual_source = "generated_group"
                visual_group_id = scene_to_group.get(i, i)
                visual_group = group_lookup.get(visual_group_id, {})
                scene_decision = scene_decision_lookup.get(i, {})
                selected_image_index = None
                if selected_image_paths:
                    bg_image_path = self._selected_image_for_visual_group(
                        selected_image_paths,
                        visual_group_id,
                    )
                    try:
                        selected_image_index = selected_image_paths.index(bg_image_path)
                    except Exception:
                        selected_image_index = None
                    visual_source = "selected_image"
                elif use_single_bg and video_bg_paths:
                    try:
                        import random
                        bg_image_path = random.choice(video_bg_paths)
                    except Exception:
                        bg_image_path = video_bg_paths[0]
                    visual_source = "single_bg_pool"
                else:
                    if visual_group_id in generated_group_paths and os.path.exists(generated_group_paths[visual_group_id]):
                        bg_image_path = generated_group_paths[visual_group_id]
                        reused_from_pool = True
                        visual_source = generated_group_sources.get(visual_group_id) or "reused_group_image"
                    else:
                        group_prompt = str(visual_group.get("prompt") or image_prompt or "").strip()
                        prompt_key = (
                            str(aspect_ratio).strip(),
                            group_prompt.lower() or clean_text[:220].strip().lower(),
                        )
                        cached = image_cache.get(prompt_key)
                        if cached and os.path.exists(cached):
                            bg_image_path = cached
                            visual_source = "cached_group_image"
                        else:
                            bg_image_path = self._recovery_image_from_pool(
                                recovery_image_budget,
                                scene_image_pool,
                                visual_group_id,
                            )
                            if bg_image_path:
                                reused_from_pool = True
                                visual_source = "recovery_budget_reused_pool"
                                _scene_status(
                                    "Limite de imagens atingido; reutilizando ativo válido sem nova cobrança..."
                                )
                            else:
                                try:
                                    bg_image_path = self._ensure_image_for_scene(
                                        group_prompt or image_prompt,
                                        text_fallback=clean_text,
                                        aspect_ratio=aspect_ratio,
                                        status_callback=_scene_status,
                                        max_rounds=image_max_rounds,
                                        allow_non_ai_fallback=allow_non_ai_fallback,
                                        paid_call_guard=paid_image_call_guard,
                                    )
                                    visual_source = "generated_group"
                                except RecoveryImageBudgetExceeded:
                                    # Há uma corrida benigna possível entre a
                                    # pré-checagem e o guard. Repita somente a
                                    # seleção local; nunca a chamada externa.
                                    bg_image_path = self._recovery_image_from_pool(
                                        recovery_image_budget,
                                        scene_image_pool,
                                        visual_group_id,
                                    )
                                    if not bg_image_path:
                                        raise
                                    reused_from_pool = True
                                    visual_source = "recovery_budget_reused_pool"
                                    _scene_status(
                                        "Limite de imagens atingido; reutilizando ativo válido sem nova cobrança..."
                                    )
                            if bg_image_path:
                                generated_group_paths[visual_group_id] = bg_image_path
                                generated_group_sources[visual_group_id] = visual_source
                        if (not bg_image_path) and allow_image_reuse and scene_image_pool:
                            bg_image_path = scene_image_pool[i % len(scene_image_pool)]
                            reused_from_pool = True
                            visual_source = "reused_pool_image"
                            _scene_status("Reutilizando imagem valida com variacao de movimento para manter o video completo...")

                if not bg_image_path:
                    raise Exception(f"A imagem da cena {i+1} não foi gerada nem pôde ser reaproveitada.")
                try:
                    if prompt_key and not (use_single_bg and video_bg_path):
                        image_cache[prompt_key] = bg_image_path
                except Exception:
                    pass
                try:
                    if bg_image_path not in scene_image_seen:
                        scene_image_pool.append(bg_image_path)
                        scene_image_seen.add(bg_image_path)
                except Exception:
                    pass
                _track_image_path(bg_image_path)
                debug_ctx["bg_image_path"] = bg_image_path

                bg_colors = [(24, 24, 24), (30, 30, 30), (36, 36, 36), (42, 42, 42)]
                bg_color = bg_colors[i % len(bg_colors)]
                if use_single_bg and video_bg_frame is not None:
                    bg_frame = video_bg_frame
                else:
                    bg_frame = self.create_text_image("", size=video_size, bg_color=bg_color, bg_image_path=bg_image_path)

                scene_timeline_entry = story_timeline_entries[i] if i < len(story_timeline_entries) else {}
                planned_scene_duration = max(0.0, float(scene_timeline_entry.get("audio_end") or 0.0) - float(scene_timeline_entry.get("audio_start") or 0.0))
                required_caption_duration = max(0.0, float(scene_timeline_entry.get("caption_end") or 0.0) - float(scene_timeline_entry.get("caption_start") or 0.0))
                scene_duration_info = self._resolve_scene_visual_duration(
                    scene_timeline_entry,
                    min_scene_visual_duration,
                )
                timeline_scene_duration = float(scene_duration_info["timeline_span"])
                timeline_is_audio_anchored = bool(scene_duration_info["audio_anchored"])
                scene_dur = float(scene_duration_info["duration"])
                reuse_count = int(scene_reuse_counts.get(bg_image_path, 0))
                scene_reuse_counts[bg_image_path] = reuse_count + 1
                if i < len(story_timeline_entries):
                    story_timeline_entries[i]["image"] = bg_image_path

                # CODEXIA_AUDIO_TIMED_GLOBAL_CAPTIONS_V1
                # Keep caption_blocks in the official timeline for diagnostics,
                # but do not render a local copy. Local scene coordinates drift
                # whenever visual pacing stretches or reuses a scene.
                scene_caption_timeline = list(scene_timeline_entry.get("caption_blocks") or [])
                expanded_scene_timeline: List[Dict[str, Any]] = []
                visual_beats = self._plan_cinematic_visual_beats(
                    scene_dur,
                    max_hold_sec=cinematic_visual_hold_sec,
                )
                if not visual_beats:
                    visual_beats = [{"index": 0.0, "start": 0.0, "end": scene_dur, "duration": scene_dur}]
                beat_effect_names: List[str] = []
                for beat_number, beat in enumerate(visual_beats):
                    beat_start = float(beat.get("start") or 0.0)
                    beat_end = float(beat.get("end") or scene_dur)
                    beat_duration = max(0.1, float(beat.get("duration") or (beat_end - beat_start)))
                    beat_bg_clip = ImageClip(bg_frame)
                    self._assert_clip_not_none(
                        beat_bg_clip,
                        "scene_bg_clip",
                        {"scene_index": i, "visual_beat": beat_number + 1},
                    )
                    beat_bg_clip = self._set_clip_duration(beat_bg_clip, beat_duration)
                    motion_plan = self._motion_plan_for_scene(
                        (i * 8) + beat_number,
                        max(total_scenes, total_scenes * 2),
                        reuse_count=reuse_count + beat_number,
                        reused_visual=bool(
                            reused_from_pool
                            or scene_reuse_counts.get(bg_image_path, 0) > 1
                            or beat_number > 0
                        ),
                    )
                    if beat_number == 0:
                        motion_plan = self._motion_plan_override_from_scene(
                            scene if isinstance(scene, dict) else {},
                            motion_plan,
                        )
                    beat_bg_clip = self._apply_motion_effect(beat_bg_clip, video_size, motion_plan)
                    beat_effect_names.append(str(motion_plan.get("name") or "slow_zoom"))
                    render_report["effects_applied"].append({
                        "scene_number": i + 1,
                        "visual_beat": beat_number + 1,
                        "visual_beat_count": len(visual_beats),
                        "image_group_id": visual_group_id + 1,
                        "effect": motion_plan.get("name"),
                        "zoom_factor": motion_plan.get("zoom_factor"),
                        "requested_by_scene": bool(motion_plan.get("requested_by_scene")),
                        "transition": "soft_cut" if beat_number > 0 or total_scenes > 1 else "none",
                    })

                    beat_overlays = []
                    for item in expanded_scene_timeline:
                        caption = str(item.get("caption") or "").strip()
                        start = float(item.get("start") or 0.0)
                        end = float(item.get("end") or 0.0)
                        overlap_start = max(start, beat_start)
                        overlap_end = min(end, beat_end)
                        if not caption or overlap_end <= overlap_start:
                            continue
                        overlay_arr = self.create_text_overlay(
                            caption,
                            size=video_size,
                            text_color=(255, 255, 255),
                            reserved_bottom_ratio=CAPTION_SAFE_AREA_BOTTOM_RATIO,
                        )
                        overlay_clip = self._clip_from_rgba(
                            overlay_arr,
                            overlap_end - overlap_start,
                            crop_transparent=True,
                        )
                        overlay_clip = self._set_clip_start(overlay_clip, overlap_start - beat_start)
                        beat_overlays.append(overlay_clip)

                    for overlay_clip in beat_overlays:
                        self._assert_clip_not_none(
                            overlay_clip,
                            "scene_overlay_clip",
                            {"scene_index": i, "visual_beat": beat_number + 1},
                        )
                    clip_scene = CompositeVideoClip([beat_bg_clip] + beat_overlays, size=video_size)
                    self._assert_clip_not_none(
                        clip_scene,
                        "scene_composite_clip",
                        {"scene_index": i, "visual_beat": beat_number + 1},
                    )
                    clip_scene = self._apply_scene_transition_style(
                        clip_scene,
                        transition_sec=DEFAULT_SCENE_TRANSITION_SEC,
                    )
                    clips.append(clip_scene)
                    last_story_scene_clip = clip_scene

                render_report["scene_visuals"].append({
                    "scene_number": i + 1,
                    "image_group_id": visual_group_id + 1,
                    "reused": bool(reused_from_pool or scene_reuse_counts.get(bg_image_path, 0) > 1),
                    "source": visual_source,
                    "selected_image_index": selected_image_index,
                    "decision": scene_decision.get("decision"),
                    "justification": scene_decision.get("justification"),
                    "image_path": bg_image_path,
                    "prompt": str((visual_group.get("prompt") or image_prompt or "")).strip()[:600],
                    "camera_movement": str((scene or {}).get("camera_movement") or (scene or {}).get("motion_effect") or "").strip() if isinstance(scene, dict) else "",
                    "dominant_emotion": str((((scene or {}).get("scene_card") or {}).get("dominant_emotion") or "").strip()) if isinstance(scene, dict) else "",
                    "scene_qc_status": str((scene or {}).get("scene_qc_status") or "").strip() if isinstance(scene, dict) else "",
                    "scene_qc": (scene or {}).get("scene_qc") if isinstance(scene, dict) and isinstance((scene or {}).get("scene_qc"), dict) else {},
                    "clean_narration": clean_text,
                    "scene_start_sec": scene_timeline_entry.get("scene_start"),
                    "scene_end_sec": scene_timeline_entry.get("scene_end"),
                    "audio_start_sec": scene_timeline_entry.get("audio_start"),
                    "audio_end_sec": scene_timeline_entry.get("audio_end"),
                    "caption_start_sec": scene_timeline_entry.get("caption_start"),
                    "caption_end_sec": scene_timeline_entry.get("caption_end"),
                    "audio_duration_sec": round(planned_scene_duration, 2),
                    "required_caption_duration_sec": round(required_caption_duration, 2),
                    "scene_audio_margin_sec": 0.0 if timeline_is_audio_anchored else round(DEFAULT_SCENE_AUDIO_MARGIN_SEC, 2),
                    "timeline_is_audio_anchored": timeline_is_audio_anchored,
                    "planned_visual_duration_sec": round(timeline_scene_duration, 2),
                    "final_visual_duration_sec": round(scene_dur, 2),
                    "visual_beat_count": len(visual_beats),
                    "max_visual_hold_sec": round(max(float(beat.get("duration") or 0.0) for beat in visual_beats), 2),
                    "visual_beat_effects": beat_effect_names,
                })
                last_story_scene_image_path = bg_image_path

                if bg_image_path and "temp_" in bg_image_path and bg_image_path not in cached_temp_paths:
                    try:
                        os.remove(bg_image_path)
                    except Exception:
                        pass
                gc.collect()

            if progress_callback:
                progress_callback(85, "Criando slide final...")

            if last_story_scene_clip is not None and pause_before_cta_sec > 0:
                pause_clip = self._freeze_last_frame_clip(last_story_scene_clip, pause_before_cta_sec)
                if pause_clip is not None:
                    pause_clip = self._apply_soft_fade(
                        pause_clip,
                        fade_in_sec=min(0.12, pause_before_cta_sec * 0.3),
                        fade_out_sec=min(0.18, pause_before_cta_sec * 0.6),
                    )
                    clips.append(pause_clip)

            closing_background = self._resolve_closing_background_image(
                branding_profile,
                opening_visual=opening_visual if isinstance(opening_visual, dict) else None,
                last_scene_image_path=last_story_scene_image_path,
                cover_image_path=cover_image_path,
                selected_primary_path=selected_primary_path,
                video_bg_path=video_bg_path,
            )
            end_bg_path = closing_background.get("path")
            if not end_bg_path:
                generated_end_bg = self._generate_fallback_background(video_size)
                if generated_end_bg and os.path.exists(generated_end_bg):
                    closing_background = {"path": generated_end_bg, "source": "generated_fallback_background"}
                    end_bg_path = generated_end_bg
            _track_image_path(end_bg_path)
            for item in official_scene_timeline:
                kind = str(item.get("kind") or "")
                if kind in {"closing", "endcard"}:
                    item["image"] = end_bg_path or ""
            if closing_has_narration:
                img_cta = self.create_text_image(
                    "",
                    size=video_size,
                    bg_color=(18, 18, 18),
                    bg_image_path=end_bg_path,
                )
                clip_cta = ImageClip(img_cta)
                self._assert_clip_not_none(clip_cta, "cta_slide")
                clip_cta = self._set_clip_duration(clip_cta, voice_closing_duration)
                clip_cta = self._apply_motion_effect(
                    clip_cta,
                    video_size,
                    {"name": "slow_zoom", "zoom_factor": 1.04, "scene_number": total_scenes + 1, "total_scenes": max(1, total_scenes + 2)},
                )
                clip_cta = self._apply_soft_fade(
                    clip_cta,
                    fade_in_sec=min(0.35, voice_closing_duration * 0.14),
                    fade_out_sec=min(0.35, voice_closing_duration * 0.18),
                )
                closing_audio_start = float((closing_timeline_entry or {}).get("audio_start") or 0.0)
                closing_audio_end = float((closing_timeline_entry or {}).get("audio_end") or closing_audio_start)
                # CODEXIA_AUDIO_TIMED_GLOBAL_CAPTIONS_V1
                # The CTA is covered by the same global audio timeline as every
                # other word; a local CTA copy would create duplicate subtitles.
                closing_caption_overlays = []
                clips.append(clip_cta)
                render_report["cta_rendered"] = True
                render_report["visual_plan"]["cta_visual_mode"] = "cinematic_background_bridge"
                render_report["visual_plan"]["closing_caption_blocks"] = 0

            img_end = self._build_cinematic_endcard_frame(
                branding_profile,
                background_path=end_bg_path,
                size=video_size,
            )
            clip_end = ImageClip(img_end)
            self._assert_clip_not_none(clip_end, "end_slide")
            clip_end = self._set_clip_duration(clip_end, end_clip_duration)
            clip_end = self._apply_motion_effect(
                clip_end,
                video_size,
                {"name": "slow_zoom", "zoom_factor": 1.05, "scene_number": total_scenes + 1, "total_scenes": max(1, total_scenes + 1)},
            )
            clip_end = self._apply_soft_fade(
                clip_end,
                fade_in_sec=min(0.45, end_clip_duration * 0.18),
                fade_out_sec=min(0.45, end_clip_duration * 0.20),
            )
            clips.append(clip_end)
            render_report["visual_plan"]["closing_background_source"] = closing_background.get("source")
            render_report["visual_plan"]["closing_logo_present"] = bool(branding_profile.get("logo_path"))
            render_report["visual_plan"]["closing_caption_suppressed"] = False
            render_report["visual_plan"]["closing_message_lines"] = list(branding_profile.get("final_message_lines") or [])
            render_report["visual_plan"]["contextual_closing"] = dict(branding_profile.get("contextual_closing") or {})
            render_report["visual_plan"]["endcard_cta_text"] = branding_profile.get("endcard_cta_text")
            render_report["visual_plan"]["cinematic_closing_enabled"] = True
            render_report["visual_plan"]["closing_mode"] = "end_screen_after_cta" if closing_has_narration else "silent_endcard"
            render_report["visual_plan"]["closing_ken_burns"] = "slow_zoom"
            render_report["end_screen_rendered"] = True
            render_report["plain_background_detected_at_end"] = bool(not end_bg_path)
            
            # Concatenar todos
            debug_ctx["stage"] = "concat"
            for ci, c in enumerate(list(clips)):
                self._assert_clip_not_none(c, "clips_list_item", {"clip_index": ci})
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
            
            # Tenta gerar música exclusiva com IA
            if self.ai_service:
                print(f"Gerando música exclusiva para mood: {music_mood}...")
                music_brief = music_prompt or f"{music_mood} style, inspired by {title}"
                music_content = self.ai_service.generate_music(music_brief)
                if music_content:
                    filename = f"music_{uuid.uuid4()}.wav" 
                    generated_music_path = os.path.join(self.output_dir, filename)
                    with open(generated_music_path, "wb") as f:
                        f.write(music_content)
                    music_path = generated_music_path
            
            # Se falhou ou não tem IA, usa biblioteca local
            if not music_path or not os.path.exists(music_path):
                 self._ensure_fallback_music()
                 local_path = os.path.join("app/static/music", f"{fallback_music_mood}.mp3")
                 if os.path.exists(local_path):
                     music_path = local_path
                 else:
                     try:
                         import glob
                         mp3_files = glob.glob("app/static/music/*.mp3")
                         if mp3_files:
                             music_path = mp3_files[0]
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
