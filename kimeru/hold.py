"""`保留 N <いつ>`: a hold that comes back on that day (R09).

`保留 3 明日` keeps #3 on hold until the morning brief hour of tomorrow. The first daily cycle after that time posts #3 again under
the same number, its first line says `（保留から・2 日目）`, and that post is the boundary for the replies: the old reply above it is
not read again. Nothing is approved, rejected or executed on the way; a re-post only asks the question again.

Words read (the day counts from the reply; a day word gives that day at the brief hour): 明日, 明後日, 来週 (the Monday of next week),
月〜金 with 曜 or 曜日 (the next such day after today: a 金 said on a Friday is the Friday of next week), N日後 (N >= 1), M/D and
M月D日 (a later date this year), 15時 and 15:30 (today while still ahead, else tomorrow). A word that is not read, or a day further
ahead than `full_text_keep_days`, is a hold without a date (`保留（日付なし）`, named once on the next approval post).

The fields kept on an item of approvals.json: held_until (local time, ISO, minutes), held_at (when the hold began), hold_count,
hold_word (a hash of the word: the same reply read again in a later cycle does not move the date), hold_round (how many times the
item came back; the phone's key is `#N` and `#Nr<round>`), hold_day (the day of the hold, on the next post), hold_note (the undated
note still to name). Nothing of the word itself is kept.
"""
import hashlib
import re
import unicodedata
from datetime import date, datetime, time, timedelta

from . import config, fsutil

HOLD_UNTIL = re.compile(r"^保留\s*#?(\d+)\s+(\S{1,10})$")          # after NFKC: 保留 3 明日, 保留 ３ 金曜
WEEKDAY = re.compile(r"^([月火水木金])(?:曜日?)?$")
WEEKDAYS = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4}
DAYS_LATER = re.compile(r"^(\d{1,2})日後$")
DATE_WORD = re.compile(r"^(?:(\d{1,2})/(\d{1,2})|(\d{1,2})月(\d{1,2})日)$")
TIME_OF_DAY = re.compile(r"^(\d{1,2})(?::(\d{2})|時(?:(\d{1,2})分)?)$")


def parse_hold_until(line):
    """("3", "明日") for "保留 3 明日" (a full-width or re-cased phone text too); None for any other line, and for a word that is only
    punctuation: `保留 3 。` is the plain hold (notify reads it as one)."""
    m = HOLD_UNTIL.match(unicodedata.normalize("NFKC", line).strip())
    if not m:
        return None
    word = m.group(2).rstrip(".。!")
    return (m.group(1), word) if word else None


def _local(now):
    """A naive local datetime (the cycle's clock is naive local; an aware one is converted)."""
    return now.astimezone().replace(tzinfo=None) if now.tzinfo is not None else now


def _stamp(dt):
    return dt.isoformat(timespec="minutes")


def _parse(text):
    try:
        return datetime.fromisoformat(text) if text else None
    except (TypeError, ValueError):
        return None


def _brief_hour():
    return config.int_value("brief_hour")


def parse_when(word, now, brief_hour=8, keep_days=None):
    """The moment a hold `保留 N <word>` lasts until (naive local time), or None when the word is not read or the day is further ahead
    than `keep_days` (default: full_text_keep_days). The words are in the module doc."""
    from . import fulltext   # lazily: fulltext reads the approvals file through notify, which imports this module
    now = _local(now)
    today = now.date()
    keep = fulltext.keep_days() if keep_days is None else keep_days
    w = unicodedata.normalize("NFKC", str(word or "")).strip()
    day = at = None
    if w == "明日":
        day = today + timedelta(days=1)
    elif w == "明後日":
        day = today + timedelta(days=2)
    elif w == "来週":
        day = today + timedelta(days=7 - today.weekday())
    elif WEEKDAY.match(w):
        wd = WEEKDAYS[WEEKDAY.match(w).group(1)]
        day = today + timedelta(days=(wd - today.weekday()) % 7 or 7)
    elif DAYS_LATER.match(w):
        n = int(DAYS_LATER.match(w).group(1))
        if n < 1:
            return None
        day = today + timedelta(days=n)
    elif DATE_WORD.match(w):
        m = DATE_WORD.match(w)
        month, dd = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        try:
            day = date(today.year, int(month), int(dd))
        except ValueError:
            return None
        if day <= today:
            return None
    elif TIME_OF_DAY.match(w):
        m = TIME_OF_DAY.match(w)
        hh, mm = int(m.group(1)), int(m.group(2) or m.group(3) or 0)
        if hh > 23 or mm > 59:
            return None
        at = datetime.combine(today, time(hh, mm))
        if at <= now:
            at += timedelta(days=1)
    if at is None:
        if day is None:
            return None
        at = datetime.combine(day, time(brief_hour))
    if (at.date() - today).days > keep:
        return None
    return at


