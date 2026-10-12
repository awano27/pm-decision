"""R20 (control_words): the PM stops and resumes kimeru from the phone, with a line of the own self chat.

A line that is exactly 停止 (NFKC, without the name prefix the list shows, nothing else on the line) stops the daily cycle: it takes
nothing from Teams, judges nothing, posts nothing, runs nothing and collects nothing. The time-limit purges still run, and the chat
list is still read once a cycle, so that 再開 is seen. 再開 takes the work up again; what came meanwhile is read then. Only a change of
that line moves the mode: a line that is already on the screen at the first sighting (no out/control.json yet) only records, and so
does a line that stays. Setting control_words=0 makes both words mean nothing.

Stopping is in one direction only: it reduces what kimeru does. While it is stopped, the notices that would reach the PM (Sev0/1
included) do not come; the README and SECURITY.md say so.

R27 (quiet_words=1, read only with control_words=1): the line 休み <いつ> (for example 休み 15時 or 休み 明日; the time words are those
of hold.parse_when) sets mode quiet until that time. While it lasts, the PC notification and the push hold what is not urgent
(attention.plan with the time; the urgent items go at once, as before); intake, judging, recording and posting go on. The quiet
ends by its time: the first cycle after it announces what was held. 停止 ends a quiet at once; 再開 does not end a quiet (it only ends
a stop), and a 休み line during a stop is not read.

out/control.json keeps mode (run | stop | quiet), until (the end of a quiet, local time to the minute; null otherwise), set_at (when
the stop or the quiet began), last_sig (the sha1 of the line, not the line), ack_pending (the stop or quiet notice is not yet in the
self chat), reminded_on (the day of the last reminder). A damaged file is replaced by its copy (.bak); when neither can be read the
state is a stop, until the line says 再開.
"""
import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from . import config, fsutil, hold

STOP, RESUME = "停止", "再開"
QUIET_LINE = re.compile(r"^休み\s+(\S+)$")   # after NFKC: 休み 15時 (the whole line is the word and its time)
NAME_PREFIXES = ("あなた:", "You:")   # the list shows my own message with the name in front
ACK_STOP = ("[kimeru 停止中] 取り込み・判断・投稿・実行を止めました。重大な知らせも止まります。"
            "停止中に打った OK は再開後に効きます。「再開」で戻ります")
ACK_RESUME = "[kimeru 再開] 停止中の分を取り込みます"
ACK_QUIET = "[kimeru 休み] {when} まで、緊急でない件の PC の通知と push を止めます。取り込み・判断・投稿と、重大な件の知らせは続きます"
TOAST = ("kimeru: 停止中", "取り込み・判断・投稿・実行を止めました。「再開」で戻ります")
KEYS = ("mode", "until", "set_at", "last_sig", "ack_pending", "reminded_on")


def enabled():
    return config.value("control_words") == "1"


def quiet_enabled():
    """R27: 休み is read: quiet_words=1, with the words on (control_words=1). Off, the line means nothing."""
    return enabled() and config.value("quiet_words") == "1"


def reminder_text(hours):
    return f"[kimeru 停止中 {hours} 時間] 「再開」で戻ります"


def quiet_ack(until):
    return ACK_QUIET.format(when=until.strftime("%m/%d %H:%M"))


def line_of(chats):
    """(sig, word) of my line in the self chat: sig is the sha1 of the line without its name prefix, word is 停止, 再開 or the whole
    休み <いつ> line (R27) when the line is that, else None. None when the list has no self chat (nothing is decided then)."""
    for c in chats or []:
        if c.get("kind") == "self":
            body = unicodedata.normalize("NFKC", str(c.get("preview") or "")).strip()
            for p in NAME_PREFIXES:
                if body.startswith(p):
                    body = body[len(p):].strip()
                    break
            word = body if body in (STOP, RESUME) or QUIET_LINE.match(body) else None
            return hashlib.sha1(body.encode("utf-8")).hexdigest(), word
    return None


def _path(out):
    return Path(out) / "control.json"


def _until(text):
    """The end of a quiet as saved (naive local time, to the minute), or None when the text is not one."""
    if not isinstance(text, str):
        return None
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        return None
    return t if t.tzinfo is None else None


