from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Iterable, List


SPOKEN_TEXT_BOUNDARY_VERSION = 4

# Campos que podem conter fala real quando a IA devolve um objeto estruturado.
_NARRATIVE_KEYS = {
    "text",
    "narration",
    "narration_text",
    "spoken_text",
    "voiceover",
    "voice_over",
    "locution",
    "locucao",
    "narracao",
    "speech",
    "reflection",
    "reflection_text",
    "reflexao",
    "closing_message",
    "closing_text",
    "cta_text",
    "narrated_cta_text",
    "body_text",
    "opening_text",
}

# Chaves de produção nunca são fala. A lista inclui EN/PT-BR porque os modelos
# alternam idioma em estruturas técnicas mesmo quando a narração é portuguesa.
_TECHNICAL_KEYS = {
    "image_prompt",
    "visual_prompt",
    "prompt_visual",
    "prompt_de_imagem",
    "prompt_imagem",
    "negative_prompt",
    "camera_movement",
    "movimento_camera",
    "motion_effect",
    "scene_qc",
    "scene_card",
    "on_screen_text",
    "texto_na_tela",
    "selected_images",
    "generated_image_path",
    "asset_path",
    "render_report",
    "metadata",
    "duration",
    "duration_sec",
    "duracao",
    "transition",
    "transicao",
    "sound_effect",
    "efeito_sonoro",
    "music",
    "musica",
    "trilha",
    "lighting",
    "iluminacao",
    "framing",
    "enquadramento",
    "aspect_ratio",
    "style",
    "visual_style",
    "estilo_visual",
    "scene_id",
    "segment_id",
    "timestamp",
    "timecode",
}

_NARRATIVE_LABEL = re.compile(
    r"(?ix)^\s*(?:"
    r"narra[cç][aã]o|texto\s+(?:de\s+)?narra[cç][aã]o|texto\s+narrado|"
    r"locu[cç][aã]o|fala|voice\s*over|voiceover|spoken\s*text|"
    r"reflex[aã]o(?:\s+final)?|mensagem\s+final|encerramento|cta(?:\s+final)?"
    r")\s*[:=\-–—]+\s*(.*)$"
)

_TECHNICAL_LABEL = re.compile(
    r"(?ix)^\s*(?:"
    r"prompt(?:\s+visual|\s+de\s+imagem|\s+da\s+imagem)?|"
    r"visual\s+prompt|image\s+prompt|negative\s+prompt|"
    r"imagem(?:\s+da\s+cena)?|descri[cç][aã]o\s+(?:visual|da\s+imagem)|"
    r"movimento\s+(?:de\s+)?c[aâ]mera|camera\s+movement|c[aâ]mera|"
    r"transi[cç][aã]o|transition|dura[cç][aã]o|duration|tempo\s+de\s+cena|"
    r"efeito(?:\s+sonoro|\s+visual)?|sound\s+effect|motion\s+effect|"
    r"m[uú]sica|trilha(?:\s+sonora)?|background\s+music|"
    r"texto\s+na\s+tela|on\s*screen\s*text|legenda|subtitle|caption|"
    r"enquadramento|framing|ilumina[cç][aã]o|lighting|"
    r"estilo\s+visual|visual\s+style|aspect\s+ratio|propor[cç][aã]o|"
    r"metadados?|metadata|render(?:\s+report)?|relat[oó]rio\s+de\s+render|"
    r"arquivo|caminho\s+do\s+arquivo|asset|output\s*path|file\s*path|"
    r"scene[_\s-]*id|segment[_\s-]*id|timestamp|timecode"
    r")\s*[:=\-–—]+\s*.*$"
)

_SCENE_ONLY = re.compile(
    r"(?ix)^\s*(?:cena|scene|take|shot|plano|bloco|segmento)\s*#?\s*\d+"
    r"(?:\s*(?:de|/|\-)\s*\d+)?\s*[:.\-–—]*\s*$"
)

# Permite "CENA 1 — NARRAÇÃO: texto" sem jogar fora a fala.
_SCENE_PREFIX = re.compile(
    r"(?ix)^\s*(?:cena|scene|take|shot|plano|bloco|segmento)\s*#?\s*\d+"
    r"(?:\s*(?:de|/|\-)\s*\d+)?\s*[:.\-–—]+\s*(.+)$"
)

_STAGE_DIRECTION = re.compile(
    r"(?ix)[\[(]\s*(?:"
    r"pausa(?:\s+dram[aá]tica|\s+curta|\s+longa)?|respira(?:r)?|sil[eê]ncio|"
    r"tom\s+(?:emocional|suave|solene|forte|baixo|calmo|dram[aá]tico)|"
    r"voz\s+(?:baixa|suave|emocionada|solene)|"
    r"ênfase|enfase|sussurrando|lentamente|pausadamente|"
    r"fade\s*(?:in|out)|corte|transi[cç][aã]o|m[uú]sica\s+ao\s+fundo"
    r")[^\])]*[\])]"
)

