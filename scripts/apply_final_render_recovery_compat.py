from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "app/routers/youtube.py"

OLD_SNAPSHOT = '''        unified_obj = _recovery_unified_snapshot(db, str(task_id))\n        uv = ('''
NEW_SNAPSHOT = '''        try:\n            unified_obj = _recovery_unified_snapshot(db, str(task_id))\n        except Exception:\n            # Tarefas antigas podem não possuir um UnifiedVideo completo. A busca\n            # física do MP4 deve continuar usando VideoTask + /data/media/videos.\n            unified_obj = {}\n        uv = ('''

OLD_UV_GUARD = '''        if uv is None:\n            return None\n\n        choice = _recovery_choose_existing_final_video'''
NEW_UV_GUARD = '''        # Não abandonar o salvamento só porque a linha UnifiedVideo não existe.\n        # O MP4 pode ter sido escrito pelo worker antes da falha do wrapper final.\n        uv_missing_at_recovery = uv is None\n\n        choice = _recovery_choose_existing_final_video'''

OLD_FRAME_COUNT = '''        requested_frames = max(1, min(64, int(getattr(uv, "image_count", 1) or 1)))'''
NEW_FRAME_COUNT = '''        try:\n            recovered_image_count = int(getattr(uv, "image_count", 0) or 0) if uv is not None else 0\n        except Exception:\n            recovered_image_count = 0\n        if recovered_image_count <= 0:\n            try:\n                recovered_image_count = int(payload.get("image_count") or 0)\n            except Exception:\n                recovered_image_count = 0\n        if recovered_image_count <= 0:\n            # Para produção antiga sem manifesto de imagens, usar uma amostra de\n            # auditoria suficiente sem inventar dezenas de frames obrigatórios.\n            recovered_image_count = 8\n        requested_frames = max(1, min(64, recovered_image_count))'''

OLD_UV_WRITE = '''        uv.video_path = video_path\n        uv.video_url = video_url or getattr(uv, "video_url", None)'''
NEW_UV_WRITE = '''        if uv is None:\n            uv = _recovery_ensure_unified_row_for_final_render(\n                db, row, payload, result_obj, requested_frames\n            )\n        if uv is None:\n            message = (\n                "MP4 final válido foi encontrado, mas não foi possível reconstruir o registro "\n                "canônico UnifiedVideo. Nenhuma chamada paga foi feita."\n            )\n            update_task(task_id, message=message)\n            return {"recovered": False, "blocked": True, "task_id": task_id, "message": message}\n\n        uv.video_path = video_path\n        uv.video_url = video_url or getattr(uv, "video_url", None)'''

OLD_NOT_FOUND_RETURN = '''            if choice.get("reason") == "ambiguous_final_render_candidates":\n                message = (\n                    "Recuperação segura encontrou mais de um MP4 final compatível e não escolheu automaticamente. "\n                    "Nenhuma nova mídia foi gerada."\n                )\n                update_task(task_id, message=message)\n                return {"recovered": False, "blocked": True, "task_id": task_id, "message": message}\n            return None'''
NEW_NOT_FOUND_RETURN = '''            if choice.get("reason") == "ambiguous_final_render_candidates":\n                message = (\n                    "Recuperação segura encontrou mais de um MP4 final compatível e não escolheu automaticamente. "\n                    "Nenhuma nova mídia foi gerada."\n                )\n                update_task(task_id, message=message)\n                return {"recovered": False, "blocked": True, "task_id": task_id, "message": message}\n\n            diagnostics = choice.get("candidates") if isinstance(choice.get("candidates"), list) else []\n            reason_counts = {}\n            for item in diagnostics:\n                if not isinstance(item, dict):\n                    continue\n                reason = str(item.get("error") or "rejeitado_sem_motivo").strip() or "rejeitado_sem_motivo"\n                reason_counts[reason] = int(reason_counts.get(reason) or 0) + 1\n            reason_text = ", ".join(\n                f"{key}={value}" for key, value in sorted(reason_counts.items())\n            )\n            base_reason = str(choice.get("reason") or "no_valid_final_render")\n            message = (\n                "Recuperação segura não encontrou um MP4 final utilizável antes da retomada paga. "\n                f"Motivo: {base_reason}. Arquivos analisados: {len(diagnostics)}"\n                + (f"; rejeições: {reason_text}." if reason_text else ".")\n                + " Os ativos existentes foram preservados e nenhuma nova mídia foi gerada."\n            )\n            update_task(task_id, message=message)\n            return {\n                "recovered": False,\n                "blocked": True,\n                "task_id": task_id,\n                "message": message,\n                "reason": base_reason,\n            }'''

