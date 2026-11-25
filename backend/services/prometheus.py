import httpx
from typing import Any, Optional


async def prometheus_query(base_url: str, query: str) -> Optional[dict[str, Any]]:
    if not base_url:
        return None

    url = base_url.rstrip("/") + "/api/v1/query"

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(url, params={"query": query})
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "success":
                return None
            return payload
    except (httpx.HTTPError, ValueError):
        return None


def first_value(payload: Optional[dict[str, Any]]) -> Optional[float]:
    if not payload:
        return None

    result = payload.get("data", {}).get("result", [])
    if not result:
        return None

    value = result[0].get("value")
    if not value or len(value) < 2:
        return None

    try:
        return float(value[1])
    except (TypeError, ValueError):
        return None


async def prometheus_range(base_url: str, query: str, minutes: int = 30, step_s: int = 60):
    """Range query -> list of [unix_ts, float] (first series only), or None."""
    import time
    if not base_url:
        return None
    end = time.time()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(base_url.rstrip("/") + "/api/v1/query_range", params={
                "query": query, "start": end - minutes * 60, "end": end, "step": step_s})
            r.raise_for_status()
            payload = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if payload.get("status") != "success":
        return None
    result = payload.get("data", {}).get("result", [])
    if not result:
        return None
    try:
        return [[float(t), float(v)] for t, v in result[0].get("values", []) if v != "NaN"]
    except (TypeError, ValueError):
        return None


async def prometheus_targets(base_url: str):
    """Active scrape targets with health, or None if Prometheus is unreachable."""
    if not base_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(base_url.rstrip("/") + "/api/v1/targets", params={"state": "active"})
            r.raise_for_status()
            payload = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    return [{
        "job": t.get("labels", {}).get("job"), "instance": t.get("labels", {}).get("instance"),
        "health": t.get("health"), "lastError": (t.get("lastError") or "")[:160],
        "lastScrape": t.get("lastScrape"),
    } for t in payload.get("data", {}).get("activeTargets", [])]
