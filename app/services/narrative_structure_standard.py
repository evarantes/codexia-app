from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Mapping


NARRATIVE_STRUCTURE_STANDARD_VERSION = 2

CHANNEL_NAME = "Herdeiros das Promessas"
CHANNEL_PRESENTATION_TEXT = "Seja muito bem-vindo ao canal Herdeiros das Promessas."
DEFAULT_NARRATED_CTA_TEXT = (
    "Se esta mensagem falou com você, curta este vídeo, inscreva-se no canal "
    "Herdeiros das Promessas, ative o sininho para receber as próximas mensagens, "
    "compartilhe este vídeo e conte nos comentários o que mais tocou o seu coração."
)
CANONICAL_NARRATION_SECTION_KEYS = (
    "hook",
    "development",
    "central_truth",
    "transformation",
    "application",
    "climax",
    "reflection",
)

PROFILE_DEVOTIONAL_EMOTIONAL = "devotional_emotional"
PROFILE_BIBLICAL_STORY = "biblical_story"
PROFILE_BIBLE_EXPLAINER = "bible_explainer"
PROFILE_COMFORT_FAITH = "comfort_faith"
PROFILE_GUIDED_PRAYER = "guided_prayer"
PROFILE_GENERAL_NARRATED = "general_narrated"

_SKIP_KINDS = {
    "music",
    "musica",
    "song",
    "soundtrack",
    "instrumental",
    "karaoke",
    "short",
    "shorts",
    "youtube_short",
    "youtube_shorts",
}

_BIBLICAL_STORY_MARKERS = (
    "davi", "golias", "josé", "jose", "moisés", "moises", "daniel", "noé", "noe",
    "abraão", "abraao", "sara", "ester", "rute", "samuel", "saul", "salomão", "salomao",
    "elias", "eliseu", "josué", "josue", "gideão", "gideao", "sansão", "sansao", "jonas",
    "pedro", "paulo", "maria", "marta", "lázaro", "lazaro", "zacqueu", "zaqueu",
    "história bíblica", "historia biblica", "relato bíblico", "relato biblico",
)

_EXPLAINER_MARKERS = (
    "o que significa", "quem foi", "quem é", "quem e", "por que", "porque", "explique",
    "explicação", "explicacao", "entenda", "significado", "contexto bíblico", "contexto biblico",
    "bíblia explicada", "biblia explicada", "estudo bíblico", "estudo biblico",
)

_COMFORT_MARKERS = (
    "ansiedade", "ansioso", "ansiosa", "triste", "tristeza", "luto", "perda", "sofrimento",
    "medo", "solidão", "solidao", "desânimo", "desanimo", "espera", "esperar em deus",
    "deus não esqueceu", "deus nao esqueceu", "não está sozinho", "nao esta sozinho",
    "consolo", "cura emocional", "coração quebrantado", "coracao quebrantado",
)


