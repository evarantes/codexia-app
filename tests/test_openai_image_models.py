from datetime import date

from app.services.openai_image_models import (
    available_openai_image_models,
    openai_image_model_unavailable_message,
)


def test_available_openai_image_models_filters_retired_and_shutdown_models():
    models = available_openai_image_models(
        [
            {"id": "gpt-image-1"},
            {"id": "gpt-image-2", "shutdown_date": "2027-01-01"},
            {"id": "gpt-image-1.5", "shutdown_date": "2026-10-28"},
            {"id": "gpt-image-1-mini"},
            {"id": "gpt-4.1"},
        ],
        today=date(2026, 10, 29),
    )

    assert [model["id"] for model in models] == ["gpt-image-2", "gpt-image-1-mini"]
    assert models[0]["label"] == "GPT Image 2 — padrão do sistema"


def test_available_openai_image_models_prefers_system_default_if_available():
    models = available_openai_image_models(
        [
            {"id": "gpt-image-1-mini"},
            {"id": "gpt-image-2.5-flare"},
            {"id": "gpt-image-2.5-sunburst"},
            {"id": "gpt-image-2"},
            {"id": "chatgpt-image-latest"},
        ],
        today=date(2026, 10, 29),
    )

    assert [model["id"] for model in models] == [
        "gpt-image-2",
        "gpt-image-2.5-sunburst",
        "gpt-image-2.5-flare",
        "gpt-image-1-mini",
        "chatgpt-image-latest",
    ]


def test_retired_model_message_explains_shutdown_date():
    message = openai_image_model_unavailable_message("gpt-image-1")

    assert "23/10/2026" in message
    assert "Configurações" in message
