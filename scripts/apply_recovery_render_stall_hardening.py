from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "app/services/production_manifest.py"
DIAGNOSTICS = ROOT / "app/services/production_manifest_diagnostics.py"
YOUTUBE = ROOT / "app/routers/youtube.py"
VIDEO = ROOT / "app/services/video_generator.py"

MARKER_STAGE = "CODEXIA_RECOVERY_STAGE_MONOTONIC_V2"
MARKER_POOL = "CODEXIA_TASK_OWNED_IMAGE_POOL_RECOVERY_V2"
MARKER_AUDIO = "CODEXIA_AUDIO_CHECKPOINT_TRUST_V2"
MARKER_PAUSED = "CODEXIA_RECOVERY_CONFIRMATION_PAUSED_V2"
MARKER_POSTMIX = "CODEXIA_FFMPEG_BACKGROUND_POSTMIX_V1"
MARKER_WATCHDOG = "CODEXIA_RENDER_STALL_WATCHDOG_V1"


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


STAGE_OLD = '''def _stage_from_snapshot(snapshot: Dict[str, Any]) -> str:\n    message = str(snapshot.get("message") or "").strip().lower()\n    progress = int(snapshot.get("progress") or 0)\n    if "revis" in message or "editorial" in message:\n        return "stage_1_editorial"\n    if "narra" in message or "tts" in message or "voice" in message or "áudio" in message or "audio" in message:\n        return "stage_2_voice"\n    if "imagem" in message or "visual" in message:\n        return "stage_3_images"\n    if "compos" in message or "montag" in message:\n        return "stage_5_compose"\n    if "render" in message or progress >= 85:\n        return "stage_6_render"\n    if progress >= 70:\n        return "stage_5_compose"\n    if progress >= 35:\n        return "stage_3_images"\n    if progress >= 18:\n        return "stage_2_voice"\n    if progress >= 8:\n        return "stage_1_editorial"\n    return "starting"'''

STAGE_NEW = '''def _stage_from_snapshot(snapshot: Dict[str, Any]) -> str:\n    # CODEXIA_RECOVERY_STAGE_MONOTONIC_V2\n    # Progresso comprovado sempre vence palavras presentes em mensagens de\n    # pausa/recuperação. Uma tarefa que chegou a 89% nunca volta para stage_2\n    # apenas porque a mensagem menciona "narração".\n    message = str(snapshot.get("message") or "").strip().lower()\n    progress = int(snapshot.get("progress") or 0)\n    if progress >= 85 or "render" in message:\n        return "stage_6_render"\n    if progress >= 70 or "compos" in message or "montag" in message:\n        return "stage_5_compose"\n    if progress >= 35 or "imagem" in message or "visual" in message:\n        return "stage_3_images"\n    if progress >= 18 or "narra" in message or "tts" in message or "voice" in message or "áudio" in message or "audio" in message:\n        return "stage_2_voice"\n    if progress >= 8 or "revis" in message or "editorial" in message:\n        return "stage_1_editorial"\n    return "starting"'''


POOL_OLD = '''        elif len(remaining) > needed:\n            slots: Dict[int, List[Dict[str, Any]]] = {}\n            for entry in remaining:\n                scene_number = int(entry.get("_scene_number") or 0)\n                if 1 <= scene_number <= target:\n                    slots.setdefault(scene_number, []).append(entry)\n            used_scene_numbers = {\n                int(item.get("_scene_number") or 0)\n                for item in candidates\n                if str(item.get("_content_key") or "") in chosen_keys\n            }\n            slot_candidates = [\n                items[0]\n                for scene_number, items in sorted(slots.items())\n                if scene_number not in used_scene_numbers and len(items) == 1\n            ]\n            if len(slot_candidates) == needed:\n                for entry in slot_candidates:\n                    if _choose(entry):\n                        fallback_count += 1\n            else:\n                ambiguous_fallback = True'''

