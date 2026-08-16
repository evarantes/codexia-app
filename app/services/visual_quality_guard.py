from __future__ import annotations

import base64
import inspect
import io
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Type


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "sim", "on", "enabled", "enable"}


def _env_int(name: str, default: int, minimum: int = 0, maximum: int = 5) -> int:
    try:
        value = int(str(os.getenv(name) or default).strip())
    except Exception:
        value = int(default)
    return max(minimum, min(maximum, value))


def _safe_json(raw: Any) -> Optional[Dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        return None
    candidates = [text]
    if "```json" in text:
        candidates.append(text.split("```json", 1)[1].split("```", 1)[0].strip())
    if "```" in text:
        candidates.append(text.split("```", 1)[1].split("```", 1)[0].strip())
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    return None


def _data_url_for_image(path: str) -> str:
    """Cria uma versão compacta para crítica visual sem alterar o artefato original."""
    from PIL import Image

    with Image.open(path) as source:
        image = source.convert("RGB")
        image.thumbnail((1024, 1024))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=82, optimize=True)
    payload = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{payload}"


def _local_metrics(path: str) -> Dict[str, Any]:
    try:
        from app.services.visual_quality_shadow import _inspect_image

        return _inspect_image(path)
    except Exception as exc:
        return {
            "path": path,
            "exists": bool(path and os.path.isfile(path)),
            "readable": False,
            "local_flags": [
                {
                    "severity": "warning",
                    "code": "local_inspection_unavailable",
                    "message": f"Inspeção local indisponível: {type(exc).__name__}",
                }
            ],
        }


def _extract_response_text(response: Any) -> str:
    value = getattr(response, "output_text", None)
    if value:
        return str(value)
    try:
        output = getattr(response, "output", None) or []
        chunks = []
        for item in output:
            for content in getattr(item, "content", None) or []:
                text = getattr(content, "text", None)
                if text:
                    chunks.append(str(text))
        return "\n".join(chunks)
    except Exception:
        return ""


def _critical_issue_codes(review: Dict[str, Any]) -> list[str]:
    issues = review.get("issues") if isinstance(review.get("issues"), list) else []
    critical = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        severity = str(issue.get("severity") or "").strip().lower()
        if severity in {"critical", "grave", "high"}:
            code = str(issue.get("code") or issue.get("category") or "critical_visual_issue").strip()
            if code:
                critical.append(code)
    return critical


def _normalize_review(data: Optional[Dict[str, Any]], *, local: Dict[str, Any], model: Optional[str] = None) -> Dict[str, Any]:
    payload = data if isinstance(data, dict) else {}
    issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    normalized_issues = []
    for issue in issues[:12]:
        if not isinstance(issue, dict):
            continue
        normalized_issues.append(
            {
                "code": str(issue.get("code") or issue.get("category") or "visual_issue").strip()[:80],
                "severity": str(issue.get("severity") or "warning").strip().lower()[:20],
                "message": str(issue.get("message") or issue.get("reason") or "").strip()[:400],
            }
        )
    try:
        score = float(payload.get("score") if payload.get("score") is not None else 0.0)
    except Exception:
        score = 0.0
    score = max(0.0, min(10.0, score))
    approve_value = payload.get("approve")
    approve = bool(approve_value) if isinstance(approve_value, bool) else score >= 8.0
    critical = _critical_issue_codes({"issues": normalized_issues})
    if critical:
        approve = False
    return {
        "reviewed_at": _utc_iso(),
        "status": "reviewed",
        "model": model,
        "approve": approve,
        "score": round(score, 2),
        "issues": normalized_issues,
        "critical_issue_codes": critical,
        "summary": str(payload.get("summary") or payload.get("reason") or "").strip()[:600],
        "local_metrics": local,
    }


