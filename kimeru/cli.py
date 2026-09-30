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
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import actions, config, events, execute, fsutil, graph
from . import writer as writer_mod
from . import plan as planner
from .backends import BackendUnavailable, ClmBackend, JevBackend, KevBackend, StubBackend, judge_name

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


EVENT_KEEP = ("kind", "id", "author", "text", "item", "meeting", "title", "description", "work_item_type",
              "rule", "severity", "condition", "mentions_me", "context", "date", "chat_id", "chat_title", "origin")


EVENT_TEXT = ("text", "description", "item", "title", "meeting", "condition", "context")   # free text: cut in the records
EVENT_TEXT_SUMMARY = 120     # what the records kept before the event was recorded at all: the length of `summary`
EVENT_TEXT_FULL = 2000       # with the setting record_event_full=1


def _recorded_event(ev):
    """The event as decisions.jsonl keeps it. By default its free text is cut to the length of a summary (SECURITY.md);
    record_event_full=1 keeps up to 2,000 characters, which makes `kimeru review` examples better and the record more sensitive."""
    limit = EVENT_TEXT_FULL if config.value("record_event_full") == "1" else EVENT_TEXT_SUMMARY
    out = {}
    for k, v in ev.items():
        if k not in EVENT_KEEP or v in (None, ""):
            continue
        if isinstance(v, str) and k in EVENT_TEXT and len(v) > limit:
            v = v[:limit - 1] + "…" if limit == EVENT_TEXT_SUMMARY else v[:limit]
        out[k] = v
    return out


def _record_limit():
    return EVENT_TEXT_FULL if config.value("record_event_full") == "1" else EVENT_TEXT_SUMMARY


def _cut(v, limit):
    return v[:limit - 1] + "…" if limit == EVENT_TEXT_SUMMARY else v[:limit]


def _seal_material(res, held, out, key, ev_run, merged):
    """A writer works from the whole text, but the records that stay (decisions.jsonl, queue.jsonl) keep a summary-length
    excerpt like everything else (SECURITY.md). The longer text of the free-text fields, and the request built from it, wait in
    full_text.json while the item waits for the PM. Returns (res, held) as the records may keep them."""
    from . import fulltext
    limit = _record_limit()
    mat = res.get("material_event") or {}
    long = {k: v for k, v in mat.items() if k in EVENT_TEXT and isinstance(v, str) and len(v) > limit}
    if not long:
        return res, held
    keep = {**mat, **{k: _cut(v, limit) for k, v in long.items()}}
    request = res.get("copilot_request")
    for k, v in long.items():
        res, held = fulltext.scrub(res, v, keep[k]), fulltext.scrub(held, v, keep[k])
    res["material_event"] = keep
    if request:
        res["copilot_request"] = writer_mod.human_request(res, keep)
    if res.get("needs_human") and not merged:
        fulltext.save(out, key, {"text": long.get("text", ""), "thread": ev_run.get("thread", []),
                                 "author": mat.get("author", "")}, request=request,
                      material={k: v for k, v in long.items() if k != "text"})
    return res, held


class Meter:
    """Counts what one event costs the judge: calls, questions and seconds. Everything else is passed through."""

    def __init__(self, inner):
        self.inner, self.calls, self.questions, self.seconds = inner, 0, 0, 0.0
        self.profile = getattr(inner, "profile", None)

    def ask(self, state, questions):
        t = time.perf_counter()
        try:
            return self.inner.ask(state, questions)
        finally:
            self.seconds += time.perf_counter() - t
            self.calls += 1
            self.questions += len(questions)

    def __getattr__(self, name):   # NAME, model, ... of the wrapped backend
        return getattr(self.__dict__["inner"], name)


def _judge_info(backend):
    """Which judge answered, seen through the wrappers that time or record it (Timed, Recording)."""
    seen = 0
    while hasattr(backend, "inner") and seen < 5:
        backend, seen = backend.inner, seen + 1
    return {"name": judge_name(backend), "model": str(getattr(backend, "model", ""))[:60]}


