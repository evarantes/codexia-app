from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "app/services/channel_excellence_guard.py"
YOUTUBE = ROOT / "app/routers/youtube.py"
MARKER = "CODEXIA_FINAL_QUALITY_POSTRENDER_V1"
MARKER_EDITORIAL = "CODEXIA_TTS_VISUAL_PUBLICATION_QUALITY_V1"
MARKER_AUTO_UPLOAD = "CODEXIA_AUTO_UPLOAD_QUALITY_CONTEXT_V1"


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1)


def _insert_before_once(text: str, anchor: str, insertion: str, label: str) -> str:
    if insertion in text:
        return text
    count = text.count(anchor)
    if count != 1:
        raise PatchError(f"{label}: âncora esperada 1 vez, encontrada {count}")
    return text.replace(anchor, insertion + anchor, 1)


HELPERS = '''\n\n# CODEXIA_FINAL_QUALITY_POSTRENDER_V1\n# CODEXIA_TTS_VISUAL_PUBLICATION_QUALITY_V1\ndef _quality_normalize_text(value: Any) -> str:\n    folded = _fold(value)\n    return re.sub(r"[^a-z0-9]+", " ", folded).strip()\n\n\ndef _quality_text_contains(container: Any, fragment: Any) -> bool:\n    \"\"\"Compara conteúdo sem depender de pontuação/acento do TTS/legenda.\"\"\"\n    needle = _quality_normalize_text(fragment)\n    if not needle:\n        return True\n    haystack = _quality_normalize_text(container)\n    if not haystack:\n        return False\n    if needle in haystack:\n        return True\n    words = needle.split()\n    if len(words) < 6:\n        return False\n    width = min(10, len(words))\n    lead = " ".join(words[:width])\n    tail = " ".join(words[-width:])\n    return lead in haystack and tail in haystack\n\n\ndef _postrender_candidate_exists(result: Any) -> bool:\n    if not isinstance(result, dict):\n        return False\n    return bool(str(result.get("file_path") or result.get("video_url") or "").strip())\n\n\ndef _quality_spoken_text_contract(raw_text: Any) -> Dict[str, Any]:\n    \"\"\"Confirma que o texto canônico enviado ao TTS era texto puro.\n\n    O saneador remove somente diretivas editoriais seguras (por exemplo\n    ``<break time=...>``). Se o registro canônico muda ao sanear, o render\n    antigo foi produzido a partir de markup técnico e não pode ser publicado.\n    Resíduo de JSON/código continua fail-closed.\n    \"\"\"\n    raw = str(raw_text or "").strip()\n    if not raw:\n        return {\n            "checked": False,\n            "passed": True,\n            "reason": "canonical_tts_text_missing",\n            "sanitized_changed": False,\n            "issues": [],\n        }\n    try:\n        from app.services.narration_contract_guard import sanitize_narration_text, structural_issues\n        sanitized = sanitize_narration_text(raw)\n        issues = list(structural_issues(sanitized))\n    except Exception as exc:\n        return {\n            "checked": True,\n            "passed": False,\n            "reason": "tts_contract_check_failed",\n            "sanitized_changed": False,\n            "issues": [f"contract_error:{type(exc).__name__}"],\n        }\n    changed = sanitized != raw\n    return {\n        "checked": True,\n        "passed": bool(not changed and not issues),\n        "reason": "plain_text_only" if not changed and not issues else "technical_markup_in_tts_source",\n        "sanitized_changed": bool(changed),\n        "issues": issues,\n    }\n\n\ndef _quality_video_duration_seconds(rr: Dict[str, Any], plan: Any = None) -> float:\n    duration_plan = rr.get("duration_plan") if isinstance(rr.get("duration_plan"), dict) else {}\n    candidates = (\n        duration_plan.get("obtained_duration_sec"),\n        duration_plan.get("target_video_duration_sec"),\n        duration_plan.get("actual_audio_duration_sec"),\n        rr.get("narration_duration_sec"),\n        rr.get("requested_duration_sec"),\n    )\n    for value in candidates:\n        try:\n            seconds = float(value or 0.0)\n        except (TypeError, ValueError):\n            seconds = 0.0\n        if seconds > 0.0:\n            return seconds\n    return float(_duration_target_seconds(plan) or 0.0)\n\n\ndef _quality_scene_count(rr: Dict[str, Any], plan: Any = None) -> int:\n    scene_visuals = rr.get("scene_visuals") if isinstance(rr.get("scene_visuals"), list) else []\n    if scene_visuals:\n        return len(scene_visuals)\n    if isinstance(plan, dict) and isinstance(plan.get("scenes"), list):\n        return len(plan.get("scenes") or [])\n    return 0\n\n\ndef _quality_unique_visual_count(rr: Dict[str, Any], visual: Dict[str, Any]) -> int:\n    scene_visuals = rr.get("scene_visuals") if isinstance(rr.get("scene_visuals"), list) else []\n    paths = set()\n    for item in scene_visuals:\n        if not isinstance(item, dict):\n            continue\n        path = str(\n            item.get("image_path")\n            or item.get("selected_image_path")\n            or item.get("visual_path")\n            or ""\n        ).strip()\n        if path:\n            paths.add(path)\n    try:\n        generated = int(visual.get("generated_image_count") or 0)\n    except (TypeError, ValueError):\n        generated = 0\n    return max(generated, len(paths))\n\n\ndef _quality_visual_density(rr: Dict[str, Any], visual: Dict[str, Any], plan: Any = None) -> Dict[str, Any]:\n    duration_sec = _quality_video_duration_seconds(rr, plan)\n    scene_count = _quality_scene_count(rr, plan)\n    actual_unique = _quality_unique_visual_count(rr, visual)\n    try:\n        seconds_per_image = float(os.getenv("VIDEO_VISUAL_QUALITY_SECONDS_PER_IMAGE") or "15")\n    except (TypeError, ValueError):\n        seconds_per_image = 15.0\n    seconds_per_image = max(10.0, min(30.0, seconds_per_image))\n\n    # Vídeos curtos não recebem uma regra artificialmente pesada. Em longos,\n    # a meta é uma troca visual aproximadamente a cada 15s, sem jamais exigir\n    # mais imagens únicas do que cenas narrativas disponíveis.\n    if duration_sec < 60.0 or actual_unique <= 0:\n        required = 1 if duration_sec > 0 else 0\n        checked = bool(duration_sec > 0 and actual_unique > 0)\n    else:\n        required = max(1, int(__import__("math").ceil(duration_sec / seconds_per_image)))\n        if scene_count > 0:\n            required = min(required, scene_count)\n        checked = True\n\n    passed = bool(not checked or actual_unique >= required)\n    actual_seconds_per_image = (duration_sec / actual_unique) if duration_sec > 0 and actual_unique > 0 else 0.0\n    return {\n        "checked": checked,\n        "passed": passed,\n        "duration_sec": round(duration_sec, 2),\n        "scene_count": int(scene_count),\n        "required_unique_image_count": int(required),\n        "actual_unique_image_count": int(actual_unique),\n        "visual_density_deficit": max(0, int(required) - int(actual_unique)),\n        "target_seconds_per_unique_image": round(seconds_per_image, 2),\n        "actual_seconds_per_unique_image": round(actual_seconds_per_image, 2),\n        "reason": "visual_density_ok" if passed else "too_few_unique_visuals_for_duration",\n    }\n\n'''


