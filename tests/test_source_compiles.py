"""Every Python file compiles with warnings as errors. Python 3.12 warns about an invalid escape such as "\\k" in a
string, and Windows PowerShell 5.1 (run-check.cmd) turns any warning on stderr into an error: the check then fails on
a PC whose Python is newer than the one the change was tested with."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestSourceCompiles(unittest.TestCase):
    def test_no_python_file_has_an_invalid_escape(self):
        bad = []
        for p in sorted(ROOT.rglob("*.py")):
            if any(part.startswith(".") or part in ("__pycache__", "out", "demo") for part in p.relative_to(ROOT).parts):
                continue
            src = p.read_text(encoding="utf-8")
            with warnings.catch_warnings():
                warnings.simplefilter("error")   # DeprecationWarning (3.11) and SyntaxWarning (3.12) alike
                try:
                    compile(src, str(p), "exec")
                except (SyntaxError, SyntaxWarning, DeprecationWarning) as e:
                    bad.append(f"{p.relative_to(ROOT)}: {e}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
