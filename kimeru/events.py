"""Normalize raw source payloads into flat Event dicts.

Every event has: kind, id, source, ts, and kind-specific fields.
The `state` sent to the judge is the event minus `raw`.
"""
import hashlib
import html
import json
import re
from datetime import datetime

KINDS = ("teams.chat", "monitor.alert", "ado.workitem.created", "meeting.item")


def _strip_html(s):
    s = re.sub(r"<br\s*/?>|</p>", "\n", s or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _id(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def teams_chat(p):
    """Microsoft Graph chatMessage resource (or change-notification `resourceData`)."""
    m = p.get("resourceData", p)
    frm = (m.get("from") or {}).get("user") or {}
    return [{
        "kind": "teams.chat", "source": "teams",
        "id": m.get("id") or _id(m.get("createdDateTime"), frm.get("displayName")),
        "ts": m.get("createdDateTime"),
        "chat_id": m.get("chatId") or (m.get("channelIdentity") or {}).get("channelId"),
        "author": frm.get("displayName"),
        "mentions_me": bool(m.get("mentions")),
        "text": _strip_html((m.get("body") or {}).get("content", "")),
    }]


def monitor_alert(p):
    """Azure Monitor common alert schema."""
    ess = (p.get("data") or p).get("essentials") or {}
    ctx = (p.get("data") or p).get("alertContext") or {}
    return [{
        "kind": "monitor.alert", "source": "azure-monitor",
        "id": ess.get("alertId") or _id(ess.get("alertRule"), ess.get("firedDateTime")),
        "ts": ess.get("firedDateTime"),
        "rule": ess.get("alertRule"),
        "severity": ess.get("severity"),
        "condition": ess.get("monitorCondition"),
        "resources": ess.get("alertTargetIDs") or [],
        "description": ess.get("description") or "",
        "context": ctx,
    }]


def ado_workitem_created(p):
    """Azure DevOps service hook `workitem.created`."""
    r = p.get("resource") or {}
    f = r.get("fields") or {}
    return [{
        "kind": "ado.workitem.created", "source": "azure-devops",
        "id": str(r.get("id")),
        "ts": f.get("System.CreatedDate"),
        "type": f.get("System.WorkItemType"),
        "title": f.get("System.Title"),
        "area": f.get("System.AreaPath"),
        "created_by": (f.get("System.CreatedBy") or {}).get("displayName") if isinstance(f.get("System.CreatedBy"), dict) else f.get("System.CreatedBy"),
        "priority": f.get("Microsoft.VSTS.Common.Priority"),
        "description": _strip_html(f.get("System.Description") or ""),
        "repro_steps": _strip_html(f.get("Microsoft.VSTS.TCM.ReproSteps") or ""),
        "acceptance_criteria": _strip_html(f.get("Microsoft.VSTS.Common.AcceptanceCriteria") or ""),
        # where `pull ado` took it from ({"org", "project"}); a service-hook payload has none: nothing is written back to it
        "origin": p.get("kimeru_origin") if isinstance(p.get("kimeru_origin"), dict) else None,
    }]


_BULLET = re.compile(r"^\s*(?:[-*・•]|\d+[.)])\s+(.*\S)")


def meeting_minutes(p):
    """Minutes as {title, date, text}. Fans out: one event per bullet line."""
    title, date, text = p.get("title", ""), p.get("date"), p.get("text", "")
    items = [m.group(1) for line in text.splitlines() if (m := _BULLET.match(line))]
    return [{
        "kind": "meeting.item", "source": "minutes",
        # id from the line's text, not its position: an edited file (lines inserted above) or another
        # meeting with the same title and date must not collide with lines already decided
        "id": _id(title, date, re.sub(r"\s+", " ", it).strip()), "ts": date,
        "meeting": title, "index": i, "item": it,
    } for i, it in enumerate(items)]


_DATE = re.compile(r"(20\d\d)[-/.年](\d{1,2})[-/.月](\d{1,2})")
INBOX_SUFFIXES = (".json", ".txt", ".md")


def minutes_text(text, name="", mtime=None):
    """Plain-text minutes (e.g. a Copilot recap the PM pasted into Notepad) -> minutes payload.

    Title: first non-bullet line, else the file name. Date: first YYYY-MM-DD in the title or
    file name, else the file's modified date. Every bullet line becomes one item."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    title = next((l.lstrip("#").strip() for l in lines if not _BULLET.match(l)), "") or name
    m = _DATE.search(title) or _DATE.search(name)
    date = f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}" if m else (mtime.strftime("%Y-%m-%d") if mtime else None)
    return {"title": title, "date": date, "text": text}


def inbox_files(inbox):
    return sorted(f for f in inbox.iterdir() if f.is_file() and f.suffix.lower() in INBOX_SUFFIXES)


def read_inbox_file(f):
    """Raw payload from an inbox file: JSON as-is, .txt/.md as minutes (UTF-8, or Shift_JIS from old Notepad)."""
    b = f.read_bytes()
    try:
        text = b.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = b.decode("cp932")
    if f.suffix.lower() == ".json":
        return json.loads(text)
    return minutes_text(text, f.stem, datetime.fromtimestamp(f.stat().st_mtime))


NORMALIZERS = {
    "teams": teams_chat,
    "alert": monitor_alert,
    "ado": ado_workitem_created,
    "minutes": meeting_minutes,
}


def detect(p):
    """Guess the source type of a raw payload."""
    if p.get("eventType") == "workitem.created":
        return "ado"
    if p.get("schemaId") == "azureMonitorCommonAlertSchema" or "essentials" in (p.get("data") or {}):
        return "alert"
    if "text" in p and "title" in p:
        return "minutes"
    if "body" in p or "resourceData" in p:
        return "teams"
    raise ValueError("unknown payload type")


def normalize(p, source=None):
    if "kind" in p and p["kind"] in KINDS:
        return [p]
    return NORMALIZERS[source or detect(p)](p)


def summary(event, limit=120):
    """One human-readable line for logs and reports."""
    k = event.get("kind")
    if k == "teams.chat":
        s = f"{event.get('author') or ''}: {event.get('text') or ''}"
    elif k == "monitor.alert":
        s = f"{event.get('rule') or ''} ({event.get('severity') or ''}) {event.get('description') or ''}"
    elif k == "ado.workitem.created":
        s = f"#{event.get('id')} {event.get('title') or ''}"
    elif k == "meeting.item":
        s = f"{event.get('meeting') or ''}: {event.get('item') or ''}"
    else:
        s = str(event.get("id"))
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


JUDGE_HIDDEN = ("raw", "thread", "full", "chat_title", "origin")   # origin: where `pull ado` took it from (execute.py), not for the judge


def state_of(event):
    """What the judge sees: the event without its raw payload and the earlier messages of a thread (they are for the writer);
    `chat_title` is kept on the event but not sent (it repeats `author`; an extra field lowers Kev's confidence);
    `text` is cut at `judge_text_max` characters (Kev refuses input that is too long, and a long text lowers its confidence)."""
    from . import config
    try:
        cap = max(100, int(config.value("judge_text_max")))
    except ValueError:
        cap = 1200
    s = {k: v for k, v in event.items() if k not in JUDGE_HIDDEN}
    if isinstance(s.get("text"), str) and len(s["text"]) > cap:
        s["text"] = s["text"][:cap]
    return s
