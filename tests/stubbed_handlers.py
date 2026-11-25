"""Calls the REAL endpoint functions in backend/app.py with *stubbed* FastAPI/pydantic/httpx.

This is NOT an HTTP test (no routing, no middleware, no validation). It proves that
app.py imports cleanly, every route decorator is wired, and the handler logic runs without
NameError/TypeError for the cases that need no external service. Real HTTP tests are in
backend/tests/test_api.py and need `pip install -r requirements-dev.txt`.
"""
import asyncio, dataclasses, sys, tempfile, types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def stub(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m; return m


class FastAPI:
    def __init__(self, *a, **k): self.routes = {}
    def _reg(self, method):
        def deco(path, **kw):
            def wrap(fn): self.routes[(method, path)] = fn; return fn
            return wrap
        return deco
    def __getattr__(self, n):
        if n in ("get", "post"): return self._reg(n.upper())
        raise AttributeError(n)
    def middleware(self, kind): return lambda fn: fn
    def add_middleware(self, *a, **k): pass
    def mount(self, *a, **k): pass


class Resp:
    def __init__(self, content=None, status_code=200, **kw): self.content, self.status_code = content, status_code


stub("fastapi", FastAPI=FastAPI, File=lambda *a, **k: None, Form=lambda *a, **k: None, Request=object, UploadFile=object)
stub("fastapi.middleware"); stub("fastapi.middleware.cors", CORSMiddleware=object)
stub("fastapi.responses", FileResponse=Resp, JSONResponse=Resp, PlainTextResponse=Resp, StreamingResponse=Resp)
stub("fastapi.staticfiles", StaticFiles=lambda *a, **k: None)
stub("pydantic", BaseModel=type("BaseModel", (), {"__init__": lambda self, **k: self.__dict__.update(k)}))
stub("httpx", HTTPError=Exception, AsyncClient=object, Client=object)

import backend.app as app_mod  # noqa: E402

results = []
def check(name, cond):
    cond = bool(cond); results.append(cond); print(("PASS " if cond else "FAIL ") + name)

tmp = tempfile.mkdtemp()
s = dataclasses.replace(app_mod.settings, work_dir=tmp, data_dir=tmp, prometheus_url=None, argocd_url=None,
                        github_repo_url=None, github_owner=None, github_repo=None)
run = asyncio.run

with mock.patch.object(app_mod, "settings", s):
    routes = app_mod.app.routes
    check(f"{len(routes)} routes registered at import time", len(routes) >= 28)
    for must in [("POST", "/api/deploy"), ("POST", "/api/k8s/deployments/{name}/rollback"), ("POST", "/api/webhooks/github"),
                 ("GET", "/api/security"), ("GET", "/api/docker/status"), ("GET", "/metrics")]:
        check(f"route {must[0]} {must[1]}", must in routes)

    env = run(app_mod.deploy_config())
    check("deploy config envelope + local target", env["mode"] == "live" and env["data"]["mode"] == "local" and env["data"]["maxUploadMb"] == s.max_upload_mb)
    check("observability unconfigured -> unavailable, data null", (lambda r: r["mode"] == "unavailable" and r["data"] is None)(run(app_mod.observability())))
    check("gitops unconfigured -> unavailable", run(app_mod.gitops())["mode"] == "unavailable")
    check("pipelines unconfigured -> unavailable", run(app_mod.pipelines())["mode"] == "unavailable")
    check("costs honestly unavailable", run(app_mod.costs())["mode"] == "unavailable")
    sec = run(app_mod.security())
    check("security: Trivy absent + no scans -> unavailable with data", sec["mode"] == "unavailable" and sec["data"]["scanCount"] == 0 and sec["data"]["trivy"]["installed"] is False)
    cfg = run(app_mod.public_config())["data"]
    check("public config exposes limits, no secrets", "limits" in cfg and "nexus_api_key" not in str(cfg))
    gh = run(app_mod.github_config())["data"]
    check("github config: unconfigured", gh["repoConfigured"] is False and gh["jenkins"]["status"] == "not_configured")
    r = run(app_mod.github_deploy(types.SimpleNamespace(branch=None, commit=None)))
    check("github deploy without config -> 409", r.status_code == 409)
    check("history empty list", run(app_mod.deployment_history())["data"] == [])
    check("unknown job -> 404", run(app_mod.get_job("nope")).status_code == 404)
    check("bad deployment name -> 400", run(app_mod.k8s_revisions("Bad_Name")).status_code == 400)
    rb = run(app_mod.k8s_rollback("web", types.SimpleNamespace(revision=1, namespace="kube-system")))
    check("rollback outside configured namespace -> 400", rb.status_code == 400)
    check("rollback revision 0 -> 400", run(app_mod.k8s_rollback("web", types.SimpleNamespace(revision=0, namespace=None))).status_code == 400)
    check("security report path traversal -> 404", run(app_mod.security_report("../../etc/passwd")).status_code == 404)
    check("/metrics renders", "nexus_build_info" in run(app_mod.prometheus_metrics()).content)
    infra = run(app_mod.infrastructure())["data"]
    check("infrastructure: host facts real, AWS honestly unavailable", infra["host"]["cpuCount"] and infra["aws"] is None and infra["awsError"])
    dk = run(app_mod.docker_status())
    check("docker status returns a coded diagnosis (Docker absent here)", dk["data"]["connected"] is False and dk["data"]["code"] in ("sdk_missing", "not_installed", "desktop_not_running", "engine_unavailable"))
    check("healthz", run(app_mod.healthz()) == "ok")

print(f"\n{sum(results)}/{len(results)} stubbed-handler checks passed")
sys.exit(0 if all(results) else 1)