POOL_NEW = '''        elif len(remaining) > needed:\n            # CODEXIA_TASK_OWNED_IMAGE_POOL_RECOVERY_V2\n            # Se não existem referências antigas, os candidatos abaixo não são\n            # arquivos globais aleatórios: todos já foram copiados para o\n            # manifesto durável DESTA task_id. Portanto podemos formar um pool\n            # determinístico local, sem chamar provedor e sem declarar 0/N.\n            if not raw_references:\n                for entry in remaining[:needed]:\n                    if _choose(entry):\n                        fallback_count += 1\n            else:\n                slots: Dict[int, List[Dict[str, Any]]] = {}\n                for entry in remaining:\n                    scene_number = int(entry.get("_scene_number") or 0)\n                    if 1 <= scene_number <= target:\n                        slots.setdefault(scene_number, []).append(entry)\n                used_scene_numbers = {\n                    int(item.get("_scene_number") or 0)\n                    for item in candidates\n                    if str(item.get("_content_key") or "") in chosen_keys\n                }\n                slot_candidates = [\n                    items[0]\n                    for scene_number, items in sorted(slots.items())\n                    if scene_number not in used_scene_numbers and len(items) == 1\n                ]\n                if len(slot_candidates) == needed:\n                    for entry in slot_candidates:\n                        if _choose(entry):\n                            fallback_count += 1\n                else:\n                    ambiguous_fallback = True'''

POOL_STRATEGY_OLD = '''        "strategy": "manifest_original_path_then_unique_scene_slot_v1",'''
POOL_STRATEGY_NEW = '''        "strategy": (\n            "task_manifest_verified_pool_v2"\n            if fallback_count and not raw_references\n            else "manifest_original_path_then_unique_scene_slot_v1"\n        ),'''


AUDIO_HELPERS = '''# CODEXIA_AUDIO_CHECKPOINT_TRUST_V2\ndef _task_audio_checkpoint(task_id: Any) -> Dict[str, Any]:\n    try:\n        from app.services.task_manager import get_task\n        task = get_task(str(task_id or "")) or {}\n        result = task.get("result") if isinstance(task.get("result"), dict) else {}\n        for candidate in (\n            result.get("audio_checkpoint"),\n            result.get("audio_generation"),\n            (result.get("render_report") or {}).get("audio_generation") if isinstance(result.get("render_report"), dict) else None,\n        ):\n            if isinstance(candidate, dict) and candidate:\n                return dict(candidate)\n    except Exception:\n        pass\n    return {}\n\n\ndef _raw_file_sha256(path: str) -> str:\n    try:\n        h = hashlib.sha256()\n        with open(path, "rb") as fh:\n            for chunk in iter(lambda: fh.read(1024 * 1024), b""):\n                h.update(chunk)\n        return h.hexdigest()\n    except Exception:\n        return ""\n\n\ndef _audio_matches_checkpoint(task_id: str, item: Dict[str, Any], checkpoint: Dict[str, Any]) -> bool:\n    if not checkpoint:\n        return False\n    cp_task = str(checkpoint.get("task_id") or "").strip()\n    if cp_task and cp_task != str(task_id or "").strip():\n        return False\n    validation = str(checkpoint.get("validation_status") or checkpoint.get("checkpoint_status") or "").strip().lower()\n    if validation in {"failed", "invalid", "rejected", "error"}:\n        return False\n    item_paths = {\n        os.path.abspath(str(item.get(key) or ""))\n        for key in ("original_path", "durable_path", "resolved_path")\n        if str(item.get(key) or "").strip()\n    }\n    cp_paths = {\n        os.path.abspath(str(checkpoint.get(key) or ""))\n        for key in ("output_path", "final_audio_path", "audio_path")\n        if str(checkpoint.get(key) or "").strip()\n    }\n    if item_paths.intersection(cp_paths):\n        return True\n    expected_hash = str(checkpoint.get("audio_sha256") or "").strip().lower()\n    resolved = str(item.get("resolved_path") or item.get("durable_path") or "").strip()\n    if expected_hash and resolved and os.path.isfile(resolved):\n        return _raw_file_sha256(resolved).lower() == expected_hash\n    return False\n\n\n'''

