"""How long does a judgment take, and how does that depend on the number of questions and calls?

  python eval/measure_speed.py --backend kev [--n 12] [--kinds teams.chat,monitor.alert] [--repeat 1]

Runs the fixture events (eval/fixtures.jsonl, fictional) three ways and prints a table:

  sequential   one question per call, and every step's deadline asked (KIMERU_PLAN_LEAN=0)   the old behaviour
  lean         one question per call, a deadline asked only for the steps that are kept       the default
  batch        the judge questions of a graph in one call (KIMERU_BATCH=1), lean plan

Columns: calls, questions, seconds per event (median / max), and how many final actions differ from `sequential`.
It calls the judge: `--backend kev` needs a running Kev (127.0.0.1); it is never started from here.
Jev's numbers must stay private (TypeSafe terms); write nothing produced with --backend jev into the repository.
`--backend stub` runs the same table with instant answers (useful to check the counts).
"""
import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kimeru import cli, graph, plan  # noqa: E402
from kimeru.backends import JevBackend, KevBackend, StubBackend  # noqa: E402

MODES = {
    "sequential": {"KIMERU_PLAN_LEAN": "0", "KIMERU_BATCH": "0"},
    "lean": {"KIMERU_PLAN_LEAN": "1", "KIMERU_BATCH": "0"},
    "batch": {"KIMERU_PLAN_LEAN": "1", "KIMERU_BATCH": "1"},
}


def actions_of(res):
    return json.dumps([res["node"], res["actions"]], sort_keys=True, ensure_ascii=False)


def measure(backend, fixtures, graphs, pbs):
    """{mode: {"rows": [(calls, questions, seconds)], "actions": [json...]}}"""
    out = {}
    for mode, env in MODES.items():
        os.environ.update(env)
        rows, acts = [], []
        for fx in fixtures:
            meter = cli.Meter(backend)
            t = time.perf_counter()
            res = graph.run(graphs[fx["event"]["kind"]], fx["event"], meter, playbooks=pbs)
            rows.append((meter.calls, meter.questions, time.perf_counter() - t))
            acts.append(actions_of(res))
        out[mode] = {"rows": rows, "actions": acts}
    return out


def table(result):
    base = result["sequential"]["actions"]
    lines = [f"{'mode':<11}{'events':>7}{'calls/ev':>10}{'questions/ev':>14}{'sec median':>12}{'sec max':>9}{'actions differ':>16}"]
    for mode, r in result.items():
        n = len(r["rows"])
        calls = statistics.mean(x[0] for x in r["rows"])
        qs = statistics.mean(x[1] for x in r["rows"])
        secs = sorted(x[2] for x in r["rows"])
        differ = sum(a != b for a, b in zip(r["actions"], base))
        lines.append(f"{mode:<11}{n:>7}{calls:>10.1f}{qs:>14.1f}{secs[n // 2]:>12.2f}{secs[-1]:>9.2f}{differ:>16}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["stub", "kev", "jev"], default="stub")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--kinds", default="teams.chat,monitor.alert,ado.workitem.created,meeting.item")
    ap.add_argument("--fixtures", default="fixtures.jsonl")
    a = ap.parse_args()
    backend = {"kev": KevBackend, "jev": JevBackend}.get(a.backend, StubBackend)()
    pbs = plan.load_playbooks(ROOT / "playbooks")
    graphs = {k: v[0] for k, v in graph.load_dir(ROOT / "graphs", pbs).items()}
    kinds = a.kinds.split(",")
    fx = [json.loads(l) for l in (ROOT / "eval" / a.fixtures).read_text(encoding="utf-8").splitlines() if l.strip()]
    per = max(1, a.n // len(kinds))
    fixtures = [x for k in kinds for x in [f for f in fx if f["event"]["kind"] == k][:per]]
    try:
        result = measure(backend, fixtures, graphs, pbs)
    finally:
        for k in ("KIMERU_PLAN_LEAN", "KIMERU_BATCH"):
            os.environ.pop(k, None)
    print(table(result))
    if a.backend == "jev":
        print("(Jev: these numbers must stay private)")


if __name__ == "__main__":
    main()
