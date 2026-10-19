"""`kimeru replay`: run the reviewed cases again on today's graphs, with the answers recorded then (the model is not called).

The cases are the ones `kimeru review` / `違う N` collected: reviews.jsonl names the decision, decisions.jsonl holds the event and
the answers the judge gave, fixtures_user.jsonl holds the label of a question marked wrong (the same files `kimeru calibrate`
reads, through the same functions in review.py). Each case is walked through the graph as it is now; the recorded answer is
served for every question. A question with no recorded answer (a plan question keeps only some of its answers) prints 記録なし and
stops that case; the other cases go on.

It compares the final action then and now and counts: cases marked right whose action changed, cases marked wrong whose action
changed, and cases whose wrong question now matches its label. What is printed and saved are numbers, case ids ("user-...") and
node names. The text of an event is never printed or written. The result goes to its own folder (default: `replay` under the
state folder); queue.jsonl, notices.jsonl and approvals.json, and nothing in --out, are not touched, and nothing is posted.
For a record made by Jev the right/wrong counts are not shown (TypeSafe terms; as in calibrate).
"""
import json
from pathlib import Path

from . import calibrate, config, fsutil, graph as graph_mod, profiles, review
from .backends import BackendAnswerError

VERDICT_RIGHT = ("yes",)
VERDICT_WRONG = ("no", "wrong")


class NoRecord(Exception):
    """The walk reached a question the record has no answer for."""

    def __init__(self, node):
        super().__init__(node)
        self.node = node


class RecordedBackend:
    """Serves the answers of one recorded decision, by question id. Never calls a model."""

    def __init__(self, rec, profile):
        self.profile = profile
        self.answers = {s.get("node"): s.get("answer") or {} for s in rec.get("path", [])}

    def ask(self, state, questions):
        out = {}
        for qid, q in questions.items():
            ans = self.answers.get(qid)
            if ans is None or "matched" in ans:
                raise NoRecord(qid)
            if "invalid" in ans:   # the model's answer was malformed then; it is now (the walk takes the safe route)
                raise BackendAnswerError(ans["invalid"])
            out[qid] = {"type": q["type"], **{k: v for k, v in ans.items() if k != "guard"}}
        return out


def result_dir():
    return config.state_dir() / "replay"


def cases(out):
    """[(case id, verdict, review row, decision)] for the reviewed decisions that still carry their event and answers."""
    decisions = {review.decision_key(r): r for r in review.read_jsonl(Path(out) / "decisions.jsonl")
                 if r.get("path") is not None and r.get("event")}
    last = {}
    for rev in review.read_jsonl(review.reviews_path()):
        last[rev.get("key")] = rev   # the latest verdict of a decision stands
    res = []
    for key, rev in last.items():
        rec = decisions.get(key)
        if rec and rev.get("verdict") in VERDICT_RIGHT + VERDICT_WRONG:
            res.append((review.fixture_row(rec, {})["id"], rev["verdict"], rev, rec))
    return res


def _final(res):
    return [res.get("outcome"), res.get("node")]


def replay_one(case, by_name, pbs, labels):
    """One case -> a row of numbers and names only."""
    cid, verdict, rev, rec = case
    row = {"id": cid, "verdict": "right" if verdict in VERDICT_RIGHT else "wrong", "judge": review.judge_of(rec),
           "before": _final(rec)}
    g = by_name.get(rec.get("graph"))
    if g is None:
        return {**row, "status": "no_record", "at": None}
    prof = profiles.PROFILES.get(profiles.JUDGE_PROFILE.get(row["judge"]))
    try:
        res = graph_mod.run(g, rec["event"], RecordedBackend(rec, prof), playbooks=pbs, batch=False, split_asks=False)
    except NoRecord as e:
        return {**row, "status": "no_record", "at": e.node}
    except Exception as e:   # one case never stops the others
        return {**row, "status": "failed", "at": type(e).__name__}
    row.update(status="ok", after=_final(res), changed=_final(res) != row["before"])
    nid = rev.get("node")
    want = (labels.get(cid) or {}).get(nid) if rev.get("verdict") == "no" else None
    if want is not None:
        step = next((s for s in res["path"] if s["node"] == nid), None)
        was = next((s for s in rec["path"] if s["node"] == nid), None)
        node = g["nodes"].get(nid)
        if step and was and node and "matched" not in (step.get("answer") or {}):
            row["label_node"] = nid
            row["label_before"] = calibrate._matches(node, was.get("answer") or {}, was.get("edge"), want)
            row["label_after"] = calibrate._matches(node, step.get("answer") or {}, step.get("edge"), want)
    return row


