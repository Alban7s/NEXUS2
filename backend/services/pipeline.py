"""
The ONE shared release pipeline.

    upload -> inspect -> test -> build -> security -> publish -> deploy -> verify

Both submission methods converge here:
  * ZIP upload                    POST /api/deploy
  * GitHub -> Jenkins -> NEXUS    Jenkins zips the checkout and calls the same
                                  POST /api/deploy with source=github

Everything that *changes* something (Docker, registry, Kubernetes, Git) lives
in this module so each external call is explicit, logged, and wrapped: a
failure in one step leaves a clear error instead of a half-applied release.

Deploy mode is chosen automatically, in order of preference:
  1. gitops  - GIT_REPO_URL + Argo CD configured: NEXUS commits manifests and
               Argo CD syncs the cluster.
  2. direct  - Kubernetes API reachable and an image source is configured
               (registry, or Minikube local image load): NEXUS applies manifests.
  3. local   - nothing else configured: run the image with Docker on this host.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from ..config import settings
from ..errors import ArchiveError, DockerUnavailable, PipelineError
from . import archive, docker_client, k8s_ops, trivy
from . import kubernetes as k8s_service
from .archive import slugify  # re-exported for app.py / tests
from .jobs import Job, job_store

__all__ = ["PipelineError", "slugify", "inspect_upload", "run_pipeline"]


# --------------------------------------------------------------------------
# Upload inspection / extraction (delegates to the hardened archive module)
# --------------------------------------------------------------------------

def inspect_upload(zip_path: Path, original_filename: str) -> dict[str, Any]:
    return archive.inspect_archive(zip_path, original_filename)


def extract_upload(zip_path: Path, dest: Path) -> Path:
    return archive.safe_extract(zip_path, dest)


def detect_project(root: Path) -> dict[str, Any]:
    pkg_path = root / "package.json"
    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PipelineError(f"package.json is not valid JSON: {exc}", code="bad_package") from exc
        if not isinstance(pkg, dict):
            raise PipelineError("package.json must contain a JSON object.", code="bad_package")
        summary = archive.summarize_package(pkg)
        return {"root": root, **summary}
    if (root / "index.html").exists():
        return {"root": root, "type": "static", "framework": "Static HTML/CSS/JS",
                "kind": "Static website", "hasBuildScript": False, "hasTestScript": False,
                "lifecycleScripts": []}
    raise PipelineError("Couldn't find index.html or package.json - nothing here looks like a website.",
                        code="no_project")


# --------------------------------------------------------------------------
# Dockerfile / nginx generation (NEXUS always writes its own; an uploaded
# Dockerfile is never trusted or used)
# --------------------------------------------------------------------------

NGINX_CONF = """server {
    listen 80;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location = /healthz {
        access_log off;
        default_type text/plain;
        return 200 'ok';
    }
}
"""

DOCKERIGNORE = """.git
.gitignore
.github
node_modules
__pycache__
*.pyc
.env
.env.*
"""

STATIC_DOCKERFILE = """FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY . /usr/share/nginx/html
"""


def node_dockerfile(ignore_scripts: bool, with_tests: bool) -> str:
    flag = " --ignore-scripts" if ignore_scripts else ""
    test_stage = (
        "\nFROM deps AS test\nCOPY . .\nRUN CI=true npm test\n" if with_tests else ""
    )
    return f"""FROM node:20-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN if [ -f package-lock.json ] || [ -f npm-shrinkwrap.json ]; then \\
      npm ci --no-audit --no-fund{flag}; \\
    else \\
      npm install --no-audit --no-fund{flag}; \\
    fi
{test_stage}
FROM deps AS builder
COPY . .
RUN npm run build
RUN mkdir -p /out \\
    && (cp -r build/. /out/ 2>/dev/null || true) \\
    && (cp -r dist/. /out/ 2>/dev/null || true) \\
    && (cp -r out/. /out/ 2>/dev/null || true)
