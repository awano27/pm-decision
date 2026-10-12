"""約束の台帳 (R18): what the PM promised a chat, and the decisions asked for this week, with the day each falls due (setting promises=1).

Read only: approvals.json and decisions.jsonl are read, nothing is written and no file is added. No model is asked, and nothing here
changes a rank, a subject or a judgment: the only effect on a judgment is needs_human (and its advice) of a status question from a chat
with an overdue promise (status_guard). Three sources:
  (a) an approved reply (teams.reply) whose sentence says a commitment with a deadline ("金曜までに回答します"). The deadline counts
      from the day the PM's OK was observed (measurement.approval_observed_at); an item without that time is left out.
  (b) a decision_later judgment (graphs/teams_chat.json): a request to decide this week (判断依頼（今週中と判定）), counted from its
      decision day and shown until DECISION_SHOW_DAYS after that day. It is not a promise made to anyone, so it is only in the brief.
  (c) a step of an approved item that is due today or this week (due_level <= 1, not 期限要確認), counted as the brief counts it.
An (a) or (c) promise is closed when the item's work is done, and it is left out PROMISE_KEEP_DAYS after its day (no reply closes it).
Where it shows (promises=1 only): the morning brief's 約束 section, the end of the arrow line of a new post from the same chat, 詳細 N
(the chat's open promises of a and c). With promises=0 nothing here is read.
"""
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import brief, config, fsutil, memory, plan

PROMISE_KEEP_DAYS = 7       # an (a) or (c) promise is shown this many days after its day, then left out
DECISION_SHOW_DAYS = 2      # a (b) decision is shown until this many days after its day (the brief drops it on that day)
URGENCY_BAND = (0.5, 1.5)   # decision_later: the urgency score in [0.5, 1.5) (0 = no deadline, 1 = this week, 2 = today)
SHOW_MAX = 5                # lines of the brief's 約束 section
DETAIL_MAX = 3              # lines of 「この相手の約束」 in 詳細 N
NOTE_MAX = 60               # the note at the end of an arrow line
LABEL_MAX = 20              # a promise's label (the words of the reply, cut)
DECISION_LABEL = "判断依頼（今週中と判定）"
ADVICE = "期限を過ぎた約束がある相手からの状況確認"
WEEKDAYS = "月火水木金土日"

# The PM's own commitment: a verb in the polite form (回答します, 返信いたします, 報告致します, 送ります), not a question (ますか).
COMMIT = re.compile(r"(?P<stem>回答|返信|返答|連絡|報告|共有|確認|対応|判断|送付|提出|完了|修正|お知らせ|ご連絡|ご報告|ご回答|お返事"
                    r"|検討|実施|送信|返|送)(?:いた|致)?(?:し|り)ます(?!か)")
MEETING = re.compile(r"会議|打合せ|MTG")       # a sentence about a meeting is not a promise of the PM's
SENTENCE_END = re.compile(r"[。！？!?\r\n；;]")


def _ymd(year, month, day):
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _friday(day):
    """今週中: the Friday of the day's week, the day itself on a weekend (the same rule as brief.due_date with due_level 1)."""
    return day + timedelta(days=max(0, 4 - day.weekday()))


def _monday(day):
    return day - timedelta(days=day.weekday())


def _weekday_deadline(m, day):
    i = WEEKDAYS.index(m.group("wd"))
    wk = m.group("wk") or ""
    if wk.startswith("来週"):
        return _monday(day) + timedelta(days=7 + i)
    if wk.startswith("今週"):
        return _monday(day) + timedelta(days=i)
    if re.search("今週|来週|今月|来月", m.string):   # 「来週の件、金曜までに」: the week of the 金曜 is not said, so no date (a safe miss)
        return None
    return day + timedelta(days=(i - day.weekday()) % 7)   # a bare weekday: the next one on or after the day


def _month_day_deadline(m, day):
    due = _ymd(day.year, int(m.group("m")), int(m.group("d")))
    if due is not None and due < day:
        due = _ymd(day.year + 1, int(m.group("m")), int(m.group("d")))
    return due


def _day_deadline(m, day):
    """「5日まで」: the 5th of this month, or of the next month when that day has passed."""
    n = int(m.group("d"))
    due = _ymd(day.year, day.month, n)
    if due is not None and due < day:
        year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
        due = _ymd(year, month, n)
    return due


DEADLINES = (
    (re.compile(r"本日中|今日中"), lambda m, day: day),
    (re.compile(r"明日(?:中|まで)"), lambda m, day: day + timedelta(days=1)),
    (re.compile(r"今週中"), lambda m, day: _friday(day)),
    (re.compile(r"(?P<wk>今週の?|来週の?)?(?P<wd>[月火水木金土日])曜日?(?:まで|までに)"), _weekday_deadline),
    (re.compile(r"(?<![\d/])(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:まで|までに)"), _month_day_deadline),
    (re.compile(r"(?<![\d/年])(?P<m>\d{1,2})月(?P<d>\d{1,2})日(?:まで|までに)"), _month_day_deadline),   # 「12月3日」; a year before it is a safe miss
    (re.compile(r"(?<![\d/月])(?P<d>\d{1,2})日(?:まで|までに)"), _day_deadline),   # 「3日」; 「12月3日」 and 「来月15日」 are not this form
)


