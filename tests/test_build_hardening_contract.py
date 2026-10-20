"""Regression checks for the Codexia build-hardening contract.

These tests do not modify production files or require external services.
Run: python -m unittest discover -s tests -p 'test_build_hardening_contract.py'
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_build_hardening.py"


class BuildHardeningContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))

    def _scripts(self, name):
        for node in self.tree.body:
            if isinstance(node, ast.Assign):
                if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                    return ast.literal_eval(node.value)
        self.fail(f"{name} not found in hardening runner")

    def test_api_worker_have_duration_seconds_contract(self):
        for profile in ("API_SCRIPTS", "WORKER_SCRIPTS"):
            with self.subTest(profile=profile):
                scripts = self._scripts(profile)
                self.assertIn("apply_duration_confirmation_hardening.py", scripts)
                self.assertIn("apply_duration_seconds_support.py", scripts)
                self.assertLess(
                    scripts.index("apply_duration_confirmation_hardening.py"),
                    scripts.index("apply_duration_seconds_support.py"),
                )

    def test_api_worker_share_critical_recovery_and_quality_scripts(self):
        required = {
            "apply_caption_integrity_self_heal.py",
            "apply_final_visual_quality_gate_self_heal.py",
            "apply_recovery_checkpoint_hardening.py",
            "apply_narration_contract_hardening.py",
            "apply_manifest_asset_recovery_hardening.py",
            "apply_render_watchdog_false_positive_fix.py",
        }
        for profile in ("API_SCRIPTS", "WORKER_SCRIPTS"):
            with self.subTest(profile=profile):
                scripts = self._scripts(profile)
                self.assertFalse(required.difference(scripts), f"Missing: {required.difference(scripts)}")

    def test_all_referenced_hardening_scripts_exist(self):
        for profile in ("API_SCRIPTS", "WORKER_SCRIPTS"):
            for name in self._scripts(profile):
                with self.subTest(profile=profile, script=name):
                    self.assertTrue((ROOT / "scripts" / name).is_file(), name)

    def test_runner_checks_each_applied_script(self):
        runner = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('_run(script_name, "--apply")', runner)
        self.assertIn('_run(script_name, "--check")', runner)


if __name__ == "__main__":
    unittest.main()
