from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Mapping


NARRATIVE_STRUCTURE_STANDARD_VERSION = 1

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
    if any(_fold(marker) in text for marker in _EXPLAINER_MARKERS):
        return PROFILE_BIBLE_EXPLAINER
    if any(_fold(marker) in text for marker in _COMFORT_MARKERS):
        return PROFILE_COMFORT_FAITH
    if safe_kind == "devotional":
        return PROFILE_DEVOTIONAL_EMOTIONAL
    if safe_kind == "story" and any(_fold(marker) in text for marker in _BIBLICAL_STORY_MARKERS):
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
- Inspire-se na qualidade de progressão emocional do projeto de referência "Jesus, o motivo de eu existir", mas nunca copie frases, imagens, exemplos ou o texto daquele vídeo.
- A estrutura deve servir ao tema solicitado; não force emoção, dor, repetição ou linguagem devocional quando o assunto pedir explicação ou narrativa histórica.
- O pedido explícito do usuário, o tema e a duração têm prioridade. Não force cinco minutos nem qualquer duração fixa.
- O gancho deve ser curto. Não desperdice grande parte da duração preparando o assunto.
- Cada parágrafo/bloco deve acrescentar uma ideia, informação, consequência ou avanço emocional novo.
- Não use repetição como enchimento. Repetição no clímax só é permitida quando reforça uma ideia já construída pelo roteiro.
- O fechamento precisa responder ou resolver a tensão criada no começo. O texto deve soar terminado, nunca interrompido.
- CTA é um elemento separado. Não insira pedidos de like/inscrição no meio da reflexão espiritual.
- Nunca coloque no texto narrável rótulos como "GANCHO", "CLÍMAX", "CENA", "PROMPT", JSON, instruções técnicas, marcações de câmera ou metadados.
- Não invente versículos, referências, citações, datas, diálogos ou fatos que não estejam seguros no contexto fornecido.
- Escreva para locução humana em português do Brasil: natural, claro, específico e agradável de ouvir em voz alta.

TIPO RECEBIDO: {str(kind or '').strip()}
TEMA RECEBIDO: {str(instruction or '').strip()}
""".strip()


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
]
