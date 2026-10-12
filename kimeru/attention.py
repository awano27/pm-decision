"""When the PC notification and the phone push tell the PM about a posted item (R17, interrupt=sittings).

With interrupt=all (the default) nothing here changes anything: every posted item is announced at once, as before.

With interrupt=sittings, a posted item that is not urgent is announced at the first daily cycle at or after the next sitting
(sit_hours, for example 9, 13, 17), not at once. An urgent item is announced at once. Urgent means one of:

  severe      graph.severe_hit: a Sev0/Sev1 alert, a work item at priority 1 (also one that went to triage_pm)
  critical    a critical rule (critical_incident, critical_bug, critical_outage) that said yes or mixed
  notify      the decision asks for notice (res["notify"])
  alert       the event is a monitor.alert
  words       a word of urgent_words (default 至急|本日中|今日中|大至急|緊急|ASAP) is in the whole text of the event

The judgment is taken when the item is decided and kept in its record as `attn` (only in this mode, or while 休み is read (R27,
quiet_words=1), so the records of the default do not change). A record without `attn` counts as urgent: what is not known is never
held. A follow-up that came after the item was judged, or an item that came back from a hold (R09), is announced at once too. The
reasons say which rule applied, never the text.

R27 (休み <いつ>, control.quiet_until): until the time of a quiet, what is not urgent is held exactly as with the sittings, whatever
interrupt says, and no sitting announces. When the time has come, interrupt=sittings takes it as a sitting of its own (announced once,
then the sittings go on as before); interrupt=all has nothing held, so the next cycle announces what waited.

Notices, the count of posts that failed and the acknowledgements of replies are never held: only posted items are.
The sittings are kept per route: approvals.json `sitting_toast` for the PC notification, push_state.json `sitting_at` for each
phone route. A route whose last recorded sitting is not the current one has it due; the sitting is recorded only after the
announcement succeeded, so a failed one is tried again in the next cycle (see daily.cycle).
"""
import json
import re
import unicodedata
from datetime import datetime, time, timedelta
from pathlib import Path

from . import config, control, fsutil, graph

DEFAULT_WORDS = "至急|本日中|今日中|大至急|緊急|ASAP"   # the default of urgent_words (config.SETTINGS)
CRITICAL = ("critical_incident", "critical_bug", "critical_outage")
CHASE_MINUTES = 30   # R37: a follow-up that comes this long after the first ready time of a waiting item is a chase
CHASE_WORDS = "まだ|催促|督促|返事|返信|いかが|どうなり|進捗"   # R37: a follow-up with one of these words is a chase at any time


def enabled():
    """True when the sittings are on (interrupt=sittings). Off, nothing is recorded and nothing is held."""
    return config.value("interrupt") == "sittings"


def recording():
    """True when each decision records its urgency (`attn`): with the sittings, or while 休み is read (R27), so that a quiet can
    hold what is not urgent. Otherwise no field is written, as before."""
    return enabled() or control.quiet_enabled()


def hours():
    """The sorted hours of the sittings when they apply, else None (interrupt=all, or sit_hours is not a list of hours: then
    every item is announced at once, as before)."""
    if not enabled():
        return None
    return config.sit_hours_list(config.value("sit_hours"))


def _local(now):
    """A naive local datetime (the cycle's clock is naive local; an aware one is converted)."""
    return now.astimezone().replace(tzinfo=None) if now.tzinfo is not None else now


