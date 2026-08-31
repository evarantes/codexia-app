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
APPROVED_AUDIO_MARKER_V3 = "# narration-gate-approved-audio-reuse-v3"
REQUEST_FALLBACK_MARKER = "CODEXIA_APPROVED_NARRATION_REQUEST_FALLBACK_V1"
STRUCTURAL_RESOLVER_MARKER = "CODEXIA_APPROVED_NARRATION_STRUCTURAL_RESOLVER_V1"

APPROVED_AUDIO_BLOCK_V3 = '''        # narration-gate-approved-audio-reuse-v3
        # CODEXIA_APPROVED_NARRATION_STRUCTURAL_RESOLVER_V1
        # O worker reconstrói a aprovação a partir do preview_id + registro
        # protegido do narration gate. Campos do request e do payload persistido
        # são apenas pistas; a fonte de verdade é o JSON approved=true ao lado do MP3.
        approved_audio = request.reuse_audio_from if isinstance(request.reuse_audio_from, dict) else {}
        task_payload = {}
        try:
            _task_snapshot = get_task(task_id) or {}
            _task_result = _task_snapshot.get("result") if isinstance(_task_snapshot.get("result"), dict) else {}
            task_payload = _task_result.get("payload") if isinstance(_task_result.get("payload"), dict) else {}
        except Exception:
            task_payload = {}
        persisted_audio = task_payload.get("reuse_audio_from") if isinstance(task_payload.get("reuse_audio_from"), dict) else {}
        if not approved_audio and persisted_audio:
            approved_audio = dict(persisted_audio)

        # CODEXIA_APPROVED_NARRATION_REQUEST_FALLBACK_V1
        request_text_hash = str(getattr(request, "approved_narration_text_sha256", "") or "").strip().lower()
        request_preview_id = str(getattr(request, "approved_narration_preview_id", "") or "").strip().lower()
        persisted_text_hash = str(task_payload.get("approved_narration_text_sha256") or "").strip().lower()
        persisted_preview_id = str(task_payload.get("approved_narration_preview_id") or "").strip().lower()
        approved_text_hash_hint = str(approved_audio.get("text_sha256") or request_text_hash or persisted_text_hash or "").strip().lower()
        approved_preview_id_hint = str(approved_audio.get("preview_id") or request_preview_id or persisted_preview_id or "").strip().lower()
        approved_path_raw = str(approved_audio.get("output_path") or persisted_audio.get("output_path") or "").strip()

        from app.config import AUDIO_OUTPUT_DIR as _APPROVED_AUDIO_ROOT
        approved_root = Path(_APPROVED_AUDIO_ROOT).resolve() / "youtube_narration_gate"

        def _safe_preview_hint(value):
            _value = str(value or "").strip().lower()
            return _value if len(_value) == 32 and all(ch in "0123456789abcdef" for ch in _value) else ""

        approved_preview_id_hint = _safe_preview_hint(approved_preview_id_hint)
        requested_path = None
        if approved_path_raw:
            try:
                _candidate = Path(approved_path_raw).resolve()
                _candidate.relative_to(approved_root)
                requested_path = _candidate
                if not approved_preview_id_hint:
                    approved_preview_id_hint = _safe_preview_hint(_candidate.stem)
            except Exception:
                requested_path = None

        candidate_meta_paths = []
        if requested_path is not None:
            candidate_meta_paths.append(requested_path.with_suffix(".json"))
        if approved_preview_id_hint:
            try:
                candidate_meta_paths.extend(sorted(approved_root.glob(f"*/{approved_preview_id_hint}.json")))
            except Exception:
                pass

        seen_meta = set()
        approved_path = None
        approved_meta_path = None
        approved_story_text = ""
        approved_story_hash = ""
        approved_text_hash = ""
        approved_preview_id = ""
        for _meta_path in candidate_meta_paths:
            try:
                _meta_path = Path(_meta_path).resolve()
                _meta_path.relative_to(approved_root)
                _meta_key = str(_meta_path)
                if _meta_key in seen_meta or not _meta_path.is_file():
                    continue
                seen_meta.add(_meta_key)
                _meta = json.loads(_meta_path.read_text(encoding="utf-8"))
                if not isinstance(_meta, dict) or _meta.get("approved") is not True:
                    continue
                _meta_preview = _safe_preview_hint(_meta.get("preview_id"))
                if not _meta_preview:
                    continue
                if approved_preview_id_hint and _meta_preview != approved_preview_id_hint:
                    continue
                _meta_text = str(_meta.get("spoken_text_sent_to_tts") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
                _meta_hash = str(_meta.get("text_sha256") or "").strip().lower()
                _story_hash = hashlib.sha256(_meta_text.encode("utf-8")).hexdigest() if _meta_text else ""
                if not _meta_text or not _meta_hash or _story_hash != _meta_hash:
                    continue
                if approved_text_hash_hint and _meta_hash != approved_text_hash_hint:
                    continue
                _mp3_path = _meta_path.with_suffix(".mp3").resolve()
                _mp3_path.relative_to(approved_root)
                if not _mp3_path.is_file() or _mp3_path.stat().st_size <= 512:
                    continue
                approved_meta_path = _meta_path
                approved_path = _mp3_path
                approved_story_text = _meta_text
                approved_story_hash = _story_hash
                approved_text_hash = _meta_hash
                approved_preview_id = _meta_preview
                break
            except Exception:
                continue

        approved_ok = bool(
            approved_path is not None
            and approved_meta_path is not None
            and approved_path.is_file()
            and approved_story_text
            and approved_story_hash
            and approved_story_hash == approved_text_hash
            and approved_preview_id
        )
        if not approved_ok:
            raise RuntimeError(
                "Narração aprovada não pôde ser reconstruída a partir do registro protegido do narration gate. "
                "O vídeo foi bloqueado para impedir nova geração de TTS."
            )

        if not isinstance(script, dict):
            raise RuntimeError("Roteiro inválido ao aplicar a narração aprovada.")
        script["seed_audio_path"] = str(approved_path)
        script["seed_narration_text"] = approved_story_text
        script["approved_narration_preview_id"] = approved_preview_id
        script["approved_narration_text_sha256"] = approved_text_hash
        script["narration_source"] = "approved_preview_reuse"
        script["approved_narration_required"] = True
        update_task(task_id, result=_merged_task_result({
            "approved_narration_reuse": {
                "used": True,
                "preview_id": approved_preview_id,
                "text_sha256": approved_text_hash,
                "audio_path": str(approved_path),
                "metadata_path": str(approved_meta_path),
                "text_source": "youtube_narration_gate_metadata",
                "resolver": "structural_v3",
                "tts_regeneration_allowed": False,
                "approved_narration_required": True,
            }
        }))

'''