AUDIO_OLD = '''    audio_choice: Optional[Dict[str, Any]] = None\n    audio_duration = 0.0\n    for item in sorted(audio, key=lambda x: float(x.get("mtime_epoch") or 0.0), reverse=True):\n        duration = _probe_duration(str(item.get("resolved_path") or ""))\n        if duration <= 0:\n            continue\n        if target_seconds and not (target_seconds * 0.60 <= duration <= target_seconds * 1.80):\n            continue\n        audio_choice = item\n        audio_duration = duration\n        break\n    audio_found = audio_choice is not None\n    audio_reusable = bool(\n        audio_choice\n        and str(audio_choice.get("source") or "").strip().lower() == "tts_immediate"\n    )'''

AUDIO_NEW = '''    audio_choice: Optional[Dict[str, Any]] = None\n    audio_duration = 0.0\n    audio_reusable = False\n    audio_trust = "missing"\n    checkpoint = _task_audio_checkpoint(task_key)\n    untrusted_choice: Optional[Dict[str, Any]] = None\n    untrusted_duration = 0.0\n    for item in sorted(audio, key=lambda x: float(x.get("mtime_epoch") or 0.0), reverse=True):\n        duration = _probe_duration(str(item.get("resolved_path") or ""))\n        if duration <= 0:\n            continue\n        if target_seconds and not (target_seconds * 0.60 <= duration <= target_seconds * 1.80):\n            continue\n        source_trusted = str(item.get("source") or "").strip().lower() == "tts_immediate"\n        checkpoint_trusted = _audio_matches_checkpoint(task_key, item, checkpoint)\n        if source_trusted or checkpoint_trusted:\n            audio_choice = item\n            audio_duration = duration\n            audio_reusable = True\n            audio_trust = "narration_contract_v1" if source_trusted else "audio_checkpoint_v2"\n            break\n        if untrusted_choice is None:\n            untrusted_choice = item\n            untrusted_duration = duration\n    if audio_choice is None and untrusted_choice is not None:\n        audio_choice = untrusted_choice\n        audio_duration = untrusted_duration\n        audio_trust = "legacy_unverified"\n    audio_found = audio_choice is not None'''

AUDIO_PLAN_OLD = '''        "audio_trust": "narration_contract_v1" if audio_reusable else ("legacy_unverified" if audio_found else "missing"),'''
AUDIO_PLAN_NEW = '''        "audio_trust": audio_trust,'''


DIAG_AUDIO_START = '''def _audio_trust(manifest: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:\n    audio_items = _valid_artifacts(manifest, "audio")\n    audio_path = str(plan.get("audio_path") or "").strip()\n    audio_found = bool(audio_items or audio_path)\n    protected = any(\n        str(item.get("source") or "").strip().lower() == "tts_immediate"\n        for item in audio_items\n    )\n    if not audio_found:\n        return {\n            "found": False,\n            "reusable": False,\n            "trust": "missing",\n            "reason": "Nenhum áudio durável válido foi encontrado no manifesto.",\n        }\n    if protected:\n        return {\n            "found": True,\n            "reusable": bool(plan.get("audio_ok")),\n            "trust": "narration_contract_v1",\n            "reason": "Áudio foi persistido pelo guard de narração após validação pré-TTS.",\n        }\n    return {\n        "found": True,\n        "reusable": False,\n        "trust": "legacy_unverified",\n        "reason": (\n            "Áudio anterior ao guard de narração não possui prova de sanitização. "\n            "Ele pode existir fisicamente, mas não deve ser reutilizado automaticamente."\n        ),\n    }'''

