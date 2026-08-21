from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "app/services/channel_excellence_guard.py"
MARKER = "final_visual_quality_gate_self_heal_v1"


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho original, encontrado {count}")
    return text.replace(old, new, 1)


def patch_guard(text: str) -> str:
    if MARKER in text:
        return text

    old_metrics = '''    generated = int(visual.get("generated_image_count") or 0)\n    reused = int(visual.get("reused_image_count") or 0)\n    avg_hold = float(visual.get("average_image_duration_sec") or 0.0)\n    manual_visuals = bool(report["manual_visuals"])\n\n    # A trava agressiva vale apenas para produção visual automática. Seleção\n    # manual do usuário continua soberana e não dispara regenerações pagas.\n    if generated > 1 and not manual_visuals:\n        no_path_reuse = reused == 0\n        pacing_ok = avg_hold <= 11.0 if avg_hold > 0 else True\n    else:\n        no_path_reuse = True\n        pacing_ok = True\n    report["checks"]["no_reused_generated_image_paths"] = no_path_reuse\n    report["checks"]["average_visual_hold_ok"] = pacing_ok\n    report["metrics"] = {\n        "generated_image_count": generated,\n        "reused_image_count": reused,\n        "average_image_duration_sec": round(avg_hold, 2),\n    }\n    if not no_path_reuse:\n        report["violations"].append("generated_image_path_reused")\n    if not pacing_ok:\n        report["violations"].append("visual_hold_too_long")\n\n    report["passed"] = not report["violations"]\n    return report'''

    new_metrics = '''    # final_visual_quality_gate_self_heal_v1\n    generated = int(visual.get("generated_image_count") or 0)\n    reused = int(visual.get("reused_image_count") or 0)\n    legacy_avg_hold = float(visual.get("average_image_duration_sec") or 0.0)\n    manual_visuals = bool(report["manual_visuals"])\n\n    # O renderer já divide cenas longas em beats cinematográficos. O antigo\n    # cálculo somava a duração total por CAMINHO de imagem e produzia falso\n    # positivo quando um arquivo era reutilizado. A qualidade visual deve medir\n    # o maior hold real de cada beat, não a duração acumulada do asset.\n    scene_visuals = rr.get("scene_visuals") if isinstance(rr.get("scene_visuals"), list) else []\n    beat_holds = []\n    for item in scene_visuals:\n        if not isinstance(item, dict):\n            continue\n        try:\n            hold = float(item.get("max_visual_hold_sec") or 0.0)\n        except (TypeError, ValueError):\n            hold = 0.0\n        if hold > 0:\n            beat_holds.append(hold)\n\n    resource_profile = rr.get("resource_profile") if isinstance(rr.get("resource_profile"), dict) else {}\n    try:\n        planned_hold_target = float(resource_profile.get("visual_hold_target_sec") or 0.0)\n    except (TypeError, ValueError):\n        planned_hold_target = 0.0\n    hold_limit = max(11.0, planned_hold_target + 0.75 if planned_hold_target > 0 else 11.0)\n    max_beat_hold = max(beat_holds) if beat_holds else 0.0\n    avg_beat_hold = (sum(beat_holds) / len(beat_holds)) if beat_holds else 0.0\n\n    if generated > 1 and not manual_visuals:\n        no_path_reuse = reused == 0\n        pacing_ok = max_beat_hold <= hold_limit if max_beat_hold > 0 else True\n    else:\n        no_path_reuse = True\n        pacing_ok = True\n\n    report["checks"]["no_reused_generated_image_paths"] = no_path_reuse\n    report["checks"]["visual_beat_hold_ok"] = pacing_ok\n    report["metrics"] = {\n        "generated_image_count": generated,\n        "reused_image_count": reused,\n        "legacy_average_image_duration_sec": round(legacy_avg_hold, 2),\n        "average_visual_beat_hold_sec": round(avg_beat_hold, 2),\n        "max_visual_beat_hold_sec": round(max_beat_hold, 2),\n        "visual_hold_limit_sec": round(hold_limit, 2),\n        "visual_hold_target_sec": round(planned_hold_target, 2),\n    }\n\n    # Esses dois sinais são importantes para a REVISÃO HUMANA, mas não podem\n    # destruir um MP4 válido já renderizado nem forçar novo gasto automático.\n    # O renderer aplica movimentos/beats diferentes quando há reaproveitamento.\n    visual_warnings = []\n    if not no_path_reuse:\n        visual_warnings.append("generated_image_path_reused")\n    if not pacing_ok:\n        visual_warnings.append("visual_hold_too_long")\n\n    # Tudo que já estava em violations antes desta etapa continua sendo blocker\n    # (ex.: abertura genérica/fechamento oculto). Só os sinais visuais\n    # recuperáveis viram warnings antes da revisão humana.\n    blocking_violations = list(report["violations"])\n    report["violations"].extend(visual_warnings)\n    report["warnings"] = visual_warnings\n    report["blocking_violations"] = blocking_violations\n    report["review_recommended"] = bool(visual_warnings)\n    report["auto_render_preserved"] = bool(visual_warnings)\n    report["passed"] = not blocking_violations\n    return report'''

    text = _replace_once(
        text,
        old_metrics,
        new_metrics,
        label="quality-gate/use-real-visual-beat-metrics",
    )

    old_raise = '''                if _enabled("ENABLE_FINAL_VIDEO_QUALITY_GATE", "true") and not quality.get("passed"):\n                    violations = ", ".join(str(item) for item in quality.get("violations") or [])\n                    raise RuntimeError(\n                        "Vídeo reprovado pelo controle final de qualidade antes da revisão: "\n                        + (violations or "qualidade insuficiente")\n                    )'''
    new_raise = '''                if _enabled("ENABLE_FINAL_VIDEO_QUALITY_GATE", "true") and not quality.get("passed"):\n                    blocking = quality.get("blocking_violations") or quality.get("violations") or []\n                    violations = ", ".join(str(item) for item in blocking)\n                    raise RuntimeError(\n                        "Vídeo reprovado pelo controle final de qualidade antes da revisão: "\n                        + (violations or "qualidade insuficiente")\n                    )'''
    return _replace_once(
        text,
        old_raise,
        new_raise,
        label="quality-gate/raise-only-for-blocking-violations",
    )


def check_text(text: str) -> None:
    required = (
        MARKER,
        '"max_visual_beat_hold_sec"',
        '"visual_hold_limit_sec"',
        '"warnings"] = visual_warnings',
        '"blocking_violations"] = blocking_violations',
        '"auto_render_preserved"] = bool(visual_warnings)',
        'blocking = quality.get("blocking_violations")',
    )
    for token in required:
        if token not in text:
            raise PatchError(f"final visual gate self-heal incompleto: ausente {token}")
    if 'pacing_ok = avg_hold <= 11.0' in text:
        raise PatchError("métrica antiga de hold por asset ainda existe")


def check() -> None:
    check_text(TARGET.read_text(encoding="utf-8"))


def apply(*, write: bool) -> int:
    original = TARGET.read_text(encoding="utf-8")
    transformed = patch_guard(original)
    second = patch_guard(transformed)
    if second != transformed:
        raise PatchError("transformação não idempotente")
    check_text(transformed)
    if write and transformed != original:
        TARGET.write_text(transformed, encoding="utf-8")
    print(f"Final visual quality gate self-heal: {'alterado' if transformed != original else 'já aplicado'}.")
    return int(transformed != original)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply ou --check")
    try:
        if args.apply:
            apply(write=True)
        if args.check:
            check()
    except PatchError as exc:
        print(f"ERRO FINAL VISUAL QUALITY GATE SELF-HEAL: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
