from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app/routers/youtube.py"
VIDEO = ROOT / "app/services/video_generator.py"
INDEX = ROOT / "app/static/index.html"
MARKER = "CODEXIA_INTELLIGENT_COST_OPTIMIZATION_V1"


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1)


YOUTUBE_IMPORT_ANCHOR = '''from app.services.global_settings_service import get_latest_settings, serialize_official_factory_settings\n'''
YOUTUBE_IMPORT_NEW = '''from app.services.global_settings_service import get_latest_settings, serialize_official_factory_settings\nfrom app.services.intelligent_cost_optimizer import (\n    build_sparse_visual_optimization_plan,\n    validate_optimization_confirmation,\n)\n'''

HELPER_ANCHOR = '''@router.post("/task/{task_id}/retry")\ndef retry_task(task_id: str, _admin=Depends(get_current_admin_user)):'''
HELPER_BLOCK = r'''def _intelligent_retry_visual_materials(task_id: str, payload_override: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build a read-only zero-cost visual optimization proposal from local assets."""
    db = SessionLocal()
    try:
        row = db.query(VideoTask).filter(VideoTask.id == str(task_id)).first()
        if row is None:
            return {"requires_confirmation": False, "optimization_required": False}, {}
        try:
            result_obj = json.loads(getattr(row, "result_json", "") or "{}")
        except Exception:
            result_obj = {}
        if not isinstance(result_obj, dict):
            result_obj = {}
        saved_payload = result_obj.get("payload") if isinstance(result_obj.get("payload"), dict) else {}
        payload = dict(saved_payload)
        if isinstance(payload_override, dict):
            payload.update(payload_override)

        seed_script = payload.get("seeded_script") if isinstance(payload.get("seeded_script"), dict) else None
        if not _is_valid_seed_script(seed_script):
            candidate = result_obj.get("script") if isinstance(result_obj.get("script"), dict) else None
            if _is_valid_seed_script(candidate):
                seed_script = dict(candidate)
        if not isinstance(seed_script, dict):
            seed_script = {}

        image_candidates: List[str] = []
        for source in (
            payload.get("selected_images"),
            seed_script.get("selected_images") if isinstance(seed_script, dict) else None,
            result_obj.get("selected_images"),
        ):
            if not isinstance(source, list):
                continue
            for item in source:
                if not isinstance(item, str):
                    continue
                candidate = item.strip()
                if not candidate or candidate in image_candidates:
                    continue
                try:
                    if _selected_images_ok([candidate]):
                        image_candidates.append(candidate)
                except Exception:
                    continue

        audio_candidates: List[Dict[str, Any]] = []
        for candidate in (
            payload.get("reuse_audio_from"),
            result_obj.get("audio_checkpoint"),
            (result_obj.get("render_report") or {}).get("audio_generation") if isinstance(result_obj.get("render_report"), dict) else None,
        ):
            if isinstance(candidate, dict) and candidate:
                audio_candidates.append(dict(candidate))
        audio_info: Dict[str, Any] = {}
        audio_path = ""
        for candidate in audio_candidates:
            path = str(
                candidate.get("output_path")
                or candidate.get("final_audio_path")
                or candidate.get("audio_path")
                or ""
            ).strip()
            if path and _file_ok(path):
                audio_info = dict(candidate)
                audio_path = path
                break

        budget = payload.get("recovery_image_budget") if isinstance(payload.get("recovery_image_budget"), dict) else {}
        try:
            target_visual_count = int(
                budget.get("expected_image_count")
                or payload.get("expected_image_count")
                or 0
            )
        except Exception:
            target_visual_count = 0
        if target_visual_count <= 0:
            target_visual_count = len(image_candidates)

        try:
            unit_cost = float(payload.get("image_cost_unit") or 0.0)
        except Exception:
            unit_cost = 0.0
        if unit_cost <= 0 and isinstance(budget, dict):
            try:
                budget_missing = int(budget.get("missing_image_count") or 0)
                budget_cost = float(budget.get("estimated_image_cost_usd") or 0.0)
                if budget_missing > 0 and budget_cost > 0:
                    unit_cost = budget_cost / budget_missing
            except Exception:
                unit_cost = 0.0

        title = str(payload.get("override_title") or payload.get("topic") or "").strip()
        plan = build_sparse_visual_optimization_plan(
            task_id=str(task_id),
            title=title,
            target_visual_count=target_visual_count,
            valid_image_paths=image_candidates,
            script=seed_script,
            audio_path=audio_path,
            image_unit_cost_usd=unit_cost,
        )
        materials = {
            "script": dict(seed_script),
            "valid_images": list(image_candidates),
            "audio_info": dict(audio_info),
            "audio_path": audio_path,
        }
        return plan, materials
    finally:
        db.close()


def _public_intelligent_optimization_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    public = dict(plan or {})
    public.pop("valid_image_paths", None)
    return public


@router.get("/task/{task_id}/retry-plan")
def retry_task_plan(task_id: str, _admin=Depends(get_current_admin_user)):
    plan, _materials = _intelligent_retry_visual_materials(task_id)
    return _public_intelligent_optimization_plan(plan)


@router.post("/task/{task_id}/retry")
def retry_task(
    task_id: str,
    optimization_plan_hash: Optional[str] = Query(None),
    _admin=Depends(get_current_admin_user),
):'''

