"""
Job tracking for NEXUS releases and rollbacks.

Live state (steps, logs) is kept in memory so the UI can stream it. Finished
jobs are also written to a small JSON file in NEXUS_DATA_DIR so *deployment
history survives a restart*. This is intentionally not a database: NEXUS is a
control plane, and a single JSON file is easy to explain and inspect.

If NEXUS is ever run with several worker processes this must move to a real
database (SQLite/PostgreSQL/Redis); the Job API would stay the same.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..config import settings

Status = Literal["queued", "running", "success", "failed"]
StepStatus = Literal["pending", "running", "success", "failed", "skipped"]

DEPLOY_STEPS = ["upload", "inspect", "test", "build", "security", "publish", "deploy", "verify"]
ROLLBACK_STEPS = ["plan", "rollback", "verify"]
STEP_ORDER = DEPLOY_STEPS  # backwards-compatible alias

MAX_LOG_LINES_PERSISTED = 600
_id_counter = itertools.count(1)


# --------------------------------------------------------------------------
# Log hygiene: never let credentials reach logs or the UI.
# --------------------------------------------------------------------------
_REDACTIONS = [
    (re.compile(r"(?i)(authorization:\s*(?:bearer|basic)\s+)\S+"), r"\1***"),
    (re.compile(r"(?i)((?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*)\S+"), r"\1***"),
    (re.compile(r"(https?://[^/\s:@]+:)[^@\s/]+@"), r"\1***@"),   # user:pass@host URLs
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "ghp_***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


@dataclass
class LogLine:
    ts: float
    text: str
    level: str = "info"  # info | warn | error | success


@dataclass
class Job:
    id: str
    app_name: str
    kind: str = "deploy"            # deploy | rollback
    source: str = "zip"             # zip | github | rollback
    status: Status = "queued"
    mode: str | None = None         # gitops | direct | local
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    step_order: list[str] = field(default_factory=lambda: list(DEPLOY_STEPS))
    steps: dict[str, StepStatus] = field(default_factory=dict)
    logs: list[LogLine] = field(default_factory=list)
    preview_url: str | None = None
    error: str | None = None
    image_tag: str | None = None
    project: dict[str, Any] | None = None
    scans: list[dict[str, Any]] = field(default_factory=list)
    health: dict[str, Any] | None = None
    deployment: dict[str, Any] | None = None   # namespace, revision, replicas...
    git: dict[str, Any] | None = None          # repo/branch/commit for github source

    def __post_init__(self) -> None:
        for s in self.step_order:
            self.steps.setdefault(s, "pending")

    def to_summary(self) -> dict[str, Any]:
        return {
            "id": self.id, "app": self.app_name, "kind": self.kind, "source": self.source,
            "status": self.status, "mode": self.mode,
            "createdAt": self.created_at, "finishedAt": self.finished_at,
            "durationSec": round(self.finished_at - self.created_at, 1) if self.finished_at else None,
            "stepOrder": self.step_order, "steps": self.steps,
            "previewUrl": self.preview_url, "error": self.error, "imageTag": self.image_tag,
            "project": self.project, "scans": [_scan_brief(s) for s in self.scans],
            "health": self.health, "deployment": self.deployment, "git": self.git,
        }

    def to_detail(self, since: int = 0) -> dict[str, Any]:
        d = self.to_summary()
        d["logs"] = [{"ts": l.ts, "text": l.text, "level": l.level} for l in self.logs[since:]]
        d["logCount"] = len(self.logs)
        return d

    def to_record(self) -> dict[str, Any]:
        """Full persisted form (includes findings and capped logs)."""
        d = self.to_summary()
        d["scans"] = self.scans
        d["logs"] = [{"ts": l.ts, "text": l.text, "level": l.level}
                     for l in self.logs[-MAX_LOG_LINES_PERSISTED:]]
        return d

    @classmethod
    def from_record(cls, r: dict[str, Any]) -> "Job":
        job = cls(
            id=r["id"], app_name=r.get("app", "unknown"), kind=r.get("kind", "deploy"),
            source=r.get("source", "zip"), status=r.get("status", "failed"), mode=r.get("mode"),
            created_at=r.get("createdAt", time.time()), finished_at=r.get("finishedAt"),
            step_order=r.get("stepOrder") or list(DEPLOY_STEPS), steps=dict(r.get("steps") or {}),
            preview_url=r.get("previewUrl"), error=r.get("error"), image_tag=r.get("imageTag"),
            project=r.get("project"), scans=r.get("scans") or [], health=r.get("health"),
            deployment=r.get("deployment"), git=r.get("git"),
        )
        job.logs = [LogLine(l["ts"], l["text"], l.get("level", "info")) for l in r.get("logs", [])]
        return job


def _scan_brief(scan: dict[str, Any]) -> dict[str, Any]:
    """Scan summary without the (potentially large) findings list."""
    return {k: v for k, v in scan.items() if k != "findings"}


class JobStore:
    def __init__(self, max_jobs: int = 200, persist: bool = True) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._max_jobs = max_jobs
        self._persist_enabled = persist
        self._file_lock = threading.Lock()
        self._events: dict[str, asyncio.Event] = {}

    # -- persistence ------------------------------------------------------
    @property
    def _path(self) -> Path:
        return Path(settings.data_dir) / "history" / "jobs.json"

    def load(self) -> int:
        """Load persisted history (called once at startup)."""
        if not self._persist_enabled:
            return 0
        try:
            records = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        count = 0
        for r in records:
            try:
                job = Job.from_record(r)
            except (KeyError, TypeError):
                continue
            if job.status in ("queued", "running"):
                job.status = "failed"
                job.error = job.error or "NEXUS restarted while this job was running."
                job.finished_at = job.finished_at or time.time()
            if job.id not in self._jobs:
                self._jobs[job.id] = job
                self._order.append(job.id)
                count += 1
        return count

    def _persist(self) -> None:
        if not self._persist_enabled:
            return
        with self._file_lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                finished = [self._jobs[i].to_record() for i in self._order
                            if i in self._jobs and self._jobs[i].status in ("success", "failed")]
                fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(finished[-self._max_jobs:], fh)
                os.replace(tmp, self._path)  # atomic on Windows and Linux
            except OSError:
                pass  # history is best-effort; never break a deployment because of it

    # -- API --------------------------------------------------------------
    def create(self, app_name: str, project: dict[str, Any] | None = None, *,
               kind: str = "deploy", source: str = "zip",
               step_order: list[str] | None = None) -> Job:
        job_id = f"job-{next(_id_counter)}-{int(time.time())}"
        order = step_order or (ROLLBACK_STEPS if kind == "rollback" else list(DEPLOY_STEPS))
        job = Job(id=job_id, app_name=app_name, project=project, kind=kind, source=source,
                  step_order=order)
        self._jobs[job_id] = job
        self._order.append(job_id)
        self._events[job_id] = asyncio.Event()
        while len(self._order) > self._max_jobs:
            oldest = self._order.pop(0)
            self._jobs.pop(oldest, None)
            self._events.pop(oldest, None)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_recent(self, limit: int = 50, kind: str | None = None) -> list[Job]:
        jobs = [self._jobs[i] for i in reversed(self._order) if i in self._jobs]
        if kind:
            jobs = [j for j in jobs if j.kind == kind]
        return jobs[:limit]

    def _notify(self, job_id: str) -> None:
        ev = self._events.get(job_id)
        if ev:
            ev.set()
            ev.clear()

    def log(self, job_id: str, text: str, level: str = "info") -> None:
        job = self._jobs.get(job_id)
        if job:
            job.logs.append(LogLine(ts=time.time(), text=redact(text), level=level))
            self._notify(job_id)

    def set_step(self, job_id: str, step: str, status: StepStatus) -> None:
        job = self._jobs.get(job_id)
        if job:
            job.steps[step] = status
            self._notify(job_id)

    def update(self, job_id: str, **fields: Any) -> None:
        job = self._jobs.get(job_id)
        if job:
            for k, v in fields.items():
                setattr(job, k, v)
            self._notify(job_id)

    def add_scan(self, job_id: str, scan: dict[str, Any]) -> None:
        job = self._jobs.get(job_id)
        if job:
            job.scans.append(scan)
            self._notify(job_id)

    def set_project(self, job_id: str, project: dict[str, Any]) -> None:
        self.update(job_id, project=project)

    def finish(self, job_id: str, status: Status, error: str | None = None) -> None:
        job = self._jobs.get(job_id)
        if job:
            job.status = status
            job.error = error
            job.finished_at = time.time()
            self._notify(job_id)
            self._persist()

    async def wait(self, job_id: str, timeout: float = 25.0) -> None:
        ev = self._events.get(job_id)
        if not ev:
            return
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass


job_store = JobStore()