DIAG_AUDIO_NEW = '''def _audio_trust(manifest: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:\n    # O plano é a fonte única de confiança: ele já cruza o artefato físico com\n    # o checkpoint da mesma tarefa e, quando disponível, com o hash do MP3.\n    found = bool(plan.get("audio_found"))\n    reusable = bool(plan.get("audio_reusable"))\n    trust = str(plan.get("audio_trust") or ("missing" if not found else "legacy_unverified"))\n    if not found:\n        reason = "Nenhum áudio durável compatível foi encontrado para esta tarefa."\n    elif reusable and trust == "audio_checkpoint_v2":\n        reason = "Áudio físico confere com o checkpoint persistido da própria tarefa."\n    elif reusable:\n        reason = "Áudio persistido pelo contrato de narração está apto para reutilização."\n    else:\n        reason = "Áudio físico existe, mas não há prova suficiente para reutilização automática."\n    return {"found": found, "reusable": reusable, "trust": trust, "reason": reason}'''

DIAG_CHECKPOINT_OLD = '''    max_checkpoint = _max_checkpoint(plan, audio_info)'''
DIAG_CHECKPOINT_NEW = '''    max_checkpoint = _max_checkpoint(plan, audio_info)\n    # Uma produção que comprovadamente chegou a 85%+ com roteiro, imagens e\n    # áudio reutilizáveis estava em render. Não rebaixe o diagnóstico por uma\n    # mensagem posterior de pausa/recuperação.\n    try:\n        manifest_progress = int(manifest.get("progress") or 0)\n    except Exception:\n        manifest_progress = 0\n    if (\n        manifest_progress >= 85\n        and bool(plan.get("script_ok"))\n        and bool(plan.get("images_ok"))\n        and bool(audio_info.get("reusable"))\n    ):\n        max_checkpoint = "stage_6_render"'''


YOUTUBE_CONFIRM_OLD = '''                    recovery_message = recovery_confirmation_message(confirmed_plan if isinstance(confirmed_plan, dict) else manifest_plan)\n                    update_task(task_id, message=recovery_message)\n                    raise HTTPException(status_code=409, detail=recovery_message)'''
YOUTUBE_CONFIRM_NEW = '''                    recovery_message = recovery_confirmation_message(confirmed_plan if isinstance(confirmed_plan, dict) else manifest_plan)\n                    # CODEXIA_RECOVERY_CONFIRMATION_PAUSED_V2\n                    # Confirmação de custo não é falha técnica. Mantenha o estado\n                    # pausado e devolva 200 para a UI não pintar a tarefa como failed.\n                    update_task(task_id, status="paused", message=recovery_message)\n                    return {\n                        "task_id": task_id,\n                        "status": "paused",\n                        "paused": True,\n                        "recovery_confirmation_required": True,\n                        "message": recovery_message,\n                        "recovery_plan": confirmed_plan if isinstance(confirmed_plan, dict) else manifest_plan,\n                    }'''


POSTMIX_METHOD = '''    def _codexia_ffmpeg_postmix_background_music(self, output_path, music_path, volume, duration_sec):\n        # CODEXIA_FFMPEG_BACKGROUND_POSTMIX_V1\n        # MoviePy CompositeAudioClip mostrou estagnação real em produção. O\n        # vídeo é codificado com a narração simples; depois o FFmpeg mistura a\n        # trilha em streaming, sem materializar milhões de amostras em Python.\n        import shutil as _shutil\n        import subprocess as _subprocess\n        ffmpeg = _shutil.which("ffmpeg")\n        if not ffmpeg or not output_path or not music_path:\n            return {"mixed": False, "reason": "ffmpeg_or_path_missing"}\n        if not os.path.isfile(output_path) or not os.path.isfile(music_path):\n            return {"mixed": False, "reason": "input_missing"}\n        try:\n            dur = max(0.1, float(duration_sec or 0.0))\n            vol = max(0.0, min(0.2, float(volume or 0.0)))\n        except Exception:\n            dur, vol = 0.1, 0.035\n        fade_dur = min(1.4, max(0.8, dur * 0.01))\n        fade_start = max(0.0, dur - fade_dur)\n        temp_path = str(output_path) + ".bgmix.tmp.mp4"\n        audio_filter = (\n            f"[1:a]volume={vol:.6f},afade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}[bg];"\n            "[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2[a]"\n        )\n        cmd = [\n            ffmpeg, "-y", "-loglevel", "error",\n            "-i", str(output_path), "-stream_loop", "-1", "-i", str(music_path),\n            "-filter_complex", audio_filter,\n            "-map", "0:v:0", "-map", "[a]",\n            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",\n            "-t", f"{dur:.3f}", "-movflags", "+faststart", temp_path,\n        ]\n        try:\n            timeout = int((os.getenv("VIDEO_BG_POSTMIX_TIMEOUT_SECONDS") or "600").strip() or "600")\n        except Exception:\n            timeout = 600\n        timeout = max(60, min(1800, timeout))\n        try:\n            _subprocess.run(cmd, check=True, timeout=timeout)\n            if not os.path.isfile(temp_path) or os.path.getsize(temp_path) < 4096:\n                raise RuntimeError("FFmpeg postmix não produziu arquivo válido")\n            os.replace(temp_path, output_path)\n            return {"mixed": True, "method": "ffmpeg_streaming_amix"}\n        except Exception as exc:\n            try:\n                if os.path.exists(temp_path):\n                    os.remove(temp_path)\n            except Exception:\n                pass\n            return {"mixed": False, "reason": str(exc)[:500]}\n\n'''

