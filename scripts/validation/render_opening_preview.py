import argparse
import json
import sys
from pathlib import Path

from PIL import Image

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.video_generator import VideoGenerator


def main():
    parser = argparse.ArgumentParser(description="Gera preview isolado da abertura do YouTube Auto.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--title", default="Despertar da Alma: A Jornada da Superação")
    parser.add_argument("--footer-text", default="")
    parser.add_argument("--output-dir", default="artifacts/preview_opening")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    generator = VideoGenerator(ai_service=None)
    layout_report = {}
    frame = generator.create_text_overlay(
        args.title,
        size=(int(args.width), int(args.height)),
        text_color=(255, 255, 255),
        footer_text=args.footer_text or None,
        max_lines=3,
        vertical_anchor="center",
        reserved_bottom_ratio=0.0,
        layout_report=layout_report,
        safe_area_override={"top": 0.08, "bottom": 0.08, "left": 0.08, "right": 0.08},
    )

    png_path = output_dir / "opening_preview.png"
    Image.fromarray(frame).save(png_path)

    mp4_path = output_dir / "opening_preview.mp4"
    try:
        try:
            from moviepy.editor import ImageClip
        except ImportError:
            from moviepy import ImageClip
        clip = ImageClip(frame)
        if hasattr(clip, "set_duration"):
            clip = clip.set_duration(4.0)
        else:
            clip = clip.with_duration(4.0)
        clip.write_videofile(str(mp4_path), fps=24, codec="libx264", audio=False, logger=None)
        clip.close()
    except Exception as exc:
        mp4_path = None
        (output_dir / "opening_preview_error.txt").write_text(str(exc), encoding="utf-8")

    report = {
        "resolution": f"{int(args.width)}x{int(args.height)}",
        "safe_area": {"top": 0.08, "bottom": 0.08, "left": 0.08, "right": 0.08},
        "text_fits": bool(layout_report.get("text_fits")),
        "overflow_detected": bool(layout_report.get("overflow_detected")),
        "font_size_used": int(layout_report.get("font_size_used") or 0),
        "line_count": int(layout_report.get("line_count") or 0),
        "layout": layout_report,
        "image": str(png_path),
        "video": str(mp4_path) if mp4_path else None,
    }
    (output_dir / "opening_preview_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
