"""`kimeru digest --week [--share]`: the last seven days in numbers.

--share is meant to be pasted into a public issue: numbers and environment facts only. No message text, subject, name,
id or path ever enters it. Jev's decisions are left out of the agreement rate, and the time line is not shown when Jev
is among the judges (TypeSafe's terms do not allow publishing Jev's performance).
"""
import json
import platform
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from . import __version__, autorun, calibrate, config, learn, profiles, review
from .backends import JevBackend

HERE = Path(__file__).resolve().parent.parent


def _parse(ts):
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _within(rows, key, since, now=None):
    now = now or datetime.now(timezone.utc)
    out = []
    for r in rows:
        d = _parse(r.get(key))
        if d and since <= d <= now:
            out.append(r)
    return out


def _date_issues(rows, key, now, include_missing=True):
    """Count records that cannot support a weekly date-based metric."""
    invalid = future = 0
    for row in rows:
        raw = row.get(key)
        if not raw:
            invalid += bool(include_missing)
            continue
        parsed = _parse(raw)
        if parsed is None:
            invalid += 1
        elif parsed > now:
            future += 1
    return invalid, future


def _rule_only(rec):
    path = rec.get("path") or []
    return all("matched" in (s.get("answer") or {}) for s in path)


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


def _normalise_judge_name(name):
    if not isinstance(name, str) or not name.strip():
        return "unknown"
    name = name.strip()
    if name.casefold() == "unknown":
        return "unknown"
    if name.casefold() == "jev":
        return "Jev"
    return name


def _record_judge_name(row):
    judge = row.get("judge")
    if not isinstance(judge, dict):
        return "unknown"
    return _normalise_judge_name(judge.get("name"))


def _record_model_name(row):
    judge = row.get("judge")
    if not isinstance(judge, dict):
        return None
    model = judge.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


def _summary_judges(value):
    """Return normalized legacy summary names and whether every name is usable."""
    if not isinstance(value, (list, tuple, set)):
        return ["unknown"], False
    names, valid = set(), True
    for item in value:
        if not isinstance(item, str) or not item.strip():
            names.add("unknown")
            valid = False
            continue
        name = item.strip()
        names.add(name)
        if name.casefold() in ("jev", "unknown"):
            valid = False
    return sorted(names), valid and bool(names)


def perf_line(rows):
    """One line: what one event cost (calls to the judge, questions, seconds incl. the writer): median and max."""
    # Every row contributes to the aggregate, so one missing/unknown or Jev
    # judgment makes the combined performance figure unsafe to display.
    if any(_record_judge_name(r).casefold() in ("jev", "unknown") for r in rows):
        return ""
    ps = [r["perf"] for r in rows if r.get("perf")]
    if not ps:
        return ""
    secs = [p.get("judge_sec", 0) + p.get("writer_sec", 0) for p in ps]
    return (f"1 イベントあたり（{len(ps)} 件）: 判断の呼び出し 中央値 {_median([p.get('calls', 0) for p in ps])} 回・"
            f"質問 中央値 {_median([p.get('questions', 0) for p in ps])} 問（最大 {max(p.get('questions', 0) for p in ps)}）・"
            f"時間 中央値 {_median(secs):.1f} 秒・最大 {max(secs):.1f} 秒（writer を含む）")


def _judge_of_review(rev, judge_of_key):
    raw = rev.get("judge")
    if isinstance(raw, str) and raw.strip():
        return _normalise_judge_name(raw)
    if raw is not None and not isinstance(raw, str):
        return "unknown"
    # Older review rows may lack a judge; use the linked decision when present.
    return _normalise_judge_name(judge_of_key.get(rev.get("key")))


def bundled_graph_names():
    """The names of the graphs that ship with kimeru: the only graph names --share may show."""
    names = set()
    for p in (Path(__file__).resolve().parent.parent / "graphs").glob("*.json"):
        try:
            names.add(json.loads(p.read_text(encoding="utf-8"))["name"])
        except (OSError, ValueError, KeyError):
            continue
    return names


def _repeated_instructions(rows):
    """Revision instructions written again on another item: for each fingerprint, the items it came up on, minus one."""
    items = {}
    for r in rows:
        sig, key = r.get("instruction_sig"), r.get("key")
        if sig and key:
            items.setdefault(str(sig), set()).add(str(key))
    return sum(len(keys) - 1 for keys in items.values())


def _span(rows, key, start, end):
    """Records with start <= time < end: the week before the one `collect` counts, so the two weeks share no record."""
    out = []
    for r in rows:
        d = _parse(r.get(key))
        if d and start <= d < end:
            out.append(r)
    return out


