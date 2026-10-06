"""`kimeru review`: say, in the terminal, whether an automatic decision was right, one at a time.

The answers become labeled examples in the same shape as eval/fixtures.jsonl ({"id", "event", "expect"}), stored next to
the local state (never inside the repository), so the PM's own data can measure the judge and tune the thresholds
(`kimeru digest --week`, `kimeru calibrate`). Only the terminal is used; Teams is not touched.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import config


def reviews_path():
    return config.state_dir() / "reviews.jsonl"


def fixtures_path():
    return config.state_dir() / "fixtures_user.jsonl"


def decision_key(rec):
    """The identity of a decision: the same key the approval list uses."""
    return f"{rec.get('graph')}:{rec.get('event_id')}:{rec.get('node')}"


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _append(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def judged(rec, graph_by_name):
    """[(node id, node dict, answer, edge)] for the questions the model answered on the way (rules are not judged)."""
    g = graph_by_name.get(rec.get("graph"))
    out = []
    for step in rec.get("path", []):
        node = (g or {}).get("nodes", {}).get(step.get("node"))
        ans = step.get("answer") or {}
        if node and node.get("kind") in ("judge", "plan") and "matched" not in ans:
            out.append((step["node"], node, ans, step.get("edge")))
    return out


def label_of(node, ans, edge):
    """What the model committed to, as an eval label; None when it was unsure (nothing to confirm)."""
    if edge == "unsure":
        return None
    if node["kind"] == "plan":
        return ans.get("playbook") if edge == "ok" and ans.get("playbook") not in (None, "none") else None
    t = node["question"]["type"]
    if t == "choice":
        return ans.get("choice")
    if t == "noul":
        return True if edge == "yes" else False if edge == "no" else None
    if "score" in ans:
        return score_range(int(round(ans["score"])))
    return None


def score_range(level):
    """The right answer of a score question, held as a range (a model's answer is a decimal: 2.4 is level 2)."""
    return [level - 0.5, level + 0.5]


def options_of(node, playbooks):
    """[(label, text)] a person can pick as the right answer for this question."""
    if node["kind"] == "plan":
        ids = list(node.get("playbooks") if node.get("playbooks") not in (None, "*") else playbooks)
        return [(i, (playbooks.get(i) or {}).get("title", i)) for i in ids] + [("none", "どれにも当てはまらない")]
    q = node["question"]
    if q["type"] == "choice":
        return [(k, str(v)[:70]) for k, v in q["criteria"].items()]
    if q["type"] == "noul":
        return [(True, "はい"), (False, "いいえ")]
    return [(score_range(i), str(v)[:70]) for i, v in enumerate(q["criteria"])]


def fixture_row(rec, expect):
    key = decision_key(rec)
    return {"id": "user-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8], "event": rec["event"], "expect": expect}


def judge_of(rec):
    return (rec.get("judge") or {}).get("name", "unknown")


def review_row(rec, verdict, now, **extra):
    """A line of reviews.jsonl. It names the judge that made the decision: the agreement rate leaves Jev's out."""
    return {"key": decision_key(rec), "verdict": verdict, "at": now, "judge": judge_of(rec), **extra}


def pending(out, reviewed=None):
    """Decisions that carry their event and have not been reviewed yet, oldest first."""
    done = {r.get("key") for r in read_jsonl(reviews_path())} if reviewed is None else reviewed
    rows = [r for r in read_jsonl(Path(out) / "decisions.jsonl") if r.get("event") and r.get("path") is not None]
    return [r for r in rows if decision_key(r) not in done]


def run(out, graphs, playbooks, input_fn=input, print_fn=print, limit=20):
    """Ask about up to `limit` decisions. Returns {"yes": n, "no": n, "unknown": n}."""
    by_name = {g["name"]: g for lst in graphs.values() for g in lst}
    todo = pending(out)
    skipped_old = sum(1 for r in read_jsonl(Path(out) / "decisions.jsonl") if not r.get("event"))
    if skipped_old:
        print_fn(f"（{skipped_old} 件は、元のイベントが記録されていない古い判断なので、確かめられません）")
    if not todo:
        print_fn("確かめる判断はありません。")
        return {"yes": 0, "no": 0, "wrong": 0, "unknown": 0}
    tally = {"yes": 0, "no": 0, "wrong": 0, "unknown": 0}
    with_jev = False
    for i, rec in enumerate(todo[:limit], 1):
        steps = judged(rec, by_name)
        with_jev = with_jev or judge_of(rec) == "Jev"
        print_fn(f"\n[{i}/{min(len(todo), limit)}] {rec.get('event_kind')} #{rec.get('event_id')}  {rec.get('at', '')}")
        print_fn(f"  元: {rec.get('summary', '')}")
        print_fn("  経路: " + " → ".join(f"{s['node']}[{s['edge']}]" for s in rec["path"]) + f" → {rec['node']}")
        if rec.get("advice"):
            print_fn(f"  結果: {rec['advice']}")
        ans = (input_fn("  この判断は？ [y]合っている  [n]質問の答えが違う  [w]最終結果が違う  [s]分からない  [q]終了 > ") or "").strip().lower()
        if ans in ("q", "quit"):
            break
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if ans in ("y", "yes"):
            expect = {nid: lab for nid, node, a, edge in steps if (lab := label_of(node, a, edge)) is not None}
            _append(reviews_path(), review_row(rec, "yes", now))
            if expect:
                _append(fixtures_path(), fixture_row(rec, expect))
            tally["yes"] += 1
        elif ans in ("w", "wrong"):
            _append(reviews_path(), review_row(rec, "wrong", now))
            tally["wrong"] += 1
        elif ans in ("n", "no"):
            if not steps:
                print_fn("  質問の答えに分けられない最終結果の誤りとして記録します（校正用の質問ラベルは作りません）。")
                _append(reviews_path(), review_row(rec, "wrong", now))
                tally["wrong"] += 1
                continue
            for j, (nid, node, a, edge) in enumerate(steps, 1):
                shown = a.get("choice") or a.get("playbook") or a.get("score") or a.get("noul")
                print_fn(f"    {j}. {nid}（モデルの答え: {shown}、扱い: {edge}）")
            k = (input_fn("  どの質問の答えが違いましたか？ 番号 > ") or "").strip()
            if not k.isdigit() or not 1 <= int(k) <= len(steps):
                print_fn("  番号が分からないので、分からないとして記録します。")
                _append(reviews_path(), review_row(rec, "unknown", now))
                tally["unknown"] += 1
                continue
            nid, node, a, edge = steps[int(k) - 1]
            opts = options_of(node, playbooks)
            for j, (_, text) in enumerate(opts, 1):
                print_fn(f"    {j}. {text}")
            c = (input_fn("  正しい答えは？ 番号 > ") or "").strip()
            if not c.isdigit() or not 1 <= int(c) <= len(opts):
                print_fn("  番号が分からないので、分からないとして記録します。")
                _append(reviews_path(), review_row(rec, "unknown", now))
                tally["unknown"] += 1
                continue
            label = opts[int(c) - 1][0]
            _append(reviews_path(), review_row(rec, "no", now, node=nid))
            _append(fixtures_path(), fixture_row(rec, {nid: label}))
            tally["no"] += 1
        else:
            _append(reviews_path(), review_row(rec, "unknown", now))
            tally["unknown"] += 1
    if with_jev:   # what the answers say about Jev's accuracy is not shown (TypeSafe's terms); the records are kept
        print_fn(f"\n記録しました: {sum(tally.values())} 件（判断モデルが Jev の判断を含むため、内訳は出しません）（{fixtures_path()}）")
    else:
        print_fn(f"\n記録しました: 合っている {tally['yes']} / 質問の答えが違う {tally['no']} / 最終結果が違う {tally['wrong']} / 分からない {tally['unknown']}"
                 f"（{fixtures_path()}）")
    return tally
