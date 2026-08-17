from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Type


def _enabled(name: str, default: str = "true") -> bool:
    return str(os.getenv(name) or default).strip().lower() in {
        "1", "true", "yes", "sim", "on", "enabled", "enable"
    }


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text.strip()


def _scene_text(scene: Dict[str, Any]) -> str:
    for key in ("text", "narration_text", "narration", "content"):
        value = _clean(scene.get(key))
        if value:
            return value
    return ""


def _scene_key(scene: Dict[str, Any]) -> str:
    for key in ("text", "narration_text", "narration", "content"):
        if key in scene:
            return key
    return "text"


def _word_count(value: Any) -> int:
    return len(re.findall(r"\b\w+\b", str(value or ""), flags=re.UNICODE))


def _safe_json(raw: Any) -> Optional[Dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        return None
    candidates = [text]
    if "```json" in text:
        candidates.append(text.split("```json", 1)[1].split("```", 1)[0].strip())
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _extract_response_text(response: Any) -> str:
    value = getattr(response, "output_text", None)
    if value:
        return str(value)
    chunks: List[str] = []
    try:
        for item in getattr(response, "output", None) or []:
            for content in getattr(item, "content", None) or []:
                text = getattr(content, "text", None)
                if text:
                    chunks.append(str(text))
    except Exception:
        pass
    return "\n".join(chunks)


def _fallback_title(plan: Dict[str, Any], scenes: List[Dict[str, Any]]) -> str:
    for key in ("title", "titulo", "topic", "theme", "subject"):
        value = _clean(plan.get(key))
        if value:
            return value[:90]
    if scenes:
        first = _scene_text(scenes[0])
        if first:
            sentence = re.split(r"(?<=[.!?])\s+", first)[0].strip(" .!?—–-")
            if sentence:
                return sentence[:90]
    return "Mensagem de Reflexão"


def analyze_narrative_plan(plan: Any) -> Dict[str, Any]:
    payload = plan if isinstance(plan, dict) else {}
    scenes = [item for item in (payload.get("scenes") or []) if isinstance(item, dict)]
    texts = [_scene_text(scene) for scene in scenes]
    title = _clean(payload.get("title") or payload.get("titulo"))
    total_words = sum(_word_count(text) for text in texts)
    nonempty = sum(1 for text in texts if text)
    issues: List[str] = []
    if not title:
        issues.append("missing_title")
    if len(scenes) < 3:
        issues.append("too_few_scenes_for_clear_arc")
    if nonempty != len(scenes):
        issues.append("empty_scene_text")
    if total_words < 45:
        issues.append("narration_too_short")
    if len(texts) >= 3:
        thirds = [
            " ".join(texts[: max(1, len(texts) // 3)]),
            " ".join(texts[max(1, len(texts) // 3): max(2, (len(texts) * 2) // 3)]),
            " ".join(texts[max(2, (len(texts) * 2) // 3):]),
        ]
        if any(_word_count(chunk) < 8 for chunk in thirds):
            issues.append("weak_beginning_middle_end_balance")
    score = max(0.0, 10.0 - len(issues) * 1.5)
    return {
        "scene_count": len(scenes),
        "word_count": total_words,
        "has_title": bool(title),
        "issues": issues,
        "score": round(score, 2),
    }


def revise_plan_with_ai(generator: Any, plan: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    scenes = [item for item in (plan.get("scenes") or []) if isinstance(item, dict)]
    original_texts = [_scene_text(scene) for scene in scenes]
    original_words = sum(_word_count(text) for text in original_texts)
    report: Dict[str, Any] = {
        "version": 1,
        "generated_at": _utc_iso(),
        "enabled": _enabled("ENABLE_NARRATIVE_EDITOR", "true"),
        "mode": "active" if _enabled("ENABLE_NARRATIVE_EDITOR", "true") else "disabled",
        "model": str(os.getenv("NARRATIVE_EDITOR_MODEL") or "gpt-4.1-mini").strip() or "gpt-4.1-mini",
        "changed": False,
        "fail_open": True,
        "original": analyze_narrative_plan(plan),
    }
    if not report["enabled"] or not scenes:
        return None, report

    try:
        ai_service = getattr(generator, "ai_service", None)
        if ai_service is not None and hasattr(ai_service, "_load_config"):
            ai_service._load_config()
        api_key = str(getattr(ai_service, "api_key", None) or os.getenv("OPENAI_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY ausente para editor narrativo")

        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        scene_payload = [
            {"scene": idx + 1, "text": text}
            for idx, text in enumerate(original_texts)
        ]
        instruction = (
            "Você é o Editor Narrativo do Codexia. Reescreva a narração em português do Brasil para soar como um vídeo premium, "
            "natural e envolvente quando lido em voz alta. O roteiro PRECISA ter arco claro de começo, meio e fim. "
            "Começo: título forte e abertura que contextualiza e desperta interesse sem clickbait barato. "
            "Meio: desenvolvimento progressivo, transições naturais, ideias conectadas, sem repetição e sem frases soltas. "
            "Fim: conclusão que resolve a ideia central e deixa uma reflexão memorável; CTA somente se o texto original já tiver intenção equivalente. "
            "Preserve rigorosamente o tema e os fatos fornecidos. Não invente versículos, capítulos, números, citações ou fatos que não estejam no material. "
            "Use prosa falada, frases de tamanho variado, pontuação que ajude a locução e evite linguagem robótica. "
            "Mantenha EXATAMENTE a mesma quantidade de cenas e a mesma ordem. Cada cena deve conter apenas texto narrável, sem markdown, rótulos, instruções de câmera ou metadados. "
            "Mantenha o total de palavras entre 85% e 115% do original para preservar duração e sincronização planejada. "
            "Responda SOMENTE JSON: {\"title\":\"...\",\"scenes\":[{\"scene\":1,\"text\":\"...\"}],\"closing_message\":\"...\"}."
        )
        context = {
            "current_title": _clean(plan.get("title") or plan.get("titulo")),
            "topic": _clean(plan.get("topic") or plan.get("theme") or plan.get("subject")),
            "scene_count": len(scenes),
            "original_word_count": original_words,
            "scenes": scene_payload,
            "existing_closing": _clean(plan.get("closing_message") or plan.get("end_message") or ""),
        }
        response = client.responses.create(
            model=report["model"],
            input=instruction + "\n\nMATERIAL ORIGINAL:\n" + json.dumps(context, ensure_ascii=False),
        )
        parsed = _safe_json(_extract_response_text(response))
        if not parsed:
            raise RuntimeError("editor narrativo retornou JSON inválido")
        revised_scenes = parsed.get("scenes")
        if not isinstance(revised_scenes, list) or len(revised_scenes) != len(scenes):
            raise RuntimeError("editor narrativo alterou a quantidade de cenas")
        new_texts = []
        for idx, item in enumerate(revised_scenes):
            if not isinstance(item, dict):
                raise RuntimeError(f"cena {idx + 1} inválida")
            text = _clean(item.get("text"))
            if not text:
                raise RuntimeError(f"cena {idx + 1} ficou vazia")
            new_texts.append(text)
        new_words = sum(_word_count(text) for text in new_texts)
        if original_words >= 30:
            ratio = new_words / max(1, original_words)
            if ratio < 0.80 or ratio > 1.20:
                raise RuntimeError(f"editor narrativo excedeu faixa segura de palavras: {ratio:.3f}")

        revised = deepcopy(plan)
        revised["title"] = _clean(parsed.get("title"))[:90] or _fallback_title(plan, scenes)
        revised_scene_objs = [item for item in (revised.get("scenes") or []) if isinstance(item, dict)]
        for scene, new_text in zip(revised_scene_objs, new_texts):
            scene[_scene_key(scene)] = new_text
        revised["scenes"] = revised_scene_objs
        closing = _clean(parsed.get("closing_message"))
        if closing:
            revised["closing_message"] = closing[:900]
        report["changed"] = True
        report["revised"] = analyze_narrative_plan(revised)
        report["word_count_ratio"] = round(new_words / max(1, original_words), 3)
        return revised, report
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        return None, report


def install_narrative_editor_patch(video_generator_cls: Type[Any]) -> Type[Any]:
    if getattr(video_generator_cls, "_codexia_narrative_editor_installed", False):
        return video_generator_cls
    original_create = getattr(video_generator_cls, "create_video_from_plan", None)
    if not callable(original_create):
        return video_generator_cls

    def create_with_narrative_editor(self: Any, plan: Any, *args: Any, **kwargs: Any):
        if not isinstance(plan, dict):
            return original_create(self, plan, *args, **kwargs)
        working = deepcopy(plan)
        scenes = [item for item in (working.get("scenes") or []) if isinstance(item, dict)]
        if not _clean(working.get("title") or working.get("titulo")):
            working["title"] = _fallback_title(working, scenes)
        revised, report = revise_plan_with_ai(self, working)
        final_plan = revised if isinstance(revised, dict) else working
        result = original_create(self, final_plan, *args, **kwargs)
        if isinstance(result, dict):
            result["narrative_editor"] = deepcopy(report)
            rr = result.get("render_report") if isinstance(result.get("render_report"), dict) else {}
            rr["narrative_editor"] = deepcopy(report)
            result["render_report"] = rr
            try:
                ai_service = getattr(self, "ai_service", None)
                task_id = getattr(ai_service, "ai_task_id", None) if ai_service is not None else None
                if task_id:
                    from app.services.task_manager import merge_task_result
                    merge_task_result(str(task_id), {
                        "narrative_editor": deepcopy(report),
                        "render_report": deepcopy(rr),
                    })
            except Exception:
                pass
        return result

    video_generator_cls.create_video_from_plan = create_with_narrative_editor
    video_generator_cls._codexia_narrative_editor_installed = True
    return video_generator_cls