HELPER_ANCHOR = '''def _recovery_try_promote_final_render(payload: Dict[str, Any], task_id: str) -> Optional[Dict[str, Any]]:'''
HELPER_BLOCK = '''def _recovery_ensure_unified_row_for_final_render(\n    db: Any,\n    row: Any,\n    payload: Dict[str, Any],\n    result_obj: Dict[str, Any],\n    requested_frames: int,\n) -> Any:\n    """Rebuild the canonical audit row only after a physical MP4 is proven valid.\n\n    This never queues work and never calls a provider. It exists solely so old\n    VideoTask rows can be promoted to review after a late wrapper failure.\n    """\n    task_id = str(getattr(row, "id", None) or "").strip()\n    if not task_id:\n        return None\n\n    uv = None\n    try:\n        uv = (\n            db.query(UnifiedVideo)\n            .filter(UnifiedVideo.task_id == task_id)\n            .order_by(UnifiedVideo.updated_at.desc())\n            .first()\n        )\n    except Exception:\n        uv = None\n\n    idempotency_key = str(payload.get("idempotency_key") or "").strip()[:255]\n    if uv is None and idempotency_key:\n        try:\n            candidate = db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == idempotency_key).first()\n        except Exception:\n            candidate = None\n        if candidate is not None and str(getattr(candidate, "task_id", None) or "").strip() in {"", task_id}:\n            uv = candidate\n\n    if uv is None:\n        # A chave de recuperação é determinística e não colide com outra tarefa.\n        fallback_key = f"recovery:{task_id}"[:255]\n        try:\n            collision = db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == fallback_key).first()\n        except Exception:\n            collision = None\n        if collision is not None and str(getattr(collision, "task_id", None) or "").strip() in {"", task_id}:\n            uv = collision\n        elif collision is not None:\n            return None\n        else:\n            try:\n                duration_minutes = max(1, min(180, int(payload.get("duration") or payload.get("duration_minutes") or 5)))\n            except Exception:\n                duration_minutes = 5\n            try:\n                user_id = int(payload.get("user_id") or 0) or None\n            except Exception:\n                user_id = None\n            uv = UnifiedVideo(\n                idempotency_key=fallback_key,\n                task_id=task_id,\n                source_module="story_recovery",\n                source_id=f"recovery:{task_id}"[:191],\n                user_id=user_id,\n                content_type=str(payload.get("kind") or "story")[:64] or "story",\n                topic=str(payload.get("topic") or payload.get("override_title") or "") or None,\n                script_text=str(payload.get("story_content") or "") or None,\n                duration_minutes=duration_minutes,\n                aspect_ratio=str(payload.get("aspect_ratio") or "16:9")[:12],\n                image_count=max(1, min(64, int(requested_frames or 8))),\n                visibility=str(payload.get("visibility") or "unlisted")[:32],\n                review_required=True,\n                auto_publish=False,\n                force_regenerate=False,\n                force_reuse_assets=True,\n                force_render_only=True,\n                text_provider=str(payload.get("text_provider") or "configured")[:64],\n                image_provider=str(payload.get("image_provider") or "configured")[:64],\n                voice_provider=str(payload.get("voice_provider") or "configured")[:64],\n                status=UnifiedVideoStatus.VALIDATING,\n                current_step="recovery_final_render",\n                progress=95,\n                last_message="Reconstruindo registro canônico a partir do MP4 final validado.",\n            )\n            db.add(uv)\n\n    uv.task_id = task_id\n    uv.force_reuse_assets = True\n    uv.force_render_only = True\n    uv.review_required = True\n    uv.auto_publish = False\n    uv.status = UnifiedVideoStatus.VALIDATING\n    uv.current_step = "recovery_final_render"\n    uv.progress = max(int(getattr(uv, "progress", 0) or 0), 95)\n    try:\n        uv.image_count = max(1, min(64, int(requested_frames or getattr(uv, "image_count", 8) or 8)))\n    except Exception:\n        pass\n\n    seeded_script = payload.get("seeded_script") if isinstance(payload.get("seeded_script"), dict) else None\n    if seeded_script is None and isinstance(result_obj.get("script"), dict):\n        seeded_script = result_obj.get("script")\n    if seeded_script:\n        uv.script_json = json.dumps(seeded_script, ensure_ascii=False)\n\n    reuse_audio = payload.get("reuse_audio_from") if isinstance(payload.get("reuse_audio_from"), dict) else {}\n    audio_path = str(\n        reuse_audio.get("output_path")\n        or reuse_audio.get("final_audio_path")\n        or reuse_audio.get("audio_path")\n        or ""\n    ).strip()\n    if audio_path and os.path.isfile(audio_path):\n        uv.audio_path = audio_path\n        try:\n            uv.audio_size_bytes = int(os.path.getsize(audio_path) or 0) or None\n        except Exception:\n            pass\n        try:\n            duration = float(\n                reuse_audio.get("final_audio_duration_sec")\n                or reuse_audio.get("duration_seconds")\n                or reuse_audio.get("audio_duration_seconds")\n                or 0.0\n            )\n        except Exception:\n            duration = 0.0\n        if duration > 0:\n            uv.audio_duration_seconds = duration\n\n    try:\n        db.flush()\n    except Exception:\n        db.rollback()\n        return None\n    return uv\n\n\n'''

