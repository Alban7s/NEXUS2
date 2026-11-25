# NEXUS — validation results for `NEXUS-forsure.zip`

Validation date: 2026-11-25. These results are from the exact uploaded archive after the self-deployment pipeline and Minikube runtime updates. Statuses use **PASS / FAIL / BLOCKED / NOT RUN** only. A parsed manifest or mocked test is not evidence that a cloud service or cluster deployment succeeded.

## Executed locally

| Test | Status | Evidence | Blocker / scope |
|---|---|---|---|
| Backend unit/API suite | PASS | `python -m pytest backend/tests -q -rs` → **107 passed, 2 skipped** | The 2 skipped tests require Docker SDK/daemon. |
| Python syntax | PASS | `python -m compileall -q backend tests` | Syntax only. |
| JavaScript syntax | PASS | `node --check frontend/app.js`, `frontend/api.js`, `frontend/views.js` | Syntax only; does not exercise the browser. |
| Stubbed FastAPI handlers | PASS | `python tests/stubbed_handlers.py` → **26/26** | Handler-level test; TestClient coverage is in pytest suite. |
| Live local FastAPI smoke | PASS | Uvicorn started; `/healthz` returned `ok`; `/` and `/api/deploy/config` returned HTTP 200 | Sandbox loopback only; no remote host. |
| Kubernetes YAML structure | PASS | PyYAML parsed **36 YAML files** across `k8s/`, `ansible/`, `monitoring/`, `argocd/` and root Compose file | Not Kubernetes API schema validation. |
| NEXUS Kubernetes manifest contracts | PASS | Tests assert port 8000, probes, private image pull secret, PVC, non-root security context, local mTLS mount, namespace and service type | Does not prove scheduling or readiness on a cluster. |
| Kubernetes RBAC structure | PASS | Test asserts observer ClusterRole has read-only verbs and workload Role is namespace-scoped | `kubectl auth can-i` remains unrun. |
| Prometheus config and Grafana dashboard | PASS | Embedded Prometheus YAML parsed; dashboard JSON parsed and panels present | `promtool` and live Prometheus/Grafana unavailable. |
| Terraform source constraints | PASS | Tests assert ap-south-1 default, m7i-flex.large, 25 GB root, admin CIDR restriction and variable-driven volume | Static assertions only, not HCL validation. |
| Ansible Docker mTLS template | PASS | Rendered the Jinja script with test variables; `bash -n` passed | Ansible itself not installed; remote playbook not run. |
| Docker TLS client helper | PASS | Unit tests cover complete/missing cert bundle handling and redaction of userinfo/query parameters | Does not test a real TLS handshake. |
| Jenkinsfile static contract | PASS | Tests assert checkout/test/build/Trivy/push/deploy/health stages, trusted `linux-docker` label, and credential IDs; all six embedded shell blocks pass `bash -n` | Jenkins/Groovy Declarative Pipeline linter unavailable. |
| Docker mTLS Jenkins credential archive | PASS | Helper tests valid bundle and rejects unexpected/traversal members; extracted key has mode 0600 | Real Jenkins Secret-file integration remains unverified. |
| EC2 host exporter config | PASS | Compose YAML, Prometheus scrape targets and Ansible UFW/source restrictions parsed and asserted | Requires Minikube Pod-to-host network verification. |
| Secret-pattern audit | PASS | No `.env`, Terraform state, kubeconfig, PEM/private key or Docker client bundle found in project tree; only a dummy redaction token in a unit test | Not a substitute for scanning a deployed secret store. |

## External environment checks

