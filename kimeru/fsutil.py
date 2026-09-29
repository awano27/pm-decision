"""State files that survive a crash: write to a temp file, keep the previous version as .bak, replace.

A half-written approvals.json used to make every step fail with JSONDecodeError, every cycle, forever.
"""
import json
import os
from pathlib import Path


def write_atomic(path, text):
    """Write `text` so a reader (or a crash) never sees a half-written file; the last good copy stays as .bak."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        try:
            os.replace(path, path.with_name(path.name + ".bak"))
        except OSError:
            pass
    os.replace(tmp, path)


def read_json(path, default):
    """The JSON in `path`; if it is missing return `default`; if it is damaged use the .bak copy, else `default`."""
    path = Path(path)
    for p in (path, path.with_name(path.name + ".bak")):
        if not p.exists():
            continue
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
    return default
