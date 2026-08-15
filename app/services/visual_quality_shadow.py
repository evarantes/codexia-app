from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Type


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "sim", "on", "enabled", "enable"}


def _task_id_from_generator(generator: Any) -> Optional[str]:
    ai_service = getattr(generator, "ai_service", None)
    task_id = getattr(ai_service, "ai_task_id", None) if ai_service is not None else None
    return str(task_id).strip() if task_id else None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _inspect_image(path: str) -> Dict[str, Any]:
    """Inspeção local e gratuita do artefato visual.

    Não tenta inferir anatomia humana. Essa decisão fica explicitamente marcada
    como não executada para evitar falsa confiança. O objetivo desta fase é
    medir integridade, resolução, exposição/contraste e duplicação sem qualquer
    chamada de IA ou alteração do pipeline canônico.
    """
    result: Dict[str, Any] = {
        "path": path or None,
        "exists": False,
        "readable": False,
        "width": None,
        "height": None,
        "size_bytes": 0,
        "brightness": None,
        "contrast": None,
        "average_hash": None,
        "local_flags": [],
    }
    if not path or not os.path.isfile(path):
        result["local_flags"].append({
            "code": "artifact_not_retained_for_postcheck",
            "severity": "info",
            "message": "A imagem não estava mais retida no disco no pós-render; nenhuma decisão de qualidade foi tomada com base nisso.",
        })
        return result

    result["exists"] = True
    try:
        result["size_bytes"] = int(os.path.getsize(path))
    except Exception:
        pass

    try:
        from PIL import Image, ImageStat

        with Image.open(path) as img:
            img.load()
            result["readable"] = True
            result["width"] = int(img.width)
            result["height"] = int(img.height)
            gray = img.convert("L")
            stat = ImageStat.Stat(gray)
            brightness = float(stat.mean[0]) if stat.mean else 0.0
            contrast = float(stat.stddev[0]) if stat.stddev else 0.0
            result["brightness"] = round(brightness, 2)
            result["contrast"] = round(contrast, 2)

            tiny = gray.resize((8, 8))
            pixels = list(tiny.getdata())
            mean = sum(pixels) / max(1, len(pixels))
            bits = 0
            for idx, px in enumerate(pixels):
                if float(px) >= mean:
                    bits |= 1 << idx
            result["average_hash"] = f"{bits:016x}"

            pixels_total = int(img.width) * int(img.height)
            if min(int(img.width), int(img.height)) < 480 or pixels_total < 500_000:
                result["local_flags"].append({
                    "code": "low_source_resolution",
                    "severity": "warning",
                    "message": f"Resolução de origem baixa para acabamento premium ({img.width}x{img.height}).",
                })
            if brightness < 18:
                result["local_flags"].append({
                    "code": "extreme_darkness",
                    "severity": "warning",
                    "message": "Imagem extremamente escura; detalhes podem desaparecer após compressão/legenda.",
                })
            elif brightness > 242:
                result["local_flags"].append({
                    "code": "extreme_brightness",
                    "severity": "warning",
                    "message": "Imagem extremamente clara; há risco de áreas lavadas.",
                })
            if contrast < 12:
                result["local_flags"].append({
                    "code": "very_low_contrast",
                    "severity": "warning",
                    "message": "Contraste visual muito baixo.",
                })
    except Exception as exc:
        result["local_flags"].append({
            "code": "unreadable_image",
            "severity": "critical",
            "message": f"Arquivo visual não pôde ser lido: {type(exc).__name__}: {str(exc)[:180]}",
        })
    return result


def _hash_distance(hash_a: Optional[str], hash_b: Optional[str]) -> Optional[int]:
    if not hash_a or not hash_b:
        return None
    try:
        return (int(hash_a, 16) ^ int(hash_b, 16)).bit_count()
    except Exception:
        return None


def _flag(severity: str, code: str, message: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"severity": severity, "code": code, "message": message}
    payload.update(extra)
    return payload


