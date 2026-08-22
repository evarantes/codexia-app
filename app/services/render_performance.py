from __future__ import annotations

import os
from typing import Any, Dict, Optional


DEFAULT_MAX_THREADS = 2
DEFAULT_MIN_AVAILABLE_MB_FOR_TWO_THREADS = 1536.0
DEFAULT_MAX_SWAP_RATIO_FOR_TWO_THREADS = 0.70


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def _read_int(path: str) -> Optional[int]:
    raw = _read_text(path)
    if not raw or raw == "max":
        return None
    try:
        return int(raw)
    except Exception:
        return None


def _proc_meminfo() -> Dict[str, int]:
    values: Dict[str, int] = {}
    raw = _read_text("/proc/meminfo")
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        token = value.strip().split()[0] if value.strip() else "0"
        try:
            values[key.strip()] = int(token) * 1024
        except Exception:
            continue
    return values


def _host_available_bytes() -> Optional[int]:
    info = _proc_meminfo()
    value = int(info.get("MemAvailable") or 0)
    return value if value > 0 else None


def _cgroup_available_bytes() -> Optional[int]:
    # cgroup v2
    limit = _read_int("/sys/fs/cgroup/memory.max")
    current = _read_int("/sys/fs/cgroup/memory.current")
    if limit and current is not None and 0 < limit < (1 << 60):
        return max(0, limit - current)

    # cgroup v1 fallback
    limit = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    current = _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    if limit and current is not None and 0 < limit < (1 << 60):
        return max(0, limit - current)
    return None


def effective_available_memory_mb() -> float:
    candidates = [value for value in (_host_available_bytes(), _cgroup_available_bytes()) if value is not None]
    if not candidates:
        return 0.0
    return round(min(candidates) / (1024.0 * 1024.0), 1)


def swap_usage_ratio() -> float:
    info = _proc_meminfo()
    total = float(info.get("SwapTotal") or 0)
    free = float(info.get("SwapFree") or 0)
    if total <= 0:
        return 0.0
    used = max(0.0, total - free)
    return max(0.0, min(1.0, used / total))


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name) or default).strip())
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name) or default).strip())
    except Exception:
        return float(default)


def ffmpeg_thread_decision(
    *,
    available_mb: Optional[float] = None,
    cpu_count: Optional[int] = None,
    swap_ratio: Optional[float] = None,
    max_threads: Optional[int] = None,
) -> Dict[str, Any]:
    available = float(effective_available_memory_mb() if available_mb is None else available_mb)
    cpus = int((os.cpu_count() or 1) if cpu_count is None else cpu_count)
    swap = float(swap_usage_ratio() if swap_ratio is None else swap_ratio)

    requested_max = int(max_threads if max_threads is not None else _env_int("CODEXIA_FFMPEG_MAX_THREADS", DEFAULT_MAX_THREADS))
    # Primeira versão deliberadamente limitada a 2. Aumentar só depois de benchmark real.
    allowed_max = max(1, min(DEFAULT_MAX_THREADS, requested_max, max(1, cpus)))
    min_available = max(512.0, _env_float(
        "CODEXIA_FFMPEG_2THREAD_MIN_AVAILABLE_MB",
        DEFAULT_MIN_AVAILABLE_MB_FOR_TWO_THREADS,
    ))
    max_swap = max(0.0, min(1.0, _env_float(
        "CODEXIA_FFMPEG_2THREAD_MAX_SWAP_RATIO",
        DEFAULT_MAX_SWAP_RATIO_FOR_TWO_THREADS,
    )))

    reasons = []
    selected = 1
    if allowed_max < 2:
        reasons.append("max_threads_or_cpu_below_2")
    elif available <= 0:
        reasons.append("available_memory_unknown")
    elif available < min_available:
        reasons.append(f"available_memory_below_{int(min_available)}mb")
    elif swap > max_swap:
        reasons.append(f"swap_above_{int(max_swap * 100)}pct")
    else:
        selected = 2
        reasons.append("resources_allow_2_threads")

    return {
        "selected_threads": selected,
        "max_threads": allowed_max,
        "cpu_count": cpus,
        "available_memory_mb": round(available, 1),
        "swap_ratio": round(swap, 4),
        "min_available_mb_for_two_threads": round(min_available, 1),
        "max_swap_ratio_for_two_threads": round(max_swap, 4),
        "reason": reasons[0],
    }


def choose_ffmpeg_threads() -> int:
    decision = ffmpeg_thread_decision()
    try:
        print(
            "[Codexia Render] FFmpeg threads="
            f"{decision['selected_threads']} | CPU={decision['cpu_count']} | "
            f"RAM disponível={decision['available_memory_mb']} MB | "
            f"swap={decision['swap_ratio'] * 100:.1f}% | {decision['reason']}"
        )
    except Exception:
        pass
    return int(decision["selected_threads"])


__all__ = [
    "DEFAULT_MAX_THREADS",
    "effective_available_memory_mb",
    "swap_usage_ratio",
    "ffmpeg_thread_decision",
    "choose_ffmpeg_threads",
]
