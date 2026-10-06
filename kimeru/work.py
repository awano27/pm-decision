"""Explicit progress tracking for approved cases; approval and execution stay independent."""
import json
import os
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from . import fsutil, notify

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


def set_state(out, case, state, *, owner=None, due=None, completion_condition=None, now=None):
    """Set an explicitly approved case's progress and optional metadata under the approvals lock."""
    if state not in STATES:
        raise WorkError(f"state must be one of: {', '.join(STATES)}")
    if due is not None:
        try:
            if date.fromisoformat(due).isoformat() != due:
                raise ValueError
        except (TypeError, ValueError):
            raise WorkError("due must be a calendar date in YYYY-MM-DD form") from None
    with fsutil.exclusive(notify.lock_path(out)) as got:
        if not got:
            raise WorkError("approvals are busy; retry later")
        ap = notify.Approvals(out)
        history_path = Path(out) / "work-state.jsonl"
        _recover_pending(ap, history_path)
        item = ap.data.get("items", {}).get(str(case))
        if not item or item.get("status") != "approved":
            raise WorkError(f"case #{case} is not explicitly approved")
        previous = item.get("work") if isinstance(item.get("work"), dict) else {}
        previous_state = previous.get("state") if previous.get("state") in STATES else "Unknown"
        entry = dict(previous)
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
        entry["updated_at"] = at
        item["work"] = entry
        row = {"transition_id": uuid.uuid4().hex, "at": at, "case": str(case), "from": previous_state,
               "to": state, "owner": entry.get("owner"), "due": entry.get("due"),
               "completion_condition": entry.get("completion_condition")}
        ap.data.setdefault("work_history_pending", []).append(row)
        try:
            ap.save()
        except OSError as exc:
            raise WorkError(f"progress was not saved; retry work set ({type(exc).__name__})") from None
        _recover_pending(ap, history_path)
    return "updated"


def _next_action(record):
    memo = record.get("memo") or {}
    plan = record.get("plan") or {}
    return memo.get("next") or plan.get("summary") or record.get("advice") or "Unknown"


def list_work(out):
    """Approved cases with explicit tracked fields; absent progress is exposed as Unknown."""
    ap = notify.Approvals(out)
    rows = []
    for number, item in ap.data.get("items", {}).items():
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
    """Aggregate approved work and confirmed/ambiguous copy-ready handoffs without text or identifiers."""
    ap = notify.Approvals(out)
    approved = [item for item in ap.data.get("items", {}).values() if item.get("status") == "approved"]
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
