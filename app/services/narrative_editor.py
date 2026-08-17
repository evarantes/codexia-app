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
    return "Mensagem de Fé e Esperança"


def _repair_dangling_phrases(text: str) -> str:
    value = _clean(text)
    if not value:
        return value
    # Evita aberturas quebradas como "Uma mensagem de..." sem complemento.
    value = re.sub(
        r"(?i)\b(?:esta|uma)\s+mensagem\s+de\s*(?:\.{2,}|[.!?,;:]|$)",
        "Esta mensagem é para você. ",
        value,
    )
    value = re.sub(
        r"(?i)\b(?:esta|uma)\s+palavra\s+de\s*(?:\.{2,}|[.!?,;:]|$)",
        "Esta palavra é para você. ",
        value,
    )
    return _clean(value)


def _repair_repetitive_hook(text: str) -> tuple[str, bool]:
    """Remove o vício de abrir sempre com 'Você já...' sem inventar novo fato."""
    value = _clean(text)
    if not value:
        return value, False
    patterns = [
        (r"(?i)^você\s+já\s+se\s+sentiu\s+(.+?)[?]\s*", r"Há momentos em que \1. "),
        (r"(?i)^você\s+já\s+sentiu\s+(.+?)[?]\s*", r"Há momentos em que \1. "),
        (r"(?i)^você\s+já\s+passou\s+por\s+(.+?)[?]\s*", r"Algumas fases da vida nos fazem atravessar \1. "),
        (r"(?i)^você\s+já\s+percebeu\s+(.+?)[?]\s*", r"Às vezes percebemos \1. "),
        (r"(?i)^você\s+já\s+(.+?)[?]\s*", r"Existem momentos em que \1. "),
    ]
    for pattern, replacement in patterns:
        repaired, count = re.subn(pattern, replacement, value, count=1)
        if count:
            return _clean(repaired), True
    return value, False


def _short_closing(value: Any, fallback: str = "Leve esta esperança com você: Deus continua presente.") -> str:
    text = _clean(value)
    if not text:
        text = fallback
    # Endcard precisa ser curto e legível em celular.
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    if sentences:
        text = " ".join(sentences[:2])
    if len(text) > 150:
        clipped = text[:147].rsplit(" ", 1)[0].rstrip(" ,;:-")
        text = clipped + "."
    return text


