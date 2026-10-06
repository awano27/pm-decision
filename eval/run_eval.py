"""Question-quality eval for the shipped graphs.

  python eval/run_eval.py dump  > requests.jsonl        # one Jev request per fixture (also: --fixtures FILE)
  python eval/run_eval.py score answers.jsonl           # answers: {"id", "answers": {...}} per line
  python eval/run_eval.py score answers.jsonl --fixtures FILE   # your own reviewed decisions (state folder: fixtures_user.jsonl)
  python eval/run_eval.py tune  answers.jsonl [--fixtures FILE]
  python eval/run_eval.py live  [--out answers.jsonl]   # call Jev directly (TYPESAFE_API_KEY); --backend kev for the local Kev

Each fixture labels some judge / plan nodes of its graph. All labeled questions for
one event go in ONE request (Jev answers them independently).
Labels: choice -> "opt" or ["opt", ...]; noul -> true/false; score -> [lo, hi] (a range: `kimeru review` writes
[level - 0.5, level + 0.5], so a decimal answer of 2.4 counts as level 2);
plan node -> expected playbook id (or list).

Jev accuracy numbers must not be published (TypeSafe terms); keep answers/results private.

The numbers do not depend on the PC: the coefficients that `kimeru calibrate --apply` wrote to the state folder are not
read, unless you pass --user-thresholds (the output then says so).
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kimeru import graph, plan, profiles  # noqa: E402
from kimeru.backends import jev_questions  # noqa: E402
from kimeru.events import state_of  # noqa: E402

PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = {k: v[0] for k, v in graph.load_dir(ROOT / "graphs", PBS).items()}
DEFAULT_FIXTURES = ROOT / "eval" / "fixtures.jsonl"


def fixtures(path=DEFAULT_FIXTURES):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def labeled_nodes(fx):
    """[(node id, node, label)] for the labels that name a judge or plan node this version of the graph has."""
    g = GRAPHS[fx["event"]["kind"]]
    return [(nid, g["nodes"][nid], exp) for nid, exp in fx["expect"].items()
            if g["nodes"].get(nid, {}).get("kind") in ("judge", "plan")]   # the graph may have changed since a user's label


def plan_question(node):
    ids = plan.allowed(node, PBS)
    return {"type": "choice", "instructions": node.get("instructions", "Which playbook fits the work the project manager must do next?"),
            "criteria": {**{i: PBS[i]["when"] for i in ids}, "none": "None of these playbooks fits"}}


def request(fx):
    qs = {}
    for nid, n, _ in labeled_nodes(fx):
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


def outcomes(ans, profile=None, fx_path=DEFAULT_FIXTURES):
    """(correct, unsure, confidently_wrong, total) with the given threshold profile."""
    ok = un = wrong = n = 0
    for fx in fixtures(fx_path):
        if fx["id"] not in ans:
            continue
        for nid, node, exp in labeled_nodes(fx):
            a = ans[fx["id"]].get(nid)
            want_type = "choice" if node["kind"] == "plan" else node["question"]["type"]
            if a is None or a.get("type", want_type) != want_type:
                continue
            good, edge, _ = judge(node, exp, a, profile)
            n += 1
            ok += good
            un += (not good) and edge == "unsure"
            wrong += (not good) and edge != "unsure"
    return ok, un, wrong, n


def _note(use_user_thresholds):
    """One line when the user's own coefficients are read (nothing when they are not, or there are none)."""
    if use_user_thresholds:
        line = profiles.overrides_note()
        if line:
            print(line)


def tune(answers_path, fx_path=DEFAULT_FIXTURES, use_user_thresholds=False):
    """Grid-search the two profile knobs. Pick the most automation that does not add a
    single confidently-wrong decision over the untuned (Jev) thresholds; ties go to the
    more conservative (larger) scales. The user's local coefficients are not read unless asked."""
    _note(use_user_thresholds)
    with profiles.ignoring_overrides(not use_user_thresholds):
        return _tune(answers_path, fx_path)


def _tune(answers_path, fx_path):
    ans = load_answers(answers_path)
    base = outcomes(ans, None, fx_path)
    print(f"untuned: correct={base[0]} unsure={base[1]} confident-wrong={base[2]} / {base[3]}")
    grid = [round(0.40 + 0.05 * i, 2) for i in range(13)]   # 0.40 .. 1.00
    best = None
    rows = []
    for cs in grid:
        for ns in grid:
            ok, un, wrong, n = outcomes(ans, {"conf_scale": cs, "noul_scale": ns}, fx_path)
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


