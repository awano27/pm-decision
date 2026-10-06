"""The version is the same everywhere, and a shared report says which commit it came from."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import re
import tempfile
import unittest
from pathlib import Path

import kimeru
from kimeru import stats

ROOT = Path(__file__).resolve().parent.parent


class TestVersion(unittest.TestCase):
    def test_pyproject_package_and_changelog_agree(self):
        py = re.search(r'(?m)^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8")).group(1)
        self.assertEqual(py, kimeru.__version__)
        log = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertRegex(log, rf"(?m)^## {re.escape(py)}（\d{{4}}-\d{{2}}-\d{{2}}）$")

    def test_the_share_line_names_the_version_and_a_commit(self):
        env = stats.environment()
        self.assertRegex(env["kimeru"], rf"^{re.escape(kimeru.__version__)} \((?:[0-9a-f]{{7}}|unknown)\)$")


class TestBuildId(unittest.TestCase):
    def test_a_plain_git_folder(self):
        with tempfile.TemporaryDirectory() as d:
            g = Path(d) / ".git"
            (g / "refs" / "heads").mkdir(parents=True)
            (g / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (g / "refs" / "heads" / "main").write_text("abcdef1234567890\n", encoding="utf-8")
            self.assertEqual(stats.build_id(d), "abcdef1")

    def test_packed_refs(self):
        with tempfile.TemporaryDirectory() as d:
            g = Path(d) / ".git"
            g.mkdir()
            (g / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (g / "packed-refs").write_text("# pack-refs\n1234567deadbeef refs/heads/main\n", encoding="utf-8")
            self.assertEqual(stats.build_id(d), "1234567")

    def test_a_bring_in_copy_reads_its_version_line(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "VERSION").write_text("kimeru 9f8e7d6 built 2026-10-06 01:00\n", encoding="ascii")
            self.assertEqual(stats.build_id(d), "9f8e7d6")

    def test_nothing_known_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(stats.build_id(d), "unknown")


if __name__ == "__main__":
    unittest.main()
