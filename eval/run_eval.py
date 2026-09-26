"""Question-quality eval for the shipped graphs.

  python eval/run_eval.py dump  > requests.jsonl        # one Jev request per fixture
  python eval/run_eval.py score answers.jsonl           # answers: {"id", "answers": {...}} per line
  python eval/run_eval.py live  [--out answers.jsonl]   # call Jev directly (TYPESAFE_API_KEY)

Each fixture labels some judge / plan nodes of its graph. All labeled questions for
one event go in ONE request (Jev answers them independently).
Labels: choice -> "opt" or ["opt", ...]; noul -> true/false; score -> [lo, hi];
plan node -> expected playbook id (or list).

Jev accuracy numbers must not be published (TypeSafe terms); keep answers/results private.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kimeru import graph, plan  # noqa: E402
from kimeru.backends import jev_questions  # noqa: E402
from kimeru.events import state_of  # noqa: E402

PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = {k: v[0] for k, v in graph.load_dir(ROOT / "graphs", PBS).items()}


def fixtures(path=ROOT / "eval" / "fixtures.jsonl"):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def plan_question(node):
    ids = plan.allowed(node, PBS)
    return {"type": "choice", "instructions": node.get("instructions", "Which playbook fits the work the project manager must do next?"),
            "criteria": {**{i: PBS[i]["when"] for i in ids}, "none": "None of these playbooks fits"}}


def request(fx):
    g = GRAPHS[fx["event"]["kind"]]
    qs = {}
    for nid in fx["expect"]:
        n = g["nodes"][nid]
        qs[nid] = plan_question(n) if n["kind"] == "plan" else n["question"]
    return {"id": fx["id"], "state": state_of(fx["event"]), "questions": jev_questions(qs)}


def judge(node, exp, ans, profile=None):
    """Return (ok, routed_edge, detail)."""
    from kimeru.profiles import conf as conf_at
    if node["kind"] == "plan":
        conf, choice = ans.get("confidence", 0), ans.get("choice")
        edge = "unsure" if conf < conf_at(node, "min_conf", 0.6, profile) else choice
        want = exp if isinstance(exp, list) else [exp]
        return edge in want, edge, f"{choice} ({conf:.2f})"
    t = node["question"]["type"]
    edge, _ = graph.route(node, ans, profile)  # same thresholds/guards as production
    if t == "noul":
        return edge == ("yes" if exp else "no"), edge, f"{ans['noul']:.2f}"
    if t == "choice":
        want = exp if isinstance(exp, list) else [exp]
        return edge in want, edge, f"{ans.get('choice')} ({ans.get('confidence', 0):.2f})"
    s, conf = ans["score"], ans.get("confidence", 0)
    lo, hi = exp
    return edge != "unsure" and lo <= s <= hi, edge, f"{s:.2f} ({conf:.2f})"


def load_answers(answers_path):
    ans = {}
    for l in Path(answers_path).read_text(encoding="utf-8").splitlines():
        if l.strip():
            r = json.loads(l)
            ans[r["id"]] = r["answers"]
    return ans


def outcomes(ans, profile=None):
    """(correct, unsure, confidently_wrong, total) with the given threshold profile."""
    ok = un = wrong = n = 0
    for fx in fixtures():
        if fx["id"] not in ans:
            continue
        g = GRAPHS[fx["event"]["kind"]]
        for nid, exp in fx["expect"].items():
            node, a = g["nodes"][nid], ans[fx["id"]].get(nid)
            want_type = "choice" if node["kind"] == "plan" else node["question"]["type"]
            if a is None or a.get("type", want_type) != want_type:
                continue
            good, edge, _ = judge(node, exp, a, profile)
            n += 1
            ok += good
            un += (not good) and edge == "unsure"
            wrong += (not good) and edge != "unsure"
    return ok, un, wrong, n


def tune(answers_path):
    """Grid-search the two profile knobs. Pick the most automation that does not add a
    single confidently-wrong decision over the untuned (Jev) thresholds; ties go to the
    more conservative (larger) scales."""
    ans = load_answers(answers_path)
    base = outcomes(ans)
    print(f"untuned: correct={base[0]} unsure={base[1]} confident-wrong={base[2]} / {base[3]}")
    grid = [round(0.40 + 0.05 * i, 2) for i in range(13)]   # 0.40 .. 1.00
    best = None
    rows = []
    for cs in grid:
        for ns in grid:
            ok, un, wrong, n = outcomes(ans, {"conf_scale": cs, "noul_scale": ns})
            rows.append((cs, ns, ok, un, wrong))
            if wrong <= base[2] and (best is None or (ok, cs, ns) > (best[2], best[0], best[1])):
                best = (cs, ns, ok, un, wrong)
    print("conf_scale noul_scale  correct unsure wrong")
    for r in rows:
        if r[0] in (1.0, 0.8, 0.6, 0.5, 0.4) and r[1] in (1.0, 0.8, 0.6, 0.4):
            print(f"  {r[0]:.2f}      {r[1]:.2f}      {r[2]:>4}   {r[3]:>4}  {r[4]:>4}")
    cs, ns, ok, un, wrong = best
    print(f"best (confident-wrong <= {base[2]}): conf_scale={cs} noul_scale={ns} -> correct={ok} unsure={un} wrong={wrong}")
    return best


def score(answers_path, profile=None):
    ans = load_answers(answers_path)
    per_node, misses = {}, []
    for fx in fixtures():
        if fx["id"] not in ans:
            continue
        g = GRAPHS[fx["event"]["kind"]]
        for nid, exp in fx["expect"].items():
            node, a = g["nodes"][nid], ans[fx["id"]].get(nid)
            want_type = "choice" if node["kind"] == "plan" else node["question"]["type"]
            if a is None or a.get("type", want_type) != want_type:
                continue  # answer recorded for an older version of this question
            ok, edge, detail = judge(node, exp, a, profile)
            key = f"{g['name']}/{nid}"
            c = per_node.setdefault(key, [0, 0, 0])
            c[0] += ok
            c[1] += 1
            c[2] += edge == "unsure"
            if not ok:
                misses.append(f"{fx['id']:<11} {key:<34} want={exp} got={detail} edge={edge}")
    print("node                                   ok/n  unsure")
    for k, (ok, n, un) in sorted(per_node.items()):
        print(f"{k:<38} {ok}/{n}   {un}")
    print(f"\nmisses ({len(misses)}):")
    print("\n".join(misses) or "  none")


def live(out, backend="jev"):
    import time
    from kimeru.backends import JevBackend, KevBackend
    be = KevBackend() if backend == "kev" else JevBackend()
    times = []
    with open(out, "w", encoding="utf-8") as f:
        for fx in fixtures():
            r = request(fx)
            t0 = time.time()
            ans = be.ask(r["state"], r["questions"])
            times.append(time.time() - t0)
            f.write(json.dumps({"id": r["id"], "answers": ans}, ensure_ascii=False) + "\n")
    times.sort()
    print(f"latency per request: median {times[len(times) // 2]:.2f}s, max {times[-1]:.2f}s ({len(times)} requests)")
    score(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dump")
    p = sub.add_parser("score")
    p.add_argument("answers")
    p.add_argument("--profile", choices=["jev", "kev"], default="jev", help="threshold profile (kimeru/profiles.py)")
    p = sub.add_parser("tune", help="grid-search profile knobs on recorded answers")
    p.add_argument("answers")
    p = sub.add_parser("live")
    p.add_argument("--out", default="answers.jsonl")
    p.add_argument("--backend", choices=["jev", "kev"], default="jev")
    a = ap.parse_args()
    if a.cmd == "dump":
        for fx in fixtures():
            print(json.dumps(request(fx), ensure_ascii=False))
    elif a.cmd == "score":
        from kimeru.profiles import PROFILES
        score(a.answers, PROFILES[a.profile])
    elif a.cmd == "tune":
        tune(a.answers)
    else:
        live(a.out, a.backend)
