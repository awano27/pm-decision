"""Notification routes for the phone: numbers and counts only, to a place the PM chose.

A post in the Teams self chat never notifies the PM's own devices. Besides the Windows notification on the PC
(notify.show_toast), this module can send a short line to one or more routes the PM sets up:

  teams_webhook  a Teams Workflows (Power Automate) flow the PM built; its URL is the secret KIMERU_PUSH_TEAMS_URL
  webhook        any https endpoint: KIMERU_PUSH_WEBHOOK_URL (secret), an optional key KIMERU_PUSH_WEBHOOK_KEY (secret,
                 sent as a Bearer header, or put into the body where the template says {key}) and a body template
  outlook        a mail to the address Outlook is signed in with, and nobody else (tools/outlook-mail.ps1)

Nothing else is ever sent: no message text, no sender, no subject; not even with KIMERU_TOAST=detail.
URLs come from the environment only and never appear in a log, a record or an error text.
Every route has its own state (what it announced, when, failures, a rest after repeated failures) in
<out>/push_state.json, so one broken route never affects another route or the cycle.
When a route is turned on, what was pending before that moment is only recorded (nothing old is sent), but what is posted
from the start of that cycle on is announced (begin_cycle is called first in the cycle). A route that is turned off loses its
state, so turning it on again starts from a fresh baseline: what came while it was off is never sent.
A webhook that answers with a redirect (30x) is a failure: the request is never sent on, and the key header with it.
"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, fsutil

HERE = Path(__file__).resolve().parent.parent
ROUTES = ("teams_webhook", "webhook", "outlook")
URL_ENV = {"teams_webhook": "KIMERU_PUSH_TEAMS_URL", "webhook": "KIMERU_PUSH_WEBHOOK_URL"}
FAILS_BEFORE_REST = 3
REST_BASE = 30 * 60          # seconds; doubles with every further failure, at most 6 hours
REST_MAX = 6 * 3600


def enabled_routes():
    """Routes named in the `push` setting (case and surrounding spaces ignored; a name that is not a route is ignored, the
    others are used: config.apply warns about it)."""
    return [r for r in config.push_read(config.value("push"))[0] if r in ROUTES]


def min_interval():
    try:
        return max(0, int(config.value("push_min_minutes"))) * 60
    except ValueError:
        return 300


def message(posted, notices, unposted):
    """The only text that leaves the PC: counts, and the numbers of the items waiting."""
    parts = []
    if posted:
        parts.append(f"確認待ち {len(posted)} 件（#" + ", #".join(str(n) for n in posted[:5]) + ("…" if len(posted) > 5 else "") + "）")
    if notices:
        parts.append(f"自動決定の通知 {notices} 件")
    if unposted:
        parts.append(f"投稿できていない確認待ち {unposted} 件")
    return "kimeru: " + " / ".join(parts) + "。Teams の自分とのチャットを確認してください" if parts else ""


# ---- senders: each returns None on success or raises PushError with a message that holds no URL ----

class PushError(Exception):
    pass


def check_url(url):
    """Raise PushError (with no part of the URL in it) unless `url` is a well-formed https URL."""
    if re.search(r"\s", url):
        raise PushError("the URL is not valid (it contains a space)")
    try:
        u = urllib.parse.urlsplit(url)
        host = u.hostname
    except ValueError:
        raise PushError("the URL is not valid") from None
    if u.scheme.lower() == "http":
        raise PushError("the URL must be https (an http URL is never used)")
    if u.scheme.lower() != "https" or not host:
        raise PushError("the URL is not a valid https URL")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is never followed: urllib would carry the key header on to the new address, even to another host or http."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _urlopen(req, timeout=15):
    return _OPENER.open(req, timeout=timeout)


def _post_json(url, payload, headers=None):
    check_url(url)
    try:
        req = urllib.request.Request(url, data=payload.encode("utf-8"),
                                     headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
        with _urlopen(req, timeout=15) as r:
            if r.status >= 300:
                raise PushError(f"HTTP {r.status}")
    except urllib.error.HTTPError as e:
        raise PushError(f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise PushError(f"network error ({type(e.reason).__name__})") from None
    except PushError:
        raise
    except Exception as e:   # whatever it is, its text may hold the URL
        raise PushError(f"send failed ({type(e).__name__})") from None


def send_teams_webhook(url, text):
    card = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
                    "body": [{"type": "TextBlock", "text": text, "wrap": True}]}}]}
    _post_json(url, json.dumps(card, ensure_ascii=False))


def send_webhook(url, text):
    template = config.value("push_webhook_body") or '{"text": "{text}"}'
    key = os.environ.get("KIMERU_PUSH_WEBHOOK_KEY", "")
    fill = {"text": json.dumps(text, ensure_ascii=False)[1:-1], "key": json.dumps(key, ensure_ascii=False)[1:-1]}
    body = re.sub(r"\{(text|key)\}", lambda m: fill[m.group(1)], template)   # one pass: a value is never re-read as a placeholder
    try:
        json.loads(body)
    except ValueError:
        raise PushError("the webhook body template does not produce valid JSON") from None
    headers = {"Authorization": "Bearer " + key} if key and "{key}" not in template else None
    _post_json(url, body, headers)


def send_outlook(text):
    """The recipient is fixed inside outlook-mail.ps1: the address Outlook is signed in with. There is no -To."""
    script = HERE / "tools" / "outlook-mail.ps1"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                            "-Subject", "kimeru", "-Body", text],
                           capture_output=True, text=True, encoding="utf-8", timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        raise PushError(f"Outlook could not be started ({type(e).__name__})") from None
    if r.returncode != 0:
        raise PushError("Outlook did not send the mail (is the desktop Outlook installed and signed in?)")
    try:   # the script says "verified" only after it checked that the resolved address is the signed-in user's own
        verified = bool(json.loads((r.stdout or "").strip().lstrip("﻿") or "{}").get("verified"))
    except ValueError:
        verified = False
    if not verified:
        raise PushError("Outlook did not confirm that the recipient is your own address")


def _sender(route):
    """The function that sends `text` through `route`, or a PushError when the route is not set up."""
    if route in URL_ENV:
        url = os.environ.get(URL_ENV[route], "")
        if not url:
            raise PushError(f"{URL_ENV[route]} is not set")
        check_url(url)
        fn = send_teams_webhook if route == "teams_webhook" else send_webhook
        return lambda text: fn(url, text)
    return send_outlook


# ---- per-route state ----

def _state_path(out):
    return Path(out) / "push_state.json"


def _load(out):
    return fsutil.read_json(_state_path(out), {"routes": {}})


def _save(out, data):
    fsutil.write_atomic(_state_path(out), json.dumps(data, ensure_ascii=False))


def pending(out):
    """(numbers of pending items that are posted, keys of notices not yet announced anywhere, unposted item numbers)."""
    from . import notify
    ap = notify.Approvals(out)
    posted = sorted(int(n) for n, it in ap.data["items"].items() if it["posted"] and it["status"] in ("pending", "held"))
    unposted = sorted(int(n) for n, it in ap.data["items"].items() if not it["posted"] and it["status"] == "pending")
    return posted, list(ap.data.get("notices", [])), unposted


def begin_cycle(out, now=None):
    """Called first in a cycle that sends. A route seen for the first time is baselined NOW, before this cycle posts anything: what
    was pending until now is not sent, what this cycle posts is. Routes that are off lose their state (also when run() is not
    called while nothing is enabled), so turning one on again never sends what came while it was off."""
    routes = enabled_routes()
    data = _load(out)
    changed = False
    for r in [r for r in data["routes"] if r not in routes]:
        del data["routes"][r]
        changed = True
    if routes:
        posted, notices, unposted = pending(out)
        everything = {f"#{n}" for n in posted} | {f"unposted:{n}" for n in unposted}
        for route in routes:
            if "notice_n" not in data["routes"].get(route, {}):
                data["routes"][route] = {"sent": sorted(everything), "notice_n": len(notices), "fails": 0,
                                         "enabled_at": time.time() if now is None else now}
                changed = True
    if changed:
        _save(out, data)


def run(out, now=None, sender=None):
    """Announce what is new to each enabled route. Returns {route: result}. Never raises for a route's failure.

    What a route has been told: `sent` = the item keys it announced (only keys still pending are kept, so the list is
    bounded by the pending items and never cut short), `notice_n` = how many of the notices (an append-only list) it has
    been told about. A route's first run only records the baseline and sends nothing."""
    routes = enabled_routes()
    data = _load(out)
    if not routes:
        if data["routes"]:
            data["routes"] = {}
            _save(out, data)
        return {}
    now = time.time() if now is None else now
    posted, notices, unposted = pending(out)
    everything = {f"#{n}" for n in posted} | {f"unposted:{n}" for n in unposted}
    for r in [r for r in data["routes"] if r not in routes]:
        del data["routes"][r]            # turned off: turning it on again starts from a fresh baseline
    result = {}
    for route in routes:
        st = data["routes"].setdefault(route, {})
        if "notice_n" not in st:
            st.update({"sent": sorted(everything), "notice_n": len(notices), "fails": 0})
            result[route] = "baseline recorded (nothing old is sent)"
            continue
        st["sent"] = [k for k in st.get("sent", []) if k in everything]     # forget what is no longer pending
        st["notice_n"] = min(st["notice_n"], len(notices))
        if st.get("rest_until", 0) > now:
            result[route] = f"resting until {time.strftime('%H:%M', time.localtime(st['rest_until']))}: {st.get('last_error', '')}"
            continue
        new = everything - set(st["sent"])
        new_notices = len(notices) - st["notice_n"]
        if not new and not new_notices:
            result[route] = "nothing new"
            continue
        if now - st.get("last_at", 0) < min_interval():
            result[route] = "waiting for the minimum interval"
            continue
        text = message([n for n in posted if f"#{n}" in new], new_notices, sum(1 for n in unposted if f"unposted:{n}" in new))
        try:
            if sender is None:
                _sender(route)(text)
            else:
                sender(route, text)
        except Exception as e:   # a route's failure (of any kind) never reaches another route or the cycle
            st["fails"] = st.get("fails", 0) + 1
            st["last_error"] = (str(e) if isinstance(e, PushError) else f"unexpected error ({type(e).__name__})")[:120]
            st["last_at"] = now
            if st["fails"] >= FAILS_BEFORE_REST:
                st["rest_until"] = now + min(REST_MAX, REST_BASE * 2 ** (st["fails"] - FAILS_BEFORE_REST))
            result[route] = f"failed: {st['last_error']}"
            continue
        st["sent"] = sorted(set(st["sent"]) | new)
        st["notice_n"] = len(notices)
        st.update({"fails": 0, "last_at": now, "last_ok": now, "last_error": ""})
        st.pop("rest_until", None)
        result[route] = "sent"
    _save(out, data)
    return result


