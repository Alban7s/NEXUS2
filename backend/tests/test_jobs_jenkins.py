import json
import os
import unittest
from unittest import mock

from ._helpers import tmpdir

from backend.errors import PipelineError
from backend.services import jenkins
from backend.services import jobs as jobs_mod
from backend.services.jobs import JobStore, redact


class Redaction(unittest.TestCase):
    def test_secrets_are_masked(self):
        self.assertNotIn("abc123", redact("password=abc123"))
        self.assertNotIn("s3cret", redact("git clone https://bot:s3cret@github.com/x/y"))
        self.assertNotIn("ghp_" + "a" * 30, redact("token ghp_" + "a" * 30))
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", redact("key AKIAABCDEFGHIJKLMNOP"))
        self.assertNotIn("zzz", redact("Authorization: Bearer zzz"))

    def test_plain_text_untouched(self):
        self.assertEqual(redact("Step 3/9 : RUN npm ci"), "Step 3/9 : RUN npm ci")


class Persistence(unittest.TestCase):
    def test_history_survives_restart(self):
        d = str(tmpdir())
        with mock.patch.object(jobs_mod, "settings", mock.Mock(data_dir=d)):
            s1 = JobStore()
            j = s1.create("demo", {"framework": "Vite"})
            s1.log(j.id, "hello password=hunter2")
            s1.add_scan(j.id, {"scanType": "image", "status": "completed", "findings": [{"id": "X"}]})
            s1.finish(j.id, "success")
            running = s1.create("half-done")
            s1.update(running.id, status="running")
            s1._persist()
            s2 = JobStore()
            self.assertEqual(s2.load(), 1)
            r = s2.get(j.id)
            self.assertEqual(r.status, "success")
            self.assertNotIn("hunter2", r.logs[0].text)
            self.assertEqual(r.scans[0]["findings"][0]["id"], "X")
            self.assertIsNone(s2.get(running.id))   # unfinished jobs are not persisted as success

    def test_rollback_job_has_own_steps(self):
        s = JobStore(persist=False)
        j = s.create("demo", kind="rollback")
        self.assertEqual(list(j.steps), ["plan", "rollback", "verify"])


class GitHubSecurity(unittest.TestCase):
    def test_signature(self):
        import hashlib, hmac
        body = b'{"ref":"refs/heads/main"}'
        good = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
        self.assertTrue(jenkins.verify_github_signature("topsecret", body, good))
        self.assertFalse(jenkins.verify_github_signature("topsecret", body, "sha256=" + "0" * 64))
        self.assertFalse(jenkins.verify_github_signature("topsecret", body + b"x", good))
        self.assertFalse(jenkins.verify_github_signature("topsecret", body, None))
        self.assertFalse(jenkins.verify_github_signature(None, body, good))

    def test_repo_validation(self):
        jenkins.validate_repo_url("https://github.com/octo/hello-world.git")
        for bad in ("http://github.com/a/b", "https://evil.com/a/b", "https://github.com/a/b; rm -rf /", ""):
            with self.assertRaises(PipelineError):
                jenkins.validate_repo_url(bad)

    def test_branch_validation(self):
        jenkins.validate_branch("feature/login-2")
        for bad in ("../x", "a b", "$(id)", "x;y", "", "-rf", "a..b"):
            with self.assertRaises(PipelineError):
                jenkins.validate_branch(bad)

    def test_commit_validation(self):
        self.assertEqual(jenkins.validate_commit("ABCDEF1"), "abcdef1")
        self.assertIsNone(jenkins.validate_commit(None))
        with self.assertRaises(PipelineError):
            jenkins.validate_commit("xyz; ls")


if __name__ == "__main__":
    unittest.main()