def _dicts(rows):
    """The records that are objects: a damaged line (a number, a list) is left out, not an error."""
    return [r for r in rows if isinstance(r, dict)]


def _performance_judge(name):
    """Only a judge that is named and is not Jev counts in a performance figure (the agreement rate, the wrong answers, the OKs)."""
    return name not in (JevBackend.NAME, "unknown")


def _count_note(n):
    """What a figure made of n records says: 1 to 4 is a reference only; 5 and more is the figure (0 is shown as Unknown)."""
    return "・参考" if n < 5 else ""


def _approval_items(out):
    """The items of approvals.json (read only: the Approvals class writes nothing when it is loaded)."""
    from . import notify
    data = notify.Approvals(out).data
    items = data.get("items") if isinstance(data, dict) else None
    return learn.cases(items) if isinstance(items, dict) else {}   # R21: a learn card is no case


def _unchanged_oks(out, week_replies):
    """(OKs of items with a draft, of them the ones whose proposal never changed (generation 1)). The draft rule is trial's; the
    demo, the fictional item and Jev or an unnamed judge are left out, as the agreement rate leaves them out."""
    from . import trial
    items = _approval_items(out)
    draft = unchanged = 0
    for r in week_replies:
        if r.get("status") != "approved":
            continue
        item = items.get(str(r.get("id")))
        if not isinstance(item, dict) or item.get("key") != r.get("key"):
            continue
        rec = item.get("record") if isinstance(item.get("record"), dict) else {}
        timing = r.get("measurement") if isinstance(r.get("measurement"), dict) else {}
        reference = rec.get("measurement") if isinstance(rec.get("measurement"), dict) else {}
        if timing.get("source") == "demo" or reference.get("source") == "demo":
            continue
        if autorun._key_is_sample(r.get("key")) or autorun.is_sample(rec.get("event_kind"), rec.get("event_id")):
            continue
        if not _performance_judge(_record_judge_name(rec)) or not trial._eligible_draft(rec):
            continue
        draft += 1
        generation = timing.get("generation")
        unchanged += isinstance(generation, int) and not isinstance(generation, bool) and generation == 1
    return draft, unchanged


def _calibration(out):
    """Labeled questions per judge that `calibrate` can tune (Jev left out). The graphs that ship with kimeru are read, the model
    is not called; None when they cannot be read (then the line says Unknown)."""
    from . import graph, plan
    try:
        graphs = graph.load_dir(HERE / "graphs", plan.load_playbooks(HERE / "playbooks"))
        questions = calibrate.labeled_questions(out, graphs)
    except Exception:   # a damaged shipped file or review row: the line says Unknown, and nothing is changed
        return None
    counts = {name: 0 for name in profiles.JUDGE_PROFILE if name != JevBackend.NAME}
    for q in questions:
        if q.get("judge") in counts:
            counts[q["judge"]] += 1
    return counts


def _week_extra(out, decision_rows, decisions, week_replies, review_rows, now, since):
    """What the week digest adds: the week before, the kinds that arrived, the automatic decisions a review called wrong, the replies
    per item, the OKs whose proposal never changed, the weekly medians of the approval latency, and what calibrate still needs.
    Only files are read; no model is called. Jev's decisions and those of an unnamed judge are counted in the totals and left out
    of every performance figure."""
    from . import trial
    prev_since = since - timedelta(days=7)
    previous = _span(decision_rows, "at", prev_since, since)
    counted = [r for r in decisions if _performance_judge(_record_judge_name(r))]
    auto_keys = {review.decision_key(r) for r in counted if not r.get("needs_human") and not r.get("demoted_by")}   # R33: a rule's line is counted apart (rule_recorded)
    wrong_keys = {r.get("key") for r in review_rows if r.get("verdict") == "wrong"}
    replies = {}
    for r in week_replies:
        key = r.get("key")
        if isinstance(key, str) and key and not autorun._key_is_sample(key):
            replies[key] = replies.get(key, 0) + 1
    draft, unchanged = _unchanged_oks(out, week_replies)
    arrivals = {}
    for r in decisions:
        kind = r.get("event_kind")
        label = autorun.KINDS.get(kind, "その他") if isinstance(kind, str) else "その他"
        arrivals[label] = arrivals.get(label, 0) + 1
    latencies = trial.approval_latencies(out, now=now.timestamp())
    since_ts, now_ts, prev_ts = since.timestamp(), now.timestamp(), prev_since.timestamp()
    return {
        "previous": {"since": prev_since.date().isoformat(), "decisions": len(previous),
                     "auto": sum(not r.get("needs_human") for r in previous),
                     "human": sum(bool(r.get("needs_human")) for r in previous)},
        "arrivals": arrivals,
        "jev_decisions": sum(_record_judge_name(r) == JevBackend.NAME for r in decisions),
        "unnamed_decisions": sum(_record_judge_name(r) == "unknown" for r in decisions),
        "auto_checked": len(auto_keys), "auto_wrong": len(auto_keys & wrong_keys),
        **({"rule_recorded": sum(1 for r in counted if r.get("demoted_by"))} if any(r.get("demoted_by") for r in counted) else {}),   # R33 (only when a rule recorded a line)
        "replied_items": len(replies), "replies": sum(replies.values()),
        "oks_with_draft": draft, "oks_unchanged": unchanged,
        "latency": {"now": [s for t, s in latencies if since_ts <= t <= now_ts],
                    "previous": [s for t, s in latencies if prev_ts <= t < since_ts]},
        "calibration": _calibration(out),
        # R12: the decisions that waited only for a writer's draft (post_kind_mark=1; None at 0, the digest is then unchanged)
        "drafts": (sum(1 for r in decisions if r.get("needs_human") and r.get("held_reason") == "draft")
                   if config.value("post_kind_mark") == "1" else None),
        # R24: the six rows of the second stage (read only; Jev and unnamed judges left out of every one)
        "report": _report(out, decision_rows, review_rows, now, since),
    }


