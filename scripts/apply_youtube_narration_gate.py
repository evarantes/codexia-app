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
APPROVED_AUDIO_MARKER_CORE = "# narration-core-approved-audio-reuse-v1"
APPROVED_AUDIO_DURABLE_MARKER = "# narration-core-approved-audio-durable-v1"

APPROVED_AUDIO_BLOCK_CORE = '''        # narration-core-approved-audio-reuse-v1\n        # O vídeo final só aceita MP3 aprovado pelo Narration Core v1.\n        # Não há fallback para novo TTS: perda/incompatibilidade do áudio falha fechado.\n        approved_audio = request.reuse_audio_from if isinstance(request.reuse_audio_from, dict) else None\n        if approved_audio:\n            from app.config import AUDIO_OUTPUT_DIR as _APPROVED_AUDIO_ROOT\n            from app.services.narration_core import (\n                NARRATION_CORE_NAMESPACE as _NARRATION_CORE_NAMESPACE,\n                NARRATION_CORE_VERSION as _NARRATION_CORE_VERSION,\n            )\n\n            approved_source = str(approved_audio.get("source") or "").strip()\n            approved_path_raw = str(approved_audio.get("output_path") or "").strip()\n            approved_text_hash = str(approved_audio.get("text_sha256") or "").strip().lower()\n            approved_preview_id = str(approved_audio.get("preview_id") or "").strip().lower()\n            approved_core_version = int(approved_audio.get("narration_core_version") or 0)\n            approved_core_namespace = str(approved_audio.get("narration_core_namespace") or "").strip()\n\n            approved_root = Path(_APPROVED_AUDIO_ROOT).resolve() / "youtube_narration_core_v1"\n            approved_path = Path(approved_path_raw).resolve() if approved_path_raw else None\n            path_inside_gate = False\n            if approved_path is not None:\n                try:\n                    approved_path.relative_to(approved_root)\n                    path_inside_gate = True\n                except Exception:\n                    path_inside_gate = False\n\n            approved_story_text = ""\n            approved_story_hash = ""\n            approved_meta_ok = False\n            if approved_path is not None and path_inside_gate:\n                approved_meta_path = approved_path.with_suffix(".json")\n                try:\n                    approved_meta = json.loads(approved_meta_path.read_text(encoding="utf-8"))\n                    approved_story_text = str(approved_meta.get("spoken_text_sent_to_tts") or "").replace("\\r\\n", "\\n").replace("\\r", "\\n").strip()\n                    approved_story_hash = hashlib.sha256(approved_story_text.encode("utf-8")).hexdigest() if approved_story_text else ""\n                    approved_meta_ok = bool(\n                        isinstance(approved_meta, dict)\n                        and approved_meta.get("approved") is True\n                        and int(approved_meta.get("narration_core_version") or 0) == _NARRATION_CORE_VERSION\n                        and str(approved_meta.get("narration_core_namespace") or "").strip() == _NARRATION_CORE_NAMESPACE\n                        and str(approved_meta.get("preview_id") or "").strip().lower() == approved_preview_id\n                        and str(approved_meta.get("text_sha256") or "").strip().lower() == approved_text_hash\n                        and approved_story_hash == approved_text_hash\n                    )\n                except Exception:\n                    approved_meta_ok = False\n\n            approved_ok = bool(\n                approved_source == "youtube_narration_core_v1_approved"\n                and approved_core_version == _NARRATION_CORE_VERSION\n                and approved_core_namespace == _NARRATION_CORE_NAMESPACE\n                and approved_path is not None\n                and path_inside_gate\n                and approved_path.is_file()\n                and approved_path.stat().st_size > 512\n                and approved_text_hash\n                and approved_preview_id\n                and approved_meta_ok\n            )\n            if not approved_ok:\n                raise RuntimeError(\n                    "Narração aprovada inválida ou criada por núcleo antigo. "\n                    "O vídeo foi bloqueado para impedir qualquer novo TTS silencioso; gere e aprove uma nova narração."\n                )\n\n            # narration-core-approved-audio-durable-v1\n            from app.services.production_manifest import record_artifact as _record_production_artifact\n\n            approved_audio_sha256 = hashlib.sha256(approved_path.read_bytes()).hexdigest()\n            durable_audio_entry = _record_production_artifact(task_id, str(approved_path), kind="audio", source="approved_narration_core_v1")\n            durable_audio_path_raw = str((durable_audio_entry or {}).get("durable_path") or "").strip()\n            durable_audio_path = Path(durable_audio_path_raw).resolve() if durable_audio_path_raw else None\n            durable_audio_ok = bool(durable_audio_path is not None and durable_audio_path.is_file() and durable_audio_path.stat().st_size == approved_path.stat().st_size and durable_audio_path.stat().st_size > 512)\n            if durable_audio_ok:\n                durable_audio_sha256 = hashlib.sha256(durable_audio_path.read_bytes()).hexdigest()\n                durable_audio_ok = durable_audio_sha256 == approved_audio_sha256\n            if not durable_audio_ok:\n                raise RuntimeError("O MP3 aprovado foi validado, mas não pôde ser preservado no manifesto da tarefa. A produção foi bloqueada antes de imagens/render e nenhum novo TTS será criado.")\n\n            if not isinstance(script, dict):\n                raise RuntimeError("Roteiro inválido ao aplicar a narração aprovada.")\n            script["seed_audio_path"] = str(durable_audio_path)\n            script["seed_narration_text"] = approved_story_text\n            script["approved_narration_preview_id"] = approved_preview_id\n            script["approved_narration_text_sha256"] = approved_text_hash\n            script["approved_narration_required"] = True\n            script["approved_narration_audio_sha256"] = approved_audio_sha256\n            script["narration_core_version"] = _NARRATION_CORE_VERSION\n            script["narration_core_namespace"] = _NARRATION_CORE_NAMESPACE\n            script["narration_source"] = "approved_narration_core_v1_reuse"\n            update_task(task_id, result=_merged_task_result({"approved_narration_reuse": {"used": True, "preview_id": script.get("approved_narration_preview_id"), "text_sha256": approved_text_hash, "audio_path": str(durable_audio_path), "original_audio_path": str(approved_path), "audio_sha256": approved_audio_sha256, "manifest_persisted": True, "text_source": "narration_core_v1_metadata", "tts_regeneration_allowed": False, "approved_narration_required": True, "narration_core_version": _NARRATION_CORE_VERSION, "narration_core_namespace": _NARRATION_CORE_NAMESPACE}}))\n\n'''


