"""Morning brief: rank open work and format one post for the Teams self chat.

Candidates come from local files only:
  - approvals still pending/held (out/approvals.json) and queue items not yet in approvals
  - plan steps due today / this week from recent decisions (out/decisions.jsonl)
Ordering: due level first (already judged by Jev inside the plan node), then the
harm of a one-day delay, scored by Jev in ONE call (one score question per item).
The due label is NOT shown to Jev for the harm question, so it cannot anchor on it.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

HARM = ["No real harm", "Minor inconvenience for the team",
        "Significant harm to the schedule or to stakeholders", "Severe harm to customers or the business"]
MAX_ITEMS = 30  # keep a single Jev call small
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

    def add(key, kind, text, subject, due_level):
        if key not in seen:
            seen.add(key)
            items.append({"key": key, "kind": kind, "text": text, "subject": subject, "due_level": due_level})

    ap = json.loads((out / "approvals.json").read_text(encoding="utf-8")) if (out / "approvals.json").exists() else {"items": {}}
    queued = set()
    for n, it in ap["items"].items():
        queued.add(it["key"])
        if it["status"] in ("pending", "held"):
            what = it['record'].get('advice') or it['record'].get('graph')
            add(f"approval:{n}", "approval", f"確認待ち #{n}: {what}", f"Waiting for the PM's approval: {what}", 0)
    for rec in reversed(_rows(out / "queue.jsonl")):
        key = f"{rec.get('graph')}:{rec.get('event_id')}:{rec.get('node')}"
        if key not in queued:
            what = rec.get('advice') or rec.get('graph')
            add(f"queue:{key}", "approval", f"確認待ち（未投稿）: {what}", f"Waiting for the PM's approval: {what}", 0)
    for rec in reversed(_rows(out / "decisions.jsonl")):
        at = rec.get("at")
        if at and datetime.fromisoformat(at) < since:
            continue
        p = rec.get("plan")
        today = now.astimezone().date()
        for s in (p or {}).get("steps", []):
            if s.get("due_level", 2) <= 1:
                # "今日" was relative to the decision; re-anchor it so yesterday's "today" shows as overdue
                due = due_date(at, s["due_level"]) if at else None
                label, level = (due_label(due, today), 0 if due <= today else 1) if due else (s["due"], s["due_level"])
                add(f"step:{rec.get('event_id')}:{p['playbook']}:{s['id']}", "step",
                    f"{p['title']}: {s['title']}（{label}）", f"{p['title']}: {s['title']}", level)
    return items[:MAX_ITEMS]


def rank(items, backend, top=3):
    if not items:
        return []
    state = {"items": [i["subject"] for i in items]}
    qs = {f"h{i}": {"type": "score", "criteria": HARM, "hints": STUB_HINTS,
                    "instructions": f"How much harm would a one-day delay of item `items[{i}]` cause?"}
          for i in range(len(items))}
    ans = backend.ask(state, qs)
    keyed = [((-it["due_level"], ans[f"h{i}"]["score"], -i), it) for i, it in enumerate(items)]  # tie -> newer first
    keyed.sort(key=lambda t: t[0], reverse=True)
    return [{**it, "harm": round(k[1], 2)} for k, it in keyed[:top]]


def format_post(ranked, total, pending, date=None):
    date = date or datetime.now().strftime("%Y-%m-%d")
    lines = [f"[kimeru brief {date}] 今日の進め方"]
    if not ranked:
        lines.append("対応が必要な項目はありません")
    for i, it in enumerate(ranked, 1):
        lines.append(f"{i}. {it['text']}")
    if total > len(ranked):
        lines.append(f"ほか {total - len(ranked)} 件")
    if pending:
        lines.append(f"確認待ち {pending} 件（OK 番号 / NG 番号 / 保留 番号 で返信）")
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


def build(out, backend, top=3, now=None, date=None):
    """now: aware datetime for the collection window; date: "YYYY-MM-DD" shown in the header."""
    items = collect(out, now=now)
    ranked = rank(items, backend, top)
    pending = sum(1 for i in items if i["kind"] == "approval")
    text = format_post(ranked, len(items), pending, date)
    quiet = quiet_reads(out, now=now)
    if quiet:
        text += (f"\n開いて読みましたが、対応は不要でした: {len(quiet)} 件（{'、'.join(quiet[:5])}{' ほか' if len(quiet) > 5 else ''}）"
                 "。開いたので、Teams では既読になっています")
    lost = not_put_back(out, now=now)
    if lost:
        text += f"\n⚠ 開いたチャットを元へ戻せなかった件が {lost} 件あります。Teams で開いているチャットを確認してください"
    return text, ranked
