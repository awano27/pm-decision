"""How long does a judgment take, and how does that depend on the number of questions and calls?

  python eval/measure_speed.py --backend kev [--n 12] [--kinds teams.chat,monitor.alert] [--repeat 3]

Runs the fixture events (eval/fixtures.jsonl, fictional) three ways and prints a table:

  sequential   one question per call, and every step's deadline asked (KIMERU_PLAN_LEAN=0)   the old behaviour
  lean         one question per call, a deadline asked only for the steps that are kept       the default
  batch        the judge questions of a graph in one call (KIMERU_BATCH=1), lean plan

Columns: calls, questions, seconds per event (median / max), and how many final actions differ from `sequential`.
--repeat N runs every fixture N times (the medians then rest on more than one run of each).
It calls the judge: `--backend kev` needs a running Kev on this PC (KIMERU_KEV_URL, default 127.0.0.1); it checks that
first and, when Kev is not up (or the address is another machine), prints one line and stops. It is never started from here.
Jev is not accepted: its speed must not be published (TypeSafe terms).
`--backend stub` runs the same table with instant answers (useful to check the counts).
The coefficients `kimeru calibrate --apply` wrote to the state folder are not read, unless you pass --user-thresholds.
"""
import argparse
import json
import os
import socket
import statistics
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kimeru import cli, graph, plan, profiles  # noqa: E402
from kimeru.backends import LOOPBACK, KevBackend, StubBackend  # noqa: E402

MODES = {
    "sequential": {"KIMERU_PLAN_LEAN": "0", "KIMERU_BATCH": "0"},
    "lean": {"KIMERU_PLAN_LEAN": "1", "KIMERU_BATCH": "0"},
    "batch": {"KIMERU_PLAN_LEAN": "1", "KIMERU_BATCH": "1"},
}


def actions_of(res):
    return json.dumps([res["node"], res["actions"]], sort_keys=True, ensure_ascii=False)


def measure(backend, fixtures, graphs, pbs, repeat=1):
    """{mode: {"rows": [(calls, questions, seconds)], "actions": [json...]}}; every fixture runs `repeat` times."""
    out = {}
    for mode, env in MODES.items():
        os.environ.update(env)
        rows, acts = [], []
        for _ in range(max(1, repeat)):
            for fx in fixtures:
                meter = cli.Meter(backend)
                t = time.perf_counter()
                res = graph.run(graphs[fx["event"]["kind"]], fx["event"], meter, playbooks=pbs)
                rows.append((meter.calls, meter.questions, time.perf_counter() - t))
                acts.append(actions_of(res))
        out[mode] = {"rows": rows, "actions": acts}
    return out


def kev_problem(url=None, connect=socket.create_connection):
    """One line of guidance when Kev cannot be measured (not on this PC, or not running), else None. Nothing is sent:
    only a connection to the port is tried."""
    url = url or os.environ.get("KIMERU_KEV_URL") or "http://127.0.0.1:8009/v1"
    u = urllib.parse.urlparse(url)
    host = u.hostname or ""
    if host not in LOOPBACK:
        return f"Kev の接続先（KIMERU_KEV_URL）がこの PC ではありません（{host or url}）。この PC で起動した Kev（127.0.0.1）だけを測ります。"
    try:
        with connect((host, u.port or 80), timeout=3):
            pass
    except OSError:
        return f"Kev が起動していません（{host}:{u.port or 80} に接続できません）。kev.serve を起動してから、もう一度実行してください。"
    return None


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


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["stub", "kev"], default="stub", help="jev is not accepted (its speed must not be published)")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--kinds", default="teams.chat,monitor.alert,ado.workitem.created,meeting.item")
    ap.add_argument("--fixtures", default="fixtures.jsonl")
    ap.add_argument("--repeat", type=int, default=1, help="run every fixture this many times")
    ap.add_argument("--user-thresholds", action="store_true",
                    help="also read the coefficients `kimeru calibrate --apply` wrote (default: not read)")
    a = ap.parse_args(argv)
    if a.backend == "kev":
        problem = kev_problem()
        if problem:
            print(problem)
            return 1
    backend = {"kev": KevBackend}.get(a.backend, StubBackend)()
    pbs = plan.load_playbooks(ROOT / "playbooks")
    graphs = {k: v[0] for k, v in graph.load_dir(ROOT / "graphs", pbs).items()}
    kinds = a.kinds.split(",")
    fx = [json.loads(l) for l in (ROOT / "eval" / a.fixtures).read_text(encoding="utf-8").splitlines() if l.strip()]
    per = max(1, a.n // len(kinds))
    fixtures = [x for k in kinds for x in [f for f in fx if f["event"]["kind"] == k][:per]]
    if a.user_thresholds and profiles.overrides_note():
        print(profiles.overrides_note())
    try:
        with profiles.ignoring_overrides(not a.user_thresholds):
            result = measure(backend, fixtures, graphs, pbs, a.repeat)
    finally:
        for k in ("KIMERU_PLAN_LEAN", "KIMERU_BATCH"):
            os.environ.pop(k, None)
    print(table(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
