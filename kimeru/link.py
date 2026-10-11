"""Related items (R19, setting link_related=1, default 0): the same case under another name, and an alert that comes back.

Every judgment that names a case gets a `link` in its record (decisions.jsonl; the judge never sees it, its state is the same
with the setting 0 and 1):
  {"fp": the case's fingerprint or null, "refs": ["ado:<id>", ...], "target": a name of at most 40 characters}
- fp: an alert = a hash of its rule and the last part of its resource (the resource itself is not kept); an ADO item = "ado:<id>";
  a minutes line = a hash of its normalized text; a chat has none.
- refs: the ADO numbers the text names (`#1234`, `ADO 1234`), only those that exist in the last 7 days' records.
- target: an alert's rule, an ADO item's title or a minutes line; "" when it holds an ARM path or a GUID.

What it changes, and only in the texts: the approval post and the automatic notice end their arrow line with `↔` and the earlier
item of the same case (`↔ 同じ件の可能性: 10:02 アラート <rule>（#12）`, or `↔ ADO #4512（#3）` when a chat names that item); an
alert that comes back within 24 hours with the same or a lower severity is a 再発火 notice (`再発火 3 回目（初回 10:02）: ` in front
of its first line); a repeated ado.create gets dup_of (the time of the first one) and stays a dry run. Nothing is merged, dropped,
quieted or moved to another route, and a severe notice is sent as before. Reads only what the records already keep.
"""
import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, fsutil

DAYS = 7                  # how far back the ADO numbers and the earlier records count
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


def _rank(value):
    from .cli import _severity_rank   # the ranking the merge of follow-ups uses (Sev0 4 ... Sev3 1)
    return _severity_rank(value)


def _hm(t):
    return t.astimezone().strftime("%H:%M")


def index(out, items, now=None):
    """What the records already keep, read once for the decisions of a run (nothing is written): `rows` = the decisions of the last
    7 days that carry a link (in the file's order, the oldest first), `known` = the ADO numbers of those records and of the waiting items, `now` = the time the
    windows are counted from. `items` is approvals.json's items by number."""
    from . import memory
    out = Path(out)
    at_now = now or datetime.now(timezone.utc)
    since = at_now - timedelta(days=DAYS)
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
        if t is None or t < since:
            continue
        kind = str(r.get("event_kind") or "")
        if kind.startswith("ado.") and r.get("event_id") is not None:
            known.add(str(r["event_id"]))
        link = r.get("link") if isinstance(r.get("link"), dict) else None
        if link is None:
            continue
        ev = r.get("event") if isinstance(r.get("event"), dict) else {}
        key = f"{r.get('graph')}:{r.get('event_id')}:{r.get('node')}"
        rows.append({
            "fp": link.get("fp") if isinstance(link.get("fp"), str) else None,
            "refs": [x for x in (link.get("refs") or []) if isinstance(x, str)],
            "target": str(link.get("target") or ""), "at": t, "kind": kind, "event_id": str(r.get("event_id")),
            "graph_key": r.get("graph_key"), "n": numbers.get(key), "severity": _rank(ev.get("severity")),
            "creates": any(isinstance(a, dict) and a.get("type") == "ado.create" for a in (r.get("actions") or [])),
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
        return f"↔ 同じ件の可能性: {_hm(row['at'])} {_label(row)}{number}"
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
            return {"prefix": f"再発火 {len(earlier) + 1} 回目（初回 {_hm(first['at'])}）: ", "tail": ""}
    return {"prefix": "", "tail": post_note(idx, rec) or ""}


def attach(res, ev, out, now=None):
    """Adds the `link` of the event to the judgment `res` (its event and graph_key are set already), and dup_of to an ado.create that
    repeats one of the same case within 24 hours (the time of the first). Reads the records and writes nothing. Returns res."""
    data = fsutil.read_json(Path(out) / "approvals.json", {"items": {}})
    items = data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), dict) else {}
    idx = index(out, items, now)
    fp = fingerprint(ev)
    refs = refs_of(ev, idx["known"])
    if fp is not None or refs:
        res["link"] = {"fp": fp, "refs": refs, "target": target_of(ev)}
    if fp is not None:
        earlier = [r for r in _same_case_within_a_day(idx, res, fp, pending=True) if r["creates"]]
        if earlier:
            first = min(earlier, key=lambda r: r["at"])
            for a in res.get("actions") or []:
                if isinstance(a, dict) and a.get("type") == "ado.create":
                    a["dup_of"] = _hm(first["at"])
    return res
