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
import re
import unicodedata
from datetime import datetime, time, timedelta

from . import config, control, graph

DEFAULT_WORDS = "至急|本日中|今日中|大至急|緊急|ASAP"   # the default of urgent_words (config.SETTINGS)
CRITICAL = ("critical_incident", "critical_bug", "critical_outage")


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
    """The numbers of the posted items (pending or held) that wait for the next sitting: the ones not urgent (announce_now)."""
    from . import notify   # lazily: notify imports the modules that read the settings this one is part of
    items = notify.Approvals(out).data.get("items", {})
    return {int(n) for n, it in items.items() if it.get("posted") and it.get("status") in ("pending", "held") and not announce_now(it)}


def plan(out, now, until=None):
    """What this cycle holds: {"sitting_at": the last sitting at or before now (or None), "hold": the waiting numbers}.
    Called after this cycle has posted what it posts, so the items of this cycle are in it. `until` (R27, control.quiet_until): the end
    of a 休み. Until it comes, no sitting is announced and the waiting numbers are held (the judgment recorded at decision time, not a
    new one); when it has come, interrupt=sittings takes it as the sitting that announces what was held (the sitting it records)."""
    now = _local(now)
    if until is not None and now < until:
        return {"sitting_at": None, "hold": waiting(out)}
    at = boundary(now)
    if until is not None and hours() and until.isoformat(timespec="minutes") > at:
        at = until.isoformat(timespec="minutes")
    return {"sitting_at": at, "hold": waiting(out) if at else set()}


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
