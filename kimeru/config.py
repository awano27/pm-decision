"""One place for the settings that are not secret, and a record of where each one came from.

Order of precedence: command-line argument > environment variable > config file > default.
The config file is `config.json` in the state folder (KIMERU_STATE_DIR, else %LOCALAPPDATA%\\kimeru). It is read once
at start-up: `apply()` copies file values into the environment for the settings the environment does not already
set, so the rest of the code keeps reading environment variables and every route (daily, watch, run, demo, eval,
the scheduler) sees the same value.

An empty environment variable (or one with only spaces) counts as not set, everywhere in kimeru: the file value, then
the default, is used; to force a value, set it to something non-empty. The same holds for an empty value in the file and
an empty command-line value. Code that needs a setting reads it through `value()` (or `int_value()`), never with a bare
`os.environ.get`, so an empty variable can never become an empty address or an empty model name.

Values are read forgivingly: case and surrounding spaces are ignored, and on/off, true/false, yes/no mean 1/0. A value that is
still not allowed stops the command only for `backend` (it decides where the text goes); any other setting falls back to
its default and the reason is shown by `config show` and written to the daily log.

Secrets (API keys, tokens, passwords) never come from the file: a secret-looking key in it is ignored and reported.
"""
import json
import os
import re
from pathlib import Path

from . import fsutil

