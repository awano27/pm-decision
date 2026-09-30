"""State files that survive a crash: write to a temp file, keep the previous version as .bak, replace.

A half-written approvals.json used to make every step fail with JSONDecodeError, every cycle, forever.
"""
import contextlib
import json
import os
import time
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


@contextlib.contextmanager
def exclusive(path, stale_sec=1800):
    """Hold a lock file for the duration of the block. Yields True when the lock was taken and False when another process holds it
    (then nothing is touched). A lock file older than `stale_sec` was left by a process that died: it is taken over."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime <= stale_sec:
                    break
                path.unlink()
            except OSError:
                break
    if fd is None:
        yield False
        return
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield True
    finally:
        try:
            path.unlink()
        except OSError:
            pass
