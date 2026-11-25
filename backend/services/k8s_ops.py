"""
Kubernetes operations for NEXUS: status, revision history, rollback, and
verification.

Rollback
--------
``kubectl rollout undo --to-revision=N`` works by copying the Pod template of
the old ReplicaSet back into the Deployment. The Python client has no
`rollout undo` call, so we do the same thing the kubectl command does: a JSON
patch that *replaces* ``/spec/template`` with the old ReplicaSet's template
(minus the ``pod-template-hash`` label that Kubernetes adds itself).

NEXUS never says a rollback succeeded because the patch was accepted. After
patching it waits for the rollout and then re-reads the cluster to confirm the
pods run the expected image and are Ready.

All functions take the API objects as parameters so they can be unit-tested
with fakes — no cluster is needed for the logic tests.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from ..errors import PipelineError

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
CHANGE_CAUSE_ANNOTATION = "kubernetes.io/change-cause"
HASH_LABEL = "pod-template-hash"


def _iso(ts) -> str | None:
    return ts.isoformat() if ts else None


def _images(template) -> list[str]:
    containers = getattr(getattr(template, "spec", None), "containers", None) or []
    return [c.image for c in containers]


# --------------------------------------------------------------------------
# Read-only views
# --------------------------------------------------------------------------

def list_revisions(apps, name: str, namespace: str) -> list[dict[str, Any]]:
    """Revision history of a Deployment, newest first."""
    dep = apps.read_namespaced_deployment(name, namespace)
    labels = (dep.spec.selector.match_labels or {}) if dep.spec and dep.spec.selector else {}
    selector = ",".join(f"{k}={v}" for k, v in labels.items())
    replica_sets = apps.list_namespaced_replica_set(namespace, label_selector=selector).items
    uid = dep.metadata.uid
    current = int((dep.metadata.annotations or {}).get(REVISION_ANNOTATION, 0) or 0)

    out = []
    for rs in replica_sets:
        owners = rs.metadata.owner_references or []
        if not any(o.uid == uid for o in owners):
            continue
        ann = rs.metadata.annotations or {}
        try:
            rev = int(ann.get(REVISION_ANNOTATION, 0))
        except ValueError:
            continue
        if rev <= 0:
            continue
        imgs = _images(rs.spec.template)
        out.append({
            "revision": rev,
            "images": imgs,
            "image": imgs[0] if imgs else None,
            "createdAt": _iso(rs.metadata.creation_timestamp),
            "replicas": rs.status.replicas or 0,
            "readyReplicas": rs.status.ready_replicas or 0,
            "current": rev == current,
            "changeCause": ann.get(CHANGE_CAUSE_ANNOTATION),
        })
    out.sort(key=lambda r: -r["revision"])
    return out


def workload_overview(core, apps, namespace: str, managed_only: bool = False) -> dict[str, Any]:
    """Deployments, pods and services in a namespace (real cluster data)."""
    selector = "managed-by=nexus" if managed_only else None
    deployments = apps.list_namespaced_deployment(namespace, label_selector=selector).items
    pods = core.list_namespaced_pod(namespace).items
    services = core.list_namespaced_service(namespace).items

    dep_rows = []
    for d in deployments:
        st = d.status
        desired = d.spec.replicas or 0
        ready = (st.ready_replicas or 0) if st else 0
        dep_rows.append({
            "name": d.metadata.name, "namespace": namespace,
            "images": _images(d.spec.template), "desired": desired, "ready": ready,
            "available": (st.available_replicas or 0) if st else 0,
            "updated": (st.updated_replicas or 0) if st else 0,
            "revision": int((d.metadata.annotations or {}).get(REVISION_ANNOTATION, 0) or 0),
            "healthy": desired > 0 and ready >= desired,
            "managedByNexus": (d.metadata.labels or {}).get("managed-by") == "nexus",
        })

    pod_rows = []
    for p in pods:
        statuses = (p.status.container_statuses or []) if p.status else []
        pod_rows.append({
            "name": p.metadata.name, "phase": p.status.phase if p.status else "Unknown",
            "ready": bool(statuses) and all(s.ready for s in statuses),
            "restarts": sum(s.restart_count or 0 for s in statuses),
            "app": (p.metadata.labels or {}).get("app"),
            "image": (p.spec.containers[0].image if p.spec and p.spec.containers else None),
            "node": p.spec.node_name if p.spec else None,
        })

    svc_rows = []
    for s in services:
        ports = [{"port": pt.port, "targetPort": str(pt.target_port), "nodePort": pt.node_port,
                  "protocol": pt.protocol} for pt in (s.spec.ports or [])]
        lb = (s.status.load_balancer.ingress or []) if s.status and s.status.load_balancer else []
        svc_rows.append({
            "name": s.metadata.name, "type": s.spec.type, "clusterIP": s.spec.cluster_ip,
            "ports": ports,
            "external": [i.hostname or i.ip for i in lb] or None,
        })
    return {"namespace": namespace, "deployments": dep_rows, "pods": pod_rows, "services": svc_rows}


# --------------------------------------------------------------------------
# Rollout waiting / verification
# --------------------------------------------------------------------------

def rollout_complete(dep) -> tuple[bool, str]:
    """Same completeness rule as `kubectl rollout status`."""
    st = dep.status
    desired = dep.spec.replicas or 0
    if desired == 0:
        return False, "Deployment has 0 desired replicas."
    if st is None:
        return False, "No status reported yet."
    if (st.observed_generation or 0) < (dep.metadata.generation or 0):
        return False, "Waiting for the controller to observe the new spec."
    if (st.updated_replicas or 0) < desired:
        return False, f"{st.updated_replicas or 0}/{desired} replicas updated."
    if (st.replicas or 0) > (st.updated_replicas or 0):
        return False, "Old replicas are still terminating."
    if (st.available_replicas or 0) < desired:
        return False, f"{st.available_replicas or 0}/{desired} replicas available."
    return True, f"{desired}/{desired} replicas updated and available."


def wait_rollout(apps, name: str, namespace: str, timeout_s: int, log: Callable[[str], None],
                 sleep: Callable[[float], None] = time.sleep, poll_s: float = 3.0) -> bool:
    deadline = time.time() + timeout_s
    last = ""
    while True:
        dep = apps.read_namespaced_deployment(name, namespace)
        done, msg = rollout_complete(dep)
        if msg != last:
            log(f"Rollout: {msg}")
            last = msg
        if done:
            return True
        if time.time() >= deadline:
            return False
        sleep(poll_s)


def verify_pods(core, apps, name: str, namespace: str, expected_image: str | None) -> dict[str, Any]:
    """Re-read the cluster and report facts (never assumptions)."""
    dep = apps.read_namespaced_deployment(name, namespace)
    done, msg = rollout_complete(dep)
    labels = (dep.spec.selector.match_labels or {})
    selector = ",".join(f"{k}={v}" for k, v in labels.items())
    pods = core.list_namespaced_pod(namespace, label_selector=selector).items
    active = [p for p in pods if not p.metadata.deletion_timestamp]
    ready = [p for p in active if p.status and p.status.container_statuses
             and all(s.ready for s in p.status.container_statuses)]
    images = sorted({p.spec.containers[0].image for p in active if p.spec and p.spec.containers})
    image_ok = expected_image is None or images == [expected_image]
    verified = bool(done and active and len(ready) == len(active) and image_ok)
    return {
        "verified": verified, "rolloutComplete": done, "rolloutMessage": msg,
        "podsTotal": len(active), "podsReady": len(ready), "images": images,
        "expectedImage": expected_image, "imageMatches": image_ok,
        "revision": int((dep.metadata.annotations or {}).get(REVISION_ANNOTATION, 0) or 0),
        "checkedAt": time.time(),
    }


# --------------------------------------------------------------------------
# Rollback
# --------------------------------------------------------------------------

def rollback_deployment(
    core, apps, name: str, namespace: str, to_revision: int,
    log: Callable[[str], None], sanitize: Callable[[Any], Any],
    timeout_s: int = 120, sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Roll a Deployment back to `to_revision` and verify the result."""
    revisions = list_revisions(apps, name, namespace)
    if not revisions:
        raise PipelineError(f"No revision history found for deployment '{name}'.", code="no_history")
    target = next((r for r in revisions if r["revision"] == to_revision), None)
    if target is None:
        available = ", ".join(str(r["revision"]) for r in revisions)
        raise PipelineError(
            f"Revision {to_revision} does not exist for '{name}'. Available: {available}.",
            code="unknown_revision",
        )
    if target["current"]:
        raise PipelineError(f"Revision {to_revision} is already the current revision.", code="already_current")

    dep = apps.read_namespaced_deployment(name, namespace)
    uid = dep.metadata.uid
    labels = (dep.spec.selector.match_labels or {})
    selector = ",".join(f"{k}={v}" for k, v in labels.items())
    rs = next(
        r for r in apps.list_namespaced_replica_set(namespace, label_selector=selector).items
        if any(o.uid == uid for o in (r.metadata.owner_references or []))
        and (r.metadata.annotations or {}).get(REVISION_ANNOTATION) == str(to_revision)
    )

    template = sanitize(rs.spec.template)
    (template.get("metadata", {}).get("labels") or {}).pop(HASH_LABEL, None)
    log(f"Rolling '{name}' back to revision {to_revision} (image {target['image']})")
    patch = [
        {"op": "replace", "path": "/spec/template", "value": template},
        {"op": "add", "path": "/metadata/annotations/kubernetes.io~1change-cause",
         "value": f"nexus rollback to revision {to_revision}"},
    ]
    if dep.metadata.annotations is None:
        patch[1] = {"op": "add", "path": "/metadata/annotations",
                    "value": {CHANGE_CAUSE_ANNOTATION: f"nexus rollback to revision {to_revision}"}}
    apps.patch_namespaced_deployment(name, namespace, patch)
    log("Rollback patch accepted by the Kubernetes API; waiting for the rollout...")

    ok = wait_rollout(apps, name, namespace, timeout_s, log, sleep=sleep)
    facts = verify_pods(core, apps, name, namespace, target["image"])
    facts["rolledBackTo"] = to_revision
    if not (ok and facts["verified"]):
        raise PipelineError(
            "Rollback was submitted but the cluster did not reach a healthy state "
            f"({facts['rolloutMessage']}; pods ready {facts['podsReady']}/{facts['podsTotal']}).",
            code="rollback_unverified",
        )
    log(f"Verified: {facts['podsReady']}/{facts['podsTotal']} pods Ready running {facts['images'][0]}")
    return facts
