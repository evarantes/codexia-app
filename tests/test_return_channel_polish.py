from __future__ import annotations

from pathlib import Path

from app.services.return_channel_polish import ensure_narrated_return_cta
from scripts.apply_voice_closure_hardening import patch_renderer, patch_scene_voice, patch_voice


ROOT = Path(__file__).resolve().parents[1]


def test_return_cta_is_a_real_narrated_scene_with_all_requested_actions():
    plan = {
        "title": "Quem é Jesus na sua vida?",
        "closing_message": "Jesus não é apenas parte da história; Ele precisa ocupar o centro da nossa vida.",
        "scenes": [
            {"text": "A pergunta sobre quem é Jesus não termina em uma definição."},
            {"text": "Se Ele é o seu ponto de virada, sua vida precisa refletir essa verdade."},
        ],
    }

    result = ensure_narrated_return_cta(plan)

    assert len(result["scenes"]) == 3
    closing = result["scenes"][-1]
    spoken = closing["text"].lower()
    assert "inscreva-se" in spoken
    assert "sininho" in spoken
    assert "compartilhe" in spoken
    assert closing["codexia_narrated_channel_cta"] is True
    assert "image_path" not in closing
    assert "no text inside the image" in closing["image_prompt"].lower()
    assert result["cta_text"] == ""
    assert result["closing_text"] == ""
    assert result["pause_duration_sec"] == 0.0
    assert result["end_screen_target_duration_sec"] == 1.2
    assert result["endcard_cta_text"] == "INSCREVA-SE • ATIVE O SININHO • COMPARTILHE"


def test_existing_complete_cta_in_scene_is_not_duplicated():
    plan = {
        "scenes": [
            {"text": "Jesus continua sendo o centro da mensagem."},
            {
                "text": (
                    "Inscreva-se no canal, ative o sininho para receber as próximas mensagens "
                    "e compartilhe este vídeo com alguém."
                )
            },
        ]
    }

    result = ensure_narrated_return_cta(plan)

    assert len(result["scenes"]) == 2
    joined = " ".join(scene["text"] for scene in result["scenes"]).lower()
    assert joined.count("inscreva-se") == 1
    assert result["codexia_narrated_channel_cta_applied"] is False


def test_complete_legacy_cta_is_moved_into_scene_before_legacy_field_is_cleared():
    legacy = (
        "Inscreva-se no canal, ative o sininho para receber as próximas mensagens "
        "e compartilhe este vídeo com alguém que precisa ouvi-lo."
    )
    plan = {
        "scenes": [{"text": "Jesus permanece conosco e esta mensagem termina aqui."}],
        "closing_text": legacy,
    }

    result = ensure_narrated_return_cta(plan)

    assert len(result["scenes"]) == 2
    assert result["scenes"][-1]["text"] == legacy
    assert result["closing_text"] == ""
    assert result["codexia_narrated_channel_cta_applied"] is True


def test_excellence_guard_preserves_explicit_endcard_cta():
    path = ROOT / "app/services/channel_excellence_guard.py"
    transformed = patch_voice(path.read_text(encoding="utf-8"))

    assert '_clean_line(guarded.get("endcard_cta_text"))' in transformed
    assert 'branding["endcard_cta_text"] = guarded["endcard_cta_text"]' in transformed
    assert 'branding.setdefault("endcard_cta_text", guarded["endcard_cta_text"])' not in transformed


def test_inner_scene_director_no_longer_reintroduces_phonetic_jesus():
    path = ROOT / "app/services/scene_director_active.py"
    transformed = patch_scene_voice(path.read_text(encoding="utf-8"))

    assert 'value = re.sub(r"(?i)\\bjesus\\b", "Jesus", value)' in transformed
    active_function = transformed.split("def _spoken_ptbr", 1)[1].split("def install_scene_director_active_patch", 1)[0]
    assert '"Jêzus"' not in active_function


def test_renderer_removes_synthetic_caption_lead_and_long_silent_tail():
    path = ROOT / "app/services/video_generator.py"
    transformed = patch_renderer(path.read_text(encoding="utf-8"))

    assert "DEFAULT_SCENE_CAPTION_LEAD_SEC = 0.0" in transformed
    assert "DEFAULT_SCENE_AUDIO_MARGIN_SEC = 0.10" in transformed
    assert "DEFAULT_SCENE_IMAGE_LEAD_SEC = 0.12" in transformed
    assert "if not closing_has_narration:\n                pause_before_cta_sec = 0.0" in transformed
    assert "end_clip_duration = min(1.6, max(0.8" in transformed
    assert "end_screen_target_duration_sec = float(_end_screen_configured if _end_screen_configured is not None else 1.2)" in transformed