RUN [ -n "$(ls -A /out 2>/dev/null)" ] || (echo "No build output found in build/, dist/ or out/" && exit 1)

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /out /usr/share/nginx/html
"""


NEXTJS_DOCKERFILE = """FROM node:20-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund{flag}; else npm install --no-audit --no-fund{flag}; fi

FROM deps AS builder
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build
RUN test -f .next/BUILD_ID || (echo "Next.js server build not found; static export projects need a different mode" && exit 1)

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
COPY --from=builder /app ./
EXPOSE 3000
CMD ["npm", "run", "start"]
"""


def write_build_context(project: dict[str, Any]) -> None:
    root: Path = project["root"]
    (root / "nginx.conf").write_text(NGINX_CONF, encoding="utf-8")
    (root / ".dockerignore").write_text(DOCKERIGNORE, encoding="utf-8")

    if project["type"] == "node":
        if not project.get("hasBuildScript"):
            raise PipelineError(
                'package.json has no "build" script. Add one (e.g. "build": "vite build") and re-upload.',
                code="no_build_script",
            )
        if project.get("framework") == "Next.js":
            pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
            start = (pkg.get("scripts") or {}).get("start", "")
            if "next start" not in start:
                raise PipelineError(
                    'Next.js server projects need a "start" script using "next start". '
                    "Static-export Next.js projects are not supported by this runtime.",
                    code="unsupported_nextjs",
                )
            flag = " --ignore-scripts" if settings.npm_ignore_scripts else ""
            text = NEXTJS_DOCKERFILE.replace("{flag}", flag)
        else:
            text = node_dockerfile(settings.npm_ignore_scripts, with_tests=run_tests_for(project))
    else:
        text = STATIC_DOCKERFILE
    (root / "Dockerfile").write_text(text, encoding="utf-8")


def run_tests_for(project: dict[str, Any]) -> bool:
    return bool(settings.run_tests and project.get("type") == "node"
                and project.get("hasTestScript") and project.get("framework") != "Next.js")


def runtime_port(project: dict[str, Any]) -> int:
    return 3000 if project.get("framework") == "Next.js" else 80


def health_path(project: dict[str, Any]) -> str:
    return "/" if project.get("framework") == "Next.js" else "/healthz"


# --------------------------------------------------------------------------
# Docker build
# --------------------------------------------------------------------------

def make_tag(app_name: str, job_id: str) -> str:
    """Versioned tag: <app>:<UTC timestamp>-<job number>  e.g. site:20261122-143005-7"""
    number = job_id.split("-")[1] if job_id.count("-") >= 2 else "0"
    return f"{app_name}:{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{number}"


def build_image(project_root: Path, tag: str, log, *, target: str | None = None,
                job_id: str | None = None) -> str:
    """Build an image, streaming the Docker log live into the job log."""
    client = docker_client.get_client()  # raises DockerUnavailable with a precise code
    for required in ("Dockerfile", "nginx.conf", ".dockerignore"):
        if not (project_root / required).exists():
            raise PipelineError(f"Generated {required} is missing from the build context.",
                                code="image_build_failed")
    ignored = {l.strip() for l in (project_root / ".dockerignore").read_text(encoding="utf-8").splitlines()}
    if "nginx.conf" in ignored:
        raise PipelineError("nginx.conf is excluded by .dockerignore.", code="image_build_failed")

    mem = settings.build_memory_mb * 1024 * 1024
    log(f"docker build{' --target ' + target if target else ''} -t {tag}  (context: {project_root.name})")
    docker = docker_client._sdk()
    try:
        stream = client.api.build(
            path=str(project_root), tag=tag, rm=True, forcerm=True, decode=True, target=target,
            labels={"managed-by": "nexus", "nexus.job": job_id or ""},
            container_limits={"memory": mem, "memswap": mem},
        )
        for chunk in stream:
            if "error" in chunk:
                raise PipelineError(f"Image build failed: {str(chunk['error']).strip()[:500]}",
                                    code="image_build_failed")
            line = chunk.get("stream") or (chunk.get("status") if "progress" not in chunk else None)
            if line and line.strip():
                log(line.rstrip())
    except PipelineError:
        raise
    except docker.errors.APIError as exc:
        raise PipelineError(f"Docker rejected the build: {exc.explanation or exc}"[:500],
                            code="image_build_failed") from exc
    except docker.errors.DockerException as exc:
        raise PipelineError(f"Image build failed: {exc}"[:500], code="image_build_failed") from exc
    finally:
        client.close()
    return tag


# --------------------------------------------------------------------------
# Security (Trivy) - shared gate
# --------------------------------------------------------------------------

def run_security(project_root: Path, image_tag: str, job: Job, log) -> bool:
    """Scan source + image. Returns False if skipped; raises if the gate fails."""
    scans = [trivy.scan_filesystem(project_root, job.id, log), trivy.scan_image(image_tag, job.id, log)]
    for s in scans:
        job_store.add_scan(job.id, s)
        if s["status"] == "skipped":
            log(f"{s['scanType']} scan skipped: {s['error']}", level="warn")

    if all(s["status"] == "skipped" for s in scans):
        return False

    enforce = settings.trivy_fail_on != "NONE"
    errors = [s for s in scans if s["status"] == "error"]
    for s in errors:
        log(f"{s['scanType']} scan error: {s['error']}", level="error" if enforce else "warn")
    if errors and enforce:
        raise PipelineError("The security scan could not complete, so the release was stopped "
                            "(set TRIVY_FAIL_ON_SEVERITY=NONE to make scans report-only).",
                            code="scan_error")

    for s in scans:
        gate = s.get("gate")
        if s["status"] == "completed" and gate:
            log(f"{s['scanType']} gate: {gate['reason']}", level="success" if gate["passed"] else "error")
            if not gate["passed"]:
                raise PipelineError(f"Security gate failed on the {s['scanType']} scan. {gate['reason']} "
                                    "Release stopped before publishing.", code="security_gate")
    return any(s["status"] == "completed" for s in scans)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def _push_stream(client, repo_uri: str, tag: str, log) -> None:
    docker = docker_client._sdk()
    try:
        for chunk in client.images.push(repo_uri, tag=tag, stream=True, decode=True):
            if "errorDetail" in chunk:
                raise PipelineError(chunk["errorDetail"].get("message", "push failed"), code="push_failed")
            if "status" in chunk and not chunk.get("progress"):
                log(f"{chunk['status']} {chunk.get('id', '')}".strip())
    except docker.errors.DockerException as exc:
        raise PipelineError(f"Image push failed: {exc}"[:400], code="push_failed") from exc


def push_image(app_name: str, local_tag: str, log) -> str:
    """Push to ECR or a generic registry. Returns the full image URI."""
    client = docker_client.get_client()
    docker = docker_client._sdk()
    remote_tag = local_tag.split(":", 1)[1]
    try:
        if settings.ecr_repository_prefix:
            import boto3
            from botocore.exceptions import BotoCoreError, ClientError
            ecr = boto3.client("ecr", region_name=settings.aws_region)
            try:
                ecr.create_repository(repositoryName=app_name)
                log(f"Created ECR repository '{app_name}'")
            except ecr.exceptions.RepositoryAlreadyExistsException:
                pass
            except (BotoCoreError, ClientError) as exc:
                raise PipelineError(f"Couldn't create/check ECR repository: {exc}", code="registry_error") from exc
            try:
                auth = ecr.get_authorization_token()["authorizationData"][0]
            except (BotoCoreError, ClientError) as exc:
                raise PipelineError(f"Couldn't get ECR auth token: {exc}", code="registry_auth") from exc
            user, pw = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
            registry = auth["proxyEndpoint"].replace("https://", "")
            repo_uri = f"{registry}/{app_name}"
        else:
            registry = (settings.registry_url or "").rstrip("/")
            user, pw = settings.registry_username, settings.registry_password
            repo_uri = f"{registry}/{app_name}"
            if not (user and pw):
                log("No REGISTRY_USERNAME/REGISTRY_PASSWORD set; pushing without login.", level="warn")

        if user and pw:
            try:
                client.login(username=user, password=pw, registry=registry.split("/")[0])
            except docker.errors.DockerException as exc:
                raise PipelineError(f"Docker login to the registry failed: {exc}"[:300],
                                    code="registry_auth") from exc

        client.images.get(local_tag).tag(repo_uri, tag=remote_tag)
        full = f"{repo_uri}:{remote_tag}"
        log(f"Pushing {full}")
        _push_stream(client, repo_uri, remote_tag, log)
        return full
    finally:
        client.close()


def load_into_minikube(local_tag: str, log) -> None:
    exe = shutil.which("minikube")
    if not exe:
        raise PipelineError("K8S_IMAGE_SOURCE=minikube but the `minikube` command was not found.",
                            code="minikube_missing")
    log(f"minikube image load {local_tag}")
    proc = subprocess.run([exe, "image", "load", local_tag], capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise PipelineError(f"minikube image load failed: {(proc.stderr or proc.stdout).strip()[:300]}",
                            code="minikube_load_failed")


# --------------------------------------------------------------------------
# Mode selection
# --------------------------------------------------------------------------

def kubernetes_reachable(timeout_s: int = 5) -> bool:
    try:
        from kubernetes import client as kc
        k8s_service.load_clients()
        kc.VersionApi().get_code(_request_timeout=timeout_s)
        return True
    except Exception:
        return False


def choose_mode(log) -> str:
    if settings.git_repo_url and settings.argocd_url and settings.registry_configured:
        log("GitOps repo + Argo CD + registry are configured -> GitOps mode.")
        return "gitops"
    image_ok = settings.registry_configured or settings.k8s_image_source == "minikube"
    if image_ok:
        if kubernetes_reachable():
            log("Kubernetes API is reachable -> direct Kubernetes mode.")
            return "direct"
        log("Kubernetes API is not reachable; falling back to local Docker preview.", level="warn")
        return "local"
    log("No registry/cluster configured -> local Docker preview mode.")
    return "local"


# --------------------------------------------------------------------------
# Kubernetes manifests
# --------------------------------------------------------------------------

def build_manifests(app_name: str, image_uri: str, container_port: int = 80, *,
                    change_cause: str | None = None, pull_policy: str | None = None,
                    probe_path: str = "/healthz") -> dict[str, dict]:
    labels = {"app": app_name, "managed-by": "nexus"}
    probe: dict[str, Any] = (
        {"httpGet": {"path": probe_path, "port": container_port}, "initialDelaySeconds": 3, "periodSeconds": 5}
        if container_port == 80 else
        {"tcpSocket": {"port": container_port}, "initialDelaySeconds": 5, "periodSeconds": 5}
    )
    container: dict[str, Any] = {
        "name": app_name, "image": image_uri, "ports": [{"containerPort": container_port}],
        "readinessProbe": probe,
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                      "limits": {"cpu": "250m", "memory": "256Mi"}},
    }
    if pull_policy:
        container["imagePullPolicy"] = pull_policy

    metadata: dict[str, Any] = {"name": app_name, "namespace": settings.k8s_namespace, "labels": labels}
    if change_cause:
        metadata["annotations"] = {"kubernetes.io/change-cause": change_cause}

    deployment = {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata,
        "spec": {
            "replicas": 2, "revisionHistoryLimit": 5,
            "selector": {"matchLabels": {"app": app_name}},
            "template": {"metadata": {"labels": labels}, "spec": {"containers": [container]}},
        },
    }
    service = {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": app_name, "namespace": settings.k8s_namespace, "labels": labels},
        "spec": {"type": settings.k8s_service_type, "selector": {"app": app_name},
                 "ports": [{"port": 80, "targetPort": container_port}]},
    }
    manifests = {"deployment": deployment, "service": service}

    if settings.ingress_domain:
        manifests["ingress"] = {
            "apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
            "metadata": {"name": app_name, "namespace": settings.k8s_namespace, "labels": labels,
                         "annotations": {"kubernetes.io/ingress.class": settings.ingress_class}},
            "spec": {"rules": [{"host": f"{app_name}.{settings.ingress_domain}", "http": {"paths": [
                {"path": "/", "pathType": "Prefix",
                 "backend": {"service": {"name": app_name, "port": {"number": 80}}}}]}}]},
        }
    return manifests


def preview_url_for(app_name: str) -> str | None:
    return f"https://{app_name}.{settings.ingress_domain}" if settings.ingress_domain else None


# --------------------------------------------------------------------------
# Direct Kubernetes deployment
# --------------------------------------------------------------------------

def deploy_direct(app_name: str, image_uri: str, log, container_port: int, probe_path: str) -> dict[str, Any]:
    from kubernetes import client as kc
    from kubernetes.client.rest import ApiException

    pull_policy = "Never" if settings.k8s_image_source == "minikube" else None
    manifests = build_manifests(app_name, image_uri, container_port,
                                change_cause=f"nexus deploy {image_uri}",
                                pull_policy=pull_policy, probe_path=probe_path)
    core, apps = k8s_service.load_clients()
    networking = kc.NetworkingV1Api()
    ns = settings.k8s_namespace

    def apply(read, create, patch, body, kind):
        name = body["metadata"]["name"]
        try:
            read(name, ns)
            patch(name, ns, body)  # PATCH (not replace): Services forbid replacing immutable fields
            log(f"Updated {kind} '{name}' in namespace '{ns}'")
        except ApiException as exc:
            if exc.status == 404:
                create(ns, body)
                log(f"Created {kind} '{name}' in namespace '{ns}'")
            else:
                raise PipelineError(f"Kubernetes API error applying {kind} '{name}': {exc.reason}",
                                    code="k8s_error") from exc

    apply(apps.read_namespaced_deployment, apps.create_namespaced_deployment,
          apps.patch_namespaced_deployment, manifests["deployment"], "Deployment")
    apply(core.read_namespaced_service, core.create_namespaced_service,
          core.patch_namespaced_service, manifests["service"], "Service")
    if "ingress" in manifests:
        apply(networking.read_namespaced_ingress, networking.create_namespaced_ingress,
              networking.patch_namespaced_ingress, manifests["ingress"], "Ingress")

    svc = core.read_namespaced_service(app_name, ns)
    node_port = next((p.node_port for p in (svc.spec.ports or []) if p.node_port), None)
    return {"namespace": ns, "name": app_name, "image": image_uri, "serviceType": svc.spec.type,
            "clusterIP": svc.spec.cluster_ip, "nodePort": node_port}


def verify_direct(app_name: str, image_uri: str, log) -> dict[str, Any]:
    core, apps = k8s_service.load_clients()
    ok = k8s_ops.wait_rollout(apps, app_name, settings.k8s_namespace, settings.rollout_timeout_s, log)
    facts = k8s_ops.verify_pods(core, apps, app_name, settings.k8s_namespace, image_uri)
    facts["verified"] = bool(ok and facts["verified"])
    return facts


# --------------------------------------------------------------------------
# GitOps
# --------------------------------------------------------------------------

def _git_env() -> dict[str, str]:
    """Environment for git. The token is passed as an HTTP header via env so it
    never appears in a URL, a command line, or a log."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if settings.git_token:
        basic = base64.b64encode(f"x-access-token:{settings.git_token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                   GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
    return env


def _run_git(args: list[str], cwd: Path, log) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env=_git_env(), timeout=120)
    if proc.stdout.strip():
        log(proc.stdout.strip())
    if proc.returncode != 0:
        raise PipelineError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}", code="git_failed")
    return proc.stdout


