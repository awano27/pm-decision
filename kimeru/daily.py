"""One cycle of the PM's day, meant to run every few minutes (see `kimeru schedule`).

  pull (teams; ado/alerts when configured) -> judge the inbox -> post the human queue to
  the Teams self chat -> read OK/NG/保留 replies -> morning brief once a day

Each step is isolated: a failing step is logged and the cycle continues, so one
broken source (e.g. Teams closed) does not stop the rest.
"""
import json
from datetime import datetime
from pathlib import Path

from . import actions, brief as brief_mod, events, fsutil, graph, notify, pull
from .backends import BackendUnavailable


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


def _with_key_points(text):
    """Morning brief + "今日の要点" (3 lines) from the writer LLM when one is set (copilot / claude).
    Any failure leaves the brief as it was."""
    try:
        from . import writer as writer_mod
        head, *rest = text.split("\n")
        points = writer_mod.summarize_day(writer_mod.get_writer(), rest)
    except Exception:
        return text
    if not points:
        return text
    return "\n".join([head, "今日の要点（Copilot）:"] + [f"・{p}" for p in points] + rest)


def retry_errors(inbox):
    """Move inbox/done/*.error back into the inbox (decided events are skipped by dedup). Returns the count."""
    inbox = Path(inbox)
    n = 0
    for f in sorted((inbox / "done").glob("*.error")) if (inbox / "done").exists() else []:
        target = inbox / f.name[: -len(".error")]
        if target.exists():
            continue
        f.replace(target)
        n += 1
    return n


def process_inbox(inbox, out, graphs, backend, playbooks, process):
    inbox = Path(inbox)
    done = inbox / "done"
    done.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in events.inbox_files(inbox):
        try:
            n += len(process(events.read_inbox_file(f), graphs, backend, Path(out), playbooks, dedup=True))
            f.replace(done / f.name)
        except BackendUnavailable as e:
            # judge down or overloaded (Kev not up yet, 429/5xx, timeout): keep the file for the next cycle;
            # events already decided from it are skipped then (dedup), so a half-done file is safe to retry
            _log(out, {"step": "judge", "waiting": str(e)})
            break
        except Exception as e:
            _log(out, {"step": "judge", "file": f.name, "error": f"{type(e).__name__}: {e}"})
            f.replace(done / (f.name + ".error"))
    return n


def cycle(out, inbox, graphs, backend, playbooks, process, bridge=None, send=False,
          ado=None, subscription=None, brief_hour=8, now=None, toaster=None):
    """Run one cycle. Returns a small report dict (also written to daily.log.jsonl)."""
    now = now or datetime.now()
    out = Path(out)
    # the PC notification goes with the real Teams bridge only (tests and demos pass their own bridge)
    toaster = toaster or (notify.show_toast if bridge is None else None)
    bridge = bridge or notify.PowerShellBridge()
    r = {}
    _step(out, "pull_teams", lambda: pull.pull_teams(inbox, out, bridge=bridge), r)
    if ado:
        _step(out, "pull_ado", lambda: pull.pull_ado(ado[0], ado[1], inbox, out), r)
    if subscription:
        _step(out, "pull_alerts", lambda: pull.pull_alerts(subscription, inbox, out), r)
    _step(out, "judge", lambda: process_inbox(inbox, out, graphs, backend, playbooks, process), r)
    _step(out, "notify", lambda: notify.notify(out, bridge, send=send), r)
    _step(out, "notices", lambda: len(notify.notify_notices(out, bridge, send=send)), r)
    r["waiting"] = len(events.inbox_files(Path(inbox)))   # files the judge could not take (down / overloaded)
    if send and toaster:
        # self-chat posts never notify the PM's own devices: a Windows notification on this PC does. It is built
        # from what is posted and not yet announced, so a failure on item 2 never loses the announcement of item 1
        nums, keys = notify.untoasted(out)
        if nums or keys:
            def do_toast():
                try:
                    toaster(*notify.toast_text(out, nums, len(keys)))
                except Exception as e:   # a missing notification must not turn the cycle into a failure
                    return f"failed: {type(e).__name__}: {str(e)[:120]}"
                notify.mark_toasted(out, nums, keys)
                return "shown"
            _step(out, "toast", do_toast, r)
    changes = []

    def do_collect():
        changes.extend(notify.collect(out, bridge))
        return [f"#{c['id']}:{c['status']}" for c in changes]
    _step(out, "approvals", do_collect, r)
    if send and toaster and changes:
        try:
            toaster(*notify.ack_text(changes))
        except Exception:
            pass

    st_path = out / "daily_state.json"
    st = fsutil.read_json(st_path, {})
    today = now.strftime("%Y-%m-%d")
    # while the judge is down the brief would say "nothing to do" and be final for the day: wait for it
    if now.hour >= brief_hour and st.get("brief_date") != today and not r["waiting"]:
        def do_brief():
            text, ranked = brief_mod.build(out, backend, date=today)
            if len(ranked) >= 2:   # a one-line day needs no summary, and an empty one must not invent one
                text = _with_key_points(text)
            bridge.post(text, send)
            return len(ranked)
        _step(out, "brief", do_brief, r)
        if not str(r.get("brief", "")).startswith("error") and send:
            st["brief_date"] = today
            fsutil.write_atomic(st_path, json.dumps(st))
    _log(out, {"step": "cycle", "report": r})
    return r
