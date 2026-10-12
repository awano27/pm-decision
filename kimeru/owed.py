"""返事の借り帳 (R29, C04c): the replies the PM owes a chat, and how each debt closed (setting sent_watch=1; the default 0 reads and writes
nothing here).

A debt is a terminal judgment (decide or advise) that holds a teams.reply action with a chat id, except the log_fyi and ack_only nodes
(a thanks or an OK needs no reply). Read from approvals.json and the bounded tail of decisions.jsonl, and R14's own time per chat from
pull_state.json (a time, never a text):
  確認待ち          an approval that waits for the PM's OK (pending, held, and the other open statuses)
  承認済み・未送信   approved, and my own message in the chat has not been seen since
  未返信            a judgment with no approval: kimeru does not send the reply, only the PM can
A debt closes when: my own message shows in its chat after it was opened (how own_preview, the time is R14's), the PM rejected the reply
(ng), the PM marked the item 済 (done), or owed_keep_days (default 5) have passed (expired, at that time).
out/owed.jsonl gets one row per closed debt, kept 90 days: {chat_id, k, opened_at, closed_at, how, n}. k is the first 12 hex digits of the
sha1 of the decision's key (an identity, not a name); n counts the teams.reply actions. No text, no name.
The daily cycle records (settle, real=True and send=True only); the brief section and `kimeru owed` only read.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, fsutil, memory

LOG, LOCK = "owed.jsonl", "owed.lock"
KEEP_DAYS = 5               # owed_keep_days: a debt is open this many days after its decision, then it expires
RECORD_DAYS = 90            # out/owed.jsonl keeps a row this many days after it closed
SHOW_MAX = 5                # chats the brief's section names; the rest are counted
NO_REPLY_NODES = ("ack_only", "log_fyi")
TERMINAL = ("decide", "advise")   # the outcomes of graph.TERMINALS
ASK_NODE = "ask_pm"         # a question the rule could not place: it can be chit-chat, so its chats are counted and not named
STATE = {None: "未返信", "approved": "承認済み・未送信"}   # every other open status waits for the PM's OK
OFF_TEXT = "返事の借り帳は設定 sent_watch=1 のときだけ働きます（既定 0）。今は 0 のため、読むことも書くこともしていません。"
HOWS = ("own_preview", "ng", "done", "expired")


def enabled():
    return config.value("sent_watch") == "1"


def keep_days():
    """owed_keep_days: a whole number of days from 1; any other value is the default 5."""
    try:
        n = int(config.value("owed_keep_days"))
    except (TypeError, ValueError):
        return KEEP_DAYS
    return n if n >= 1 else KEEP_DAYS


def _aware(now):
    """`now` as an aware time; a naive one is this PC's local time; None is the present."""
    if now is None:
        return datetime.now().astimezone()
    return now if now.tzinfo else now.astimezone()


def _when(value):
    """An aware time from an ISO text (a naive one is local time); None when it cannot be read."""
    try:
        t = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.astimezone()


def _iso(when):
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _k(key):
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _debt_of(rec):
    """(chat id, number of teams.reply actions) when the judgment is a debt; (None, 0) when it is not."""
    if rec.get("node") in NO_REPLY_NODES or rec.get("outcome") not in TERMINAL:
        return None, 0
    chats = [str(a["chat_id"]) for a in rec.get("actions") or []
             if isinstance(a, dict) and a.get("type") == "teams.reply" and a.get("chat_id")]
    return (chats[0], len(chats)) if chats else (None, 0)


def _debt(key, rec, chat, n, opened, status):
    ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
    return {"k": _k(key), "chat": chat, "n": n, "opened": opened, "status": status, "node": str(rec.get("node") or ""),
            "who": str(ev.get("chat_title") or ev.get("author") or "").strip()[:30],
            "state": STATE.get(status, "確認待ち")}


