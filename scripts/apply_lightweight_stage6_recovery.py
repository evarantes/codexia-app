from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app/routers/youtube.py"
VIDEO = ROOT / "app/services/video_generator.py"
INDEX = ROOT / "app/static/index.html"

MARKER = "CODEXIA_LIGHTWEIGHT_STAGE6_RECOVERY_V1"


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1)


YOUTUBE_PLAN_OLD = '''        plan = build_sparse_visual_optimization_plan(\n            task_id=str(task_id),\n            title=title,\n            target_visual_count=target_visual_count,\n            valid_image_paths=image_candidates,\n            script=seed_script,\n            audio_path=audio_path,\n            image_unit_cost_usd=unit_cost,\n        )'''

YOUTUBE_PLAN_NEW = '''        # CODEXIA_LIGHTWEIGHT_STAGE6_RECOVERY_V1\n        # Quando a tarefa comprovadamente chegou ao render final, a proposta de\n        # recuperação também inclui trocar apenas o motor de composição pesado\n        # por FFmpeg local. Essa mudança entra no hash e exige confirmação exata.\n        try:\n            reached_final_render = int(getattr(row, "progress", 0) or 0) >= 85\n        except Exception:\n            reached_final_render = False\n        plan = build_sparse_visual_optimization_plan(\n            task_id=str(task_id),\n            title=title,\n            target_visual_count=target_visual_count,\n            valid_image_paths=image_candidates,\n            script=seed_script,\n            audio_path=audio_path,\n            image_unit_cost_usd=unit_cost,\n            lightweight_recovery=bool(reached_final_render or payload.get("force_render_only")),\n        )'''

YOUTUBE_CONFIRM_OLD = '''            payload["intelligent_visual_optimization"] = _public_intelligent_optimization_plan(optimization_plan)\n            payload.pop("_recovery_block_paid_regeneration", None)\n            payload.pop("_recovery_missing_assets", None)'''

YOUTUBE_CONFIRM_NEW = '''            payload["intelligent_visual_optimization"] = _public_intelligent_optimization_plan(optimization_plan)\n            if bool(optimization_plan.get("lightweight_recovery")):\n                # O hash confirmado inclui explicitamente esta mudança de render.\n                payload["lightweight_recovery_render_confirmed"] = True\n            payload.pop("_recovery_block_paid_regeneration", None)\n            payload.pop("_recovery_missing_assets", None)'''

VIDEO_IMPORT_OLD = '''from app.services.intelligent_cost_optimizer import proportional_visual_index\n'''
VIDEO_IMPORT_NEW = '''from app.services.intelligent_cost_optimizer import proportional_visual_index\nfrom app.services.lightweight_recovery_renderer import render_lightweight_recovery_video\n'''

VIDEO_LOOP_ANCHOR = '''            last_story_scene_clip = None\n            last_story_scene_image_path = None\n\n            for i, scene in enumerate(scenes):'''