def _texts(x):
    """Every string inside an event, whatever its nesting (the whole text of the event)."""
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _texts(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _texts(v)


def urgent_pattern():
    """The regular expression of urgent_words (NFKC text, any case). A value that is not a valid pattern gives the default words
    (config.apply says so once at start-up)."""
    try:
        return re.compile(config.value("urgent_words"), re.IGNORECASE)
    except re.error:
        return re.compile(DEFAULT_WORDS, re.IGNORECASE)


def judge(g, res, ev):
    """{"urgent": bool, "why": [rule names]} for one decided event (`res` is the judgment, `ev` the event as it was judged).
    Only the rules listed in the module doc make an item urgent: a match hit such as has_ac, ack_only or label_* never does."""
    why = []
    if graph.severe_hit(g, res.get("node"), ev):
        why.append("severe")
    if any(s.get("node") in CRITICAL and s.get("edge") in ("yes", "mixed") for s in res.get("path") or []):
        why.append("critical")
    if res.get("notify"):
        why.append("notify")
    if ev.get("kind") == "monitor.alert":
        why.append("alert")
    pat = urgent_pattern()
    if any(pat.search(unicodedata.normalize("NFKC", t)) for t in _texts(ev)):
        why.append("words")
    return {"urgent": bool(why), "why": why}


def announce_now(it):
    """True when a posted item is announced at once, whatever the sitting: urgent when it was decided, or with no record of that
    (an item from before this setting), or a follow-up came after it was judged, or it came back from a hold (R09)."""
    rec = it.get("record") or {}
    attn = rec.get("attn")
    if not isinstance(attn, dict) or not isinstance(attn.get("urgent"), bool) or attn["urgent"]:
        return True
    if int(it.get("revision") or rec.get("revision") or 1) > 1 and rec.get("followups"):
        return True
    return int(it.get("hold_round") or 0) > 0


def boundary(now):
    """The last sitting at or before `now` (naive local, "YYYY-MM-DDTHH:MM"), or None when the sittings do not apply."""
    hs = hours()
    if not hs:
        return None
    now = _local(now)
    past = [h for h in hs if h <= now.hour]
    if past:
        return datetime.combine(now.date(), time(max(past))).isoformat(timespec="minutes")
    return datetime.combine(now.date() - timedelta(days=1), time(hs[-1])).isoformat(timespec="minutes")


def next_sitting(now):
    """The first sitting after `now` (a naive datetime), or None when the sittings do not apply."""
    hs = hours()
    if not hs:
        return None
    now = _local(now)
    later = [h for h in hs if h > now.hour]
    if later:
        return datetime.combine(now.date(), time(min(later)))
    return datetime.combine(now.date() + timedelta(days=1), time(hs[0]))


def waiting(out):
    """The numbers of the posted items (pending or held) that wait for the next sitting: the ones not urgent (announce_now). R37: the
    items of a sender listed in out/attention.json are not waiting (they are announced at once)."""
    from . import notify   # lazily: notify imports the modules that read the settings this one is part of
    items = notify.Approvals(out).data.get("items", {})
    listed = senders(out)
    return {int(n) for n, it in items.items()
            if it.get("posted") and it.get("status") in ("pending", "held") and not announce_now(it) and _author(it) not in listed}


def unsent(out, now):
    """R28 (sent_watch=1): the numbers of the approved replies that were nudged and whose send is still watched at `now`
    (sent.nudged_open). Not urgent, so they wait with the posted items: the push holds them (push.py), nothing else reads them."""
    from . import notify, sent   # lazily: notify imports the modules that read the settings this one is part of
    if not sent.enabled():
        return set()
    items = notify.Approvals(out).data.get("items", {})
    return {int(n) for n, it in items.items() if sent.nudged_open(it, now)}


def plan(out, now, until=None):
    """What this cycle holds: {"sitting_at": the last sitting at or before now (or None), "hold": the waiting numbers}.
    Called after this cycle has posted what it posts, so the items of this cycle are in it. `until` (R27, control.quiet_until): the end
    of a 休み. Until it comes, no sitting is announced and the waiting numbers are held (the judgment recorded at decision time, not a
    new one); when it has come, interrupt=sittings takes it as the sitting that announces what was held (the sitting it records).
    The hold includes the nudged replies still unsent (R28, unsent): their push waits like the not urgent items, and the same holds apply."""
    now = _local(now)
    if until is not None and now < until:
        return {"sitting_at": None, "hold": waiting(out) | unsent(out, now)}
    at = boundary(now)
    if until is not None and hours() and until.isoformat(timespec="minutes") > at:
        at = until.isoformat(timespec="minutes")
    return {"sitting_at": at, "hold": (waiting(out) | unsent(out, now)) if at else set()}


def status_lines(out, now=None):
    """`schedule status`: the next sitting and how many items wait for it (nothing when the sittings do not apply)."""
    now = datetime.now() if now is None else _local(now)
    nxt = next_sitting(now)
    if nxt is None:
        return []
    from . import notify
    items = notify.Approvals(out).data.get("items", {})
    left = sum(1 for n in waiting(out) if not (items.get(str(n)) or {}).get("toasted"))
    when = nxt.strftime("%H:%M") if nxt.date() == now.date() else "明日 " + nxt.strftime("%H:%M")
    return [f"次のまとめ {when}・待っている告知 {left}"]


# ---- R37: a sender who chases an item that waits for the sitting is announced at once from then on (interrupt=sittings only) ----
#
# The held numbers and their revisions are kept in approvals.json (`sitting_waited`, top level, not in the item: a merge clears the
# item, cli._merge_locked). A later cycle that finds a held item with a higher revision (a follow-up, not a thanks or OK) lists its
# sender in out/attention.json when that follow-up came CHASE_MINUTES or more after the first ready time, or has a chasing word
# (CHASE_WORDS). A listed sender's waiting items are announced at once (waiting), and the morning brief names them (brief_line).
# Two follow-ups in a row, with neither the time nor a word, list nobody. The sender's name stays in out/attention.json and the
# brief: never in the push, in --share, or in daily.log (the counts only). A listing only ever raises an item's urgency.

def _author(it):
    """The sender of a chat item as its record keeps it ("" when there is none)."""
    rec = it.get("record") if isinstance(it.get("record"), dict) else {}
    ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
    return " ".join(str(ev.get("author") or "").split())


def _revision(it):
    """The revision of an approvals item (1 when it has none), as cli._merge_locked counts it."""
    rec = it.get("record") if isinstance(it.get("record"), dict) else {}
    return int(it.get("revision") or rec.get("revision") or 1)


def _first_ready(it):
    """The first ready time of an item (its measurement, or the record's): read only, nothing is filled in."""
    rec = it.get("record") if isinstance(it.get("record"), dict) else {}
    for meas in (it.get("measurement"), rec.get("measurement")):
        if isinstance(meas, dict) and (meas.get("first_ready_at") or meas.get("ready_at")):
            return meas.get("first_ready_at") or meas.get("ready_at")
    return None


def _minutes_since(value, now):
    """Minutes from an ISO time (aware, or naive local) to `now` (naive local); None when the time is missing or not a time."""
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)
    return (_local(now) - when).total_seconds() / 60