def _quality_guard_texts(texts: List[str]) -> tuple[List[str], Dict[str, Any]]:
    repaired: List[str] = []
    report = {"dangling_phrase_repairs": 0, "repetitive_hook_repaired": False}
    for idx, text in enumerate(texts):
        before = _clean(text)
        after = _repair_dangling_phrases(before)
        if after != before:
            report["dangling_phrase_repairs"] += 1
        if idx == 0:
            after, changed = _repair_repetitive_hook(after)
            report["repetitive_hook_repaired"] = bool(changed)
        repaired.append(after)
    return repaired, report


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
    if texts and re.match(r"(?i)^você\s+já\b", _clean(texts[0])):
        issues.append("repetitive_voce_ja_hook")
    if any(re.search(r"(?i)\b(?:uma|esta)\s+(?:mensagem|palavra)\s+de\s*(?:\.{2,}|[.!?,;:]|$)", t) for t in texts):
        issues.append("dangling_opening_phrase")
    if len(texts) >= 3:
        thirds = [
            " ".join(texts[: max(1, len(texts) // 3)]),
            " ".join(texts[max(1, len(texts) // 3): max(2, (len(texts) * 2) // 3)]),
            " ".join(texts[max(2, (len(texts) * 2) // 3):]),
        ]
        if any(_word_count(chunk) < 8 for chunk in thirds):
            issues.append("weak_beginning_middle_end_balance")
    score = max(0.0, 10.0 - len(issues) * 1.25)
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
        "version": 2,
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
            "Você é o Editor-Chefe Narrativo do Codexia para um canal cristão premium. Reescreva a narração em português do Brasil para soar humana, calorosa, memorável e excelente quando lida em voz alta. "
            "O roteiro PRECISA ter arco claro de COMEÇO, MEIO e FIM, mantendo rigorosamente o tema e os fatos fornecidos. "
            "TÍTULO: gere um título forte, específico e natural, sem clickbait barato. "
            "ABERTURA: depois do título, entregue uma ponte COMPLETA e natural para a mensagem; jamais deixe frases penduradas como 'uma mensagem de...' ou 'uma palavra de...' sem complemento. "
            "GANCHO: seja criativo e varie o estilo. A primeira cena NÃO deve começar com 'Você já...', 'Você alguma vez...' ou fórmula equivalente. Prefira alternar entre observação emocional, contraste, imagem poética, afirmação de esperança, pergunta profunda, acolhimento pastoral, contexto bíblico ou promessa de valor. "
            "Evite bordões repetitivos e evite que roteiros diferentes pareçam cópias uns dos outros. A abertura deve aproximar o ouvinte e fazê-lo querer permanecer até o fim sem manipulação. "
            "MEIO: desenvolva a ideia progressivamente, com transições naturais, exemplos/metáforas somente quando compatíveis com o material, e sem repetir a mesma ideia com palavras diferentes. "
            "FIM: conclua a ideia central, entregue uma reflexão espiritual curta e memorável e feche a mensagem de forma completa. CTA somente se houver intenção equivalente no material ou se for um convite discreto de continuidade do canal, nunca misturado à reflexão principal. "
            "LOCUÇÃO: use prosa falada, frases de tamanhos variados e pontuação que favoreça a pronúncia. Evite trava-línguas, construções truncadas e sequências ambíguas. Quando usar 'pelo contrário', prefira 'muito pelo contrário' ou 'ao contrário do que parece' para uma fala mais clara. "
            "FIDELIDADE: não invente versículos, capítulos, números, citações, promessas específicas ou fatos que não estejam no material. "
            "ESTRUTURA TÉCNICA: mantenha EXATAMENTE a mesma quantidade de cenas e a mesma ordem. Cada cena deve conter apenas texto narrável, sem markdown, rótulos, instruções de câmera ou metadados. "
            "DURAÇÃO: mantenha o total de palavras entre 85% e 115% do original. "
            "ENCERRAMENTO VISUAL: closing_message deve ser uma frase/reflexão forte e curta, idealmente até 150 caracteres, separada do CTA. "
            "Responda SOMENTE JSON: {\"title\":\"...\",\"hook_style\":\"...\",\"scenes\":[{\"scene\":1,\"text\":\"...\"}],\"closing_message\":\"...\",\"endcard_cta_text\":\"Inscreva-se e acompanhe novas mensagens de fé e esperança.\"}."
        )
        context = {
            "current_title": _clean(plan.get("title") or plan.get("titulo")),
            "topic": _clean(plan.get("topic") or plan.get("theme") or plan.get("subject")),
            "scene_count": len(scenes),
            "original_word_count": original_words,
            "scenes": scene_payload,
            "existing_closing": _clean(plan.get("closing_message") or plan.get("end_message") or plan.get("final_message") or ""),
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

        new_texts, guard_report = _quality_guard_texts(new_texts)
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

        closing = _short_closing(parsed.get("closing_message") or plan.get("closing_message") or plan.get("final_message"))
        revised["closing_message"] = closing
        revised["final_message"] = closing
        revised["endcard_cta_text"] = _clean(parsed.get("endcard_cta_text"))[:110] or "Inscreva-se e acompanhe novas mensagens de fé e esperança."

        report["changed"] = True
        report["hook_style"] = _clean(parsed.get("hook_style"))[:80]
        report["quality_guard"] = guard_report
        report["revised"] = analyze_narrative_plan(revised)
        report["word_count_ratio"] = round(new_words / max(1, original_words), 3)
        report["closing_chars"] = len(closing)
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
        if bool(working.get('editorial_reviewed')):
            report = {
                'version': 3,
                'enabled': True,
                'mode': 'human_review_preserved',
                'changed': False,
                'fail_open': True,
                'skip_reason': 'editorial_reviewed_before_video_generation',
                'original': analyze_narrative_plan(working),
            }
            revised = None
        else:
            revised, report = revise_plan_with_ai(self, working)
        final_plan = revised if isinstance(revised, dict) else working

        # Mesmo em fail-open, remove frases penduradas e garante endcard legível.
        final_scenes = [item for item in (final_plan.get("scenes") or []) if isinstance(item, dict)]
        fallback_texts, fallback_guard = _quality_guard_texts([_scene_text(scene) for scene in final_scenes])
        for scene, repaired_text in zip(final_scenes, fallback_texts):
            if repaired_text:
                scene[_scene_key(scene)] = repaired_text
        final_plan["scenes"] = final_scenes
        final_plan["final_message"] = _short_closing(final_plan.get("final_message") or final_plan.get("closing_message"))
        final_plan["closing_message"] = final_plan["final_message"]
        final_plan.setdefault("endcard_cta_text", "Inscreva-se e acompanhe novas mensagens de fé e esperança.")
        if isinstance(report, dict):
            report.setdefault("fallback_quality_guard", fallback_guard)

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
