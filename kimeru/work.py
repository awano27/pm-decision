"""Explicit progress tracking for approved cases; approval and execution stay independent.

R30 (promises=1 only; the default 0 reads and writes nothing here): the self chat's replies `着手 N`, `完了 N`, `完了 N k` (step k of the
plan) and `延期 N <いつ>` change the work of an approved case, and the deadline post `[kimeru 期限 #N ...]` (once a day, from nudge_hour)
lists the work that is due. A reply counts once per deadline post (`acked`), so a reply that stays on the screen after `work set` changed
the case by hand is not applied again.
"""
import hashlib
import json
import os
import threading
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config, fsutil, hold, learn, notify, plan

STATES = ("approved", "in_progress", "done", "blocked")
SEND_READY = {"teams.reply", "teams.post"}


class WorkError(ValueError):
    pass


def _history(path):
    """Read every history row or fail closed; a damaged row is never discarded."""
    source = path
    if not source.exists():
        backup = path.with_name(path.name + ".bak")
        if backup.exists():
            source = backup
        else:
            return []
    rows = []
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkError(f"work history cannot be read; preserve it and retry ({type(exc).__name__})") from None
    for number, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            raise WorkError(f"work history is damaged at line {number}; preserve it for repair") from None
        if not isinstance(row, dict):
            raise WorkError(f"work history is damaged at line {number}; preserve it for repair")
        if "transition_id" in row and (not isinstance(row["transition_id"], str) or not row["transition_id"]):
            raise WorkError(f"work history is damaged at line {number}; preserve it for repair")
        rows.append(row)
    return rows


def _write_history(path, rows):
    """Stage and fsync the full journal, then replace it without moving the old journal away first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}")
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    try:
        with tmp.open("x", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        fsutil._replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _recover_pending(ap, path):
    pending = ap.data.get("work_history_pending", [])
    rows = _history(path)
    if not pending:
        return
    if not isinstance(pending, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("transition_id"), str) or not row["transition_id"]
            for row in pending):
        raise WorkError("pending work history is damaged; preserve approvals.json for repair")
    known = {row.get("transition_id") for row in rows if row.get("transition_id")}
    merged = list(rows)
    for row in pending:
        if row["transition_id"] not in known:
            merged.append(row)
            known.add(row["transition_id"])
    try:
        _write_history(path, merged)
        ap.data["work_history_pending"] = []
        ap.save()
    except OSError as exc:
        raise WorkError(f"progress is saved, but work history recovery is still pending; retry work set ({type(exc).__name__})") from None


def _valid_due(due):
    if due is not None:
        try:
            if date.fromisoformat(due).isoformat() != due:
                raise ValueError
        except (TypeError, ValueError):
            raise WorkError("due must be a calendar date in YYYY-MM-DD form") from None


def set_state(out, case, state, *, owner=None, due=None, completion_condition=None, now=None):
    """Set an explicitly approved case's progress and optional metadata under the approvals lock."""
    if state not in STATES:
        raise WorkError(f"state must be one of: {', '.join(STATES)}")
    _valid_due(due)
    with fsutil.exclusive(notify.lock_path(out)) as got:
        if not got:
            raise WorkError("approvals are busy; retry later")
        ap = notify.Approvals(out)
        history_path = Path(out) / "work-state.jsonl"
        _recover_pending(ap, history_path)
        return _apply_state(ap, history_path, case, state, owner=owner, due=due,
                            completion_condition=completion_condition, now=now)


def _approved_item(ap, case):
    item = ap.data.get("items", {}).get(str(case))
    if not item or learn.is_card(item) or item.get("status") != "approved":
        raise WorkError(f"case #{case} is not explicitly approved")
    return item


def _commit(ap, history_path, case, item, entry, previous_state, at, step=None):
    """Put the new progress on the item and journal it. The caller holds approvals.lock and `ap`. The journal row is kept as pending
    until it is written (_recover_pending), so a failure here is recovered by the next call and not lost."""
    entry["updated_at"] = at
    item["work"] = entry
    row = {"transition_id": uuid.uuid4().hex, "at": at, "case": str(case), "from": previous_state,
           "to": entry["state"] if entry.get("state") in STATES else "Unknown",
           "owner": entry.get("owner"), "due": entry.get("due"), "completion_condition": entry.get("completion_condition")}
    if step is not None:
        row["step"] = step
    ap.data.setdefault("work_history_pending", []).append(row)
    try:
        ap.save()
    except OSError as exc:
        raise WorkError(f"progress was not saved; retry work set ({type(exc).__name__})") from None
    _recover_pending(ap, history_path)
    return "updated"