# --- R24: the second stage of the week digest, and the six lines of the Monday brief (weekly_report=1). Read only: nothing is
# written, no model is called, and a figure leaves out Jev, the demo, the fictional item and an unnamed judge.

WEEK = timedelta(days=7)


def _local_day(value):
    d = _parse(value)
    return d.astimezone().date().isoformat() if d is not None else None


def _sample_or_demo_item(item):
    """True for the trial's fictional item (by its key or its event) and for a demo item: no R24 row counts them."""
    rec = item.get("record") if isinstance(item.get("record"), dict) else {}
    timing = item.get("measurement") if isinstance(item.get("measurement"), dict) else {}
    reference = rec.get("measurement") if isinstance(rec.get("measurement"), dict) else {}
    return (timing.get("source") == "demo" or reference.get("source") == "demo" or autorun._key_is_sample(item.get("key"))
            or autorun.is_sample(rec.get("event_kind"), rec.get("event_id")))


def _sample_or_demo_decision(row):
    """The same rule for a line of decisions.jsonl: a demo decision, or the fictional item's."""
    metadata = row.get("measurement") if isinstance(row.get("measurement"), dict) else {}
    return metadata.get("source") == "demo" or autorun.is_sample(row.get("event_kind"), row.get("event_id"))


def _counted_cases(out):
    """(item, timing, reference) of the approval items that count in the R24 rows: a case (no learn card), not the demo, not the
    fictional item, and a judge that is named and is not Jev."""
    found = []
    for item in _approval_items(out).values():
        if not isinstance(item, dict) or _sample_or_demo_item(item):
            continue
        rec = item.get("record") if isinstance(item.get("record"), dict) else {}
        timing = item.get("measurement") if isinstance(item.get("measurement"), dict) else {}
        reference = rec.get("measurement") if isinstance(rec.get("measurement"), dict) else {}
        if _performance_judge(_record_judge_name(rec)):
            found.append((item, timing, reference))
    return found


def _arrival(cases, since, now):
    """R01: from the time the PC saw the event. Judged: the first ready time falls in the week. Approved: the OK was seen in the week.
    A missing arrival, or one after the moment counted, is Unknown and never estimated (the rule of trial's medians)."""
    from . import trial
    end = now.timestamp()
    judged, approved = [], []
    for item, timing, reference in cases:
        valid = trial.measurement_valid(item, timing, reference)
        ready = _parse(timing.get("first_ready_at"))
        if ready is not None and since <= ready <= now:
            judged.append(trial._interval(timing, "arrived_at", "first_ready_at", end) if valid else None)
        seen = _parse(timing.get("approval_observed_at"))
        if item.get("status") == "approved" and seen is not None and since <= seen <= now:
            confirmed = valid and timing.get("posting_source") == "bridge_readback" and not item.get("delivery_unknown")
            approved.append(trial._interval(timing, "arrived_at", "approval_observed_at", end) if confirmed else None)
    return trial._coverage(judged), trial._coverage(approved)


def _sends(cases, since, now):
    """R14: the state the self-chat list showed after an OK, for the OKs whose send was seen this week (sent_watch=1). A reading of the
    list, not a receipt."""
    from . import sent
    found = {state: 0 for state in sent.STATES}
    for item, _timing, _reference in cases:
        seen = item.get("sent_seen") if isinstance(item.get("sent_seen"), dict) else {}
        state, at = seen.get("state"), _parse(seen.get("at"))
        if item.get("status") == "approved" and isinstance(state, str) and state in found and at is not None and since <= at <= now:
            found[state] += 1
    return found


