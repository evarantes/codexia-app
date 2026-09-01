from __future__ import annotations

from typing import Any, Dict, MutableMapping


VIDEO_CREATION_STANDARD_VERSION = 1
STANDARD_REQUIRED_CTA_SIGNALS = frozenset({"like", "subscribe", "bell", "share"})
STANDARD_COMPLETE_CTA = (
    "Se esta mensagem falou com você, curta este vídeo, inscreva-se no canal, "
    "ative o sininho para receber as próximas mensagens e compartilhe este vídeo "
    "com alguém que precisa ouvi-lo."
)

_STANDARD_STRUCTURE = {
    "opening": {
        "enabled": True,
        "style": "gancho emocional curto e temático desde o primeiro segundo",
    },
    "narration": {
        "enabled": True,
        "language": "pt-BR",
        "delivery": "humana, natural, acolhedora, reverente e emocional, sem leitura robótica",
        "plain_text_only": True,
    },
    "visuals": {
        "style": "cinematográfico, coerente com a narrativa, emocional e sem poluição visual",
        "avoid_long_static_holds": True,
    },
    "captions": {
        "enabled": True,
        "language": "pt-BR",
        "max_lines": 2,
        "sync_to_narration": True,
        "export_srt": True,
    },
    "background_music": {
        "enabled": True,
        "style": "instrumental suave, piano e cordas discretas, sem vocais",
        "duck_under_voice": True,
    },
    "closing": {
        "reflection": True,
        "cta": ["curtir", "inscrever-se", "ativar o sininho", "compartilhar"],
        "automatic_endcard": True,
    },
}

# Formatos especiais têm contratos próprios e não devem herdar automaticamente
# toda a estrutura de um vídeo narrado longo. Se o chamador quiser o perfil mesmo
# assim, ele pode usar um kind narrado normal e controlar duração/aspect ratio.
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


def _disabled(plan: MutableMapping[str, Any]) -> bool:
    for key in (
        "disable_standard_video_structure",
        "disable_codexia_video_standard",
        "skip_standard_video_structure",
    ):
        if plan.get(key) is True:
            return True
    explicit = plan.get("codexia_video_standard")
    return explicit is False


def should_apply_video_creation_standard(plan: Any) -> bool:
    if not isinstance(plan, MutableMapping):
        return False
    if _disabled(plan):
        return False
    kind = str(plan.get("kind") or plan.get("video_kind") or "").strip().lower()
    return kind not in _SKIP_KINDS


def apply_standard_video_structure(plan: Any) -> Any:
    """Apply the Codexia narrated-video defaults without overriding user choices.

    This is intentionally additive: explicit request fields always win. The
    standard defines structure and presentation, not a mandatory duration.
    """
    if not should_apply_video_creation_standard(plan):
        return plan

    assert isinstance(plan, MutableMapping)

    # Runtime defaults already understood by VideoGenerator.
    plan.setdefault("music_mood", "peaceful")
    plan.setdefault("music_mood_fallback", "drama")
    plan.setdefault(
        "music_prompt",
        "soft instrumental underscore, gentle piano and warm strings, reverent and emotional, "
        "no vocals, unobtrusive under spoken narration, smooth ending",
    )
    plan.setdefault("bg_music_volume", 0.025)
    plan.setdefault("prefer_peaceful_music", True)
    plan.setdefault("narrated_cta_text", STANDARD_COMPLETE_CTA)

    # Canonical presentation metadata. Existing renderer capabilities already
    # provide automatic opening/closing and SRT; these fields make the intent
    # explicit for every producer/router and future modules.
    plan.setdefault("captions_enabled", True)
    plan.setdefault("caption_language", "pt-BR")
    plan.setdefault("caption_max_lines", 2)
    plan.setdefault("export_srt", True)
    plan.setdefault("automatic_opening", True)
    plan.setdefault("automatic_closing", True)
    plan.setdefault("cta_required", True)
    plan.setdefault("voice_delivery_profile", "natural_warm_reverent_pt_br")

    existing = plan.get("codexia_video_standard")
    custom = dict(existing) if isinstance(existing, dict) else {}
    canonical: Dict[str, Any] = {
        "version": VIDEO_CREATION_STANDARD_VERSION,
        "profile": "narrated_cinematic_human",
        "duration_policy": "request_wins; standard_does_not_force_duration",
        **_STANDARD_STRUCTURE,
    }
    # A caller may extend descriptive metadata, but the standard version stays
    # observable and explicit request fields above remain authoritative.
    for key, value in custom.items():
        if key != "version":
            canonical[key] = value
    plan["codexia_video_standard"] = canonical
    plan["codexia_video_standard_version"] = VIDEO_CREATION_STANDARD_VERSION
    return plan


__all__ = [
    "VIDEO_CREATION_STANDARD_VERSION",
    "STANDARD_REQUIRED_CTA_SIGNALS",
    "STANDARD_COMPLETE_CTA",
    "should_apply_video_creation_standard",
    "apply_standard_video_structure",
]
