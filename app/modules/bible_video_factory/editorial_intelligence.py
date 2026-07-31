from datetime import datetime
from time import perf_counter
from typing import Any, Dict, List, Optional


ALLOWED_EDITORIAL_INTELLIGENCE_PROVIDERS = (
    "OpenAI",
    "OpenRouter",
    "Gemini",
    "Claude",
    "Local",
    "Disabled",
)

DEFAULT_EDITORIAL_INTELLIGENCE_SETTINGS: Dict[str, Any] = {
    "editorial_intelligence_enabled": True,
    "editorial_intelligence_fail_open": True,
    "editorial_intelligence_mode": "pre_narration",
    "editorial_intelligence_provider": "OpenAI",
    "primary_provider": "OpenAI",
    "fallback_provider": "OpenRouter",
    "editorial_provider": "OpenAI",
    "editorial_fallback_provider": "OpenRouter",
    "provider_priority": "OpenAI,OpenRouter",
    "approved_models": "OpenAI:gpt-4o-mini\nOpenRouter:openai/gpt-4o-mini",
}

EDITORIAL_INTELLIGENCE_TEXT_FIELDS = {
    "editorial_intelligence_mode",
    "editorial_intelligence_provider",
    "primary_provider",
    "fallback_provider",
    "editorial_provider",
    "editorial_fallback_provider",
    "provider_priority",
    "approved_models",
}