def _labels(decision_rows, review_rows, since, now):
    """R03: the labels the week added, by where they came from: a reply (`違う N`, source teams) or `kimeru review`. A `違う` on the
    fictional item or on a demo decision is a label too, so those are left out here (as in the other R24 rows)."""
    judge_of_key = {review.decision_key(r): _record_judge_name(r) for r in decision_rows}
    left_out = {review.decision_key(r) for r in decision_rows if _sample_or_demo_decision(r)}
    found = {"teams": 0, "terminal": 0}
    for r in _within(review_rows, "at", since, now):
        if r.get("key") in left_out or autorun._key_is_sample(r.get("key")):
            continue
        if r.get("verdict") not in ("yes", "no", "wrong") or not _performance_judge(_judge_of_review(r, judge_of_key)):
            continue
        found["teams" if r.get("source") == "teams" else "terminal"] += 1
    return found


def _judged_between(rows, judge, start, end):
    """(decisions, sent to the PM) of one judge with start <= time < end, the demo and the fictional item left out."""
    total = human = 0
    for r in rows:
        at = _parse(r.get("at"))
        if at is None or not start <= at < end or _record_judge_name(r) != judge or _sample_or_demo_decision(r):
            continue
        total += 1
        human += bool(r.get("needs_human"))
    return total, human


def _learning(decision_rows, since, now):
    """R21: each adoption of the week that has not been taken back, against an equal window before and after its own time: the share of
    the judge's decisions that went to the PM. The window is seven days, or less when the adoption is newer; under one day it is not
    compared. Only a named judge other than Jev counts (the card's judge)."""
    ledger = learn._ledger()
    reverted = {str(r.get("id")) for r in ledger if r.get("kind") == "revert"}
    found = {"adoptions": 0, "comparable": 0, "before": [0, 0], "after": [0, 0]}
    for r in ledger:
        if r.get("kind") != "adopt" or str(r.get("id")) in reverted:
            continue
        when = learn._parse(r.get("adopted_at"))
        if when is None:
            continue
        when = when.astimezone() if when.tzinfo is None else when   # learn writes the local time without a zone
        judge = _normalise_judge_name(r.get("judge"))
        if not since <= when <= now or not _performance_judge(judge):
            continue
        found["adoptions"] += 1
        window = min(WEEK, now - when)
        if window < timedelta(days=1):
            continue
        found["comparable"] += 1
        for slot, start, end in (("before", when - window, when), ("after", when, when + window)):
            total, human = _judged_between(decision_rows, judge, start, end)
            found[slot][0] += total
            found[slot][1] += human
    return found


def _top_three(out, since, now):
    """R04: of the morning brief's top three rows that carry an approval number (the denominator), how many had a reply or a `work set`
    on the same local day as the brief (out/brief_history.jsonl x approvals.log.jsonl and work-state.jsonl). A weak signal: a look
    without an answer, or a reply made elsewhere, is in no log. A brief whose judge is Jev or unnamed is left out, and so is every row
    of the fictional item or a demo item (their approval numbers, in the brief, the reply log and the work log)."""
    from . import brief
    left_out = {n for n, item in _approval_items(out).items() if isinstance(item, dict) and _sample_or_demo_item(item)}
    replied = set()
    for r in _dicts(review.read_jsonl(Path(out) / "approvals.log.jsonl")):
        if learn.is_card_key(r.get("key")) or autorun._key_is_sample(r.get("key")) or str(r.get("id")) in left_out:
            continue
        day = _local_day(r.get("at"))
        if day is not None:
            replied.add((str(r.get("id")), day))
    for r in _dicts(review.read_jsonl(Path(out) / "work-state.jsonl")):
        day = _local_day(r.get("at"))
        if day is not None and str(r.get("case")) not in left_out:
            replied.add((str(r.get("case")), day))
    first, last = since.astimezone().date().isoformat(), now.astimezone().date().isoformat()
    numbered = hit = 0
    for day_row in _dicts(review.read_jsonl(brief.history_path(Path(out)))):
        date = day_row.get("date")
        if not isinstance(date, str) or not first <= date <= last:
            continue
        if not _performance_judge(_normalise_judge_name(day_row.get("backend"))):
            continue
        rows = day_row.get("rows") if isinstance(day_row.get("rows"), list) else []
        for row in rows:
            rank, key = (row.get("rank"), row.get("key")) if isinstance(row, dict) else (None, None)
            if not isinstance(rank, int) or isinstance(rank, bool) or not 1 <= rank <= 3:
                continue
            if not isinstance(key, str) or not key.startswith(brief.APPROVAL_PREFIX) or key[len(brief.APPROVAL_PREFIX):] in left_out:
                continue
            numbered += 1
            hit += (key[len(brief.APPROVAL_PREFIX):], date) in replied
    return {"numbered": numbered, "hit": hit}


