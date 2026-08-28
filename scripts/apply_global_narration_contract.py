#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AI = ROOT / "app/services/ai_generator.py"
VIDEO = ROOT / "app/services/video_generator.py"
YOUTUBE = ROOT / "app/routers/youtube.py"
GATE_JS = ROOT / "app/static/youtube_narration_gate.js"
NARRATION_GUARD = ROOT / "app/services/narration_contract_guard.py"

MARKER_AI = "CODEXIA_GLOBAL_TTS_PROVIDER_GUARD_V1"
MARKER_VIDEO = "CODEXIA_GLOBAL_VIDEO_TTS_GUARD_V1"
MARKER_SEED = "CODEXIA_APPROVED_NARRATION_FAIL_CLOSED_V1"
MARKER_GATE = "CODEXIA_APPROVED_NARRATION_NETWORK_GUARD_V1"
MARKER_SERIALIZED = "CODEXIA_SERIALIZED_TECHNICAL_PAYLOAD_GUARD_V1"


class PatchError(RuntimeError):
    pass


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1)


def patch_ai(text: str) -> str:
    old = '''    def _assert_tts_text_not_truncated(self, text: str, *, provider: str, max_chars: int) -> str:\n        normalized_text = str(text or "").strip()\n        if not normalized_text:\n            raise ValueError(f"{provider} recebeu texto vazio para TTS.")\n        if len(normalized_text) > int(max_chars):'''
    new = '''    def _assert_tts_text_not_truncated(self, text: str, *, provider: str, max_chars: int) -> str:\n        # CODEXIA_GLOBAL_TTS_PROVIDER_GUARD_V1\n        # Ultima barreira antes de QUALQUER provider premium. Nenhum chamador\n        # consegue enviar JSON/codigo/SSML residual diretamente ao TTS.\n        from app.services.narration_contract_guard import validate_narration_text\n        normalized_text = validate_narration_text(\n            text,\n            label=f"{provider} TTS",\n            require_terminal_sentence=False,\n        )\n        if len(normalized_text) > int(max_chars):'''
    return replace_once(text, old, new, "ai/provider-boundary")


def patch_video(text: str) -> str:
    old = '''        # Limpeza de segurança para evitar leitura de metadados\n        clean_text = self._normalize_tts_text(text)\n        if not clean_text: \n            print("Aviso: Texto ficou vazio após limpeza em generate_audio")\n            return None'''
    new = '''        # CODEXIA_GLOBAL_VIDEO_TTS_GUARD_V1\n        # O renderer possui Edge/gTTS próprios; por isso a validação canônica\n        # acontece ANTES da normalização e novamente no texto final narrável.\n        from app.services.narration_contract_guard import validate_narration_text\n        guarded_text = validate_narration_text(\n            text,\n            label=f"{segment_label} antes do TTS",\n            require_terminal_sentence=False,\n        )\n        clean_text = self._normalize_tts_text(guarded_text)\n        clean_text = validate_narration_text(\n            clean_text,\n            label=f"{segment_label} enviado ao TTS",\n            require_terminal_sentence=False,\n        )\n        if not clean_text:\n            raise RuntimeError("Contrato de narração produziu texto vazio; TTS bloqueado.")'''
    text = replace_once(text, old, new, "video/direct-tts-boundary")

    old_seed = '''            if isinstance(plan, dict):\n                seed_audio_path = str(plan.get("seed_audio_path") or "").strip()\n                seed_audio_text = str(plan.get("seed_narration_text") or "").strip()\n            if seed_audio_path and os.path.exists(seed_audio_path) and os.path.getsize(seed_audio_path) > 1000:\n                seed_audio_used = True'''
    new_seed = '''            approved_narration_required = False\n            if isinstance(plan, dict):\n                seed_audio_path = str(plan.get("seed_audio_path") or "").strip()\n                seed_audio_text = str(plan.get("seed_narration_text") or "").strip()\n                approved_narration_required = bool(plan.get("approved_narration_required"))\n            # CODEXIA_APPROVED_NARRATION_FAIL_CLOSED_V1\n            seed_audio_valid = bool(\n                seed_audio_path\n                and os.path.exists(seed_audio_path)\n                and os.path.getsize(seed_audio_path) > 1000\n            )\n            if approved_narration_required and not seed_audio_valid:\n                raise RuntimeError(\n                    "Narração aprovada era obrigatória, mas o MP3 aprovado não está disponível. "\n                    "Render bloqueado: o Codexia não gerará um novo TTS silenciosamente."\n                )\n            if seed_audio_valid:\n                seed_audio_used = True'''
    text = replace_once(text, old_seed, new_seed, "video/approved-seed-fail-closed")

    old_debug = '''                debug_ctx["tts_provider_used"] = "seed_reuse"\n                debug_ctx["tts_fallback_used"] = False'''
    new_debug = '''                debug_ctx["tts_provider_used"] = "seed_reuse"\n                debug_ctx["tts_fallback_used"] = False\n                debug_ctx["approved_narration_required"] = bool(approved_narration_required)\n                debug_ctx["approved_narration_reused"] = True'''
    text = replace_once(text, old_debug, new_debug, "video/seed-audit")
    return text


