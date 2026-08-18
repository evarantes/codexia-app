from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.channel_excellence_guard import install_channel_excellence_guard_patch
from app.services.final_video_presentation_guard import install_final_video_presentation_guard


class QualityGenerator:
    def __init__(self, tmp_path: Path | None = None):
        self.tmp_path = tmp_path
        self.audio_text = ""

    def generate_audio(self, text, lang="pt", *args, **kwargs):
        self.audio_text = text
        return {"text": text, "lang": lang}

    def _default_opening_text(self, channel_name, *, plan=None):
        return "Prepare o coração: há uma mensagem de fé para o seu dia."

    def _default_reflection_text(self, plan=None, scenes=None):
        return "Reflexão automática escondida."

    def _default_closing_text(self, channel_name):
        return "Continue conosco e acompanhe as próximas mensagens de fé."

    def _target_visual_count(self, scenes, plan=None, *, ai_available=True):
        return 1

    def _build_visual_transition_decision(self, previous_profile, current_profile):
        return {"should_generate_new": False, "reason": "reuse"}

    def _resolve_contextual_closing(self, plan=None):
        return {"kind": "custom", "lines": ["Leve esta esperança com você."]}

    def create_video_from_plan(self, plan, *args, **kwargs):
        return {
            "file_path": "/tmp/fake.mp4",
            "render_report": {
                "narration_plan": {
                    "opening_text": str(plan.get("title") or ""),
                    "closing_text": "",
                },
                "visual_plan": {
                    "generated_image_count": 8,
                    "reused_image_count": 0,
                    "average_image_duration_sec": 7.5,
                },
            },
        }

    def _resolve_closing_background_image(self, branding, **kwargs):
        return {"path": None, "source": "dedicated_generated_endcard"}

    def _ensure_image_for_scene(self, prompt, text_fallback, aspect_ratio="16:9", **kwargs):
        assert "no people" in prompt.lower()
        path = (self.tmp_path or Path("/tmp")) / "premium-endcard.png"
        path.write_bytes(b"fake-image")
        return str(path)


def _fresh_cls(name: str):
    return type(name, (QualityGenerator,), {})


def test_approved_narration_only_removes_generic_intro_reflection_and_spoken_cta(monkeypatch):
    monkeypatch.setenv("ENABLE_APPROVED_NARRATION_ONLY", "true")
    cls = _fresh_cls("ApprovedNarrationGenerator")
    install_channel_excellence_guard_patch(cls)
    instance = cls()

    plan = {"title": "Jesus Está no Centro da Sua Vida?"}
    opening = instance._default_opening_text("Canal", plan=plan)
    assert opening == plan["title"]
    assert "mensagem de fé" not in opening.lower()
    assert instance._default_reflection_text(plan, []) == ""
    assert instance._default_closing_text("Canal") == ""


def test_ptbr_guard_normalizes_jesus_and_pelo_contrario(monkeypatch):
    monkeypatch.setenv("ENABLE_CHANNEL_EXCELLENCE_GUARD", "true")
    cls = _fresh_cls("PtBrFinalGenerator")
    install_channel_excellence_guard_patch(cls)
    result = cls().generate_audio("Jesus disse: pelo contrário, continue.", lang="pt-BR")
    assert result["lang"] == "pt"
    assert "Jêzus" in result["text"]
    assert "muito pelo contrário" in result["text"].lower()


def test_auto_video_requires_one_visual_target_per_scene(monkeypatch):
    monkeypatch.setenv("ENABLE_STRICT_VISUAL_UNIQUENESS", "true")
    cls = _fresh_cls("UniqueVisualGenerator")
    install_channel_excellence_guard_patch(cls)
    instance = cls()
    scenes = [{"text": f"Cena {idx}"} for idx in range(9)]
    assert instance._target_visual_count(scenes, {}) == 9
    decision = instance._build_visual_transition_decision({}, {})
    assert decision["should_generate_new"] is True
    assert decision["forced_by_channel_excellence"] is True


def test_manual_single_image_mode_is_preserved(monkeypatch):
    monkeypatch.setenv("ENABLE_STRICT_VISUAL_UNIQUENESS", "true")
    cls = _fresh_cls("ManualSingleVisualGenerator")
    install_channel_excellence_guard_patch(cls)
    scenes = [{"text": "A"}, {"text": "B"}]
    assert cls()._target_visual_count(scenes, {"selected_images": ["/tmp/manual.png"]}) == 1


def test_quality_gate_blocks_generic_opening_and_reused_generated_paths(monkeypatch):
    monkeypatch.setenv("ENABLE_FINAL_VIDEO_QUALITY_GATE", "true")

    class BadGenerator(QualityGenerator):
        def create_video_from_plan(self, plan, *args, **kwargs):
            return {
                "file_path": "/tmp/fake.mp4",
                "render_report": {
                    "narration_plan": {
                        "opening_text": "Uma mensagem de fé para hoje.",
                        "closing_text": "",
                    },
                    "visual_plan": {
                        "generated_image_count": 8,
                        "reused_image_count": 2,
                        "average_image_duration_sec": 14.0,
                    },
                },
            }

    cls = type("BlockedQualityGenerator", (BadGenerator,), {})
    install_channel_excellence_guard_patch(cls)
    with pytest.raises(RuntimeError, match="controle final de qualidade"):
        cls().create_video_from_plan({"title": "Teste", "scenes": [{"text": "Mensagem"}]})


def test_premium_endcard_generates_dedicated_ai_background(monkeypatch, tmp_path):
    monkeypatch.setenv("ENABLE_AI_PREMIUM_ENDCARD", "true")
    cls = _fresh_cls("PremiumEndcardGenerator")
    install_final_video_presentation_guard(cls)
    instance = cls(tmp_path=tmp_path)
    result = instance._resolve_closing_background_image({})
    assert result["source"] == "generated_premium_endcard_ai"
    assert Path(result["path"]).exists()


def test_quality_gate_accepts_clean_result(monkeypatch):
    monkeypatch.setenv("ENABLE_FINAL_VIDEO_QUALITY_GATE", "true")
    cls = _fresh_cls("CleanQualityGenerator")
    install_channel_excellence_guard_patch(cls)
    result = cls().create_video_from_plan({"title": "Título aprovado", "scenes": [{"text": "Mensagem"}]})
    quality = result["channel_excellence_guard"]["quality_gate"]
    assert quality["passed"] is True
    assert quality["violations"] == []