RETRY_PLAN_ANCHOR = '''        if bool(payload.get("_recovery_block_paid_regeneration")):\n            missing = [str(item) for item in (payload.get("_recovery_missing_assets") or []) if str(item or "").strip()]'''
RETRY_PLAN_NEW = '''        # CODEXIA_INTELLIGENT_COST_OPTIMIZATION_V1\n        # Antes de aceitar regeneração paga por falta de poucas imagens, proponha\n        # reutilização visual local e exija confirmação do plano exato.\n        optimization_plan, optimization_materials = _intelligent_retry_visual_materials(task_id, payload)\n        if bool(optimization_plan.get("requires_confirmation")):\n            if not validate_optimization_confirmation(optimization_plan, optimization_plan_hash):\n                raise HTTPException(\n                    status_code=409,\n                    detail={\n                        "code": "optimization_confirmation_required",\n                        "message": "O Codexia encontrou uma alternativa sem novas imagens pagas e precisa da sua confirmação.",\n                        "optimization_plan": _public_intelligent_optimization_plan(optimization_plan),\n                    },\n                )\n            payload["seeded_script"] = dict(optimization_materials.get("script") or {})\n            payload["selected_images"] = list(optimization_materials.get("valid_images") or [])\n            payload["reuse_audio_from"] = dict(optimization_materials.get("audio_info") or {})\n            payload["force_reuse_assets"] = True\n            payload["force_render_only"] = True\n            payload["intelligent_visual_optimization"] = _public_intelligent_optimization_plan(optimization_plan)\n            payload.pop("_recovery_block_paid_regeneration", None)\n            payload.pop("_recovery_missing_assets", None)\n\n        if bool(payload.get("_recovery_block_paid_regeneration")):\n            missing = [str(item) for item in (payload.get("_recovery_missing_assets") or []) if str(item or "").strip()]'''

