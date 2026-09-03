from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NARRATION = ROOT / "app/services/narration_contract_guard.py"
VIDEO = ROOT / "app/services/video_generator.py"
INDEX = ROOT / "app/static/index.html"
MARKER = "CODEXIA_NARRATION_CTA_FINISH_HARDENING_V1"


class PatchError(RuntimeError):
    pass


def _replace_once_or_already(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1)


def _validate_source_owned_narration_guard(text: str) -> None:
    """Valida as proteções sem reescrever o guard canônico.

    Desde o Narration Core v1, ``narration_contract_guard.py`` pertence ao
    código-fonte e não pode mais ser modificado por hardenings de build. Este
    script continua responsável pelo acabamento visual do CTA e pela correção
    de compatibilidade do frontend, mas apenas CONFERE as barreiras de fala.
    """
    required = (
        '"inline_code"',
        '"source_code_assignment"',
        '"source_code_declaration"',
        '"source_code_arrow"',
        '"sql_statement"',
        'raw = re.sub(r"(?m)^\\s{0,3}#{1,6}\\s+", "", raw)',
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError(
            "narration guard source-owned sem proteções obrigatórias: "
            + ", ".join(missing)
        )


def patch_narration_guard(text: str) -> str:
    _validate_source_owned_narration_guard(text)
    return text


def patch_video(text: str) -> str:
    text = _replace_once_or_already(
        text,
        '            end_screen_target_duration_sec = float(_end_screen_configured if _end_screen_configured is not None else 1.2)',
        '            # CODEXIA_NARRATION_CTA_FINISH_HARDENING_V1\n            # O CTA precisa ficar visível tempo suficiente para leitura e compreensão.\n            end_screen_target_duration_sec = float(_end_screen_configured if _end_screen_configured is not None else 5.0)',
        "video/default-visible-endcard-duration",
    )
    text = _replace_once_or_already(
        text,
        '            end_clip_duration = min(1.6, max(0.8, round(end_screen_target_duration_sec, 2)))',
        '            end_clip_duration = min(6.0, max(4.0, round(end_screen_target_duration_sec, 2)))',
        "video/visible-endcard-duration-range",
    )
    text = _replace_once_or_already(
        text,
        '            cta_ok = (0.8 <= float(end_clip_duration or 0.0) <= 1.6) if closing_has_narration else True',
        '            cta_ok = (4.0 <= float(end_clip_duration or 0.0) <= 6.0) if closing_has_narration else True',
        "video/validate-visible-endcard-range",
    )
    return text


def _brace_detail_handler(text: str, variable: str, fallback: str) -> str:
    # PR #110 expandiu um throw de uma linha para três declarações. Quando o
    # throw original era o corpo direto de um if sem chaves, isso produziu
    # `if (...) const ...`, JavaScript inválido. Corrija apenas esse formato.
    detail = "retryDetail" if variable == "data" else "planDetail"
    start_re = re.compile(
        rf"(?P<indent>^[ \t]*)if \((?P<cond>![A-Za-z_$][A-Za-z0-9_$.]*\.ok)\) "
        rf"const {detail} = {variable} && {variable}\.detail;",
        re.MULTILINE,
    )
    match = start_re.search(text)
    if match:
        replacement = (
            f"{match.group('indent')}if ({match.group('cond')}) {{ "
            f"const {detail} = {variable} && {variable}.detail;"
        )
        text = text[:match.start()] + replacement + text[match.end():]

        throw_line = (
            f"throw new Error({detail}Message || {variable}.message || '{fallback}');"
        )
        if throw_line not in text:
            raise PatchError(f"frontend/{detail}: throw final não encontrado")
        text = text.replace(throw_line, throw_line + " }", 1)
    return text


def patch_index(text: str) -> str:
    text = _brace_detail_handler(text, "data", "Falha ao recolocar a tarefa na fila.")
    text = _brace_detail_handler(text, "planData", "Falha ao analisar alternativas de recuperação.")
    if MARKER not in text:
        text = text.rstrip() + f"\n<!-- {MARKER} -->\n"
    return text


def check() -> None:
    narration = NARRATION.read_text(encoding="utf-8")
    video = VIDEO.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")

    # O guard de narração não recebe mais marker/patch deste script. Apenas
    # verificamos que o Narration Core v1 já contém as proteções necessárias.
    _validate_source_owned_narration_guard(narration)

    required_video = (
        MARKER,
        "else 5.0)",
        "min(6.0, max(4.0",
        "4.0 <= float(end_clip_duration or 0.0) <= 6.0",
    )
    missing = [token for token in required_video if token not in video]
    if missing:
        raise PatchError("CTA visual incompleto: " + ", ".join(missing))

    invalid_js = re.search(
        r"if\s*\([^)]*\)\s+const\s+(?:retryDetail|planDetail)\b",
        index,
    )
    if invalid_js:
        raise PatchError("frontend ainda contém `if (...) const ...` inválido")
    if MARKER not in index:
        raise PatchError("marker do frontend ausente")

    compile(narration, str(NARRATION), "exec")
    compile(video, str(VIDEO), "exec")


def apply() -> None:
    for path, patcher in (
        (NARRATION, patch_narration_guard),
        (VIDEO, patch_video),
        (INDEX, patch_index),
    ):
        original = path.read_text(encoding="utf-8")
        transformed = patcher(original)
        second = patcher(transformed)
        if second != transformed:
            raise PatchError(f"{path.name}: patch não idempotente")
        if transformed != original:
            path.write_text(transformed, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply ou --check")
    try:
        if args.apply:
            apply()
        if args.check:
            check()
    except PatchError as exc:
        print(f"ERRO NARRATION/CTA FINISH HARDENING: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
