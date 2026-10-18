"""paths.root(): the folder that holds graphs/ playbooks/ examples/ tools/. A clone wins; an install falls back to the shared data
folder; with neither there is one plain line (no traceback), and importing the package never fails."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import tempfile
import unittest
from pathlib import Path

from kimeru import paths


def _make(base, *names):
    for n in names:
        (Path(base) / n).mkdir(parents=True, exist_ok=True)
    return Path(base)


class TestFind(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(paths.reset)
        paths.reset()

    def test_the_folder_next_to_the_package_wins(self):
        repo = _make(self.tmp / "repo", "graphs", "playbooks")
        _make(self.tmp / "prefix" / "share" / "kimeru", "graphs", "playbooks")
        self.assertEqual(paths._find(repo, [self.tmp / "prefix"]), repo)

    def test_falls_back_to_the_shared_data(self):
        repo = _make(self.tmp / "site-packages")   # no graphs/ here: an installed copy
        shared = _make(self.tmp / "prefix" / "share" / "kimeru", "graphs", "playbooks")
        self.assertEqual(paths._find(repo, [self.tmp / "prefix"]), shared)

    def test_the_first_prefix_that_has_the_data_is_taken(self):
        repo = _make(self.tmp / "site-packages")
        _make(self.tmp / "p1")   # no share/kimeru
        second = _make(self.tmp / "p2" / "share" / "kimeru", "graphs", "playbooks")
        self.assertEqual(paths._find(repo, [self.tmp / "p1", self.tmp / "p2"]), second)

    def test_a_folder_with_graphs_but_no_playbooks_is_not_taken(self):
        repo = _make(self.tmp / "site-packages")
        _make(self.tmp / "prefix" / "share" / "kimeru", "graphs")
        with self.assertRaises(paths.NotFound):
            paths._find(repo, [self.tmp / "prefix"])

    def test_neither_gives_one_plain_line(self):
        repo = _make(self.tmp / "site-packages")
        with self.assertRaises(paths.NotFound) as cm:
            paths._find(repo, [self.tmp / "prefix"])
        text = str(cm.exception)
        self.assertNotIn("\n", text)
        self.assertIn("判断グラフ／進め方の型のフォルダが見つかりません", text)
        self.assertIn("python -m kimeru", text)

    def test_the_real_root_is_this_clone_and_is_cached(self):
        repo = Path(paths.__file__).resolve().parent.parent
        self.assertEqual(paths.root(), repo)
        self.assertIs(paths.root(), paths.root())
        for name in ("graphs", "playbooks", "examples", "tools"):
            self.assertTrue((paths.root() / name).is_dir(), name)

    def test_base_never_raises(self):
        self.assertIsInstance(paths.base(), Path)


class TestNoStrayRepoPaths(unittest.TestCase):
    def test_the_package_does_not_build_repo_paths_itself(self):
        src_dir = Path(paths.__file__).resolve().parent
        bad = []
        for p in sorted(src_dir.glob("*.py")):
            if p.name == "paths.py":
                continue
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if "parent.parent" in line:
                    bad.append(f"{p.name}:{n}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
