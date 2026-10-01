"""Reading a chat in full, only when it is needed (off by default: setting `read_full`).

The Teams chat list gives one line of preview per chat. A long request is cut off there, and the messages before it are not
seen at all. When enabled, kimeru opens a chat and reads its last messages (read only: it never touches the input box), but
only when the preview is cut off, or when the decision made from the preview would end at the PM (a person's confirmation or
a notice). Opening a chat marks it as read in Teams, so what was opened and needed no action is listed in the morning brief.

Limits: at most `read_max_open` chats and `read_budget_sec` seconds per cycle (0 = do not read: the preview decides). A chat
whose preview is shorter than `read_min_preview` characters is not opened: the text read could not be matched to it. When a chat
cannot be opened or read, or the text read does not fit the preview (another chat may have been read), nothing is used: the
decision stays the one made from the preview, and the approval post and the record say so. Whether the chat that was open
before could be put back is recorded either way (`returned`, `restore`); when it could not, the self chat is opened instead and
the PC is notified.

The full text is kept only while the item waits for the PM (out/full_text.json, at most `full_text_keep_days` days); after an
approval or a rejection it is deleted, and what stays in the records is a summary of the same length as before. It is never
written to a .jsonl file: the paste-in request for Microsoft 365 Copilot is kept in the record with the excerpt, and the
version with the whole text waits in full_text.json beside it.
"""
import contextlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

ELLIPSIS = ("…", "...", "‥")
PERSIST_TEXT = 120          # what the permanent records keep of a full text (the length of a summary)
THREAD_ITEM = 300           # characters kept per earlier message
BUSY = "the keyboard / mouse has been in use"     # the script's message when the person was using the PC: nothing was touched
IN_USE = "Teams is in use"                        # ... or when Teams is in front / an input box has the focus: nothing was opened
MIN_MATCH = 12                                    # the shortest preview (characters) that may identify a chat, on every route


def enabled():
    return config.value("read_full") == "1"


def _int(key, default):
    try:
        return max(0, int(config.value(key)))
    except ValueError:
        return default


def max_defer():
    """How many times an event may be put off before the preview decides. 0 = it is never put off: the chat is tried once, and when
    it cannot be read now (the person is at the PC, Teams is in use, the cycle's limit) the preview decides."""
    return _int("read_max_defer", 12)


def gave_up(n):
    """The read_full record of an event that was put off too often: decided from the preview."""
    if n <= 0:
        return {"state": "preview_only", "why": "延期しない設定（read_max_defer=0）で、今は読めなかったため、プレビューで判断しました"}
    return {"state": "preview_only", "why": f"延期が上限（{n} 回）に達したため、プレビューで判断しました"}


def truncated(text):
    """The preview ends with an ellipsis (the message goes on), or is at least `preview_cut_len` characters long
    (a length measured with the T23 check, for Teams layouts that cut without a mark; 0 = off)."""
    t = str(text or "").rstrip()
    if t.endswith(ELLIPSIS):
        return True
    n = _int("preview_cut_len", 0)
    return n > 0 and len(t) >= n


def _norm(s):
    return " ".join(str(s or "").split())


def _core(preview):
    return _norm(str(preview or "").rstrip(" …."))


_NAME_RE = re.compile(r"^[^:：]{1,30}[:：]\s*(.*)$", re.S)


def _body(preview):
    """The part of a preview that says something about the chat: no ellipsis and no leading "name: " (the same rule as the script's
    Test-PreviewMatch: a name of 1 to 30 characters, a half- or full-width colon, spaces or none)."""
    core = _core(preview)
    m = _NAME_RE.match(core)
    return m.group(1) if m else core


class BudgetExhausted(Exception):
    """The cycle's opening budget is used up: the event waits for the next cycle (it is not decided from the preview)."""


class Deferred(BudgetExhausted):
    """The person was using the keyboard / mouse: nothing was touched, and the event waits for the next cycle."""


def _accepts(fn, name):
    import inspect
    try:
        ps = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in ps or any(p.kind is p.VAR_KEYWORD for p in ps.values())


RESTORED = ("restored", "unchanged", "none")   # the chat that was open before is open again (or was never left)


