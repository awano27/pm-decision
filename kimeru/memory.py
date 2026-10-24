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

from . import autorun, config, explain, fsutil, recall

DAYS = 60                       # how far back a decision counts
TAIL_BYTES = 8 * 1024 * 1024    # a log longer than this counts its last bytes only
LINE_MAX = 120                  # one history line
PRECEDENT_MAX = 20              # the precedent on the arrow line, its brackets included
HISTORY_MAX = 3                 # history lines in 詳細 N
FINAL = ("OK", "NG")            # the decisions a precedent may name (a hold or a rewrite is only history)
WRITER_TEXT_MAX = 200           # K02: the approved reply's text a precedent line may carry
SIMILAR_LINE_MAX = 60           # K07: one 似た件 line
SIMILAR_NAME_MAX = 40           # K07: the other person's shown name (recall_names=1)
OTHER_PERSON = "（別の相手）"     # K07: what stands for the name with recall_names=0
SIMILAR_HEAD = "似た件（この相手・チャットの件ではない。話題が近いだけ）:"   # K07: reads on its own under the writer's precedent heading
WRITER_LINES = 2                # R36: the earlier decisions the writer's request gets (writer_precedent=1), at most this many lines


def enabled():
    """memory=1: the posts, 詳細 N and the brief show the earlier decisions. 0 (the default): none of them is read."""
    return config.value("memory") == "1"


def _now(now):
    """The clock of a lookup: the one given (a naive one is local time, as the cycle's clock is: notify._naive_now), else now in UTC."""
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo else now.astimezone()


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


def _tail_rows_and_cut(path, max_bytes=TAIL_BYTES):
    """(rows, cut): the JSON objects of a JSON Lines file, and whether only its last `max_bytes` were read (a line cut at the start is
    dropped). A line that is not JSON is skipped; a missing file gives ([], False)."""
    p = Path(path)
    cut = False
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                cut = True
                f.seek(size - max_bytes)
                f.readline()
            data = f.read()
    except OSError:
        return [], False
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
    return rows, cut


def _tail_rows(path, max_bytes=TAIL_BYTES):
    """The JSON objects of a JSON Lines file: its last `max_bytes` when it is longer (see _tail_rows_and_cut)."""
    return _tail_rows_and_cut(path, max_bytes)[0]


def _size(p):
    try:
        return p.stat().st_size
    except OSError:
        return None


_cache = {}   # at most one entry per kind, {"logs": (key, value), "items": (key, value)}; a new key replaces it


def _logs(out):
    """{"log", "merged", "decisions", "decisions_cut"}: the tail rows of the three logs, read once per state of the files. The key is
    their sizes (a missing file has None); approvals.json is not in it, so a save of approvals.json costs this no re-read. Callers
    only read the result."""
    out = Path(out)
    key = (str(out.resolve()), _size(out / "approvals.log.jsonl"), _size(out / "decisions.jsonl"), _size(out / "merged.jsonl"))
    hit = _cache.get("logs")
    if hit and hit[0] == key:
        return hit[1]
    decisions, cut = _tail_rows_and_cut(out / "decisions.jsonl")
    value = {"log": _tail_rows(out / "approvals.log.jsonl"), "merged": _tail_rows(out / "merged.jsonl"),
             "decisions": decisions, "decisions_cut": cut}
    _cache["logs"] = (key, value)
    return value


_UNREAD = object()


