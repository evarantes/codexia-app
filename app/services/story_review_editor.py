from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).strip()


def _safe_json(raw: Any) -> Optional[Dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        return None
    candidates = [text]
    if "```json" in text:
        candidates.append(text.split("```json", 1)[1].split("```", 1)[0].strip())
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _repair_opening(text: str) -> str:
    value = _clean(text)
    if not value:
        return value
    value = re.sub(
        r"(?i)^quantas\s+vezes\s+você\s+já\s+se\s+sentiu\s+(.+?)[?]\s*",
        r"Há momentos em que \1. ",
        value,
        count=1,
    )
    value = re.sub(
        r"(?i)^você\s+já\s+se\s+sentiu\s+(.+?)[?]\s*",
        r"Há momentos em que \1. ",
        value,
        count=1,
    )
    value = re.sub(
        r"(?i)^você\s+já\s+(.+?)[?]\s*",
        r"Existem momentos em que \1. ",
        value,
        count=1,
    )
    value = re.sub(
        r"(?i)\b(?:uma|esta)\s+(?:mensagem|palavra)\s+de\s*(?:\.{2,}|[.!?,;:]|$)",
        "Esta mensagem é para você. ",
        value,
    )
    return _clean(value)


def _short_closing(value: Any) -> str:
    text = _clean(value)
    if not text:
        return "Leve esta esperança com você: Deus continua presente."
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    if sentences:
        text = " ".join(sentences[:2])
    if len(text) > 150:
        text = text[:147].rsplit(" ", 1)[0].rstrip(" ,;:-") + "."
    return text


def _fallback_title(instruction: str, kind: str) -> str:
    base = _clean(instruction).strip(" .!?—–-")
    if base:
        return base[:90]
    return "Mensagem de Fé e Esperança" if kind != "story" else "Uma História de Fé e Esperança"


def generate_review_ready_story_text(
    ai_service: Any,
    *,
    instruction: str,
    kind: str = "story",
    duration_min_minutes: int = 10,
    duration_max_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """Gera o conteúdo que o usuário revisará ANTES do pipeline de vídeo.

    O contrato é estruturado: título separado, narração final e fechamento/CTA.
    Não altera áudio, cenas ou render. Se o retorno estruturado falhar, usa o
    gerador legado e aplica apenas proteções determinísticas seguras.
    """
    safe_kind = str(kind or "story").strip().lower()
    if safe_kind not in {"story", "devotional", "prayer"}:
        safe_kind = "story"
    min_m = max(1, min(60, int(duration_min_minutes or 1)))
    max_m = max(min_m, min(60, int(duration_max_minutes or min_m)))
    min_words = max(90, int(min_m * 125))
    max_words = max(min_words + 20, int(max_m * 165))
    source_instruction = _clean(instruction)

    prompt = f"""
Crie o conteúdo FINAL para revisão humana antes da geração de um vídeo cristão premium.

TIPO: {safe_kind}
TEMA / INSTRUÇÃO DO USUÁRIO: {source_instruction}
DURAÇÃO: {min_m} a {max_m} minuto(s)
FAIXA DE NARRAÇÃO: aproximadamente {min_words} a {max_words} palavras.

REGRAS OBRIGATÓRIAS:
1. Gere um TÍTULO forte, específico, natural e coerente com o tema. Não use clickbait barato.
2. A narração precisa ter COMEÇO, MEIO e FIM perceptíveis, mas sem escrever rótulos como "Introdução" ou "Conclusão".
3. A abertura deve ser criativa e convidativa. NÃO comece com "Você já...", "Quantas vezes você já...", "Você alguma vez..." ou fórmulas equivalentes.
4. Varie o estilo do gancho: observação emocional, contraste, imagem poética, esperança, pergunta profunda, acolhimento pastoral ou contexto bíblico quando o material permitir.
5. Nunca deixe frases incompletas como "uma mensagem de..." ou "uma palavra de...".
6. Escreva para LOCUÇÃO: português do Brasil natural, frases fluidas, pontuação clara e sem construções que induzam pronúncia ruim. Se a ideia pedir "pelo contrário", prefira "muito pelo contrário" ou "ao contrário do que parece".
7. Preserve rigorosamente o tema. Não invente versículos, capítulos, citações, números ou fatos não fornecidos.
8. Evite repetição de ideias, clichês em sequência e excesso de perguntas retóricas.
9. O fim deve concluir a mensagem e entregar uma reflexão curta e memorável.
10. O CTA deve ficar SEPARADO da reflexão e ser curto. Não misture pedido de like/inscrição com a última frase espiritual.
11. Retorne apenas JSON válido, sem markdown.

FORMATO EXATO:
{{
  "title": "...",
  "text": "narração completa em parágrafos",
  "closing_message": "reflexão final curta, até 150 caracteres",
  "endcard_cta_text": "Inscreva-se e acompanhe novas mensagens."
}}
""".strip()

    try:
        raw = ai_service._generate_text(
            prompt,
            system_prompt="Você é o Editor-Chefe Narrativo do Codexia. Responda somente JSON válido em português do Brasil.",
            json_mode=True,
        )
        parsed = _safe_json(raw)
        if parsed:
            title = _clean(parsed.get("title"))[:90]
            text = _repair_opening(str(parsed.get("text") or ""))
            if title and text:
                return {
                    "title": title,
                    "text": text,
                    "closing_message": _short_closing(parsed.get("closing_message")),
                    "endcard_cta_text": _clean(parsed.get("endcard_cta_text"))[:90] or "Inscreva-se e acompanhe novas mensagens.",
                    "editorial_review_ready": True,
                    "editorial_source": "structured_ai",
                }
    except Exception:
        pass

    legacy = ai_service.generate_story_or_devotional_text(
        instruction=instruction,
        kind=safe_kind,
        duration_min_minutes=min_m,
        duration_max_minutes=max_m,
    )
    return {
        "title": _fallback_title(source_instruction, safe_kind),
        "text": _repair_opening(str(legacy or "")),
        "closing_message": "Leve esta esperança com você: Deus continua presente.",
        "endcard_cta_text": "Inscreva-se e acompanhe novas mensagens.",
        "editorial_review_ready": True,
        "editorial_source": "legacy_fail_open",
    }
