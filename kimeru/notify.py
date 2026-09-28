"""Human-in-the-loop over Teams "chat with yourself".

Queue items (advise nodes with queue=true) are posted to the self chat as
"[kimeru #N] ..." and the PM replies from any device (e.g. iPhone) with
"OK N" / "NG N" / "保留 N". Actions attached to an advise node are only
*proposed*; they run (dry-run in v0.1) when approved. A reply drafted by the
writer (writer.py) is shown in full; "修正 N <指示>" redrafts it and posts it again.
"""
import json
import re
import subprocess
import unicodedata
from pathlib import Path

from . import actions
from . import writer as writer_mod

HERE = Path(__file__).resolve().parent.parent
REPLY = re.compile(r"^(OK|NG|保留)\s*#?(\d+)$", re.IGNORECASE)
REDRAFT = re.compile(r"^修正\s*#?(\d+)\s*[:：]?\s*(\S.*)$")


def parse_reply(line):
    """("OK", "3") for "OK 3", "ok3", "ＯＫ　３", "Ok #3"; None otherwise."""
    m = REPLY.match(unicodedata.normalize("NFKC", line).strip())
    return (m.group(1).upper(), m.group(2)) if m else None


def parse_redraft(line):
    """("3", "もっと短く") for "修正 3 もっと短く"; None otherwise."""
    m = REDRAFT.match(unicodedata.normalize("NFKC", line).strip())
    return (m.group(1), m.group(2).strip()) if m else None
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

    def chats(self):
        return self._run("-Action", "chats").get("chats", [])


def format_post(n, rec):
    ev = rec.get("event_kind", "")
    lines = [f"[kimeru #{n}] 判断が必要（{rec.get('graph')}）", f"{ev} #{rec.get('event_id')}"]
    if rec.get("advice"):
        lines.append(f"内容: {rec['advice']}")
    if rec.get("actions"):
        lines.append("承認で実行: " + ", ".join(a.get("type", "?") for a in rec["actions"]))
    src = (rec.get("material_event") or {})
    if src and any(a.get("drafted_by") for a in rec.get("actions", [])):
        what = src.get("text") or src.get("item") or src.get("title") or src.get("rule") or ""
        who = src.get("author")
        lines.append("元: " + (f"{who}: " if who else "") + str(what)[:200])
    drafts = [a for a in rec.get("actions", []) if a.get("drafted_by")]
    for a in drafts:
        label = writer_mod.LABEL.get(a["type"], a["type"]) + (f"「{a['title']}」" if a.get("title") else "")
        lines.append(f"{label}の下書き（{a['drafted_by']}）:\n{a[writer_mod.FIELD[a['type']]]}")
        if a.get("unverified"):
            lines.append("⚠ 元の材料に無い日付・数値: " + ", ".join(a["unverified"]))
    lines.append(f"返信: OK {n} / NG {n} / 保留 {n}" + (f" / 修正 {n} <直してほしい点>" if drafts else ""))
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


def toast_text(out, posted, notices):
    """Title and body for the PC notification: counts, plus the first item's short summary."""
    ap = Approvals(out)
    parts = []
    if posted:
        parts.append(f"確認待ち {len(posted)} 件")
    if notices:
        parts.append(f"自動決定の通知 {notices} 件")
    first = ap.data["items"].get(str(posted[0])) if posted else None
    what = ""
    if first:
        rec = first["record"]
        what = f"#{posted[0]} " + str(rec.get("summary") or rec.get("advice") or rec.get("graph") or "")[:60]
    body = (what + (f" ほか {len(posted) - 1} 件" if len(posted) > 1 else "")).strip() or "Teams の自分とのチャットを確認してください"
    return "kimeru: " + " / ".join(parts), body


def show_toast(title, body):
    """Windows notification on this PC (tools/toast.ps1). Opt out with KIMERU_TOAST=0."""
    import os
    if os.environ.get("KIMERU_TOAST", "1") == "0":
        return "off"
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(HERE / "tools" / "toast.ps1"), "-Title", title, "-Body", body],
                       capture_output=True, text=True, encoding="utf-8", timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError((r.stdout + r.stderr).strip()[:200])
    return "shown"


