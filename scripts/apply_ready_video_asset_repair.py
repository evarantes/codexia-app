from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app/routers/youtube.py"
GENERATOR = ROOT / "app/services/video_generator.py"
INDEX = ROOT / "app/static/index.html"
MARKER = "CODEXIA_READY_VIDEO_ASSET_REPAIR_V1"


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1)


def _insert_before_once(text: str, anchor: str, insertion: str, label: str) -> str:
    if insertion in text:
        return text
    count = text.count(anchor)
    if count != 1:
        raise PatchError(f"{label}: âncora esperada 1 vez, encontrada {count}")
    return text.replace(anchor, insertion + anchor, 1)


VIDEO_REQUEST_OLD = '''    force_reuse_assets: bool = False\n    force_render_only: bool = False\n    editorial_reviewed: bool = False'''
VIDEO_REQUEST_NEW = '''    force_reuse_assets: bool = False\n    force_render_only: bool = False\n    # CODEXIA_READY_VIDEO_ASSET_REPAIR_V1\n    repair_mode: bool = False\n    repair_regenerate_audio: bool = False\n    repair_exclude_video: bool = False\n    repair_complete_visuals: bool = False\n    repair_image_budget: Optional[Dict[str, Any]] = None\n    repair_source_scheduled_video_id: Optional[int] = None\n    editorial_reviewed: bool = False'''

RENDER_ONLY_OLD = '''    payload.setdefault("force_reuse_assets", True)\n    if bool(payload.get("force_render_only")):\n        return payload'''
RENDER_ONLY_NEW = '''    payload.setdefault("force_reuse_assets", True)\n    # CODEXIA_READY_VIDEO_ASSET_REPAIR_V1\n    # Uma correção editorial precisa reconstruir áudio/visuais. O MP4 antigo é\n    # preservado para auditoria, mas nunca pode ser promovido como resultado.\n    if bool(payload.get("repair_mode") or payload.get("repair_exclude_video")):\n        payload["force_render_only"] = False\n        return payload\n    if bool(payload.get("force_render_only")):\n        return payload'''

SEED_AUDIO_OLD = '''                seed_script_ok = _is_valid_seed_script(seed_script)\n                seed_audio_ok = _file_ok(seed_audio_path)\n                seed_images_ok = _selected_images_ok(seed_selected_images)'''
SEED_AUDIO_NEW = '''                seed_script_ok = _is_valid_seed_script(seed_script)\n                seed_audio_ok = _file_ok(seed_audio_path)\n                seed_images_ok = _selected_images_ok(seed_selected_images)\n                # CODEXIA_READY_VIDEO_ASSET_REPAIR_V1\n                # Áudio tecnicamente válido pode estar editorialmente contaminado.\n                # No modo de correção ele é deliberadamente excluído do reuso.\n                if bool(getattr(request, "repair_regenerate_audio", False)):\n                    seed_audio_ok = False'''

SEED_SCRIPT_OLD = '''                    script = dict(seed_script or {})\n                    reused: List[str] = ["roteiro"]\n                    if seed_images_ok:'''
SEED_SCRIPT_NEW = '''                    script = dict(seed_script or {})\n                    # CODEXIA_READY_VIDEO_ASSET_REPAIR_V1\n                    if bool(getattr(request, "repair_mode", False)):\n                        script["repair_complete_visuals"] = bool(getattr(request, "repair_complete_visuals", True))\n                        repair_budget = getattr(request, "repair_image_budget", None)\n                        if isinstance(repair_budget, dict) and repair_budget.get("enabled"):\n                            script["_partial_image_recovery"] = dict(repair_budget)\n                            script["expected_image_count"] = int(repair_budget.get("expected_image_count") or 0)\n                    reused: List[str] = ["roteiro"]\n                    if seed_images_ok:'''

