import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw


class SafeTextLayout:
    def __init__(
        self,
        *,
        size: Tuple[int, int],
        font_loader: Callable[[int], Any],
        safe_area: Optional[Dict[str, float]] = None,
    ) -> None:
        self.size = (int(size[0]), int(size[1]))
        self.font_loader = font_loader
        self.safe_area = safe_area or {"top": 0.08, "bottom": 0.08, "left": 0.08, "right": 0.08}
        self._canvas = Image.new("RGBA", self.size, (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self._canvas)

    def safe_rect(self, area: Optional[Dict[str, float]] = None) -> Tuple[int, int, int, int]:
        w, h = self.size
        ratios = dict(self.safe_area)
        ratios.update(area or {})
        left = int(w * max(0.0, float(ratios.get("left", 0.0))))
        top = int(h * max(0.0, float(ratios.get("top", 0.0))))
        right = w - int(w * max(0.0, float(ratios.get("right", 0.0))))
        bottom = h - int(h * max(0.0, float(ratios.get("bottom", 0.0))))
        return left, top, max(left + 1, right), max(top + 1, bottom)

    def _measure_text_width(self, text: str, font: Any) -> int:
        try:
            return int(self.draw.textlength(text, font=font))
        except Exception:
            bbox = self.draw.textbbox((0, 0), text, font=font)
            return int(bbox[2] - bbox[0])

    def _measure_text_height(self, text: str, font: Any) -> int:
        bbox = self.draw.textbbox((0, 0), text or "Ag", font=font)
        return int(max(1, bbox[3] - bbox[1]))

    def _normalize_lines(self, text: str) -> List[str]:
        parts = []
        for raw_line in str(text or "").splitlines():
            clean = re.sub(r"\s+", " ", str(raw_line or "").strip())
            if clean:
                parts.append(clean)
        return parts

    def wrap_text(self, text: str, font: Any, max_width: int) -> List[str]:
        normalized_lines = self._normalize_lines(text)
        if not normalized_lines:
            return []
        wrapped: List[str] = []
        for raw_line in normalized_lines:
            words = [word for word in raw_line.split() if word]
            if not words:
                continue
            current = ""
            for word in words:
                candidate = word if not current else f"{current} {word}"
                if self._measure_text_width(candidate, font) <= max_width:
                    current = candidate
                    continue
                if current:
                    wrapped.append(current)
                    current = word
                    continue
                wrapped.append(word)
            if current:
                wrapped.append(current)
        return wrapped

    def fit_text_block(
        self,
        *,
        text: str = "",
        fixed_lines: Optional[List[str]] = None,
        area: Optional[Dict[str, float]] = None,
        preferred_font_size: int = 48,
        min_font_size: int = 18,
        max_lines: int = 2,
        line_spacing_ratio: float = 1.20,
    ) -> Dict[str, Any]:
        left, top, right, bottom = self.safe_rect(area=area)
        area_width = max(1, right - left)
        area_height = max(1, bottom - top)
        normalized_fixed_lines = [
            re.sub(r"\s+", " ", str(line or "").strip())
            for line in list(fixed_lines or [])
            if re.sub(r"\s+", " ", str(line or "").strip())
        ]

        last_layout: Dict[str, Any] = {}
        for font_size in range(int(preferred_font_size), int(min_font_size) - 1, -2):
            font = self.font_loader(max(1, font_size))
            lines = list(normalized_fixed_lines) if normalized_fixed_lines else self.wrap_text(text, font, area_width)
            if not lines:
                lines = [re.sub(r"\s+", " ", str(text or "").strip())] if str(text or "").strip() else []
            widths = [self._measure_text_width(line, font) for line in lines] if lines else [0]
            text_height = self._measure_text_height("Ag", font)
            line_height = max(text_height, int(font_size * max(1.0, float(line_spacing_ratio))))
            block_height = max(0, len(lines) * line_height)
            fits = bool(
                lines
                and len(lines) <= max_lines
                and max(widths or [0]) <= area_width
                and block_height <= area_height
            )
            last_layout = {
                "fits": fits,
                "overflow_detected": not fits,
                "font": font,
                "font_size_used": int(font_size),
                "lines": lines,
                "line_count": len(lines),
                "line_height": line_height,
                "block_width": max(widths or [0]),
                "block_height": block_height,
                "area": {
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                    "width": area_width,
                    "height": area_height,
                },
                "text_fits": fits,
            }
            if fits:
                return last_layout
        if not last_layout:
            font = self.font_loader(max(1, int(min_font_size)))
            last_layout = {
                "fits": False,
                "overflow_detected": True,
                "font": font,
                "font_size_used": int(min_font_size),
                "lines": normalized_fixed_lines or ([str(text or "").strip()] if str(text or "").strip() else []),
                "line_count": len(normalized_fixed_lines or ([str(text or "").strip()] if str(text or "").strip() else [])),
                "line_height": max(1, int(min_font_size * max(1.0, float(line_spacing_ratio)))),
                "block_width": 0,
                "block_height": 0,
                "area": {
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                    "width": area_width,
                    "height": area_height,
                },
                "text_fits": False,
            }
        return last_layout

    def render_text_block(
        self,
        *,
        draw: Any,
        layout: Dict[str, Any],
        fill: Tuple[int, int, int, int],
        outline: Optional[Tuple[int, int, int, int]] = None,
        shadow: Optional[Tuple[int, int, int, int]] = None,
        start_y: Optional[int] = None,
        align: str = "center",
    ) -> Dict[str, Any]:
        area = dict(layout.get("area") or {})
        left = int(area.get("left", 0))
        top = int(area.get("top", 0))
        width = int(area.get("width", self.size[0]))
        font = layout.get("font")
        lines = list(layout.get("lines") or [])
        line_height = int(layout.get("line_height") or 0)
        block_height = int(layout.get("block_height") or 0)
        y = int(start_y if start_y is not None else top + max(0, int((int(area.get("height", self.size[1])) - block_height) / 2)))
        rendered_boxes: List[Dict[str, int]] = []
        for line in lines:
            line_width = self._measure_text_width(line, font)
            if str(align or "").lower() == "left":
                x = left
            else:
                x = left + max(0, int((width - line_width) / 2))
            if shadow is not None:
                draw.text((x + 2, y + 2), line, font=font, fill=shadow)
            if outline is not None:
                for off in [(2, 2), (-2, -2), (2, -2), (-2, 2), (0, 2), (2, 0), (-2, 0), (0, -2)]:
                    draw.text((x + off[0], y + off[1]), line, font=font, fill=outline)
            draw.text((x, y), line, font=font, fill=fill)
            rendered_boxes.append(
                {
                    "x": int(x),
                    "y": int(y),
                    "width": int(line_width),
                    "height": int(line_height),
                }
            )
            y += line_height
        result = dict(layout)
        result["rendered_boxes"] = rendered_boxes
        return result
