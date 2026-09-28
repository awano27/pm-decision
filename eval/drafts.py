"""Draft quality check for the writer (writer.py) on fixture events of every kind.

  python eval/drafts.py --backend kev --writer copilot [--n 20] [--kinds teams.chat,monitor.alert] [--out drafts.jsonl]

For each event: judge with the backend, draft every follow-up text with the writer, then check:
  invented   dates / numbers / @names / #ids in a draft that are not in the material
  long       a reply / comment over 200 characters, a post over 300
  failed     writer error or no JSON (template kept)
Reading the drafts is still the real review: open the --out file.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from kimeru import graph, plan, writer  # noqa: E402
from kimeru.backends import JevBackend, KevBackend, StubBackend  # noqa: E402

LIMIT = {"teams.reply": 200, "ado.comment": 200, "teams.post": 300, "ado.create": 400}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["stub", "jev", "kev"], default="kev")
    ap.add_argument("--writer", default="copilot")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--kinds", default="teams.chat,monitor.alert,ado.workitem.created,meeting.item")
    ap.add_argument("--fixtures", default="fixtures.jsonl")
    ap.add_argument("--out")
    a = ap.parse_args()
    be = {"kev": KevBackend, "jev": JevBackend}.get(a.backend, StubBackend)()
    w = writer.get_writer(a.writer)
    pbs = plan.load_playbooks(ROOT / "playbooks")
    graphs = {k: v[0] for k, v in graph.load_dir(ROOT / "graphs", pbs).items()}
    kinds = a.kinds.split(",")
    fx = [json.loads(l) for l in (ROOT / "eval" / a.fixtures).read_text(encoding="utf-8").splitlines() if l.strip()]
    per_kind = max(1, a.n // len(kinds))
    fx = [x for k in kinds for x in [f for f in fx if f["event"]["kind"] == k][:per_kind]]
    rows, secs = [], []
    for x in fx:
        ev = x["event"]
        res = graph.run(graphs[ev["kind"]], ev, be, playbooks=pbs)
        if not writer.targets(res):
            continue
        t0 = time.time()
        drafted = writer.apply(res, ev, w)
        secs.append(time.time() - t0)
        items = [{"type": d["type"], "title": d.get("title"), "text": d[writer.FIELD[d["type"]]],
                  "template": d.get("template_text"), "unverified": d.get("unverified", []),
                  "long": len(d[writer.FIELD[d["type"]]]) > LIMIT[d["type"]]} for d in drafted]
        rows.append({"id": x["id"], "kind": ev["kind"], "node": res["node"], "error": res.get("writer_error"),
                     "targets": len(writer.targets(res)), "drafted": items, "sec": round(secs[-1], 1)})
        flags = sum(bool(i["unverified"]) for i in items)
        print(f"{x['id']:12} {res['node']:18} {secs[-1]:5.1f}s drafted {len(items)}/{len(writer.targets(res))}"
              f"{'  invented=' + str(flags) if flags else ''}{'  ERROR ' + res['writer_error'][:60] if res.get('writer_error') else ''}",
              flush=True)
    if a.out:
        Path(a.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    if not rows:
        print("no event produced text to draft")
        return
    items = [i for r in rows for i in r["drafted"]]
    secs.sort()
    print(f"\nevents={len(rows)} texts={sum(r['targets'] for r in rows)} drafted={len(items)} "
          f"failed_events={sum(1 for r in rows if r['error'])} invented={sum(bool(i['unverified']) for i in items)} "
          f"long={sum(i['long'] for i in items)} median={secs[len(secs) // 2]:.1f}s max={secs[-1]:.1f}s")


if __name__ == "__main__":
    main()