# key: (environment variable, default). Only settings that are safe to write to a file.
SETTINGS = {
    "backend": ("KIMERU_BACKEND", "stub"),
    "kev_url": ("KIMERU_KEV_URL", "http://127.0.0.1:8009/v1"),
    "clm_url": ("KIMERU_CLM_URL", "http://127.0.0.1:8700/v1"),
    "kev_url_remote_ok": ("KIMERU_KEV_URL_REMOTE_OK", "0"),    # 1 = Kev may be at an address that is not this PC (the event text then leaves the PC)
    "clm_url_remote_ok": ("KIMERU_CLM_URL_REMOTE_OK", "0"),
    "writer": ("KIMERU_WRITER", ""),
    "writer_model": ("KIMERU_WRITER_MODEL", ""),
    "codex_model": ("KIMERU_CODEX_MODEL", "gpt-6-luna"),
    "codex_effort": ("KIMERU_CODEX_EFFORT", "low"),
    "grok_model": ("KIMERU_GROK_MODEL", ""),
    "toast": ("KIMERU_TOAST", "1"),
    "self_chat_marker": ("KIMERU_SELF_MARKER", ""),
    "idle_sec": ("KIMERU_IDLE_SEC", ""),
    "az": ("KIMERU_AZ", ""),
    "brief_hour": ("KIMERU_BRIEF_HOUR", "8"),
    "ado_org": ("KIMERU_ADO_ORG", ""),
    "ado_project": ("KIMERU_ADO_PROJECT", ""),
    "subscription": ("KIMERU_SUBSCRIPTION", ""),
    "priority": ("KIMERU_PRIORITY", "monitor.alert,teams.chat,ado.workitem.created,meeting.item"),   # judged first -> last
    "cycle_budget_sec": ("KIMERU_CYCLE_BUDGET", ""),          # empty = the interval between cycles
    "plan_lean": ("KIMERU_PLAN_LEAN", "0"),                    # 1 = skip the deadline of a step judged unneeded (fewer calls; measure with Kev before turning on)
    "batch_questions": ("KIMERU_BATCH", "0"),                  # 1 = ask a graph's judge questions in one call
    "execute": ("KIMERU_EXECUTE", ""),                         # kinds carried out after approval (implemented: ado.comment)
    "execute_signature": ("KIMERU_EXECUTE_SIGNATURE", "1"),    # 0 = no signature line under an ADO comment
    "send_ready_post": ("KIMERU_SEND_READY", "1"),             # a reply is approved -> its text alone comes back to the self chat
    "open_chat_link": ("KIMERU_OPEN_CHAT_LINK", "0"),          # experimental: a link that opens the other person's chat
    "read_full": ("KIMERU_READ_FULL", "0"),                    # 1 = open a chat to read it in full, when it is needed
    "read_max_open": ("KIMERU_READ_MAX_OPEN", "3"),            # chats opened per cycle
    "read_budget_sec": ("KIMERU_READ_BUDGET", "60"),           # seconds spent reading per cycle
    "read_messages": ("KIMERU_READ_MESSAGES", "5"),            # last messages read from an opened chat
    "read_min_preview": ("KIMERU_READ_MIN_PREVIEW", "12"),     # a preview shorter than this (characters; 12 at least) cannot confirm which chat was opened: the chat is not read
    "read_idle_sec": ("KIMERU_READ_IDLE_SEC", "30"),           # seconds without keyboard / mouse use before a chat is opened to read it, and before each operation of the read (0 = do not check; idle_sec has no say)
    "read_click": ("KIMERU_READ_CLICK", "0"),                  # 1 = reading may also bring Teams to the front and click the chat (off: only the ways that touch no window)
    "read_max_defer": ("KIMERU_READ_MAX_DEFER", "12"),         # how many times a chat may be put off (the person is at the PC, Teams in use, the cycle's limit) before the preview decides (0 = never put off: tried once, then the preview decides)
    "preview_cut_len": ("KIMERU_PREVIEW_CUT_LEN", "0"),        # a preview at least this long counts as cut off even without an ellipsis (0 = the ellipsis only; set it from the T23 result)
    "full_text_keep_days": ("KIMERU_FULL_TEXT_KEEP_DAYS", "7"),   # days a full text is kept for an item that still waits for the PM
    "hold_stale_days": ("KIMERU_HOLD_STALE_DAYS", "0"),          # 1 or more: a hold without a day that is older than this many days is named "長く保留" in the morning brief (0 = not named)
    "brief_delta": ("KIMERU_BRIEF_DELTA", "0"),                  # 1 = the morning brief says what changed since the last one: replies and closed rows, 新 / 継続 N 日目, unchanged rows folded (brief.py)
    "brief_style": ("KIMERU_BRIEF_STYLE", "rank"),               # desk = a fixed order and no judge call or writer call for the brief (brief.py, daily.py)
    "inbox_done_keep_days": ("KIMERU_INBOX_DONE_KEEP_DAYS", "7"),   # days a judged original (inbox/done/*.json, the full text) is kept; 0 = never deleted
    "learn": ("KIMERU_LEARN", "0"),                            # 1 = the reply 違う N / 違う N k labels a decision (reviews.jsonl) and 詳細 N lists the question it was unsure of
    "post_reason": ("KIMERU_POST_REASON", "0"),                # 1 = the approval post and 詳細 N add one line: the question the model was unsure of (explain.py); 詳細 N also starts with the words a rule read (hit / rejected / sent to a person, at most 3 lines: explain.rule_evidence)
    "post_kind_mark": ("KIMERU_POST_KIND_MARK", "0"),          # 1 = a post that waits only for a writer's draft says 下書き: in its first line; brief, toast and digest count it apart (notify.py, brief.py, stats.py)
    "sent_watch": ("KIMERU_SENT_WATCH", "0"),                  # 1 = after an OK, the self-chat list shows whether the reply went out: as_is / edited / other, kept without the text (sent.py)
    "sent_watch_days": ("KIMERU_SENT_WATCH_DAYS", "3"),        # days after an OK in which that send is looked for (sent.py)
    "sent_nudge_hours": ("KIMERU_SENT_NUDGE_HOURS", "2"),      # hours after an OK, with no send seen and no `送った N`, before the one nudge of that reply (sent.py; only with sent_watch=1)
    "memory": ("KIMERU_MEMORY", "0"),                          # 1 = the post's arrow line ends with 前例, 詳細 N has 「これまでの経緯」, the brief counts the same person's items (memory.py; read only)
    "link_related": ("KIMERU_LINK_RELATED", "0"),              # 1 = the same case is shown beside its earlier item (↔), an alert that comes back within 24 hours is a 再発火 notice, a repeated ado.create gets dup_of (link.py; the judge never sees it)
    "record_event_full": ("KIMERU_RECORD_EVENT_FULL", "0"),   # 1 = decisions.jsonl keeps the whole event (2,000 characters of text), not a summary-length excerpt
    "judge_text_max": ("KIMERU_JUDGE_TEXT_MAX", "1200"),       # characters of `text` the judge sees
    "judge_context": ("KIMERU_JUDGE_CONTEXT", "0"),            # 1 = a judge on this PC (Kev or CLM at a loopback address) also sees the earlier messages of the same chat (the newest 2 decided within 72 hours, 120 characters each, from decisions.jsonl) and today's date (cli._judge_context); the text is cut shorter to make room
    "title_max": ("KIMERU_TITLE_MAX", "100"),                  # characters of an action's title
    "push": ("KIMERU_PUSH", ""),                                   # comma list of: teams_webhook, webhook, outlook
    "push_min_minutes": ("KIMERU_PUSH_MIN_MINUTES", "5"),
    "push_webhook_body": ("KIMERU_PUSH_WEBHOOK_BODY", ""),           # JSON template with {text}; empty = {"text": "{text}"}
    "interrupt": ("KIMERU_INTERRUPT", "all"),                      # sittings = the PC notification and the push wait for the next sitting (sit_hours) unless the item is urgent (attention.py); all = at once
    "sit_hours": ("KIMERU_SIT_HOURS", "9,13,17"),                  # the hours of the sittings, 0 to 23, comma list (used with interrupt=sittings)
    "urgent_words": ("KIMERU_URGENT_WORDS", "至急|本日中|今日中|大至急|緊急|ASAP"),   # a regular expression: an item whose text has one of these is urgent (interrupt=sittings)
    "promises": ("KIMERU_PROMISES", "0"),                          # 1 = the brief has a 約束 section, a new post of the same chat ends with the promise, 詳細 N lists the chat's promises, a status question from a chat with an overdue promise goes to the PM (promises.py; read only)
    "control_words": ("KIMERU_CONTROL_WORDS", "1"),                # 1 = my own self-chat line 停止 stops the daily cycle (nothing is taken from Teams, judged, posted, run or collected; the time-limit purges go on) and 再開 resumes it (control.py); 0 = the two words mean nothing
    "quiet_words": ("KIMERU_QUIET_WORDS", "0"),                    # 1 = my own self-chat line 休み <いつ> (for example 休み 15時) holds what is not urgent (no PC notification, no push) until that time; the urgent items still go at once, and nothing else changes (control.py, attention.py); 0 = the word means nothing. Read only with control_words=1
    "redraft_prev": ("KIMERU_REDRAFT_PREV", "0"),                  # 1 = 修正 N gives the writer the posted draft as data (the request only), and a first draft with a date or number the material lacks is asked once more (writer.py); 0 = as before
    "weekly_report": ("KIMERU_WEEKLY_REPORT", "0"),                # 1 = the Monday morning brief ends with six lines of the week's report (stats.weekly_lines: arrival to judgment and to OK, the observed sends, the label increments, the adopted learning before and after, the top three with a same-day reply or work record); read only, the writer never gets them (daily.py)
    "spot_check_per_day": ("KIMERU_SPOT_CHECK_PER_DAY", "0"),      # 1 to 5 = after the morning brief, that many of the day's automatic decisions are posted to the self chat as cards to confirm (spot.py); 正しい S<k> / 違う S<k> label them; 0 = no cards
}
# command-line flag -> setting, for the "source: arg" record
FLAGS = {"--backend": "backend", "--ado-org": "ado_org", "--ado-project": "ado_project",
         "--subscription": "subscription", "--brief-hour": "brief_hour"}