class Reader:
    """Opens chats through the bridge, within a per-cycle budget. Call it with an event; it returns a dict:
    {"ok": True, "messages": [...], "returned": bool, "restore": str} or {"ok": False, "why": "..."} (a reason that holds no
    message text). `restore` (when a chat was opened): restored | unchanged | self | failed. `alerts` collects what the person
    must be told on this PC (a chat that could not be put back)."""

    def __init__(self, bridge, max_open=None, seconds=None, count=None, clock=time.monotonic, min_preview=None):
        self.bridge = bridge
        self.max_open = _int("read_max_open", 3) if max_open is None else max_open
        self.seconds = _int("read_budget_sec", 60) if seconds is None else seconds
        self.count = _int("read_messages", 5) if count is None else count
        self.min_preview = max(MIN_MATCH, _int("read_min_preview", MIN_MATCH) if min_preview is None else min_preview)
        self.clock = clock
        self.opened = 0
        self.spent = 0.0
        self.alerts = []

    def __call__(self, ev):
        if not ev.get("chat_id"):
            return {"ok": False, "why": "チャットの識別子がありません"}
        if self.max_open <= 0 or self.seconds <= 0:   # switched off by the setting: the preview decides, and nothing waits
            return {"ok": False, "why": "全文を読む件数か時間の設定が 0 のため、読みません"}
        body = _body(ev.get("text"))
        if len(body) < max(1, self.min_preview):
            return {"ok": False, "why": f"プレビューが短く（{len(body)} 字）、開いたチャットが正しいか確かめられないため、読みません"}
        if self.opened >= self.max_open:
            raise BudgetExhausted(f"1 サイクルで開く件数の上限（{self.max_open} 件）に達しました")
        if self.spent >= self.seconds:
            raise BudgetExhausted(f"1 サイクルで読む時間の上限（{self.seconds} 秒）に達しました")
        t = self.clock()
        self.opened += 1
        extra = {"preview": body[:40]} if _accepts(self.bridge.readchat, "preview") else {}
        data, err = {}, None
        try:
            data = self.bridge.readchat(ev["chat_id"], self.count, **extra) or {}
        except Exception as e:   # a fixed message from the script, or the type of the failure: never chat text
            err, data = e, getattr(e, "data", None) or {}
        finally:
            self.spent += self.clock() - t
        restore = self._restore_of(data)
        if restore == "failed" or (restore == "self" and data.get("hadOriginal") is not False):
            self.alerts.append(restore)     # there was a chat to go back to, and it could not be
        if err is not None:
            msg = str(err)
            if msg.startswith(BUSY):
                raise Deferred("キーボード・マウスを使っている間は、Teams を操作しません。次のサイクルに回します")
            if msg.startswith(IN_USE):
                raise Deferred("Teams が前面にあるか、入力欄にフォーカスがあるため、開きません。次のサイクルに回します")
            return self._with_restore({"ok": False, "why": _safe(msg) or f"読み取りに失敗しました（{type(err).__name__}）"}, data, restore)
        msgs = [m for m in (data.get("messages") or []) if _norm(m.get("text"))]
        if not msgs:
            return self._with_restore({"ok": False, "why": "メッセージを読み取れませんでした"}, data, restore)
        if not _fits(ev.get("text"), msgs[-1]["text"], self.min_preview):
            return self._with_restore({"ok": False, "why": "読んだ内容がプレビューと合いません（別のチャットを読んだ可能性があるので、使いません）"}, data, restore)
        return self._with_restore({"ok": True, "messages": msgs}, data, restore)

    @staticmethod
    def _restore_of(data):
        r = str(data.get("restore") or "")
        if r in ("restored", "unchanged", "none", "self", "failed"):
            return r
        if "returned" in data:   # a bridge that reports only whether it went back
            return "restored" if data.get("returned") else "failed"
        return ""

    @staticmethod
    def _with_restore(out, data, restore):
        if restore:
            out["restore"] = restore
            out["returned"] = restore in RESTORED
            if data.get("hadOriginal") is False:
                out["had_original"] = False
        elif "returned" in data:
            out["returned"] = bool(data["returned"])
        return out