| Test | Status | Evidence | Blocker |
|---|---|---|---|
| Docker CLI / Engine | BLOCKED | `docker` executable absent | Run on the Ubuntu EC2 host with Docker Engine installed. |
| NEXUS Docker API mTLS from Minikube Pod | BLOCKED | Ansible certificate generation, UFW rules, client TLS code and K8s Secret mount are implemented and unit/static checked | Requires the Ansible playbook and live Minikube host network. |
| NEXUS Docker image build | BLOCKED | Dockerfile and root Jenkins stage present | Docker daemon unavailable here. |
| Trivy real source/image scan | BLOCKED | Trivy implementation and Jenkins image-scan stage present | Trivy executable and vulnerability DB unavailable. |
| Jenkins Declarative Pipeline lint/run | BLOCKED | Root `Jenkinsfile` added and statically tested | Jenkins/Groovy CLI and credentials unavailable. |
| Docker Hub push / private pull | BLOCKED | Jenkins credential handling and Kubernetes pull Secret creation implemented | Requires user Docker Hub repository and PAT. |
| Minikube startup / kubectl apply | BLOCKED | Manifests and Ansible role present | `minikube` and `kubectl` absent here. |
| Kubernetes rollout / health / rollback | BLOCKED | Jenkins rollout/health/rollback commands and NEXUS rollback API exist | Requires a live cluster. |
| Terraform `fmt`, `init`, `validate`, `plan` | BLOCKED | Terraform files and static safety tests present | Terraform binary/provider download and AWS access unavailable. |
| Ansible `--syntax-check` / `--check` / run | BLOCKED | Playbook and role YAML parsed; rendered shell script passes `bash -n` | `ansible-playbook` and target EC2 access unavailable. A live run requires authorization. |
| Prometheus/Grafana deployment | BLOCKED | 36 YAML parse and dashboard JSON checks pass | No Kubernetes/Docker runtime or `promtool`. |
| Browser UI smoke via Playwright | BLOCKED | Playwright installed; system Chromium launched | Browser policy returned `ERR_BLOCKED_BY_ADMINISTRATOR` on local test URL. No new screenshot is claimed. |
| AWS API / EC2 / EKS | BLOCKED | Terraform structure and region defaults checked | No AWS credentials or account supplied; no resources created. |
| Argo CD sync | NOT RUN | Example GitOps manifests remain optional | No cluster or Argo CD server configured. |

## Important implementation changes in this revision

1. Added a **root `Jenkinsfile`** for deploying NEXUS itself. It is separate from `jenkins/Jenkinsfile`, which submits customer apps to the NEXUS shared pipeline.
2. The root pipeline performs Git checkout, Python tests, versioned Docker build, Trivy image gate, Docker Hub push, Kubernetes apply, rollout wait and HTTP `/healthz` verification.
3. Corrected the NEXUS Kubernetes service/container/probe ports to **8000**, replacing the old incorrect port 80 configuration.
4. Replaced the stale local image tag with `${NEXUS_IMAGE}` injected by Jenkins. The deployed image tag includes commit/build identity.
5. Added namespace-scoped workload RBAC and a read-only cluster observer role.
6. Added private registry image-pull Secret and a separate `nexus-registry-push` Secret for NEXUS backend publisher credentials; customer workloads use the configured `K8S_IMAGE_PULL_SECRET`.
7. Added optional mutual-TLS Docker API access for the NEXUS Pod on Minikube. Ansible creates the CA/server/client certificates, the local manifest mounts a Secret and the Docker SDK honours `DOCKER_TLS_VERIFY`/`DOCKER_CERT_PATH`. AWS security group does not expose TCP 2376.
8. Corrected the Trivy gate so the selected severity parameter is the actual severity filter.
9. Added the self-hosted EC2/Jenkins/Minikube runbook and updated architecture / README references.
10. Updated Ansible Minikube defaults to 2 CPU, 3072 MiB and 8 GiB node disk to leave room on the fixed 25 GB EC2 root disk.

## Known boundaries and safety notes

- The Terraform module provisions a **new** EC2 instance. It does not silently adopt an existing instance. Review any Terraform plan before apply.
- Terraform defaults remain `m7i-flex.large`, `ap-south-1` and exactly 25 GB encrypted gp3 root volume.
- Jenkins, NEXUS, Prometheus and Grafana should be accessed through SSH tunnels unless a separately secured ingress is configured.
- Docker socket/API access is root-equivalent even with mTLS. Keep Jenkins restricted to trusted administrators and never run untrusted repository code with privileged Docker access.
- The Docker mTLS client bundle is secret material. It must be uploaded to Jenkins as Secret file credential `docker-client-certs`; never commit or paste it.
- A live deployment is not claimed until the user's EC2 server, Jenkins credentials, Docker Hub account and Minikube/EKS cluster have been configured and tested.


### EC2 host metrics addition

Added Ansible-managed host node-exporter and cAdvisor Compose services. Kubernetes Prometheus is configured to scrape `host.minikube.internal:9100` and `host.minikube.internal:8081`; Ansible restricts these exporter ports using UFW rules for the Minikube node and Pod CIDRs. Port 8081 avoids collision with the Jenkins listener on 8080. This is statically validated only; live name resolution/scrape verification requires the EC2/Minikube environment. The Jenkins pipeline also extracts the three expected Docker mTLS certificate files using a dedicated helper that rejects unexpected paths, members and oversized files.
