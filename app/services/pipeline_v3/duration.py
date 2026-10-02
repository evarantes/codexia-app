"""Duração livre para o Pipeline V3.

Aceita segundos, minutos e HH:MM:SS sem impor o catálogo antigo de 8-15 min.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Union

DurationInput = Union[int, float, str]


@dataclass(frozen=True)
class DurationSpec:
    seconds: float

    def __post_init__(self) -> None:
        if self.seconds <= 0:
            raise ValueError("duration_seconds deve ser maior que zero")
        if self.seconds > 24 * 60 * 60:
            raise ValueError("duration_seconds não pode ultrapassar 24 horas")

    @property
    def minutes(self) -> float:
        return self.seconds / 60.0


def parse_duration(value: DurationInput) -> DurationSpec:
    if isinstance(value, bool):
        raise ValueError("duração booleana não é válida")

    if isinstance(value, (int, float)):
        return DurationSpec(float(value))

    text = str(value).strip().lower().replace(",", ".")
    if not text:
        raise ValueError("duração vazia")

    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return DurationSpec(float(text))

    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|sec|secs|seg|segs|m|min|mins|h|hr|hrs)", text)
    if match:
        amount = float(match.group(1))
        unit = match.group(2)
        factor = 1.0 if unit.startswith(("s", "seg")) else 60.0
        if unit.startswith(("h", "hr")):
            factor = 3600.0
        return DurationSpec(amount * factor)

    parts = text.split(":")
    if len(parts) in (2, 3) and all(re.fullmatch(r"\d+(?:\.\d+)?", part) for part in parts):
        numbers = [float(part) for part in parts]
        if len(numbers) == 2:
            minutes, seconds = numbers
            if seconds >= 60:
                raise ValueError("segundos devem ser menores que 60 em MM:SS")
            return DurationSpec(minutes * 60 + seconds)
        hours, minutes, seconds = numbers
        if minutes >= 60 or seconds >= 60:
            raise ValueError("minutos e segundos devem ser menores que 60 em HH:MM:SS")
        return DurationSpec(hours * 3600 + minutes * 60 + seconds)

    raise ValueError(f"duração inválida: {value!r}")