def score(answers_path, profile=None, fx_path=DEFAULT_FIXTURES, use_user_thresholds=False):
    _note(use_user_thresholds)
    with profiles.ignoring_overrides(not use_user_thresholds):
        return _score(answers_path, profile, fx_path)


def _score(answers_path, profile, fx_path):
    ans = load_answers(answers_path)
    per_node, misses, skipped = {}, [], []
    for fx in fixtures(fx_path):
        asked = request(fx)["questions"]
        if fx["id"] not in ans:
            skipped += [f"{fx['id']}/{nid}" for nid in asked]
            continue
        g = GRAPHS[fx["event"]["kind"]]
        for nid, node, exp in labeled_nodes(fx):
            a = ans[fx["id"]].get(nid)
            want_type = "choice" if node["kind"] == "plan" else node["question"]["type"]
            if a is None or a.get("type", want_type) != want_type:
                if nid in asked:
                    skipped.append(f"{fx['id']}/{nid}")   # missing, or recorded for an older version of the question
                continue
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
    total = sum(c[1] for c in per_node.values())
    print(f"\nscored {total} labeled model questions; not scored (no current answer): {len(skipped)}"
          + (f" -> {', '.join(skipped[:10])}{' ...' if len(skipped) > 10 else ''}" if skipped else ""))
    print(f"\nmisses ({len(misses)}):")
    print("\n".join(misses) or "  none")


def live(out, backend="jev", use_user_thresholds=False, fx_path=DEFAULT_FIXTURES):
    import time
    from kimeru.backends import ClmBackend, JevBackend, KevBackend
    be = {"kev": KevBackend, "clm": ClmBackend}.get(backend, JevBackend)()
    times = []
    with open(out, "w", encoding="utf-8") as f:
        for fx in fixtures(fx_path):
            r = request(fx)
            if not r["questions"]:
                continue   # only rule-decided nodes labeled: nothing to ask a model
            t0 = time.time()
            ans = be.ask(r["state"], r["questions"])
            times.append(time.time() - t0)
            f.write(json.dumps({"id": r["id"], "answers": ans}, ensure_ascii=False) + "\n")
    times.sort()
    print(f"latency per request: median {times[len(times) // 2]:.2f}s, max {times[-1]:.2f}s ({len(times)} requests)")
    from kimeru.profiles import PROFILES
    print(f"scored with the '{backend}' threshold profile")
    score(out, PROFILES.get(backend, PROFILES["jev"]), fx_path, use_user_thresholds)


def add_thresholds_flag(parser):
    parser.add_argument("--user-thresholds", action="store_true",
                        help="also read the coefficients `kimeru calibrate --apply` wrote (default: not read)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("dump")
    p.add_argument("--fixtures", default=str(DEFAULT_FIXTURES), help="labeled events (your own: <state folder>/fixtures_user.jsonl)")
    p = sub.add_parser("score")
    p.add_argument("answers")
    p.add_argument("--profile", choices=["jev", "kev", "clm"], default="jev", help="threshold profile (kimeru/profiles.py)")
    p.add_argument("--fixtures", default=str(DEFAULT_FIXTURES), help="labeled events (your own: <state folder>/fixtures_user.jsonl)")
    add_thresholds_flag(p)
    p = sub.add_parser("tune", help="grid-search profile knobs on recorded answers")
    p.add_argument("answers")
    p.add_argument("--fixtures", default=str(DEFAULT_FIXTURES), help="labeled events (your own: <state folder>/fixtures_user.jsonl)")
    add_thresholds_flag(p)
    p = sub.add_parser("live")
    p.add_argument("--out", default="answers.jsonl")
    p.add_argument("--backend", choices=["jev", "kev", "clm"], default="jev")
    p.add_argument("--fixtures", default=str(DEFAULT_FIXTURES), help="labeled events (your own: <state folder>/fixtures_user.jsonl)")
    add_thresholds_flag(p)
    a = ap.parse_args()
    if a.cmd == "dump":
        for fx in fixtures(a.fixtures):
            r = request(fx)
            if r["questions"]:
                print(json.dumps(r, ensure_ascii=False))
    elif a.cmd == "score":
        from kimeru.profiles import PROFILES
        score(a.answers, PROFILES[a.profile], a.fixtures, a.user_thresholds)
    elif a.cmd == "tune":
        tune(a.answers, a.fixtures, a.user_thresholds)
    else:
        live(a.out, a.backend, a.user_thresholds, a.fixtures)
