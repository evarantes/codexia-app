from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VOICE_TARGET = ROOT / "app/services/channel_excellence_guard.py"
EDITOR_TARGET = ROOT / "app/services/story_review_editor.py"


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
    return _replace_once(text, old, new, label="voice/preserve-jesus-spelling")


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


def check() -> None:
    voice = VOICE_TARGET.read_text(encoding="utf-8")
    editor = EDITOR_TARGET.read_text(encoding="utf-8")
    required_voice = (
        'value = re.sub(r"(?i)\\bjesus\\b", "Jesus", value)',
        'antiga grafia fonética "Jêzus"',
    )
    for token in required_voice:
        if token not in voice:
            raise PatchError(f"voice hardening incompleto: ausente {token}")
    if 'value = re.sub(r"(?i)\\bjesus\\b", "Jêzus", value)' in voice:
        raise PatchError("substituição fonética antiga de Jesus ainda ativa")

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


def apply(*, write: bool) -> int:
    changed = 0
    targets = ((VOICE_TARGET, patch_voice), (EDITOR_TARGET, patch_editor))
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
    print(f"Voice/closure hardening: {changed} arquivo(s) alterado(s).")
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
        print(f"ERRO VOICE/CLOSURE HARDENING: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
