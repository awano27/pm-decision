"""A PM's day through the real pipeline, with only Teams replaced by an in-memory fake.

  python eval/day_run.py --backend kev --writer copilot [--n 8] [--out day-out]

Drops fixture events of every kind into an inbox, runs daily cycles (judge -> writer drafts ->
approval posts -> replies -> actions -> morning brief) and checks what a PM would see:
  - every approval post shows the source and the full drafted text
  - "修正 N ..." redrafts under the same number; "OK N" runs the approved text; "NG N" runs nothing
  - no text drafted by the writer was executed without an OK
  - the same event dropped twice is decided once
Prints a timeline and a pass/fail list. No Teams, no company data.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from kimeru import daily, graph, notify, plan, writer  # noqa: E402
from kimeru.backends import JevBackend, KevBackend, StubBackend  # noqa: E402
from kimeru.cli import process  # noqa: E402


class FakeTeams:
    """Self chat in memory: posts become "P:N", the PM's replies "R:...". Chat list is empty."""

    def __init__(self):
        self.timeline, self.posts = [], []

    def chats(self):
        return []

    def readchat(self, chat_id, count=5):
        """Opening a chat to read it in full: the messages set in `chat_messages` (nothing is opened when it is unknown)."""
        self.opened = getattr(self, "opened", []) + [chat_id]
        msgs = getattr(self, "chat_messages", {}).get(chat_id)
        if msgs is None:
            raise RuntimeError("the chat is not in the list on screen; nothing was opened")
        return {"ok": True, "opened": True, "returned": True, "messages": [{"text": m, "sender": "", "time": ""} for m in msgs[-count:]]}

    def post(self, text, send):
        self.posts.append(text)
        if send and text.startswith("[kimeru #"):
            self.timeline.append("P:" + text.split("#", 1)[1].split("]", 1)[0])
        return {"ok": True, "typed": True, "sent": send}

    def read(self):
        return {"ok": True, "timeline": list(self.timeline)}

    def reply(self, line):
        self.timeline.append("R:" + line)