def deploy_gitops(app_name: str, image_uri: str, workdir: Path, log, container_port: int,
                  probe_path: str) -> None:
    import yaml
    clone_dir = workdir / "gitops-repo"
    log(f"Cloning GitOps repository (branch {settings.git_branch})")
    _run_git(["clone", "--depth", "1", "--branch", settings.git_branch,
              settings.git_repo_url or "", str(clone_dir)], cwd=workdir, log=log)
    _run_git(["config", "user.name", settings.git_user_name], clone_dir, log)
    _run_git(["config", "user.email", settings.git_user_email], clone_dir, log)

    app_dir = clone_dir / settings.git_manifests_path / app_name
    app_dir.mkdir(parents=True, exist_ok=True)
    for name, body in build_manifests(app_name, image_uri, container_port,
                                      change_cause=f"nexus deploy {image_uri}",
                                      probe_path=probe_path).items():
        (app_dir / f"{name}.yaml").write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
        log(f"Wrote {settings.git_manifests_path}/{app_name}/{name}.yaml")

    _run_git(["add", "."], clone_dir, log)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=clone_dir, capture_output=True, text=True)
    if not status.stdout.strip():
        log("No manifest changes to commit (image tag unchanged).")
        return
    _run_git(["commit", "-m", f"nexus: deploy {app_name} -> {image_uri}"], clone_dir, log)
    _run_git(["push", "origin", settings.git_branch], clone_dir, log)
    log("Pushed to the GitOps repository. Argo CD will sync it.")


