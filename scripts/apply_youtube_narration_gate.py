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
APPROVED_AUDIO_MARKER_V1 = "# narration-gate-approved-audio-reuse-v1"
APPROVED_AUDIO_MARKER_V2 = "# narration-gate-approved-audio-reuse-v2"

APPROVED_AUDIO_BLOCK_V2 = '''        # narration-gate-approved-audio-reuse-v2\n        # Quando o usuario aprovou uma narracao, o render DEVE usar exatamente\n        # aquele MP3 e o texto autenticado pelos metadados do proprio gate.\n        # A revisao editorial posterior pode mudar roteiro/cenas, mas nunca pode\n        # invalidar nem substituir silenciosamente a narracao ja aprovada.\n        approved_audio = request.reuse_audio_from if isinstance(request.reuse_audio_from, dict) else None\n        if approved_audio:\n            from app.config import AUDIO_OUTPUT_DIR as _APPROVED_AUDIO_ROOT\n\n            approved_source = str(approved_audio.get("source") or "").strip()\n            approved_path_raw = str(approved_audio.get("output_path") or "").strip()\n            approved_text_hash = str(approved_audio.get("text_sha256") or "").strip().lower()\n            approved_preview_id = str(approved_audio.get("preview_id") or "").strip().lower()\n\n            approved_root = Path(_APPROVED_AUDIO_ROOT).resolve() / "youtube_narration_gate"\n            approved_path = Path(approved_path_raw).resolve() if approved_path_raw else None\n            path_inside_gate = False\n            if approved_path is not None:\n                try:\n                    approved_path.relative_to(approved_root)\n                    path_inside_gate = True\n                except Exception:\n                    path_inside_gate = False\n\n            approved_story_text = ""\n            approved_story_hash = ""\n            approved_meta_ok = False\n            if approved_path is not None and path_inside_gate:\n                approved_meta_path = approved_path.with_suffix(".json")\n                try:\n                    approved_meta = json.loads(approved_meta_path.read_text(encoding="utf-8"))\n                    approved_story_text = str(approved_meta.get("spoken_text_sent_to_tts") or "").replace("\\r\\n", "\\n").replace("\\r", "\\n").strip()\n                    approved_story_hash = hashlib.sha256(approved_story_text.encode("utf-8")).hexdigest() if approved_story_text else ""\n                    approved_meta_ok = bool(\n                        isinstance(approved_meta, dict)\n                        and approved_meta.get("approved") is True\n                        and str(approved_meta.get("preview_id") or "").strip().lower() == approved_preview_id\n                        and str(approved_meta.get("text_sha256") or "").strip().lower() == approved_text_hash\n                        and approved_story_hash == approved_text_hash\n                    )\n                except Exception:\n                    approved_meta_ok = False\n\n            approved_ok = bool(\n                approved_source == "youtube_narration_gate_approved"\n                and approved_path is not None\n                and path_inside_gate\n                and approved_path.is_file()\n                and approved_path.stat().st_size > 512\n                and approved_text_hash\n                and approved_preview_id\n                and approved_meta_ok\n            )\n            if not approved_ok:\n                raise RuntimeError(\n                    "Narração aprovada inválida ou metadados incompatíveis. "\n                    "O vídeo foi bloqueado para impedir nova geração de TTS; aprove novamente o áudio."\n                )\n\n            if not isinstance(script, dict):\n                raise RuntimeError("Roteiro inválido ao aplicar a narração aprovada.")\n            script["seed_audio_path"] = str(approved_path)\n            script["seed_narration_text"] = approved_story_text\n            script["approved_narration_preview_id"] = approved_preview_id\n            script["approved_narration_text_sha256"] = approved_text_hash\n            script["narration_source"] = "approved_preview_reuse"\n            update_task(task_id, result=_merged_task_result({\n                "approved_narration_reuse": {\n                    "used": True,\n                    "preview_id": script.get("approved_narration_preview_id"),\n                    "text_sha256": approved_text_hash,\n                    "audio_path": str(approved_path),\n                    "text_source": "youtube_narration_gate_metadata",\n                    "tts_regeneration_allowed": False,\n                }\n            }))\n\n'''


def _upgrade_v1_router_block(source: str) -> str:
    if APPROVED_AUDIO_MARKER_V1 not in source:
        return source
    start = source.find(APPROVED_AUDIO_MARKER_V1)
    if start < 0:
        return source
    line_start = source.rfind("\n", 0, start) + 1
    end = source.find(RENDER_CALL_MARKER, start)
    if end < 0:
        raise SystemExit("youtube narration gate: canonical render call missing after v1 block")
    return source[:line_start] + APPROVED_AUDIO_BLOCK_V2 + source[end:]


def _apply_router_patch() -> bool:
    if not YOUTUBE_ROUTER.is_file():
        raise SystemExit("youtube narration gate: youtube router missing")
    source = YOUTUBE_ROUTER.read_text(encoding="utf-8")
    if APPROVED_AUDIO_MARKER_V2 in source:
        return False
    if APPROVED_AUDIO_MARKER_V1 in source:
        upgraded = _upgrade_v1_router_block(source)
        YOUTUBE_ROUTER.write_text(upgraded, encoding="utf-8")
        return True
    if source.count(RENDER_CALL_MARKER) != 1:
        raise SystemExit(
            f"youtube narration gate: expected one canonical render call marker, found {source.count(RENDER_CALL_MARKER)}"
        )
    source = source.replace(RENDER_CALL_MARKER, APPROVED_AUDIO_BLOCK_V2 + RENDER_CALL_MARKER, 1)
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
        APPROVED_AUDIO_MARKER_V2,
        'script["seed_audio_path"] = str(approved_path)',
        'script["seed_narration_text"] = approved_story_text',
        'approved_source == "youtube_narration_gate_approved"',
        'approved_story_hash == approved_text_hash',
        'approved_meta.get("approved") is True',
        'text_source": "youtube_narration_gate_metadata"',
        'tts_regeneration_allowed": False',
    ]
    router_missing = [item for item in router_required if item not in router_source]
    if router_missing:
        raise SystemExit(f"youtube narration gate: approved-audio render contract missing {router_missing}")
    if APPROVED_AUDIO_MARKER_V1 in router_source or "current_story_hash == approved_text_hash" in router_source:
        raise SystemExit("youtube narration gate: legacy post-editorial hash contract still present")


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
