"""Plano visual do piloto, derivado do áudio medido e da identidade privada.

Não chama provedores, não gera vídeo e não publica. Cada fala aponta para
uma tomada do personagem correto, protegendo contra trocas de identidade.
"""
from __future__ import annotations

from app.services.entre_nos_dois import HOSTS, PROGRAM_DISCLOSURE
from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest


class VisualPlanError(ValueError):
    pass


def build_visual_pilot_plan(*, timeline: dict, reference_keys: dict[str, str]) -> dict:
    """Converte a linha do tempo validada em um plano revisável de cenas."""
    canonical = prepare_speech_manifest()
    if timeline.get("program") != canonical["program"]:
        raise VisualPlanError("Programa divergente.")
    project_id = timeline.get("project_id")
    if type(project_id) is not int or project_id <= 0:
        raise VisualPlanError("Projeto inválido.")
    if timeline.get("status") != "awaiting_audio_and_visual_review":
        raise VisualPlanError("Áudio ainda não preparado para revisão.")
    if timeline.get("auto_publish") is not False or timeline.get("video_generated") is not False:
        raise VisualPlanError("Não é permitido planejamento com publicação ou vídeo pré-aprovado.")
    expected = {host.name: host.provider_voice_id for host in HOSTS}
    if set(reference_keys) != set(expected):
        raise VisualPlanError("Referências visuais incompletas ou com personagens adicionais.")
    for name in expected:
        key = reference_keys[name]
        if not isinstance(key, str) or not key.startswith(f"projects/{project_id}/") or ".." in key.split("/"):
            raise VisualPlanError("Referência visual fora do projeto.")
    if reference_keys["Gabriel"] == reference_keys["Helena"]:
        raise VisualPlanError("Cada personagem exige referência própria.")

    entries = timeline.get("segments")
    planned = canonical["segments"]
    if not isinstance(entries, list) or len(entries) != len(planned):
        raise VisualPlanError("Trechos da linha do tempo divergentes.")
    scenes = []
    last_end = 0.0
    for index, (entry, expected_turn) in enumerate(zip(entries, planned)):
        if entry.get("index") != expected_turn["index"] or entry.get("speaker") != expected_turn["speaker"]:
            raise VisualPlanError("Ordem ou personagem divergente.")
        if entry.get("voice_id") != expected_turn["voice_id"] or entry.get("text") != expected_turn["text"]:
            raise VisualPlanError("Voz ou roteiro divergente.")
        if entry.get("requires_lip_sync") is not True:
            raise VisualPlanError("Sincronização labial obrigatória em cada fala.")
        start, end = entry.get("start_seconds"), entry.get("end_seconds")
        if type(start) not in (int, float) or type(end) not in (int, float):
            raise VisualPlanError("Marcação de tempo inválida.")
        if not (0 <= start < end <= 300) or start + 0.002 < last_end:
            raise VisualPlanError("Tempos inválidos ou sobrepostos.")
        speaker = entry["speaker"]
        scenes.append({
            "scene_index": index + 1,
            "active_speaker": speaker,
            "voice_id": expected[speaker],
            "private_reference_key": reference_keys[speaker],
            "start_seconds": start,
            "end_seconds": end,
            "audio_path": entry["audio_path"],
            "shot": "medium_single",
            "camera_focus": speaker,
            "other_host": "Helena" if speaker == "Gabriel" else "Gabriel",
            "other_host_behavior": "quiet_listening_no_lip_movement",
            "lip_sync_required": True,
            "caption": entry["text"],
            "caption_max_lines": 2,
            "asset_generation_status": "not_started",
        })
        last_end = end
    if abs(float(timeline["measured_total_seconds"]) - last_end) > 3.1:
        raise VisualPlanError("Duração total incompatível com a última fala.")
    return {
        "program": canonical["program"],
        "project_id": project_id,
        "planned_duration_seconds": canonical["target_seconds"],
        "measured_duration_seconds": timeline["measured_total_seconds"],
        "disclosure": PROGRAM_DISCLOSURE,
        "scenes": scenes,
        "visual_identity_locked": True,
        "human_approval_required": True,
        "lip_sync_generated": False,
        "video_generated": False,
        "auto_publish": False,
        "status": "awaiting_lip_sync_provider_integration",
    }