WORKER_SEED_ANCHOR = '''                seed_script_ok = _is_valid_seed_script(seed_script)\n                seed_audio_ok = _file_ok(seed_audio_path)\n                seed_images_ok = _selected_images_ok(seed_selected_images)'''
WORKER_SEED_NEW = '''                # CODEXIA_INTELLIGENT_COST_OPTIMIZATION_V1\n                # Em render-only o payload confirmado é uma fonte legítima de\n                # ativos. result_json pode ser anterior ao último checkpoint.\n                request_seed_script = getattr(request, "seeded_script", None)\n                if isinstance(request_seed_script, dict) and not _is_valid_seed_script(seed_script):\n                    seed_script = dict(request_seed_script)\n\n                request_selected_images = getattr(request, "selected_images", None)\n                if not seed_selected_images and isinstance(request_selected_images, list):\n                    seed_selected_images = [\n                        str(x).strip()\n                        for x in request_selected_images\n                        if isinstance(x, str) and str(x).strip()\n                    ]\n                if isinstance(seed_script, dict) and seed_selected_images:\n                    seed_script = dict(seed_script)\n                    seed_script["selected_images"] = list(seed_selected_images)\n\n                if not seed_audio_path:\n                    request_reuse_audio = getattr(request, "reuse_audio_from", None)\n                    if isinstance(request_reuse_audio, dict):\n                        seed_audio_path = str(\n                            request_reuse_audio.get("output_path")\n                            or request_reuse_audio.get("final_audio_path")\n                            or request_reuse_audio.get("audio_path")\n                            or ""\n                        ).strip()\n                        if not seed_narration_text:\n                            seed_narration_text = str(\n                                request_reuse_audio.get("final_text_sent_to_tts")\n                                or request_reuse_audio.get("narration_text")\n                                or ""\n                            ).strip()\n\n                seed_script_ok = _is_valid_seed_script(seed_script)\n                seed_audio_ok = _file_ok(seed_audio_path)\n                seed_images_ok = _selected_images_ok(seed_selected_images)'''

WORKER_FAIL_OLD = '''                            message="Recuperação (render-only) bloqueada: faltam roteiro, imagens ou áudio válidos para reutilização.",'''
WORKER_FAIL_NEW = '''                            message=(\n                                "Recuperação (render-only) bloqueada: "\n                                f"roteiro={'OK' if seed_script_ok else 'FALTA'}, "\n                                f"imagens={'OK (' + str(len(seed_selected_images)) + ')' if seed_images_ok else 'FALTA'}, "\n                                f"áudio={'OK' if seed_audio_ok else 'FALTA'}."\n                            ),'''

VIDEO_IMPORT_ANCHOR = '''from app.services.recovery_image_budget import RecoveryImageCallBudget\n'''
VIDEO_IMPORT_NEW = '''from app.services.recovery_image_budget import RecoveryImageCallBudget\nfrom app.services.intelligent_cost_optimizer import proportional_visual_index\n'''

SELECTED_HELPER_OLD = '''    def _selected_image_for_visual_group(\n        self,\n        selected_image_paths: List[str],\n        visual_group_id: int,\n    ) -> Optional[str]:\n        """Map prepared images to contiguous narrative groups, never round-robin scenes."""\n        paths = [str(path).strip() for path in (selected_image_paths or []) if str(path).strip()]\n        if not paths:\n            return None\n        try:\n            group_index = max(0, int(visual_group_id or 0))\n        except Exception:\n            group_index = 0\n        return paths[min(group_index, len(paths) - 1)]'''
SELECTED_HELPER_NEW = '''    def _selected_image_for_visual_group(\n        self,\n        selected_image_paths: List[str],\n        visual_group_id: int,\n        total_visual_groups: Optional[int] = None,\n    ) -> Optional[str]:\n        """Reuse ordered visuals across adjacent narrative groups when needed."""\n        paths = [str(path).strip() for path in (selected_image_paths or []) if str(path).strip()]\n        if not paths:\n            return None\n        try:\n            group_index = max(0, int(visual_group_id or 0))\n        except Exception:\n            group_index = 0\n        try:\n            group_count = max(1, int(total_visual_groups or len(paths) or 1))\n        except Exception:\n            group_count = max(1, len(paths))\n        image_index = proportional_visual_index(group_index, len(paths), group_count)\n        return paths[image_index]'''

SELECTED_CALL_OLD = '''                    bg_image_path = self._selected_image_for_visual_group(\n                        selected_image_paths,\n                        visual_group_id,\n                    )'''
SELECTED_CALL_NEW = '''                    bg_image_path = self._selected_image_for_visual_group(\n                        selected_image_paths,\n                        visual_group_id,\n                        total_visual_groups=max(1, len(group_lookup)),\n                    )'''

