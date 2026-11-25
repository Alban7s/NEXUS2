# NEXUS architecture

## 1. End-to-end flow

The repository contains **two intentionally separate CI/CD paths**: the root Jenkinsfile deploys NEXUS itself, while `jenkins/Jenkinsfile` submits customer application source to the shared NEXUS release pipeline.

```mermaid
flowchart TB
  subgraph PlatformSelfDeploy["NEXUS platform self-deployment"]
    GH["GitHub: NEXUS repository"] --> JK["Root Jenkinsfile"]
    JK --> TEST["Compile + pytest"] --> BUILD["Docker build: commit/build tag"]
    BUILD --> SEC["Trivy image scan + severity gate"]
    SEC --> PUSH["Docker Hub registry"] --> K8S["Minikube / EKS: namespace nexus"]
    K8S --> HEALTH["Rollout + /healthz verification"]
  end

  subgraph CustomerDeploy["Customer application deployments through NEXUS"]
    ZIP["ZIP upload"] --> NX["NEXUS FastAPI + dashboard"]
    REPO["Administrator GitHub repo"] --> JKC["jenkins/Jenkinsfile"] --> NX
    NX --> VALIDATE["Safe archive + project detection"] --> TESTC["Build/test in isolated Docker build"]
    TESTC --> TRIVYC["Trivy filesystem/image scan"] --> PUB["Registry publish"] --> DEPLOY["Kubernetes direct or GitOps"]
    DEPLOY --> HEALTHC["Pod rollout + health verification"]
  end
  NX -. "mTLS Docker API from Pod to host" .-> DOCKER["Host Docker Engine :2376"]
  HEALTHC --> MON["Prometheus"] --> GRAF["Grafana"]
  NX -- "/metrics" --> MON
  TF["Terraform: AWS VPC, SG, EC2 (25 GB)"] --> HOST["Ubuntu EC2 host"]
  AN["Ansible: Docker, Java 21, Jenkins, Trivy, kubectl, Minikube"] --> HOST
  HOST --> JK
  HOST --> DOCKER
  ARGO["Argo CD (optional GitOps sync)"] --> DEPLOY
```

## 2. Who does what

| Component | Responsibility | Not its job |
|---|---|---|
| **NEXUS** | Accepts ZIPs, runs the single pipeline, stores history, shows status, performs rollback | Holding end-user GitHub logins |
| **Jenkins** | Root `Jenkinsfile` tests/builds/scans/pushes/deploys NEXUS; `jenkins/Jenkinsfile` checks out customer code and submits it to the shared NEXUS pipeline | Replacing NEXUS's shared release logic |
| **Docker** | Jenkins builds the NEXUS platform image; NEXUS uses a host Docker Engine via an optional mutual-TLS endpoint for isolated customer builds | Exposing an unauthenticated Docker socket/API |
| **Trivy** | Scans the source tree and the built image; NEXUS applies the gate | Deciding policy (the threshold is NEXUS config) |
| **Kubernetes** | Runs the app, rolls out and rolls back | |
| **Argo CD** (optional) | Keeps the cluster equal to the manifests in Git | Building images |
| **Prometheus / Grafana** | Collect and display metrics | Making deployment decisions |
| **Terraform** | Creates the AWS network + EC2 server (run once, from your laptop) | Installing software |
| **Ansible** | Installs/configures software on that server | Creating AWS resources |

## 3. Deploy modes (chosen automatically at release time)

| Mode | When | What happens |
|---|---|---|
| `gitops` | `GIT_REPO_URL` + `ARGOCD_URL` + registry set | Push image, commit manifests to Git, wait for Argo CD Synced + Healthy |
| `direct` | Registry (or `K8S_IMAGE_SOURCE=minikube`) set **and** the cluster API answers | Push/load image, create-or-patch Deployment + Service, wait for rollout, verify pods |
| `local` | Nothing else configured | Run the image as a Docker container, health-check it from inside |

## 4. Release sequence

```mermaid
sequenceDiagram
  participant U as User / Jenkins
  participant N as NEXUS API
  participant A as archive.py
  participant D as Docker
  participant T as Trivy
  participant K as Kubernetes
  U->>N: POST /api/deploy (ZIP)
  N->>A: validate (limits, traversal, symlinks, bombs)
  A-->>N: summary or rejection (400 + code)
  N-->>U: 200 {jobId}
  N->>A: safe_extract -> temp dir
  N->>D: build --target test (if project has tests)
  N->>D: build final image (memory limited)
  N->>T: fs scan + image scan -> JSON reports
  T-->>N: counts per severity
  alt gate fails
    N-->>U: job failed at "security" (nothing published)
  else gate passes
    N->>K: apply Deployment/Service
    N->>K: wait for rollout, read pods back
    N-->>U: success only if pods Ready on the new image
  end
```

## 5. Rollback sequence

```mermaid
sequenceDiagram
  participant U as User
  participant N as NEXUS
  participant K as Kubernetes API
  U->>N: POST /api/k8s/deployments/{name}/rollback {revision}
  N->>K: list ReplicaSets (revision history)
  N->>K: JSON-patch /spec/template = old ReplicaSet template
  N->>K: poll rollout status
  N->>K: list pods: Ready? image == target?
  alt verified
    N-->>U: rollback success (logged in history)
  else not healthy
    N-->>U: rollback FAILED with the observed cluster facts
  end
```
