# NEXUS — Viva preparation (47 questions)

Answers are written so you can say them in your own words. Where something is **not yet demonstrated in a real
environment**, the answer says so — examiners respect honesty more than overclaiming.

## A. Project and architecture

**1. What is NEXUS in one sentence?**
A platform that takes a website from a ZIP file or a GitHub repository and automatically tests, builds, security-scans, deploys, verifies and (if needed) rolls it back, while showing the status in one dashboard.

**2. What problem does it solve?**
Deploying by hand is slow, inconsistent and easy to get wrong. NEXUS turns it into one repeatable pipeline with security checks and an undo button, so a beginner gets what a DevOps team would build.

**3. Describe the architecture.**
A FastAPI backend plus a plain HTML/CSS/JS dashboard. The backend runs one pipeline (inspect → test → build → scan → publish → deploy → verify). Jenkins only fetches GitHub code; Docker builds; Trivy scans; Kubernetes runs; Prometheus/Grafana monitor. Terraform and Ansible prepare the AWS server.

**4. Why only one pipeline for ZIP and GitHub?**
Two engines would mean two places for bugs and security gaps. Jenkins zips the checkout and calls the same `/api/deploy` endpoint, so every release gets the same validation, scanning and verification.

**5. Why FastAPI?**
It was already used in the project, it is simple, supports async (needed for streaming logs) and gives automatic validation. I kept the existing stack on purpose.

**6. Why no React/Vue for the dashboard?**
The brief asked to avoid heavy libraries. Plain JavaScript with CSS transitions keeps it small, needs no build step and is easy to explain.

**7. How does the dashboard show live progress?**
The backend streams job updates with Server-Sent Events; the browser reads the stream and updates the stage icons, progress bar and log. If streaming fails it falls back to polling.

**8. How does NEXUS avoid showing fake data?**
Each API returns `mode: live` or `unavailable`. When an integration is missing the dashboard shows “Not connected / Not configured” instead of numbers.

## B. CI/CD and Jenkins

**9. What is CI/CD?**
Continuous Integration automatically builds and tests every change; Continuous Delivery/Deployment automatically releases it. NEXUS automates that chain.

**10. What does Jenkins do here?**
It holds the GitHub credentials and, when triggered, checks out the repository, makes a ZIP with `git archive` and uploads it to NEXUS. It is the bridge for private repositories, so users never log in to GitHub.

**11. Why not build inside Jenkins?**
Then I would maintain build logic twice. Keeping Jenkins thin means one tested pipeline.

**12. What is a GitHub webhook?**
GitHub sends an HTTP POST to NEXUS on every push. NEXUS checks the HMAC-SHA256 signature (so only GitHub can trigger it), checks the repository and branch, and asks Jenkins to build.

**13. What if GitHub cannot reach my server?**
The EC2 security group is SSH-only by default. Then I use the manual “Deploy now” button, or open port 8000 only to GitHub’s published webhook IP ranges behind HTTPS (Terraform variable `webhook_ingress_cidrs`). Real webhook delivery is not yet demonstrated.

**14. How are Jenkins credentials protected?**
They are stored in Jenkins credentials (not in Git or NEXUS), injected with `withCredentials`, and the shell script uses single quotes so Groovy never prints them.

## C. Docker

**15. What is Docker and why use it here?**
Docker packages an app and its dependencies in an image that runs the same everywhere. NEXUS builds uploaded code *inside* Docker so untrusted code never runs directly on the server.

**16. Explain the generated Dockerfile.**
Static sites: nginx serving the files. Node projects: a multi-stage build — install dependencies, (optionally) run tests, `npm run build`, then copy only the build output into a small nginx image.

**17. Why do you ignore an uploaded Dockerfile?**
A user-supplied Dockerfile could run anything at build time. NEXUS always writes its own.

**18. What was the “Couldn't reach the Docker daemon … CreateFile” error?**
On Windows the Docker SDK connects through a named pipe. “CreateFile … cannot find the file” means the pipe does not exist, almost always because Docker Desktop is not running (or the active Docker context uses a different pipe).

**19. How did you fix it?**
`docker_client.py` tries `DOCKER_HOST`, then the active Docker context, then the Windows/Linux default endpoints, with a short retry. If all fail it classifies the cause (not installed, Desktop not running, engine unavailable, bad connection, permission) and shows steps to fix it. I verified the logic with simulated failures; **real Docker Desktop behaviour must be confirmed on Windows.**

**20. Why is the Docker socket dangerous?**
Whoever can use it can start a privileged container that mounts the host disk — effectively root. So NEXUS is protected by an API key, bound to localhost, and meant to run on a machine I control.

**21. How do you limit build damage?**
Memory limit on the build, npm lifecycle scripts disabled by default, one build at a time, container resource limits and `no-new-privileges` for previews, and cleanup of temporary images.

## D. ZIP security

**22. What is a ZIP bomb?**
A tiny archive that expands to gigabytes. NEXUS checks the compression ratio, per-file and total size, file count, and also counts bytes while extracting because headers can lie.

**23. What is path traversal (zip-slip)?**
Entries like `../../etc/cron.d/x` that escape the extraction folder. NEXUS rejects `..` and absolute paths and double-checks the resolved path stays inside the folder.