def format_notice(rec):
    lines = [f"[kimeru 通知] 自動で決定しました（{rec.get('graph')}）"]
    if rec.get("summary"):
        lines.append(f"元: {str(rec['summary'])[:200]}")
    if rec.get("advice"):
        lines.append(f"内容: {rec['advice']}")
    ran = [e["action"].get("type", "?") for e in rec.get("executed", [])]
    if ran:
        lines.append("記録した行動: " + ", ".join(ran) + "（現在は記録のみ）")
    lines.append("返信は不要です")
    return "\n".join(lines)


def notify_notices(out, bridge, send=False):
    """Post automatic decisions the PM should know about (notices.jsonl), each once."""
    out = Path(out)
    ap = Approvals(out)
    done = set(ap.data.setdefault("notices", []))
    q = out / "notices.jsonl"
    rows = [json.loads(l) for l in q.read_text(encoding="utf-8").splitlines() if l.strip()] if q.exists() else []
    posted = []
    for rec in rows:
        key = _key(rec)
        if key in done:
            continue
        r = bridge.post(format_notice(rec), send) or {}
        if send and (r.get("typed") is False or r.get("sent") is False):
            raise RuntimeError(f"notice {key} was not posted as planned: {r}")
        if send:
            ap.data["notices"].append(key); done.add(key)
            ap.save()
        posted.append(key)
    return posted


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
        r = bridge.post(format_post(n, it["record"]), send) or {}
        if send and (r.get("typed") is False or r.get("sent") is False):
            raise RuntimeError(f"#{n} was not posted as planned: {r}")   # stays unposted; retried next cycle
        if send:
            it["posted"] = True
            ap.save()  # a failure on a later item must not forget what was already sent
        posted.append(int(n))
    ap.save()
    return posted


def fresh_replies(read):
    """Replies that come after the most recent visible post with the same number.

    Old "OK 1" lines from an earlier run stay in the chat; without this, a new
    item #1 would be approved by them. With a timeline, a reply counts only if
    it appears after the last "[kimeru #1]" post; if that post is not visible,
    freshness cannot be proven and the reply is ignored.
    """
    tl = read.get("timeline")
    if tl is None:  # older bridge without ordering
        return list(read.get("replies", []))
    last_post = {}
    for i, e in enumerate(tl):
        if e.startswith("P:"):
            last_post[e[2:]] = i
    out = []
    for i, e in enumerate(tl):
        if e.startswith("R:"):
            r = parse_reply(e[2:]) or parse_redraft(e[2:])
            num = r and (r[1] if r[0] in STATUS else r[0])
            if num and i > last_post.get(num, len(tl)):
                out.append(e[2:])
    return out


def _redraft(it, instruction, writer):
    rec = it["record"]
    if writer is None or not rec.get("material_event"):
        return None
    if not writer_mod.apply(rec, rec["material_event"], writer, instruction):
        return None
    it["posted"], it["status"] = False, "pending"   # next notify posts the new draft under the same number
    return {"status": "redrafted", "instruction": instruction}


def collect(out, bridge, writer=None):
    """Read replies from the self chat and apply them. Returns applied changes."""
    ap = Approvals(out)
    # nothing is waiting for an answer: do not touch Teams at all (reading switches it to the self chat)
    if not any(it["posted"] and it["status"] in ("pending", "held") for it in ap.data["items"].values()):
        return []
    writer = writer if writer is not None else writer_mod.get_writer()
    changes = []
    for line in fresh_replies(bridge.read()):
        rd = parse_redraft(line)
        if rd:
            it = ap.data["items"].get(rd[0])
            if it and it["posted"] and it["status"] in ("pending", "held"):
                ch = _redraft(it, rd[1], writer)
                if ch:
                    changes.append({"id": int(rd[0]), **ch})
            continue
        r = parse_reply(line)
        if not r:
            continue
        word, num = r
        it = ap.data["items"].get(num)
        if not it or not it["posted"] or it["status"] not in ("pending", "held"):
            continue
        new = STATUS[word]
        if new == it["status"]:
            continue
        it["status"] = new
        ch = {"id": int(num), "status": new}
        if new == "approved":
            ch["executed"] = [actions.execute(a, dry_run=True) for a in it["record"].get("actions", [])]
        changes.append(ch)
    ap.save()
    if changes:
        with (Path(out) / "approvals.log.jsonl").open("a", encoding="utf-8") as f:
            for ch in changes:
                f.write(json.dumps(ch, ensure_ascii=False) + "\n")
    return changes
