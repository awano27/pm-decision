"""Release steps that used to be done by hand (see the "Release 1.x.0" commits).

  python tools/ship.py bump 1.4.0 [--date 2026-11-01]   version in pyproject, package, README badge; CHANGELOG heading
  python tools/ship.py check 1.4.0                      every place agrees and the CHANGELOG section has a summary
  python tools/ship.py notes 1.4.0 --out notes.md       the CHANGELOG section body, for the GitHub Release

tools/ship.ps1 runs these around the tag; .github/workflows/release.yml runs check and notes on the tag push.
"""
import argparse
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNRELEASED = "## 未リリース"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# (file, pattern with one group around the version, how the version is written there)
PLACES = (
    ("pyproject.toml", re.compile(r'^version = "([^"]+)"', re.M), lambda v: v),
    ("kimeru/__init__.py", re.compile(r'^__version__ = "([^"]+)"', re.M), lambda v: v),
    ("README.md", re.compile(r"badge/status-v([0-9.]+)%20"), lambda v: ".".join(v.split(".")[:2])),
)


def _read(root, name):
    return (root / name).read_text(encoding="utf-8")


def _write(root, name, text):
    (root / name).write_text(text, encoding="utf-8", newline="\n")


def section(changelog, version):
    """The body under '## <version>（...）' up to the next '## ' heading, or None."""
    m = re.search(rf"^## {re.escape(version)}（[^）]*）[ \t]*\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    return m.group(1).strip("\n") + "\n" if m else None


def bump(root, version, date):
    changes = []
    for name, pattern, form in PLACES:
        text = _read(root, name)
        new, n = pattern.subn(lambda m: m.group(0).replace(m.group(1), form(version)), text, count=1)
        if n != 1:
            raise SystemExit(f"{name}: version not found")
        if new != text:
            _write(root, name, new)
            changes.append(name)
    log = _read(root, "CHANGELOG.md")
    if section(log, version) is None:
        if UNRELEASED + "\n" not in log:
            raise SystemExit(f"CHANGELOG.md: no '{UNRELEASED}' section to release")
        log = log.replace(UNRELEASED + "\n", f"## {version}（{date}）\n", 1)
        _write(root, "CHANGELOG.md", log)
        changes.append("CHANGELOG.md")
    return changes


def check(root, version):
    """The problems that block releasing <version>; empty when it can go out."""
    problems = []
    if not VERSION_RE.match(version):
        return [f"not a version: {version}"]
    for name, pattern, form in PLACES:
        m = pattern.search(_read(root, name))
        if not m:
            problems.append(f"{name}: version not found")
        elif m.group(1) != form(version):
            problems.append(f"{name}: {m.group(1)} (expected {form(version)})")
    log = _read(root, "CHANGELOG.md")
    body = section(log, version)
    if body is None:
        problems.append(f"CHANGELOG.md: no '## {version}（YYYY-MM-DD）' section")
    else:
        first = next((line for line in body.splitlines() if line.strip()), "")
        if not first or first.startswith("- "):
            problems.append(f"CHANGELOG.md: the {version} section needs a summary paragraph before the entries")
        if log.index(f"## {version}（") > log.find(UNRELEASED) >= 0:
            problems.append(f"CHANGELOG.md: '{UNRELEASED}' entries sit above {version}; move them into it")
    return problems


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("step", choices=("bump", "check", "notes"))
    p.add_argument("version")
    p.add_argument("--date", default=datetime.date.today().isoformat())
    p.add_argument("--out", help="notes: write here instead of stdout")
    p.add_argument("--root", type=Path, default=ROOT)
    a = p.parse_args(argv)
    version = a.version.removeprefix("v")
    if a.step == "bump":
        if not VERSION_RE.match(version):
            raise SystemExit(f"not a version: {version}")
        changed = bump(a.root, version, a.date)
        print("updated: " + (", ".join(changed) or "nothing (already at this version)"))
        print(f"next: write the summary paragraph under '## {version}' in CHANGELOG.md, then commit 'Release {version}'")
        return 0
    problems = check(a.root, version)
    if a.step == "check" or problems:
        for line in problems:
            print("NG " + line, file=sys.stderr)
        if not problems:
            print(f"OK {version}")
        return 1 if problems else 0
    notes = section(_read(a.root, "CHANGELOG.md"), version)
    if a.out:
        Path(a.out).write_text(notes, encoding="utf-8", newline="\n")
    else:
        sys.stdout.buffer.write(notes.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
