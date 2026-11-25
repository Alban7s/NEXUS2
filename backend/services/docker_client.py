"""
Docker connection handling for NEXUS (Windows Docker Desktop, Linux, EC2).

The original error
------------------
    Couldn't reach the Docker daemon from NEXUS
    CreateFile: The system cannot find the file specified.

On Windows, the Docker SDK talks to the Docker Engine through a *named pipe*
(``//./pipe/docker_engine`` or, on current Docker Desktop,
``//./pipe/dockerDesktopLinuxEngine``). "CreateFile ... cannot find the file"
means that pipe does not exist, which almost always means **Docker Desktop is
not running** (or is still starting). The SDK's own default pipe can also be
the wrong one for the active Docker *context*.

What this module does
---------------------
1. Builds an ordered list of candidate endpoints:
     DOCKER_HOST  ->  active Docker CLI context  ->  platform defaults
2. Pings each (short timeout, small retry) and remembers the first that works.
3. When nothing works, classifies *why* into one of these codes and returns
   plain-English hints:

     sdk_missing         the `docker` Python package is not installed
     not_installed       Docker does not appear to be installed
     desktop_not_running Docker Desktop installed but not running / still starting
     engine_unavailable  endpoint exists but the engine does not answer
     bad_connection      DOCKER_HOST (or context) points somewhere unusable
     permission_denied   the OS user may not access the Docker socket

   (Image-build and container-start failures are reported by the pipeline with
   codes `image_build_failed` and `container_start_failed`.)
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import settings
from ..errors import DockerUnavailable

PING_TIMEOUT_S = 4
PING_ATTEMPTS = 2
PING_BACKOFF_S = 1.0

_cached_host: str | None = None


# --------------------------------------------------------------------------
# SDK import (lazy so the rest of NEXUS works without the package, and so
# tests can substitute a fake).
# --------------------------------------------------------------------------

def _sdk():
    try:
        import docker  # type: ignore
        return docker
    except ImportError as exc:  # pragma: no cover - exercised via mocks
        raise DockerUnavailable(
            "The `docker` Python package is not installed in the NEXUS environment. "
            "Run: pip install -r requirements.txt",
            code="sdk_missing",
        ) from exc


# --------------------------------------------------------------------------
# Candidate endpoints
# --------------------------------------------------------------------------

def _docker_config_dir() -> Path:
    return Path(os.getenv("DOCKER_CONFIG") or Path.home() / ".docker")


def current_context_host(config_dir: Path | None = None) -> tuple[str, str] | None:
    """Return (context_name, host) for the active Docker CLI context, if any.

    The CLI stores contexts under ``<config>/contexts/meta/<sha256(name)>/meta.json``.
    """
    cfg = config_dir or _docker_config_dir()
    try:
        name = os.getenv("DOCKER_CONTEXT")
        if not name:
            data = json.loads((cfg / "config.json").read_text(encoding="utf-8"))
            name = data.get("currentContext")
        if not name or name == "default":
            return None
        digest = hashlib.sha256(name.encode()).hexdigest()
        meta = json.loads((cfg / "contexts" / "meta" / digest / "meta.json").read_text(encoding="utf-8"))
        host = (meta.get("Endpoints") or {}).get("docker", {}).get("Host")
        return (name, host) if host else None
    except (OSError, ValueError, AttributeError):
        return None


def platform_defaults(system: str | None = None) -> list[tuple[str, str]]:
    system = system or platform.system()
    home = str(Path.home())
    if system == "Windows":
        return [
            ("Docker Desktop (Linux engine)", "npipe:////./pipe/dockerDesktopLinuxEngine"),
            ("Default Windows pipe", "npipe:////./pipe/docker_engine"),
        ]
    if system == "Darwin":
        return [
            ("Docker Desktop (macOS)", f"unix://{home}/.docker/run/docker.sock"),
            ("Default Unix socket", "unix:///var/run/docker.sock"),
        ]
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    return [
        ("Default Unix socket", "unix:///var/run/docker.sock"),
        ("Rootless Docker", f"unix:///run/user/{uid}/docker.sock"),
        ("Docker Desktop (Linux)", f"unix://{home}/.docker/desktop/docker.sock"),
    ]


def candidate_hosts(system: str | None = None) -> list[tuple[str, str]]:
    """Ordered, de-duplicated list of (label, url)."""
    out: list[tuple[str, str]] = []
    explicit = settings.docker_host or os.getenv("DOCKER_HOST")
    if explicit:
        out.append(("DOCKER_HOST environment variable", explicit))
    ctx = current_context_host()
    if ctx:
        out.append((f"Docker context '{ctx[0]}'", ctx[1]))
    out.extend(platform_defaults(system))
    seen: set[str] = set()
    unique = []
    for label, url in out:
        if url not in seen:
            seen.add(url)
            unique.append((label, url))
    return unique


def _endpoint_exists(url: str) -> bool | None:
    """True/False if we can tell whether a local socket/pipe exists, else None."""
    try:
        if url.startswith("unix://"):
            return os.path.exists(url[len("unix://"):])
        if url.startswith("npipe://"):
            pipe = url[len("npipe://"):].replace("/", "\\")
            return os.path.exists(pipe)
    except OSError:
        return None
    return None


# --------------------------------------------------------------------------
# Failure classification (pure function -> easy to unit-test)
# --------------------------------------------------------------------------

@dataclass
class Failure:
    code: str
    message: str
    hints: list[str] = field(default_factory=list)


def classify_failure(
    exc: BaseException | None,
    *,
    system: str,
    cli_installed: bool,
    any_endpoint_exists: bool,
    explicit_host: str | None = None,
) -> Failure:
    text = f"{type(exc).__name__}: {exc}".lower() if exc else ""
    desktop = system in ("Windows", "Darwin")

    if "permission denied" in text or isinstance(exc, PermissionError) or "access is denied" in text:
        hints = (
            ["Run the terminal as a user that may use Docker, or start Docker Desktop with the same user."]
            if desktop else
            ["Add your user to the docker group:  sudo usermod -aG docker $USER",
             "Then log out and back in (or run `newgrp docker`)."]
        )
        return Failure(
            "permission_denied",
            "Docker is running but this user is not allowed to access it.",
            hints + ["Access to the Docker socket is root-equivalent; grant it only to trusted users."],
        )

    if explicit_host and (
        "invalid" in text or "unsupported" in text or "not supported" in text
        or "name or service not known" in text or "getaddrinfo" in text
        or "connection refused" in text or "timed out" in text or "tls" in text or "ssl" in text
    ):
        return Failure(
            "bad_connection",
            f"DOCKER_HOST is set to '{explicit_host}' but NEXUS cannot use it.",
            ["Unset DOCKER_HOST to let NEXUS auto-detect the engine, or correct the value.",
             "Windows example:  DOCKER_HOST=npipe:////./pipe/dockerDesktopLinuxEngine",
             "Linux example:    DOCKER_HOST=unix:///var/run/docker.sock"],
        )

    if not cli_installed and not any_endpoint_exists:
        install = (
            "Install Docker Desktop from https://www.docker.com/products/docker-desktop/ (enable WSL 2)."
            if desktop else
            "Install Docker Engine: https://docs.docker.com/engine/install/ubuntu/"
        )
        return Failure("not_installed", "Docker does not appear to be installed on this machine.", [install])

    if desktop and not any_endpoint_exists:
        return Failure(
            "desktop_not_running",
            "Docker Desktop is installed but not running (the Docker engine pipe does not exist).",
            ["Start 'Docker Desktop' from the Start menu.",
             "Wait until it says 'Engine running' (the whale icon stops animating), then click Refresh.",
             "Check in a terminal:  docker version   (both Client and Server must print).",
             "If it never starts: enable virtualization in BIOS and run `wsl --update`."],
        )

    if not desktop and not any_endpoint_exists:
        return Failure(
            "engine_unavailable",
            "The Docker socket does not exist, so the Docker service is probably stopped.",
            ["Start it:  sudo systemctl start docker",
             "Enable on boot:  sudo systemctl enable docker",
             "Check:  sudo systemctl status docker"],
        )

    return Failure(
        "engine_unavailable",
        "A Docker endpoint exists but the engine did not answer.",
        ["Docker may still be starting; wait 30 seconds and refresh.",
         "Restart Docker Desktop / the docker service.",
         f"Last error: {str(exc)[:200]}" if exc else "No further detail."],
    )


# --------------------------------------------------------------------------
# Connecting
# --------------------------------------------------------------------------

def _try_ping(url: str) -> tuple[Any | None, BaseException | None]:
    docker = _sdk()
    last: BaseException | None = None
    for attempt in range(PING_ATTEMPTS):
        client = None
        try:
            client = docker.DockerClient(base_url=url, timeout=PING_TIMEOUT_S)
            client.ping()
            client.close()
            return url, None
        except BaseException as exc:  # noqa: BLE001 - we classify below
            last = exc
            if client is not None:
                try:
                    client.close()
                except Exception:  # pragma: no cover
                    pass
            # Missing pipe/socket won't appear within a second; don't wait.
            if _endpoint_exists(url) is False:
                break
            if attempt + 1 < PING_ATTEMPTS:
                time.sleep(PING_BACKOFF_S)
    return None, last


def _failure_for(errors: list[BaseException], tried: list[tuple[str, str]]) -> Failure:
    system = platform.system()
    explicit = settings.docker_host or os.getenv("DOCKER_HOST")
    exists = any(_endpoint_exists(u) for _, u in tried)
    # Prefer the most informative error: permission > first non-"file not found".
    chosen = next((e for e in errors if isinstance(e, PermissionError) or "denied" in str(e).lower()), None)
    chosen = chosen or (errors[0] if errors else None)
    return classify_failure(
        chosen, system=system, cli_installed=shutil.which("docker") is not None,
        any_endpoint_exists=bool(exists), explicit_host=explicit,
    )


def get_client():
    """Return a connected Docker client or raise DockerUnavailable with a code."""
    global _cached_host
    docker = _sdk()

    if _cached_host:
        try:
            client = docker.DockerClient(base_url=_cached_host, timeout=settings.docker_timeout_s)
            client.ping()
            return client
        except BaseException:  # noqa: BLE001
            _cached_host = None  # fall through and re-detect

    tried = candidate_hosts()
    errors: list[BaseException] = []
    for _label, url in tried:
        ok, err = _try_ping(url)
        if ok:
            _cached_host = url
            return docker.DockerClient(base_url=url, timeout=settings.docker_timeout_s)
        if err:
            errors.append(err)

    failure = _failure_for(errors, tried)
    raise DockerUnavailable(
        f"{failure.message} " + " ".join(failure.hints[:2]), code=failure.code
    )


def diagnose() -> dict[str, Any]:
    """Full, safe-to-display connection report (used by /api/docker/status)."""
    system = platform.system()
    base: dict[str, Any] = {
        "platform": system,
        "pythonVersion": sys.version.split()[0],
        "cliInstalled": shutil.which("docker") is not None,
        "dockerHostEnv": bool(os.getenv("DOCKER_HOST") or settings.docker_host),
    }
    try:
        docker = _sdk()
    except DockerUnavailable as exc:
        return {**base, "connected": False, "code": exc.code, "message": str(exc),
                "hints": ["pip install -r requirements.txt"], "candidates": []}

    tried = candidate_hosts()
    candidates = [{"label": l, "host": u, "endpointExists": _endpoint_exists(u)} for l, u in tried]
    errors: list[BaseException] = []
    for label, url in tried:
        ok, err = _try_ping(url)
        if ok:
            try:
                client = docker.DockerClient(base_url=url, timeout=PING_TIMEOUT_S)
                ver = client.version()
                client.close()
            except Exception:  # pragma: no cover
                ver = {}
            return {
                **base, "connected": True, "host": url, "source": label,
                "serverVersion": ver.get("Version"), "apiVersion": ver.get("ApiVersion"),
                "serverOs": ver.get("Os"), "candidates": candidates,
                "code": "ok", "message": "Docker Engine is reachable.", "hints": [],
            }
        if err:
            errors.append(err)
    failure = _failure_for(errors, tried)
    return {**base, "connected": False, "code": failure.code, "message": failure.message,
            "hints": failure.hints, "candidates": candidates}


def reset_cache() -> None:
    global _cached_host
    _cached_host = None


# --------------------------------------------------------------------------
# Cleanup helpers
# --------------------------------------------------------------------------

def cleanup_after_build(client, tags: list[str], log) -> None:
    """Remove temporary test images and dangling layers created by NEXUS builds."""
    for tag in tags:
        try:
            client.images.remove(tag, force=False, noprune=False)
            log(f"Cleaned up temporary image {tag}")
        except Exception:  # image missing or in use: not an error
            pass
    try:
        result = client.images.prune(filters={"dangling": True, "label": ["managed-by=nexus"]})
        freed = (result or {}).get("SpaceReclaimed", 0)
        if freed:
            log(f"Pruned dangling NEXUS build layers ({freed // (1024 * 1024)} MB freed)")
    except Exception:
        pass
