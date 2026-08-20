from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VOICE_TARGET = ROOT / "app/services/channel_excellence_guard.py"
SCENE_VOICE_TARGET = ROOT / "app/services/scene_director_active.py"
EDITOR_TARGET = ROOT / "app/services/story_review_editor.py"
RENDER_TARGET = ROOT / "app/services/video_generator.py"


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho original, encontrado {count}")
    return text.replace(old, new, 1)


def patch_voice(text: str) -> str:
    old = '''    # Forma fonética somente para a voz. Texto/legenda permanecem com a grafia oficial.\n    value = re.sub(r"(?i)\\bjesus\\b", "Jêzus", value)'''
    new = '''    # Preserve a grafia oficial no texto enviado ao TTS. As vozes pt-BR atuais\n    # pronunciam "Jesus" corretamente; a antiga grafia fonética "Jêzus" podia\n    # distorcer a sílaba final em alguns providers/vozes.\n    value = re.sub(r"(?i)\\bjesus\\b", "Jesus", value)'''
    text = _replace_once(text, old, new, label="voice/preserve-jesus-spelling")

    old_cta = '''            guarded["endcard_cta_text"] = "Inscreva-se e acompanhe novas mensagens."\n            branding = guarded.get("branding") if isinstance(guarded.get("branding"), dict) else {}\n            branding = deepcopy(branding)\n            branding["final_message"] = lines\n            branding.setdefault("endcard_cta_text", guarded["endcard_cta_text"])'''
    new_cta = '''            # Respeita CTA explícito vindo da preparação final do canal. Assim\n            # inscrição + sininho + compartilhamento não são reduzidos pelo guard.\n            guarded["endcard_cta_text"] = (\n                _clean_line(guarded.get("endcard_cta_text"))\n                or "Inscreva-se e acompanhe novas mensagens."\n            )\n            branding = guarded.get("branding") if isinstance(guarded.get("branding"), dict) else {}\n            branding = deepcopy(branding)\n            branding["final_message"] = lines\n            branding["endcard_cta_text"] = guarded["endcard_cta_text"]'''
    return _replace_once(text, old_cta, new_cta, label="voice/preserve-explicit-endcard-cta")


def patch_scene_voice(text: str) -> str:
    old = '''def _spoken_ptbr(text: Any) -> str:\n    value = str(text or "")\n    # A forma fonética só vai para o TTS; legenda/texto aprovado continuam "Jesus".\n    value = re.sub(r"(?i)\\bjesus\\b", "Jêzus", value)\n    return value'''
    new = '''def _spoken_ptbr(text: Any) -> str:\n    value = str(text or "")\n    # Esta é a camada mais interna antes do provider. Ela também precisa preservar\n    # "Jesus"; caso contrário desfaz o guard externo e acelera/distorce a palavra.\n    value = re.sub(r"(?i)\\bjesus\\b", "Jesus", value)\n    return value'''
    return _replace_once(text, old, new, label="scene-voice/preserve-jesus-at-inner-boundary")


