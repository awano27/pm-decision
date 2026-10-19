"""Related items (R19, setting link_related=1, default 0): the same case under another name, and an alert that comes back.

Every judgment that names a case gets a `link` in its record (decisions.jsonl; the judge never sees it, its state is the same
with the setting 0 and 1):
  {"fp": the case's fingerprint or null, "refs": ["ado:<id>", ...], "target": a name of at most 40 characters}
- fp: an alert = a hash of its rule and the last part of its resource (the resource itself is not kept); an ADO item = "ado:<id>";
  a minutes line = a hash of its normalized text (it is linked for 35 days, the same risk line of a weekly meeting); a chat has none.
- refs: the ADO numbers the text names (`#1234`, `ADO 1234`), only those that exist in the last 7 days' records.
- target: an alert's rule, an ADO item's title or a minutes line; "" when it holds an ARM path or a GUID.

What it changes, and only in the texts: the approval post and the automatic notice end their arrow line with `↔` and the earlier
item of the same case (`↔ 同じ件の可能性: 10:02 アラート <rule>（#12）`, or `↔ ADO #4512（#3）` when a chat names that item); an
alert that comes back within 24 hours with the same or a lower severity is a 再発火 notice (`再発火 3 回目（初回 10:02）: ` in front
of its first line); a repeated ado.create gets dup_of (the time of the first one) and stays a dry run. Nothing is merged, dropped,
quieted or moved to another route, and a severe notice is sent as before. Reads only what the records already keep.
R32 (setting merge_recurring=1): `same_recurring` says when a new alert is a recurrence of a waiting item, and notify.fold_recurring
joins it (the same number, revision + 1, posted again). The link then keeps only the fingerprint (attach_case), never the names.
"""
import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, fsutil

DAYS = 7                  # how far back the ADO numbers and the earlier records count
LONG_DAYS = 35            # how far back a minutes line of the same text counts (its fp only: the refs stay at DAYS)
REFIRE_HOURS = 24         # an alert of the same case within this many hours is a refire
TARGET_MAX = 40           # the name kept in a link
REFS_MAX = 5              # ADO numbers kept in one link
TEXT_FIELDS = ("text", "title", "description", "repro_steps", "acceptance_criteria", "item", "meeting", "rule")
NUMBER = re.compile(r"#(\d{1,9})(?!\d)|\bADO\s*(\d{1,9})(?!\d)", re.IGNORECASE)
PLACE = re.compile(r"/subscriptions/|/resourcegroups/|/providers/|[0-9a-f]{8}-[0-9a-f]{4}-", re.IGNORECASE)


def enabled():
    """link_related=1: the records get a link and the posts and notices show the related items. 0 (the default): none of it."""
    return config.value("link_related") == "1"


def fingerprint(ev):
    """The case of an event as a short string, or None (a chat, or an event without enough to tell it apart)."""
    kind = ev.get("kind")
    if kind == "monitor.alert" and ev.get("rule"):
        places = [str(r).rstrip("/") for r in (ev.get("resources") or []) if r]
        last = places[-1].rsplit("/", 1)[-1] if places else ""
        return hashlib.sha1(f"{ev['rule']}|{last}".encode("utf-8")).hexdigest()[:12]
    if kind == "ado.workitem.created" and ev.get("id") not in (None, ""):
        return f"ado:{ev['id']}"
    if kind == "meeting.item" and ev.get("item"):
        norm = " ".join(unicodedata.normalize("NFKC", str(ev["item"])).split())
        return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]
    return None


def target_of(ev):
    """The name of the case kept in a link: at most 40 characters; "" when it holds an ARM path or a GUID (never written)."""
    raw = {"monitor.alert": ev.get("rule"), "ado.workitem.created": ev.get("title"), "meeting.item": ev.get("item")}.get(ev.get("kind"))
    text = " ".join(str(raw or "").split())
    if not text or PLACE.search(text):
        return ""
    return text if len(text) <= TARGET_MAX else text[:TARGET_MAX - 1] + "…"


