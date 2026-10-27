"""`kimeru --version`: one line, "kimeru <version> (<commit>)", exit code 0, no command and no setting needed."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import contextlib
import io
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from kimeru import __version__, cli, stats

ROOT = Path(__file__).resolve().parent.parent


def _run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(argv)
    return code, out.getvalue()


class VersionTest(unittest.TestCase):
    def test_shape_and_exit_code(self):
        with mock.patch.object(stats, "build_id", return_value="abc1234"):
            code, text = _run(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(text, f"kimeru {__version__} (abc1234)\n")

    def test_same_source_as_config_share(self):
        with mock.patch.object(stats, "build_id", return_value="abc1234"):
            code, text = _run(["--version"])
            first = stats.config_share().splitlines()[0]
        self.assertTrue(first.startswith(text.strip()))

    def test_unknown_commit_gives_version_only(self):
        with mock.patch.object(stats, "build_id", return_value="unknown"):
            code, text = _run(["--version"])
        self.assertEqual((code, text), (0, f"kimeru {__version__}\n"))

    def test_global_option_before_it(self):
        code, text = _run(["--backend", "stub", "--version"])
        self.assertEqual(code, 0)
        self.assertTrue(re.match(r"kimeru \d+\.\d+\.\d+( \(\w+\))?\n$", text))

    def test_as_a_process(self):
        r = subprocess.run([sys.executable, "-m", "kimeru", "--version"], cwd=str(ROOT), capture_output=True, text=True,
                           env={**__import__("os").environ, "PYTHONIOENCODING": "cp1252"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
