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
        for s in (p or {}).get("steps", []):
            if s.get("due_level", 2) <= 1:
                add(f"step:{rec.get('event_id')}:{p['playbook']}:{s['id']}", "step",
                    f"{p['title']}: {s['title']}（{s['due']}）", f"{p['title']}: {s['title']}", s["due_level"])
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


def build(out, backend, top=3, now=None):
    items = collect(out, now=now)
    ranked = rank(items, backend, top)
    pending = sum(1 for i in items if i["kind"] == "approval")
    return format_post(ranked, len(items), pending), ranked
