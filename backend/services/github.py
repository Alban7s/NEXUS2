import httpx
from typing import Any


async def workflow_runs(owner: str, repo: str, token: str | None) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.get(url, headers=headers, params={"per_page": 10})
        response.raise_for_status()
        return response.json()
