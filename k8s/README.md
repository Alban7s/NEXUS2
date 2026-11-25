# Kubernetes manifests

| File | Purpose |
|---|---|
| `nexus-local.yaml` | Original Minikube manifest from the project (image `nexus:<tag>` loaded with `minikube image load`). Kept unchanged. |
| `nexus-eks.yaml` | Original EKS manifest with an ECR image reference. Kept unchanged. |
| `nexus-service-nlb.yaml` | Original AWS Network Load Balancer Service (was `k8s-service.yaml`). Kept unchanged. |

Deployments created **by NEXUS** are generated in `backend/services/pipeline.py` (`build_manifests`):
2 replicas, `revisionHistoryLimit: 5` (so rollback has revisions to go back to), a readiness probe,
CPU/memory requests and limits, and a `kubernetes.io/change-cause` annotation per release.

The images referenced in the original manifests are examples from the earlier project; replace them
with your own image before applying.
