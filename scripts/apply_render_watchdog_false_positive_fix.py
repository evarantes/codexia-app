from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT / "app/services/video_generator.py"

MARKER = "CODEXIA_RENDER_STALL_FALSE_POSITIVE_FIX_V1"
START = "                            if _seen_render_io and _stall_age >= _stall_seconds:\n"
END = "                            _mb = round(_size / (1024 * 1024), 1) if _size else 0\n"

OLD_INIT = '''                    try:\n                        _stall_seconds = int((os.getenv("VIDEO_RENDER_STALL_SECONDS") or "240").strip() or "240")\n                    except Exception:\n                        _stall_seconds = 240\n                    _stall_seconds = max(120, min(1200, _stall_seconds))'''

NEW_INIT = '''                    # CODEXIA_RENDER_STALL_FALSE_POSITIVE_FIX_V1\n                    # 240s era agressivo demais para vídeos longos: algumas fases do\n                    # MoviePy/FFmpeg podem trabalhar sem alterar o arquivo observado.\n                    # O primeiro limite agora é apenas diagnóstico; somente o limite\n                    # prolongado pode encerrar o executor.\n                    try:\n                        _stall_default = 600 if is_long_video else 360\n                        _stall_seconds = int((os.getenv("VIDEO_RENDER_STALL_SECONDS") or str(_stall_default)).strip() or str(_stall_default))\n                    except Exception:\n                        _stall_seconds = 600 if is_long_video else 360\n                    _stall_seconds = max(300, min(1800, _stall_seconds))\n                    try:\n                        _hard_default = 2400 if is_long_video else 1200\n                        _hard_stall_seconds = int((os.getenv("VIDEO_RENDER_HARD_STALL_SECONDS") or str(_hard_default)).strip() or str(_hard_default))\n                    except Exception:\n                        _hard_stall_seconds = 2400 if is_long_video else 1200\n                    _hard_stall_seconds = max(_stall_seconds + 300, min(7200, _hard_stall_seconds))\n                    _last_soft_notice_ts = None'''

NEW_BLOCK = '''                            if _seen_render_io and _stall_age >= _stall_seconds:\n                                _task_id = str(\n                                    getattr(self, "_codexia_task_id", "")\n                                    or getattr(getattr(self, "ai_service", None), "ai_task_id", "")\n                                    or ""\n                                ).strip()\n                                _hard_stall = bool(_stall_age >= _hard_stall_seconds)\n                                _pause_requested = False\n                                try:\n                                    if _task_id:\n                                        from app.services.task_manager import get_task as _get_task, mark_task_paused as _mark_paused, update_task as _update_task, merge_task_result as _merge_result\n                                        _task = _get_task(_task_id) or {}\n                                        _status = str(_task.get("status") or "").strip().lower()\n                                        _pause_requested = (_status == "pause_requested")\n                                        _telemetry = {\n                                            "detected": True,\n                                            "suspected_stall": True,\n                                            "stall_seconds": _stall_age,\n                                            "soft_stall_seconds": _stall_seconds,\n                                            "hard_stall_seconds": _hard_stall_seconds,\n                                            "output_bytes": int(_size),\n                                            "temp_audio_bytes": int(_temp_size),\n                                            "stage": "audio_temp" if _temp_size and not _size else "video_encode",\n                                            "action": "pause" if _pause_requested else ("hard_fail" if _hard_stall else "observe"),\n                                        }\n                                        _merge_result(_task_id, {"render_stall_guard": _telemetry})\n                                        if _pause_requested:\n                                            _mark_paused(_task_id, message="Produção pausada com segurança durante o render; ativos preservados.")\n                                        elif _hard_stall:\n                                            _update_task(\n                                                _task_id,\n                                                status="failed",\n                                                message=(\n                                                    f"Render interrompido após {_stall_age}s sem crescimento real (limite prolongado); "\n                                                    "roteiro, áudio e imagens foram preservados para recuperação local."\n                                                ),\n                                            )\n                                        else:\n                                            _notice_due = (\n                                                _last_soft_notice_ts is None\n                                                or (_now_growth - float(_last_soft_notice_ts or 0.0)) >= 120.0\n                                            )\n                                            if _notice_due:\n                                                _update_task(\n                                                    _task_id,\n                                                    message=(\n                                                        f"Render lento: {_stall_age}s sem crescimento visível do arquivo; "\n                                                        f"aguardando até {_hard_stall_seconds}s antes de considerar falha real."\n                                                    ),\n                                                )\n                                                _last_soft_notice_ts = _now_growth\n                                except Exception as _stall_state_err:\n                                    print(f"Aviso: falha ao registrar observação de render: {_stall_state_err}")\n\n                                # Pausa explícita pode encerrar o executor para liberar o\n                                # servidor. Fora disso, só o limite prolongado autoriza kill.\n                                if _pause_requested or _hard_stall:\n                                    if str(os.getenv("VIDEO_RENDER_STALL_KILL_EXECUTOR") or "true").strip().lower() not in {"0", "false", "no", "off"}:\n                                        try:\n                                            import signal as _signal\n                                            _reason = "pausa solicitada" if _pause_requested else f"estagnação prolongada ({_stall_age}s)"\n                                            print(f"[Codexia Render] {_reason}; encerrando executor PID={os.getpid()} com ativos preservados")\n                                            os.kill(os.getpid(), _signal.SIGTERM)\n                                        except Exception as _kill_err:\n                                            print(f"Aviso: não foi possível encerrar executor: {_kill_err}")\n                                    return\n\n'''


def apply() -> None:
    video = VIDEO.read_text(encoding="utf-8")
    if MARKER in video:
        return
    if OLD_INIT not in video:
        raise RuntimeError("watchdog base não encontrado; aplique apply_recovery_render_stall_hardening.py antes")
    video = video.replace(OLD_INIT, NEW_INIT, 1)
    start = video.find(START)
    if start < 0:
        raise RuntimeError("início do bloco de stall não encontrado")
    end = video.find(END, start)
    if end < 0:
        raise RuntimeError("fim do bloco de stall não encontrado")
    video = video[:start] + NEW_BLOCK + video[end:]
    VIDEO.write_text(video, encoding="utf-8")


def check() -> None:
    video = VIDEO.read_text(encoding="utf-8")
    required = [
        MARKER,
        "VIDEO_RENDER_HARD_STALL_SECONDS",
        '"action": "pause" if _pause_requested else ("hard_fail" if _hard_stall else "observe")',
        "aguardando até {_hard_stall_seconds}s antes de considerar falha real",
        "if _pause_requested or _hard_stall:",
    ]
    missing = [item for item in required if item not in video]
    if missing:
        raise RuntimeError("fix de falso positivo incompleto: " + ", ".join(missing))
    if 'os.getenv("VIDEO_RENDER_STALL_SECONDS") or "240"' in video:
        raise RuntimeError("limite agressivo de 240s ainda ativo")
    old_fail = 'if _seen_render_io and _stall_age >= _stall_seconds:' in video and 'status="failed"' in video
    if old_fail and "_hard_stall" not in video:
        raise RuntimeError("falha imediata por soft stall ainda ativa")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.apply:
        apply()
    if args.check:
        check()
    if not args.apply and not args.check:
        parser.error("use --apply e/ou --check")


if __name__ == "__main__":
    main()
