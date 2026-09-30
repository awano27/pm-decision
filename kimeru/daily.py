"""One cycle of the PM's day, meant to run every few minutes (see `kimeru schedule`).

  pull (teams; ado/alerts when configured) -> judge the inbox -> post the human queue to
  the Teams self chat -> read OK/NG/保留 replies -> morning brief once a day

Each step is isolated: a failing step is logged and the cycle continues, so one
broken source (e.g. Teams closed) does not stop the rest.
"""
import json
from datetime import datetime
from pathlib import Path

from . import actions, brief as brief_mod, config, events, fsutil, fulltext, graph, notify, pull, push
from .backends import BackendUnavailable, is_jev


LOG_MAX_BYTES = 256 * 1024   # daily.log.jsonl is trimmed to its newest half when it grows past this


def _log(out, rec):
    p = Path(out) / "daily.log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **rec}, ensure_ascii=False) + "\n")
    try:
        if p.stat().st_size > LOG_MAX_BYTES:
            data = p.read_bytes()[-LOG_MAX_BYTES // 2:]
            data = data[data.find(b"\n") + 1:]   # drop the cut line
            fsutil.write_atomic(p, data.decode("utf-8", errors="ignore"))
            p.with_name(p.name + ".bak").unlink(missing_ok=True)
    except OSError:
        pass


def _tail_rows(p, nbytes=LOG_MAX_BYTES // 2):
    """The JSON rows in the last `nbytes` of the log (the whole file is never read)."""
    with p.open("rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - nbytes))
        data = f.read()
    lines = data.decode("utf-8", errors="ignore").splitlines()
    if size > nbytes:
        lines = lines[1:]   # the first line may be cut
    rows = []
    for l in lines:
        try:
            rows.append(json.loads(l))
        except ValueError:
            continue
    return rows


def status_lines(out):
    """`schedule status`: the last cycle, what failed in it, how many wait for the PM, and where the settings came from."""
    out = Path(out)
    p = out / "daily.log.jsonl"
    rows = _tail_rows(p) if p.exists() else []
    last = next((r for r in reversed(rows) if r.get("step") == "cycle"), None)
    cfg = next((r for r in reversed(rows) if r.get("step") == "config"), None)
    lines = []
    if last:
        rep = last.get("report", {})
        failed = [k for k, v in rep.items() if isinstance(v, str) and v.startswith("error:")]
        lines.append(f"last cycle: {last.get('at')}  failed steps: {', '.join(failed) or 'none'}  "
                     f"files the judge could not take: {rep.get('waiting', 0)}  "
                     f"files left for the next cycle (time limit): {rep.get('left_for_next_cycle', 0)}")
    else:
        lines.append("last cycle: none recorded yet")
    data = fsutil.read_json(out / "approvals.json", {"items": {}})
    pending = sum(1 for it in data.get("items", {}).values() if it.get("posted") and it.get("status") in ("pending", "held"))
    lines.append(f"waiting for your answer: {pending}")
    lines += push.status_lines(out)
    if cfg:
        eff = cfg.get("effective", {})
        lines.append("settings in effect (source): " + (", ".join(f"{k}={v['source']}" for k, v in eff.items()) or "defaults only"))
        if cfg.get("writer_note"):
            lines.append(cfg["writer_note"])
    else:
        lines.append("settings in effect: not recorded yet (after the first cycle)")
    return lines


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


def _rank(f, order):
    """Where a file goes in the judging order: the most urgent kind of event it holds (unknown or unreadable: last)."""
    try:
        kinds = [e["kind"] for e in events.normalize(events.read_inbox_file(f))]
    except Exception:
        return len(order)
    return min((order.index(k) if k in order else len(order) for k in kinds), default=len(order))


def priority_order():
    return [k.strip() for k in config.value("priority").split(",") if k.strip()]


def process_inbox(inbox, out, graphs, backend, playbooks, process, deadline=None, on_file=None, perf=None, reader=None):
    """Judge the inbox, most urgent kind first (settings: priority). `on_file` runs after each file (used to post what
    is ready before the rest is judged); `deadline` (time.monotonic) stops new files: they stay for the next cycle."""
    import time
    inbox = Path(inbox)
    done = inbox / "done"
    done.mkdir(parents=True, exist_ok=True)
    order = priority_order()
    files = sorted(events.inbox_files(inbox), key=lambda f: (_rank(f, order), f.name))
    n = 0
    for i, f in enumerate(files):
        if deadline is not None and time.monotonic() >= deadline:
            _log(out, {"step": "judge", "over_budget": len(files) - i})   # left in the inbox; the next cycle takes them
            if perf is not None:
                perf["left_for_next_cycle"] = len(files) - i
            break
        try:
            extra = {"reader": reader} if reader is not None else {}
            results = process(events.read_inbox_file(f), graphs, backend, Path(out), playbooks, dedup=True, **extra)
            n += len(results)
            if perf is not None:
                for r in results:
                    p = r.get("perf") or {}
                    perf["events"] = perf.get("events", 0) + 1
                    for k in ("calls", "questions"):
                        perf[k] = perf.get(k, 0) + p.get(k, 0)
                    for k in ("judge_sec", "writer_sec"):
                        perf[k] = round(perf.get(k, 0.0) + p.get(k, 0.0), 2)
            f.replace(done / f.name)
            if on_file:
                try:
                    on_file()
                except Exception as e:   # a post that fails here is tried again by the cycle's own notify step
                    _log(out, {"step": "notify_early", "error": f"{type(e).__name__}: {str(e)[:120]}"})
        except fulltext.BudgetExhausted as e:
            # this cycle opened as many chats as it may: the file stays in the inbox for the next cycle (events already
            # decided from it are skipped then)
            _log(out, {"step": "judge", "read_budget": str(e)})
            if perf is not None:
                perf["left_for_next_cycle"] = perf.get("left_for_next_cycle", 0) + 1
            continue
        except BackendUnavailable as e:
            # judge down or overloaded (Kev not up yet, 429/5xx, timeout): keep the file for the next cycle;
            # events already decided from it are skipped then (dedup), so a half-done file is safe to retry
            _log(out, {"step": "judge", "waiting": str(e)})
            break
        except Exception as e:
            _log(out, {"step": "judge", "file": f.name, "error": f"{type(e).__name__}: {e}"})
            f.replace(done / (f.name + ".error"))
    return n


def _alert_left_open(out, alerts, toaster, bridge=None, send=False):
    """A chat that kimeru opened could not be put back: tell the person. This is never silent: with KIMERU_TOAST=0, or when the
    PC notification is not available or fails, the notice goes to the self chat instead (only in a cycle that posts)."""
    _log(out, {"step": "read_restore", "alerts": list(alerts)})
    title = "kimeru: チャットの表示"
    body = ("元のチャットへ戻せず、開いたままの可能性があります。Teams を確認してください" if "failed" in alerts
            else "元のチャットへ戻せなかったため、自分とのチャットへ移しました")
    if toaster and notify.toast_enabled():
        try:
            toaster(title, body)
            return
        except Exception as e:   # the log line above stays; the self chat is tried below
            _log(out, {"step": "read_restore", "toast": f"failed: {type(e).__name__}"})
    if not (send and bridge is not None):
        _log(out, {"step": "read_restore", "notice": "not shown (no PC notification, and this cycle does not post)"})
        return
    try:
        bridge.post(f"[kimeru 通知] {body}", True)
    except Exception as e:
        _log(out, {"step": "read_restore", "post": f"failed: {type(e).__name__}"})


def cycle(out, inbox, graphs, backend, playbooks, process, bridge=None, send=False,
          ado=None, subscription=None, brief_hour=8, now=None, toaster=None, budget=None, real=False):
    """Run one cycle. Returns a small report dict (also written to daily.log.jsonl).
    real=True (the `daily` command only): approvals carry out the kinds switched on with `config set execute`. Anything else
    that calls this (the eval scripts, tests) only records."""
    now = now or datetime.now()
    out = Path(out)
    # the PC notification goes with the real Teams bridge only (tests and demos pass their own bridge)
    toaster = toaster or (notify.show_toast if bridge is None else None)
    bridge = bridge or notify.PowerShellBridge()
    r = {}
    _log(out, {"step": "config", **config.report()})   # which settings this cycle runs with, and where they came from
    try:   # full texts kept past their time are deleted (the items are then treated as decided from the preview)
        purged = fulltext.purge(out)
    except Exception as e:
        purged = []
        _log(out, {"step": "full_text", "error": f"{type(e).__name__}"})
    if getattr(purged, "busy", False):
        r["full_text_purge"] = "busy: approvals.json is in use by another run, nothing was purged this cycle"
    elif purged:
        r["full_text_purged"] = len(purged)
    if send:   # a route turned on is baselined before this cycle posts anything; a route turned off forgets what came meanwhile
        try:
            push.begin_cycle(out)
        except Exception as e:   # the phone routes never fail a cycle
            _log(out, {"step": "push_begin", "error": type(e).__name__})
    _step(out, "pull_teams", lambda: pull.pull_teams(inbox, out, bridge=bridge), r)
    if ado:
        _step(out, "pull_ado", lambda: pull.pull_ado(ado[0], ado[1], inbox, out), r)
    if subscription:
        _step(out, "pull_alerts", lambda: pull.pull_alerts(subscription, inbox, out), r)
    import time
    try:
        budget = float(config.value("cycle_budget_sec")) if config.value("cycle_budget_sec") else budget
    except ValueError:
        pass
    deadline = time.monotonic() + budget if budget and budget > 0 else None
    perf = {}

    early, early_notices = [], []

    def post_ready():   # what is ready is posted now, not after the rest of the inbox has been judged
        if send:
            early.extend(notify.notify(out, bridge, send=True, real=real))   # (busy: nothing posted now; the next call retries)
            early_notices.extend(notify.notify_notices(out, bridge, send=True))
    reader = fulltext.Reader(bridge) if fulltext.enabled() else None   # opens chats in full only when it is needed (off by default)
    _step(out, "judge", lambda: process_inbox(inbox, out, graphs, backend, playbooks, process,
                                              deadline=deadline, on_file=post_ready, perf=perf, reader=reader), r)
    if reader is not None and reader.opened:
        perf["chats_opened"] = reader.opened
    if reader is not None and reader.alerts:
        _alert_left_open(out, reader.alerts, toaster, bridge=bridge, send=send)
    if perf:
        if is_jev(backend):   # Jev's speed is not written anywhere (TypeSafe's terms); the counts stay
            perf = {k: v for k, v in perf.items() if k != "judge_sec"}
        r["perf"] = perf
    def do_notify():
        got = notify.notify(out, bridge, send=send, real=real)
        if getattr(got, "busy", False):
            return {"posted": early, "busy": "another approvals run holds the lock, nothing was posted this call"}
        return early + got

    def do_notices():
        got = notify.notify_notices(out, bridge, send=send)
        if getattr(got, "busy", False):
            return {"posted": len(early_notices), "busy": "another approvals run holds the lock, nothing was posted this call"}
        return len(early_notices) + len(got)
    _step(out, "notify", do_notify, r)
    _step(out, "notices", do_notices, r)
    # files the judge could not take (down / overloaded). Files kept back for the time limit or the reading limit are
    # not that: the judge is fine, the next cycle takes them (reported apart, and they do not fail the run or hold the brief)
    left = perf.get("left_for_next_cycle", 0)
    r["waiting"] = max(0, len(events.inbox_files(Path(inbox))) - left)
    if left:
        r["left_for_next_cycle"] = left
    if send and toaster and notify.toast_enabled():
        # self-chat posts never notify the PM's own devices: a Windows notification on this PC does. It is built
        # from what is posted and not yet announced, so a failure on item 2 never loses the announcement of item 1.
        # With KIMERU_TOAST=0 nothing is marked as announced: turning it back on announces what waited.
        nums, keys = notify.untoasted(out)
        unposted_now = notify.unposted_count(out)
        # the unposted count is announced when it changes, not every cycle while Teams stays unreachable
        announced = notify.unposted_announced(out)
        unposted = unposted_now if unposted_now != announced else 0
        if announced and not unposted_now:
            notify.mark_toasted(out, [], [], unposted=0)   # (busy: the next cycle does it)
        if nums or keys or unposted:
            def do_toast():
                try:
                    toaster(*notify.toast_text(out, nums, len(keys), unposted))
                except Exception as e:   # a missing notification must not turn the cycle into a failure
                    return f"failed: {type(e).__name__}: {str(e)[:120]}"
                if not notify.mark_toasted(out, nums, keys, unposted=unposted_now):
                    return "shown (not recorded: approvals.json is in use; announced again next cycle)"
                return "shown"
            _step(out, "toast", do_toast, r)
    if send:   # the routes that reach a phone (counts and numbers only); each has its own state
        def do_push():
            res = push.run(out)
            return {k: v for k, v in res.items() if v != "nothing new"} or "nothing new"
        if push.enabled_routes():
            _step(out, "push", do_push, r)
    changes = []

    def do_collect():
        got = notify.collect(out, bridge, real=real, send=send)
        if getattr(got, "busy", False):
            return "busy: another approvals run holds the lock, nothing was applied this cycle"
        changes.extend(got)
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