def patch_youtube(text: str) -> str:
    old = '''            script["approved_narration_text_sha256"] = approved_text_hash\n            script["narration_source"] = "approved_preview_reuse"'''
    new = '''            script["approved_narration_text_sha256"] = approved_text_hash\n            script["narration_source"] = "approved_preview_reuse"\n            # CODEXIA_APPROVED_NARRATION_FAIL_CLOSED_V1\n            # Esta flag chega ao renderer e transforma qualquer perda do MP3\n            # aprovado em erro explícito, nunca em regeneração de TTS.\n            script["approved_narration_required"] = True'''
    text = replace_once(text, old, new, "youtube/approved-required")

    old_result = '''                    "audio_path": str(approved_path),\n                    "tts_regeneration_allowed": False,'''
    new_result = '''                    "audio_path": str(approved_path),\n                    "tts_regeneration_allowed": False,\n                    "approved_narration_required": True,\n                    "contract_version": 3,'''
    return replace_once(text, old_result, new_result, "youtube/approved-audit")


def patch_gate_js(text: str) -> str:
    old_decl = '''  let preview = null;\n  let approved = null;\n  let audioObjectUrl = '';'''
    new_decl = '''  let preview = null;\n  let approved = null;\n  let audioObjectUrl = '';\n  // CODEXIA_APPROVED_NARRATION_NETWORK_GUARD_V1\n  let approvedLaunchArmed = false;\n  let approvedInjectionCount = 0;'''
    text = replace_once(text, old_decl, new_decl, "gate/network-state")

    old_continue = '''    setStatus(panel, 'Iniciando o vídeo com a narração aprovada e preservada…', 'success');\n    button.click();'''
    new_continue = '''    approvedLaunchArmed = true;\n    approvedInjectionCount = 0;\n    setStatus(panel, 'Iniciando o vídeo com a narração aprovada e preservada…', 'success');\n    button.click();\n    window.setTimeout(() => {\n      if (approvedLaunchArmed && approvedInjectionCount === 0) {\n        approvedLaunchArmed = false;\n        setStatus(panel, 'O pedido de vídeo não recebeu o áudio aprovado. Geração bloqueada; recarregue a página e tente novamente.', 'error');\n      }\n    }, 2500);'''
    text = replace_once(text, old_continue, new_continue, "gate/arm-approved-launch")

    old_fetch = '''      if (method === 'POST' && /\\/youtube\\/generate_video(?:\\?|$)/.test(url) && approved && typeof init.body === 'string') {\n        const payload = JSON.parse(init.body);\n        const text = normalizeText(payload.story_content || payload.script_text || '');\n        if (text && await sha256(text) === approved.text_sha256) {\n          payload.reuse_audio_from = approved.reuse_audio_from;\n          payload.approved_narration_preview_id = approved.preview_id;\n          payload.approved_narration_text_sha256 = approved.text_sha256;\n          init = { ...init, body: JSON.stringify(payload) };\n        } else if (text) {\n          clearApproved();\n        }\n      }'''
    new_fetch = '''      if (method === 'POST' && /\\/youtube\\/generate_video(?:\\?|$)/.test(url) && approvedLaunchArmed) {\n        if (!approved || typeof init.body !== 'string') {\n          approvedLaunchArmed = false;\n          throw new Error('Narração aprovada ausente no pedido de vídeo; geração bloqueada.');\n        }\n        const payload = JSON.parse(init.body);\n        const text = normalizeText(payload.story_content || payload.script_text || '');\n        if (!text || await sha256(text) !== approved.text_sha256) {\n          approvedLaunchArmed = false;\n          clearApproved();\n          throw new Error('O texto do vídeo não corresponde à narração aprovada; geração bloqueada.');\n        }\n        payload.reuse_audio_from = approved.reuse_audio_from;\n        payload.approved_narration_preview_id = approved.preview_id;\n        payload.approved_narration_text_sha256 = approved.text_sha256;\n        payload.approved_narration_required = true;\n        approvedInjectionCount += 1;\n        approvedLaunchArmed = false;\n        init = { ...init, body: JSON.stringify(payload) };\n      }'''
    return replace_once(text, old_fetch, new_fetch, "gate/fail-closed-interceptor")


