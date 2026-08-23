from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "app/services/channel_excellence_guard.py"
MARKER = "CODEXIA_FINAL_QUALITY_POSTRENDER_V1"


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


HELPERS = '''\n\n# CODEXIA_FINAL_QUALITY_POSTRENDER_V1\ndef _quality_normalize_text(value: Any) -> str:\n    folded = _fold(value)\n    return re.sub(r"[^a-z0-9]+", " ", folded).strip()\n\n\ndef _quality_text_contains(container: Any, fragment: Any) -> bool:\n    \"\"\"Compara conteúdo sem depender de pontuação/acento do TTS/legenda.\"\"\"\n    needle = _quality_normalize_text(fragment)\n    if not needle:\n        return True\n    haystack = _quality_normalize_text(container)\n    if not haystack:\n        return False\n    if needle in haystack:\n        return True\n    words = needle.split()\n    if len(words) < 6:\n        return False\n    width = min(10, len(words))\n    lead = " ".join(words[:width])\n    tail = " ".join(words[-width:])\n    return lead in haystack and tail in haystack\n\n\ndef _postrender_candidate_exists(result: Any) -> bool:\n    if not isinstance(result, dict):\n        return False\n    return bool(str(result.get("file_path") or result.get("video_url") or "").strip())\n\n'''


CLOSING_OLD = '''    closing = str(narration.get("closing_text") or "").strip()\n    no_hidden_spoken_cta = not bool(closing)\n    report["checks"]["no_hidden_spoken_cta"] = no_hidden_spoken_cta\n    if not no_hidden_spoken_cta:\n        report["violations"].append("hidden_spoken_closing")'''

CLOSING_NEW = '''    closing = str(narration.get("closing_text") or "").strip()\n    text_integrity = rr.get("text_integrity") if isinstance(rr.get("text_integrity"), dict) else {}\n    audio_generation = rr.get("audio_generation") if isinstance(rr.get("audio_generation"), dict) else {}\n    sync_validation = rr.get("sync_validation") if isinstance(rr.get("sync_validation"), dict) else {}\n    canonical_spoken = str(\n        text_integrity.get("final_text_sent_to_tts")\n        or audio_generation.get("final_text_sent_to_tts")\n        or ""\n    ).strip()\n    caption_source = str(text_integrity.get("captions_source_text") or "").strip()\n    closing_in_spoken = _quality_text_contains(canonical_spoken, closing) if closing else True\n    closing_in_captions = _quality_text_contains(caption_source, closing) if closing else True\n    captions_verified = bool(\n        text_integrity.get("captions_match_narration_source")\n        or sync_validation.get("captions_synced_with_audio")\n    )\n    # "hidden_spoken_closing" só é verdadeiro quando o fechamento não pode ser\n    # comprovado na voz/timeline. A simples existência de closing_text nunca\n    # deve reprovar um encerramento realmente narrado e legendado.\n    no_hidden_spoken_cta = bool(\n        not closing\n        or (closing_in_spoken and (closing_in_captions or captions_verified))\n    )\n    report["checks"]["no_hidden_spoken_cta"] = no_hidden_spoken_cta\n    report["checks"]["closing_present_in_canonical_audio"] = bool(closing_in_spoken)\n    report["checks"]["closing_present_in_captions"] = bool(closing_in_captions or captions_verified)\n    if closing and not no_hidden_spoken_cta:\n        report["violations"].append("hidden_spoken_closing")'''


CLASSIFY_OLD = '''    # Violações anteriores continuam bloqueantes. Os alertas visuais são\n    # mantidos para revisão humana sem invalidar um render já concluído.\n    blocking_violations = list(report["violations"])\n    report["violations"].extend(visual_warnings)\n    report["warnings"] = visual_warnings\n    report["blocking_violations"] = blocking_violations\n    report["review_recommended"] = bool(visual_warnings)\n    report["auto_render_preserved"] = bool(visual_warnings)\n    report["passed"] = not blocking_violations\n    return report'''

CLASSIFY_NEW = '''    # CODEXIA_FINAL_QUALITY_POSTRENDER_V1\n    # Se um MP4 candidato já foi produzido, um hidden_spoken_closing residual\n    # é uma divergência editorial/timeline para REVISÃO, não motivo para destruir\n    # o render ou induzir nova mídia paga. Outros blockers (inclusive orçamento)\n    # continuam fail-closed. Alertas visuais mantêm a semântica histórica de\n    # preservar o render para revisão mesmo em fixtures legadas sem file_path.\n    postrender_candidate = _postrender_candidate_exists(result)\n    existing_violations = list(report["violations"])\n    editorial_warnings = [\n        code for code in existing_violations\n        if code == "hidden_spoken_closing" and postrender_candidate\n    ]\n    blocking_violations = [\n        code for code in existing_violations\n        if code not in editorial_warnings\n    ]\n    combined_warnings = list(dict.fromkeys(editorial_warnings + visual_warnings))\n    report["violations"].extend(visual_warnings)\n    report["warnings"] = combined_warnings\n    report["editorial_warnings"] = editorial_warnings\n    report["blocking_violations"] = blocking_violations\n    report["review_recommended"] = bool(combined_warnings)\n    report["auto_render_preserved"] = bool(\n        visual_warnings or (postrender_candidate and editorial_warnings)\n    )\n    report["late_quality_policy"] = "preserve_valid_render_review_first_v1"\n    report["passed"] = not blocking_violations\n    return report'''


def apply() -> None:
    text = TARGET.read_text(encoding="utf-8")
    text = _insert_before_once(text, "def _quality_gate(result: Any, plan: Any = None) -> Dict[str, Any]:\n", HELPERS, "quality helpers")
    text = _replace_once(text, CLOSING_OLD, CLOSING_NEW, "real hidden spoken closing check")
    text = _replace_once(text, CLASSIFY_OLD, CLASSIFY_NEW, "postrender review-first classification")
    TARGET.write_text(text, encoding="utf-8")


def check() -> None:
    text = TARGET.read_text(encoding="utf-8")
    required = (
        MARKER,
        "_quality_text_contains",
        '"closing_present_in_canonical_audio"',
        '"closing_present_in_captions"',
        'code == "hidden_spoken_closing" and postrender_candidate',
        '"late_quality_policy"] = "preserve_valid_render_review_first_v1"',
        'visual_warnings or (postrender_candidate and editorial_warnings)',
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError("hardening final de qualidade incompleto: " + ", ".join(missing))
    if 'no_hidden_spoken_cta = not bool(closing)' in text:
        raise PatchError("regra antiga que reprova qualquer closing_text ainda está ativa")


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
