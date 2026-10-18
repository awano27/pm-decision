"""`違う N` and `違う N k` (setting learn=1): the PM's verdict, on the phone, about a decision that was posted.

`違う N` says the final result of #N was wrong. `違う N k` says the k-th option of the question that `詳細 N` listed (the last
question the route was unsure of) is the right answer. Both become reviews in the state folder: the same rows `kimeru review` writes
(reviews.jsonl, fixtures_user.jsonl), so `kimeru calibrate` reads them. They change nothing about the item (status, post, execution,
work): the reply is a label, not a decision. labels.log.jsonl (in the output folder) keeps the key, the verdict, k and the time, never
the words of the reply. The questions and their options come from the graphs and playbooks that ship with kimeru.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import config, paths, review

HERE = paths.base()
LOG = "labels.log.jsonl"
MAX_OPTIONS = 8       # two to a line, at most four lines under the header: the section is at most five lines
OPTION_CHARS = 40     # characters of an option's text that the section shows


def enabled():
    return config.value("learn") == "1"


def log_path(out):
    return Path(out) / LOG


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(out, row):
    with log_path(out).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _now(), **row}, ensure_ascii=False) + "\n")


def revision_of(it):
    return int(it.get("revision") or (it.get("record") or {}).get("revision") or 1)


def _shipped():
    """(graphs by name, playbooks) of the folders kimeru ships with; None when they cannot be read (then no question is offered)."""
    from . import graph, plan
    try:
        pbs = plan.load_playbooks(HERE / "playbooks")
        gs = graph.load_dir(HERE / "graphs", pbs)
    except Exception:   # a damaged shipped file: `詳細` goes on without the question, and nothing is labeled
        return None
    return {g["name"]: g for lst in gs.values() for g in lst}, pbs


def question_of(rec):
    """(node id, [(label, text)]) for the last question the route was unsure of; None when there is none (the last unsure step
    was a rule, or the route had no unsure step)."""
    shipped = _shipped()
    if shipped is None:
        return None
    graphs, pbs = shipped
    nodes = (graphs.get(rec.get("graph")) or {}).get("nodes", {})
    for step in reversed(rec.get("path") or []):
        if step.get("edge") != "unsure":
            continue
        node = nodes.get(step.get("node"))
        if not node or node.get("kind") not in ("judge", "plan") or "matched" in (step.get("answer") or {}):
            return None
        return step["node"], review.options_of(node, pbs)[:MAX_OPTIONS]
    return None


def _short(text):
    """An option's text in at most OPTION_CHARS characters, cut at a word where one is near the end."""
    t = " ".join(str(text).split())
    if len(t) <= OPTION_CHARS:
        return t
    cut = t[:OPTION_CHARS]
    if cut.rfind(" ") >= OPTION_CHARS // 2:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(",.;:") + "…"


def section_lines(n, opts):
    """The 迷った質問 section of `詳細 n`: a header, then the options two to a line, numbered from 1."""
    cells = [f"{i}. {_short(text)}" for i, (_, text) in enumerate(opts[:MAX_OPTIONS], 1)]
    return [f"迷った質問（正しい答えの番号を「違う {n} 番号」で返信。判断は変わりません）"] + \
        ["　".join(cells[j:j + 2]) for j in range(0, len(cells), 2)]


def attach(it, n, text):
    """`text` (the 詳細 post of item n) with the section above its reply line, and the question kept on the item as
    `detail_question` (the options and the revision) for `違う N k`. An item without an unsure question keeps none. The caller
    decides whether learning is on."""
    q = question_of(it.get("record") or {})
    if q is None or not q[1]:
        it.pop("detail_question", None)
        return text
    nid, opts = q
    it["detail_question"] = {"node": nid, "options": [label for label, _ in opts], "revision": revision_of(it)}
    section = section_lines(n, opts)
    head, _, last = text.rpartition("\n")
    if last.startswith("返信:"):
        return head + "\n" + "\n".join(section) + "\n" + last
    return text + "\n" + "\n".join(section)