def _clip(text, n):
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def commitment(text, day):
    """(due date, label) of the first sentence of `text` in which the PM commits to a deadline, or None. The sentence needs the PM's
    commitment verb and a deadline term; a sentence about a meeting, a question or a negation is not one. `day` is the local day of
    the OK. The label is the words from the deadline to the verb: 金曜までに回答."""
    for sentence in SENTENCE_END.split(unicodedata.normalize("NFKC", str(text or ""))):
        verb = COMMIT.search(sentence)
        if verb is None or MEETING.search(sentence):
            continue
        hits = []
        for pattern, due_of in DEADLINES:
            for m in pattern.finditer(sentence):
                due = due_of(m, day)
                if due is not None:
                    hits.append((m, due))
        if not hits:
            continue
        m, due = min(hits, key=lambda h: h[0].start())
        return due, _clip(sentence[min(m.start(), verb.start()):max(m.end(), verb.end("stem"))], LABEL_MAX)
    return None


def enabled():
    """promises=1: the brief, the posts, 詳細 N and a status question use the open promises. 0 (the default): none of them is read."""
    return config.value("promises") == "1"


def _when(value):
    """An aware time from a record's ISO text (a naive one is taken as local time); None when it cannot be read."""
    try:
        t = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.astimezone()


def _urgency(rec):
    """The urgency score of a decision_later row (its trace), or None. A trace that is not a list is no trace."""
    path = rec.get("path")
    for step in path if isinstance(path, list) else []:
        if isinstance(step, dict) and step.get("node") == "urgency":
            score = (step.get("answer") or {}).get("score")
            return float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None
    return None


def _who(ev):
    return str(ev.get("chat_title") or ev.get("author") or "").strip()[:30]


def _entry(src, n, chat, who, label, due, made):
    return {"src": src, "n": n, "chat": chat, "who": who, "label": label, "due": due, "made": made}


def _from_item(n, item, rec, ev):
    """The (a) and (c) promises of one approved item."""
    rows, chat, who = [], ev.get("chat_id"), _who(ev)
    measurement = item.get("measurement") if isinstance(item.get("measurement"), dict) else {}
    observed = _when(measurement.get("approval_observed_at")) if measurement.get("approval_observed_at") else None
    if observed is not None:
        for a in rec.get("actions") or []:
            if isinstance(a, dict) and a.get("type") == "teams.reply":
                hit = commitment(a.get("text"), observed.astimezone().date())
                if hit:
                    rows.append(_entry("a", n, a.get("chat_id") or chat, who, hit[1], hit[0], observed.astimezone().date()))
                    break
    at = _when(rec.get("at"))
    if at is not None:
        for s in (rec.get("plan") or {}).get("steps") or []:
            if isinstance(s, dict) and s.get("due_level", 2) <= 1 and s.get("due") != plan.DUE_UNSURE:
                rows.append(_entry("c", n, chat, who, _clip(s.get("title"), LABEL_MAX),
                                   brief.due_date(rec["at"], s["due_level"]), at.astimezone().date()))
    return rows


def _from_decisions(out, today):
    """The (b) rows: the newest decision_later judgment of each event, with a this-week urgency, until DECISION_SHOW_DAYS after its day.
    Reads the bounded tail of decisions.jsonl (memory._tail_rows, as the memory and link readers do): a torn line or a line that is
    not an object is skipped, so one damaged line cannot stop the ledger, the post, 詳細 N or a status question."""
    rows, seen = [], set()
    for rec in reversed(memory._tail_rows(Path(out) / "decisions.jsonl")):
        if rec.get("event_kind") != "teams.chat" or rec.get("node") != "decision_later" or rec.get("outcome") != "decide":
            continue
        if rec.get("event_id") in seen:
            continue
        seen.add(rec.get("event_id"))
        at, score = _when(rec.get("at")), _urgency(rec)
        if at is None or score is None or not URGENCY_BAND[0] <= score < URGENCY_BAND[1]:
            continue
        due = brief.due_date(rec["at"], 1)
        if today >= due + timedelta(days=DECISION_SHOW_DAYS):
            continue
        ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
        rows.append(_entry("b", None, ev.get("chat_id"), _who(ev), DECISION_LABEL, due, at.astimezone().date()))
    return rows