def _items(out):
    """approvals.json's items by number, read once per state of the file. The key is its (size, mtime_ns) because it is rewritten in
    place; a missing file has None. A file that exists but could not be read is not kept, so the next call reads it again. Callers
    only read the result."""
    out = Path(out)
    path = out / "approvals.json"
    try:
        st = path.stat()
        akey = (st.st_size, st.st_mtime_ns)
    except OSError:
        akey = None
    key = (str(out.resolve()), akey)
    hit = _cache.get("items")
    if hit and hit[0] == key:
        return hit[1]
    data = fsutil.read_json(path, _UNREAD)
    items = data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), dict) else {}
    if data is _UNREAD and akey is not None:   # present but unreadable (a sharing violation): read_json gave its default
        return items
    _cache["items"] = (key, items)
    return items


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
    loaded = _logs(out)
    since = _now(now) - timedelta(days=DAYS)
    by_key = {}

    def add(rec, entry):
        for _, key in keys_of(rec):
            by_key.setdefault(key, []).append(entry)

    for r in loaded["log"]:   # the PM's replies (OK, NG, 保留, 修正, 聞き返し), with their times
        word = autorun.REPLY_NAMES.get(r.get("status"))
        t = _when(r.get("at"))
        if not word or t is None or t < since or autorun._key_is_sample(r.get("key")) or autorun._card_key(r.get("key")):
            continue
        item = items.get(str(r.get("id")))
        rec = item.get("record") if isinstance(item, dict) else None
        if isinstance(rec, dict):
            add(rec, {"n": str(r.get("id")), "at": t, "label": word, "who": _who(rec), "auto": False})

    aliases = {}   # a follow-up merged into an item counts once, under the item's key
    for m in loaded["merged"]:
        if m.get("into") and m.get("event_id") is not None:
            aliases[str(m["event_id"])] = str(m["into"])
    seen = set()
    for r in reversed(loaded["decisions"]):   # newest first: the first one of a case is the one kept
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


def _line(e):
    """One line of 「これまでの経緯」 (history and writer_lines_with_text share it, so the two never drift apart)."""
    if e["auto"]:
        text = f"・{_md(e['at'])} {e['label']}"
    else:
        text = f"・{_md(e['at'])} #{e['n']} {e['label']}: {e['who']}"
    return _clip(text, LINE_MAX)


def history(idx, rec, n=None):
    """The lines of 「これまでの経緯」 in 詳細 N: at most HISTORY_MAX, in the order of _earlier. A decision of the PM:
    「・10/3 #12 OK: 佐藤さん: 仕様変更の可否」; an automatic one only its date and label: 「・10/2 自動: 障害対応」."""
    return [_line(e) for _, e in _earlier(idx, rec, n)[:HISTORY_MAX]]


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


def writer_lines(out, rec, n=None, now=None, text=None, writer_name=None):
    """R36 (writer_precedent=1): the first WRITER_LINES lines of 「これまでの経緯」 for the writer's request, from the same lookup and in
    the same order as 詳細 N (history). `rec` is the case's record (event_kind, event_id, event); `n` is the item's own number, whose
    entries are left out. With writer_precedent_text=1 the lines carry the approved reply text (writer_lines_with_text), so the callers
    need no switch of their own. With recall=1 the 似た件 lines of other chats follow (similar_writer_lines; `text` is the whole
    text of the event when the caller has it), and only for a `writer_name` of learn.STYLE_WRITERS: the writers SECURITY.md says
    send the request out; m365, m365-auto and codex never get an item of another chat. [] when the setting is 0 (nothing is read) or there is no state folder. Read only."""
    if out is None or not writer_enabled():
        return []
    if writer_text_enabled():
        lines = writer_lines_with_text(out, rec, n, now=now)
    else:
        lines = history(index(out, _items(out), now=now), rec, n)[:WRITER_LINES]
    from . import learn   # local: learn is loaded after this module
    if writer_name not in learn.STYLE_WRITERS:
        return lines
    return lines + similar_writer_lines(out, rec, n, now=now, text=text)


def writer_text_enabled():
    """writer_precedent_text=1 (K02): effective only with writer_precedent=1; alone it reads nothing."""
    return writer_enabled() and config.value("writer_precedent_text") == "1"


def _approved_reply(items, n):
    """The text of the reply the PM approved for item n (a drafted teams.reply / ado.comment), or None. A text that names an
    Azure subscription path is left out, as the writer's thread is."""
    from . import writer   # lazily: writer imports config and memory's callers
    it = (items or {}).get(str(n))
    if not isinstance(it, dict) or it.get("status") != "approved":
        return None
    rec = it.get("record") if isinstance(it.get("record"), dict) else {}
    for a in rec.get("actions") or []:
        if not isinstance(a, dict) or not a.get("drafted_by") or a.get("type") not in ("teams.reply", "ado.comment"):
            continue
        text = a.get(writer.FIELD[a["type"]])
        if isinstance(text, str) and text.strip() and "/subscriptions/" not in text.lower():
            return text
    return None


