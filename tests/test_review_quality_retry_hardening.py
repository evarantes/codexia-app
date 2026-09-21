from pathlib import Path


def test_review_quality_retry_hardening_is_in_build_and_clears_rejected_assets():
    root = Path(__file__).resolve().parents[1]
    build = (root / "scripts" / "run_build_hardening.py").read_text(encoding="utf-8")
    hardening = (root / "scripts" / "apply_review_quality_retry_hardening.py").read_text(encoding="utf-8")

    assert build.count('"apply_review_quality_retry_hardening.py"') == 2
    assert "quality_correction_required" in hardening
    assert "fresh_quality_regeneration" in hardening
    assert 'prepared["force_reuse_assets"] = False' in hardening
    assert 'prepared["force_render_only"] = False' in hardening
    assert '"seeded_script",' in hardening
    assert '"selected_images",' in hardening
    assert '"reuse_audio_from",' in hardening
    assert "final_render_recovery = None" in hardening
    assert 'message.startswith("reprovado na revisão:")' in hardening
    assert '_is_review_quality_retry_payload(payload, getattr(row, "message", ""))' in hardening
    assert '_is_review_quality_retry_payload(payload, (task or {}).get("message"))' in hardening
