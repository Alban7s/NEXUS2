from typing import Any


def load_clients():
    from kubernetes import client, config
    from kubernetes.config.config_exception import ConfigException

    try:
        config.load_incluster_config()
    except ConfigException:
        config.load_kube_config()

    return client.CoreV1Api(), client.AppsV1Api()


def cluster_snapshot() -> dict[str, Any]:
    core, apps = load_clients()

    nodes = core.list_node().items
    namespaces = core.list_namespace().items
    pods = core.list_pod_for_all_namespaces().items
    deployments = apps.list_deployment_for_all_namespaces().items

    ready_nodes = 0
    for node in nodes:
        conditions = node.status.conditions or []
        if any(c.type == "Ready" and c.status == "True" for c in conditions):
            ready_nodes += 1

    pod_counts = {
        "running": sum(1 for p in pods if p.status and p.status.phase == "Running"),
        "pending": sum(1 for p in pods if p.status and p.status.phase == "Pending"),
        "failed": sum(1 for p in pods if p.status and p.status.phase == "Failed"),
        "total": len(pods),
    }

    available = 0
    for deployment in deployments:
        status = deployment.status
        if status and (status.available_replicas or 0) > 0:
            available += 1

    return {
        "clusterStatus": "HEALTHY" if ready_nodes == len(nodes) and len(nodes) > 0 else "DEGRADED",
        "nodes": {"ready": ready_nodes, "total": len(nodes)},
        "pods": pod_counts,
        "deployments": {"available": available, "total": len(deployments)},
        "namespaces": len(namespaces),
        "restarts24h": None,
    }