FINAL_PROMOTE_OLD = '''def _recovery_try_promote_final_render(payload: Dict[str, Any], task_id: str) -> Optional[Dict[str, Any]]:\n    if not isinstance(payload, dict):\n        return None'''
FINAL_PROMOTE_NEW = '''def _recovery_try_promote_final_render(payload: Dict[str, Any], task_id: str) -> Optional[Dict[str, Any]]:\n    if not isinstance(payload, dict):\n        return None\n    # CODEXIA_READY_VIDEO_ASSET_REPAIR_V1\n    # O render anterior pode conter áudio contaminado ou densidade visual baixa.\n    # Preserve-o fisicamente, porém force reconstrução no modo de correção.\n    if bool(payload.get("repair_mode") or payload.get("repair_exclude_video")):\n        return None'''

TARGET_SELECTED_OLD = '''        if selected_image_count > 0:\n            return min(scene_count, selected_image_count)'''
TARGET_SELECTED_NEW = '''        # CODEXIA_READY_VIDEO_ASSET_REPAIR_V1\n        # Imagens preservadas são ponto de partida na correção, não um teto.\n        repair_complete_visuals = bool(isinstance(plan, dict) and plan.get("repair_complete_visuals"))\n        if selected_image_count > 0 and not repair_complete_visuals:\n            return min(scene_count, selected_image_count)'''

VISUAL_SELECTED_OLD = '''                selected_image_index = None\n                if selected_image_paths:\n                    bg_image_path = self._selected_image_for_visual_group(\n                        selected_image_paths,\n                        visual_group_id,\n                    )'''
VISUAL_SELECTED_NEW = '''                selected_image_index = None\n                # CODEXIA_READY_VIDEO_ASSET_REPAIR_V1\n                # Em reparo, use cada imagem preservada uma vez no seu grupo.\n                # Grupos além do pool existente seguem para a geração controlada\n                # pelo RecoveryImageCallBudget, em vez de repetir a última imagem.\n                repair_complete_visuals = bool(isinstance(plan, dict) and plan.get("repair_complete_visuals"))\n                can_use_selected_image = bool(selected_image_paths) and (\n                    (not repair_complete_visuals)\n                    or int(visual_group_id or 0) < len(selected_image_paths)\n                )\n                if can_use_selected_image:\n                    bg_image_path = self._selected_image_for_visual_group(\n                        selected_image_paths,\n                        visual_group_id,\n                    )'''


