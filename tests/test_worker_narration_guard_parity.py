from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkerNarrationGuardParityTests(unittest.TestCase):
    def test_worker_and_api_use_same_core_adapter(self) -> None:
        api = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        worker = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
        required = (
            "scripts/apply_narration_contract_hardening.py --apply",
            "scripts/apply_narration_contract_hardening.py --check",
            "scripts/apply_youtube_narration_gate.py --apply",
            "scripts/apply_youtube_narration_gate.py --check",
            "python -m compileall -q app scripts",
        )
        for needle in required:
            self.assertIn(needle, api, f"API sem contrato obrigatório: {needle}")
            self.assertIn(needle, worker, f"Worker sem contrato obrigatório: {needle}")
        self.assertIn("narration_core.py", api)
        self.assertIn("Narration Core v1", worker)

    def test_legacy_narration_patch_layers_are_absent(self) -> None:
        api = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        worker = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
        legacy = (
            "scripts/apply_global_narration_contract.py --apply",
            "scripts/apply_spoken_text_boundary_v4.py --apply",
            "scripts/apply_canonical_narration_logo_test_mode.py --apply",
        )
        for content, label in ((api, "API"), (worker, "worker")):
            for needle in legacy:
                self.assertNotIn(needle, content, f"camada legada ainda ativa no {label}: {needle}")

    def test_core_adapter_order_matches_api_and_worker(self) -> None:
        api = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        worker = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
        for content, label in ((api, "API"), (worker, "worker")):
            adapter = content.index("scripts/apply_narration_contract_hardening.py --apply")
            gate = content.index("scripts/apply_youtube_narration_gate.py --apply")
            compileall = content.index("python -m compileall -q app scripts")
            self.assertLess(adapter, gate, f"adapter/gate fora de ordem no {label}")
            self.assertLess(gate, compileall, f"gate/compileall fora de ordem no {label}")


if __name__ == "__main__":
    unittest.main()
