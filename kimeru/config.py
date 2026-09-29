"""One place for the settings that are not secret, and a record of where each one came from.

Order of precedence: command-line argument > environment variable > config file > default.
The config file is `config.json` in the state folder (KIMERU_STATE_DIR, else %LOCALAPPDATA%\\kimeru). It is read once
at start-up: `apply()` copies file values into the environment for the settings the environment does not already
set, so the rest of the code keeps reading environment variables and every route (daily, watch, run, demo, eval,
the scheduler) sees the same value.

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
    "plan_lean": ("KIMERU_PLAN_LEAN", "1"),                    # 0 = ask every step's deadline, as before
    "batch_questions": ("KIMERU_BATCH", "0"),                  # 1 = ask a graph's judge questions in one call
    "execute": ("KIMERU_EXECUTE", ""),                         # kinds carried out after approval (implemented: ado.comment)
    "execute_signature": ("KIMERU_EXECUTE_SIGNATURE", "1"),    # 0 = no signature line under an ADO comment
    "send_ready_post": ("KIMERU_SEND_READY", "1"),             # a reply is approved -> its text alone comes back to the self chat
    "open_chat_link": ("KIMERU_OPEN_CHAT_LINK", "0"),          # experimental: a link that opens the other person's chat
    "read_full": ("KIMERU_READ_FULL", "0"),                    # 1 = open a chat to read it in full, when it is needed
    "read_max_open": ("KIMERU_READ_MAX_OPEN", "3"),            # chats opened per cycle
    "read_budget_sec": ("KIMERU_READ_BUDGET", "60"),           # seconds spent reading per cycle
    "read_messages": ("KIMERU_READ_MESSAGES", "5"),            # last messages read from an opened chat
    "judge_text_max": ("KIMERU_JUDGE_TEXT_MAX", "1200"),       # characters of `text` the judge sees
    "title_max": ("KIMERU_TITLE_MAX", "100"),                  # characters of an action's title
    "push": ("KIMERU_PUSH", ""),                                   # comma list of: teams_webhook, webhook, outlook
    "push_min_minutes": ("KIMERU_PUSH_MIN_MINUTES", "5"),
    "push_webhook_body": ("KIMERU_PUSH_WEBHOOK_BODY", ""),           # JSON template with {text}; empty = {"text": "{text}"}
    "push_mail_to": ("KIMERU_PUSH_MAIL_TO", ""),
}
# command-line flag -> setting, for the "source: arg" record
FLAGS = {"--backend": "backend", "--ado-org": "ado_org", "--ado-project": "ado_project",
         "--subscription": "subscription", "--brief-hour": "brief_hour"}
SECRET_ENV = ("TYPESAFE_API_KEY", "KEV_API_KEY", "CLM_API_KEY", "KIMERU_PUSH_TEAMS_URL", "KIMERU_PUSH_WEBHOOK_URL")   # shown only as set / not set
SECRET_KEY = re.compile(r"key|token|secret|passw|credential|auth|hook.*url|push.*url|teams_url", re.I)
# settings whose value may be written to the daily log (the others are recorded by name and source only)
LOGGABLE = ("backend", "writer", "writer_model", "codex_model", "codex_effort", "grok_model", "toast", "brief_hour",
            "push", "push_min_minutes", "execute", "read_full", "judge_text_max", "priority", "cycle_budget_sec", "plan_lean", "batch_questions")

SOURCES = {}     # key -> "arg" | "env" | "file" | "default" (after apply())
WARNINGS = []


class ConfigError(Exception):
    pass


def state_dir():
    return Path(os.environ.get("KIMERU_STATE_DIR")
                or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "kimeru"))


def path():
    return state_dir() / "config.json"


def read_file():
    """(values, warnings) from the config file. A missing file is empty; a broken one raises ConfigError."""
    p = path()
    if not p.exists():
        return {}, []
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{p}: not valid JSON ({e.msg}, line {e.lineno} column {e.colno})") from e
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{p}: cannot be read ({type(e).__name__})") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: must be a JSON object of setting: value")
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
    SOURCES.clear()
    WARNINGS.clear()
    values, warnings = read_file()
    WARNINGS.extend(warnings)
    for key, (env, _default) in SETTINGS.items():
        if os.environ.get(env) not in (None, ""):
            SOURCES[key] = "env"
        elif key in values and values[key] != "":
            os.environ[env] = values[key]
            SOURCES[key] = "file"
        else:
            SOURCES[key] = "default"
    for tok in argv or ():
        flag = tok.split("=", 1)[0]
        if flag in FLAGS:
            SOURCES[FLAGS[flag]] = "arg"
    return list(WARNINGS)


def value(key):
    env, default = SETTINGS[key]
    return os.environ.get(env) or default


def rows():
    """[(key, value, source)] for `config show`."""
    if not SOURCES:
        apply()
    return [(k, value(k), SOURCES.get(k, "default")) for k in SETTINGS]


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
    if key == "brief_hour" and not (val.isdigit() and 0 <= int(val) <= 23):
        raise ConfigError("brief_hour must be a whole number from 0 to 23")
    values, _ = read_file()
    values[key] = val
    _write(values)


def unset_value(key):
    values, _ = read_file()
    if key not in values:
        return False
    del values[key]
    _write(values)
    return True


def save_effective(extra=None):
    """`schedule install`: keep the settings in effect now in the config file, so the scheduled runs read them.
    Settings that only have their default, and empty ones, are not written."""
    values, _ = read_file()
    if not SOURCES:
        apply()
    for key in SETTINGS:
        v = (extra or {}).get(key) or (value(key) if SOURCES.get(key) in ("env", "arg") else "")
        if v and v != SETTINGS[key][1]:
            values[key] = str(v)
    _write(values)
    return sorted(values)
