from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


SCRIPT_TAG = '<script src="/static/cinematic_async_director.js?v=20260914-async1"></script>'


def install_cinematic_async_ui(index_path: Optional[str | Path] = None) -> bool:
    """Inject the async director controller without rewriting the large V2 shell.

    This is intentionally idempotent. The Codexia V2 shell is a single legacy
    HTML file; keeping this patch isolated avoids risky wholesale rewrites while
    still letting the production image opt into the resilient controller.
    """
    if index_path is None:
        index_path = Path(__file__).resolve().parents[1] / "static" / "index.html"
    path = Path(index_path)
    try:
        if not path.is_file():
            return False
        html = path.read_text(encoding="utf-8")
        if SCRIPT_TAG in html:
            return False
        if "</body>" not in html:
            return False
        patched = html.replace("</body>", f"{SCRIPT_TAG}\n</body>", 1)
        tmp = path.with_suffix(path.suffix + ".async.tmp")
        tmp.write_text(patched, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as exc:
        print(f"Cinematic async UI patch warning: {exc}")
        return False