MARKER = "# CODEXIA_FINAL_RENDER_RECOVERY_COMPAT_V2"


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, label: str) -> tuple[str, bool]:
    if new in text:
        return text, False
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1), True


def apply() -> None:
    text = TARGET.read_text(encoding="utf-8")
    if "CODEXIA_FINAL_RENDER_RECOVERY_V1_START" not in text:
        raise PatchError("final render recovery v1 deve ser aplicado antes do compat v2")
    changed = False

    for old, new, label in (
        (OLD_SNAPSHOT, NEW_SNAPSHOT, "snapshot-fail-open"),
        (OLD_UV_GUARD, NEW_UV_GUARD, "remove-unified-hard-dependency"),
        (OLD_FRAME_COUNT, NEW_FRAME_COUNT, "frame-count-without-unified"),
        (OLD_UV_WRITE, NEW_UV_WRITE, "rebuild-unified-after-valid-mp4"),
        (OLD_NOT_FOUND_RETURN, NEW_NOT_FOUND_RETURN, "surface-final-render-diagnostics"),
    ):
        text, did_change = _replace_once(text, old, new, label)
        changed = changed or did_change

    if "def _recovery_ensure_unified_row_for_final_render(" not in text:
        if HELPER_ANCHOR not in text:
            raise PatchError("anchor do helper de final render não encontrado")
        text = text.replace(HELPER_ANCHOR, HELPER_BLOCK + HELPER_ANCHOR, 1)
        changed = True

    if MARKER not in text:
        text = text.rstrip() + f"\n\n{MARKER}\n"
        changed = True

    if changed:
        TARGET.write_text(text, encoding="utf-8")


def check() -> None:
    text = TARGET.read_text(encoding="utf-8")
    required = (
        MARKER,
        "try:\n            unified_obj = _recovery_unified_snapshot(db, str(task_id))",
        "uv_missing_at_recovery = uv is None",
        "def _recovery_ensure_unified_row_for_final_render(",
        "recovered_image_count = int(getattr(uv, \"image_count\", 0) or 0) if uv is not None else 0",
        "Recuperação segura não encontrou um MP4 final utilizável antes da retomada paga.",
        "db, row, payload, result_obj, requested_frames",
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError(f"final render recovery compat v2 incompleto: {missing}")
    if 'if uv is None:\n            return None\n\n        choice = _recovery_choose_existing_final_video' in text:
        raise PatchError("dependência rígida de UnifiedVideo ainda existe")
    compile(text, str(TARGET), "exec")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply e/ou --check")
    try:
        if args.apply:
            apply()
        if args.check:
            check()
    except PatchError as exc:
        print(f"ERRO FINAL RENDER RECOVERY COMPAT V2: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