def _apply_state(ap, history_path, case, state, *, owner=None, due=None, completion_condition=None, now=None):
    """The state and metadata of an approved case (state None keeps the state: 延期 changes only the day). The caller holds the lock.
    "unchanged" when nothing differs (nothing is journaled)."""
    item = _approved_item(ap, case)
    previous = item.get("work") if isinstance(item.get("work"), dict) else {}
    previous_state = previous.get("state") if previous.get("state") in STATES else "Unknown"
    entry = dict(previous)
    if state is not None:
        entry["state"] = state
    if owner is not None:
        entry["owner"] = owner
    if due is not None:
        entry["due"] = due
    if completion_condition is not None:
        entry["completion_condition"] = completion_condition
    if entry.get("state") == previous.get("state") and all(
            entry.get(key) == previous.get(key) for key in ("owner", "due", "completion_condition")):
        return "unchanged"
    at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return _commit(ap, history_path, case, item, entry, previous_state, at)


def _apply_step(ap, history_path, case, step_id, now):
    """One step of the plan done (R30 `完了 N k`), kept by the step's id. The case's state does not change."""
    item = _approved_item(ap, case)
    previous = item.get("work") if isinstance(item.get("work"), dict) else {}
    previous_state = previous.get("state") if previous.get("state") in STATES else "Unknown"
    done = previous.get("steps") if isinstance(previous.get("steps"), dict) else {}
    if step_id in done:
        return "unchanged"
    at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    entry = dict(previous)
    entry["steps"] = {**done, step_id: {"done_at": at}}
    return _commit(ap, history_path, case, item, entry, previous_state, at, step=step_id)


def _next_action(record):
    memo = record.get("memo") or {}
    plan = record.get("plan") or {}
    return memo.get("next") or plan.get("summary") or record.get("advice") or "Unknown"


def list_work(out):
    """Approved cases with explicit tracked fields; absent progress is exposed as Unknown."""
    ap = notify.Approvals(out)
    rows = []
    for number, item in learn.cases(ap.data.get("items", {})).items():
        if item.get("status") != "approved":
            continue
        record = item.get("record") or {}
        work = item.get("work") if isinstance(item.get("work"), dict) else {}
        state = work.get("state") if work.get("state") in STATES else "Unknown"
        rows.append({"case": int(number), "revision": item.get("revision", 1), "state": state,
                     "next_action": _next_action(record), "owner": work.get("owner", "Unknown"),
                     "due": work.get("due", "Unknown"),
                     "completion_condition": work.get("completion_condition", "Unknown")})
    return sorted(rows, key=lambda row: row["case"])


def evidence(out):
    """Aggregate approved work and confirmed/ambiguous copy-ready handoffs without text or identifiers. A learn card (R21) is
    no case: its OK is not an approval of work."""
    ap = notify.Approvals(out)
    approved = [item for item in learn.cases(ap.data.get("items", {})).values() if item.get("status") == "approved"]
    counts = {state: 0 for state in STATES}
    tracked = verified = unknown = 0
    for item in approved:
        progress = item.get("work") if isinstance(item.get("work"), dict) else {}
        state = progress.get("state")
        if state in STATES:
            tracked += 1
            counts[state] += 1
        actions = (item.get("record") or {}).get("actions") or []
        handoff_eligible = any(action.get("type") in SEND_READY and str(action.get("text") or "").strip()
                               for action in actions)
        handoffs = item.get("handoff_evidence") or []
        if handoff_eligible and any(row.get("source") in ("bridge_readback", "user_confirmation") for row in handoffs):
            verified += 1
        elif handoff_eligible:
            # A legacy approved case has no handoff record; that is unknown history,
            # not proof that the copy-ready text reached the self chat.
            unknown += 1
    return {"approved_cases": len(approved), "tracked_cases": tracked,
            "unknown_progress_cases": len(approved) - tracked, "states": counts,
            "handoff_verified_cases": verified, "handoff_unknown_cases": unknown}


# ---- R30: the replies of the self chat and the deadline post (promises=1 only; the default 0 reads and writes nothing here)

