"""Where the folders kimeru reads at run time live (graphs/, playbooks/, examples/, tools/).

In a clone (or an unpacked ZIP) they sit next to the `kimeru` package. After `pip install` / `uv tool install` they are in the
shared data folder of the installation (<prefix>/share/kimeru). `root()` finds whichever exists; nothing else in the package
builds these paths. Records (`out/`) and the state folder are not decided here."""
import site
import sys
import sysconfig
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_NEEDED = ("graphs", "playbooks")
_cache = None


class NotFound(Exception):
    """The data folders are in neither place; str() is one plain line (the caller prints it)."""


def _has_data(d):
    return all((Path(d) / n).is_dir() for n in _NEEDED)


def _prefixes():
    found = []
    for get in (lambda: sys.prefix, lambda: sysconfig.get_path("data"), lambda: site.getuserbase()):
        try:
            v = get()
        except Exception:
            v = None
        if v and Path(v) not in found:
            found.append(Path(v))
    return found


def _find(repo, prefixes):
    if _has_data(repo):
        return Path(repo)
    tried = [Path(repo)]
    for pre in prefixes:
        shared = Path(pre) / "share" / "kimeru"
        if _has_data(shared):
            return shared
        tried.append(shared)
    raise NotFound(f"判断グラフ／進め方の型のフォルダが見つかりません（{', '.join(str(t / n) for t in tried[:2] for n in _NEEDED)}）。"
                   "clone したフォルダで python -m kimeru を実行するか、pip / uv tool install で入れ直してください")


def root():
    """The folder that holds graphs/, playbooks/, examples/ and tools/. Raises NotFound when there is none. Kept for the run."""
    global _cache
    if _cache is None:
        _cache = _find(_REPO, _prefixes())
    return _cache


def base():
    """root(), or the clone's place when there is no data at all: for names that are computed when a module is imported (an
    import must not fail; the use of a missing folder is reported where it is used)."""
    try:
        return root()
    except NotFound:
        return _REPO


def home():
    """The folder that holds the `kimeru` package (a clone's top; the place of .git or VERSION). Not a data folder."""
    return _REPO


def reset():
    """Forget the kept result (tests)."""
    global _cache
    _cache = None