ENDPOINTS = r'''

# CODEXIA_READY_VIDEO_ASSET_REPAIR_V1
def _ready_video_repair_task_id(scheduled: Any) -> str:
    direct = str(getattr(scheduled, "task_id", None) or "").strip()
    if direct:
        return direct
    try:
        data = json.loads(getattr(scheduled, "script_data", None) or "{}")
    except Exception:
        data = {}
    return str(data.get("task_id") or "").strip() if isinstance(data, dict) else ""


def _ready_video_repair_preview(db: Session, scheduled: Any) -> Dict[str, Any]:
    from app.services.production_manifest import load_manifest
    from app.services.ready_video_repair import build_repair_preview

    task_id = _ready_video_repair_task_id(scheduled)
    if not task_id:
        raise HTTPException(status_code=409, detail="Este vídeo não possui tarefa original vinculada para reaproveitar os ativos.")
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="A tarefa original do vídeo não foi encontrada.")
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    manifest = load_manifest(task_id) or {}
    settings = db.query(Settings).first()
    unit = 0.0
    try:
        unit = float(getattr(settings, "image_cost_unit", 0.0) or 0.0)
    except Exception:
        unit = 0.0
    try:
        seconds_per_image = float(os.getenv("VIDEO_VISUAL_QUALITY_SECONDS_PER_IMAGE") or "15")
    except Exception:
        seconds_per_image = 15.0
    preview = build_repair_preview(
        task_id=task_id,
        title=getattr(scheduled, "title", None) or "",
        task_result=result,
        payload=payload,
        manifest=manifest,
        image_cost_unit=unit,
        seconds_per_image=seconds_per_image,
    )
    preview["scheduled_video_id"] = int(getattr(scheduled, "id", 0) or 0)
    preview["current_status"] = str(getattr(scheduled, "status", None) or "")
    return preview


@router.get("/schedule/{video_id}/repair-preview")
def preview_ready_video_asset_repair(
    video_id: int,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin_user),
):
    scheduled = db.query(ScheduledVideo).filter(ScheduledVideo.id == int(video_id)).first()
    if not scheduled:
        raise HTTPException(status_code=404, detail="Vídeo aguardando publicação não encontrado.")
    return _ready_video_repair_preview(db, scheduled)


@router.post("/schedule/{video_id}/repair-with-assets")
def repair_ready_video_with_assets(
    video_id: int,
    body: Dict[str, Any],
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin_user),
):
    from app.services.production_manifest import load_manifest
    from app.services.ready_video_repair import build_confirmed_image_budget, extract_script

    scheduled = db.query(ScheduledVideo).filter(ScheduledVideo.id == int(video_id)).first()
    if not scheduled:
        raise HTTPException(status_code=404, detail="Vídeo aguardando publicação não encontrado.")
    preview = _ready_video_repair_preview(db, scheduled)
    task_id = str(preview.get("task_id") or "").strip()
    current = get_task(task_id) or {}
    status = str(current.get("status") or "").strip().lower()
    if status in {"pending", "processing", "pause_requested"}:
        raise HTTPException(status_code=409, detail="A tarefa original já está na fila ou em execução.")

    missing = int(preview.get("missing_image_count") or 0)
    confirmed = bool((body or {}).get("confirm_paid_images"))
    try:
        max_new_images = int((body or {}).get("max_new_images") if (body or {}).get("max_new_images") is not None else missing)
    except Exception:
        max_new_images = -1
    if missing > 0 and not confirmed:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "paid_images_confirmation_required",
                "message": f"A correção precisa de até {missing} novas imagens. Confirme o limite antes de qualquer chamada paga.",
                "preview": preview,
            },
        )
    try:
        image_budget = build_confirmed_image_budget(preview, max_new_images=max_new_images)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"error": "repair_plan_changed", "message": str(exc), "preview": preview})

    task_result = current.get("result") if isinstance(current.get("result"), dict) else {}
    saved_payload = task_result.get("payload") if isinstance(task_result.get("payload"), dict) else None
    if not isinstance(saved_payload, dict):
        raise HTTPException(status_code=409, detail="A tarefa original não possui payload recuperável.")
    payload = dict(saved_payload)
    manifest = load_manifest(task_id) or {}
    script = extract_script(task_result, payload, manifest)
    if not script:
        raise HTTPException(status_code=409, detail="O roteiro original não pôde ser recuperado; a correção seletiva foi bloqueada.")

    preserved_images = [str(x) for x in (preview.get("preserved_images") or []) if str(x or "").strip()]
    script = dict(script)
    script["selected_images"] = preserved_images
    script["repair_complete_visuals"] = True
    script["_partial_image_recovery"] = dict(image_budget)
    script["expected_image_count"] = int(preview.get("required_unique_image_count") or 0)

    payload["seeded_script"] = script
    payload["selected_images"] = preserved_images
    payload["force_reuse_assets"] = True
    payload["force_render_only"] = False
    payload["repair_mode"] = True
    payload["repair_regenerate_audio"] = True
    payload["repair_exclude_video"] = True
    payload["repair_complete_visuals"] = True
    payload["repair_image_budget"] = dict(image_budget)
    payload["repair_source_scheduled_video_id"] = int(video_id)
    payload["auto_upload"] = False
    for key in ("seed_audio_path", "audio_path", "final_audio_path", "video_path", "final_video_path", "output_video_path"):
        payload.pop(key, None)

    merge_task_result(task_id, {
        "payload": payload,
        "repair_requested": {
            "scheduled_video_id": int(video_id),
            "regenerate_audio": True,
            "reuse_old_mp4": False,
            "preserved_image_count": len(preserved_images),
            "required_unique_image_count": int(preview.get("required_unique_image_count") or 0),
            "max_new_image_calls": missing,
        },
    })

    try:
        scheduled_meta = json.loads(scheduled.script_data or "{}") if scheduled.script_data else {}
    except Exception:
        scheduled_meta = {}
    if not isinstance(scheduled_meta, dict):
        scheduled_meta = {}
    scheduled_meta["repair"] = {
        "task_id": task_id,
        "status": "requested",
        "prior_video_url": getattr(scheduled, "video_url", None),
        "prior_video_path": getattr(scheduled, "video_path", None),
        "preserved_image_count": len(preserved_images),
        "required_unique_image_count": int(preview.get("required_unique_image_count") or 0),
        "max_new_image_calls": missing,
    }
    scheduled.script_data = json.dumps(scheduled_meta, ensure_ascii=False)
    scheduled.status = "repairing"
    scheduled.auto_post = False
    db.commit()

    # retry_task lê o payload que acabamos de persistir e usa a MESMA tarefa.
    # Se o registro estava apenas como completed, converta-o para um estado
    # recuperável sem apagar resultado/manifesto.
    if status == "completed":
        update_task(
            task_id,
            status="failed",
            progress=int(current.get("progress") or 100),
            message="Correção seletiva solicitada; preservando roteiro/imagens e descartando áudio/MP4 do reuso.",
        )
    restarted = retry_task(task_id, _admin=_admin)
    return {
        "status": "repair_queued",
        "task_id": task_id,
        "scheduled_video_id": int(video_id),
        "preview": preview,
        "image_budget": image_budget,
        "retry": restarted,
        "message": "Correção seletiva iniciada: roteiro/imagens válidas preservados; áudio e MP4 antigos não serão reutilizados.",
    }

'''