REPORT_ANCHOR = '''            scene_to_group = {\n                int(k): int(v)\n                for k, v in (visual_group_plan.get("scene_to_group") or {}).items()\n            }'''
REPORT_NEW = '''            scene_to_group = {\n                int(k): int(v)\n                for k, v in (visual_group_plan.get("scene_to_group") or {}).items()\n            }\n            if selected_image_paths and len(selected_image_paths) < max(1, len(group_lookup)):\n                render_report["intelligent_cost_optimization"] = {\n                    "strategy": "ordered_adjacent_visual_reuse_v1",\n                    "valid_image_count": len(selected_image_paths),\n                    "narrative_group_count": max(1, len(group_lookup)),\n                    "paid_image_calls": 0,\n                    "preserve_full_narration": True,\n                    "preserve_full_text": True,\n                }'''

UI_RETRY_OLD = '''                        const res = await this.authFetch(`/youtube/task/${encodeURIComponent(taskId)}/retry`, { method: 'POST' });\n                        const data = await res.json().catch(() => ({}));'''
UI_RETRY_NEW = '''                        // CODEXIA_INTELLIGENT_COST_OPTIMIZATION_V1\n                        // Sempre consulte o plano antes de aplicar uma economia que\n                        // altere a estratégia visual original.\n                        const planRes = await this.authFetch(`/youtube/task/${encodeURIComponent(taskId)}/retry-plan`);\n                        const planData = await planRes.json().catch(() => ({}));\n                        if (!planRes.ok) {\n                            throw new Error(planData.detail || planData.message || 'Falha ao analisar alternativas de recuperação.');\n                        }\n                        let optimizationHash = '';\n                        if (planData && planData.requires_confirmation) {\n                            const valid = Number(planData.valid_image_count || 0);\n                            const target = Number(planData.target_visual_count || 0);\n                            const missing = Number(planData.missing_visual_count || 0);\n                            const savedCalls = Number(planData.estimated_image_calls_avoided || missing || 0);\n                            const savings = Number(planData.estimated_savings_usd || 0);\n                            const savingsText = savings > 0 ? `\\nEconomia estimada: US$ ${savings.toFixed(4)}.` : '';\n                            const proposal =\n                                `OTIMIZAÇÃO INTELIGENTE DE CUSTO\\n\\n` +\n                                `O Codexia encontrou uma forma de concluir o vídeo sem gerar novas imagens pagas.\\n\\n` +\n                                `Imagens válidas disponíveis: ${valid}\\n` +\n                                `Meta visual original: ${target}\\n` +\n                                `Imagens que deixariam de ser compradas: ${savedCalls}\\n` +\n                                `Estratégia: redistribuir as imagens existentes na ordem do roteiro e aumentar apenas o tempo visual quando necessário.\\n\\n` +\n                                `GARANTIAS:\\n` +\n                                `• Narração completa será preservada.\\n` +\n                                `• Nenhum texto será cortado.\\n` +\n                                `• A mensagem e a ordem narrativa serão preservadas.\\n` +\n                                `• Novas chamadas pagas de imagem: 0.` + savingsText + `\\n\\n` +\n                                `Deseja aplicar esta otimização?`;\n                            if (!window.confirm(proposal)) {\n                                this.ytStoryVideoLoading = false;\n                                return;\n                            }\n                            optimizationHash = String(planData.plan_hash || '');\n                            if (!optimizationHash) throw new Error('Plano de otimização sem assinatura; operação bloqueada.');\n                        }\n                        const retryUrl = `/youtube/task/${encodeURIComponent(taskId)}/retry` +\n                            (optimizationHash ? `?optimization_plan_hash=${encodeURIComponent(optimizationHash)}` : '');\n                        const res = await this.authFetch(retryUrl, { method: 'POST' });\n                        const data = await res.json().catch(() => ({}));'''


