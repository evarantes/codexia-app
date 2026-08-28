from __future__ import annotations

import argparse
from pathlib import Path

INDEX = Path("app/static/index.html")
JS = Path("app/static/youtube_narration_gate.js")
YOUTUBE_ROUTER = Path("app/routers/youtube.py")
TAG = '<script src="/static/youtube_narration_gate.js"></script>'
MARKER = "</body>"

OLD_BUTTON_LOOKUP = "return [...card.querySelectorAll('button')].find(btn => normalizeText(btn.textContent).toLowerCase().includes('gerar vídeo narrado')) || null;"
NEW_BUTTON_LOOKUP = "return [...card.querySelectorAll('button')].find(btn => !btn.closest('[data-youtube-narration-gate]') && normalizeText(btn.textContent).toLowerCase().includes('gerar vídeo narrado')) || null;"

RENDER_CALL_MARKER = "        video_result = video_service.create_video_from_plan(\n"
APPROVED_AUDIO_MARKER = "# narration-gate-approved-audio-reuse-v1"
APPROVED_AUDIO_BLOCK = '''        # narration-gate-approved-audio-reuse-v1\n        # Quando o usuario aprovou uma narracao, o render DEVE usar exatamente\n        # aquele MP3. Falhamos fechado em qualquer inconsistencia para impedir\n        # um novo TTS silencioso (e, consequentemente, codigo narrado/custo duplicado).\n        approved_audio = request.reuse_audio_from if isinstance(request.reuse_audio_from, dict) else None\n        if approved_audio:\n            from app.config import AUDIO_OUTPUT_DIR as _APPROVED_AUDIO_ROOT\n\n            approved_source = str(approved_audio.get("source") or "").strip()\n            approved_path_raw = str(approved_audio.get("output_path") or "").strip()\n            approved_text_hash = str(approved_audio.get("text_sha256") or "").strip().lower()\n            current_story_text = str(request.story_content or "").replace("\\r\\n", "\\n").replace("\\r", "\\n").strip()\n            current_story_hash = hashlib.sha256(current_story_text.encode("utf-8")).hexdigest() if current_story_text else ""\n\n            approved_root = Path(_APPROVED_AUDIO_ROOT).resolve() / "youtube_narration_gate"\n            approved_path = Path(approved_path_raw).resolve() if approved_path_raw else None\n            path_inside_gate = False\n            if approved_path is not None:\n                try:\n                    approved_path.relative_to(approved_root)\n                    path_inside_gate = True\n                except Exception:\n                    path_inside_gate = False\n\n            approved_ok = bool(\n                approved_source == "youtube_narration_gate_approved"\n                and approved_path is not None\n                and path_inside_gate\n                and approved_path.is_file()\n                and approved_path.stat().st_size > 512\n                and approved_text_hash\n                and current_story_hash == approved_text_hash\n            )\n            if not approved_ok:\n                raise RuntimeError(\n                    "Narração aprovada inválida ou incompatível com o texto atual. "\n                    "O vídeo foi bloqueado para impedir nova geração de TTS; aprove novamente o áudio."\n                )\n\n            if not isinstance(script, dict):\n                raise RuntimeError("Roteiro inválido ao aplicar a narração aprovada.")\n            script["seed_audio_path"] = str(approved_path)\n            script["seed_narration_text"] = current_story_text\n            script["approved_narration_preview_id"] = str(approved_audio.get("preview_id") or "").strip()\n            script["approved_narration_text_sha256"] = approved_text_hash\n            script["narration_source"] = "approved_preview_reuse"\n            update_task(task_id, result=_merged_task_result({\n                "approved_narration_reuse": {\n                    "used": True,\n                    "preview_id": script.get("approved_narration_preview_id"),\n                    "text_sha256": approved_text_hash,\n                    "audio_path": str(approved_path),\n                    "tts_regeneration_allowed": False,\n                }\n            }))\n\n'''


def _apply_router_patch() -> bool:
    if not YOUTUBE_ROUTER.is_file():
        raise SystemExit("youtube narration gate: youtube router missing")
    source = YOUTUBE_ROUTER.read_text(encoding="utf-8")
    if APPROVED_AUDIO_MARKER in source:
        return False
    if source.count(RENDER_CALL_MARKER) != 1:
        raise SystemExit(
            f"youtube narration gate: expected one canonical render call marker, found {source.count(RENDER_CALL_MARKER)}"
        )
    source = source.replace(RENDER_CALL_MARKER, APPROVED_AUDIO_BLOCK + RENDER_CALL_MARKER, 1)
    YOUTUBE_ROUTER.write_text(source, encoding="utf-8")
    return True


def apply() -> bool:
    changed = False
    text = INDEX.read_text(encoding="utf-8")
    if TAG not in text:
        if MARKER not in text:
            raise SystemExit("youtube narration gate: </body> marker not found")
        text = text.replace(MARKER, f"    {TAG}\n{MARKER}", 1)
        INDEX.write_text(text, encoding="utf-8")
        changed = True

    if not JS.is_file():
        raise SystemExit("youtube narration gate: JS asset missing")
    source = JS.read_text(encoding="utf-8")
    if OLD_BUTTON_LOOKUP in source:
        source = source.replace(OLD_BUTTON_LOOKUP, NEW_BUTTON_LOOKUP, 1)
        JS.write_text(source, encoding="utf-8")
        changed = True
    elif NEW_BUTTON_LOOKUP not in source:
        raise SystemExit("youtube narration gate: canonical video button lookup not found")

    if _apply_router_patch():
        changed = True

    return changed


def check() -> None:
    text = INDEX.read_text(encoding="utf-8")
    if text.count(TAG) != 1:
        raise SystemExit(f"youtube narration gate: expected exactly one script tag, found {text.count(TAG)}")
    if not JS.is_file():
        raise SystemExit("youtube narration gate: JS asset missing")
    source = JS.read_text(encoding="utf-8")
    required = [
        "Gerar primeiro o áudio da narração",
        "Avançar para geração do vídeo com este áudio",
        "/youtube/narration-lab/production-preview",
        "reuse_audio_from",
        "approved_narration_text_sha256",
        NEW_BUTTON_LOOKUP,
    ]
    missing = [item for item in required if item not in source]
    if missing:
        raise SystemExit(f"youtube narration gate: JS contract missing {missing}")
    if OLD_BUTTON_LOOKUP in source:
        raise SystemExit("youtube narration gate: supervised button can still shadow canonical video button")

    router_source = YOUTUBE_ROUTER.read_text(encoding="utf-8")
    router_required = [
        APPROVED_AUDIO_MARKER,
        'script["seed_audio_path"] = str(approved_path)',
        'script["seed_narration_text"] = current_story_text',
        'approved_source == "youtube_narration_gate_approved"',
        'current_story_hash == approved_text_hash',
        'tts_regeneration_allowed": False',
    ]
    router_missing = [item for item in router_required if item not in router_source]
    if router_missing:
        raise SystemExit(f"youtube narration gate: approved-audio render contract missing {router_missing}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply and/or --check")
    if args.apply:
        changed = apply()
        print("youtube narration gate:", "applied" if changed else "already applied")
    if args.check:
        check()
        print("youtube narration gate: OK")


if __name__ == "__main__":
    main()
