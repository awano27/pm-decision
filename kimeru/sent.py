"""Did the approved reply go out? (R14, setting sent_watch=1; the default 0 does nothing here.)

After an OK, the self-chat list (the one the pull reads every cycle) may show the PM's own new message in the reply's chat. When it does,
and the chat's list signature has changed since the OK, the message is compared with the approved text:

  as_is   the list text starts the approved text (cut lists end with an ellipsis, which is ignored)
  edited  at least EDITED_MIN of the characters match (the PM changed a word before sending)
  other   anything else (another message of mine)

Only the state, the time and the similarity are kept, on the approved item (`sent_seen`). No text, no chat is opened, nothing is clicked
or sent, and the state of the item and the work do not change. It is a reading of the list, not a receipt: the list shows that a message
of mine is there, not that the other person got it. Only the daily path calls observe() (real=True).

R28 (C04b, the same setting): an approved reply that no send was seen for, `sent_nudge_hours` after the OK, is nudged once in its life, one
post a day, to the self chat (`[kimeru 未送信 #N]`, numbers only). `送った N` (notify.parse_sent) says it was sent: the nudge stops and the
item keeps `sent_confirmed`. Nothing else on the item changes, and nothing is nudged without sent_watch=1."""
import difflib
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from . import config, fsutil, notify, pull

EDITED_MIN = 0.4                        # similarity from which a message counts as the approved text, edited
STATES = ("as_is", "edited", "other")
BASE, SEEN = "sent_base", "sent_seen"   # on the approved item: the reply's chat and list signature at the OK; the state seen
DAYS = 3                                # days after the OK in which the send is looked for (setting sent_watch_days)
NUDGED, CONFIRMED = "nudged_at", "sent_confirmed"   # R28: when the nudge was put in the outbox; when `送った N` came
NUDGE_DAY = "nudge_day"                 # R28, top level of approvals.json: the local day of the last nudge post
NUDGE_HOURS = 2                         # hours after the OK before the nudge (setting sent_nudge_hours)
NUDGE_LINE = "承認した返信が、自分とのチャットの一覧にまだ見えません。送り終えたら「送った N」と返信してください（例: 送った 3）。"
_ELLIPSIS = re.compile(r"\.{3,}$")      # after NFKC, a cut list preview ends with "..."


def enabled():
    return config.value("sent_watch") == "1"


def nudge_hours():
    """sent_nudge_hours: a whole number of hours, 1 or more; anything else is the default (2)."""
    try:
        n = int(config.value("sent_nudge_hours"))
    except (TypeError, ValueError):
        return NUDGE_HOURS
    return n if n >= 1 else NUDGE_HOURS


def days():
    try:
        n = int(config.value("sent_watch_days"))
    except (TypeError, ValueError):
        return DAYS
    return n if n >= 1 else DAYS


def _norm(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text or ""))).strip()


def classify(sent, shown):
    """(state, ratio): what the list shows (`shown`, without the name prefix, possibly cut) against the approved text (`sent`)."""
    a = _norm(sent)
    b = _ELLIPSIS.sub("", _norm(shown)).rstrip()
    if not b:
        return "other", 0.0
    if a.startswith(b):
        return "as_is", 1.0
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    return ("edited" if ratio >= EDITED_MIN else "other"), round(ratio, 2)


def _aware(when):
    """A naive time is this PC's local time (the cycle's clock); an aware one is kept."""
    return when.astimezone() if when.tzinfo is None else when


def _utc_iso(when):
    return _aware(when).astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value):
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def list_signatures(out):
    """The chat list signatures the pull saved: {chat_id: {"p": hash of the preview, ...}}. A hash, never the text."""
    teams = pull.State(out).data.get("teams")
    sigs = teams.get("sigs") if isinstance(teams, dict) else None
    return sigs if isinstance(sigs, dict) else {}


def _reply_actions(item, chat_id=None):
    for action in (item.get("record") or {}).get("actions") or []:
        if isinstance(action, dict) and action.get("type") == "teams.reply" and action.get("chat_id") and _norm(action.get("text")):
            if chat_id is None or action["chat_id"] == chat_id:
                yield action


def note_approval(item, sigs, now):
    """An OK was recorded (sent_watch=1): the reply's chat and its list signature now are kept, so that a send can be told from what
    was on the list before. A new OK starts again: an earlier sighting is dropped. Nothing is kept when the chat is not on the list."""
    item.pop(SEEN, None)
    item.pop(BASE, None)
    item.pop(NUDGED, None)       # R28: a new OK starts the nudge again (a nudge or `送った N` for an earlier OK does not count)
    item.pop(CONFIRMED, None)
    for action in _reply_actions(item):
        chat_id = str(action["chat_id"])
        entry = sigs.get(chat_id)
        if isinstance(entry, dict) and isinstance(entry.get("p"), str):
            item[BASE] = {"chat_id": chat_id, "p": entry["p"], "approved_at": _utc_iso(now)}
        return


