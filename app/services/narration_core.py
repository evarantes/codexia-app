from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List


NARRATION_CORE_VERSION = 1
NARRATION_CORE_NAMESPACE = "codexia-narration-core-v1"


class NarrationCoreError(ValueError):
    """Conteúdo inseguro/ambíguo que não pode alcançar um provedor TTS."""


@dataclass(frozen=True)
class NarrationArtifact:
    spoken_text: str
    text_sha256: str
    core_version: int
    namespace: str
    removed_technical_blocks: int
    source_kind: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_NARRATIVE_KEYS = {
    "text",
    "narration",
    "narration_text",
    "spoken_text",
    "voiceover",
    "voice_over",
    "speech",
    "locution",
    "locucao",
    "narracao",
    "opening_text",
    "body_text",
    "reflection",
    "reflection_text",
    "reflexao",
    "closing_message",
    "closing_text",
    "cta_text",
    "narrated_cta_text",
    "content",
}

_TECHNICAL_KEYS = {
    "image_prompt",
    "visual_prompt",
    "prompt_visual",
    "prompt_de_imagem",
    "prompt_imagem",
    "negative_prompt",
    "camera_movement",
    "movimento_camera",
    "movimento_de_camera",
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
    "prompt",
    "system_prompt",
    "payload",
}

_NARRATIVE_LABEL = re.compile(
    r"^\s*(?:narr(?:a[cç][aã]o|ation)|locu[cç][aã]o|voice\s*over|voiceover|fala|texto\s*narrado)\s*[:=\-–—]\s*(.*)$",
    re.IGNORECASE,
)
_TECHNICAL_LABEL = re.compile(
    r"^\s*(?:prompt\s*(?:visual|de\s*imagem|imagem)?|image\s*prompt|visual\s*prompt|"
    r"dura[cç][aã]o|duration|movimento\s*(?:de\s*)?c[aâ]mera|camera\s*movement|"
    r"texto\s*na\s*tela|on\s*screen\s*text|transi[cç][aã]o|transition|"
    r"efeito\s*sonoro|sound\s*effect|m[uú]sica|music|trilha|lighting|ilumina[cç][aã]o|"
    r"enquadramento|framing|negative\s*prompt|metadata|payload|render\s*report)\s*[:=\-–—]",
    re.IGNORECASE,
)
_SCENE_HEADER = re.compile(
    r"^\s*(?:cena|scene|take|shot|segmento|segment)\s*(?:#\s*)?\d+[A-Za-z]?\s*(?:[:=\-–—].*)?$",
    re.IGNORECASE,
)
_STAGE_DIRECTION = re.compile(
    r"[\[(]\s*(?:pausa|pause|break|sil[eê]ncio|respira[cç][aã]o|"
    r"tom\s+[^\])]+|voz\s+[^\])]+|m[uú]sica\s+[^\])]+|"
    r"c[aâ]mera\s+[^\])]+|camera\s+[^\])]+|efeito\s+[^\])]+)\s*[\])]",
    re.IGNORECASE,
)
_SAFE_SSML_CONTAINER = re.compile(
    r"(?is)</?\s*(?:speak|prosody|voice|p|s|emphasis|sub|phoneme)\b[^>]*>"
)
_BREAK_TAG = re.compile(r"(?is)<\s*break\b[^>]*?/?>")
_CODE_FENCE = re.compile(r"```|~~~")
_JSON_FIELD_RESIDUE = re.compile(
    r"(?i)[\"']?(?:image_prompt|visual_prompt|camera_movement|motion_effect|"
    r"on_screen_text|metadata|payload|render_report|system_prompt)[\"']?\s*:"
)
_TECH_RESIDUE = re.compile(
    r"(?im)^\s*(?:prompt\s*visual|prompt\s*de\s*imagem|image_prompt|visual_prompt|"
    r"dura[cç][aã]o|duration|movimento\s*(?:de\s*)?c[aâ]mera|camera_movement|"
    r"texto\s*na\s*tela|on_screen_text|metadata|payload|render_report)\s*[:=]"
)
_TEMPLATE_RESIDUE = re.compile(r"\{\{[^{}]+\}\}|\$\{[^{}]+\}")
_XML_RESIDUE = re.compile(r"(?is)<\s*/?\s*[A-Za-z][^>]*>")


def _fold_key(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    raw = "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")
    raw = raw.strip().lower().replace("-", "_").replace(" ", "_")
    return re.sub(r"_+", "_", raw)