def _replace_approved_router_block(source: str) -> str:
    if APPROVED_AUDIO_MARKER_V3 in source and STRUCTURAL_RESOLVER_MARKER in source:
        return source
    starts = [
        pos for pos in (
            source.find(APPROVED_AUDIO_MARKER_V2),
            source.find(APPROVED_AUDIO_MARKER_V1),
        )
        if pos >= 0
    ]
    if starts:
        start = min(starts)
        line_start = source.rfind("\n", 0, start) + 1
        end = source.find(RENDER_CALL_MARKER, start)
        if end < 0:
            raise SystemExit("youtube narration gate: canonical render call missing after approved-audio block")
        return source[:line_start] + APPROVED_AUDIO_BLOCK_V3 + source[end:]
    if source.count(RENDER_CALL_MARKER) != 1:
        raise SystemExit(
            f"youtube narration gate: expected one canonical render call marker, found {source.count(RENDER_CALL_MARKER)}"
        )
    return source.replace(RENDER_CALL_MARKER, APPROVED_AUDIO_BLOCK_V3 + RENDER_CALL_MARKER, 1)


def _apply_router_patch() -> bool:
    if not YOUTUBE_ROUTER.is_file():
        raise SystemExit("youtube narration gate: youtube router missing")
    source = YOUTUBE_ROUTER.read_text(encoding="utf-8")
    upgraded = _replace_approved_router_block(source)
    if upgraded == source:
        return False
    YOUTUBE_ROUTER.write_text(upgraded, encoding="utf-8")
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
        APPROVED_AUDIO_MARKER_V3,
        STRUCTURAL_RESOLVER_MARKER,
        REQUEST_FALLBACK_MARKER,
        'task_payload.get("reuse_audio_from")',
        'approved_root.glob(f"*/{approved_preview_id_hint}.json")',
        'approved_narration_required"] = True',
        'resolver": "structural_v3"',
        'tts_regeneration_allowed": False',
    ]
    router_missing = [item for item in router_required if item not in router_source]
    if router_missing:
        raise SystemExit(f"youtube narration gate: approved-audio v3 contract missing {router_missing}")
    if APPROVED_AUDIO_MARKER_V1 in router_source or APPROVED_AUDIO_MARKER_V2 in router_source:
        raise SystemExit("youtube narration gate: legacy approved-audio contract still present")
    if 'approved_source == "youtube_narration_gate_approved"' in router_source:
        raise SystemExit("youtube narration gate: source-string legacy guard still blocks structural recovery")


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
