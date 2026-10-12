"""Morning brief: rank open work and format one post for the Teams self chat.

Candidates come from local files only:
  - approvals still pending/held (out/approvals.json) and queue items not yet in approvals
  - plan steps due today / this week from recent decisions (out/decisions.jsonl)
Ordering: due level first (already judged by Jev inside the plan node), then the
harm of a one-day delay, scored by Jev in ONE call (one score question per item).
The due label is NOT shown to Jev for the harm question, so it cannot anchor on it.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import autorun, config, fsutil, hold, learn, memory
from .backends import judge_name

HARM = ["No real harm", "Minor inconvenience for the team",
        "Significant harm to the schedule or to stakeholders", "Severe harm to customers or the business"]
MAX_ITEMS = 30  # keep a single Jev call small
TOP = 3         # the items the post names; the rest are counted
HISTORY_KEEP_DAYS = 90   # out/brief_history.jsonl keeps a row this many days back (the next brief compares with it)
FOLD_DAYS = 4            # brief_delta=1: a numbered row that stays the same (sig and rank band) this many days in a row is folded
RANK_BAND = TOP          # rank bands are this wide (1-3, 4-6, ...): a row that stays in its band has not moved
APPROVAL_PREFIX = "approval:"                                  # the key of a numbered approval row (a number the PM answers)
REPLY_STATUSES = ("approved", "rejected", "held", "redrafted")   # the replies the window line counts (autorun.REPLY_NAMES)
STUB_HINTS = {"3": ["障害", "incident", "顧客", "customer"], "2": ["スケジュール", "リリース", "判断"], "1": ["確認待ち"]}


def _rows(p):
    p = Path(p)
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def due_date(at, due_level):
    """Absolute local due date of a plan step decided at `at` (ISO, UTC): 今日 -> that day,
    今週 -> Friday of that week (the same day if decided on a weekend), later -> None."""
    d = datetime.fromisoformat(at).astimezone().date()
    if due_level == 0:
        return d
    if due_level == 1:
        return d + timedelta(days=max(0, 4 - d.weekday()))
    return None


def due_label(due, today):
    if due < today:
        return f"{due.month}/{due.day} 期限・超過"
    if due == today:
        return "今日"
    return f"{due.month}/{due.day} まで"


def collect(out, now=None, days=2):
    """Return candidate items: [{key, kind, text, subject, due_level}] (deduped, newest first, capped).
    `text` is for humans; `subject` (no due label) is what Jev sees."""
    out = Path(out)
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    items, seen = [], set()

    def add(key, kind, text, subject, due_level, rec=None):
        # `notify` and `severe` are copied from the decision record (what the history keeps); neither reaches Jev
        if key not in seen:
            seen.add(key)
            items.append({"key": key, "kind": kind, "text": text, "subject": subject, "due_level": due_level,
                          "notify": bool((rec or {}).get("notify")), "severe": bool((rec or {}).get("severe_guard")),
                          "draft": (rec or {}).get("held_reason") == "draft"})   # R12: waits only for a writer's draft

    ap = json.loads((out / "approvals.json").read_text(encoding="utf-8")) if (out / "approvals.json").exists() else {"items": {}}
    queued = set()
    for n, it in learn.cases(ap["items"]).items():   # R21: a learn card is no case
        queued.add(it["key"])
        if it["status"] in ("pending", "held"):
            what = it['record'].get('advice') or it['record'].get('graph')
            head = hold.brief_label(n, it, now) or f"確認待ち #{n}"   # R09: 保留 #N（10/15 に再表示）, 長く保留 (hold_stale_days)
            same = memory.same_person_note(ap["items"], n, it["record"], now)   # R16 (memory=1): 同じ相手 N 件目; "" otherwise
            add(f"approval:{n}", "approval", f"{head}: {what}{same}", f"Waiting for the PM's approval: {what}", 0, it["record"])
    for rec in reversed(_rows(out / "queue.jsonl")):
        key = f"{rec.get('graph')}:{rec.get('event_id')}:{rec.get('node')}"
        if key not in queued and not learn.is_card(rec):
            what = rec.get('advice') or rec.get('graph')
            add(f"queue:{key}", "approval", f"確認待ち（未投稿）: {what}", f"Waiting for the PM's approval: {what}", 0, rec)
    decisions = _rows(out / "decisions.jsonl")
    # A decision row is only a proposal snapshot. For a case with a current
    # approval record, that record's status, revision and plan are authoritative.
    # Follow-up merges keep the original item key while replacing its record;
    # merged.jsonl connects the follow-up event id back to that stable key.
    approvals_by_key = {}
    for n, item in ap.get("items", {}).items():
        record = item.get("record") or {}
        approvals_by_key[item.get("key")] = (n, item)
        approvals_by_key[f"{record.get('graph')}:{record.get('event_id')}:{record.get('node')}"] = (n, item)
    merged_aliases = {}
    for merge in _rows(out / "merged.jsonl"):
        stable = merge.get("into")
        current = approvals_by_key.get(stable) if stable else None
        record = (current[1].get("record") or {}) if current else {}
        if stable and record.get("graph"):
            merged_aliases[(record["graph"], str(merge.get("event_id")))] = stable

    def case_for(rec):
        key = f"{rec.get('graph')}:{rec.get('event_id')}:{rec.get('node')}"
        found = approvals_by_key.get(key)
        if found:
            return found
        stable = merged_aliases.get((rec.get("graph"), str(rec.get("event_id"))))
        return approvals_by_key.get(stable) if stable else None

    def add_plan(rec, source_key):
        p = rec.get("plan")
        at = rec.get("at")
        today = now.astimezone().date()
        for s in (p or {}).get("steps", []):
            if s.get("due_level", 2) <= 1:
                # "今日" was relative to the decision; re-anchor it so yesterday's "today" shows as overdue.
                due = due_date(at, s["due_level"]) if at else None
                label, level = (due_label(due, today), 0 if due <= today else 1) if due else (s["due"], s["due_level"])
                add(f"step:{source_key}:{p['playbook']}:{s['id']}", "step",
                    f"{p['title']}: {s['title']}（{label}）", f"{p['title']}: {s['title']}", level, rec)

    for _, item in ap.get("items", {}).items():
        record = item.get("record") or {}
        key = item.get("key") or f"{record.get('graph')}:{record.get('event_id')}:{record.get('node')}"
        progress = item.get("work") if isinstance(item.get("work"), dict) else {}
        if item.get("status") == "approved" and progress.get("state") != "done":
            add_plan(record, key)

    for rec in reversed(decisions):
        linked = case_for(rec)
        if linked:
            # This snapshot is either superseded, or represented by the current
            # approved record above. Pending/held cases already appear as a
            # confirmation candidate; rejected and completed cases have no steps.
            continue
        at = rec.get("at")
        if at and datetime.fromisoformat(at) < since:
            continue
        add_plan(rec, rec.get("event_id"))
    return items[:MAX_ITEMS]


def rank(items, backend, top=None, reuse=None):
    """Every item, best first, with its harm (one score per item, one call) and its `rank` (1 = first). `top` keeps the first N.
    reuse (R15, brief_delta=1): {sig: harm} from the previous brief made by the same judge. An item whose sig is there keeps that
    harm and is not asked again; with nothing new to ask, the backend is not called."""
    if not items:
        return []
    reuse = reuse or {}
    harm = {i: reuse[_sig(it["subject"])] for i, it in enumerate(items) if _sig(it["subject"]) in reuse}
    fresh = [i for i in range(len(items)) if i not in harm]
    if fresh:
        state = {"items": [items[i]["subject"] for i in fresh]}
        qs = {f"h{j}": {"type": "score", "criteria": HARM, "hints": STUB_HINTS,
                        "instructions": f"How much harm would a one-day delay of item `items[{j}]` cause?"}
              for j in range(len(fresh))}
        ans = backend.ask(state, qs)
        for j, i in enumerate(fresh):
            harm[i] = ans[f"h{j}"]["score"]
    keyed = [((-it["due_level"], harm[i], -i), it) for i, it in enumerate(items)]  # tie -> newer first
    keyed.sort(key=lambda t: t[0], reverse=True)
    ranked = [{**it, "harm": round(k[1], 2), "rank": n} for n, (k, it) in enumerate(keyed, 1)]
    return ranked if top is None else ranked[:top]


def desk_order(items):
    """brief_style=desk (R15): a fixed order with no judge call. The severe and the notify rows come first, then the overdue and
    today's, then this week's, then the rest; each group keeps the order the items were collected in. harm is None (not judged)."""
    def group(it):
        if it["severe"] or it["notify"]:
            return 0
        return 1 if it["due_level"] == 0 else 2 if it["due_level"] == 1 else 3
    return [{**it, "harm": None, "rank": n} for n, it in enumerate(sorted(items, key=group), 1)]


def is_desk():
    """brief_style=desk: the fixed order, and the brief is not summarised by the writer (daily.py)."""
    return config.value("brief_style") == "desk"


def named(ranked, top=TOP):
    """The items the post names: the first `top` that are not folded (brief_delta=1); all of them are kept in the history."""
    return [i for i in ranked if not i.get("folded")][:top]


def format_post(ranked, total, pending, date=None, empty_message="対応が必要な項目はありません", drafts=None, unchanged=None):
    """drafts (R12, post_kind_mark=1): how many of the `pending` waiting items only need their draft checked; None (the default) keeps
    the one count. unchanged (R15, brief_delta=1): the numbers of the folded rows, named by number; None or empty adds no line."""
    date = date or datetime.now().strftime("%Y-%m-%d")
    lines = [f"[kimeru brief {date}] 今日の進め方"]
    if not ranked:
        lines.append(empty_message)
    for i, it in enumerate(ranked, 1):
        lines.append(f"{i}. {it['text']}")
    if total > len(ranked):
        lines.append(f"ほか {total - len(ranked)} 件")
    if unchanged:
        lines.append(f"変わらず {len(unchanged)} 件（{', '.join('#' + n for n in unchanged)}）")
    if pending and drafts is None:
        lines.append(f"確認待ち {pending} 件（OK 番号 / NG 番号 / 詳細 番号 で返信）")
    elif pending:
        lines.append(f"判断が必要 {pending - drafts} / 下書きの確認 {drafts}（OK 番号 / NG 番号 / 詳細 番号 で返信）")
    return "\n".join(lines)


def _opened_rows(out, now, days):
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    for rec in _rows(Path(out) / "decisions.jsonl"):
        at = rec.get("at")
        rf = rec.get("read_full") or {}
        if at and datetime.fromisoformat(at) >= since and ("returned" in rf or rf.get("state") == "full"):   # a chat was opened
            yield rec, rf


def quiet_reads(out, now=None, days=1):
    """Chats kimeru opened that turned out to need no action (read in full, or opened and not read). Opening a chat marks it
    as read in Teams, so the PM would otherwise lose the unread mark and never see them: the brief lists them by chat name."""
    names = []
    for rec, rf in _opened_rows(out, now, days):
        if not rec.get("needs_human") and not rec.get("notify"):
            names.append(((rec.get("event") or {}).get("chat_title") or (rec.get("event") or {}).get("author") or "（名前なし）")[:30])
    return names


def not_put_back(out, now=None, days=1):
    """How many chats kimeru opened could not be put back (whatever was decided): the person may find another chat open."""
    return sum(1 for _, rf in _opened_rows(out, now, days) if rf.get("returned") is False)


ADO_LIST_MAX = 5   # work items named in the brief; the rest are counted
MAX_WINDOW_DAYS = 7   # "since the last brief" never reaches back further than this (a brief that was off for weeks)


def window_start(now=None, days=1, since=None):
    """(start, known) of the window the automatic sections count: `since` (the last brief's time, from daily_state) when it is
    known, else `days` days back. A `since` in the future or unreadable is not trusted; one older than a week is cut to a week."""
    now = now or datetime.now(timezone.utc)
    if since is not None:
        try:
            if isinstance(since, str):
                since = datetime.fromisoformat(since)
            if since.tzinfo is None:
                since = since.astimezone()
            if since <= now:
                return max(since, now - timedelta(days=MAX_WINDOW_DAYS)), True
        except (TypeError, ValueError):
            pass
    return now - timedelta(days=days), False


def _window_name(known, days):
    return "前回のまとめ以降" if known else f"直近 {days * 24} 時間"


ADO_NODE_LABEL = {"record_only": "記録のみ"}   # Epic / Feature: recorded, no priority proposed


def _ado_outcome(rec):
    """"P2" for a decision that set priority 2, "記録のみ" for an Epic / Feature, else the node's name."""
    for a in rec.get("actions") or []:
        n = (a.get("fields") or {}).get("Microsoft.VSTS.Common.Priority")
        if n:
            return f"P{n}"
    return ADO_NODE_LABEL.get(rec.get("node"), str(rec.get("node") or "?"))


def ado_auto(out, now=None, days=1, since=None):
    """Work items the graph decided by itself in the last `days` day(s), with nothing for the PM to answer (no confirmation, no
    notice): [{id, type, title, outcome}], one per work item (the latest judgment), newest first. A decision that waits in a
    numbered post, or was notified, is shown there and not here."""
    since, _ = window_start(now, days, since)
    seen, found = set(), []
    for rec in reversed(_rows(Path(out) / "decisions.jsonl")):
        if rec.get("event_kind") != "ado.workitem.created" or rec.get("outcome") != "decide":
            continue
        if rec.get("needs_human") or rec.get("notify") or not rec.get("at") or datetime.fromisoformat(rec["at"]) < since:
            continue
        if rec.get("event_id") in seen:
            continue
        seen.add(rec.get("event_id"))
        if rec.get("via_unsure"):   # listed under "迷って自動で決めた件" instead
            continue
        ev = rec.get("event") or {}
        found.append({"id": rec.get("event_id"), "type": ev.get("type"), "title": ev.get("title"), "outcome": _ado_outcome(rec)})
    return found


def ado_section(out, now=None, days=1, since=None):
    """The brief's lines for `ado_auto`: counts per outcome, a few "#id [type] title → P2" lines, then "ほか N 件". "" when none."""
    rows = ado_auto(out, now=now, days=days, since=since)
    if not rows:
        return ""
    from . import graph
    counts = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    lines = [f"ADO の自動判断（{_window_name(window_start(now, days, since)[1], days)}）: {len(rows)} 件（" + " / ".join(f"{k} {v} 件" for k, v in sorted(counts.items())) + "）"]
    for r in rows[:ADO_LIST_MAX]:
        lines.append(f"・#{r['id']}" + (f" [{r['type']}]" if r.get("type") else "")
                     + (" " + graph.one_line(str(r["title"])) if r.get("title") else "") + f" → {r['outcome']}")
    if len(rows) > ADO_LIST_MAX:
        lines.append(f"ほか {len(rows) - ADO_LIST_MAX} 件")
    return "\n".join(lines)


TEAMS_OUTCOME = {"log_fyi": "返信不要", "reply_status": "状況の確認（返信は送っていません）"}


def teams_section(out, now=None, days=1, since=None):
    """Teams chats the graph settled by itself in the last `days` day(s): a thanks or an OK recorded as FYI, and a status question
    the rule closed with no reply sent. One line with the counts and up to five chat names. Nothing went to the PM for these,
    so this is where the PM sees that they were not lost. "" when none."""
    since, known = window_start(now, days, since)
    seen, counts, names = set(), {}, []
    for rec in reversed(_rows(Path(out) / "decisions.jsonl")):
        if rec.get("event_kind") != "teams.chat" or rec.get("outcome") != "decide" or rec.get("node") not in TEAMS_OUTCOME:
            continue
        if rec.get("needs_human") or rec.get("notify") or not rec.get("at") or datetime.fromisoformat(rec["at"]) < since:
            continue
        if rec.get("event_id") in seen:
            continue
        seen.add(rec.get("event_id"))
        label = TEAMS_OUTCOME[rec["node"]]
        counts[label] = counts.get(label, 0) + 1
        ev = rec.get("event") or {}
        name = str(ev.get("chat_title") or ev.get("author") or "").strip()[:30]
        if name and name not in names:
            names.append(name)
    if not counts:
        return ""
    total = sum(counts.values())
    return (f"Teams の自動判断（{_window_name(known, days)}）: {total} 件（" + " / ".join(f"{k} {v} 件" for k, v in sorted(counts.items())) + "）"
            + (f": {'、'.join(names[:ADO_LIST_MAX])}{' ほか' if len(names) > ADO_LIST_MAX else ''}" if names else ""))


UNSURE_LIST_MAX = 5
UNSURE_KIND = {"teams.chat": "Teams", "monitor.alert": "アラート", "ado.workitem.created": "ADO", "meeting.item": "議事録"}
UNSURE_NODE = {"task_internal": "アラートの確認タスクにした", "ask_owner": "担当・期限を確認する扱いにした",
               "risk_item": "リスクとして記録した", "record_only": "記録のみにした"}


def unsure_auto(out, now=None, days=1, since=None):
    """Results that were reached through an answer the model was unsure of and that asked nobody (no confirmation, no notice):
    [{id, kind, line}], one per event (the latest judgment), newest first. They are decided, but the PM may want to check them."""
    from . import graph
    since, _ = window_start(now, days, since)
    seen, found = set(), []
    for rec in reversed(_rows(Path(out) / "decisions.jsonl")):
        if not rec.get("via_unsure") or rec.get("outcome") != "decide" or rec.get("needs_human") or rec.get("notify"):
            continue
        if not rec.get("at") or datetime.fromisoformat(rec["at"]) < since:
            continue
        key = (rec.get("graph"), rec.get("event_id"))
        if key in seen:
            continue
        seen.add(key)
        kind = rec.get("event_kind")
        what = UNSURE_NODE.get(rec.get("node")) or (f"{_ado_outcome(rec)} にした" if kind == "ado.workitem.created" else "")
        found.append({"id": rec.get("event_id"), "kind": kind,
                      "line": f"・{UNSURE_KIND.get(kind, '')} {graph.one_line(str(rec.get('summary') or rec.get('event_id')), 70)}".replace("・ ", "・")
                              + (f" → {what}" if what else "")})
    return found


def unsure_section(out, now=None, days=1, since=None):
    """"迷って自動で決めた件（確かめてください）" with up to five lines and "ほか N 件". "" when none."""
    rows = unsure_auto(out, now=now, days=days, since=since)
    if not rows:
        return ""
    lines = ["迷って自動で決めた件（確かめてください）"] + [r["line"] for r in rows[:UNSURE_LIST_MAX]]
    if len(rows) > UNSURE_LIST_MAX:
        lines.append(f"ほか {len(rows) - UNSURE_LIST_MAX} 件")
    return "\n".join(lines)


# --- brief_delta=1 (R15): what changed since the last brief. Read only: the history rows (brief_history.jsonl), the reply log
# (approvals.log.jsonl) and the approval statuses (approvals.json). Nothing is written here.

def _sig(subject):
    """The first 12 hex digits of the sha1 of a subject: the identity of a row in brief_history (no text is kept)."""
    return hashlib.sha1(subject.encode("utf-8")).hexdigest()[:12]


def _band(rank):
    """The rank band of a rank: 0 for 1-3, 1 for 4-6, ... (None when the rank is not a number)."""
    return (rank - 1) // RANK_BAND if isinstance(rank, int) and rank >= 1 else None


def _day(date, now):
    """The local day of the brief (`date` as given, else the day of `now`)."""
    return date or (now or datetime.now(timezone.utc)).astimezone().strftime("%Y-%m-%d")


def _previous(out, today):
    """The brief rows dated before `today`, newest first: [(date, backend name, {key: row})]. A damaged line is skipped."""
    found = []
    for line in _lines(history_path(out)):
        d = _row_date(line)
        if d is None or d >= today:
            continue
        rec = json.loads(line)
        rows = rec.get("rows") if isinstance(rec.get("rows"), list) else []
        found.append((d, rec.get("backend"), {r["key"]: r for r in rows if isinstance(r, dict) and "key" in r}))
    return sorted(found, key=lambda t: t[0], reverse=True)


def _reuse(prev, name):
    """{sig: harm} of the previous brief, when the same judge (by name) made its points. Another judge's points are not reused."""
    if not prev or prev[0][1] != name:
        return {}
    return {r["sig"]: r["harm"] for r in prev[0][2].values()
            if isinstance(r.get("harm"), (int, float)) and isinstance(r.get("sig"), str)}


def _run(prev, key, sig, band):
    """(days, same): how many brief rows in a row, from the newest, hold this key; and how many of those, from the newest, have the same
    sig and rank band as today's row (the count stops at the first one that differs)."""
    days = same = 0
    differed = False
    for _, _, rows in prev:
        row = rows.get(key)
        if row is None:
            break
        days += 1
        if not differed and row.get("sig") == sig and _band(row.get("rank")) == band:
            same += 1
        else:
            differed = True
    return days, same


def _mark(ranked, prev):
    """The numbered rows get 新 or 継続 N 日目 in their text, and are folded when unchanged for FOLD_DAYS days (not when notify or
    severe). Steps and queue rows have no number: they are never labelled or folded. Returns (ranked, folded numbers)."""
    marked, folded = [], []
    for it in ranked:
        if not it["key"].startswith(APPROVAL_PREFIX):
            marked.append(it)
            continue
        days, same = _run(prev, it["key"], _sig(it["subject"]), _band(it["rank"]))
        label = "新" if days == 0 else f"継続 {days + 1} 日目"
        fold = same + 1 >= FOLD_DAYS and not it["notify"] and not it["severe"]
        if fold:
            folded.append(it["key"][len(APPROVAL_PREFIX):])
        marked.append({**it, "text": f"{it['text']}（{label}）", "folded": fold})
    return marked, folded


def _when(value):
    """A record time as an aware datetime (None when it cannot be read)."""
    try:
        t = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()


def _replies(out, start):
    """How many replies of each kind were written to approvals.log.jsonl since `start`. The trial's fictional item is left out."""
    counts = {status: 0 for status in REPLY_STATUSES}
    for r in autorun._rows(Path(out) / "approvals.log.jsonl"):
        t = _when(r.get("at"))
        if t is None or t < start or autorun._key_is_sample(r.get("key")) or autorun._card_key(r.get("key")):
            continue
        if r.get("status") in counts:
            counts[r["status"]] += 1
    return counts


def _closed(out, prev):
    """How many numbered rows of the last brief are no longer waiting (approved, rejected or closed in approvals.json)."""
    if not prev:
        return 0
    ap = fsutil.read_json(Path(out) / "approvals.json", {"items": {}})
    items = ap.get("items") if isinstance(ap, dict) and isinstance(ap.get("items"), dict) else {}
    closed = 0
    for key in prev[0][2]:
        if key.startswith(APPROVAL_PREFIX):
            item = items.get(key[len(APPROVAL_PREFIX):])
            if isinstance(item, dict) and item.get("status") not in ("pending", "held"):
                closed += 1
    return closed


def _summary(out, prev, now, since):
    """The two lines under the header: the replies since the last brief (or the last 24 hours) and the rows that closed."""
    start, known = window_start(now, 1, since)
    counts = _replies(out, start)
    words = " / ".join(f"{autorun.REPLY_NAMES[s]} {n}" for s, n in counts.items())
    return [f"{_window_name(known, 1)}: {words}", f"閉じた: {_closed(out, prev)} 件"]


def build(out, backend, top=TOP, now=None, date=None, since=None):
    """now: aware datetime for the collection window; date: "YYYY-MM-DD" shown in the header; since: when the last brief was
    built (ISO text or datetime): the ADO / Teams / unsure sections count from there instead of the last 24 hours.
    Returns (text, ranked): the text names the first `top` items; `ranked` is every item with its harm and rank (the history)."""
    items = collect(out, now=now)
    delta = config.value("brief_delta") == "1"
    prev = _previous(out, _day(date, now)) if delta else []
    if is_desk():
        ranked = desk_order(items)
    else:
        ranked = rank(items, backend, reuse=_reuse(prev, judge_name(backend)) if delta else None)
    folded = []
    if delta:
        ranked, folded = _mark(ranked, prev)
    pending = sum(1 for i in items if i["kind"] == "approval")
    drafts = sum(1 for i in items if i["kind"] == "approval" and i["draft"]) if config.value("post_kind_mark") == "1" else None
    from . import work
    unfinished = [row for row in work.list_work(out) if row["state"] != "done"]
    empty_message = ("実行候補はありません。未完了作業の状況は下記を確認してください"
                     if unfinished else "対応が必要な項目はありません")
    shown = named(ranked, top)
    if folded and not shown:
        empty_message = "前回から変わった件はありません"
    text = format_post(shown, len(items) - len(folded), pending, date, empty_message, drafts=drafts, unchanged=folded)
    if delta:   # the two lines under the header: the replies and the closed rows since the last brief
        head, *rest = text.split("\n", 1)
        text = "\n".join([head, *_summary(out, prev, now, since)] + rest)
    quiet = quiet_reads(out, now=now)
    if quiet:
        text += (f"\n開いて読みましたが、対応は不要でした: {len(quiet)} 件（{'、'.join(quiet[:5])}{' ほか' if len(quiet) > 5 else ''}）"
                 "。開いたので、Teams では既読になっています")
    lost = not_put_back(out, now=now)
    if lost:
        text += f"\n⚠ 開いたチャットを元へ戻せなかった件が {lost} 件あります。Teams で開いているチャットを確認してください"
    ado = ado_section(out, now=now, since=since)
    if ado:
        text += "\n" + ado
    teams = teams_section(out, now=now, since=since)
    if teams:
        text += "\n" + teams
    unsure = unsure_section(out, now=now, since=since)
    if unsure:
        text += "\n" + unsure
    from . import promises   # R18: the 約束 section (promises=1 only; nothing is read at 0)
    if promises.enabled():
        section = promises.brief_section(out, now=now)
        if section:
            text += "\n" + section
    from . import owed   # R29: the 返事を待っている人 section (sent_watch=1 only; nothing is read at 0)
    if owed.enabled():
        section = owed.brief_section(out, now=now)
        if section:
            text += "\n" + section
    # Progress is a local, explicit fact (or Unknown for legacy approvals). It is
    # appended separately and does not create another model/ranking call.
    if unfinished:
        text += "\n\n承認済み・未完了の作業:"   # three rows at most; what nobody recorded (Unknown) is left out
        for row in unfinished[:3]:
            known = [f"{label}: {row[key]}" for label, key in (("担当", "owner"), ("期限", "due"), ("完了条件", "completion_condition"))
                     if row[key] != "Unknown"]
            text += (f"\n- #{row['case']} {row['next_action']}" + (f"（{row['state']}）" if row["state"] != "Unknown" else "")
                     + ("　" + " / ".join(known) if known else ""))
        if len(unfinished) > 3:
            text += f"\nほか {len(unfinished) - 3} 件（kimeru work list で全件）"
    return text, ranked


# --- out/brief_history.jsonl: what each delivered morning brief ranked (the next brief's changes are counted from it) ---
# One row per day: {"date", "backend", "rows": [{key, kind, sig, harm, rank, notify, severe, due_level}]}. No text, no name:
# `sig` is the first 12 hex digits of the sha1 of the subject. Written by daily.cycle only, after a delivery was read back.

def history_path(out):
    return Path(out) / "brief_history.jsonl"


def history_lock(out):
    return Path(out) / "brief_history.lock"


def _lines(path):
    """The non-empty lines as they are on disk. A damaged line is kept as it is: nothing here drops what it cannot read."""
    if not path.exists():
        return []
    return [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _row_date(line):
    """The date of a history line, or None when the line is damaged or has no valid date."""
    try:
        d = json.loads(line).get("date")
        datetime.strptime(d, "%Y-%m-%d")
        return d
    except (ValueError, TypeError, AttributeError):
        return None


def _write(path, lines):
    fsutil.write_atomic(path, "".join(l + "\n" for l in lines))
    path.with_name(path.name + ".bak").unlink(missing_ok=True)   # no second copy: the rows are kept only as long as the rule says


def _history_row(it):
    return {"key": it["key"], "kind": it["kind"],
            "sig": _sig(it["subject"]),
            "harm": it.get("harm"), "rank": it.get("rank"), "notify": bool(it.get("notify")),
            "severe": bool(it.get("severe")), "due_level": it["due_level"]}


def record_history(out, date, backend, ranked):
    """Write the row of `date`; a second write for the same day replaces it. `ranked` is what build returns (every item, with its
    rank and harm). Returns "recorded", or "busy" when another run holds brief_history.lock (then nothing is written)."""
    entry = json.dumps({"date": date, "backend": judge_name(backend), "rows": [_history_row(it) for it in ranked]},
                       ensure_ascii=False)
    path = history_path(out)
    with fsutil.exclusive(history_lock(out)) as got:
        if not got:
            return "busy"
        lines, placed = [], False
        for line in _lines(path):
            if _row_date(line) == date:
                if not placed:
                    lines.append(entry)
                    placed = True
            else:
                lines.append(line)
        if not placed:
            lines.append(entry)
        _write(path, lines)
    return "recorded"


def purge_history(out, now=None, keep_days=HISTORY_KEEP_DAYS):
    """Delete the rows dated more than `keep_days` before `now` (a row exactly `keep_days` back is kept). Returns how many were
    deleted: 0 when nothing is old, or when another run holds brief_history.lock (the next cycle tries again)."""
    path = history_path(out)
    cutoff = ((now or datetime.now()).date() - timedelta(days=keep_days)).isoformat()

    def old(line):
        d = _row_date(line)
        return d is not None and d < cutoff
    if not any(old(l) for l in _lines(path)):   # read without the lock first: most cycles have nothing to delete
        return 0
    with fsutil.exclusive(history_lock(out)) as got:
        if not got:
            return 0
        lines = _lines(path)
        kept = [l for l in lines if not old(l)]
        if len(kept) == len(lines):
            return 0
        _write(path, kept)
        return len(lines) - len(kept)
