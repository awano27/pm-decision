"""kimeru CLI.

  python -m kimeru validate [--graphs DIR]
  python -m kimeru run FILE... [--backend stub|jev] [--out DIR]
  python -m kimeru watch INBOX [--backend ...] [--interval 5]
  python -m kimeru digest [--out DIR]
  python -m kimeru notify [--send]      # queue -> Teams self chat
  python -m kimeru approvals            # OK/NG/保留 replies -> execute approved
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import actions, events, graph
from .backends import JevBackend, StubBackend

HERE = Path(__file__).resolve().parent.parent


def _backend(name, model):
    return JevBackend(model=model) if name == "jev" else StubBackend()


def _append(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def process(payload, graphs, backend, out):
    results = []
    for ev in events.normalize(payload):
        for g in graphs.get(ev["kind"], []):
            res = graph.run(g, ev, backend)
            # decide runs now; advise actions are only proposed until approved (see notify.collect)
            res["executed"] = [actions.execute(a, dry_run=True) for a in res["actions"]] if res["outcome"] == "decide" else []
            res["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _append(out / "decisions.jsonl", res)
            if res["needs_human"]:
                _append(out / "queue.jsonl", res)
            results.append(res)
    return results


def _fmt(r):
    path = " → ".join(f"{s['node']}[{s['edge']}]" for s in r["path"])
    head = f"[{r['outcome'].upper()}{'/人の確認' if r['needs_human'] else ''}] {r['graph']} #{r['event_id']}"
    lines = [head, f"  path: {path} → {r['node']}"]
    if r["advice"]:
        lines.append(f"  advice: {r['advice']}")
    for e in r["executed"]:
        lines.append(f"  {e['status']}: {e['action'].get('type')} {json.dumps({k: v for k, v in e['action'].items() if k != 'type'}, ensure_ascii=False)}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kimeru")
    ap.add_argument("--graphs", default=str(HERE / "graphs"))
    ap.add_argument("--out", default="out")
    ap.add_argument("--backend", choices=["stub", "jev"], default="stub")
    ap.add_argument("--model", default="jev-latest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    p_run = sub.add_parser("run")
    p_run.add_argument("files", nargs="+")
    p_w = sub.add_parser("watch")
    p_w.add_argument("inbox")
    p_w.add_argument("--interval", type=float, default=5)
    p_w.add_argument("--once", action="store_true")
    sub.add_parser("digest")
    p_n = sub.add_parser("notify", help="post human-queue items to Teams self chat")
    p_n.add_argument("--send", action="store_true", help="actually press Enter (default: paste only)")
    sub.add_parser("approvals", help="read OK/NG/保留 replies from Teams self chat")
    a = ap.parse_args(argv)
    out = Path(a.out)

    if a.cmd in ("notify", "approvals"):
        from . import notify as nt
        bridge = nt.PowerShellBridge()
        if a.cmd == "notify":
            ids = nt.notify(out, bridge, send=a.send)
            print(f"{'posted' if a.send else 'pasted (not sent)'}: {ids}")
        else:
            for ch in nt.collect(out, bridge):
                print(f"#{ch['id']} -> {ch['status']}" + (f" ({len(ch['executed'])} actions planned)" if "executed" in ch else ""))
        return 0

    if a.cmd == "validate":
        gs = graph.load_dir(a.graphs)
        for kind, lst in gs.items():
            for g in lst:
                print(f"ok  {g['name']} ({kind}, {len(g['nodes'])} nodes)")
        return 0

    if a.cmd == "digest":
        return digest(out)

    gs = graph.load_dir(a.graphs)
    be = _backend(a.backend, a.model)
    if a.cmd == "run":
        for f in a.files:
            for r in process(json.loads(Path(f).read_text(encoding="utf-8")), gs, be, out):
                print(_fmt(r))
        return 0

    inbox = Path(a.inbox)
    done = inbox / "done"
    done.mkdir(parents=True, exist_ok=True)
    while True:
        for f in sorted(inbox.glob("*.json")):
            try:
                for r in process(json.loads(f.read_text(encoding="utf-8")), gs, be, out):
                    print(_fmt(r), flush=True)
                f.replace(done / f.name)
            except Exception as e:  # keep the loop alive; park the bad file
                print(f"error {f.name}: {e}", file=sys.stderr, flush=True)
                f.replace(done / (f.name + ".error"))
        if a.once:
            return 0
        time.sleep(a.interval)


def digest(out):
    """Daily summary: counts by outcome and the human queue."""
    p = out / "decisions.jsonl"
    if not p.exists():
        print("no decisions yet")
        return 0
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    n = len(rows)
    dec = sum(r["outcome"] == "decide" for r in rows)
    q = [r for r in rows if r["needs_human"]]
    print(f"判断 {n} 件: 自動決定 {dec} / アドバイス {n - dec}（うち人の確認 {len(q)}）")
    for r in q:
        print(f"- [{r['event_kind']} #{r['event_id']}] {r['advice']}")
    return 0