def _sighting(item, own, sigs, when, limit):
    """{state, at, ratio} when the item's approved reply is seen in `own` now; None when it is not (or was seen before)."""
    if not isinstance(item, dict) or item.get("status") != "approved" or SEEN in item:
        return None
    base = item.get(BASE)
    if not isinstance(base, dict) or not isinstance(base.get("chat_id"), str):
        return None
    approved = _parse(base.get("approved_at"))
    if approved is None or not approved <= when <= approved + limit:
        return None
    chat_id = base["chat_id"]
    entry = sigs.get(chat_id)
    current = entry.get("p") if isinstance(entry, dict) else None
    if not isinstance(current, str) or current == base.get("p"):
        return None                                  # the list shows what it showed at the OK: no send is seen
    shown = [body for cid, body in own if cid == chat_id]
    if not shown:
        return None
    sent_text = next((a["text"] for a in _reply_actions(item, chat_id)), "")
    state, ratio = classify(sent_text, shown[-1])
    return {"state": state, "at": _utc_iso(when), "ratio": ratio}


def observe(out, own, now=None):
    """Look for the sends of approved replies among `own`: the (chat_id, body) pairs of my own new messages the list showed in this
    cycle (in memory only). Returns the items seen as (number, state) in a BusyList. With the setting off, or without own messages,
    nothing is read or written and the list is empty. When approvals.lock is held by another run, nothing is written (busy)."""
    if not own or not enabled():
        return fsutil.BusyList()
    when = _aware(now if now is not None else datetime.now())
    limit = timedelta(days=days())
    sigs = list_signatures(out)
    with fsutil.exclusive(notify.lock_path(out)) as got:
        if not got:
            return fsutil.BusyList.busy_result()
        ap = notify.Approvals(out)
        found = []
        for num, item in ap.data.get("items", {}).items():
            seen = _sighting(item, own, sigs, when, limit)
            if seen is None:
                continue
            item[SEEN] = seen
            found.append((int(num), seen["state"]))
        if found:
            ap.save()
    return fsutil.BusyList(found)


# ---- R28: the nudge and `送った N` (called by notify._collect with real=True and send=True only)

def _day(when):
    return _aware(when).astimezone().date().isoformat()


def _open(item, when):
    """The OK's time when the item's reply is still watched at `when`: approved, its chat kept at the OK, no send seen, no `送った N`,
    within days() after the OK (the same window as observe). None otherwise."""
    if not isinstance(item, dict) or item.get("status") != "approved" or SEEN in item or CONFIRMED in item:
        return None
    base = item.get(BASE)
    if not isinstance(base, dict) or not isinstance(base.get("chat_id"), str):
        return None
    approved = _parse(base.get("approved_at"))
    if approved is None or not approved <= when <= approved + timedelta(days=days()):
        return None
    return approved


def nudge_due(item, when):
    """A watched reply that is nudge_hours() old and has not been nudged yet."""
    approved = _open(item, _aware(when))
    return approved is not None and NUDGED not in item and _aware(when) >= approved + timedelta(hours=nudge_hours())


def nudged_open(item, when):
    """A nudged reply that is still watched: it counts for the phone (push.py, numbers only). Nothing while the setting is off."""
    return enabled() and NUDGED in item and _open(item, _aware(when)) is not None


def awaiting(data, item, when):
    """True when this cycle has to read the self chat for the item: a nudged reply whose send is not seen or confirmed yet, or a due one on
    a day when no nudge was posted yet (the nudge decides after the read, so a `送った N` already on the screen stops it). False when off."""
    if not enabled():
        return False
    when = _aware(when)
    if NUDGED in item:
        return _open(item, when) is not None
    return nudge_due(item, when) and data.get(NUDGE_DAY) != _day(when)


def nudge(data, post, when):
    """One nudge post a day: the items that are due (see nudge_due) are listed by number, once each, and the day is kept (nudge_day).
    The state is set before `post(text)` runs: notify.make_post saves the outbox text and this state in one save, so a run that stops
    in between never leaves the text in the outbox without the state (which would post a second nudge the same day). Returns the
    numbers listed; [] writes nothing."""
    if not enabled():
        return []
    when = _aware(when)
    if data.get(NUDGE_DAY) == _day(when):
        return []
    items = data.get("items", {})
    due = sorted((n for n, it in items.items() if nudge_due(it, when)), key=int)
    if not due:
        return []
    for n in due:
        items[n][NUDGED] = _utc_iso(when)
    data[NUDGE_DAY] = _day(when)
    post("[kimeru 未送信 " + " ".join(f"#{n}" for n in due) + "]\n" + NUDGE_LINE)
    return due


def confirm(item, when):
    """`送った N` (R28): the PM says the approved reply was sent. Kept once, as a time. Only an approved item whose reply chat was kept at
    the OK counts. The status, the record and the chat do not change. True when it was recorded now."""
    if not isinstance(item, dict) or item.get("status") != "approved" or CONFIRMED in item or not isinstance(item.get(BASE), dict):
        return False
    item[CONFIRMED] = {"at": _utc_iso(when)}
    return True
