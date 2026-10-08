"""Guard against invisible control characters in text files.

Escape sequences written through tooling have turned "\\t", "\\b", "\\v" and "\\r" into real
TAB/BACKSPACE/VT/CR characters more than once (broken paths like "examples<TAB>eams_chat.json",
a regex "\\b" that became BACKSPACE). This fails fast instead of on the managed PC.
"""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT = {".py", ".ps1", ".md", ".json", ".jsonl", ".cmd", ".toml", ".txt"}
BAD = re.compile(r"[\x00-\x09\x0b\x0c\x0e-\x1f]|\r(?!\n)")


def shipped_files():
    """The files of this version: git's list in a checkout, the FILES list of an update zip, else everything. Result
    files, probes and other local output next to the code (run-auto-check writes them there) are not the code's text."""
    import subprocess
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True).stdout
        if out.strip():
            return [ROOT / f for f in out.splitlines()]
    except (OSError, subprocess.CalledProcessError):
        pass
    listed = ROOT / "FILES"
    if listed.exists():
        return [ROOT / f.strip() for f in listed.read_text(encoding="utf-8-sig").splitlines() if f.strip()]
    return list(ROOT.rglob("*"))


class TestNoControlChars(unittest.TestCase):
    def test_text_files_have_no_control_characters(self):
        found = []
        for p in shipped_files():
            if not p.is_file() or {".git", ".python", "__pycache__", "out", "inbox"} & set(p.parts):
                continue
            if p.suffix not in TEXT:
                continue
            text = p.read_bytes().decode("utf-8", "replace")
            for m in BAD.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                found.append(f"{p.relative_to(ROOT)}:{line} {hex(ord(m.group()[0]))}")
        self.assertEqual(found, [], "control characters found (a lost backslash escape?)")


if __name__ == "__main__":
    unittest.main()