def summarize(rows):
    ok = [r for r in rows if r["status"] == "ok"]
    right, wrong = [r for r in ok if r["verdict"] == "right"], [r for r in ok if r["verdict"] == "wrong"]
    return {"cases": len(rows), "replayed": len(ok), "no_record": sum(r["status"] == "no_record" for r in rows),
            "failed": sum(r["status"] == "failed" for r in rows), "changed": sum(r["changed"] for r in ok),
            "right_cases": len(right), "right_changed": sum(r["changed"] for r in right),
            "wrong_cases": len(wrong), "wrong_changed": sum(r["changed"] for r in wrong),
            "wrong_now_match_label": sum(1 for r in wrong if r.get("label_after") and not r.get("label_before")),
            "wrong_no_longer_match_label": sum(1 for r in wrong if r.get("label_before") and not r.get("label_after"))}


def run(out, graphs, playbooks, result=None, print_fn=print):
    """Replay and report. Returns the summary dict, or None when there is nothing to replay."""
    out, dest = Path(out), Path(result) if result else result_dir()
    if dest.resolve() in (out.resolve(), config.state_dir().resolve()):
        print_fn("書き出し先が記録のフォルダまたは状態フォルダそのものです。別のフォルダを指定してください。何も書きません。")
        return None
    by_name = {g["name"]: g for lst in graphs.values() for g in lst}
    labels = {r.get("id"): r.get("expect") for r in review.read_jsonl(review.fixtures_path())}
    todo = cases(out)
    if not todo:
        print_fn("再生する事例がありません（`kimeru review` で正誤を付けてください）。何も書きません。")
        return None
    rows = [replay_one(c, by_name, playbooks, labels) for c in todo]
    s = summarize(rows)
    hide = any(r["judge"] == "Jev" for r in rows)   # accuracy of Jev is not shown
    print_fn(f"再生した事例: {s['replayed']} / {s['cases']} 件（記録なし {s['no_record']} 件、失敗 {s['failed']} 件。モデルは呼んでいません）")
    for r in rows:
        if r["status"] == "no_record":
            print_fn(f"  {r['id']}: 記録なし（{r['at'] or 'グラフなし'}）")
        elif r["status"] == "failed":
            print_fn(f"  {r['id']}: 失敗（{r['at']}）")
    print_fn(f"最終行動が変わった件: {s['changed']} 件")
    for r in rows:
        if r["status"] == "ok" and r["changed"]:
            tag = "" if hide else f"[{'正' if r['verdict'] == 'right' else '誤'}] "
            print_fn(f"  {r['id']}: {tag}{r['before'][0]}:{r['before'][1]} → {r['after'][0]}:{r['after'][1]}")
    if hide:
        print_fn("判断モデルが Jev の事例を含むため、正しかった件・違うと言われた件の内訳は出しません（TypeSafe の利用規約）。")
        s = {k: v for k, v in s.items() if k in ("cases", "replayed", "no_record", "failed", "changed")}
        rows = [{k: v for k, v in r.items() if k not in ("verdict", "label_node", "label_before", "label_after")} for r in rows]
    else:
        print_fn(f"正しいと付いた件のうち変わった: {s['right_changed']} / {s['right_cases']} 件")
        print_fn(f"違うと付いた件のうち変わった: {s['wrong_changed']} / {s['wrong_cases']} 件"
                 f"（間違った質問の正解に合うようになった {s['wrong_now_match_label']} 件、合わなくなった {s['wrong_no_longer_match_label']} 件）")
    try:
        fsutil.write_atomic(dest / "replay.json", json.dumps({"summary": s, "cases": rows}, ensure_ascii=False, indent=1) + "\n")
        print_fn(f"書き出し: {dest / 'replay.json'}")
    except OSError as e:
        print_fn(f"書き出せませんでした（{type(e).__name__}）。画面の数字は上のとおりです。")
    return s
