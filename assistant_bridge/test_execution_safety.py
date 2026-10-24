"""Fast structural regression checks for execution safety, no paid API calls."""
import ast
from pathlib import Path

SOURCE = Path(__file__).with_name("execution.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def body_of(name):
    for item in TREE.body:
        if isinstance(item, ast.FunctionDef) and item.name == name:
            return ast.get_source_segment(SOURCE, item)
    raise AssertionError(f"Missing handler: {name}")


def test_every_private_handler_uses_strict_owner_check():
    for name in ("get_owned_project", "cancel_owned_project", "approve_owned_project",
                 "start_music", "music_status", "start_clip", "clip_status"):
        assert "is_project_owner(" in body_of(name), name


def test_paid_calls_require_trackable_task_ids_and_do_not_autopublish():
    music = body_of("start_music")
    clip = body_of("start_clip")
    assert 'result.get("task_id")' in music
    assert 'result.get("task_id")' in clip
    assert "RuntimeError(" in music
    assert "RuntimeError(" in clip
    assert '"auto_upload_youtube": False' in music
    assert '"auto_upload_youtube": False' in clip


def test_project_list_filters_by_owner_before_limit():
    listing = body_of("list_owned_projects")
    assert "json_extract(payload" in listing
    assert "LIMIT 500" in listing
