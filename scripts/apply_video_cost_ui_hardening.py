#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

TARGET = Path("app/static/index.html")


class PatchError(RuntimeError):
    pass


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1)


def patch(text: str) -> str:
    text = replace_once(
        text,
        "</body>",
        '    <script src="/static/video_cost_control.js"></script>\n</body>',
        "load-cost-panel",
    )
    text = replace_once(
        text,
        '''                    if (this.ytStoryTaskId && this.ytStoryTask && String(this.ytStoryTask.status || '').toLowerCase() === 'failed') {
                        alert('Esta solicitação pode ser recuperada. Use “Reiniciar tarefa” para reaproveitar roteiro, imagens e áudio sem criar outro vídeo.');
                        return;
                    }
                    this.ytStoryVideoLoading = true;''',
        '''                    if (this.ytStoryTaskId && this.ytStoryTask && String(this.ytStoryTask.status || '').toLowerCase() === 'failed') {
                        alert('Esta solicitação pode ser recuperada. Use “Reiniciar tarefa” para reaproveitar roteiro, imagens e áudio sem criar outro vídeo.');
                        return;
                    }
                    if (window.CodexiaVideoCost && !await window.CodexiaVideoCost.beforeGenerate(this)) return;
                    this.ytStoryVideoLoading = true;''',
        "cost-before-paid-submit",
    )
    text = replace_once(
        text,
        '''                            duration_override_approved: durationOverrideApproved,
                            auto_upload: !!this.ytStoryAutoUpload,''',
        '''                            duration_override_approved: durationOverrideApproved,
                            production_mode: window.CodexiaVideoCost ? window.CodexiaVideoCost.mode() : 'balanced',
                            max_cost_brl: window.CodexiaVideoCost ? window.CodexiaVideoCost.maxCostBrl() : null,
                            cost_override_approved: window.CodexiaVideoCost ? window.CodexiaVideoCost.overrideApproved() : false,
                            auto_upload: !!this.ytStoryAutoUpload,''',
        "send-cost-control",
    )
    return text


def run(write: bool) -> None:
    original = TARGET.read_text(encoding="utf-8")
    transformed = patch(original)
    if write and transformed != original:
        TARGET.write_text(transformed, encoding="utf-8")
    if patch(transformed) != transformed:
        raise PatchError("patch não idempotente")
    print("Video cost UI hardening OK")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply ou --check")
    try:
        run(bool(args.apply))
    except PatchError as exc:
        print(f"ERRO VIDEO COST UI: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
