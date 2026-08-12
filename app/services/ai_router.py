import base64
import hashlib
import io
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import openai
import requests
from PIL import Image
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Settings
from app.services.financial_guardian_service import FinancialContext, FinancialGuardianService


class AICapability:
    TEXT_GENERATION = "TEXT_GENERATION"
    SCRIPT_GENERATION = "SCRIPT_GENERATION"
    EDITORIAL_REVIEW = "EDITORIAL_REVIEW"
    ANALYSIS = "ANALYSIS"
    IMAGE_GENERATION = "IMAGE_GENERATION"
    THUMBNAIL_GENERATION = "THUMBNAIL_GENERATION"
    TRANSCRIPTION = "TRANSCRIPTION"


@dataclass(frozen=True)
class AIPolicy:
    capability: str
    primary_provider: str
    primary_model: Optional[str]
    fallback_enabled: bool
    fallback_provider: Optional[str]
    fallback_model: Optional[str]
    cache_enabled: bool
    estimated_cost: float
    max_cost: Optional[float]
    is_active: bool


def _sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data or b"")
    return h.hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes((value or "").encode("utf-8", errors="ignore"))


def _utcnow() -> datetime:
    return datetime.utcnow()


def _env_flag_enabled(*names: str) -> bool:
    for name in names:
        raw = str(os.getenv(name) or "").strip().lower()
        if raw in {"1", "true", "yes", "sim", "on", "enabled", "enable"}:
            return True
    return False


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _normalize_model_id(value: Optional[str]) -> str:
    return str(value or "").strip()


def _is_openrouter_model_explicit(value: Optional[str]) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return False
    if raw in {"auto", "automatico", "automático", "best", "melhor", "openrouter/auto"}:
        return False
    return True