def verify_gitops(app_name: str, log, timeout_s: int = 120) -> dict[str, Any]:
    """Wait for Argo CD to report Synced + Healthy for this application."""
    from .argocd import application_status_sync
    deadline = time.time() + timeout_s
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        last = application_status_sync(app_name)
        if last is None:
            return {"verified": False, "reason": f"Argo CD has no application named '{app_name}' "
                    "(or it is unreachable). Create it from argocd/application.yaml."}
        log(f"Argo CD: sync={last['sync']} health={last['health']}")
        if last["sync"] == "Synced" and last["health"] == "Healthy":
            return {"verified": True, **last}
        time.sleep(5)
    return {"verified": False, "reason": "Timed out waiting for Argo CD to become Synced/Healthy.", **(last or {})}


# --------------------------------------------------------------------------
# Local Docker preview
# --------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def deploy_local(app_name: str, local_tag: str, log, container_port: int) -> tuple[str, str]:
    """Run the image locally. Returns (preview_url, container_name)."""
    client = docker_client.get_client()
    docker = docker_client._sdk()
    name = f"nexus-{app_name}"
    try:
        try:
            client.containers.get(name).remove(force=True)
            log(f"Removed previous preview container '{name}'")
        except docker.errors.NotFound:
            pass
        port = _free_port()
        try:
            client.containers.run(
                local_tag, name=name, detach=True,
                ports={f"{container_port}/tcp": (settings.preview_bind, port)},
                labels={"managed-by": "nexus"}, mem_limit="256m", pids_limit=200,
                security_opt=["no-new-privileges"], restart_policy={"Name": "unless-stopped"},
            )
        except docker.errors.APIError as exc:
            raise PipelineError(f"Container failed to start: {exc.explanation or exc}"[:400],
                                code="container_start_failed") from exc
        log(f"Container '{name}' started on {settings.preview_bind}:{port}")
        return f"http://{settings.public_host}:{port}", name
    finally:
        client.close()


