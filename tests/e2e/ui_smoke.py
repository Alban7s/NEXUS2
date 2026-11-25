"""Headless-browser UI smoke test (Playwright).

IMPORTANT: this serves the REAL frontend files but intercepts every /api request with
canned MOCK responses (see MOCK below). It therefore proves that the UI renders, routes,
animates, handles errors and that each button calls the right endpoint. It does NOT prove
the backend works - the backend is covered by backend/tests.

Usage:  python tests/e2e/ui_smoke.py [--shots docs/testing/screenshots]
"""
import argparse, json, re, sys, threading, time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
NOW = time.time()


def env(data=None, mode="live", source="MOCK", error=None):
    return {"mode": mode, "source": source, "updatedAt": "2026-01-01T00:00:00Z", "data": data, "error": error}


STEPS = ["upload", "inspect", "test", "build", "security", "publish", "deploy", "verify"]


def job(i, app, status, mode="local", kind="deploy", steps=None, error=None, age=300):
    st = steps or {s: ("success" if status == "success" else "pending") for s in STEPS}
    return {"id": f"job-{i}-1", "app": app, "kind": kind, "source": "zip", "status": status, "mode": mode,
            "createdAt": NOW - age, "finishedAt": NOW - age + 80, "durationSec": 80, "stepOrder": STEPS if kind == "deploy" else ["plan", "rollback", "verify"],
            "steps": st, "previewUrl": "http://localhost:49153" if status == "success" else None, "error": error,
            "imageTag": f"{app}:20260101-120000-{i}", "project": {"framework": "React (Vite)"}, "scans": [], "health": {"verified": status == "success"}, "deployment": None, "git": None}


JOBS = [job(3, "portfolio", "success", age=120),
        job(2, "shop-ui", "failed", steps={**{s: "success" for s in STEPS[:3]}, "build": "failed", "security": "pending", "publish": "pending", "deploy": "pending", "verify": "pending"}, error="Image build failed: npm ERR! missing script: build", age=3600),
        job(1, "portfolio", "success", mode="direct", age=90000)]

K8S_SNAP = {"clusterStatus": "HEALTHY", "nodes": {"ready": 1, "total": 1}, "pods": {"running": 2, "pending": 0, "failed": 0, "total": 2}, "deployments": {"available": 1, "total": 1}, "namespaces": 4, "restarts24h": 0}
WORKLOADS = {"namespace": "default", "deployments": [{"name": "portfolio", "namespace": "default", "images": ["portfolio:v3"], "desired": 2, "ready": 2, "available": 2, "updated": 2, "revision": 3, "healthy": True, "managedByNexus": True}],
             "pods": [{"name": "portfolio-abc-1", "phase": "Running", "ready": True, "restarts": 0, "app": "portfolio", "image": "portfolio:v3", "node": "minikube"}],
             "services": [{"name": "portfolio", "type": "ClusterIP", "clusterIP": "10.0.0.1", "ports": [{"port": 80, "targetPort": "80", "nodePort": None, "protocol": "TCP"}], "external": None}]}
REVISIONS = [{"revision": 3, "image": "portfolio:v3", "images": ["portfolio:v3"], "createdAt": "2026-01-01T10:00:00Z", "replicas": 2, "readyReplicas": 2, "current": True, "knownGood": True},
             {"revision": 2, "image": "portfolio:v2", "images": ["portfolio:v2"], "createdAt": "2026-01-01T09:00:00Z", "replicas": 0, "readyReplicas": 0, "current": False, "knownGood": True}]
SEC = {"trivy": {"installed": True, "version": "0.0-test", "enabled": True}, "gate": {"failOn": "CRITICAL", "ignoreUnfixed": False},
       "totals": {"CRITICAL": 0, "HIGH": 3, "MEDIUM": 5, "LOW": 7, "UNKNOWN": 0}, "scanCount": 1,
       "scans": [{"scanner": "trivy", "scanType": "image", "target": "portfolio:v3", "image": "portfolio:v3", "scannedAt": "2026-01-01T10:00:00+00:00", "status": "completed", "counts": {"CRITICAL": 0, "HIGH": 3, "MEDIUM": 5, "LOW": 7, "UNKNOWN": 0}, "total": 15, "gate": {"passed": True, "reason": "ok"}, "reportFile": "job-3-1-image.json", "jobId": "job-3-1", "app": "portfolio"}]}
SERIES = [{"label": "CPU utilization", "unit": "%", "points": [[NOW - 1800 + i * 60, 20 + (i * 7) % 30] for i in range(30)]},
          {"label": "Memory utilization", "unit": "%", "points": [[NOW - 1800 + i * 60, 40 + i * 0.5] for i in range(30)]},
          {"label": "NEXUS request rate", "unit": "req/s", "points": None}]


