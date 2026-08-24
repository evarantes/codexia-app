from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app/routers/youtube.py"
MARKER = "CODEXIA_RUNTIME_RENDER_MONITOR_COMPAT_V1"


class PatchError(RuntimeError):
    pass


BASE_HELPER = '''def _runtime_interruption_seconds() -> int:\n    try:\n        raw = int((os.getenv("VIDEO_RUNTIME_INTERRUPTION_SECONDS") or "").strip() or "300")\n    except Exception:\n        raw = 300\n    return max(120, min(30 * 60, raw))\n'''

HELPER = '''def _runtime_interruption_seconds() -> int:\n    try:\n        raw = int((os.getenv("VIDEO_RUNTIME_INTERRUPTION_SECONDS") or "").strip() or "300")\n    except Exception:\n        raw = 300\n    return max(120, min(30 * 60, raw))\n\n\ndef _runtime_effective_interruption_seconds(task: Dict[str, Any], telemetry_obj: Optional[Dict[str, Any]] = None) -> int:\n    """Não deixa o monitor genérico matar um render final antes do watchdog de FFmpeg.\n\n    O render possui seu próprio watchdog com limite suave/prolongado. O monitor\n    de heartbeat continua em 300s para as outras fases, mas stage_6_render usa\n    um teto posterior ao hard-stall do render, evitando falso positivo durante\n    MoviePy/FFmpeg sem atualização de heartbeat.\n    """\n    # CODEXIA_RUNTIME_RENDER_MONITOR_COMPAT_V1\n    telemetry = telemetry_obj if isinstance(telemetry_obj, dict) else {}\n    base = _runtime_interruption_seconds()\n    normal_limit = base if int(telemetry.get("version") or 0) >= 1 else max(15 * 60, base)\n\n    task_obj = task if isinstance(task, dict) else {}\n    result_obj = task_obj.get("result") if isinstance(task_obj.get("result"), dict) else {}\n    render_guard = result_obj.get("render_stall_guard") if isinstance(result_obj.get("render_stall_guard"), dict) else {}\n    recovery = result_obj.get("recovery_checkpoint") if isinstance(result_obj.get("recovery_checkpoint"), dict) else {}\n    message = str(task_obj.get("message") or "").strip().lower()\n    stage_text = " ".join(\n        str(value or "").strip().lower()\n        for value in (\n            telemetry.get("stage"),\n            render_guard.get("stage"),\n            recovery.get("stage"),\n            result_obj.get("stage"),\n            result_obj.get("checkpoint"),\n            message,\n        )\n    )\n    is_final_render = (\n        "stage_6_render" in stage_text\n        or "renderizando vídeo final" in stage_text\n        or "renderizando video final" in stage_text\n        or "renderizando arquivo final" in stage_text\n    )\n    if not is_final_render:\n        return int(normal_limit)\n\n    try:\n        raw_render_limit = int(\n            (\n                os.getenv("VIDEO_RUNTIME_RENDER_INTERRUPTION_SECONDS")\n                or os.getenv("VIDEO_RENDER_HARD_STALL_SECONDS")\n                or "2400"\n            ).strip()\n            or "2400"\n        )\n    except Exception:\n        raw_render_limit = 2400\n    # O monitor genérico deve ficar depois do watchdog especializado, nunca antes.\n    render_limit = max(900, min(7200, raw_render_limit)) + 120\n    return int(max(normal_limit, render_limit))\n'''

OLD_CONDITION = '''and int(signal_age) >= (\n                _runtime_interruption_seconds()\n                if int(telemetry_obj.get("version") or 0) >= 1\n                else max(15 * 60, _runtime_interruption_seconds())\n            )'''
NEW_CONDITION = '''and int(signal_age) >= _runtime_effective_interruption_seconds(task, telemetry_obj)'''


def patch_youtube(text: str) -> str:
    if MARKER not in text:
        count = text.count(BASE_HELPER)
        if count != 1:
            raise PatchError(f"helper base de interrupção esperado 1 vez; encontrado {count}")
        text = text.replace(BASE_HELPER, HELPER, 1)

    if OLD_CONDITION in text:
        count = text.count(OLD_CONDITION)
        if count < 1:
            raise PatchError("condição antiga de interrupção não encontrada")
        text = text.replace(OLD_CONDITION, NEW_CONDITION)

    if NEW_CONDITION not in text:
        raise PatchError("condição compatível de render não foi aplicada")
    return text


def apply() -> None:
    original = YOUTUBE.read_text(encoding="utf-8")
    transformed = patch_youtube(original)
    if patch_youtube(transformed) != transformed:
        raise PatchError("patch do monitor de render não é idempotente")
    if transformed != original:
        YOUTUBE.write_text(transformed, encoding="utf-8")


def check() -> None:
    text = YOUTUBE.read_text(encoding="utf-8")
    required = (
        MARKER,
        "def _runtime_effective_interruption_seconds",
        'os.getenv("VIDEO_RUNTIME_RENDER_INTERRUPTION_SECONDS")',
        'os.getenv("VIDEO_RENDER_HARD_STALL_SECONDS")',
        '"stage_6_render" in stage_text',
        "render_limit = max(900, min(7200, raw_render_limit)) + 120",
        "_runtime_effective_interruption_seconds(task, telemetry_obj)",
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError("compatibilidade do monitor de render incompleta: " + ", ".join(missing))
    if OLD_CONDITION in text:
        raise PatchError("monitor genérico de 300s ainda pode preemptar o render")
    compile(text, str(YOUTUBE), "exec")


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
        print(f"ERRO RUNTIME RENDER MONITOR COMPAT: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
