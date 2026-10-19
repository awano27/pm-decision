"""T7: a draft check that nobody answered is closed as 期限切れ (status "expired") after `draft_expire_hours` hours.

Only a post that waits for nothing but a writer's draft is closed (held_reason "draft", the item is posted and still pending).
A judgment the model was unsure of, a severe case, a held item, and any item with an action that can write outside (an ADO
comment, a Teams reply: execute.is_gated) is never closed. The status is neither an approval nor a rejection: `OK N` after it is
not applied, nothing is executed, and the case stays in approvals.json and approvals.log.jsonl for the week's digest and the trial
week. At 0 (the default) nothing is looked at."""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import config, fsutil, hold


def _posted_at(it):
    t = (it.get("measurement") or {}).get("posted_at")
    if not isinstance(t, str):
        return None
    try:
        v = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    return v if v.tzinfo else None


def eligible(it):
    """True for an item that waits only for a writer's draft, with no way to write outside on OK."""
    from . import execute, learn
    if not isinstance(it, dict) or learn.is_card(it) or it.get("status") != "pending" or not it.get("posted"):
        return False
    rec = it.get("record") if isinstance(it.get("record"), dict) else {}
    if rec.get("held_reason") != "draft" or rec.get("severe_guard") or rec.get("via_unsure") or it.get("delivery_unknown"):
        return False
    actions = rec.get("actions") or []
    return not any(_writes_outside(a, execute) for a in actions if isinstance(a, dict))


def _writes_outside(a, execute):
    """An action that an OK can carry out (an ADO comment, whether or not execute is switched on now) or that `送る N` can send."""
    t = a.get("type")
    return bool(execute.is_gated(a) or a.get("exec_text") or a.get("reply_text")
                or (t in execute.SUPPORTED and t not in execute.REPLY_KINDS))


def close_expired(out, now=None):
    """Close the draft checks older than draft_expire_hours. Under approvals.lock (busy: nothing changed). Returns the numbers."""
    hours = config.int_value("draft_expire_hours")
    if hours <= 0:
        return fsutil.BusyList([])
    from . import notify
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.astimezone()
    with fsutil.exclusive(notify.lock_path(out)) as got:
        if not got:
            return fsutil.BusyList.busy_result()
        ap = notify.Approvals(out)
        closed = []
        for n, it in ap.data["items"].items():
            if not eligible(it):
                continue
            at = _posted_at(it)
            if at is None or (now - at).total_seconds() < hours * 3600:
                continue
            it["status"] = "expired"
            it["expired_at"] = now.isoformat(timespec="seconds")
            it.pop("detail_question", None)
            hold.clear(it)
            closed.append(n)
        if closed:
            from . import fulltext
            for n in closed:
                fulltext.drop_safe(out, ap.data["items"][n]["key"])
            ap.save()
            with (Path(out) / "approvals.log.jsonl").open("a", encoding="utf-8") as f:
                for n in closed:
                    it = ap.data["items"][n]
                    f.write(json.dumps({"at": now.astimezone(timezone.utc).isoformat(timespec="seconds"), "key": it.get("key"),
                                        "id": int(n), "status": "expired", "measurement": dict(notify.measurement_for(it))},
                                       ensure_ascii=False) + "\n")
        return fsutil.BusyList(closed)
