# NEXUS demonstration script (8–10 minutes)

**Before you start (10 min earlier):** Docker Desktop running · NEXUS running (`http://127.0.0.1:8000`) ·
`python tests/fixtures/build_fixtures.py` done · Trivy installed · (EC2 demo) SSH tunnel open · Minikube started ·
monitoring stack up. Open these tabs: NEXUS Dashboard, Grafana. Keep `static-site.zip`, `react-vite-app.zip`, `path-traversal.zip` on the desktop.
Do a full dry run once; the first Docker build is slow because base images download.

| Min | Do | Say | Expected result | If it fails (fallback) |
|---|---|---|---|---|
| 0:00 | Show **Dashboard** | “NEXUS turns a ZIP or a GitHub repo into a tested, scanned, deployed and monitored release. Everything shown is real backend data; missing integrations say *Not connected*.” | Hero, live status pill “N of M services connected”, release counters | Backend not reachable → start `uvicorn`; explain pill turns red honestly |
| 0:45 | **Settings → Docker Engine** | “This fixes the Windows error ‘cannot find the file specified’: NEXUS checks DOCKER_HOST, the Docker context and defaults, and tells me why Docker is unreachable.” | Connected with engine version | If Docker Desktop is off, show the diagnosis panel (“Docker Desktop is not running”) — that *is* the feature — then start Docker and click **Check again** |
| 1:30 | **ZIP Upload** → `path-traversal.zip` | “First, safety. This archive tries to write outside its folder.” | Red message: unsafe path rejected; nothing extracted | The 400 message is the proof; show `docs/testing` tests as backup |
| 2:15 | Upload `static-site.zip` → **Start release** | “Same pipeline for ZIP and GitHub: extract safely, inspect, test, build, scan, publish, deploy, verify.” | Stage icons turn green one by one with live log; **Open preview** link | Build slow → talk through the stages; if Docker fails, show the log + error and the failed stage (NEXUS fails clearly) |
| 4:00 | Click the preview link | “Health check ran inside the container on /healthz before NEXUS said success.” | The static page loads | Use `http://localhost:<port>` from the log |
| 4:30 | **Security** | “Trivy scanned the source and the image. The gate here fails the release at CRITICAL; configurable.” | Severity bars from the scan, scans table → **Findings**, **Download JSON** | Trivy missing → page says “Trivy is not installed”: explain it never fakes results; show gate setting |
| 5:30 | **Deployment History** → click the release | “History persists across restarts.” | Drawer with stages, health, scan summary, log | |
| 6:00 | (Kubernetes available) release `react-vite-app.zip` twice with a visible change | “Direct mode creates a Deployment and Service; each release is a revision.” | Kubernetes page shows deployment, pods, services | If no cluster: show the *Not connected* banner and explain local mode |
| 7:00 | **Kubernetes → Revisions and rollback → Roll back** older revision | “NEXUS swaps the pod template back, waits for the rollout and re-reads the pods. It only says success if they’re Ready on the old image.” | Rollback panel stages go green; history shows a *rollback* entry | If rollback fails, the result shows the observed pod facts — explain this honesty |
| 8:00 | **Monitoring** + Grafana tab | “Prometheus scrapes NEXUS, the host, containers and Kubernetes; Grafana shows the same data.” | CPU/memory charts, scrape targets UP | Prometheus down → *Not connected* state with start command |
| 8:45 | **Infrastructure** / **GitHub** quickly | “Terraform builds the 25 GB EC2 server with SSH from my IP only; Ansible installs Docker, Jenkins, Java 21. GitHub pushes reach NEXUS through a signed webhook via Jenkins, with no per-user GitHub login.” | Disk gauge; GitHub checklist | Webhook cannot reach EC2 → use **Deploy now**; say webhooks need a reachable HTTPS endpoint |
| 9:30 | Close | “Limitations: single server job store, one shared API key, some integrations demonstrated only in unit tests; see the test report.” | | |

## Likely examiner interruptions
* *“Is this real?”* — Open `docs/testing/TEST_RESULTS.md`; point out BLOCKED vs PASS and what you ran yourself.
* *“What if the scan finds a critical bug?”* — Set `TRIVY_FAIL_ON_SEVERITY=HIGH`, upload a project with an old dependency: the release stops at **Scan** and nothing is published.
