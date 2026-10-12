"""前例 (R16): what kimeru decided before for the same chat, person, work item or rule, shown beside an item (setting memory=1).

Read only. The lookup works from records that already exist: approvals.json (its items are never removed), approvals.log.jsonl (the
PM's replies and when they were written), the automatic decisions of decisions.jsonl (the last 8 MB, 60 days) and merged.jsonl (a
follow-up merged into an item). Nothing is written, no model and no Teams is asked, and nothing here reaches a judgment: the judge's
state, the writer's material and the brief's subject are the same with memory=0 and 1.

Where it shows: the approval post adds the precedent (the last OK or NG of the same chat, else of the same person, else of the same
work item or rule) to the end of its arrow line; 詳細 N adds 「これまでの経緯」 (at most three lines, the same order); the morning brief
adds （同じ相手 N 件目） to a waiting row; `kimeru memory N` prints the history in the terminal.

The keys are only held in memory, never written: chat:<chat_id>, person:<author>, ado:<id>, alert:<rule> (the rule's name: the resource
is not recorded), line:<hash of the normalized minutes line>. An automatic decision shows its date and its label only.
"""
import hashlib
import json
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import autorun, config, explain, fsutil

DAYS = 60                       # how far back a decision counts
TAIL_BYTES = 8 * 1024 * 1024    # a log longer than this counts its last bytes only
LINE_MAX = 120                  # one history line
PRECEDENT_MAX = 20              # the precedent on the arrow line, its brackets included
HISTORY_MAX = 3                 # history lines in 詳細 N
FINAL = ("OK", "NG")            # the decisions a precedent may name (a hold or a rewrite is only history)
WRITER_LINES = 2                # R36: the earlier decisions the writer's request gets (writer_precedent=1), at most this many lines


def enabled():
    """memory=1: the posts, 詳細 N and the brief show the earlier decisions. 0 (the default): none of them is read."""
    return config.value("memory") == "1"


def _now(now):
    return now or datetime.now(timezone.utc)


def _when(value):
    """An aware time from a record's ISO text (a naive one is taken as local time); None when it cannot be read."""
    try:
        t = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.astimezone()


def _md(t):
    local = t.astimezone()
    return f"{local.month}/{local.day}"


def _clip(text, n):
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:max(1, n - 1)] + "…"


def _who(rec):
    from . import notify   # lazily: notify imports this module
    return notify._who(rec, 40)


