"""Proteções locais de recursos para renderizações longas de séries.

Este módulo não altera o pipeline de conteúdo. Ele somente impede que uma
renderização pesada comece quando a máquina não tem margem suficiente para
concluí-la sem comprometer a API, o Coolify ou o banco.
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Dict, Optional


def _env_truthy(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return float(raw) if raw is not None and str(raw).strip() else float(default)
    except Exception:
        return float(default)


def _meminfo_mb(path: str = "/proc/meminfo") -> Dict[str, float]:
    values: Dict[str, float] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, raw = line.partition(":")
                if not raw:
                    continue
                token = raw.strip().split()[0]
                try:
                    values[str(key)] = float(token) / 1024.0
                except Exception:
                    continue
    except Exception:
        return {}
    return values


def capture_resource_snapshot(
    *,
    disk_path: Optional[str] = None,
    meminfo_path: str = "/proc/meminfo",
) -> Dict[str, Any]:
    memory = _meminfo_mb(meminfo_path)
    total_memory_mb = float(memory.get("MemTotal") or 0.0)
    available_memory_mb = float(
        memory.get("MemAvailable")
        or memory.get("MemFree")
        or 0.0
    )
    swap_total_mb = float(memory.get("SwapTotal") or 0.0)
    swap_free_mb = float(memory.get("SwapFree") or 0.0)
    swap_used_percent = 0.0
    if swap_total_mb > 0:
        swap_used_percent = max(
            0.0,
            min(100.0, ((swap_total_mb - swap_free_mb) / swap_total_mb) * 100.0),
        )

    target_path = str(disk_path or ("/data" if os.path.isdir("/data") else "/"))
    try:
        disk = shutil.disk_usage(target_path)
        disk_free_gb = float(disk.free) / (1024.0 ** 3)
        disk_used_percent = (float(disk.used) / float(disk.total) * 100.0) if disk.total else 0.0
    except Exception:
        disk_free_gb = 0.0
        disk_used_percent = 100.0

    try:
        load_1m = float(os.getloadavg()[0])
    except Exception:
        load_1m = 0.0
    cpu_count = max(1, int(os.cpu_count() or 1))

    return {
        "total_memory_mb": round(total_memory_mb, 1),
        "available_memory_mb": round(available_memory_mb, 1),
        "swap_total_mb": round(swap_total_mb, 1),
        "swap_used_percent": round(swap_used_percent, 1),
        "disk_path": target_path,
        "disk_free_gb": round(disk_free_gb, 2),
        "disk_used_percent": round(disk_used_percent, 1),
        "load_1m": round(load_1m, 2),
        "cpu_count": cpu_count,
        "load_per_cpu": round(load_1m / float(cpu_count), 2),
    }


def series_video_resource_requirements(duration_minutes: int) -> Dict[str, Any]:
    try:
        minutes = max(1, min(60, int(duration_minutes or 1)))
    except Exception:
        minutes = 1

    default_total_mb = 6144.0 if minutes >= 8 else 0.0
    default_available_mb = max(1536.0, min(6144.0, 2048.0 + (minutes * 128.0)))
    default_disk_gb = max(8.0, min(30.0, 5.0 + float(minutes)))

    return {
        "duration_minutes": minutes,
        "min_total_memory_mb": _env_float("SERIES_MIN_TOTAL_MEMORY_MB", default_total_mb),
        "min_available_memory_mb": _env_float("SERIES_MIN_AVAILABLE_MEMORY_MB", default_available_mb),
        "min_free_disk_gb": _env_float("SERIES_MIN_FREE_DISK_GB", default_disk_gb),
        "max_swap_used_percent": _env_float("SERIES_MAX_SWAP_USED_PERCENT", 85.0),
        "max_load_per_cpu": _env_float("SERIES_MAX_LOAD_PER_CPU", 3.0),
    }


def evaluate_series_video_resources(
    duration_minutes: int,
    *,
    snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    current = dict(snapshot or capture_resource_snapshot())
    required = series_video_resource_requirements(duration_minutes)
    enabled = _env_truthy("SERIES_RESOURCE_GUARD_ENABLED", True)
    reasons = []

    if enabled:
        total_memory = float(current.get("total_memory_mb") or 0.0)
        available_memory = float(current.get("available_memory_mb") or 0.0)
        disk_free = float(current.get("disk_free_gb") or 0.0)
        swap_used = float(current.get("swap_used_percent") or 0.0)
        load_per_cpu = float(current.get("load_per_cpu") or 0.0)

        min_total = float(required.get("min_total_memory_mb") or 0.0)
        if min_total > 0 and total_memory < min_total:
            reasons.append(
                f"RAM total insuficiente: {total_memory:.0f} MB instalados na máquina; mínimo seguro {min_total:.0f} MB."
            )
        min_available = float(required.get("min_available_memory_mb") or 0.0)
        if available_memory < min_available:
            reasons.append(
                f"Memória disponível insuficiente: {available_memory:.0f} MB; mínimo seguro {min_available:.0f} MB."
            )
        min_disk = float(required.get("min_free_disk_gb") or 0.0)
        if disk_free < min_disk:
            reasons.append(
                f"Espaço livre insuficiente: {disk_free:.1f} GB; mínimo seguro {min_disk:.1f} GB."
            )
        max_swap = float(required.get("max_swap_used_percent") or 100.0)
        if swap_used > max_swap:
            reasons.append(
                f"Swap sob pressão: {swap_used:.1f}% em uso; máximo seguro {max_swap:.1f}%."
            )
        max_load = float(required.get("max_load_per_cpu") or 0.0)
        if max_load > 0 and load_per_cpu > max_load:
            reasons.append(
                f"Carga do servidor elevada: {load_per_cpu:.2f} por CPU; máximo seguro {max_load:.2f}."
            )

    return {
        "enabled": enabled,
        "allowed": bool(not enabled or not reasons),
        "reasons": reasons,
        "snapshot": current,
        "requirements": required,
    }


def resource_guard_message(report: Dict[str, Any]) -> str:
    reasons = report.get("reasons") if isinstance(report, dict) else None
    clean_reasons = [str(item).strip() for item in (reasons or []) if str(item or "").strip()]
    if not clean_reasons:
        return "Recursos do servidor aprovados para iniciar a produção."
    return (
        "Produção aguardando recursos seguros do servidor. "
        + " ".join(clean_reasons)
        + " A série permanece preservada e pode continuar após liberar recursos ou ampliar a VPS."
    )


__all__ = [
    "capture_resource_snapshot",
    "series_video_resource_requirements",
    "evaluate_series_video_resources",
    "resource_guard_message",
]