def _normalize_plain_text(value: Any) -> str:
    raw = unicodedata.normalize("NFKC", html.unescape(str(value or ""))).replace("\x00", " ")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = _BREAK_TAG.sub(", ", raw)
    raw = _SAFE_SSML_CONTAINER.sub(" ", raw)
    raw = _STAGE_DIRECTION.sub(" ", raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\s+([,;:.!?])", r"\1", raw)
    raw = re.sub(r"([,;:.!?])(?=[^\s\n\"”’')\]])", r"\1 ", raw)
    return raw.strip()


def _collect_structured(value: Any, *, removed: List[int]) -> List[str]:
    out: List[str] = []
    if value is None:
        return out
    if isinstance(value, str):
        text, skipped = _extract_from_lines(value)
        removed[0] += skipped
        if text:
            out.append(text)
        return out
    if isinstance(value, (int, float, bool)):
        return out
    if isinstance(value, list):
        for item in value:
            out.extend(_collect_structured(item, removed=removed))
        return out
    if isinstance(value, dict):
        saw_narrative = False
        for key, item in value.items():
            normalized = _fold_key(key)
            if normalized in _TECHNICAL_KEYS:
                removed[0] += 1
                continue
            if normalized in _NARRATIVE_KEYS:
                saw_narrative = True
                out.extend(_collect_structured(item, removed=removed))
        # Estruturas neutras como scenes/segments podem conter campos narrativos.
        # Só percorremos os filhos que ainda não foram consumidos/descartados.
        for key, item in value.items():
            normalized = _fold_key(key)
            if normalized in _NARRATIVE_KEYS or normalized in _TECHNICAL_KEYS:
                continue
            if isinstance(item, (dict, list)):
                out.extend(_collect_structured(item, removed=removed))
        if not saw_narrative and not out:
            # Um objeto puramente técnico não pode virar fala por fallback.
            return []
    return out


def _extract_from_lines(value: str) -> tuple[str, int]:
    raw = _normalize_plain_text(value)
    removed = 0
    spoken: List[str] = []
    for original_line in raw.split("\n"):
        line = original_line.strip()
        if not line:
            continue
        if _CODE_FENCE.search(line):
            removed += 1
            continue
        if _SCENE_HEADER.match(line):
            removed += 1
            continue
        narrative_match = _NARRATIVE_LABEL.match(line)
        if narrative_match:
            candidate = narrative_match.group(1).strip()
            if candidate:
                spoken.append(candidate)
            removed += 1
            continue
        if _TECHNICAL_LABEL.match(line):
            removed += 1
            continue
        # Linhas serializadas/técnicas nunca são transformadas em prosa.
        if _JSON_FIELD_RESIDUE.search(line) or _TECH_RESIDUE.search(line):
            removed += 1
            continue
        spoken.append(line)

    text = " ".join(spoken)
    text = re.sub(r"\s+", " ", text).strip(" ,")
    return text, removed


def _loads_structured(value: str) -> Any:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except Exception:
        return None


def _assert_no_technical_residue(text: str) -> None:
    issues: List[str] = []
    if _CODE_FENCE.search(text):
        issues.append("code_fence")
    if _JSON_FIELD_RESIDUE.search(text):
        issues.append("technical_json_field")
    if _TECH_RESIDUE.search(text):
        issues.append("technical_label")
    if _TEMPLATE_RESIDUE.search(text):
        issues.append("template_placeholder")
    if _XML_RESIDUE.search(text):
        issues.append("xml_or_ssml_residue")
    if issues:
        raise NarrationCoreError(
            "conteúdo técnico residual detectado: " + ", ".join(sorted(set(issues)))
        )


def extract_spoken_text(value: Any) -> tuple[str, int, str]:
    """Retorna somente a fala segura, sem chamar TTS nem depender do renderer."""
    removed = [0]
    source_kind = "plain_text"

    if isinstance(value, (dict, list)):
        source_kind = "structured"
        pieces = _collect_structured(value, removed=removed)
        text = " ".join(pieces)
    else:
        raw = str(value or "")
        structured = _loads_structured(raw)
        if structured is not None:
            source_kind = "json"
            pieces = _collect_structured(structured, removed=removed)
            text = " ".join(pieces)
        else:
            text, skipped = _extract_from_lines(raw)
            removed[0] += skipped

    text = _normalize_plain_text(text)
    text = re.sub(r"\s+", " ", text).strip(" ,")
    if not text:
        raise NarrationCoreError("nenhum texto falável seguro foi encontrado")
    _assert_no_technical_residue(text)
    return text, int(removed[0]), source_kind


def build_narration_artifact(value: Any) -> NarrationArtifact:
    spoken_text, removed, source_kind = extract_spoken_text(value)
    digest = hashlib.sha256(spoken_text.encode("utf-8")).hexdigest()
    return NarrationArtifact(
        spoken_text=spoken_text,
        text_sha256=digest,
        core_version=NARRATION_CORE_VERSION,
        namespace=NARRATION_CORE_NAMESPACE,
        removed_technical_blocks=removed,
        source_kind=source_kind,
    )


def narration_fingerprint(*, spoken_text: str, voice: str = "", provider: str = "") -> str:
    artifact = build_narration_artifact(spoken_text)
    payload = "\n".join(
        [
            NARRATION_CORE_NAMESPACE,
            str(NARRATION_CORE_VERSION),
            str(provider or "").strip().lower(),
            str(voice or "").strip(),
            artifact.text_sha256,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def require_current_core(metadata: Dict[str, Any]) -> None:
    version = int((metadata or {}).get("narration_core_version") or 0)
    namespace = str((metadata or {}).get("narration_core_namespace") or "").strip()
    if version != NARRATION_CORE_VERSION or namespace != NARRATION_CORE_NAMESPACE:
        raise NarrationCoreError(
            "áudio criado por um núcleo antigo; gere uma nova narração antes de continuar"
        )


__all__ = [
    "NARRATION_CORE_VERSION",
    "NARRATION_CORE_NAMESPACE",
    "NarrationCoreError",
    "NarrationArtifact",
    "extract_spoken_text",
    "build_narration_artifact",
    "narration_fingerprint",
    "require_current_core",
]