def refs_of(ev, known):
    """The ADO items the text names, as "ado:<id>": only the numbers in `known` (the ADO numbers of the last 7 days' records);
    the item's own number is left out. At most REFS_MAX, in the order they appear."""
    own = str(ev.get("id")) if ev.get("kind") == "ado.workitem.created" else None
    found = []
    for field in TEXT_FIELDS:
        for m in NUMBER.finditer(str(ev.get(field) or "")):
            number = m.group(1) or m.group(2)
            ref = f"ado:{number}"
            if number == own or number not in known or ref in found:
                continue
            found.append(ref)
    return found[:REFS_MAX]


def _names_a_number(ev):
    """True when a text field of the event holds a number that refs_of could read (the only other way to get a link)."""
    return any(NUMBER.search(str(ev.get(field) or "")) for field in TEXT_FIELDS)


def _rank(value):
    from .cli import _severity_rank   # the ranking the merge of follow-ups uses (Sev0 4 ... Sev3 1)
    return _severity_rank(value)


def _stamp(t, idx):
    """The time of a record in a text: HH:MM on the day the windows are counted from, else M/D HH:MM (a minutes line of an earlier week)."""
    local = t.astimezone()
    if local.date() == idx["now"].astimezone().date():
        return local.strftime("%H:%M")
    return f"{local.month}/{local.day} {local.strftime('%H:%M')}"


def index(out, items, now=None):
    """What the records already keep, read once for the decisions of a run (nothing is written): `rows` = the decisions that carry a link
    of the last 35 days (in the file's order, the oldest first; after 7 days only the minutes lines, and their refs are dropped), `known` =
    the ADO numbers of the last 7 days' records and of the waiting items, `now` = the time the windows are counted from. `items` is
    approvals.json's items by number."""
    from . import memory
    out = Path(out)
    at_now = now or datetime.now(timezone.utc)
    since = at_now - timedelta(days=DAYS)
    long_since = at_now - timedelta(days=LONG_DAYS)
    numbers, known = {}, set()
    for n, it in (items or {}).items():
        if not isinstance(it, dict):
            continue
        rec = it.get("record") if isinstance(it.get("record"), dict) else {}
        numbers[str(it.get("key"))] = str(n)
        t = memory._when(rec.get("at"))
        if str(rec.get("event_kind") or "").startswith("ado.") and rec.get("event_id") is not None and (
                it.get("status") in ("pending", "held") or (t is not None and t >= since)):
            known.add(str(rec["event_id"]))
    rows = []
    for r in memory._tail_rows(out / "decisions.jsonl"):
        t = memory._when(r.get("at"))
        if t is None or t < long_since:
            continue
        recent = t >= since
        kind = str(r.get("event_kind") or "")
        if recent and kind.startswith("ado.") and r.get("event_id") is not None:
            known.add(str(r["event_id"]))
        link = r.get("link") if isinstance(r.get("link"), dict) else None
        if link is None or not (recent or kind == "meeting.item"):   # after 7 days only a minutes line (its fp) counts
            continue
        ev = r.get("event") if isinstance(r.get("event"), dict) else {}
        key = f"{r.get('graph')}:{r.get('event_id')}:{r.get('node')}"
        rows.append({
            "fp": link.get("fp") if isinstance(link.get("fp"), str) else None,
            "refs": [x for x in (link.get("refs") or []) if isinstance(x, str)] if recent else [],
            "target": str(link.get("target") or ""), "at": t, "kind": kind, "event_id": str(r.get("event_id")),
            "graph_key": r.get("graph_key"), "n": numbers.get(key), "severity": _rank(ev.get("severity")),
            "creates": any(isinstance(a, dict) and a.get("type") == "ado.create" for a in (r.get("actions") or [])),
            "bug": any(isinstance(a, dict) and a.get("type") == "ado.create" and a.get("work_item_type") == "Bug"
                       for a in (r.get("actions") or [])),
        })
    return {"rows": rows, "known": known, "now": at_now}   # the file's order: it is only appended to


def _tags(rec):
    link = rec.get("link") if isinstance(rec.get("link"), dict) else {}
    tags = {x for x in (link.get("refs") or []) if isinstance(x, str)}
    if isinstance(link.get("fp"), str):
        tags.add(link["fp"])
    return tags


