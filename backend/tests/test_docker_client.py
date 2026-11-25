"""Docker connection diagnostics (pure logic; no Docker needed)."""
import hashlib
import json
import os
import unittest
from unittest import mock

from ._helpers import tmpdir

from backend.services import docker_client as dc

WIN_ERR = OSError("Error while fetching server API version: (2, 'CreateFile', 'The system cannot find the file specified.')")


class Classify(unittest.TestCase):
    def test_windows_desktop_not_running(self):
        f = dc.classify_failure(WIN_ERR, system="Windows", cli_installed=True, any_endpoint_exists=False)
        self.assertEqual(f.code, "desktop_not_running")
        self.assertTrue(any("Docker Desktop" in h for h in f.hints))

    def test_not_installed(self):
        f = dc.classify_failure(WIN_ERR, system="Windows", cli_installed=False, any_endpoint_exists=False)
        self.assertEqual(f.code, "not_installed")

    def test_linux_service_stopped(self):
        f = dc.classify_failure(FileNotFoundError("sock"), system="Linux", cli_installed=True, any_endpoint_exists=False)
        self.assertEqual(f.code, "engine_unavailable")
        self.assertTrue(any("systemctl" in h for h in f.hints))

    def test_linux_permission_denied(self):
        f = dc.classify_failure(PermissionError("permission denied"), system="Linux", cli_installed=True,
                                any_endpoint_exists=True)
        self.assertEqual(f.code, "permission_denied")
        self.assertTrue(any("usermod" in h for h in f.hints))

    def test_bad_docker_host(self):
        f = dc.classify_failure(ConnectionError("Connection refused"), system="Linux", cli_installed=True,
                                any_endpoint_exists=True, explicit_host="tcp://10.0.0.9:2375")
        self.assertEqual(f.code, "bad_connection")

    def test_endpoint_exists_but_silent(self):
        f = dc.classify_failure(TimeoutError("timed out"), system="Windows", cli_installed=True,
                                any_endpoint_exists=True)
        self.assertEqual(f.code, "engine_unavailable")


class Candidates(unittest.TestCase):
    def test_windows_defaults_use_named_pipes(self):
        urls = [u for _, u in dc.platform_defaults("Windows")]
        self.assertIn("npipe:////./pipe/dockerDesktopLinuxEngine", urls)
        self.assertIn("npipe:////./pipe/docker_engine", urls)

    def test_linux_defaults(self):
        self.assertEqual(dc.platform_defaults("Linux")[0][1], "unix:///var/run/docker.sock")

    def test_docker_host_env_goes_first(self):
        with mock.patch.dict(os.environ, {"DOCKER_HOST": "tcp://example:2375"}), \
             mock.patch.object(dc, "current_context_host", return_value=None):
            self.assertEqual(dc.candidate_hosts("Linux")[0][1], "tcp://example:2375")

    def test_context_is_read_from_cli_config(self):
        cfg = tmpdir()
        (cfg / "config.json").write_text(json.dumps({"currentContext": "desktop-linux"}))
        digest = hashlib.sha256(b"desktop-linux").hexdigest()
        meta = cfg / "contexts" / "meta" / digest
        meta.mkdir(parents=True)
        (meta / "meta.json").write_text(json.dumps(
            {"Endpoints": {"docker": {"Host": "npipe:////./pipe/dockerDesktopLinuxEngine"}}}))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOCKER_CONTEXT", None)
            self.assertEqual(dc.current_context_host(cfg),
                             ("desktop-linux", "npipe:////./pipe/dockerDesktopLinuxEngine"))

    def test_default_context_returns_none(self):
        cfg = tmpdir()
        (cfg / "config.json").write_text(json.dumps({"currentContext": "default"}))
        os.environ.pop("DOCKER_CONTEXT", None)
        self.assertIsNone(dc.current_context_host(cfg))


class FakeDocker:
    """Stand-in for the docker SDK: only the pipe 'good' answers."""
    class errors:
        class DockerException(Exception): pass

    class DockerClient:
        def __init__(self, base_url, timeout=None):
            self.base_url = base_url
        def ping(self):
            if "good" not in self.base_url:
                raise OSError("(2, 'CreateFile', 'The system cannot find the file specified.')")
            return True
        def version(self):
            return {"Version": "27.0", "ApiVersion": "1.46", "Os": "linux"}
        def close(self): pass


class Connect(unittest.TestCase):
    def setUp(self):
        dc.reset_cache()

    def test_falls_through_candidates_and_caches(self):
        with mock.patch.object(dc, "_sdk", return_value=FakeDocker), \
             mock.patch.object(dc, "candidate_hosts", return_value=[("bad", "npipe:////./pipe/bad"),
                                                                    ("ok", "npipe:////./pipe/good")]), \
             mock.patch.object(dc, "_endpoint_exists", return_value=True), \
             mock.patch.object(dc.time, "sleep"):
            client = dc.get_client()
            self.assertTrue(client.base_url.endswith("good"))
            self.assertEqual(dc._cached_host, "npipe:////./pipe/good")

    def test_all_fail_raises_coded_error(self):
        with mock.patch.object(dc, "_sdk", return_value=FakeDocker), \
             mock.patch.object(dc, "candidate_hosts", return_value=[("bad", "npipe:////./pipe/bad")]), \
             mock.patch.object(dc, "_endpoint_exists", return_value=False), \
             mock.patch.object(dc.platform, "system", return_value="Windows"), \
             mock.patch.object(dc.shutil, "which", return_value="C:/docker.exe"):
            with self.assertRaises(dc.DockerUnavailable) as ctx:
                dc.get_client()
            self.assertEqual(ctx.exception.code, "desktop_not_running")

    def test_diagnose_reports_connected(self):
        with mock.patch.object(dc, "_sdk", return_value=FakeDocker), \
             mock.patch.object(dc, "candidate_hosts", return_value=[("ok", "unix:///good.sock")]), \
             mock.patch.object(dc, "_endpoint_exists", return_value=True):
            d = dc.diagnose()
        self.assertTrue(d["connected"])
        self.assertEqual(d["serverVersion"], "27.0")

    def test_diagnose_sdk_missing(self):
        with mock.patch.object(dc, "_sdk", side_effect=dc.DockerUnavailable("x", code="sdk_missing")):
            d = dc.diagnose()
        self.assertFalse(d["connected"])
        self.assertEqual(d["code"], "sdk_missing")


if __name__ == "__main__":
    unittest.main()
