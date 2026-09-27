"""Contrato audiovisual portado da Fábrica de Vídeos para o Codexia.

Esta camada não cria um segundo renderizador. Ela normaliza o plano antes do
renderer canônico e acrescenta as garantias que provaram ser importantes no
aplicativo Windows:

* direção de câmera pertence à faixa visual, nunca à narração/legenda;
* identidade dos personagens é fixada antes das chamadas de imagem;
* movimentos locais continuam sendo o fallback econômico para imagens fixas;
* a trilha automática premium é opcional e tem fallback local/IA já existente.

O módulo é deliberadamente independente de Tkinter e de caminhos locais do
Windows para poder ser usado pelo worker RQ do Codexia.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests


FABRICA_PIPELINE_VERSION = "fabrica-structure-v1"

_CAMERA_DIRECTIVE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:a\s+)?c[âa]mera\b"
    r"|\bclose[\s-]?up\b"
    r"|\bplano\s+(?:fechado|geral|detalhe|aberto)\b"
    r"|\bfoca(?:liza)?\s+em\b"
    r"|\bfoco\s+em\b"
    r"|\bzoom\b"
    r"|\baproxima(?:ç[ãa]o|cao)\b"
    r"|\bmovimento\s+de\s+c[âa]mera\b"
    r"|\bcorte\s+para\b"
    r"|\bpan\s+(?:para|a|à)\b"
    r"|\bdesloca-se\b"
    r")"
)
_CAMERA_PARENTHESES_RE = re.compile(
    r"(?is)(\([^()]*?(?:c[âa]mera|close[\s-]?up|zoom|foco\s+em|foca\s+em|panor[âa]mica)[^()]*\)|"
    r"\[[^\[\]]*?(?:c[âa]mera|close[\s-]?up|zoom|foco\s+em|foca\s+em|panor[âa]mica)[^\[\]]*?\])"
)


def _enabled(name: str, default: str = "true") -> bool:
    return str(os.getenv(name) or default).strip().lower() in {
        "1", "true", "yes", "sim", "on", "enabled", "enable"
    }


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _scene_text(scene: Dict[str, Any]) -> str:
    for key in ("text", "narration_text", "narration", "content"):
        value = str(scene.get(key) or "").strip()
        if value:
            return value
    return ""


def _set_spoken_text(scene: Dict[str, Any], text: str) -> None:
    """Update every narration alias that was present, avoiding stale leakage."""
    spoken = str(text or "").strip()
    keys = [key for key in ("text", "narration_text", "narration", "content") if key in scene]
    if not keys:
        keys = ["text"]
    for key in keys:
        scene[key] = spoken


def split_visual_directions(text: str) -> Tuple[str, str]:
    """Separate explicit camera directions from words intended for speech.

    The split is conservative: once a sentence contains an explicit camera
    marker, the marker and the remaining technical instruction become visual
    metadata. Quoted dialogue before the marker remains spoken text.
    """
    normalized = _compact(text)
    if not normalized:
        return "", ""

    directions: List[str] = []

    def remove_parenthesized(match: re.Match[str]) -> str:
        value = match.group(1).strip("()[] ")
        if value:
            directions.append(value)
        return " "

    normalized = _CAMERA_PARENTHESES_RE.sub(remove_parenthesized, normalized)
    units = [part.strip() for part in re.split(r"(?<=[.!?…])\s+", normalized) if part.strip()]
    spoken: List[str] = []
    for unit in units or [normalized]:
        marker = _CAMERA_DIRECTIVE_RE.search(unit)
        if not marker:
            spoken.append(unit)
            continue
        prefix = unit[: marker.start()].strip(" ,;:-")
        direction = unit[marker.start():].strip()
        if prefix:
            spoken.append(prefix)
        if direction:
            directions.append(direction)

    return _compact(" ".join(spoken)), _compact(" ".join(directions))


def camera_effect_from_direction(direction: str) -> Dict[str, str]:
    """Map Portuguese camera notes to the local MoviePy motion vocabulary."""
    value = _compact(direction)
    lower = value.lower()
    if not value:
        return {"type": "none", "target": "", "side": "center", "direction": ""}

    target_match = re.search(
        r"(?i)\b(?:foco|foca|focaliza|close[\s-]?up|aproxima(?:ç[ãa]o|cao))\s+"
        r"(?:em|de|no|na|nos|nas)\s+([^,.;!?]+)",
        value,
    )
    target = _compact(target_match.group(1)).strip(" \"'()[]") if target_match else ""
    if len(target.split()) > 5:
        target = " ".join(target.split()[:5])

    if "direita" in lower or "lado direito" in lower:
        side = "right"
    elif "esquerda" in lower or "lado esquerdo" in lower:
        side = "left"
    else:
        side = "center"

    if "panor" in lower or re.search(r"\bpan\b", lower) or "desloca-se" in lower:
        effect_type = "pan"
        direction_name = "right" if side == "right" or "para a direita" in lower else "left"
    elif any(token in lower for token in ("foco", "foca", "focaliza", "close", "zoom", "aproxima")):
        effect_type = "focus"
        direction_name = ""
    else:
        effect_type = "slow_zoom"
        direction_name = ""

    return {
        "type": effect_type,
        "target": target,
        "side": side,
        "direction": direction_name,
    }


def _local_motion_name(camera_effect: Dict[str, Any]) -> str:
    effect_type = str((camera_effect or {}).get("type") or "none").strip().lower()
    side = str((camera_effect or {}).get("side") or "center").strip().lower()
    direction = str((camera_effect or {}).get("direction") or "").strip().lower()
    if effect_type == "pan":
        return "pan_right" if side == "right" or direction == "right" else "pan_left"
    if effect_type == "focus":
        return "push_in"
    if effect_type == "slow_zoom":
        return "slow_zoom"
    return "ambient"


def _first_nonempty(mapping: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _character_entries(value: Any) -> List[Tuple[str, str]]:
    entries: List[Tuple[str, str]] = []
    if isinstance(value, dict):
        # Accept both {"Maria": "..."} and the director's list-like dicts.
        if any(key in value for key in ("name", "character", "id")):
            name = _compact(value.get("name") or value.get("character") or value.get("id"))
            description = _compact(
                value.get("fixed_visual_description")
                or value.get("description")
                or value.get("appearance")
                or value.get("wardrobe")
                or value.get("continuity_rules")
            )
            if name or description:
                entries.append((name or "Personagem recorrente", description or "identidade definida na primeira aparição"))
        else:
            for name, description in value.items():
                if isinstance(description, (dict, list)):
                    description = json.dumps(description, ensure_ascii=False)
                entries.append((_compact(name), _compact(description)))
    elif isinstance(value, list):
        for item in value:
            entries.extend(_character_entries(item))
    elif isinstance(value, str) and value.strip():
        entries.append(("Bíblia visual do canal", _compact(value)))
    return [(name, description) for name, description in entries if name or description]


def _automatic_character_names(plan: Dict[str, Any], scenes: List[Dict[str, Any]]) -> List[str]:
    source = " ".join([str(plan.get("title") or "")] + [_scene_text(scene) for scene in scenes[:12]])
    known = (
        "Jesus", "Cristo", "Maria", "Marta", "Pedro", "João", "Joao", "José", "Jose",
        "Davi", "Elias", "Ester", "Rute", "Moisés", "Moises", "Paulo", "Lúcia", "Lucia",
        "Juninho", "Dona Cida", "Zé Ninguém", "Ze Ninguem",
    )
    found: List[str] = []
    folded = source.lower()
    for name in known:
        if name.lower() in folded and name not in found:
            found.append(name)
    for token in re.findall(r"\b[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{2,}\b", source):
        if token not in found and token.lower() not in {"Hoje", "Quando", "Então", "Depois", "Aquela", "Neste"}:
            found.append(token)
    return found[:8]


def character_bible_prompt(plan: Dict[str, Any], scenes: List[Dict[str, Any]]) -> Dict[str, Any]:
    candidates = [
        plan.get("character_bible"),
        plan.get("visual_bible"),
        plan.get("channel_visual_bible"),
        plan.get("channel_character_bible"),
        plan.get("characters"),
    ]
    entries: List[Tuple[str, str]] = []
    source = "automatic_identity_lock"
    for candidate in candidates:
        parsed = _character_entries(candidate)
        if parsed:
            entries = parsed[:24]
            source = "plan_character_bible"
            break

    names = _automatic_character_names(plan, scenes)
    if not entries and names:
        entries = [
            (name, "A aparência deve ser definida na primeira aparição e repetida exatamente nas cenas seguintes; variar apenas expressão, ação e enquadramento.")
            for name in names
        ]

    lines = [
        "BÍBLIA VISUAL FIXA — aplicar em TODAS as imagens deste episódio.",
        "Preserve rigorosamente o mesmo rosto, idade aparente, tom de pele, cabelo, proporções, roupa-base, acessórios e paleta de cada personagem recorrente.",
        "Não troque identidade entre cenas. Mudanças de roupa ou expressão só podem ocorrer quando a ação exigir, sem perder reconhecimento.",
        "Continuidade visual não significa repetir pose, ambiente ou enquadramento: varie a composição mantendo o elenco coerente.",
    ]
    if entries:
        lines.append("Personagens e regras fixas:")
        for name, description in entries:
            lines.append(f"- {name}: {description[:600]}")
    else:
        lines.append("Nenhuma ficha foi fornecida: estabeleça a identidade na primeira aparição e reutilize-a exatamente nas cenas seguintes.")
    lines.append("Não inserir texto, legenda, logotipo ou marca d'água na imagem gerada.")
    return {
        "source": source,
        "characters": [name for name, _ in entries],
        "prompt": " ".join(lines),
    }


def apply_fabrica_scene_contract(plan: Any) -> Tuple[Any, Dict[str, Any]]:
    """Prepare a plan for the canonical Codexia renderer."""
    report: Dict[str, Any] = {
        "version": FABRICA_PIPELINE_VERSION,
        "generated_at": _utc_iso(),
        "enabled": _enabled("ENABLE_FABRICA_PIPELINE", "true"),
        "directions_separated": 0,
        "narration_fallbacks": 0,
        "scenes_changed": 0,
        "source_text_preserved": True,
        "motion_effects": {},
        "caption_policy": {
            "max_lines": 2,
            "anchor": "bottom",
            "source": "real_audio_timeline",
        },
        "duration_policy": {
            "audio_is_authority": True,
            "never_cut_complete_narration": True,
            "visual_hold_fallback_sec": "renderer_default",
        },
    }
    if not report["enabled"] or not isinstance(plan, dict):
        report["enabled"] = False
        return plan, report

    directed = deepcopy(plan)
    raw_scenes = directed.get("scenes")
    scenes = [item for item in raw_scenes if isinstance(item, dict)] if isinstance(raw_scenes, list) else []
    bible = character_bible_prompt(directed, scenes)
    report["character_bible"] = {
        "source": bible["source"],
        "characters": list(bible["characters"]),
        "automatic": bible["source"] == "automatic_identity_lock",
    }

    visual_only_suffix = (
        " DIREÇÃO VISUAL — executar somente na câmera/movimento; nunca narrar, nunca transformar em legenda "
        "e nunca inserir como texto na imagem."
    )
    for index, scene in enumerate(scenes):
        original = _scene_text(scene)
        spoken, direction = split_visual_directions(original)
        scene["_fabrica_original_text"] = original
        if direction:
            effect = camera_effect_from_direction(direction)
            scene["visual_direction"] = direction
            scene["camera_effect"] = effect
            scene["camera_movement"] = direction
            scene["motion_effect"] = _local_motion_name(effect)
            report["directions_separated"] += 1
            report["motion_effects"][_local_motion_name(effect)] = report["motion_effects"].get(_local_motion_name(effect), 0) + 1
            if not spoken:
                spoken = _compact(scene.get("title") or "A cena continua.")
                report["narration_fallbacks"] += 1
            _set_spoken_text(scene, spoken)
            report["scenes_changed"] += 1
        elif not original:
            spoken = _compact(scene.get("title") or "A cena continua.")
            _set_spoken_text(scene, spoken)
            report["narration_fallbacks"] += 1
            report["scenes_changed"] += 1

        prompt = _compact(scene.get("image_prompt") or scene.get("visual_prompt") or spoken or original)
        if direction:
            prompt = f"{prompt}.{visual_only_suffix} {direction}."
            scene["fabrica_visual_contract"] = {
                "direction": direction,
                "camera_effect": dict(scene.get("camera_effect") or {}),
                "spoken_text": spoken,
            }
        if prompt:
            scene["image_prompt"] = prompt[:3600]

    if scenes:
        directed["scenes"] = scenes
        # The scene narration is the canonical spoken source. Keep a parallel
        # full_script synchronized when a caller supplied one, otherwise the
        # duration contract could count technical directions as speech.
        canonical_script = "\n\n".join(_scene_text(scene) for scene in scenes if _scene_text(scene)).strip()
        if canonical_script and any(key in directed for key in ("full_script", "script", "narration", "text")):
            for key in ("full_script", "script"):
                if key in directed and isinstance(directed.get(key), str):
                    directed[key] = canonical_script

    directed["fabrica_pipeline"] = {
        "version": FABRICA_PIPELINE_VERSION,
        "directions_removed_from_speech": True,
        "character_bible_source": bible["source"],
        "character_bible_characters": list(bible["characters"]),
        "caption_max_lines": 2,
        "caption_anchor": "bottom",
        "audio_authoritative": True,
    }
    directed["fabrica_character_bible_prompt"] = bible["prompt"]
    report["scene_count"] = len(scenes)
    return directed, report


def build_instrumental_music_prompt(plan: Dict[str, Any]) -> str:
    """Build a copyright-safe, narration-friendly music brief."""
    title = _compact(plan.get("title") or plan.get("theme") or "episódio")
    tone = _compact(plan.get("tone") or plan.get("mood") or "cinematográfico, emocional e esperançoso")
    scenes = plan.get("scenes") if isinstance(plan.get("scenes"), list) else []
    topics = ", ".join(_compact(_scene_text(scene))[:100] for scene in scenes[:8] if isinstance(scene, dict))
    return (
        "Original instrumental underscore for a Brazilian Portuguese narrated YouTube video. "
        "No vocals, no lyrics, no spoken words, no recognizable copyrighted melody. "
        "Keep space for narration, subtle dynamics, gentle opening, restrained development, "
        "a modest emotional peak and a soft ending suitable for ducking. "
        f"Episode: {title}. Tone: {tone}. Story beats: {topics or 'opening, development, reflection and conclusion'}."
    )[:4100]


def generate_eleven_music_track(
    plan: Dict[str, Any],
    output_dir: str,
    duration_seconds: float,
    *,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """Generate a cached Eleven Music track when explicitly configured.

    Returning ``None`` is intentional: the canonical renderer then uses its
    existing AI/local fallback instead of making the whole video fail because
    the optional soundtrack provider is unavailable.
    """
    provider = str(
        (plan or {}).get("music_provider")
        or os.getenv("FABRICA_MUSIC_PROVIDER")
        or "auto"
    ).strip().lower()
    api_key = str((plan or {}).get("elevenlabs_api_key") or os.getenv("ELEVENLABS_API_KEY") or "").strip()
    if provider not in {"auto", "elevenlabs", "eleven-music", "eleven_music"} or not api_key:
        return None

    model_id = str((plan or {}).get("music_model") or os.getenv("ELEVENLABS_MUSIC_MODEL") or "music_v2_5").strip()
    duration = max(3, min(600, int(round(float(duration_seconds or 300)))))
    music_prompt = build_instrumental_music_prompt(plan)
    cache_identity = f"{model_id}:{duration}:{music_prompt}"
    prompt_fingerprint = hashlib.sha256(cache_identity.encode("utf-8")).hexdigest()[:20]
    output_path = Path(output_dir) / f"fabrica_music_{prompt_fingerprint}.mp3"
    if output_path.is_file() and output_path.stat().st_size > 1000:
        return {"path": str(output_path), "provider": f"Eleven Music ({model_id})", "cached": True, "prompt": music_prompt}

    if progress_callback:
        progress_callback(90, f"Trilha sonora: Eleven Music ({model_id})...")
    payload = {
        "prompt": music_prompt,
        "music_length_ms": duration * 1000,
        "model_id": model_id,
        "force_instrumental": True,
    }
    try:
        response = requests.post(
            "https://api.elevenlabs.io/v1/music?output_format=mp3_48000_192",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json=payload,
            timeout=(20, max(120, min(1200, int(os.getenv("FABRICA_MUSIC_TIMEOUT_SECONDS") or "900")))),
        )
        if not response.ok or len(response.content or b"") <= 1000:
            return None
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        temp_fd, temp_name = tempfile.mkstemp(prefix="fabrica_music_", suffix=".part", dir=str(output_dir))
        os.close(temp_fd)
        temp_path = Path(temp_name)
        try:
            temp_path.write_bytes(response.content)
            os.replace(temp_path, output_path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
        return {"path": str(output_path), "provider": f"Eleven Music ({model_id})", "cached": False, "prompt": payload["prompt"]}
    except Exception:
        return None


def install_fabrica_pipeline_patch(video_generator_cls: Any) -> Any:
    """Install the contract on the class used by the RQ worker."""
    if getattr(video_generator_cls, "_codexia_fabrica_pipeline_installed", False):
        return video_generator_cls

    original_create = getattr(video_generator_cls, "create_video_from_plan", None)
    original_image = getattr(video_generator_cls, "_ensure_image_for_scene", None)

    if callable(original_create):
        def create_with_fabrica(self: Any, plan: Any, *args: Any, **kwargs: Any):
            directed_plan, report = apply_fabrica_scene_contract(plan)
            previous_bible = getattr(self, "_codexia_fabrica_character_bible", "")
            bible_prompt = str((directed_plan or {}).get("fabrica_character_bible_prompt") or "") if isinstance(directed_plan, dict) else ""
            self._codexia_fabrica_character_bible = bible_prompt
            try:
                result = original_create(self, directed_plan, *args, **kwargs)
            finally:
                self._codexia_fabrica_character_bible = previous_bible
            if isinstance(result, dict):
                result["fabrica_pipeline"] = deepcopy(report)
                render_report = result.get("render_report") if isinstance(result.get("render_report"), dict) else {}
                render_report["fabrica_pipeline"] = deepcopy(report)
                render_report.setdefault("visual_plan", {})
                render_report["visual_plan"]["character_identity_lock"] = report.get("character_bible")
                render_report["visual_plan"]["fabrica_motion_contract"] = {
                    "explicit_directions_executed_locally": int(report.get("directions_separated") or 0),
                    "ambient_motion_for_static_scenes": True,
                    "fallback": "ffmpeg_local",
                }
                result["render_report"] = render_report
                try:
                    ai_service = getattr(self, "ai_service", None)
                    task_id = getattr(ai_service, "ai_task_id", None) if ai_service is not None else None
                    if task_id:
                        from app.services.task_manager import merge_task_result
                        merge_task_result(str(task_id), {
                            "fabrica_pipeline": deepcopy(report),
                            "render_report": deepcopy(render_report),
                        })
                except Exception:
                    pass
            return result
        video_generator_cls.create_video_from_plan = create_with_fabrica

    if callable(original_image):
        def image_with_character_bible(self: Any, prompt: str, *args: Any, **kwargs: Any):
            bible = str(getattr(self, "_codexia_fabrica_character_bible", "") or "").strip()
            if bible and _enabled("ENABLE_FABRICA_CHARACTER_BIBLE", "true"):
                prompt = f"{bible} {str(prompt or '').strip()}"
                prompt = prompt[:5200]
            return original_image(self, prompt, *args, **kwargs)
        video_generator_cls._ensure_image_for_scene = image_with_character_bible

    video_generator_cls._codexia_fabrica_pipeline_installed = True
    return video_generator_cls