EDITORIAL_INTELLIGENCE_BOOL_FIELDS = {
    "editorial_intelligence_enabled",
    "editorial_intelligence_fail_open",
}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_bool(value: Any, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    lowered = _normalize_text(value).lower()
    if lowered in {"1", "true", "yes", "sim", "on", "enabled", "enable"}:
        return True
    if lowered in {"0", "false", "no", "nao", "off", "disabled", "disable"}:
        return False
    return bool(default)


def _normalize_provider(value: Any) -> str:
    lowered = _normalize_text(value).lower()
    aliases = {
        "openai": "OpenAI",
        "openrouter": "OpenRouter",
        "gemini": "Gemini",
        "claude": "Claude",
        "anthropic": "Claude",
        "local": "Local",
        "disabled": "Disabled",
        "disable": "Disabled",
        "off": "Disabled",
    }
    normalized = aliases.get(lowered, _normalize_text(value))
    return normalized if normalized in ALLOWED_EDITORIAL_INTELLIGENCE_PROVIDERS else DEFAULT_EDITORIAL_INTELLIGENCE_SETTINGS["editorial_provider"]


def normalize_editorial_intelligence_settings(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    normalized = dict(DEFAULT_EDITORIAL_INTELLIGENCE_SETTINGS)
    raw = payload if isinstance(payload, dict) else {}
    for key in EDITORIAL_INTELLIGENCE_TEXT_FIELDS:
        if key in raw:
            if key in {
                "editorial_intelligence_provider",
                "primary_provider",
                "fallback_provider",
                "editorial_provider",
                "editorial_fallback_provider",
            }:
                normalized[key] = _normalize_provider(raw.get(key))
            else:
                normalized[key] = _normalize_text(raw.get(key)) or DEFAULT_EDITORIAL_INTELLIGENCE_SETTINGS[key]
    for key in EDITORIAL_INTELLIGENCE_BOOL_FIELDS:
        if key in raw:
            normalized[key] = _normalize_bool(raw.get(key), bool(DEFAULT_EDITORIAL_INTELLIGENCE_SETTINGS[key]))
    if normalized.get("editorial_intelligence_provider") and not _normalize_text(raw.get("editorial_provider")):
        normalized["editorial_provider"] = normalized["editorial_intelligence_provider"]
    if normalized.get("editorial_provider") == "Disabled":
        normalized["editorial_intelligence_enabled"] = False
    return normalized


def _split_items(value: Any) -> List[str]:
    text = _normalize_text(value)
    if not text:
        return []
    normalized = text.replace("\r", "\n").replace(";", "\n")
    items: List[str] = []
    for chunk in normalized.split("\n"):
        for part in chunk.split(","):
            clean = _normalize_text(part)
            if clean:
                items.append(clean)
    return items


def _default_models_for_provider(provider: str) -> List[str]:
    if provider == "OpenRouter":
        return ["openai/gpt-4o-mini"]
    if provider == "OpenAI":
        return ["gpt-4o-mini"]
    if provider in {"Gemini", "Claude"}:
        return []
    if provider == "Local":
        return ["heuristic"]
    return []


def _parse_approved_models(settings: Dict[str, Any]) -> Dict[str, List[str]]:
    parsed: Dict[str, List[str]] = {}
    for entry in _split_items(settings.get("approved_models")):
        provider_part, sep, model_part = entry.partition(":")
        provider = _normalize_provider(provider_part)
        model = _normalize_text(model_part) if sep else ""
        if provider not in ALLOWED_EDITORIAL_INTELLIGENCE_PROVIDERS or not model:
            continue
        parsed.setdefault(provider, [])
        if model not in parsed[provider]:
            parsed[provider].append(model)
    for provider in ALLOWED_EDITORIAL_INTELLIGENCE_PROVIDERS:
        parsed.setdefault(provider, _default_models_for_provider(provider))
    return parsed


def _resolve_provider_chain(settings: Dict[str, Any]) -> List[str]:
    providers: List[str] = []
    requested = _normalize_provider(settings.get("editorial_provider") or settings.get("editorial_intelligence_provider"))
    fallback = _normalize_provider(settings.get("editorial_fallback_provider") or settings.get("fallback_provider"))
    for raw in _split_items(settings.get("provider_priority")):
        provider = _normalize_provider(raw)
        if provider not in providers:
            providers.append(provider)
    if requested not in providers:
        providers.insert(0, requested)
    if fallback not in providers:
        providers.append(fallback)
    filtered = [provider for provider in providers if provider in {"OpenAI", "OpenRouter", "Gemini", "Claude", "Local"}]
    return filtered or ["OpenAI", "OpenRouter"]


def _classify_provider_error(provider: str, exc: Exception) -> str:
    text = _normalize_text(exc).lower()
    if not text:
        return f"{provider.lower()}_unknown_error"
    if "insufficient_quota" in text or "insufficient balance" in text or "saldo" in text or "billing" in text:
        return f"{provider.lower()}_insufficient_balance"
    if "rate limit" in text or "429" in text:
        return f"{provider.lower()}_rate_limit"
    if "timeout" in text or "timed out" in text:
        return f"{provider.lower()}_timeout"
    if "401" in text or "403" in text or "invalid api key" in text or "auth" in text:
        return f"{provider.lower()}_authentication_error"
    if "500" in text or "502" in text or "503" in text or "504" in text or "server" in text:
        return f"{provider.lower()}_upstream_5xx"
    if "api_key_missing" in text:
        return f"{provider.lower()}_api_key_missing"
    return f"{provider.lower()}_provider_error"


class EditorialIntelligenceService:
    def __init__(self, quality_service: Any):
        self.quality_service = quality_service

    def _now_iso(self) -> str:
        return datetime.utcnow().isoformat(timespec="seconds") + "Z"

    def _scene_texts_from_plan(self, plan: Dict[str, Any]) -> List[str]:
        texts: List[str] = []
        for item in plan.get("scenes") or []:
            if not isinstance(item, dict):
                continue
            text = _normalize_text(item.get("text"))
            if text:
                texts.append(text)
        return texts

    def _join_scene_texts(self, texts: List[str]) -> str:
        return "\n\n".join(_normalize_text(item) for item in texts if _normalize_text(item)).strip()

    def _summarize_changes(self, original_texts: List[str], revised_texts: List[str]) -> Dict[str, Any]:
        changes: List[Dict[str, Any]] = []
        for idx, original in enumerate(original_texts or []):
            revised = revised_texts[idx] if idx < len(revised_texts or []) else ""
            if _normalize_text(original) != _normalize_text(revised):
                changes.append(
                    {
                        "scene_index": idx,
                        "scene_number": idx + 1,
                        "original_excerpt": _normalize_text(original)[:220],
                        "revised_excerpt": _normalize_text(revised)[:220],
                    }
                )
        summary = (
            "Nenhuma alteracao textual foi necessaria."
            if not changes
            else f"Correcoes aplicadas em {len(changes)} trecho(s) do roteiro."
        )
        return {
            "summary": summary,
            "changes": changes,
            "correction_count": len(changes),
        }

    def _apply_revised_scene_texts(self, plan: Dict[str, Any], revised_scene_texts: List[str]) -> None:
        scenes = plan.get("scenes") or []
        for idx, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                continue
            if idx < len(revised_scene_texts):
                scene["text"] = _normalize_text(revised_scene_texts[idx]) or scene.get("text") or ""

    def _build_failure_record(
        self,
        *,
        base_record: Dict[str, Any],
        scene_texts: List[str],
        requested_provider: str,
        requested_model: str,
        attempts: List[Dict[str, Any]],
        elapsed_ms: int,
        failover_reason: str,
        error: str,
    ) -> Dict[str, Any]:
        return {
            **base_record,
            "status": "failed",
            "summary": "Falha na revisao editorial; roteiro original preservado.",
            "provider_requested": requested_provider,
            "provider_used": "",
            "model_requested": requested_model,
            "model_used": "",
            "failover_reason": failover_reason,
            "original_text": self._join_scene_texts(scene_texts),
            "revised_text": self._join_scene_texts(scene_texts),
            "original_scene_texts": scene_texts,
            "revised_scene_texts": scene_texts,
            "change_summary": "Nenhuma alteracao textual foi aplicada devido a falha do editor.",
            "correction_count": 0,
            "review_time_ms": elapsed_ms,
            "review_time_seconds": round(float(elapsed_ms) / 1000.0, 3),
            "issues": [],
            "actions": [],
            "attempts": attempts,
            "error": error,
        }

    def review_plan(
        self,
        plan: Dict[str, Any],
        settings_payload: Optional[Dict[str, Any]] = None,
        *,
        script_id: Optional[int] = None,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        settings = normalize_editorial_intelligence_settings(settings_payload)
        updated_plan = dict(plan if isinstance(plan, dict) else {})
        scene_texts = self._scene_texts_from_plan(updated_plan)
        approved_models = _parse_approved_models(settings)
        provider_chain = _resolve_provider_chain(settings)
        requested_provider = _normalize_provider(settings.get("editorial_provider") or settings.get("editorial_intelligence_provider"))
        requested_models = approved_models.get(requested_provider) or _default_models_for_provider(requested_provider)
        requested_model = requested_models[0] if requested_models else ""
        base_record = {
            "applied_at": self._now_iso(),
            "mode": settings.get("editorial_intelligence_mode"),
            "provider": requested_provider,
            "script_id": script_id,
            "task_id": task_id,
            "error": "",
        }
        if not settings.get("editorial_intelligence_enabled", True):
            return {
                "status": "disabled",
                "plan_updates": {
                    "editorial_intelligence": {
                        **base_record,
                        "status": "disabled",
                        "summary": "Editor Editorial desabilitado por configuracao.",
                        "provider_requested": requested_provider,
                        "provider_used": "Disabled",
                        "model_requested": requested_model,
                        "model_used": "",
                        "failover_reason": "",
                        "correction_count": 0,
                        "review_time_ms": 0,
                        "review_time_seconds": 0.0,
                        "attempts": [],
                    }
                },
            }
        if not scene_texts:
            return {
                "status": "skipped",
                "plan_updates": {
                    "editorial_intelligence": {
                        **base_record,
                        "status": "skipped",
                        "summary": "Roteiro sem trechos narrativos para revisao.",
                        "provider_requested": requested_provider,
                        "provider_used": requested_provider,
                        "model_requested": requested_model,
                        "model_used": "",
                        "failover_reason": "",
                        "correction_count": 0,
                        "review_time_ms": 0,
                        "review_time_seconds": 0.0,
                        "attempts": [],
                    }
                },
            }

        payload = {
            "title": _normalize_text(updated_plan.get("title")),
            "scene_texts": scene_texts,
        }
        started_at = perf_counter()
        attempts: List[Dict[str, Any]] = []
        failover_reasons: List[str] = []

        for attempt_index, provider in enumerate(provider_chain):
            model_candidates = approved_models.get(provider) or _default_models_for_provider(provider)
            requested_attempt_model = model_candidates[0] if model_candidates else ""
            try:
                review = self.quality_service.review_narration_package(
                    payload,
                    provider_choice=provider,
                    model_candidates=model_candidates,
                )
                elapsed_ms = int(round((perf_counter() - started_at) * 1000))
                editor_report = review.get("editor_report") if isinstance(review.get("editor_report"), dict) else {}
                critic_report = review.get("critic_report") if isinstance(review.get("critic_report"), dict) else {}
                revised_scene_texts = [
                    _normalize_text(item)
                    for item in (editor_report.get("scene_texts") or scene_texts)
                    if _normalize_text(item)
                ]
                if len(revised_scene_texts) != len(scene_texts):
                    revised_scene_texts = list(scene_texts)
                self._apply_revised_scene_texts(updated_plan, revised_scene_texts)
                diff_report = self._summarize_changes(scene_texts, revised_scene_texts)
                attempts.append(
                    {
                        "provider": provider,
                        "model_requested": requested_attempt_model,
                        "model_used": _normalize_text(review.get("model_used")),
                        "status": "success",
                        "reason": "",
                    }
                )
                record = {
                    **base_record,
                    "status": "applied",
                    "summary": _normalize_text(review.get("summary")) or diff_report.get("summary"),
                    "provider_requested": requested_provider,
                    "provider_used": _normalize_text(review.get("provider_used")) or provider,
                    "model_requested": requested_model,
                    "model_used": _normalize_text(review.get("model_used")),
                    "failover_reason": " | ".join(failover_reasons),
                    "original_text": self._join_scene_texts(scene_texts),
                    "revised_text": self._join_scene_texts(revised_scene_texts),
                    "original_scene_texts": scene_texts,
                    "revised_scene_texts": revised_scene_texts,
                    "change_summary": diff_report.get("summary"),
                    "correction_count": int(diff_report.get("correction_count") or 0),
                    "review_time_ms": elapsed_ms,
                    "review_time_seconds": round(float(elapsed_ms) / 1000.0, 3),
                    "issues": critic_report.get("issues") if isinstance(critic_report.get("issues"), list) else [],
                    "actions": editor_report.get("actions") if isinstance(editor_report.get("actions"), list) else [],
                    "attempts": attempts,
                    "error": "",
                }
                updated_plan["editorial_intelligence"] = record
                return {"status": "applied", "plan_updates": updated_plan}
            except Exception as exc:
                reason = _classify_provider_error(provider, exc)
                failover_reasons.append(reason)
                attempts.append(
                    {
                        "provider": provider,
                        "model_requested": requested_attempt_model,
                        "model_used": "",
                        "status": "failed",
                        "reason": reason,
                        "error": _normalize_text(exc),
                    }
                )
                if attempt_index == len(provider_chain) - 1:
                    elapsed_ms = int(round((perf_counter() - started_at) * 1000))
                    updated_plan["editorial_intelligence"] = self._build_failure_record(
                        base_record=base_record,
                        scene_texts=scene_texts,
                        requested_provider=requested_provider,
                        requested_model=requested_model,
                        attempts=attempts,
                        elapsed_ms=elapsed_ms,
                        failover_reason=" | ".join(failover_reasons),
                        error=_normalize_text(exc),
                    )
                    if settings.get("editorial_intelligence_fail_open", True):
                        return {"status": "failed", "plan_updates": updated_plan}
                    raise

        elapsed_ms = int(round((perf_counter() - started_at) * 1000))
        updated_plan["editorial_intelligence"] = self._build_failure_record(
            base_record=base_record,
            scene_texts=scene_texts,
            requested_provider=requested_provider,
            requested_model=requested_model,
            attempts=attempts,
            elapsed_ms=elapsed_ms,
            failover_reason=" | ".join(failover_reasons),
            error="editorial_provider_chain_empty",
        )
        return {"status": "failed", "plan_updates": updated_plan}
