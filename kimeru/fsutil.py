"""State files that survive a crash: write to a temp file, keep the previous version as .bak, replace.

A half-written approvals.json used to make every step fail with JSONDecodeError, every cycle, forever.
"""
import contextlib
import json
import os
import threading
import time
from pathlib import Path


def write_atomic(path, text):
    """Write `text` so a reader (or a crash) never sees a half-written file; the last good copy stays as .bak.
    The temporary file is named per process (and per call), so two writers at once never share one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        if path.exists():
            try:
                os.replace(path, path.with_name(path.name + ".bak"))
            except OSError:
                pass
        _replace(tmp, path)
    finally:
        try:
            tmp.unlink()   # only when the replace did not happen
        except OSError:
            pass


def _replace(src, dst, tries=40):
    """os.replace that waits out a moment in which the target is held open by another process (Windows answers PermissionError
    while a reader or another writer has it)."""
    for k in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if k == tries - 1:
                raise
            time.sleep(0.02 * min(k + 1, 5))


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


def pid_alive(pid):
    """True when a process with this id exists (a process we may not look at counts as alive). Never signals the process."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return k32.GetLastError() == 5         # access denied: it exists; invalid parameter: it does not
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code)):
                return True
            return code.value == 259               # STILL_ACTIVE
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _lock_pid(path):
    """The process id written in a lock file, or None (missing, empty or damaged)."""
    try:
        return int(Path(path).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


_held = []   # the lock files this process holds now (see heartbeat)


@contextlib.contextmanager
def exclusive(path, stale_sec=1800):
    """Hold a lock file for the duration of the block. Yields True when the lock was taken and False when another process holds it
    (then nothing is touched). The file holds the id of its owner. A lock of a process that is alive is never taken over, however old
    the file is (a long step, a sleeping PC); a lock of a process that is gone is taken over at once. Only a lock file without a
    readable id falls back to its age: older than `stale_sec`, it is taken over. The owner removes only its own lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    me = os.getpid()
    fd = None
    for _ in range(3):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            owner = _lock_pid(path)
            try:
                if owner is None:
                    dead = time.time() - path.stat().st_mtime > stale_sec
                else:
                    dead = owner != me and not pid_alive(owner)
            except OSError:
                break
            if not dead or not _take_over(path, owner):
                break
    if fd is None:
        yield False
        return
    try:
        os.write(fd, str(me).encode("ascii"))
    finally:
        os.close(fd)
    _held.append(path)
    try:
        yield True
    finally:
        if path in _held:
            _held.remove(path)
        if _lock_pid(path) == me:   # never remove a lock that another process holds now
            try:
                path.unlink()
            except OSError:
                pass


def _take_over(path, owner):
    """Move the lock of a dead owner out of the way. Of several processes that find it, one wins the rename; what was moved is
    checked to be the dead owner's file, and put back when it was not (a live process took the lock in between)."""
    side = path.with_name(f"{path.name}.dead.{os.getpid()}")
    try:
        os.replace(path, side)
    except OSError:
        return False
    if _lock_pid(side) != owner:
        try:
            os.rename(side, path)   # fails when yet another lock exists: then that one stays
        except OSError:
            pass
        try:
            side.unlink()
        except OSError:
            pass
        return False
    try:
        side.unlink()
    except OSError:
        pass
    return True


def refresh(path):
    """Mark a lock file we hold as still in use."""
    try:
        os.utime(str(path))
    except OSError:
        pass


def heartbeat():
    """Mark every lock this process holds as in use. Called between the long steps (a call to the writer, to ADO, a page of comments)."""
    for p in list(_held):
        refresh(p)


class BusyList(list):
    """A result list; `busy` is True when another process held the lock: nothing was read, changed or saved."""
    busy = False

    @classmethod
    def busy_result(cls):
        res = cls()
        res.busy = True
        return res