**24. What else is rejected?**
Symlinks and special files, `.git`, `.env`, private keys, nested archives, duplicate names, Windows reserved names (CON, NUL…), control characters, encrypted entries, and archives with no `index.html`/`package.json`.

**25. How did you test it?**
Automated fixtures: valid static and React/Vite ZIPs, plus corrupt, empty, traversal, absolute path, symlink, `.env`, nested zip, bomb, reserved name and duplicate archives — 24 tests, all passing. A real test found a bug where my own error handling hid the specific reason; I fixed it.

**26. What about malicious `package.json` scripts?**
`npm ci --ignore-scripts` by default, and NEXUS warns when `preinstall/postinstall` exist. The build still runs `npm run build` inside Docker, so it is isolated but not risk-free.

## E. Trivy and security gate

**27. What is Trivy?**
An open-source scanner for vulnerabilities (and secrets) in files and container images.

**28. How is it used?**
Two scans: filesystem (source and lock files) and the built image. NEXUS asks for JSON output, parses counts per severity, stores the report and shows it on the Security page.

**29. What is the security gate?**
A rule that stops the release before publishing if findings reach a threshold. `TRIVY_FAIL_ON_SEVERITY` can be CRITICAL, HIGH, MEDIUM, LOW or NONE (report only).

**30. What if Trivy is not installed?**
The Scan stage is marked **skipped** and the dashboard says Trivy is not installed. NEXUS never invents results.

**31. Why do you decide pass/fail instead of Trivy’s exit code?**
So the threshold is configurable and the same for source and image scans.

## F. Kubernetes and rollback

**32. What is Kubernetes / Minikube?**
Kubernetes runs and manages containers (restarts, scaling, updates). Minikube is a single-node cluster for learning/demo, which fits one 8 GB server.

**33. What does NEXUS create in Kubernetes?**
A Deployment (2 replicas, readiness probe, resource limits, 5 revisions kept) and a Service, using a versioned image tag.

**34. Why patch instead of replace?**
Replacing a Service fails because some fields (like clusterIP) are immutable. Patch updates only what changed. (This fixes a bug in the earlier version.)

**35. How does rollback work?**
Each rollout leaves an old ReplicaSet (a “revision”). `kubectl rollout undo` copies the old pod template back into the Deployment. The Python client has no such call, so NEXUS does the same with a JSON patch that replaces the pod template.

**36. How do you know a rollback worked?**
After patching, NEXUS waits for the rollout and re-reads the pods: all must be Ready and running the target image. Otherwise the job is marked **failed** with the observed state. It never reports success just because the API accepted the patch. I tested the logic against a simulated cluster; a real-cluster rollback must be shown in the demo.

**37. What is a readiness probe?**
A check Kubernetes runs before sending traffic to a pod. NEXUS’ nginx exposes `/healthz` for it.

**38. Where is release history stored?**
In a JSON file in `NEXUS_DATA_DIR`, written atomically, so history survives restarts. For many users it should move to a database.

## G. Monitoring

**39. What do Prometheus and Grafana do?**
Prometheus scrapes metrics every 15 s and stores them; Grafana draws dashboards from it.

**40. What is monitored?**
Host CPU/memory/disk (node-exporter), containers (cAdvisor), NEXUS request rate/errors/job counts (`/metrics`), pod and deployment health (kube-state-metrics) and health endpoints (blackbox exporter).

**41. How does NEXUS use Prometheus?**
It queries the Prometheus HTTP API for panel values and 30-minute series (drawn as SVG charts). If Prometheus is down the page shows “Not connected”.

## H. Infrastructure as code and GitOps

**42. Terraform vs Ansible?**
Terraform *creates* cloud resources (VPC, EC2, security group). Ansible *configures* software on the machine (Docker, Jenkins…). Terraform is declarative state; Ansible is idempotent steps.

**43. How is the AWS server kept safe?**
SSH only from my IP (a validation blocks 0.0.0.0/0), IMDSv2 required, encrypted 25 GB disk, no keys in files, and Jenkins/NEXUS reached through an SSH tunnel.

**44. What does “idempotent” mean?**
Running the playbook twice gives the same result; the second run should change nothing.

**45. What is Argo CD and is it complete?**
GitOps: Argo CD watches a Git repo and makes the cluster match it. The NEXUS code path (commit manifests, read sync/health) exists, but I could not run it end-to-end, so it is documented as **optional/unverified** until demonstrated.

## I. Honesty and challenges

**46. What were the biggest challenges?**
Docker connectivity on Windows, making ZIP intake safe, keeping the dashboard honest when integrations are missing, and verifying rollback from real cluster state. Also: the old server exposed the whole project folder (including `.env`) over HTTP — I fixed it by serving only `frontend/` and `assets/`.

**47. What are the limitations?**
Single-server job state (JSON file), no per-user accounts (one shared API key), no rate limiting, Next.js static export not supported, previews run on the same host, and several integrations (real Docker builds, Trivy, Kubernetes, Jenkins/webhook, Argo CD, Terraform, Ansible) were implemented and unit-tested but not run end-to-end in the development sandbox.