def patch_narration_guard(text: str) -> str:
    old = '''def structural_issues(text: Any) -> List[str]:\n    raw = str(text or "")\n    issues = [name for name, pattern in _STRUCTURAL_PATTERNS if pattern.search(raw)]\n    if re.search(r"\\\\[nrt]\\s*[\\\"']?[A-Za-z_][A-Za-z0-9_]*[\\\"']?\\s*:", raw):\n        issues.append("escaped_serialized_field")\n    return sorted(set(issues))'''
    new = '''def structural_issues(text: Any) -> List[str]:\n    raw = str(text or "")\n    issues = [name for name, pattern in _STRUCTURAL_PATTERNS if pattern.search(raw)]\n    if re.search(r"\\\\[nrt]\\s*[\\\"']?[A-Za-z_][A-Za-z0-9_]*[\\\"']?\\s*:", raw):\n        issues.append("escaped_serialized_field")\n\n    # CODEXIA_SERIALIZED_TECHNICAL_PAYLOAD_GUARD_V1\n    # Bloqueia dumps YAML/log/key-value mesmo depois da normalização de espaços.\n    # Exigimos dois ou mais campos técnicos para evitar falso positivo em prosa\n    # comum que eventualmente use uma palavra como \"status\" seguida de dois-pontos.\n    technical_field_pattern = re.compile(\n        r"(?i)(?<!\\w)(?:status|progress|output_path|file_path|video_path|audio_path|"\n        r"task_id|job_id|request_id|executor_id|provider|model|pipeline_stage|"\n        r"render_stage|error_code|result_json|payload_json)\\s*[:=]"\n    )\n    if len(technical_field_pattern.findall(raw)) >= 2:\n        issues.append("serialized_technical_payload")\n    return sorted(set(issues))'''
    return replace_once(text, old, new, "guard/serialized-technical-payload")


def apply(write: bool) -> int:
    changed = 0
    for path, patcher in (
        (AI, patch_ai),
        (VIDEO, patch_video),
        (YOUTUBE, patch_youtube),
        (GATE_JS, patch_gate_js),
        (NARRATION_GUARD, patch_narration_guard),
    ):
        source = path.read_text(encoding="utf-8")
        transformed = patcher(source)
        if patcher(transformed) != transformed:
            raise PatchError(f"{path.name}: patch não idempotente")
        if source != transformed:
            changed += 1
            if write:
                path.write_text(transformed, encoding="utf-8")
    print(f"Global narration contract: {changed} arquivo(s) {'aplicados' if write else 'necessários'}")
    return changed


def check() -> None:
    requirements = {
        AI: [MARKER_AI, "validate_narration_text(", "require_terminal_sentence=False"],
        VIDEO: [MARKER_VIDEO, MARKER_SEED, 'debug_ctx["approved_narration_reused"] = True'],
        YOUTUBE: [MARKER_SEED, 'script["approved_narration_required"] = True', '"tts_regeneration_allowed": False'],
        GATE_JS: [MARKER_GATE, "approvedLaunchArmed", "payload.approved_narration_required = true", "geração bloqueada"],
        NARRATION_GUARD: [MARKER_SERIALIZED, "serialized_technical_payload", "technical_field_pattern"],
    }
    for path, needles in requirements.items():
        source = path.read_text(encoding="utf-8")
        missing = [needle for needle in needles if needle not in source]
        if missing:
            raise PatchError(f"{path.name}: contrato global ausente: {missing}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply e/ou --check")
    try:
        if args.apply:
            apply(True)
        if args.check:
            check()
    except PatchError as exc:
        print(f"ERRO GLOBAL NARRATION CONTRACT: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
