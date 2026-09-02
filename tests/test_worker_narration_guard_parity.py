from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkerNarrationGuardParityTests(unittest.TestCase):
    def test_worker_applies_same_narration_guards_as_api(self) -> None:
        api = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        worker = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
        required = (
            "scripts/apply_youtube_narration_gate.py --apply",
            "scripts/apply_youtube_narration_gate.py --check",
            "scripts/apply_global_narration_contract.py --apply",
            "scripts/apply_global_narration_contract.py --check",
            "scripts/apply_spoken_text_boundary_v4.py --apply",
            "scripts/apply_spoken_text_boundary_v4.py --check",
            "scripts/apply_canonical_narration_logo_test_mode.py --apply",
            "scripts/apply_canonical_narration_logo_test_mode.py --check",
        )
        for needle in required:
            self.assertIn(needle, api, f"API sem contrato obrigatório: {needle}")
            self.assertIn(needle, worker, f"Worker sem contrato obrigatório: {needle}")

    def test_worker_guard_order_matches_api(self) -> None:
        api = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        worker = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
        for content, label in ((api, "API"), (worker, "worker")):
            gate = content.index("scripts/apply_youtube_narration_gate.py --apply")
            global_guard = content.index("scripts/apply_global_narration_contract.py --apply")
            spoken_boundary = content.index("scripts/apply_spoken_text_boundary_v4.py --apply")
            canonical = content.index("scripts/apply_canonical_narration_logo_test_mode.py --apply")
            compileall = content.index("python -m compileall -q app scripts")
            self.assertLess(gate, global_guard, f"gate/global fora de ordem no {label}")
            self.assertLess(global_guard, spoken_boundary, f"global/boundary fora de ordem no {label}")
            self.assertLess(spoken_boundary, canonical, f"boundary/canonical fora de ordem no {label}")
            self.assertLess(canonical, compileall, f"canonical/compileall fora de ordem no {label}")


if __name__ == "__main__":
    unittest.main()
