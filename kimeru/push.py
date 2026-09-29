"""Notification routes for the phone: numbers and counts only, to a place the PM chose.

A post in the Teams self chat never notifies the PM's own devices. Besides the Windows notification on the PC
(notify.show_toast), this module can send a short line to one or more routes the PM sets up:

  teams_webhook  a Teams Workflows (Power Automate) flow the PM built; its URL is the secret KIMERU_PUSH_TEAMS_URL
  webhook        any HTTP endpoint: KIMERU_PUSH_WEBHOOK_URL (secret) and a body template (push_webhook_body)
  outlook        a mail to the PM's own address through the desktop Outlook (tools/outlook-mail.ps1)

Nothing else is ever sent: no message text, no sender, no subject; not even with KIMERU_TOAST=detail.
URLs come from the environment only and never appear in a log, a record or an error text.
Every route has its own state (what it announced, when, failures, a rest after repeated failures) in
<out>/push_state.json, so one broken route never affects another route or the cycle.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config, fsutil

HERE = Path(__file__).resolve().parent.parent
ROUTES = ("teams_webhook", "webhook", "outlook")
URL_ENV = {"teams_webhook": "KIMERU_PUSH_TEAMS_URL", "webhook": "KIMERU_PUSH_WEBHOOK_URL"}
FAILS_BEFORE_REST = 3
REST_BASE = 30 * 60          # seconds; doubles with every further failure, at most 6 hours
REST_MAX = 6 * 3600
KEEP_KEYS = 200              # announced keys kept per route


def enabled_routes():
    """Routes named in the `push` setting (unknown names are ignored)."""
    raw = config.value("push")
    return [r for r in (x.strip() for x in raw.split(",")) if r in ROUTES]


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


def _post_json(url, payload):
    req = urllib.request.Request(url, data=payload.encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status >= 300:
                raise PushError(f"HTTP {r.status}")
    except urllib.error.HTTPError as e:
        raise PushError(f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise PushError(f"network error ({type(e.reason).__name__})") from None
    except (OSError, ValueError) as e:
        raise PushError(f"send failed ({type(e).__name__})") from None


def send_teams_webhook(url, text):
    card = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
                    "body": [{"type": "TextBlock", "text": text, "wrap": True}]}}]}
    _post_json(url, json.dumps(card, ensure_ascii=False))


def send_webhook(url, text):
    template = config.value("push_webhook_body") or '{"text": "{text}"}'
    body = template.replace("{text}", json.dumps(text, ensure_ascii=False)[1:-1])
    try:
        json.loads(body)
    except ValueError:
        raise PushError("the webhook body template does not produce valid JSON") from None
    _post_json(url, body)


def send_outlook(to, text):
    script = HERE / "tools" / "outlook-mail.ps1"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                            "-To", to, "-Subject", "kimeru", "-Body", text],
                           capture_output=True, text=True, encoding="utf-8", timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        raise PushError(f"Outlook could not be started ({type(e).__name__})") from None
    if r.returncode != 0:
        raise PushError("Outlook did not send the mail (is the desktop Outlook installed and signed in?)")


def _sender(route):
    """The function that sends `text` through `route`, or a PushError when the route is not set up."""
    if route in URL_ENV:
        url = os.environ.get(URL_ENV[route], "")
        if not url:
            raise PushError(f"{URL_ENV[route]} is not set")
        fn = send_teams_webhook if route == "teams_webhook" else send_webhook
        return lambda text: fn(url, text)
    to = config.value("push_mail_to")
    if not to:
        raise PushError("push_mail_to is not set")
    return lambda text: send_outlook(to, text)


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


def run(out, now=None, sender=None):
    """Announce what is new to each enabled route. Returns {route: result}. Never raises for a route's failure."""
    routes = enabled_routes()
    if not routes:
        return {}
    now = time.time() if now is None else now
    posted, notices, unposted = pending(out)
    everything = {f"#{n}" for n in posted} | {f"notice:{k}" for k in notices} | {f"unposted:{n}" for n in unposted}
    data = _load(out)
    result = {}
    for route in routes:
        st = data["routes"].setdefault(route, {"sent": [], "fails": 0})
        if st.get("rest_until", 0) > now:
            result[route] = f"resting until {time.strftime('%H:%M', time.localtime(st['rest_until']))}: {st.get('last_error', '')}"
            continue
        new = everything - set(st["sent"])
        if not new:
            result[route] = "nothing new"
            continue
        if now - st.get("last_at", 0) < min_interval():
            result[route] = "waiting for the minimum interval"
            continue
        text = message([n for n in posted if f"#{n}" in new],
                       sum(1 for k in notices if f"notice:{k}" in new),
                       sum(1 for n in unposted if f"unposted:{n}" in new))
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
        st["sent"] = (st["sent"] + sorted(new))[-KEEP_KEYS:]
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