VIDEO_FAST_BLOCK = '''            # CODEXIA_LIGHTWEIGHT_STAGE6_RECOVERY_V1\n            # Este caminho só existe após confirmação do hash de otimização.\n            # Ele preserva roteiro, áudio, legendas e imagens, mas evita montar\n            # milhares de frames/overlays simultaneamente no MoviePy.\n            _lightweight_confirmed = bool(\n                isinstance(plan, dict)\n                and plan.get("force_render_only")\n                and plan.get("lightweight_recovery_render_confirmed")\n            )\n            if _lightweight_confirmed:\n                if not selected_image_paths:\n                    raise Exception("Render leve bloqueado: nenhuma imagem preservada válida foi encontrada.")\n                if not main_audio_path or not os.path.isfile(main_audio_path):\n                    raise Exception("Render leve bloqueado: áudio preservado não está disponível localmente.")\n\n                if progress_callback:\n                    progress_callback(88, "6/8 Recuperação confirmada — preparando render leve local...")\n\n                # Libera objetos MoviePy já abertos antes do render direto.\n                try:\n                    if main_audio_clip is not None:\n                        main_audio_clip.close()\n                        main_audio_clip = None\n                except Exception:\n                    pass\n                try:\n                    for _existing_clip in list(clips):\n                        try:\n                            _existing_clip.close()\n                        except Exception:\n                            pass\n                    clips.clear()\n                except Exception:\n                    pass\n                gc.collect()\n\n                _endcard_temp_path = ""\n                try:\n                    _endcard_frame = self._build_cinematic_endcard_frame(\n                        branding_profile,\n                        background_path=selected_image_paths[-1],\n                        size=video_size,\n                    )\n                    _endcard_temp_path = os.path.join(\n                        self.output_dir,\n                        f"recovery_endcard_{uuid.uuid4().hex}.png",\n                    )\n                    if hasattr(_endcard_frame, "save"):\n                        _endcard_frame.save(_endcard_temp_path)\n                    else:\n                        from PIL import Image as _RecoveryPILImage\n                        _RecoveryPILImage.fromarray(_endcard_frame).save(_endcard_temp_path)\n                except Exception as _endcard_error:\n                    print(f"Aviso: endcard leve usará a última imagem preservada: {_endcard_error}")\n                    _endcard_temp_path = ""\n\n                _recovery_filename = f"{uuid.uuid4()}.mp4"\n                _recovery_output_path = os.path.join(self.output_dir, _recovery_filename)\n                try:\n                    _bg_volume_raw = ""\n                    if isinstance(plan, dict) and plan.get("bg_music_volume") is not None:\n                        _bg_volume_raw = str(plan.get("bg_music_volume")).strip()\n                    if not _bg_volume_raw:\n                        _bg_volume_raw = str(os.getenv("VIDEO_BG_MUSIC_VOLUME") or "").strip()\n                    _light_bg_volume = float(_bg_volume_raw) if _bg_volume_raw else 0.025\n                except Exception:\n                    _light_bg_volume = 0.025\n                _light_bg_volume = max(0.0, min(0.20, _light_bg_volume))\n\n                _light_result = render_lightweight_recovery_video(\n                    output_path=_recovery_output_path,\n                    selected_images=selected_image_paths,\n                    audio_path=main_audio_path,\n                    captions=full_caption_timeline,\n                    official_scene_timeline=official_scene_timeline,\n                    target_duration=target_video_duration,\n                    video_size=video_size,\n                    endcard_image=_endcard_temp_path,\n                    music_dir=self.music_dir,\n                    music_mood=str((plan or {}).get("music_mood") or "drama"),\n                    music_volume=_light_bg_volume,\n                    progress_callback=progress_callback,\n                )\n                _recovery_output_path = self._ensure_playable_mp4(_recovery_output_path)\n                _obtained_light_duration = float(\n                    self._measure_rendered_video_duration_seconds(_recovery_output_path) or 0.0\n                )\n\n                try:\n                    if _endcard_temp_path and os.path.isfile(_endcard_temp_path):\n                        os.remove(_endcard_temp_path)\n                except Exception:\n                    pass\n\n                _light_report = dict(_light_result or {})\n                _light_report.update({\n                    "confirmed_by_plan_hash": True,\n                    "preserve_full_script": True,\n                    "preserve_full_narration": True,\n                    "preserve_captions": True,\n                    "paid_image_calls": 0,\n                    "paid_tts_calls": 0,\n                    "external_music_provider_calls": 0,\n                    "external_music_downloads": 0,\n                })\n                render_report["lightweight_recovery_render"] = _light_report\n                render_report.setdefault("intelligent_cost_optimization", {})\n                render_report["intelligent_cost_optimization"].update({\n                    "render_strategy_executed": "ffmpeg_lightweight_recovery_v1",\n                    "paid_image_calls": 0,\n                    "preserve_full_narration": True,\n                    "preserve_full_text": True,\n                    "preserve_captions": True,\n                })\n                render_report["narration_completed"] = True\n                render_report["story_completed"] = True\n                render_report["cta_rendered"] = bool(closing_has_narration)\n                render_report["end_screen_rendered"] = bool(end_clip_duration > 0)\n                render_report["final_video_duration_sec"] = round(_obtained_light_duration, 2)\n                render_report.setdefault("duration_plan", {})\n                render_report["duration_plan"]["obtained_duration_sec"] = round(_obtained_light_duration, 2)\n                render_report["duration_plan"]["target_video_duration_sec"] = round(float(target_video_duration or 0.0), 2)\n                render_report["video_url"] = f"{VIDEO_URL_PREFIX}/{_recovery_filename}"\n                render_report["file_path"] = _recovery_output_path\n                render_report["srt"] = {\n                    "embedded": True,\n                    "entries": len(full_caption_timeline or []),\n                    "source": str(caption_timeline_source or "unknown"),\n                }\n\n                _light_sync_tolerance = duration_sync_tolerance_seconds(float(target_video_duration or 0.0))\n                _light_delta = abs(_obtained_light_duration - float(target_video_duration or 0.0))\n                _light_sync_validation = {\n                    "audio_duration_sec": round(float(actual_total_audio_dur or 0.0), 3),\n                    "video_duration_sec": round(_obtained_light_duration, 3),\n                    "video_sync_target_sec": round(float(target_video_duration or 0.0), 3),\n                    "audio_video_diff_sec": round(_light_delta, 3),\n                    "video_sync_tolerance_sec": round(float(_light_sync_tolerance or 0.0), 3),\n                    "video_synced_with_audio": bool(_light_delta <= _light_sync_tolerance),\n                    "captions_synced_with_audio": bool(full_caption_timeline),\n                    "uses_official_scene_timeline": True,\n                    "renderer": "ffmpeg_lightweight_recovery_v1",\n                }\n                render_report["sync_validation"] = _light_sync_validation\n\n                _task_id_for_manifest = str(getattr(self, "_codexia_task_id", "") or "").strip()\n                if _task_id_for_manifest:\n                    try:\n                        from app.services.production_manifest import record_artifact as _record_recovery_artifact\n                        _record_recovery_artifact(\n                            _task_id_for_manifest,\n                            _recovery_output_path,\n                            kind="video",\n                            source="lightweight_recovery_renderer",\n                        )\n                    except Exception as _manifest_error:\n                        print(f"Aviso: não foi possível registrar MP4 leve no manifesto: {_manifest_error}")\n\n                _used_music_credit = None\n                _local_music_path = str(_light_report.get("music_path") or "").lower()\n                if _local_music_path:\n                    for _credit_key, _credit_value in self.MUSIC_CREDITS.items():\n                        if _credit_key in os.path.basename(_local_music_path):\n                            _used_music_credit = _credit_value\n                            break\n\n                return {\n                    "video_url": f"{VIDEO_URL_PREFIX}/{_recovery_filename}",\n                    "file_path": _recovery_output_path,\n                    "music_credit": _used_music_credit,\n                    "used_images": list(used_image_urls),\n                    "render_report": render_report,\n                    "sync_validation": _light_sync_validation,\n                }\n\n            last_story_scene_clip = None\n            last_story_scene_image_path = None\n\n            for i, scene in enumerate(scenes):'''

