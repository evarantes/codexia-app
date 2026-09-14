from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.models import Settings


@dataclass
class DirectorResult:
    plan: Dict[str, Any]
    provider: str
    model: str
    usage: Dict[str, Any]
    estimated_cost_usd: float


class CinematicDirectorError(RuntimeError):
    pass


class CinematicDirector:
    """Claude-first director for the 30-day monetization workflow.

    Primary: Anthropic direct API using Settings.anthropic_api_key / ANTHROPIC_API_KEY.
    Fallback: OpenRouter using the same Claude Sonnet model, when configured.
    No paid call is made unless ``build_plan`` is explicitly invoked.
    """

    ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
    OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings
        self.model = (os.getenv("CLAUDE_DIRECTOR_MODEL") or "claude-sonnet-5").strip()
        self.openrouter_model = (os.getenv("CLAUDE_OPENROUTER_MODEL") or "anthropic/claude-sonnet-5").strip()

    @staticmethod
    def _secret(settings: Optional[Settings], attr: str, env: str) -> str:
        return str(getattr(settings, attr, None) or os.getenv(env) or "").strip()

    def status(self) -> Dict[str, Any]:
        anthropic = bool(self._secret(self.settings, "anthropic_api_key", "ANTHROPIC_API_KEY"))
        openrouter = bool(self._secret(self.settings, "openrouter_api_key", "OPENROUTER_API_KEY"))
        return {
            "available": bool(anthropic or openrouter),
            "anthropic_direct": anthropic,
            "openrouter_fallback": openrouter,
            "model": self.model,
            "fallback_model": self.openrouter_model,
        }

    @staticmethod
    def _extract_json(raw: str) -> Dict[str, Any]:
        text = str(raw or "").strip()
        if not text:
            raise CinematicDirectorError("Claude retornou uma resposta vazia.")
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
                if isinstance(parsed, dict):
                    return parsed
            except Exception as exc:
                raise CinematicDirectorError(f"Claude retornou JSON inválido: {exc}") from exc
        raise CinematicDirectorError("Claude não retornou um plano JSON válido.")

    @staticmethod
    def _profile(content_type: str, duration_minutes: int, budget_brl: float) -> Dict[str, Any]:
        kind = str(content_type or "story").strip().lower()
        duration = max(1, min(30, int(duration_minutes or 10)))
        if kind == "devotional":
            return {
                "kind": "devotional",
                "target_motion_ratio": 0.10,
                "target_scenes": max(16, min(28, duration * 2)),
                "default_budget_brl": min(float(budget_brl or 30), 45.0),
                "goal": "duracao_media_e_recorrencia",
            }
        if kind == "short":
            return {
                "kind": "short",
                "target_motion_ratio": 0.70,
                "target_scenes": 4,
                "default_budget_brl": min(float(budget_brl or 5), 12.0),
                "goal": "aquisicao_e_cliffhanger",
            }
        return {
            "kind": "story",
            "target_motion_ratio": 0.28,
            "target_scenes": max(24, min(42, duration * 3)),
            "default_budget_brl": min(float(budget_brl or 90), 120.0),
            "goal": "ctr_retencao_e_inscritos",
        }

    def _system_prompt(self) -> str:
        return (
            "Você é o Diretor-Chefe do Codexia, especializado em vídeos cristãos/bíblicos de alta retenção para YouTube. "
            "Seu trabalho é dirigir roteiro, ritmo, continuidade visual, cenas e aplicação devocional sem inventar fatos bíblicos. "
            "Priorize clareza, emoção, retenção e originalidade. Evite conteúdo repetitivo, genérico ou produzido em massa. "
            "Para histórias bíblicas, conte a narrativa com fidelidade e depois conecte-a a um problema humano real. "
            "Para devocionais, priorize acolhimento, mensagem e permanência até o fim. "
            "Retorne SOMENTE JSON válido, sem markdown."
        )

    def _user_prompt(self, *, theme: str, content_type: str, duration_minutes: int, budget_brl: float) -> str:
        profile = self._profile(content_type, duration_minutes, budget_brl)
        schema = {
            "content_type": profile["kind"],
            "theme": theme,
            "objective": profile["goal"],
            "duration_minutes": duration_minutes,
            "budget_limit_brl": round(profile["default_budget_brl"], 2),
            "title_options": ["", "", ""],
            "thumbnail_options": [
                {"hook": "", "visual_prompt": "", "overlay_text": ""},
                {"hook": "", "visual_prompt": "", "overlay_text": ""},
                {"hook": "", "visual_prompt": "", "overlay_text": ""},
            ],
            "opening_hook": "",
            "promise": "",
            "biblical_accuracy_notes": [],
            "character_bible": [
                {"name": "", "fixed_visual_description": "", "wardrobe": "", "age": "", "continuity_rules": []}
            ],
            "full_script": "",
            "application_devotional": "",
            "closing_prayer_or_reflection": "",
            "cta_next_video": "",
            "scenes": [
                {
                    "index": 1,
                    "narration": "",
                    "purpose": "hook|story|tension|climax|application|prayer|cta",
                    "tier": "A|B|C",
                    "visual_prompt": "",
                    "motion_prompt": "",
                    "recommended_provider": "kling|veo|runway|still",
                    "target_seconds": 10,
                    "generative_video_seconds": 0,
                    "retention_device": "",
                }
            ],
            "shorts": [
                {"title": "", "hook": "", "script": "", "source_scene_indexes": [1], "cta": ""}
            ],
            "quality_checks": {
                "no_slow_intro": True,
                "character_continuity": True,
                "biblical_consistency": True,
                "originality": True,
                "budget_respected": True,
            },
        }
        return f"""
TEMA: {theme.strip()}
TIPO: {profile['kind']}
DURAÇÃO ALVO: {int(duration_minutes)} minutos
TETO DE CUSTO: R$ {profile['default_budget_brl']:.2f}
PROPORÇÃO MÁXIMA ALVO DE VÍDEO GENERATIVO: {profile['target_motion_ratio']:.0%}
QUANTIDADE ALVO DE CENAS: aproximadamente {profile['target_scenes']}

REGRAS DE DIREÇÃO:
1. O vídeo precisa começar com conteúdo forte nos primeiros 3-5 segundos; nada de vinheta longa antes do conteúdo.
2. Classifique cada cena: A = momento decisivo/clímax; B = movimento útil; C = imagem cinematográfica com parallax/zoom/partículas.
3. Use Kling/Veo prioritariamente só em cenas A; Runway/Veo Lite em B; still em C.
4. A soma de generative_video_seconds deve respeitar a proporção máxima e o orçamento.
5. Cada cena deve ter narração suficiente para sustentar o target_seconds, sem frases artificiais de enchimento.
6. Histórias bíblicas: reservar o final para aplicação à vida atual e reflexão/oração, sem transformar a narrativa em sermão desde o início.
7. Devocionais: use 5-10% de vídeo generativo; o restante deve ser visual bonito e calmo, sem gastos desnecessários.
8. Crie 2 ou 3 Shorts a partir dos melhores momentos do vídeo longo, terminando com cliffhanger/CTA para o vídeo completo.
9. O visual_prompt deve repetir as características fixas do character_bible quando o personagem reaparecer.
10. Não inclua texto dentro das imagens; textos de thumbnail ficam no campo overlay_text.

Estrutura JSON obrigatória (preencha tudo e não inclua campos fora dela):
{json.dumps(schema, ensure_ascii=False)}
""".strip()

    def _call_anthropic(self, api_key: str, system: str, prompt: str) -> Tuple[str, Dict[str, Any]]:
        body = {
            "model": self.model,
            "max_tokens": 18000,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        response = requests.post(
            self.ANTHROPIC_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
            timeout=180,
        )
        if not response.ok:
            raise CinematicDirectorError(f"Anthropic respondeu HTTP {response.status_code}: {response.text[:500]}")
        data = response.json()
        blocks = data.get("content") or []
        text = "\n".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        usage = data.get("usage") or {}
        return text, usage

    def _call_openrouter(self, api_key: str, system: str, prompt: str) -> Tuple[str, Dict[str, Any]]:
        response = requests.post(
            self.OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": os.getenv("BASE_URL", "https://codexia.local"),
                "X-Title": "Codexia Cinematic Director",
            },
            json={
                "model": self.openrouter_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 18000,
            },
            timeout=180,
        )
        if not response.ok:
            raise CinematicDirectorError(f"OpenRouter respondeu HTTP {response.status_code}: {response.text[:500]}")
        data = response.json()
        choices = data.get("choices") or []
        text = str(((choices[0] if choices else {}).get("message") or {}).get("content") or "")
        usage = data.get("usage") or {}
        return text, usage

    @staticmethod
    def _estimate_cost_usd(usage: Dict[str, Any]) -> float:
        # Claude Sonnet 5 current list pricing: $2 / MTok input, $10 / MTok output.
        # Environment overrides let us update the guard without a code release if
        # the provider changes pricing again.
        try:
            input_per_mtok = float(os.getenv("CLAUDE_SONNET5_INPUT_USD_PER_MTOK") or "2")
        except Exception:
            input_per_mtok = 2.0
        try:
            output_per_mtok = float(os.getenv("CLAUDE_SONNET5_OUTPUT_USD_PER_MTOK") or "10")
        except Exception:
            output_per_mtok = 10.0
        inp = float(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        out = float(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        return (inp / 1_000_000.0) * input_per_mtok + (out / 1_000_000.0) * output_per_mtok

    @staticmethod
    def _normalize_plan(plan: Dict[str, Any], *, content_type: str, duration_minutes: int, budget_brl: float) -> Dict[str, Any]:
        profile = CinematicDirector._profile(content_type, duration_minutes, budget_brl)
        scenes = plan.get("scenes") if isinstance(plan.get("scenes"), list) else []
        normalized: List[Dict[str, Any]] = []
        max_motion = max(0, int(round(duration_minutes * 60 * profile["target_motion_ratio"])))
        used_motion = 0
        for i, raw in enumerate(scenes, start=1):
            if not isinstance(raw, dict):
                continue
            tier = str(raw.get("tier") or "C").strip().upper()
            if tier not in {"A", "B", "C"}:
                tier = "C"
            target = max(4, min(45, int(raw.get("target_seconds") or 12)))
            requested_motion = max(0, min(15, int(raw.get("generative_video_seconds") or 0)))
            if tier == "C":
                requested_motion = 0
            allowed_motion = max(0, min(requested_motion, max_motion - used_motion))
            used_motion += allowed_motion
            provider = str(raw.get("recommended_provider") or "still").strip().lower()
            if tier == "C":
                provider = "still"
            elif provider not in {"runway", "veo", "kling"}:
                provider = "kling" if tier == "A" else "runway"
            normalized.append({
                "index": i,
                "narration": str(raw.get("narration") or "").strip(),
                "purpose": str(raw.get("purpose") or "story").strip(),
                "tier": tier,
                "visual_prompt": str(raw.get("visual_prompt") or "").strip(),
                "motion_prompt": str(raw.get("motion_prompt") or "").strip(),
                "recommended_provider": provider,
                "target_seconds": target,
                "generative_video_seconds": allowed_motion,
                "retention_device": str(raw.get("retention_device") or "").strip(),
            })
        plan["scenes"] = normalized
        plan["content_type"] = profile["kind"]
        plan["duration_minutes"] = int(duration_minutes)
        plan["budget_limit_brl"] = round(profile["default_budget_brl"], 2)
        plan["motion_budget_seconds"] = max_motion
        plan["motion_planned_seconds"] = used_motion
        return plan

    def build_plan(self, *, theme: str, content_type: str, duration_minutes: int, budget_brl: float) -> DirectorResult:
        theme = str(theme or "").strip()
        if not theme:
            raise CinematicDirectorError("Informe o tema do vídeo.")
        system = self._system_prompt()
        prompt = self._user_prompt(
            theme=theme,
            content_type=content_type,
            duration_minutes=duration_minutes,
            budget_brl=budget_brl,
        )
        anthropic_key = self._secret(self.settings, "anthropic_api_key", "ANTHROPIC_API_KEY")
        openrouter_key = self._secret(self.settings, "openrouter_api_key", "OPENROUTER_API_KEY")
        errors: List[str] = []
        if anthropic_key:
            try:
                raw, usage = self._call_anthropic(anthropic_key, system, prompt)
                plan = self._normalize_plan(
                    self._extract_json(raw),
                    content_type=content_type,
                    duration_minutes=duration_minutes,
                    budget_brl=budget_brl,
                )
                return DirectorResult(plan, "anthropic", self.model, usage, self._estimate_cost_usd(usage))
            except Exception as exc:
                errors.append(str(exc))
        if openrouter_key:
            try:
                raw, usage = self._call_openrouter(openrouter_key, system, prompt)
                plan = self._normalize_plan(
                    self._extract_json(raw),
                    content_type=content_type,
                    duration_minutes=duration_minutes,
                    budget_brl=budget_brl,
                )
                return DirectorResult(plan, "openrouter", self.openrouter_model, usage, self._estimate_cost_usd(usage))
            except Exception as exc:
                errors.append(str(exc))
        if not anthropic_key and not openrouter_key:
            raise CinematicDirectorError("Claude não está configurado. Cadastre ANTHROPIC_API_KEY ou mantenha OpenRouter como fallback.")
        raise CinematicDirectorError("Falha ao chamar Claude: " + " | ".join(errors)[:900])