def _report(out, decision_rows, review_rows, now, since):
    """The six rows' figures, computed once for the digest and for the Monday brief."""
    cases = _counted_cases(out)
    judged, approved = _arrival(cases, since, now)
    return {"judged": judged, "approved": approved, "sends": _sends(cases, since, now),
            "labels": _labels(decision_rows, review_rows, since, now),
            "learning": _learning(decision_rows, since, now), "top": _top_three(out, since, now)}


def _seconds_row(label, cov):
    if not cov["eligible"]:
        return f"{label}: 不明（記録なし）"
    if not cov["measured"]:
        return f"{label}: 不明（実測 0 件・不明 {cov['unknown']} 件）"
    return (f"{label}: 中央値 {cov['median_seconds']:.1f} 秒（実測 {cov['measured']} 件・不明 {cov['unknown']} 件"
            f"{_count_note(cov['measured'])}）")


def _share_of(human, total):
    return f"{human * 100 // total}%（{total} 件）" if total else "不明（0 件）"


def _report_lines(rep):
    """The six lines of the R24 rows, in this order. `digest --week` prints them, and the Monday brief gets the same six."""
    sends, labels, learning, top = rep["sends"], rep["labels"], rep["learning"], rep["top"]
    lines = [_seconds_row("到着から判断完了まで（PC が見た時刻から）", rep["judged"]),
             _seconds_row("到着から承認まで（PC が見た時刻から）", rep["approved"])]
    lines.append("OK の後の送信（自分のチャットの一覧で見た。届いたことの証明ではない）: " + (
        "不明（記録なし）" if not sum(sends.values()) else
        f"as_is {sends['as_is']} 件・edited {sends['edited']} 件・other {sends['other']} 件"))
    lines.append("正誤のラベルの増分（今週）: " + (
        "不明（記録なし）" if not labels["teams"] + labels["terminal"] else
        f"返信 {labels['teams']} 件・kimeru review {labels['terminal']} 件"))
    if not learning["adoptions"]:
        lines.append("採用した学習の前後（今週）: 不明（記録なし）")
    elif not learning["comparable"]:
        lines.append(f"採用した学習の前後（今週）: 採用 {learning['adoptions']} 件（比べるには採用から 1 日以上たつ必要があります）")
    else:
        lines.append(f"採用した学習の前後（今週）: 採用 {learning['adoptions']} 件。同じ長さの窓で PM に回った割合は、"
                     f"採用の前 {_share_of(learning['before'][1], learning['before'][0])}、"
                     f"後 {_share_of(learning['after'][1], learning['after'][0])}"
                     "（基準は採用の前の同じ長さの期間。kimeru はその間も動いていた）")
    if not top["numbered"]:
        lines.append("朝のまとめの上位 3 件で、同じ日に返信か作業の記録があった件: 不明（承認番号のある件 0 件）")
    else:
        lines.append(f"朝のまとめの上位 3 件（承認番号のある件 {top['numbered']} 件）で、同じ日に返信か作業の記録があった件: "
                     f"{top['hit']} 件（弱い信号。見ただけの件と、ほかの経路の返信は分からない）")
    return lines


