# NEXUS — DevOps deployment platform

NEXUS takes a website (a **ZIP upload** or a **GitHub repository**), then **tests, builds, security-scans,
publishes, deploys, verifies and — if needed — rolls back** the release, and shows everything in one dashboard.
GitHub and ZIP releases run through the **same single pipeline**.


> Honest status: see [docs/testing/TEST_RESULTS.md](docs/testing/TEST_RESULTS.md). The source tests can be run locally; live Docker, Jenkins, Trivy, Minikube/EKS, Terraform and Ansible operations require the relevant tools, credentials and host. Do not treat mocked tests or YAML parsing as proof of a real deployment.

## Features

* **Dashboard** with live status (every number comes from the backend; otherwise it says *Not connected*).
* **ZIP upload** — static HTML/CSS/JS, React/Vite and other Node frontends. Hardened intake (path traversal,
  symlinks, ZIP bombs, secrets, nested archives, file-count/size limits).
* **Platform self-deployment** — the root [`Jenkinsfile`](Jenkinsfile) checks out NEXUS, tests it, builds a commit-tagged image, scans it with Trivy, pushes to Docker Hub and deploys/health-checks NEXUS on Minikube or EKS.
* **Customer GitHub deployment** — [`jenkins/Jenkinsfile`](jenkins/Jenkinsfile) submits an administrator-managed app repository to the NEXUS shared release API. This is deliberately separate from the root platform pipeline.
* **Docker connection diagnostics** — tells you *why* Docker is unreachable (not installed / Docker Desktop not running /
  permission / bad `DOCKER_HOST`), including the Windows named-pipe error.
* **Trivy** source + image scanning, severity counts, JSON reports, configurable pipeline gate.
* **Kubernetes** deployment, status, **revision history and verified rollback**.
* **Monitoring** — Prometheus + Grafana configuration, NEXUS `/metrics`, live charts in the dashboard.
* **Infrastructure as code** — Terraform (AWS EC2, 25 GB disk) and Ansible (Docker, Java 21, Jenkins, Trivy, kubectl and Minikube). Ansible can configure an optional mutual-TLS Docker API for the NEXUS Pod; its client bundle is stored as a Jenkins Secret file and must never be committed.
* **GitOps** — optional Argo CD integration.

## Quick start (Windows, local)

```powershell
# 1. Install Python 3.10+ and Docker Desktop (start it and wait for "Engine running")
cd NEXUS
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # edit if you want; everything is optional
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000  ->  Settings page shows whether Docker is reachable
```

Then **ZIP Upload** → choose `tests/fixtures/_generated/static-site.zip` (create it with
`python tests/fixtures/build_fixtures.py`). Full instructions: [docs/setup/SETUP_GUIDE.md](docs/setup/SETUP_GUIDE.md). For the EC2 + Jenkins + Minikube self-deployment workflow, follow [docs/setup/SELF_HOSTED_CICD.md](docs/setup/SELF_HOSTED_CICD.md).

## Quick start (Docker Compose)

```bash
cp .env.example .env     # set NEXUS_API_KEY
docker compose up -d --build
```
The compose file mounts the Docker socket. **That is root-equivalent access to the host** — see Security below.

## Project layout

```
backend/            FastAPI app, services (pipeline, archive, docker_client, trivy, k8s_ops, jenkins, ...)
backend/tests/      unit + API + Docker integration tests
frontend/           dashboard (HTML/CSS/vanilla JS; no framework, no build step)
assets/             logo and illustration SVGs
k8s/                Kubernetes manifests, namespace and restricted RBAC
Jenkinsfile         Root pipeline to deploy the NEXUS platform itself
jenkins/            Jenkinsfile for customer-app GitHub deployments through NEXUS
terraform/          AWS infrastructure
ansible/            server configuration
monitoring/         Prometheus, Grafana, exporters, kube-state-metrics
argocd/             Argo CD ApplicationSet + notes (optional)
deploy/             systemd unit for EC2
docs/               architecture, report, setup, viva, demo, testing
tests/              fixtures, UI smoke test, stubbed-handler check
```

## Security model (short)

* `NEXUS_API_KEY` protects every `/api` route (set it before exposing NEXUS). Webhooks use an HMAC signature instead.
* Only `frontend/` and `assets/` are served — never the project root (the old build served `.env` and source files).
* CORS is **off** unless `NEXUS_CORS_ORIGINS` is set (no `*`).
* Uploaded code is never run on the NEXUS host: it is built in Docker, npm lifecycle scripts are disabled by default,
  and an uploaded `Dockerfile` is ignored.
* Secrets come from environment variables only; logs redact tokens/passwords.
* **Docker socket = root.** Anyone who can talk to Docker can mount the host filesystem. Run NEXUS only on a machine you own,
  keep port 8000 on loopback / behind an SSH tunnel, and give Jenkins/NEXUS users only to trusted admins.

## Tests

```bash
python -m unittest discover -s backend/tests -t .        # logic tests (no network needed)
pip install -r requirements-dev.txt && python -m pytest backend/tests   # also runs API tests
python tests/stubbed_handlers.py
python tests/e2e/ui_smoke.py                              # needs: pip install playwright && playwright install chromium
```