def patch_editor(text: str) -> str:
    helper = '''\n\ndef _closing_structure_issues(text: str) -> List[str]:\n    """Detecta finais que parecem corte de pipeline, não conclusão editorial."""\n    value = _clean(text)\n    if not value:\n        return ["missing_closing"]\n\n    issues: List[str] = []\n    final_slice = value[-420:]\n    final_words = re.findall(r"[A-Za-zÀ-ÿ0-9]+", final_slice, flags=re.UNICODE)\n    if len(final_words) < 20:\n        issues.append("closing_too_short")\n\n    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\\s+", value) if part.strip()]\n    final_sentence = sentences[-1] if sentences else value\n    sentence_words = re.findall(r"[A-Za-zÀ-ÿ0-9]+", final_sentence, flags=re.UNICODE)\n    if len(sentence_words) < 7:\n        issues.append("weak_final_sentence")\n    if not re.search(r"[.!?][\\\"'’”)]?$", final_sentence):\n        issues.append("unfinished_final_sentence")\n    if re.search(\n        r"(?i)\\b(?:e|mas|porque|quando|se|para|que|pois|então|portanto|por isso)\\s*[.!?]?[\\\"'’”)]?$",\n        final_sentence,\n    ):\n        issues.append("dangling_final_connector")\n\n    return issues\n'''
    if "def _closing_structure_issues" not in text:
        anchor = "\n\ndef _fallback_title(instruction: str, kind: str) -> str:"
        if anchor not in text:
            raise PatchError("editor/closing-helper-anchor não encontrado")
        text = text.replace(anchor, helper + anchor, 1)

    old_return = '''    return issues\n\n\ndef _build_editorial_prompt('''
    new_return = '''    for closing_issue in _closing_structure_issues(clean_text):\n        if closing_issue not in issues:\n            issues.append(closing_issue)\n\n    return issues\n\n\ndef _build_editorial_prompt('''
    text = _replace_once(text, old_return, new_return, label="editor/closing-quality-check")

    old_rule = '10. FIM: a conclusão precisa RETOMAR explicitamente a pergunta, tensão ou tese central do tema e respondê-la de forma pessoal e memorável. O ouvinte deve sentir que a mensagem realmente terminou, e não apenas parou.'
    new_rule = '10. FIM: construa obrigatoriamente a conclusão DENTRO do campo text em três movimentos: (a) retome explicitamente a pergunta, tensão ou tese central; (b) faça uma aplicação espiritual/pessoal específica ao tema; (c) encerre com uma última frase curta, afirmativa, memorável e claramente conclusiva. A última frase não pode abrir assunto novo, terminar em conectivo nem soar como continuação cortada.'
    text = _replace_once(text, old_rule, new_rule, label="editor/three-stage-closing-prompt")

    old_tail = '''13. Nunca deixe frases incompletas como "uma mensagem de..." ou "uma palavra de...".\n14. Retorne apenas JSON válido, sem markdown.'''
    new_tail = '''13. Nunca deixe frases incompletas como "uma mensagem de..." ou "uma palavra de...".\n14. closing_message é apenas o RESUMO VISUAL da conclusão que já foi narrada no campo text; não coloque nele uma ideia nova e nunca o use como substituto da conclusão falada.\n15. Retorne apenas JSON válido, sem markdown.'''
    text = _replace_once(text, old_tail, new_tail, label="editor/endcard-not-hidden-conclusion")
    return text


def patch_renderer(text: str) -> str:
    text = _replace_once(
        text,
        "DEFAULT_SCENE_AUDIO_MARGIN_SEC = 0.40\nDEFAULT_OPENING_SILENCE_SEC = 0.45\nDEFAULT_SCENE_IMAGE_LEAD_SEC = 0.30\nDEFAULT_SCENE_CAPTION_LEAD_SEC = 0.20",
        "DEFAULT_SCENE_AUDIO_MARGIN_SEC = 0.10\nDEFAULT_OPENING_SILENCE_SEC = 0.45\nDEFAULT_SCENE_IMAGE_LEAD_SEC = 0.12\nDEFAULT_SCENE_CAPTION_LEAD_SEC = 0.0",
        label="renderer/tighter-caption-audio-alignment",
    )
    text = _replace_once(
        text,
        '            pause_before_cta_sec = float(planning_meta.get("pause_duration_sec") or 1.25)',
        '            _pause_configured = planning_meta.get("pause_duration_sec")\n            pause_before_cta_sec = max(0.0, float(_pause_configured if _pause_configured is not None else 0.45))',
        label="renderer/honor-zero-pause-before-cta",
    )
    text = _replace_once(
        text,
        '            pause_before_cta_sec = float(planning_meta.get("pause_duration_sec") or pause_before_cta_sec or 1.25)\n            initial_opening_silence_sec = float(planning_meta.get("intro_opening_hold_sec") or initial_opening_silence_sec or DEFAULT_OPENING_SILENCE_SEC)\n            end_screen_target_duration_sec = float(planning_meta.get("end_screen_target_duration_sec") or 5.0)',
        '            _pause_configured = planning_meta.get("pause_duration_sec")\n            pause_before_cta_sec = max(0.0, float(_pause_configured if _pause_configured is not None else pause_before_cta_sec))\n            if not closing_has_narration:\n                pause_before_cta_sec = 0.0\n            initial_opening_silence_sec = float(planning_meta.get("intro_opening_hold_sec") or initial_opening_silence_sec or DEFAULT_OPENING_SILENCE_SEC)\n            _end_screen_configured = planning_meta.get("end_screen_target_duration_sec")\n            end_screen_target_duration_sec = float(_end_screen_configured if _end_screen_configured is not None else 1.2)',
        label="renderer/remove-orphan-closing-pause",
    )
    text = _replace_once(
        text,
        '            end_clip_duration = min(6.0, max(3.0, round(end_screen_target_duration_sec, 2)))',
        '            end_clip_duration = min(1.6, max(0.8, round(end_screen_target_duration_sec, 2)))',
        label="renderer/short-purposeful-endcard",
    )
    text = _replace_once(
        text,
        '            cta_ok = (3.0 <= float(end_clip_duration or 0.0) <= 6.0) if closing_has_narration else True',
        '            cta_ok = (0.8 <= float(end_clip_duration or 0.0) <= 1.6) if closing_has_narration else True',
        label="renderer/validate-short-endcard",
    )
    return text


