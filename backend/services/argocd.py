import httpx

from ..config import settings


def _headers(token: str | None) -> dict:
    h = {"Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def applications(base_url: str, token: str | None):
    if not base_url:
        return None
    url = base_url.rstrip("/") + "/api/v1/applications"
    async with httpx.AsyncClient(timeout=8.0, verify=settings.argocd_verify_tls) as client:
        response = await client.get(url, headers=_headers(token))
        response.raise_for_status()
        return response.json()


def application_status_sync(name: str) -> dict | None:
    """Blocking lookup of one Argo CD application (used from the pipeline thread).

    Returns {"sync": "Synced", "health": "Healthy", "revision": "..."} or None
    if the application does not exist / Argo CD is unreachable.
    """
    if not settings.argocd_url:
        return None
    url = settings.argocd_url.rstrip("/") + f"/api/v1/applications/{name}"
    try:
        with httpx.Client(timeout=8.0, verify=settings.argocd_verify_tls) as client:
            r = client.get(url, headers=_headers(settings.argocd_token))
        if r.status_code != 200:
            return None
        st = r.json().get("status", {})
        return {
            "sync": (st.get("sync") or {}).get("status"),
            "health": (st.get("health") or {}).get("status"),
            "revision": (st.get("sync") or {}).get("revision"),
        }
    except (httpx.HTTPError, ValueError):
        return None