def test(sender=None):
    """`kimeru push test`: one fixed line to every enabled route (that is the only text this command ever sends)."""
    routes = enabled_routes()
    if not routes:
        return {"(none)": "no route is set: config set push teams_webhook,webhook,outlook"}
    text = "kimeru: 試験の通知です（件数も本文もありません）"
    result = {}
    for route in routes:
        try:
            (sender(route, text) if sender else _sender(route)(text))
            result[route] = "sent"
        except PushError as e:
            result[route] = f"failed: {e}"
        except Exception as e:   # the text of an unexpected error may hold the URL: the type only
            result[route] = f"failed: unexpected error ({type(e).__name__})"
    return result


def status_lines(out, now=None):
    now = time.time() if now is None else now
    routes = enabled_routes()
    if not routes:
        return ["push routes: none (the PC notification only)"]
    data = _load(out).get("routes", {})
    lines = []
    for r in routes:
        st = data.get(r, {})
        bits = [f"push {r}:"]
        if st.get("rest_until", 0) > now:
            bits.append(f"resting until {time.strftime('%H:%M', time.localtime(st['rest_until']))}")
        if st.get("last_error"):
            bits.append(f"last error: {st['last_error']}")
        if st.get("last_ok"):
            bits.append("last ok " + time.strftime("%m-%d %H:%M", time.localtime(st["last_ok"])))
        if len(bits) == 1:
            bits.append("no notification yet")
        lines.append(" ".join(bits))
    return lines
