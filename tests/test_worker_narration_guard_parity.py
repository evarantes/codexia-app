from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_worker_applies_same_narration_guards_as_api() -> None:
    api = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    worker = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    required = (
        "scripts/apply_youtube_narration_gate.py --apply",
        "scripts/apply_youtube_narration_gate.py --check",
        "scripts/apply_global_narration_contract.py --apply",
        "scripts/apply_global_narration_contract.py --check",
    )
    for needle in required:
        assert needle in api, f"API sem contrato obrigatório: {needle}"
        assert needle in worker, f"Worker sem contrato obrigatório: {needle}"


def test_worker_guard_order_matches_api() -> None:
    api = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    worker = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    for content, label in ((api, "API"), (worker, "worker")):
        gate = content.index("scripts/apply_youtube_narration_gate.py --apply")
        global_guard = content.index("scripts/apply_global_narration_contract.py --apply")
        compileall = content.index("python -m compileall -q app scripts")
        assert gate < global_guard < compileall, f"Ordem incorreta dos guards no {label}"