COMPOSITE_OLD = '''                    if has_voice_audio:\n                        final_audio = CompositeAudioClip([bg_music, final_clip.audio])\n                    else:\n                        final_audio = bg_music'''
COMPOSITE_NEW = '''                    if has_voice_audio:\n                        # CODEXIA_FFMPEG_BACKGROUND_POSTMIX_V1\n                        # Evita CompositeAudioClip de música+voz no Python. A\n                        # trilha será adicionada ao MP4 já codificado via FFmpeg.\n                        final_audio = final_clip.audio\n                        render_report["visual_plan"]["background_music_deferred_to_ffmpeg"] = True\n                    else:\n                        final_audio = bg_music'''

POST_WRITE_ANCHOR = '''            try:\n                self._dbg_event("H1", "write_videofile done (narrated)", {'''
POST_WRITE_INSERT = '''            if has_voice_audio and music_path and bool(render_report.get("visual_plan", {}).get("background_music_deferred_to_ffmpeg")):\n                _mix_result = self._codexia_ffmpeg_postmix_background_music(\n                    output_path, music_path, bg_volume, final_dur\n                )\n                render_report["visual_plan"]["background_music_postmix"] = _mix_result\n                if not bool((_mix_result or {}).get("mixed")):\n                    print(f"Aviso: pós-mixagem FFmpeg falhou; mantendo vídeo com narração: {_mix_result}")\n\n'''

HB_INIT_OLD = '''                    _last_size = -1\n                    _start_ts = None\n                    _tick = 0'''
HB_INIT_NEW = '''                    # CODEXIA_RENDER_STALL_WATCHDOG_V1\n                    _last_size = -1\n                    _start_ts = None\n                    _tick = 0\n                    _last_signature = None\n                    _last_growth_ts = None\n                    _seen_render_io = False\n                    try:\n                        _stall_seconds = int((os.getenv("VIDEO_RENDER_STALL_SECONDS") or "240").strip() or "240")\n                    except Exception:\n                        _stall_seconds = 240\n                    _stall_seconds = max(120, min(1200, _stall_seconds))'''