SECRET_ENV = ("TYPESAFE_API_KEY", "KEV_API_KEY", "CLM_API_KEY", "KIMERU_PUSH_TEAMS_URL", "KIMERU_PUSH_WEBHOOK_URL", "KIMERU_PUSH_WEBHOOK_KEY")   # shown only as set / not set
SECRET_KEY = re.compile(r"key|token|secret|passw|credential|auth|hook.*url|push.*url|teams_url", re.I)
# settings whose value may be written to the daily log (the others are recorded by name and source only)
LOGGABLE = ("backend", "writer", "writer_model", "codex_model", "codex_effort", "grok_model", "toast", "brief_hour",
            "push", "push_min_minutes", "execute", "read_full", "record_event_full", "judge_text_max", "priority", "cycle_budget_sec", "plan_lean", "batch_questions", "learn", "post_reason", "post_kind_mark", "hold_stale_days", "sent_watch", "sent_watch_days", "sent_nudge_hours", "brief_delta", "brief_style", "memory", "promises", "interrupt", "sit_hours", "link_related", "control_words", "quiet_words", "judge_context", "redraft_prev", "weekly_report", "spot_check_per_day")

# settings with a fixed set of values: checked by `config set` and again when read. A value is first read forgivingly
# (case, spaces, on/off, true/false, yes/no); if it is still not allowed, `backend` stops the command (it decides where the
# text goes) and every other setting falls back to its default with a warning.
_ONOFF = ("0", "1")
CHOICES = {
    "backend": ("stub", "jev", "kev", "clm"),
    "writer": ("", "copilot", "codex", "grok", "claude", "cmd", "m365", "m365-auto"),
    "toast": ("0", "1", "detail"),
    "plan_lean": _ONOFF, "batch_questions": _ONOFF, "learn": _ONOFF, "post_reason": _ONOFF, "post_kind_mark": _ONOFF, "sent_watch": _ONOFF, "read_full": _ONOFF, "read_click": _ONOFF, "record_event_full": _ONOFF, "open_chat_link": _ONOFF,
    "send_ready_post": _ONOFF, "execute_signature": _ONOFF, "kev_url_remote_ok": _ONOFF, "clm_url_remote_ok": _ONOFF,
    "brief_delta": _ONOFF, "brief_style": ("rank", "desk"), "memory": _ONOFF, "link_related": _ONOFF, "promises": _ONOFF, "control_words": _ONOFF, "quiet_words": _ONOFF, "judge_context": _ONOFF, "redraft_prev": _ONOFF, "weekly_report": _ONOFF,
    "interrupt": ("all", "sittings"), "spot_check_per_day": ("0", "1", "2", "3", "4", "5"),
}
_YES, _NO = ("1", "on", "true", "yes"), ("0", "off", "false", "no")
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def normalize(key, raw):
    """(value, ok): `raw` read the forgiving way. ok is False when the setting has a fixed set of values and this is not one."""
    v = str(raw).strip()
    if key == "brief_hour":
        return (str(int(v)), True) if v.isdigit() and 0 <= int(v) <= 23 else (v, False)
    allowed = CHOICES.get(key)
    if allowed is None:
        return v, True
    v = v.lower()
    if allowed == _ONOFF or key == "toast":
        v = "1" if v in _YES else "0" if v in _NO else v
    return v, v in allowed