DUE_KEEP_DAYS = 7                # work due this many days ago or less is still listed (the promises ledger keeps 7 days too)
NOTICE_HOURS = 24                # the replies to a deadline post are read for this many hours after it was posted
NOTICE_LIMIT = 8                 # lines of one deadline post; the rest is counted
NOTICE_DAY = "work_notice_day"   # top level of approvals.json: the local day of the last deadline post
NUDGE_HOUR = 8                   # the hour from which the deadline post is made (setting nudge_hour)
NOTICE_HEAD = "期限の作業があります（今日が期限、または過ぎています）。返信: 完了 N（件を完了）、完了 N k（手順 k を完了）、着手 N、延期 N <いつ>"


def enabled():
    """promises=1: the replies and the deadline post are read and written. 0 (the default): nothing here is read or written."""
    return config.value("promises") == "1"


def nudge_hour():
    """nudge_hour: a whole hour from 0 to 23; anything else is the default (8)."""
    try:
        n = int(config.value("nudge_hour"))
    except (TypeError, ValueError):
        return NUDGE_HOUR
    return n if 0 <= n <= 23 else NUDGE_HOUR


def _clip(text, n):
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _day(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _aware(value):
    """The local time of an ISO text (a naive one is local time already); None when it cannot be read."""
    try:
        return datetime.fromisoformat(str(value)).astimezone()
    except (TypeError, ValueError):
        return None


def _open_items(data):
    """(number, item, progress) of the approved, posted cases that are not done (a learn card is no case)."""
    for n, it in (data.get("items") or {}).items():
        if not isinstance(it, dict) or it.get("status") != "approved" or not it.get("posted") or learn.is_card(it):
            continue
        progress = it.get("work") if isinstance(it.get("work"), dict) else {}
        if progress.get("state") != "done":
            yield n, it, progress


def _steps(item):
    return ((item.get("record") or {}).get("plan") or {}).get("steps") or []


def _due_lines(n, item, progress, today):
    """The lines of one case in the deadline post. The case's own day (work.due, set by 延期 or the CLI) when it is set, otherwise each
    plan step of 今日 / 今週 whose day (from the decision) has come. Due: that day is today or earlier, within DUE_KEEP_DAYS."""
    def due_now(day):
        return day is not None and day <= today and (today - day).days <= DUE_KEEP_DAYS

    explicit = _day(progress.get("due"))
    if explicit is not None:
        if not due_now(explicit):
            return []
        return [f"・#{n}「{_clip(_next_action(item.get('record') or {}), 20)}」（終わったら「完了 {n}」）"]
    from . import brief   # lazily: brief imports other modules that this one is not needed by
    done = progress.get("steps") if isinstance(progress.get("steps"), dict) else {}
    at = (item.get("record") or {}).get("at")
    lines = []
    for k, step in enumerate(_steps(item), 1):
        if not isinstance(step, dict) or step.get("id") in done or step.get("due") == plan.DUE_UNSURE or step.get("due_level") not in (0, 1):
            continue
        try:
            day = brief.due_date(at, step["due_level"])
        except (TypeError, ValueError, KeyError):
            continue
        if due_now(day):
            lines.append(f"・#{n} 手順 {k}「{_clip(step.get('title'), 20)}」（終わったら「完了 {n} {k}」）")
    return lines


def due_rows(data, today):
    """[(number, [line, ...])] for the deadline post of `today` (see _due_lines); a case with no line is left out."""
    rows = []
    for n, item, progress in _open_items(data):
        lines = _due_lines(n, item, progress, today)
        if lines:
            rows.append((n, lines))
    return rows


def _notice_text(rows):
    head = "[kimeru 期限 " + " ".join(f"#{n}" for n, _ in rows) + "]"
    shown = [line for _, lines in rows for line in lines]
    body = [NOTICE_HEAD] + shown[:NOTICE_LIMIT]
    if len(shown) > NOTICE_LIMIT:
        body.append(f"ほか {len(shown) - NOTICE_LIMIT} 件")
    return head + "\n" + "\n".join(body)


def awaiting(data, when):
    """True when this cycle reads the self chat for the work replies: the deadline post is due today (from nudge_hour on) and not posted
    yet, or one was posted less than NOTICE_HOURS ago for a case that is not done. False when promises=0."""
    if not enabled():
        return False
    local = when.astimezone()
    if data.get(NOTICE_DAY) != local.date().isoformat() and local.hour >= nudge_hour() and due_rows(data, local.date()):
        return True
    for _, _, progress in _open_items(data):
        posted = _aware(progress.get("notice_at"))
        if posted is not None and timedelta(0) <= local - posted <= timedelta(hours=NOTICE_HOURS):
            return True
    return False


def due_notice(data, post, when):
    """The deadline post of the day: one a day, from nudge_hour on, that lists the cases due (due_rows) by number. The day and each listed
    case's notice number are set before `post(text)` runs (the order of sent.nudge), so a run that stops in between never posts it twice.
    Returns the numbers listed; [] writes nothing (promises=0, already posted today, nothing due, or too early)."""
    if not enabled():
        return []
    local = when.astimezone()
    today = local.date()
    if data.get(NOTICE_DAY) == today.isoformat() or local.hour < nudge_hour():
        return []
    rows = due_rows(data, today)
    if not rows:
        return []
    stamp = local.astimezone(timezone.utc).isoformat(timespec="seconds")
    for n, _ in rows:
        item = data["items"][n]
        progress = dict(item["work"]) if isinstance(item.get("work"), dict) else {}
        progress["notice_seq"] = int(progress.get("notice_seq") or 0) + 1
        progress["notice_at"] = stamp
        progress["notice_day"] = today.isoformat()
        item["work"] = progress
    data[NOTICE_DAY] = today.isoformat()
    post(_notice_text(rows))
    return [int(n) for n, _ in rows]


def due_today_numbers(out, when=None):
    """The numbers of the cases whose deadline post was made today (push.py: the numbers only). [] when promises=0."""
    if not enabled():
        return []
    today = (when or datetime.now(timezone.utc)).astimezone().date().isoformat()
    ap = notify.Approvals(out)
    return sorted(int(n) for n, _, progress in _open_items(ap.data) if progress.get("notice_day") == today)


def _reply_key(line):
    return hashlib.sha1(unicodedata.normalize("NFKC", str(line)).strip().encode("utf-8")).hexdigest()[:12]


def _deadline(word, now):
    """The day that `延期 N <いつ>` moves the case to (the words hold.parse_when reads: 明日, 金, 来週, M/D, N日後 ...), or None."""
    when = hold.parse_when(word, now, keep_days=366)
    return when.date() if when is not None else None


def apply_reply(out, ap, number, parsed, line, now):
    """One work reply (notify.parse_work) of an approved case, read below a visible post of it. It counts once per deadline post: the
    case's `acked` keeps the reply's key with the notice number it was read under, so a reply that stays on the screen after the CLI
    changed the case does not change it again. A reply to a case that is not approved, or to a step that does not exist, changes nothing.
    The caller holds approvals.lock. Returns "updated", "unchanged" or None (nothing was done)."""
    item = ap.data.get("items", {}).get(str(number))
    if not isinstance(item, dict) or item.get("status") != "approved" or not item.get("posted") or learn.is_card(item):
        return None
    word, _, arg = parsed
    progress = item.get("work") if isinstance(item.get("work"), dict) else {}
    seq = int(progress.get("notice_seq") or 0)
    key = _reply_key(line)
    acked = progress.get("acked") if isinstance(progress.get("acked"), dict) else {}
    if acked.get(key) == seq:
        return None
    history_path = Path(out) / "work-state.jsonl"
    if word == "着手":
        result = _apply_state(ap, history_path, number, "in_progress", now=now)
    elif word == "完了" and arg is None:
        result = _apply_state(ap, history_path, number, "done", now=now)
    elif word == "完了":
        steps = _steps(item)
        if not 1 <= arg <= len(steps) or not isinstance(steps[arg - 1], dict):
            return None
        result = _apply_step(ap, history_path, number, steps[arg - 1]["id"], now)
    else:   # 延期: the case's day becomes that absolute day, so the words are not read again on a later cycle
        day = _deadline(arg, now)
        if day is None:
            return None
        result = _apply_state(ap, history_path, number, None, due=day.isoformat(), now=now)
    current = ap.data["items"][str(number)]
    progress = dict(current["work"]) if isinstance(current.get("work"), dict) else {}
    progress["acked"] = {**(progress.get("acked") if isinstance(progress.get("acked"), dict) else {}), key: seq}
    current["work"] = progress
    ap.save()
    return result
