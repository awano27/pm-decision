"""R26 (setting spot_check_per_day, 0 to 5, default 0): the day's cards to confirm, after the morning brief.

Up to K automatic decisions of the last day are posted to the self chat, one card each: `[kimeru 確認 S<k>] …`. The numbers are
their own counter S1, S2, ... and never the `#N` of an approval, so `OK 3` / `NG 3` cannot answer a card and a card is never read as
an approval post (tools/teams-self.ps1 reads it as "S:k"). The PM answers `正しい S<k>` (the decision was right) or `違う S<k>` (the
final result was wrong). The answer is a label: labels.record_spot writes the rows `kimeru review` writes, and nothing about the
decision, its post, its approval or its execution changes. The cards live in out/spot.json, never in approvals.json, so the digest,
push, brief, trial and work counts do not move. Only an automatic decision that a model answered is chosen: not a rule alone, not
the offline stub, not one that waits for the PM, not one that is labeled already, not one older than a day.
"""
import json
import random
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import brief as brief_mod, config, fsutil, graph, labels, notify, review
from .backends import StubBackend

MAX_PER_DAY = 5
DAYS_VALID = 3                                   # a card (and its answer) is kept this many days after it was posted
WINDOW = timedelta(days=1)                       # the decisions a card may ask about: the last day
CARD_CHARS = 70                                  # characters of the decision's summary on the card
STUB = StubBackend.__name__                      # judge_name of the offline stub: no judgment to check
REPLY = re.compile(r"^(正しい|違う)\s*S(\d+)$", re.IGNORECASE)   # the answer to a card (the reader hands it over as "R:正しい S<k>")


def path(out):
    return Path(out) / "spot.json"


def per_day():
    try:
        return max(0, min(MAX_PER_DAY, int(config.value("spot_check_per_day"))))
    except ValueError:
        return 0


def enabled():
    """On with a setting of 1 to 5, and never in a test run (the test reader would not read these cards)."""
    return per_day() > 0 and not notify.test_mode()


def _aware(now):
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo else now.astimezone()          # a naive now is the local clock (what the daily cycle passes)


def _when(s):
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.astimezone()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _day(now):
    return _aware(now).astimezone().strftime("%Y-%m-%d")


def _expired(it, now):
    until = it.get("until")
    try:
        return not until or _when(until) < now
    except (TypeError, ValueError):
        return True


def load(out):
    st = fsutil.read_json(path(out), {})
    if not isinstance(st, dict):
        st = {}
    st.setdefault("next", 1)
    st.setdefault("date", "")
    if not isinstance(st.get("items"), list):
        st["items"] = []
    return st


def _live(it, now, today):
    """A posted card lives until `until` (three days); one that was not posted yet lives for its own day only."""
    if it.get("posted_at"):
        return not _expired(it, now)
    return it.get("day") == today


def save(out, st, now):
    st["items"] = [it for it in st["items"] if _live(it, now, _day(now))]
    Path(out).mkdir(parents=True, exist_ok=True)
    fsutil.write_atomic(path(out), json.dumps(st, ensure_ascii=False, indent=1))


def awaiting(out, now=None):
    """True while a posted card waits for its answer: the cycle then reads the reply (notify.collect) even with no approval waiting."""
    if not enabled():
        return False
    now = _aware(now)
    return any(it.get("posted_at") and not it.get("answer") and not _expired(it, now) for it in load(out)["items"])


def candidates(out, now=None):
    """The decision records a card may ask about: automatic (outcome decide, nothing asked of the PM), with the event kept, decided
    within the last day, decided by a model (a rule alone is not), not by the offline stub, not labeled yet and not on a card already.
    The newest judgment of each decision counts."""
    now = _aware(now)
    since = now - WINDOW
    labeled = {r.get("key") for r in review.read_jsonl(review.reviews_path())}
    carded = {it.get("key") for it in load(out)["items"]}
    seen, found = set(), []
    for rec in reversed(review.read_jsonl(Path(out) / "decisions.jsonl")):
        key = review.decision_key(rec)
        if key in seen:
            continue
        seen.add(key)
        if not rec.get("event") or rec.get("path") is None or rec.get("outcome") != "decide":
            continue
        if rec.get("needs_human") or rec.get("notify") or not rec.get("at"):
            continue
        try:
            at = _when(rec["at"])
        except (TypeError, ValueError):
            continue
        if not since <= at <= now:
            continue
        if (rec.get("judge") or {}).get("name") == STUB or key in labeled or key in carded:
            continue
        if not any("matched" not in (step.get("answer") or {}) for step in rec["path"]):
            continue
        found.append(rec)
    return found


def choose(out, now, k, rng=None):
    """k candidates, drawn uniformly without replacement (fewer when there are fewer)."""
    pool = candidates(out, now)
    if k <= 0 or not pool:
        return []
    return (rng or random.Random()).sample(pool, min(k, len(pool)))


def _outcome(rec):
    if rec.get("event_kind") == "ado.workitem.created":
        return brief_mod._ado_outcome(rec)
    return brief_mod.UNSURE_NODE.get(rec.get("node")) or brief_mod.TEAMS_OUTCOME.get(rec.get("node")) or ""


