"""Trivy report parsing and the security gate (the report below is a SYNTHETIC
test input in Trivy's JSON schema; it is never shown in the dashboard)."""
import unittest

from backend.services import trivy

SYNTHETIC = {"Results": [
    {"Target": "app (alpine)", "Vulnerabilities": [
        {"VulnerabilityID": "CVE-TEST-1", "PkgName": "libx", "InstalledVersion": "1", "FixedVersion": "2", "Severity": "CRITICAL", "Title": "t1"},
        {"VulnerabilityID": "CVE-TEST-2", "PkgName": "liby", "InstalledVersion": "1", "Severity": "HIGH"},
        {"VulnerabilityID": "CVE-TEST-3", "PkgName": "libz", "InstalledVersion": "1", "Severity": "LOW"},
        {"VulnerabilityID": "CVE-TEST-4", "PkgName": "libw", "InstalledVersion": "1", "Severity": "WEIRD"},
    ]},
    {"Target": "package-lock.json", "Secrets": [{"RuleID": "aws-key", "Severity": "CRITICAL", "Title": "AWS key"}]},
    {"Target": "empty"},
]}


class Parse(unittest.TestCase):
    def test_counts_and_order(self):
        r = trivy.parse_report(SYNTHETIC)
        self.assertEqual(r["counts"], {"CRITICAL": 2, "HIGH": 1, "MEDIUM": 0, "LOW": 1, "UNKNOWN": 1})
        self.assertEqual(r["total"], 5)
        self.assertEqual(r["findings"][0]["severity"], "CRITICAL")
        self.assertEqual(r["findings"][-1]["severity"], "UNKNOWN")

    def test_empty_report(self):
        r = trivy.parse_report({})
        self.assertEqual(r["total"], 0)

    def test_null_results(self):
        self.assertEqual(trivy.parse_report({"Results": None})["total"], 0)


class Gate(unittest.TestCase):
    C = {"CRITICAL": 0, "HIGH": 2, "MEDIUM": 5, "LOW": 9, "UNKNOWN": 3}

    def test_critical_threshold_passes_with_only_high(self):
        self.assertTrue(trivy.evaluate_gate(self.C, "CRITICAL")["passed"])

    def test_high_threshold_fails(self):
        g = trivy.evaluate_gate(self.C, "HIGH")
        self.assertFalse(g["passed"])
        self.assertIn("2 HIGH", g["reason"])

    def test_none_is_report_only(self):
        g = trivy.evaluate_gate({"CRITICAL": 9, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}, "NONE")
        self.assertTrue(g["passed"])
        self.assertFalse(g["enforced"])

    def test_unknown_never_blocks(self):
        self.assertTrue(trivy.evaluate_gate({"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 50}, "LOW")["passed"])

    def test_invalid_threshold_defaults_to_critical(self):
        self.assertEqual(trivy.evaluate_gate(self.C, "bogus")["threshold"], "CRITICAL")


class Runner(unittest.TestCase):
    def test_skipped_when_trivy_missing(self):
        from unittest import mock
        logs = []
        with mock.patch.object(trivy, "trivy_path", return_value=None):
            r = trivy.scan_image("app:1", "job-1", lambda m, level="info": logs.append(m))
        self.assertEqual(r["status"], "skipped")
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["findings"], [])

    def test_report_name_validation(self):
        self.assertIsNone(trivy.safe_report_path("../../etc/passwd"))
        self.assertIsNone(trivy.safe_report_path("x.txt"))


if __name__ == "__main__":
    unittest.main()
