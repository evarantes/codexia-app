from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


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
        r"Há situações em que \1. ",
        value,
        count=1,
    )
    value = re.sub(
        r"(?i)^você\s+já\s+se\s+sentiu\s+(.+?)[?]\s*",
        r"Há situações em que \1. ",
        value,
        count=1,
    )
    value = re.sub(
        r"(?i)^você\s+já\s+(.+?)[?]\s*",
        r"Algumas experiências nos fazem perceber que \1. ",
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


_GENERIC_OPENINGS = (
    r"^em\s+meio\s+ao?\b",
    r"^em\s+meio\s+à\b",
    r"^no\s+turbilhão\b",
    r"^na\s+jornada\s+da\s+vida\b",
    r"^na\s+correria\s+do\s+dia\s+a\s+dia\b",
    r"^há\s+momentos\s+em\s+que\b",
    r"^existem\s+momentos\s+em\s+que\b",
    r"^quantas\s+vezes\b",
    r"^você\s+já\b",
    r"^você\s+alguma\s+vez\b",
)

_GENERIC_TITLE_FRAGMENTS = (
    "coração da nossa jornada",
    "jornada de fé",
    "caminho de esperança",
    "mensagem de fé",
    "uma história de fé",
    "luz em meio à escuridão",
    "luz na escuridão",
)

_METAPHOR_WORDS = (
    "âncora", "bússola", "porto", "tempestade", "mares", "mar revolto",
    "farol", "luz", "trevas", "caminho", "jornada", "abraço",
)


def _editorial_quality_issues(title: str, text: str, instruction: str) -> List[str]:
    """Detecta sinais de texto genérico/automatizado antes da revisão humana."""
    issues: List[str] = []
    clean_title = _clean(title).lower()
    clean_text = _clean(text)
    clean_text_lower = clean_text.lower()
    source = _clean(instruction).lower()

    if not clean_title:
        issues.append("missing_title")
    if any(fragment in clean_title for fragment in _GENERIC_TITLE_FRAGMENTS):
        issues.append("generic_title")

    opening = clean_text_lower[:220]
    if any(re.search(pattern, opening, flags=re.IGNORECASE) for pattern in _GENERIC_OPENINGS):
        issues.append("generic_opening")

    metaphor_count = sum(1 for term in _METAPHOR_WORDS if term in clean_text_lower)
    if metaphor_count >= 5:
        issues.append("metaphor_stacking")

    question_count = clean_text.count("?")
    if question_count >= 4:
        issues.append("too_many_rhetorical_questions")

    # O fechamento deve voltar ao tema central. Usamos palavras significativas
    # do pedido como um guardrail leve, sem exigir cópia literal.
    theme_tokens = [
        token for token in re.findall(r"[a-záàâãéêíóôõúç]{4,}", source, flags=re.IGNORECASE)
        if token not in {"quem", "vida", "para", "como", "qual", "uma", "você"}
    ]
    if theme_tokens:
        final_slice = clean_text_lower[-420:]
        if not any(token in final_slice for token in theme_tokens[:6]):
            issues.append("weak_return_to_theme")

    return issues


def _build_editorial_prompt(
    *,
    safe_kind: str,
    source_instruction: str,
    min_m: int,
    max_m: int,
    min_words: int,
    max_words: int,
    retry_issues: Optional[List[str]] = None,
) -> str:
    retry_note = ""
    if retry_issues:
        retry_note = (
            "\nA tentativa anterior foi rejeitada pelo controle editorial por: "
            + ", ".join(retry_issues)
            + ". Corrija especificamente esses pontos sem mencionar a rejeição.\n"
        )

    return f"""
Crie o conteúdo FINAL para revisão humana antes da geração de um vídeo cristão premium.

TIPO: {safe_kind}
TEMA / INSTRUÇÃO DO USUÁRIO: {source_instruction}
DURAÇÃO: {min_m} a {max_m} minuto(s)
FAIXA DE NARRAÇÃO: aproximadamente {min_words} a {max_words} palavras.
{retry_note}
PADRÃO EDITORIAL OBRIGATÓRIO:
1. TÍTULO: específico ao tema, memorável e natural. Evite títulos genéricos que serviriam para qualquer devocional. Não use clickbait barato.
2. COMEÇO: entre diretamente no conflito, ideia, cena mental ou verdade central do tema. NÃO comece com "Você já...", "Quantas vezes você já...", "Você alguma vez...", "Em meio ao turbilhão da vida...", "Na jornada da vida...", "Há momentos em que..." ou fórmulas equivalentes.
3. GANCHO: faça o ouvinte querer continuar por identificação, curiosidade legítima ou relevância espiritual. Não prometa algo que o roteiro não entrega.
4. PERSONALIDADE: escreva como um comunicador cristão humano e experiente, não como um gerador de frases motivacionais. Evite clichês empilhados, frases intercambiáveis e linguagem excessivamente abstrata.
5. METÁFORAS: use no máximo UMA metáfora central forte por bloco de ideia. Não empilhe âncora + bússola + tempestade + porto + luz + trevas no mesmo texto curto.
6. MEIO: desenvolva uma ideia de cada vez, com progressão lógica e transições naturais. Cada parágrafo precisa acrescentar algo novo.
7. FIDELIDADE: preserve rigorosamente o tema. Não invente versículos, capítulos, citações, números ou fatos não fornecidos.
8. LOCUÇÃO: português do Brasil natural, frases que soem bem em voz alta e pontuação clara. Evite trava-línguas, construções truncadas e palavras desnecessariamente rebuscadas. Se a ideia pedir "pelo contrário", prefira "muito pelo contrário" ou "ao contrário do que parece".
9. PERGUNTAS: use perguntas retóricas com parcimônia. Em um texto curto, prefira no máximo duas.
10. FIM: a conclusão precisa RETOMAR explicitamente a pergunta, tensão ou tese central do tema e respondê-la de forma pessoal e memorável. O ouvinte deve sentir que a mensagem realmente terminou, e não apenas parou.
11. TEOLOGIA/FORMULAÇÃO: evite simplificações vagas como "o amor é a única lei". Prefira formulações cristãs claras, coerentes e diretamente ligadas ao tema fornecido.
12. CTA: mantenha o CTA separado da reflexão espiritual e curto. Não misture "deixe seu like/inscreva-se" com a última frase da mensagem.
13. Nunca deixe frases incompletas como "uma mensagem de..." ou "uma palavra de...".
14. Retorne apenas JSON válido, sem markdown.

FORMATO EXATO:
{{
  "title": "...",
  "text": "narração completa em parágrafos",
  "closing_message": "reflexão final curta, até 150 caracteres",
  "endcard_cta_text": "Inscreva-se e acompanhe novas mensagens."
}}
""".strip()


def generate_review_ready_story_text(
    ai_service: Any,
    *,
    instruction: str,
    kind: str = "story",
    duration_min_minutes: int = 10,
    duration_max_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """Gera o conteúdo que o usuário revisará ANTES do pipeline de vídeo.

    Faz no máximo duas tentativas textuais. A segunda só acontece quando o
    controle editorial detecta sinais objetivos de título/abertura genéricos,
    excesso de metáforas ou fechamento desconectado do tema. Não gera mídia.
    """
    safe_kind = str(kind or "story").strip().lower()
    if safe_kind not in {"story", "devotional", "prayer"}:
        safe_kind = "story"
    min_m = max(1, min(60, int(duration_min_minutes or 1)))
    max_m = max(min_m, min(60, int(duration_max_minutes or min_m)))
    min_words = max(90, int(min_m * 125))
    max_words = max(min_words + 20, int(max_m * 165))
    source_instruction = _clean(instruction)

    retry_issues: Optional[List[str]] = None
    best_result: Optional[Dict[str, Any]] = None

    for attempt in range(2):
        prompt = _build_editorial_prompt(
            safe_kind=safe_kind,
            source_instruction=source_instruction,
            min_m=min_m,
            max_m=max_m,
            min_words=min_words,
            max_words=max_words,
            retry_issues=retry_issues,
        )
        try:
            raw = ai_service._generate_text(
                prompt,
                system_prompt=(
                    "Você é o Editor-Chefe Narrativo do Codexia para um canal cristão premium. "
                    "Priorize especificidade, naturalidade, progressão temática e conclusão completa. "
                    "Responda somente JSON válido em português do Brasil."
                ),
                json_mode=True,
            )
            parsed = _safe_json(raw)
            if not parsed:
                retry_issues = ["invalid_json"]
                continue

            title = _clean(parsed.get("title"))[:90]
            text = _repair_opening(str(parsed.get("text") or ""))
            if not title or not text:
                retry_issues = ["missing_title_or_text"]
                continue

            result = {
                "title": title,
                "text": text,
                "closing_message": _short_closing(parsed.get("closing_message")),
                "endcard_cta_text": _clean(parsed.get("endcard_cta_text"))[:90] or "Inscreva-se e acompanhe novas mensagens.",
                "editorial_review_ready": True,
                "editorial_source": "structured_ai" if attempt == 0 else "structured_ai_retry",
            }
            issues = _editorial_quality_issues(title, text, source_instruction)
            result["editorial_quality_issues"] = issues
            best_result = result
            if not issues:
                return result
            retry_issues = issues
        except Exception:
            retry_issues = ["generation_error"]

    # Fail-open: se houve resposta estruturada válida, preserva a melhor
    # tentativa para revisão humana em vez de cair para um texto legado pior.
    if best_result:
        return best_result

    legacy = ai_service.generate_story_or_devotional_text(
        instruction=instruction,
        kind=safe_kind,
        duration_min_minutes=min_m,
        duration_max_minutes=max_m,
    )
    legacy_text = _repair_opening(str(legacy or ""))
    return {
        "title": _fallback_title(source_instruction, safe_kind),
        "text": legacy_text,
        "closing_message": "Leve esta esperança com você: Deus continua presente.",
        "endcard_cta_text": "Inscreva-se e acompanhe novas mensagens.",
        "editorial_review_ready": True,
        "editorial_source": "legacy_fail_open",
        "editorial_quality_issues": _editorial_quality_issues(
            _fallback_title(source_instruction, safe_kind), legacy_text, source_instruction
        ),
    }
