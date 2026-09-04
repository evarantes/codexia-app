from __future__ import annotations

from typing import Any, Dict, Mapping

from app.services.narrative_structure_standard import (
    apply_narrative_standard_metadata,
    narrative_structure_prompt,
    select_narrative_profile,
    should_apply_narrative_standard,
)


REFERENCE_STYLE_ID = "jesus_o_motivo_de_eu_existir_without_copying"
REFERENCE_QUALITY_RULE = (
    "Use 'Jesus, o motivo de eu existir' somente como referência de qualidade narrativa: "
    "gancho curto, progressão emocional/lógica, verdade central, transformação, aplicação, "
    "clímax, reflexão completa e CTA separado. Nunca copie frases, exemplos ou imagens do roteiro de referência."
)


def prepare_global_narrative_request(payload: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Ponto de entrada único de roteiro para produtores narrados compatíveis.

    História/Devocional, YouTube Auto, planejamento automático, séries e regenerações
    devem convergir neste helper antes de qualquer geração de roteiro. Shorts e música
    continuam respeitando os contratos especiais existentes.
    """
    result = dict(payload or {})
    kind = str(result.get("kind") or result.get("content_type") or "story").strip().lower() or "story"
    if not should_apply_narrative_standard(kind):
        return result

    instruction = str(
        result.get("instruction")
        or result.get("topic")
        or result.get("theme")
        or result.get("title")
        or ""
    ).strip()
    profile = select_narrative_profile(
        kind,
        instruction,
        str(result.get("narrative_profile") or ""),
    )
    result = apply_narrative_standard_metadata(result, profile=profile)
    base_prompt = narrative_structure_prompt(profile, kind=kind, instruction=instruction)
    result["narrative_structure_prompt"] = (
        base_prompt
        + "\n\nREFERÊNCIA GLOBAL DE QUALIDADE — OBRIGATÓRIA EM TODOS OS ENTRYPOINTS NARRADOS:\n"
        + REFERENCE_QUALITY_RULE
    )
    result["narrative_reference_style"] = REFERENCE_STYLE_ID
    result["narrative_standard_entrypoint"] = str(
        result.get("source_module") or result.get("entrypoint") or "shared"
    )
    return result


__all__ = [
    "REFERENCE_STYLE_ID",
    "REFERENCE_QUALITY_RULE",
    "prepare_global_narrative_request",
]