def _graph_version(g):
    return hashlib.sha1(json.dumps(g, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:8]


def _processed(out):
    p = out / "processed.txt"
    return set(p.read_text(encoding="utf-8").split()) if p.exists() else set()


def _no_action(res):
    """Decided as needing nothing from anyone: no notice, and every action is only a log line."""
    return (res.get("outcome") == "decide" and not res.get("notify") and not res.get("needs_human")
            and all(a.get("type") == "log.only" for a in res.get("actions") or []))


def _merge_into_pending(out, ev, res):
    """A new message in a chat whose earlier message still waits for the PM joins that item (posted again under the same
    number) instead of becoming a second one, but only when all of these hold; otherwise it is a matter of its own:
    the same sender as the waiting item, a result that raises no notice, and a result that goes to the same place as the
    waiting item or needs nothing. `res` is the judgment of the new message. Returns True when it was merged."""
    if ev.get("kind") != "teams.chat" or not ev.get("chat_id") or not ev.get("author") or res.get("notify"):
        return False
    from . import notify
    with fsutil.exclusive(notify.lock_path(out)) as got:
        if not got:   # another run holds approvals.json: nothing is changed; the message stays a matter of its own (noted in warnings.jsonl)
            _append(out / "warnings.jsonl", {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                             "merge": "skipped: approvals.json is in use by another run", "event_id": ev.get("id")})
            return False
        return _merge_locked(out, ev, res, notify)


def _merge_locked(out, ev, res, notify):
    ap = notify.Approvals(out)
    for it in ap.data["items"].values():
        rec = it["record"]
        old = rec.get("event") or {}
        if it["status"] not in ("pending", "held") or old.get("chat_id") != ev["chat_id"]:
            continue
        if old.get("author") != ev.get("author"):
            continue
        if not (_no_action(res) or res.get("node") == rec.get("node")):
            continue
        rec.setdefault("followups", []).append({"text": " ".join(str(ev.get("text", "")).split())[:300],
                                               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        it["posted"], it["request_posted"] = False, False      # posted again, with the follow-up, under the same number
        ap.save()
        _append(out / "merged.jsonl", {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "into": it["key"], "event_id": ev.get("id")})
        return True
    return False


def _judge(g, ev, meter, playbooks):
    """The graph only: no writer is called."""
    return graph.run(g, ev, meter, playbooks=playbooks)


def _draft(res, ev, meter, writer):
    """The writer, once, for the judgment that stands. Returns (res, held)."""
    held = []
    t_writer = time.perf_counter()
    drafted_any = writer_mod.apply(res, ev, writer)
    res["perf"] = {"calls": meter.calls, "questions": meter.questions, "judge_sec": round(meter.seconds, 2),
                   "writer_sec": round(time.perf_counter() - t_writer, 2)}
    if drafted_any:
        res["material_event"] = {k: ev.get(k) for k in writer_mod.EVENT_FIELDS if ev.get(k)}
        # LLM text goes out only after the PM approves its exact wording
        held = [a for a in res["actions"] if a.get("drafted_by") or a.get("held_for")]
    # a kind the PM switched on for real execution never runs by itself, decided by the graph or not
    held += [a for a in res["actions"] if execute.is_gated(a) and a not in held]
    if held and res["outcome"] == "decide":
        res["needs_human"] = True
    return res, held


def _ends_at_pm(res, writer):
    """Would this judgment end at the PM (a confirmation, a notice, or a draft that waits for approval)? Known before the
    writer is called, so that the chat can be read first and the draft written once."""
    if res.get("needs_human") or res.get("notify"):
        return True
    if res.get("outcome") != "decide":
        return False
    return bool(writer is not None and writer_mod.targets(res)) or any(execute.is_gated(a) for a in res["actions"])


class _PreviewJudgments:
    """Events whose chat could not be read yet (the cycle's opening budget was used up, the person was at the PC, Teams was in use):
    the next cycle continues from the reading. Holds the preview's judgment (what the records keep anyway; None for an event that
    was not judged yet) and how many times the event was put off. KEEP runs from the first time it was put off (a later delay does
    not extend it); after `read_max_defer` delays the preview decides. Dropped when the event is finished."""

    KEEP = 24 * 3600

    def __init__(self, out):
        self.path = Path(out) / "read_pending.json"
        self.data = fsutil.read_json(self.path, {})

    def _live(self, e):
        return isinstance(e, dict) and time.time() - e.get("t", 0) < self.KEEP

    def get(self, key):
        e = self.data.get(key)
        return e.get("res") if self._live(e) else None

    def deferrals(self, key):
        e = self.data.get(key)
        return int(e.get("n", 1)) if self._live(e) else 0

    def put(self, key, res=None):
        self.data = {k: e for k, e in self.data.items() if self._live(e)}
        old = self.data.get(key) or {}
        self.data[key] = {"t": old.get("t", time.time()), "n": int(old.get("n", 0)) + 1, "res": res if res is not None else old.get("res")}
        fsutil.write_atomic(self.path, json.dumps(self.data, ensure_ascii=False))

    def drop(self, key):
        if key in self.data:
            del self.data[key]
            fsutil.write_atomic(self.path, json.dumps(self.data, ensure_ascii=False))


def process(payload, graphs, backend, out, playbooks=None, writer=None, dedup=False, reader=None):
    """Judge every event in `payload`. dedup=True (inbox/daily) skips an event already decided
    by the same graph version, so a retried or re-dropped file does not decide twice.
    `reader` (fulltext.Reader) opens a chat to read it in full, only when needed (see fulltext.py)."""
    from . import fulltext
    if playbooks is None:
        playbooks = planner.load_playbooks(DEFAULT_PLAYBOOKS)
    if writer is None:
        writer = writer_mod.get_writer()   # a misspelled KIMERU_WRITER reads as unset: decide without a writer, say so, never park the file
        for key, msg in config.not_allowed():
            if key != "writer":
                continue
            _append(out / "warnings.jsonl", {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), key: msg})
    seen = _processed(out) if dedup else set()
    results = []
    for ev in events.normalize(payload):
        for g in graphs.get(ev["kind"], []):
            key = f"{g['name']}@{_graph_version(g)}:{ev['kind']}:{ev.get('id')}".replace(" ", "_")
            if key in seen:
                continue
            can_read = reader is not None and ev["kind"] == "teams.chat" and bool(ev.get("chat_id"))
            read_info, ev_run = None, ev
            meter = Meter(backend)
            pend = _PreviewJudgments(out) if can_read else None
            # put off too often (the person was at the PC, Teams in use, the cycle's limit): the preview decides
            max_defer = fulltext.max_defer()
            # read_max_defer=0: never put off. The chat is tried once; when it cannot be read now, the preview decides (no waiting)
            tired = bool(pend) and max_defer > 0 and pend.deferrals(key) >= max_defer
            if can_read and fulltext.truncated(ev.get("text")):        # the preview is cut off: read before judging
                if tired:
                    read_info = fulltext.gave_up(fulltext.max_defer())
                else:
                    try:
                        ev_run, read_info = fulltext.deepen(reader, ev)   # (over the limit: nothing has been judged yet)
                    except fulltext.BudgetExhausted:
                        if max_defer > 0:
                            pend.put(key)                              # counted: the next cycle tries again, up to read_max_defer times
                            raise
                        read_info = fulltext.gave_up(0)
                res = _judge(g, ev_run, meter, playbooks)
            else:
                res = pend.get(key) if pend else None                 # a preview judged in an earlier cycle: continue from the reading
                if res is None:
                    res = _judge(g, ev, meter, playbooks)
                if can_read and _ends_at_pm(res, writer):              # a decision that ends at the PM: look at the whole chat
                    if tired:
                        read_info = fulltext.gave_up(fulltext.max_defer())
                    else:
                        try:
                            ev_run, read_info = fulltext.deepen(reader, ev)
                        except fulltext.BudgetExhausted:
                            if max_defer > 0:
                                pend.put(key, res)                     # the next cycle reads; it does not judge again
                                raise
                            read_info = fulltext.gave_up(0)
                    if read_info["state"] == "full":
                        res = _judge(g, ev_run, meter, playbooks)      # the whole text may change the judgment
            if pend:
                pend.drop(key)
            res, held = _draft(res, ev_run, meter, writer)            # the draft is written once, for the judgment that stands
            if read_info:
                res["read_full"] = read_info
            # decide runs now; advise actions are only proposed until approved (see notify.collect)
            now = [a for a in res["actions"] if a not in held] if res["outcome"] == "decide" else []
            res["executed"] = [actions.execute(a, dry_run=True) for a in now]
            res["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            res["summary"] = events.summary(ev_run)
            kept = fulltext.persistable(ev_run)   # a full text is kept only while the item waits (full_text.json)
            res["event"] = _recorded_event(kept)   # local only: lets `kimeru review` build a labeled example
            if ev_run.get("full") and res.get("material_event"):
                res["material_event"] = {**res["material_event"], "text": kept["text"]}
                res["material_event"].pop("直前のやり取り", None)
            res["judge"] = _judge_info(backend)
            res["graph_key"] = key
            # a chat's new message is always judged; it joins a waiting item only under the rules of _merge_into_pending
            merged = dedup and _merge_into_pending(out, ev, res)
            if ev_run.get("full"):
                # the records that stay hold an excerpt; the whole text waits in full_text.json until the PM decides.
                # No .jsonl file gets it: the paste-in request for Copilot is rebuilt from the excerpt for the record
                request = fulltext.redact_request(res, ev_run)
                res_keep = fulltext.scrub(res, ev_run["text"], kept["text"])
                held = fulltext.scrub(held, ev_run["text"], kept["text"])
                if res["needs_human"] and not merged:
                    fulltext.save(out, f"{res.get('graph')}:{res.get('event_id')}:{res.get('node')}", ev_run, request=request)
                res = res_keep
            else:
                res, held = _seal_material(res, held, out, f"{res.get('graph')}:{res.get('event_id')}:{res.get('node')}", ev_run, merged)
            _append(out / "decisions.jsonl", res)
            if merged:
                res["merged"] = True
            elif res["needs_human"]:
                _append(out / "queue.jsonl", {**res, "actions": held} if held and res["outcome"] == "decide" else res)
            elif res.get("notify"):   # decided automatically, but the PM should know (paging, P1, today's decision)
                _append(out / "notices.jsonl", res)
            if dedup:
                with (out / "processed.txt").open("a", encoding="utf-8") as f:
                    f.write(key + "\n")
                seen.add(key)
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


def _safe_streams():
    """Output must never crash the run: a piped stdout on Windows is cp1252 / cp932 and cannot encode "→" or Japanese.
    Piped output (a scheduler, another program) is UTF-8; a console keeps its encoding but substitutes what it cannot show."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _brief_hour():
    return config.int_value("brief_hour")


class _Parser(argparse.ArgumentParser):
    """No abbreviated options (`--back` for `--backend`): config.apply() reads the command line by the full names,
    so an abbreviation would work for argparse and not reach the settings."""

    def __init__(self, *a, **k):
        k["allow_abbrev"] = False
        super().__init__(*a, **k)


def main(argv=None):
    _safe_streams()
    argv_list = list(sys.argv[1:] if argv is None else argv)
    broken = None
    try:   # settings: argument > environment > config file > default (read once, here)
        for w in config.apply(argv_list):
            print(f"kimeru: warning: {w}", file=sys.stderr)
    except config.ConfigError as e:
        broken = e   # decided after parsing: some commands must work without a readable config file
    ap = _Parser(prog="kimeru")
    ap.add_argument("--graphs", default=str(HERE / "graphs"))
    ap.add_argument("--playbooks", default=str(DEFAULT_PLAYBOOKS))
    ap.add_argument("--out", default="out")
    ap.add_argument("--backend", type=lambda v: v.strip().lower(), choices=["stub", "jev", "kev", "clm"], default=config.value("backend"))
    ap.add_argument("--model", default="jev-latest")
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    sub.add_parser("validate")
    p_run = sub.add_parser("run")
    p_run.add_argument("files", nargs="+")
    p_w = sub.add_parser("watch")
    p_w.add_argument("inbox")
    p_w.add_argument("--interval", type=float, default=5)
    p_w.add_argument("--once", action="store_true")
    p_dg = sub.add_parser("digest", help="summary of the decisions; --week for the last 7 days in numbers")
    p_dg.add_argument("--week", action="store_true", help="the last 7 days: counts, shares, approvals, agreement")
    p_dg.add_argument("--share", action="store_true", help="with --week: numbers and environment only, safe to paste into an issue")
    p_rv = sub.add_parser("review", help="say in the terminal whether automatic decisions were right (Teams is not used)")
    p_rv.add_argument("--limit", type=int, default=20)
    p_cal = sub.add_parser("calibrate", help="tune the threshold coefficients on your reviewed decisions (the model is not called)")
    p_cal.add_argument("--min-questions", type=int, default=100)
    p_cal.add_argument("--allow-wider", action="store_true", help="also propose changes that decide more automatically")
    p_cal.add_argument("--apply", action="store_true", help="write the proposal to the local thresholds file")
    p_cal.add_argument("--revert", action="store_true", help="remove the local thresholds file")
    p_n = sub.add_parser("notify", help="post human-queue items to Teams self chat")
    p_n.add_argument("--send", action="store_true", help="actually press Enter (default: paste only)")
    p_ap = sub.add_parser("approvals", help="read OK/NG/保留/再実行/済 replies from Teams self chat; carries out approved kinds that are switched on")
    p_ap.add_argument("--send", action="store_true", help="send the results and texts ready to copy (default: paste only)")
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
    p_d.add_argument("--ado-org", default=config.value("ado_org") or None)
    p_d.add_argument("--ado-project", default=config.value("ado_project") or None)
    p_d.add_argument("--subscription", default=config.value("subscription") or None)
    p_d.add_argument("--brief-hour", type=int, default=_brief_hour())
    p_s = sub.add_parser("schedule", help="register/remove `daily --once --send` every N minutes (Task Scheduler, no admin)")
    p_s.add_argument("action", choices=["install", "remove", "status"])
    p_s.add_argument("--minutes", type=int, default=5)
    p_s.add_argument("--extra", default="", help="extra args for daily, e.g. \"--subscription s\"")
    p_s.add_argument("--ado-org", default=config.value("ado_org"))
    p_s.add_argument("--ado-project", default=config.value("ado_project"))
    p_push = sub.add_parser("push", help="notification routes for the phone (counts and numbers only)")
    p_push.add_argument("action", choices=["test", "status"])
    p_c = sub.add_parser("config", help="show / set / unset the settings that are not secret, and where each comes from")
    p_c.add_argument("action", choices=["show", "set", "unset", "path"])
    p_c.add_argument("key", nargs="?")
    p_c.add_argument("value", nargs="?")
    p_c.add_argument("--share", action="store_true", help="with show: no path, organization, subscription or address; safe to paste into an issue")
    p_r = sub.add_parser("retry", help="put inbox/done/*.error files back into the inbox (after fixing what parked them)")
    p_r.add_argument("--inbox", default="inbox")
    p_p = sub.add_parser("pull", help="poll ADO / Azure Monitor with your az login into an inbox")
    p_p.add_argument("source", choices=["ado", "alerts", "teams"])
    p_p.add_argument("--include-existing", action="store_true", help="teams: also emit chats already on screen at the first poll")
    p_p.add_argument("--inbox", default="inbox")
    p_p.add_argument("--org")
    p_p.add_argument("--project")
    p_p.add_argument("--subscription")
    p_p.add_argument("--login", action="store_true", help="ado: first `az login` into the organization's tenant")
    a = ap.parse_args(argv)
    out = Path(a.out)
    exempt = (a.cmd == "config" or (a.cmd == "schedule" and a.action in ("remove", "status"))
              or (a.cmd == "demo" and a.replay))   # these must work when the file is broken or a value is wrong
    file_ok = exempt and not (a.cmd == "config" and a.action != "path")   # `config show/set/unset` need a readable file
    if broken is not None and not file_ok:
        print(f"kimeru: the config file is broken: {broken}", file=sys.stderr)
        return 2
    if not exempt:
        bad = config.problems()
        if bad:
            for b in bad:
                print(f"kimeru: {b}", file=sys.stderr)
            print("kimeru: fix it with `kimeru config set <setting> <value>` (or unset the environment variable)", file=sys.stderr)
            return 2

    if a.cmd == "schedule":
        return schedule(a, out)

    if a.cmd == "config":
        return config_cmd(a)

    if a.cmd == "push":
        from . import push
        if a.action == "status":
            print("\n".join(push.status_lines(out)))
            return 0
        res = push.test()
        for route, r in res.items():
            print(f"{route}: {r}")
        return 1 if any(v.startswith("failed") or v.startswith("no route") for v in res.values()) else 0

    if a.cmd == "retry":
        from . import daily
        n = daily.retry_errors(a.inbox)
        print(f"retry: {n} file(s) moved back to {a.inbox}")
        return 0

    if a.cmd == "pull":
        from . import pull
        if a.source == "ado" and not all(pull.ado_names(a.org or "", a.project or "")):
            ap.error("pull ado needs --org and --project (or --org with the project URL)")
        if a.source == "alerts" and not a.subscription:
            ap.error("pull alerts needs --subscription")
        try:
            if a.source == "teams":
                n = pull.pull_teams(a.inbox, out, include_existing=a.include_existing)
            elif a.source == "ado":
                if a.login and pull.ado_login(a.org) != 0:
                    print("pull ado failed: az login for the organization's tenant did not finish", file=sys.stderr)
                    return 1
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
                ids = nt.notify(out, bridge, send=a.send, real=True)
                print(f"{'posted' if a.send else 'pasted (not sent)'}: {ids}")
            else:
                res = nt.collect(out, bridge, real=True, send=a.send)
                if getattr(res, "busy", False):
                    print("another approvals run is in progress (the daily cycle, or another window): nothing was read or applied. Try again in a minute.")
                for ch in res:
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
        if a.week or a.share:
            from . import stats
            print(stats.week(out, share=a.share))
            return 0
        return digest(out)

    if a.cmd == "calibrate" and a.revert:
        from . import calibrate
        print("removed the local thresholds file" if calibrate.revert() else "there was no local thresholds file")
        return 0

    if a.cmd == "report":
        from . import report
        page = Path(a.html) if a.html else out / "report.html"
        page.write_text(report.build(out, graph.load_dir(a.graphs, pbs)), encoding="utf-8")
        print(page)
        return 0

    gs = graph.load_dir(a.graphs, pbs)
    if a.cmd == "review":
        from . import review
        review.run(out, gs, pbs, limit=a.limit)
        return 0
    if a.cmd == "calibrate":
        from . import calibrate
        calibrate.run(out, gs, min_questions=a.min_questions, allow_wider=a.allow_wider, do_apply=a.apply)
        return 0
    be = _backend(a.backend, a.model)
    if a.cmd in ("run", "watch", "demo") and not (a.cmd == "demo" and a.replay):
        _record_warnings(out)
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
                            subscription=a.subscription, brief_hour=a.brief_hour, budget=a.interval, real=True)
            print(datetime.now().strftime("%H:%M"), json.dumps(r, ensure_ascii=False), flush=True)
            if a.once:   # non-zero when a step failed, so Task Scheduler's "last result" shows it
                failed = any(isinstance(v, str) and v.startswith("error:") for v in r.values())
                return 1 if failed or r.get("waiting") else 0
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
        for f in events.inbox_files(inbox):
            try:
                for r in process(events.read_inbox_file(f), gs, be, out, pbs, dedup=True):
                    print(_fmt(r), flush=True)
                f.replace(done / f.name)
            except BackendUnavailable as e:   # keep the file; retried next round (decided events are skipped)
                print(f"waiting: {e}", file=sys.stderr, flush=True)
                break
            except Exception as e:  # keep the loop alive; park the bad file
                print(f"error {f.name}: {e}", file=sys.stderr, flush=True)
                f.replace(done / (f.name + ".error"))
        if a.once:
            return 0
        time.sleep(a.interval)


def _record_warnings(out):
    """Values that were not allowed (and so replaced by their default) are also kept in warnings.jsonl: run, watch and demo
    would otherwise show them on the screen only (daily writes them to daily.log.jsonl)."""
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        for w in config.WARNINGS:
            _append(out / "warnings.jsonl", {"at": at, "config": w})
    except OSError:
        pass


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
    from . import stats
    line = stats.perf_line(rows)
    if line and not any((r.get("judge") or {}).get("name") == "Jev" for r in rows):
        print(line)
    for r in q:
        print(f"- [{r['event_kind']} #{r['event_id']}] {r['advice']}")
    return 0


def config_cmd(a):
    if a.share and a.action != "show":
        print("kimeru: --share goes with `config show` only", file=sys.stderr)
        return 2
    if a.action == "path":
        print(config.path())
        return 0
    if a.action == "show" and a.share:
        from . import stats
        print(stats.config_share())
        return 0
    if a.action == "show":
        print(f"config file: {config.path()}" + ("" if config.path().exists() else "  (not created yet)"))
        width = max(len(k) for k in config.SETTINGS)
        print(f"{'setting'.ljust(width)}  {'value':<40}  source")
        for k, v, src in config.rows():
            print(f"{k.ljust(width)}  {(v or '-'):<40}  {src}")
        print("secrets (environment variables; the value is never shown): "
              + ", ".join(f"{n}={'set' if on else 'not set'}" for n, on in config.secrets_status().items()))
        from . import push
        for line in push.status_lines(Path(a.out)):   # includes why a route is resting
            print(line)
        return 0
    try:
        if a.action == "set":
            if not a.key or a.value is None:
                print("kimeru: config set needs a setting and a value", file=sys.stderr)
                return 2
            val = a.value
            if a.key == "push_webhook_body" and val.startswith("@"):   # quotes do not survive every shell: the body comes from a file
                try:
                    val = Path(val[1:]).read_text(encoding="utf-8-sig").strip()
                except (OSError, UnicodeDecodeError) as e:
                    raise config.ConfigError(f"cannot read {val[1:]} ({type(e).__name__})") from None
            config.set_value(a.key, val)
            print(f"set {a.key} in {config.path()}")
            if a.key == "execute":
                from . import execute
                for t in execute.unsupported():
                    print(f"kimeru: '{t}' は未対応です（実行できるのは {', '.join(execute.SUPPORTED)} だけ）。この種類は、これまでどおり記録のみです")
        else:
            if not a.key:
                print("kimeru: config unset needs a setting", file=sys.stderr)
                return 2
            print(("unset " if config.unset_value(a.key) else "was not set: ") + a.key)
    except config.ConfigError as e:
        print(f"kimeru: {e}", file=sys.stderr)
        return 2
    return 0


TASK = "kimeru-daily"


def schedule(a, state_out=None):
    """Windows Task Scheduler entry that runs one daily cycle every N minutes as the
    current user (no admin). The task calls a tiny hidden VBS runner in the data folder,
    so no console window flashes and the /TR command stays short whatever the repo path."""
    import subprocess
    if a.action == "remove":
        return subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"]).returncode
    if a.action == "status":
        rc = subprocess.run(["schtasks", "/Query", "/TN", TASK, "/FO", "LIST"]).returncode
        from . import daily
        for line in daily.status_lines(Path(a.out)):
            print(line)
        return rc
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.exists() else exe
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    backend = f"--backend {a.backend}"   # always: the environment must not change the judge of a scheduled run
    ado = getattr(a, "ado_org", ""), getattr(a, "ado_project", "")
    extra = (f'--ado-org "{ado[0]}" --ado-project "{ado[1]}" ' if all(ado) else "") + (a.extra or "")
    # the settings in effect now are kept in the config file: the scheduled runs read it (a task has no shell profile)
    saved = config.save_effective({"backend": a.backend, "ado_org": ado[0], "ado_project": ado[1]})
    # the state folder is carried over (the task has no shell profile, and the config file lives there); the other
    # machine-specific variables (KIMERU_WRITER_CMD, KIMERU_*_EXE) are not: set them as user environment variables
    state = f'set "KIMERU_STATE_DIR={os.environ["KIMERU_STATE_DIR"]}" && ' if os.environ.get("KIMERU_STATE_DIR") else ""
    line = (f'cmd /c {state}cd /d "{HERE}" && "{runner}" -m kimeru --out "{out}" {backend} daily --once --send '
            f'--inbox "{out / "inbox"}" {extra}').strip()
    vbs = out / "run-daily.vbs"
    # VBS string literal: double every quote; window style 0 = hidden, wait for completion
    # window style 0 = hidden, wait for completion; WScript.Quit passes the exit code on, so the task's "last result" is real
    vbs.write_text('WScript.Quit CreateObject("WScript.Shell").Run("' + line.replace('"', '""') + '", 0, True)\n', encoding="utf-16")  # WSH reads UTF-8 as ANSI: Japanese paths break
    tr = f'wscript.exe "{vbs}"'
    r = subprocess.run(["schtasks", "/Create", "/TN", TASK, "/SC", "MINUTE", "/MO", str(a.minutes),
                        "/TR", tr, "/F", "/RL", "LIMITED"])
    print(("installed: " if r.returncode == 0 else "failed: ") + tr)
    print("runs: " + line)
    print(f"settings kept in {config.path()}: {', '.join(saved) or '(none)'}")
    return r.returncode