def _row_tags(row):
    return ({row["fp"]} if row["fp"] else set()) | set(row["refs"])


def _own(row, rec):
    return bool(rec.get("graph_key")) and row.get("graph_key") == rec.get("graph_key")


def _fp_of(rec):
    link = rec.get("link") if isinstance(rec.get("link"), dict) else {}
    return link.get("fp") if isinstance(link.get("fp"), str) else None


def _rank_of(rec):
    ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
    return _rank(ev.get("severity"))


def _before(idx, rec):
    """The time a record counts from: its own decision time (a record that has none counts from `now`)."""
    from . import memory
    return memory._when(rec.get("at")) or idx["now"]


def _earlier(idx, rec, pending=False):
    """The decisions that came before `rec`. The decisions file is only appended to, so a record that is in it counts the rows above
    its own row (two decisions in the same second keep their order); `pending`: the record is not in the file yet (it is being
    judged), so every row came before it; a record that is not found counts the rows decided before its time."""
    rows = idx["rows"]
    if pending:
        return rows
    pos = next((i for i, r in enumerate(rows) if _own(r, rec)), None)
    if pos is not None:
        return rows[:pos]
    before = _before(idx, rec)
    return [r for r in rows if r["at"] < before]


def _same_case_within_a_day(idx, rec, fp, pending=False):
    """The earlier decisions of the same case, within REFIRE_HOURS before `rec`, with the same or a lower severity than `rec` (a higher
    one is an escalation, not a refire). Never the record itself."""
    since = _before(idx, rec) - timedelta(hours=REFIRE_HOURS)
    cur = _rank_of(rec)
    return [r for r in _earlier(idx, rec, pending) if fp and r["fp"] == fp and r["at"] >= since
            and (cur is None or r["severity"] is None or cur <= r["severity"])]


def _label(row):
    if row["kind"].startswith("ado."):
        return f"ADO #{row['event_id']}"
    if row["kind"] == "monitor.alert":
        return f"アラート {row['target']}".rstrip()
    if row["kind"] == "meeting.item":
        return f"議事録 {row['target']}".rstrip()
    return "チャット"


def post_note(idx, rec, n=None):
    """The `↔` text that ends the arrow line of the post of `rec` (item number n): the newest earlier record that shares a tag with it
    (its fingerprint, or an ADO number it names). None when there is none, or when the setting is 0 (idx is None)."""
    if idx is None:
        return None
    mine = _tags(rec)
    best = None
    for r in _earlier(idx, rec):   # in the file's order: the last match is the newest of the earlier ones
        if _own(r, rec) or (n is not None and r["n"] == str(n)):
            continue
        common = mine & _row_tags(r)
        if common:
            best = (r, common)
    if best is None:
        return None
    row, common = best
    number = f"（#{row['n']}）" if row["n"] else ""
    if any(not t.startswith("ado:") for t in common):   # the same case (its fingerprint): with the time it was seen
        return f"↔ 同じ件の可能性: {_stamp(row['at'], idx)} {_label(row)}{number}"
    return f"↔ {_label(row)}{number}"


def notice_note(idx, rec):
    """What the automatic notice of `rec` adds: {"prefix": the 再発火 text in front of its first line's text (an alert only),
    "tail": the `↔` text at the end of its arrow line}. None when the setting is 0 (idx is None): the notice is then as before."""
    if idx is None:
        return None
    if str(rec.get("event_kind") or "") == "monitor.alert":
        earlier = _same_case_within_a_day(idx, rec, _fp_of(rec))
        if earlier:
            first = min(earlier, key=lambda r: r["at"])
            bug = "（Bug は初回で記録済み）" if first.get("bug") else ""   # the first firing already planned the Bug: no second one is needed
            return {"prefix": f"再発火 {len(earlier) + 1} 回目（初回 {_stamp(first['at'], idx)}）{bug}: ", "tail": ""}
    return {"prefix": "", "tail": post_note(idx, rec) or ""}


