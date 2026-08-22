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
from typing import Optional, Callable, List, Dict, Any

from app.config import VIDEO_OUTPUT_DIR, VIDEO_URL_PREFIX, STATIC_DIR
from app.services.media_probe import duration_sync_tolerance_seconds
from app.services.recovery_image_budget import RecoveryImageCallBudget
from app.services.safe_text_layout import SafeTextLayout

CAPTION_SAFE_AREA_X_RATIO = 0.06
CAPTION_SAFE_AREA_TOP_RATIO = 0.08
CAPTION_SAFE_AREA_BOTTOM_RATIO = 0.08
DEFAULT_SCENE_TRANSITION_SEC = 0.30
DEFAULT_SCENE_AUDIO_MARGIN_SEC = 0.40
DEFAULT_OPENING_SILENCE_SEC = 0.45
DEFAULT_SCENE_IMAGE_LEAD_SEC = 0.30
DEFAULT_SCENE_CAPTION_LEAD_SEC = 0.20
DEFAULT_CINEMATIC_END_SCREEN_SEC = 4.0
DEFAULT_MAX_CINEMATIC_VISUAL_HOLD_SEC = 7.0

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
        plan = plan if isinstance(plan, dict) else {}
        explicit_hook = next(
            (
                str(plan.get(key) or "").strip()
                for key in ("opening_text", "opening_hook", "hook_text", "hook")
                if str(plan.get(key) or "").strip()
            ),
            "",
        )
        if explicit_hook:
            return self._compact_cinematic_phrase(explicit_hook, max_words=14)

        title = re.sub(r"^\s*(?:estudo|epis[oó]dio)\s+\d+\s*[—–:\-]\s*", "", str(plan.get("title") or "").strip(), flags=re.IGNORECASE)
        title_hook = self._compact_cinematic_phrase(title, max_words=10)
        if title_hook:
            return self._compact_cinematic_phrase(
                f"{title_hook.rstrip('.!?')} — uma mensagem de fé para hoje.",
                max_words=14,
            )
        return "Prepare o coração: há uma mensagem de fé para o seu dia."

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
        safe_channel = str(channel_name or "").strip() or "Herdeiros das Promessas"
        return (
            f"Continue conosco. Inscreva-se no canal {safe_channel} "
            "e acompanhe as próximas mensagens de fé."
        )

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
    ) -> Dict[str, Any]:
        try:
            from moviepy.editor import AudioFileClip, concatenate_audioclips, AudioClip
        except ImportError:
            from moviepy import AudioFileClip, concatenate_audioclips, AudioClip

        main_audio_path = self.generate_audio(main_text, voice_style=voice_style, voice_gender=voice_gender)
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
                cta_audio_path = self.generate_audio(cta_text, voice_style=voice_style, voice_gender=voice_gender)
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
            "environment": self._extract_semantic_tags(source_text, environment_catalog),
            "action": self._extract_semantic_tags(source_text, action_catalog),
            "emotion": self._extract_semantic_tags(source_text, emotion_catalog),
            "time": self._extract_semantic_tags(source_text, time_catalog),
            "weather": self._extract_semantic_tags(source_text, weather_catalog),
            "lighting": self._extract_semantic_tags(source_text, lighting_catalog),
            "viewpoint": self._extract_semantic_tags(source_text, viewpoint_catalog),
            "style": self._extract_semantic_tags(source_text, style_catalog) or ["cinematic_realism"],
            "prompt_seed": prompt_seed,
        }
        signature_tokens = set(self._scene_visual_signature({"text": raw_text, "image_prompt": prompt_seed}))
        for key in ("characters", "environment", "action", "emotion", "time", "weather", "lighting", "viewpoint", "style"):
            signature_tokens.update(str(item).lower() for item in (profile.get(key) or []))
        profile["signature"] = sorted(signature_tokens)
        return profile

    def _build_visual_transition_decision(self, previous_profile: Dict[str, Any], current_profile: Dict[str, Any]) -> Dict[str, Any]:
        def _diff(key: str) -> Dict[str, Any]:
            prev = {str(item).lower() for item in (previous_profile.get(key) or []) if str(item).strip()}
            curr = {str(item).lower() for item in (current_profile.get(key) or []) if str(item).strip()}
            return {
                "changed": bool((prev or curr) and prev != curr),
                "introduced": sorted(curr - prev) if not prev else [],
                "dropped": sorted(prev - curr) if not curr else [],
                "added": sorted(curr - prev),
                "removed": sorted(prev - curr),
            }

        dimensions = {key: _diff(key) for key in ("characters", "environment", "action", "emotion", "time", "weather", "lighting", "viewpoint", "style")}
        character_change = dimensions["characters"]["changed"]
        environment_change = dimensions["environment"]["changed"]
        action_change = dimensions["action"]["changed"]
        emotion_change = dimensions["emotion"]["changed"]
        lighting_change = dimensions["lighting"]["changed"]
        relevant_soft_changes = sum(1 for changed in (action_change, emotion_change, lighting_change) if changed)
        should_generate_new = bool(
            character_change
            or environment_change
            or relevant_soft_changes >= 2
        )

        changed_labels: List[str] = []
        for key, label in (
            ("characters", "personagem"),
            ("environment", "cenario"),
            ("action", "acao principal"),
            ("emotion", "emocao"),
            ("time", "momento temporal"),
            ("weather", "clima"),
            ("lighting", "iluminacao"),
            ("viewpoint", "ponto de vista"),
            ("style", "estilo"),
        ):
            if dimensions[key]["changed"]:
                changed_labels.append(label)

        if should_generate_new:
            justification = "Nova imagem por mudanca semantica em " + ", ".join(changed_labels[:4]) if changed_labels else "Nova imagem para preservar clareza semantica"
        else:
            reuse_basis = changed_labels[:2]
            if reuse_basis:
                justification = "Reutiliza imagem com pequenas variacoes porque a continuidade visual segue dominante, apesar de leves mudancas em " + ", ".join(reuse_basis)
            else:
                justification = "Reutiliza imagem porque personagem, ambiente, luz e momento dramatico permanecem coerentes"

        return {
            "should_generate_new": bool(should_generate_new),
            "changed_dimensions": changed_labels,
            "justification": justification,
            "dimensions": dimensions,
        }

    def _build_visual_continuity_anchor(self, title: str, scenes: List[Any], plan: Optional[Dict[str, Any]] = None) -> str:
        profiles = [
            self._build_semantic_scene_profile(scene if isinstance(scene, dict) else {"text": str(scene or ""), "image_prompt": ""}, idx + 1)
            for idx, scene in enumerate(scenes[:3])
        ]
        def _dominant_values(key: str, *, min_hits: int = 2, fallback_hits: int = 1, max_items: int = 2) -> List[str]:
            counts: Dict[str, int] = {}
            ordered: List[str] = []
            for profile in profiles:
                seen_local = set()
                for item in (profile.get(key) or []):
                    label = str(item).strip()
                    if not label:
                        continue
                    normalized = label.lower()
                    if normalized in seen_local:
                        continue
                    seen_local.add(normalized)
                    counts[normalized] = counts.get(normalized, 0) + 1
                    if label not in ordered:
                        ordered.append(label)
            preferred = [item for item in ordered if counts.get(item.lower(), 0) >= min_hits]
            if preferred:
                return preferred[:max_items]
            return [item for item in ordered if counts.get(item.lower(), 0) >= fallback_hits][:max_items]

        common_characters = _dominant_values("characters", min_hits=2, fallback_hits=1)
        common_environment = _dominant_values("environment", min_hits=2, fallback_hits=1)
        common_style = _dominant_values("style", min_hits=1, fallback_hits=1, max_items=1)

        bits: List[str] = []
        clean_title = (title or "").strip()
        if clean_title:
            bits.append(f"Historia: {clean_title[:72]}")
        if common_characters:
            bits.append("Mesma identidade visual de " + ", ".join(common_characters[:2]))
        if common_environment:
            bits.append("Mesmo universo visual em " + ", ".join(common_environment[:2]))
        bits.append("Manter roupa, idade, iluminacao, epoca e paleta coerentes entre as cenas")
        if common_style:
            bits.append("Estilo " + ", ".join(common_style[:2]).replace("_", " "))
        return ". ".join(bit for bit in bits if bit).strip()

    def _scene_visual_signature(self, scene: Any) -> set:
        if isinstance(scene, dict):
            prompt = str(scene.get("image_prompt") or scene.get("visual_prompt") or "").strip()
            text = str(scene.get("text") or "").strip()
        else:
            prompt = ""
            text = str(scene or "").strip()
        base = f"{prompt} {text}".lower()
        tokens = re.findall(r"[a-zA-ZÀ-ÿ0-9']+", base)
        stop = {
            "the", "and", "with", "this", "that", "from", "into", "para", "com", "uma", "um", "de", "do", "da", "dos",
            "das", "que", "sobre", "scene", "cena", "photorealistic", "cinematic", "representing", "narration",
            "story", "video", "shot", "light", "lighting", "camera", "mood", "realistic", "natural",
        }
        return {token for token in tokens if len(token) > 2 and token not in stop}

    def _should_force_new_visual(self, previous_scene: Any, current_scene: Any) -> bool:
        prev_profile = self._build_semantic_scene_profile(
            previous_scene if isinstance(previous_scene, dict) else {"text": str(previous_scene or ""), "image_prompt": ""},
            0,
        )
        curr_profile = self._build_semantic_scene_profile(
            current_scene if isinstance(current_scene, dict) else {"text": str(current_scene or ""), "image_prompt": ""},
            0,
        )
        transition = self._build_visual_transition_decision(prev_profile, curr_profile)
        return bool(transition.get("should_generate_new"))

    def _target_visual_count(
        self,
        scenes: List[Any],
        plan: Optional[Dict[str, Any]] = None,
        *,
        ai_available: bool = True,
        use_single_bg: bool = False,
        selected_image_count: int = 0,
    ) -> int:
        scene_count = max(0, len(scenes or []))
        if scene_count <= 1:
            return max(1, scene_count)
        recovery_budget = RecoveryImageCallBudget(plan)
        if recovery_budget.enabled:
            target = int(recovery_budget.target_image_count or 0)
            return max(1, min(scene_count, target))
        if selected_image_count > 0:
            return min(scene_count, selected_image_count)
        if use_single_bg:
            return 1

        total_seconds = 0.0
        for scene in scenes:
            if isinstance(scene, dict):
                total_seconds += float(scene.get("_estimated_narration_sec") or self._estimate_narration_seconds(scene.get("text") or ""))
            else:
                total_seconds += self._estimate_narration_seconds(str(scene or ""))
        if isinstance(plan, dict):
            try:
                target_duration = float(plan.get("target_duration_sec") or 0)
                if target_duration > total_seconds:
                    total_seconds = target_duration
            except Exception:
                pass

        base_target = max(1, int(round(total_seconds / 14.0))) if total_seconds else scene_count
        base_target = min(scene_count, base_target)

        forced_breaks = 0
        continuity_hits = 0
        comparisons = 0
        for idx in range(1, scene_count):
            comparisons += 1
            force_new = self._should_force_new_visual(scenes[idx - 1], scenes[idx])
            if force_new:
                forced_breaks += 1
            else:
                continuity_hits += 1
        continuity_ratio = (float(continuity_hits) / float(comparisons)) if comparisons else 0.0

        duration_floor = 1
        if total_seconds >= 18:
            duration_floor = 2
        if total_seconds >= 36:
            duration_floor = 3
        if total_seconds >= 60:
            duration_floor = 4

        scene_floor = 1
        if scene_count >= 4:
            scene_floor = 2
        if scene_count >= 7:
            scene_floor = 3
        if scene_count >= 10:
            scene_floor = 4

        min_required = max(1, forced_breaks + 1, duration_floor, scene_floor)
        base_target = max(base_target, min_required)

        if not ai_available:
            reduced_target = max(min_required, base_target - 1)
            base_target = min(scene_count, reduced_target)

        if continuity_ratio >= 0.78 and scene_count >= 6:
            reduced_target = max(min_required, base_target - 1)
            if total_seconds <= 24 and scene_count <= 4:
                reduced_target = max(1, min(reduced_target, 2))
            base_target = reduced_target

        return max(1, min(scene_count, base_target))

    def _compose_visual_prompt_for_group(self, scenes: List[Dict[str, Any]], group_indexes: List[int], continuity_anchor: str) -> str:
        if not group_indexes:
            return continuity_anchor or "Cinematic keyframe with continuity"
        lead_scene = scenes[group_indexes[0]]
        lead_profile = lead_scene.get("_visual_semantic_profile") or self._build_semantic_scene_profile(lead_scene, group_indexes[0] + 1)
        moment = self._compact_narrative_moment(" ".join(str(scenes[idx].get("_tts_text") or scenes[idx].get("text") or "") for idx in group_indexes), max_chars=100)
        clauses: List[str] = []
        if lead_profile.get("characters"):
            clauses.append("Personagem: " + ", ".join(lead_profile["characters"][:2]))
        if lead_profile.get("environment"):
            clauses.append("Ambiente: " + ", ".join(lead_profile["environment"][:2]).replace("_", " "))
        if lead_profile.get("lighting"):
            clauses.append("Iluminacao: " + ", ".join(lead_profile["lighting"][:2]).replace("_", " "))
        if moment:
            clauses.append("Momento narrativo: " + moment)
        if continuity_anchor:
            clauses.append("Continuidade: " + continuity_anchor[:120])
        clauses.append("Estilo: realismo cinematografico, elegante, natural")
        prompt = ". ".join(clause.strip().rstrip(".") for clause in clauses if clause).strip()
        if len(prompt) > 360:
            prompt = prompt[:360].rsplit(" ", 1)[0].strip(" ,.;:-")
        return prompt

    def _selected_image_for_visual_group(
        self,
        selected_image_paths: List[str],
        visual_group_id: int,
    ) -> Optional[str]:
        """Map prepared images to contiguous narrative groups, never round-robin scenes."""
        paths = [str(path).strip() for path in (selected_image_paths or []) if str(path).strip()]
        if not paths:
            return None
        try:
            group_index = max(0, int(visual_group_id or 0))
        except Exception:
            group_index = 0
        return paths[min(group_index, len(paths) - 1)]

    def _resolve_scene_visual_duration(
        self,
        scene_timeline_entry: Dict[str, Any],
        minimum_duration: float,
    ) -> Dict[str, Any]:
        entry = scene_timeline_entry if isinstance(scene_timeline_entry, dict) else {}
        span = max(
            0.0,
            float(entry.get("scene_end") or 0.0) - float(entry.get("scene_start") or 0.0),
        )
        audio_anchored = bool(entry.get("caption_blocks"))
        duration = span if audio_anchored and span > 0 else max(float(minimum_duration or 0.0), span)
        return {
            "duration": duration,
            "timeline_span": span,
            "audio_anchored": audio_anchored,
        }

    def _compose_opening_cover_prompt(
        self,
        title: str,
        scenes: List[Dict[str, Any]],
        continuity_anchor: str,
        plan: Optional[Dict[str, Any]] = None,
    ) -> str:
        first_scene = scenes[0] if scenes else {}
        if not isinstance(first_scene, dict):
            first_scene = {"text": str(first_scene or ""), "image_prompt": ""}
        profile = first_scene.get("_visual_semantic_profile") or self._build_semantic_scene_profile(first_scene, 1)
        title_clean = self._clean_text(title or (plan or {}).get("title") or "").strip()
        first_text = str(first_scene.get("_tts_text") or first_scene.get("text") or "").strip()
        first_prompt = self._clean_image_prompt_seed(
            str(first_scene.get("image_prompt") or first_scene.get("visual_prompt") or ""),
            max_chars=220,
        )
        moment = self._compact_narrative_moment(first_text, max_chars=120)
        clauses: List[str] = []
        if title_clean:
            clauses.append(f"Theme: {title_clean[:90]}")
        if profile.get("characters"):
            clauses.append("Characters: " + ", ".join(profile.get("characters")[:3]))
        if profile.get("environment"):
            clauses.append("Setting: " + ", ".join(str(item).replace("_", " ") for item in profile.get("environment")[:2]))
        if moment:
            clauses.append("Narrative moment: " + moment)
        if first_prompt:
            clauses.append("Visual direction: " + first_prompt)
        if continuity_anchor:
            clauses.append("Continuity: " + continuity_anchor[:140])
        clauses.append(
            "Opening cover frame for a cinematic biblical video, elegant composition, emotional and reverent mood, "
            "clear focal point, center-safe framing for title overlay, natural dramatic light, no text, no watermark, no logo"
        )
        prompt = ". ".join(clause.strip().rstrip(".") for clause in clauses if clause).strip()
        return prompt[:700].rsplit(" ", 1)[0].strip(" ,.;:-") if len(prompt) > 700 else prompt

    def _resolve_opening_background_image(
        self,
        title: str,
        scenes: List[Dict[str, Any]],
        continuity_anchor: str,
        *,
        plan: Optional[Dict[str, Any]] = None,
        selected_primary_path: Optional[str] = None,
        cover_image_path: Optional[str] = None,
        video_bg_path: Optional[str] = None,
        aspect_ratio: str = "9:16",
        image_max_rounds: int = 2,
        allow_non_ai_fallback: bool = False,
        status_callback=None,
        paid_call_guard=None,
        generated_group_paths: Optional[Dict[int, str]] = None,
        generated_group_sources: Optional[Dict[int, str]] = None,
        scene_to_group: Optional[Dict[int, int]] = None,
    ) -> Dict[str, Any]:
        provided_cover = str(cover_image_path or "").strip()
        if provided_cover and os.path.exists(provided_cover):
            return {
                "path": provided_cover,
                "source": "provided_cover_image",
                "generated": False,
                "generation_attempted": False,
                "generation_error": None,
                "fallback_reason": None,
            }

        first_scene = scenes[0] if scenes else {}
        if not isinstance(first_scene, dict):
            first_scene = {"text": str(first_scene or ""), "image_prompt": ""}
        first_scene_text = str(first_scene.get("_tts_text") or first_scene.get("text") or "").strip()
        opening_prompt = self._compose_opening_cover_prompt(title, scenes, continuity_anchor, plan=plan)
        opening_generation_error = None
        opening_generation_exception = None

        if opening_prompt:
            try:
                opening_path = self._ensure_image_for_scene(
                    opening_prompt,
                    text_fallback=(first_scene_text or title)[:220],
                    aspect_ratio=aspect_ratio,
                    status_callback=status_callback,
                    max_rounds=image_max_rounds,
                    paid_call_guard=paid_call_guard,
                    # A abertura precisa revelar falha real da capa temática;
                    # o fallback genérico só entra depois, explicitamente.
                    allow_non_ai_fallback=False,
                )
                if opening_path:
                    opening_group_id = 0
                    if isinstance(scene_to_group, dict) and 0 in scene_to_group:
                        opening_group_id = int(scene_to_group.get(0) or 0)
                    if generated_group_paths is not None and opening_group_id not in generated_group_paths:
                        generated_group_paths[opening_group_id] = opening_path
                    if generated_group_sources is not None and opening_group_id not in generated_group_sources:
                        generated_group_sources[opening_group_id] = "opening_theme_image"
                    return {
                        "path": opening_path,
                        "source": "generated_opening_cover",
                        "generated": True,
                        "generation_attempted": True,
                        "generation_error": None,
                        "fallback_reason": None,
                    }
            except Exception as exc:
                opening_generation_error = str(exc)
                opening_generation_exception = exc

        selected_path = str(selected_primary_path or "").strip()
        if selected_path and os.path.exists(selected_path):
            return {
                "path": selected_path,
                "source": "selected_first_scene_image",
                "generated": False,
                "generation_attempted": bool(opening_prompt),
                "generation_error": opening_generation_error,
                "fallback_reason": "thematic_generation_failed_using_first_scene_image" if opening_generation_error else None,
            }

        pooled_background = str(video_bg_path or "").strip()
        if pooled_background and os.path.exists(pooled_background):
            return {
                "path": pooled_background,
                "source": "video_background_pool",
                "generated": False,
                "generation_attempted": bool(opening_prompt),
                "generation_error": opening_generation_error,
                "fallback_reason": "thematic_generation_failed_using_video_background_pool" if opening_generation_error else None,
            }

        if opening_generation_exception is not None:
            # Sem imagem já existente para reaproveitar, não faça uma segunda
            # chamada paga para a primeira cena após a capa ter falhado.
            raise opening_generation_exception

        return {
            "path": None,
            "source": "fallback_background",
            "generated": False,
            "generation_attempted": bool(opening_prompt),
            "generation_error": opening_generation_error or ("opening_cover_prompt_unavailable" if not opening_prompt else None),
            "fallback_reason": "thematic_generation_failed_no_other_opening_image_available",
        }

    def _apply_soft_fade(self, clip, fade_in_sec: float = 0.45, fade_out_sec: float = 0.30):
        try:
            clip_duration = float(getattr(clip, "duration", 0.0) or 0.0)
        except Exception:
            clip_duration = 0.0
        if clip_duration <= 0:
            return clip

        fade_in = max(0.0, min(float(fade_in_sec or 0.0), clip_duration * 0.45))
        fade_out = max(0.0, min(float(fade_out_sec or 0.0), clip_duration * 0.35))
        result = clip
        try:
            if fade_in > 0:
                if hasattr(result, "fadein"):
                    result = result.fadein(fade_in)
                elif hasattr(result, "crossfadein"):
                    result = result.crossfadein(fade_in)
                else:
                    try:
                        from moviepy.editor import vfx
                    except ImportError:
                        from moviepy import vfx
                    result = result.fx(vfx.fadein, fade_in)
            if fade_out > 0:
                if hasattr(result, "fadeout"):
                    result = result.fadeout(fade_out)
                elif hasattr(result, "crossfadeout"):
                    result = result.crossfadeout(fade_out)
                else:
                    try:
                        from moviepy.editor import vfx
                    except ImportError:
                        from moviepy import vfx
                    result = result.fx(vfx.fadeout, fade_out)
        except Exception:
            return clip
        return result

    def _build_opening_title_overlay(
        self,
        title: str,
        size,
        *,
        footer_text: Optional[str] = None,
        duration: float = 2.0,
    ):
        safe_title = str(title or "").strip()
        if not safe_title:
            return None
        overlay_arr = self.create_text_overlay(
            safe_title,
            size=size,
            text_color=(255, 255, 255),
            footer_text=footer_text,
            max_lines=3,
            vertical_anchor="center",
            reserved_bottom_ratio=0.0,
            safe_area_override={"top": 0.08, "bottom": 0.08, "left": 0.08, "right": 0.08},
        )
        overlay_clip = self._clip_from_rgba(overlay_arr, duration)
        overlay_clip = self._apply_motion_effect(
            overlay_clip,
            size,
            {"name": "slow_zoom", "zoom_factor": 1.04, "scene_number": 0, "total_scenes": 1},
        )
        overlay_clip = self._apply_soft_fade(
            overlay_clip,
            fade_in_sec=min(0.65, float(duration or 0.0) * 0.28),
            fade_out_sec=min(0.40, float(duration or 0.0) * 0.18),
        )
        return overlay_clip

    def _build_visual_groups(
        self,
        scenes: List[Dict[str, Any]],
        plan: Optional[Dict[str, Any]] = None,
        *,
        ai_available: bool = True,
        use_single_bg: bool = False,
        selected_image_count: int = 0,
    ) -> Dict[str, Any]:
        scene_count = len(scenes or [])
        if scene_count <= 0:
            return {"target_image_count": 0, "groups": [], "scene_to_group": {}, "scene_decisions": []}

        profiles: List[Dict[str, Any]] = []
        for idx, scene in enumerate(scenes):
            profile = self._build_semantic_scene_profile(scene, idx + 1)
            scene["_visual_semantic_profile"] = profile
            profiles.append(profile)

        target_count = self._target_visual_count(
            scenes,
            plan,
            ai_available=ai_available,
            use_single_bg=use_single_bg,
            selected_image_count=selected_image_count,
        )
        recovery_budget_limited = RecoveryImageCallBudget(plan).enabled
        ideal_group_size = max(1, int(math.ceil(float(scene_count) / float(max(1, target_count)))))
        groups: List[Dict[str, Any]] = []
        current_indexes: List[int] = []
        scene_decisions: List[Dict[str, Any]] = []

        def _flush_group() -> None:
            if not current_indexes:
                return
            lead_profile = profiles[current_indexes[0]]
            groups.append({
                "group_id": len(groups),
                "scene_indexes": list(current_indexes),
                "semantic_summary": {
                    "characters": lead_profile.get("characters") or [],
                    "environment": lead_profile.get("environment") or [],
                    "action": lead_profile.get("action") or [],
                    "moment": lead_profile.get("moment") or "",
                },
            })
            current_indexes.clear()

        for idx in range(scene_count):
            current_indexes.append(idx)
            if idx == 0:
                scene_decisions.append({
                    "scene_index": 0,
                    "decision": "new_image",
                    "justification": "Primeira cena define a base visual e a continuidade do video",
                })
            remaining_scenes = scene_count - (idx + 1)
            remaining_groups = target_count - (len(groups) + 1)
            if remaining_scenes <= 0:
                _flush_group()
                continue
            transition = self._build_visual_transition_decision(profiles[idx], profiles[idx + 1])
            # A confirmação de recuperação define um número menor de imagens
            # que o total de cenas. Nesse caso distribuímos os grupos de forma
            # uniforme; a trava global de unicidade não pode concentrar quase
            # todas as cenas na última imagem nem ampliar o gasto aprovado.
            force_new = bool(transition["should_generate_new"]) and not recovery_budget_limited
            enough_for_split = len(current_indexes) >= ideal_group_size and remaining_scenes >= max(1, remaining_groups)
            if force_new:
                scene_decisions.append({
                    "scene_index": idx + 1,
                    "decision": "new_image",
                    "justification": transition["justification"],
                })
            else:
                scene_decisions.append({
                    "scene_index": idx + 1,
                    "decision": "reuse_image",
                    "justification": (
                        "Reutilização distribuída dentro do limite de imagens confirmado pelo usuário"
                        if recovery_budget_limited
                        else transition["justification"]
                    ),
                })
            if remaining_groups > 0 and (force_new or enough_for_split):
                _flush_group()

        scene_to_group: Dict[int, int] = {}
        for group in groups:
            for scene_idx in group.get("scene_indexes") or []:
                scene_to_group[int(scene_idx)] = int(group["group_id"])
        return {
            "target_image_count": max(1, len(groups)),
            "groups": groups,
            "scene_to_group": scene_to_group,
            "scene_decisions": scene_decisions,
        }

    def _plan_scene_visual_durations(
        self,
        scenes: List[Dict[str, Any]],
        requested_total_duration: Optional[float],
        title_duration: float,
        end_duration: float,
        scene_decisions: Optional[List[Dict[str, Any]]] = None,
        transition_duration: float = 0.0,
    ) -> Dict[str, Any]:
        scene_decisions = scene_decisions or []
        scene_count = len(scenes or [])
        if scene_count <= 0:
            return {
                "requested_duration_sec": float(requested_total_duration or 0.0),
                "target_body_duration_sec": 0.0,
                "allocated_scene_durations": [],
                "baseline_body_duration_sec": 0.0,
            }

        baseline_scene_durations: List[float] = []
        weights: List[float] = []
        for idx, scene in enumerate(scenes):
            estimated = float(scene.get("_estimated_narration_sec") or self._estimate_narration_seconds(scene.get("text") or ""))
            estimated = max(2.5, estimated)
            decision = scene_decisions[idx] if idx < len(scene_decisions) else {}
            is_new = str(decision.get("decision") or "") == "new_image"
            weight = estimated
            if is_new:
                weight += 0.65
            if idx == 0:
                weight += 0.35
            if idx == scene_count - 1:
                weight += 0.25
            baseline_scene_durations.append(estimated)
            weights.append(weight)

        baseline_body_duration = sum(baseline_scene_durations) + max(0.0, transition_duration * max(0, scene_count - 1))
        if not requested_total_duration or requested_total_duration <= (title_duration + end_duration):
            requested_total_duration = baseline_body_duration + title_duration + end_duration

        target_body_duration = max(
            baseline_body_duration,
            float(requested_total_duration) - float(title_duration) - float(end_duration)
        )
        allocated = list(baseline_scene_durations)
        extra_budget = max(0.0, target_body_duration - baseline_body_duration)
        if extra_budget > 0:
            stretch_weights: List[float] = []
            for idx, base in enumerate(baseline_scene_durations):
                decision = scene_decisions[idx] if idx < len(scene_decisions) else {}
                is_reuse = str(decision.get("decision") or "") != "new_image"
                stretch_weight = max(1.0, base) * (1.2 if is_reuse else 0.8)
                if idx == 0 or idx == scene_count - 1:
                    stretch_weight += 0.25
                stretch_weights.append(stretch_weight)
            total_stretch = sum(stretch_weights) or float(scene_count)
            allocated = [
                round(base + (extra_budget * (stretch_weight / total_stretch)), 2)
                for base, stretch_weight in zip(baseline_scene_durations, stretch_weights)
            ]
            rounding_gap = round(target_body_duration - sum(allocated), 2)
            if allocated and abs(rounding_gap) >= 0.01:
                allocated[-1] = round(max(baseline_scene_durations[-1], allocated[-1] + rounding_gap), 2)

        return {
            "requested_duration_sec": float(requested_total_duration or 0.0),
            "target_body_duration_sec": round(target_body_duration, 2),
            "allocated_scene_durations": allocated,
            "baseline_body_duration_sec": round(baseline_body_duration, 2),
        }

    def _split_caption_units(self, text: str, max_words: int = 8, max_chars: int = 54) -> List[str]:
        cleaned = self._normalize_tts_text(text)
        if not cleaned:
            return []
        units: List[str] = []

        def flush_piece(piece: str):
            words = [w for w in str(piece or "").split() if w]
            if not words:
                return
            current_words: List[str] = []
            for word in words:
                candidate = " ".join(current_words + [word]).strip()
                if current_words and (len(current_words) + 1 > max_words or len(candidate) > max_chars):
                    units.append(" ".join(current_words).strip())
                    current_words = [word]
                else:
                    current_words.append(word)
            if current_words:
                units.append(" ".join(current_words).strip())

        sentence_parts = [
            part.strip()
            for part in re.findall(r'[^.!?…\n]+(?:[.!?…]+["”’\']?|$)', cleaned)
            if str(part or "").strip()
        ]
        if not sentence_parts:
            sentence_parts = [cleaned]

        normalized_pieces: List[str] = []
        for sentence in sentence_parts:
            soft_parts = [part.strip() for part in re.split(r"(?<=[,;:])\s+", sentence) if part and part.strip()]
            normalized_pieces.extend(soft_parts or [sentence.strip()])

        current = ""
        current_words = 0
        for piece in normalized_pieces:
            piece_words = len(piece.split())
            if piece_words <= max_words and len(piece) <= max_chars:
                if (
                    current
                    and current_words + piece_words <= max_words
                    and len(f"{current} {piece}".strip()) <= max_chars
                    and not re.search(r"[.!?…]$", current)
                ):
                    current = f"{current} {piece}".strip()
                    current_words += piece_words
                else:
                    if current:
                        units.append(current.strip())
                    current = piece
                    current_words = piece_words
                continue

            if current:
                units.append(current.strip())
                current = ""
                current_words = 0
            flush_piece(piece)

        if current.strip():
            units.append(current.strip())

        units = [unit.strip() for unit in units if unit and unit.strip()]
        return units

    def _join_caption_tokens(self, tokens: List[str]) -> str:
        caption = ""
        for raw in tokens:
            token = str(raw or "").strip()
            if not token:
                continue
            if not caption:
                caption = token
                continue
            if re.match(r"^[,.;:!?…)\]\}%]+$", token):
                caption += token
            elif re.match(r"^['\"“”‘’`]+$", token):
                caption += token
            elif caption.endswith(("(", "[", "{", "“", '"', "‘", "'")):
                caption += token
            else:
                caption += f" {token}"
        return caption.strip()

    def _caption_timeline_from_text(self, narration: str, duration: float) -> List[Dict[str, Any]]:
        total_duration = float(duration or 0.0)
        if total_duration <= 0:
            return []
        chunks = self._split_caption_units(narration, max_words=8, max_chars=54)
        if not chunks:
            return []

        total_words = sum(max(1, len(c.split())) for c in chunks)
        cursor = 0.0
        timeline: List[Dict[str, Any]] = []
        remaining_words = total_words
        remaining_duration = total_duration
        for idx, chunk in enumerate(chunks):
            chunk_words = max(1, len(chunk.split()))
            if idx == len(chunks) - 1:
                end = total_duration
            else:
                proportional = remaining_duration * (chunk_words / max(1, remaining_words))
                seg_dur = max(0.9, min(4.2, proportional))
                end = min(total_duration, cursor + seg_dur)
            if end <= cursor:
                continue
            timeline.append({"start": cursor, "end": end, "caption": chunk})
            remaining_words -= chunk_words
            remaining_duration = max(0.0, total_duration - end)
            cursor = end
        if timeline:
            timeline[0]["start"] = 0.0
            timeline[-1]["end"] = total_duration
        return timeline

    def _realign_caption_timeline_to_narration(
        self,
        timeline: List[Dict[str, Any]],
        narration: str,
    ) -> List[Dict[str, Any]]:
        if not isinstance(timeline, list) or not timeline:
            return timeline
        normalized_narration = self._normalize_tts_text(narration)
        narration_tokens = [token for token in normalized_narration.split() if token]
        if not narration_tokens:
            return timeline

        target_counts = [
            max(1, len(str(item.get("caption") or "").strip().split()))
            for item in timeline
        ]
        total_target = max(1, sum(target_counts))
        remaining_tokens = len(narration_tokens)
        remaining_target = total_target
        token_cursor = 0
        aligned: List[Dict[str, Any]] = []

        for idx, item in enumerate(timeline):
            blocks_left = len(timeline) - idx
            minimum_reserved = max(0, blocks_left - 1)
            if idx == len(timeline) - 1:
                take_count = remaining_tokens
            else:
                proportional = int(round(remaining_tokens * (target_counts[idx] / max(1, remaining_target))))
                take_count = max(1, min(remaining_tokens - minimum_reserved, proportional))
            if take_count <= 0:
                take_count = max(1, remaining_tokens)
            caption_tokens = narration_tokens[token_cursor:token_cursor + take_count]
            if not caption_tokens and remaining_tokens > 0:
                caption_tokens = narration_tokens[token_cursor:token_cursor + 1]
            token_cursor += len(caption_tokens)
            remaining_tokens = max(0, len(narration_tokens) - token_cursor)
            remaining_target = max(0, remaining_target - target_counts[idx])
            aligned_item = dict(item)
            aligned_item["caption"] = " ".join(caption_tokens).strip()
            aligned.append(aligned_item)

        if aligned and token_cursor < len(narration_tokens):
            tail = " ".join(narration_tokens[token_cursor:]).strip()
            if tail:
                last_caption = str(aligned[-1].get("caption") or "").strip()
                aligned[-1]["caption"] = f"{last_caption} {tail}".strip() if last_caption else tail

        aligned = [item for item in aligned if str(item.get("caption") or "").strip()]
        return aligned or timeline

    def _caption_timeline_from_segments(self, segments: List[Dict[str, Any]], duration: float, narration: str = "") -> List[Dict[str, Any]]:
        total_duration = float(duration or 0.0)
        if total_duration <= 0 or not isinstance(segments, list):
            return []

        words: List[Dict[str, Any]] = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_words = seg.get("words")
            if isinstance(seg_words, list) and seg_words:
                for item in seg_words:
                    if not isinstance(item, dict):
                        continue
                    token = str(item.get("word") or item.get("text") or "").strip()
                    if not token:
                        continue
                    try:
                        ws = max(0.0, float(item.get("start")))
                        we = min(total_duration, float(item.get("end")))
                    except Exception:
                        continue
                    if we <= ws:
                        continue
                    words.append({"start": ws, "end": we, "word": token})

        timeline: List[Dict[str, Any]] = []
        if words:
            current: List[Dict[str, Any]] = []

            def flush_words():
                nonlocal current, timeline
                if not current:
                    return
                caption = self._join_caption_tokens([str(w.get("word") or "").strip() for w in current])
                if not caption:
                    current = []
                    return
                start = float(current[0].get("start") or 0.0)
                end = float(current[-1].get("end") or start)
                if end > start:
                    timeline.append({"start": start, "end": end, "caption": caption})
                current = []

            for word in words:
                token = str(word.get("word") or "").strip()
                current.append(word)
                caption = self._join_caption_tokens([str(w.get("word") or "").strip() for w in current])
                dur = float(current[-1].get("end") or 0.0) - float(current[0].get("start") or 0.0)
                hard_break = bool(re.search(r"[.!?…]$", token))
                soft_break = bool(re.search(r"[,;:]$", token))
                if (
                    len(current) >= 6
                    or len(caption) >= 42
                    or dur >= 2.4
                    or hard_break
                    or (soft_break and dur >= 1.2)
                ):
                    flush_words()
            flush_words()

        if timeline:
            timeline[0]["start"] = 0.0
            timeline[-1]["end"] = total_duration
            return self._realign_caption_timeline_to_narration(timeline, narration)

        approx: List[Dict[str, Any]] = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            try:
                seg_start = max(0.0, float(seg.get("start")))
                seg_end = min(total_duration, float(seg.get("end")))
            except Exception:
                continue
            if seg_end <= seg_start:
                continue
            units = self._split_caption_units(text, max_words=8, max_chars=54)
            if not units:
                continue
            seg_duration = seg_end - seg_start
            total_words = sum(max(1, len(u.split())) for u in units)
            local_cursor = seg_start
            remaining_words = total_words
            remaining_duration = seg_duration
            for idx, unit in enumerate(units):
                unit_words = max(1, len(unit.split()))
                if idx == len(units) - 1:
                    unit_end = seg_end
                else:
                    unit_dur = remaining_duration * (unit_words / max(1, remaining_words))
                    unit_end = min(seg_end, local_cursor + max(0.6, unit_dur))
                if unit_end > local_cursor:
                    approx.append({"start": local_cursor, "end": unit_end, "caption": unit})
                remaining_words -= unit_words
                remaining_duration = max(0.0, seg_end - unit_end)
                local_cursor = unit_end
        if approx:
            approx[0]["start"] = 0.0
            approx[-1]["end"] = total_duration
        return self._realign_caption_timeline_to_narration(approx, narration)

    def _build_caption_timeline(self, narration: str, duration: float, audio_path: Optional[str] = None) -> List[Dict[str, Any]]:
        details = self._build_caption_timeline_details(narration, duration, audio_path=audio_path)
        timeline = details.get("timeline") if isinstance(details, dict) else None
        return timeline if isinstance(timeline, list) else []

    def _build_caption_timeline_details(self, narration: str, duration: float, audio_path: Optional[str] = None) -> Dict[str, Any]:
        total_duration = float(duration or 0.0)
        if total_duration <= 0:
            return {"timeline": [], "source": "empty"}
        if audio_path and self.ai_service and hasattr(self.ai_service, "transcribe_audio_segments_detailed"):
            try:
                strict = str(os.getenv("CAPTION_SYNC_STRICT") or "").strip().lower() in {"1", "true", "yes", "on"}
                info = self.ai_service.transcribe_audio_segments_detailed(audio_path, language="pt")
                segments = info.get("segments") if isinstance(info, dict) else None
                if isinstance(segments, list) and segments:
                    timed = self._caption_timeline_from_segments(segments, total_duration, narration=narration)
                    if timed:
                        return {"timeline": timed, "source": "real_segments_aligned_to_narration"}
                if strict:
                    err = info.get("error") if isinstance(info, dict) else None
                    raise Exception(f"Transcrição indisponível para sincronização de legendas: {err or 'no_segments'}")
            except Exception:
                pass
        return {
            "timeline": self._caption_timeline_from_text(narration, total_duration),
            "source": "text_fallback",
        }

    def _find_scene_text_ranges_in_body(self, body_text: str, scenes: List[Dict[str, Any]]) -> List[Dict[str, int]]:
        normalized_body = self._normalize_tts_text(body_text)
        ranges: List[Dict[str, int]] = []
        cursor = 0
        for scene in scenes or []:
            scene_text = self._normalize_tts_text((scene or {}).get("_tts_text") or (scene or {}).get("text") or "")
            if not scene_text:
                ranges.append({"start": cursor, "end": cursor})
                continue
            found = normalized_body.find(scene_text, cursor)
            if found < 0:
                found = normalized_body.find(scene_text)
            if found < 0:
                start = cursor
                end = cursor + len(scene_text)
            else:
                start = found
                end = found + len(scene_text)
            ranges.append({"start": start, "end": end})
            cursor = max(cursor, end)
        return ranges

    def _find_caption_position_in_body(self, body_text: str, caption_text: str, search_cursor: int = 0) -> Dict[str, int]:
        normalized_body = self._normalize_tts_text(body_text)
        normalized_caption = self._normalize_tts_text(caption_text)
        if not normalized_body or not normalized_caption:
            return {"start": max(0, int(search_cursor or 0)), "end": max(0, int(search_cursor or 0))}
        found = normalized_body.find(normalized_caption, max(0, int(search_cursor or 0)))
        if found < 0 and search_cursor:
            found = normalized_body.find(normalized_caption, max(0, int(search_cursor or 0)) - 80)
        if found < 0:
            found = normalized_body.find(normalized_caption)
        if found < 0:
            found = max(0, min(len(normalized_body), int(search_cursor or 0)))
        return {"start": found, "end": min(len(normalized_body), found + len(normalized_caption))}

    def _legacy_scene_index_for_time(self, moment_sec: float, legacy_windows: List[Dict[str, float]]) -> int:
        try:
            moment = float(moment_sec or 0.0)
        except Exception:
            moment = 0.0
        for idx, window in enumerate(legacy_windows or []):
            start = float(window.get("start") or 0.0)
            end = float(window.get("end") or 0.0)
            if moment >= start and moment < end:
                return idx
        if legacy_windows:
            return max(0, len(legacy_windows) - 1)
        return 0

    def _build_scene_caption_sync_map(
        self,
        full_timeline: List[Dict[str, Any]],
        scenes: List[Dict[str, Any]],
        planning_meta: Dict[str, Any],
        legacy_scene_windows: List[Dict[str, float]],
        title_duration: float,
        end_duration: float,
        actual_total_audio_dur: float,
        timeline_source: str = "text_fallback",
    ) -> Dict[str, Any]:
        scene_count = len(scenes or [])
        empty_result = {
            "timeline_source": timeline_source,
            "scene_timelines": [[] for _ in range(scene_count)],
            "scene_required_durations": [0.0 for _ in range(scene_count)],
            "block_sync_report": {
                "total_blocks": 0,
                "largest_delay_estimated_sec": 0.0,
                "largest_advance_estimated_sec": 0.0,
                "blocks_with_risk_of_drift": 0,
                "risky_block_indices": [],
            },
        }
        if not isinstance(full_timeline, list) or not full_timeline or scene_count <= 0:
            return empty_result

        body_text = self._normalize_tts_text((planning_meta or {}).get("body_text") or "")
        if not body_text:
            return empty_result

        body_start = max(0.0, float(title_duration or 0.0))
        body_end = max(body_start, float(actual_total_audio_dur or 0.0) - max(0.0, float(end_duration or 0.0)))
        scene_ranges = self._find_scene_text_ranges_in_body(body_text, scenes)
        per_scene_global: List[List[Dict[str, Any]]] = [[] for _ in range(scene_count)]
        search_cursor = 0

        for block_idx, item in enumerate(full_timeline):
            try:
                item_start = float(item.get("start") or 0.0)
                item_end = float(item.get("end") or 0.0)
            except Exception:
                continue
            if item_end <= body_start or item_start >= body_end:
                continue
            caption = str(item.get("caption") or "").strip()
            if not caption:
                continue

            position = self._find_caption_position_in_body(body_text, caption, search_cursor=search_cursor)
            cap_start = int(position.get("start") or 0)
            cap_end = int(position.get("end") or cap_start)
            search_cursor = max(search_cursor, cap_end)
            midpoint = (cap_start + cap_end) / 2.0

            scene_idx = 0
            for idx, item_range in enumerate(scene_ranges):
                start = int(item_range.get("start") or 0)
                end = int(item_range.get("end") or start)
                if midpoint >= start and midpoint <= max(start, end):
                    scene_idx = idx
                    break
                if idx == len(scene_ranges) - 1 and midpoint > max(start, end):
                    scene_idx = idx

            legacy_scene_idx = self._legacy_scene_index_for_time((item_start + item_end) / 2.0, legacy_scene_windows)
            per_scene_global[scene_idx].append({
                "block_index": block_idx,
                "caption": caption,
                "global_start": max(body_start, item_start),
                "global_end": min(body_end, item_end),
                "legacy_scene_index": legacy_scene_idx,
                "assigned_scene_index": scene_idx,
            })

        scene_timelines: List[List[Dict[str, Any]]] = [[] for _ in range(scene_count)]
        scene_required_durations: List[float] = []
        largest_delay = 0.0
        largest_advance = 0.0
        risky_blocks: List[int] = []

        for idx in range(scene_count):
            items = per_scene_global[idx]
            legacy_start = float((legacy_scene_windows[idx] if idx < len(legacy_scene_windows) else {}).get("start") or body_start)
            if not items:
                scene_required_durations.append(0.0)
                continue
            actual_scene_start = min(float(item.get("global_start") or body_start) for item in items)
            local_items: List[Dict[str, Any]] = []
            for item in items:
                global_start = float(item.get("global_start") or actual_scene_start)
                global_end = float(item.get("global_end") or global_start)
                local_start = max(0.0, global_start - actual_scene_start)
                local_end = max(local_start, global_end - actual_scene_start)
                drift_estimated = round(actual_scene_start - legacy_start, 3)
                if drift_estimated > 0:
                    largest_delay = max(largest_delay, drift_estimated)
                elif drift_estimated < 0:
                    largest_advance = max(largest_advance, abs(drift_estimated))
                is_risky = bool(abs(drift_estimated) >= 0.25 or int(item.get("legacy_scene_index") or 0) != idx)
                if is_risky:
                    risky_blocks.append(int(item.get("block_index") or 0))
                local_items.append({
                    "block_index": int(item.get("block_index") or 0),
                    "caption": str(item.get("caption") or "").strip(),
                    "start": round(local_start, 3),
                    "end": round(local_end, 3),
                    "global_start": round(global_start, 3),
                    "global_end": round(global_end, 3),
                    "estimated_drift_sec": drift_estimated,
                    "risk_of_drift": is_risky,
                })
            scene_timelines[idx] = local_items
            scene_required_durations.append(round(max(float(item.get("end") or 0.0) for item in local_items), 3))

        return {
            "timeline_source": timeline_source,
            "scene_timelines": scene_timelines,
            "scene_required_durations": scene_required_durations,
            "block_sync_report": {
                "total_blocks": sum(len(items) for items in scene_timelines),
                "largest_delay_estimated_sec": round(largest_delay, 3),
                "largest_advance_estimated_sec": round(largest_advance, 3),
                "blocks_with_risk_of_drift": len(risky_blocks),
                "risky_block_indices": sorted(set(risky_blocks)),
            },
        }

    def _build_official_scene_timeline(
        self,
        *,
        scenes: List[Dict[str, Any]],
        scene_caption_sync: Dict[str, Any],
        planned_scene_durations: List[float],
        opening_text: str,
        opening_image: str,
        title_duration: float,
        initial_opening_silence_sec: float,
        cta_text: str,
        closing_image: str,
        pause_before_cta_sec: float,
        cta_duration: float,
        end_duration: float,
        timeline_source: str,
        transition_name: str = "fade",
    ) -> List[Dict[str, Any]]:
        scene_count = len(scenes or [])
        sync_timelines = scene_caption_sync.get("scene_timelines") or []
        official_timeline: List[Dict[str, Any]] = []
        title_duration = max(0.0, float(title_duration or 0.0))
        initial_opening_silence_sec = max(0.0, float(initial_opening_silence_sec or 0.0))
        pause_before_cta_sec = max(0.0, float(pause_before_cta_sec or 0.0))
        cta_duration = max(0.0, float(cta_duration or 0.0))
        end_duration = max(0.0, float(end_duration or 0.0))
        if title_duration > 0:
            opening_audio_start = min(title_duration, initial_opening_silence_sec) if str(opening_text or "").strip() else 0.0
            opening_audio_end = title_duration if str(opening_text or "").strip() else opening_audio_start
            opening_caption_start = opening_audio_start if str(opening_text or "").strip() else 0.0
            opening_caption_end = opening_audio_end if str(opening_text or "").strip() else opening_caption_start
            official_timeline.append({
                "scene": 0,
                "kind": "opening",
                "text": self._normalize_tts_text(opening_text or ""),
                "audio_start": round(opening_audio_start, 3),
                "audio_end": round(opening_audio_end, 3),
                "scene_start": 0.0,
                "scene_end": round(title_duration, 3),
                "caption_start": round(opening_caption_start, 3),
                "caption_end": round(opening_caption_end, 3),
                "image": opening_image or "",
                "transition": transition_name,
                "timeline_source": timeline_source,
                "uses_real_audio_timing": False,
                "caption_blocks": [],
            })
        previous_scene_end = title_duration

        for idx in range(scene_count):
            scene = scenes[idx] if idx < len(scenes) and isinstance(scenes[idx], dict) else {}
            clean_text = self._normalize_tts_text(scene.get("_tts_text") or scene.get("text") or "")
            sync_items = list(sync_timelines[idx] or []) if idx < len(sync_timelines) else []
            planned_audio_duration = float(planned_scene_durations[idx]) if idx < len(planned_scene_durations) else float(scene.get("_estimated_narration_sec") or 0.0)

            if sync_items:
                raw_audio_start = min(float(item.get("global_start") or 0.0) for item in sync_items)
                raw_audio_end = max(float(item.get("global_end") or 0.0) for item in sync_items)
                scene_start = previous_scene_end
                audio_start = max(scene_start, raw_audio_start)
                audio_end = max(audio_start, raw_audio_end)
                caption_start = audio_start
                caption_end = audio_end
                scene_end = audio_end
                shift_sec = 0.0
            else:
                audio_start = previous_scene_end + DEFAULT_SCENE_IMAGE_LEAD_SEC + DEFAULT_SCENE_CAPTION_LEAD_SEC
                audio_end = audio_start + max(0.0, planned_audio_duration)
                minimum_audio_start = previous_scene_end + DEFAULT_SCENE_IMAGE_LEAD_SEC + DEFAULT_SCENE_CAPTION_LEAD_SEC
                shift_sec = 0.0
                audio_start = max(previous_scene_end, float(audio_start or 0.0))
                if audio_start < minimum_audio_start:
                    shift_sec = minimum_audio_start - audio_start
                    audio_start += shift_sec
                    audio_end += shift_sec
                audio_end = max(audio_start, float(audio_end or 0.0))
                scene_start = max(previous_scene_end, audio_start - (DEFAULT_SCENE_IMAGE_LEAD_SEC + DEFAULT_SCENE_CAPTION_LEAD_SEC))
                caption_start = max(scene_start + DEFAULT_SCENE_IMAGE_LEAD_SEC, audio_start - DEFAULT_SCENE_CAPTION_LEAD_SEC)
                caption_end = max(caption_start, audio_end)
                scene_end = max(audio_end + DEFAULT_SCENE_AUDIO_MARGIN_SEC, caption_end + DEFAULT_SCENE_AUDIO_MARGIN_SEC)

            caption_blocks: List[Dict[str, Any]] = []
            if sync_items:
                for block_index, item in enumerate(sync_items):
                    block_start = max(scene_start, float(item.get("global_start") or audio_start))
                    block_end = max(block_start, float(item.get("global_end") or audio_end))
                    caption_blocks.append({
                        "block_index": int(item.get("block_index") or block_index),
                        "caption": str(item.get("caption") or clean_text).strip(),
                        "start": round(max(0.0, block_start - scene_start), 3),
                        "end": round(max(0.0, block_end - scene_start), 3),
                        "global_start": round(block_start, 3),
                        "global_end": round(block_end, 3),
                        "source": "audio_timeline",
                    })
            elif clean_text:
                caption_blocks.append({
                    "block_index": 0,
                    "caption": clean_text,
                    "start": round(max(0.0, caption_start - scene_start), 3),
                    "end": round(max(0.0, caption_end - scene_start), 3),
                    "global_start": round(caption_start, 3),
                    "global_end": round(caption_end, 3),
                    "source": "scene_text_fallback",
                })

            entry = {
                "scene": idx + 1,
                "kind": "story",
                "text": clean_text,
                "audio_start": round(audio_start, 3),
                "audio_end": round(audio_end, 3),
                "scene_start": round(scene_start, 3),
                "scene_end": round(scene_end, 3),
                "caption_start": round(caption_start, 3),
                "caption_end": round(caption_end, 3),
                "image": "",
                "transition": transition_name,
                "timeline_source": timeline_source,
                "uses_real_audio_timing": bool(sync_items and timeline_source != "text_fallback"),
                "synthetic_timeline_shift_sec": round(shift_sec, 3),
                "caption_blocks": caption_blocks,
            }
            official_timeline.append(entry)
            previous_scene_end = float(entry["scene_end"])

        if pause_before_cta_sec > 0 or cta_duration > 0:
            cta_audio_start = previous_scene_end + pause_before_cta_sec
            cta_audio_end = cta_audio_start + cta_duration
            cta_scene_end = cta_audio_end + (DEFAULT_SCENE_AUDIO_MARGIN_SEC if cta_duration > 0 else 0.0)
            official_timeline.append({
                "scene": scene_count + 1,
                "kind": "closing",
                "text": self._normalize_tts_text(cta_text or ""),
                "audio_start": round(cta_audio_start, 3),
                "audio_end": round(cta_audio_end, 3),
                "scene_start": round(previous_scene_end, 3),
                "scene_end": round(cta_scene_end, 3),
                "caption_start": round(cta_audio_start, 3),
                "caption_end": round(cta_audio_end, 3),
                "image": closing_image or "",
                "transition": transition_name,
                "timeline_source": timeline_source,
                "uses_real_audio_timing": False,
                "caption_blocks": [{
                    "block_index": 0,
                    "caption": self._normalize_tts_text(cta_text or ""),
                    "start": round(max(0.0, cta_audio_start - previous_scene_end), 3),
                    "end": round(max(0.0, cta_audio_end - previous_scene_end), 3),
                    "global_start": round(cta_audio_start, 3),
                    "global_end": round(cta_audio_end, 3),
                    "source": "cta_timeline",
                }] if str(cta_text or "").strip() and cta_duration > 0 else [],
            })
            previous_scene_end = cta_scene_end

        if end_duration > 0:
            official_timeline.append({
                "scene": scene_count + 2,
                "kind": "endcard",
                "text": "",
                "audio_start": round(previous_scene_end, 3),
                "audio_end": round(previous_scene_end, 3),
                "scene_start": round(previous_scene_end, 3),
                "scene_end": round(previous_scene_end + end_duration, 3),
                "caption_start": round(previous_scene_end, 3),
                "caption_end": round(previous_scene_end, 3),
                "image": closing_image or "",
                "transition": transition_name,
                "timeline_source": timeline_source,
                "uses_real_audio_timing": False,
                "caption_blocks": [],
            })

        return official_timeline

    def review_plan(self, plan: dict):
        if not isinstance(plan, dict):
            return plan
        scenes = plan.get("scenes") or []
        if not isinstance(scenes, list) or not scenes:
            return plan
        notes = []
        for i, s in enumerate(scenes):
            if not isinstance(s, dict):
                continue
            txt = (s.get("text") or "").strip()
            clean = self._clean_text(txt)
            cap = (s.get("caption") or s.get("on_screen_text") or "").strip()
            if not cap:
                cap = clean if len(clean) <= 220 else self._make_caption(clean)
                s["caption"] = cap
                notes.append(f"caption_auto:cena_{i+1}")
            elif len(cap) > 220:
                s["caption"] = self._make_caption(cap)
                notes.append(f"caption_trunc:cena_{i+1}")
        plan["scenes"] = scenes
        if notes:
            plan["review_notes"] = notes
        return plan

    def _clean_text(self, text):
        """Limpa o texto de metadados, instruções de roteiro e markdown"""
        if not text: return ""
        
        # 1. Remove Markdown Bold (**text**) -> text (keep content, remove markers)
        text = text.replace("**", "")

        raw = (text or "").strip()
        if "```" in raw:
            t = raw
            if "```json" in t:
                try:
                    t = t.split("```json", 1)[1]
                except Exception:
                    t = t
            try:
                t = t.split("```", 1)[1] if t.strip().startswith("```") else t
            except Exception:
                t = t
            try:
                t = t.rsplit("```", 1)[0]
            except Exception:
                t = t
            raw = t.strip() or raw

        if raw.startswith("{") or raw.startswith("["):
            try:
                import json

                data = json.loads(raw)

                def pick_str(d: dict, keys: list):
                    for k in keys:
                        v = d.get(k)
                        if isinstance(v, str) and v.strip():
                            return v.strip()
                    return None

                parts = []
                if isinstance(data, dict):
                    title = pick_str(data, ["title", "titulo"])
                    if title:
                        parts.append(title)
                    sections = data.get("sections")
                    if isinstance(sections, list):
                        for s in sections:
                            if isinstance(s, dict):
                                seg = pick_str(s, ["content", "text", "narration", "narration_text"])
                                if seg:
                                    parts.append(seg)
                    scenes = data.get("scenes")
                    if isinstance(scenes, list):
                        for s in scenes:
                            if isinstance(s, dict):
                                seg = pick_str(s, ["text", "narration", "narration_text", "content"])
                                if seg:
                                    parts.append(seg)
                    script_full = pick_str(data, ["script_full", "script", "content", "text", "narration_text", "narration"])
                    if script_full:
                        parts.append(script_full)
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, str) and item.strip():
                            parts.append(item.strip())
                        elif isinstance(item, dict):
                            seg = pick_str(item, ["content", "text", "narration", "narration_text"])
                            if seg:
                                parts.append(seg)

                extracted = "\n\n".join([p for p in parts if isinstance(p, str) and p.strip()]).strip()
                if extracted:
                    text = extracted
            except Exception:
                pass
        
        # 2. Remove Script Prefixes
        # "Narrador:", "Cena 1:", "Imagem:"
        text = re.sub(r'^(Narrador|Narrator|Cena|Scene|Imagem|Visual)(\s+\d+)?\s*[:.-]\s*', '', text, flags=re.IGNORECASE)
        
        # 3. Remove Instructions in Brackets [Visual: ...] or [Sound: ...]
        text = re.sub(r'\[.*?\]', '', text)
        
        # 4. Remove Instructions in Parentheses that look like metadata
        # Removes (Music: ...), (Visual: ...), (Tone: ...)
        text = re.sub(r'\((Music|Visual|Sound|Tone|Credit|Source).*?\)', '', text, flags=re.IGNORECASE)
        
        # 5. Remove explicit credits lines
        text = re.sub(r'^Music:.*$', '', text, flags=re.MULTILINE|re.IGNORECASE)
        text = re.sub(r'^Credits:.*$', '', text, flags=re.MULTILINE|re.IGNORECASE)

        return text.strip()

    def _clean_title(self, title: str) -> str:
        t = (title or "").strip()
        if not t:
            return "Música"
        t = re.sub(r"\s*[-–—|:]\s*$", "", t).strip()
        t = re.sub(r"(\s*[-–—|:]?\s*E\.?MA\.?\s*)$", "", t, flags=re.IGNORECASE).strip()
        return t or "Música"

    def generate_audio(self, text, lang='pt', voice_style=None, voice_gender=None):
        """Gera arquivo de áudio usando OpenAI (Human-like), Edge-TTS (Natural Free) ou gTTS (Fallback)"""
        if not text or not text.strip(): 
            print("Aviso: Texto vazio para generate_audio")
            return None
        
        # Limpeza de segurança para evitar leitura de metadados
        clean_text = self._normalize_tts_text(text)
        if not clean_text: 
            print("Aviso: Texto ficou vazio após limpeza em generate_audio")
            return None

        style = (voice_style or "human").lower()
        gender = (voice_gender or "female").lower()
        tts_debug: Dict[str, Any] = {
            "configured_provider": None,
            "provider_used": None,
            "fallback_used": False,
            "ffprobe_available": self._is_ffprobe_available(),
            "requested_voice_style": style,
            "requested_voice_gender": gender,
            "input_char_count": len(clean_text),
            "input_word_count": len(re.findall(r"\w+", clean_text, flags=re.UNICODE)),
            "attempts": [],
        }
        self._last_tts_debug = tts_debug

        def _record_attempt(provider: str, status: str, reason: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
            item: Dict[str, Any] = {"provider": provider, "status": status}
            if reason:
                item["reason"] = str(reason)[:500]
            if details:
                item["details"] = details
            tts_debug.setdefault("attempts", []).append(item)
        
        print(f"Gerando áudio para: '{clean_text[:30]}...' (Style: {style}, Gender: {gender})")
        
        openai_voice = None
        if self.ai_service and hasattr(self.ai_service, "select_tts_voice_hint"):
            try:
                openai_voice = self.ai_service.select_tts_voice_hint(
                    voice_style=style,
                    voice_gender=gender,
                )
            except Exception:
                openai_voice = None
        if not openai_voice:
            openai_voice = "onyx"
            if style in ["my_voice", "myvoice", "minha_voz", "minhavoz"]:
                openai_voice = "my_voice"
            elif style in ["human", "humana"] or style.startswith("human"):
                openai_voice = "onyx" if gender == "male" else "nova"
            elif style in ["soft", "soft_prayer", "soft-relaxing", "suave", "suave_relaxante"]:
                openai_voice = "echo" if gender == "male" else "nova"
            elif style in ["child", "infantil"]:
                openai_voice = "echo" if gender == "male" else "shimmer"
            elif style in ["angelic", "angelical"]:
                openai_voice = "fable"
            elif style in ["robotic", "robotica", "robótica"]:
                openai_voice = None
        tts_debug["requested_voice_hint"] = openai_voice

        def _infer_voice_settings(txt: str, is_male: bool, style_tag: str) -> dict:
            t = (txt or "").lower()
            excls = t.count("!")
            qmarks = t.count("?")
            stress_words = [
                "meu deus", "pelo amor", "socorro", "urgente", "não acredito", "nao acredito",
                "absurdo", "tá doido", "ta doido", "sério", "serio", "para", "pare",
                "calma", "relaxa", "mentira", "que isso", "que é isso", "não", "nao",
            ]
            drama_hits = sum(1 for w in stress_words if w in t)
            excited = excls >= 2 or "!!" in (txt or "") or "??" in (txt or "")
            skeptical = qmarks >= 1 and ("como" in t or "por quê" in t or "porque" in t)
            tag = (style_tag or "").lower()
            base_style = 0.16
            base_stability = 0.74
            if excited or drama_hits >= 2:
                base_style = 0.24
                base_stability = 0.64
            elif drama_hits >= 1 or skeptical:
                base_style = 0.20
                base_stability = 0.68
            if "calma" in t or "relaxa" in t:
                base_style = 0.10
                base_stability = 0.84
            if any(k in tag for k in ["soft", "suave", "relax", "prayer", "oração", "oracao", "meditat"]):
                base_style = 0.09
                base_stability = 0.88
            if "young" in tag or "jovem" in tag:
                base_style = min(0.30, base_style + 0.04)
            if "mature" in tag or "madura" in tag or "indign" in tag or "angel" in tag:
                base_stability = min(0.88, base_stability + 0.06)
                base_style = max(0.08, min(base_style, 0.18))
            if not is_male:
                base_style = min(0.28, base_style + 0.02)
            return {
                "stability": float(max(0.55, min(0.90, base_stability))),
                "similarity_boost": 0.9,
                "style": float(max(0.08, min(0.32, base_style))),
                "use_speaker_boost": True,
            }

        def _infer_edge_prosody(txt: str, is_male: bool, style_tag: str) -> tuple:
            t = (txt or "").lower()
            excls = t.count("!")
            qmarks = t.count("?")
            drama = any(k in t for k in ["meu deus", "pelo amor", "socorro", "absurdo", "não acredito", "nao acredito"])
            calm = any(k in t for k in ["calma", "relaxa", "devagar"])
            tag = (style_tag or "").lower()
            rate = "+0%"
            pitch = "+0Hz"
            if calm:
                rate = "-4%"
                pitch = "-2Hz" if is_male else "-1Hz"
            elif drama or excls >= 2:
                rate = "+6%"
                pitch = "+2Hz" if not is_male else "+1Hz"
            elif qmarks >= 1:
                rate = "+3%"
                pitch = "+1Hz"
            if "young" in tag or "jovem" in tag:
                rate = "+4%" if rate == "+0%" else rate
                pitch = "+2Hz" if not is_male else "+1Hz"
            if "mature" in tag or "madura" in tag:
                rate = "-2%" if rate == "+0%" else rate
                pitch = "-2Hz" if is_male else "-1Hz"
            if any(k in tag for k in ["soft", "suave", "relax", "prayer", "oração", "oracao", "meditat"]):
                rate = "-10%"
                pitch = "-2Hz" if is_male else "-1Hz"
            volume = "+0%"
            if calm:
                volume = "-4%"
            elif drama or excls >= 2:
                volume = "+4%"
            elif qmarks >= 1:
                volume = "+2%"
            if any(k in tag for k in ["soft", "suave", "relax", "prayer", "oração", "oracao", "meditat"]):
                volume = "-6%"
            return rate, pitch, volume

        def _edge_ssml(txt: str, voice_name: str, rate: str, pitch: str, volume: str, lang_tag: str) -> str:
            import html
            t = (txt or "").strip()
            if not t:
                t = "..."
            t = t.replace("...", "…")
            parts = re.split(r"([,.;:!?…]+)", t)
            out = []
            for p in parts:
                if not p:
                    continue
                if re.fullmatch(r"[,.;:!?…]+", p):
                    out.append(html.escape(p))
                    ms = 120 if "," in p else 220
                    if "…" in p:
                        ms = 520
                    elif "!" in p:
                        ms = 320
                    elif "?" in p:
                        ms = 280
                    elif any(ch in p for ch in ".;:"):
                        ms = 240
                    out.append(f'<break time="{ms}ms"/>')
                else:
                    out.append(html.escape(p))
            body = "".join(out)
            return (
                f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="{lang_tag}">'
                f'<voice name="{voice_name}">'
                f'<prosody rate="{rate}" pitch="{pitch}" volume="{volume}">{body}</prosody>'
                f"</voice></speak>"
            )

        # 1. ElevenLabs/OpenAI TTS (ai_service tenta ElevenLabs primeiro, depois OpenAI)
        # Importante: não depende de OPENAI_API_KEY para usar ElevenLabs.
        if openai_voice and self.ai_service and hasattr(self.ai_service, "generate_audio"):
            try:
                print(f"Tentando TTS premium ({openai_voice})...")
                voice_settings = _infer_voice_settings(clean_text, is_male=(gender == "male"), style_tag=style)
                audio_content = None
                if hasattr(self.ai_service, "generate_audio_with_diagnostics"):
                    premium_debug = self.ai_service.generate_audio_with_diagnostics(clean_text, voice=openai_voice, voice_settings=voice_settings)
                    if isinstance(premium_debug, dict):
                        for key, value in premium_debug.items():
                            if key not in {"attempts", "audio_content"}:
                                tts_debug[key] = value
                        for attempt in premium_debug.get("attempts") or []:
                            if isinstance(attempt, dict):
                                tts_debug.setdefault("attempts", []).append(dict(attempt))
                        audio_content = premium_debug.get("audio_content")
                else:
                    audio_content = self.ai_service.generate_audio(clean_text, voice=openai_voice, voice_settings=voice_settings)
                    if audio_content:
                        tts_debug["provider_used"] = "premium_unknown"
                        _record_attempt("premium_unknown", "success", "Audio gerado via ai_service sem diagnostico detalhado.")
                if audio_content:
                    filename = f"{uuid.uuid4()}.mp3"
                    path = os.path.join(self.output_dir, filename)
                    with open(path, "wb") as f:
                        f.write(audio_content)
                    dur = 0.0
                    try:
                        dur = float(self._ffprobe_duration_seconds(path) or 0)
                    except Exception:
                        dur = 0.0
                    if os.path.exists(path) and os.path.getsize(path) > 500 and dur > 0.2:
                        tts_debug["provider_used"] = tts_debug.get("provider_used") or "premium_unknown"
                        tts_debug["final_audio_duration_sec"] = round(float(dur or 0.0), 2)
                        tts_debug["output_path"] = path
                        print(f"TTS premium sucesso: {path} ({dur:.2f}s)")
                        return path
                    _record_attempt(
                        str(tts_debug.get("provider_used") or "premium_unknown"),
                        "failed",
                        "Provider retornou bytes, mas o arquivo salvo ficou invalido.",
                        {"duration_sec": round(float(dur or 0.0), 2)},
                    )
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except Exception:
                        pass
            except Exception as e:
                _record_attempt("premium_pipeline", "failed", str(e))
                print(f"TTS premium falhou, tentando fallback: {e}")

        # 3. Edge TTS (Qualidade Natural Gratuita - Microsoft)
        if style not in ["robotic", "robotica", "robótica"]:
            try:
                print("Tentando Edge TTS...")
                import edge_tts
                import asyncio
                import threading
                
                if lang == 'pt':
                    if gender == "male":
                        voice = "pt-BR-AntonioNeural"
                    else:
                        voice = "pt-BR-FranciscaNeural"
                    lang_tag = "pt-BR"
                else:
                    if gender == "male":
                        voice = "en-US-ChristopherNeural"
                    else:
                        voice = "en-US-JennyNeural"
                    lang_tag = "en-US"

                filename = f"{uuid.uuid4()}.mp3"
                path = os.path.join(self.output_dir, filename)
                rate, pitch, volume = _infer_edge_prosody(clean_text, is_male=(gender == "male"), style_tag=style)
                ssml = _edge_ssml(clean_text, voice_name=voice, rate=rate, pitch=pitch, volume=volume, lang_tag=lang_tag)

                async def _run_edge_tts():
                    communicate = edge_tts.Communicate(ssml, voice)
                    await asyncio.wait_for(communicate.save(path), timeout=90)
                    
                t = threading.Thread(target=lambda: asyncio.run(_run_edge_tts()))
                t.start()
                t.join(timeout=95)
                if t.is_alive():
                    raise TimeoutError("Edge TTS timeout")

                ffprobe_available = bool(tts_debug.get("ffprobe_available"))
                file_size = 0
                if os.path.exists(path):
                    try:
                        file_size = int(os.path.getsize(path) or 0)
                    except Exception:
                        file_size = 0
                dur = 0.0
                try:
                    dur = float(self._ffprobe_duration_seconds(path) or 0)
                except Exception:
                    dur = 0.0
                estimated_dur = 0.0
                used_estimated_duration = False
                if file_size > 500 and dur <= 0.2 and not ffprobe_available:
                    estimated_dur = float(
                        self._estimate_text_duration_with_voice(
                            clean_text,
                            voice_style=style,
                            voice_gender=gender,
                        ) or 0.0
                    )
                    if estimated_dur > 0.2:
                        used_estimated_duration = True
                        dur = estimated_dur

                if os.path.exists(path) and file_size > 500 and dur > 0.2:
                    _record_attempt(
                        "edge_tts",
                        "success",
                        "Fallback Edge TTS gerou audio valido.",
                        {
                            "duration_sec": round(float(dur or 0.0), 2),
                            "duration_source": "estimated_from_text" if used_estimated_duration else "ffprobe",
                            "measured_duration_sec": round(float(self._ffprobe_duration_seconds(path) or 0.0), 2) if ffprobe_available else 0.0,
                            "estimated_duration_sec": round(float(estimated_dur or 0.0), 2) if used_estimated_duration else 0.0,
                            "ffprobe_available": ffprobe_available,
                            "file_size_bytes": file_size,
                        },
                    )
                    tts_debug["provider_used"] = "edge_tts"
                    tts_debug["fallback_used"] = True
                    tts_debug["edge_tts_file_size_bytes"] = file_size
                    tts_debug["edge_tts_duration_source"] = "estimated_from_text" if used_estimated_duration else "ffprobe"
                    tts_debug["edge_tts_measured_duration_sec"] = round(float(self._ffprobe_duration_seconds(path) or 0.0), 2) if ffprobe_available else 0.0
                    tts_debug["edge_tts_estimated_duration_sec"] = round(float(estimated_dur or 0.0), 2) if used_estimated_duration else 0.0
                    tts_debug["final_audio_duration_sec"] = round(float(dur or 0.0), 2)
                    tts_debug["output_path"] = path
                    print(f"Edge TTS sucesso: {path} ({dur:.2f}s)")
                    return path
                else:
                    _record_attempt(
                        "edge_tts",
                        "failed",
                        "Arquivo gerado invalido ou vazio.",
                        {
                            "duration_sec": round(float(dur or 0.0), 2),
                            "duration_source": "estimated_from_text" if used_estimated_duration else "ffprobe",
                            "estimated_duration_sec": round(float(estimated_dur or 0.0), 2),
                            "ffprobe_available": ffprobe_available,
                            "file_size_bytes": file_size,
                        },
                    )
                    print(f"Edge TTS gerou arquivo vazio ou falhou (Size check failed). Path: {path}")
            except Exception as e:
                 _record_attempt("edge_tts", "failed", str(e))
                 print(f"Edge TTS falhou: {e}")

        # 4. Fallback offline no Windows via System.Speech
        if os.name == "nt":
            try:
                import base64
                import subprocess

                print("Tentando Fallback Windows SAPI...")
                filename = f"{uuid.uuid4()}.wav"
                path = os.path.join(self.output_dir, filename)
                path_ps = path.replace("'", "''")
                text_ps = clean_text.replace("'", "''")
                desired_gender = "Male" if gender == "male" else "Female"
                culture_prefix = "pt" if lang == "pt" else "en"
                script = f"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voice = $synth.GetInstalledVoices() |
    ForEach-Object {{ $_.VoiceInfo }} |
    Where-Object {{ $_.Culture.Name -like '{culture_prefix}*' -and $_.Gender.ToString() -eq '{desired_gender}' }} |
    Select-Object -First 1
if (-not $voice) {{
    $voice = $synth.GetInstalledVoices() | ForEach-Object {{ $_.VoiceInfo }} | Select-Object -First 1
}}
if ($voice) {{
    $synth.SelectVoice($voice.Name)
}}
$synth.Rate = 0
$synth.Volume = 100
$synth.SetOutputToWaveFile('{path_ps}')
$synth.Speak('{text_ps}')
$synth.Dispose()
"""
                encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
                if proc.returncode != 0:
                    raise Exception((proc.stderr or proc.stdout or "System.Speech retornou erro").strip())

                dur = 0.0
                try:
                    dur = float(self._ffprobe_duration_seconds(path) or 0)
                except Exception:
                    dur = 0.0
                if dur <= 0.2:
                    dur = float(
                        self._estimate_text_duration_with_voice(
                            clean_text,
                            voice_style=style,
                            voice_gender=gender,
                        ) or 0.0
                    )
                if os.path.exists(path) and os.path.getsize(path) > 1000 and dur > 0.2:
                    _record_attempt("windows_sapi", "success", "Fallback offline local gerou audio valido.", {"duration_sec": round(float(dur or 0.0), 2)})
                    tts_debug["provider_used"] = "windows_sapi"
                    tts_debug["fallback_used"] = True
                    tts_debug["final_audio_duration_sec"] = round(float(dur or 0.0), 2)
                    tts_debug["output_path"] = path
                    print(f"Windows SAPI sucesso: {path} ({dur:.2f}s)")
                    return path
                _record_attempt("windows_sapi", "failed", "Arquivo WAV invalido ou vazio.", {"duration_sec": round(float(dur or 0.0), 2)})
            except Exception as e:
                _record_attempt("windows_sapi", "failed", str(e))
                print(f"Windows SAPI falhou: {e}")

        # 4. Fallback gTTS (Robótico)
        try:
            from gtts import gTTS
            print("Tentando Fallback gTTS (Robótico)...")
            tts = gTTS(text=clean_text, lang=lang)
            filename = f"{uuid.uuid4()}.mp3"
            path = os.path.join(self.output_dir, filename)
            tts.save(path)
            
            # Verificação de segurança
            dur = 0.0
            try:
                dur = float(self._ffprobe_duration_seconds(path) or 0)
            except Exception:
                dur = 0.0
            if os.path.exists(path) and os.path.getsize(path) > 100 and dur > 0.2:
                _record_attempt("gtts", "success", "Fallback gTTS gerou audio valido.", {"duration_sec": round(float(dur or 0.0), 2)})
                tts_debug["provider_used"] = "gtts"
                tts_debug["fallback_used"] = True
                tts_debug["final_audio_duration_sec"] = round(float(dur or 0.0), 2)
                tts_debug["output_path"] = path
                print(f"gTTS sucesso: {path} ({dur:.2f}s)")
                return path
            else:
                 _record_attempt("gtts", "failed", "Arquivo gerado vazio ou invalido.", {"duration_sec": round(float(dur or 0.0), 2)})
                 tts_debug["error_summary"] = self._summarize_tts_failure(tts_debug)
                 print("gTTS gerou arquivo vazio.")
                 return None
        except Exception as e:
            _record_attempt("gtts", "failed", str(e))
            tts_debug["error_summary"] = self._summarize_tts_failure(tts_debug)
            print(f"Erro no TTS Final (gTTS): {e}")
            return None

    def download_image(self, url, retries=3, timeout=20):
        import time
        try:
            import imghdr
        except ImportError:
            imghdr = None
        
        for attempt in range(retries):
            try:
                print(f"Baixando imagem de: {url[:50]}... (Tentativa {attempt+1}/{retries}, timeout={timeout}s)")
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8'
                }
                response = requests.get(url, headers=headers, stream=True, timeout=timeout)
                
                if response.status_code == 200:
                    filename = f"genimg_{uuid.uuid4().hex}.png"
                    filepath = os.path.join(self.generated_dir, filename)
                    
                    with open(filepath, 'wb') as f:
                        for chunk in response.iter_content(4096):
                            f.write(chunk)
                
                    # Verificação de tamanho
                    file_size = os.path.getsize(filepath)
                    if file_size < 1000: # 1KB mínimo
                        print(f"AVISO: Imagem muito pequena ({file_size} bytes). Ignorando.")
                        try: os.remove(filepath)
                        except: pass
                        continue
                        
                    # Verificação de tipo de arquivo (Header)
                    try:
                        img_type = None
                        if imghdr:
                            img_type = imghdr.what(filepath)
                            
                        if not img_type and not filepath.lower().endswith('.svg'):
                            # Tenta abrir com PIL para confirmar
                            from PIL import Image
                            try:
                                with Image.open(filepath) as img:
                                    img.verify()
                            except:
                                print(f"AVISO: Arquivo baixado não é imagem válida. Ignorando.")
                                try: os.remove(filepath)
                                except: pass
                                continue
                    except:
                        pass
                    
                    return filepath
                elif response.status_code in [502, 503, 504, 429]:
                    print(f"Erro temporário ({response.status_code}). Retentando em 2s...")
                    time.sleep(2)
                    continue
                else:
                    print(f"Falha ao baixar imagem. Status: {response.status_code}")
                    # Se for 403/404, não adianta tentar muito
                    if response.status_code in [403, 404]:
                        break
            except Exception as e:
                print(f"Erro ao baixar imagem: {e}")
                time.sleep(1)
        
        return None

    def _generate_fallback_background(self, size):
        """Gera um fundo gradiente/texturizado localmente quando tudo falha"""
        try:
            from PIL import Image, ImageDraw
            import random
            
            width, height = size
            # Cria imagem base
            img = Image.new('RGB', (width, height), color=(20, 20, 20))
            draw = ImageDraw.Draw(img)
            
            # Cores com contraste e luminosidade média para evitar aspecto de tela preta.
            color_top = (random.randint(90, 150), random.randint(90, 160), random.randint(120, 210))
            color_bottom = (random.randint(30, 90), random.randint(30, 90), random.randint(60, 130))
            
            # Desenha gradiente vertical (linha por linha para simplicidade sem numpy)
            # Para performance em 1080p, desenhamos em baixa resolução e redimensionamos
            small_h = 256
            small_w = int(width * (small_h / height))
            small_img = Image.new('RGB', (small_w, small_h))
            small_draw = ImageDraw.Draw(small_img)
            
            for y in range(small_h):
                ratio = y / small_h
                r = int(color_top[0] + (color_bottom[0] - color_top[0]) * ratio)
                g = int(color_top[1] + (color_bottom[1] - color_top[1]) * ratio)
                b = int(color_top[2] + (color_bottom[2] - color_top[2]) * ratio)
                small_draw.line([(0, y), (small_w, y)], fill=(r, g, b))
            
            # Redimensiona para tamanho final com suavização
            img = small_img.resize((width, height), Image.BICUBIC)
            
            filename = f"genbg_{uuid.uuid4().hex}.png"
            filepath = os.path.join(self.generated_dir, filename)
            img.save(filepath)
            return filepath
        except Exception as e:
            print(f"Erro ao gerar fundo local: {e}")
            return None

    def _generate_local_background(self, text_fallback="", aspect_ratio="9:16"):
        """Compat shim: gera um fundo local simples quando a IA de imagem falha."""
        try:
            ratio = str(aspect_ratio or "9:16").strip()
            size = (1280, 720) if ratio == "16:9" else (720, 1280)
            return self._generate_fallback_background(size)
        except Exception as e:
            print(f"Erro ao gerar background local compatível: {e}")
            return None

    def _ensure_image_for_scene(
        self,
        prompt,
        text_fallback,
        aspect_ratio="9:16",
        status_callback=None,
        max_rounds=2,
        allow_non_ai_fallback=False,
        paid_call_guard=None,
    ):
        """
        Gera imagem por IA usando OpenAI e retorna o motivo exato quando falha.
        """

        def notify(msg):
            if status_callback:
                try:
                    status_callback(msg)
                except Exception:
                    pass

        if not prompt and text_fallback:
            prompt = f"Photorealistic cinematic photography representing this narration: {text_fallback[:220]}"

        base_prompt = (prompt or "").strip()
        if not base_prompt:
            raise Exception("Sem prompt de imagem válido para esta cena.")
        norm_bp = (base_prompt or "").strip().lower()
        strict_worship = any(k in norm_bp for k in ["christian", "worship", "gospel", "louvor", "jesus", "cristo", "cruz", "calvario", "golgota"])
        combined_identity_text = f"{base_prompt} {text_fallback or ''}".strip()
        has_jesus = bool(
            re.search(
                r"(?<!\w)(?:jesus(?:\s+christ)?|cristo|christ)(?!\w)",
                self._fold_text_for_matching(combined_identity_text),
            )
        )
        parts = [
            f"{base_prompt}. ",
            "Biblical cinematic realism, elegant composition, natural light, family-friendly, visually coherent with the narration. ",
        ]
        if strict_worship:
            parts.append("Respectful gospel atmosphere. ")
        if has_jesus:
            parts.append(
                "Identity lock for Jesus Christ: whenever Jesus is explicitly present, portray the same adult Middle Eastern Jewish man from first-century Judea, with clearly masculine presentation, shoulder-length dark brown hair, a natural full beard, a simple cream tunic, and a brown mantle; never gender-swap Jesus or portray Jesus as a woman. "
            )
        parts += [
            "Character identity lock: keep each person's face, age, presentation, hair, wardrobe, and facial-hair pattern internally coherent and distinct from every other character. ",
            "Do not accidentally copy a moustache or beard from a male character onto a female character, and do not merge facial traits between people. ",
            "Prefer medium or wide shot, realistic humans, natural anatomy, subtle emotion, professional color palette. ",
            "Avoid extreme facial close-up. No text, watermark or logo. ",
            "Negative prompt: horror, gore, occult, demons, skulls, cemetery, dystopian, sci-fi, robots, distorted anatomy, extra limbs, uncanny faces, gender-swapped Jesus, female Jesus, inconsistent character identity, moustache or beard copied onto a female character, mixed facial identity traits.",
        ]
        final_prompt = "".join(parts)
        self._last_image_prompt_debug = {
            "jesus_identity_lock_applied": has_jesus,
            "character_identity_lock_applied": True,
            "female_facial_hair_transfer_blocked": True,
            "final_prompt_length": len(final_prompt),
        }
        if not self.ai_service:
            raise Exception("AI Service não inicializado para geração de imagem.")

        # Uma solicitação paga por cena. O retry é sempre explícito e reutiliza
        # a mesma tarefa; não deixamos chamadas em threads continuarem depois de
        # timeout, pois isso poderia gerar uma imagem cobrada e disparar outra.
        _ = max_rounds  # compatibilidade com chamadas antigas; retries automáticos foram removidos.
        try:
            budget_state = paid_call_guard() if callable(paid_call_guard) else {}
            budget_suffix = ""
            if isinstance(budget_state, dict) and bool(budget_state.get("enabled")):
                budget_suffix = (
                    " Limite confirmado: imagem paga "
                    f"{int(budget_state.get('used_new_image_calls') or 0)}/"
                    f"{int(budget_state.get('allowed_new_image_calls') or 0)}."
                )
            notify("Gerando imagem com OpenAI (uma chamada protegida contra duplicação)..." + budget_suffix)
            url = self.ai_service.generate_image(
                final_prompt,
                aspect_ratio=aspect_ratio,
                providers=["openai_direct"],
                status_callback=notify,
            )
            path = self._resolve_input_image_path(url) if url else None
            if path and os.path.exists(path) and os.path.getsize(path) >= 1000:
                return path
            raise Exception("A OpenAI não retornou uma imagem utilizável.")
        except Exception:
            if allow_non_ai_fallback:
                notify("A imagem por IA falhou. Usando fundo local autorizado para concluir sem nova cobrança...")
                bg = self._generate_local_background(text_fallback=text_fallback, aspect_ratio=aspect_ratio)
                if bg and os.path.exists(bg) and os.path.getsize(bg) > 1000:
                    return bg
            raise

    def _set_clip_duration(self, clip, duration):
        """Compatível com MoviePy 1.x (set_duration) e 2.x (with_duration)."""
        if hasattr(clip, "with_duration"):
            return clip.with_duration(duration)
        return clip.set_duration(duration)

    def _set_clip_start(self, clip, start):
        """Compatível com MoviePy 1.x (set_start) e 2.x (with_start)."""
        if hasattr(clip, "with_start"):
            return clip.with_start(start)
        return clip.set_start(start)

    def _set_clip_audio(self, clip, audio_clip):
        """Compatível com MoviePy 1.x (set_audio) e 2.x (with_audio)."""
        if hasattr(clip, "with_audio"):
            return clip.with_audio(audio_clip)
        return clip.set_audio(audio_clip)

    def _assert_clip_not_none(self, clip, label: str, meta: Optional[dict] = None):
        if clip is None:
            extra = ""
            try:
                if meta:
                    extra = f" | meta={str(meta)[:600]}"
            except Exception:
                extra = ""
            raise Exception(f"Clip None detectado: {label}{extra}")

    def _clip_from_rgba(self, rgba_arr, duration, *, crop_transparent: bool = False):
        try:
            from moviepy.editor import ImageClip
        except Exception:
            from moviepy import ImageClip
        position = None
        if crop_transparent:
            try:
                alpha_source = rgba_arr[:, :, 3]
                ys, xs = alpha_source.nonzero()
                if len(xs) and len(ys):
                    padding = 4
                    x0 = max(0, int(xs.min()) - padding)
                    x1 = min(int(rgba_arr.shape[1]), int(xs.max()) + padding + 1)
                    y0 = max(0, int(ys.min()) - padding)
                    y1 = min(int(rgba_arr.shape[0]), int(ys.max()) + padding + 1)
                    rgba_arr = rgba_arr[y0:y1, x0:x1]
                    position = (x0, y0)
            except Exception:
                position = None
        rgb = rgba_arr[:, :, :3]
        alpha = (rgba_arr[:, :, 3].astype("float32") / 255.0)
        base = ImageClip(rgb)
        mask = None
        try:
            mask = ImageClip(alpha, ismask=True)
        except Exception:
            try:
                mask = ImageClip(alpha)
            except Exception:
                mask = None
        if mask is not None:
            if hasattr(base, "with_mask"):
                base = base.with_mask(mask)
            else:
                base = base.set_mask(mask)
        base = self._set_clip_duration(base, duration)
        if position is not None:
            base = self._clip_with_position(base, position)
        if mask is not None:
            try:
                mask = self._set_clip_duration(mask, duration)
            except Exception:
                pass
        return base

    def _subclip(self, clip, start_t, end_t):
        """Compatível com MoviePy 1.x/2.x e seus limites de ponto flutuante.

        O MoviePy aplica o mesmo ``end_t`` ao vídeo, áudio e máscara. Depois de
        concatenações, esses objetos podem terminar com diferenças inferiores
        a um frame e o MoviePy rejeita o corte mesmo exibindo durações iguais
        com duas casas decimais. Limitamos somente diferenças marginais; uma
        divergência real continua sendo levantada pela própria biblioteca.
        """
        self._last_subclip_clamp_debug = None
        try:
            requested_value = float(end_t) if end_t is not None else None
        except (TypeError, ValueError):
            requested_value = None

        if requested_value is not None:
            component_durations = []
            for name, component in (
                ("clip", clip),
                ("audio", getattr(clip, "audio", None)),
                ("mask", getattr(clip, "mask", None)),
            ):
                try:
                    duration = float(getattr(component, "duration", 0.0) or 0.0)
                except (TypeError, ValueError):
                    duration = 0.0
                if duration > 0:
                    component_durations.append((name, duration))

            if component_durations:
                limiting_component, available_end = min(
                    component_durations,
                    key=lambda item: item[1],
                )
                overshoot = requested_value - available_end
                if 0.0 < overshoot <= 0.02:
                    end_t = available_end
                    self._last_subclip_clamp_debug = {
                        "requested_end_sec": requested_value,
                        "clamped_end_sec": available_end,
                        "overshoot_sec": overshoot,
                        "limiting_component": limiting_component,
                    }

        if hasattr(clip, "subclip"):
            return clip.subclip(start_t, end_t)
        if hasattr(clip, "subclipped"):
            return clip.subclipped(start_t, end_t)
        raise AttributeError("Objeto de clip sem subclip/subclipped")

    def _synchronize_video_clip_duration(self, clip, target_duration: float):
        """Fit a MoviePy clip to the final audio timeline without black frames.

        Trims excess video or freezes the last valid frame when the visual
        timeline is short.  The final explicit duration assignment avoids
        MoviePy/container rounding from failing the pre-render validation.
        """

        self._assert_clip_not_none(clip, "duration_sync_input")
        target = float(target_duration or 0.0)
        current = float(getattr(clip, "duration", 0.0) or 0.0)
        if target <= 0 or current <= 0:
            raise Exception(
                f"Duracao invalida no ajuste final: video={current:.3f}s alvo={target:.3f}s."
            )

        original_audio = getattr(clip, "audio", None)
        action = "already_aligned"
        adjusted = clip
        delta_before = current - target

        if current > target + 0.001:
            adjusted = self._subclip(clip, 0, target)
            action = "trim_video"
        elif current < target - 0.001:
            extra = target - current
            hold = self._freeze_last_frame_clip(clip, extra)
            if hold is None:
                raise Exception(
                    f"Nao foi possivel prolongar o ultimo frame por {extra:.3f}s."
                )
            try:
                from moviepy.editor import concatenate_videoclips
            except ImportError:
                from moviepy import concatenate_videoclips
            adjusted = concatenate_videoclips([clip, hold], method="compose")
            action = "freeze_last_frame"

        if original_audio is not None:
            adjusted = self._set_clip_audio(adjusted, original_audio)
        adjusted = self._set_clip_duration(adjusted, target)
        obtained = float(getattr(adjusted, "duration", 0.0) or 0.0)
        if abs(obtained - target) > 0.02:
            raise Exception(
                f"Ajuste final nao convergiu: video={obtained:.3f}s alvo={target:.3f}s."
            )
        return adjusted, {
            "action": action,
            "duration_before_sec": round(current, 3),
            "target_duration_sec": round(target, 3),
            "duration_after_sec": round(obtained, 3),
            "delta_before_sec": round(delta_before, 3),
            "delta_after_sec": round(obtained - target, 3),
        }

    def _apply_ken_burns(self, clip, size, zoom_factor=1.15):
        """
        Aplica efeito suave de zoom (Ken Burns) em um ImageClip.
        """
        try:
            w, h = size
            # Função de transformação para zoom
            def resize_func(t):
                # Zoom linear de 1.0 até zoom_factor ao longo da duração do clip
                current_zoom = 1 + (zoom_factor - 1) * (t / clip.duration)
                return current_zoom

            # Aplica o resize animado e centraliza
            # Nota: Isso pode ser custoso para processar. Se der timeout, simplificar.
            # Alternativa mais leve: Apenas um crop variável se a imagem for maior que o vídeo
            zoomed = clip.resized(resize_func) if hasattr(clip, "resized") else clip.resize(resize_func)
            if hasattr(zoomed, "with_position"):
                return zoomed.with_position('center')
            return zoomed.set_position('center')
        except Exception as e:
            print(f"Erro ao aplicar Ken Burns: {e}")
            return clip

    def _clip_resize(self, clip, resize_value):
        if hasattr(clip, "resized"):
            return clip.resized(resize_value)
        return clip.resize(resize_value)

    def _clip_with_position(self, clip, position):
        if hasattr(clip, "with_position"):
            return clip.with_position(position)
        return clip.set_position(position)

    def _motion_plan_for_scene(self, scene_idx: int, total_scenes: int, reuse_count: int = 0, reused_visual: bool = False) -> Dict[str, Any]:
        if reused_visual:
            variants = [
                "push_in", "push_out", "slow_zoom", "pan_left", "pan_right", "tilt_up", "tilt_down",
                "drift", "parallax", "orbit_leve", "camera_breathing", "dolly_out",
                "leve_handheld", "slow_rotation", "depth_movement", "foreground_parallax",
                "rack_focus_digital",
            ]
        else:
            variants = [
                "push_in", "slow_zoom", "push_out", "pan_left", "pan_right",
                "tilt_up", "dolly_in", "drift", "depth_movement",
            ]
        effect_name = variants[(scene_idx + reuse_count) % len(variants)]
        zoom_factor = 1.06 + (((scene_idx + reuse_count) % 4) * 0.02)
        if reused_visual:
            zoom_factor = min(1.16, zoom_factor + 0.02)
        return {
            "name": effect_name,
            "zoom_factor": zoom_factor,
            "reused_visual": bool(reused_visual),
            "scene_number": int(scene_idx) + 1,
            "total_scenes": int(total_scenes),
        }

    def _plan_cinematic_visual_beats(
        self,
        duration_sec: float,
        *,
        max_hold_sec: float = DEFAULT_MAX_CINEMATIC_VISUAL_HOLD_SEC,
    ) -> List[Dict[str, Any]]:
        """Divide uma cena longa em cortes digitais sem gerar novas imagens.

        Os cortes usam a mesma imagem com enquadramentos e movimentos diferentes.
        Assim, a timeline oficial de áudio permanece intacta e não há nova chamada
        a provedores pagos apenas para melhorar o ritmo visual.
        """

        duration = max(0.0, float(duration_sec or 0.0))
        if duration <= 0:
            return []
        max_hold = max(3.0, float(max_hold_sec or DEFAULT_MAX_CINEMATIC_VISUAL_HOLD_SEC))
        beat_count = max(1, int(math.ceil(duration / max_hold)))
        beat_duration = duration / beat_count
        beats: List[Dict[str, Any]] = []
        cursor = 0.0
        for beat_index in range(beat_count):
            end = duration if beat_index == beat_count - 1 else min(duration, cursor + beat_duration)
            beats.append({
                "index": beat_index,
                "start": round(cursor, 6),
                "end": round(end, 6),
                "duration": round(max(0.0, end - cursor), 6),
            })
            cursor = end
        return beats

    def _memory_safe_visual_hold_seconds(self, duration_sec: float) -> float:
        """Limita a quantidade de composições simultâneas em vídeos longos.

        O movimento cinematográfico continua em cada trecho; somente evitamos
        manter dezenas de matrizes 720p extras em memória até o encode final.
        """
        duration = max(0.0, float(duration_sec or 0.0))
        if duration < 5 * 60:
            return DEFAULT_MAX_CINEMATIC_VISUAL_HOLD_SEC
        try:
            max_beats = int((os.getenv("VIDEO_LONG_MAX_VISUAL_BEATS") or "36").strip() or "36")
        except Exception:
            max_beats = 36
        max_beats = max(24, min(72, max_beats))
        return round(max(DEFAULT_MAX_CINEMATIC_VISUAL_HOLD_SEC, duration / max_beats), 3)

    def _motion_plan_override_from_scene(self, scene: Dict[str, Any], default_plan: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        if not isinstance(scene, dict):
            return default_plan
        base = dict(default_plan or {})
        raw_hint = " ".join(
            str(value or "").strip()
            for value in [
                scene.get("motion_effect"),
                scene.get("camera_movement"),
                (scene.get("scene_card") or {}).get("camera_framing") if isinstance(scene.get("scene_card"), dict) else "",
            ]
            if str(value or "").strip()
        ).lower()
        if not raw_hint:
            return default_plan

        effect_name = ""
        if any(term in raw_hint for term in ["push_in", "zoom in", "slow_zoom", "close-up", "close up"]):
            effect_name = "push_in"
        elif any(term in raw_hint for term in ["push_out", "zoom out", "dolly_out", "pull back"]):
            effect_name = "dolly_out"
        elif any(term in raw_hint for term in ["dolly_in", "reveal in"]):
            effect_name = "dolly_in"
        elif any(term in raw_hint for term in ["parallax", "depth", "crowd", "layered"]):
            effect_name = "parallax"
        elif any(term in raw_hint for term in ["pan left", "pan_left"]):
            effect_name = "pan_left"
        elif any(term in raw_hint for term in ["pan right", "pan_right"]):
            effect_name = "pan_right"
        elif any(term in raw_hint for term in ["tilt up", "tilt_up"]):
            effect_name = "tilt_up"
        elif any(term in raw_hint for term in ["tilt down", "tilt_down"]):
            effect_name = "tilt_down"
        elif any(term in raw_hint for term in ["handheld", "hand held"]):
            effect_name = "leve_handheld"
        elif any(term in raw_hint for term in ["drift", "float"]):
            effect_name = "drift"
        elif any(term in raw_hint for term in ["slow zoom", "zoom"]):
            effect_name = "slow_zoom"

        if not effect_name:
            return default_plan
        base["name"] = effect_name
        base["requested_by_scene"] = True
        if effect_name in {"push_in", "slow_zoom", "dolly_in"}:
            base["zoom_factor"] = max(float(base.get("zoom_factor") or 1.08), 1.10)
        elif effect_name in {"dolly_out", "push_out"}:
            base["zoom_factor"] = max(float(base.get("zoom_factor") or 1.08), 1.08)
        return base

    def _apply_motion_effect(self, clip, size, motion_plan: Optional[Dict[str, Any]] = None):
        plan = motion_plan or {}
        effect_name = str(plan.get("name") or "zoom_in").strip().lower()
        zoom_factor = float(plan.get("zoom_factor") or 1.10)
        if effect_name in {"zoom_in", "push_in", "dolly_in", "slow_zoom"}:
            return self._apply_ken_burns(clip, size, zoom_factor=zoom_factor)
        try:
            try:
                from moviepy.editor import CompositeVideoClip
            except ImportError:
                from moviepy import CompositeVideoClip

            width, height = size
            duration = max(0.1, float(getattr(clip, "duration", 0) or 0.1))

            def _progress(t: float) -> float:
                try:
                    return max(0.0, min(1.0, float(t) / duration))
                except Exception:
                    return 0.0

            if effect_name in {"zoom_out", "dolly_out", "push_out"}:
                start_zoom = max(1.08, zoom_factor + 0.05)
                end_zoom = max(1.0, zoom_factor - 0.04)

                def _resize_func(t: float):
                    p = _progress(t)
                    return start_zoom - ((start_zoom - end_zoom) * p)

                zoomed = self._clip_resize(clip, _resize_func)
                return self._clip_with_position(zoomed, "center")

            resized = self._clip_resize(clip, zoom_factor)
            extra_x = max(0.0, float(getattr(resized, "w", width) or width) - float(width))
            extra_y = max(0.0, float(getattr(resized, "h", height) or height) - float(height))

            def _position(t: float):
                p = _progress(t)
                if effect_name == "pan_left":
                    return (-extra_x * p, -extra_y / 2.0)
                if effect_name == "pan_right":
                    return (-extra_x * (1.0 - p), -extra_y / 2.0)
                if effect_name == "tilt_up":
                    return (-extra_x / 2.0, -extra_y * p)
                if effect_name == "tilt_down":
                    return (-extra_x / 2.0, -extra_y * (1.0 - p))
                if effect_name in {"parallax_soft", "parallax"}:
                    return (-extra_x * (0.2 + (0.6 * p)), -extra_y * (0.15 + (0.3 * (1.0 - p))))
                if effect_name == "drift_diag":
                    return (-extra_x * (0.15 + (0.55 * p)), -extra_y * (0.15 + (0.45 * p)))
                if effect_name in {"slow_drift", "drift"}:
                    return (-extra_x * (0.1 + (0.35 * p)), -extra_y * 0.35)
                if effect_name == "camera_breathing":
                    breathe = math.sin(p * math.pi * 2.0) * 0.08
                    return (-extra_x * (0.45 + breathe), -extra_y * (0.45 - breathe))
                if effect_name in {"orbit_soft", "orbit_leve", "slow_rotation"}:
                    orbit = math.sin(p * math.pi) * 0.28
                    return (-extra_x * (0.3 + orbit), -extra_y * (0.25 + (0.2 * (1.0 - p))))
                if effect_name == "leve_handheld":
                    shake_x = math.sin(p * math.pi * 3.0) * 0.05
                    shake_y = math.cos(p * math.pi * 2.0) * 0.03
                    return (-extra_x * (0.45 + shake_x), -extra_y * (0.45 + shake_y))
                if effect_name == "depth_movement":
                    depth = math.sin(p * math.pi) * 0.12
                    return (-extra_x * (0.18 + (0.52 * p)), -extra_y * (0.28 + depth))
                if effect_name == "foreground_parallax":
                    return (-extra_x * (0.05 + (0.75 * p)), -extra_y * (0.25 + (0.2 * p)))
                if effect_name == "rack_focus_digital":
                    wobble = math.sin(p * math.pi * 1.5) * 0.1
                    return (-extra_x * (0.4 + wobble), -extra_y * 0.4)
                return ("center", "center")

            moved = self._clip_with_position(resized, _position)
            composed = CompositeVideoClip([moved], size=size)
            return self._set_clip_duration(composed, duration)
        except Exception:
            return self._apply_ken_burns(clip, size, zoom_factor=zoom_factor)

    def _freeze_last_frame_clip(self, clip, duration):
        """Repete o ultimo frame valido para evitar padding preto quando o audio passa do video."""
        if clip is None:
            return None
        try:
            extra = float(duration or 0)
        except Exception:
            extra = 0
        if extra <= 0:
            return None
        try:
            try:
                from moviepy.editor import ImageClip
            except Exception:
                from moviepy import ImageClip
            clip_duration = float(getattr(clip, "duration", 0) or 0)
            if clip_duration <= 0:
                return None
            frame_t = max(0.0, clip_duration - 0.05)
            frame = clip.get_frame(frame_t)
            hold = ImageClip(frame)
            return self._set_clip_duration(hold, extra)
        except Exception as e:
            print(f"Aviso: nao foi possivel congelar o ultimo frame: {e}")
            return None

    def _resolve_input_image_path(self, value: str) -> str:
        v = (value or "").strip()
        if not v:
            return ""
        v = v.replace("\\", "/").split("?", 1)[0].split("#", 1)[0].strip()
        if not v:
            return ""

        if v.startswith("/static/"):
            rel = v.replace("/static/", "", 1).lstrip("/")
            candidate = os.path.join("app", "static", rel)
            if os.path.exists(candidate):
                return candidate

        if v.startswith("/generated_assets/"):
            rel = v.replace("/generated_assets/", "", 1).lstrip("/")
            candidate = os.path.join("generated_assets", rel)
            if os.path.exists(candidate):
                return candidate

        if v.startswith("static/"):
            candidate = os.path.join("app", v)
            if os.path.exists(candidate):
                return candidate

        if v.startswith("app/static/"):
            candidate = v
            if os.path.exists(candidate):
                return candidate

        if os.path.isabs(v) and os.path.exists(v):
            return v

        if v.startswith("http://") or v.startswith("https://"):
            try:
                path = self.download_image(v, retries=1)
                if path and os.path.exists(path) and os.path.getsize(path) > 1000:
                    return path
            except Exception:
                return ""

        candidate = os.path.join("app", "static", v.lstrip("/"))
        if os.path.exists(candidate):
            return candidate
        return ""

    def create_video_from_plan(self, plan, cover_image_path=None, aspect_ratio="9:16", progress_callback=None, voice_style=None, voice_gender=None, music_file_path=None):
        """Gera vídeo complexo com áudio e cenas a partir do plano da IA"""
        # Lazy imports: moviepy 1.x usa .editor, moviepy 2.x exporta direto de moviepy
        try:
            from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip, CompositeVideoClip, CompositeAudioClip, concatenate_audioclips, AudioClip
        except ImportError:
            from moviepy import ImageClip, concatenate_videoclips, AudioFileClip, CompositeVideoClip, CompositeAudioClip, concatenate_audioclips, AudioClip
        import numpy as np

        if progress_callback:
            progress_callback(0, "Iniciando composição do vídeo...")
            
        clips = []
        final_clip = None
        bg_music = None
        cinematic_visual_hold_sec = DEFAULT_MAX_CINEMATIC_VISUAL_HOLD_SEC
        allow_non_ai_fallback_raw = os.getenv("ALLOW_NON_AI_IMAGE_FALLBACK")
        allow_non_ai_fallback = str(allow_non_ai_fallback_raw or "").strip().lower() in {"1", "true", "yes", "on"}
        image_max_rounds = int((os.getenv("IMAGE_MAX_ROUNDS") or "2").strip() or "2")
        recovery_image_budget = RecoveryImageCallBudget(plan)
        paid_image_call_guard = recovery_image_budget.consume if recovery_image_budget.enabled else None
        image_cache = {}
        cached_temp_paths = set()
        fallback_bg_path = None
        use_single_bg = (os.getenv("VIDEO_SINGLE_BG") or "").strip().lower() in {"1", "true", "yes", "on"}
        kind_norm = str(plan.get("kind") or "").strip().lower() if isinstance(plan, dict) else ""
        allow_image_reuse = bool(plan.get("allow_image_reuse")) if isinstance(plan, dict) else False
        prefer_peaceful_music = bool(plan.get("prefer_peaceful_music")) if isinstance(plan, dict) else False
        video_bg_path = None
        video_bg_paths = []
        video_bg_frame = None
        used_image_urls = []
        used_image_url_set = set()

        def _track_image_path(p: str):
            try:
                if not p or not isinstance(p, str):
                    return
                pp = os.path.abspath(p)
                static_root = os.path.abspath(os.path.join("app", "static"))
                if not pp.startswith(static_root):
                    return
                rel = pp[len(static_root):].lstrip(os.sep).replace(os.sep, "/")
                if not rel:
                    return
                url = f"/static/{rel}"
                if url not in used_image_url_set:
                    used_image_url_set.add(url)
                    used_image_urls.append(url)
            except Exception:
                return
        
        debug_ctx = {
            "stage": "init",
            "scene_index": None,
            "scene_count": None,
            "bg_image_path": None,
            "audio_path": None,
            "title_audio_path": None,
            "end_audio_path": None,
            "cover_image_path": cover_image_path,
            "aspect_ratio": aspect_ratio,
            "video_size": None,
        }
        render_report: Dict[str, Any] = {
            "original_script": {"title": None, "scenes": []},
            "narration_for_tts": [],
            "narration_plan": {},
            "audio_generation": {},
            "sync_validation": {},
            "text_integrity": {},
            "utf8_audit": {},
            "branding": {},
            "visual_plan": {},
            "scene_visuals": [],
            "effects_applied": [],
        }
        try:
            title = plan.get('title', 'Vídeo Sem Título')
            render_report["original_script"]["title"] = title
            try:
                plan = self.review_plan(plan)
            except Exception:
                pass
            cinematic_v2_meta = plan.get("cinematic_engine_v2") if isinstance(plan, dict) and isinstance(plan.get("cinematic_engine_v2"), dict) else {}
            branding_profile = self._resolve_channel_branding(plan if isinstance(plan, dict) else {})
            render_report["branding"] = dict(branding_profile)
            force_single_bg = bool(plan.get("single_bg")) or str(plan.get("image_mode") or "").strip().lower() == "single"
            if force_single_bg:
                use_single_bg = True
            raw_scenes = plan.get('scenes', [])
            
            # Validação extra: Se 'scenes' não for lista, tenta corrigir ou usa lista vazia
            if not isinstance(raw_scenes, list):
                print(f"ALERTA: 'scenes' não é lista. Tipo: {type(raw_scenes)}. Valor: {raw_scenes}")
                if isinstance(raw_scenes, str):
                    # Pode ser que a IA retornou uma string única como cena
                    raw_scenes = [{"text": raw_scenes, "image_prompt": ""}]
                else:
                    raw_scenes = []

            def _scene_prompt_for_fragment(base_prompt: str, fragment_text: str) -> str:
                frag = self._compact_narrative_moment(fragment_text, max_chars=100)
                bp = self._clean_image_prompt_seed(base_prompt, max_chars=150)
                if bp and frag:
                    return f"{bp}. Momento: {frag}"
                if bp:
                    return bp
                if frag:
                    return f"Personagem e ambiente coerentes. Momento: {frag}"
                return "Personagem e ambiente coerentes. Estilo cinematografico natural."

            def _materialize_scenes(raw_list):
                scenes_local = []
                if music_file_path:
                    return raw_list if isinstance(raw_list, list) else []
                if not isinstance(raw_list, list):
                    return []
                for scene in raw_list:
                    scene_text = ""
                    scene_prompt = ""

                    if isinstance(scene, str):
                        scene_text = scene
                    elif isinstance(scene, dict):
                        scene_payload = dict(scene)
                        scene_text = scene.get('text', '')
                        scene_prompt = scene.get('image_prompt', '')
                    else:
                        scene_text = str(scene)

                    scene_text = (scene_text or "").strip()
                    if not scene_text:
                        continue

                    if not scene_prompt and scene_text:
                        scene_prompt = f"Photorealistic cinematic photography representing: {scene_text[:140]}"

                    disable_split = False
                    try:
                        if isinstance(plan, dict):
                            v = plan.get("scene_text_split")
                            disable_split = bool(plan.get("disable_scene_text_split")) or str(v or "").strip().lower() in {"none", "off", "false", "0"}
                    except Exception:
                        disable_split = False

                    split_threshold = int((os.getenv("SCENE_TEXT_SPLIT_THRESHOLD") or "320").strip() or "320")
                    if disable_split:
                        split_threshold = 10**9
                    target_chars = int((os.getenv("SCENE_TEXT_TARGET_CHARS") or "240").strip() or "240")
                    target_chars = max(160, min(800, target_chars))

                    if len(scene_text) > split_threshold:
                        parts = re.split(r'(?<=[.!?])\s+', scene_text)
                        buf = ""
                        for part in parts:
                            p = (part or "").strip()
                            if not p:
                                continue
                            if not buf:
                                buf = p
                                continue
                            if len(buf) + 1 + len(p) <= target_chars:
                                buf = f"{buf} {p}"
                                continue
                            split_scene = dict(scene_payload) if isinstance(scene, dict) else {}
                            split_scene.update({
                                "text": buf.strip(),
                                "image_prompt": _scene_prompt_for_fragment(scene_prompt, buf.strip()),
                            })
                            split_scene["caption"] = str(split_scene.get("caption") or self._make_caption(buf.strip())).strip()
                            scenes_local.append(split_scene)
                            buf = p
                        if buf.strip():
                            split_scene = dict(scene_payload) if isinstance(scene, dict) else {}
                            split_scene.update({
                                "text": buf.strip(),
                                "image_prompt": _scene_prompt_for_fragment(scene_prompt, buf.strip()),
                            })
                            split_scene["caption"] = str(split_scene.get("caption") or self._make_caption(buf.strip())).strip()
                            scenes_local.append(split_scene)
                    else:
                        base_scene = dict(scene_payload) if isinstance(scene, dict) else {}
                        base_scene.update({
                            "text": scene_text,
                            "image_prompt": _scene_prompt_for_fragment(scene_prompt, scene_text),
                        })
                        scenes_local.append(base_scene)
                return scenes_local

            scenes = _materialize_scenes(raw_scenes)
            if not scenes and not music_file_path:
                alt_raw = plan.get("blocks") or plan.get("segments") or plan.get("parts") or plan.get("chapters") or []
                if isinstance(alt_raw, list) and alt_raw:
                    raw_scenes = alt_raw
                    scenes = _materialize_scenes(raw_scenes)

            if not scenes and not music_file_path:
                base_text = ""
                for k in (
                    "roteiro",
                    "script",
                    "text",
                    "content",
                    "story_content",
                    "narration_text",
                    "narration",
                    "raw_text",
                    "raw_script",
                    "full_text",
                ):
                    v = plan.get(k)
                    if isinstance(v, str) and v.strip():
                        base_text = v.strip()
                        break
                if base_text:
                    raw_scenes = [{"text": base_text, "image_prompt": plan.get("image_prompt") or ""}]
                    scenes = _materialize_scenes(raw_scenes)

            if not scenes and not music_file_path:
                fallback_text = ""
                try:
                    desc = (plan.get("description") or "").strip() if isinstance(plan, dict) else ""
                except Exception:
                    desc = ""
                if isinstance(title, str) and title.strip():
                    fallback_text = title.strip()
                if desc:
                    fallback_text = (fallback_text + "\n\n" + desc).strip()
                if not fallback_text:
                    fallback_text = "Conteúdo em preparação."
                scenes = [{"text": fallback_text, "image_prompt": plan.get("image_prompt") if isinstance(plan, dict) else ""}]

            for idx, scene in enumerate(scenes):
                if isinstance(scene, dict):
                    original_text = str(scene.get("text") or "").strip()
                    original_prompt = str(scene.get("image_prompt") or "").strip()
                else:
                    original_text = str(scene or "").strip()
                    original_prompt = ""
                    scenes[idx] = {"text": original_text, "image_prompt": original_prompt}
                    scene = scenes[idx]
                cleaned_tts = self._normalize_tts_text(original_text)
                estimated_sec = self._estimate_narration_seconds(cleaned_tts or original_text)
                scene["_tts_text"] = cleaned_tts
                scene["_estimated_narration_sec"] = estimated_sec
                render_report["original_script"]["scenes"].append({
                    "scene_number": idx + 1,
                    "original_text": original_text,
                    "image_prompt": original_prompt,
                })
                render_report["narration_for_tts"].append({
                    "scene_number": idx + 1,
                    "original_text": original_text,
                    "clean_text": cleaned_tts,
                    "estimated_duration_sec": estimated_sec,
                })

            # Enriquecimento: IA gera image_prompts profissionais com base na narração (imagens próprias para vídeo profissional)
            # Skip enrichment if music mode (handled by generator) or if images were preselected
            selected_raw_pre = plan.get("selected_images") or plan.get("images") or []
            has_preselected_images = isinstance(selected_raw_pre, list) and any(isinstance(x, str) and x.strip() for x in selected_raw_pre)
            if self.ai_service and scenes and not music_file_path and not has_preselected_images:
                try:
                    enriched = self.ai_service.enrich_scenes_with_image_prompts({"title": title, "scenes": scenes})
                    if enriched and enriched.get("scenes"):
                        scenes = enriched["scenes"]
                except Exception as e:
                    print(f"Aviso: enriquecimento de image_prompts falhou, usando prompts existentes: {e}")

            for idx, scene in enumerate(scenes):
                if not isinstance(scene, dict):
                    scenes[idx] = {"text": str(scene or "").strip(), "image_prompt": ""}
                    scene = scenes[idx]
                scene["_tts_text"] = self._normalize_tts_text(scene.get("text") or "")
                scene["_estimated_narration_sec"] = self._estimate_narration_seconds(scene.get("_tts_text") or scene.get("text") or "")

            narration_plan = self.prepare_final_narration_text(
                plan,
                scenes,
                voice_style=voice_style,
                voice_gender=voice_gender,
            )
            for idx, scene in enumerate(scenes):
                planned_scene_texts = narration_plan.get("scene_texts") or []
                planned_scene_estimates = narration_plan.get("scene_estimated_durations_sec") or []
                if idx < len(planned_scene_texts):
                    scene["_tts_text"] = self._normalize_tts_text(planned_scene_texts[idx] or scene.get("_tts_text") or scene.get("text") or "")
                if idx < len(planned_scene_estimates):
                    scene["_estimated_narration_sec"] = float(planned_scene_estimates[idx] or scene.get("_estimated_narration_sec") or 0.0)
                if idx < len(render_report["narration_for_tts"]):
                    render_report["narration_for_tts"][idx]["clean_text"] = scene.get("_tts_text") or ""
                    render_report["narration_for_tts"][idx]["estimated_duration_sec"] = round(float(scene.get("_estimated_narration_sec") or 0.0), 2)
            render_report["narration_plan"] = {
                "channel_name": narration_plan.get("channel_name"),
                "opening_text": narration_plan.get("opening_text"),
                "body_text": narration_plan.get("body_text"),
                "closing_text": narration_plan.get("closing_text"),
                "full_text": narration_plan.get("full_text"),
                "char_count": narration_plan.get("char_count"),
                "word_count": narration_plan.get("word_count"),
                "voice_words_per_minute": narration_plan.get("voice_words_per_minute"),
                "opening_duration_est_sec": narration_plan.get("opening_duration_est_sec"),
                "body_duration_est_sec": narration_plan.get("body_duration_est_sec"),
                "closing_duration_est_sec": narration_plan.get("closing_duration_est_sec"),
                "estimated_total_duration_sec": narration_plan.get("estimated_total_duration_sec"),
                "requested_duration_range_sec": narration_plan.get("requested_duration_range_sec"),
                "planning_attempts": narration_plan.get("planning_attempts") or [],
                "has_automatic_opening": bool(narration_plan.get("opening_text")),
                "has_automatic_closing": bool(narration_plan.get("closing_text")),
            }

            # Otimização de memória: Reduzir resolução para 720p para evitar OOM em tiers gratuitos
            if aspect_ratio == "16:9":
                video_size = (1280, 720) # Antes: 1920, 1080
            else:
                video_size = (720, 1280) # Antes: 1080, 1920
            debug_ctx["video_size"] = list(video_size)

            selected_image_paths = []
            selected_primary_path = None
            scene_image_pool = []
            scene_image_seen = set()
            scene_reuse_counts = {}
            selected_raw = plan.get("selected_images") or plan.get("images") or []
            if isinstance(selected_raw, list):
                for item in selected_raw:
                    if not isinstance(item, str):
                        continue
                    p = self._resolve_input_image_path(item)
                    if p and os.path.exists(p):
                        selected_image_paths.append(p)
                        if p not in scene_image_seen:
                            scene_image_pool.append(p)
                            scene_image_seen.add(p)
            if selected_image_paths:
                selected_primary_path = selected_image_paths[0]
                if force_single_bg:
                    selected_image_paths = [selected_primary_path]
                elif not music_file_path:
                    use_single_bg = False
            force_asset_reuse = False
            if isinstance(plan, dict):
                force_asset_reuse = bool(plan.get("force_reuse_assets") or plan.get("force_render_only"))
            if force_asset_reuse and isinstance(selected_raw, list) and selected_raw and not selected_image_paths:
                raise Exception("selected_images fornecidas, mas nenhuma imagem válida foi encontrada no disco para reutilização.")

            continuity_anchor = self._build_visual_continuity_anchor(title, scenes, plan if isinstance(plan, dict) else None)
            visual_group_plan = self._build_visual_groups(
                scenes,
                plan if isinstance(plan, dict) else None,
                ai_available=bool(self.ai_service),
                use_single_bg=bool(use_single_bg),
                selected_image_count=len(selected_image_paths),
            )
            for group in visual_group_plan.get("groups") or []:
                group["prompt"] = self._compose_visual_prompt_for_group(scenes, group.get("scene_indexes") or [], continuity_anchor)
            render_report["visual_plan"] = {
                "continuity_anchor": continuity_anchor,
                "requested_image_count": int(visual_group_plan.get("target_image_count") or 0),
                "group_count": len(visual_group_plan.get("groups") or []),
                "recovery_image_budget": recovery_image_budget.snapshot(),
                "cinematic_engine_v2": {
                    "enabled": bool(cinematic_v2_meta.get("enabled")),
                    "version": cinematic_v2_meta.get("version"),
                    "target_scene_count": cinematic_v2_meta.get("target_scene_count"),
                    "actual_scene_count": cinematic_v2_meta.get("actual_scene_count"),
                    "regenerated_scene_numbers": list(cinematic_v2_meta.get("regenerated_scene_numbers") or []),
                    "quality_control": cinematic_v2_meta.get("quality_control") if isinstance(cinematic_v2_meta.get("quality_control"), dict) else {},
                } if cinematic_v2_meta else {},
                "groups": [
                    {
                        "group_id": int(group.get("group_id") or 0),
                        "scene_numbers": [int(idx) + 1 for idx in (group.get("scene_indexes") or [])],
                        "semantic_summary": group.get("semantic_summary") or {},
                        "prompt": str(group.get("prompt") or "")[:600],
                    }
                    for group in (visual_group_plan.get("groups") or [])
                ],
                "scene_decisions": [
                    {
                        "scene_number": int(item.get("scene_index") or 0) + 1,
                        "decision": item.get("decision"),
                        "justification": item.get("justification"),
                    }
                    for item in (visual_group_plan.get("scene_decisions") or [])
                ],
            }
            group_lookup = {
                int(group.get("group_id") or 0): group
                for group in (visual_group_plan.get("groups") or [])
            }
            scene_to_group = {
                int(k): int(v)
                for k, v in (visual_group_plan.get("scene_to_group") or {}).items()
            }
            scene_decision_lookup = {
                int(item.get("scene_index") or 0): item
                for item in (visual_group_plan.get("scene_decisions") or [])
            }
            generated_group_paths: Dict[int, str] = {}
            generated_group_sources: Dict[int, str] = {}

            if use_single_bg and scenes and not music_file_path and not selected_image_paths:
                try:
                    first_txt = ""
                    s0 = scenes[0]
                    if isinstance(s0, dict):
                        first_txt = (s0.get("text") or "").strip()
                    else:
                        first_txt = str(s0).strip()

                    pool_size_raw = (os.getenv("VIDEO_BG_POOL_SIZE") or "").strip()
                    try:
                        pool_size = int(pool_size_raw) if pool_size_raw else 3
                    except Exception:
                        pool_size = 3
                    pool_size = max(1, min(5, pool_size))

                    base_for_bg = ((title or "") + "\n\n" + (first_txt or "")).strip()
                    if isinstance(plan, dict) and isinstance(plan.get("description"), str) and plan.get("description").strip():
                        base_for_bg = (base_for_bg + "\n\n" + plan.get("description").strip()).strip()

                    prompts = []
                    bg_prompt = plan.get("background_prompt") if isinstance(plan, dict) else None
                    if isinstance(bg_prompt, str) and bg_prompt.strip():
                        prompts = [bg_prompt.strip()]
                    elif self.ai_service and hasattr(self.ai_service, "generate_image_prompts_from_text"):
                        try:
                            prompts = self.ai_service.generate_image_prompts_from_text(
                                base_for_bg or title or first_txt or "",
                                count=pool_size,
                                kind=(plan.get("kind") if isinstance(plan, dict) else None),
                            )
                        except Exception:
                            prompts = []
                    if not prompts:
                        prompts = [
                            f"{title}. Photorealistic cinematic background representing the story. Warm natural light, pleasant mood. {first_txt[:220]}",
                            f"{title}. Photorealistic cinematic landscape illustrating the message. Realistic, peaceful, uplifting atmosphere. {first_txt[:220]}",
                            f"{title}. Photorealistic cinematic scene with realistic people, kind expressions, hopeful mood. {first_txt[:220]}",
                        ][:pool_size]

                    def _bg_status(message: str):
                        if progress_callback:
                            progress_callback(8, f"Fundo do vídeo: {message}")

                    video_bg_paths = []
                    for pidx, ptxt in enumerate(prompts[:pool_size]):
                        _bg_status(f"Gerando fundo {pidx+1}/{pool_size}...")
                        path = self._ensure_image_for_scene(
                            ptxt,
                            text_fallback=(first_txt or title)[:220],
                            aspect_ratio=aspect_ratio,
                            status_callback=_bg_status,
                            max_rounds=image_max_rounds,
                            allow_non_ai_fallback=allow_non_ai_fallback,
                            paid_call_guard=paid_image_call_guard,
                        )
                        if path:
                            video_bg_paths.append(path)
                            _track_image_path(path)
                            if path not in scene_image_seen:
                                scene_image_pool.append(path)
                                scene_image_seen.add(path)

                    if not video_bg_paths:
                        raise Exception("Falha ao gerar fundo do vídeo com OpenAI.")

                    if video_bg_paths:
                        video_bg_path = video_bg_paths[0]
                        if len(video_bg_paths) == 1:
                            video_bg_frame = self.create_text_image(
                                "",
                                size=video_size,
                                bg_color=(20, 20, 20),
                                text_color=(255, 255, 255),
                                bg_image_path=video_bg_path,
                            )
                        else:
                            video_bg_frame = None
                except Exception:
                    video_bg_path = None
                    video_bg_frame = None
                    video_bg_paths = []
                    raise

            # --- MODO MÚSICA ---
            if music_file_path and os.path.exists(music_file_path):
                if progress_callback:
                    progress_callback(10, "Modo Música: Preparando áudio e imagens...")
                
                # Carregar áudio principal
                main_audio = AudioFileClip(music_file_path)
                
                # Gerar clips visuais sincronizados
                for i, scene in enumerate(scenes):
                    if progress_callback:
                        progress_callback(10 + int((i / len(scenes)) * 60), f"Gerando cena {i+1}/{len(scenes)}...")
                    
                    # Duration comes from the plan
                    duration = scene.get('duration', 5)
                    image_prompt = scene.get('image_prompt', '')
                    if not image_prompt:
                         image_prompt = f"Scene for {title}, photorealistic, cinematic"

                    def _music_status(message, scene_idx=i, total=len(scenes)):
                        if progress_callback:
                            progress_callback(
                                10 + int((scene_idx / max(1, total)) * 60),
                                f"Cena {scene_idx+1}/{total}: {message}"
                            )

                    img_path = self._ensure_image_for_scene(
                        image_prompt,
                        text_fallback=title,
                        aspect_ratio=aspect_ratio,
                        status_callback=_music_status,
                        max_rounds=image_max_rounds,
                        allow_non_ai_fallback=allow_non_ai_fallback,
                        paid_call_guard=paid_image_call_guard,
                    )

                    if img_path and os.path.exists(img_path):
                        # Criar clip
                        try:
                            img_clip = ImageClip(img_path)
                            clip = self._set_clip_duration(img_clip, duration)
                            clip = self._apply_ken_burns(clip, video_size)
                            clips.append(clip)
                            # Não fechamos o clip aqui porque ele será concatenado depois
                        except Exception as e:
                            print(f"Erro ao criar clip da cena {i+1}: {e}")
                            if 'img_clip' in locals(): img_clip.close()
                    else:
                        raise Exception("Falha ao gerar imagem da cena musical com OpenAI.")
                    
                    # Limpeza de memória periódica em loops longos
                    if i % 5 == 0:
                        gc.collect()
                
                # Concatenar clips visuais
                if clips:
                    transition_sec = 0.25
                    if len(clips) > 1:
                        faded = []
                        for idx, c in enumerate(clips):
                            if idx > 0 and hasattr(c, "crossfadein"):
                                try:
                                    c = c.crossfadein(transition_sec)
                                except Exception:
                                    pass
                            faded.append(c)
                        clips = faded
                        try:
                            final_video = concatenate_videoclips(clips, method="compose", padding=-transition_sec)
                        except Exception:
                            final_video = concatenate_videoclips(clips, method="compose")
                    else:
                        final_video = concatenate_videoclips(clips, method="compose")
                    
                    # Ajustar áudio: Se vídeo for menor que áudio, corta áudio. Se vídeo for maior, loop ou corta vídeo.
                    # Vamos cortar o vídeo para bater com o áudio ou vice-versa.
                    # Preferência: Vídeo segue o áudio (mas o plano visual já foi calculado para bater aprox.)
                    
                    if final_video.duration > main_audio.duration:
                        final_video = self._subclip(final_video, 0, main_audio.duration)
                    else:
                        # Se vídeo ficou menor (arredondamentos), corta o áudio
                        main_audio = self._subclip(main_audio, 0, final_video.duration)
                        
                    final_video = self._set_clip_audio(final_video, main_audio)
                    
                    # Exportar
                    output_filename = f"music_video_{uuid.uuid4().hex}.mp4"
                    output_path = os.path.join(self.output_dir, output_filename)
                    
                    if progress_callback:
                        progress_callback(80, "Renderizando vídeo final...")
                        
                    self._dbg_event("H1", "write_videofile start (music)", {"output_path": output_path})
                    final_video.write_videofile(
                        output_path,
                        fps=24,
                        codec='libx264',
                        audio_codec='aac',
                        threads=1,
                        ffmpeg_params=["-preset", "ultrafast", "-movflags", "+faststart", "-pix_fmt", "yuv420p"]
                    )
                    try:
                        self._dbg_event("H1", "write_videofile done (music)", {
                            "output_path": output_path,
                            "exists": bool(os.path.exists(output_path)),
                            "size": int(os.path.getsize(output_path)) if os.path.exists(output_path) else 0,
                        })
                    except Exception:
                        pass
                    self._dbg_event("H1", "_ensure_playable_mp4 start (music)", {"output_path": output_path})
                    output_path = self._ensure_playable_mp4(output_path)
                    try:
                        self._dbg_event("H1", "_ensure_playable_mp4 done (music)", {
                            "output_path": output_path,
                            "exists": bool(os.path.exists(output_path)),
                            "size": int(os.path.getsize(output_path)) if os.path.exists(output_path) else 0,
                        })
                    except Exception:
                        pass
                    
                    # Cleanup
                    try:
                        main_audio.close()
                        final_video.close()
                        for c in clips: c.close()
                    except:
                        pass
                        
                    return {"video_url": f"{VIDEO_URL_PREFIX}/{output_filename}", "file_path": output_path}
                    
                else:
                    raise Exception("Nenhuma cena visual gerada para o clipe musical.")

            # --- MODO NORMAL (NARRADO) ---
            if progress_callback:
                progress_callback(5, "Planejando narracao final...")

            clean_title = title
            if "Music:" in clean_title:
                clean_title = clean_title.split("Music:")[0].strip()
            if "http" in clean_title:
                clean_title = clean_title.split("http")[0].strip()
            if len(clean_title) > 100:
                clean_title = clean_title[:97] + "..."

            planning_meta = render_report.get("narration_plan") or {}
            requested_range = planning_meta.get("requested_duration_range_sec") or {}
            min_requested_duration = float(requested_range.get("min_sec") or 0.0)
            max_requested_duration = float(requested_range.get("max_sec") or 0.0)
            target_requested_duration = float(requested_range.get("target_sec") or 0.0)
            range_tolerance = 0.05
            duration_range_report = {
                "requested_duration_min_sec": round(min_requested_duration, 2),
                "requested_duration_max_sec": round(max_requested_duration, 2),
                "requested_duration_target_sec": round(target_requested_duration, 2),
                "estimated_full_narration_duration_sec": round(float(planning_meta.get("estimated_total_duration_sec") or 0.0), 2),
                "actual_audio_duration_sec": 0.0,
                "range_reference_only": True,
                "attempted_replanning_after_real_audio": False,
                "kept_complete_narration": False,
                "decision": "pending",
                "decision_reason": "",
                "above_requested_range_sec": 0.0,
                "below_requested_range_sec": 0.0,
                "within_requested_range": False,
            }

            final_narration_text = str(planning_meta.get("full_text") or "").strip()
            if not final_narration_text:
                raise Exception("Falha ao montar a narracao final antes do TTS.")
            main_story_narration_text = " ".join(
                part for part in [
                    str(planning_meta.get("opening_text") or "").strip(),
                    str(planning_meta.get("body_text") or "").strip(),
                ]
                if part
            ).strip()
            cta_narration_text = str(planning_meta.get("cta_text") or planning_meta.get("closing_text") or "").strip()
            initial_opening_silence_sec = float(planning_meta.get("intro_opening_hold_sec") or DEFAULT_OPENING_SILENCE_SEC)
            pause_before_cta_sec = float(planning_meta.get("pause_duration_sec") or 1.25)
            render_report["audio_generation"] = {
                "final_text_sent_to_tts": final_narration_text,
                "text_char_count": len(final_narration_text),
                "text_word_count": self._count_words(final_narration_text),
                "cta_text_sent_to_tts": cta_narration_text,
                "initial_opening_silence_sec": round(initial_opening_silence_sec, 2),
                "pause_before_cta_sec": round(pause_before_cta_sec, 2),
            }

            main_audio_path = None
            main_audio_clip = None
            actual_total_audio_dur = 0.0
            planning_target_max_sec = float(planning_meta.get("planning_target_max_sec") or 0.0)
            real_audio_target_max_sec = planning_target_max_sec or (max_requested_duration * 0.97 if max_requested_duration > 0 else 0.0)
            seed_audio_used = False
            seed_audio_path = ""
            seed_audio_text = ""
            if isinstance(plan, dict):
                seed_audio_path = str(plan.get("seed_audio_path") or "").strip()
                seed_audio_text = str(plan.get("seed_narration_text") or "").strip()
            if seed_audio_path and os.path.exists(seed_audio_path) and os.path.getsize(seed_audio_path) > 1000:
                seed_audio_used = True
                main_audio_path = seed_audio_path
                debug_ctx["audio_path"] = main_audio_path
                debug_ctx["tts_provider_configured"] = "seed_reuse"
                debug_ctx["tts_provider_used"] = "seed_reuse"
                debug_ctx["tts_fallback_used"] = False
                try:
                    if seed_audio_text:
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
            render_report["visual_plan"]["opening_caption_suppressed"] = True
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

                scene_caption_timeline = list(scene_timeline_entry.get("caption_blocks") or [])
                expanded_scene_timeline = []
                for item in scene_caption_timeline:
                    expanded_scene_timeline.extend(
                        self._expand_caption_item_for_overlay(
                            item,
                            size=video_size,
                            max_lines=2,
                            reserved_bottom_ratio=CAPTION_SAFE_AREA_BOTTOM_RATIO,
                        )
                    )
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
                clips.append(clip_cta)
                render_report["cta_rendered"] = True
                render_report["visual_plan"]["cta_visual_mode"] = "cinematic_background_bridge"

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
            render_report["visual_plan"]["closing_caption_suppressed"] = True
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
                audio_caption_diff = abs(caption_duration - actual_total_audio_dur)
                video_sync_tolerance = duration_sync_tolerance_seconds(video_sync_target)
                sync_validation = {
                    "planned_text_duration_sec": round(float(estimated_total_duration or 0.0), 2),
                    "audio_duration_sec": round(float(actual_total_audio_dur or 0.0), 2),
                    "captions_duration_sec": round(float(caption_duration or 0.0), 2),
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
                    "opening_hook_starts_within_0_8_sec": bool(float(initial_opening_silence_sec or 0.0) <= 0.8),
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
                if not sync_validation["opening_hook_starts_within_0_8_sec"]:
                    raise Exception("Falha de validacao: abertura demorou mais de 0,8s para iniciar.")

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
