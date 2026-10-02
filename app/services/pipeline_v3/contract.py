"""Contrato mínimo de entrada do Pipeline V3."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from .duration import DurationSpec, parse_duration


@dataclass(frozen=True)
class PipelineV3Request:
    title: str
    topic: str
    duration: DurationSpec
    environment: str = "test"
    channel_id: str | None = None
    options: dict[str, Any] | None = None

    @classmethod
    def create(
        cls,
        *,
        title: str,
        topic: str,
        duration: Any,
        environment: str = "test",
        channel_id: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> "PipelineV3Request":
        title = title.strip()
        topic = topic.strip()
        environment = environment.strip().lower()
        if not title or not topic:
            raise ValueError("title e topic são obrigatórios")
        if environment not in {"test", "production"}:
            raise ValueError("environment deve ser test ou production")
        return cls(
            title=title,
            topic=topic,
            duration=parse_duration(duration),
            environment=environment,
            channel_id=channel_id,
            options=dict(options or {}),
        )

    @property
    def idempotency_key(self) -> str:
        payload = {
            "title": self.title,
            "topic": self.topic,
            "duration_seconds": self.duration.seconds,
            "environment": self.environment,
            "channel_id": self.channel_id,
            "options": self.options or {},
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