def patch_youtube(text: str) -> str:
    if MARKER in text:
        return text
    text = _replace_once(text, YOUTUBE_IMPORT_ANCHOR, YOUTUBE_IMPORT_NEW, "optimizer import youtube")
    if HELPER_ANCHOR not in text:
        raise PatchError("âncora da rota retry não encontrada")
    text = text.replace(HELPER_ANCHOR, HELPER_BLOCK, 1)
    text = _replace_once(text, RETRY_PLAN_ANCHOR, RETRY_PLAN_NEW, "confirmed optimization guard")
    text = _replace_once(text, WORKER_SEED_ANCHOR, WORKER_SEED_NEW, "request asset fallback")
    if WORKER_FAIL_OLD in text:
        text = text.replace(WORKER_FAIL_OLD, WORKER_FAIL_NEW, 1)
    text = text.rstrip() + f"\n\n# {MARKER}\n"
    return text


def patch_video(text: str) -> str:
    if MARKER in text:
        return text
    text = _replace_once(text, VIDEO_IMPORT_ANCHOR, VIDEO_IMPORT_NEW, "optimizer import video")
    text = _replace_once(text, SELECTED_HELPER_OLD, SELECTED_HELPER_NEW, "proportional visual helper")
    text = _replace_once(text, SELECTED_CALL_OLD, SELECTED_CALL_NEW, "proportional visual call")
    text = _replace_once(text, REPORT_ANCHOR, REPORT_NEW, "optimization render report")
    text = text.rstrip() + f"\n\n# {MARKER}\n"
    return text


def patch_index(text: str) -> str:
    if MARKER in text:
        return text
    text = _replace_once(text, UI_RETRY_OLD, UI_RETRY_NEW, "retry optimization confirmation UI")
    return text.rstrip() + f"\n<!-- {MARKER} -->\n"


def apply() -> None:
    yt = YOUTUBE.read_text(encoding="utf-8")
    vd = VIDEO.read_text(encoding="utf-8")
    ix = INDEX.read_text(encoding="utf-8")
    yt2 = patch_youtube(yt)
    vd2 = patch_video(vd)
    ix2 = patch_index(ix)
    if patch_youtube(yt2) != yt2 or patch_video(vd2) != vd2 or patch_index(ix2) != ix2:
        raise PatchError("hardening inteligente não é idempotente")
    if yt2 != yt:
        YOUTUBE.write_text(yt2, encoding="utf-8")
    if vd2 != vd:
        VIDEO.write_text(vd2, encoding="utf-8")
    if ix2 != ix:
        INDEX.write_text(ix2, encoding="utf-8")


def check() -> None:
    yt = YOUTUBE.read_text(encoding="utf-8")
    vd = VIDEO.read_text(encoding="utf-8")
    ix = INDEX.read_text(encoding="utf-8")
    required_yt = (
        MARKER,
        '"/task/{task_id}/retry-plan"',
        "optimization_plan_hash: Optional[str] = Query(None)",
        "validate_optimization_confirmation(optimization_plan, optimization_plan_hash)",
        'payload["force_render_only"] = True',
        'payload["intelligent_visual_optimization"]',
        "request_selected_images = getattr(request, \"selected_images\", None)",
        "request_reuse_audio = getattr(request, \"reuse_audio_from\", None)",
    )
    required_vd = (
        MARKER,
        "proportional_visual_index",
        "total_visual_groups=max(1, len(group_lookup))",
        'render_report["intelligent_cost_optimization"]',
        '"paid_image_calls": 0',
    )
    required_ix = (
        MARKER,
        "OTIMIZAÇÃO INTELIGENTE DE CUSTO",
        "retry-plan",
        "optimization_plan_hash",
        "Narração completa será preservada",
        "Novas chamadas pagas de imagem: 0",
    )
    missing = [x for x in required_yt if x not in yt] + [x for x in required_vd if x not in vd] + [x for x in required_ix if x not in ix]
    if missing:
        raise PatchError("otimização inteligente incompleta: " + ", ".join(missing))
    compile(yt, str(YOUTUBE), "exec")
    compile(vd, str(VIDEO), "exec")


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
        print(f"ERRO INTELLIGENT COST OPTIMIZATION: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
