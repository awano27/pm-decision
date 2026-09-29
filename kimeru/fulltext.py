"""Reading a chat in full, only when it is needed (off by default: setting `read_full`).

The Teams chat list gives one line of preview per chat. A long request is cut off there, and the messages before it are not
seen at all. When enabled, kimeru opens a chat and reads its last messages (read only: it never touches the input box), but
only when the preview is cut off, or when the decision made from the preview would end at the PM (a person's confirmation or
a notice). Opening a chat marks it as read in Teams, so what was opened and needed no action is listed in the morning brief.

Limits: at most `read_max_open` chats and `read_budget_sec` seconds per cycle. When a chat cannot be opened or read, or the
text read does not fit the preview (another chat may have been read), nothing is used: the decision stays the one made from
the preview, and the approval post and the record say so.

The full text is kept only while the item waits for the PM (out/full_text.json); after an approval or a rejection it is deleted,
and what stays in the records is a summary of the same length as before.
"""
import json
import time
from pathlib import Path

from . import config, fsutil

ELLIPSIS = ("…", "...", "‥")
PERSIST_TEXT = 200          # what the permanent records keep of a full text (the length of a summary)
THREAD_ITEM = 300           # characters kept per earlier message


def enabled():
    return config.value("read_full") == "1"


def truncated(text):
    """The preview ends with an ellipsis: the message goes on."""
    return str(text or "").rstrip().endswith(ELLIPSIS)


def _norm(s):
    return " ".join(str(s or "").split())


def _int(key, default):
    try:
        return max(0, int(config.value(key)))
    except ValueError:
        return default


class BudgetExhausted(Exception):
    """The cycle's opening budget is used up: the event waits for the next cycle (it is not decided from the preview)."""


class Reader:
    """Opens chats through the bridge, within a per-cycle budget. Call it with an event; it returns a dict:
    {"ok": True, "messages": [...], "returned": bool} or {"ok": False, "why": "..."} (a reason that holds no message text)."""

    def __init__(self, bridge, max_open=None, seconds=None, count=None, clock=time.monotonic):
        self.bridge = bridge
        self.max_open = _int("read_max_open", 3) if max_open is None else max_open
        self.seconds = _int("read_budget_sec", 60) if seconds is None else seconds
        self.count = _int("read_messages", 5) if count is None else count
        self.clock = clock
        self.opened = 0
        self.spent = 0.0

    def __call__(self, ev):
        if not ev.get("chat_id"):
            return {"ok": False, "why": "チャットの識別子がありません"}
        if self.opened >= self.max_open:
            raise BudgetExhausted(f"1 サイクルで開く件数の上限（{self.max_open} 件）に達しました")
        if self.spent >= self.seconds:
            raise BudgetExhausted(f"1 サイクルで読む時間の上限（{self.seconds} 秒）に達しました")
        t = self.clock()
        self.opened += 1
        try:
            res = self.bridge.readchat(ev["chat_id"], self.count)
        except Exception as e:   # a fixed message from the script, or the type of the failure: never chat text
            return {"ok": False, "why": _safe(str(e)) or f"読み取りに失敗しました（{type(e).__name__}）"}
        finally:
            self.spent += self.clock() - t
        msgs = [m for m in (res.get("messages") or []) if _norm(m.get("text"))]
        if not msgs:
            return {"ok": False, "why": "メッセージを読み取れませんでした"}
        if not _fits(ev.get("text"), msgs[-1]["text"]):
            return {"ok": False, "why": "読んだ内容がプレビューと合いません（別のチャットを読んだ可能性があるので、使いません）"}
        return {"ok": True, "messages": msgs, "returned": bool(res.get("returned", False))}


SAFE_MESSAGES = (
    "the chat is not in the list on screen; nothing was opened",
    "the chat has no title to check the screen against; nothing was opened",
    "could not confirm that the chat is open (the title and the selection did not agree); nothing was read",
    "Teams window not found",
    "readchat needs the id of another chat (not the self chat)",
    "another kimeru Teams operation is running; Teams was not touched",
)


def _safe(msg):
    """The script's own fixed messages are shown as they are (and only that part of the text); anything else is not shown."""
    return next((m for m in SAFE_MESSAGES if msg.startswith(m)), "") or (
        "キーボード・マウスを使っている間は、Teams を操作しません" if msg.startswith("the keyboard / mouse has been in use") else "")


def _fits(preview, full):
    """The preview (minus its ellipsis and a leading "name:") is the start of the text that was read."""
    core = _norm(str(preview or "").rstrip(" …."))
    core = core.split(": ", 1)[1] if ": " in core[:40] and not _norm(full).startswith(core[:20]) else core
    head = core[:40]
    return bool(head) and head in _norm(full)


def deepen(reader, ev):
    """(event to judge, info). info is None when nothing was attempted."""
    r = reader(ev)
    if not r.get("ok"):
        return ev, {"state": "preview_only", "why": r.get("why", "")}
    msgs = r["messages"]
    thread = [_norm(m["text"])[:THREAD_ITEM] for m in msgs[:-1]]
    ev2 = {**ev, "text": _norm(msgs[-1]["text"]), "full": True}
    if thread:
        ev2["thread"] = thread
    info = {"state": "full", "returned": r.get("returned", False)}
    if not info["returned"]:
        info["note"] = "元のチャットへ戻せませんでした"
    return ev2, info


def scrub(obj, full, excerpt):
    """A copy of `obj` in which the full text is replaced by its excerpt (for the records that stay after the decision)."""
    if not full or full == excerpt:
        return obj
    if isinstance(obj, str):
        return obj.replace(full, excerpt)
    if isinstance(obj, list):
        return [scrub(x, full, excerpt) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub(v, full, excerpt) for k, v in obj.items()}
    return obj


def persistable(ev):
    """A full-text event as the permanent records may keep it: the text is a summary-length excerpt, the thread is dropped."""
    if not ev.get("full"):
        return ev
    out = {k: v for k, v in ev.items() if k not in ("thread", "full")}
    t = _norm(out.get("text"))
    out["text"] = t if len(t) <= PERSIST_TEXT else t[:PERSIST_TEXT - 1] + "…"
    return out


# ---- the full text of items that wait for the PM ----

def _path(out):
    return Path(out) / "full_text.json"


def save(out, key, ev):
    data = fsutil.read_json(_path(out), {})
    data[key] = {"text": ev.get("text", ""), "thread": ev.get("thread", []), "title": ev.get("author", "")}
    fsutil.write_atomic(_path(out), json.dumps(data, ensure_ascii=False))


def load(out, key):
    return fsutil.read_json(_path(out), {}).get(key)


def drop(out, key):
    data = fsutil.read_json(_path(out), {})
    if key in data:
        del data[key]
        fsutil.write_atomic(_path(out), json.dumps(data, ensure_ascii=False))
        return True
    return False
