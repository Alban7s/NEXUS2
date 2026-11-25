"""NEXUS configuration — every value comes from environment variables.

On Windows/local development, values are read from a `.env` file in the
project root (requires `python-dotenv`, listed in requirements.txt).
Nothing secret is ever hard-coded here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:  # optional so unit tests can run without the dependency installed
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))  # type: ignore[arg-type]
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _list(name: str) -> tuple[str, ...]:
    value = _env(name, "") or ""
    return tuple(v.strip() for v in value.split(",") if v.strip())


@dataclass(frozen=True)
class Settings:
    # --- Optional GitHub Actions read-only feed ---------------------------
    github_token: Optional[str] = _env("GITHUB_TOKEN")
    github_owner: Optional[str] = _env("GITHUB_OWNER")
    github_repo: Optional[str] = _env("GITHUB_REPO")

    # --- Integrations -----------------------------------------------------
    prometheus_url: Optional[str] = _env("PROMETHEUS_URL")
    grafana_url: Optional[str] = _env("GRAFANA_URL")
    argocd_url: Optional[str] = _env("ARGOCD_URL")
    argocd_token: Optional[str] = _env("ARGOCD_TOKEN")
    argocd_verify_tls: bool = _bool("ARGOCD_VERIFY_TLS", True)
    aws_region: str = _env("AWS_REGION", "us-east-1") or "us-east-1"

    # --- Security of NEXUS itself ----------------------------------------
    nexus_api_key: Optional[str] = _env("NEXUS_API_KEY")
    # Comma-separated browser origins allowed for CORS. Empty = same-origin only.
    cors_origins: tuple[str, ...] = _list("NEXUS_CORS_ORIGINS")

    # --- Storage ----------------------------------------------------------
    work_dir: str = _env("NEXUS_WORK_DIR", "/tmp/nexus-jobs") or "/tmp/nexus-jobs"
    data_dir: str = _env("NEXUS_DATA_DIR", "./nexus-data") or "./nexus-data"

    # --- ZIP intake limits ------------------------------------------------
    max_upload_mb: int = _int("NEXUS_MAX_UPLOAD_MB", 100)
    max_uncompressed_mb: int = _int("NEXUS_MAX_UNCOMPRESSED_MB", 500)
    max_single_file_mb: int = _int("NEXUS_MAX_SINGLE_FILE_MB", 100)
    max_files: int = _int("NEXUS_MAX_FILES", 10_000)
    max_compression_ratio: int = _int("NEXUS_MAX_COMPRESSION_RATIO", 100)

    # --- Build ------------------------------------------------------------
    # Lifecycle scripts (preinstall/postinstall) in uploaded code are
    # untrusted. Default: do not run them during `npm ci`.
    npm_ignore_scripts: bool = _bool("NEXUS_NPM_IGNORE_SCRIPTS", True)
    run_tests: bool = _bool("NEXUS_RUN_TESTS", True)
    build_memory_mb: int = _int("NEXUS_BUILD_MEMORY_MB", 2048)
    max_concurrent_jobs: int = _int("NEXUS_MAX_CONCURRENT_JOBS", 1)
    docker_timeout_s: int = _int("NEXUS_DOCKER_TIMEOUT", 900)
    docker_host: Optional[str] = _env("DOCKER_HOST")

    # --- Registry ---------------------------------------------------------
    # Either Amazon ECR (credentials from the normal AWS chain) ...
    ecr_repository_prefix: Optional[str] = _env("ECR_REPOSITORY_PREFIX")
    # ... or any Docker registry, e.g. REGISTRY_URL=docker.io/<your-user>
    registry_url: Optional[str] = _env("REGISTRY_URL")
    registry_username: Optional[str] = _env("REGISTRY_USERNAME")
    registry_password: Optional[str] = _env("REGISTRY_PASSWORD")

    # --- Trivy ------------------------------------------------------------
    trivy_enabled: bool = _bool("TRIVY_ENABLED", True)
    # Fail the pipeline when a finding at or above this severity exists.
    # One of: CRITICAL, HIGH, MEDIUM, LOW, NONE (NONE = report only).
    trivy_fail_on: str = (_env("TRIVY_FAIL_ON_SEVERITY", "CRITICAL") or "CRITICAL").upper()
    trivy_ignore_unfixed: bool = _bool("TRIVY_IGNORE_UNFIXED", False)
    trivy_timeout_s: int = _int("TRIVY_TIMEOUT", 600)

    # --- Kubernetes -------------------------------------------------------
    k8s_namespace: str = _env("K8S_NAMESPACE", "default") or "default"
    ingress_domain: Optional[str] = _env("INGRESS_DOMAIN")
    ingress_class: str = _env("INGRESS_CLASS", "nginx") or "nginx"
    rollout_timeout_s: int = _int("K8S_ROLLOUT_TIMEOUT", 120)
    # "registry" = pull from ECR/registry. "minikube" = load the local image
    # into Minikube with `minikube image load` (no registry needed).
    k8s_image_source: str = (_env("K8S_IMAGE_SOURCE", "registry") or "registry").lower()
    k8s_service_type: str = _env("K8S_SERVICE_TYPE", "ClusterIP") or "ClusterIP"

    # --- GitOps -----------------------------------------------------------
    git_repo_url: Optional[str] = _env("GIT_REPO_URL")
    git_branch: str = _env("GIT_BRANCH", "main") or "main"
    git_manifests_path: str = _env("GIT_MANIFESTS_PATH", "apps") or "apps"
    git_token: Optional[str] = _env("GIT_TOKEN")   # used via env header, never in URLs/logs
    git_user_name: str = _env("GIT_USER_NAME", "nexus-bot") or "nexus-bot"
    git_user_email: str = _env("GIT_USER_EMAIL", "nexus-bot@local") or "nexus-bot@local"

    # --- GitHub -> Jenkins (administrator-managed) ------------------------
    jenkins_url: Optional[str] = _env("JENKINS_URL")
    jenkins_user: Optional[str] = _env("JENKINS_USER")
    jenkins_api_token: Optional[str] = _env("JENKINS_API_TOKEN")
    jenkins_job: str = _env("JENKINS_JOB", "nexus-github-deploy") or "nexus-github-deploy"
    github_repo_url: Optional[str] = _env("GITHUB_REPO_URL")
    github_branch: str = _env("GITHUB_BRANCH", "main") or "main"
    github_webhook_secret: Optional[str] = _env("GITHUB_WEBHOOK_SECRET")

    # --- Preview ----------------------------------------------------------
    public_host: str = _env("NEXUS_PUBLIC_HOST", "localhost") or "localhost"
    # Interface the local preview containers listen on. Default is loopback so
    # uploaded sites are not exposed to the network unless you opt in.
    preview_bind: str = _env("NEXUS_PREVIEW_BIND", "127.0.0.1") or "127.0.0.1"
    health_check_timeout_s: int = _int("NEXUS_HEALTH_TIMEOUT", 30)

    @property
    def registry_configured(self) -> bool:
        return bool(self.ecr_repository_prefix or self.registry_url)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


settings = Settings()
