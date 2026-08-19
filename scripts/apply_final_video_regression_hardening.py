from __future__ import annotations

import argparse
from pathlib import Path


YOUTUBE = Path("app/routers/youtube.py")
INDEX = Path("app/static/index.html")

YOUTUBE_IMPORT_ANCHOR = "from app.services.media_probe import media_durations_match, probe_media_file\n"
YOUTUBE_IMPORT_INSERT = (
    "from app.services.media_probe import media_durations_match, probe_media_file\n"
    "from app.services.video_cost_reporting import build_task_cost_summary\n"
    "from app.services.final_production_guard import install_openai_quality_policy_override\n"
    "\n"
    "# Qualidade final de imagem é determinística mesmo quando existe política antiga\n"
    "# persistida no banco (ex.: gpt-image-1-mini).\n"
    "install_openai_quality_policy_override()\n"
)

TASK_ANCHOR = (
    '@router.get("/task/{task_id}")\n'
    'def get_task_status(task_id: str):\n'
    '    task = get_task(task_id)\n'
    '    if not task:\n'
    '        raise HTTPException(status_code=404, detail="Tarefa não encontrada")\n'
    '    task = dict(task)\n'
)
TASK_INSERT = TASK_ANCHOR + (
    '    # Sempre expõe custos: estimativa antes/sem telemetria e chamadas rastreadas quando disponíveis.\n'
    '    task["cost_summary"] = build_task_cost_summary(task_id, task)\n'
)

SCRIPT_TAG = '    <script src="/static/js/video_quality_cost_panel.js"></script>\n'


def patch_youtube(text: str) -> str:
    if "from app.services.video_cost_reporting import build_task_cost_summary" not in text:
        if YOUTUBE_IMPORT_ANCHOR not in text:
            raise RuntimeError("youtube.py import anchor not found")
        text = text.replace(YOUTUBE_IMPORT_ANCHOR, YOUTUBE_IMPORT_INSERT, 1)
    if 'task["cost_summary"] = build_task_cost_summary(task_id, task)' not in text:
        if TASK_ANCHOR not in text:
            raise RuntimeError("youtube.py task endpoint anchor not found")
        text = text.replace(TASK_ANCHOR, TASK_INSERT, 1)
    return text


def patch_index(text: str) -> str:
    if "/static/js/video_quality_cost_panel.js" in text:
        return text
    if "</body>" not in text:
        raise RuntimeError("index.html body anchor not found")
    return text.replace("</body>", SCRIPT_TAG + "</body>", 1)


def check() -> None:
    youtube = YOUTUBE.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    required_youtube = (
        "from app.services.video_cost_reporting import build_task_cost_summary",
        "from app.services.final_production_guard import install_openai_quality_policy_override",
        "install_openai_quality_policy_override()",
        'task["cost_summary"] = build_task_cost_summary(task_id, task)',
    )
    missing = [item for item in required_youtube if item not in youtube]
    if missing:
        raise RuntimeError(f"final video regression hardening incomplete in youtube.py: {missing}")
    if "/static/js/video_quality_cost_panel.js" not in index:
        raise RuntimeError("video cost UI script not wired into index.html")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not YOUTUBE.exists() or not INDEX.exists():
        raise RuntimeError("Required target files are missing")
    if args.apply:
        YOUTUBE.write_text(patch_youtube(YOUTUBE.read_text(encoding="utf-8")), encoding="utf-8")
        INDEX.write_text(patch_index(INDEX.read_text(encoding="utf-8")), encoding="utf-8")
    if args.check or args.apply:
        check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