SAFE_MESSAGES = (
    "the chat is not in the list on screen; nothing was opened",
    "the chat has no title to check the screen against; nothing was opened",
    "the preview is too short to tell the chat apart on this screen; nothing was opened",
    "the chat that is open has the title of the chat to read, so they cannot be told apart on this screen; nothing was opened, nothing was read",
    "could not confirm that the chat is open (the title and the selection did not agree); nothing was read",
    "the chat that opened does not match the preview; nothing was used",
    "Teams window not found",
    "readchat needs the id of another chat (not the self chat)",
    "another kimeru Teams operation is running; Teams was not touched",
    "the time for reading the chat ran out; stopped",
    "unexpected error while reading the chat",
)


def _safe(msg):
    """The script's own fixed messages are shown as they are (and only that part of the text); anything else is not shown."""
    return next((m for m in SAFE_MESSAGES if msg.startswith(m)), "")


def _fits(preview, full, min_len=MIN_MATCH):
    """The body of the preview (no ellipsis, no leading "name: ") is at the start of what was read, for at least `min_len`
    characters (a shorter preview matches almost any text; never fewer than MIN_MATCH)."""
    min_len = max(MIN_MATCH, min_len)
    text = _norm(full)
    for c in (_core(preview), _body(preview)):
        head = c[:40]
        if len(head) >= max(1, min_len) and head in text:
            return True
    return False


def _restore_note(info):
    if info.get("restore") == "self" and info.get("had_original") is False:
        return "開いていたチャットが分からなかったため、自分とのチャットを開いた状態にしました"
    if info.get("restore") == "self":
        return "元のチャットへ戻せなかったため、自分とのチャットへ移しました"
    if info.get("restore") == "failed" or info.get("returned") is False:
        return "元のチャットへ戻せませんでした。Teams で開いているチャットを確認してください"
    return ""


def deepen(reader, ev):
    """(event to judge, info). Raises BudgetExhausted / Deferred: the event waits for the next cycle."""
    r = reader(ev)
    info = {"state": "full" if r.get("ok") else "preview_only"}
    if not r.get("ok"):
        info["why"] = r.get("why", "")
    for k in ("returned", "restore", "had_original"):   # whether the chat that was open is open again: recorded whatever the decision was
        if k in r:
            info[k] = r[k]
    note = _restore_note(info)
    if note:
        info["note"] = note
    if not r.get("ok"):
        return ev, info
    msgs = r["messages"]
    thread = [_norm(m["text"])[:THREAD_ITEM] for m in msgs[:-1]]
    ev2 = {**ev, "text": _norm(msgs[-1]["text"]), "full": True}
    if thread:
        ev2["thread"] = thread
    return ev2, info


def _scrub_str(text, full, excerpt):
    """Every stretch of `text` that copies more than the excerpt from the start of the full text (a whole copy, or one cut
    at some length: a description, a title) becomes the excerpt."""
    head, out, pos = full[:40], [], 0
    if len(head) < 40:
        return text.replace(full, excerpt)
    while True:
        i = text.find(head, pos)
        if i < 0:
            break
        n, m = 0, min(len(full), len(text) - i)
        while n < m and text[i + n] == full[n]:
            n += 1
        out.append(text[pos:i])
        out.append(excerpt if n > len(excerpt) - 1 else text[i:i + n])
        pos = i + n
    out.append(text[pos:])
    return "".join(out)


def scrub(obj, full, excerpt):
    """A copy of `obj` in which the full text (and any long copy of its start) is replaced by its excerpt (for the records that
    stay after the decision)."""
    if not full or full == excerpt:
        return obj
    if isinstance(obj, str):
        return _scrub_str(obj, full, excerpt)
    if isinstance(obj, list):
        return [scrub(x, full, excerpt) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub(v, full, excerpt) for k, v in obj.items()}
    return obj


def excerpt_event(ev):
    """The event with a summary-length text and no earlier messages."""
    out = {k: v for k, v in ev.items() if k not in ("thread", "full")}
    for k in ("text", "description", "item", "title", "meeting", "condition", "context"):   # the free-text fields
        if isinstance(out.get(k), str):
            t = _norm(out[k]) if k == "text" else out[k]
            out[k] = t if len(t) <= PERSIST_TEXT else t[:PERSIST_TEXT - 1] + "…"
    out["text"] = out.get("text") or ""
    return out