def _decisions(out):
    """The decisions that carry their event (kimeru review needs it) and their path, by decision key."""
    return {review.decision_key(r): r for r in review.read_jsonl(Path(out) / "decisions.jsonl")
            if isinstance(r, dict) and r.get("event") and r.get("path") is not None}


def row_of(out, key):
    """The decision record of `key` as `kimeru review` reads it (with its event and path); None when there is none."""
    return _decisions(out).get(key)


def _expect(row):
    """The labels of the questions the model answered on the way, as `y` in kimeru review writes them: {node: label}."""
    shipped = _shipped()
    if shipped is None:
        return {}
    return {nid: lab for nid, node, a, edge in review.judged(row, shipped[0])
            if (lab := review.label_of(node, a, edge)) is not None}


def record_spot(out, key, word, k, now):
    """R26 (spot.py): the PM's verdict on card S<k> of decision `key`. `正しい` is yes (the decision was right; its example is kept as
    `y` in kimeru review keeps it), `違う` is wrong (the final result was wrong, as `w`). The same rows as the terminal review, from
    Teams. A decision that has a review already keeps it ("already"). None when the decision record is not there. The caller holds
    the approvals lock and saves the card: the item (status, post, execution) is never touched here."""
    row = _decisions(out).get(key)
    if row is None:
        return None
    if any(r.get("key") == key for r in review.read_jsonl(review.reviews_path())):
        verdict = "already"
    elif word == "正しい":
        expect = _expect(row)
        if expect:   # the example first: a review without it would be read as "no question to fix"
            review._append(review.fixtures_path(), review.fixture_row(row, expect))
        review._append(review.reviews_path(), review.review_row(row, "yes", now, source="teams"))
        verdict = "yes"
    else:
        review._append(review.reviews_path(), review.review_row(row, "wrong", now, source="teams"))
        verdict = "wrong"
    _log(out, {"key": key, "verdict": verdict, "spot": k})
    return verdict


def _refused(out, it, key, reason, k):
    """A label that cannot be recorded leaves a reason in the log, once per reason and number."""
    seen = it.get("label_refused") or []
    if [reason, k] in seen:
        return None
    it["label_refused"] = seen + [[reason, k]]
    _log(out, {"key": key, "verdict": "refused", "reason": reason, "k": k})
    return {"status": "refused", "reason": reason}


def record(out, it, k):
    """Label the decision of item `it` from the PM's reply: k None for `違う N` (the final result is wrong), else the k-th option of
    the question that `詳細 N` listed. Once per revision: the same reply stays on the screen between cycles. A reply that cannot
    be a label is logged as a reason only. Returns what was recorded (or None). The caller holds the approvals lock and saves the item;
    the item's status, post and execution are never touched here."""
    rev = revision_of(it)
    done = it.get("labeled")
    if isinstance(done, dict) and done.get("revision") == rev:
        return None
    key = str(it.get("key"))
    row = _decisions(out).get(key)
    if row is None:
        return _refused(out, it, key, "no_record", k)
    expect = None
    if k is not None:
        dq = it.get("detail_question")
        if not isinstance(dq, dict):
            return _refused(out, it, key, "no_detail", k)
        if dq.get("revision") != rev:
            return _refused(out, it, key, "revision_changed", k)
        opts = dq.get("options") or []
        if not 1 <= k <= len(opts):
            return _refused(out, it, key, "out_of_range", k)
        expect = {dq["node"]: opts[k - 1]}
    now = _now()
    if expect is not None:
        # the example first: a review row without its example would be read as "no question to fix" by calibrate
        review._append(review.fixtures_path(), review.fixture_row(row, expect))
        review._append(review.reviews_path(), review.review_row(row, "no", now, node=dq["node"], source="teams"))
        verdict = "no"
    else:
        review._append(review.reviews_path(), review.review_row(row, "wrong", now, source="teams"))
        verdict = "wrong"
    _log(out, {"key": key, "verdict": verdict, "k": k})
    it["labeled"] = {"revision": rev}
    return {"status": verdict, "k": k}