TTS_QUALITY_BLOCK = '''    # CODEXIA_TTS_VISUAL_PUBLICATION_QUALITY_V1\n    text_integrity_pre = rr.get("text_integrity") if isinstance(rr.get("text_integrity"), dict) else {}\n    audio_generation_pre = rr.get("audio_generation") if isinstance(rr.get("audio_generation"), dict) else {}\n    canonical_tts_text = str(\n        text_integrity_pre.get("final_text_sent_to_tts")\n        or audio_generation_pre.get("final_text_sent_to_tts")\n        or narration.get("full_text")\n        or ""\n    ).strip()\n    tts_contract = _quality_spoken_text_contract(canonical_tts_text)\n    report["checks"]["tts_plain_text_only"] = bool(tts_contract.get("passed"))\n    report["tts_text_contract"] = tts_contract\n    if tts_contract.get("checked") and not tts_contract.get("passed"):\n        report["violations"].append("tts_text_contains_technical_markup")\n\n'''


DENSITY_BLOCK = '''    # CODEXIA_TTS_VISUAL_PUBLICATION_QUALITY_V1\n    visual_density = _quality_visual_density(rr, visual, plan)\n    report["checks"]["visual_density_ok"] = bool(visual_density.get("passed"))\n    report["visual_density"] = visual_density\n    report["metrics"].update({\n        "required_unique_image_count": int(visual_density.get("required_unique_image_count") or 0),\n        "actual_unique_image_count": int(visual_density.get("actual_unique_image_count") or 0),\n        "visual_density_deficit": int(visual_density.get("visual_density_deficit") or 0),\n        "target_seconds_per_unique_image": float(visual_density.get("target_seconds_per_unique_image") or 0.0),\n        "actual_seconds_per_unique_image": float(visual_density.get("actual_seconds_per_unique_image") or 0.0),\n    })\n    visual_density_below_target = bool(\n        visual_density.get("checked") and not visual_density.get("passed")\n    )\n    auto_publish_requested = bool(\n        isinstance(plan, dict)\n        and (plan.get("_codexia_auto_upload_requested") or plan.get("auto_upload"))\n    )\n    report["auto_publish_requested"] = auto_publish_requested\n    if visual_density_below_target and auto_publish_requested:\n        # Auto-publicação nunca pode enviar um vídeo editorialmente pobre.\n        # Em recuperação, o MP4 continua preservado e nenhuma mídia paga é\n        # regenerada automaticamente; apenas a publicação fica bloqueada.\n        report["violations"].append("visual_density_below_publication_target")\n\n'''


