"""
GitHub -> Jenkins -> NEXUS integration (administrator-managed).

How the GitHub path works (one shared pipeline)
-----------------------------------------------
1. The administrator configures, ONCE:
     * the GitHub repository + branch (GITHUB_REPO_URL, GITHUB_BRANCH)
     * a Jenkins job (JENKINS_JOB) whose Git credentials live inside Jenkins
       (so private repositories work without any end-user GitHub login)
     * a GitHub webhook pointing at NEXUS (POST /api/webhooks/github)
2. A push (webhook) or the "Deploy now" button makes NEXUS ask Jenkins to run
   the job.
3. The Jenkins job (jenkins/Jenkinsfile) checks out the code, zips it and
   uploads it to NEXUS `POST /api/deploy` with source=github.
4. From there it is *exactly* the same run_pipeline() as a manual ZIP upload:
   inspect -> test -> build -> scan -> publish -> deploy -> verify.

So there is only ONE build/scan/publish/deploy engine.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

from ..config import settings
from ..errors import PipelineError

_REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}(\.git)?/?$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def validate_repo_url(url: str) -> str:
    if not _REPO_RE.match(url or ""):
        raise PipelineError("Repository must be an https://github.com/<owner>/<repo> URL.", code="bad_repo")
    return url


def validate_branch(branch: str) -> str:
    if not _BRANCH_RE.match(branch or "") or ".." in branch or branch.endswith(("/", ".lock")):
        raise PipelineError("Branch name contains unsupported characters.", code="bad_branch")
    return branch


def validate_commit(sha: str | None) -> str | None:
    if sha is None or sha == "":
        return None
    sha = sha.strip().lower()
    if not _SHA_RE.match(sha):
        raise PipelineError("Commit must be a hex SHA.", code="bad_commit")
    return sha


def verify_github_signature(secret: str | None, body: bytes, header: str | None) -> bool:
    """Validate GitHub's `X-Hub-Signature-256` header (HMAC-SHA256 of the raw body)."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def is_configured() -> bool:
    return bool(settings.jenkins_url and settings.jenkins_user and settings.jenkins_api_token)


def _auth() -> tuple[str, str]:
    return (settings.jenkins_user or "", settings.jenkins_api_token or "")


async def check_connection() -> dict[str, Any]:
    import httpx
    """Real reachability + credential check against Jenkins' JSON API."""
    if not settings.jenkins_url:
        return {"status": "not_configured", "detail": "JENKINS_URL is not set."}
    if not is_configured():
        return {"status": "not_configured", "detail": "Set JENKINS_USER and JENKINS_API_TOKEN."}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(settings.jenkins_url.rstrip("/") + "/api/json?tree=mode,nodeDescription",
                                 auth=_auth())
        if r.status_code in (401, 403):
            return {"status": "not_connected", "detail": "Jenkins rejected the credentials."}
        r.raise_for_status()
        return {"status": "connected", "detail": "Jenkins API responded."}
    except httpx.HTTPError as exc:
        return {"status": "not_connected", "detail": f"Jenkins unreachable: {type(exc).__name__}"}


async def trigger_build(repo_url: str, branch: str, commit: str | None = None) -> dict[str, Any]:
    import httpx
    """Ask Jenkins to run the configured job. Returns the queue location."""
    if not is_configured():
        raise PipelineError(
            "Jenkins is not configured (JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN).",
            code="jenkins_not_configured",
        )
    validate_repo_url(repo_url)
    validate_branch(branch)
    commit = validate_commit(commit)

    url = f"{settings.jenkins_url.rstrip('/')}/job/{settings.jenkins_job}/buildWithParameters"
    params = {"REPO_URL": repo_url, "BRANCH": branch}
    if commit:
        params["COMMIT"] = commit
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, params=params, auth=_auth())
    except httpx.HTTPError as exc:
        raise PipelineError(f"Could not reach Jenkins: {type(exc).__name__}", code="jenkins_unreachable") from exc

    if r.status_code in (401, 403):
        raise PipelineError("Jenkins rejected the NEXUS credentials.", code="jenkins_auth")
    if r.status_code == 404:
        raise PipelineError(f"Jenkins job '{settings.jenkins_job}' was not found.", code="jenkins_no_job")
    if r.status_code not in (200, 201, 202):
        raise PipelineError(f"Jenkins returned HTTP {r.status_code}.", code="jenkins_error")
    return {"queued": True, "queueUrl": r.headers.get("Location"), "job": settings.jenkins_job,
            "repo": repo_url, "branch": branch, "commit": commit}
