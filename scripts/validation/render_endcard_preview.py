import argparse
import json
import os
import sys
from pathlib import Path

from PIL import Image

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.video_generator import VideoGenerator


def main():
    parser = argparse.ArgumentParser(description="Gera preview isolado do encerramento do YouTube Auto.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--channel-name", default="HERDEIROS DAS PROMESSAS! ONDE A FÉ SE TORNA ATITUDE!")
    parser.add_argument("--channel-slogan", default="ONDE A FÉ SE TORNA ATITUDE!")
    parser.add_argument("--background-path", default="")
    parser.add_argument("--output-dir", default="artifacts/preview_endcard")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    generator = VideoGenerator(ai_service=None)
    branding = generator._resolve_channel_branding(
        {
            "channel_name": args.channel_name,
            "channel_slogan": args.channel_slogan,
        }
    )
    if args.background_path:
        branding["closing_image_path"] = args.background_path

    layout_report = {}
    frame = generator._build_cinematic_endcard_frame(
        branding,
        background_path=branding.get("closing_image_path"),
        size=(int(args.width), int(args.height)),
        layout_report=layout_report,
    )

    png_path = output_dir / "endcard_preview.png"
    Image.fromarray(frame).save(png_path)

    mp4_path = output_dir / "endcard_preview.mp4"
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
        (output_dir / "endcard_preview_error.txt").write_text(str(exc), encoding="utf-8")

    report = {
        "resolution": f"{int(args.width)}x{int(args.height)}",
        "safe_area": {"top": 0.08, "bottom": 0.08, "left": 0.08, "right": 0.08},
        "text_fits": bool(layout_report.get("text_fits")),
        "overflow_detected": bool(layout_report.get("overflow_detected")),
        "font_size_used": int(layout_report.get("font_size_used") or 0),
        "line_count": int(layout_report.get("line_count") or 0),
        "channel_name": branding.get("channel_name"),
        "channel_slogan": branding.get("channel_slogan"),
        "channel_title_lines": branding.get("channel_title_lines"),
        "layout": layout_report,
        "image": str(png_path),
        "video": str(mp4_path) if mp4_path else None,
    }
    (output_dir / "endcard_preview_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
