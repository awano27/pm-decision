"""End-to-end eval: does each event end in the right final action?

The node-level eval (run_eval.py) scores each labeled question in isolation, so it
cannot see the deterministic safety nets (match nodes) or how errors compound along
a path. This walks the whole graph twice per event:

  model path  - every judge/plan question answered by the backend
  ideal path  - labeled judgments replaced by the label ("oracle"); every other
                question gets the same answer the backend gave (cached), so the only
                difference is where the model disagreed with a label

and compares the terminal nodes.

  outcome      model terminal == ideal terminal           -> correct
               model ended in an advise/queue (a human)   -> to-human (safe)
               otherwise                                  -> wrong
  severe miss  ideal terminal is a severe action (page, P1, 24h prevention) but the
               model neither reached a severe action nor asked a human

  python eval/e2e.py --backend kev [--out results.jsonl]
Jev numbers must stay private (TypeSafe terms): write output outside the repo.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kimeru import graph, plan  # noqa: E402
from kimeru.backends import ClmBackend, JevBackend, KevBackend  # noqa: E402

SEVERE = {"page", "page_planned", "set_p1", "set_p1_critical", "prevent_now"}
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = {k: v[0] for k, v in graph.load_dir(ROOT / "graphs", PBS).items()}


def fixtures():
    return [json.loads(l) for l in (ROOT / "eval" / "fixtures.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


class Recording:
    """Wraps a backend, caching every answer by (node id / question id)."""

    def __init__(self, inner):
        self.inner, self.cache = inner, {}
        self.profile = getattr(inner, "profile", None)

    def ask(self, state, questions):
        missing = {q: v for q, v in questions.items() if q not in self.cache}
        if missing:
            self.cache.update(self.inner.ask(state, missing))
        return {q: self.cache[q] for q in questions}


def ideal_answer(node, label):
    """A confident answer equal to the label, in the backend's answer shape."""
    if node["kind"] == "plan":
        return {"choice": label if isinstance(label, str) else label[0], "confidence": 0.99}
    t = node["question"]["type"]
    if t == "noul":
        return {"noul": 0.99 if label else 0.01}
    if t == "choice":
        return {"choice": label if isinstance(label, str) else label[0], "confidence": 0.99}
    lo, hi = label
    return {"score": (lo + hi) / 2, "confidence": 0.99, "probabilities": {}}


class Oracle:
    """Labels for labeled nodes; otherwise the recorded model answer (asks live if unseen)."""

    def __init__(self, rec, labels, g):
        self.rec, self.labels, self.g = rec, labels, g
        self.profile = rec.profile

    def ask(self, state, questions):
        out = {}
        for q, v in questions.items():
            node = self.g["nodes"].get(q)
            if node is not None and q in self.labels and node["kind"] == "judge":
                out[q] = ideal_answer(node, self.labels[q])
            elif q == "playbook":   # plan node: label keyed by the plan node id
                plan_ids = [n for n, x in self.g["nodes"].items() if x["kind"] == "plan" and n in self.labels]
                out[q] = ideal_answer(self.g["nodes"][plan_ids[0]], self.labels[plan_ids[0]]) if plan_ids else \
                    self.rec.ask(state, {q: v})[q]
            else:
                out[q] = self.rec.ask(state, {q: v})[q]
        return out


def run_one(fx, backend):
    g = GRAPHS[fx["event"]["kind"]]
    rec = Recording(backend)
    model = graph.run(g, fx["event"], rec, playbooks=PBS)
    ideal = graph.run(g, fx["event"], Oracle(rec, fx["expect"], g), playbooks=PBS)
    human = model["outcome"] == "advise"
    unsure = any(s["edge"] == "unsure" for s in model["path"])
    if model["node"] == ideal["node"]:
        outcome = "correct"
    elif human:
        outcome = "human"
    elif unsure:
        outcome = "fallback"   # low confidence took the graph's designed safe route (e.g. task marked needs-owner)
    else:
        outcome = "wrong"
    severe_miss = ideal["node"] in SEVERE and model["node"] not in SEVERE and not human
    return {"id": fx["id"], "graph": g["name"], "model": model["node"], "ideal": ideal["node"],
            "outcome": outcome, "severe_miss": severe_miss,
            "path": [f"{s['node']}[{s['edge']}]" for s in model["path"]]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["jev", "kev", "clm"], required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    be = {"kev": KevBackend, "clm": ClmBackend}.get(a.backend, JevBackend)()
    rows, t0 = [], time.time()
    for fx in fixtures():
        rows.append(run_one(fx, be))
    if a.out:
        Path(a.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    n = len(rows)
    count = lambda o: sum(r["outcome"] == o for r in rows)
    print(f"backend={a.backend} events={n} ({time.time() - t0:.0f}s)")
    print(f"correct final action: {count('correct')}/{n}  to-human: {count('human')}  safe fallback: {count('fallback')}"
          f"  wrong: {count('wrong')}  severe misses: {sum(r['severe_miss'] for r in rows)}")
    by = {}
    kinds = ["correct", "human", "fallback", "wrong"]
    for r in rows:
        c = by.setdefault(r["graph"], [0] * 5)
        c[kinds.index(r["outcome"])] += 1
        c[4] += 1
    for g, (c, h, f, w, tot) in sorted(by.items()):
        print(f"  {g:<20} correct {c}/{tot}  human {h}  fallback {f}  wrong {w}")
    for r in rows:
        if r["outcome"] == "wrong" or r["severe_miss"]:
            print(f"  {'SEVERE ' if r['severe_miss'] else ''}{r['id']}: model={r['model']} ideal={r['ideal']}  {' > '.join(r['path'])}")


if __name__ == "__main__":
    main()