def payloads(n):
    """n raw payloads per kind, built from the fixtures (the normalizers accept these shapes)."""
    fx = [json.loads(l) for l in (ROOT / "eval" / "fixtures.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    out = []
    for kind in ("teams.chat", "monitor.alert", "ado.workitem.created", "meeting.item"):
        for x in [f for f in fx if f["event"]["kind"] == kind][:n]:
            e = x["event"]
            if kind == "teams.chat":
                p = {"id": e["id"], "chatId": e.get("chat_id") or "19:c@unq.gbl.spaces", "createdDateTime": "2026-10-01T09:00:00Z",
                     "from": {"user": {"displayName": e.get("author") or "同僚"}}, "body": {"contentType": "text", "content": e["text"]}}
            elif kind == "monitor.alert":
                p = {"schemaId": "azureMonitorCommonAlertSchema", "data": {"essentials": {
                    "alertId": e["id"], "alertRule": e.get("rule"), "severity": e.get("severity"),
                    "monitorCondition": e.get("condition"), "firedDateTime": "2026-10-01T09:00:00Z",
                    "description": e.get("description", "")}, "alertContext": e.get("context") or {}}}
            elif kind == "ado.workitem.created":
                p = {"eventType": "workitem.created", "resource": {"id": e["id"], "fields": {
                    "System.Title": e.get("title", ""), "System.Description": e.get("description", ""),
                    "System.WorkItemType": e.get("work_item_type", "Bug"),
                    "Microsoft.VSTS.Common.AcceptanceCriteria": e.get("acceptance_criteria", "")}}}
            else:
                p = {"title": e.get("meeting") or "週次定例 2026-10-01", "date": "2026-10-01", "text": "- " + e["item"]}
            out.append((x["id"], p))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["stub", "jev", "kev"], default="kev")
    ap.add_argument("--writer", default="copilot")
    ap.add_argument("--n", type=int, default=2, help="events per kind")
    ap.add_argument("--out", default=str(ROOT / "out" / f"day-{datetime.now():%Y%m%d-%H%M%S}"))
    a = ap.parse_args()
    be = {"kev": KevBackend, "jev": JevBackend}.get(a.backend, StubBackend)()
    os.environ["KIMERU_WRITER"] = a.writer   # approvals (修正 N) use the same writer
    w = writer.get_writer(a.writer)
    pbs = plan.load_playbooks(ROOT / "playbooks")
    graphs = graph.load_dir(ROOT / "graphs", pbs)
    out, inbox, t = Path(a.out), Path(a.out) / "inbox", FakeTeams()
    inbox.mkdir(parents=True, exist_ok=True)
    proc = lambda p, g, b, o, pb, dedup=False: process(p, g, b, o, pb, writer=w, dedup=dedup)
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    items = payloads(a.n)
    for fid, p in items:
        (inbox / f"{fid}.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    t0 = time.time()
    r = daily.cycle(out, inbox, graphs, be, pbs, proc, bridge=t, send=True, now=datetime(2026, 10, 1, 9, 0))
    judge_s = time.time() - t0
    print(f"cycle 1: judged {r.get('judge')} events in {judge_s:.0f}s, posted {r.get('notify')}")
    check("all events judged", r.get("judge") == len(items), f"{r.get('judge')}/{len(items)}")

    ap_data = json.loads((out / "approvals.json").read_text(encoding="utf-8")) if (out / "approvals.json").exists() else {"items": {}}
    posted = {n: it for n, it in ap_data["items"].items() if it["posted"]}
    drafted_posts = [p for p in t.posts if "の下書き（" in p]
    check("approval posts show the drafted text", all("元:" in p for p in drafted_posts), f"{len(drafted_posts)} posts with drafts")
    for p in t.posts[:6]:
        print("  | " + p.replace("\n", "\n  | ")[:700])

    # the PM answers: first item -> 修正, second -> OK, third -> NG
    nums = sorted(posted, key=int)
    if len(nums) >= 3:
        n_fix, n_ok, n_ng = nums[0], nums[1], nums[2]
        t.reply(f"修正 {n_fix} もっと短く、敬語は丁寧に")
        r2 = daily.cycle(out, inbox, graphs, be, pbs, proc, bridge=t, send=True, now=datetime(2026, 10, 1, 9, 5))
        print(f"cycle 2 (修正 {n_fix}): approvals {r2.get('approvals')}")
        check("修正 redrafts", f"#{n_fix}:redrafted" in (r2.get("approvals") or []), str(r2.get("approvals")))
        t.reply(f"OK {n_ok}")
        t.reply(f"NG {n_ng}")
        r3 = daily.cycle(out, inbox, graphs, be, pbs, proc, bridge=t, send=True, now=datetime(2026, 10, 1, 9, 10))
        print(f"cycle 3 (OK {n_ok}, NG {n_ng}): reposted {r3.get('notify')}, approvals {r3.get('approvals')}")
        check("the redraft is reposted under the same number", int(n_fix) in (r3.get("notify") or []), str(r3.get("notify")))
        redraft = [p for p in t.posts if p.startswith(f"[kimeru #{n_fix}]")]
        if len(redraft) >= 2:
            print("  redraft of #" + n_fix + ":\n  | " + redraft[-1].replace("\n", "\n  | ")[:600])
        check("OK and NG applied", f"#{n_ok}:approved" in (r3.get("approvals") or []) and f"#{n_ng}:rejected" in (r3.get("approvals") or []))
        log = [json.loads(l) for l in (out / "approvals.log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        ran = [c for c in log if c.get("status") == "approved"]
        check("only the approved item ran", len(ran) == 1 and all(c["id"] == int(n_ok) for c in ran))
    else:
        check("at least 3 approval posts to answer", False, f"{len(nums)} posted")

    # drafted text never executed without an OK
    decisions = [json.loads(l) for l in (out / "decisions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    leaked = [d["event_id"] for d in decisions for e in d.get("executed", []) if e["action"].get("drafted_by")]
    check("no drafted text executed before approval", not leaked, ", ".join(map(str, leaked)))
    flagged = [a for d in decisions for a in d.get("actions", []) if a.get("unverified")]
    check("no invented dates/numbers in drafts", not flagged, f"{len(flagged)} flagged")

    # the same file dropped again is not decided twice
    fid, p = items[0]
    (inbox / f"{fid}-again.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    before = len(decisions)
    daily.cycle(out, inbox, graphs, be, pbs, proc, bridge=t, send=True, now=datetime(2026, 10, 1, 9, 15))
    after = len([l for l in (out / "decisions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()])
    check("re-dropped event is not decided twice", after == before, f"{before} -> {after}")

    r5 = daily.cycle(out, inbox, graphs, be, pbs, proc, bridge=t, send=True, now=datetime(2026, 10, 2, 8, 5))
    brief = [p for p in t.posts if p.startswith("[kimeru brief")]
    check("next morning brief posted", bool(brief) and r5.get("brief") is not None)
    if brief:
        print("  | " + brief[-1].replace("\n", "\n  | "))

    print(f"\njudge+draft time for {len(items)} events: {judge_s:.0f}s")
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}{'  (' + detail + ')' if detail else ''}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