def sit_hours_list(raw):
    """The hours of a comma list (0 to 23), sorted and without repeats; None when `raw` is not one (an empty list is not one).
    Empty entries are skipped: "9,13," is 9 and 13."""
    try:
        hours = sorted({int(x) for x in (p.strip() for p in str(raw).split(",")) if x})
    except ValueError:
        return None
    return hours if hours and all(0 <= h <= 23 for h in hours) else None


PUSH_ROUTES = ("teams_webhook", "webhook", "outlook")   # the only values of `push` (a URL written here by mistake is refused, never echoed)


def push_valid(raw):
    """True when `raw` is a comma list of route names only (empty is allowed: no route)."""
    return all(x.strip() in PUSH_ROUTES for x in str(raw).split(",") if x.strip())


def push_read(raw):
    """(routes, ignored): the route names in a comma list of `push`, read forgivingly (case and surrounding spaces are ignored;
    a route named twice counts once), and how many entries are not route names. The entries that are not names are never repeated
    anywhere: one may be a URL."""
    routes, ignored = [], 0
    for x in str(raw).split(","):
        n = x.strip().lower()
        if not n:
            continue
        if n in PUSH_ROUTES:
            if n not in routes:
                routes.append(n)
        else:
            ignored += 1
    return routes, ignored


