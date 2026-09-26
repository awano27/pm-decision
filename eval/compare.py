"""Compare backends on the same recorded answers (eval/run_eval.py live output).

  python eval/compare.py jev=answers_jev.jsonl kev=answers_kev.jsonl [--profile-kev kev]

Per backend, with its production threshold profile:
  auto-correct      decided automatically and right
  to-human          routed to "unsure" (safe: a person decides)
  confident-wrong   decided automatically and wrong (the costly outcome)
  top-1 accuracy    the model's first choice, ignoring thresholds
plus per-graph breakdown, pairwise agreement, and every confident-wrong item.
Jev numbers must stay private (TypeSafe terms): write the output outside the repo.
"""
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("run_eval", HERE / "run_eval.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)
from kimeru.profiles import PROFILES  # noqa: E402  (run_eval put the repo on sys.path)


def top1(node, exp, a):
    t = "choice" if node["kind"] == "plan" else node["question"]["type"]
    if t == "noul":
        return (a["noul"] >= 0.5) == exp, a["noul"] >= 0.5
    if t == "choice":
        return a["choice"] in (exp if isinstance(exp, list) else [exp]), a["choice"]
    return exp[0] <= a["score"] <= exp[1], round(a["score"])


def evaluate(ans, profile):
    rows = []
    for fx in ev.fixtures():
        g = ev.GRAPHS[fx["event"]["kind"]]
        for nid, exp in fx["expect"].items():
            node, a = g["nodes"][nid], ans.get(fx["id"], {}).get(nid)
            if a is None:
                rows.append({"key": (fx["id"], nid), "graph": g["name"], "missing": True})
                continue
            good, edge, detail = ev.judge(node, exp, a, profile)
            t_ok, t_val = top1(node, exp, a)
            rows.append({"key": (fx["id"], nid), "graph": g["name"], "missing": False,
                         "outcome": "correct" if good else ("human" if edge == "unsure" else "wrong"),
                         "top1": t_ok, "top1_val": t_val, "detail": detail, "want": exp})
    return rows


def main(argv):
    specs = [a.split("=", 1) for a in argv if "=" in a and not a.startswith("--")]
    prof = {"jev": PROFILES["jev"], "kev": PROFILES["kev"]}
    res = {name: evaluate(ev.load_answers(path), prof.get(name, PROFILES["jev"])) for name, path in specs}
    names = [n for n, _ in specs]

    def pct(x, n):
        return f"{x}/{n} ({x / n:.0%})" if n else "-"

    print("## Overall (production thresholds)\n")
    print("| backend | questions | auto-correct | to-human | confident-wrong | top-1 accuracy | missing |")
    print("|---|---|---|---|---|---|---|")
    for n in names:
        r = [x for x in res[n] if not x["missing"]]
        c = defaultdict(int)
        for x in r:
            c[x["outcome"]] += 1
        print(f"| {n} | {len(r)} | {pct(c['correct'], len(r))} | {pct(c['human'], len(r))} | "
              f"{pct(c['wrong'], len(r))} | {pct(sum(x['top1'] for x in r), len(r))} | {len(res[n]) - len(r)} |")

    print("\n## By graph (auto-correct / to-human / confident-wrong)\n")
    graphs = sorted({x["graph"] for x in res[names[0]]})
    print("| graph | " + " | ".join(names) + " |")
    print("|---|" + "---|" * len(names))
    for g in graphs:
        cells = []
        for n in names:
            r = [x for x in res[n] if x["graph"] == g and not x["missing"]]
            c = defaultdict(int)
            for x in r:
                c[x["outcome"]] += 1
            cells.append(f"{c['correct']} / {c['human']} / {c['wrong']} (n={len(r)})")
        print(f"| {g} | " + " | ".join(cells) + " |")

    if len(names) == 2:
        a, b = names
        ra = {x["key"]: x for x in res[a] if not x["missing"]}
        rb = {x["key"]: x for x in res[b] if not x["missing"]}
        both = ra.keys() & rb.keys()
        agree = sum(ra[k]["top1_val"] == rb[k]["top1_val"] for k in both)
        only_a = sum(ra[k]["top1"] and not rb[k]["top1"] for k in both)
        only_b = sum(rb[k]["top1"] and not ra[k]["top1"] for k in both)
        print(f"\n## Agreement\n\n- same first choice: {pct(agree, len(both))}")
        print(f"- first choice right only on {a}: {only_a}; only on {b}: {only_b}")

    for n in names:
        wrong = [x for x in res[n] if not x["missing"] and x["outcome"] == "wrong"]
        print(f"\n## Confident-wrong: {n} ({len(wrong)})\n")
        for x in wrong:
            print(f"- {x['key'][0]} / {x['key'][1]}: want {x['want']}, got {x['detail']}")


if __name__ == "__main__":
    main(sys.argv[1:])
