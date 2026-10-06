"""Local trial evidence capture and a privacy-safe aggregate report."""
import argparse
import json
import math
import time
from datetime import datetime
from statistics import median
from pathlib import Path

from . import fsutil, notify, onboarding, review

_FILE = "trial.json"


def _path(out):
    return Path(out) / _FILE


def _read(out):
    data = fsutil.read_json(_path(out), {"samples": []})
    return data if isinstance(data, dict) and isinstance(data.get("samples"), list) else {"samples": []}


def _write(out, data):
    fsutil.write_atomic(_path(out), json.dumps(data, ensure_ascii=False))


def _minutes(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("minutes must be finite and nonnegative")
    return number


def _case_exists(out, case):
    ap = notify.Approvals(out)
    return str(case) in ap.data.get("items", {})


def _judge_group(out, case, item=None):
    if item is None:
        item = notify.Approvals(out).data.get("items", {}).get(str(case), {})
    name = review.judge_of(item.get("record") or {})
    if name == "Jev":
        return "jev"
    if name in ("", "unknown"):
        return "unknown"
    return "non_jev"


def dispatch(argv, out, print_fn=print, now=None):
    parser = argparse.ArgumentParser(prog="trial")
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record")
    record.add_argument("--case", type=int, required=True)
    record.add_argument("--before-minutes", type=_minutes)
    record.add_argument("--after-minutes", type=_minutes, required=True)
    record.add_argument("--draft", choices=("unchanged", "edited", "unused"), required=True)
    record.add_argument("--outcome", choices=("correct", "wrong", "unknown"), required=True)
    report = sub.add_parser("report")
    report.add_argument("--share", action="store_true")
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as e:
        return int(e.code)
    if args.command == "record":
        item = notify.Approvals(out).data.get("items", {}).get(str(args.case))
        if args.case < 1 or not isinstance(item, dict):
            print_fn("失敗: 指定された case はありません")
            return 1
        timing = item.get("measurement") if isinstance(item.get("measurement"), dict) else {}
        reference = item.get("record", {}).get("measurement")
        reference = reference if isinstance(reference, dict) else {}
        sample = {"case": args.case, "before_minutes": args.before_minutes,
                  "after_minutes": args.after_minutes, "draft": args.draft,
                  "outcome": args.outcome, "judge_group": _judge_group(out, args.case, item),
                  "at": time.time() if now is None else now,
                  "measurement_source": reference.get("source", "legacy"),
                  "proposal_generation": timing.get("generation")}
        with fsutil.exclusive(Path(out) / "trial.lock") as locked:
            if not locked:
                print_fn("失敗: trial の記録が使用中です")
                return 1
            data = _read(out)
            data["samples"].append(sample)
            _write(out, data)
        print_fn("trial の記録を保存しました")
        return 0
    if args.command == "report":
        print_fn(render(collect(out, share=args.share), share=args.share))
        return 0
    return 2


def _finite_nonnegative(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _work_evidence(out):
    try:
        from . import work
        evidence = work.evidence(out)
    except (ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None
    keys = ("approved_cases", "tracked_cases", "unknown_progress_cases", "handoff_verified_cases", "handoff_unknown_cases")
    if not isinstance(evidence, dict) or any(not isinstance(evidence.get(key), int) or evidence[key] < 0 for key in keys):
        return None
    states = evidence.get("states")
    if not isinstance(states, dict) or any(not isinstance(states.get(key), int) or states[key] < 0
                                           for key in ("approved", "in_progress", "done", "blocked")):
        return None
    if (evidence["tracked_cases"] + evidence["unknown_progress_cases"] != evidence["approved_cases"]
            or sum(states[key] for key in ("approved", "in_progress", "done", "blocked")) != evidence["tracked_cases"]):
        return None
    return evidence


def collect(out, share=False):
    source_rows = _read(out)["samples"]
    # Shared output omits Jev and unknown-provenance samples, including their duration and draft-use data.
    if share:
        source_rows = [r for r in source_rows if isinstance(r, dict) and r.get("judge_group") == "non_jev"]
    rows = []
    invalid = 0
    synthetic = 0
    for row in source_rows:
        if isinstance(row, dict) and row.get("measurement_source") == "demo":
            synthetic += 1
            continue
        valid = (isinstance(row, dict) and isinstance(row.get("case"), int) and not isinstance(row.get("case"), bool)
                 and row["case"] > 0 and _finite_nonnegative(row.get("after_minutes"))
                 and _finite_nonnegative(row.get("at"))
                 and row["at"] <= time.time()
                 and row.get("draft") in ("unchanged", "edited", "unused")
                 and row.get("outcome") in ("correct", "wrong", "unknown")
                 and (row.get("before_minutes") is None or _finite_nonnegative(row.get("before_minutes"))))
        if valid:
            rows.append(row)
        else:
            invalid += 1
    distinct_cases = {r.get("case") for r in rows if isinstance(r.get("case"), int) and r.get("case") > 0}
    paired = [{"before": r["before_minutes"], "after": r["after_minutes"],
               "delta": r["after_minutes"] - r["before_minutes"]}
              for r in rows if isinstance(r.get("before_minutes"), (int, float))]
    deltas = [pair["delta"] for pair in paired]
    paired_delta_summary = None
    if deltas:
        count = len(deltas)
        paired_delta_summary = {"count": count,
                                "mean": math.fsum(delta / count for delta in deltas),
                                "min": min(deltas), "max": max(deltas)}
    paired_cases = {r["case"] for r in rows if isinstance(r.get("before_minutes"), (int, float))}
    drafts = {k: sum(r.get("draft") == k for r in rows) for k in ("unchanged", "edited", "unused")}
    outcomes = {k: sum(r.get("outcome") == k for r in rows) for k in ("correct", "wrong", "unknown")}
    challenges = onboarding._read(out).get("challenges", [])
    receipt_count = sum(bool(c.get("receipt_confirmed")) for c in challenges)
    work = _work_evidence(out)
    if work is None:
        handoff = "Unknown (handoff evidence unavailable)"
        completion = "Unknown (work evidence unavailable)"
        approved = 0
    else:
        handoff = (f"{work['handoff_verified_cases']} confirmed copy-ready self-chat handoffs; "
                   f"{work['handoff_unknown_cases']} cases with ambiguous handoff evidence; "
                   "approval-to-handoff elapsed time Unknown")
        states = work["states"]
        if work["approved_cases"] == 0:
            completion = "0 approved work cases"
        else:
            completion = (f"done {states['done']} / tracked {work['tracked_cases']} / approved {work['approved_cases']}; "
                          f"progress Unknown for {work['unknown_progress_cases']} untracked cases "
                          f"(approved {states['approved']}, in_progress {states['in_progress']}, blocked {states['blocked']})")
        approved = work["approved_cases"]
    return {"share_safe": share, "sample_count": len(rows), "invalid_samples": invalid, "synthetic_samples": synthetic, "paired_deltas": paired,
            "paired_delta_summary": paired_delta_summary,
            "baseline_missing": len(rows) - len(paired), "drafts": drafts, "outcomes": outcomes,
            "distinct_case_count": len(distinct_cases), "paired_case_count": len(paired_cases),
            "effect": "inconclusive" if len(paired_cases) < 5 else "observed",
            "approval_count": approved, "approval_to_handoff": handoff,
            "work_completion": completion, "notification_receipts": receipt_count,
            "measurement": measurement_collect(out, rows, share=share)}


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _interval(timing, start, end, now):
    left, right = _timestamp(timing.get(start)), _timestamp(timing.get(end))
    if left is None or right is None or not 0 <= left <= right <= now:
        return None
    return right - left


def _coverage(values):
    measured = [v for v in values if v is not None]
    return {"eligible": len(values), "measured": len(measured), "unknown": len(values) - len(measured),
            "median_seconds": median(measured) if measured else None}


def _eligible_draft(rec):
    for action in rec.get("actions") or []:
        if not isinstance(action, dict):
            continue
        kind = action.get("type")
        text = action.get(notify.writer_mod.FIELD.get(kind)) if isinstance(kind, str) else None
        if isinstance(text, str) and text.strip() and not action.get("held_for"):
            return True
    return False


def _share_judge(rec):
    return review.judge_of(rec) not in ("Jev", "unknown", "")


def measurement_valid(item, timing, reference):
    """Join telemetry to one proposal without using it for lifecycle decisions."""
    if not isinstance(timing, dict) or not isinstance(reference, dict):
        return False
    generation = timing.get("generation")
    reference_generation = reference.get("generation")
    return (timing.get("schema") == reference.get("schema") == 1
            and isinstance(generation, int) and not isinstance(generation, bool) and generation > 0
            and isinstance(reference_generation, int) and not isinstance(reference_generation, bool)
            and generation == reference_generation
            and timing.get("source") in ("local", "demo", "legacy")
            and timing.get("source") == reference.get("source")
            and isinstance(item.get("key"), str) and bool(item["key"])
            and timing.get("case_key") == item["key"])


def measurement_collect(out, observations, share=False):
    """Only aggregates leave this function. No implicit trial or actual-arrival evidence."""
    now = time.time()
    items = notify.Approvals(out).data.get("items", {})
    groups = {source: {"delivery_wait": [], "approval_latency": [], "total_case_wait": [], "pending_age": [],
                       "processing": [], "ambiguous_delivery": 0, "human_delivery_confirmations": 0}
              for source in ("local", "demo")}
    eligible, valid_cases, readiness, sources = {}, set(), {}, {}
    for case, item in items.items():
        if not isinstance(item, dict):
            continue
        rec = item.get("record") if isinstance(item.get("record"), dict) else {}
        if share and not _share_judge(rec):
            continue
        timing = item.get("measurement") if isinstance(item.get("measurement"), dict) else {}
        reference = rec.get("measurement") if isinstance(rec.get("measurement"), dict) else {}
        source = "demo" if reference.get("source") == "demo" else "local"
        group = groups[source]
        generation = timing.get("generation")
        valid = measurement_valid(item, timing, reference)
        confirmed = valid and timing.get("posting_source") == "bridge_readback" and not item.get("delivery_unknown")
        group["delivery_wait"].append(_interval(timing, "ready_at", "posted_at", now) if confirmed else None)
        if item.get("status") == "approved":
            group["approval_latency"].append(_interval(timing, "posted_at", "approval_observed_at", now) if confirmed else None)
            group["total_case_wait"].append(_interval(timing, "first_ready_at", "approval_observed_at", now) if confirmed else None)
        elif item.get("status") in ("pending", "held"):
            posted = _timestamp(timing.get("posted_at"))
            group["pending_age"].append(now - posted if confirmed and posted is not None and 0 <= posted <= now else None)
        group["ambiguous_delivery"] += bool(item.get("delivery_unknown"))
        group["human_delivery_confirmations"] += timing.get("posting_source") == "user_confirmation"
        if source == "local" and _eligible_draft(rec):
            eligible[str(case)] = generation
            if valid:
                valid_cases.add(str(case))
                readiness[str(case)] = _timestamp(timing.get("ready_at"))
                sources[str(case)] = timing.get("source")
    for rec in review.read_jsonl(Path(out) / "decisions.jsonl"):
        if not isinstance(rec, dict):
            continue
        if share and not _share_judge(rec):
            continue
        metadata = rec.get("measurement") if isinstance(rec.get("measurement"), dict) else {}
        source = "demo" if metadata.get("source") == "demo" else "local"
        perf = rec.get("perf") if isinstance(rec.get("perf"), dict) else {}
        seconds = [perf.get("judge_sec"), perf.get("writer_sec")]
        value = sum(seconds) if all(_finite_nonnegative(v) for v in seconds) else None
        if not _finite_nonnegative(value) or _timestamp(rec.get("at")) is None or _timestamp(rec.get("at")) > now:
            value = None
        groups[source]["processing"].append(value)
    latest, explicit = {}, 0
    for row in observations:
        case = str(row["case"])
        generation = row.get("proposal_generation")
        ready = readiness.get(case)
        if (case not in valid_cases or row.get("measurement_source") != sources.get(case)
                or not isinstance(generation, int) or isinstance(generation, bool) or generation != eligible.get(case)
                or ready is None or not 0 <= ready <= row["at"] <= now
                or (share and row.get("judge_group") != "non_jev")):
            continue
        explicit += 1
        if case not in latest or latest[case]["at"] <= row["at"]:
            latest[case] = row
    used = sum(r["draft"] in ("unchanged", "edited") for r in latest.values())
    edited = sum(r["draft"] == "edited" for r in latest.values())
    result = {"eligible_draft_cases": len(eligible), "adoption_observed_cases": len(latest),
              "adoption_unknown_cases": len(eligible) - len(latest), "repeat_observations": explicit - len(latest),
              "adoption_rate": used / len(latest) if latest else None,
              "correction_rate": edited / used if used else None,
              "adopted_cases": used, "edited_cases": edited, "arrival_to_approval": None}
    for source, group in groups.items():
        result[source] = {key: _coverage(value) if isinstance(value, list) else value for key, value in group.items()}
    return result


def measurement_lines(data):
    lines = ["Manual review: explicit active minutes; excludes unattended waiting; descriptive, not causal",
             "Event-arrival-to-approval: Unknown (reliable arrival evidence not recorded)"]
    for source in ("local", "demo"):
        group = data[source]
        label = "synthetic demo (not pilot/business evidence)" if source == "demo" else "local observations"
        for key, name in (("processing", "model/writer component runtime"), ("delivery_wait", "Draft delivery wait"),
                          ("approval_latency", "Observed approval latency (includes reply polling and holds)"),
                          ("total_case_wait", "First-ready-to-approval case wait"), ("pending_age", "Pending age (open interval)")):
            value = group[key]
            seconds = "Unknown" if value["median_seconds"] is None else f"{value['median_seconds']:.3f}s"
            lines.append(f"{label}: {name}: median {seconds}; measured {value['measured']} / eligible {value['eligible']}; Unknown {value['unknown']}")
        lines.append(f"{label}: ambiguous delivery {group['ambiguous_delivery']}; human delivery confirmations {group['human_delivery_confirmations']} (confirmation time, not original posting time)")
    rate = "Unknown" if data["adoption_rate"] is None else f"{data['adoption_rate']:.1%}"
    correction = "Unknown" if data["correction_rate"] is None else f"{data['correction_rate']:.1%}"
    lines += [f"Explicit draft adoption: {rate}; observed {data['adoption_observed_cases']} / eligible {data['eligible_draft_cases']}; Unknown {data['adoption_unknown_cases']}; repeat observations {data['repeat_observations']}",
              f"Correction among adopted drafts: {correction}; edited {data['edited_cases']} / adopted {data['adopted_cases']}"]
    return lines


def render(data, share=False):
    if share and not data.get("share_safe"):
        return "shared trial report unavailable: collect with share=True to exclude Jev and unknown provenance"
    lines = ["data source: explicit manual trial observations; descriptive, not causal",
             f"trial records: {data['sample_count']} / distinct cases: {data['distinct_case_count']}",
             f"review duration pairs: {data['paired_case_count']} distinct paired cases / {len(data['paired_deltas'])} explicit pairs"]
    summary = data.get("paired_delta_summary")
    if share:
        if summary:
            lines.append(f"aggregate paired delta: mean delta {summary['mean']:g} minutes; "
                         f"range {summary['min']:g} to {summary['max']:g} minutes")
        else:
            lines.append("aggregate paired delta: Unknown (no complete duration pairs)")
    else:
        for i, pair in enumerate(data["paired_deltas"], 1):
            lines.append(f"  pair {i}: {pair['before']:g} -> {pair['after']:g} minutes (delta {pair['delta']:+g})")
    lines.append(f"missing baseline: {data['baseline_missing']} (duration delta: Unknown)")
    if data.get("invalid_samples"):
        lines.append(f"invalid trial records excluded: {data['invalid_samples']}")
    if data.get("synthetic_samples"):
        lines.append(f"synthetic trial observations excluded from business/manual comparison: {data['synthetic_samples']}")
    lines.append("drafts: " + ", ".join(f"{k} {v}" for k, v in data["drafts"].items()))
    scope = "non-Jev only; Jev/unknown provenance omitted" if share else "all local provenance"
    lines.append("case outcomes: " + ", ".join(f"{k} {v}" for k, v in data["outcomes"].items()) + f" ({scope})")
    lines.append("effect: " + ("inconclusive (fewer than 5 distinct paired cases with before and after)"
                               if data["effect"] == "inconclusive" else "sample threshold met (descriptive only; not causal)"))
    lines.append(f"approval status records: {data['approval_count']}")
    lines.append("approval-to-handoff: " + data["approval_to_handoff"])
    lines.append("work completion: " + data["work_completion"])
    lines.append(f"notification receipt: {data['notification_receipts']} challenge confirmations (each confirms at least 1 route)")
    if data.get("measurement"):
        lines.extend(measurement_lines(data["measurement"]))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kimeru trial")
    parser.add_argument("--out", default="out")
    args, rest = parser.parse_known_args(argv)
    return dispatch(rest, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
