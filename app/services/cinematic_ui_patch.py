from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional


ASYNC_SCRIPT_TAG = '<script src="/static/cinematic_async_director.js?v=20260914-async1"></script>'
# Backwards-compatible name used by the existing regression test.
SCRIPT_TAG = ASYNC_SCRIPT_TAG
PROJECT_SCRIPT_TAG = '<script src="/static/cinematic_project_state.js?v=20260916-state4"></script>'
MOBILE_NAV_SCRIPT_TAG = '<script src="/static/mobile_nav.js?v=20260916-mobile1"></script>'
_PROJECT_SCRIPT_RE = re.compile(r'<script src="/static/cinematic_project_state\.js\?v=[^"]+"></script>\s*')


def install_cinematic_async_ui(index_path: Optional[str | Path] = None) -> bool:
    """Inject resilient cinematic/mobile controllers without rewriting the V2 shell."""
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

        project_tags = _PROJECT_SCRIPT_RE.findall(html)
        normalized = [x.strip() for x in project_tags]
        if normalized != [PROJECT_SCRIPT_TAG]:
            cleaned = _PROJECT_SCRIPT_RE.sub("", html)
            if cleaned != html:
                html = cleaned
                changed = True

        for tag in (ASYNC_SCRIPT_TAG, PROJECT_SCRIPT_TAG, MOBILE_NAV_SCRIPT_TAG):
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