def _debts(out):
    """Every reply debt: each approval item (whatever its status), and the newest judgment of each event that no approval covers
    (a judgment with no approval: status None). A follow-up merged into an item (merged.jsonl) is covered by that item."""
    ap = fsutil.read_json(Path(out) / "approvals.json", {"items": {}})
    items = ap.get("items") if isinstance(ap, dict) and isinstance(ap.get("items"), dict) else {}
    found, keys = [], set()
    for item in items.values():
        if not isinstance(item, dict) or not isinstance(item.get("record"), dict):
            continue
        rec = item["record"]
        key = item.get("key") or f"{rec.get('graph')}:{rec.get('event_id')}:{rec.get('node')}"
        keys.add(key)
        chat, n = _debt_of(rec)
        opened = _when(rec.get("at"))
        if chat is not None and opened is not None:
            found.append(_debt(key, rec, chat, n, opened, item.get("status")))
    alias = {}
    for m in memory._tail_rows(Path(out) / "merged.jsonl"):
        if m.get("event_id") is not None and isinstance(m.get("into"), str):
            alias[str(m["event_id"])] = m["into"]
    seen = set()
    for rec in reversed(memory._tail_rows(Path(out) / "decisions.jsonl")):
        event = (rec.get("graph"), str(rec.get("event_id")))
        if event in seen:
            continue
        seen.add(event)                                   # the newest judgment of an event is the one that counts
        key = f"{rec.get('graph')}:{rec.get('event_id')}:{rec.get('node')}"
        if key in keys or alias.get(str(rec.get("event_id"))) in keys:
            continue
        chat, n = _debt_of(rec)
        opened = _when(rec.get("at"))
        if chat is not None and opened is not None:
            found.append(_debt(key, rec, chat, n, opened, None))
    return found


def _own_times(out):
    """{chat id: when} of my last own message the list showed (R14: pull_state.json teams.sigs[chat].own). A time, never a text."""
    st = fsutil.read_json(Path(out) / "pull_state.json", {})
    teams = st.get("teams") if isinstance(st, dict) else None
    sigs = teams.get("sigs") if isinstance(teams, dict) else None
    if not isinstance(sigs, dict):
        return {}
    found = {}
    for cid, entry in sigs.items():
        when = _when(entry.get("own")) if isinstance(entry, dict) and entry.get("own") else None
        if when is not None:
            found[str(cid)] = when
    return found


def _outcome(debt, own, now, days):
    """(how, when) that closed the debt, (None, None) while it is open. For a rejection or 済 the time is `now`, and None when the
    decision is older than the keep days (then it is no debt and no closure). A closure is never later than `now`."""
    opened, expiry = debt["opened"], debt["opened"] + timedelta(days=days)
    if debt["status"] in ("rejected", "closed"):
        how = "ng" if debt["status"] == "rejected" else "done"
        return how, (now if expiry > now else None)
    mine = own.get(debt["chat"])
    if mine is not None and opened < mine <= now and mine < expiry:
        return "own_preview", mine
    if expiry <= now:
        return "expired", expiry
    return None, None


def ledger(out, now=None):
    """{"open": the debts open at `now`, oldest first, "closed": the closures at `now` in the last RECORD_DAYS}. Read only."""
    now = _aware(now)
    days, own = keep_days(), _own_times(out)
    opened, closed = [], []
    for debt in _debts(out):
        how, when = _outcome(debt, own, now, days)
        if how is None:
            opened.append(debt)
        elif when is not None and when >= now - timedelta(days=RECORD_DAYS):
            closed.append({"k": debt["k"], "chat": debt["chat"], "n": debt["n"], "opened": debt["opened"], "closed": when, "how": how})
    opened.sort(key=lambda d: d["opened"])
    return {"open": opened, "closed": closed}


def _row(c):
    return {"chat_id": c["chat"], "k": c["k"], "opened_at": _iso(c["opened"]), "closed_at": _iso(c["closed"]), "how": c["how"], "n": c["n"]}