SCHEDULED_BUTTON_OLD = '''                                                <button @click="regenerateScheduledVideo(video)" class="text-orange-600 hover:text-orange-800 p-2" title="Refazer vídeo"><i class="fas fa-sync"></i> Refazer</button>'''
SCHEDULED_BUTTON_NEW = '''                                                <!-- CODEXIA_READY_VIDEO_ASSET_REPAIR_V1 -->\n                                                <button v-if="video.task_id" @click="repairScheduledVideoWithAssets(video)" class="text-amber-800 hover:text-amber-950 bg-amber-100 hover:bg-amber-200 p-2 rounded font-medium" title="Corrigir áudio e completar imagens reaproveitando ativos"><i class="fas fa-screwdriver-wrench"></i> Corrigir com ativos</button>\n                                                <button v-else @click="regenerateScheduledVideo(video)" class="text-orange-600 hover:text-orange-800 p-2" title="Refazer vídeo"><i class="fas fa-sync"></i> Refazer</button>'''

UI_METHOD = r'''
                // CODEXIA_READY_VIDEO_ASSET_REPAIR_V1
                async repairScheduledVideoWithAssets(video) {
                    if (!video || !video.id) return;
                    try {
                        const previewRes = await this.authFetch(`/youtube/schedule/${video.id}/repair-preview`);
                        const preview = await previewRes.json().catch(() => ({}));
                        if (!previewRes.ok) {
                            const msg = preview && preview.detail ? (typeof preview.detail === 'string' ? preview.detail : (preview.detail.message || JSON.stringify(preview.detail))) : 'Não foi possível preparar a correção.';
                            alert(msg);
                            return;
                        }
                        const existing = Number(preview.existing_image_count || 0);
                        const required = Number(preview.required_unique_image_count || 0);
                        const missing = Number(preview.missing_image_count || 0);
                        const minutes = Number(preview.duration_minutes || 0).toFixed(1);
                        let costLine = '';
                        if (preview.estimated_new_image_cost !== null && preview.estimated_new_image_cost !== undefined) {
                            costLine = `\nEstimativa pelo custo unitário configurado: ${Number(preview.estimated_new_image_cost).toFixed(4)}.`;
                        }
                        const text =
                            `CORRIGIR COM ATIVOS EXISTENTES\n\n` +
                            `Vídeo: ${preview.title || video.title || ''}\n` +
                            `Duração: ~${minutes} min\n` +
                            `Roteiro: será preservado\n` +
                            `Imagens válidas preservadas: ${existing}\n` +
                            `Meta visual: ${required}\n` +
                            `Novas imagens necessárias (máximo): ${missing}\n` +
                            `Áudio antigo: NÃO será reutilizado; será narrado novamente com o filtro de códigos\n` +
                            `MP4 antigo: será preservado apenas para histórico, NÃO será reutilizado\n` +
                            `Publicação automática: bloqueada durante a correção` + costLine + `\n\n` +
                            (missing > 0
                                ? `Confirmar autoriza NO MÁXIMO ${missing} novas chamadas de imagem. Nenhuma chamada além desse limite será permitida.`
                                : `Nenhuma nova imagem paga é necessária pelo plano atual.`);
                        if (!confirm(text)) return;

                        const res = await this.authFetch(`/youtube/schedule/${video.id}/repair-with-assets`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                confirm_paid_images: missing > 0,
                                max_new_images: missing,
                            }),
                        });
                        const data = await res.json().catch(() => ({}));
                        if (!res.ok) {
                            const detail = data && data.detail ? data.detail : data;
                            alert(typeof detail === 'string' ? detail : (detail.message || JSON.stringify(detail)));
                            return;
                        }
                        alert(data.message || 'Correção seletiva colocada na fila.');
                        if (data.task_id) this.ytStoryTaskId = data.task_id;
                        await this.fetchProductionQueue();
                        await this.fetchScheduledVideos();
                        if (typeof this.fetchActiveVideoTasks === 'function') await this.fetchActiveVideoTasks();
                    } catch (e) {
                        alert('Erro ao iniciar correção seletiva: ' + (e && e.message ? e.message : e));
                    }
                },
'''