def _path(out):
    return Path(out) / "attention.json"


def _rows(out):
    """The listed senders as the file keeps them, in the order they were listed (read only; no setting is read)."""
    data = fsutil.read_json(_path(out), {"senders": []})
    rows = data.get("senders") if isinstance(data, dict) else None
    return [r for r in (rows or []) if isinstance(r, dict) and str(r.get("name") or "")]


def senders(out):
    """R37: the senders whose waiting items are announced at once. Nothing is listed unless interrupt=sittings."""
    if not enabled():
        return set()
    return {str(r["name"]) for r in _rows(out)}


def _chase_rule(it, before, now):
    """Why the follow-ups of a held item since revision `before` are a chase: "words" (a chasing word), "minutes" (CHASE_MINUTES or
    more after its first ready time), or None (an ordinary follow-up, or two in a row with neither)."""
    rec = it.get("record") if isinstance(it.get("record"), dict) else {}
    rows = [r for r in (rec.get("followups") or []) if isinstance(r, dict)]
    new = rows[-(_revision(it) - before):]
    text = " ".join(" ".join(str(r.get("text") or "").split()) for r in new)
    if re.search(CHASE_WORDS, unicodedata.normalize("NFKC", text)):
        return "words"
    minutes = _minutes_since(_first_ready(it), now)
    return "minutes" if minutes is not None and minutes >= CHASE_MINUTES else None