_PROFILE_ARCS: Dict[str, Dict[str, Any]] = {
    PROFILE_DEVOTIONAL_EMOTIONAL: {
        "label": "Devocional emocional",
        "purpose": "Conectar uma tensão humana real a uma verdade cristã central e terminar com esperança prática e memorável.",
        "beats": (
            "Gancho temático curto e específico, capaz de prender desde os primeiros segundos.",
            "Identificação humana: apresente a pergunta, dor, conflito ou necessidade de forma concreta.",
            "Aprofundamento emocional: aumente a conexão sem melodrama, clichês ou repetição vazia.",
            "Verdade bíblica/cristã central: apresente a resposta espiritual com clareza e sem inventar referências.",
            "Transformação: mostre como essa verdade muda perspectiva, esperança, direção ou atitude.",
            "Aplicação pessoal: diga o que isso significa para quem está assistindo hoje.",
            "Clímax memorável: use frases curtas, contraste ou repetição apenas quando forem conquistados pelo desenvolvimento.",
            "Reflexão final: retome explicitamente a tensão inicial e entregue uma conclusão reverente e completa.",
            "CTA separado da mensagem espiritual; nunca transforme o fechamento em propaganda.",
        ),
    },
    PROFILE_BIBLICAL_STORY: {
        "label": "História bíblica narrada",
        "purpose": "Contar o relato com progressão dramática, fidelidade ao tema e uma aplicação clara, sem transformar a história em resumo frio.",
        "beats": (
            "Gancho que introduza o conflito ou a decisão central da história sem revelar todo o desfecho.",
            "Contexto essencial: quem, onde e qual tensão está em jogo, apenas no nível necessário.",
            "Escalada do conflito: cada bloco precisa aumentar entendimento, risco, decisão ou consequência.",
            "Ponto de virada: destaque a ação, escolha, intervenção ou verdade central do relato.",
            "Desfecho claro e completo, sem cortar o acontecimento principal.",
            "Sentido espiritual: explique a lição sem inventar detalhes, versículos ou diálogos.",
            "Aplicação pessoal contemporânea, conectada organicamente ao relato.",
            "Clímax/conclusão memorável que retome o conflito inicial.",
            "CTA separado da narração espiritual.",
        ),
    },
    PROFILE_BIBLE_EXPLAINER: {
        "label": "Bíblia explicada",
        "purpose": "Responder uma pergunta bíblica de forma clara, progressiva e interessante, mantendo rigor e aplicação prática.",
        "beats": (
            "Abra com a pergunta ou dúvida central de modo direto e específico.",
            "Explique por que a questão importa para a compreensão da fé ou do texto.",
            "Forneça contexto bíblico/histórico apenas quando necessário e sem inventar fatos.",
            "Desenvolva a resposta em passos lógicos, uma ideia nova por bloco.",
            "Antecipe a principal confusão ou interpretação simplista e esclareça-a com cuidado.",
            "Traga aplicação prática coerente com a explicação.",
            "Resuma a resposta em uma formulação memorável e fiel ao tema.",
            "Feche respondendo explicitamente à pergunta inicial.",
            "CTA separado da conclusão.",
        ),
    },
    PROFILE_COMFORT_FAITH: {
        "label": "Consolo e fé",
        "purpose": "Acolher uma dor real sem promessas fáceis, conduzir à esperança cristã e oferecer um próximo passo possível.",
        "beats": (
            "Gancho acolhedor e específico à situação, sem frases genéricas que serviriam para qualquer sofrimento.",
            "Reconheça a dor ou incerteza sem minimizá-la e sem prometer solução instantânea.",
            "Aprofunde a identificação com linguagem humana e digna, evitando dramatização excessiva.",
            "Apresente a verdade de fé central como fundamento de esperança.",
            "Mostre uma mudança possível de perspectiva, postura, oração ou decisão.",
            "Ofereça aplicação pessoal simples e realizável para hoje.",
            "Construa um clímax de esperança sem repetir slogans.",
            "Reflexão final curta, serena e completa, retomando a dor inicial.",
            "CTA separado da mensagem.",
        ),
    },
    PROFILE_GUIDED_PRAYER: {
        "label": "Oração guiada",
        "purpose": "Conduzir o ouvinte de forma reverente e natural, com intenção clara, progressão e encerramento completo.",
        "beats": (
            "Introdução muito curta: diga o propósito da oração e convide a pessoa a se concentrar.",
            "Acolhimento/entrega: reconheça a situação diante de Deus com linguagem simples.",
            "Petição principal: desenvolva o pedido central sem listas mecânicas ou repetições vazias.",
            "Confiança e fé: conecte o pedido à esperança cristã sem promessas não fundamentadas.",
            "Aplicação interior: convide a uma atitude de confiança, perdão, gratidão ou entrega quando fizer sentido.",
            "Clímax espiritual sereno, não teatral.",
            "Encerramento da oração completo e reverente.",
            "Breve reflexão final opcional, sem quebrar o clima da oração.",
            "CTA somente depois da oração e separado dela.",
        ),
    },
    PROFILE_GENERAL_NARRATED: {
        "label": "Narrativa geral premium",
        "purpose": "Organizar qualquer vídeo narrado longo em um arco claro de atenção, desenvolvimento, entrega e conclusão.",
        "beats": (
            "Gancho específico e curto.",
            "Contexto mínimo necessário para entender a tensão ou promessa do tema.",
            "Desenvolvimento progressivo: cada bloco acrescenta informação, significado ou emoção.",
            "Ponto central: entregue claramente a ideia que justifica o vídeo.",
            "Consequência/transformação: mostre por que essa ideia importa.",
            "Aplicação ou implicação concreta para o espectador.",
            "Clímax memorável proporcional ao tema, sem exagero.",
            "Conclusão que retoma a abertura e fecha o raciocínio.",
            "CTA separado do conteúdo principal.",
        ),
    },
}


