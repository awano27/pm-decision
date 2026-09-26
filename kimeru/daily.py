"""One cycle of the PM's day, meant to run every few minutes (see `kimeru schedule`).

  pull (teams; ado/alerts when configured) -> judge the inbox -> post the human queue to
  the Teams self chat -> read OK/NG/保留 replies -> morning brief once a day

Each step is isolated: a failing step is logged and the cycle continues, so one
broken source (e.g. Teams closed) does not stop the rest.
"""
import json
from datetime import datetime
from pathlib import Path

from . import actions, brief as brief_mod, events, graph, notify, pull


def _log(out, rec):
    p = Path(out) / "daily.log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **rec}, ensure_ascii=False) + "\n")


def _step(out, name, fn, report):
    try:
        report[name] = fn()
    except Exception as e:  # keep the cycle alive; the log says what broke
        report[name] = f"error: {type(e).__name__}: {e}"
        _log(out, {"step": name, "error": report[name]})


def process_inbox(inbox, out, graphs, backend, playbooks, process):
    inbox = Path(inbox)
    done = inbox / "done"
    done.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(inbox.glob("*.json")):
        try:
            n += len(process(json.loads(f.read_text(encoding="utf-8")), graphs, backend, Path(out), playbooks))
            f.replace(done / f.name)
        except RuntimeError as e:
            if "not reachable" in str(e):   # judge backend (e.g. local Kev) not up yet: keep files for the next cycle
                _log(out, {"step": "judge", "waiting": str(e)})
                break
            _log(out, {"step": "judge", "file": f.name, "error": f"{type(e).__name__}: {e}"})
            f.replace(done / (f.name + ".error"))
        except Exception as e:
            _log(out, {"step": "judge", "file": f.name, "error": f"{type(e).__name__}: {e}"})
            f.replace(done / (f.name + ".error"))
    return n


def cycle(out, inbox, graphs, backend, playbooks, process, bridge=None, send=False,
          ado=None, subscription=None, brief_hour=8, now=None):
    """Run one cycle. Returns a small report dict (also written to daily.log.jsonl)."""
    now = now or datetime.now()
    out = Path(out)
    bridge = bridge or notify.PowerShellBridge()
    r = {}
    _step(out, "pull_teams", lambda: pull.pull_teams(inbox, out, bridge=bridge), r)
    if ado:
        _step(out, "pull_ado", lambda: pull.pull_ado(ado[0], ado[1], inbox, out), r)
    if subscription:
        _step(out, "pull_alerts", lambda: pull.pull_alerts(subscription, inbox, out), r)
    _step(out, "judge", lambda: process_inbox(inbox, out, graphs, backend, playbooks, process), r)
    _step(out, "notify", lambda: notify.notify(out, bridge, send=send), r)
    _step(out, "approvals", lambda: [f"#{c['id']}:{c['status']}" for c in notify.collect(out, bridge)], r)

    st_path = out / "daily_state.json"
    st = json.loads(st_path.read_text(encoding="utf-8")) if st_path.exists() else {}
    today = now.strftime("%Y-%m-%d")
    if now.hour >= brief_hour and st.get("brief_date") != today:
        def do_brief():
            text, ranked = brief_mod.build(out, backend, date=today)
            bridge.post(text, send)
            return len(ranked)
        _step(out, "brief", do_brief, r)
        if not str(r.get("brief", "")).startswith("error") and send:
            st["brief_date"] = today
            st_path.write_text(json.dumps(st), encoding="utf-8")
    _log(out, {"step": "cycle", "report": r})
    return r