def collect(out, now=None):
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    out = Path(out)
    decision_rows = _dicts(review.read_jsonl(out / "decisions.jsonl"))
    approval_rows = [r for r in _dicts(review.read_jsonl(out / "approvals.log.jsonl")) if not learn.is_card_key(r.get("key"))]   # R21: a card's OK is no approval
    review_rows = _dicts(review.read_jsonl(review.reviews_path()))
    decisions = _within(decision_rows, "at", since, now)
    dated = _within(approval_rows, "at", since, now)
    undated = sum(1 for r in approval_rows if not r.get("at"))
    judge_of_key = {review.decision_key(r): _record_judge_name(r) for r in decision_rows}
    reviews = _within(review_rows, "at", since, now)
    sources = ((decision_rows, True), (approval_rows, False), (review_rows, True))
    invalid_evidence = sum(_date_issues(rows, "at", now, include_missing=missing)[0] for rows, missing in sources)
    future_evidence = sum(_date_issues(rows, "at", now, include_missing=False)[1] for rows, _ in sources)
    per_graph = {}
    for r in decisions:
        g = per_graph.setdefault(r.get("graph", "?"), {"n": 0, "auto": 0, "human": 0, "rule": 0})
        g["n"] += 1
        g["human" if r.get("needs_human") else "auto"] += 1
        g["rule"] += _rule_only(r)
    statuses = {}
    for r in dated:
        statuses[r.get("status")] = statuses.get(r.get("status"), 0) + 1
    judges = sorted({_record_judge_name(r) for r in decisions})
    models = sorted({model for r in decisions if (model := _record_model_name(r))})
    # The agreement rate leaves Jev's decisions out, whatever the period or the output folder: a review names the judge that
    # made the decision; an older one is looked up in this folder's decisions, and when that is not possible (another folder,
    # a decision no longer here) it is not counted, because it cannot be shown not to be Jev's.
    counted = [r for r in reviews if _judge_of_review(r, judge_of_key) not in (JevBackend.NAME, "unknown")]
    yes = sum(r.get("verdict") == "yes" for r in counted)
    no = sum(r.get("verdict") == "no" for r in counted)
    wrong = sum(r.get("verdict") == "wrong" for r in counted)
    left_out = sum(r.get("verdict") in ("yes", "no") for r in reviews) - yes - no
    n = len(decisions)
    return {
        "since": since.date().isoformat(), "decisions": n,
        "auto": sum(g["auto"] for g in per_graph.values()), "human": sum(g["human"] for g in per_graph.values()),
        "rule": sum(g["rule"] for g in per_graph.values()),
        "critical": sum(1 for r in decisions if r.get("notify")),
        "per_graph": per_graph, "approvals": statuses, "approvals_undated": undated,
        "instruction_repeats": _repeated_instructions(dated),
        "invalid_evidence": invalid_evidence, "future_evidence": future_evidence,
        "judges": judges, "models": models, "reviewed_yes": yes, "reviewed_no": no,
        "reviewed_wrong": wrong, "reviewed_left_out": left_out,
        "perf": perf_line(decisions),
        "week": _week_extra(out, decision_rows, decisions, dated, review_rows, now, since),
    }


_DPI_PROBE = ("import ctypes; u = ctypes.windll.user32; f = getattr(u, 'SetProcessDpiAwarenessContext', None); "
              "f and f(ctypes.c_void_p(-4)); print(u.GetDpiForSystem())")   # -4 is a handle-sized value: a plain int is passed as 32 bits and fails on 64-bit Python


def _dpi_percent():
    """The display scale of the primary screen in percent, or None when it cannot be told (then it is left out, not guessed).
    A plain call to GetDpiForSystem in this process says 96 (100%) at any scale, because the process is not DPI aware; the
    setting Windows applied is in the registry, and a child process that declares itself DPI aware is the fallback."""
    try:
        import winreg
    except ImportError:
        return None
    for sub, name in ((r"Control Panel\Desktop\WindowMetrics", "AppliedDPI"), (r"Control Panel\Desktop", "LogPixels")):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
                v = winreg.QueryValueEx(k, name)[0]
        except OSError:
            continue
        if isinstance(v, int) and v > 0:
            return round(v * 100 / 96)
    try:
        r = subprocess.run([sys.executable, "-c", _DPI_PROBE], capture_output=True, text=True, timeout=10)
        v = int(r.stdout.strip())
        return round(v * 100 / 96) if v > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def build_id(root=None):
    """The short commit this copy was made from, so a shared report can be tied to the code: read from the git files
    (no git command needed; a worktree's `.git` file is followed), else from the VERSION line a bring-in zip carries
    ("kimeru <commit> built ..."), else "unknown"."""
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    try:
        git = root / ".git"
        if git.is_file():   # a worktree: "gitdir: <path>"
            git = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
        if git.is_dir():
            common = git
            if (git / "commondir").is_file():
                common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
            head = (git / "HEAD").read_text(encoding="utf-8").strip()
            if not head.startswith("ref:"):
                return head[:7]
            ref = head.split(":", 1)[1].strip()
            for base in (git, common):
                if (base / ref).is_file():
                    return (base / ref).read_text(encoding="utf-8").strip()[:7]
            packed = common / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + ref):
                        return line.split()[0][:7]
        ver = root / "VERSION"
        if ver.is_file():
            parts = ver.read_text(encoding="utf-8", errors="replace").split()
            if len(parts) >= 2 and parts[0] == "kimeru" and all(c in "0123456789abcdef" for c in parts[1].lower()):
                return parts[1][:7]
    except (OSError, IndexError, ValueError):
        pass
    return "unknown"


def environment():
    return {"kimeru": f"{__version__} ({build_id()})", "os": platform.platform(), "python": platform.python_version(),
            "writer": config.value("writer") or "未設定（定型文）", "display_scale_percent": _dpi_percent()}


