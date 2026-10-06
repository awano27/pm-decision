"""State files that survive a crash: write to a temp file, keep the previous version as .bak, replace.

A half-written approvals.json used to make every step fail with JSONDecodeError, every cycle, forever.
"""
import contextlib
import json
import os
import re
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


def _pid_of(pid):
    """`pid` as an int in 1 .. 2**32-1, else None (a damaged value never reaches the system calls)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError, OverflowError):
        return None
    return pid if 0 < pid < 2 ** 32 else None


def pid_alive(pid):
    """True when a process with this id exists and may be looked at. A process that Windows refuses to open (access denied) is not
    counted: kimeru runs with the rights of the user who started it, so a process that cannot be opened is never a kimeru of this user
    (its id was re-used). Never signals the process.
    Only the id is checked here; `lock_owner_alive` also compares the creation time, so a re-used id is not taken for the owner."""
    pid = _pid_of(pid)
    if pid is None:
        return False
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = ctypes.c_void_p
            h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False                           # access denied: not ours (see above); invalid parameter: it does not exist
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code)):
                    return True
                return code.value == 259               # STILL_ACTIVE
            finally:
                k32.CloseHandle(ctypes.c_void_p(h))
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False    # a process of another user: not a kimeru of this user
    except Exception:   # OSError, OverflowError, ctypes.ArgumentError and the like: an unusable id is not a live owner
        return False
    return True


def proc_start(pid):
    """The creation time of the process (an integer that is the same for the whole life of that process and differs for the next
    process that gets the same id), or None when it cannot be read here."""
    pid = _pid_of(pid)
    if pid is None:
        return None
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = ctypes.c_void_p
            h = k32.OpenProcess(0x1000, False, pid)
            if not h:
                return None
            try:
                t = [ctypes.c_ulonglong() for _ in range(4)]   # creation, exit, kernel, user (FILETIME = 64 bits)
                if not k32.GetProcessTimes(ctypes.c_void_p(h), *[ctypes.byref(x) for x in t]):
                    return None
                return int(t[0].value) or None
            finally:
                k32.CloseHandle(ctypes.c_void_p(h))
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        return int(stat.rsplit(")", 1)[1].split()[19]) + 1   # field 22, starttime (+1 so that it is never 0)
    except Exception:
        return None


def lock_text(pid=None):
    """What a lock file holds: `pid:creation-time` (just `pid` where the creation time cannot be read)."""
    pid = os.getpid() if pid is None else pid
    start = proc_start(pid)
    return f"{pid}:{start}" if start else str(pid)


_LOCK_RE = re.compile(r"(\d{1,10})(?::(\d{1,20}))?")


def _lock_info(path):
    """(pid, creation time or None) written in a lock file, or None when it is missing, empty, or damaged
    (not digits, out of range, any other shape)."""
    try:
        text = Path(path).read_bytes().decode("ascii").strip()
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    m = _LOCK_RE.fullmatch(text)
    if not m:
        return None
    pid = _pid_of(m.group(1))
    start = int(m.group(2)) if m.group(2) else None
    if pid is None or (start is not None and start >= 2 ** 64):
        return None
    return pid, start


def _lock_pid(path):
    """The process id written in a lock file, or None (missing, empty or damaged)."""
    info = _lock_info(path)
    return info[0] if info else None


def lock_owner_alive(info):
    """True when the process that wrote `info` is still the one running: its id is alive and its creation time is the one written.
    A process we may not look at is not the owner. Without a written creation time only the id is known."""
    pid, start = info
    if not pid_alive(pid):
        return False
    if start is None:
        return True
    now = proc_start(pid)
    return now is None or now == start


_held = []   # the lock files this process holds now (see heartbeat)


class LockFolderError(OSError):
    """The lock file cannot be created because the folder cannot be written (not because another run holds the lock)."""


def _folder_writable(folder):
    """True when a file can be created in `folder` (a probe file that is removed at once)."""
    probe = Path(folder) / f".probe.{os.getpid()}.{threading.get_ident()}"
    try:
        fd = os.open(str(probe), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    os.close(fd)
    try:
        probe.unlink()
    except OSError:
        pass
    return True


def _unlink_retry(path, tries=25):
    """Remove a file, waiting out the moment in which another process has it open (Windows: PermissionError). True when it is gone."""
    for k in range(tries):
        try:
            Path(path).unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.01 * min(k + 1, 5))
    return False


@contextlib.contextmanager
def exclusive(path, stale_sec=1800):
    """Hold a lock file for the duration of the block. Yields True when the lock was taken and False when another process holds it
    (then nothing is touched). The file holds the id and the creation time of its owner. A lock of a process that is still running is
    never taken over, however old the file is (a long step, a sleeping PC); a lock of a process that is gone, or of another process
    that has the same id now, is taken over at once; so is a leftover of this same process that is not held now (a release that
    failed). A lock file without a readable owner (empty, damaged), or one written where the creation time cannot be read, falls back
    to its age: older than `stale_sec`, it is taken over. The owner removes only its own lock, trying again when it is busy."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    me = os.getpid()
    fd = None
    denied = 0
    for _ in range(6):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except PermissionError:   # Windows: the file is being deleted by its owner, or is open in another process: not ours yet
            denied += 1
            time.sleep(0.01)
            continue
        except FileExistsError:
            info = _lock_info(path)
            try:
                if info is None:
                    dead = time.time() - path.stat().st_mtime > stale_sec
                elif info[0] == me and (info[1] is None or info[1] == proc_start(me)):
                    dead = path not in _held                  # ours: a leftover unless this process holds it right now
                elif not lock_owner_alive(info):
                    dead = True
                elif info[1] is None:
                    dead = time.time() - path.stat().st_mtime > stale_sec   # no creation time to compare: the age decides
                else:
                    dead = False
            except OSError:
                break
            if not dead or not _take_over(path, info):
                break
    if fd is None:
        if denied >= 6 and not path.exists() and not _folder_writable(path.parent):
            # not "another run is in progress": nobody holds a lock; the folder cannot be written
            raise LockFolderError(f"cannot create the lock file {path}: the folder cannot be written to (check its permissions)")
        yield False
        return
    try:
        os.write(fd, lock_text(me).encode("ascii"))
    finally:
        os.close(fd)
    _held.append(path)
    try:
        yield True
    finally:
        mine = _lock_info(path)
        if mine is not None and mine[0] == me:   # never remove a lock that another process holds now
            _unlink_retry(path)
        if path in _held:   # only after the file is gone, so that a thread of this process cannot take a half-released lock
            _held.remove(path)


def _take_over(path, info):
    """Move the lock of a dead owner out of the way. Of several processes that find it, one wins the rename; what was moved is
    checked to be the file that was judged dead, and put back when it was not (a live process took the lock in between)."""
    side = path.with_name(f"{path.name}.dead.{os.getpid()}.{threading.get_ident()}")
    try:
        os.replace(path, side)
    except OSError:
        return False
    if _lock_info(side) != info:
        try:
            os.rename(side, path)   # fails when yet another lock exists: then that one stays
        except OSError:
            pass
        _unlink_retry(side)
        return False
    _unlink_retry(side)
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
