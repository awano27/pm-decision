"""Normalize raw source payloads into flat Event dicts.

Every event has: kind, id, source, ts, and kind-specific fields.
The `state` sent to the judge is the event minus `raw`.
"""
import hashlib
import html
import re

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
        "description": _strip_html(f.get("System.Description") or f.get("Microsoft.VSTS.TCM.ReproSteps") or ""),
        "acceptance_criteria": _strip_html(f.get("Microsoft.VSTS.Common.AcceptanceCriteria") or ""),
    }]


_BULLET = re.compile(r"^\s*(?:[-*・•]|\d+[.)])\s+(.*\S)")


def meeting_minutes(p):
    """Minutes as {title, date, text}. Fans out: one event per bullet line."""
    title, date, text = p.get("title", ""), p.get("date"), p.get("text", "")
    items = [m.group(1) for line in text.splitlines() if (m := _BULLET.match(line))]
    return [{
        "kind": "meeting.item", "source": "minutes",
        "id": _id(title, date, i), "ts": date,
        "meeting": title, "index": i, "item": it,
    } for i, it in enumerate(items)]


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


def state_of(event):
    return {k: v for k, v in event.items() if k != "raw"}
