from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from codexia_v2_legacy_ui_bridge import BridgeError, check as bridge_check, enter as bridge_enter, exit_bridge


ROOT = Path(__file__).resolve().parent.parent

API_SCRIPTS = [
    "apply_consolidated_hardening.py",
    "apply_duration_confirmation_hardening.py",
    "apply_duration_seconds_support.py",
    "apply_openai_quality_cost_hardening.py",
    "apply_video_cost_backend_hardening.py",
    "apply_video_cost_ui_hardening.py",
    "apply_voice_closure_hardening.py",
    "apply_caption_integrity_self_heal.py",
    "apply_audio_timed_global_captions.py",
    "apply_final_visual_quality_gate_self_heal.py",
    "apply_recovery_checkpoint_hardening.py",
    "apply_final_render_recovery.py",
    "apply_final_render_recovery_compat.py",
    "apply_final_render_recovery_scope.py",
    "apply_production_manifest_hardening.py",
    "apply_narration_contract_hardening.py",
    "apply_manifest_diagnostics_hardening.py",
    "apply_manifest_asset_recovery_hardening.py",
    "apply_adaptive_render_threads_hardening.py",
    "apply_recovery_render_stall_hardening.py",
    "apply_recovery_render_stall_compat.py",
    "apply_render_watchdog_false_positive_fix.py",
    "apply_recoverable_archive_queue_hardening.py",
    "apply_recoverable_archive_queue_compat.py",
    "apply_final_quality_postrender_hardening.py",
    "apply_ready_video_asset_repair_v2.py",
    "apply_ready_video_asset_repair_v3.py",
    "apply_youtube_narration_gate.py",
    "apply_global_logo_only_visual_mode.py",
    "apply_review_quality_retry_hardening.py",
]

WORKER_SCRIPTS = [
    "apply_duration_confirmation_hardening.py",
    "apply_duration_seconds_support.py",
    "apply_openai_quality_cost_hardening.py",
    "apply_video_cost_backend_hardening.py",
    "apply_voice_closure_hardening.py",
    "apply_caption_integrity_self_heal.py",
    "apply_audio_timed_global_captions.py",
    "apply_final_visual_quality_gate_self_heal.py",
    "apply_recovery_checkpoint_hardening.py",
    "apply_final_render_recovery.py",
    "apply_final_render_recovery_compat.py",
    "apply_final_render_recovery_scope.py",
    "apply_production_manifest_hardening.py",
    "apply_narration_contract_hardening.py",
    "apply_manifest_asset_recovery_hardening.py",
    "apply_adaptive_render_threads_hardening.py",
    "apply_recovery_render_stall_hardening.py",
    "apply_recovery_render_stall_compat.py",
    "apply_render_watchdog_false_positive_fix.py",
    "apply_recoverable_archive_queue_hardening.py",
    "apply_recoverable_archive_queue_compat.py",
    "apply_final_quality_postrender_hardening.py",
    "apply_ready_video_asset_repair_v2.py",
    "apply_ready_video_asset_repair_v3.py",
    "apply_youtube_narration_gate.py",
    "apply_global_logo_only_visual_mode.py",
    "apply_review_quality_retry_hardening.py",
]


def _run(script_name: str, mode: str) -> None:
    script = ROOT / "scripts" / script_name
    if not script.is_file():
        raise RuntimeError(f"Hardening script ausente: {script_name}")
    print(f"CODEXIA_HARDENING: {script_name} {mode}", flush=True)
    subprocess.run([sys.executable, str(script), mode], cwd=str(ROOT), check=True)


def run_profile(profile: str) -> None:
    scripts = API_SCRIPTS if profile == "api" else WORKER_SCRIPTS
    entered = False
    try:
        bridge_enter()
        entered = True
        for script_name in scripts:
            _run(script_name, "--apply")
            _run(script_name, "--check")
        exit_bridge()
        entered = False
        bridge_check()
    finally:
        if entered:
            try:
                exit_bridge()
            except Exception as exc:
                print(f"CODEXIA_HARDENING: falha ao restaurar UI V2 após erro: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Executa o hardening oficial sem sobrescrever o shell Codexia V2.")
    parser.add_argument("--profile", choices=("api", "worker"), default="api")
    args = parser.parse_args()
    try:
        run_profile(args.profile)
    except (subprocess.CalledProcessError, RuntimeError, BridgeError) as exc:
        print(f"CODEXIA_HARDENING_ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
