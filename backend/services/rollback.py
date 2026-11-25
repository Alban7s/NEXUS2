"""Rollback job runner: plan -> rollback -> verify (all against the real cluster)."""
from __future__ import annotations

import asyncio
from typing import Any

from ..config import settings
from ..errors import PipelineError
from . import k8s_ops
from . import kubernetes as k8s_service
from .jobs import Job, job_store


def _sanitize(obj: Any) -> Any:
    from kubernetes import client
    return client.ApiClient().sanitize_for_serialization(obj)


def annotate_known_good(revisions: list[dict[str, Any]], app: str) -> list[dict[str, Any]]:
    """Mark revisions whose image NEXUS itself released and verified healthy."""
    good = {
        j.image_tag for j in job_store.list_recent(200, kind="deploy")
        if j.app_name == app and j.status == "success" and (j.health or {}).get("verified")
    }
    for r in revisions:
        r["knownGood"] = r["image"] in good
    return revisions


async def run_rollback(job: Job, name: str, namespace: str, revision: int) -> None:
    def log(text: str, level: str = "info") -> None:
        job_store.log(job.id, text, level)

    def step(n: str, s: str) -> None:
        job_store.set_step(job.id, n, s)  # type: ignore[arg-type]

    try:
        job_store.update(job.id, status="running")

        step("plan", "running")
        core, apps = await asyncio.to_thread(k8s_service.load_clients)
        revisions = await asyncio.to_thread(k8s_ops.list_revisions, apps, name, namespace)
        log(f"Revisions of '{name}': " + ", ".join(
            f"#{r['revision']}{'*' if r['current'] else ''} ({r['image']})" for r in revisions))
        target = next((r for r in revisions if r["revision"] == revision), None)
        if target is None:
            raise PipelineError(f"Revision {revision} not found.", code="unknown_revision")
        if target["current"]:
            raise PipelineError(f"Revision {revision} is already current.", code="already_current")
        job_store.update(job.id, image_tag=target["image"])
        step("plan", "success")

        step("rollback", "running")
        try:
            facts = await asyncio.to_thread(
                k8s_ops.rollback_deployment, core, apps, name, namespace, revision,
                log, _sanitize, settings.rollout_timeout_s)
        except PipelineError as exc:
            if exc.code == "rollback_unverified":
                step("rollback", "success")   # the patch was applied ...
                step("verify", "failed")      # ... but the cluster did not become healthy
            raise
        step("rollback", "success")

        step("verify", "running")
        job_store.update(job.id, health=facts,
                         deployment={"namespace": namespace, "name": name,
                                     "rolledBackTo": revision, "revision": facts.get("revision")})
        step("verify", "success")
        log(f"Rollback of '{name}' to revision {revision} verified against the cluster.", level="success")
        job_store.finish(job.id, "success")
    except PipelineError as exc:
        _fail(job, str(exc), log, step)
    except Exception as exc:
        _fail(job, f"Unexpected error ({type(exc).__name__}): {exc}", log, step)


def _fail(job: Job, message: str, log, step) -> None:
    log(message, level="error")
    current = next((s for s in job.step_order if job.steps.get(s) == "running"), None)
    if current:
        step(current, "failed")
    job_store.finish(job.id, "failed", error=message)
