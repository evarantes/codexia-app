import argparse
import json
import sys
from pathlib import Path

from PIL import Image

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.video_generator import VideoGenerator


def _write_case_preview(generator: VideoGenerator, output_dir: Path, label: str, text: str, width: int, height: int):
    layout_report = {}
    frame = generator.create_text_overlay(
        text,
        size=(width, height),
        text_color=(255, 255, 255),
        max_lines=2,
        vertical_anchor="bottom",
        reserved_bottom_ratio=0.08,
        layout_report=layout_report,
    )
    png_path = output_dir / f"caption_preview_{label}.png"
    Image.fromarray(frame).save(png_path)
    return {
        "label": label,
        "text": text,
        "image": str(png_path),
        "text_fits": bool(layout_report.get("text_fits")),
        "overflow_detected": bool(layout_report.get("overflow_detected")),
        "font_size_used": int(layout_report.get("font_size_used") or 0),
        "line_count": int(layout_report.get("line_count") or 0),
        "layout": layout_report,
    }


def main():
    parser = argparse.ArgumentParser(description="Gera previews de legenda para textos curto, médio e longo.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--output-dir", default="artifacts/preview_caption")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    generator = VideoGenerator(ai_service=None)
    cases = {
        "short": "Deus não se atrasa.",
        "medium": "Quando a fé permanece firme, o coração encontra direção e paz.",
        "long": "Mesmo quando tudo parece silencioso, Deus continua trabalhando em cada detalhe da história e sustentando quem decide perseverar.",
    }
    reports = [
        _write_case_preview(generator, output_dir, label, text, int(args.width), int(args.height))
        for label, text in cases.items()
    ]

    mp4_path = output_dir / "caption_preview.mp4"
    try:
        try:
            from moviepy.editor import ImageClip, concatenate_videoclips
        except ImportError:
            from moviepy import ImageClip, concatenate_videoclips
        clips = []
        for item in reports:
            img = str(item["image"])
            clip = ImageClip(img)
            if hasattr(clip, "set_duration"):
                clip = clip.set_duration(2.0)
            else:
                clip = clip.with_duration(2.0)
            clips.append(clip)
        final_clip = concatenate_videoclips(clips, method="compose")
        final_clip.write_videofile(str(mp4_path), fps=24, codec="libx264", audio=False, logger=None)
        final_clip.close()
        for clip in clips:
            clip.close()
    except Exception as exc:
        mp4_path = None
        (output_dir / "caption_preview_error.txt").write_text(str(exc), encoding="utf-8")

    summary = {
        "resolution": f"{int(args.width)}x{int(args.height)}",
        "safe_area": {"top": 0.08, "bottom": 0.08, "left": 0.06, "right": 0.06},
        "text_fits": all(bool(item.get("text_fits")) for item in reports),
        "overflow_detected": any(bool(item.get("overflow_detected")) for item in reports),
        "font_size_used": min(int(item.get("font_size_used") or 0) for item in reports),
        "line_count": max(int(item.get("line_count") or 0) for item in reports),
        "cases": reports,
        "video": str(mp4_path) if mp4_path else None,
    }
    (output_dir / "caption_preview_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
