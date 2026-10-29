from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Optional


OPENAI_IMAGE_MODEL_PREFIXES = ("gpt-image-", "chatgpt-image-")
OPENAI_RETIRED_IMAGE_MODELS = {"gpt-image-1": "2026-10-23"}

OPENAI_IMAGE_MODEL_PREFERENCE = (
    "gpt-image-2",
    "gpt-image-2-2026-04-21",
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5-flare",
    "gpt-image-1.5",
    "gpt-image-1-mini",
    "chatgpt-image-latest",
)

OPENAI_IMAGE_MODEL_LABELS = {
    "gpt-image-2.5-sunburst": "GPT Image 2.5 Sunburst",
    "gpt-image-2.5-flare": "GPT Image 2.5 Flare — geração rápida",
    "gpt-image-2": "GPT Image 2 — padrão do sistema",
    "gpt-image-2-2026-04-21": "GPT Image 2 — versão fixa (21/04/2026)",
    "gpt-image-1.5": "GPT Image 1.5",
    "gpt-image-1-mini": "GPT Image 1 Mini",
    "chatgpt-image-latest": "ChatGPT Image Latest",
}


def is_openai_image_model(model_id: Any) -> bool:
    normalized = str(model_id or "").strip().lower()
    return normalized.startswith(OPENAI_IMAGE_MODEL_PREFIXES)


def is_retired_openai_image_model(model_id: Any) -> bool:
    normalized = str(model_id or "").strip().lower()
    return normalized in OPENAI_RETIRED_IMAGE_MODELS


def openai_image_model_unavailable_message(model_id: Any) -> str:
    normalized = str(model_id or "modelo selecionado").strip()
    retired_date = OPENAI_RETIRED_IMAGE_MODELS.get(normalized.lower())
    if retired_date:
        year, month, day = retired_date.split("-")
        return (
            f"O modelo de imagem {normalized} foi encerrado pela OpenAI em "
            f"{day}/{month}/{year}. Atualize o modelo em Configurações."
        )
    return (
        f"O modelo de imagem {normalized} não está disponível para esta chave OpenAI. "
        "Escolha um dos modelos listados em Configurações."
    )


def available_openai_image_models(
    api_models: Any,
    *,
    today: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Filter the account's /v1/models response to usable GPT Image entries."""
    current_date = today or date.today()
    options: List[Dict[str, Any]] = []
    seen = set()
    records: Iterable[Any] = api_models if isinstance(api_models, list) else []

    for record in records:
        if not isinstance(record, dict):
            continue
        model_id = str(record.get("id") or "").strip().lower()
        if not model_id or not is_openai_image_model(model_id):
            continue
        if model_id in seen or is_retired_openai_image_model(model_id):
            continue

        shutdown_date = str(record.get("shutdown_date") or "").strip()
        if shutdown_date:
            try:
                if date.fromisoformat(shutdown_date[:10]) <= current_date:
                    continue
            except ValueError:
                # Ignore an unparseable optional field; the API's availability
                # response remains the authoritative source for this model.
                pass

        seen.add(model_id)
        options.append({
            "id": model_id,
            "label": OPENAI_IMAGE_MODEL_LABELS.get(model_id, model_id),
            "shutdown_date": shutdown_date or None,
        })

    preference = {model_id: index for index, model_id in enumerate(OPENAI_IMAGE_MODEL_PREFERENCE)}
    options.sort(key=lambda item: (preference.get(item["id"], len(preference)), item["id"]))
    return options


__all__ = [
    "OPENAI_IMAGE_MODEL_LABELS",
    "OPENAI_IMAGE_MODEL_PREFERENCE",
    "OPENAI_IMAGE_MODEL_PREFIXES",
    "OPENAI_RETIRED_IMAGE_MODELS",
    "available_openai_image_models",
    "is_openai_image_model",
    "is_retired_openai_image_model",
    "openai_image_model_unavailable_message",
]