NEVER_SHOWN = ("push_webhook_body",)   # `config show` says only whether it is set (a body template may hold a key)
ARGS = {}        # key -> value given on the command line (highest precedence)
_FILLED = {}     # environment variables apply() filled from the file (undone at the next apply, so a re-read is clean)
SOURCES = {}     # key -> "arg" | "env" | "file" | "default" (after apply())
WARNINGS = []


class ConfigError(Exception):
    pass


def state_dir():
    return Path(os.environ.get("KIMERU_STATE_DIR")
                or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "kimeru"))


def path():
    return state_dir() / "config.json"


def read_raw():
    """The config file as it is (every key, even unknown or secret-looking ones). Missing = {}; broken raises ConfigError."""
    p = path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{p}: not valid JSON ({e.msg}, line {e.lineno} column {e.colno})") from e
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{p}: cannot be read ({type(e).__name__})") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: must be a JSON object of setting: value")
    return raw


def read_file():
    """(values, warnings) from the config file. A missing file is empty; a broken one raises ConfigError."""
    raw = read_raw()
    values, warnings = {}, []
    for k, v in raw.items():
        if SECRET_KEY.search(str(k)):
            warnings.append(f"config file: '{k}' looks like a secret and is ignored (secrets belong in environment variables)")
        elif k not in SETTINGS:
            warnings.append(f"config file: unknown setting '{k}' is ignored")
        elif isinstance(v, (dict, list)):
            warnings.append(f"config file: '{k}' must be a single value and is ignored")
        else:
            values[k] = "" if v is None else str(v)
    return values, warnings


def apply(argv=None):
    """Load the file once and fill the environment for settings that nothing else sets. Returns the warnings."""
    for env, filled in list(_FILLED.items()):
        if os.environ.get(env) == filled:
            del os.environ[env]
    _FILLED.clear()
    SOURCES.clear()
    WARNINGS.clear()
    ARGS.clear()
    toks = list(argv or ())
    for i, tok in enumerate(toks):   # `--flag value` and `--flag=value`
        flag, eq, val = tok.partition("=")
        if flag in FLAGS:
            if not eq:
                val = toks[i + 1] if i + 1 < len(toks) else None
            if val is not None:
                ARGS[FLAGS[flag]] = val
    values, warnings = read_file()
    WARNINGS.extend(warnings)
    for key, (env, default) in SETTINGS.items():
        if key in ARGS and _present(ARGS[key]):
            SOURCES[key], raw = "arg", ARGS[key]
        elif _present(os.environ.get(env)):
            SOURCES[key], raw = "env", os.environ[env]
        elif _present(values.get(key)):
            SOURCES[key], raw = "file", values[key]
        else:
            SOURCES[key] = "default"
            ARGS.pop(key, None)
            continue
        if SOURCES[key] != "arg":
            ARGS.pop(key, None)
        if key in CHOICES or key == "brief_hour":
            v, ok = normalize(key, raw)
            if not ok and key != "backend":   # only backend stops the command (see problems())
                WARNINGS.append(f"{key}={str(raw).strip()!r} ({_where(key)}) is not allowed: the default {default!r} is used")
                v = default
        elif key == "sit_hours":   # not hours: the sittings do not apply (every item is announced at once, as with interrupt=all)
            if sit_hours_list(raw) is None:
                WARNINGS.append(f"sit_hours={str(raw).strip()!r} ({_where(key)}) is not a list of hours from 0 to 23: "
                                "interrupt=sittings does not apply and every item is announced at once")
            v = raw
        elif key == "urgent_words":   # not a regular expression: the default words are used
            try:
                re.compile(raw)
                v = raw
            except re.error:
                WARNINGS.append(f"urgent_words ({_where(key)}) is not a valid regular expression: the default words are used")
                v = default
        elif key == "push":   # the names that are not routes are ignored, the others are used; never print a value: it may be a URL
            routes, ignored = push_read(raw)
            if ignored:
                WARNINGS.append(f"push ({_where(key)}) has {ignored} entr{'y' if ignored == 1 else 'ies'} that "
                                f"{'is' if ignored == 1 else 'are'} not a route name ({', '.join(PUSH_ROUTES)}): "
                                f"{'it is' if ignored == 1 else 'they are'} ignored" + (", the others are used" if routes else ", no route is used"))
            v = ",".join(routes)
        else:
            v = raw
        if SOURCES[key] == "arg":
            ARGS[key] = v
        elif SOURCES[key] == "env":
            os.environ[env] = v
        else:
            os.environ[env] = _FILLED[env] = v
    return list(WARNINGS)


