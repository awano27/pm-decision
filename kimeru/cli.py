"""kimeru CLI.

  python -m kimeru validate [--graphs DIR]
  python -m kimeru run FILE... [--backend stub|jev] [--out DIR]
  python -m kimeru watch INBOX [--backend ...] [--interval 5]
  python -m kimeru digest [--out DIR]
  python -m kimeru notify [--send]      # queue -> Teams self chat
  python -m kimeru approvals            # OK/NG/保留 replies -> execute approved
  python -m kimeru brief [--post [--send]]  # today's top 3 (+ pending approvals)
  python -m kimeru pull ado --org O --project P [--inbox inbox]   # uses `az login`
  python -m kimeru pull alerts --subscription S [--inbox inbox]
  python -m kimeru pull teams [--inbox inbox]      # Teams chat list on screen: 1:1 + mentions
  python -m kimeru --backend kev daily [--once] [--send]   # the whole day's loop
  python -m kimeru --backend kev schedule install [--minutes 5]   # every N min, no admin
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import actions, events, graph
from . import plan as planner
from .backends import ClmBackend, JevBackend, KevBackend, StubBackend

HERE = Path(__file__).resolve().parent.parent
DEFAULT_PLAYBOOKS = HERE / "playbooks"


def _backend(name, model):
    if name == "jev":
        return JevBackend(model=model)
    if name == "kev":
        return KevBackend()
    if name == "clm":
        return ClmBackend()
    return StubBackend()


def _append(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def process(payload, graphs, backend, out, playbooks=None):
    if playbooks is None:
        playbooks = planner.load_playbooks(DEFAULT_PLAYBOOKS)
    results = []
    for ev in events.normalize(payload):
        for g in graphs.get(ev["kind"], []):
            res = graph.run(g, ev, backend, playbooks=playbooks)
            # decide runs now; advise actions are only proposed until approved (see notify.collect)
            res["executed"] = [actions.execute(a, dry_run=True) for a in res["actions"]] if res["outcome"] == "decide" else []
            res["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            res["summary"] = events.summary(ev)
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
    if r.get("plan"):
        lines.append(f"  plan: {r['plan']['title']} / {r['plan']['summary']}")
    for e in r["executed"]:
        lines.append(f"  {e['status']}: {e['action'].get('type')} {json.dumps({k: v for k, v in e['action'].items() if k != 'type'}, ensure_ascii=False)}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kimeru")
    ap.add_argument("--graphs", default=str(HERE / "graphs"))
    ap.add_argument("--playbooks", default=str(DEFAULT_PLAYBOOKS))
    ap.add_argument("--out", default="out")
    ap.add_argument("--backend", choices=["stub", "jev", "kev", "clm"], default=os.environ.get("KIMERU_BACKEND", "stub"))
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
    p_b = sub.add_parser("brief", help="rank today's work; print or post to Teams self chat")
    p_b.add_argument("--top", type=int, default=3)
    p_b.add_argument("--post", action="store_true", help="paste into Teams self chat")
    p_b.add_argument("--send", action="store_true", help="with --post: press Enter")
    p_demo = sub.add_parser("demo", help="replay a scripted PM day (simulated Teams, real judge) for presentations")
    p_demo.add_argument("--scenario", default=str(HERE / "examples" / "demo_day.json"))
    p_demo.add_argument("--pace", type=float, default=1.5, help="seconds between lines (0 = no pauses)")
    p_demo.add_argument("--record", help="also save the run (lines + report) to this file, for --replay")
    p_demo.add_argument("--replay", help="print a recorded run with the same pacing; no model needed")
    p_demo.add_argument("--step", action="store_true", help="presenter mode: wait for Enter before each event")
    p_rep = sub.add_parser("report", help="HTML page of the decisions in --out")
    p_rep.add_argument("--html", help="output file (default: <out>/report.html)")
    p_d = sub.add_parser("daily", help="pull -> judge -> self-chat queue -> approvals -> morning brief")
    p_d.add_argument("--inbox", default="inbox")
    p_d.add_argument("--once", action="store_true", help="one cycle and exit (for the scheduler)")
    p_d.add_argument("--interval", type=int, default=300, help="seconds between cycles when looping")
    p_d.add_argument("--send", action="store_true", help="actually send self-chat posts (default: paste only)")
    p_d.add_argument("--ado-org")
    p_d.add_argument("--ado-project")
    p_d.add_argument("--subscription")
    p_d.add_argument("--brief-hour", type=int, default=8)
    p_s = sub.add_parser("schedule", help="register/remove `daily --once --send` every N minutes (Task Scheduler, no admin)")
    p_s.add_argument("action", choices=["install", "remove", "status"])
    p_s.add_argument("--minutes", type=int, default=5)
    p_s.add_argument("--extra", default="", help="extra args for daily, e.g. \"--ado-org o --ado-project p\"")
    p_p = sub.add_parser("pull", help="poll ADO / Azure Monitor with your az login into an inbox")
    p_p.add_argument("source", choices=["ado", "alerts", "teams"])
    p_p.add_argument("--include-existing", action="store_true", help="teams: also emit chats already on screen at the first poll")
    p_p.add_argument("--inbox", default="inbox")
    p_p.add_argument("--org")
    p_p.add_argument("--project")
    p_p.add_argument("--subscription")
    a = ap.parse_args(argv)
    out = Path(a.out)

    if a.cmd == "schedule":
        return schedule(a)

    if a.cmd == "pull":
        from . import pull
        if a.source == "ado" and not (a.org and a.project):
            ap.error("pull ado needs --org and --project")
        if a.source == "alerts" and not a.subscription:
            ap.error("pull alerts needs --subscription")
        try:
            if a.source == "teams":
                n = pull.pull_teams(a.inbox, out, include_existing=a.include_existing)
            elif a.source == "ado":
                n = pull.pull_ado(a.org, a.project, a.inbox, out)
            else:
                n = pull.pull_alerts(a.subscription, a.inbox, out)
        except (pull.PullError, RuntimeError) as e:
            print(f"pull {a.source} failed: {e}", file=sys.stderr)
            return 1
        print(f"{a.source}: {n} new -> {a.inbox}")
        return 0

    if a.cmd == "brief":
        from . import brief as br
        text, _ = br.build(out, _backend(a.backend, a.model), top=a.top)
        print(text)
        if a.post:
            from . import notify as nt
            nt.PowerShellBridge().post(text, a.send)
            print("sent" if a.send else "pasted (not sent)")
        return 0

    if a.cmd in ("notify", "approvals"):
        from . import notify as nt
        bridge = nt.PowerShellBridge()
        try:
            if a.cmd == "notify":
                ids = nt.notify(out, bridge, send=a.send)
                print(f"{'posted' if a.send else 'pasted (not sent)'}: {ids}")
            else:
                for ch in nt.collect(out, bridge):
                    print(f"#{ch['id']} -> {ch['status']}" + (f" ({len(ch['executed'])} actions planned)" if "executed" in ch else ""))
        except Exception as e:  # one readable line instead of a traceback (the check script records it)
            print(f"{a.cmd} failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        return 0

    pbs = planner.load_playbooks(a.playbooks)
    if a.cmd == "validate":
        gs = graph.load_dir(a.graphs, pbs)
        for pid, pb in pbs.items():
            print(f"ok  playbook {pid} ({len(pb['steps'])} steps)")
        for kind, lst in gs.items():
            for g in lst:
                print(f"ok  {g['name']} ({kind}, {len(g['nodes'])} nodes)")
        return 0

    if a.cmd == "digest":
        return digest(out)

    if a.cmd == "report":
        from . import report
        page = Path(a.html) if a.html else out / "report.html"
        page.write_text(report.build(out, graph.load_dir(a.graphs, pbs)), encoding="utf-8")
        print(page)
        return 0

    gs = graph.load_dir(a.graphs, pbs)
    be = _backend(a.backend, a.model)
    if a.cmd == "demo":
        from . import demo, report
        page = out / "report.html"
        if a.replay:   # fallback for a live talk: same output, no model
            rec = json.loads(Path(a.replay).read_text(encoding="utf-8"))
            demo.replay(a.replay, pace=a.pace, step=a.step)
            if rec.get("report_html"):
                out.mkdir(parents=True, exist_ok=True)
                page.write_text(rec["report_html"], encoding="utf-8")
                print(f"    レポート: {page}")
            return 0
        run = lambda: demo.run(a.scenario, gs, be, pbs, process, out, pace=a.pace, step=a.step)
        demo.record_to(a.record, run) if a.record else run()
        page.write_text(report.build(out, gs, "kimeru デモ: PM の 1 日"), encoding="utf-8")
        print(f"    レポート: {page}")
        if a.record:   # keep the report with the recording so a replay can show it too
            rec = json.loads(Path(a.record).read_text(encoding="utf-8"))
            rec["report_html"] = page.read_text(encoding="utf-8")
            Path(a.record).write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            print(f"    記録: {a.record}（当日は --replay {a.record} で再生）")
        return 0
    if a.cmd == "daily":
        from . import daily
        ado = (a.ado_org, a.ado_project) if a.ado_org and a.ado_project else None
        while True:
            r = daily.cycle(out, a.inbox, gs, be, pbs, process, send=a.send, ado=ado,
                            subscription=a.subscription, brief_hour=a.brief_hour)
            print(datetime.now().strftime("%H:%M"), json.dumps(r, ensure_ascii=False), flush=True)
            if a.once:
                return 0
            time.sleep(a.interval)
    if a.cmd == "run":
        for f in a.files:
            for r in process(json.loads(Path(f).read_text(encoding="utf-8")), gs, be, out, pbs):
                print(_fmt(r))
        return 0

    inbox = Path(a.inbox)
    done = inbox / "done"
    done.mkdir(parents=True, exist_ok=True)
    while True:
        for f in sorted(inbox.glob("*.json")):
            try:
                for r in process(json.loads(f.read_text(encoding="utf-8")), gs, be, out, pbs):
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


TASK = "kimeru-daily"


def schedule(a):
    """Windows Task Scheduler entry that runs one daily cycle every N minutes as the
    current user (no admin). The task calls a tiny hidden VBS runner in the data folder,
    so no console window flashes and the /TR command stays short whatever the repo path."""
    import subprocess
    if a.action == "remove":
        return subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"]).returncode
    if a.action == "status":
        return subprocess.run(["schtasks", "/Query", "/TN", TASK, "/FO", "LIST"]).returncode
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.exists() else exe
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    backend = f"--backend {a.backend}" if a.backend != "stub" else ""
    line = (f'cmd /c cd /d "{HERE}" && "{runner}" -m kimeru --out "{out}" {backend} daily --once --send '
            f'--inbox "{out / "inbox"}" {a.extra}').strip()
    vbs = out / "run-daily.vbs"
    # VBS string literal: double every quote; window style 0 = hidden, wait for completion
    vbs.write_text('CreateObject("WScript.Shell").Run "' + line.replace('"', '""') + '", 0, True\n', encoding="utf-16")  # WSH reads UTF-8 as ANSI: Japanese paths break
    tr = f'wscript.exe "{vbs}"'
    r = subprocess.run(["schtasks", "/Create", "/TN", TASK, "/SC", "MINUTE", "/MO", str(a.minutes),
                        "/TR", tr, "/F", "/RL", "LIMITED"])
    print(("installed: " if r.returncode == 0 else "failed: ") + tr)
    print("runs: " + line)
    return r.returncode