def card(k, rec):
    """The card's text. Its first line is the only one the reader takes (it starts with `[kimeru`, so only that line counts)."""
    kind = brief_mod.UNSURE_KIND.get(rec.get("event_kind"), "")
    line = graph.one_line(str(rec.get("summary") or rec.get("event_id") or ""), CARD_CHARS)
    what = _outcome(rec)
    body = "・" + " ".join(x for x in (kind, line) if x) + (f" → {what}" if what else "")
    return "\n".join([f"[kimeru 確認 S{k}] 自動で決めた件を確かめてください", body, f"返信: 正しい S{k} / 違う S{k}"])


def _mark(it, now):
    it["posted_at"], it["until"] = _iso(now), _iso(now + timedelta(days=DAYS_VALID))


def _post(bridge, text):
    """The answer of a post; the data of the error when it raised (None when it has none, which notify reads as unknown)."""
    try:
        return bridge.post(text, True)
    except Exception as e:
        return getattr(e, "data", None)


def step(out, bridge, now=None, brief_done=False, real=True, send=True, rng=None):
    """The day's cards, once a day, after the morning brief (the cycle calls it with real and send). Returns the steps done
    ("S1:posted", "S1:unknown", "S1:not_sent"), [] when there is nothing to do, None when it is off or the approvals lock is busy.
    A card that was not sent is tried again on the next cycle of the day; one that may have been sent is not posted again."""
    if not (enabled() and real and send and brief_done):
        return None
    now = _aware(now)
    today = _day(now)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    with fsutil.exclusive(notify.lock_path(out)) as got:
        if not got:
            return None
        st = load(out)
        if st.get("date") != today:
            base = int(st.get("next") or 1)
            chosen = choose(out, now, per_day(), rng)
            st["items"] += [{"k": base + j, "key": review.decision_key(rec), "day": today, "posted_at": None, "until": None, "answer": None}
                            for j, rec in enumerate(chosen)]
            st["next"], st["date"] = base + len(chosen), today
            save(out, st, now)
        done = []
        for it in st["items"]:
            if it.get("posted_at") or it.get("day") != today:
                continue
            rec = labels.row_of(out, it["key"])
            if rec is None:
                continue
            result = _post(bridge, card(it["k"], rec))
            tag = f"S{it['k']}"
            if notify._delivered(result, True):
                _mark(it, now)
                done.append(f"{tag}:posted")
            elif notify._delivery_unknown(result, True):
                _mark(it, now)                 # it may be on the screen: it is not posted a second time
                it["unknown"] = True
                done.append(f"{tag}:unknown")
            else:
                done.append(f"{tag}:not_sent")  # nothing was sent: the next cycle of the day tries again
                break
            save(out, st, now)
        save(out, st, now)
        return done


def apply(out, read, now=None):
    """The answers on the screen: `正しい S<k>` / `違う S<k>` that stand below the card's own `S:k` entry. Each card is answered once
    (the first answer counts); a card past its days takes nothing. Called by notify.collect with the approvals lock held, on a real run
    that sends. Returns [(k, verdict)] for the answers recorded."""
    if not enabled():
        return []
    tl = notify.scoped_timeline(read)
    if tl is None:
        return []
    now = _aware(now)
    st = load(out)
    items = {it.get("k"): it for it in st["items"]}
    cards = {}
    for i, e in enumerate(tl):
        if e.startswith("S:") and e[2:].isdigit():
            cards[int(e[2:])] = i
    done = []
    for i, e in enumerate(tl):
        if not e.startswith("R:"):
            continue
        m = REPLY.match(unicodedata.normalize("NFKC", e[2:]).strip())
        if not m:
            continue
        word, k = m.group(1), int(m.group(2))
        it = items.get(k)
        if it is None or not it.get("posted_at") or it.get("answer") or k not in cards or i <= cards[k] or _expired(it, now):
            continue
        verdict = labels.record_spot(out, it["key"], word, k, _iso(now))
        it["answer"] = {"verdict": verdict or "none", "at": _iso(now)}
        done.append((k, verdict))
    if done:
        save(out, st, now)
    return done


_noted = set()


def _warn(out, what):
    """A failure of the cards is noted once per process in warnings.jsonl (the exception's name only; the reply waits for the next cycle)."""
    if (str(out), what) in _noted:
        return
    _noted.add((str(out), what))
    Path(out).mkdir(parents=True, exist_ok=True)
    with (Path(out) / "warnings.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _iso(_aware(None)), "spot": what}, ensure_ascii=False) + "\n")


def apply_safe(out, read, now=None):
    """apply, for notify.collect: it never raises into the approvals step (a failure is noted; the approvals go on)."""
    try:
        return apply(out, read, now)
    except Exception as e:
        _warn(out, f"apply ({type(e).__name__})")
        return []


def answers(out, bridge, now=None):
    """The answers when no approval waits (notify.collect's early return): Teams is read only while a card waits for its answer."""
    if not awaiting(out, now):
        return []
    try:
        read = bridge.read()
    except Exception as e:
        _warn(out, f"read ({type(e).__name__})")
        return []
    return apply_safe(out, read, now)