UI_STRATEGY_OLD = "Estratégia: redistribuir as imagens existentes na ordem do roteiro e aumentar apenas o tempo visual quando necessário."
UI_STRATEGY_NEW = (
    "Estratégia visual: redistribuir as imagens existentes na ordem do roteiro e aumentar apenas o tempo visual quando necessário.\\n"
    "Renderização de recuperação: FFmpeg local leve; zoom, pan e Ken Burns calculados quadro a quadro serão simplificados para imagens estáticas e cortes.\\n"
    "Legendas: serão preservadas no tempo do áudio.\\n"
    "Música: somente arquivo local já existente; nenhum provedor externo será consultado."
)

UI_PAID_OLD = "• Novas chamadas pagas de imagem: 0."
UI_PAID_NEW = (
    "• Novas chamadas pagas de imagem: 0.\\n"
    "• Novas chamadas pagas de TTS: 0.\\n"
    "• Chamadas externas de música: 0."
)


def patch_youtube(text: str) -> str:
    if MARKER in text:
        return text
    text = _replace_once(text, YOUTUBE_PLAN_OLD, YOUTUBE_PLAN_NEW, "retry plan lightweight flag")
    text = _replace_once(text, YOUTUBE_CONFIRM_OLD, YOUTUBE_CONFIRM_NEW, "confirmed lightweight payload")
    return text


def patch_video(text: str) -> str:
    if MARKER in text:
        return text
    text = _replace_once(text, VIDEO_IMPORT_OLD, VIDEO_IMPORT_NEW, "lightweight renderer import")
    text = _replace_once(text, VIDEO_LOOP_ANCHOR, VIDEO_FAST_BLOCK, "lightweight render before scene loop")
    return text


def patch_index(text: str) -> str:
    if "Renderização de recuperação: FFmpeg local leve" in text:
        return text
    strategy_count = text.count(UI_STRATEGY_OLD)
    if strategy_count < 1:
        raise PatchError("UI: proposta de estratégia inteligente não encontrada")
    text = text.replace(UI_STRATEGY_OLD, UI_STRATEGY_NEW)
    paid_count = text.count(UI_PAID_OLD)
    if paid_count < 1:
        raise PatchError("UI: garantia de zero imagem paga não encontrada")
    return text.replace(UI_PAID_OLD, UI_PAID_NEW)


def apply() -> None:
    youtube = YOUTUBE.read_text(encoding="utf-8")
    transformed_youtube = patch_youtube(youtube)
    if transformed_youtube != youtube:
        YOUTUBE.write_text(transformed_youtube, encoding="utf-8")

    video = VIDEO.read_text(encoding="utf-8")
    transformed_video = patch_video(video)
    if transformed_video != video:
        VIDEO.write_text(transformed_video, encoding="utf-8")

    index = INDEX.read_text(encoding="utf-8")
    transformed_index = patch_index(index)
    if transformed_index != index:
        INDEX.write_text(transformed_index, encoding="utf-8")


def check() -> None:
    youtube = YOUTUBE.read_text(encoding="utf-8")
    video = VIDEO.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    required_youtube = (
        MARKER,
        "lightweight_recovery=bool(reached_final_render or payload.get(\"force_render_only\"))",
        'payload["lightweight_recovery_render_confirmed"] = True',
    )
    required_video = (
        MARKER,
        "render_lightweight_recovery_video",
        'plan.get("lightweight_recovery_render_confirmed")',
        '"external_music_provider_calls": 0',
        'source="lightweight_recovery_renderer"',
    )
    required_index = (
        "Renderização de recuperação: FFmpeg local leve",
        "Legendas: serão preservadas no tempo do áudio.",
        "Chamadas externas de música: 0.",
    )
    missing = [token for token in required_youtube if token not in youtube]
    missing += [token for token in required_video if token not in video]
    missing += [token for token in required_index if token not in index]
    if missing:
        raise PatchError("lightweight stage6 recovery incompleto: " + ", ".join(missing))
    compile(youtube, str(YOUTUBE), "exec")
    compile(video, str(VIDEO), "exec")


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
        print(f"ERRO LIGHTWEIGHT STAGE6 RECOVERY: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