def integrations(live):
    mk = lambda k, n, s, d: {"key": k, "name": n, "status": s, "detail": d}
    return {"items": [mk("zip", "ZIP release pipeline", "available", "Built in"),
                      mk("docker", "Docker Engine", "connected" if live else "not_connected", "Reachable" if live else "Docker Desktop is installed but not running."),
                      mk("jenkins", "Jenkins", "connected" if live else "not_configured", "ok" if live else "JENKINS_URL is not set."),
                      mk("kubernetes", "Kubernetes", "connected" if live else "not_connected", "ok"),
                      mk("trivy", "Trivy scanner", "installed" if live else "not_installed", "ok"),
                      mk("prometheus", "Prometheus", "connected" if live else "not_configured", "ok"),
                      mk("terraform", "Terraform", "external", "Run from your terminal")]}


def make_mock(live, calls):
    def handler(route):
        req = route.request
        path = re.sub(r"^https?://[^/]+", "", req.url).split("?")[0]
        calls.append((req.method, path))
        def ok(body, status=200): route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        def off(msg): ok(env(None, "unavailable", "MOCK", msg))
        if path == "/api/session": return ok(env({"authRequired": False, "authenticated": True, "version": "1.0.0"}))
        if path == "/api/health":
            return ok(env({"status": "CONNECTED", "checks": {"docker": {"connected": live}, "kubernetes": {"connected": live}, "prometheus": {"connected": live}, "github": {"configured": live}}}))
        if path == "/api/integrations": return ok(env(integrations(live)))
        if path == "/api/deploy/jobs": return ok(env(JOBS if live else []))
        if path == "/api/deployments": return ok(env(JOBS + [job(4, "portfolio", "success", mode="direct", kind="rollback", age=60)] if live else []))
        if path == "/api/deploy/config": return ok(env({"mode": "local", "label": "Local preview target", "detail": "x", "requiresApiKey": False, "maxUploadMb": 100}))
        if path == "/api/kubernetes": return ok(env(K8S_SNAP)) if live else off("Kubernetes cluster is unavailable.")
        if path == "/api/k8s/workloads": return ok(env(WORKLOADS)) if live else off("Cluster unavailable")
        if re.match(r"/api/k8s/deployments/[^/]+/revisions", path): return ok(env(REVISIONS)) if live else off("x")
        if path.endswith("/rollback"): return ok(env({"jobId": "job-9-1", "app": "portfolio", "revision": 2}))
        if path == "/api/security":
            if live: return ok(env(SEC))
            return ok(env({"trivy": {"installed": False, "version": None, "enabled": True}, "gate": {"failOn": "CRITICAL", "ignoreUnfixed": False}, "totals": {k: 0 for k in SEC["totals"]}, "scans": [], "scanCount": 0}, "unavailable", "Trivy", "Trivy is not installed and no scans have been recorded."))
        if path.startswith("/api/security/scans/"): return ok(env({"jobId": "job-3-1", "scans": [{**SEC["scans"][0], "findings": [{"kind": "vulnerability", "severity": "HIGH", "id": "CVE-0000-TEST", "package": "libtest", "installed": "1.0", "fixed": "1.1", "title": "synthetic test finding"}]}]}))
        if path == "/api/observability":
            return ok(env({"panels": [{"label": "CPU utilization", "value": "23.40%", "status": "LIVE"}, {"label": "Running pods", "value": None, "status": "NO DATA"}], "targets": [{"job": "nexus", "instance": "host:8000", "health": "up", "lastError": "", "lastScrape": "2026-01-01T10:00:00Z"}], "grafanaUrl": "http://localhost:3000"})) if live else off("PROMETHEUS_URL not configured")
        if path == "/api/observability/series": return ok(env(SERIES)) if live else off("x")
        if path == "/api/infrastructure": return ok(env({"host": {"os": "Linux 6", "cpuCount": 2, "memoryGb": 8.0, "python": "3.12", "disks": {"work": {"path": "/tmp/nexus-jobs", "totalGb": 25.0, "freeGb": 14.2, "usedPct": 43.2}}}, "aws": None, "awsError": "AWS not available (NoCredentialsError).", "region": "ap-southeast-2"}))
        if path == "/api/gitops": return off("ARGOCD_URL not configured (Argo CD is optional).")
        if path == "/api/pipelines": return off("GITHUB_OWNER / GITHUB_REPO not configured (optional).")
        if path == "/api/github/config":
            return ok(env({"repoUrl": "https://github.com/octo/site" if live else None, "branch": "main", "repoConfigured": live, "webhookSecretConfigured": live, "webhookPath": "/api/webhooks/github", "jenkins": {"status": "connected" if live else "not_configured", "detail": "", "job": "nexus-github-deploy"}, "recentDeliveries": [{"at": NOW - 50, "event": "push", "branch": "main", "result": "queued"}] if live else []}))
        if path == "/api/github/deploy": return ok(env({"queued": True, "branch": "main"}))
        if path == "/api/docker/status":
            if live: return ok(env({"connected": True, "host": "npipe:////./pipe/dockerDesktopLinuxEngine", "source": "Docker context 'desktop-linux'", "serverVersion": "27.0", "apiVersion": "1.46", "platform": "Windows", "candidates": [], "code": "ok", "message": "ok", "hints": []}))
            return ok(env({"connected": False, "code": "desktop_not_running", "message": "Docker Desktop is installed but not running (the Docker engine pipe does not exist).", "hints": ["Start 'Docker Desktop' from the Start menu."], "platform": "Windows", "candidates": [{"label": "Docker Desktop (Linux engine)", "host": "npipe:////./pipe/dockerDesktopLinuxEngine", "endpointExists": False}]}, "unavailable"))
        if path == "/api/config": return ok(env({"version": "1.0.0", "authRequired": False, "limits": {"maxUploadMb": 100, "maxUncompressedMb": 500, "maxFiles": 10000, "maxCompressionRatio": 100}, "build": {"npmIgnoreScripts": True, "runTests": True, "buildMemoryMb": 2048, "maxConcurrentJobs": 1}, "security": {"trivyEnabled": True, "failOn": "CRITICAL", "ignoreUnfixed": False}, "kubernetes": {"namespace": "default", "imageSource": "registry", "serviceType": "ClusterIP"}, "corsOrigins": []}))
        if path == "/api/deploy" and req.method == "POST":
            body = (req.post_data_buffer or b"")
            if b"corrupt" in body[:400]: return ok({"detail": "Upload is not a valid ZIP archive (it may be corrupt or truncated).", "code": "corrupt"}, 400)
            return ok(env({"jobId": "job-8-1", "app": "demo", "project": {"framework": "Static HTML/CSS/JS"}}))
        m = re.match(r"/api/deploy/jobs/([^/]+)/stream", path)
        if m:
            steps_done = {s: ("success" if s != "security" else "skipped") for s in STEPS}
            ev = lambda d: "data: " + json.dumps(d) + "\n\n"
            base = {**job(8, "demo", "running"), "steps": {**{s: "pending" for s in STEPS}, "upload": "success", "inspect": "running"}, "logs": [{"ts": NOW, "text": "Extracting upload for 'demo'", "level": "info"}], "logCount": 1}
            fin = {**job(8, "demo", "success"), "app": "demo", "steps": steps_done, "logs": [{"ts": NOW, "text": "Done. demo released via local mode.", "level": "success"}], "logCount": 2, "mode": "local", "previewUrl": "http://localhost:49999", "health": {"verified": True}}
            return route.fulfill(status=200, content_type="text/event-stream", body=ev(base) + ev(fin))
        m = re.match(r"/api/deploy/jobs/([^/]+)$", path)
        if m: return ok(env({**JOBS[0], "logs": [{"ts": NOW, "text": "hello log", "level": "info"}], "logCount": 1}))
        route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": "unmocked " + path}))
    return handler


