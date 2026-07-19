from __future__ import annotations

import os

try:
    import resource
except ImportError:  # pragma: no cover - resource is unavailable on Windows.
    resource = None


RESOURCE_EXHAUSTION_MARKERS = (
    "too many open files",
    "getaddrinfo() thread failed to start",
    "failed to create resolver",
)


def process_fd_snapshot() -> dict[str, int | float]:
    if resource is None or not os.path.isdir("/proc/self/fd"):
        return {}
    try:
        open_fds = len(os.listdir("/proc/self/fd"))
        soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return {}
    if soft_limit in (-1, resource.RLIM_INFINITY) or soft_limit <= 0:
        return {"open_fds": open_fds}
    soft_limit = int(soft_limit)
    pressure_limit = min(4096, max(128, int(soft_limit * 0.7)))
    return {
        "open_fds": open_fds,
        "fd_soft_limit": soft_limit,
        "fd_pressure_limit": pressure_limit,
        "fd_usage_percent": round(open_fds * 100 / soft_limit, 1),
    }


def fd_pressure(snapshot: dict[str, int | float] | None = None) -> bool:
    values = snapshot if snapshot is not None else process_fd_snapshot()
    return bool(
        values
        and int(values.get("fd_pressure_limit") or 0) > 0
        and int(values.get("open_fds") or 0) >= int(values.get("fd_pressure_limit") or 0)
    )


def is_resource_exhaustion_error(error: object) -> bool:
    detail = str(error or "").strip().lower()
    return any(marker in detail for marker in RESOURCE_EXHAUSTION_MARKERS)
