from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional


ASYNC_SCRIPT_TAG = '<script src="/static/cinematic_async_director.js?v=20260914-async1"></script>'
# Backwards-compatible name used by the existing regression test.
SCRIPT_TAG = ASYNC_SCRIPT_TAG
PROJECT_SCRIPT_TAG = '<script src="/static/cinematic_project_state.js?v=20260916-state3"></script>'
_PROJECT_SCRIPT_RE = re.compile(r'<script src="/static/cinematic_project_state\.js\?v=[^"]+"></script>\s*')


def install_cinematic_async_ui(index_path: Optional[str | Path] = None) -> bool:
    """Inject resilient cinematic controllers without rewriting the V2 shell."""
    if index_path is None:
        index_path = Path(__file__).resolve().parents[1] / "static" / "index.html"
    path = Path(index_path)
    try:
        if not path.is_file():
            return False
        html = path.read_text(encoding="utf-8")
        if "</body>" not in html:
            return False
        changed = False

        # Keep the current tag untouched for true idempotence. Only remove
        # stale versions so the page never loads two project controllers.
        matches = list(_PROJECT_SCRIPT_RE.finditer(html))
        stale_tags = [m.group(0) for m in matches if PROJECT_SCRIPT_TAG not in m.group(0)]
        if stale_tags:
            for stale in stale_tags:
                html = html.replace(stale, "")
            changed = True

        for tag in (ASYNC_SCRIPT_TAG, PROJECT_SCRIPT_TAG):
            if tag not in html:
                html = html.replace("</body>", f"{tag}\n</body>", 1)
                changed = True
        if not changed:
            return False
        tmp = path.with_suffix(path.suffix + ".async.tmp")
        tmp.write_text(html, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as exc:
        print(f"Cinematic UI patch warning: {exc}")
        return False
