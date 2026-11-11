import hashlib
import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from packaging.requirements import Requirement

from scripts import lock_dependencies as locks
from scripts import release_metadata as release


class DependencyLockTests(unittest.TestCase):
    def test_dependency_closure_pins_transitives_and_ignores_inactive_markers(self):
        distributions = {
            "root": types.SimpleNamespace(version="1.0", requires=[
                "child>=2", "unused; python_version < '1'", "optional; extra == 'test'"]),
            "child": types.SimpleNamespace(version="2.5", requires=["root==1.0"]),
        }
        actual = locks.installed_closure([Requirement("root==1.0")], distributions.__getitem__)
        self.assertEqual(actual, {"child": "2.5", "root": "1.0"})
        with self.assertRaises(ValueError):
            locks.installed_closure([Requirement("root==9.0")], distributions.__getitem__)

    def test_requirement_includes_and_exact_version_policy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "base.txt").write_text("root==1.0\n", encoding="utf-8")
            (root / "build.txt").write_text("-r base.txt\nbuild==2.0\n", encoding="utf-8")
            self.assertEqual([r.name for r in locks.roots_from_file(root / "build.txt")], ["root", "build"])
            for invalid in ("root>=1", "root==1.*", "root @ https://example.invalid/a.whl", "root[test]==1"):
                (root / "invalid.txt").write_text(invalid, encoding="utf-8")
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    locks.roots_from_file(root / "invalid.txt")
            (root / "loop.txt").write_text("-r loop.txt\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                locks.roots_from_file(root / "loop.txt")

    def test_lock_hashes_match_downloaded_artifact_and_missing_wheels_fail(self):
        with tempfile.TemporaryDirectory() as td:
            wheel = Path(td) / "root-1.0-py3-none-any.whl"
            wheel.write_bytes(b"synthetic wheel content")
            hashes = locks.wheel_hashes(td, {"root": "1.0"})
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            self.assertEqual(hashes, {"root": {digest}})
            rendered = locks.render_lock({"root": "1.0"}, hashes)
            self.assertIn("root==1.0 --hash=sha256:" + digest, rendered)
            with self.assertRaises(ValueError):
                locks.wheel_hashes(td, {"root": "1.0", "missing": "2.0"})
            with self.assertRaises(ValueError):
                locks.wheel_hashes(td, {"root": "2.0"})


class ReleaseMetadataTests(unittest.TestCase):
    def test_tag_matches_version_exactly(self):
        release.validate_tag("v1.0.1", "1.0.1")
        for tag in ("v1.0.0", "1.0.1", "v1.0.1;echo injected", "refs/tags/v1.0.1"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release.validate_tag(tag, "1.0.1")

    def test_git_proof_rejects_wrong_tag_and_dirty_release(self):
        def response(text):
            return subprocess.CompletedProcess([], 0, text)
        with patch.object(release.subprocess, "run", side_effect=[response("abc\n"), response(""), response("def\n")]):
            with self.assertRaises(ValueError):
                release.git_info("synthetic", "v1.0.1")
        with patch.object(release.subprocess, "run", side_effect=[response("abc\n"), response(" M app.py\n")]):
            with self.assertRaises(ValueError):
                release.git_info("synthetic", require_clean=True)

    def test_provenance_checksums_and_signature_status_are_factual(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app").mkdir()
            (root / "app" / "appinfo.py").write_text('__version__ = "1.0.1"\n', encoding="utf-8")
            for lock in release.LOCKS:
                (root / lock).write_text("synthetic==1 --hash=sha256:abc\n", encoding="utf-8")
            appdir = root / "dist" / "DiscordAnt"
            manifest = appdir / "_internal" / "driver" / "browsers.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"browsers": [{"name": "chromium", "revision": "123",
                                                          "browserVersion": "150.0"}]}), encoding="utf-8")
            browser = appdir / "browser" / "chromium-123" / "chrome-win" / "chrome.exe"
            browser.parent.mkdir(parents=True)
            browser.write_bytes(b"synthetic browser exe")
            output = root / "release"
            output.mkdir()
            artifact = output / "DiscordAnt-Setup-1.0.1.exe"
            artifact.write_bytes(b"synthetic unsigned installer")
            dist = types.SimpleNamespace(metadata={"Name": "synthetic"}, version="1.0")
            with patch.object(release, "git_info", return_value={"commit": "abc", "tag": None, "dirty": True}), \
                    patch.object(release, "authenticode", return_value={"status": "NotSigned"}), \
                    patch.object(release.importlib.metadata, "distributions", return_value=[dist]):
                actual = release.write_metadata(root, [artifact], appdir, output)
            self.assertEqual(actual["artifacts"][0]["authenticode"]["status"], "NotSigned")
            self.assertTrue(actual["source"]["dirty"])
            self.assertEqual(actual["browser"]["declared"][0]["revision"], "123")
            for line in (output / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
                digest, filename = line.split("  ", 1)
                self.assertEqual(digest, release.sha256(output / filename))
            browser.write_bytes(b"changed browser exe")
            self.assertNotEqual(release.sha256(browser), actual["browser"]["executables"][0]["sha256"])

    def test_browser_revision_mismatch_fails_release_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "browser" / "chromium-999").mkdir(parents=True)
            (root / "browsers.json").write_text(
                json.dumps({"browsers": [{"name": "chromium", "revision": "123"}]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                release.browser_info(root)


if __name__ == "__main__":
    unittest.main()
