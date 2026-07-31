import hashlib
import json
import re
import unicodedata
from typing import Any, Dict, List


_TITLE_MARKER_PATTERN = re.compile(
    r"(?i)\s*[\(\[\-–—]?\s*"
    r"(reflex[aã]o|devocional|hist[oó]ria\s+b[ií]blica|ora[cç][aã]o|mensagem)"
    r"\s*[\)\]]?\s*$"
)


def normalize_text_for_fingerprint(value: Any, *, lower: bool = False) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.lower() if lower else text


def normalize_list_for_fingerprint(values: Any, *, lower: bool = False) -> List[str]:
    items: List[str] = []
    if isinstance(values, list):
        for item in values:
            text = normalize_text_for_fingerprint(item, lower=lower)
            if text:
                items.append(text)
    return sorted(items)


def strip_youtube_title_markers(title: Any) -> str:
    text = normalize_text_for_fingerprint(title)
    if not text:
        return ""
    previous = None
    while previous != text:
        previous = text
        text = _TITLE_MARKER_PATTERN.sub("", text).strip(" -–—")
    return re.sub(r"\s+", " ", text).strip()


def sanitize_narrated_title(title: Any) -> str:
    cleaned = strip_youtube_title_markers(title)
    return cleaned or normalize_text_for_fingerprint(title)


def build_video_content_fingerprint_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    mode = normalize_text_for_fingerprint(payload.get("mode") or "topic", lower=True) or "topic"
    kind = normalize_text_for_fingerprint(payload.get("kind") or "story", lower=True) or "story"
    image_mode = normalize_text_for_fingerprint(payload.get("image_mode") or "", lower=True)
    voice_style = normalize_text_for_fingerprint(payload.get("voice_style") or "", lower=True)
    voice_gender = normalize_text_for_fingerprint(payload.get("voice_gender") or "", lower=True)
    aspect_ratio = normalize_text_for_fingerprint(payload.get("aspect_ratio") or "16:9", lower=True) or "16:9"
    youtube_title = normalize_text_for_fingerprint(payload.get("override_title") or payload.get("topic") or "")
    narrated_title = sanitize_narrated_title(youtube_title)
    internal_title = normalize_text_for_fingerprint(
        payload.get("topic") or payload.get("override_title") or narrated_title or youtube_title,
        lower=True,
    )
    canonical = {
        "mode": mode,
        "kind": kind,
        "topic": normalize_text_for_fingerprint(payload.get("topic") or ""),
        "story_content": normalize_text_for_fingerprint(payload.get("story_content") or ""),
        "duration": max(1, min(60, int(payload.get("duration") or 5))),
        "aspect_ratio": aspect_ratio,
        "voice_style": voice_style,
        "voice_gender": voice_gender,
        "image_mode": image_mode,
        "selected_images": normalize_list_for_fingerprint(payload.get("selected_images") or []),
        "custom_image_paths": normalize_list_for_fingerprint(payload.get("custom_image_paths") or []),
        "title_theme": narrated_title or internal_title,
        "narrated_title": narrated_title,
        "internal_title": internal_title,
    }
    return canonical


def build_video_content_fingerprint(payload: Dict[str, Any]) -> Dict[str, Any]:
    canonical = build_video_content_fingerprint_payload(payload)
    canonical_json = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return {
        "canonical_payload": canonical,
        "canonical_json": canonical_json,
        "content_fingerprint": digest,
        "internal_title": canonical.get("internal_title") or "",
        "youtube_title": canonical.get("youtube_title") or "",
        "narrated_title": canonical.get("narrated_title") or "",
    }
