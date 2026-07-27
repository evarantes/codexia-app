import json
import os
import re
from typing import Any, Dict, List, Optional

import openai


class CinematicQualityService:
    def __init__(self, ai_service: Any = None):
        self.ai_service = ai_service

    def _compact_text(self, value: Any) -> str:
        return str(value or "").strip()

    def _normalize_provider(self, value: Any) -> str:
        lowered = self._compact_text(value).lower()
        aliases = {
            "openai": "OpenAI",
            "openrouter": "OpenRouter",
            "local": "Local",
            "disabled": "Disabled",
            "off": "Disabled",
        }
        return aliases.get(lowered, self._compact_text(value) or "OpenAI")

    def _ensure_ai_config_loaded(self) -> None:
        loader = getattr(self.ai_service, "_load_config", None)
        if callable(loader):
            try:
                loader()
            except Exception:
                pass

    def _safe_json_loads(self, raw: str) -> Optional[Dict[str, Any]]:
        payload = str(raw or "").strip()
        if not payload:
            return None
        try:
            data = json.loads(payload)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _count_punctuation_issues(self, text: str) -> int:
        score = 0
        if re.search(r"\s+[,.!?;:]", text):
            score += 1
        if re.search(r"([,.!?;:])\1+", text):
            score += 1
        if re.search(r"\s{2,}", text):
            score += 1
        if re.search(r"\b(\w+)\s+\1\b", text, flags=re.IGNORECASE):
            score += 1
        return score

    def _rewrite_locally(self, text: str) -> str:
        cleaned = str(text or "").strip()
        if not cleaned:
            return ""
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
        cleaned = re.sub(r"([,.!?;:]){2,}", r"\1", cleaned)
        cleaned = re.sub(r",\s*,", ", ", cleaned)
        cleaned = re.sub(r"\b(\w+)(\s+\1\b)+", r"\1", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            cleaned = cleaned[0].upper() + cleaned[1:]
        return cleaned

    def _fallback_review(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        scene_texts = [self._compact_text(item) for item in (payload.get("scene_texts") or []) if self._compact_text(item)]
        revised_scene_texts: List[str] = []
        issues: List[Dict[str, Any]] = []
        actions: List[Dict[str, Any]] = []
        correction_count = 0
        for idx, scene_text in enumerate(scene_texts):
            revised = self._rewrite_locally(scene_text)
            revised_scene_texts.append(revised)
            issue_count = self._count_punctuation_issues(scene_text)
            if issue_count:
                issues.append(
                    {
                        "scene_index": idx,
                        "category": "fluency",
                        "problem": "Pontuacao, espacos ou repeticoes reduziram a fluidez da narracao.",
                    }
                )
            if revised != scene_text:
                correction_count += 1
                actions.append(
                    {
                        "scene_index": idx,
                        "category": "grammar",
                        "action": "normalized_text",
                    }
                )
        summary = (
            f"Correcao local aplicada em {correction_count} trecho(s)."
            if correction_count
            else "Nenhuma correcao local foi necessaria."
        )
        return {
            "provider": "local",
            "provider_used": "Local",
            "model_used": "heuristic",
            "summary": summary,
            "corrections_applied": bool(correction_count),
            "critic_report": {
                "provider": "local",
                "provider_used": "Local",
                "model_used": "heuristic",
                "summary": summary,
                "issues": issues,
            },
            "editor_report": {
                "provider": "local",
                "provider_used": "Local",
                "model_used": "heuristic",
                "summary": summary,
                "scene_texts": revised_scene_texts,
                "actions": actions,
            },
        }

    def _extract_response_text(self, response: Any) -> str:
        try:
            content = response.choices[0].message.content
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts: List[str] = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text")
                        if isinstance(text, str) and text.strip():
                            parts.append(text.strip())
                return "\n".join(parts).strip()
        except Exception:
            return ""
        return ""

    def _chat_completion(
        self,
        client: Any,
        *,
        model_id: str,
        payload: Dict[str, Any],
        allow_json_mode: bool,
    ) -> Dict[str, Any]:
        prompt_payload = {
            "title": self._compact_text(payload.get("title")),
            "scene_texts": [self._compact_text(item) for item in (payload.get("scene_texts") or []) if self._compact_text(item)],
        }
        kwargs = {
            "model": model_id,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Voce e um editor editorial senior para narracao de video biblico. "
                        "Corrija ortografia, gramatica, pontuacao, concordancia, repeticoes e fluidez. "
                        "Preserve significado, coerencia biblica, estilo do canal e estrutura narrativa. "
                        "Retorne apenas JSON valido com as chaves: summary, scene_texts, actions, issues."
                    ),
                },
                {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
            ],
            "temperature": 0.1,
            "max_tokens": 4096,
        }
        if allow_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = client.chat.completions.create(**kwargs)
        text = self._extract_response_text(response)
        if not text:
            raise RuntimeError(f"Resposta vazia do modelo {model_id}.")
        reviewed = self._safe_json_loads(text)
        if not reviewed:
            raise RuntimeError(f"Modelo {model_id} nao retornou JSON valido.")
        scene_texts = reviewed.get("scene_texts") if isinstance(reviewed.get("scene_texts"), list) else []
        clean_scene_texts = [self._compact_text(item) for item in scene_texts if self._compact_text(item)]
        if not clean_scene_texts:
            raise RuntimeError(f"Modelo {model_id} nao retornou textos revisados validos.")
        summary = self._compact_text(reviewed.get("summary")) or "Revisao editorial concluida."
        return {
            "summary": summary,
            "scene_texts": clean_scene_texts,
            "actions": reviewed.get("actions") if isinstance(reviewed.get("actions"), list) else [],
            "issues": reviewed.get("issues") if isinstance(reviewed.get("issues"), list) else [],
        }

    def _build_openai_client(self, provider: str, api_key: str) -> Any:
        if provider == "OpenRouter":
            headers = {"HTTP-Referer": "https://codexia.com", "X-Title": "Codexia"}
            try:
                return openai.OpenAI(
                    api_key=api_key,
                    base_url="https://openrouter.ai/api/v1",
                    default_headers=headers,
                    timeout=180.0,
                )
            except TypeError:
                return openai.OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
        try:
            return openai.OpenAI(api_key=api_key, timeout=180.0)
        except TypeError:
            return openai.OpenAI(api_key=api_key)

    def _provider_api_key(self, provider: str) -> str:
        self._ensure_ai_config_loaded()
        if provider == "OpenAI":
            return self._compact_text(getattr(self.ai_service, "api_key", None) or os.getenv("OPENAI_API_KEY"))
        if provider == "OpenRouter":
            return self._compact_text(getattr(self.ai_service, "openrouter_key", None) or os.getenv("OPENROUTER_API_KEY"))
        return ""

    def _review_with_provider(
        self,
        payload: Dict[str, Any],
        *,
        provider: str,
        model_candidates: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        normalized_provider = self._normalize_provider(provider)
        if normalized_provider == "Disabled":
            return self._fallback_review(payload)
        if normalized_provider == "Local":
            return self._fallback_review(payload)

        api_key = self._provider_api_key(normalized_provider)
        if not api_key:
            raise RuntimeError(f"{normalized_provider}_api_key_missing")

        fallback_model = "openai/gpt-4o-mini" if normalized_provider == "OpenRouter" else "gpt-4o-mini"
        candidates = [self._compact_text(item) for item in (model_candidates or []) if self._compact_text(item)]
        if not candidates:
            candidates = [fallback_model]

        client = self._build_openai_client(normalized_provider, api_key)
        errors: List[str] = []
        for model_id in candidates:
            try:
                review = self._chat_completion(
                    client,
                    model_id=model_id,
                    payload=payload,
                    allow_json_mode=True,
                )
                return {
                    "provider": normalized_provider.lower(),
                    "provider_used": normalized_provider,
                    "model_used": model_id,
                    "summary": review.get("summary"),
                    "corrections_applied": True,
                    "critic_report": {
                        "provider": normalized_provider.lower(),
                        "provider_used": normalized_provider,
                        "model_used": model_id,
                        "summary": review.get("summary"),
                        "issues": review.get("issues") or [],
                    },
                    "editor_report": {
                        "provider": normalized_provider.lower(),
                        "provider_used": normalized_provider,
                        "model_used": model_id,
                        "summary": review.get("summary"),
                        "scene_texts": review.get("scene_texts") or [],
                        "actions": review.get("actions") or [],
                    },
                }
            except Exception as exc:
                errors.append(f"{normalized_provider}[{model_id}]: {self._compact_text(exc)}")
        raise RuntimeError(" | ".join(errors) if errors else f"{normalized_provider}_review_failed")

    def review_narration_package(
        self,
        payload: Dict[str, Any],
        provider_choice: Optional[str] = None,
        model_candidates: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        provider = self._normalize_provider(provider_choice)
        return self._review_with_provider(
            payload,
            provider=provider,
            model_candidates=model_candidates,
        )