def _present(v):
    return v is not None and str(v).strip() != ""


def value(key):
    """The setting in effect: argument, then environment, then default (the file was copied into the environment by apply()).
    An empty value counts as not set."""
    env, default = SETTINGS[key]
    for v in (ARGS.get(key), os.environ.get(env)):
        if _present(v):
            if key in CHOICES or key == "brief_hour":
                n, ok = normalize(key, v)
                return n if ok or key == "backend" else default
            if key == "push":
                return ",".join(push_read(v)[0])
            return str(v)
    return default


def int_value(key):
    """`value()` as a whole number; a value that is not one gives the default."""
    try:
        return int(value(key))
    except ValueError:
        return int(SETTINGS[key][1])


def url_problem(key, remote_ok_key):
    """Why the judge address `key` must not be used, or None. An address that is not this PC needs an explicit setting,
    because the text of the events would be sent there."""
    from urllib.parse import urlparse
    url = value(key)
    host = urlparse(url).hostname or ""
    if host in LOOPBACK or value(remote_ok_key) == "1":
        return None
    return (f"{key} points to {host or url!r}, not this PC: the event text would leave it. "
            f"Use an address on this PC, or set {SETTINGS[remote_ok_key][0]}=1 ({remote_ok_key}) on purpose")


def not_allowed():
    """[(key, message)] for the settings (other than backend) whose value in effect is not allowed and so was replaced by the
    default. apply() reports these once at start-up; code that runs without apply() (a test, a library user) can ask here."""
    found = []
    for key, (env, default) in SETTINGS.items():
        if key == "backend" or not (key in CHOICES or key == "brief_hour"):
            continue
        raw = ARGS.get(key) if _present(ARGS.get(key)) else os.environ.get(env)
        if _present(raw) and not normalize(key, raw)[1]:
            found.append((key, f"{key}={str(raw).strip()!r} is not allowed: the default {default!r} is used"))
    return found


def problems():
    """Values in effect that stop the command: a backend that does not exist (it decides where the text goes) and an
    address of a local judge that is not this PC without an explicit setting. Empty when all is well."""
    found = []
    v = value("backend")
    if v not in CHOICES["backend"]:
        found.append(f"backend={v!r} ({_where('backend')}) is not one of: {', '.join(CHOICES['backend'])}")
    elif v in ("kev", "clm"):
        p = url_problem(f"{v}_url", f"{v}_url_remote_ok")
        if p:
            found.append(p)
    return found


def _where(key):
    return {"arg": "command line", "env": f"environment variable {SETTINGS[key][0]}", "file": "config file"}.get(SOURCES.get(key), "default")


def rows():
    """[(key, value, source)] for `config show`."""
    if not SOURCES:
        apply()
    return [(k, ("(set)" if value(k) else "") if k in NEVER_SHOWN else value(k), SOURCES.get(k, "default")) for k in SETTINGS]


# `config show --share` (pasted into a public issue) prints a value only for these settings, and for a setting that still
# has its default (a constant of kimeru). Any other value that was set (a folder, an organization, a subscription, an
# address, a model or deployment name) is only reported as set.
SHARE_VALUES = ("backend", "writer", "toast", "brief_hour", "priority", "cycle_budget_sec", "plan_lean", "batch_questions", "learn", "post_reason", "post_kind_mark",
                "sent_watch", "sent_watch_days", "sent_nudge_hours", "execute", "execute_signature", "send_ready_post", "open_chat_link", "read_full", "record_event_full",
                "read_max_open", "read_budget_sec", "read_messages", "read_min_preview", "read_idle_sec", "read_click", "read_max_defer", "preview_cut_len", "full_text_keep_days", "inbox_done_keep_days", "judge_text_max", "title_max", "push",
                "push_min_minutes", "idle_sec", "codex_effort", "hold_stale_days", "brief_delta", "brief_style", "memory", "promises", "interrupt", "sit_hours", "link_related", "control_words", "quiet_words", "judge_context", "redraft_prev", "weekly_report", "spot_check_per_day")