def verify_local(container_name: str, container_port: int, path: str, log,
                 timeout_s: int | None = None) -> dict[str, Any]:
    """Health check from *inside* the container (works even when NEXUS itself
    runs in Docker), plus a check that the container did not crash-loop."""
    timeout_s = timeout_s or settings.health_check_timeout_s
    client = docker_client.get_client()
    try:
        container = client.containers.get(container_name)
        deadline = time.time() + timeout_s
        attempts = 0
        while time.time() < deadline:
            attempts += 1
            container.reload()
            state = container.attrs.get("State", {})
            if container.status in ("exited", "dead") or state.get("Restarting"):
                logs = container.logs(tail=15).decode("utf-8", "replace").strip()
                raise PipelineError(f"Container is not running (status: {container.status}). Last logs: {logs[-300:]}",
                                    code="container_start_failed")
            if container.status == "running":
                code, _out = container.exec_run(
                    ["wget", "-q", "-O", "/dev/null", f"http://127.0.0.1:{container_port}{path}"])
                if code == 0:
                    log(f"Health check OK: GET {path} inside the container responded (attempt {attempts}).")
                    return {"verified": True, "method": "container-exec wget", "path": path,
                            "attempts": attempts, "checkedAt": time.time()}
            time.sleep(1.5)
        return {"verified": False, "method": "container-exec wget", "path": path, "attempts": attempts,
                "reason": f"No healthy response from {path} within {timeout_s}s.", "checkedAt": time.time()}
    finally:
        client.close()


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

