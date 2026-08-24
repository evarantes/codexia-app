from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app/routers/youtube.py"
MARKER = "CODEXIA_RETRY_IMAGE_PATH_COMPAT_V1"


class PatchError(RuntimeError):
    pass


FUNCTION_RE = re.compile(
    r"def _selected_images_ok\(urls: List\[str\], \*, min_bytes: int = 1000\) -> bool:\n"
    r".*?(?=\ndef [A-Za-z_][A-Za-z0-9_]*\()",
    re.DOTALL,
)

REPLACEMENT = '''def _selected_images_ok(urls: List[str], *, min_bytes: int = 1000) -> bool:
    # CODEXIA_RETRY_IMAGE_PATH_COMPAT_V1
    # Reparos confirmados persistem caminhos absolutos duráveis (/data/...).
    # O validador legado tratava todo item como URL /static e remontava esses
    # caminhos em app/static/data/..., acusando falso "faltam imagens" no retry.
    if not urls:
        return False
    try:
        from app.config import absolute_path_for_image, absolute_path_for_static
    except Exception:
        absolute_path_for_image = None
        absolute_path_for_static = None

    checked = 0
    for value in urls:
        raw = str(value or "").strip()
        if not raw:
            continue
        checked += 1
        if checked > 6:
            break

        candidates: List[str] = []
        # 1) Caminho físico durável já resolvido pelo manifesto/preview.
        if not raw.lower().startswith(("http://", "https://")):
            try:
                if os.path.isfile(raw):
                    candidates.append(raw)
            except Exception:
                pass

        # 2) URLs/caminhos do armazenamento unificado (/media/images/...)
        # e 3) compatibilidade com URLs estáticas legadas.
        for resolver in (absolute_path_for_image, absolute_path_for_static):
            if resolver is None:
                continue
            try:
                resolved = str(resolver(raw) or "").strip()
            except Exception:
                resolved = ""
            if resolved and resolved not in candidates:
                candidates.append(resolved)

        valid = False
        for path in candidates:
            try:
                if os.path.isfile(path) and os.path.getsize(path) >= int(min_bytes or 1):
                    valid = True
                    break
            except Exception:
                continue
        if not valid:
            return False
    return checked > 0
'''


def patch_youtube(text: str) -> str:
    if MARKER in text:
        return text
    match = FUNCTION_RE.search(text)
    if not match:
        raise PatchError("função _selected_images_ok não encontrada")
    transformed = text[: match.start()] + REPLACEMENT + text[match.end() :]
    if MARKER not in transformed:
        raise PatchError("marcador da compatibilidade não aplicado")
    return transformed


def apply() -> None:
    original = YOUTUBE.read_text(encoding="utf-8")
    transformed = patch_youtube(original)
    if patch_youtube(transformed) != transformed:
        raise PatchError("patch de caminho de imagem não é idempotente")
    if transformed != original:
        YOUTUBE.write_text(transformed, encoding="utf-8")


def check() -> None:
    text = YOUTUBE.read_text(encoding="utf-8")
    required = (
        MARKER,
        "absolute_path_for_image, absolute_path_for_static",
        "if os.path.isfile(raw):",
        "for resolver in (absolute_path_for_image, absolute_path_for_static):",
        "return checked > 0",
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError("compatibilidade de caminho de imagem incompleta: " + ", ".join(missing))
    compile(text, str(YOUTUBE), "exec")


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
        print(f"ERRO RETRY IMAGE PATH COMPAT: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
