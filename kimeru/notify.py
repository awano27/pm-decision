"""Human-in-the-loop over Teams "chat with yourself".

Queue items (advise nodes with queue=true) are posted to the self chat as
"[kimeru #N] ..." and the PM replies from any device (e.g. iPhone) with
"OK N" / "NG N" / "保留 N". Actions attached to an advise node are only
*proposed*; they run (dry-run in v0.1) when approved.
"""
import json
import re
import subprocess
from pathlib import Path

from . import actions

HERE = Path(__file__).resolve().parent.parent
REPLY = re.compile(r"^(OK|NG|保留)\s*#?(\d+)$")
STATUS = {"OK": "approved", "NG": "rejected", "保留": "held"}


class PowerShellBridge:
    """Runs tools/teams-self.ps1 (built-in UI Automation, no downloads)."""

    def __init__(self, script=HERE / "tools" / "teams-self.ps1"):
        self.script = str(script)

    def _run(self, *args):
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", self.script, *args],
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        out = json.loads(r.stdout.strip().lstrip("﻿") or "{}")
        if not out.get("ok"):
            raise RuntimeError(out.get("error") or r.stderr.strip()[:300])
        return out

    def post(self, text, send):
        # line breaks travel as literal \n (command-line args); the script expands them
        return self._run("-Action", "post", "-Text", text.replace("\n", "\\n"), *(["-Send"] if send else []))

    def read(self):
        return self._run("-Action", "read")


def format_post(n, rec):
    ev = rec.get("event_kind", "")
    lines = [f"[kimeru #{n}] 判断が必要（{rec.get('graph')}）", f"{ev} #{rec.get('event_id')}"]
    if rec.get("advice"):
        lines.append(f"内容: {rec['advice']}")
    if rec.get("actions"):
        lines.append("承認で実行: " + ", ".join(a.get("type", "?") for a in rec["actions"]))
    lines.append(f"返信: OK {n} / NG {n} / 保留 {n}")
    return "\n".join(lines)


class Approvals:
    def __init__(self, out):
        self.path = Path(out) / "approvals.json"
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"next": 1, "items": {}}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")

    def known(self, key):
        return any(it["key"] == key for it in self.data["items"].values())

    def add(self, key, rec):
        n = self.data["next"]
        self.data["next"] += 1
        self.data["items"][str(n)] = {"key": key, "status": "pending", "posted": False, "record": rec}
        return n


def _key(rec):
    return f"{rec.get('graph')}:{rec.get('event_id')}:{rec.get('node')}"


def notify(out, bridge, send=False):
    """Post every not-yet-posted queue item to the self chat."""
    out = Path(out)
    ap = Approvals(out)
    q = out / "queue.jsonl"
    rows = [json.loads(l) for l in q.read_text(encoding="utf-8").splitlines() if l.strip()] if q.exists() else []
    for rec in rows:
        if not ap.known(_key(rec)):
            ap.add(_key(rec), rec)
    posted = []
    for n, it in ap.data["items"].items():
        if it["posted"]:
            continue
        bridge.post(format_post(n, it["record"]), send)
        if send:
            it["posted"] = True
        posted.append(int(n))
    ap.save()
    return posted


def collect(out, bridge):
    """Read replies from the self chat and apply them. Returns applied changes."""
    ap = Approvals(out)
    changes = []
    for line in bridge.read().get("replies", []):
        m = REPLY.match(line.strip())
        if not m:
            continue
        it = ap.data["items"].get(m.group(2))
        if not it or it["status"] not in ("pending", "held"):
            continue
        new = STATUS[m.group(1)]
        if new == it["status"]:
            continue
        it["status"] = new
        ch = {"id": int(m.group(2)), "status": new}
        if new == "approved":
            ch["executed"] = [actions.execute(a, dry_run=True) for a in it["record"].get("actions", [])]
        changes.append(ch)
    ap.save()
    if changes:
        with (Path(out) / "approvals.log.jsonl").open("a", encoding="utf-8") as f:
            for ch in changes:
                f.write(json.dumps(ch, ensure_ascii=False) + "\n")
    return changes
