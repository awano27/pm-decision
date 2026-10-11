"""Golden paths: where each labeled fixture ends when only its labels answer (no model is asked).

eval/golden.json keeps, for each fixture id, the terminal node, a sha1 of its planned actions and the path (node[edge] steps).
After a graph change, the fixtures that moved are listed as `id: before -> after` (tests/test_golden_paths.py fails with them).

  python eval/golden.py            check: list the fixtures that moved (exit 1 when any)
  python eval/golden.py --write    after an intended change: write the current paths

A labeled question answers its label (a list of accepted values takes the first; a score range its middle). Every other
question gets unsure_answer(): confidence 0, so a choice or score falls below its min_conf and a yes/no question lands in
the middle of the unsure band. A plan's playbook is then unsure, and a plan step is kept with its deadline marked as to be
checked. The file keeps ids, nodes, edges and hashes only: no judge's number, no fixture text. The coefficients that
`kimeru calibrate --apply` wrote are not read, and this PC's KIMERU_* settings are set to their defaults during the walk.
"""
import argparse
import contextlib
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kimeru import config, graph, plan, profiles  # noqa: E402

GOLDEN = ROOT / "eval" / "golden.json"
FIXTURE_FILES = ("fixtures.jsonl", "fixtures_holdout.jsonl")


def load_playbooks():
    return plan.load_playbooks(ROOT / "playbooks")


def load_graphs(playbooks=None):
    pbs = load_playbooks() if playbooks is None else playbooks
    return {k: v[0] for k, v in graph.load_dir(ROOT / "graphs", pbs).items()}


def fixtures(names=FIXTURE_FILES):
    out = []
    for name in names:
        out += [json.loads(line) for line in (ROOT / "eval" / name).read_text(encoding="utf-8").splitlines() if line.strip()]
    return out


def unsure_answer(question):
    """The answer that decides nothing: confidence 0 (below any min_conf), and the middle of a yes/no question."""
    t = question["type"]
    if t == "noul":
        return {"type": "noul", "noul": 0.5}
    if t == "choice":
        return {"type": "choice", "choice": next(iter(question["criteria"])), "confidence": 0.0, "probabilities": {}}
    return {"type": "score", "score": (len(question["criteria"]) - 1) / 2, "confidence": 0.0, "probabilities": {}}


def _label_answer(node, label):
    """The answer that says the label: the first of a list of accepted values, or the middle of a score range."""
    if node["kind"] == "plan":
        return {"choice": label[0] if isinstance(label, list) else label, "confidence": 0.99}
    t = node["question"]["type"]
    if t == "noul":
        value = label[0] if isinstance(label, list) else label
        return {"type": "noul", "noul": 0.99 if value else 0.01}
    if t == "choice":
        return {"choice": label[0] if isinstance(label, list) else label, "confidence": 0.99, "probabilities": {}}
    lo, hi = label
    return {"score": (lo + hi) / 2, "confidence": 0.99, "probabilities": {}}


class Oracle:
    """Labeled questions get their label; every other question gets unsure_answer(). No backend is asked."""

    def __init__(self, labels, g):
        plans = [n for n, x in g["nodes"].items() if x["kind"] == "plan" and n in labels]
        if len(plans) > 1:   # the playbook question carries no node id, so two labeled plans would be ambiguous
            raise ValueError(f"{g['name']}: a fixture labels more than one plan node ({', '.join(plans)})")
        self.labels, self.g = labels, g
        self.plan = plans[0] if plans else None

    def ask(self, state, questions):
        out = {}
        for q, question in questions.items():
            node = self.g["nodes"].get(q)
            if node is not None and node["kind"] == "judge" and q in self.labels:
                out[q] = _label_answer(node, self.labels[q])
            elif q == "playbook" and self.plan:
                out[q] = _label_answer(self.g["nodes"][self.plan], self.labels[self.plan])
            else:
                out[q] = unsure_answer(question)
        return out


def walk(fx, graphs, playbooks):
    g = graphs[fx["event"]["kind"]]
    res = graph.run(g, fx["event"], Oracle(fx["expect"], g), playbooks=playbooks, batch=False)
    actions = json.dumps(res["actions"], ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {"node": res["node"], "actions_sha1": hashlib.sha1(actions).hexdigest(),
            "path": [f"{s['node']}[{s['edge']}]" for s in res["path"]]}


@contextlib.contextmanager
def _defaults_only():
    """The walk uses the settings' defaults, whatever this PC's KIMERU_* variables say (a title length or the plan mode would
    change the expected actions). The variables are restored afterwards."""
    names = [env for env, _ in config.SETTINGS.values()]
    saved = {n: os.environ.pop(n) for n in names if n in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def collect(graphs=None, playbooks=None, names=FIXTURE_FILES):
    pbs = load_playbooks() if playbooks is None else playbooks
    gs = load_graphs(pbs) if graphs is None else graphs
    out = {}
    with profiles.ignoring_overrides(True), _defaults_only():
        for fx in fixtures(names):
            out[fx["id"]] = walk(fx, gs, pbs)
    return out


def diff(expected, actual):
    """One line per fixture that moved: `id: before -> after` for the end node, then its path and whether its actions changed."""
    lines = []
    for fid in sorted(set(expected) | set(actual)):
        e, a = expected.get(fid), actual.get(fid)
        if e is None or a is None:
            lines.append(f"{fid}: {'added' if e is None else 'removed'}")
            continue
        if e == a:
            continue
        if e["node"] != a["node"]:
            line = f"{fid}: {e['node']} -> {a['node']}"
        else:
            line = f"{fid}: {a['node']}, same end"
        if e["path"] != a["path"]:
            line += f"; path {' > '.join(e['path'])} -> {' > '.join(a['path'])}"
        if e["actions_sha1"] != a["actions_sha1"]:
            line += "; actions changed"
        lines.append(line)
    return lines


def read(path=None):
    p = Path(path) if path else GOLDEN
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def write(data, path=None):
    p = Path(path) if path else GOLDEN
    p.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check eval/golden.json against the current graphs, or write it.")
    ap.add_argument("--write", action="store_true", help="write the current paths (after an intended graph change)")
    a = ap.parse_args(argv)
    now = collect()
    if a.write:
        write(now)
        print(f"wrote {len(now)} paths to {GOLDEN.name}")
        return 0
    lines = diff(read(), now)
    for line in lines:
        print(line)
    print(f"{len(lines)} fixture(s) moved" if lines else "no fixture moved")
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main())