def _fold(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or "").lower())
    raw = "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", raw).strip()


def _contains_marker(text: str, marker: str) -> bool:
    """Match editorial markers as terms, not as accidental substrings."""
    folded_marker = _fold(marker)
    if not folded_marker:
        return False
    return bool(re.search(rf"(?<!\w){re.escape(folded_marker)}(?!\w)", text))


def should_apply_narrative_standard(kind: str) -> bool:
    return _fold(kind) not in _SKIP_KINDS


def select_narrative_profile(kind: str, instruction: str = "", explicit_profile: str = "") -> str:
    explicit = str(explicit_profile or "").strip().lower()
    if explicit in _PROFILE_ARCS:
        return explicit

    safe_kind = _fold(kind)
    text = _fold(instruction)

    if safe_kind == "prayer" or "oracao" in safe_kind:
        return PROFILE_GUIDED_PRAYER
    if any(_contains_marker(text, marker) for marker in _EXPLAINER_MARKERS):
        return PROFILE_BIBLE_EXPLAINER
    if any(_contains_marker(text, marker) for marker in _COMFORT_MARKERS):
        return PROFILE_COMFORT_FAITH
    if safe_kind == "devotional":
        return PROFILE_DEVOTIONAL_EMOTIONAL
    if safe_kind == "story" and any(_contains_marker(text, marker) for marker in _BIBLICAL_STORY_MARKERS):
        return PROFILE_BIBLICAL_STORY
    if safe_kind == "story":
        return PROFILE_GENERAL_NARRATED
    return PROFILE_GENERAL_NARRATED


def narrative_structure_metadata(profile: str) -> Dict[str, Any]:
    selected = profile if profile in _PROFILE_ARCS else PROFILE_GENERAL_NARRATED
    arc = _PROFILE_ARCS[selected]
    return {
        "version": NARRATIVE_STRUCTURE_STANDARD_VERSION,
        "scope": "global",
        "profile": selected,
        "profile_label": arc["label"],
        "purpose": arc["purpose"],
        "beats": list(arc["beats"]),
        "reference_style": "jesus_o_motivo_de_eu_existir_without_copying",
    }