def review_generated_image(
    generator: Any,
    image_path: str,
    *,
    visual_prompt: str = "",
    narration_context: str = "",
) -> Dict[str, Any]:
    """Revisa uma imagem recém-gerada.

    A chamada multimodal só ocorre quando ENABLE_VISUAL_CRITIC_AI=true. Em qualquer
    indisponibilidade do crítico a produção continua (fail-open), evitando que uma
    ferramenta de QA derrube o pipeline funcional.
    """
    local = _local_metrics(image_path)
    local_critical = [
        flag
        for flag in list(local.get("local_flags") or [])
        if isinstance(flag, dict) and str(flag.get("severity") or "").lower() == "critical"
    ]
    if local_critical:
        return {
            "reviewed_at": _utc_iso(),
            "status": "local_reject",
            "model": None,
            "approve": False,
            "score": 0.0,
            "issues": local_critical,
            "critical_issue_codes": [str(item.get("code") or "unreadable_image") for item in local_critical],
            "summary": "Artefato visual inválido antes da crítica multimodal.",
            "local_metrics": local,
        }

    if not _env_bool("ENABLE_VISUAL_CRITIC_AI", False):
        return {
            "reviewed_at": _utc_iso(),
            "status": "ai_critic_disabled",
            "model": None,
            "approve": True,
            "score": None,
            "issues": [],
            "critical_issue_codes": [],
            "summary": "Crítico multimodal desligado; somente inspeção local executada.",
            "local_metrics": local,
        }

    ai_service = getattr(generator, "ai_service", None)
    if ai_service is None:
        return {
            "reviewed_at": _utc_iso(),
            "status": "critic_unavailable",
            "model": None,
            "approve": True,
            "score": None,
            "issues": [],
            "critical_issue_codes": [],
            "summary": "Serviço de IA indisponível para crítica visual; fail-open.",
            "local_metrics": local,
        }

    try:
        if hasattr(ai_service, "_load_config"):
            ai_service._load_config()
        api_key = str(getattr(ai_service, "api_key", None) or os.getenv("OPENAI_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY ausente para crítico visual")

        from openai import OpenAI

        model = str(os.getenv("VISUAL_QA_MODEL") or "gpt-4.1-mini").strip() or "gpt-4.1-mini"
        client = OpenAI(api_key=api_key)
        instruction = (
            "Você é o Fiscal Visual do Codexia. Avalie SOMENTE qualidade técnica e coerência visual desta cena. "
            "Reprove apenas defeitos claros que prejudicariam um canal premium: olhos vazios/brancos/deformados, "
            "rostos severamente deformados, mãos/dedos grotescos quando visíveis, membros impossíveis, fusão de corpos, "
            "atributos de gênero manifestamente incompatíveis com o personagem solicitado, personagem principal errado, "
            "artefatos de geração evidentes, texto/marca-d'água indesejado ou imagem incompatível com o contexto narrado. "
            "Não reprove por preferência estética subjetiva nem por pequenas imperfeições. "
            "Responda APENAS JSON no formato: "
            "{\"approve\":true|false,\"score\":0-10,\"summary\":\"...\","
            "\"issues\":[{\"code\":\"...\",\"severity\":\"critical|warning|info\",\"message\":\"...\"}]}"
        )
        context = (
            f"PROMPT VISUAL ESPERADO:\n{str(visual_prompt or '')[:1800]}\n\n"
            f"CONTEXTO DA NARRAÇÃO:\n{str(narration_context or '')[:1200]}"
        )
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": instruction + "\n\n" + context},
                        {"type": "input_image", "image_url": _data_url_for_image(image_path), "detail": "high"},
                    ],
                }
            ],
        )
        parsed = _safe_json(_extract_response_text(response))
        if not parsed:
            raise RuntimeError("Crítico visual retornou resposta sem JSON utilizável")
        return _normalize_review(parsed, local=local, model=model)
    except Exception as exc:
        return {
            "reviewed_at": _utc_iso(),
            "status": "critic_error",
            "model": str(os.getenv("VISUAL_QA_MODEL") or "gpt-4.1-mini"),
            "approve": True,
            "score": None,
            "issues": [],
            "critical_issue_codes": [],
            "summary": f"Crítico visual indisponível; fail-open: {type(exc).__name__}: {str(exc)[:240]}",
            "local_metrics": local,
        }


def _corrective_prompt(original_prompt: str, review: Dict[str, Any], attempt: int) -> str:
    issue_codes = [str(code) for code in list(review.get("critical_issue_codes") or []) if str(code).strip()]
    issue_text = ", ".join(issue_codes[:6]) or "critical visual artifact"
    return (
        f"{str(original_prompt or '').strip()}\n\n"
        f"QUALITY RETRY {attempt}: regenerate this scene as a NEW composition. Fix these rejected defects: {issue_text}. "
        "All visible people must have natural anatomically plausible eyes, faces, hands and limbs. "
        "No blank white eyes, no malformed pupils, no fused fingers, no extra limbs, no duplicated faces, "
        "no unintended facial hair, no text, no watermark. Preserve the intended biblical character identity, "
        "wardrobe, era, emotion and narrative meaning while changing camera framing enough to avoid repeating the rejected image."
    ).strip()


def _record_event(generator: Any, event: Dict[str, Any]) -> None:
    try:
        events = getattr(generator, "_codexia_visual_guard_events", None)
        if not isinstance(events, list):
            events = []
            generator._codexia_visual_guard_events = events
        events.append(deepcopy(event))
    except Exception:
        pass


def _persist_summary(generator: Any, result: Dict[str, Any], events: list[Dict[str, Any]]) -> None:
    summary = {
        "version": 1,
        "generated_at": _utc_iso(),
        "mode": "strict" if _env_bool("ENABLE_STRICT_VISUAL_REJECT", False) else "observe",
        "ai_critic_enabled": _env_bool("ENABLE_VISUAL_CRITIC_AI", False),
        "fail_closed": _env_bool("VISUAL_QA_FAIL_CLOSED", False),
        "max_retries_per_generated_image": _env_int("VISUAL_QA_MAX_RETRIES", 1, 0, 3),
        "event_count": len(events),
        "rejected_count": sum(1 for item in events if not bool((item.get("review") or {}).get("approve", True))),
        "retry_count": sum(1 for item in events if int(item.get("attempt") or 0) > 0),
        "events": deepcopy(events[-100:]),
    }
    result["visual_quality_critic"] = summary
    render_report = result.get("render_report") if isinstance(result.get("render_report"), dict) else {}
    render_report["visual_quality_critic"] = deepcopy(summary)
    result["render_report"] = render_report
    task_id = None
    try:
        ai_service = getattr(generator, "ai_service", None)
        task_id = getattr(ai_service, "ai_task_id", None) if ai_service is not None else None
    except Exception:
        task_id = None
    if task_id:
        try:
            from app.services.task_manager import merge_task_result

            merge_task_result(str(task_id), {
                "visual_quality_critic": deepcopy(summary),
                "render_report": deepcopy(render_report),
            })
        except Exception:
            pass