def patch_youtube(text: str) -> str:
    text = _replace_once(text, VIDEO_REQUEST_OLD, VIDEO_REQUEST_NEW, "campos de repair no VideoRequest")
    text = _replace_once(text, RENDER_ONLY_OLD, RENDER_ONLY_NEW, "bloqueio de MP4 antigo")
    if SEED_AUDIO_OLD in text:
        text = _replace_once(text, SEED_AUDIO_OLD, SEED_AUDIO_NEW, "exclusão de áudio preservado")
    elif "repair_regenerate_audio" not in text:
        raise PatchError("exclusão de áudio preservado: âncora não encontrada")
    if SEED_SCRIPT_OLD in text:
        text = _replace_once(text, SEED_SCRIPT_OLD, SEED_SCRIPT_NEW, "budget no script reparado")
    elif "script[\"repair_complete_visuals\"]" not in text:
        raise PatchError("budget no script reparado: âncora não encontrada")
    text = _replace_once(text, FINAL_PROMOTE_OLD, FINAL_PROMOTE_NEW, "não promover MP4 antigo")
    text = _insert_before_once(
        text,
        '@router.get("/diagnostics/video_generation")',
        ENDPOINTS,
        "endpoints de correção seletiva",
    )
    return text


def patch_generator(text: str) -> str:
    text = _replace_once(text, TARGET_SELECTED_OLD, TARGET_SELECTED_NEW, "imagens preservadas não são teto")
    text = _replace_once(text, VISUAL_SELECTED_OLD, VISUAL_SELECTED_NEW, "gerar grupos além do pool preservado")
    return text


def patch_index(text: str) -> str:
    text = _replace_once(text, SCHEDULED_BUTTON_OLD, SCHEDULED_BUTTON_NEW, "botão corrigir com ativos")
    text = _insert_before_once(text, '                async regenerateScheduledVideo(video) {', UI_METHOD, "método UI de correção")
    return text


def apply() -> None:
    youtube = YOUTUBE.read_text(encoding="utf-8")
    generator = GENERATOR.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    YOUTUBE.write_text(patch_youtube(youtube), encoding="utf-8")
    GENERATOR.write_text(patch_generator(generator), encoding="utf-8")
    INDEX.write_text(patch_index(index), encoding="utf-8")


def check() -> None:
    youtube = YOUTUBE.read_text(encoding="utf-8")
    generator = GENERATOR.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    required = [
        (youtube, "repair_with_assets", "endpoint repair"),
        (youtube, "repair_regenerate_audio", "flag de áudio"),
        (youtube, "repair_exclude_video", "flag de MP4"),
        (generator, "can_use_selected_image", "completar grupos visuais"),
        (generator, "repair_complete_visuals", "meta visual de reparo"),
        (index, "Corrigir com ativos", "botão UI"),
        (index, "repairScheduledVideoWithAssets", "método UI"),
    ]
    missing = [label for content, token, label in required if token not in content]
    if missing:
        raise PatchError("hardening incompleto: " + ", ".join(missing))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        args.check = True
    if args.apply:
        apply()
    if args.check:
        check()


if __name__ == "__main__":
    main()