def writer_lines_with_text(out, rec, n=None, now=None):
    """K02 (writer_precedent=1 and writer_precedent_text=1): the lines of writer_lines, an OK line followed by 「　返信: <the text the PM
    approved, WRITER_TEXT_MAX characters>」 when that item is still approved and its reply was drafted by the writer. Only the OK line:
    a 修正 line of the same item has no text, or one reply would fill both precedent slots. The text is a model of wording, never a
    source of facts. [] unless both settings are 1 (nothing is read)."""
    if out is None or not writer_text_enabled():
        return []
    items = _items(out)
    idx = index(out, items, now=now)
    lines, seen = [], set()
    for _, e in _earlier(idx, rec, n)[:HISTORY_MAX][:WRITER_LINES]:
        lines.append(_line(e))
        if e["auto"] or e["label"] != autorun.REPLY_NAMES["approved"] or e["n"] in seen:
            continue
        seen.add(e["n"])
        text = _approved_reply(items, e["n"])
        if text:
            lines.append("　返信: " + _clip(text, WRITER_TEXT_MAX))
    return lines


def recall_enabled():
    """recall=1 (K07): 詳細 N (with memory=1) and the writer's precedent (with writer_precedent=1) add the similar items of other chats."""
    return config.value("recall") == "1"


def _recall_min():
    try:
        v = float(config.value("recall_min"))
    except ValueError:
        v = float(config.SETTINGS["recall_min"][1])
    return v if v == v and 0 <= v <= 1 else float(config.SETTINGS["recall_min"][1])


def _recall_max():
    n = config.int_value("recall_max")
    return n if n >= 0 else int(config.SETTINGS["recall_max"][1])   # a negative number is not a count: the default


def _recall_docs(items, loaded):
    """The decided items as search documents [{"n", "at", "label", "rec", "keys", "body", "terms"}], built once per state of approvals.json
    and the logs (the same objects _items and _logs hand out, so no file is read and nothing is written). A pending or held item is
    not a document. The time is the PM's reply in approvals.log.jsonl, else the record's own."""
    hit = _cache.get("recall")
    if hit and hit[0][0] is items and hit[0][1] is loaded:
        return hit[1]
    replied = {}
    for r in loaded["log"]:
        t = _when(r.get("at"))
        if t is not None and r.get("status") in ("approved", "rejected") and (str(r.get("id")) not in replied or t > replied[str(r.get("id"))]):
            replied[str(r.get("id"))] = t
    docs = []
    for m, it in items.items():
        if not isinstance(it, dict) or it.get("status") not in ("approved", "rejected") or autorun._key_is_sample(it.get("key")):
            continue
        rec = it.get("record") if isinstance(it.get("record"), dict) else {}
        t = replied.get(str(m)) or _when(rec.get("at"))
        if t is None:
            continue
        ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
        body = " ".join(str(x) for x in (ev.get("text"), (rec.get("memo") or {}).get("summary") or rec.get("summary"),
                                         _approved_reply(items, m)) if x)
        docs.append({"n": str(m), "at": t, "label": autorun.REPLY_NAMES[it["status"]], "rec": rec,
                     "keys": {key for _, key in keys_of(rec)}, "body": body, "terms": None})
    _cache["recall"] = ((items, loaded), docs)
    return docs