class Static(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        p = path.split("?")[0]
        if p.startswith("/assets/"): return str(ROOT / "assets" / p[len("/assets/"):])
        return str(ROOT / "frontend" / (p.lstrip("/") or "index.html"))
    def log_message(self, *a): pass


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--shots", default=None); args = ap.parse_args()
    shots = Path(args.shots) if args.shots else None
    if shots: shots.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Static); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    results = []
    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail)); print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        def new_page(live, width=1440, height=900, reduced=False):
            ctx = browser.new_context(viewport={"width": width, "height": height}, reduced_motion="reduce" if reduced else "no-preference", accept_downloads=True)
            page = ctx.new_page(); calls = []; errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
            page.route(re.compile(r".*/api/.*"), make_mock(live, calls))
            page.route(re.compile(r"https://fonts\.(googleapis|gstatic)\.com/.*"), lambda r: r.abort())
            return ctx, page, calls, errors

        # ---------- LIVE scenario: navigation, every view renders ----------
        ctx, page, calls, errors = new_page(True)
        page.goto(base + "/#/dashboard"); page.wait_for_selector("#dash-stats .stat-value")
        check("dashboard shows real release count from API", "3" in page.inner_text("#dash-stats"))
        check("header status derived from /api/health", "services connected" in page.inner_text("#header-status"))
        routes = {"deploy": "New Deployment", "zip": "ZIP Upload", "github": "GitHub Deployment", "history": "Deployment History", "pipeline": "CI/CD Pipeline", "kubernetes": "Kubernetes", "security": "Security", "monitoring": "Monitoring", "infrastructure": "Infrastructure", "gitops": "GitOps / Argo CD", "settings": "Settings", "dashboard": "Dashboard"}
        for r, title in routes.items():
            page.click(f'.nav-link[data-route="{r}"]'); page.wait_for_timeout(450)
            visible = page.eval_on_selector_all(".view", "els => els.filter(e => !e.hidden).map(e => e.dataset.view)")
            check(f"nav -> {title}", visible == [r] and page.inner_text("#page-title") == title and page.get_attribute(f'.nav-link[data-route="{r}"]', "aria-current") == "page", str(visible))
        if shots:
            for r in ["dashboard", "zip", "history", "kubernetes", "security", "monitoring", "settings", "infrastructure", "github", "pipeline"]:
                page.goto(base + f"/#/{r}"); page.wait_for_timeout(1500); page.screenshot(path=str(shots / f"live-{r}-desktop.png"), full_page=(r != "dashboard"))
        # history filter + drawer
        page.goto(base + "/#/history"); page.wait_for_selector("#history-table tr[data-id]")
        n_all = page.locator("#history-table tbody tr").count()
        page.click('.chip[data-filter="failed"]'); n_failed = page.locator("#history-table tbody tr").count()
        check("desktop: hamburger hidden, rail visible", not page.is_visible("#nav-toggle") and page.is_visible("#rail"))
        check("history filter narrows the list", n_failed == 1 and n_all == 4, f"{n_all}/{n_failed}")
        page.click('.chip[data-filter="all"]'); page.click("#history-table tr[data-id]:first-child"); page.wait_for_selector("#drawer[open] .console")
        check("history row opens detail drawer with log", "hello log" in page.inner_text("#drawer-body")); page.click("#drawer-close")
        # security findings + download
        page.goto(base + "/#/security"); page.wait_for_selector("#sec-scans [data-i]"); page.click("#sec-scans [data-i]"); page.wait_for_selector("#sec-findings td.mono")
        check("security findings come from API", "CVE-0000-TEST" in page.inner_text("#sec-findings"))
        # kubernetes rollback flow
        page.goto(base + "/#/kubernetes"); page.wait_for_selector("#k8s-revisions [data-rev]")
        check("current revision's rollback button is disabled", page.locator('#k8s-revisions [data-rev="3"]').is_disabled())
        page.click('#k8s-revisions [data-rev="2"]'); page.wait_for_selector("#dialog[open]")
        page.click("#dialog-ok"); page.wait_for_timeout(1200)
        check("rollback confirmed -> POST /rollback called", any(c[1].endswith("/portfolio/rollback") and c[0] == "POST" for c in calls))
        check("rollback shows live run panel", page.locator("#rollback-run .stages").count() == 1)
        # zip upload flow
        page.goto(base + "/#/zip"); page.wait_for_timeout(500)
        check("submit disabled until a file is chosen", page.is_disabled("#zip-submit"))
        zpath = ROOT / "tests/fixtures/_generated/static-site.zip"
        page.set_input_files("#zip-input", str(zpath))
        check("valid zip enables submit", page.is_enabled("#zip-submit"))
        page.click("#zip-submit"); page.wait_for_selector("#zip-run .result--success", timeout=8000)
        check("zip upload -> stages + success result", page.locator("#zip-run .stage[data-status='success']").count() >= 6 and "localhost:49999" in page.inner_html("#zip-run"))
        check("deploy POST used the shared /api/deploy endpoint", ("POST", "/api/deploy") in calls)
        page.goto(base + "/#/dashboard"); page.goto(base + "/#/zip")
        page.set_input_files("#zip-input", files=[{"name": "notes.txt", "mimeType": "text/plain", "buffer": b"hi"}])
        check("non-zip file rejected client-side", page.is_visible("#zip-error") and page.is_disabled("#zip-submit"))
        check("no JavaScript errors across live run", not errors, "; ".join(errors[:3]))
        ctx.close()

        # ---------- OFFLINE scenario: honest 'not connected' states ----------
        ctx, page, calls, errors = new_page(False)
        page.goto(base + "/#/kubernetes"); page.wait_for_selector("#k8s-stats .banner")
        check("offline: kubernetes says 'not connected', no fake numbers", "not connected" in page.inner_text("#k8s-stats").lower() and not re.search(r"\d/\d", page.inner_text("#k8s-stats")))
        page.goto(base + "/#/monitoring"); page.wait_for_selector("#mon-banner .banner")
        check("offline: monitoring shows disconnected, no panels", "not connected" in page.inner_text("#mon-banner").lower() and page.locator("#mon-panels .stat").count() == 0)
        page.goto(base + "/#/security"); page.wait_for_selector("#sec-banner .banner")
        check("offline: security says Trivy not installed", "not installed" in page.inner_text("#sec-banner").lower())
        page.goto(base + "/#/settings"); page.wait_for_selector("#docker-panel .banner--error")
        check("offline: Docker Desktop not running diagnosis shown", "Docker Desktop is not running" in page.inner_text("#docker-panel") and "Start 'Docker Desktop'" in page.inner_text("#docker-panel"))
        page.goto(base + "/#/gitops"); page.wait_for_selector("#gitops-body .banner")
        check("offline: Argo CD shown as optional/not configured", "optional" in page.inner_text("#gitops-body").lower())
        page.goto(base + "/#/github"); page.wait_for_timeout(700)
        check("offline: GitHub 'Deploy now' disabled when repo not configured", page.is_disabled("#gh-submit"))
        if shots:
            for r in ["dashboard", "kubernetes", "monitoring", "settings"]:
                page.goto(base + f"/#/{r}"); page.wait_for_timeout(1200); page.screenshot(path=str(shots / f"offline-{r}-desktop.png"), full_page=(r != "dashboard"))
        check("offline: no JavaScript errors", not errors, "; ".join(errors[:3]))
        ctx.close()

        # ---------- Responsive ----------
        for label, w, h in (("tablet", 820, 1100), ("mobile", 390, 844)):
            ctx, page, calls, errors = new_page(True, w, h)
            page.goto(base + "/#/dashboard"); page.wait_for_selector("#dash-stats .stat-value"); page.wait_for_timeout(1200)
            overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            check(f"{label}: no horizontal page scroll on dashboard", overflow <= 1, f"overflow {overflow}px")
            rail_hidden = page.evaluate("document.getElementById('rail').getBoundingClientRect().right <= 0")
            check(f"{label}: navigation rail is off-canvas", rail_hidden)
            page.click("#nav-toggle"); page.wait_for_timeout(450)
            check(f"{label}: menu button opens navigation", page.evaluate("document.getElementById('rail').getBoundingClientRect().left >= 0") and page.get_attribute("#nav-toggle", "aria-expanded") == "true")
            if shots: page.screenshot(path=str(shots / f"live-menu-{label}.png"))
            page.click('.nav-link[data-route="history"]'); page.wait_for_timeout(500)
            check(f"{label}: choosing a link navigates and closes menu", page.inner_text("#page-title") == "Deployment History" and page.get_attribute("#nav-toggle", "aria-expanded") == "false")
            for r in ["history", "kubernetes", "security", "monitoring", "zip", "settings", "github", "pipeline", "infrastructure", "gitops", "deploy"]:
                page.goto(base + f"/#/{r}"); page.wait_for_timeout(900)
                ov = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                check(f"{label}: {r} has no horizontal page overflow", ov <= 1, f"{ov}px")
            if shots:
                page.goto(base + "/#/dashboard"); page.wait_for_timeout(1200); page.screenshot(path=str(shots / f"live-dashboard-{label}.png"), full_page=True)
                page.goto(base + "/#/history"); page.wait_for_timeout(900); page.screenshot(path=str(shots / f"live-history-{label}.png"), full_page=True)
            ctx.close()

        # ---------- Reduced motion ----------
        ctx, page, calls, errors = new_page(True, reduced=True)
        page.goto(base + "/#/dashboard"); page.wait_for_selector("#dash-stats .stat-value")
        dur_ = page.evaluate("getComputedStyle(document.querySelector('.infinity-stage')).animationDuration")
        check("reduced motion: hero float animation is effectively disabled", dur_ in ("0.001s", "1e-06s", "0s") or float(dur_.rstrip('s')) < 0.01, dur_)
        ctx.close()
        browser.close()

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} UI checks passed")
    Path(ROOT / "tests/e2e/last-run.json").write_text(json.dumps([{"name": n, "pass": p, "detail": d} for n, p, d in results], indent=1))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