def _iter_scene_visuals(render_report: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for item in list(render_report.get("scene_visuals") or []):
        if isinstance(item, dict):
            yield item


def build_visual_quality_shadow_report(
    render_report: Optional[Dict[str, Any]],
    *,
    captured_images: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    rr = render_report if isinstance(render_report, dict) else {}
    captured_images = captured_images if isinstance(captured_images, dict) else {}
    visual_plan = rr.get("visual_plan") if isinstance(rr.get("visual_plan"), dict) else {}
    sync = rr.get("sync_validation") if isinstance(rr.get("sync_validation"), dict) else {}
    max_hold_target = _safe_float(sync.get("max_visual_hold_target_sec"), 7.0) or 7.0

    scenes: list[Dict[str, Any]] = []
    global_flags: list[Dict[str, Any]] = []
    prior_hashes: list[tuple[int, str, str]] = []
    unique_paths: set[str] = set()
    duplicate_pairs: list[Dict[str, Any]] = []

    for position, item in enumerate(_iter_scene_visuals(rr), start=1):
        scene_number = int(item.get("scene_number") or position)
        path = str(item.get("image_path") or "").strip()
        if path:
            unique_paths.add(os.path.abspath(path))
        metrics = deepcopy(captured_images.get(path) or {}) if path else {}
        if not metrics:
            metrics = _inspect_image(path)

        flags: list[Dict[str, Any]] = [dict(x) for x in list(metrics.get("local_flags") or []) if isinstance(x, dict)]
        hold = _safe_float(item.get("max_visual_hold_sec") or item.get("final_visual_duration_sec"), 0.0)
        if hold > max_hold_target + 0.05:
            flags.append(_flag(
                "warning",
                "long_visual_hold",
                f"A mesma composição permanece até {hold:.2f}s; alvo cinematográfico é até {max_hold_target:.2f}s.",
                hold_seconds=round(hold, 2),
            ))
        if bool(item.get("reused")):
            flags.append(_flag(
                "info",
                "visual_reused",
                "A cena reutiliza uma composição visual anterior.",
            ))

        current_hash = str(metrics.get("average_hash") or "").strip()
        if current_hash:
            closest = None
            for prev_scene, prev_path, prev_hash in prior_hashes:
                distance = _hash_distance(current_hash, prev_hash)
                if distance is None:
                    continue
                if closest is None or distance < closest[0]:
                    closest = (distance, prev_scene, prev_path)
            if closest is not None and closest[0] <= 5:
                distance, prev_scene, prev_path = closest
                flags.append(_flag(
                    "warning",
                    "near_duplicate_visual",
                    f"Composição muito semelhante à cena {prev_scene}; pouca variedade visual.",
                    compared_scene=prev_scene,
                    hash_distance=distance,
                ))
                duplicate_pairs.append({
                    "scene_a": prev_scene,
                    "scene_b": scene_number,
                    "hash_distance": distance,
                    "path_a": prev_path,
                    "path_b": path,
                })
            prior_hashes.append((scene_number, path, current_hash))

        critical = sum(1 for f in flags if f.get("severity") == "critical")
        warnings = sum(1 for f in flags if f.get("severity") == "warning")
        infos = sum(1 for f in flags if f.get("severity") == "info")
        score = max(0.0, 10.0 - critical * 4.0 - warnings * 1.0 - infos * 0.15)
        scenes.append({
            "scene_number": scene_number,
            "image_path": path or None,
            "source": item.get("source"),
            "image_group_id": item.get("image_group_id"),
            "reused": bool(item.get("reused")),
            "final_visual_duration_sec": round(_safe_float(item.get("final_visual_duration_sec"), 0.0), 2),
            "max_visual_hold_sec": round(hold, 2),
            "local_metrics": {
                "exists": bool(metrics.get("exists")),
                "readable": bool(metrics.get("readable")),
                "width": metrics.get("width"),
                "height": metrics.get("height"),
                "size_bytes": int(metrics.get("size_bytes") or 0),
                "brightness": metrics.get("brightness"),
                "contrast": metrics.get("contrast"),
                "average_hash": metrics.get("average_hash"),
            },
            "flags": flags,
            "score": round(score, 2),
            "anatomy_ai_review": "not_run",
        })

    scene_count = len(scenes)
    critical_count = sum(1 for s in scenes for f in s.get("flags") or [] if f.get("severity") == "critical")
    warning_count = sum(1 for s in scenes for f in s.get("flags") or [] if f.get("severity") == "warning")
    reused_count = sum(1 for s in scenes if s.get("reused"))
    scores = [float(s.get("score") or 0.0) for s in scenes]
    overall_score = round(sum(scores) / len(scores), 2) if scores else 0.0

    if scene_count and len(unique_paths) <= max(1, scene_count // 3):
        global_flags.append(_flag(
            "warning",
            "low_visual_variety",
            f"Apenas {len(unique_paths)} imagens distintas para {scene_count} cenas registradas.",
        ))
    average_duration = _safe_float(visual_plan.get("average_image_duration_sec"), 0.0)
    if average_duration > 9.0:
        global_flags.append(_flag(
            "warning",
            "slow_visual_pacing",
            f"Duração média por imagem de {average_duration:.2f}s está alta para um vídeo dinâmico.",
        ))

    # O shadow mode nunca aprova anatomia. Ele apenas informa que a etapa
    # multimodal ainda precisa ser ligada na fase seguinte.
    global_flags.append(_flag(
        "info",
        "anatomy_vision_critic_pending",
        "Olhos, mãos, rostos, gênero e anatomia ainda não foram julgados por IA multimodal nesta fase shadow.",
    ))

    readiness = "needs_review" if (critical_count or warning_count or any(f.get("severity") == "warning" for f in global_flags)) else "local_checks_ok"
    return {
        "version": 1,
        "mode": "shadow",
        "generated_at": _utc_iso(),
        "blocking": False,
        "paid_ai_calls": 0,
        "scene_count": scene_count,
        "unique_image_count": len(unique_paths),
        "reused_scene_count": reused_count,
        "near_duplicate_pair_count": len(duplicate_pairs),
        "duplicate_pairs": duplicate_pairs[:20],
        "critical_flag_count": critical_count,
        "warning_flag_count": warning_count + sum(1 for f in global_flags if f.get("severity") == "warning"),
        "overall_local_score": overall_score,
        "publish_readiness": readiness,
        "global_flags": global_flags,
        "scenes": scenes,
        "feature_flags": {
            "visual_qa_shadow": True,
            "strict_visual_reject": _env_bool("ENABLE_STRICT_VISUAL_REJECT", False),
            "scene_director": _env_bool("ENABLE_SCENE_DIRECTOR", False),
            "cinematic_captions": _env_bool("ENABLE_CINEMATIC_CAPTIONS", False),
            "post_render_qa": _env_bool("ENABLE_POST_RENDER_QA", False),
        },
    }


def _persist_shadow(generator: Any, report: Dict[str, Any], result: Optional[Dict[str, Any]] = None) -> None:
    task_id = _task_id_from_generator(generator)
    if not task_id:
        return
    try:
        from app.services.task_manager import get_task, merge_task_result

        task = get_task(task_id) or {}
        existing_result = task.get("result") if isinstance(task.get("result"), dict) else {}
        rr = deepcopy(existing_result.get("render_report")) if isinstance(existing_result.get("render_report"), dict) else {}
        if isinstance(result, dict) and isinstance(result.get("render_report"), dict):
            rr = deepcopy(result.get("render_report") or {})
        rr["visual_quality_shadow"] = deepcopy(report)
        merge_task_result(task_id, {
            "visual_quality_shadow": deepcopy(report),
            "render_report": rr,
        })
    except Exception:
        # Shadow QA jamais pode transformar uma produção saudável em falha.
        pass


def install_visual_quality_shadow_patch(video_generator_cls: Type[Any]) -> Type[Any]:
    """Instrumenta o VideoGenerator canônico sem criar outro executor.

    O patch é local, idempotente e fail-open: qualquer erro no QA é ignorado e
    o resultado original do vídeo é preservado. Nenhuma imagem é regenerada e
    nenhuma IA é chamada nesta fase.
    """
    if getattr(video_generator_cls, "_codexia_visual_quality_shadow_installed", False):
        return video_generator_cls

    original_create = video_generator_cls.create_video_from_plan
    original_ensure = getattr(video_generator_cls, "_ensure_image_for_scene", None)

    if callable(original_ensure):
        def ensure_with_capture(self: Any, *args: Any, **kwargs: Any):
            path = original_ensure(self, *args, **kwargs)
            if not _env_bool("ENABLE_VISUAL_QA_SHADOW", True):
                return path
            try:
                if not isinstance(getattr(self, "_codexia_visual_shadow_images", None), dict):
                    self._codexia_visual_shadow_images = {}
                if path:
                    self._codexia_visual_shadow_images[str(path)] = _inspect_image(str(path))
            except Exception:
                pass
            return path
        video_generator_cls._ensure_image_for_scene = ensure_with_capture

    def create_with_visual_shadow(self: Any, plan: Any, *args: Any, **kwargs: Any):
        result = original_create(self, plan, *args, **kwargs)
        if not _env_bool("ENABLE_VISUAL_QA_SHADOW", True):
            return result
        try:
            if not isinstance(result, dict):
                return result
            render_report = result.get("render_report") if isinstance(result.get("render_report"), dict) else {}
            captured = getattr(self, "_codexia_visual_shadow_images", {})
            qa = build_visual_quality_shadow_report(render_report, captured_images=captured)
            render_report["visual_quality_shadow"] = qa
            result["render_report"] = render_report
            result["visual_quality_shadow"] = qa
            _persist_shadow(self, qa, result=result)
        except Exception as exc:
            # Registra somente na memória do resultado quando possível; jamais
            # altera status, vídeo ou custo por causa do monitor shadow.
            try:
                if isinstance(result, dict):
                    result["visual_quality_shadow_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            except Exception:
                pass
        return result

    video_generator_cls.create_video_from_plan = create_with_visual_shadow
    video_generator_cls._codexia_visual_quality_shadow_installed = True
    return video_generator_cls
