"""Draft quality check for the writer (writer.py) on the Teams fixtures.

  python eval/drafts.py --backend kev --writer claude [--n 20] [--out drafts.jsonl]

For each event: judge with the backend, draft with the writer, then check the draft
mechanically:
  invented   dates / numbers / @names / #ids in the draft that are not in the material
  long       reply over 200 characters
  failed     writer error or no JSON (template kept)
Reading the drafts is still the real review: open the --out file.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from kimeru import graph, plan, writer  # noqa: E402
from kimeru.backends import JevBackend, KevBackend, StubBackend  # noqa: E402

TOKENS = re.compile(r"\d+[/月]\d+日?|\d{1,2}:\d{2}|#\d+|@\S+|\d+(?:\.\d+)?\s*(?:%|件|日|時間|人|円|万|週間)")


def material_text(res, ev):
    return writer._material(res, ev)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["stub", "jev", "kev"], default="kev")
    ap.add_argument("--writer", default="claude")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--fixtures", default="fixtures.jsonl")
    ap.add_argument("--out")
    a = ap.parse_args()
    be = {"kev": KevBackend, "jev": JevBackend}.get(a.backend, StubBackend)()
    w = writer.get_writer(a.writer)
    pbs = plan.load_playbooks(ROOT / "playbooks")
    g = graph.load_dir(ROOT / "graphs", pbs)["teams.chat"][0]
    fx = [json.loads(l) for l in (ROOT / "eval" / a.fixtures).read_text(encoding="utf-8").splitlines() if l.strip()]
    fx = [x for x in fx if x["event"]["kind"] == "teams.chat"][: a.n]
    rows, secs = [], []
    for x in fx:
        ev = x["event"]
        res = graph.run(g, ev, be, playbooks=pbs)
        mat = material_text(res, ev)
        t0 = time.time()
        drafted = writer.apply(res, ev, w)
        secs.append(time.time() - t0)
        reply = next((ac["text"] for ac in res["actions"] if ac.get("type") == "teams.reply" and ac.get("drafted_by")), "")
        descs = " ".join(ac.get("description", "") for ac in res["actions"] if ac.get("drafted_by"))
        invented = sorted({t for t in TOKENS.findall(reply + " " + descs) if t not in mat})
        rows.append({"id": x["id"], "node": res["node"], "text": ev.get("text"), "reply": reply,
                     "tasks": [ac.get("description") for ac in res["actions"] if ac.get("type") == "ado.create"],
                     "drafted": drafted, "error": res.get("writer_error"), "invented": invented,
                     "long": len(reply) > 200, "sec": round(secs[-1], 1)})
        print(f"{x['id']:12} {res['node']:16} {secs[-1]:5.1f}s {'OK ' if drafted else 'TPL'} "
              f"{'invented=' + ','.join(invented) if invented else ''} {reply[:60]}", flush=True)
    if a.out:
        Path(a.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    n = len(rows)
    has_reply = [r for r in rows if r["drafted"]]
    print(f"\nevents={n} drafted={len(has_reply)} failed={sum(1 for r in rows if r['error'])} "
          f"invented={sum(1 for r in rows if r['invented'])} long={sum(r['long'] for r in rows)} "
          f"median={sorted(secs)[n // 2]:.1f}s max={max(secs):.1f}s")


if __name__ == "__main__":
    main()
