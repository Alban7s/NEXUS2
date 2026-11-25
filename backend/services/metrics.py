"""Tiny Prometheus exposition for NEXUS itself (no extra dependency).

Exposes:
  nexus_http_requests_total{method,path,status}
  nexus_http_request_duration_seconds_sum / _count
  nexus_jobs{kind,status}
  nexus_build_info{version}
"""
from __future__ import annotations

import threading
from collections import defaultdict

_lock = threading.Lock()
_requests: dict[tuple[str, str, str], int] = defaultdict(int)
_dur_sum: dict[str, float] = defaultdict(float)
_dur_count: dict[str, int] = defaultdict(int)


def observe(method: str, path: str, status: int, seconds: float) -> None:
    with _lock:
        _requests[(method, path, str(status))] += 1
        _dur_sum[path] += seconds
        _dur_count[path] += 1


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render(jobs: list, version: str) -> str:
    lines = [
        "# HELP nexus_build_info NEXUS version.", "# TYPE nexus_build_info gauge",
        f'nexus_build_info{{version="{_esc(version)}"}} 1',
        "# HELP nexus_http_requests_total HTTP requests handled by NEXUS.",
        "# TYPE nexus_http_requests_total counter",
    ]
    with _lock:
        for (m, p, s), n in sorted(_requests.items()):
            lines.append(f'nexus_http_requests_total{{method="{m}",path="{_esc(p)}",status="{s}"}} {n}')
        lines += ["# HELP nexus_http_request_duration_seconds Request latency.",
                  "# TYPE nexus_http_request_duration_seconds summary"]
        for p in sorted(_dur_sum):
            lines.append(f'nexus_http_request_duration_seconds_sum{{path="{_esc(p)}"}} {_dur_sum[p]:.6f}')
            lines.append(f'nexus_http_request_duration_seconds_count{{path="{_esc(p)}"}} {_dur_count[p]}')
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for j in jobs:
        counts[(j.kind, j.status)] += 1
    lines += ["# HELP nexus_jobs Releases/rollbacks known to NEXUS by status.", "# TYPE nexus_jobs gauge"]
    for (kind, status), n in sorted(counts.items()):
        lines.append(f'nexus_jobs{{kind="{kind}",status="{status}"}} {n}')
    return "\n".join(lines) + "\n"