CLOSING_OLD = '''    closing = str(narration.get("closing_text") or "").strip()\n    no_hidden_spoken_cta = not bool(closing)\n    report["checks"]["no_hidden_spoken_cta"] = no_hidden_spoken_cta\n    if not no_hidden_spoken_cta:\n        report["violations"].append("hidden_spoken_closing")'''

CLOSING_NEW = '''    closing = str(narration.get("closing_text") or "").strip()\n    text_integrity = rr.get("text_integrity") if isinstance(rr.get("text_integrity"), dict) else {}\n    audio_generation = rr.get("audio_generation") if isinstance(rr.get("audio_generation"), dict) else {}\n    sync_validation = rr.get("sync_validation") if isinstance(rr.get("sync_validation"), dict) else {}\n    canonical_spoken = str(\n        text_integrity.get("final_text_sent_to_tts")\n        or audio_generation.get("final_text_sent_to_tts")\n        or ""\n    ).strip()\n    caption_source = str(text_integrity.get("captions_source_text") or "").strip()\n    closing_in_spoken = _quality_text_contains(canonical_spoken, closing) if closing else True\n    closing_in_captions = _quality_text_contains(caption_source, closing) if closing else True\n    captions_verified = bool(\n        text_integrity.get("captions_match_narration_source")\n        or sync_validation.get("captions_synced_with_audio")\n    )\n    # "hidden_spoken_closing" só é verdadeiro quando o fechamento não pode ser\n    # comprovado na voz/timeline. A simples existência de closing_text nunca\n    # deve reprovar um encerramento realmente narrado e legendado.\n    no_hidden_spoken_cta = bool(\n        not closing\n        or (closing_in_spoken and (closing_in_captions or captions_verified))\n    )\n    report["checks"]["no_hidden_spoken_cta"] = no_hidden_spoken_cta\n    report["checks"]["closing_present_in_canonical_audio"] = bool(closing_in_spoken)\n    report["checks"]["closing_present_in_captions"] = bool(closing_in_captions or captions_verified)\n    if closing and not no_hidden_spoken_cta:\n        report["violations"].append("hidden_spoken_closing")'''


VISUAL_WARNINGS_OLD = '''    visual_warnings = []\n    if not no_path_reuse:\n        visual_warnings.append("generated_image_path_reused")\n    if not pacing_ok:\n        visual_warnings.append("visual_hold_too_long")'''

VISUAL_WARNINGS_NEW = '''    visual_warnings = []\n    if not no_path_reuse:\n        visual_warnings.append("generated_image_path_reused")\n    if not pacing_ok:\n        visual_warnings.append("visual_hold_too_long")\n    if visual_density_below_target:\n        visual_warnings.append("visual_density_below_quality_target")'''


CLASSIFY_OLD = '''    # Violações anteriores continuam bloqueantes. Os alertas visuais são\n    # mantidos para revisão humana sem invalidar um render já concluído.\n    blocking_violations = list(report["violations"])\n    report["violations"].extend(visual_warnings)\n    report["warnings"] = visual_warnings\n    report["blocking_violations"] = blocking_violations\n    report["review_recommended"] = bool(visual_warnings)\n    report["auto_render_preserved"] = bool(visual_warnings)\n    report["passed"] = not blocking_violations\n    return report'''