def attach(res, ev, out, now=None):
    """Adds the `link` of the event to the judgment `res` (its event and graph_key are set already), and dup_of to an ado.create that
    repeats one of the same case within 24 hours (the time of the first). A minutes risk line whose text came before within 35 days
    gets `既出のリスク（N 回目、初回 M/D）: ` in front of its advice (the advice itself stays). Reads the records and writes nothing.
    Returns res."""
    fp = fingerprint(ev)
    if fp is None and not _names_a_number(ev):   # nothing here can link: the records are not read
        return res
    data = fsutil.read_json(Path(out) / "approvals.json", {"items": {}})
    items = data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), dict) else {}
    idx = index(out, items, now)
    refs = refs_of(ev, idx["known"])
    if fp is not None or refs:
        res["link"] = {"fp": fp, "refs": refs, "target": target_of(ev)}
    if fp is not None:
        earlier = [r for r in _same_case_within_a_day(idx, res, fp, pending=True) if r["creates"]]
        if earlier:
            first = min(earlier, key=lambda r: r["at"])
            for a in res.get("actions") or []:
                if isinstance(a, dict) and a.get("type") == "ado.create":
                    a["dup_of"] = _stamp(first["at"], idx)
        _mark_repeated_risk(res, ev, fp, idx)
    return res


RISK_NODES = ("risk_item", "risk_planned")   # the nodes of graphs/meeting_item.json that register a risk line


def _mark_repeated_risk(res, ev, fp, idx):
    """A minutes risk line of the same text (the same fingerprint) that was judged before, within 35 days: its advice starts with
    `既出のリスク（N 回目、初回 M/D）: ` and goes on with the advice it had. Nothing else about the item changes."""
    if ev.get("kind") != "meeting.item" or res.get("node") not in RISK_NODES or not res.get("advice"):
        return
    earlier = [r for r in idx["rows"] if r["fp"] == fp and r["kind"] == "meeting.item"]
    if not earlier:
        return
    first = min(earlier, key=lambda r: r["at"]).get("at").astimezone()
    res["advice"] = f"既出のリスク（{len(earlier) + 1} 回目、初回 {first.month}/{first.day}）: {res['advice']}"


# ---- R32: a recurring alert joins the waiting item (merge_recurring=1; notify.fold_recurring does the merge) ----

SEVERE_RANK = 3   # Sev1 (high) and Sev0 (critical) are never joined: a recurrence of them keeps its own number


def recurring_enabled():
    """merge_recurring=1 (R32): a recurring alert of the same case joins its waiting item. 0 (the default): nothing is merged."""
    return config.value("merge_recurring") == "1"


def attach_case(res, ev):
    """R32 (merge_recurring=1, link_related=0): the case's fingerprint only, so that a later recurrence can be matched. The judge
    never sees it, and nothing else is linked."""
    fp = fingerprint(ev) if ev.get("kind") == "monitor.alert" else None   # only an alert comes back (an ADO item never joins)
    if fp is not None:
        res["link"] = {"fp": fp, "refs": [], "target": ""}


def same_recurring(rec, res):
    """True when `res`, a new alert result, is a recurrence of the item whose record is `rec`: the same graph and node, the same
    fingerprint (rule and resource), no notice raised by `res`, neither severity Sev0/1, and `res` not more severe than `rec`.
    A severity that cannot be read is no match. The item's status is the caller's to check."""
    fp = _fp_of(res)
    if fp is None or res.get("notify") or fp != _fp_of(rec):
        return False
    if res.get("event_kind") != "monitor.alert" or rec.get("event_kind") != "monitor.alert":   # the queue lines of this cycle too
        return False
    if rec.get("graph") != res.get("graph") or rec.get("node") != res.get("node"):
        return False
    old, new = _rank_of(rec), _rank_of(res)
    return old is not None and new is not None and old < SEVERE_RANK and new < SEVERE_RANK and new <= old


def hm_of(at):
    """HH:MM (local time) of an ISO time, as a 再発 followup line shows it; the current time when `at` cannot be read."""
    try:
        return datetime.fromisoformat(str(at)).astimezone().strftime("%H:%M")
    except (TypeError, ValueError):
        return datetime.now().astimezone().strftime("%H:%M")