_TECHNICAL_RESIDUE = re.compile(
    r"(?ix)(?:"
    r"\b(?:prompt\s+visual|prompt\s+de\s+imagem|image\s+prompt|visual\s+prompt|"
    r"movimento\s+de\s+c[aâ]mera|camera\s+movement|texto\s+na\s+tela|on\s*screen\s*text|"
    r"negative\s+prompt|aspect\s+ratio|scene[_\s-]*id|segment[_\s-]*id|"
    r"output[_\s-]*path|file[_\s-]*path|asset[_\s-]*path|render[_\s-]*report)\b"
    r"|```|~~~|\{\s*[\"'][A-Za-z_][A-Za-z0-9_]*[\"']\s*:"
    r")"
)


def _fold_key(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    raw = "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")
    raw = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return raw


def _try_json(value: str) -> Any:
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [text]
    fenced = re.search(r"(?is)```(?:json)?\s*(.*?)```", text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start_obj, end_obj = text.find("{"), text.rfind("}")
    if start_obj >= 0 and end_obj > start_obj:
        candidates.append(text[start_obj:end_obj + 1])
    start_arr, end_arr = text.find("["), text.rfind("]")
    if start_arr >= 0 and end_arr > start_arr:
        candidates.append(text[start_arr:end_arr + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _extract_from_structured(value: Any, *, parent_key: str = "") -> List[str]:
    parts: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            folded = _fold_key(key)
            if folded in _TECHNICAL_KEYS:
                continue
            if folded in _NARRATIVE_KEYS:
                parts.extend(_extract_from_structured(item, parent_key=folded))
                continue
            # Contêineres comuns podem agrupar cenas/blocos; percorremos somente
            # objetos/listas e nunca transformamos strings desconhecidas em fala.
            if isinstance(item, (dict, list, tuple)):
                parts.extend(_extract_from_structured(item, parent_key=folded))
        return parts
    if isinstance(value, (list, tuple)):
        for item in value:
            parts.extend(_extract_from_structured(item, parent_key=parent_key))
        return parts
    if parent_key in _NARRATIVE_KEYS:
        text = str(value or "").strip()
        if text:
            parts.append(text)
    return parts


def _clean_line(line: str) -> str:
    value = str(line or "").strip()
    if not value:
        return ""
    value = _STAGE_DIRECTION.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip(" \t-–—")
    return value


def _extract_from_lines(text: str) -> List[str]:
    parts: List[str] = []
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _clean_line(raw_line)
        if not line:
            continue
        if _SCENE_ONLY.match(line):
            continue
        scene_prefixed = _SCENE_PREFIX.match(line)
        if scene_prefixed:
            line = _clean_line(scene_prefixed.group(1))
            if not line:
                continue
        technical = _TECHNICAL_LABEL.match(line)
        if technical:
            continue
        narrative = _NARRATIVE_LABEL.match(line)
        if narrative:
            line = _clean_line(narrative.group(1))
            if line:
                parts.append(line)
            continue
        # Cabeçalhos editoriais isolados não são fala.
        if re.match(
            r"(?ix)^\s*(?:roteiro|storyboard|dire[cç][aã]o|dire[cç][aã]o\s+visual|"
            r"instru[cç][oõ]es?|observa[cç][oõ]es?|notas?|metadados?)\s*:?\s*$",
            line,
        ):
            continue
        parts.append(line)
    return parts


def prepare_spoken_narration_text(text: Any) -> str:
    """Retorna somente conteúdo que pode ser enviado ao TTS.

    A função primeiro tenta extrair campos narrativos de JSON estruturado. Em
    texto comum, remove rótulos/linhas de produção e direções de palco. Ela não
    converte prompt visual em prosa. Qualquer resíduo técnico reconhecível após
    a extração provoca erro para que o chamador bloqueie o TTS (fail-closed).
    """
    raw = str(text or "").replace("\x00", " ").strip()
    if not raw:
        return ""

    parsed = _try_json(raw)
    if isinstance(parsed, (dict, list)):
        parts = _extract_from_structured(parsed)
        if not parts:
            raise ValueError("estrutura técnica sem campo narrativo seguro")
    else:
        parts = _extract_from_lines(raw)

    spoken = " ".join(_clean_line(part) for part in parts if _clean_line(part))
    spoken = re.sub(r"\s+", " ", spoken).strip()
    if not spoken:
        return ""
    if _TECHNICAL_RESIDUE.search(spoken):
        raise ValueError("resíduo técnico detectado após extração de texto falável")
    return spoken


__all__ = ["SPOKEN_TEXT_BOUNDARY_VERSION", "prepare_spoken_narration_text"]
