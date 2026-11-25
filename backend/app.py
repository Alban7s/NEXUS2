"""
NEXUS API (FastAPI).

Conventions
-----------
* Every JSON endpoint returns the same envelope:
      {"mode": "live|unavailable|error", "source": str, "updatedAt": iso,
       "data": ..., "error": str|null}
  `data` is null when an integration is unavailable - the UI then shows
  "Not connected" instead of inventing numbers.
* If NEXUS_API_KEY is set, every /api route except a few public ones requires
  the `X-Nexus-Api-Key` header. GitHub webhooks use their own HMAC signature.
* Blocking work (Docker, Kubernetes, boto3) always runs in a worker thread so
  the event loop stays responsive.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import platform
import re
import shutil
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .config import settings
from .errors import ArchiveError, PipelineError
from .services import docker_client, jenkins, k8s_ops, metrics, trivy
from .services import kubernetes as k8s_service
from .services.argocd import applications as argocd_applications
from .services.github import workflow_runs
from .services.jobs import job_store
from .services.pipeline import inspect_upload, run_pipeline, slugify
from .services.prometheus import first_value, prometheus_query, prometheus_range, prometheus_targets
from .services.rollback import annotate_known_good, run_rollback

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
ASSETS_DIR = ROOT / "assets"

_tasks: set[asyncio.Task] = set()          # keep references so tasks are not garbage-collected
_job_slots = asyncio.Semaphore(max(1, settings.max_concurrent_jobs))
_webhook_log: deque = deque(maxlen=20)
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")

OPEN_PATHS = {"/api/health", "/api/session", "/api/deploy/config"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    restored = job_store.load()
    if restored:
        print(f"[NEXUS] Restored {restored} jobs from history.")
    if not settings.nexus_api_key:
        print("[NEXUS] WARNING: NEXUS_API_KEY is not set - the API is unauthenticated. "
              "Set it before exposing NEXUS beyond your own machine.")
    yield


app = FastAPI(title="NEXUS API", version=__version__, lifespan=lifespan)


# --------------------------------------------------------------------------
# Middleware: auth, security headers, metrics
# --------------------------------------------------------------------------

def _key_ok(request: Request) -> bool:
    if not settings.nexus_api_key:
        return True
    supplied = request.headers.get("x-nexus-api-key", "")
    return hmac.compare_digest(supplied.encode(), settings.nexus_api_key.encode())


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    started = time.perf_counter()
    protected = (path.startswith("/api/") and path not in OPEN_PATHS
                 and not path.startswith("/api/webhooks/") and request.method != "OPTIONS")
    if protected and not _key_ok(request):
        response = JSONResponse({"detail": "Missing or invalid X-Nexus-Api-Key header.",
                                 "code": "unauthorized"}, status_code=401)
    else:
        response = await call_next(request)

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if not path.startswith("/api/"):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'")
    route = request.scope.get("route")
    label = getattr(route, "path", None) or ("/api/*" if path.startswith("/api/") else "static")
    metrics.observe(request.method, label, response.status_code, time.perf_counter() - started)
    return response


# CORS is OFF unless explicitly configured (same-origin deployment needs none).
# Added after `guard`, so it is the outermost layer and answers preflights first.
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
        allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-Nexus-Api-Key"],
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def response(mode: str, source: str, data: Any = None, error: str | None = None):
    return {"mode": mode, "source": source, "updatedAt": datetime.now(timezone.utc).isoformat(),
            "data": data, "error": error}


def problem(status: int, message: str, code: str = "bad_request") -> JSONResponse:
    return JSONResponse({"detail": message, "code": code}, status_code=status)


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _limited(coro_fn, *args) -> None:
    """Run a pipeline when a build slot is free (protects the 2 vCPU / 8 GB host)."""
    async with _job_slots:
        await coro_fn(*args)


def _offline_kubernetes_snapshot() -> dict[str, Any]:
    return {"clusterStatus": "OFFLINE", "nodes": {"ready": 0, "total": 0},
            "pods": {"running": 0, "pending": 0, "failed": 0, "total": 0},
            "deployments": {"available": 0, "total": 0}, "namespaces": 0, "restarts24h": None}


async def get_kubernetes_snapshot() -> dict[str, Any]:
    try:
        return await asyncio.wait_for(asyncio.to_thread(k8s_service.cluster_snapshot), timeout=6.0)
    except Exception as exc:
        print(f"[NEXUS] Kubernetes check unavailable: {type(exc).__name__}")
        return _offline_kubernetes_snapshot()


async def _http_ok(url: str | None, path: str) -> bool:
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            return (await client.get(url.rstrip("/") + path)).is_success
    except httpx.HTTPError:
        return False


async def _argocd_connected() -> bool:
    if not settings.argocd_url:
        return False
    try:
        await argocd_applications(settings.argocd_url, settings.argocd_token)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Public / session
# --------------------------------------------------------------------------

@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    """Liveness probe for Docker/Kubernetes (NEXUS itself)."""
    return "ok"


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    return PlainTextResponse(metrics.render(job_store.list_recent(500), __version__),
                             media_type="text/plain; version=0.0.4")


@app.get("/api/session")
async def session(request: Request):
    return response("live", "NEXUS", {
        "authRequired": bool(settings.nexus_api_key),
        "authenticated": _key_ok(request),
        "version": __version__,
    })


@app.get("/api/health")
async def health():
    prom_ok = await _http_ok(settings.prometheus_url, "/-/ready")
    snapshot = await get_kubernetes_snapshot()
    docker_info = await asyncio.to_thread(docker_client.diagnose)
    checks = {
        "docker": {"connected": docker_info["connected"], "code": docker_info["code"]},
        "kubernetes": {"connected": snapshot["clusterStatus"] in ("HEALTHY", "DEGRADED"),
                       "status": snapshot["clusterStatus"]},
        "prometheus": {"connected": prom_ok, **({} if settings.prometheus_url else {"error": "PROMETHEUS_URL not configured"})},
        "github": {"configured": bool(settings.github_repo_url or (settings.github_owner and settings.github_repo))},
        "argocd": {"configured": bool(settings.argocd_url)},
    }
    connected = sum(1 for c in checks.values() if c.get("connected") is True)
    probes = [c for c in checks.values() if "connected" in c]
    status = "CONNECTED" if connected == len(probes) else "DEGRADED" if connected else "NOT CONNECTED"
    return response("live", "NEXUS integration health", {"status": status, "checks": checks})


# --------------------------------------------------------------------------
# Integrations (Settings view)
# --------------------------------------------------------------------------

@app.get("/api/docker/status")
async def docker_status():
    info = await asyncio.to_thread(docker_client.diagnose)
    return response("live" if info["connected"] else "unavailable", "Docker Engine", info,
                    None if info["connected"] else info["message"])


@app.get("/api/integrations")
async def integrations():
    docker_info, snapshot, prom_ok, graf_ok, argo_ok, jenkins_info = await asyncio.gather(
        asyncio.to_thread(docker_client.diagnose),
        get_kubernetes_snapshot(),
        _http_ok(settings.prometheus_url, "/-/ready"),
        _http_ok(settings.grafana_url, "/api/health"),
        _argocd_connected(),
        jenkins.check_connection(),
    )
    k8s_ok = snapshot.get("clusterStatus") in ("HEALTHY", "DEGRADED")
    trivy_ver = await asyncio.to_thread(trivy.trivy_version)

    def st(ok: bool, url: str | None) -> str:
        return "connected" if ok else "not_configured" if not url else "not_connected"

    items = [
        {"key": "zip", "name": "ZIP release pipeline", "status": "available",
         "detail": "Upload API, archive validation and the shared build/scan/deploy pipeline are built in."},
        {"key": "docker", "name": "Docker Engine", "status": "connected" if docker_info["connected"] else "not_connected",
         "detail": docker_info["message"] + ("" if docker_info["connected"] else " " + " ".join(docker_info["hints"][:2])),
         "meta": {"code": docker_info["code"], "host": docker_info.get("host")}},
        {"key": "jenkins", "name": "Jenkins", "status": jenkins_info["status"], "detail": jenkins_info["detail"]},
        {"key": "github", "name": "GitHub webhook",
         "status": "configured" if (settings.github_repo_url and settings.github_webhook_secret) else "not_configured",
         "detail": "Repository and webhook secret are set." if (settings.github_repo_url and settings.github_webhook_secret)
         else "Set GITHUB_REPO_URL and GITHUB_WEBHOOK_SECRET (admin-managed; no per-user login)."},
        {"key": "kubernetes", "name": "Kubernetes", "status": "connected" if k8s_ok else "not_connected",
         "detail": "Cluster API is reachable." if k8s_ok else "No reachable Kubernetes cluster was detected by the backend."},
        {"key": "trivy", "name": "Trivy scanner", "status": "installed" if trivy_ver else "not_installed",
         "detail": f"Trivy {trivy_ver} found. Gate: fail on {settings.trivy_fail_on}." if trivy_ver
         else "Trivy is not installed on the NEXUS host; scan stages will be marked skipped."},
        {"key": "registry", "name": "Container registry",
         "status": "configured" if settings.registry_configured else "not_configured",
         "detail": "A registry is configured; pushing also needs valid credentials." if settings.registry_configured
         else "Set ECR_REPOSITORY_PREFIX or REGISTRY_URL to publish images."},
        {"key": "prometheus", "name": "Prometheus", "status": st(prom_ok, settings.prometheus_url),
         "detail": "Prometheus readiness endpoint responded." if prom_ok else "Set PROMETHEUS_URL and make it reachable from NEXUS."},
        {"key": "grafana", "name": "Grafana", "status": st(graf_ok, settings.grafana_url),
         "detail": "Grafana health endpoint responded." if graf_ok else "Set GRAFANA_URL (see monitoring/)."},
        {"key": "argocd", "name": "Argo CD", "status": st(argo_ok, settings.argocd_url),
         "detail": "Argo CD applications API responded." if argo_ok else "Optional. Configure ARGOCD_URL and ARGOCD_TOKEN."},
        {"key": "terraform", "name": "Terraform", "status": "external",
         "detail": "Provided as files in terraform/. You run it from your terminal; NEXUS does not execute it."},
        {"key": "ansible", "name": "Ansible", "status": "external",
         "detail": "Provided as files in ansible/. You run it from your terminal; NEXUS does not execute it."},
    ]
    return response("live", "NEXUS integration inventory", {"items": items})


@app.get("/api/config")
async def public_config():
    """Non-secret effective configuration (for the Settings view)."""
    return response("live", "NEXUS configuration", {
        "version": __version__, "authRequired": bool(settings.nexus_api_key),
        "limits": {"maxUploadMb": settings.max_upload_mb, "maxUncompressedMb": settings.max_uncompressed_mb,
                   "maxFiles": settings.max_files, "maxCompressionRatio": settings.max_compression_ratio},
        "build": {"npmIgnoreScripts": settings.npm_ignore_scripts, "runTests": settings.run_tests,
                  "buildMemoryMb": settings.build_memory_mb, "maxConcurrentJobs": settings.max_concurrent_jobs},
        "security": {"trivyEnabled": settings.trivy_enabled, "failOn": settings.trivy_fail_on,
                     "ignoreUnfixed": settings.trivy_ignore_unfixed},
        "kubernetes": {"namespace": settings.k8s_namespace, "imageSource": settings.k8s_image_source,
                       "serviceType": settings.k8s_service_type},
        "corsOrigins": list(settings.cors_origins),
    })


# --------------------------------------------------------------------------
# Deployment (shared pipeline entry point)
# --------------------------------------------------------------------------

@app.get("/api/deploy/config")
async def deploy_config():
    if settings.git_repo_url and settings.argocd_url:
        target = {"mode": "gitops", "label": "GitOps / Argo CD target configured",
                  "detail": "NEXUS commits manifests; Argo CD performs the cluster rollout."}
    elif settings.registry_configured or settings.k8s_image_source == "minikube":
        target = {"mode": "direct-candidate", "label": "Kubernetes direct target configured",
                  "detail": "NEXUS deploys to Kubernetes if the cluster is reachable; otherwise it uses a local Docker preview."}
    else:
        target = {"mode": "local", "label": "Local preview target",
                  "detail": "No registry/cluster configured. NEXUS builds the image and runs a local Docker preview."}
    target["requiresApiKey"] = bool(settings.nexus_api_key)
    target["maxUploadMb"] = settings.max_upload_mb
    return response("live", "NEXUS deploy configuration", target)


@app.post("/api/deploy")
async def deploy(
    file: UploadFile = File(...),
    app_name: str | None = Form(None),
    source: str = Form("zip"),
    git_repo: str | None = Form(None),
    git_branch: str | None = Form(None),
    git_commit: str | None = Form(None),
):
    """Upload a project ZIP and start the shared release pipeline."""
    if not file.filename or not file.filename.lower().endswith(".zip"):
        return problem(400, "Upload must be a .zip file.", "bad_type")
    if source not in ("zip", "github"):
        return problem(400, "source must be 'zip' or 'github'.", "bad_source")

    git_info = None
    if source == "github":
        try:
            git_info = {"repo": jenkins.validate_repo_url(git_repo or ""),
                        "branch": jenkins.validate_branch(git_branch or settings.github_branch),
                        "commit": jenkins.validate_commit(git_commit)}
        except PipelineError as exc:
            return problem(400, str(exc), exc.code)

    work_root = Path(settings.work_dir)
    work_root.mkdir(parents=True, exist_ok=True)
    upload_path = work_root / f"upload-{uuid.uuid4().hex}.zip"
    limit = settings.max_upload_bytes
    size = 0
    try:
        with open(upload_path, "wb") as fh:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise ArchiveError(f"Upload exceeds the {settings.max_upload_mb} MB limit.", code="too_large")
                fh.write(chunk)
        inspection = await asyncio.to_thread(inspect_upload, upload_path, file.filename)
    except ArchiveError as exc:
        upload_path.unlink(missing_ok=True)
        return problem(413 if exc.code == "too_large" else 400, str(exc), exc.code)
    except PipelineError as exc:
        upload_path.unlink(missing_ok=True)
        return problem(400, str(exc), exc.code)
    except Exception as exc:  # never crash the server on a hostile upload
        upload_path.unlink(missing_ok=True)
        print(f"[NEXUS] Upload handling error: {type(exc).__name__}: {exc}")
        return problem(500, "The upload could not be processed.", "internal")

    name = slugify(app_name) if app_name and app_name.strip() else inspection["suggestedName"]
    job = job_store.create(app_name=name, project=inspection, source=source)
    if git_info:
        job_store.update(job.id, git=git_info)
    spawn(_limited(run_pipeline, job, upload_path))
    return response("live", "NEXUS deploy pipeline", {"jobId": job.id, "app": name, "project": inspection})


@app.get("/api/deploy/jobs")
async def list_jobs(kind: str | None = None, limit: int = 50):
    jobs = [j.to_summary() for j in job_store.list_recent(max(1, min(limit, 200)), kind)]
    return response("live", "NEXUS deploy pipeline", jobs)


@app.get("/api/deploy/jobs/{job_id}")
async def get_job(job_id: str, since: int = 0):
    job = job_store.get(job_id)
    if not job:
        return problem(404, "Job not found.", "not_found")
    return response("live", "NEXUS deploy pipeline", job.to_detail(since=max(0, since)))


@app.get("/api/deploy/jobs/{job_id}/stream")
async def stream_job(job_id: str):
    """Server-sent events: step changes and log lines as they happen."""
    if not job_store.get(job_id):
        return problem(404, "Job not found.", "not_found")

    async def event_source():
        sent = 0
        while True:
            job = job_store.get(job_id)
            if not job:
                break
            detail = job.to_detail(since=sent)
            sent += len(detail["logs"])
            yield f"data: {json.dumps(detail)}\n\n"
            if job.status in ("success", "failed"):
                break
            await job_store.wait(job_id, timeout=10.0)

    return StreamingResponse(event_source(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/deployments")
async def deployment_history():
    """Deployment + rollback history recorded by NEXUS."""
    jobs = [j.to_summary() for j in job_store.list_recent(100)]
    return response("live", "NEXUS release history", jobs)


# --------------------------------------------------------------------------
# GitHub -> Jenkins -> NEXUS
# --------------------------------------------------------------------------

class GithubDeployRequest(BaseModel):
    branch: str | None = None
    commit: str | None = None


@app.get("/api/github/config")
async def github_config():
    jenkins_info = await jenkins.check_connection()
    return response("live", "GitHub / Jenkins configuration", {
        "repoUrl": settings.github_repo_url, "branch": settings.github_branch,
        "repoConfigured": bool(settings.github_repo_url),
        "webhookSecretConfigured": bool(settings.github_webhook_secret),
        "webhookPath": "/api/webhooks/github",
        "jenkins": {**jenkins_info, "job": settings.jenkins_job},
        "recentDeliveries": list(_webhook_log),
    })


@app.post("/api/github/deploy")
async def github_deploy(body: GithubDeployRequest):
    """Manual 'Deploy now': ask Jenkins to build the configured repository."""
    if not settings.github_repo_url:
        return problem(409, "GITHUB_REPO_URL is not configured by the administrator.", "not_configured")
    try:
        result = await jenkins.trigger_build(settings.github_repo_url, body.branch or settings.github_branch,
                                             body.commit)
    except PipelineError as exc:
        return problem(502 if exc.code.startswith("jenkins_") and exc.code != "jenkins_not_configured" else 409,
                       str(exc), exc.code)
    _webhook_log.appendleft({"at": time.time(), "event": "manual", "branch": result["branch"], "result": "queued"})
    return response("live", "Jenkins", result)


@app.post("/api/webhooks/github")
async def github_webhook(request: Request):
    body = await request.body()
    if len(body) > 5 * 1024 * 1024:
        return problem(413, "Payload too large.", "too_large")
    if not settings.github_webhook_secret:
        return problem(503, "Webhook secret is not configured (GITHUB_WEBHOOK_SECRET).", "not_configured")
    if not jenkins.verify_github_signature(settings.github_webhook_secret, body,
                                           request.headers.get("x-hub-signature-256")):
        _webhook_log.appendleft({"at": time.time(), "event": "invalid", "branch": None, "result": "bad signature"})
        return problem(401, "Invalid webhook signature.", "bad_signature")

    event = request.headers.get("x-github-event", "")
    if event == "ping":
        _webhook_log.appendleft({"at": time.time(), "event": "ping", "branch": None, "result": "pong"})
        return response("live", "GitHub webhook", {"pong": True})
    if event != "push":
        return response("live", "GitHub webhook", {"ignored": f"event '{event}'"})

    try:
        payload = json.loads(body)
    except ValueError:
        return problem(400, "Body is not valid JSON.", "bad_json")

    ref = str(payload.get("ref", ""))
    branch = ref.removeprefix("refs/heads/")
    full_name = str((payload.get("repository") or {}).get("full_name", "")).lower()
    configured = (settings.github_repo_url or "").lower().removesuffix(".git").rstrip("/")
    if not configured.endswith("/" + full_name) or not full_name:
        _webhook_log.appendleft({"at": time.time(), "event": "push", "branch": branch, "result": "wrong repository"})
        return response("live", "GitHub webhook", {"ignored": "repository does not match configuration"})
    if branch != settings.github_branch:
        _webhook_log.appendleft({"at": time.time(), "event": "push", "branch": branch, "result": "ignored branch"})
        return response("live", "GitHub webhook", {"ignored": f"branch '{branch}' is not '{settings.github_branch}'"})

    commit = (payload.get("after") or "")[:40]
    try:
        result = await jenkins.trigger_build(settings.github_repo_url or "", branch,
                                             commit if jenkins._SHA_RE.match(commit) else None)
    except PipelineError as exc:
        _webhook_log.appendleft({"at": time.time(), "event": "push", "branch": branch, "result": f"failed: {exc.code}"})
        return problem(502, str(exc), exc.code)
    _webhook_log.appendleft({"at": time.time(), "event": "push", "branch": branch, "result": "queued"})
    return JSONResponse(response("live", "Jenkins", result), status_code=202)


# --------------------------------------------------------------------------
# Kubernetes + rollback
# --------------------------------------------------------------------------

@app.get("/api/kubernetes")
async def kubernetes():
    snapshot = await get_kubernetes_snapshot()
    if snapshot["clusterStatus"] == "OFFLINE":
        return response("unavailable", "Kubernetes API", snapshot, "Kubernetes cluster is unavailable or timed out.")
    return response("live", "Kubernetes API", snapshot)


def _workloads(namespace: str):
    core, apps = k8s_service.load_clients()
    return k8s_ops.workload_overview(core, apps, namespace)


@app.get("/api/k8s/workloads")
async def k8s_workloads():
    ns = settings.k8s_namespace
    try:
        data = await asyncio.wait_for(asyncio.to_thread(_workloads, ns), timeout=10.0)
        return response("live", "Kubernetes API", data)
    except Exception as exc:
        return response("unavailable", "Kubernetes API", None, f"Cluster unavailable ({type(exc).__name__}).")


def _revisions(name: str, ns: str):
    _core, apps = k8s_service.load_clients()
    return annotate_known_good(k8s_ops.list_revisions(apps, name, ns), name)


@app.get("/api/k8s/deployments/{name}/revisions")
async def k8s_revisions(name: str):
    if not _DNS_LABEL.match(name):
        return problem(400, "Invalid deployment name.", "bad_name")
    try:
        data = await asyncio.wait_for(asyncio.to_thread(_revisions, name, settings.k8s_namespace), timeout=10.0)
        return response("live", "Kubernetes API", data)
    except Exception as exc:
        return response("unavailable", "Kubernetes API", None, f"Cannot read revisions ({type(exc).__name__}).")


class RollbackRequest(BaseModel):
    revision: int
    namespace: str | None = None


@app.post("/api/k8s/deployments/{name}/rollback")
async def k8s_rollback(name: str, body: RollbackRequest):
    if not _DNS_LABEL.match(name):
        return problem(400, "Invalid deployment name.", "bad_name")
    ns = body.namespace or settings.k8s_namespace
    if ns != settings.k8s_namespace:
        return problem(400, f"Rollbacks are only allowed in the configured namespace '{settings.k8s_namespace}'.",
                       "bad_namespace")
    if body.revision < 1:
        return problem(400, "revision must be a positive integer.", "bad_revision")
    job = job_store.create(app_name=name, kind="rollback", source="rollback")
    job_store.update(job.id, mode="direct")
    spawn(_limited(run_rollback, job, name, ns, body.revision))
    return response("live", "NEXUS rollback", {"jobId": job.id, "app": name, "revision": body.revision})


# --------------------------------------------------------------------------
# Security (Trivy)
# --------------------------------------------------------------------------

@app.get("/api/security")
async def security():
    version = await asyncio.to_thread(trivy.trivy_version)
    scans = []
    for j in job_store.list_recent(200, kind="deploy"):
        for s in j.scans:
            scans.append({**{k: v for k, v in s.items() if k != "findings"},
                          "jobId": j.id, "app": j.app_name, "image": j.image_tag})
    latest: dict[tuple[str, str], dict] = {}
    for s in scans:  # newest first
        latest.setdefault((s["app"], s["scanType"]), s)
    totals = {k: 0 for k in trivy.SEVERITIES}
    for s in latest.values():
        if s["status"] == "completed":
            for k, n in s["counts"].items():
                totals[k] += n
    data = {
        "trivy": {"installed": bool(version), "version": version, "enabled": settings.trivy_enabled},
        "gate": {"failOn": settings.trivy_fail_on, "ignoreUnfixed": settings.trivy_ignore_unfixed},
        "totals": totals, "scans": scans[:40],
        "scanCount": len(scans),
    }
    if not version and not scans:
        return response("unavailable", "Trivy", data,
                        "Trivy is not installed and no scans have been recorded. Install Trivy (docs/setup).")
    return response("live", "Trivy", data)


@app.get("/api/security/scans/{job_id}")
async def security_scan_detail(job_id: str):
    job = job_store.get(job_id)
    if not job:
        return problem(404, "Job not found.", "not_found")
    return response("live", "Trivy", {"jobId": job.id, "app": job.app_name, "image": job.image_tag, "scans": job.scans})


@app.get("/api/security/reports/{name}")
async def security_report(name: str):
    path = trivy.safe_report_path(name)
    if not path:
        return problem(404, "Report not found.", "not_found")
    return FileResponse(path, media_type="application/json", filename=name)


# --------------------------------------------------------------------------
# Observability (Prometheus)
# --------------------------------------------------------------------------

PANEL_QUERIES = {
    "CPU utilization": ('100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])))', "%"),
    "Memory utilization": ("100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))", "%"),
    "NEXUS request rate": ("sum(rate(nexus_http_requests_total[5m]))", " req/s"),
    "NEXUS error rate": ('100 * sum(rate(nexus_http_requests_total{status=~"5.."}[5m])) '
                         "/ sum(rate(nexus_http_requests_total[5m]))", "%"),
    "Running pods": ('sum(kube_pod_status_phase{phase="Running"})', ""),
}


@app.get("/api/observability")
async def observability():
    if not settings.prometheus_url:
        return response("unavailable", "Prometheus", None, "PROMETHEUS_URL not configured")
    if not await _http_ok(settings.prometheus_url, "/-/ready"):
        return response("unavailable", "Prometheus", None, "Prometheus is configured but not reachable.")

    async def one(label: str, query: str, suffix: str):
        value = first_value(await prometheus_query(settings.prometheus_url, query))
        if value is None:
            return {"label": label, "value": None, "status": "NO DATA", "source": "Prometheus"}
        return {"label": label, "value": f"{value:.2f}{suffix}", "raw": value, "status": "LIVE", "source": "Prometheus"}

    panels, targets = await asyncio.gather(
        asyncio.gather(*(one(l, q, s) for l, (q, s) in PANEL_QUERIES.items())),
        prometheus_targets(settings.prometheus_url),
    )
    return response("live", "Prometheus", {"panels": list(panels), "targets": targets,
                                           "grafanaUrl": settings.grafana_url})


@app.get("/api/observability/series")
async def observability_series():
    if not settings.prometheus_url:
        return response("unavailable", "Prometheus", None, "PROMETHEUS_URL not configured")
    names = ["CPU utilization", "Memory utilization", "NEXUS request rate"]
    results = await asyncio.gather(*(prometheus_range(settings.prometheus_url, PANEL_QUERIES[n][0]) for n in names))
    series = [{"label": n, "unit": PANEL_QUERIES[n][1].strip(), "points": pts} for n, pts in zip(names, results)]
    if all(s["points"] is None for s in series):
        return response("unavailable", "Prometheus", None, "No range data returned (is Prometheus scraping?).")
    return response("live", "Prometheus", series)


# --------------------------------------------------------------------------
# GitOps / Argo CD
# --------------------------------------------------------------------------

@app.get("/api/gitops")
async def gitops():
    if not settings.argocd_url:
        return response("unavailable", "Argo CD", None, "ARGOCD_URL not configured (Argo CD is optional).")
    try:
        payload = await argocd_applications(settings.argocd_url, settings.argocd_token)
    except Exception as exc:
        return response("error", "Argo CD", None, f"Argo CD unreachable ({type(exc).__name__}).")
    rows = []
    for item in payload.get("items") or []:
        st = item.get("status", {})
        op = st.get("operationState", {})
        src = item.get("spec", {}).get("source", {})
        rows.append({
            "app": item.get("metadata", {}).get("name"), "repo": src.get("repoURL"), "path": src.get("path"),
            "targetRevision": src.get("targetRevision"), "live": (st.get("sync") or {}).get("revision"),
            "sync": ((st.get("sync") or {}).get("status") or "UNKNOWN").upper(),
            "health": ((st.get("health") or {}).get("status") or "UNKNOWN").upper(),
            "lastSync": op.get("finishedAt") or op.get("startedAt"),
        })
    return response("live", "Argo CD", rows)


# --------------------------------------------------------------------------
# CI/CD feed (optional, read-only GitHub Actions)
# --------------------------------------------------------------------------

@app.get("/api/pipelines")
async def pipelines():
    if not settings.github_owner or not settings.github_repo:
        return response("unavailable", "GitHub Actions", None, "GITHUB_OWNER / GITHUB_REPO not configured (optional).")
    try:
        payload = await workflow_runs(settings.github_owner, settings.github_repo, settings.github_token)
    except Exception as exc:
        return response("error", "GitHub Actions", None, f"GitHub request failed ({type(exc).__name__}).")
    runs = [{
        "name": r.get("name"), "branch": r.get("head_branch"), "commit": (r.get("head_sha") or "")[:7],
        "actor": (r.get("actor") or {}).get("login"), "startedAt": r.get("run_started_at") or r.get("created_at"),
        "status": (r.get("conclusion") or r.get("status") or "unknown").upper(),
    } for r in payload.get("workflow_runs", [])]
    return response("live", "GitHub Actions", runs)


# --------------------------------------------------------------------------
# Infrastructure (host facts + optional AWS)
# --------------------------------------------------------------------------

def _host_facts() -> dict[str, Any]:
    mem_gb = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            kb = int(fh.readline().split()[1])
            mem_gb = round(kb / 1024 / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    disks = {}
    for label, p in (("work", settings.work_dir), ("data", settings.data_dir)):
        try:
            Path(p).mkdir(parents=True, exist_ok=True)
            u = shutil.disk_usage(p)
            disks[label] = {"path": p, "totalGb": round(u.total / 1e9, 1), "freeGb": round(u.free / 1e9, 1),
                            "usedPct": round(100 * (u.total - u.free) / u.total, 1)}
        except OSError:
            continue
    return {"os": f"{platform.system()} {platform.release()}", "cpuCount": os.cpu_count(),
            "memoryGb": mem_gb, "python": platform.python_version(), "disks": disks}


def _aws_snapshot() -> dict[str, Any]:
    import boto3
    ec2 = boto3.client("ec2", region_name=settings.aws_region)
    out = []
    for res in ec2.describe_instances(Filters=[{"Name": "tag:Project", "Values": ["nexus"]}]).get("Reservations", []):
        for i in res.get("Instances", []):
            out.append({"id": i["InstanceId"], "type": i["InstanceType"], "state": i["State"]["Name"],
                        "publicIp": i.get("PublicIpAddress"),
                        "name": next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), None)})
    return {"instances": out}


@app.get("/api/infrastructure")
async def infrastructure():
    host = await asyncio.to_thread(_host_facts)
    aws: dict[str, Any] | None
    aws_error = None
    try:
        aws = await asyncio.wait_for(asyncio.to_thread(_aws_snapshot), timeout=8.0)
    except ImportError:
        aws, aws_error = None, "boto3 is not installed."
    except Exception as exc:
        aws, aws_error = None, f"AWS not available ({type(exc).__name__}). Provide credentials via the AWS credential chain."
    return response("live", "NEXUS host + AWS", {"host": host, "aws": aws, "awsError": aws_error,
                                                  "region": settings.aws_region})


# --------------------------------------------------------------------------
# Not-yet-wired endpoints stay honest
# --------------------------------------------------------------------------

@app.get("/api/costs")
async def costs():
    return response("unavailable", "AWS Cost Explorer", None, "Cost Explorer integration is not configured.")


# --------------------------------------------------------------------------
# Frontend (only the frontend/ and assets/ folders are served - never the
# project root, which contains .env and backend source)
# --------------------------------------------------------------------------

if ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