def similar(out, rec, n=None, now=None, text=None):
    """K07: [(doc, shown)] the decided items of other chats and persons whose words overlap the case's text, best first. The query is
    `text` (the event's whole text), else the case's recorded text. A doc is left out when it shares a key with the case (the earlier
    decisions already show it: keys_of), is the case itself, or is older than DAYS. At most recall_max, the Dice score at least
    recall_min, a tie to the newer. `shown`: up to two terms they share. Reads the cached items and logs; writes nothing."""
    if out is None:
        return []
    ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
    query = recall.terms(text or (rec.get("material_event") or {}).get("text") or ev.get("text"))
    limit = _recall_max()
    if not query or limit <= 0:
        return []
    items, loaded = _items(out), _logs(out)
    own = {key for _, key in keys_of(rec)}
    since = _now(now) - timedelta(days=DAYS)
    docs = []
    for d in _recall_docs(items, loaded):
        if d["at"] >= since and d["n"] != str(n) and not (d["keys"] & own):
            if d["terms"] is None:   # only the items within DAYS are cut into terms
                d["terms"] = recall.terms(d["body"])
            docs.append((d["at"], d, d["terms"]))
    return [(d, recall.shared(query, d["terms"])) for _, d in recall.rank(query, docs, _recall_min(), limit)]


def _similar_line(doc, shown):
    """「・10/3 #12 OK: 佐藤さん（似た点: QA環境, リリース）」: the other person's name is （別の相手） unless recall_names=1."""
    rec = doc["rec"]
    if config.value("recall_names") == "1":
        author = (rec.get("event") or {}).get("author")
        name = _clip(f"{author}さん" if author else _who(rec), SIMILAR_NAME_MAX)
    else:
        name = OTHER_PERSON
    why = f"（似た点: {', '.join(shown)}）" if shown else ""
    return _clip(f"・{_md(doc['at'])} #{doc['n']} {doc['label']}: {name}{why}", SIMILAR_LINE_MAX)


def similar_lines(out, rec, n=None, now=None, text=None):
    """The 似た件 lines of 詳細 N, at most recall_max. [] unless recall=1 (nothing is read)."""
    if not recall_enabled():
        return []
    return [_similar_line(d, shown) for d, shown in similar(out, rec, n, now=now, text=text)]


def similar_writer_lines(out, rec, n=None, now=None, text=None):
    """The 似た件 lines the writer's precedent ends with (writer_lines adds them, so only with writer_precedent=1): a marker line, then
    each item's line, followed by 「　返信: …」 with writer_precedent_text=1 when that item's reply was approved. [] unless recall=1."""
    if out is None or not recall_enabled():
        return []
    found = similar(out, rec, n, now=now, text=text)
    if not found:
        return []
    items = _items(out)
    lines = [SIMILAR_HEAD]
    for d, shown in found:
        lines.append(_similar_line(d, shown))
        reply = _approved_reply(items, d["n"]) if config.value("writer_precedent_text") == "1" else None
        if reply:
            lines.append("　返信: " + _clip(reply, WRITER_TEXT_MAX))
    return lines


def _cut_note(out, now=None):
    """The one line `kimeru memory N` adds when decisions.jsonl was read from its last TAIL_BYTES only and the oldest row read is newer
    than DAYS ago: the automatic decisions before that day were not read. "" otherwise (the posts, 詳細 N and the brief never show it)."""
    loaded = _logs(out)
    if not loaded["decisions_cut"]:
        return ""
    oldest = next((t for t in (_when(r.get("at")) for r in loaded["decisions"]) if t is not None), None)
    if oldest is None or oldest <= _now(now) - timedelta(days=DAYS):
        return ""
    return f"（自動決定は {_md(oldest)} 以降の分だけ読めました）"


def show(out, n, now=None):
    """`kimeru memory N` (read only, whatever memory is set to): the earlier decisions of item #N, as 詳細 N shows them."""
    out = Path(out)
    items = _items(out)
    it = items.get(str(n))
    if not isinstance(it, dict):
        return f"#{n} は承認の記録にありません"
    rec = it.get("record") if isinstance(it.get("record"), dict) else {}
    lines = history(index(out, items, now=now), rec, n)
    head = f"[kimeru 経緯 #{n}] {_who(rec)}"
    note = _cut_note(out, now)
    tail = [note] if note else []
    if not lines:
        return "\n".join([head + f"\n同じチャット・相手・作業項目・規則の過去の件はありません（{DAYS} 日）", *tail])
    return "\n".join([head, "これまでの経緯:", *lines, *tail])