def narrative_structure_prompt(profile: str, *, kind: str = "", instruction: str = "") -> str:
    metadata = narrative_structure_metadata(profile)
    beats = "\n".join(f"{index}. {beat}" for index, beat in enumerate(metadata["beats"], start=1))
    return f"""
ESTRUTURA NARRATIVA CANÔNICA DO CODEXIA — V{NARRATIVE_STRUCTURE_STANDARD_VERSION}
ESCOPO: GLOBAL. Este padrão vale para todo o sistema que produzir vídeos narrados longos; o YouTube Auto é um dos pontos de entrada, não uma implementação isolada.
PERFIL SELECIONADO: {metadata['profile_label']} ({metadata['profile']})
OBJETIVO: {metadata['purpose']}

ARCO OBRIGATÓRIO (estrutura semântica; NÃO escreva os nomes dos blocos na narração):
{beats}

REGRAS GLOBAIS DE ROTEIRO:
- A primeira fala, depois de quatro segundos exclusivamente visuais, será exatamente: "{CHANNEL_PRESENTATION_TEXT}"
- Entregue gancho, desenvolvimento, verdade central, transformação, aplicação, clímax e reflexão como sete blocos narrativos separados, nessa ordem e sem escrever os rótulos.
- Inspire-se na qualidade de progressão emocional do projeto de referência "Jesus, o motivo de eu existir", mas nunca copie frases, imagens, exemplos ou o texto daquele vídeo.
- A estrutura deve servir ao tema solicitado; não force emoção, dor, repetição ou linguagem devocional quando o assunto pedir explicação ou narrativa histórica.
- O pedido explícito do usuário, o tema e a duração têm prioridade. Não force cinco minutos nem qualquer duração fixa.
- O gancho deve ser curto. Não desperdice grande parte da duração preparando o assunto.
- Cada parágrafo/bloco deve acrescentar uma ideia, informação, consequência ou avanço emocional novo.
- Não use repetição como enchimento. Repetição no clímax só é permitida quando reforça uma ideia já construída pelo roteiro.
- O fechamento precisa responder ou resolver a tensão criada no começo. O texto deve soar terminado, nunca interrompido.
- CTA é um elemento separado. No bloco final, convide a curtir, inscrever-se, ativar o sininho, compartilhar e comentar; não coloque esses pedidos no meio da reflexão espiritual.
- Nunca coloque no texto narrável rótulos como "GANCHO", "CLÍMAX", "CENA", "PROMPT", JSON, instruções técnicas, marcações de câmera ou metadados.
- Não invente versículos, referências, citações, datas, diálogos ou fatos que não estejam seguros no contexto fornecido.
- Escreva para locução humana em português do Brasil: natural, claro, específico e agradável de ouvir em voz alta.
- Revise gramática, concordância, acentuação e pontuação antes de entregar. Use frases com respirações naturais e grafias que deixem a pronúncia e a dicção inequívocas no TTS.

TIPO RECEBIDO: {str(kind or '').strip()}
TEMA RECEBIDO: {str(instruction or '').strip()}
""".strip()


def _cta_signal_map(value: Any) -> Dict[str, bool]:
    folded = _fold(value)
    return {
        "like": "curta este video" in folded or "deixe seu like" in folded,
        "subscribe": "inscreva-se" in folded or "inscreva se" in folded,
        "bell": "sininho" in folded or "notifica" in folded,
        "share": "compartilh" in folded,
        "comment": "coment" in folded,
    }


def _final_cta_scope(value: Any) -> str:
    """Return only the final paragraph/sentence that is allowed to be the CTA."""
    text = str(value or "").strip()
    if not text:
        return ""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if len(paragraphs) > 1:
        return paragraphs[-1]
    sentences = [
        part.strip()
        for part in re.findall(r'[^.!?…]+(?:[.!?…]+["”’\']?|$)', text)
        if part.strip()
    ]
    return sentences[-1] if sentences else text


def _narration_paragraphs(value: Any) -> list[str]:
    return [
        re.sub(r"\s+", " ", part).strip()
        for part in re.split(r"\n\s*\n", str(value or "").strip())
        if re.sub(r"\s+", " ", part).strip()
    ]


def compose_canonical_narration(
    sections: Mapping[str, Any] | None,
    *,
    cta_text: Any = "",
    fallback_text: Any = "",
) -> str:
    """Build the exact text that may be sent to TTS, in the agreed order."""
    source = dict(sections or {})
    ordered = [
        re.sub(r"\s+", " ", str(source.get(key) or "")).strip()
        for key in CANONICAL_NARRATION_SECTION_KEYS
    ]
    body = "\n\n".join(part for part in ordered if part).strip()
    if not body:
        body = str(fallback_text or "").strip()

    # The channel presentation is a fixed spoken block after the 0-4s visual
    # opening. The spiritual conclusion remains separate from the CTA.
    presentation_pattern = re.compile(
        rf"^\s*{re.escape(CHANNEL_PRESENTATION_TEXT)}",
        re.IGNORECASE,
    )
    presentation_match = presentation_pattern.match(body)
    if presentation_match:
        body = body[presentation_match.end():].lstrip(" \t\r\n")

    cta = str(cta_text or DEFAULT_NARRATED_CTA_TEXT).strip()
    if not all(_cta_signal_map(cta).values()):
        cta = DEFAULT_NARRATED_CTA_TEXT
    existing_cta = _final_cta_scope(body)
    if existing_cta and all(_cta_signal_map(existing_cta).values()):
        cta = existing_cta
        if body.rstrip().endswith(existing_cta):
            body = body.rstrip()[:-len(existing_cta)].rstrip()

    parts = [CHANNEL_PRESENTATION_TEXT]
    if body:
        parts.append(body)
    if cta:
        parts.append(cta)
    return "\n\n".join(parts).strip()