def read(out):
    """The saved state, or None when there is none yet. A file that cannot be read is replaced by its copy; when neither can be
    read, the state is a stop that has to be recorded again (`recovered`), so that 再開 is what resumes. A quiet without a readable
    time is a run."""
    p = _path(out)
    copy = p.with_name(p.name + ".bak")
    if not p.exists() and not copy.exists():
        return None
    for q in (p, copy):
        try:
            d = json.loads(q.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("mode") in ("run", "stop", "quiet"):
            mode, until = d["mode"], None
            if mode == "quiet":
                until = _until(d.get("until"))
                mode = "quiet" if until is not None else "run"
            return {"mode": mode, "until": until.isoformat(timespec="minutes") if until else None, "set_at": d.get("set_at"),
                    "last_sig": d.get("last_sig"), "ack_pending": bool(d.get("ack_pending")), "reminded_on": d.get("reminded_on")}
    return {"mode": "stop", "until": None, "set_at": None, "last_sig": None, "ack_pending": True, "reminded_on": None,
            "recovered": True}


def current(out):
    """The mode as saved, without a lock and without any change: 'stop' or 'run' (a quiet is a run for the cycle)."""
    st = read(out)
    return "stop" if st and st["mode"] == "stop" else "run"


def quiet_until(out):
    """R27: the end of the quiet (naive local time) while 休み is read and a quiet is saved, else None. attention.plan takes it
    every cycle; a quiet whose time has come is still returned, so that the first cycle after it can announce what was held."""
    if not quiet_enabled():
        return None
    st = read(out)
    return _until(st["until"]) if st and st["mode"] == "quiet" else None


def _iso(now):
    return now.isoformat(timespec="seconds")


def _day(now):
    return now.strftime("%Y-%m-%d")


def _hours(set_at, now):
    try:
        return max(0, int((now - datetime.fromisoformat(set_at)).total_seconds() // 3600))
    except (TypeError, ValueError):
        return 0


def _stopped(st, now):
    st.update(mode="stop", until=None, set_at=_iso(now), ack_pending=True, reminded_on=_day(now))
    return st


def _quieted(st, until, now):
    st.update(mode="quiet", until=until.isoformat(timespec="minutes"), set_at=_iso(now), ack_pending=True, reminded_on=None)
    return st


def _resumed(st):
    st.update(mode="run", until=None, set_at=None, ack_pending=False, reminded_on=None)
    return st


def observe(st, line, now):
    """The state after one look at my line, and the event it causes: 'stop', 'resume', 'quiet' or None."""
    sig, word = line
    if st is None:   # the first sighting: recorded, nothing happens
        return {"mode": "run", "until": None, "set_at": None, "last_sig": sig, "ack_pending": False, "reminded_on": None}, None
    if st["last_sig"] is None:   # no line remembered (a recovered state): 再開 resumes, anything else is recorded
        if st["mode"] == "stop" and word == RESUME:
            return _resumed(st), "resume"
        st["last_sig"] = sig
        return st, None
    if sig == st["last_sig"]:   # the same line as last time: nothing changed
        return st, None
    st["last_sig"] = sig
    if word == STOP and st["mode"] in ("run", "quiet"):
        return _stopped(st, now), "stop"
    if word == RESUME and st["mode"] == "stop":
        return _resumed(st), "resume"
    quiet = QUIET_LINE.match(word) if word else None
    if quiet and st["mode"] in ("run", "quiet") and quiet_enabled():
        until = hold.parse_when(quiet.group(1), now, config.int_value("brief_hour"))   # the words of 保留 N <いつ> (R09)
        if until is not None:
            return _quieted(st, until, now), "quiet"
    return st, None


def _say(bridge, text):
    """One line to the self chat, as the other notices are (a failure is returned, not raised: the notice is tried again)."""
    try:
        bridge.post(text, True)
        return True
    except Exception:
        return False


def _write(out, st):
    fsutil.write_atomic(_path(out), json.dumps({k: st.get(k) for k in KEYS}, ensure_ascii=False, indent=1) + "\n")


def step(out, bridge, chats, now, send=False, toaster=None):
    """The control step of one daily cycle. `chats` is the list read in this cycle (None when it could not be read). Returns 'run'
    (the cycle goes on, a quiet included: attention.plan holds what is not urgent), 'stop' (it takes nothing more) or 'busy' (the
    state is in use by another run: nothing is decided, and the cycle takes nothing more). `toaster` (title, body) shows the stop
    once; the acks and the reminder go to the self chat only in a cycle that posts (send). Nothing else is posted or run here."""
    out = Path(out)
    with fsutil.exclusive(out / "control.lock") as got:
        if not got:
            return "busy"
        st = read(out)
        before = dict(st) if st else None
        if st is not None and st.pop("recovered", False):
            st.update(set_at=_iso(now), reminded_on=_day(now))
        line = line_of(chats) if chats is not None else None
        event = None
        if line is not None:
            st, event = observe(st, line, now)
        if st is None:   # no state and no line: the cycle goes on as it always did
            return "run"
        if event == "stop" and toaster:
            try:
                toaster(*TOAST)
            except Exception:   # the notice on the PC is not needed for the stop itself
                pass
        if st["mode"] == "stop" and send:
            if st["ack_pending"]:
                if _say(bridge, ACK_STOP):
                    st["ack_pending"] = False
            elif st.get("reminded_on") != _day(now):
                if _say(bridge, reminder_text(_hours(st.get("set_at"), now))):
                    st["reminded_on"] = _day(now)
        if st["mode"] == "quiet" and send and st["ack_pending"]:   # R27: the quiet notice, tried again while the post fails
            if _say(bridge, quiet_ack(_until(st["until"]))):
                st["ack_pending"] = False
        if event == "resume" and send:
            _say(bridge, ACK_RESUME)
        if st != before:
            _write(out, st)
        return "stop" if st["mode"] == "stop" else "run"