def snapshot(out, now=None):
    """The open promises at `now`: {"today": the local day, "rows": [entry, ...]}, the earliest due first. An entry is {src: a|b|c,
    n: the approval's number (None for b), chat, who, label, due, made}. Reads approvals.json and decisions.jsonl; writes nothing."""
    out = Path(out)
    today = (now or datetime.now(timezone.utc)).astimezone().date()
    data = fsutil.read_json(out / "approvals.json", {"items": {}})
    items = data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), dict) else {}
    rows = []
    for n, item in items.items():
        if not isinstance(item, dict) or item.get("status") != "approved":
            continue
        if isinstance(item.get("work"), dict) and item["work"].get("state") == "done":   # the work is done: the promise is closed
            continue
        rec = item.get("record") if isinstance(item.get("record"), dict) else {}
        ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
        rows.extend(_from_item(str(n), item, rec, ev))
    rows.extend(_from_decisions(out, today))
    rows = [r for r in rows if (today - r["due"]).days <= PROMISE_KEEP_DAYS]
    rows.sort(key=lambda r: (r["due"], r["src"]))
    return {"today": today, "rows": rows}


def _state(row, today):
    days = (row["due"] - today).days
    if days < 0:
        return f"期限切れ {-days} 日"
    return "今日まで" if days == 0 else f"{row['due'].month}/{row['due'].day} まで"


def _bucket(row, today):
    days = (row["due"] - today).days
    return "超過" if days < 0 else "今日" if days == 0 else "今週" if days <= 7 else None


def _name(row):
    return f"#{row['n']}「{row['label']}」" if row["n"] is not None else row["label"]


def _md(day):
    return f"{day.month}/{day.day}"


def _of_chat(snap, rec, n=None):
    """The open promises (a and c) of the record's chat, the earliest first, without the item's own (number n). A (b) decision is
    not a promise made to anyone, so it is in the brief only and never at the end of a post or in 詳細 N."""
    ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
    chat = ev.get("chat_id")
    if not snap or not chat:
        return []
    return [r for r in snap["rows"] if r["chat"] == chat and r["src"] != "b" and (n is None or r["n"] != str(n))]


def brief_section(out, now=None):
    """The brief's 約束 section: 「約束: 超過 1 件 / 今日 1 件 / 今週 2 件」 and a line per promise (the earliest first, SHOW_MAX lines,
    then ほか N 件). 「閉じたもの」 and the ones not due within the week are left out. "" when there are none."""
    snap = snapshot(out, now)
    today = snap["today"]
    rows = [r for r in snap["rows"] if _bucket(r, today)]
    if not rows:
        return ""
    counts = {}
    for r in rows:
        counts[_bucket(r, today)] = counts.get(_bucket(r, today), 0) + 1
    lines = ["約束: " + " / ".join(f"{k} {counts[k]} 件" for k in ("超過", "今日", "今週") if k in counts)]
    for r in rows[:SHOW_MAX]:
        lines.append(f"・{_name(r)}: {_state(r, today)}" + (f"（{r['who']}）" if r["who"] else ""))
    if len(rows) > SHOW_MAX:
        lines.append(f"ほか {len(rows) - SHOW_MAX} 件")
    return "\n".join(lines)


def post_note(snap, rec, n=None):
    """The end of the arrow line of a new post from the same chat: （約束: 10/7 #2「金曜までに回答」 期限切れ 3 日）, for the chat's first
    open promise (the most overdue first). None when there is none, or when promises=0 (snap is None)."""
    mine = _of_chat(snap, rec, n)
    if not mine:
        return None
    return _clip(f"（約束: {_md(mine[0]['made'])} {_name(mine[0])} {_state(mine[0], snap['today'])}）", NOTE_MAX)


def detail_lines(snap, rec, n=None):
    """「この相手の約束」 in 詳細 N: the chat's open promises, the earliest first (DETAIL_MAX lines). None when there are none."""
    mine = _of_chat(snap, rec, n)
    if not mine:
        return None
    return [f"・{_md(r['made'])} {_name(r)}: {_state(r, snap['today'])}" for r in mine[:DETAIL_MAX]]


def status_guard(res, event, out, now=None):
    """promises=1: a status question (node reply_status) from a chat with an overdue promise goes to the PM. Only needs_human and the
    advice change; the node, the actions and the rest stay as they are. res comes back unchanged when promises is 0, when the node is
    not reply_status, when it already goes to the PM, or when the chat has no overdue promise. A (b) decision is not a promise here."""
    if not enabled() or res.get("node") != "reply_status" or res.get("outcome") != "decide" or res.get("needs_human"):
        return res
    chat = event.get("chat_id") if isinstance(event, dict) else None
    if not chat:
        return res
    snap = snapshot(out, now)
    if not any(r["chat"] == chat and r["src"] != "b" and (snap["today"] - r["due"]).days > 0 for r in snap["rows"]):
        return res
    return {**res, "needs_human": True, "advice": ADVICE}