def persistable(ev):
    """A full-text event as the permanent records may keep it: the text is a summary-length excerpt, the thread is dropped."""
    return excerpt_event(ev) if ev.get("full") else ev


def redact_request(res, ev, instruction=None):
    """The paste-in request for Microsoft 365 Copilot holds the text it asks about. The record keeps a request built from the
    excerpt (`res` is changed); the request with the whole text is returned, for full_text.json. None when there is none."""
    req = res.get("copilot_request")
    if not req:
        return None
    from . import writer
    res["copilot_request"] = writer.human_request(res, excerpt_event(ev), instruction)
    return req


# ---- the full text of items that wait for the PM ----

def _path(out):
    return Path(out) / "full_text.json"


@contextlib.contextmanager
def _locked(out, wait_sec=30.0):
    """full_text.json is read, changed and written by the daily judgment step (outside the approvals lock) and by the approvals
    steps (inside it), so every read-modify-write takes this short lock of its own (full_text.lock), waiting for its turn: a save
    must not be skipped. A lock of a process that is gone is taken over at once (fsutil.exclusive)."""
    from . import fsutil
    end = time.time() + wait_sec
    while True:
        with fsutil.exclusive(Path(out) / "full_text.lock") as got:
            if got:
                yield
                return
        if time.time() >= end:
            raise RuntimeError("full_text.json is in use by another run (full_text.lock)")
        time.sleep(0.05)


def _read(out, tries=60, strict=False):
    """The kept texts. A reader outside full_text.lock (load) can meet the moment in which a writer replaces the file; Windows
    answers PermissionError then, which is waited out (a missing or damaged file is {}). strict=True (a delete: drop, purge): a file
    that exists but cannot be read is an error (DeleteFailed, noted in warnings.jsonl), never "nothing kept"."""
    for k in range(tries):
        try:
            data = json.loads(_path(out).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except PermissionError:
            if k == tries - 1:
                break
            time.sleep(0.005 * min(k + 1, 10))
        except OSError:
            break
        except ValueError:
            return {}
    if strict:
        _record_failure(out, "full_text.json (could not be read)")
        raise DeleteFailed(f"{_path(out)} could not be read: the full text stays until the next cycle")
    return {}


class DeleteFailed(OSError):
    """A full text that was to be deleted is still on the disk (the file could not be removed)."""


def _record_failure(out, what):
    """Leave a note in warnings.jsonl that a full text stayed on the disk (the delete is tried again in the next cycle)."""
    try:
        line = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "full_text": f"delete failed: {what}"}
        with (Path(out) / "warnings.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _write(out, data, strict=False):
    """Write the file, and only the file: fsutil.write_atomic keeps the previous version as .bak, which would keep a deleted
    full text on the disk. An empty file is not kept at all. strict=True (a delete: drop, purge): a file that cannot be removed is
    an error (DeleteFailed, noted in warnings.jsonl), never a success; the older copy is removed first, so that what is left
    still holds the deleted text under its own name and the next cycle tries again."""
    p = _path(out)
    bak = p.with_name(p.name + ".bak")
    from . import fsutil
    if not data:
        for f in (bak, p):
            if not fsutil._unlink_retry(f) and strict:   # a reader that has the file open for a moment must not fail the delete
                _record_failure(out, f"{f.name} (could not be removed)")
                raise DeleteFailed(f"{f} could not be removed: the full text stays until the next cycle")
        return
    if strict and not fsutil._unlink_retry(bak):
        _record_failure(out, f"{bak.name} (could not be removed)")
        raise DeleteFailed(f"{bak} could not be removed: the full text stays until the next cycle")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}.{threading.get_ident()}")   # one temporary file per process and thread
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        fsutil._replace(tmp, p)
    except OSError as e:
        if strict:   # the replace failed (another process keeps the file open): the old text stays, and it is noted
            _record_failure(out, f"{p.name} (could not be replaced: {type(e).__name__})")
            raise DeleteFailed(f"{p} could not be replaced: the full text stays until the next cycle") from e
        raise
    finally:
        tmp.unlink(missing_ok=True)
    fsutil._unlink_retry(bak)