def _sig(word):
    return hashlib.sha1(word.encode("utf-8")).hexdigest()[:8]


def apply(it, word, now, entering):
    """A `保留 N` (word None) or `保留 N <いつ>` on an item that is held now (the caller has set its status); `entering`: it was
    pending before. Returns {"until": "YYYY-MM-DD" or None} when a dated reply changed the hold (the change to log), else None.
    A plain 保留 clears a date. A date already read stays: the same reply is on the screen in every cycle, and is not read again."""
    if entering:
        it["held_at"] = _stamp(_local(now))
    if word is None:
        if entering:
            it["hold_count"] = int(it.get("hold_count") or 0) + 1
        for k in ("held_until", "hold_word", "hold_note"):
            it.pop(k, None)
        return None
    sig = _sig(word)
    if it.get("hold_word") == sig and not entering:
        return None
    when = parse_when(word, now, _brief_hour())
    it["hold_word"] = sig
    it["hold_note"] = when is None
    if when is None:
        it.pop("held_until", None)
    else:
        it["held_until"] = _stamp(when)
    it["hold_count"] = int(it.get("hold_count") or 0) + 1
    return {"until": when.date().isoformat() if when else None}


def clear(it):
    """The hold ends (the item is decided, or asked again): no date and no note stay on it."""
    for k in ("held_until", "hold_word", "hold_note"):
        it.pop(k, None)


def resurface(out, now):
    """Held items whose day has come go back to pending and unposted, under the same number (the next post of each is that number
    again). It runs under approvals.lock, the lock of collect: busy (nothing changed) when another run holds it. Returns the numbers,
    or the busy result. Nothing is approved, rejected or executed."""
    from . import notify   # lazily: notify imports this module
    now = _local(now)
    with fsutil.exclusive(notify.lock_path(out)) as got:
        if not got:
            return fsutil.BusyList.busy_result()
        ap = notify.Approvals(out)
        back = []
        for n, it in ap.data["items"].items():
            until = _parse(it.get("held_until"))
            if it.get("status") != "held" or not it.get("posted") or until is None or until > now or it.get("delivery_unknown"):
                continue
            since = _parse(it.get("held_at"))
            day = max(1, (now.date() - since.date()).days + 1) if since else 1   # the day it was held is the first day
            it.update({"status": "pending", "posted": False, "hold_round": int(it.get("hold_round") or 0) + 1, "hold_day": day})
            clear(it)
            notify.next_measurement(it)
            back.append(n)
        if back:
            ap.save()
        return fsutil.BusyList(back)


def post_marks(it, items):
    """What the approval post of `it` carries from R09: its day when it came back, and the numbers of the undated holds not named yet.
    None when there is nothing to say (the usual post)."""
    marks = {}
    if it.get("hold_day"):
        marks["day"] = int(it["hold_day"])
    undated = sorted((n for n, x in items.items() if x.get("hold_note")), key=int)
    if undated:
        marks["undated"] = undated
    return marks or None


def noted(it, items, marks):
    """The post of `it` was posted: its day, and the undated notes it carried, are not said again."""
    if not marks:
        return
    it.pop("hold_day", None)
    for n in marks.get("undated", []):
        if n in items:
            items[n].pop("hold_note", None)


def brief_label(n, it, now):
    """`保留 #3（10/15 に再表示）` for a held item with a day, `長く保留: #3（3 日）` for an undated one held for more than
    `hold_stale_days` days (0 = never); None otherwise (the brief keeps `確認待ち #3`)."""
    if it.get("status") != "held":
        return None
    until = _parse(it.get("held_until"))
    if until is not None:
        return f"保留 #{n}（{until.month}/{until.day} に再表示）"
    stale = config.int_value("hold_stale_days")
    since = _parse(it.get("held_at"))
    if stale > 0 and since is not None:
        days = (_local(now).date() - since.date()).days
        if days > stale:
            return f"長く保留: #{n}（{days} 日）"
    return None
