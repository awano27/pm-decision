"""How much does the judge's answer depend on the length of `text`?

  python eval/text_length.py --backend kev [--n 20] [--caps 200,400,800,1200,2000]

For each teams.chat fixture (eval/fixtures.jsonl, fictional) a long variant is built (the message, then ordinary filler sentences
up to about 2000 characters). Each variant is judged with the judge's text cut at each cap (setting judge_text_max), and the table
shows how many terminal decisions differ from the shortest cap. Pick the largest cap at which they stop differing.
`--backend stub` runs the same table with instant, keyword-based answers (a check of the script, not of the judge).
Jev's numbers must stay private (TypeSafe terms): write nothing produced with --backend jev into the repository.
The coefficients `kimeru calibrate --apply` wrote to the state folder are not read, unless you pass --user-thresholds.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kimeru import profiles, graph, plan  # noqa: E402
from kimeru.backends import JevBackend, KevBackend, StubBackend  # noqa: E402

FILLER = "念のため補足します。関連する背景と、これまでの経緯、確認済みの事項を以下に書きます。"


def long_variant(text, size=2000):
    out = text + " " + FILLER
    while len(out) < size:
        out += FILLER
    return out[:size]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["stub", "kev", "jev"], default="stub")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--caps", default="200,400,800,1200,2000")
    ap.add_argument("--user-thresholds", action="store_true",
                    help="also read the coefficients `kimeru calibrate --apply` wrote (default: not read)")
    a = ap.parse_args()
    backend = {"kev": KevBackend, "jev": JevBackend}.get(a.backend, StubBackend)()
    pbs = plan.load_playbooks(ROOT / "playbooks")
    g = {k: v[0] for k, v in graph.load_dir(ROOT / "graphs", pbs).items()}["teams.chat"]
    fx = [json.loads(l) for l in (ROOT / "eval" / "fixtures.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    fx = [x for x in fx if x["event"]["kind"] == "teams.chat"][:a.n]
    caps = [int(c) for c in a.caps.split(",")]
    results = {}
    try:
        for cap in caps:
            os.environ["KIMERU_JUDGE_TEXT_MAX"] = str(cap)
            results[cap] = [graph.run(g, {**x["event"], "text": long_variant(x["event"]["text"])}, backend, playbooks=pbs)["node"] for x in fx]
    finally:
        os.environ.pop("KIMERU_JUDGE_TEXT_MAX", None)
    base = results[caps[0]]
    print(f"{'cap (characters)':<18}{'events':>7}{'differ from ' + str(caps[0]):>20}")
    for cap in caps:
        print(f"{cap:<18}{len(fx):>7}{sum(x != y for x, y in zip(results[cap], base)):>20}")
    if a.backend == "jev":
        print("(Jev: these numbers must stay private)")


if __name__ == "__main__":
    with profiles.ignoring_overrides("--user-thresholds" not in sys.argv):   # a number printed here must not depend on what one PC has tuned
        main()