def share_rows():
    """[(key, value, source)] for `config show --share`: no path, address, organization, subscription or name."""
    if not SOURCES:
        apply()
    out = []
    for k in SETTINGS:
        src, v = SOURCES.get(k, "default"), value(k)
        if k in NEVER_SHOWN or (src != "default" and k not in SHARE_VALUES):
            v = "(set)" if v else ""
        out.append((k, v, src))
    return out


def report():
    """What one daily cycle logs: where each set setting came from, values only for the harmless ones."""
    eff = {}
    for k in SETTINGS:
        src = SOURCES.get(k, "default")
        if src == "default" and not value(k):
            continue
        eff[k] = {"source": src, **({"value": value(k)} if k in LOGGABLE else {})}
    rep = {"effective": eff}
    if not value("writer"):
        rep["writer_note"] = "writer が未設定: 文面は定型文で動く"
    if WARNINGS:
        rep["warnings"] = list(WARNINGS)
    return rep


def secrets_status():
    return {n: bool(os.environ.get(n)) for n in SECRET_ENV}


def _write(values):
    fsutil.write_atomic(path(), json.dumps(values, ensure_ascii=False, indent=2) + "\n")


def set_value(key, val):
    if SECRET_KEY.search(key):
        raise ConfigError(f"'{key}' looks like a secret: set it as an environment variable, not in the config file")
    if key not in SETTINGS:
        raise ConfigError(f"unknown setting '{key}' (one of: {', '.join(SETTINGS)})")
    if key in CHOICES or key == "brief_hour":
        val, ok = normalize(key, val)
        if not ok:
            raise ConfigError(f"brief_hour must be a whole number from 0 to 23" if key == "brief_hour" else
                              f"{key} must be one of: {', '.join(a for a in CHOICES[key] if a)}")
    if key == "sit_hours" and sit_hours_list(val) is None:
        raise ConfigError("sit_hours must be a list of hours from 0 to 23, separated by commas (for example 9,13,17)")
    if key == "urgent_words":
        try:
            re.compile(val)
        except re.error:
            raise ConfigError("urgent_words must be a regular expression (for example 至急|ASAP)") from None
    if key == "push" and not push_valid(val):   # the message never repeats the value: a URL must not reach the screen or a record
        raise ConfigError(f"push must be route names only, separated by commas: {', '.join(PUSH_ROUTES)} "
                          "(the URL goes in the environment variable, see docs/push-notification.md)")
    if key == "push_webhook_body" and val.strip():
        try:
            json.loads(val.replace("{text}", "x").replace("{key}", "x"))
        except ValueError as e:
            raise ConfigError(f"push_webhook_body must be JSON (with {{text}} where the message goes): {e}") from None
    values = read_raw()   # the raw file: settings this version does not know are kept
    values[key] = val
    _write(values)


def unset_value(key):
    values = read_raw()   # a secret-looking or unknown key that is in the file can be removed too
    if key not in values:
        return False
    del values[key]
    _write(values)
    return True


def save_effective(extra=None):
    """`schedule install`: keep the settings in effect now in the config file, so the scheduled runs read them.
    A value given now (argument or environment, `extra` first) replaces the file's, even when it equals the default.
    A setting that only has its default, or an empty value, leaves the file as it is."""
    values = read_raw()
    if not SOURCES:
        apply()
    for key in SETTINGS:
        v = (extra or {}).get(key) or (value(key) if SOURCES.get(key) in ("env", "arg") else "")
        if v:
            values[key] = str(v)
    _write(values)
    return sorted(k for k in values if k in SETTINGS)
