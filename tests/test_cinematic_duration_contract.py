from __future__ import annotations

from app.modules.bible_video_factory.editorial_intelligence import EditorialIntelligenceService
from app.services.cinematic_director import DirectorResult
from app.services.cinematic_duration_contract import build_duration_contract, enforce_director_duration_contract
from app.services.director_editorial_lock import install_director_editorial_lock


def _words(count: int) -> str:
    return " ".join(f"palavra{i}" for i in range(count))


def _plan_with_words(count: int, duration_seconds: int = 600):
    first = count // 2
    second = count - first
    return {
        "theme": "Teste",
        "content_type": "devotional",
        "full_script": "texto paralelo que deve ser substituído",
        "scenes": [
            {
                "index": 1,
                "narration": _words(first),
                "purpose": "hook",
                "tier": "C",
                "visual_prompt": "calm dawn",
                "motion_prompt": "slow zoom",
                "recommended_provider": "still",
                "target_seconds": duration_seconds // 2,
                "generative_video_seconds": 0,
                "retention_device": "",
            },
            {
                "index": 2,
                "narration": _words(second),
                "purpose": "prayer",
                "tier": "C",
                "visual_prompt": "quiet landscape",
                "motion_prompt": "slow zoom",
                "recommended_provider": "still",
                "target_seconds": duration_seconds - duration_seconds // 2,
                "generative_video_seconds": 0,
                "retention_device": "",
            },
        ],
        "quality_checks": {},
    }


class NoNetworkDirector:
    def _estimate_cost_usd(self, usage):
        return 0.0


def test_duration_contract_accepts_ten_minute_narration(monkeypatch):
    monkeypatch.setenv("CODEXIA_DIRECTOR_WPM", "146")
    monkeypatch.setenv("CODEXIA_DIRECTOR_DURATION_TOLERANCE", "0.04")
    plan = _plan_with_words(1460)
    # Make the reviewed scene narration the canonical full script, as the
    # enforcement step does before approval.
    plan["full_script"] = "\n\n".join(scene["narration"] for scene in plan["scenes"])
    contract = build_duration_contract(plan, duration_minutes=10)
    assert contract["validated"] is True
    assert contract["target_seconds"] == 600
    assert contract["actual_words"] == 1460
    assert contract["estimated_seconds"] == 600


def test_enforcement_replaces_hidden_parallel_script_without_paid_repair(monkeypatch):
    monkeypatch.setenv("CODEXIA_DIRECTOR_WPM", "146")
    monkeypatch.setenv("CODEXIA_DIRECTOR_DURATION_TOLERANCE", "0.04")
    monkeypatch.setenv("CODEXIA_DIRECTOR_DURATION_REPAIR_ATTEMPTS", "0")
    plan = _plan_with_words(1460)
    result = DirectorResult(
        plan=plan,
        provider="anthropic",
        model="claude-sonnet-5",
        usage={"input_tokens": 1, "output_tokens": 1},
        estimated_cost_usd=0.01,
    )
    enforced = enforce_director_duration_contract(
        NoNetworkDirector(),
        result,
        content_type="devotional",
        duration_minutes=10,
        budget_brl=20,
    )
    assert enforced.plan["duration_contract"]["validated"] is True
    assert enforced.plan["editorial_reviewed"] is True
    assert enforced.plan["editorial_review_ready"] is True
    assert enforced.plan["narration_locked"] is True
    assert "texto paralelo" not in enforced.plan["full_script"]


def test_editorial_lock_skips_downstream_rewrite():
    install_director_editorial_lock()

    class ShouldNotRun:
        def review_narration_package(self, *args, **kwargs):
            raise AssertionError("downstream editor must not be called for a locked director plan")

    service = EditorialIntelligenceService(ShouldNotRun())
    plan = {
        "title": "Teste",
        "scenes": [{"text": "Texto aprovado."}],
        "editorial_reviewed": True,
        "editorial_review_ready": True,
        "narration_locked": True,
        "duration_contract": {"validated": True, "locked_after_approval": True},
    }
    result = service.review_plan(plan, {"editorial_intelligence_enabled": True}, task_id="task-1")
    assert result["status"] == "skipped"
    assert result["plan_updates"]["editorial_intelligence"]["status"] == "director_locked"