class AIOperationBlocked(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        provider: Optional[str] = None,
        retryable: bool = False,
        action_required: Optional[str] = None,
        model: Optional[str] = None,
        billing_url: Optional[str] = None,
    ):
        self.code = str(code or "blocked")
        self.provider = str(provider or "").strip().lower() or None
        self.retryable = bool(retryable)
        self.action_required = str(action_required or "").strip() or None
        self.model = str(model or "").strip() or None
        self.billing_url = str(billing_url or "").strip() or None
        super().__init__(str(message or code or "blocked"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "action_required": self.action_required,
            "model": self.model,
            "billing_url": self.billing_url,
        }


class AIOperationInProgress(Exception):
    def __init__(self, operation_id: str):
        self.operation_id = str(operation_id or "")
        super().__init__(f"AI operation in progress: {self.operation_id}")


def classify_openai_image_error(error: Exception, *, model: Optional[str] = None) -> AIOperationBlocked:
    """Converte respostas da Images API em erros seguros e acionáveis.

    A classificação fica no roteador central para que vídeo manual, Séries e
    qualquer outro consumidor parem de repetir chamadas fatais pagas.
    """
    fragments = []
    try:
        fragments.append(str(error or ""))
    except Exception:
        pass
    for attr in ("code", "type", "message", "body", "response"):
        try:
            value = getattr(error, attr, None)
            if value is not None:
                if isinstance(value, (dict, list, tuple)):
                    fragments.append(json.dumps(value, ensure_ascii=False, default=str))
                else:
                    fragments.append(str(value))
        except Exception:
            pass
    status_code = None
    for source in (error, getattr(error, "response", None)):
        try:
            candidate = getattr(source, "status_code", None)
            if candidate is not None:
                status_code = int(candidate)
                break
        except Exception:
            pass
    raw = " | ".join(part.strip() for part in fragments if str(part or "").strip())
    low = raw.lower()
    model_id = _normalize_model_id(model) or "gpt-image-1-mini"
    billing_url = "https://platform.openai.com/settings/organization/billing/overview"

    no_credit_markers = (
        "insufficient_quota", "billing_hard_limit_reached", "billing_not_active",
        "payment_required", "quota exceeded", "out of credits", "credit balance",
        "sem saldo", "not enough credits", "current quota", "billing details",
        "usage limit", "hard limit",
    )
    if status_code == 402 or any(marker in low for marker in no_credit_markers):
        return AIOperationBlocked(
            "OPENAI_NO_CREDIT",
            "OpenAI sem saldo/quota para gerar imagens. Adicione créditos na OpenAI e depois desmarque “OpenAI sem saldo” em Configurações.",
            provider="openai",
            retryable=False,
            action_required="Adicionar créditos na OpenAI e liberar o indicador de saldo nas Configurações.",
            model=model_id,
            billing_url=billing_url,
        )
    if status_code == 401 or any(marker in low for marker in ("invalid_api_key", "incorrect api key", "unauthorized", "authentication")):
        return AIOperationBlocked(
            "OPENAI_AUTH_ERROR",
            "A chave da OpenAI é inválida ou foi revogada. Atualize a OpenAI API Key em Configurações.",
            provider="openai",
            retryable=False,
            action_required="Atualizar a OpenAI API Key.",
            model=model_id,
        )
    if any(marker in low for marker in ("organization must be verified", "verify your organization", "organization verification")):
        return AIOperationBlocked(
            "OPENAI_ORG_VERIFICATION_REQUIRED",
            "A OpenAI exige verificação da organização para usar este modelo de imagem. Conclua a verificação da organização ou selecione outro modelo disponível.",
            provider="openai",
            retryable=False,
            action_required="Verificar a organização na OpenAI ou selecionar outro modelo de imagem.",
            model=model_id,
        )
    if any(marker in low for marker in ("model_not_found", "does not exist", "unsupported model", "not have access to model")):
        return AIOperationBlocked(
            "OPENAI_MODEL_UNAVAILABLE",
            f"O modelo de imagem {model_id} não está disponível para esta conta. Selecione um modelo liberado em Configurações.",
            provider="openai",
            retryable=False,
            action_required="Selecionar um modelo de imagem disponível para a conta.",
            model=model_id,
        )
    if any(marker in low for marker in ("content_policy", "content policy", "safety system", "moderation")):
        return AIOperationBlocked(
            "OPENAI_CONTENT_POLICY",
            "A OpenAI recusou a descrição desta imagem pela política de conteúdo. Ajuste a descrição da cena; nenhuma repetição automática será feita.",
            provider="openai",
            retryable=False,
            action_required="Ajustar a descrição visual da cena.",
            model=model_id,
        )
    if status_code == 429 or any(marker in low for marker in ("rate_limit", "rate limit", "too many requests")):
        return AIOperationBlocked(
            "OPENAI_RATE_LIMIT",
            "A OpenAI atingiu o limite temporário de requisições. Aguarde alguns minutos e reinicie a mesma tarefa.",
            provider="openai",
            retryable=True,
            action_required="Aguardar e reiniciar a mesma tarefa, sem criar uma nova solicitação.",
            model=model_id,
        )
    if status_code is not None and status_code >= 500 or any(marker in low for marker in ("timeout", "timed out", "connection error", "service unavailable")):
        return AIOperationBlocked(
            "OPENAI_TEMPORARY_ERROR",
            "A OpenAI está temporariamente indisponível. Reinicie a mesma tarefa mais tarde; os ativos já prontos serão reaproveitados.",
            provider="openai",
            retryable=True,
            action_required="Aguardar e reiniciar a mesma tarefa.",
            model=model_id,
        )
    return AIOperationBlocked(
        "OPENAI_IMAGE_ERROR",
        "A OpenAI recusou a geração da imagem. Consulte o diagnóstico da tarefa antes de tentar novamente.",
        provider="openai",
        retryable=False,
        action_required="Verificar OpenAI, modelo e faturamento em Configurações.",
        model=model_id,
    )


class AIRouter:
    PARAMETERS_VERSION = "v1"
    _schema_lock = threading.Lock()
    _schema_ensured = False

    def __init__(self):
        self.guardian = FinancialGuardianService()

    def _dry_run_enabled(self) -> bool:
        return _env_flag_enabled("AI_COST_DRY_RUN", "CODEXIA_AI_COST_DRY_RUN")

    def _ensure_schema(self, db: Session) -> None:
        if bool(AIRouter._schema_ensured):
            return
        with AIRouter._schema_lock:
            if bool(AIRouter._schema_ensured):
                return
        db.execute(text(
            """
            CREATE TABLE IF NOT EXISTS ai_capability_policies (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NULL,
                capability VARCHAR(64) NOT NULL,
                primary_provider VARCHAR(64) NOT NULL,
                primary_model VARCHAR(128) NULL,
                fallback_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                fallback_provider VARCHAR(64) NULL,
                fallback_model VARCHAR(128) NULL,
                cache_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                estimated_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
                max_cost DOUBLE PRECISION NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
            )
            """
        ))
        db.execute(text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_capability_policies_user_capability
            ON ai_capability_policies (COALESCE(user_id, 0), capability)
            """
        ))
        db.execute(text(
            """
            CREATE TABLE IF NOT EXISTS ai_operation_cache (
                cache_key VARCHAR(128) PRIMARY KEY,
                capability VARCHAR(64) NOT NULL,
                provider VARCHAR(64) NOT NULL,
                model VARCHAR(128) NOT NULL,
                input_hash VARCHAR(64) NOT NULL,
                parameters_version VARCHAR(32) NOT NULL,
                response_json TEXT NOT NULL,
                created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMP WITHOUT TIME ZONE NULL
            )
            """
        ))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_ai_operation_cache_capability ON ai_operation_cache (capability)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_ai_operation_cache_input_hash ON ai_operation_cache (input_hash)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_ai_operation_cache_expires_at ON ai_operation_cache (expires_at)"))
        db.execute(text(
            """
            CREATE TABLE IF NOT EXISTS ai_operation_runs (
                operation_id VARCHAR(64) PRIMARY KEY,
                user_id INTEGER NULL,
                scope_type VARCHAR(32) NOT NULL DEFAULT 'global',
                scope_id VARCHAR(64) NULL,
                capability VARCHAR(64) NOT NULL,
                provider VARCHAR(64) NOT NULL,
                model VARCHAR(128) NOT NULL,
                input_hash VARCHAR(64) NOT NULL,
                parameters_version VARCHAR(32) NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'running',
                result_json TEXT NULL,
                error_json TEXT NULL,
                estimated_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
                actual_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
                latency_ms INTEGER NULL,
                created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMP WITHOUT TIME ZONE NULL
            )
            """
        ))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_ai_operation_runs_scope ON ai_operation_runs (scope_type, scope_id)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_ai_operation_runs_capability ON ai_operation_runs (capability)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_ai_operation_runs_updated_at ON ai_operation_runs (updated_at)"))
        db.execute(text(
            """
            CREATE TABLE IF NOT EXISTS ai_provider_circuit_breakers (
                provider VARCHAR(64) PRIMARY KEY,
                state VARCHAR(16) NOT NULL DEFAULT 'closed',
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                opened_at TIMESTAMP WITHOUT TIME ZONE NULL,
                last_failure_at TIMESTAMP WITHOUT TIME ZONE NULL,
                last_success_at TIMESTAMP WITHOUT TIME ZONE NULL,
                cooldown_until TIMESTAMP WITHOUT TIME ZONE NULL,
                half_open_remaining INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
            )
            """
        ))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_ai_provider_cb_state ON ai_provider_circuit_breakers (state)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS idx_ai_provider_cb_updated_at ON ai_provider_circuit_breakers (updated_at)"))
        AIRouter._schema_ensured = True

    def _get_settings(self, db: Session, *, user_id: Optional[int]) -> Settings:
        q = db.query(Settings)
        if user_id is not None:
            q = q.filter(Settings.user_id == int(user_id))
        settings = q.order_by(Settings.id.desc()).first()
        if settings is None:
            settings = Settings()
            if user_id is not None:
                settings.user_id = int(user_id)
            db.add(settings)
            db.commit()
            db.refresh(settings)
        return settings

    def _default_policy(self, capability: str, settings: Settings) -> AIPolicy:
        gemini_script = _normalize_model_id(getattr(settings, "gemini_script_model", None)) or "gemini-2.0-flash"
        gemini_text = _normalize_model_id(getattr(settings, "gemini_text_model", None)) or "gemini-2.0-flash"
        gemini_editorial = _normalize_model_id(getattr(settings, "gemini_editorial_model", None)) or "gemini-2.0-flash"
        gemini_analysis = _normalize_model_id(getattr(settings, "gemini_analysis_model", None)) or "gemini-2.0-flash"
        openrouter_model = _normalize_model_id(getattr(settings, "openrouter_model", None)) or "google/gemini-2.5-flash-lite"
        groq_transcription = _normalize_model_id(getattr(settings, "groq_transcription_model", None)) or "whisper-large-v3"
        openai_image_model = _normalize_model_id(getattr(settings, "openai_image_model", None)) or "gpt-image-1-mini"

        defaults: Dict[str, AIPolicy] = {
            AICapability.SCRIPT_GENERATION: AIPolicy(
                capability=AICapability.SCRIPT_GENERATION,
                primary_provider="gemini",
                primary_model=gemini_script,
                fallback_enabled=True,
                fallback_provider="openrouter",
                fallback_model=openrouter_model,
                cache_enabled=True,
                estimated_cost=0.0,
                max_cost=None,
                is_active=True,
            ),
            AICapability.TEXT_GENERATION: AIPolicy(
                capability=AICapability.TEXT_GENERATION,
                primary_provider="gemini",
                primary_model=gemini_text,
                fallback_enabled=True,
                fallback_provider="openrouter",
                fallback_model=openrouter_model,
                cache_enabled=True,
                estimated_cost=0.0,
                max_cost=None,
                is_active=True,
            ),
            AICapability.EDITORIAL_REVIEW: AIPolicy(
                capability=AICapability.EDITORIAL_REVIEW,
                primary_provider="gemini",
                primary_model=gemini_editorial,
                fallback_enabled=True,
                fallback_provider="openrouter",
                fallback_model=openrouter_model,
                cache_enabled=True,
                estimated_cost=0.0,
                max_cost=None,
                is_active=True,
            ),
            AICapability.ANALYSIS: AIPolicy(
                capability=AICapability.ANALYSIS,
                primary_provider="gemini",
                primary_model=gemini_analysis,
                fallback_enabled=True,
                fallback_provider="openrouter",
                fallback_model=openrouter_model,
                cache_enabled=True,
                estimated_cost=0.0,
                max_cost=None,
                is_active=True,
            ),
            AICapability.IMAGE_GENERATION: AIPolicy(
                capability=AICapability.IMAGE_GENERATION,
                primary_provider="openai",
                primary_model=openai_image_model,
                fallback_enabled=False,
                fallback_provider=None,
                fallback_model=None,
                cache_enabled=False,
                estimated_cost=0.005,
                max_cost=None,
                is_active=True,
            ),
            AICapability.THUMBNAIL_GENERATION: AIPolicy(
                capability=AICapability.THUMBNAIL_GENERATION,
                primary_provider="openai",
                primary_model=openai_image_model,
                fallback_enabled=False,
                fallback_provider=None,
                fallback_model=None,
                cache_enabled=False,
                estimated_cost=0.005,
                max_cost=None,
                is_active=True,
            ),
            AICapability.TRANSCRIPTION: AIPolicy(
                capability=AICapability.TRANSCRIPTION,
                primary_provider="groq",
                primary_model=groq_transcription,
                fallback_enabled=False,
                fallback_provider=None,
                fallback_model=None,
                cache_enabled=True,
                estimated_cost=0.0,
                max_cost=None,
                is_active=True,
            ),
        }
        return defaults.get(capability) or defaults[AICapability.TEXT_GENERATION]

    def _load_policy(self, db: Session, *, user_id: Optional[int], capability: str, settings: Settings) -> AIPolicy:
        self._ensure_schema(db)
        row = db.execute(text(
            """
            SELECT capability, primary_provider, primary_model, fallback_enabled, fallback_provider, fallback_model,
                   cache_enabled, estimated_cost, max_cost, is_active
            FROM ai_capability_policies
            WHERE (user_id = :user_id OR (user_id IS NULL AND :user_id IS NULL)) AND capability = :capability
            LIMIT 1
            """
        ), {"user_id": user_id, "capability": str(capability)}).fetchone()
        if row:
            return AIPolicy(
                capability=str(row[0]),
                primary_provider=str(row[1]),
                primary_model=str(row[2]) if row[2] else None,
                fallback_enabled=bool(row[3]),
                fallback_provider=str(row[4]) if row[4] else None,
                fallback_model=str(row[5]) if row[5] else None,
                cache_enabled=bool(row[6]),
                estimated_cost=_safe_float(row[7], 0.0),
                max_cost=_safe_float(row[8], 0.0) if row[8] is not None else None,
                is_active=bool(row[9]),
            )
        return self._default_policy(capability, settings)

    def _openai_capability_allowed(self, settings: Settings, capability: str) -> bool:
        cap = str(capability or "")
        mapping = {
            AICapability.TEXT_GENERATION: "openai_allow_text",
            AICapability.SCRIPT_GENERATION: "openai_allow_script",
            AICapability.EDITORIAL_REVIEW: "openai_allow_editorial",
            AICapability.ANALYSIS: "openai_allow_analysis",
            AICapability.IMAGE_GENERATION: "openai_allow_images",
            AICapability.THUMBNAIL_GENERATION: "openai_allow_thumbnail",
            AICapability.TRANSCRIPTION: "openai_allow_transcription",
        }
        attr = mapping.get(cap)
        if not attr:
            return bool(getattr(settings, "openai_allow_other", False))
        return bool(getattr(settings, attr, False))

    def _record_blocked(self, db: Session, *, user_id: Optional[int], task_id: Optional[str], video_id: Optional[str], capability: str, source: str) -> None:
        self.guardian.ensure_schema(db)
        context = FinancialContext(
            source_type="youtube_auto" if task_id else "ai_router",
            context_id=str(task_id or ""),
            user_id=user_id,
            estimated_cost=0.0,
            actual_cost=0.0,
            metadata={"task_id": task_id, "video_id": video_id},
        )
        self.guardian.record_context_event(
            db,
            context=context,
            event_type="OPENAI_CAPABILITY_BLOCKED",
            stage="ai_router",
            severity="warning",
            details={
                "task_id": task_id,
                "video_id": video_id,
                "capability": capability,
                "source": source,
                "timestamp": _utcnow().isoformat(),
            },
        )

    def _cb_config(self, settings: Settings) -> Dict[str, int]:
        threshold = int(getattr(settings, "ai_cb_failure_threshold", None) or 3)
        cooldown = int(getattr(settings, "ai_cb_cooldown_seconds", None) or 300)
        half_open = int(getattr(settings, "ai_cb_half_open_max_attempts", None) or 1)
        threshold = max(1, threshold)
        cooldown = max(1, cooldown)
        half_open = max(1, half_open)
        return {"threshold": threshold, "cooldown_seconds": cooldown, "half_open_max_attempts": half_open}

    def _cb_row_for_update(self, db: Session, *, provider: str) -> Dict[str, Any]:
        prov = str(provider or "").strip().lower()
        if not prov:
            return {}
        self._ensure_schema(db)
        row = db.execute(text(
            """
            INSERT INTO ai_provider_circuit_breakers (provider, state, consecutive_failures, half_open_remaining, updated_at)
            VALUES (:provider, 'closed', 0, 0, NOW())
            ON CONFLICT (provider) DO UPDATE
            SET updated_at = NOW()
            RETURNING provider, state, consecutive_failures, cooldown_until, half_open_remaining
            """
        ), {"provider": prov}).fetchone()
        if not row:
            return {}
        return {
            "provider": str(row[0]),
            "state": str(row[1] or "closed"),
            "consecutive_failures": int(row[2] or 0),
            "cooldown_until": row[3],
            "half_open_remaining": int(row[4] or 0),
        }

    def _cb_get_row(self, db: Session, *, provider: str) -> Dict[str, Any]:
        prov = str(provider or "").strip().lower()
        if not prov:
            return {}
        self._ensure_schema(db)
        row = db.execute(text(
            """
            SELECT provider, state, consecutive_failures, cooldown_until, half_open_remaining
            FROM ai_provider_circuit_breakers
            WHERE provider = :provider
            """
        ), {"provider": prov}).fetchone()
        if not row:
            return {}
        return {
            "provider": str(row[0]),
            "state": str(row[1] or "closed"),
            "consecutive_failures": int(row[2] or 0),
            "cooldown_until": row[3],
            "half_open_remaining": int(row[4] or 0),
        }

    def _record_cb_event(
        self,
        db: Session,
        *,
        user_id: Optional[int],
        task_id: Optional[str],
        video_id: Optional[str],
        provider: str,
        event_type: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.guardian.ensure_schema(db)
        context = FinancialContext(
            source_type="youtube_auto" if task_id else "ai_router",
            context_id=str(task_id or ""),
            user_id=user_id,
            estimated_cost=0.0,
            actual_cost=0.0,
            metadata={"task_id": task_id, "video_id": video_id},
        )
        payload = dict(details or {})
        payload["provider"] = str(provider or "").strip().lower()
        self.guardian.record_context_event(
            db,
            context=context,
            event_type=str(event_type or "").strip() or "AI_PROVIDER_CIRCUIT_EVENT",
            stage="circuit_breaker",
            severity="warning",
            details=payload,
        )

    def _cb_allow_provider(
        self,
        db: Session,
        *,
        settings: Settings,
        user_id: Optional[int],
        task_id: Optional[str],
        video_id: Optional[str],
        provider: str,
    ) -> bool:
        row = self._cb_get_row(db, provider=provider)
        if not row:
            return True
        config = self._cb_config(settings)
        now = _utcnow()
        state = str(row.get("state") or "closed").lower()
        cooldown_until = row.get("cooldown_until")
        remaining = int(row.get("half_open_remaining") or 0)

        if state == "open":
            if cooldown_until is not None:
                try:
                    if cooldown_until <= now:
                        db.execute(text(
                            """
                            UPDATE ai_provider_circuit_breakers
                            SET state = 'half_open', half_open_remaining = :remaining, updated_at = NOW()
                            WHERE provider = :provider
                            """
                        ), {"provider": row["provider"], "remaining": int(config["half_open_max_attempts"])})
                        self._record_cb_event(
                            db,
                            user_id=user_id,
                            task_id=task_id,
                            video_id=video_id,
                            provider=row["provider"],
                            event_type="AI_PROVIDER_CIRCUIT_HALF_OPEN",
                            details={"cooldown_seconds": int(config["cooldown_seconds"])},
                        )
                        return True
                except Exception:
                    pass
            return False

        if state == "half_open":
            if remaining <= 0:
                db.execute(text(
                    """
                    UPDATE ai_provider_circuit_breakers
                    SET state = 'open',
                        opened_at = COALESCE(opened_at, NOW()),
                        cooldown_until = NOW() + (:cooldown_seconds || ' seconds')::interval,
                        half_open_remaining = 0,
                        updated_at = NOW()
                    WHERE provider = :provider
                    """
                ), {"provider": row["provider"], "cooldown_seconds": int(config["cooldown_seconds"])})
                self._record_cb_event(
                    db,
                    user_id=user_id,
                    task_id=task_id,
                    video_id=video_id,
                    provider=row["provider"],
                    event_type="AI_PROVIDER_CIRCUIT_OPENED",
                    details={"reason": "half_open_no_remaining"},
                )
                return False
            db.execute(text(
                """
                UPDATE ai_provider_circuit_breakers
                SET half_open_remaining = GREATEST(half_open_remaining - 1, 0),
                    updated_at = NOW()
                WHERE provider = :provider
                """
            ), {"provider": row["provider"]})
            return True

        return True

    def _cb_on_success(self, db: Session, *, provider: str) -> None:
        prov = str(provider or "").strip().lower()
        if not prov:
            return
        self._ensure_schema(db)
        if not self._cb_get_row(db, provider=prov):
            return
        db.execute(text(
            """
            UPDATE ai_provider_circuit_breakers
            SET state = 'closed',
                consecutive_failures = 0,
                last_success_at = NOW(),
                cooldown_until = NULL,
                half_open_remaining = 0,
                updated_at = NOW()
            WHERE provider = :provider
            """
        ), {"provider": prov})

    def _cb_on_failure(
        self,
        db: Session,
        *,
        settings: Settings,
        user_id: Optional[int],
        task_id: Optional[str],
        video_id: Optional[str],
        provider: str,
        error_message: str,
    ) -> None:
        prov = str(provider or "").strip().lower()
        if not prov:
            return
        row = self._cb_row_for_update(db, provider=prov)
        if not row:
            return
        config = self._cb_config(settings)
        failures = int(row.get("consecutive_failures") or 0) + 1
        state = str(row.get("state") or "closed").lower()
        should_open = failures >= int(config["threshold"]) or state == "half_open"
        if should_open:
            db.execute(text(
                """
                UPDATE ai_provider_circuit_breakers
                SET state = 'open',
                    consecutive_failures = :failures,
                    opened_at = COALESCE(opened_at, NOW()),
                    last_failure_at = NOW(),
                    cooldown_until = NOW() + (:cooldown_seconds || ' seconds')::interval,
                    half_open_remaining = 0,
                    updated_at = NOW()
                WHERE provider = :provider
                """
            ), {"provider": prov, "failures": failures, "cooldown_seconds": int(config["cooldown_seconds"])})
            self._record_cb_event(
                db,
                user_id=user_id,
                task_id=task_id,
                video_id=video_id,
                provider=prov,
                event_type="AI_PROVIDER_CIRCUIT_OPENED",
                details={"consecutive_failures": failures, "error": str(error_message or "")[:160]},
            )
        else:
            db.execute(text(
                """
                UPDATE ai_provider_circuit_breakers
                SET consecutive_failures = :failures,
                    last_failure_at = NOW(),
                    updated_at = NOW()
                WHERE provider = :provider
                """
            ), {"provider": prov, "failures": failures})

    def _cb_open_immediately(
        self,
        db: Session,
        *,
        settings: Settings,
        user_id: Optional[int],
        task_id: Optional[str],
        video_id: Optional[str],
        provider: str,
        reason_code: str,
        error_message: str,
    ) -> None:
        prov = str(provider or "").strip().lower()
        row = self._cb_row_for_update(db, provider=prov)
        if not row:
            return
        config = self._cb_config(settings)
        failures = max(int(row.get("consecutive_failures") or 0) + 1, int(config["threshold"]))
        db.execute(text(
            """
            UPDATE ai_provider_circuit_breakers
            SET state = 'open',
                consecutive_failures = :failures,
                opened_at = COALESCE(opened_at, NOW()),
                last_failure_at = NOW(),
                cooldown_until = NOW() + (:cooldown_seconds || ' seconds')::interval,
                half_open_remaining = 0,
                updated_at = NOW()
            WHERE provider = :provider
            """
        ), {
            "provider": prov,
            "failures": failures,
            "cooldown_seconds": int(config["cooldown_seconds"]),
        })
        self._record_cb_event(
            db,
            user_id=user_id,
            task_id=task_id,
            video_id=video_id,
            provider=prov,
            event_type="AI_PROVIDER_CIRCUIT_OPENED",
            details={
                "reason_code": str(reason_code or "PROVIDER_FATAL_ERROR"),
                "error": str(error_message or "")[:240],
                "immediate": True,
            },
        )

    def _should_simulate_failure(self, provider: str) -> bool:
        raw = str(os.getenv("AI_ROUTER_SIMULATE_FAILURE_PROVIDERS") or "").strip()
        if not raw:
            return False
        items = {part.strip().lower() for part in raw.split(",") if part.strip()}
        return str(provider or "").strip().lower() in items

    def _cache_key(self, *, provider: str, model: str, capability: str, input_hash: str) -> str:
        raw = f"{provider}|{model}|{capability}|{input_hash}|{self.PARAMETERS_VERSION}"
        return _sha256_text(raw)

    def _operation_id(self, *, scope_type: str, scope_id: Optional[str], cache_key: str) -> str:
        raw = f"{scope_type}|{scope_id or ''}|{cache_key}"
        return _sha256_text(raw)[:64]

    def _get_cached(self, db: Session, *, cache_key: str) -> Optional[Dict[str, Any]]:
        row = db.execute(text(
            """
            SELECT response_json, expires_at
            FROM ai_operation_cache
            WHERE cache_key = :cache_key
            LIMIT 1
            """
        ), {"cache_key": cache_key}).fetchone()
        if not row:
            return None
        expires_at = row[1]
        if expires_at is not None:
            try:
                if expires_at < _utcnow():
                    return None
            except Exception:
                pass
        try:
            return json.loads(row[0])
        except Exception:
            return None

    def _set_cached(self, db: Session, *, cache_key: str, capability: str, provider: str, model: str, input_hash: str, response: Dict[str, Any], ttl_seconds: Optional[int] = None) -> None:
        expires_at = None
        if ttl_seconds is not None and int(ttl_seconds) > 0:
            expires_at = _utcnow() + timedelta(seconds=int(ttl_seconds))
        payload = {
            "cache_key": cache_key,
            "capability": str(capability),
            "provider": str(provider),
            "model": str(model),
            "input_hash": str(input_hash),
            "parameters_version": self.PARAMETERS_VERSION,
            "response_json": json.dumps(response, ensure_ascii=False),
            "expires_at": expires_at,
        }
        db.execute(text(
            """
            INSERT INTO ai_operation_cache (
                cache_key, capability, provider, model, input_hash, parameters_version, response_json, expires_at
            ) VALUES (
                :cache_key, :capability, :provider, :model, :input_hash, :parameters_version, :response_json, :expires_at
            )
            ON CONFLICT (cache_key) DO UPDATE SET
                response_json = EXCLUDED.response_json,
                updated_at = NOW(),
                expires_at = EXCLUDED.expires_at
            """
        ), payload)

    def _claim_operation(self, db: Session, *, operation_id: str, user_id: Optional[int], scope_type: str, scope_id: Optional[str], capability: str, provider: str, model: str, input_hash: str, estimated_cost: float) -> Optional[Dict[str, Any]]:
        now = _utcnow()
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"), {"lock_key": str(operation_id or "")})
        row = db.execute(text(
            """
            SELECT status, result_json, updated_at, completed_at
            FROM ai_operation_runs
            WHERE operation_id = :operation_id
            LIMIT 1
            """
        ), {"operation_id": operation_id}).fetchone()
        if not row:
            db.execute(text(
                """
                INSERT INTO ai_operation_runs (
                    operation_id, user_id, scope_type, scope_id, capability, provider, model, input_hash, parameters_version,
                    status, estimated_cost, actual_cost, created_at, updated_at
                ) VALUES (
                    :operation_id, :user_id, :scope_type, :scope_id, :capability, :provider, :model, :input_hash, :parameters_version,
                    'running', :estimated_cost, 0, :created_at, :updated_at
                )
                """
            ), {
                "operation_id": operation_id,
                "user_id": user_id,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "capability": capability,
                "provider": provider,
                "model": model,
                "input_hash": input_hash,
                "parameters_version": self.PARAMETERS_VERSION,
                "estimated_cost": float(estimated_cost or 0.0),
                "created_at": now,
                "updated_at": now,
            })
            return None
        status = str(row[0] or "")
        if status == "completed" and row[1]:
            try:
                return json.loads(row[1])
            except Exception:
                return None
        if status == "running":
            updated_at = row[2]
            try:
                if updated_at and updated_at < (_utcnow() - timedelta(minutes=20)):
                    db.execute(text(
                        """
                        UPDATE ai_operation_runs
                        SET status = 'running', updated_at = NOW(), error_json = NULL
                        WHERE operation_id = :operation_id
                        """
                    ), {"operation_id": operation_id})
                    return None
            except Exception:
                pass
            raise AIOperationInProgress(operation_id)
        return None

    def _complete_operation(self, db: Session, *, operation_id: str, result: Dict[str, Any], latency_ms: Optional[int], actual_cost: float) -> None:
        db.execute(text(
            """
            UPDATE ai_operation_runs
            SET status = 'completed',
                result_json = :result_json,
                error_json = NULL,
                latency_ms = :latency_ms,
                actual_cost = :actual_cost,
                updated_at = NOW(),
                completed_at = NOW()
            WHERE operation_id = :operation_id
            """
        ), {
            "operation_id": operation_id,
            "result_json": json.dumps(result, ensure_ascii=False),
            "latency_ms": int(latency_ms) if latency_ms is not None else None,
            "actual_cost": float(actual_cost or 0.0),
        })

    def _fail_operation(self, db: Session, *, operation_id: str, error: Dict[str, Any], latency_ms: Optional[int]) -> None:
        db.execute(text(
            """
            UPDATE ai_operation_runs
            SET status = 'failed',
                error_json = :error_json,
                latency_ms = :latency_ms,
                updated_at = NOW(),
                completed_at = NOW()
            WHERE operation_id = :operation_id
            """
        ), {
            "operation_id": operation_id,
            "error_json": json.dumps(error, ensure_ascii=False),
            "latency_ms": int(latency_ms) if latency_ms is not None else None,
        })

    def _record_ai_usage(self, db: Session, *, user_id: Optional[int], task_id: Optional[str], video_id: Optional[str], capability: str, provider: str, model: str, estimated_cost: float, actual_cost: float, latency_ms: Optional[int], cache_hit: bool, operation_id: str) -> None:
        self.guardian.ensure_schema(db)
        context = FinancialContext(
            source_type="youtube_auto" if task_id else "ai_router",
            context_id=str(task_id or ""),
            user_id=user_id,
            estimated_cost=float(estimated_cost or 0.0),
            actual_cost=float(actual_cost or 0.0),
            metadata={"task_id": task_id, "video_id": video_id},
        )
        self.guardian.record_context_event(
            db,
            context=context,
            event_type="AI_OPERATION",
            stage="ai_router",
            severity="info",
            estimated_cost=estimated_cost,
            actual_cost=actual_cost,
            details={
                "operation_id": operation_id,
                "capability": capability,
                "provider": provider,
                "model": model,
                "latency_ms": latency_ms,
                "cache_hit": bool(cache_hit),
            },
        )

    def _call_gemini_text(self, *, api_key: str, model: str, prompt: str, system_prompt: Optional[str], temperature: float, json_mode: bool) -> Tuple[str, Dict[str, Any]]:
        import google.generativeai as genai

        genai.configure(api_key=str(api_key or "").strip())
        effective_prompt = str(prompt or "")
        if system_prompt:
            effective_prompt = f"{system_prompt}\n\n{effective_prompt}"
        if json_mode:
            effective_prompt = f"{effective_prompt}\n\nRetorne APENAS um JSON valido."
        generation_config = {"temperature": float(temperature or 0.0), "max_output_tokens": 4096}
        model_obj = genai.GenerativeModel(model_name=str(model))
        resp = model_obj.generate_content(effective_prompt, generation_config=generation_config)
        text_out = getattr(resp, "text", None)
        if not isinstance(text_out, str) or not text_out.strip():
            raise Exception("Resposta vazia do Gemini.")
        return text_out.strip(), {"provider": "gemini", "model": model}

    def _call_openrouter_text(self, *, api_key: str, model: str, messages: Any, temperature: float, json_mode: bool) -> str:
        headers = {"HTTP-Referer": "https://codexia.com", "X-Title": "Codexia"}
        try:
            client = openai.OpenAI(
                api_key=str(api_key or "").strip(),
                base_url="https://openrouter.ai/api/v1",
                default_headers=headers,
                timeout=180.0,
            )
            kwargs: Dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": 4096,
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            resp = client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content
            if isinstance(content, str) and content.strip():
                return content.strip()
        except Exception:
            pass
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 4096,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {str(api_key or '').strip()}", "Content-Type": "application/json", **headers},
            json=payload,
            timeout=180,
        )
        if int(getattr(r, "status_code", 0) or 0) >= 400:
            body = ""
            try:
                body = (r.text or "")[:600]
            except Exception:
                body = ""
            body = " ".join(str(body).split())
            raise Exception(f"OpenRouter HTTP {r.status_code}: {body}")
        data = r.json() if (r.headers.get("content-type") or "").startswith("application/json") else {}
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise Exception("OpenRouter: resposta sem choices.")
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = msg.get("content") if isinstance(msg, dict) else ""
        content = str(content or "").strip()
        if not content:
            raise Exception("OpenRouter: resposta vazia.")
        return content

    def _call_openai_text(self, *, api_key: str, model: str, messages: Any, temperature: float, json_mode: bool) -> str:
        client = openai.OpenAI(api_key=str(api_key or "").strip(), timeout=180.0)
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 4096,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content
        if isinstance(content, str) and content.strip():
            return content.strip()
        raise Exception("OpenAI: resposta vazia.")

    def _call_openai_image(self, *, api_key: str, model: str, prompt: str) -> bytes:
        try:
            timeout_seconds = float(os.getenv("IMAGE_GEN_TIMEOUT_SECONDS") or 240)
        except Exception:
            timeout_seconds = 240.0
        timeout_seconds = max(30.0, min(900.0, timeout_seconds))
        client = openai.OpenAI(api_key=str(api_key or "").strip(), timeout=timeout_seconds, max_retries=0)
        kwargs: Dict[str, Any] = {"model": model, "prompt": prompt, "size": "1024x1024"}
        if str(model or "").strip().lower().startswith("gpt-image-"):
            quality = str(os.getenv("OPENAI_IMAGE_QUALITY") or "low").strip().lower()
            kwargs["quality"] = quality if quality in {"low", "medium", "high", "auto"} else "low"
        res = client.images.generate(**kwargs)
        item0 = res.data[0] if res and getattr(res, "data", None) else None
        image_base64 = getattr(item0, "b64_json", None) if item0 is not None else None
        image_base64 = (image_base64 or "").strip() if isinstance(image_base64, str) else ""
        image_bytes = b""
        if image_base64:
            image_bytes = base64.b64decode(image_base64)
        else:
            image_url = getattr(item0, "url", None) if item0 is not None else None
            if isinstance(image_url, str) and image_url.strip():
                response = requests.get(image_url.strip(), timeout=120)
                response.raise_for_status()
                image_bytes = bytes(response.content or b"")
        if len(image_bytes) < 1000:
            raise Exception("OpenAI não retornou uma imagem válida.")
        try:
            with Image.open(io.BytesIO(image_bytes)) as generated:
                generated.verify()
        except Exception as exc:
            raise Exception("OpenAI retornou uma imagem corrompida.") from exc
        return image_bytes

    def _call_groq_transcription(self, *, api_key: str, model: str, audio_path: str, language: Optional[str]) -> Dict[str, Any]:
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        data: Dict[str, Any] = {
            "model": str(model or "").strip(),
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["word", "segment"],
        }
        if language:
            data["language"] = str(language)
        with open(audio_path, "rb") as f:
            files = {"file": f}
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {str(api_key or '').strip()}"},
                data=data,
                files=files,
                timeout=240,
            )
        if int(getattr(r, "status_code", 0) or 0) >= 400:
            body = ""
            try:
                body = (r.text or "")[:600]
            except Exception:
                body = ""
            body = " ".join(str(body).split())
            raise Exception(f"Groq HTTP {r.status_code}: {body}")
        return r.json() if (r.headers.get("content-type") or "").startswith("application/json") else {}

    def generate_text(
        self,
        *,
        user_id: Optional[int],
        task_id: Optional[str],
        video_id: Optional[str],
        capability: str,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        json_mode: bool = False,
    ) -> str:
        db = SessionLocal()
        try:
            settings = self._get_settings(db, user_id=user_id)
            policy = self._load_policy(db, user_id=user_id, capability=capability, settings=settings)
            if not policy.is_active:
                raise AIOperationBlocked("POLICY_INACTIVE", f"Capability desativada: {capability}")

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            input_hash = _sha256_text(json.dumps({
                "prompt": prompt,
                "system_prompt": system_prompt,
                "temperature": float(temperature or 0.0),
                "json_mode": bool(json_mode),
            }, ensure_ascii=False, sort_keys=True))

            order: Tuple[Tuple[str, Optional[str]], ...] = ((policy.primary_provider, policy.primary_model),)
            if policy.fallback_enabled and policy.fallback_provider:
                order = order + ((policy.fallback_provider, policy.fallback_model),)

            last_error = None
            for idx, (provider, model) in enumerate(order):
                provider = str(provider or "").strip().lower()
                model_id = _normalize_model_id(model)
                if provider == "openrouter" and not _is_openrouter_model_explicit(model_id):
                    model_id = "google/gemini-2.5-flash-lite"
                if not model_id:
                    model_id = "google/gemini-2.5-flash-lite" if provider == "openrouter" else "gemini-2.0-flash"

                if provider == "openai" and not self._openai_capability_allowed(settings, capability):
                    self._record_blocked(
                        db,
                        user_id=user_id,
                        task_id=task_id,
                        video_id=video_id,
                        capability=capability,
                        source="generate_text",
                    )
                    db.commit()
                    last_error = AIOperationBlocked("OPENAI_CAPABILITY_BLOCKED", "OpenAI bloqueada para esta capability.")
                    if idx + 1 >= len(order):
                        raise last_error
                    continue

                cache_key = self._cache_key(provider=provider, model=model_id, capability=capability, input_hash=input_hash)
                operation_id = self._operation_id(scope_type="youtube_task" if task_id else "global", scope_id=task_id, cache_key=cache_key)

                if policy.cache_enabled:
                    cached = self._get_cached(db, cache_key=cache_key)
                    if cached and isinstance(cached.get("text"), str) and cached["text"].strip():
                        self._record_ai_usage(
                            db,
                            user_id=user_id,
                            task_id=task_id,
                            video_id=video_id,
                            capability=capability,
                            provider=provider,
                            model=model_id,
                            estimated_cost=policy.estimated_cost,
                            actual_cost=0.0,
                            latency_ms=0,
                            cache_hit=True,
                            operation_id=operation_id,
                        )
                        db.commit()
                        return cached["text"]

                if policy.max_cost is not None and policy.estimated_cost > float(policy.max_cost):
                    raise AIOperationBlocked("MAX_COST_BLOCKED", "Custo estimado excede limite por capability.")

                if not self._cb_allow_provider(
                    db,
                    settings=settings,
                    user_id=user_id,
                    task_id=task_id,
                    video_id=video_id,
                    provider=provider,
                ):
                    last_error = AIOperationBlocked("CIRCUIT_OPEN", f"Circuit breaker aberto para provider: {provider}")
                    if idx + 1 >= len(order):
                        raise last_error
                    continue

                reused = self._claim_operation(
                    db,
                    operation_id=operation_id,
                    user_id=user_id,
                    scope_type="youtube_task" if task_id else "global",
                    scope_id=task_id,
                    capability=capability,
                    provider=provider,
                    model=model_id,
                    input_hash=input_hash,
                    estimated_cost=policy.estimated_cost,
                )
                if reused and isinstance(reused.get("text"), str) and reused["text"].strip():
                    self._record_ai_usage(
                        db,
                        user_id=user_id,
                        task_id=task_id,
                        video_id=video_id,
                        capability=capability,
                        provider=provider,
                        model=model_id,
                        estimated_cost=policy.estimated_cost,
                        actual_cost=0.0,
                        latency_ms=0,
                        cache_hit=True,
                        operation_id=operation_id,
                    )
                    db.commit()
                    return reused["text"]

                if self._should_simulate_failure(provider):
                    self._fail_operation(db, operation_id=operation_id, error={"error": "simulated_failure"}, latency_ms=0)
                    self._cb_on_failure(
                        db,
                        settings=settings,
                        user_id=user_id,
                        task_id=task_id,
                        video_id=video_id,
                        provider=provider,
                        error_message="simulated_failure",
                    )
                    db.commit()
                    last_error = Exception("simulated_failure")
                    if idx + 1 >= len(order):
                        raise last_error
                    continue

                if self._dry_run_enabled():
                    dry_text = "{}" if json_mode else "Conteúdo gerado por IA (Dry-Run - Sem custo)"
                    cb_state = self._cb_get_row(db, provider=provider).get("state")
                    if str(cb_state or "").lower() != "closed":
                        self._record_cb_event(
                            db,
                            user_id=user_id,
                            task_id=task_id,
                            video_id=video_id,
                            provider=provider,
                            event_type="AI_PROVIDER_CIRCUIT_CLOSED",
                            details={"previous_state": str(cb_state)},
                        )
                    self._cb_on_success(db, provider=provider)
                    self._complete_operation(db, operation_id=operation_id, result={"text": dry_text}, latency_ms=0, actual_cost=0.0)
                    if policy.cache_enabled:
                        self._set_cached(db, cache_key=cache_key, capability=capability, provider=provider, model=model_id, input_hash=input_hash, response={"text": dry_text}, ttl_seconds=86400)
                    self._record_ai_usage(
                        db,
                        user_id=user_id,
                        task_id=task_id,
                        video_id=video_id,
                        capability=capability,
                        provider=provider,
                        model=model_id,
                        estimated_cost=policy.estimated_cost,
                        actual_cost=0.0,
                        latency_ms=0,
                        cache_hit=False,
                        operation_id=operation_id,
                    )
                    db.commit()
                    return dry_text

                started = time.time()
                try:
                    if provider == "gemini":
                        api_key = str(getattr(settings, "gemini_api_key", "") or os.getenv("GEMINI_API_KEY") or "").strip()
                        if not api_key:
                            raise Exception("Gemini API Key ausente.")
                        text_out, _meta = self._call_gemini_text(
                            api_key=api_key,
                            model=model_id,
                            prompt=prompt,
                            system_prompt=system_prompt,
                            temperature=temperature,
                            json_mode=json_mode,
                        )
                    elif provider == "openrouter":
                        api_key = str(getattr(settings, "openrouter_api_key", "") or os.getenv("OPENROUTER_API_KEY") or "").strip()
                        if not api_key:
                            raise Exception("OpenRouter API Key ausente.")
                        text_out = self._call_openrouter_text(
                            api_key=api_key,
                            model=model_id,
                            messages=messages,
                            temperature=temperature,
                            json_mode=json_mode,
                        )
                    elif provider == "openai":
                        if not self._openai_capability_allowed(settings, capability):
                            self._record_blocked(db, user_id=user_id, task_id=task_id, video_id=video_id, capability=capability, source="generate_text")
                            db.commit()
                            raise AIOperationBlocked("OPENAI_CAPABILITY_BLOCKED", "OpenAI bloqueada para esta capability.")
                        api_key = str(getattr(settings, "openai_api_key", "") or os.getenv("OPENAI_API_KEY") or "").strip()
                        if not api_key:
                            raise Exception("OpenAI API Key ausente.")
                        model_id = model_id or (os.getenv("OPENAI_TEXT_MODEL") or "gpt-4o-mini")
                        text_out = self._call_openai_text(
                            api_key=api_key,
                            model=model_id,
                            messages=messages,
                            temperature=temperature,
                            json_mode=json_mode,
                        )
                    else:
                        raise Exception(f"Provider nao suportado para texto: {provider}")

                    latency_ms = int((time.time() - started) * 1000)
                    cb_state = self._cb_get_row(db, provider=provider).get("state")
                    if str(cb_state or "").lower() != "closed":
                        self._record_cb_event(
                            db,
                            user_id=user_id,
                            task_id=task_id,
                            video_id=video_id,
                            provider=provider,
                            event_type="AI_PROVIDER_CIRCUIT_CLOSED",
                            details={"previous_state": str(cb_state)},
                        )
                    self._cb_on_success(db, provider=provider)
                    result = {"text": str(text_out or "").strip()}
                    self._complete_operation(db, operation_id=operation_id, result=result, latency_ms=latency_ms, actual_cost=0.0)
                    if policy.cache_enabled:
                        self._set_cached(db, cache_key=cache_key, capability=capability, provider=provider, model=model_id, input_hash=input_hash, response=result, ttl_seconds=86400 * 30)
                    self._record_ai_usage(
                        db,
                        user_id=user_id,
                        task_id=task_id,
                        video_id=video_id,
                        capability=capability,
                        provider=provider,
                        model=model_id,
                        estimated_cost=policy.estimated_cost,
                        actual_cost=0.0,
                        latency_ms=latency_ms,
                        cache_hit=False,
                        operation_id=operation_id,
                    )
                    db.commit()
                    return result["text"]
                except Exception as e:
                    latency_ms = int((time.time() - started) * 1000)
                    self._fail_operation(db, operation_id=operation_id, error={"error": str(e)[:300]}, latency_ms=latency_ms)
                    self._cb_on_failure(
                        db,
                        settings=settings,
                        user_id=user_id,
                        task_id=task_id,
                        video_id=video_id,
                        provider=provider,
                        error_message=str(e),
                    )
                    db.commit()
                    last_error = e
                    if idx + 1 >= len(order):
                        raise
                    continue

            if last_error:
                raise last_error
            raise Exception("Nenhum provedor disponivel para texto.")
        finally:
            try:
                db.close()
            except Exception:
                pass

    def ensure_image_provider_ready(
        self,
        *,
        user_id: Optional[int],
        task_id: Optional[str] = None,
        video_id: Optional[str] = None,
        capability: str = AICapability.IMAGE_GENERATION,
    ) -> Dict[str, Any]:
        """Pré-validação local e sem consumo da configuração de imagens."""
        db = SessionLocal()
        try:
            settings = self._get_settings(db, user_id=user_id)
            policy = self._load_policy(db, user_id=user_id, capability=capability, settings=settings)
            provider = str(policy.primary_provider or "").strip().lower()
            model_id = _normalize_model_id(policy.primary_model) or "gpt-image-1-mini"
            if not policy.is_active:
                raise AIOperationBlocked(
                    "POLICY_INACTIVE",
                    "A geração de imagens está desativada nas políticas de IA.",
                    provider=provider or "openai",
                    action_required="Ativar a geração de imagens em Configurações.",
                    model=model_id,
                )
            if provider != "openai":
                raise AIOperationBlocked(
                    "IMAGE_PROVIDER_NOT_CONFIGURED",
                    "O provedor de imagens não está configurado como OpenAI.",
                    provider=provider or None,
                    action_required="Configurar a OpenAI como provedora de imagens.",
                    model=model_id,
                )
            if not self._openai_capability_allowed(settings, capability):
                raise AIOperationBlocked(
                    "OPENAI_CAPABILITY_BLOCKED",
                    "A geração de imagens pela OpenAI está desativada em Configurações.",
                    provider="openai",
                    action_required="Ativar Imagens na política da OpenAI.",
                    model=model_id,
                )
            if bool(getattr(settings, "openai_no_credit", False)):
                raise AIOperationBlocked(
                    "OPENAI_NO_CREDIT",
                    "OpenAI marcada como sem saldo. Adicione créditos e depois desmarque “OpenAI sem saldo” em Configurações.",
                    provider="openai",
                    action_required="Adicionar créditos na OpenAI e liberar o indicador de saldo nas Configurações.",
                    model=model_id,
                    billing_url="https://platform.openai.com/settings/organization/billing/overview",
                )
            api_key = str(getattr(settings, "openai_api_key", "") or os.getenv("OPENAI_API_KEY") or "").strip()
            if not api_key:
                raise AIOperationBlocked(
                    "OPENAI_KEY_MISSING",
                    "A OpenAI API Key não está configurada. Adicione a chave em Configurações antes de gerar o vídeo.",
                    provider="openai",
                    action_required="Adicionar a OpenAI API Key.",
                    model=model_id,
                )
            circuit = self._cb_get_row(db, provider="openai")
            circuit_state = str(circuit.get("state") or "closed").lower()
            cooldown_until = circuit.get("cooldown_until")
            cooldown_active = True
            if cooldown_until is not None:
                try:
                    cooldown_active = cooldown_until > _utcnow()
                except Exception:
                    cooldown_active = True
            if circuit_state == "open" and cooldown_active:
                raise AIOperationBlocked(
                    "OPENAI_CIRCUIT_OPEN",
                    "A OpenAI está temporariamente bloqueada após falhas recentes. Corrija o diagnóstico anterior ou aguarde antes de reiniciar a mesma tarefa.",
                    provider="openai",
                    retryable=True,
                    action_required="Corrigir a causa informada na última tarefa ou aguardar o desbloqueio automático.",
                    model=model_id,
                )
            return {
                "ready": True,
                "provider": "openai",
                "model": model_id,
                "quality": str(os.getenv("OPENAI_IMAGE_QUALITY") or "low").strip().lower() or "low",
                "cost_check": "local_no_charge",
                "circuit_probe": bool(circuit_state == "open" and not cooldown_active),
                "task_id": task_id,
                "video_id": video_id,
            }
        finally:
            try:
                db.close()
            except Exception:
                pass

    def generate_image(
        self,
        *,
        user_id: Optional[int],
        task_id: Optional[str],
        video_id: Optional[str],
        capability: str,
        prompt: str,
        output_dir: str,
        reclaim_missing_completed_file: bool = False,
    ) -> str:
        db = SessionLocal()
        try:
            settings = self._get_settings(db, user_id=user_id)
            policy = self._load_policy(db, user_id=user_id, capability=capability, settings=settings)
            if str(policy.primary_provider or "").strip().lower() != "openai":
                raise AIOperationBlocked("IMAGE_PROVIDER_NOT_CONFIGURED", "Provider de imagem não configurado.", provider="openai")
            if not self._openai_capability_allowed(settings, capability):
                self._record_blocked(db, user_id=user_id, task_id=task_id, video_id=video_id, capability=capability, source="generate_image")
                db.commit()
                raise AIOperationBlocked("OPENAI_CAPABILITY_BLOCKED", "OpenAI bloqueada para esta capability.", provider="openai")
            if bool(getattr(settings, "openai_no_credit", False)):
                raise AIOperationBlocked(
                    "OPENAI_NO_CREDIT",
                    "OpenAI sem saldo/quota para gerar imagens. Adicione créditos e depois desmarque “OpenAI sem saldo” em Configurações.",
                    provider="openai",
                    action_required="Adicionar créditos na OpenAI e liberar o indicador de saldo nas Configurações.",
                    billing_url="https://platform.openai.com/settings/organization/billing/overview",
                )

            raw_prompt = str(prompt or "").strip()
            if not raw_prompt:
                raise Exception("Prompt vazio.")

            input_hash = _sha256_text(json.dumps({"prompt": raw_prompt}, ensure_ascii=False, sort_keys=True))
            model_id = _normalize_model_id(policy.primary_model) or "gpt-image-1-mini"
            cache_key = self._cache_key(provider="openai", model=model_id, capability=capability, input_hash=input_hash)
            operation_id = self._operation_id(scope_type="youtube_task" if task_id else "global", scope_id=task_id, cache_key=cache_key)

            api_key = str(getattr(settings, "openai_api_key", "") or os.getenv("OPENAI_API_KEY") or "").strip()
            if not api_key:
                raise AIOperationBlocked(
                    "OPENAI_KEY_MISSING",
                    "A OpenAI API Key não está configurada. Adicione a chave em Configurações antes de gerar o vídeo.",
                    provider="openai",
                    action_required="Adicionar a OpenAI API Key.",
                    model=model_id,
                )

            if not self._cb_allow_provider(
                db,
                settings=settings,
                user_id=user_id,
                task_id=task_id,
                video_id=video_id,
                provider="openai",
            ):
                raise AIOperationBlocked(
                    "OPENAI_CIRCUIT_OPEN",
                    "A OpenAI está temporariamente bloqueada após falhas recentes. Corrija o diagnóstico anterior ou aguarde antes de reiniciar a mesma tarefa.",
                    provider="openai",
                    retryable=True,
                    action_required="Corrigir a causa anterior ou aguardar o desbloqueio automático.",
                    model=model_id,
                )

            if self._should_simulate_failure("openai"):
                self._fail_operation(db, operation_id=operation_id, error={"error": "simulated_failure"}, latency_ms=0)
                self._cb_on_failure(
                    db,
                    settings=settings,
                    user_id=user_id,
                    task_id=task_id,
                    video_id=video_id,
                    provider="openai",
                    error_message="simulated_failure",
                )
                db.commit()
                raise Exception("simulated_failure")

            reused = self._claim_operation(
                db,
                operation_id=operation_id,
                user_id=user_id,
                scope_type="youtube_task" if task_id else "global",
                scope_id=task_id,
                capability=capability,
                provider="openai",
                model=model_id,
                input_hash=input_hash,
                estimated_cost=policy.estimated_cost,
            )
            if reused and isinstance(reused.get("path"), str) and reused["path"].strip():
                stale_completed_file = bool(
                    reclaim_missing_completed_file
                    and not self._completed_image_result_is_usable(reused)
                )
                if stale_completed_file:
                    # O resultado pertence a um contêiner anterior. A retomada
                    # explícita da série pode reclamar a mesma operação, sob o
                    # mesmo advisory lock, sem criar chamadas concorrentes.
                    db.execute(text(
                        """
                        UPDATE ai_operation_runs
                        SET status = 'running',
                            result_json = NULL,
                            error_json = NULL,
                            updated_at = NOW(),
                            completed_at = NULL
                        WHERE operation_id = :operation_id
                        """
                    ), {"operation_id": operation_id})
                else:
                    self._record_ai_usage(
                        db,
                        user_id=user_id,
                        task_id=task_id,
                        video_id=video_id,
                        capability=capability,
                        provider="openai",
                        model=model_id,
                        estimated_cost=policy.estimated_cost,
                        actual_cost=0.0,
                        latency_ms=0,
                        cache_hit=True,
                        operation_id=operation_id,
                    )
                    db.commit()
                    return reused["path"]

            if self._dry_run_enabled():
                base_dir = Path(output_dir)
                base_dir.mkdir(parents=True, exist_ok=True)
                filename = f"img_{operation_id[:12]}.png"
                out_path = base_dir / filename
                img = Image.new("RGB", (1024, 1024), (18, 18, 18))
                img.save(out_path, format="PNG")
                cb_state = self._cb_get_row(db, provider="openai").get("state")
                if str(cb_state or "").lower() != "closed":
                    self._record_cb_event(
                        db,
                        user_id=user_id,
                        task_id=task_id,
                        video_id=video_id,
                        provider="openai",
                        event_type="AI_PROVIDER_CIRCUIT_CLOSED",
                        details={"previous_state": str(cb_state)},
                    )
                self._cb_on_success(db, provider="openai")
                self._complete_operation(db, operation_id=operation_id, result={"path": str(out_path)}, latency_ms=0, actual_cost=0.0)
                self._record_ai_usage(
                    db,
                    user_id=user_id,
                    task_id=task_id,
                    video_id=video_id,
                    capability=capability,
                    provider="openai",
                    model=model_id,
                    estimated_cost=policy.estimated_cost,
                    actual_cost=0.0,
                    latency_ms=0,
                    cache_hit=False,
                    operation_id=operation_id,
                )
                db.commit()
                return str(out_path)

            base_dir = Path(output_dir)
            base_dir.mkdir(parents=True, exist_ok=True)
            filename = f"img_{operation_id[:12]}_{int(time.time())}.png"
            out_path = base_dir / filename

            started = time.time()
            try:
                image_bytes = self._call_openai_image(api_key=api_key, model=model_id, prompt=raw_prompt)
                out_path.write_bytes(image_bytes)
                latency_ms = int((time.time() - started) * 1000)
                cb_state = self._cb_get_row(db, provider="openai").get("state")
                if str(cb_state or "").lower() != "closed":
                    self._record_cb_event(
                        db,
                        user_id=user_id,
                        task_id=task_id,
                        video_id=video_id,
                        provider="openai",
                        event_type="AI_PROVIDER_CIRCUIT_CLOSED",
                        details={"previous_state": str(cb_state)},
                    )
                self._cb_on_success(db, provider="openai")
                result = {"path": str(out_path)}
                self._complete_operation(db, operation_id=operation_id, result=result, latency_ms=latency_ms, actual_cost=0.0)
                self._record_ai_usage(
                    db,
                    user_id=user_id,
                    task_id=task_id,
                    video_id=video_id,
                    capability=capability,
                    provider="openai",
                    model=model_id,
                    estimated_cost=policy.estimated_cost,
                    actual_cost=0.0,
                    latency_ms=latency_ms,
                    cache_hit=False,
                    operation_id=operation_id,
                )
                db.commit()
                return str(out_path)
            except Exception as e:
                latency_ms = int((time.time() - started) * 1000)
                failure = e if isinstance(e, AIOperationBlocked) else classify_openai_image_error(e, model=model_id)
                failure_payload = failure.to_dict() if isinstance(failure, AIOperationBlocked) else {"error": str(failure)}
                self._fail_operation(db, operation_id=operation_id, error=failure_payload, latency_ms=latency_ms)
                if isinstance(failure, AIOperationBlocked) and failure.code == "OPENAI_NO_CREDIT":
                    settings.openai_no_credit = True
                if isinstance(failure, AIOperationBlocked) and failure.code in {
                    "OPENAI_NO_CREDIT", "OPENAI_AUTH_ERROR", "OPENAI_MODEL_UNAVAILABLE",
                    "OPENAI_ORG_VERIFICATION_REQUIRED", "OPENAI_IMAGE_ERROR"
                }:
                    self._cb_open_immediately(
                        db,
                        settings=settings,
                        user_id=user_id,
                        task_id=task_id,
                        video_id=video_id,
                        provider="openai",
                        reason_code=failure.code,
                        error_message=str(failure),
                    )
                elif not isinstance(failure, AIOperationBlocked) or failure.code != "OPENAI_CONTENT_POLICY":
                    self._cb_on_failure(
                        db,
                        settings=settings,
                        user_id=user_id,
                        task_id=task_id,
                        video_id=video_id,
                        provider="openai",
                        error_message=str(failure),
                    )
                db.commit()
                raise failure
        finally:
            try:
                db.close()
            except Exception:
                pass

    @staticmethod
    def _completed_image_result_is_usable(result: Optional[Dict[str, Any]]) -> bool:
        """Aceita cache de imagem somente quando o arquivo físico ainda existe."""
        if not isinstance(result, dict):
            return False
        path = str(result.get("path") or "").strip()
        if not path:
            return False
        try:
            return os.path.isfile(path) and os.path.getsize(path) >= 1000
        except Exception:
            return False

    def transcribe_audio(
        self,
        *,
        user_id: Optional[int],
        task_id: Optional[str],
        video_id: Optional[str],
        audio_path: str,
        language: Optional[str] = None,
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            if not audio_path or not os.path.exists(audio_path):
                return {"segments": None, "error": "file_not_found"}
            settings = self._get_settings(db, user_id=user_id)
            policy = self._load_policy(db, user_id=user_id, capability=AICapability.TRANSCRIPTION, settings=settings)

            audio_bytes = Path(audio_path).read_bytes()
            input_hash = _sha256_bytes(audio_bytes)
            provider = str(policy.primary_provider or "").strip().lower() or "groq"
            model_id = _normalize_model_id(policy.primary_model) or "whisper-large-v3"
            cache_key = self._cache_key(provider=provider, model=model_id, capability=AICapability.TRANSCRIPTION, input_hash=input_hash)
            operation_id = self._operation_id(scope_type="youtube_task" if task_id else "global", scope_id=task_id, cache_key=cache_key)

            if provider == "openai" and not self._openai_capability_allowed(settings, AICapability.TRANSCRIPTION):
                self._record_blocked(db, user_id=user_id, task_id=task_id, video_id=video_id, capability=AICapability.TRANSCRIPTION, source="transcribe_audio")
                db.commit()
                return {"segments": None, "error": "OPENAI_CAPABILITY_BLOCKED"}

            if policy.cache_enabled:
                cached = self._get_cached(db, cache_key=cache_key)
                cached_segments = cached.get("segments") if isinstance(cached, dict) else None
                if isinstance(cached_segments, list) and cached_segments:
                    self._record_ai_usage(
                        db,
                        user_id=user_id,
                        task_id=task_id,
                        video_id=video_id,
                        capability=AICapability.TRANSCRIPTION,
                        provider=provider,
                        model=model_id,
                        estimated_cost=policy.estimated_cost,
                        actual_cost=0.0,
                        latency_ms=0,
                        cache_hit=True,
                        operation_id=operation_id,
                    )
                    db.commit()
                    return {"segments": cached_segments, "error": None}

            if not self._cb_allow_provider(
                db,
                settings=settings,
                user_id=user_id,
                task_id=task_id,
                video_id=video_id,
                provider=provider,
            ):
                return {"segments": None, "error": "CIRCUIT_OPEN"}

            reused = self._claim_operation(
                db,
                operation_id=operation_id,
                user_id=user_id,
                scope_type="youtube_task" if task_id else "global",
                scope_id=task_id,
                capability=AICapability.TRANSCRIPTION,
                provider=provider,
                model=model_id,
                input_hash=input_hash,
                estimated_cost=policy.estimated_cost,
            )
            reused_segments = reused.get("segments") if isinstance(reused, dict) else None
            if isinstance(reused_segments, list) and reused_segments:
                self._record_ai_usage(
                    db,
                    user_id=user_id,
                    task_id=task_id,
                    video_id=video_id,
                    capability=AICapability.TRANSCRIPTION,
                    provider=provider,
                    model=model_id,
                    estimated_cost=policy.estimated_cost,
                    actual_cost=0.0,
                    latency_ms=0,
                    cache_hit=True,
                    operation_id=operation_id,
                )
                db.commit()
                return {"segments": reused_segments, "error": None}

            if self._should_simulate_failure(provider):
                self._fail_operation(db, operation_id=operation_id, error={"error": "simulated_failure"}, latency_ms=0)
                self._cb_on_failure(
                    db,
                    settings=settings,
                    user_id=user_id,
                    task_id=task_id,
                    video_id=video_id,
                    provider=provider,
                    error_message="simulated_failure",
                )
                db.commit()
                return {"segments": None, "error": "simulated_failure"}

            if self._dry_run_enabled():
                result = {"segments": [], "error": None}
                cb_state = self._cb_get_row(db, provider=provider).get("state")
                if str(cb_state or "").lower() != "closed":
                    self._record_cb_event(
                        db,
                        user_id=user_id,
                        task_id=task_id,
                        video_id=video_id,
                        provider=provider,
                        event_type="AI_PROVIDER_CIRCUIT_CLOSED",
                        details={"previous_state": str(cb_state)},
                    )
                self._cb_on_success(db, provider=provider)
                self._complete_operation(db, operation_id=operation_id, result=result, latency_ms=0, actual_cost=0.0)
                if policy.cache_enabled:
                    self._set_cached(db, cache_key=cache_key, capability=AICapability.TRANSCRIPTION, provider=provider, model=model_id, input_hash=input_hash, response=result, ttl_seconds=86400 * 30)
                self._record_ai_usage(
                    db,
                    user_id=user_id,
                    task_id=task_id,
                    video_id=video_id,
                    capability=AICapability.TRANSCRIPTION,
                    provider=provider,
                    model=model_id,
                    estimated_cost=policy.estimated_cost,
                    actual_cost=0.0,
                    latency_ms=0,
                    cache_hit=False,
                    operation_id=operation_id,
                )
                db.commit()
                return result

            started = time.time()
            try:
                if provider == "groq":
                    api_key = str(getattr(settings, "groq_api_key", "") or os.getenv("GROQ_API_KEY") or "").strip()
                    if not api_key:
                        return {"segments": None, "error": "TRANSCRIPTION_PROVIDER_MISSING"}
                    raw = self._call_groq_transcription(api_key=api_key, model=model_id, audio_path=audio_path, language=language)
                    segments = raw.get("segments") if isinstance(raw, dict) else None
                    if not isinstance(segments, list):
                        return {"segments": None, "error": "no_segments"}
                    result = {"segments": segments, "error": None}
                elif provider == "openai":
                    api_key = str(getattr(settings, "openai_api_key", "") or os.getenv("OPENAI_API_KEY") or "").strip()
                    if not api_key:
                        return {"segments": None, "error": "TRANSCRIPTION_PROVIDER_MISSING"}
                    client = openai.OpenAI(api_key=api_key)
                    with open(audio_path, "rb") as f:
                        kwargs: Dict[str, Any] = {
                            "model": "whisper-1",
                            "file": f,
                            "response_format": "verbose_json",
                            "timestamp_granularities": ["word", "segment"],
                        }
                        if language:
                            kwargs["language"] = language
                        try:
                            raw = client.audio.transcriptions.create(**kwargs)
                        except TypeError:
                            kwargs.pop("timestamp_granularities", None)
                            kwargs.pop("response_format", None)
                            raw = client.audio.transcriptions.create(**kwargs)
                    segments = getattr(raw, "segments", None) if not isinstance(raw, dict) else raw.get("segments")
                    if not isinstance(segments, list):
                        return {"segments": None, "error": "no_segments"}
                    result = {"segments": segments, "error": None}
                else:
                    return {"segments": None, "error": "transcription_provider_not_supported"}

                latency_ms = int((time.time() - started) * 1000)
                cb_state = self._cb_row_for_update(db, provider=provider).get("state")
                if str(cb_state or "").lower() != "closed":
                    self._record_cb_event(
                        db,
                        user_id=user_id,
                        task_id=task_id,
                        video_id=video_id,
                        provider=provider,
                        event_type="AI_PROVIDER_CIRCUIT_CLOSED",
                        details={"previous_state": str(cb_state)},
                    )
                self._cb_on_success(db, provider=provider)
                self._complete_operation(db, operation_id=operation_id, result=result, latency_ms=latency_ms, actual_cost=0.0)
                if policy.cache_enabled and isinstance(result.get("segments"), list):
                    self._set_cached(db, cache_key=cache_key, capability=AICapability.TRANSCRIPTION, provider=provider, model=model_id, input_hash=input_hash, response=result, ttl_seconds=86400 * 90)
                self._record_ai_usage(
                    db,
                    user_id=user_id,
                    task_id=task_id,
                    video_id=video_id,
                    capability=AICapability.TRANSCRIPTION,
                    provider=provider,
                    model=model_id,
                    estimated_cost=policy.estimated_cost,
                    actual_cost=0.0,
                    latency_ms=latency_ms,
                    cache_hit=False,
                    operation_id=operation_id,
                )
                db.commit()
                return result
            except Exception as e:
                latency_ms = int((time.time() - started) * 1000)
                self._fail_operation(db, operation_id=operation_id, error={"error": str(e)[:300]}, latency_ms=latency_ms)
                self._cb_on_failure(
                    db,
                    settings=settings,
                    user_id=user_id,
                    task_id=task_id,
                    video_id=video_id,
                    provider=provider,
                    error_message=str(e),
                )
                db.commit()
                return {"segments": None, "error": str(e)[:240]}
        finally:
            try:
                db.close()
            except Exception:
                pass