def check() -> None:
    voice = VOICE_TARGET.read_text(encoding="utf-8")
    scene_voice = SCENE_VOICE_TARGET.read_text(encoding="utf-8")
    editor = EDITOR_TARGET.read_text(encoding="utf-8")
    renderer = RENDER_TARGET.read_text(encoding="utf-8")

    required_voice = (
        'value = re.sub(r"(?i)\\bjesus\\b", "Jesus", value)',
        'antiga grafia fonética "Jêzus"',
        '_clean_line(guarded.get("endcard_cta_text"))',
        'branding["endcard_cta_text"] = guarded["endcard_cta_text"]',
    )
    for token in required_voice:
        if token not in voice:
            raise PatchError(f"voice hardening incompleto: ausente {token}")
    if 'value = re.sub(r"(?i)\\bjesus\\b", "Jêzus", value)' in voice:
        raise PatchError("substituição fonética antiga de Jesus ainda ativa no channel guard")

    if 'value = re.sub(r"(?i)\\bjesus\\b", "Jêzus", value)' in scene_voice:
        raise PatchError("substituição fonética antiga de Jesus ainda ativa no scene director")
    if 'value = re.sub(r"(?i)\\bjesus\\b", "Jesus", value)' not in scene_voice:
        raise PatchError("scene director não preserva Jesus na fronteira interna do TTS")

    required_editor = (
        "def _closing_structure_issues",
        'issues.append("unfinished_final_sentence")',
        "conclusão DENTRO do campo text em três movimentos",
        "closing_message é apenas o RESUMO VISUAL",
        "for closing_issue in _closing_structure_issues(clean_text):",
    )
    for token in required_editor:
        if token not in editor:
            raise PatchError(f"closure hardening incompleto: ausente {token}")

    required_renderer = (
        "DEFAULT_SCENE_AUDIO_MARGIN_SEC = 0.10",
        "DEFAULT_SCENE_IMAGE_LEAD_SEC = 0.12",
        "DEFAULT_SCENE_CAPTION_LEAD_SEC = 0.0",
        "if not closing_has_narration:\n                pause_before_cta_sec = 0.0",
        "else 1.2)",
        "end_clip_duration = min(1.6, max(0.8",
        "cta_ok = (0.8 <= float(end_clip_duration or 0.0) <= 1.6)",
    )
    for token in required_renderer:
        if token not in renderer:
            raise PatchError(f"renderer hardening incompleto: ausente {token}")


def apply(*, write: bool) -> int:
    changed = 0
    targets = (
        (VOICE_TARGET, patch_voice),
        (SCENE_VOICE_TARGET, patch_scene_voice),
        (EDITOR_TARGET, patch_editor),
        (RENDER_TARGET, patch_renderer),
    )
    for path, patcher in targets:
        original = path.read_text(encoding="utf-8")
        transformed = patcher(original)
        if transformed != original:
            changed += 1
            if write:
                path.write_text(transformed, encoding="utf-8")
        second = patcher(transformed)
        if second != transformed:
            raise PatchError(f"{path}: transformação não idempotente")
    if write:
        check()
    print(f"Voice/closure/sync hardening: {changed} arquivo(s) alterado(s).")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply ou --check")
    try:
        if args.apply:
            apply(write=True)
        if args.check:
            check()
    except PatchError as exc:
        print(f"ERRO VOICE/CLOSURE/SYNC HARDENING: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