def _tail_rows(path, max_bytes=TAIL_BYTES):
    """The JSON objects of a JSON Lines file: its last `max_bytes` when it is longer (a line cut at the start is dropped).
    A line that is not JSON is skipped; a missing file gives nothing."""
    p = Path(path)
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            data = f.read()
    except OSError:
        return []
    rows = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _line_sig(text):
    """The key of a minutes line: the text with its width and its spaces normalized, hashed (never written)."""
    norm = " ".join(unicodedata.normalize("NFKC", str(text or "")).split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def keys_of(rec):
    """The keys of a record as [(class, key)], the most specific class first: chat, then person, then work item / rule / minutes line."""
    kind = str(rec.get("event_kind") or "")
    ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
    keys = []
    if ev.get("chat_id"):
        keys.append(("chat", f"chat:{ev['chat_id']}"))
    if ev.get("author"):
        keys.append(("person", f"person:{ev['author']}"))
    if kind.startswith("ado.") and rec.get("event_id"):
        keys.append(("object", f"ado:{rec['event_id']}"))
    if kind == "monitor.alert" and ev.get("rule"):
        keys.append(("object", f"alert:{ev['rule']}"))
    if kind == "meeting.item" and ev.get("item"):
        keys.append(("object", f"line:{_line_sig(ev['item'])}"))
    return keys


def _person(rec):
    return next((key for cls, key in keys_of(rec) if cls == "person"), None)


def index(out, items, now=None):
    """The earlier decisions by key: {key: [entry, ...]}, the newest first. An entry is {"n": the item's number (None for an automatic
    decision), "at": the local time, "label": OK / NG / 保留 / 修正 / 聞き返し, or 自動: <the playbook's label or 記録のみ>,
    "who": the item's short name (40 characters), "auto": bool}. `items`: approvals.json's items by number. A decision older than
    DAYS is left out. Nothing is written."""
    out = Path(out)
    since = _now(now) - timedelta(days=DAYS)
    by_key = {}

    def add(rec, entry):
        for _, key in keys_of(rec):
            by_key.setdefault(key, []).append(entry)

    for r in _tail_rows(out / "approvals.log.jsonl"):   # the PM's replies (OK, NG, 保留, 修正, 聞き返し), with their times
        word = autorun.REPLY_NAMES.get(r.get("status"))
        t = _when(r.get("at"))
        if not word or t is None or t < since or autorun._key_is_sample(r.get("key")) or autorun._card_key(r.get("key")):
            continue
        item = items.get(str(r.get("id")))
        rec = item.get("record") if isinstance(item, dict) else None
        if isinstance(rec, dict):
            add(rec, {"n": str(r.get("id")), "at": t, "label": word, "who": _who(rec), "auto": False})

    aliases = {}   # a follow-up merged into an item counts once, under the item's key
    for m in _tail_rows(out / "merged.jsonl"):
        if m.get("into") and m.get("event_id") is not None:
            aliases[str(m["event_id"])] = str(m["into"])
    seen = set()
    for r in reversed(_tail_rows(out / "decisions.jsonl")):   # newest first: the first one of a case is the one kept
        t = _when(r.get("at"))
        if r.get("needs_human") or t is None or t < since:
            continue
        stable = aliases.get(str(r.get("event_id"))) or f"{r.get('graph')}:{r.get('event_id')}:{r.get('node')}"
        if stable in seen:
            continue
        seen.add(stable)
        plan = r.get("plan") if isinstance(r.get("plan"), dict) else {}
        playbook = explain.PLAYBOOK_LABEL.get(plan.get("playbook")) if isinstance(plan.get("playbook"), str) else None
        add(r, {"n": None, "at": t, "label": f"自動: {playbook or '記録のみ'}", "who": "", "auto": True})

    for entries in by_key.values():
        entries.sort(key=lambda e: e["at"], reverse=True)
    return by_key


def _earlier(idx, rec, n):
    """The earlier entries about this record, as [(class, entry)]: the chat's first, then the person's, then the work item's or rule's;
    each entry once; the item's own entries (number n) left out."""
    out, seen = [], set()
    for cls, key in keys_of(rec):
        for e in (idx or {}).get(key, []):
            if id(e) in seen or (e["n"] is not None and e["n"] == str(n)):
                continue
            seen.add(id(e))
            out.append((cls, e))
    return out


def precedent(idx, rec, n=None):
    """The end of the arrow line of the approval post: （前例: 10/3 #12 OK）, for the first earlier OK or NG of the chat, else of the
    person, else of the work item or rule (the newest of each). None when there is none, or when memory is off (idx is None)."""
    for _, e in _earlier(idx, rec, n):
        if not e["auto"] and e["label"] in FINAL:
            return _clip(f"（前例: {_md(e['at'])} #{e['n']} {e['label']}）", PRECEDENT_MAX)
    return None


def history(idx, rec, n=None):
    """The lines of 「これまでの経緯」 in 詳細 N: at most HISTORY_MAX, in the order of _earlier. A decision of the PM:
    「・10/3 #12 OK: 佐藤さん: 仕様変更の可否」; an automatic one only its date and label: 「・10/2 自動: 障害対応」."""
    lines = []
    for _, e in _earlier(idx, rec, n)[:HISTORY_MAX]:
        if e["auto"]:
            text = f"・{_md(e['at'])} {e['label']}"
        else:
            text = f"・{_md(e['at'])} #{e['n']} {e['label']}: {e['who']}"
        lines.append(_clip(text, LINE_MAX))
    return lines


def same_person_count(items, n, rec, now=None):
    """How many items the same person has (approvals.json, the decision time within DAYS; this item always counts). 0 when the record
    names no person."""
    key = _person(rec)
    if key is None:
        return 0
    since = _now(now) - timedelta(days=DAYS)
    count = 0
    for m, it in (items or {}).items():
        if not isinstance(it, dict) or (str(m) != str(n) and autorun._key_is_sample(it.get("key"))):
            continue
        r = it.get("record") if isinstance(it.get("record"), dict) else {}
        if _person(r) != key:
            continue
        t = _when(r.get("at"))
        if str(m) == str(n) or (t is not None and t >= since):
            count += 1
    return count


def same_person_note(items, n, rec, now=None):
    """The brief's waiting row, at its end: （同じ相手 N 件目） when memory=1 and the person has 2 or more items in DAYS (this one
    included). "" otherwise, so with memory=0 the row is the same as before. The row's subject is not changed."""
    if not enabled():
        return ""
    count = same_person_count(items, n, rec, now)
    return f"（同じ相手 {count} 件目）" if count >= 2 else ""


def writer_enabled():
    """writer_precedent=1 (R36): the writer's request gets the newest earlier decisions of the case. 0 (the default): nothing is read."""
    return config.value("writer_precedent") == "1"


def writer_lines(out, rec, n=None, now=None):
    """R36 (writer_precedent=1): the first WRITER_LINES lines of 「これまでの経緯」 for the writer's request, from the same lookup and in
    the same order as 詳細 N (history). `rec` is the case's record (event_kind, event_id, event); `n` is the item's own number, whose
    entries are left out. [] when the setting is 0 (nothing is read) or there is no state folder. Read only."""
    if out is None or not writer_enabled():
        return []
    data = fsutil.read_json(Path(out) / "approvals.json", {"items": {}})
    items = data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), dict) else {}
    return history(index(out, items, now=now), rec, n)[:WRITER_LINES]


def show(out, n):
    """`kimeru memory N` (read only, whatever memory is set to): the earlier decisions of item #N, as 詳細 N shows them."""
    out = Path(out)
    data = fsutil.read_json(out / "approvals.json", {"items": {}})
    items = data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), dict) else {}
    it = items.get(str(n))
    if not isinstance(it, dict):
        return f"#{n} は承認の記録にありません"
    rec = it.get("record") if isinstance(it.get("record"), dict) else {}
    lines = history(index(out, items), rec, n)
    head = f"[kimeru 経緯 #{n}] {_who(rec)}"
    if not lines:
        return head + f"\n同じチャット・相手・作業項目・規則の過去の件はありません（{DAYS} 日）"
    return "\n".join([head, "これまでの経緯:", *lines])