def config_share():
    """`kimeru config show --share`: the settings in effect and where each came from, safe to paste into a public issue
    (see config.share_rows: no path, organization, subscription or address). The environment comes first."""
    env = environment()
    lines = [f"kimeru {env['kimeru']} / OS: {env['os']} / Python {env['python']}"
             + (f" / 画面の倍率 {env['display_scale_percent']}%" if env["display_scale_percent"] else ""),
             ""]
    rows = config.share_rows()
    width = max(len(k) for k, _, _ in rows)
    lines.append(f"{'setting'.ljust(width)}  {'value':<40}  source")
    lines += [f"{k.ljust(width)}  {(v or '-'):<40}  {src}" for k, v, src in rows]
    lines.append("secrets (environment variables; the value is never shown): "
                 + ", ".join(f"{n}={'set' if on else 'not set'}" for n, on in config.secrets_status().items()))
    return "\n".join(lines)


LABEL = {"approved": "OK", "rejected": "NG", "held": "保留", "redrafted": "修正", "redraft_failed": "修正できず", "ask_back": "聞き返し"}


def _graph_labels(per_graph, share):
    """{graph name: the name to print}. Shared output names only the graphs that ship with kimeru: a name the user gave
    to a graph of their own can be a project or a team, so it becomes 追加のグラフ 1, 2, ..."""
    if not share:
        return {n: n for n in per_graph}
    bundled = bundled_graph_names()
    extra = {n: f"追加のグラフ {i}" for i, n in enumerate(sorted(n for n in per_graph if n not in bundled), 1)}
    return {n: n if n in bundled else extra[n] for n in per_graph}


