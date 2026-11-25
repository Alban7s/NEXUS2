# NEXUS: An Automated, Secure CI/CD Deployment Platform with Monitoring and Rollback

*Final-year project report*  ·  Student: ____________  ·  Supervisor: ____________  ·  Year: ______

## 1. Abstract
Deploying a web application normally requires a developer to build it, test it, check it for security problems,
publish a container image, update a Kubernetes cluster, verify that it works and keep an undo option. NEXUS automates this chain behind one
web dashboard. A user uploads a website as a ZIP file, or an administrator connects a GitHub repository through Jenkins; both routes
feed a single pipeline that safely extracts the code, runs its tests, builds a Docker image, scans it with Trivy, publishes it, deploys it
(to Kubernetes, through Argo CD, or as a local Docker preview) and verifies health. Releases are recorded, can be rolled back to a
previous Kubernetes revision with the result verified from the real cluster, and are monitored with Prometheus and Grafana. The AWS server is created with Terraform
and configured with Ansible. The system deliberately never shows invented data: unavailable integrations are displayed as “Not connected”.
The current uploaded-source validation run completed 104 backend tests successfully, with two Docker integration tests skipped because no Docker SDK/daemon is installed in the sandbox. The stubbed-handler checks passed 26/26. YAML/JSON and shell-template checks passed. Real Docker, Trivy, Jenkins, Kubernetes, Terraform, Ansible and AWS operations remain BLOCKED until run on the configured EC2 environment; browser navigation was blocked by the sandbox browser policy.

## 2. Introduction
DevOps joins software development and operations through automation. Continuous Integration/Continuous Delivery (CI/CD) pipelines, containers and
orchestration are industry standard, but they are hard for beginners to assemble. NEXUS packages these practices in one understandable system built
on FastAPI, Docker, Kubernetes and open-source tooling.

## 3. Problem statement
Manual deployment is slow, error-prone and inconsistent; security checks are often skipped; failed releases are hard to undo; and small teams lack a single view of
build, security, cluster and monitoring status. Existing all-in-one platforms are costly or complex to operate on a single small server (2 vCPU, 8 GB RAM, 25 GB disk).

## 4. Objectives
1. Accept website source by ZIP upload and by an administrator-managed GitHub integration, through **one** shared pipeline.
2. Handle untrusted uploads safely (path traversal, ZIP bombs, symlinks, secrets, resource limits).
3. Build, test, scan (Trivy) and publish container images with configurable security gates.
4. Deploy to Kubernetes with verified health and **verified rollback**.
5. Provide monitoring (Prometheus, Grafana) and honest integration status.
6. Provide reproducible infrastructure (Terraform) and configuration (Ansible) for a 25 GB Ubuntu EC2 server.
7. Fix the Windows Docker Desktop connectivity problem of the previous version.

## 5. Existing system
The earlier NEXUS version had a dashboard and a FastAPI pipeline that built a Docker image and optionally deployed to Kubernetes. Review of its source found:
the Docker client used a single default connection (causing “CreateFile: The system cannot find the file specified” on Windows when Docker Desktop was not
running, with no explanation); ZIP validation lacked compression-ratio, per-file and symlink checks and relied on `extractall`; the static file server was mounted at the
project root, which would expose `.env` and source files over HTTP; Service updates used *replace*, which Kubernetes rejects for immutable fields; builds could not stream logs live;
job history was lost on restart; there was no rollback, no image scan of the built image, and CORS was open to every origin.

## 6. Proposed system
NEXUS 1.0 keeps the FastAPI + plain-JavaScript stack and adds: a hardened archive module; a Docker connection/diagnostics layer; a Trivy service with a configurable gate;
persistent job history; Kubernetes revision history and verified rollback; a GitHub → Jenkins → NEXUS path using HMAC-signed webhooks; API-key authentication; Prometheus metrics;
Terraform, Ansible, monitoring and Argo CD configuration; and a redesigned animated, responsive dashboard with twelve sections.

## 7. System architecture
See `docs/architecture/architecture.md` (Mermaid diagrams). The architecture has two separate CI/CD paths. The root `Jenkinsfile` deploys the NEXUS platform itself: GitHub → Jenkins → tests → Docker build → Trivy → Docker Hub → Kubernetes → rollout and `/healthz`. The `jenkins/Jenkinsfile` path submits customer repository source to NEXUS's shared release engine: ZIP/GitHub → NEXUS → validate → test → build → Trivy → registry → Kubernetes/GitOps → health checks. Terraform creates the AWS host, Ansible installs/configures the software, and Prometheus/Grafana observe runtime metrics. Argo CD (optional) synchronises customer Git manifests.
Deploy mode is chosen at run time: **gitops**, **direct** (Kubernetes API), or **local** (Docker preview).