def _lines(path):
    if not path.exists():
        return []
    return [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _kept(line, now):
    """False for a row closed more than RECORD_DAYS before `now`; a damaged line is kept as it is."""
    try:
        when = _when(json.loads(line).get("closed_at"))
    except (ValueError, AttributeError):
        return True
    return when is None or when >= now - timedelta(days=RECORD_DAYS)


def settle(out, now=None):
    """Record the closures not in out/owed.jsonl yet, and drop the rows closed more than RECORD_DAYS ago. Returns how many rows were
    added, or None when another run holds owed.lock (then nothing is written). The daily cycle calls it with real=True and send=True."""
    out = Path(out)
    now = _aware(now)
    with fsutil.exclusive(out / LOCK) as got:
        if not got:
            return None
        path = out / LOG
        lines = _lines(path)
        known = set()
        for line in lines:
            try:
                known.add(json.loads(line)["k"])
            except (ValueError, KeyError, TypeError):
                pass
        new = [json.dumps(_row(c), ensure_ascii=False) for c in ledger(out, now)["closed"] if c["k"] not in known]
        kept = [l for l in lines if _kept(l, now)]
        if not new and len(kept) == len(lines):
            return 0
        fsutil.write_atomic(path, "".join(l + "\n" for l in kept + new))
        return len(new)


def _elapsed(delta):
    minutes = max(0, int(delta.total_seconds()) // 60)
    if minutes < 60:
        return f"{minutes} 分"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 時間"
    days, rest = divmod(hours, 24)
    return f"{days} 日 {rest} 時間" if rest else f"{days} 日"


def brief_section(out, now=None):
    """The brief's 「返事を待っている人」: 「返事を待っている人: N 件」, one line per chat (the longest wait first, SHOW_MAX named),
    then 「ほか N 件」. A chat whose debts are all ask_pm is counted, not named. "" when no debt is open."""
    now = _aware(now)
    opened = ledger(out, now)["open"]
    if not opened:
        return ""
    groups = {}
    for d in opened:
        groups.setdefault(d["chat"], []).append(d)
    ordered = sorted(groups.values(), key=lambda g: g[0]["opened"])
    named = [g for g in ordered if any(d["node"] != ASK_NODE for d in g)][:SHOW_MAX]
    named_ids = {g[0]["chat"] for g in named}
    rest = [g for g in ordered if g[0]["chat"] not in named_ids]
    lines = [f"返事を待っている人: {len(ordered)} 件"]
    for g in named:
        first = g[0]
        who = next((d["who"] for d in g if d["who"]), "（名前なし）")
        lines.append(f"・{who} {_elapsed(now - first['opened'])}（{len(g)} 件・{first['state']}）")
    if rest:
        note = "（ask_pm は雑談も入るため、名前は出しません）" if any(all(d["node"] == ASK_NODE for d in g) for g in rest) else ""
        lines.append(f"ほか {len(rest)} 件{note}")
    return "\n".join(lines)


def _recorded_counts(out, now):
    """How many rows of out/owed.jsonl closed in the last RECORD_DAYS, by how (a damaged line is not counted)."""
    counts = {h: 0 for h in HOWS}
    for line in _lines(Path(out) / LOG):
        try:
            r = json.loads(line)
            when = _when(r.get("closed_at"))
        except (ValueError, AttributeError):
            continue
        if r.get("how") in counts and when is not None and when >= now - timedelta(days=RECORD_DAYS):
            counts[r["how"]] += 1
    return counts


def cli_text(out, now=None):
    """What `kimeru owed` prints: the open debts and the counts of the closed ones. Read only."""
    if not enabled():
        return OFF_TEXT
    now = _aware(now)
    led = ledger(out, now)
    lines = [f"返事の借り: 開いている件 {len(led['open'])} 件（期限 {keep_days()} 日）"]
    for d in led["open"]:
        lines.append(f"・{d['opened'].astimezone():%m/%d %H:%M} に開いた {d['who'] or '（名前なし）'}: {d['state']}（{d['n']} 件の返信、"
                     f"{_elapsed(now - d['opened'])} 前）")
    counts = _recorded_counts(out, now)
    lines.append(f"閉じた件（{LOG}、{RECORD_DAYS} 日）: " + " / ".join(f"{h} {counts[h]} 件" for h in HOWS))
    return "\n".join(lines)
