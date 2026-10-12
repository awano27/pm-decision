"""One cycle of the PM's day, meant to run every few minutes (see `kimeru schedule`).

  pull (teams; ado/alerts when configured) -> judge the inbox -> post the human queue to
  the Teams self chat -> read OK/NG/保留 replies -> morning brief once a day

Each step is isolated: a failing step is logged and the cycle continues, so one
broken source (e.g. Teams closed) does not stop the rest.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import actions, attention, brief as brief_mod, config, control, events, fsutil, fulltext, graph, hold, learn, notify, pull, push, sent
from .backends import BackendUnavailable, is_jev


LOG_MAX_BYTES = 256 * 1024   # daily.log.jsonl is trimmed to its newest half when it grows past this


def _log(out, rec):
    p = Path(out) / "daily.log.jsonl"
    try:   # a log that cannot be written (a folder that cannot be written to) must not stop the cycle
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **rec}, ensure_ascii=False) + "\n")
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
    if last and (last.get("report") or {}).get("busy"):
        lines.append("steps skipped in the last cycle because another run held approvals.lock (they are tried again next cycle): "
                     + ", ".join(sorted(last["report"]["busy"])))
    if last and (last.get("report") or {}).get("full_text_delete_failed"):
        lines.append("full texts that could not be deleted in the last cycle: " + str(last["report"]["full_text_delete_failed"])
                     + " (still on the disk; deleted in a later cycle. See warnings.jsonl and docs/reference.md)")
    data = fsutil.read_json(out / "approvals.json", {"items": {}})
    pending = sum(1 for it in data.get("items", {}).values() if it.get("posted") and it.get("status") in ("pending", "held"))
    lines.append(f"waiting for your answer: {pending}")
    lines += push.status_lines(out)
    lines += attention.status_lines(out)   # R17: the next sitting and what waits for it (nothing unless interrupt=sittings)
    if cfg:
        eff = cfg.get("effective", {})
        lines.append("settings in effect (source): " + (", ".join(f"{k}={v['source']}" for k, v in eff.items()) or "defaults only"))
        if cfg.get("writer_note"):
            lines.append(cfg["writer_note"])
    else:
        lines.append("settings in effect: not recorded yet (after the first cycle)")
    return lines


def _busy(report, step, why):
    """A step that was skipped because another run holds the lock. The step's own entry in the report keeps its usual shape
    (notify: a list of numbers, notices: a count, approvals: a list), so a reader of the report needs no special case; the skipped
    steps are named here: report["busy"] = {step: reason}."""
    report.setdefault("busy", {})[step] = why


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
        w = writer_mod.get_writer()
        points = writer_mod.summarize_day(w, rest)
        label = writer_mod.writer_label({"writer_name": getattr(w, "NAME", "")})
    except Exception:
        return text
    if not points:
        return text
    return "\n".join([head, f"今日の要点（{label}）:"] + [f"・{p}" for p in points] + rest)


def _with_weekly(out, text, now):
    """R24 (weekly_report=1, on a Monday): the six lines of the week's report go to the end of the brief, after the writer's key points
    and before the delivery, so the writer never gets them. A failure leaves the six lines out and the brief as it was."""
    try:
        from . import stats
        lines = stats.weekly_lines(out, now)
    except Exception as e:
        _log(out, {"step": "weekly_report", "error": f"{type(e).__name__}: {str(e)[:120]}"})
        return text
    return "\n".join([text, *lines]) if lines else text


def _record_brief(out, report, date, backend, ranked):
    """The day's row of out/brief_history.jsonl, after a delivered brief. Never changes the brief's result: a failure is logged."""
    try:
        if brief_mod.record_history(out, date, backend, ranked) == "busy":
            _busy(report, "brief_history", "another run holds brief_history.lock, this day's row was not written")
    except Exception as e:
        _log(out, {"step": "brief_history", "error": f"{type(e).__name__}: {str(e)[:120]}"})


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


def _sittings(out, now):
    """R17: the sittings of this cycle (attention.plan). A failure reads as no sitting: the items are announced at once, as before."""
    try:
        return attention.plan(out, now)
    except Exception as e:
        _log(out, {"step": "attention", "error": f"{type(e).__name__}: {str(e)[:120]}"})
        return {"sitting_at": None, "hold": set()}


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


def inbox_done_keep_days():
    """Days a judged original stays in inbox/done (0 = kept). A value that is not a number keeps the default."""
    try:
        return max(0, int(config.value("inbox_done_keep_days")))
    except ValueError:
        return 7


def purge_inbox_done(inbox, now=None, days=None):
    """Delete the judged originals in inbox/done older than `inbox_done_keep_days` (they hold the full text; the decision
    records keep an excerpt). Parked files (*.error) are kept for `retry`; whether an event was judged is in processed.txt,
    so a deleted original is never judged again. Returns the number deleted; a file that cannot be deleted stays."""
    days = inbox_done_keep_days() if days is None else days
    done = Path(inbox) / "done"
    if days <= 0 or not done.is_dir():
        return 0
    limit = (now or datetime.now()).timestamp() - days * 86400
    n = 0
    for f in done.glob("*.json"):
        try:
            if f.stat().st_mtime < limit:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n


def cycle(out, inbox, graphs, backend, playbooks, process, bridge=None, send=False,
          ado=None, subscription=None, brief_hour=8, now=None, toaster=None, budget=None, real=False):
    """Run one cycle. Returns a small report dict (also written to daily.log.jsonl).
    real=True (the `daily` command only): approvals carry out the kinds switched on with `config set execute`, and only in a
    cycle with send=True (an OK on a cycle without --send records the approval; the write waits for the next one). Anything else
    that calls this (the eval scripts, tests) only records."""
    now = now or datetime.now()
    out = Path(out)
    # the PC notification goes with the real Teams bridge only (tests and demos pass their own bridge)
    toaster = toaster or (notify.show_toast if bridge is None else None)
    bridge = bridge or notify.PowerShellBridge()
    r = {}
    fulltext.new_cycle()
    _log(out, {"step": "config", **config.report()})   # which settings this cycle runs with, and where they came from
    try:   # full texts kept past their time are deleted (the items are then treated as decided from the preview)
        purged = fulltext.purge(out)
    except Exception as e:
        purged = []
        _log(out, {"step": "full_text", "error": f"{type(e).__name__}"})
    if getattr(purged, "busy", False):
        _busy(r, "full_text_purge", "approvals.json is in use by another run, nothing was purged this cycle")
    elif purged:
        r["full_text_purged"] = len(purged)
    try:   # judged originals past their time (the full text of ADO items and minutes) are deleted
        gone = purge_inbox_done(inbox, now=now)
        if gone:
            r["inbox_done_purged"] = gone
    except Exception as e:
        _log(out, {"step": "inbox_done", "error": type(e).__name__})
    try:   # morning brief rows older than the comparison needs (out/brief_history.jsonl, 90 days); nothing is reported when none go
        brief_mod.purge_history(out, now=now)
    except Exception as e:
        _log(out, {"step": "brief_history", "error": f"{type(e).__name__}: {str(e)[:120]}"})
    # R20: my own line 停止 / 再開 (control.py). It is read before anything takes from Teams, and the list read here is the one the
    # pull uses. Stopped, this cycle takes nothing more (the purges above did run); a Teams that cannot be read is no error here
    chats, mode = None, "run"
    if control.enabled():
        try:
            chats = bridge.chats()
        except Exception as e:
            _log(out, {"step": "control", "error": f"{type(e).__name__}: {str(e)[:120]}"})
        try:
            mode = control.step(out, bridge, chats, now, send=send, toaster=toaster if toaster and notify.toast_enabled() else None)
        except Exception as e:
            _log(out, {"step": "control", "error": f"{type(e).__name__}: {str(e)[:120]}"})
            mode = control.current(out)
    if mode != "run":   # "stop", or "busy" (the state is in use by another run): nothing is decided, posted or run in this cycle
        r["mode"], r["waiting"] = mode, 0
        _log(out, {"step": "cycle", "report": r})
        return r
    if send:   # a route turned on is baselined before this cycle posts anything; a route turned off forgets what came meanwhile
        try:
            push.begin_cycle(out)
        except Exception as e:   # the phone routes never fail a cycle
            _log(out, {"step": "push_begin", "error": type(e).__name__})
    own_seen = [] if (real and sent.enabled()) else None   # R14: my own new messages the list shows in this cycle (memory only)
    _step(out, "pull_teams", lambda: pull.pull_teams(inbox, out, bridge=bridge, own_out=own_seen, chats=chats), r)
    if own_seen:   # R14: a send after an OK is seen on the list; not in the report (a failure is only logged, the cycle goes on)
        try:
            if getattr(sent.observe(out, own_seen, now=now), "busy", False):
                _log(out, {"step": "sent_watch", "busy": "approvals.lock is held by another run; not written this cycle"})
        except Exception as e:
            _log(out, {"step": "sent_watch", "error": f"{type(e).__name__}: {str(e)[:120]}"})
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
    if real and learn.enabled():   # R21 (learn=1 only, once a week, the daily path only): a card for a question the labels show to be answered wrongly
        _step(out, "learn", lambda: learn.offer(out, graphs, backend, now=now), r)   # (queued here; posted by the notify step below)

    def do_notify():
        got = notify.notify(out, bridge, send=send, real=real)
        if getattr(got, "busy", False):
            _busy(r, "notify", "another run holds approvals.lock, nothing was posted this call")
            return list(early)
        return early + got

    def do_notices():
        got = notify.notify_notices(out, bridge, send=send)
        if getattr(got, "busy", False):
            _busy(r, "notices", "another run holds approvals.lock, nothing was posted this call")
            return len(early_notices)
        return len(early_notices) + len(got)
    _step(out, "notify", do_notify, r)
    _step(out, "notices", do_notices, r)
    # files the judge could not take (down / overloaded). Files kept back for the time limit or the reading limit are
    # not that: the judge is fine, the next cycle takes them (reported apart, and they do not fail the run or hold the brief)
    left = perf.get("left_for_next_cycle", 0)
    _step(out, "waiting", lambda: max(0, len(events.inbox_files(Path(inbox))) - left), r)   # (an inbox that cannot be listed: "error: ...", and the cycle goes on)
    if left:
        r["left_for_next_cycle"] = left
    sit = _sittings(out, now) if send else {"sitting_at": None, "hold": set()}   # R17: read after this cycle's posts, so they are in it
    at = sit["sitting_at"]   # the last sitting at or before now; None unless interrupt=sittings
    if send and toaster and notify.toast_enabled():
        # self-chat posts never notify the PM's own devices: a Windows notification on this PC does. It is built
        # from what is posted and not yet announced, so a failure on item 2 never loses the announcement of item 1.
        # With KIMERU_TOAST=0 nothing is marked as announced: turning it back on announces what waited.
        try:
            nums, keys = notify.untoasted(out)
            unposted_now = notify.unposted_count(out)
            # the unposted count is announced when it changes, not every cycle while Teams stays unreachable
            announced = notify.unposted_announced(out)
        except Exception as e:   # approvals.json cannot be read now: nothing is announced this cycle
            nums, keys, unposted_now, announced = [], [], 0, 0
            _log(out, {"step": "toast", "error": f"{type(e).__name__}: {str(e)[:120]}"})
        unposted = unposted_now if unposted_now != announced else 0
        if announced and not unposted_now:
            _step(out, "toast_clear", lambda: notify.mark_toasted(out, [], [], unposted=0), r)   # (busy or a folder that cannot be written: the next cycle does it)
        # R17 (interrupt=sittings): a posted item that is not urgent waits for the next sitting, which is announced once. The sitting
        # is recorded only when it was announced (or there was nothing to announce), so a failed toast is tried again next cycle.
        due, held = False, set()
        if at:
            try:
                due = notify.sitting_toast_due(out, at)
            except Exception as e:   # approvals.json cannot be written now: the waiting items are announced, and recorded when it can be
                due = True
                _log(out, {"step": "toast", "sitting": f"{type(e).__name__}: {str(e)[:120]}"})
            held = set() if due else sit["hold"]
            nums = [n for n in nums if n not in held]
        sitting = bool(at and due)
        if nums or keys or unposted:
            def do_toast():
                try:
                    toaster(*notify.toast_text(out, nums, len(keys), unposted, sitting=sitting))
                except Exception as e:   # a missing notification must not turn the cycle into a failure
                    return f"failed: {type(e).__name__}: {str(e)[:120]}"
                if not notify.mark_toasted(out, nums, keys, unposted=unposted_now, sitting=at if sitting else None):
                    return "shown (not recorded: approvals.json is in use; announced again next cycle)"
                return "shown"
            _step(out, "toast", do_toast, r)
        elif sitting:   # the sitting passes with nothing to announce: it is used up (busy: the next cycle records it)
            try:
                notify.mark_toasted(out, [], [], sitting=at)
            except Exception as e:
                _log(out, {"step": "toast", "sitting": f"{type(e).__name__}: {str(e)[:120]}"})
    if send:   # the routes that reach a phone (counts and numbers only); each has its own state
        def do_push():
            due, hold = {}, None
            if at:   # R17: a route whose sitting is due announces what waits; the others keep it for their own next sitting
                due = push.sitting_state(out, at)
                hold = {route: (set() if due.get(route) else sit["hold"]) for route in push.enabled_routes()}
            res = push.run(out, hold=hold)
            if at:   # recorded only for the routes that announced it (sent, or nothing was left to send)
                push.sitting_done(out, at, [route for route, d in due.items() if d and res.get(route) in ("sent", "nothing new")])
            return {k: v for k, v in res.items() if v != "nothing new"} or "nothing new"
        if push.enabled_routes():
            _step(out, "push", do_push, r)
    changes = []

    def do_collect():
        got = notify.collect(out, bridge, real=real and send, send=send, now=now, brief_hour=brief_hour)   # R13: an OK on a run without --send writes nothing
        if getattr(got, "busy", False):
            _busy(r, "approvals", "another run holds approvals.lock, nothing was applied this cycle")
            return []
        changes.extend(got)
        return [f"#{c['id']}:{c['status']}" for c in changes]
    _step(out, "approvals", do_collect, r)
    if send:   # R09: a held item whose day has come is posted again under the same number, in this cycle; nothing is decided by it
        try:
            back = hold.resurface(out, now)
        except Exception as e:   # the cycle goes on; the held item stays as it was
            back = []
            _log(out, {"step": "hold", "error": f"{type(e).__name__}: {str(e)[:120]}"})
        if getattr(back, "busy", False):   # the same lock as notify and approvals, which already name the busy lock; the next cycle retries
            back = []
        if back:
            r["hold_resurfaced"] = list(back)
            _step(out, "notify_hold", lambda: notify.notify(out, bridge, send=True, real=real), r)
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
        if st.get("brief_delivery_unknown"):
            r["brief"] = "error: brief delivery_unknown; use delivery show/confirm/retry"
            _log(out, {"step": "brief", "status": "delivery_unknown_hold"})
        else:
            def do_brief():
                built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                text, ranked = brief_mod.build(out, backend, date=today, since=st.get("brief_at"))   # unknown: the last 24 hours
                shown = brief_mod.named(ranked)   # the post names the first three; the history keeps every item
                if len(shown) >= 2 and not brief_mod.is_desk():   # a one-line day needs no summary, and an empty one must not invent one; desk: no writer
                    text = _with_key_points(text)
                if config.value("weekly_report") == "1" and now.weekday() == 0:   # R24: after the writer, before the delivery
                    text = _with_weekly(out, text, now)
                result = notify.deliver_brief(out, bridge, text, send, today, built_at=built_at)
                status = result["status"]
                if status == "delivered":   # only a brief whose delivery was read back is recorded (not pasted, not unknown)
                    _record_brief(out, r, today, backend, ranked)
                if status in ("delivered", "pasted", "already_delivered"):
                    return len(shown)
                if status == "delivery_unknown":
                    return "error: brief delivery_unknown; use delivery show/confirm/retry"
                return f"error: brief delivery {status}"
            _step(out, "brief", do_brief, r)
    stuck = fulltext.failures()
    if stuck:   # a full text that could not be deleted is still on the disk: it is shown, not only written to warnings.jsonl
        r["full_text_delete_failed"] = stuck
    _log(out, {"step": "cycle", "report": r})
    return r
