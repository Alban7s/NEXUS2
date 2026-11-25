"""
Trivy integration: filesystem scan (source + dependency lock files) and Docker
image scan.

Design points
-------------
* NEXUS never invents findings. If Trivy is not installed the result has
  ``status="skipped"``; if Trivy fails the result has ``status="error"``.
* Trivy is asked to write a full JSON report; NEXUS parses *that file*.
* The pass/fail decision (the "security gate") is made by NEXUS, not by Trivy's
  exit code, so the threshold is configurable:

      TRIVY_FAIL_ON_SEVERITY = CRITICAL | HIGH | MEDIUM | LOW | NONE

  ``NONE`` means "report only, never fail the pipeline".
* Commands are built as argument lists and run without a shell, so image names
  and paths cannot inject shell commands.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings

SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]
_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}
MAX_FINDINGS_STORED = 300
_SAFE_REPORT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}\.json$")


def trivy_path() -> str | None:
    return shutil.which("trivy")


def trivy_version() -> str | None:
    exe = trivy_path()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
        match = re.search(r"Version:\s*(\S+)", out.stdout)
        return match.group(1) if match else out.stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def reports_dir() -> Path:
    d = Path(settings.data_dir) / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_report_path(name: str) -> Path | None:
    """Resolve a report file by name; None if the name is unsafe/missing."""
    if not _SAFE_REPORT_NAME.match(name):
        return None
    p = reports_dir() / name
    return p if p.is_file() else None


# --------------------------------------------------------------------------
# Pure functions (unit-tested without Trivy)
# --------------------------------------------------------------------------

def parse_report(report: dict[str, Any]) -> dict[str, Any]:
    """Extract counts and a normalised finding list from Trivy JSON output."""
    counts = {s: 0 for s in SEVERITIES}
    findings: list[dict[str, Any]] = []

    for result in report.get("Results") or []:
        target = result.get("Target", "")
        for v in result.get("Vulnerabilities") or []:
            sev = str(v.get("Severity", "UNKNOWN")).upper()
            sev = sev if sev in counts else "UNKNOWN"
            counts[sev] += 1
            findings.append({
                "kind": "vulnerability", "severity": sev, "id": v.get("VulnerabilityID"),
                "package": v.get("PkgName"), "installed": v.get("InstalledVersion"),
                "fixed": v.get("FixedVersion"), "title": (v.get("Title") or "")[:200],
                "target": target,
            })
        for s in result.get("Secrets") or []:
            sev = str(s.get("Severity", "UNKNOWN")).upper()
            sev = sev if sev in counts else "UNKNOWN"
            counts[sev] += 1
            findings.append({
                "kind": "secret", "severity": sev, "id": s.get("RuleID"),
                "package": None, "installed": None, "fixed": None,
                "title": (s.get("Title") or "Possible secret")[:200], "target": target,
            })

    findings.sort(key=lambda f: -_RANK.get(f["severity"], 0))
    return {"counts": counts, "total": sum(counts.values()), "findings": findings[:MAX_FINDINGS_STORED],
            "truncated": len(findings) > MAX_FINDINGS_STORED}


def evaluate_gate(counts: dict[str, int], threshold: str, enforce: bool = True) -> dict[str, Any]:
    """Decide whether the pipeline may continue.

    The gate fails when any finding has severity >= threshold.
    """
    threshold = (threshold or "CRITICAL").upper()
    if threshold == "NONE" or not enforce:
        return {"threshold": threshold, "enforced": False, "passed": True,
                "reason": "Report-only mode: findings do not stop the pipeline."}
    if threshold not in _RANK:
        threshold = "CRITICAL"
    limit = _RANK[threshold]
    blocking = {s: n for s, n in counts.items() if n and _RANK.get(s, 0) >= limit and s != "UNKNOWN"}
    if blocking:
        parts = ", ".join(f"{n} {s}" for s, n in blocking.items())
        return {"threshold": threshold, "enforced": True, "passed": False,
                "reason": f"Gate failed: {parts} (threshold {threshold} or higher)."}
    return {"threshold": threshold, "enforced": True, "passed": True,
            "reason": f"No findings at {threshold} or higher."}


def _base_result(scan_type: str, target: str) -> dict[str, Any]:
    return {
        "scanner": "trivy", "scanType": scan_type, "target": target,
        "scannedAt": datetime.now(timezone.utc).isoformat(),
        "status": "pending", "trivyVersion": None,
        "counts": {s: 0 for s in SEVERITIES}, "total": 0, "findings": [],
        "gate": None, "reportFile": None, "error": None,
    }


# --------------------------------------------------------------------------
# Running Trivy
# --------------------------------------------------------------------------

def _run(scan_type: str, target: str, label: str, report_name: str, log) -> dict[str, Any]:
    result = _base_result(scan_type, label)
    exe = trivy_path()

    if not settings.trivy_enabled:
        result.update(status="skipped", error="Trivy scanning is disabled (TRIVY_ENABLED=false).")
        return result
    if not exe:
        result.update(
            status="skipped",
            error="Trivy is not installed on the NEXUS host. Install it (see docs/setup) to enable scanning.",
        )
        return result

    result["trivyVersion"] = trivy_version()
    report_file = reports_dir() / report_name
    cmd = [
        exe, scan_type, "--quiet", "--format", "json", "--output", str(report_file),
        "--severity", ",".join(SEVERITIES), "--timeout", f"{settings.trivy_timeout_s}s",
        "--scanners", "vuln,secret" if scan_type == "fs" else "vuln",
    ]
    if settings.trivy_ignore_unfixed:
        cmd.append("--ignore-unfixed")
    cmd.append(target)

    log(f"Running Trivy {scan_type} scan on {label} (Trivy {result['trivyVersion'] or 'unknown version'})")
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=settings.trivy_timeout_s + 30)
    except subprocess.TimeoutExpired:
        result.update(status="error", error="Trivy timed out.")
        return result
    except OSError as exc:
        result.update(status="error", error=f"Could not start Trivy: {exc}")
        return result

    if proc.returncode != 0 or not report_file.is_file():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        hint = " (the first scan downloads the vulnerability database and needs internet access)"
        result.update(status="error", error=("Trivy failed: " + " | ".join(tail))[:400] + hint)
        return result

    try:
        parsed = parse_report(json.loads(report_file.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        result.update(status="error", error=f"Trivy report could not be parsed: {exc}")
        return result

    result.update(status="completed", reportFile=report_name, **parsed)
    result["durationSec"] = round(time.time() - started, 1)
    result["gate"] = evaluate_gate(parsed["counts"], settings.trivy_fail_on)
    c = parsed["counts"]
    log(f"Trivy result: {c['CRITICAL']} critical, {c['HIGH']} high, {c['MEDIUM']} medium, "
        f"{c['LOW']} low, {c['UNKNOWN']} unknown", level="info")
    return result


def scan_filesystem(path: Path, job_id: str, log) -> dict[str, Any]:
    return _run("fs", str(path), f"source of {job_id}", f"{job_id}-fs.json", log)


def scan_image(image: str, job_id: str, log) -> dict[str, Any]:
    return _run("image", image, image, f"{job_id}-image.json", log)