def render(s, share=False):
    judges, safe_judges = _summary_judges(s.get("judges"))
    has_jev = any(name.casefold() == "jev" for name in judges)
    lines = [f"kimeru の直近 7 日（{s['since']} から）"]
    n = s["decisions"]
    if not n:
        lines.append("判断: 0 件")
    else:
        lines.append(f"判断: {n} 件 / 自動で決めた {s['auto']} 件（{s['auto'] * 100 // n}%）・人に回した {s['human']} 件（{s['human'] * 100 // n}%）"
                     f" / 規則だけで決めた {s['rule']} 件 / 重大な通知 {s['critical']} 件")
        shown = _graph_labels(s["per_graph"], share)
        for name, g in sorted(s["per_graph"].items(), key=lambda kv: shown[kv[0]]):
            lines.append(f"  - {shown[name]}: {g['n']} 件（自動 {g['auto']}、人 {g['human']}、規則 {g['rule']}）")
    wk = s.get("week") or {}   # the week's additions; a summary made without them (older callers) shows none of them
    if wk:
        if wk.get("drafts") is not None:   # R12: the judgments that wait for the PM, and the draft checks, apart
            lines.append(f"人に回した件の内訳: 判断が必要 {s['human'] - wk['drafts']} 件 / 下書きの確認 {wk['drafts']} 件")
        prev = wk["previous"]
        if prev["decisions"]:
            lines.append(f"前の 7 日（{prev['since']} から）: 判断 {prev['decisions']} 件"
                         f"（自動 {prev['auto'] * 100 // prev['decisions']}%・人 {prev['human'] * 100 // prev['decisions']}%）")
        else:
            lines.append(f"前の 7 日（{prev['since']} から）: 判断 0 件")
        if wk["arrivals"]:
            order = list(dict.fromkeys(autorun.KINDS.values())) + ["その他"]
            lines.append("届いた件数（種類別）: " + "、".join(f"{k} {wk['arrivals'][k]} 件" for k in order if k in wk["arrivals"]))
    if s["approvals"]:
        lines.append("承認の結果: " + "、".join(f"{LABEL.get(k, k)} {v}" for k, v in sorted(s["approvals"].items(), key=lambda kv: str(kv[0]))))
    else:
        lines.append("承認の結果: 日時つきの記録なし")
    if wk:
        if wk["replied_items"]:
            lines.append(f"1 件あたりの返信: {wk['replies'] / wk['replied_items']:.1f} 回"
                         f"（返信のあった件 {wk['replied_items']} 件{_count_note(wk['replied_items'])}）")
        else:
            lines.append("1 件あたりの返信: 不明（返信のあった件 0 件）")
    lines.append(f"同じ修正指示の繰り返し（別の項目で）: {s.get('instruction_repeats', 0)} 件")
    if s["approvals_undated"]:
        lines.append(f"（日時の無い古い承認の記録 {s['approvals_undated']} 件は、期間の集計に入れていません）")
    if s.get("invalid_evidence"):
        lines.append(f"日時が不正な記録 {s['invalid_evidence']} 件は、期間の集計に入れていません")
    if s.get("future_evidence"):
        lines.append(f"未来の日時の記録 {s['future_evidence']} 件は、期間の集計に入れていません")
    if s.get("perf") and safe_judges and not has_jev:
        lines.append(s["perf"])
    if wk:
        # Performance figures: Jev's decisions and unnamed judges are left out of every one, on the screen and in --share alike.
        n_auto = wk["auto_checked"]
        lines.append("自動で決めた判断で、正誤に「違う」が付いた件: " + (f"{wk['auto_wrong']} 件（対象 {n_auto} 件{_count_note(n_auto)}）"
                                                           if n_auto else "不明（対象 0 件）"))
        if wk.get("rule_recorded"):   # R33: a rule's recorded-only lines are not in the automatic figure above (shown only when there are some)
            lines.append(f"規則で記録だけにした件（自動の正誤には入れていません）: {wk['rule_recorded']} 件")
        n_ok = wk["oks_with_draft"]
        lines.append("提案が一度も変わらなかった OK: " + (f"{wk['oks_unchanged']} 件（下書きのある OK {n_ok} 件{_count_note(n_ok)}）"
                                                     if n_ok else "不明（下書きのある OK 0 件）"))

        def _seconds(values):
            return f"{median(values):.1f} 秒（測れた {len(values)} 件{_count_note(len(values))}）" if values else "不明"
        lines.append(f"投稿から OK を見るまで（中央値）: 今週 {_seconds(wk['latency']['now'])}、"
                     f"前の 7 日 {_seconds(wk['latency']['previous'])}")
        if wk["jev_decisions"]:
            lines.append(f"（Jev の判断 {wk['jev_decisions']} 件は数えていません）")
        if wk.get("unnamed_decisions"):   # a judge with no name (an older record) is left out the same way, and the reader is told why
            lines.append(f"（判断モデルが分からない判断 {wk['unnamed_decisions']} 件は数えていません）")
        if wk["calibration"] is None:
            lines.append("calibrate の正誤: 不明（記録かグラフを読めませんでした）")
        else:
            floor = calibrate.MIN_QUESTIONS
            lines.append(f"calibrate の正誤（判断モデルごと。下限 {floor} 問）: " + "、".join(
                f"{name} {n} 問（" + (f"あと {floor - n} 問" if n < floor else "下限に達しています") + "）"
                for name, n in sorted(wk["calibration"].items())))
        if wk.get("report"):   # R24: six rows (times and counts; Jev and unnamed judges left out, on the screen and in --share alike)
            lines += _report_lines(wk["report"])
    total = s["reviewed_yes"] + s["reviewed_no"]
    left_out = s.get("reviewed_left_out", 0)
    if total:
        lines.append(f"判断の一致率: {s['reviewed_yes'] * 100 // total}%（確かめた {total} 件のうち、合っている {s['reviewed_yes']} 件）")
        if s.get("reviewed_wrong"):
            lines.append(f"  最終結果が違うとされた判断: {s['reviewed_wrong']} 件（質問単位の一致率には混ぜていません）")
        if left_out:
            lines.append("  （Jev の判断、または判断モデルが分からない判断を確かめた分は、数えていません）")
    elif s.get("reviewed_wrong"):
        lines.append("判断の一致率: 出しません（最終結果が違うという確認は質問単位の一致率に混ぜません）")
        lines.append(f"  最終結果が違うとされた判断: {s['reviewed_wrong']} 件")
    elif has_jev or left_out:
        lines.append("判断の一致率: 出しません（数えられる判断がありません。Jev の判断は数えません。Jev の性能の数値は、TypeSafe の利用規約で公開できません）")
    else:
        lines.append("判断の一致率: まだ確かめた判断がありません（kimeru review）")
    if share:
        env = environment()
        lines += ["", "環境:",
                  f"  - kimeru {env['kimeru']} / 判断モデル: {', '.join(judges) or '記録なし'}"
                  + (f"（{', '.join(s['models'])}）" if s["models"] else ""),
                  f"  - writer: {env['writer']}",
                  f"  - OS: {env['os']} / Python {env['python']}"
                  + (f" / 画面の倍率 {env['display_scale_percent']}%" if env["display_scale_percent"] else "")]
    return "\n".join(lines)


def week(out, share=False, now=None):
    return render(collect(out, now), share=share)


def weekly_lines(out, now=None):
    """R24 (weekly_report=1, the Monday brief): the six lines that `digest --week` prints for the same week. Read only. A caller that
    gets an exception leaves the six lines out (daily.py)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.astimezone(timezone.utc)   # a naive time is the local time of this PC
    return _report_lines(collect(out, now)["week"]["report"])
