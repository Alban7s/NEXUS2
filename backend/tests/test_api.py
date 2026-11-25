"""HTTP API tests using FastAPI's TestClient.

Requires:  pip install -r requirements-dev.txt
Skipped automatically if FastAPI/httpx are not installed.
"""
import dataclasses
import unittest
from unittest import mock

from ._helpers import FIXTURES, tmpdir

try:
    from fastapi.testclient import TestClient
    from backend import app as app_module
    HAVE_API = True
except ImportError:  # pragma: no cover
    HAVE_API = False


@unittest.skipUnless(HAVE_API, "fastapi/httpx not installed")
class ApiTests(unittest.TestCase):
    def setUp(self):
        s = dataclasses.replace(app_module.settings, work_dir=str(tmpdir()), data_dir=str(tmpdir()),
                                nexus_api_key=None, github_webhook_secret=None)
        self.patch = mock.patch.object(app_module, "settings", s)
        self.patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self.patch.stop()

    def upload(self, key, **headers):
        with open(FIXTURES[key], "rb") as fh:
            return self.client.post("/api/deploy", files={"file": (FIXTURES[key].name, fh, "application/zip")},
                                    headers=headers)

    def test_liveness_and_envelope(self):
        self.assertEqual(self.client.get("/healthz").text, "ok")
        body = self.client.get("/api/session").json()
        for k in ("mode", "source", "updatedAt", "data", "error"):
            self.assertIn(k, body)

    def test_invalid_uploads_are_400_with_code(self):
        for key, code in (("corrupt", "corrupt"), ("empty_zip", "empty"), ("traversal", "path_traversal"),
                          ("symlink", "policy"), ("no_project", "no_project")):
            r = self.upload(key)
            self.assertEqual(r.status_code, 400, key)
            self.assertEqual(r.json()["code"], code)

    def test_non_zip_rejected(self):
        r = self.client.post("/api/deploy", files={"file": ("a.txt", b"hi", "text/plain")})
        self.assertEqual(r.status_code, 400)

    def test_oversized_upload_is_413(self):
        small = dataclasses.replace(app_module.settings, max_upload_mb=0)
        with mock.patch.object(app_module, "settings", small):
            self.assertEqual(self.upload("static").status_code, 413)

    def test_api_key_enforced(self):
        locked = dataclasses.replace(app_module.settings, nexus_api_key="k" * 20)
        with mock.patch.object(app_module, "settings", locked):
            self.assertEqual(self.client.get("/api/deploy/jobs").status_code, 401)
            self.assertEqual(self.client.get("/api/deploy/jobs", headers={"X-Nexus-Api-Key": "k" * 20}).status_code, 200)
            self.assertEqual(self.client.get("/api/session").status_code, 200)   # public

    def test_project_root_and_secrets_not_served(self):
        for path in ("/.env", "/backend/app.py", "/requirements.txt", "/../.env"):
            self.assertNotEqual(self.client.get(path).status_code, 200, path)

    def test_security_headers(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")

    def test_webhook_requires_valid_signature(self):
        import hashlib, hmac, json
        s = dataclasses.replace(app_module.settings, github_webhook_secret="whsec", github_repo_url="https://github.com/o/r")
        body = json.dumps({"zen": "x"}).encode()
        with mock.patch.object(app_module, "settings", s):
            self.assertEqual(self.client.post("/api/webhooks/github", content=body).status_code, 401)
            sig = "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
            r = self.client.post("/api/webhooks/github", content=body,
                                 headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "ping"})
            self.assertEqual(r.status_code, 200)

    def test_rollback_validates_input(self):
        self.assertEqual(self.client.post("/api/k8s/deployments/Bad_Name/rollback", json={"revision": 1}).status_code, 400)
        self.assertEqual(self.client.post("/api/k8s/deployments/web/rollback", json={"revision": 0}).status_code, 400)
        self.assertEqual(self.client.post("/api/k8s/deployments/web/rollback",
                                          json={"revision": 1, "namespace": "kube-system"}).status_code, 400)

    def test_unconfigured_integrations_say_so(self):
        r = self.client.get("/api/observability").json()
        self.assertEqual(r["mode"], "unavailable")
        self.assertIsNone(r["data"])

    def test_metrics_endpoint(self):
        self.assertIn("nexus_http_requests_total", self.client.get("/metrics").text)


if __name__ == "__main__":
    unittest.main()