async def run_pipeline(job: Job, zip_path: Path) -> None:
    def log(text: str, level: str = "info") -> None:
        job_store.log(job.id, text, level)

    def step(name: str, status: str) -> None:
        job_store.set_step(job.id, name, status)  # type: ignore[arg-type]

    workdir = Path(settings.work_dir) / job.id
    temp_tags: list[str] = []
    try:
        job_store.update(job.id, status="running")

        # 1. UPLOAD ---------------------------------------------------------
        step("upload", "running")
        log(f"Extracting upload for '{job.app_name}' (source: {job.source})")
        project_root = await asyncio.to_thread(extract_upload, zip_path, workdir / "src")
        step("upload", "success")

        # 2. INSPECT --------------------------------------------------------
        step("inspect", "running")
        project = await asyncio.to_thread(detect_project, project_root)
        job_store.set_project(job.id, {k: v for k, v in project.items() if k != "root"})
        port, probe = runtime_port(project), health_path(project)
        log(f"Detected {project['framework']} - {project['kind']}. Runtime port {port}.")
        if project.get("lifecycleScripts"):
            note = "ignored (NEXUS_NPM_IGNORE_SCRIPTS=true)" if settings.npm_ignore_scripts else "WILL RUN during install"
            log(f"package.json has lifecycle scripts {project['lifecycleScripts']}: {note}.", level="warn")
        await asyncio.to_thread(archive.scan_extracted_tree, project_root)
        await asyncio.to_thread(write_build_context, project)
        log("Input-policy gate passed before build.", level="success")
        step("inspect", "success")

        # Fail early and clearly if Docker is not usable.
        await asyncio.to_thread(_check_docker)

        # 3. TEST -----------------------------------------------------------
        local_tag = make_tag(job.app_name, job.id)
        if run_tests_for(project):
            step("test", "running")
            test_tag = f"{job.app_name}-test:{local_tag.split(':')[1]}"
            temp_tags.append(test_tag)
            log("Running the project's own tests (npm test) in an isolated build container.")
            await asyncio.to_thread(build_image, project_root, test_tag, log, target="test", job_id=job.id)
            log("Project tests passed.", level="success")
            step("test", "success")
        else:
            step("test", "skipped")
            log("Tests skipped: no test script found (or NEXUS_RUN_TESTS=false).")

        # 4. BUILD ----------------------------------------------------------
        step("build", "running")
        await asyncio.to_thread(build_image, project_root, local_tag, log, job_id=job.id)
        job_store.update(job.id, image_tag=local_tag)
        step("build", "success")

        # 5. SECURITY -------------------------------------------------------
        step("security", "running")
        scanned = await asyncio.to_thread(run_security, project_root, local_tag, job, log)
        step("security", "success" if scanned else "skipped")

        # Mode -----------------------------------------------------------------
        mode = await asyncio.to_thread(choose_mode, log)
        job_store.update(job.id, mode=mode)

        # 6. PUBLISH --------------------------------------------------------
        image_uri = local_tag
        if mode in ("gitops", "direct") and settings.registry_configured:
            step("publish", "running")
            image_uri = await asyncio.to_thread(push_image, job.app_name, local_tag, log)
            job_store.update(job.id, image_tag=image_uri)
            step("publish", "success")
        elif mode == "direct" and settings.k8s_image_source == "minikube":
            step("publish", "running")
            await asyncio.to_thread(load_into_minikube, local_tag, log)
            step("publish", "success")
        else:
            step("publish", "skipped")
            log("Registry publish skipped: local preview mode.")

        # 7. DEPLOY ---------------------------------------------------------
        step("deploy", "running")
        container_name = f"nexus-{job.app_name}"
        deployment_info: dict[str, Any] | None = None
        if mode == "gitops":
            await asyncio.to_thread(deploy_gitops, job.app_name, image_uri, workdir, log, port, probe)
            job_store.update(job.id, preview_url=preview_url_for(job.app_name))
        elif mode == "direct":
            deployment_info = await asyncio.to_thread(deploy_direct, job.app_name, image_uri, log, port, probe)
            job_store.update(job.id, preview_url=preview_url_for(job.app_name), deployment=deployment_info)
        else:
            url, container_name = await asyncio.to_thread(deploy_local, job.app_name, local_tag, log, port)
            job_store.update(job.id, preview_url=url,
                             deployment={"container": container_name, "port": port})
        step("deploy", "success")

        # 8. VERIFY ---------------------------------------------------------
        step("verify", "running")
        if mode == "direct":
            health = await asyncio.to_thread(verify_direct, job.app_name, image_uri, log)
            deployment_info = {**(deployment_info or {}), "revision": health.get("revision")}
            job_store.update(job.id, deployment=deployment_info)
        elif mode == "gitops":
            health = await asyncio.to_thread(verify_gitops, job.app_name, log)
        else:
            health = await asyncio.to_thread(verify_local, container_name, port, probe, log)
        job_store.update(job.id, health=health)

        if health.get("verified"):
            step("verify", "success")
        elif mode == "gitops":
            step("verify", "skipped")
            log(f"GitOps verification not confirmed: {health.get('reason')}", level="warn")
        else:
            step("verify", "failed")
            raise PipelineError(health.get("reason") or "Deployment did not become healthy.",
                                code="health_check_failed")

        log(f"Done. {job.app_name} released via {mode} mode.", level="success")
        job_store.finish(job.id, "success")

    except (PipelineError, ArchiveError, DockerUnavailable) as exc:
        _fail(job, str(exc), getattr(exc, "code", "pipeline_error"), log, step)
    except Exception as exc:  # unexpected: log the type, keep the server alive
        _fail(job, f"Unexpected error ({type(exc).__name__}): {exc}", "unexpected", log, step)
    finally:
        try:
            if temp_tags or workdir.exists():
                client = None
                try:
                    client = await asyncio.to_thread(docker_client.get_client)
                    await asyncio.to_thread(docker_client.cleanup_after_build, client, temp_tags, log)
                except Exception:
                    pass
                finally:
                    if client is not None:
                        client.close()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            try:
                zip_path.unlink(missing_ok=True)
            except OSError:
                pass


def _check_docker() -> None:
    docker_client.get_client().close()


def _fail(job: Job, message: str, code: str, log, step) -> None:
    log(message, level="error")
    current = next((s for s in job.step_order if job.steps.get(s) == "running"), None)
    if current:
        step(current, "failed")
    job_store.update(job.id, health=job.health or {"verified": False, "reason": message, "code": code})
    job_store.finish(job.id, "failed", error=message)