def install_visual_quality_guard_patch(video_generator_cls: Type[Any]) -> Type[Any]:
    """Adiciona crítica e retry seletivo à mesma classe canônica.

    Por padrão apenas observa e não chama IA. A regeneração paga só é permitida
    com ENABLE_STRICT_VISUAL_REJECT=true e ENABLE_VISUAL_CRITIC_AI=true.
    """
    if getattr(video_generator_cls, "_codexia_visual_quality_guard_installed", False):
        return video_generator_cls

    original_ensure = getattr(video_generator_cls, "_ensure_image_for_scene", None)
    original_create = getattr(video_generator_cls, "create_video_from_plan", None)
    if not callable(original_ensure) or not callable(original_create):
        return video_generator_cls

    signature = inspect.signature(original_ensure)

    def ensure_with_visual_guard(self: Any, *args: Any, **kwargs: Any):
        path = original_ensure(self, *args, **kwargs)
        prompt = str(args[0] if args else kwargs.get("prompt") or kwargs.get("image_prompt") or "").strip()
        narration_context = str(kwargs.get("text_fallback") or "").strip()
        review = review_generated_image(
            self,
            str(path or ""),
            visual_prompt=prompt,
            narration_context=narration_context,
        ) if path else {
            "reviewed_at": _utc_iso(),
            "status": "missing_image",
            "approve": False,
            "score": 0.0,
            "issues": [{"code": "missing_image", "severity": "critical", "message": "Nenhuma imagem foi retornada."}],
            "critical_issue_codes": ["missing_image"],
        }
        _record_event(self, {"attempt": 0, "image_path": path, "prompt": prompt[:600], "review": review})

        strict = _env_bool("ENABLE_STRICT_VISUAL_REJECT", False)
        if not strict or bool(review.get("approve", True)):
            return path

        retries = _env_int("VISUAL_QA_MAX_RETRIES", 1, 0, 3)
        current_path = path
        current_review = review
        for attempt in range(1, retries + 1):
            retry_prompt = _corrective_prompt(prompt, current_review, attempt)
            try:
                if args:
                    retry_args = list(args)
                    retry_args[0] = retry_prompt
                    current_path = original_ensure(self, *retry_args, **kwargs)
                else:
                    retry_kwargs = dict(kwargs)
                    if "prompt" in signature.parameters:
                        retry_kwargs["prompt"] = retry_prompt
                    elif "image_prompt" in signature.parameters:
                        retry_kwargs["image_prompt"] = retry_prompt
                    else:
                        current_path = None
                    if current_path is not None:
                        current_path = original_ensure(self, **retry_kwargs)
                if not current_path:
                    continue
                current_review = review_generated_image(
                    self,
                    str(current_path),
                    visual_prompt=retry_prompt,
                    narration_context=narration_context,
                )
                _record_event(
                    self,
                    {
                        "attempt": attempt,
                        "image_path": current_path,
                        "prompt": retry_prompt[:600],
                        "review": current_review,
                    },
                )
                if bool(current_review.get("approve", True)):
                    return current_path
            except Exception as exc:
                _record_event(
                    self,
                    {
                        "attempt": attempt,
                        "image_path": current_path,
                        "prompt": retry_prompt[:600],
                        "review": {
                            "status": "retry_error",
                            "approve": True,
                            "summary": f"Retry visual falhou sem derrubar pipeline: {type(exc).__name__}: {str(exc)[:240]}",
                        },
                    },
                )
                break

        if _env_bool("VISUAL_QA_FAIL_CLOSED", False) and current_review and not bool(current_review.get("approve", True)):
            codes = ", ".join(str(x) for x in list(current_review.get("critical_issue_codes") or [])[:6]) or "qualidade visual crítica"
            raise RuntimeError(f"Cena reprovada pelo Fiscal Visual após retries seletivos: {codes}")
        return current_path or path

    def create_with_visual_guard(self: Any, plan: Any, *args: Any, **kwargs: Any):
        # Limpa somente o relatório da execução atual; não altera nenhum asset.
        self._codexia_visual_guard_events = []
        result = original_create(self, plan, *args, **kwargs)
        if isinstance(result, dict):
            try:
                _persist_summary(self, result, list(getattr(self, "_codexia_visual_guard_events", []) or []))
            except Exception:
                pass
        return result

    video_generator_cls._ensure_image_for_scene = ensure_with_visual_guard
    video_generator_cls.create_video_from_plan = create_with_visual_guard
    video_generator_cls._codexia_visual_quality_guard_installed = True
    return video_generator_cls