def keep_days():
    return _int("full_text_keep_days", 7) or 7


def _expired(entry, now=None):
    try:
        at = datetime.fromisoformat(entry.get("at", ""))
    except (TypeError, ValueError, AttributeError):
        return True     # an entry that cannot be dated cannot be aged either: it goes
    return (now or datetime.now(timezone.utc)) - at > timedelta(days=keep_days())


def save(out, key, ev, request=None, now=None, material=None):
    with _locked(out):
        _save(out, key, ev, request, now, material)


def _save(out, key, ev, request, now, material):
    data = _read(out)
    entry = {"text": ev.get("text", ""), "thread": ev.get("thread", []), "title": ev.get("author", ""),
             "at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")}
    if request:
        entry["request"] = request
    if material:   # other free-text fields of the event (a description, an item), kept whole like the text
        entry["material"] = material
    data[key] = entry
    _write(out, data)


def set_request(out, key, request):
    """Replace the request kept with a full text (after a redraft)."""
    with _locked(out):
        data = _read(out)
        if key in data:
            if request:
                data[key]["request"] = request
            else:
                data[key].pop("request", None)
            _write(out, data)


def load(out, key, now=None):
    e = _read(out).get(key)
    return None if e is None or _expired(e, now) else e


def _guard(out, fn):
    """Run a delete under full_text.lock; every failure leaves a note in warnings.jsonl and is raised as DeleteFailed."""
    try:
        with _locked(out):
            return fn()
    except DeleteFailed:
        raise
    except (OSError, RuntimeError) as e:   # the lock could not be had in time, or a file operation failed
        _record_failure(out, f"full_text.json ({type(e).__name__})")
        raise DeleteFailed(str(e)) from e


def drop(out, key):
    def body():
        data = _read(out, strict=True)
        if key in data:
            del data[key]
            _write(out, data, strict=True)
            return True
        return False
    return _guard(out, body)


def drop_safe(out, key):
    """drop that never raises: a failure is in warnings.jsonl, the full text stays, and purge_locked deletes it in a later cycle
    (an approval or a rejection goes on whether or not the text could be deleted). False when it was not deleted now."""
    try:
        return drop(out, key)
    except Exception:
        return False


def purge_safe(out, now=None):
    """purge_locked that never raises (the caller holds the lock): a failure is in warnings.jsonl and is tried again next cycle."""
    try:
        return purge_locked(out, now)
    except Exception as e:
        if not isinstance(e, DeleteFailed):
            _record_failure(out, f"full_text.json ({type(e).__name__})")
        return []


def purge(out, now=None):
    """Delete the full texts kept longer than `full_text_keep_days` (under the lock every write of approvals.json takes: the items
    are changed too). Returns the keys removed; when the lock is held nothing is done and the result has `busy` set."""
    from . import fsutil, notify
    with fsutil.exclusive(notify.lock_path(out)) as got:
        if not got:
            return fsutil.BusyList.busy_result()
        return fsutil.BusyList(purge_locked(out, now))


def purge_locked(out, now=None):
    """The body of purge (the caller holds the lock). Deletes the texts kept past their time, and the texts of items that were
    approved or rejected (an earlier delete may have failed: this is its retry). Returns the keys of the expired ones."""
    from . import notify
    ap = notify.Approvals(out)
    decided = {it.get("key") for it in ap.data["items"].values() if it.get("status") in ("approved", "rejected")}

    def body():
        data = _read(out, strict=True)
        expired = [k for k, e in data.items() if _expired(e, now)]
        gone = expired + [k for k in data if k in decided and k not in expired]
        if not gone:
            if not data:
                _write(out, data)       # nothing kept: no file, and no .bak left by an older version
            return []
        for k in gone:
            del data[k]
        _write(out, data, strict=True)
        return expired
    expired = _guard(out, body)
    changed = False
    for it in ap.data["items"].values():
        if it.get("key") in expired:
            it["record"]["read_full"] = {"state": "preview_only", "why": f"全文の保存期限（{keep_days()} 日）を過ぎたため、全文を消しました"}
            changed = True
    if changed:
        ap.save()
    return expired