def audit_canonical_narration(
    text: Any,
    *,
    sections: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    value = str(text or "").strip()
    paragraphs = _narration_paragraphs(value)
    presentation_separate = bool(
        paragraphs and _fold(paragraphs[0]) == _fold(CHANNEL_PRESENTATION_TEXT)
    )
    final_cta = paragraphs[-1] if len(paragraphs) >= 2 else ""
    required_cta_signals = _cta_signal_map(final_cta)
    body_paragraphs = paragraphs[1:-1] if len(paragraphs) >= 2 else []
    inferred_arc_complete = len(body_paragraphs) >= len(CANONICAL_NARRATION_SECTION_KEYS)
    body_blocks_substantive = bool(body_paragraphs) and all(
        len(block.split()) >= 3 for block in body_paragraphs
    )
    section_map = dict(sections or {})
    sections_complete = bool(section_map) and all(
        str(section_map.get(key) or "").strip()
        for key in CANONICAL_NARRATION_SECTION_KEYS
    )
    cta_at_end = all(required_cta_signals.values())
    narrative_arc_ordered = bool(
        body_blocks_substantive
        and (sections_complete or inferred_arc_complete)
    )
    return {
        "standard_version": NARRATIVE_STRUCTURE_STANDARD_VERSION,
        "channel_name": CHANNEL_NAME,
        "channel_presentation_text": CHANNEL_PRESENTATION_TEXT,
        "channel_presentation_at_start": presentation_separate,
        "channel_presentation_separate": presentation_separate,
        "cta_separate_at_end": cta_at_end,
        "cta_signals": required_cta_signals,
        "ordered_section_keys": list(CANONICAL_NARRATION_SECTION_KEYS),
        "body_block_count": len(body_paragraphs),
        "minimum_body_block_count": len(CANONICAL_NARRATION_SECTION_KEYS),
        "body_blocks_substantive": body_blocks_substantive,
        "narrative_sections_complete": sections_complete,
        "narrative_arc_ordered": narrative_arc_ordered,
        "valid": bool(value and presentation_separate and narrative_arc_ordered and cta_at_end),
    }


def apply_narrative_standard_metadata(payload: Mapping[str, Any] | None, *, profile: str) -> Dict[str, Any]:
    result = dict(payload or {})
    result["narrative_structure_applied"] = True
    result["narrative_standard_version"] = NARRATIVE_STRUCTURE_STANDARD_VERSION
    result["narrative_profile"] = profile
    result["narrative_standard_scope"] = "global"
    result["narrative_standard_entrypoint"] = "youtube_auto_and_shared_editor"
    result["narrative_standard"] = narrative_structure_metadata(profile)
    return result


__all__ = [
    "NARRATIVE_STRUCTURE_STANDARD_VERSION",
    "PROFILE_DEVOTIONAL_EMOTIONAL",
    "PROFILE_BIBLICAL_STORY",
    "PROFILE_BIBLE_EXPLAINER",
    "PROFILE_COMFORT_FAITH",
    "PROFILE_GUIDED_PRAYER",
    "PROFILE_GENERAL_NARRATED",
    "should_apply_narrative_standard",
    "select_narrative_profile",
    "narrative_structure_metadata",
    "narrative_structure_prompt",
    "apply_narrative_standard_metadata",
    "CHANNEL_NAME",
    "CHANNEL_PRESENTATION_TEXT",
    "DEFAULT_NARRATED_CTA_TEXT",
    "CANONICAL_NARRATION_SECTION_KEYS",
    "compose_canonical_narration",
    "audit_canonical_narration",
]