CLASSIFY_NEW = '''    # CODEXIA_FINAL_QUALITY_POSTRENDER_V1\n    # Se um MP4 candidato já foi produzido, um hidden_spoken_closing residual\n    # é uma divergência editorial/timeline para REVISÃO, não motivo para destruir\n    # o render ou induzir nova mídia paga. Outros blockers (inclusive orçamento\n    # e texto técnico enviado ao TTS) continuam fail-closed. Alertas visuais\n    # preservam o arquivo, mas deixam publication_ready=False.\n    postrender_candidate = _postrender_candidate_exists(result)\n    existing_violations = list(report["violations"])\n    editorial_warnings = [\n        code for code in existing_violations\n        if code == "hidden_spoken_closing" and postrender_candidate\n    ]\n    blocking_violations = [\n        code for code in existing_violations\n        if code not in editorial_warnings\n    ]\n    combined_warnings = list(dict.fromkeys(editorial_warnings + visual_warnings))\n    report["violations"].extend(visual_warnings)\n    report["warnings"] = combined_warnings\n    report["editorial_warnings"] = editorial_warnings\n    report["blocking_violations"] = blocking_violations\n    report["review_recommended"] = bool(combined_warnings)\n    report["auto_render_preserved"] = bool(\n        visual_warnings or (postrender_candidate and editorial_warnings)\n    )\n    report["late_quality_policy"] = "preserve_valid_render_review_first_v1"\n    report["passed"] = not blocking_violations\n    report["publication_ready"] = bool(report["passed"] and not combined_warnings)\n    if not report["publication_ready"]:\n        reasons = list(dict.fromkeys(blocking_violations + combined_warnings))\n        report["publication_block_reasons"] = reasons\n    else:\n        report["publication_block_reasons"] = []\n    return report'''


AUTO_UPLOAD_OLD = '''        video_result = video_service.create_video_from_plan(\n            script,'''

AUTO_UPLOAD_NEW = '''        # CODEXIA_AUTO_UPLOAD_QUALITY_CONTEXT_V1\n        # Propaga a intenção de publicação para o gate editorial ANTES do render.\n        # Assim, baixa densidade visual pode bloquear auto-upload sem autorizar\n        # qualquer nova geração paga durante recuperação.\n        if isinstance(script, dict):\n            script["_codexia_auto_upload_requested"] = bool(request.auto_upload)\n\n        video_result = video_service.create_video_from_plan(\n            script,'''


def apply() -> None:
    text = TARGET.read_text(encoding="utf-8")
    text = _insert_before_once(text, "def _quality_gate(result: Any, plan: Any = None) -> Dict[str, Any]:\n", HELPERS, "quality helpers")
    text = _insert_before_once(text, '    opening = str(narration.get("opening_text") or "").strip()\n', TTS_QUALITY_BLOCK, "TTS source quality gate")
    text = _replace_once(text, CLOSING_OLD, CLOSING_NEW, "real hidden spoken closing check")
    text = _insert_before_once(text, "    # Esses dois sinais são importantes para a REVISÃO HUMANA", DENSITY_BLOCK, "visual density metrics")
    text = _replace_once(text, VISUAL_WARNINGS_OLD, VISUAL_WARNINGS_NEW, "visual density review warning")
    text = _replace_once(text, CLASSIFY_OLD, CLASSIFY_NEW, "postrender review-first classification")
    TARGET.write_text(text, encoding="utf-8")

    youtube = YOUTUBE.read_text(encoding="utf-8")
    youtube = _replace_once(youtube, AUTO_UPLOAD_OLD, AUTO_UPLOAD_NEW, "auto upload quality context")
    YOUTUBE.write_text(youtube, encoding="utf-8")


def check() -> None:
    text = TARGET.read_text(encoding="utf-8")
    required = (
        MARKER,
        MARKER_EDITORIAL,
        "_quality_text_contains",
        "_quality_spoken_text_contract",
        "_quality_visual_density",
        '"tts_plain_text_only"',
        '"visual_density_ok"',
        '"visual_density_below_quality_target"',
        '"visual_density_below_publication_target"',
        '"closing_present_in_canonical_audio"',
        '"closing_present_in_captions"',
        'code == "hidden_spoken_closing" and postrender_candidate',
        '"late_quality_policy"] = "preserve_valid_render_review_first_v1"',
        '"publication_ready"] = bool(report["passed"] and not combined_warnings)',
        'visual_warnings or (postrender_candidate and editorial_warnings)',
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError("hardening final de qualidade incompleto: " + ", ".join(missing))
    if 'no_hidden_spoken_cta = not bool(closing)' in text:
        raise PatchError("regra antiga que reprova qualquer closing_text ainda está ativa")

    youtube = YOUTUBE.read_text(encoding="utf-8")
    if MARKER_AUTO_UPLOAD not in youtube or '_codexia_auto_upload_requested' not in youtube:
        raise PatchError("contexto de auto-upload não chega ao quality gate")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.apply:
        apply()
    if args.check:
        check()
    if not args.apply and not args.check:
        parser.error("use --apply e/ou --check")


if __name__ == "__main__":
    main()