HB_SIZE_OLD = '''                            _mb = round(_size / (1024 * 1024), 1) if _size else 0\n                            if progress_callback:'''
HB_SIZE_NEW = '''                            # A fase de áudio do MoviePy escreve TEMP_MPY antes do MP4 final.\n                            # Monitorar os dois evita falso travamento, mas o heartbeat só\n                            # é considerado saudável quando há crescimento real de bytes.\n                            try:\n                                import glob as _glob\n                                import time as _growth_time\n                                _stem = os.path.splitext(os.path.basename(output_path))[0]\n                                _patterns = [\n                                    os.path.join(os.getcwd(), f"{_stem}TEMP_MPY_wvf_snd.*"),\n                                    os.path.join(os.path.dirname(output_path) or ".", f"{_stem}TEMP_MPY_wvf_snd.*"),\n                                ]\n                                _temp_paths = []\n                                for _pattern in _patterns:\n                                    _temp_paths.extend(_glob.glob(_pattern))\n                                _temp_size = sum(\n                                    os.path.getsize(_p) for _p in set(_temp_paths)\n                                    if os.path.isfile(_p)\n                                )\n                                _signature = (int(_size), int(_temp_size))\n                                _now_growth = _growth_time.time()\n                                if _last_growth_ts is None:\n                                    _last_growth_ts = _now_growth\n                                if _signature != _last_signature and (_size > 0 or _temp_size > 0):\n                                    _last_growth_ts = _now_growth\n                                    _seen_render_io = True\n                                _last_signature = _signature\n                                _stall_age = int(max(0, _now_growth - (_last_growth_ts or _now_growth)))\n                            except Exception:\n                                _temp_size = 0\n                                _stall_age = 0\n\n                            if _seen_render_io and _stall_age >= _stall_seconds:\n                                _task_id = str(\n                                    getattr(self, "_codexia_task_id", "")\n                                    or getattr(getattr(self, "ai_service", None), "ai_task_id", "")\n                                    or ""\n                                ).strip()\n                                try:\n                                    if _task_id:\n                                        from app.services.task_manager import get_task as _get_task, mark_task_paused as _mark_paused, update_task as _update_task, merge_task_result as _merge_result\n                                        _task = _get_task(_task_id) or {}\n                                        _status = str(_task.get("status") or "").strip().lower()\n                                        _telemetry = {\n                                            "detected": True,\n                                            "stall_seconds": _stall_age,\n                                            "output_bytes": int(_size),\n                                            "temp_audio_bytes": int(_temp_size),\n                                            "stage": "audio_temp" if _temp_size and not _size else "video_encode",\n                                        }\n                                        _merge_result(_task_id, {"render_stall_guard": _telemetry})\n                                        if _status == "pause_requested":\n                                            _mark_paused(_task_id, message="Produção pausada automaticamente após detectar render sem crescimento real.")\n                                        else:\n                                            _update_task(\n                                                _task_id,\n                                                status="failed",\n                                                message=(\n                                                    f"Render interrompido automaticamente após {_stall_age}s sem crescimento real; "\n                                                    "roteiro, áudio e imagens foram preservados para recuperação local."\n                                                ),\n                                            )\n                                except Exception as _stall_state_err:\n                                    print(f"Aviso: falha ao registrar render estagnado: {_stall_state_err}")\n                                if str(os.getenv("VIDEO_RENDER_STALL_KILL_EXECUTOR") or "true").strip().lower() not in {"0", "false", "no", "off"}:\n                                    try:\n                                        import signal as _signal\n                                        print(f"[Codexia Render] estagnação detectada ({_stall_age}s); encerrando somente executor PID={os.getpid()}")\n                                        os.kill(os.getpid(), _signal.SIGTERM)\n                                    except Exception as _kill_err:\n                                        print(f"Aviso: não foi possível encerrar executor estagnado: {_kill_err}")\n                                return\n\n                            _mb = round(_size / (1024 * 1024), 1) if _size else 0\n                            _temp_mb = round(_temp_size / (1024 * 1024), 1) if _temp_size else 0\n                            if progress_callback:'''

HB_MSG_OLD = '''                                _msg = (f"6/8 Renderizando arquivo final... "\n                                        f"({_elapsed_str} decorridos; arquivo: ~{_mb} MB)")'''
HB_MSG_NEW = '''                                if _size > 0:\n                                    _stage_label = "6B/8 Codificando vídeo final"\n                                elif _temp_size > 0:\n                                    _stage_label = "6A/8 Preparando áudio final"\n                                else:\n                                    _stage_label = "6A/8 Preparando render final"\n                                _msg = (\n                                    f"{_stage_label}... ({_elapsed_str} decorridos; "\n                                    f"vídeo: ~{_mb} MB; áudio temp: ~{_temp_mb} MB; "\n                                    f"sem crescimento: {_stall_age}s)"\n                                )'''