def _replace_existing_router_block(source: str) -> str:
    for old_marker in (APPROVED_AUDIO_MARKER_V1, APPROVED_AUDIO_MARKER_V2, APPROVED_AUDIO_MARKER_CORE):
        if old_marker in source:
            if old_marker == APPROVED_AUDIO_MARKER_CORE and APPROVED_AUDIO_DURABLE_MARKER in source:
                return source
            start = source.find(old_marker)
            line_start = source.rfind("\n", 0, start) + 1
            end = source.find(RENDER_CALL_MARKER, start)
            if end < 0:
                raise SystemExit("youtube narration core: canonical render call missing after legacy block")
            return source[:line_start] + APPROVED_AUDIO_BLOCK_CORE + source[end:]
    return source


def _apply_router_patch() -> bool:
    if not YOUTUBE_ROUTER.is_file(): raise SystemExit("youtube narration core: youtube router missing")
    source = YOUTUBE_ROUTER.read_text(encoding="utf-8")
    if APPROVED_AUDIO_MARKER_CORE in source and APPROVED_AUDIO_DURABLE_MARKER in source: return False
    upgraded = _replace_existing_router_block(source)
    if upgraded != source:
        YOUTUBE_ROUTER.write_text(upgraded, encoding="utf-8"); return True
    if source.count(RENDER_CALL_MARKER) != 1: raise SystemExit(f"youtube narration core: expected one canonical render call marker, found {source.count(RENDER_CALL_MARKER)}")
    source = source.replace(RENDER_CALL_MARKER, APPROVED_AUDIO_BLOCK_CORE + RENDER_CALL_MARKER, 1)
    YOUTUBE_ROUTER.write_text(source, encoding="utf-8"); return True


