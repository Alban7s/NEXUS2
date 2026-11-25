"""REAL Docker tests: build the generated Dockerfile for the static and React/Vite
fixtures, start the container and call /healthz.

These need a running Docker Engine (and, for React/Vite, internet access for npm).
They are skipped - and reported as BLOCKED - when Docker is unavailable.

Run:  python -m unittest backend.tests.test_docker_integration -v
"""
import time
import unittest
import urllib.request

from ._helpers import FIXTURES, tmpdir

from backend.errors import DockerUnavailable
from backend.services import archive, docker_client, pipeline

try:
    _c = docker_client.get_client()
    _c.close()
    DOCKER = True
    WHY = ""
except Exception as exc:  # noqa: BLE001
    DOCKER = False
    WHY = f"Docker unavailable: {exc}"


@unittest.skipUnless(DOCKER, WHY or "Docker unavailable")
class RealDocker(unittest.TestCase):
    def build_and_run(self, key, name):
        root = archive.safe_extract(FIXTURES[key], tmpdir() / "src")
        project = pipeline.detect_project(root)
        pipeline.write_build_context(project)
        tag = pipeline.make_tag(name, "job-1-1")
        logs = []
        pipeline.build_image(root, tag, logs.append, job_id="itest")
        url, cname = pipeline.deploy_local(name, tag, logs.append, 80)
        self.addCleanup(lambda: docker_client.get_client().containers.get(cname).remove(force=True))
        health = pipeline.verify_local(cname, 80, "/healthz", logs.append, timeout_s=30)
        self.assertTrue(health["verified"], health)
        time.sleep(1)
        body = urllib.request.urlopen(url + "/", timeout=5).read().decode()
        return body

    def test_static_site(self):
        self.assertIn("NEXUS static test site", self.build_and_run("static", "itest-static"))

    def test_react_vite_site(self):
        body = self.build_and_run("react", "itest-react")
        self.assertIn('<div id="root">', body)


if __name__ == "__main__":
    unittest.main()
