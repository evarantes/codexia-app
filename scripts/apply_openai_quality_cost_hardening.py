from __future__ import annotations

import argparse
from pathlib import Path


TARGET = Path("app/services/ai_router.py")

REPLACEMENTS = (
    (
        'openai_image_model = _normalize_model_id(getattr(settings, "openai_image_model", None)) or "gpt-image-1-mini"',
        'openai_image_model = _normalize_model_id(getattr(settings, "openai_image_model", None)) or str(os.getenv("CODEXIA_OPENAI_IMAGE_MODEL") or "gpt-image-2").strip()',
    ),
    (
        'estimated_cost=0.005,',
        'estimated_cost=_safe_float(os.getenv("OPENAI_IMAGE_ESTIMATED_COST_USD") or 0.05, 0.05),',
    ),
    (
        'kwargs: Dict[str, Any] = {"model": model, "prompt": prompt, "size": "1024x1024"}',
        'image_size = str(os.getenv("OPENAI_IMAGE_SIZE") or "1536x1024").strip()\n        if image_size not in {"1024x1024", "1024x1536", "1536x1024", "auto"}:\n            image_size = "1536x1024"\n        kwargs: Dict[str, Any] = {"model": model, "prompt": prompt, "size": image_size}',
    ),
    (
        'quality = str(os.getenv("OPENAI_IMAGE_QUALITY") or "low").strip().lower()',
        'quality = str(os.getenv("OPENAI_IMAGE_QUALITY") or "medium").strip().lower()',
    ),
    (
        'kwargs["quality"] = quality if quality in {"low", "medium", "high", "auto"} else "low"',
        'kwargs["quality"] = quality if quality in {"low", "medium", "high", "auto"} else "medium"',
    ),
)


def patched_text(original: str) -> str:
    text = original
    for old, new in REPLACEMENTS:
        if new in text:
            continue
        if old not in text:
            raise RuntimeError(f"Anchor not found in {TARGET}: {old[:120]}")
        # estimated_cost=0.005 appears twice (image + thumbnail); intentionally update both.
        if old == 'estimated_cost=0.005,':
            text = text.replace(old, new)
        else:
            text = text.replace(old, new, 1)
    return text


def check(text: str) -> None:
    required = (
        '"gpt-image-2"',
        'OPENAI_IMAGE_ESTIMATED_COST_USD',
        'OPENAI_IMAGE_SIZE',
        '"1536x1024"',
        'OPENAI_IMAGE_QUALITY',
        'or "medium"',
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise RuntimeError(f"OpenAI quality hardening incomplete: missing {missing}")
    if 'or "gpt-image-1-mini"' in text:
        raise RuntimeError("Legacy gpt-image-1-mini default still active")
    if 'OPENAI_IMAGE_QUALITY") or "low"' in text:
        raise RuntimeError("Legacy low-quality OpenAI default still active")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not TARGET.exists():
        raise RuntimeError(f"Missing target: {TARGET}")
    text = TARGET.read_text(encoding="utf-8")
    if args.apply:
        text = patched_text(text)
        TARGET.write_text(text, encoding="utf-8")
    if args.check or args.apply:
        check(TARGET.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