def apply() -> bool:
    changed = False
    text = INDEX.read_text(encoding="utf-8")
    if TAG not in text:
        if MARKER not in text: raise SystemExit("youtube narration core: </body> marker not found")
        INDEX.write_text(text.replace(MARKER, f"    {TAG}\n{MARKER}", 1), encoding="utf-8"); changed = True
    if not JS.is_file(): raise SystemExit("youtube narration core: JS asset missing")
    source = JS.read_text(encoding="utf-8")
    if OLD_BUTTON_LOOKUP in source:
        JS.write_text(source.replace(OLD_BUTTON_LOOKUP, NEW_BUTTON_LOOKUP, 1), encoding="utf-8"); changed = True
    elif NEW_BUTTON_LOOKUP not in source: raise SystemExit("youtube narration core: canonical video button lookup not found")
    if _apply_router_patch(): changed = True
    return changed


def check() -> None:
    text = INDEX.read_text(encoding="utf-8")
    if text.count(TAG) != 1: raise SystemExit(f"youtube narration core: expected exactly one script tag, found {text.count(TAG)}")
    if not JS.is_file(): raise SystemExit("youtube narration core: JS asset missing")
    source = JS.read_text(encoding="utf-8")
    required = ["Gerar primeiro o áudio da narração", "Agora gerar o vídeo com este áudio", "Aprovar esta narração", "Refazer seguindo minha observação", "/youtube/narration-lab/production-preview", "reuse_audio_from", "approved_narration_text_sha256", "approvedLaunchArmed", "A produção foi bloqueada: gere, ouça e aprove o áudio antes de criar o vídeo.", NEW_BUTTON_LOOKUP]
    missing = [item for item in required if item not in source]
    if missing: raise SystemExit(f"youtube narration core: JS contract missing {missing}")
    router_source = YOUTUBE_ROUTER.read_text(encoding="utf-8")
    router_required = [APPROVED_AUDIO_MARKER_CORE, APPROVED_AUDIO_DURABLE_MARKER, 'Path(_APPROVED_AUDIO_ROOT).resolve() / "youtube_narration_core_v1"', 'approved_source == "youtube_narration_core_v1_approved"', 'approved_meta.get("approved") is True', 'approved_story_hash == approved_text_hash', '_record_production_artifact(', 'source="approved_narration_core_v1"', 'script["seed_audio_path"] = str(durable_audio_path)', 'script["approved_narration_required"] = True', 'script["approved_narration_audio_sha256"] = approved_audio_sha256', '"manifest_persisted": True', '"tts_regeneration_allowed": False', '"narration_core_version": _NARRATION_CORE_VERSION']
    router_missing = [item for item in router_required if item not in router_source]
    if router_missing: raise SystemExit(f"youtube narration core: approved-audio render contract missing {router_missing}")
    if APPROVED_AUDIO_MARKER_V1 in router_source or APPROVED_AUDIO_MARKER_V2 in router_source: raise SystemExit("youtube narration core: legacy approved-audio block still present")


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--apply", action="store_true"); parser.add_argument("--check", action="store_true"); args = parser.parse_args()
    if not args.apply and not args.check: parser.error("use --apply and/or --check")
    if args.apply: print("youtube narration core:", "applied" if apply() else "already applied")
    if args.check: check(); print("youtube narration core: OK")


if __name__ == "__main__": main()
