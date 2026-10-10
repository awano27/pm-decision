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
                    f"{p['title']}: {s['title']}（{label}）", f"{p['title']}: {s['title']}", level)

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


def format_post(ranked, total, pending, date=None, empty_message="対応が必要な項目はありません"):
    date = date or datetime.now().strftime("%Y-%m-%d")
    lines = [f"[kimeru brief {date}] 今日の進め方"]
    if not ranked:
        lines.append(empty_message)
    for i, it in enumerate(ranked, 1):
        lines.append(f"{i}. {it['text']}")
    if total > len(ranked):
        lines.append(f"ほか {total - len(ranked)} 件")
    if pending:
        lines.append(f"確認待ち {pending} 件（OK 番号 / NG 番号 / 詳細 番号 で返信）")
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


def _ado_outcome(rec):
    """"P2" for a decision that set priority 2, else the node's name."""
    for a in rec.get("actions") or []:
        n = (a.get("fields") or {}).get("Microsoft.VSTS.Common.Priority")
        if n:
            return f"P{n}"
    return str(rec.get("node") or "?")


def ado_auto(out, now=None, days=1):
    """Work items the graph decided by itself in the last `days` day(s), with nothing for the PM to answer (no confirmation, no
    notice): [{id, type, title, outcome}], one per work item (the latest judgment), newest first. A decision that waits in a
    numbered post, or was notified, is shown there and not here."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    seen, found = set(), []
    for rec in reversed(_rows(Path(out) / "decisions.jsonl")):
        if rec.get("event_kind") != "ado.workitem.created" or rec.get("outcome") != "decide":
            continue
        if rec.get("needs_human") or rec.get("notify") or not rec.get("at") or datetime.fromisoformat(rec["at"]) < since:
            continue
        if rec.get("event_id") in seen:
            continue
        seen.add(rec.get("event_id"))
        ev = rec.get("event") or {}
        found.append({"id": rec.get("event_id"), "type": ev.get("type"), "title": ev.get("title"), "outcome": _ado_outcome(rec)})
    return found


def ado_section(out, now=None, days=1):
    """The brief's lines for `ado_auto`: counts per outcome, a few "#id [type] title → P2" lines, then "ほか N 件". "" when none."""
    rows = ado_auto(out, now=now, days=days)
    if not rows:
        return ""
    from . import graph
    counts = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    lines = [f"ADO の自動判断（直近 24 時間）: {len(rows)} 件（" + " / ".join(f"{k} {v} 件" for k, v in sorted(counts.items())) + "）"]
    for r in rows[:ADO_LIST_MAX]:
        lines.append(f"・#{r['id']}" + (f" [{r['type']}]" if r.get("type") else "")
                     + (" " + graph.one_line(str(r["title"])) if r.get("title") else "") + f" → {r['outcome']}")
    if len(rows) > ADO_LIST_MAX:
        lines.append(f"ほか {len(rows) - ADO_LIST_MAX} 件")
    return "\n".join(lines)


def build(out, backend, top=3, now=None, date=None):
    """now: aware datetime for the collection window; date: "YYYY-MM-DD" shown in the header."""
    items = collect(out, now=now)
    ranked = rank(items, backend, top)
    pending = sum(1 for i in items if i["kind"] == "approval")
    from . import work
    unfinished = [row for row in work.list_work(out) if row["state"] != "done"]
    empty_message = ("実行候補はありません。未完了作業の状況は下記を確認してください"
                     if unfinished else "対応が必要な項目はありません")
    text = format_post(ranked, len(items), pending, date, empty_message)
    quiet = quiet_reads(out, now=now)
    if quiet:
        text += (f"\n開いて読みましたが、対応は不要でした: {len(quiet)} 件（{'、'.join(quiet[:5])}{' ほか' if len(quiet) > 5 else ''}）"
                 "。開いたので、Teams では既読になっています")
    lost = not_put_back(out, now=now)
    if lost:
        text += f"\n⚠ 開いたチャットを元へ戻せなかった件が {lost} 件あります。Teams で開いているチャットを確認してください"
    ado = ado_section(out, now=now)
    if ado:
        text += "\n" + ado
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