def _list_senders(out, pairs, now):
    """Adds the senders not listed yet (name, why) to out/attention.json; the previous copy stays as .bak until the list changes again.
    Returns how many were added."""
    rows = _rows(out)
    have = {r["name"] for r in rows}
    added = 0
    for name, why in pairs:
        if name in have:
            continue
        rows.append({"name": name, "since": _local(now).isoformat(timespec="minutes"), "why": why})
        have.add(name)
        added += 1
    if added:
        fsutil.write_atomic(_path(out), json.dumps({"senders": rows}, ensure_ascii=False, indent=1))
    return added


def note(out, now, held):
    """R37 (interrupt=sittings only; nothing is read or written otherwise): keeps the held numbers and their revisions for the next
    cycle, and lists the sender of a held item whose revision went up since then when that follow-up was a chase (_chase_rule).
    `held` is this cycle's hold (plan). Returns how many senders were listed now, or None when approvals.json is in use (the next
    cycle tries again with the same marks)."""
    if not enabled():
        return 0
    from . import notify   # lazily: notify imports the modules that read the settings this one is part of
    out = Path(out)
    with fsutil.exclusive(notify.lock_path(out)) as got:
        if not got:
            return None
        ap = notify.Approvals(out)
        items = ap.data.get("items", {})
        mark = ap.data.get("sitting_waited") if isinstance(ap.data.get("sitting_waited"), dict) else {}
        chased = []
        for n, rev in mark.items():
            it = items.get(str(n))
            if not isinstance(it, dict) or not _author(it):
                continue
            try:
                before = int(rev)
            except (TypeError, ValueError):
                continue
            if _revision(it) > before:
                why = _chase_rule(it, before, now)
                if why:
                    chased.append((_author(it), why))
        kept = {}
        for n in held:
            it = items.get(str(n))
            if (isinstance(it, dict) and it.get("posted") and it.get("status") in ("pending", "held") and _author(it)
                    and not it.get("toasted")):   # a notice that already went out is not waiting for anything
                kept[str(n)] = _revision(it)
        added = _list_senders(out, chased, now) if chased else 0
        if kept != mark:
            if kept:
                ap.data["sitting_waited"] = kept
            else:
                ap.data.pop("sitting_waited", None)
            ap.save()
        return added


def brief_line(out, text):
    """R37: the line of the morning brief that says whose items are announced at once. It comes after the writer's key points and is
    never given to the writer. Nothing is added unless interrupt=sittings and a sender is listed."""
    names = [r["name"] for r in _rows(out)] if enabled() else []
    if not names:
        return text
    shown = "・".join(f"{n}さん" for n in names[:3]) + (f" ほか {len(names) - 3} 人" if len(names) > 3 else "")
    return f"{text}\n今後 {shown}はすぐ知らせます（外すには kimeru attention remove）"


def listed(out):
    """`kimeru attention list`: (name, why, since) of each listed sender, in the order they were listed (read only)."""
    return [(r["name"], r.get("why"), r.get("since")) for r in _rows(out)]


def unlist(out, name):
    """`kimeru attention remove NAME`: takes one sender off the list, and its previous copy (.bak) goes too. Returns how many were taken
    off (0: no such name), or None when approvals.json is in use."""
    from . import notify   # lazily: notify imports the modules that read the settings this one is part of
    out = Path(out)
    with fsutil.exclusive(notify.lock_path(out)) as got:
        if not got:
            return None
        rows = _rows(out)
        keep = [r for r in rows if r["name"] != name]
        taken = len(rows) - len(keep)
        if taken:
            path = _path(out)
            if keep:
                fsutil.write_atomic(path, json.dumps({"senders": keep}, ensure_ascii=False, indent=1))
            else:
                path.unlink(missing_ok=True)
            path.with_name(path.name + ".bak").unlink(missing_ok=True)
        return taken