## 8. Technology stack
| Area | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn, httpx, pydantic |
| Frontend | HTML, CSS (transitions/keyframes), vanilla JavaScript, SVG charts |
| Containers | Docker Engine / Docker Desktop, docker-py SDK, nginx |
| Orchestration | Kubernetes (Minikube/EKS), kubernetes Python client |
| CI | Jenkins Declarative Pipeline (root NEXUS self-deployment and customer-app pipeline), GitHub polling/webhooks |
| Security | Trivy, HMAC-SHA256, API key, ZIP intake policy |
| Monitoring | Prometheus, Grafana, node-exporter, cAdvisor, blackbox exporter, kube-state-metrics |
| IaC / config | Terraform (AWS), Ansible |
| GitOps | Argo CD (optional) |
| Testing | unittest/pytest, Playwright (headless Chromium) |

## 9. Module descriptions
* **archive.py** – validates ZIPs without extracting, then extracts with byte counting. Rejects traversal, symlinks, bombs, secrets, nested archives, reserved names, duplicates.
* **docker_client.py** – finds the Docker endpoint (DOCKER_HOST → CLI context → platform defaults), retries, supports mutual-TLS remote Docker Engine clients, redacts endpoint credentials and classifies failures.
* **pipeline.py** – the single orchestrator (extract, inspect, test, build, security, publish, deploy, verify) and the three deploy modes.
* **trivy.py** – filesystem and image scans, JSON report parsing, severity counts, configurable gate.
* **k8s_ops.py / rollback.py** – workload overview, revision history, rollback by replacing the pod template, rollout and pod verification.
* **jenkins.py** – validated Jenkins trigger and GitHub signature verification.
* **jobs.py** – job state, live logs, log redaction, persistent history.
* **metrics.py** – NEXUS `/metrics` for Prometheus.
* **app.py** – REST API, authentication, security headers, optional CORS, static serving (frontend only).
* **frontend/** – twelve views: Dashboard, New Deployment, ZIP Upload, GitHub Deployment, Deployment History, CI/CD Pipeline, Kubernetes, Security, Monitoring, Infrastructure, GitOps, Settings.
* **root `Jenkinsfile`** – deploys NEXUS itself from GitHub to Docker Hub and Kubernetes; **`jenkins/Jenkinsfile`** submits customer apps to NEXUS.
* **terraform/, ansible/, monitoring/, argocd/, k8s/** – infrastructure, host automation, metrics stack, optional GitOps and platform runtime manifests.

## 10. Implementation
*Pipeline.* Each stage updates a job object; the browser follows it over Server-Sent Events. Blocking Docker/Kubernetes work runs in worker threads; at most `NEXUS_MAX_CONCURRENT_JOBS` (default 1) builds run at once to protect the 2 vCPU / 8 GB server.
*Build isolation.* NEXUS always writes its own Dockerfile and `.dockerignore`; uploaded Dockerfiles are ignored. Node builds are multi-stage, run `npm ci --ignore-scripts` by default, apply a build memory limit, and tests run in a separate `test` target.
*Verification.* Local mode health-checks `/healthz` inside the container; direct mode waits for the rollout and re-reads pod readiness and image; GitOps mode waits for Argo CD Synced/Healthy.
*Rollback.* Equivalent of `kubectl rollout undo --to-revision`: a JSON patch replaces `/spec/template` with the chosen ReplicaSet’s template (minus the pod-template-hash label); success requires all pods Ready on the target image.
*Honesty.* API envelopes carry `mode: live|unavailable`; the UI renders missing integrations as “Not connected / Not configured”.
*Secrets.* Environment variables and Jenkins/Kubernetes Secret objects; tokens are redacted in logs; Git tokens are passed via an environment-supplied HTTP header, never in URLs. The Minikube NEXUS Pod reaches the host Docker daemon through a mutual-TLS API listener restricted by UFW to the Minikube node/Pod CIDRs; the EC2 security group does not expose TCP 2376. Docker API credentials remain root-equivalent and are protected as Jenkins Secret-file material.
*Platform self-deployment.* The root Jenkinsfile runs the platform tests, creates a commit/build-tagged image, applies a configurable Trivy severity gate, pushes Docker Hub, updates the Kubernetes namespace/runtime/registry/Docker-client Secrets, applies the NEXUS manifest and verifies rollout plus `/healthz`. The self-deployment pipeline is not the same as the customer pipeline.

## 11. Testing
Full details and evidence: `docs/testing/TEST_RESULTS.md`.
* Backend suite: **104 PASS, 2 SKIPPED, 0 FAIL**. The two skipped cases require a Docker SDK/daemon and are real Docker static-site and React/Vite integration tests.
* Stubbed FastAPI handler check: **26/26 PASS**. A local Uvicorn smoke returned `/healthz` = `ok`, `/` HTTP 200 and `/api/deploy/config` HTTP 200.
* Python compile and JavaScript syntax checks: PASS.
* Kubernetes/Ansible/monitoring/Argo/Compose YAML: 35 files parsed; Grafana dashboard JSON parsed: PASS.
* Docker TLS Ansible template: daemon JSON rendered/parsed and certificate setup Bash passed `bash -n`: PASS. This is static template validation, not a live Docker TLS handshake.
* Root Jenkinsfile: static contract tests PASS. Jenkins/Groovy pipeline lint/run is BLOCKED because Jenkins/Groovy is not installed.
* Playwright browser check: BLOCKED because Chromium navigation to the local test URL returned `ERR_BLOCKED_BY_ADMINISTRATOR` under sandbox policy.
* **BLOCKED** (environment): Docker build/daemon, real Trivy scan, Docker Hub push/pull, Minikube/kubectl apply and rollout/rollback, real Jenkins pipeline, Terraform fmt/init/validate/plan, Ansible syntax/check/apply, Prometheus/Grafana runtime, EKS/AWS and Argo CD sync.

The tests explicitly distinguish mocked/structural evidence from an actual deployment. The full status table is in `docs/testing/TEST_RESULTS.md`.

## 12. Results
In the sandbox, code-level objectives were tested: unsafe archives were rejected with specific reasons; Dockerfile and Kubernetes manifests were generated as designed; the NEXUS self-deployment contract and supporting assets passed automated tests; Docker mTLS configuration templates rendered and passed shell syntax checks; failure paths (Docker unavailable, build failure, failed health check, failed security gate, unhealthy rollback) end as clear failed jobs rather than false success. Actual external deployment results have not been measured. The student should update this section with build time, image size, scan counts, rollout time and rollback time after running the pipeline on EC2. **No real Docker/Kubernetes/AWS success is claimed in this report.**

## 13. Limitations
* Real-environment integrations are implemented but not yet demonstrated (see Section 11).
* Job history is a JSON file (single process); no per-user accounts (one shared API key); no rate limiting.
* Uploaded code is built inside Docker, which isolates it from the host but is not a hardened sandbox; the build container has network access to fetch npm packages.
* Mounting the Docker socket is root-equivalent; NEXUS must run only on a trusted, access-controlled machine.
* Next.js static-export projects are not supported; backend (server-side) applications beyond Next.js servers are out of scope.
* GitHub webhooks require a publicly reachable HTTPS endpoint, which conflicts with a locked-down server unless configured deliberately.
* Argo CD integration is optional and unverified.

## 14. Future scope
Database-backed history and multi-user roles; build agents separate from the control plane (gVisor/Kaniko/BuildKit rootless); image signing and SBOMs; automated canary/blue-green releases; Slack/email notifications; cost reporting; Helm charts; a hosted reverse proxy with TLS for webhooks.

## 15. Conclusion
NEXUS demonstrates an end-to-end, security-conscious DevOps workflow that a beginner can understand and operate on one small server. Its distinguishing design choices are a single shared pipeline for both submission methods, defensive handling of untrusted input, verification of every claimed result against real state, and honest reporting of what is not connected. Completing the real-environment demonstrations is the remaining step before final submission.

## 16. References
1. Docker Inc., *Docker Engine documentation* — https://docs.docker.com/engine/
2. Docker SDK for Python — https://docker-py.readthedocs.io/
3. The Kubernetes Authors, *Kubernetes documentation: Deployments, rollbacks, probes* — https://kubernetes.io/docs/
4. Jenkins project, *Pipeline syntax and credentials* — https://www.jenkins.io/doc/
5. Aqua Security, *Trivy documentation* — https://trivy.dev/
6. Prometheus Authors, *Prometheus documentation* — https://prometheus.io/docs/
7. Grafana Labs, *Grafana provisioning* — https://grafana.com/docs/grafana/latest/administration/provisioning/
8. HashiCorp, *Terraform AWS provider* — https://registry.terraform.io/providers/hashicorp/aws/latest/docs
9. Red Hat, *Ansible documentation* — https://docs.ansible.com/
10. Argo Project, *Argo CD documentation* — https://argo-cd.readthedocs.io/
11. GitHub, *Webhooks: validating payloads* — https://docs.github.com/webhooks
12. Ramírez, S., *FastAPI* — https://fastapi.tiangolo.com/
13. OWASP, *File Upload Cheat Sheet* and *Zip Slip* — https://cheatsheetseries.owasp.org/
14. Kim, G., Humble, J., Debois, P., Willis, J., *The DevOps Handbook*, IT Revolution Press, 2016.