def apply() -> None:
    service = SERVICE.read_text(encoding="utf-8")
    service = _replace_once(service, STAGE_OLD, STAGE_NEW, "monotonic recovery stage")
    service = _replace_once(service, POOL_OLD, POOL_NEW, "task-owned image pool")
    service = _replace_once(service, POOL_STRATEGY_OLD, POOL_STRATEGY_NEW, "image pool strategy")
    service = _insert_before_once(service, "def build_recovery_plan(task_id: Any, payload_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:\n", AUDIO_HELPERS, "audio checkpoint helpers")
    service = _replace_once(service, AUDIO_OLD, AUDIO_NEW, "audio checkpoint selection")
    service = _replace_once(service, AUDIO_PLAN_OLD, AUDIO_PLAN_NEW, "audio trust label")
    SERVICE.write_text(service, encoding="utf-8")

    diagnostics = DIAGNOSTICS.read_text(encoding="utf-8")
    diagnostics = _replace_once(diagnostics, DIAG_AUDIO_START, DIAG_AUDIO_NEW, "diagnostic audio trust")
    diagnostics = _replace_once(diagnostics, DIAG_CHECKPOINT_OLD, DIAG_CHECKPOINT_NEW, "diagnostic checkpoint")
    DIAGNOSTICS.write_text(diagnostics, encoding="utf-8")

    youtube = YOUTUBE.read_text(encoding="utf-8")
    youtube = _replace_once(youtube, YOUTUBE_CONFIRM_OLD, YOUTUBE_CONFIRM_NEW, "paid confirmation remains paused")
    YOUTUBE.write_text(youtube, encoding="utf-8")

    video = VIDEO.read_text(encoding="utf-8")
    video = _insert_before_once(video, "    def create_video_from_plan(self, plan, cover_image_path=None, aspect_ratio=\"9:16\", progress_callback=None, voice_style=None, voice_gender=None, music_file_path=None):\n", POSTMIX_METHOD, "ffmpeg postmix method")
    video = _replace_once(video, COMPOSITE_OLD, COMPOSITE_NEW, "defer background music")
    video = _insert_before_once(video, POST_WRITE_ANCHOR, POST_WRITE_INSERT, "postmix after video encode")
    video = _replace_once(video, HB_INIT_OLD, HB_INIT_NEW, "render watchdog init")
    video = _replace_once(video, HB_SIZE_OLD, HB_SIZE_NEW, "render watchdog io")
    video = _replace_once(video, HB_MSG_OLD, HB_MSG_NEW, "render watchdog message")
    VIDEO.write_text(video, encoding="utf-8")


def check() -> None:
    service = SERVICE.read_text(encoding="utf-8")
    diagnostics = DIAGNOSTICS.read_text(encoding="utf-8")
    youtube = YOUTUBE.read_text(encoding="utf-8")
    video = VIDEO.read_text(encoding="utf-8")
    required = {
        "service stage": MARKER_STAGE in service,
        "service pool": MARKER_POOL in service,
        "service audio": MARKER_AUDIO in service,
        "diagnostic checkpoint": 'max_checkpoint = "stage_6_render"' in diagnostics,
        "youtube paused confirmation": MARKER_PAUSED in youtube and '"recovery_confirmation_required": True' in youtube,
        "ffmpeg postmix": MARKER_POSTMIX in video and "ffmpeg_streaming_amix" in video,
        "render watchdog": MARKER_WATCHDOG in video and "VIDEO_RENDER_STALL_SECONDS" in video,
    }
    missing = [name for name, ok in required.items() if not ok]
    if missing:
        raise PatchError("hardening incompleto: " + ", ".join(missing))
    if 'CompositeAudioClip([bg_music, final_clip.audio])' in video:
        raise PatchError("mixagem Python de música+voz ainda ativa")


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
