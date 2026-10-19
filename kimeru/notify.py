"""Human-in-the-loop over Teams "chat with yourself".

Queue items (advise nodes with queue=true) are posted to the self chat as
"[kimeru #N] ..." and the PM replies from any device (e.g. iPhone) with
"OK N" / "NG N" / "保留 N". Actions attached to an advise node are only
*proposed*: an approval records the plan (dry-run). Only the kinds the PM switched on with
`config set execute` are carried out for real (execute.py), and only by the daily and approvals paths
(collect(real=True)); the demo, the eval scripts, `run` and `watch` never execute. A reply drafted by the
writer (writer.py) is shown in full; "修正 N <指示>" redrafts it and posts it again.
Posts made while collecting replies (results, texts ready to copy) are sent only with send=True and wait in an outbox
(approvals.json) until they were really posted.
"""
import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
import unicodedata
from pathlib import Path

from . import actions, config, explain, fsutil, hold, labels, learn, paths
from . import writer as writer_mod

HERE = paths.base()
REPLY = re.compile(r"^(OK|NG|保留|聞き返し|詳細|再実行|済|送る)\s*#?(\d+)\s*[.。!！]*$", re.IGNORECASE)
REDRAFT = re.compile(r"^修正\s*#?(\d+)\s*[:：]?\s*(\S.*)$")
REDO_K = re.compile(r"^再実行\s*#?(\d+)\s*-\s*(\d+)\s*[.。!！]*$")   # `再実行 N-k`: run again, k = the result count the PM saw (stated in the result post)
REDO_BAD = re.compile(r"^再実行\s*#?(\d+)\s*[^\w\s.。!！]+\s*\d*\s*[.。!！]*$")   # looks like `再実行 N-k` but with a mark that is not a dash (~ / and the like): recorded, not dropped
DASHES = str.maketrans({c: "-" for c in "ー−‐‑‒–—―─ｰ－"})   # every dash a phone or an IME may type between N and k (NFKC leaves several of them)
PASTE = re.compile(r"^下書き\s*#?(\d+)\s*[:：]?\s*(\S.*)$")   # the PM brings back what Microsoft 365 Copilot wrote
LABEL = re.compile(r"^違う\s*#?(\d+)(?:\s+(\d+))?$")   # `違う N`: the final result of #N was wrong; `違う N k`: the k-th option of the question `詳細 N` showed is right (learn=1 only)
MAX_PASTE = 1200


def parse_reply(line):
    """("OK", "3") for "OK 3", "ok3", "ＯＫ　３", "Ok #3"; None otherwise."""
    m = REPLY.match(unicodedata.normalize("NFKC", line).strip())
    return (m.group(1).upper(), m.group(2)) if m else None


def parse_redraft(line):
    """("3", "もっと短く") for "修正 3 もっと短く"; None otherwise."""
    m = REDRAFT.match(unicodedata.normalize("NFKC", line).strip())
    return (m.group(1), m.group(2).strip()) if m else None
def parse_redo_k(line):
    """("1", 2) for "再実行 1-2" (the redo of #1 that the PM wrote under its 2nd result post); None otherwise."""
    m = REDO_K.match(unicodedata.normalize("NFKC", line).strip().translate(DASHES))
    return (m.group(1), int(m.group(2))) if m else None


REDO_BAD2 = re.compile(r"^再実行\s*#?(\d+)(\s*[^\d\s.。!！]{1,6}\s*|\s+)(\d*)\s*[.。!！]*$")   # a separator between N and k that kimeru does not read ("1/2", "1〜2", "1 2", "1一2", "1_2", "1ー")
SEP_LETTERS = "ー一_"   # letters (to a regex) that are separators here; any other letter is part of a sentence ("再実行 1 お願い")


def _redo_bad_parts(line):
    """(N, rest) for a line that begins `再実行 N` followed by a separator kimeru does not read (a form that is neither `再実行 N` nor
    `再実行 N-k`); None otherwise. `rest` is what follows N; it is only ever hashed (redo_bad_kind), never kept."""
    t = unicodedata.normalize("NFKC", line).strip().translate(DASHES)
    m = re.match(r"^再実行形式 (\d+)(?: (.*))?$", t)   # the reader (teams-self.ps1) hands such a line back as "再実行形式 N <rest>"
    if m:
        if not m.group(2):
            return m.group(1), ""   # an older reader: no rest was handed back
        t = f"再実行 {m.group(1)} {m.group(2)}"
    if REDO_K.match(t):
        return None
    m = REDO_BAD.match(t)
    if m:
        return m.group(1), t[m.end(1):]
    m = REDO_BAD2.match(t)
    if not m:
        return None
    sep, k = m.group(2), m.group(3)
    if not k and not all((not c.isalnum()) or c in SEP_LETTERS for c in sep.strip()):
        return None   # words after the number ("再実行 1 お願い"), not a separator
    return m.group(1), t[m.end(1):]


def parse_redo_bad(line):
    """The number N for a line that begins `再実行 N` but is neither `再実行 N` nor `再実行 N-k` in a form kimeru reads; None otherwise."""
    r = _redo_bad_parts(line)
    return r[0] if r else None


def redo_bad_kind(line):
    """(N, kind) for such a line. `kind` is a short hash of the line's shape (digits are all "0", spaces left out): two replies in the same
    form have the same kind, and the reply itself cannot be read back from it. None when it is not such a line."""
    r = _redo_bad_parts(line)
    if not r:
        return None
    shape = re.sub(r"\s+", "", re.sub(r"\d+", "0", r[1]))
    return r[0], hashlib.sha1(shape.encode("utf-8")).hexdigest()[:6]


def parse_paste(line):
    """("3", "受領しました。…") for "下書き 3 受領しました。…"; None otherwise."""
    m = PASTE.match(unicodedata.normalize("NFKC", line).strip())
    return (m.group(1), m.group(2).strip()) if m else None


def parse_label(line):
    """("3", None) for "違う 3", ("3", 2) for "違う 3 2"; None otherwise. Read only when learn=1 (labels.py)."""
    m = LABEL.match(unicodedata.normalize("NFKC", line).strip())
    return (m.group(1), int(m.group(2)) if m.group(2) else None) if m else None


NOT_NEEDED = re.compile(r"^不要\s*#?(\d+)\s*[.。!！]*$")   # R33 `不要 N`: NG, and the PM did not need to see the line (learn=1 only; learn.py counts it)


def parse_not_needed(line):
    """The N of a `不要 N` line ("不要 3", "不要＃３。"); None for any other line."""
    m = NOT_NEEDED.match(unicodedata.normalize("NFKC", line).strip())
    return m.group(1) if m else None


def _not_needed_reply(line):
    """("不要", "3") for `不要 3` at learn=1; None otherwise, and always None at learn=0 (the line means nothing then)."""
    n = parse_not_needed(line)
    return ("不要", n) if n is not None and learn.enabled() else None


SENT_REPLY = re.compile(r"^送った\s*#?(\d+)\s*[.。!！]*$")   # R28 `送った N`: the PM says the approved reply of #N was sent (sent.py, sent_watch=1). Not a REPLY word: it decides no state


def parse_sent(line):
    """The N of a `送った N` line ("送った 3", "送った３", "送った #3"); None for any other line."""
    m = SENT_REPLY.match(unicodedata.normalize("NFKC", line).strip())
    return m.group(1) if m else None


WORK_REPLY = re.compile(r"^(着手|完了)\s*#?(\d+)(?:\s+(\d+))?\s*[.。!！]*$")   # R30 `着手 N` / `完了 N` / `完了 N k` (step k of the plan): work.py, promises=1 only
WORK_DEFER = re.compile(r"^延期\s*#?(\d+)\s+(\S{1,10})\s*[.。!！]*$")         # R30 `延期 N <いつ>`: the case's day moves (hold.parse_when)


def parse_work(line):
    """(word, number, arg) for `着手 N` / `完了 N` / `完了 N k` / `延期 N <いつ>` (R30): arg is None, the step k (int), or the words of
    the day. None for any other line and for `着手 N k` (a step is not started). Read by fresh_work and the reader's line."""
    t = unicodedata.normalize("NFKC", str(line)).strip()
    m = WORK_REPLY.match(t)
    if m:
        if m.group(1) == "着手" and m.group(3):
            return None
        return m.group(1), m.group(2), int(m.group(3)) if m.group(3) else None
    m = WORK_DEFER.match(t)
    if m:
        word = m.group(2).rstrip(".。!！")
        return ("延期", m.group(1), word) if word else None
    return None


def fresh_work(read):
    """[(number, parsed, line)] for the R30 work replies on the screen, in order: a reply counts when it comes after the most recent
    visible post of its number, the approval post "[kimeru #N]" or the deadline post "[kimeru 期限 #N ...]" (K:N in the timeline). Only
    that boundary counts, so a reply above a newer deadline post is not read again; the same rule as fresh_sent. [] for an older reader."""
    tl = scoped_timeline(read)
    if tl is None:
        return []
    last = {}
    for i, e in enumerate(tl):
        if e.startswith("P:"):
            last[e[2:]] = i
        elif e.startswith("K:"):
            for num in filter(None, e[2:].split(",")):
                last[num] = i
    out = []
    for i, e in enumerate(tl):
        if not e.startswith("R:"):
            continue
        parsed = parse_work(e[2:])
        if parsed is not None and parsed[1] in last and i > last[parsed[1]]:
            out.append((parsed[1], parsed, e[2:]))
    return out


def _hold_reply(line):
    """(number, None) for `保留 N`, (number, word) for `保留 N <いつ>` (R09, hold.py); None for any other line."""
    r = parse_reply(line)
    if r:
        return (r[1], None) if r[0] == "保留" else None
    return hold.parse_hold_until(line)


STATUS = {"OK": "approved", "NG": "rejected", "保留": "held", "聞き返し": "ask_back", "詳細": "detail", "再実行": "redo", "済": "closed", "不要": "rejected", "送る": "send"}   # 聞き返し / 詳細 / 再実行 / 済 / 送る: not decisions; 不要 (R33, learn=1): NG, and not needed; 送る (S2): the approved reply goes out (execute.reply_request), not a decision


TEST_ENV = "KIMERU_TEST_MARK"   # tools/check.ps1 sets it for the kimeru commands it runs: they post and read test posts


def test_mode():
    return os.environ.get(TEST_ENV) == "1"


def to_test_post(text):
    """The first line of a post that tools/check.ps1 makes: "[kimeru 試験 #N]" and "[kimeru 試験 実行 #N k]". The real reader never takes them
    for an approval post or a result post, so an "OK N" meant for a test is never an answer to the item #N of the daily cycle."""
    if text.startswith("[kimeru #"):
        return "[kimeru 試験 #" + text[len("[kimeru #"):]
    if text.startswith("[kimeru 実行 #"):
        return "[kimeru 試験 実行 #" + text[len("[kimeru 実行 #"):]
    if text.startswith("[kimeru 詳細 #"):
        return "[kimeru 試験 詳細 #" + text[len("[kimeru 詳細 #"):]
    return text


def _reply_number(line):
    """The item number a reply line is about (any reply form); None for a line that is none."""
    r = parse_reply(line)
    if r:
        return r[1] if r[0] in STATUS else r[0]
    for f in (parse_redraft, parse_paste, parse_redo_k, parse_label, hold.parse_hold_until):
        r = f(line)
        if r:
            return r[0]
    not_needed = parse_not_needed(line)   # R33: `不要 N` belongs to #N for the test/real split too
    if not_needed is not None:
        return not_needed
    work_reply = parse_work(line)   # R30: `完了 N` and the others belong to #N for the test/real split too
    if work_reply is not None:
        return work_reply[1]
    sent_number = parse_sent(line)   # R28: a `送った N` belongs to #N for the test/real split (scoped_timeline)
    return sent_number if sent_number is not None else parse_redo_bad(line)


def scoped_timeline(read, test=None):
    """The timeline of a read, for one kind of reader. The real reader (daily, approvals) never uses a test post as a boundary or as an
    approval post, and it does not take a reply that follows a test post of the same number as its own: "OK 123" typed under
    "[kimeru 試験 #123]" is for the test. The test reader (KIMERU_TEST_MARK=1, tools/check.ps1) reads its own posts and the replies
    after them, and nothing else. None when the bridge gives no timeline."""
    tl = read.get("timeline")
    if tl is None:
        return None
    test = test_mode() if test is None else test
    last, out = {}, []   # last: the kind of the latest post of each number ("T" test, "M" real)
    for e in tl:
        head, _, rest = e.partition(":")
        if head in ("T", "TX", "P", "X"):
            num = rest.partition(":")[0]
            last[num] = "T" if head in ("T", "TX") else "M"
            if test and head in ("T", "TX"):
                out.append(("P:" if head == "T" else "X:") + rest)
            elif not test and head in ("P", "X"):
                out.append(e)
        elif head == "D":   # a detail post "[kimeru 詳細 #N]": the real reader only (a test never posts one that is read back)
            if not test:
                out.append(e)
        elif head == "U":   # R28: the nudge "[kimeru 未送信 #N ...]" (numbers only): the real reader only; it sets no kind for `last`
            if not test:    # (so the reply boundaries and the test/real split of fresh_entries do not change), see fresh_sent
                out.append(e)
        elif head == "K":   # R30: the deadline post "[kimeru 期限 #N ...]" (numbers only): a boundary for the work replies, real reader only
            if not test:
                out.append(e)
        elif head == "R":
            num = _reply_number(rest)
            mine = last.get(num) == "T" if test else last.get(num) != "T"
            if mine and (num is not None or not test):
                out.append(e)
        else:
            out.append(e)
    return out


class BridgeError(RuntimeError):
    """The script reported a failure. `data` is its JSON (never chat text): what it says about putting the chat back is in it."""

    def __init__(self, message, data=None):
        super().__init__(message)
        self.data = data or {}


# N2: what `chats` says when the list cannot be read and Teams runs with no window, or does not run (one line each, no names)
TEAMS_NO_WINDOW = "Teams のウィンドウが表示されていません（起動はしています）。Teams のウィンドウを開いてチャット一覧を表示してください"
TEAMS_NOT_RUNNING = "Teams が起動していません"


class PowerShellBridge:
    """Runs tools/teams-self.ps1 (built-in UI Automation, no downloads)."""

    def __init__(self, script=HERE / "tools" / "teams-self.ps1"):
        self.script = str(script)

    def _run(self, *args):
        # readchat moves the screen, waits for a quiet keyboard and puts the chat back: it needs more than the others (its own limit is 90 s);
        # sendto does the same kind of work (opens one chat, pastes, sends once, puts the chat back)
        timeout = 300 if ("readchat" in args or "sendto" in args) else 120
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", self.script, *args],
                               capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        except subprocess.TimeoutExpired as e:   # killed: the script could not put the chat back itself
            if "readchat" not in args:
                raise
            raise BridgeError("the script did not finish in time", {"restore": "failed"}) from e
        try:
            out = json.loads(r.stdout.strip().lstrip("﻿") or "{}")
        except ValueError as e:
            if "readchat" not in args:
                raise
            raise BridgeError("the script's output could not be read", {"restore": "failed"}) from e
        if not out.get("ok"):
            raise BridgeError(out.get("error") or r.stderr.strip()[:300], out)
        return out

    def post(self, text, send):
        # line breaks travel as literal \n (command-line args); the script expands them
        if test_mode():
            text = to_test_post(text)
        return self._run("-Action", "post", "-Text", text.replace("\n", "\\n"), *(["-Send"] if send else []))

    def read(self):
        return self._run("-Action", "read")

    def chats(self):
        try:
            return self._run("-Action", "chats").get("chats", [])
        except BridgeError as e:
            raise BridgeError(self._no_list_reason(e), e.data) from e

    def _no_list_reason(self, first):
        """N2: the chat list cannot be read. Teams that runs with no window shown (tray) is not "not started": one status read tells
        the two apart. Any other case, or a status that cannot be read or has no such fields, keeps the script's own message."""
        try:
            st = self._run("-Action", "status")
        except Exception:   # the status only words the error; the first failure is the one reported
            return str(first)
        if st.get("teams_window") or "teams_process" not in st:
            return str(first)
        return TEAMS_NO_WINDOW if st.get("teams_process") else TEAMS_NOT_RUNNING

    def probe(self):
        """Read only (`kimeru diagnose`): the self-chat messages as the send readback reads them. The texts stay in memory."""
        return self._run("-Action", "probe")

    def readchat(self, chat_id, count=5, preview=None):
        """Open one chat, read its last messages (read only) and go back to the chat that was open. Slow (a few seconds).
        `preview`: the start of the chat's list preview; a layout that reports no selection is checked against it."""
        return self._run("-Action", "readchat", "-ChatId", chat_id, "-Count", str(count), *(["-Preview", preview] if preview else []))

    def sendto(self, chat_id, text):
        """`送る N` (S2): puts the approved text into one 1 対 1 chat and sends it ONCE (tools/teams-self.ps1 -Action sendto). The text
        travels in a temporary file, never on the command line, and the file is removed after the call. Returns the script's answer
        when it is "done"; any other answer raises BridgeError with it (data["result"] is "failed" or "unknown")."""
        fd, path = tempfile.mkstemp(prefix="kimeru-send-", suffix=".txt")
        try:
            with os.fdopen(fd, "wb") as f:   # bytes: a text-mode write would turn each line break into CRLF
                f.write(str(text).encode("utf-8"))
            return self._run("-Action", "sendto", "-ChatId", str(chat_id), "-TextFile", path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


def ado_lines(rec):
    """Which work item an ADO post or notice is about: id, type, title, creator and a link to it (the link only when the
    record says where the item was pulled from). Nothing for any other kind of event."""
    if not str(rec.get("event_kind") or "").startswith("ado."):
        return []
    from urllib.parse import quote
    from . import execute, graph
    ev = rec.get("event") or {}
    head = f"対象: #{rec.get('event_id')}"
    if ev.get("type"):
        head += f" [{ev['type']}]"
    if ev.get("title"):
        head += " " + graph.one_line(str(ev["title"]))
    lines = [head]
    if ev.get("created_by"):
        lines.append(f"起票者: {ev['created_by']}")
    org, project = execute.origin_of(rec)
    if org and project and rec.get("event_id"):
        lines.append(f"リンク: https://dev.azure.com/{quote(org, safe='')}/{quote(project, safe='')}/_workitems/edit/{quote(str(rec['event_id']), safe='')}")
    return lines


def _clip(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:max(1, n - 1)] + "…"


WHAT_RAN = {"ado.update": "ADO 更新", "ado.comment": "ADO コメント", "ado.create": "ADO 起票", "teams.reply": "返信",
            "teams.post": "チャネル投稿", "oncall.page": "当番呼び出し"}   # what a notice says was recorded, in the PM's words


def _ado_label(rec):
    ev = rec.get("event") or {}
    label = f"ADO #{rec.get('event_id')}"
    if ev.get("type"):
        label += f" [{ev['type']}]"
    if ev.get("title"):
        label += " " + str(ev["title"])
    return label


def _who(rec, limit=40):
    """Who or what an item is about, in `limit` characters: "山田さん: 仕様変更の可否", "ADO #501 [Bug] 一覧の並び順"."""
    ev = rec.get("event") or {}
    memo = rec.get("memo") or {}
    kind = str(rec.get("event_kind") or "")
    if kind.startswith("ado."):
        return _clip(_ado_label(rec), limit)
    short = memo.get("summary") if len(str(memo.get("summary") or "")) <= 30 else ""   # a short memo summary names the topic best
    topic = short or ev.get("text") or ev.get("item") or memo.get("summary") or rec.get("summary") or rec.get("advice") or ""
    if kind == "teams.chat" and ev.get("author"):
        first = (rec.get("first_message") or {}).get("text")   # R08: a continued item names its first request, not the newest message
        return _clip(f"{ev['author']}さん: {first or topic}", limit)
    if kind == "meeting.item" and ev.get("meeting"):
        return _clip(f"{ev['meeting']}: {ev.get('item') or topic}", limit)
    if kind == "monitor.alert" and ev.get("rule"):
        return _clip(f"{ev['rule']}（{ev.get('severity') or '?'}）", limit)
    return _clip(rec.get("summary") or topic or kind, limit)


def _draft_line(rec):
    """The draft the item carries, in one line: a writer draft, the fixed comment, or the first work-item titles."""
    actions = rec.get("actions", [])
    for a in actions:
        if a.get("exec_text"):   # exactly what OK writes (execute.freeze): shown whole up to 300 characters
            text = str(a["exec_text"]).strip()
            return "コメント: " + (text if len(text) <= 300 else text[:299] + "…（全文は 詳細）")
    for a in actions:
        field = writer_mod.FIELD.get(a.get("type"))
        if field and a.get("type") != "ado.create" and str(a.get(field) or "").strip():
            return "案: " + _clip(a[field], 120)
    creates = [a for a in actions if a.get("type") == "ado.create" and a.get("title")]
    if creates:
        return "起票案: " + _clip(creates[0]["title"], 60) + (f" ほか {len(creates) - 1} 件" if len(creates) > 1 else "")
    return ""


def _why(rec):
    """R11: with the setting post_reason=1, the line that says which question the model was unsure of (explain.open_question);
    "" otherwise, so at 0 (the default) the texts are unchanged."""
    if config.value("post_reason") != "1":
        return ""
    return explain.open_question(rec)


def _why_post(rec):
    """post_reason=1: the line of the approval post: the question the model was unsure of (_why) and, when the memo names
    them, the missing material, in one line. "" when there is no question."""
    why = _why(rec)
    if not why:
        return ""
    extra = []   # no probability or confidence number: R11 shows the doubt in words (and Jev's numbers stay out of Teams)
    missing = [str(v) for v in ((rec.get("memo") or {}).get("missing") or []) if str(v).strip()]
    if missing:
        extra.append("足りない材料: " + _clip(" / ".join(missing), 60))
    return why + (" / " + " / ".join(extra) if extra else "")


def _rules(rec, full):
    """R25: with the setting post_reason=1, the lines that name the words a rule read (explain.rule_evidence) at the top of 詳細 N;
    [] otherwise, so at 0 (the default) the detail is unchanged. `full` is the whole text kept while the item waits (fulltext.load)."""
    if config.value("post_reason") != "1":
        return []
    return explain.rule_evidence(rec, full)


def _draft_mark(rec):
    """R12: with the setting post_kind_mark=1, a record that waits only for a writer's draft (held_reason "draft", cli._draft).
    False otherwise, so at 0 (the default) the texts are unchanged."""
    return config.value("post_kind_mark") == "1" and rec.get("held_reason") == "draft"


def _others(rec):
    """R35 (split_asks=1): "（ほかに依頼 N 件）" when the message had N+1 requests; the record keeps the count only (`asks.n`).
    "" otherwise, so with the setting off (no `asks` in the record) the texts are unchanged."""
    asks_rec = rec.get("asks")
    n = int(asks_rec.get("n") or 0) if isinstance(asks_rec, dict) else 0
    return f" （ほかに依頼 {n - 1} 件）" if n >= 2 else ""


def format_post(n, rec, full=None, marks=None, precedent=None, promise=None, related=None):
    """The approval post "[kimeru #N]": at most 5 short lines. Everything else (the source text, the memo, every draft, the
    revision, the kinds of action) is in the answer to `詳細 N` (format_detail). `full` is not shown here. `marks` (R09, hold.post_marks):
    {"day": 2} for an item that came back on the 2nd day of its hold, {"undated": ["3"]} for the undated holds named on this post.
    `precedent` (R16, memory.precedent): the text that ends the arrow line; None (memory=0) leaves the post as it was.
    `promise` (R18, promises.post_note): the open promise of the same chat, at the end of the arrow line too; None (promises=0) adds nothing.
    `related` (R19, link.post_note): the `↔` text of the same case, at the end of the arrow line too; None (link_related=0) adds nothing."""
    if learn.is_card(rec):   # R21: a card of learn=1 says what OK changes (learn.py); it is no case
        return learn.format_post(n, rec)
    from . import execute
    memo = rec.get("memo") or {}
    plan = rec.get("plan") or {}
    actions = rec.get("actions", [])
    drafts = [a for a in actions if a.get("drafted_by")]
    update = "（更新）" if int(rec.get("revision") or 1) > 1 else ""
    held_mark = " （前回 保留）" if rec.get("held_before") else ""   # R08: a continued item that was on hold before, until OK or NG
    day = (marks or {}).get("day")
    resurfaced = f" （保留から・{day} 日目）" if day else ""
    undated = (marks or {}).get("undated")
    named = f" （保留（日付なし）: {', '.join('#' + str(x) for x in undated)}）" if undated else ""
    draft_only = _draft_mark(rec)   # R12: 下書き: in the first line; the number "[kimeru #N]" stays where the reader looks for it
    from . import promises   # R31 (promises=1): a reply that answers an ask-back #M is marked after the number on the first line; "" otherwise
    answer = promises.answer_mark(rec)
    lines = [f"[kimeru #{n}] {answer + ' ' if answer else ''}{'下書き: ' if draft_only else ''}{update}{_who(rec)}{_others(rec)}{held_mark}{resurfaced}{named}"]
    if str(rec.get("event_kind") or "").startswith("ado.") and any(a.get("type") == "ado.comment" for a in actions) and not memo:
        proposal = "情報不足: 追記依頼のコメント案あり"
    else:
        proposal = memo.get("next") or rec.get("advice") or plan.get("summary") or "内容を確認してください"
    if draft_only:
        lead = "文面だけ確認（判断は自動: "
        arrow = "→ " + lead + _clip(proposal, 100 - len(lead) - 1) + "）"
    else:
        arrow = "→ " + _clip(proposal, 100)
    lines.append(arrow + (f" {precedent}" if precedent else "") + (f" {promise}" if promise else "")
                 + (f" {related}" if related else ""))   # R16 / R18 / R19: they end the arrow line; no line is added
    why = _why_post(rec)   # R11 / T7: one line more, after the arrow line (it replaces nothing)
    if why:
        lines.append(why)
    if rec.get("first_message"):   # R08: the newest message of a continued item, one line after the proposal (30 characters)
        newest = _clip((rec.get("event") or {}).get("text"), 30)
        if newest:
            lines.append(f"続き「{newest}」")
    draft = _draft_line(rec)
    if draft:
        lines.append(draft)
    link = next((l for l in ado_lines(rec) if l.startswith("リンク: ")), "")
    if link:
        lines.append(link[len("リンク: "):])
    rf = rec.get("read_full") or {}
    if rf.get("state") == "preview_only":
        lines.append("⚠ プレビューだけで判断しました（元のメッセージを Teams で確認）")
    if rf.get("note"):   # the chat that was open could not be put back (also when nothing could be read)
        lines.append("⚠ " + _clip(rf["note"], 100))
    if rec.get("writer_error"):
        lines.append(f"⚠ {writer_mod.writer_label(rec)} の下書きを作れなかったため定型文です")
    elif any(a.get("writer_warning") for a in actions):
        lines.append(f"⚠ {writer_mod.writer_label(rec)} の下書きの一部は定型文です（詳細 {n}）")
    if rec.get("memo_unverified") or any(a.get("unverified") for a in drafts):
        lines.append(f"⚠ 元の材料に無い日付・数値あり（詳細 {n}）")
    if rec.get("redraft_note"):
        lines.append("⚠ " + _clip(rec["redraft_note"], 100))
    if drafts and config.value("post_checked") == "1":   # T9 (b): what of the draft is in the event, and what only kimeru proposes (two lines at most)
        found, own = writer_mod.checked_points(rec, rec.get("material_event"), full)
        if found:
            lines.append("確かめた点: " + _clip(", ".join(found), 100))
        if own:
            lines.append("kimeru の提案: " + _clip(", ".join(own), 100))
    writes = [a for a in actions if a.get("exec_text")]
    if writes:   # where it will be written: the organization and the project it was pulled from (execute.freeze)
        tg = writes[0].get("exec_origin") or writes[0].get("exec_target") or {}
        where = f"（{tg['org']} / {tg['project']}）" if tg.get("org") and tg.get("project") else ""
        lines.append(f"OK で ADO{where}の #{writes[0].get('id')} にコメントを書きます（上の文面そのまま）。--send 無しの実行では次の --send で書きます")
    elif any(a.get("exec_skip") for a in actions):
        lines.append("OK は記録だけです（ADO の取り込み元が記録されていません）")
    if any(a.get("reply_skip") for a in actions):   # S2 (teams.reply switched on): a reply that `送る N` cannot send
        lines.append("送れません（グループ／相手が特定できない）")
    if rec.get("copilot_request"):
        lines.append(f"↓ 次の投稿を Copilot に貼り、返った文面を 1 行で「下書き {n} 〈文面〉」と返信")
    reply = f"OK {n} / NG {n}"
    if drafts:
        reply += f" / 修正 {n} <点>"
    if any(a.get("ask_back") for a in drafts):
        reply += f" / 聞き返し {n}"
    lines.append(reply + f" / 詳細 {n}")
    return "\n".join(lines)


def format_detail(n, rec, full=None, precedent=None, promise=None):
    """The full text of an item ("[kimeru 詳細 #N]", the answer to `詳細 N`). `full` is the full text kept while the item waits
    (fulltext.load): the post then shows it and the earlier messages. `precedent` (R16, memory.history): the lines of
    「これまでの経緯」, shown above the reply line; None or empty (memory=0) adds nothing. `promise` (R18, promises.detail_lines): the
    chat's open promises, under 「この相手の約束」 above the reply line; None (promises=0) adds nothing."""
    if learn.is_card(rec):   # R21: the numbers behind a card (learn.py)
        return learn.format_detail(n, rec)
    from . import execute
    ev = rec.get("event_kind", "")
    memo = rec.get("memo") or {}
    plan = rec.get("plan") or {}
    advice = rec.get("advice") or "判断の詳細は以下を確認してください"
    next_action = memo.get("next") or plan.get("summary")
    missing = memo.get("missing") or []
    # nothing is guessed: without a memo (no writer set) the post says where the rest would come from
    no_memo = "（判断メモなし。文面用の LLM〔writer〕を設定すると付きます）"
    effects = []
    for action in rec.get("actions", []):
        kind = action.get("type", "?")
        if kind in execute.REPLY_KINDS and (action.get("reply_text") or action.get("reply_skip")):   # S2: switched on for this reply
            effects.append("承認のあと「送る N」と返すと、相手の 1 対 1 のチャットへ 1 回送る（対象外のものは送らない）")
        elif kind in execute.SEND_READY:
            effects.append("コピー用文面を自分チャットへ返す（相手には送信しない）")
        elif kind == "ado.comment" and action.get("exec_text"):
            effects.append("実行設定が有効なら、承認後に ADO へコメントを書き込む")
        else:
            effects.append("記録のみ")
    revision = rec.get("revision", 1)
    lines = [f"[kimeru 詳細 #{n}] 判断が必要（{rec.get('graph')}）{_others(rec)} / 改訂 {revision}",
             f"判断: {advice}", "次の一手: " + (next_action or no_memo),
             "不足情報: " + (" / ".join(str(v) for v in missing) if missing else ("メモに記載なし" if memo else no_memo)),
             "OK の効果: " + ("; ".join(dict.fromkeys(effects)) if effects else "記録のみ"),
             f"{ev} #{rec.get('event_id')}"] + ado_lines(rec)
    why = _why(rec)   # R11: under the header, when post_reason=1
    lines[1:1] = _rules(rec, full) + ([why] if why else [])   # R25: the words a rule read come first, then R11's question
    rf = rec.get("read_full") or {}
    if rf.get("state") == "preview_only":
        lines.append("⚠ プレビューだけで判断しました（" + str(rf.get("why", ""))[:100] + "）。元のメッセージを Teams で確認してください")
    if rf.get("note"):   # the chat that was open could not be put back (also when nothing could be read)
        lines.append("⚠ " + rf["note"])
    if rec.get("advice"):
        lines.append(f"内容: {rec['advice']}")
    if rec.get("actions"):
        lines.append("承認の対象: " + ", ".join(a.get("type", "?") for a in rec["actions"])
                     + "（既定は記録だけ。実行するのは設定で有効にした種類だけ）")
    src = (rec.get("material_event") or {})
    if full and not (src and any(a.get("drafted_by") for a in rec.get("actions", []))):
        lines.append("元（全文）: " + (f"{(rec.get('event') or {}).get('author')}: " if (rec.get("event") or {}).get("author") else "") + str(full.get("text", ""))[:600])
        for t in (full.get("thread") or [])[-3:]:
            lines.append("　直前のやり取り: " + str(t)[:120])
    if src and any(a.get("drafted_by") for a in rec.get("actions", [])):
        what = src.get("text") or src.get("item") or src.get("title") or src.get("rule") or ""
        who = src.get("author")
        shown = (full or {}).get("text") or what
        lines.append("元" + ("（全文）" if full else "") + ": " + (f"{who}: " if who else "") + str(shown)[:(600 if full else 200)])
        for t in ((full or {}).get("thread") or [])[-3:]:
            lines.append("　直前のやり取り: " + str(t)[:120])
    first = (rec.get("first_message") or {}).get("text")
    if first:   # R08: the first request of a continued item, before the continuation lines
        lines.append("最初のメッセージ: " + str(first)[:120])
    for f in (rec.get("followups") or [])[-3:]:
        lines.append("続きのメッセージ: " + str(f.get("text", ""))[:120])
    if memo:
        lines.append(f"{writer_mod.writer_label(rec)} のメモ:")
        if memo.get("summary"):
            lines.append(f"・要点: {memo['summary']}")
        if memo.get("missing"):
            lines.append("・足りない情報: " + " / ".join(memo["missing"]))
        if memo.get("options"):
            lines.append("・選択肢: " + " / ".join(memo["options"]))
        if memo.get("next"):
            lines.append(f"・次の一手: {memo['next']}")
        if rec.get("memo_unverified"):
            lines.append("⚠ メモに、元の材料に無い日付・数値・人名: " + ", ".join(rec["memo_unverified"]))
    if rec.get("copilot_sources"):
        lines.append("Copilot が参照: " + " / ".join(rec["copilot_sources"]))
    drafts = [a for a in rec.get("actions", []) if a.get("drafted_by")]
    waiting = [a for a in rec.get("actions", []) if a.get("held_for")]
    if rec.get("redraft_note"):
        lines.append("⚠ " + rec["redraft_note"])
    if rec.get("writer_error"):
        lines.append(f"⚠ {writer_mod.writer_label(rec)} の下書きを作れなかったため定型文です（" + str(rec["writer_error"])[:60] + "）")
    for a in waiting:
        if a.get("writer_warning"):
            lines.append(f"⚠ {writer_mod.writer_label(rec)} の{writer_mod.LABEL.get(a['type'], a['type'])}は使えないため定型文です（{a['writer_warning']}）")
    for a in waiting[:1]:
        lines.append("定型文: " + str(a.get(writer_mod.FIELD[a["type"]]) or ""))
    for a in rec.get("actions", []):   # a fixed comment of the graph: no writer drafted it, and it is not written (OK records only)
        if a.get("type") == "ado.comment" and not a.get("drafted_by") and not a.get("held_for") and not a.get("exec_text")                 and not a.get("exec_skip") and a.get("text"):
            lines.append("コメント案（定型文）: " + str(a["text"]))
    if rec.get("copilot_request"):
        lines.append(f"↓ 次の投稿を Microsoft 365 Copilot に貼ってください。返ってきた 1 件目の文面は、改行を入れずに 1 行で「下書き {n} 〈文面〉」と返信すると、この投稿に取り込みます")
    tasks = [a for a in drafts if a["type"] == "ado.create"]
    hidden = tasks[2:]   # a phone screen: two work-item descriptions, the rest counted (all are kept and run on OK)
    for a in drafts:
        if a in hidden:
            continue
        label = (writer_mod.LABEL.get(a["type"], a["type"]) + ("（聞き返し）" if a.get("variant") == "ask_back" else "")
                 + (f"「{a['title']}」" if a.get("title") else ""))
        by = writer_mod.WRITER_LABEL.get(a["drafted_by"], a["drafted_by"])   # copilot -> Copilot, script -> 台本（AI ではない）
        lines.append(f"{label}の下書き（{by}）:\n{a[writer_mod.FIELD[a['type']]]}")
        if a.get("unverified"):
            lines.append("⚠ 元の材料に無い日付・数値・人名: " + ", ".join(a["unverified"])
                         + ("（メール・会議由来なら、元の内容を確認してください）" if str(a.get("drafted_by", "")).startswith("m365") else ""))
        if a.get("ask_back"):
            lines.append(f"聞き返すなら（「聞き返し {n}」でこちらを送る）:" + chr(10) + a["ask_back"])
            if a.get("ask_back_unverified"):
                lines.append("⚠ 元の材料に無い日付・数値: " + ", ".join(a["ask_back_unverified"]))
    if hidden:
        lines.append(f"作業項目の説明の下書き ほか {len(hidden)} 件（OK ですべて記録）:")
        for a in hidden:
            lines.append("・" + str(a.get("title") or "")[:50] +
                         ("　⚠ 元の材料に無い日付・数値: " + ", ".join(a["unverified"]) if a.get("unverified") else ""))
    for a in rec.get("actions", []):
        if a.get("exec_skip"):   # no origin recorded: OK records the plan only
            lines.append(f"→ OK {n} では、作業項目 {a.get('id')} に書きません（{a['exec_skip']}）。承認は記録だけです")
        elif a.get("exec_text"):   # fixed when this post was made (execute.freeze): exactly what OK will write, and where
            tg = a.get("exec_origin") or a.get("exec_target") or {}   # the origin of the work item: the place it will be written to
            lines.append(f"→ OK {n} で、作業項目 {a.get('id')}（取り込み元の組織 {tg.get('org')} / プロジェクト {tg.get('project')}）に、"
                         "次のコメントをそのまま書きます（末尾の 1 行も含みます）:")
            lines.append(a["exec_text"])
    if precedent:   # R16: the earlier decisions on the same chat, person or work item (at most three lines)
        lines.append("これまでの経緯:")
        lines.extend(precedent)
    if promise:   # R18: the chat's open promises (promises=1 only; the lines are kept out of the reply line)
        lines.append("この相手の約束:")
        lines.extend(promise)
    ask = any(a.get("ask_back") for a in drafts)
    lines.append(f"返信: OK {n} / NG {n} / 保留 {n}" + (f" / 修正 {n} <直してほしい点>" if drafts else "")
                 + (f" / 下書き {n} <Copilot の文面>" if rec.get("copilot_request") else "")
                 + (f" / 聞き返し {n}" if ask else ""))
    return "\n".join(lines)


# what a follow-up keeps of the item it replaces: the case, its work and handoff marks, the measurement, the delivery
# state and the hold state (cli._merge_locked, fold_recurring)
FOLLOWUP_KEEP = ("key", "work", "handoff_evidence", "handoff_unknown",
                 "measurement", "measurement_history",
                 "delivery_unknown", "delivery_unknown_part", "delivery_unknown_at",
                 "delivery_unknown_revision", "delivery_unknown_generation", "delivery_attempt",
                 "held_at", "hold_count", "hold_round")   # R09: when a hold began, its count and its round carry over; the day does not (the item is pending again)


class Approvals:
    def __init__(self, out):
        self.path = Path(out) / "approvals.json"
        self.data = fsutil.read_json(self.path, {"next": 1, "items": {}})

    def _drop_closed_history(self):
        """T9 (c): the instructions of redraft_history=1 stay in a waiting item (pending or held) only: an approved, rejected or expired
        item loses them, and so does every item when the setting is off. True when something was removed."""
        keep = config.value("redraft_history") == "1"
        dropped = False
        for it in (self.data.get("items") or {}).values():
            if isinstance(it, dict) and "redraft_history" in it and not (keep and it.get("status") in ("pending", "held")):
                del it["redraft_history"]
                dropped = True
        return dropped

    def save(self, compact=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        compact = self._drop_closed_history() or compact   # no .bak that would keep the instructions of a closed item
        text = json.dumps(self.data, ensure_ascii=False, indent=1)
        if not compact:
            fsutil.write_atomic(self.path, text)
            return
        # A follow-up replaces sensitive old material; an atomic backup would preserve
        # the superseded draft/execution body. Replace atomically and remove stale backup.
        tmp = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}.{id(self)}")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)
            self.path.with_name(self.path.name + ".bak").unlink(missing_ok=True)
        finally:
            tmp.unlink(missing_ok=True)

    def known(self, key):
        return any(it["key"] == key for it in self.data["items"].values())

    def add(self, key, rec):
        n = self.data["next"]
        self.data["next"] += 1
        self.data["items"][str(n)] = {"key": key, "status": "pending", "posted": False, "record": rec}
        measurement_for(self.data["items"][str(n)])
        return n


def measurement_now():
    """Observation time, not the sender's event/reply time."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


ARRIVAL_KEYS = ("arrived_at", "arrival_source", "arrival_window_s")   # when the PC saw the event (pull.arrival_of): carried, never estimated


def measurement_for(item):
    """Additive metadata only: never consulted for approval or execution eligibility."""
    current = item.get("measurement")
    if not isinstance(current, dict) or current.get("schema") != 1:
        original = item.get("record", {}).get("measurement")
        original = original if isinstance(original, dict) else {}
        current = {"schema": 1, "source": original.get("source", "legacy"),
                   "generation": original.get("generation", 1), "ready_at": original.get("ready_at"),
                   "case_key": item.get("key")}
        current["first_ready_at"] = current["ready_at"]
        current.update({k: original[k] for k in ARRIVAL_KEYS if k in original})
        item["measurement"] = current
    return current


def invalidate_measurement_post(item):
    current = measurement_for(item)
    for key in ("posted_at", "posting_source", "approval_observed_at"):
        current.pop(key, None)


def next_measurement(item, ready_at=None, source=None):
    """A new proposal generation; existing revision and approval behavior are untouched."""
    previous = dict(measurement_for(item))
    history = item.get("measurement_history")
    if not isinstance(history, list):
        history = []
        item["measurement_history"] = history
    history.append(previous)
    generation = previous.get("generation", 1)
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        generation = 1
    current = {"schema": 1, "source": source or previous.get("source", "legacy"),
               "generation": generation + 1, "ready_at": ready_at or measurement_now(),
               "first_ready_at": previous.get("first_ready_at"), "case_key": item.get("key")}
    current.update({k: previous[k] for k in ARRIVAL_KEYS if k in previous})   # the first arrival stays through every generation
    item["measurement"] = current
    item["record"]["measurement"] = dict(current)


def fold_recurring(out, ap, res, source_key=None):
    """R32 (merge_recurring=1, link.same_recurring): a recurrence of a waiting alert joins that item under its own number instead of
    becoming a new one. The item is posted again (revision + 1, `posted` False) with a `再発 HH:MM` followup line. Its status stays
    pending (or held): the PM's OK is still needed, and nothing is approved, executed or sent. `res` is the new alert result, or a
    queue line of this cycle; its full text, if any, is at `source_key` and moves to the item. The caller holds the approvals lock
    and passes the Approvals of its run (items it added and did not save yet are seen). Returns the item's number (str) when joined,
    else None."""
    from . import link
    from .cli import FOLLOWUPS_MAX, _append
    from . import fulltext
    for n, it in ap.data["items"].items():
        rec = it.get("record") if isinstance(it.get("record"), dict) else {}
        if it.get("status") not in ("pending", "held") or not link.same_recurring(rec, res):
            continue
        revision = int(it.get("revision") or rec.get("revision") or 1)
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        updated = copy.deepcopy(res)
        updated["revision"] = revision + 1
        updated["followups"] = (list(rec.get("followups") or []) + [{"text": f"再発 {link.hm_of(res.get('at'))}", "at": at}])[-FOLLOWUPS_MAX:]
        if it.get("status") == "held" or rec.get("held_before"):
            updated["held_before"] = True
        key = it["key"]
        if source_key and source_key != key:
            latest = fulltext.load(out, source_key)
            fulltext.drop_safe(out, key)   # the superseded full text goes, as in a follow-up of a chat
            if latest:
                fulltext.save(out, key, latest, request=latest.get("request"), material=latest.get("material"))
            fulltext.drop_safe(out, source_key)
        preserve = {name: it[name] for name in FOLLOWUP_KEEP if name in it}
        if it.get("delivery_unknown") and "delivery_unknown_revision" not in preserve:
            preserve["delivery_unknown_revision"] = revision
        it.clear()
        it.update(preserve)
        it.update({"revision": revision + 1, "status": "pending", "posted": False, "toasted": False,
                   "request_posted": False, "record": updated})
        next_measurement(it, ready_at=res.get("measurement", {}).get("ready_at"),
                         source=res.get("measurement", {}).get("source"))
        res["measurement"] = dict(it["measurement"])
        ap.save(compact=True)   # the stale draft is replaced: no backup of it is kept
        _append(Path(out) / "merged.jsonl", {"at": at, "into": key, "event_id": res.get("event_id"), "revision": revision + 1})
        return str(n)
    return None


def _key(rec):
    return f"{rec.get('graph')}:{rec.get('event_id')}:{rec.get('node')}"


def untoasted(out):
    """(numbers of pending items posted but not yet announced on the PC, notice keys likewise)."""
    ap = Approvals(out)
    nums = [int(n) for n, it in ap.data["items"].items()
            if it["posted"] and it["status"] in ("pending", "held") and not it.get("toasted")]
    seen = set(ap.data.get("notices_toasted", []))
    return sorted(nums), [k for k in ap.data.get("notices", []) if k not in seen]


def unposted_announced(out):
    """The number of unposted items the PC notification last named (0 when none, or after they were posted)."""
    return int(Approvals(out).data.get("unposted_announced", 0))


def lock_path(out):
    """The one lock every read-modify-write of approvals.json takes (collect, notify, notify_notices, mark_toasted, purge, the merge of a
    follow-up message). Not re-entrant: what is called with it held is a `_..._locked` body."""
    return Path(out) / "approvals.lock"


def mark_toasted(out, nums, notice_keys, unposted=None, sitting=None):
    """Record what the PC notification announced. False (nothing changed, so it is announced again next time) when the lock is held.
    `sitting` (R17, interrupt=sittings): the sitting whose announcement this is, recorded as announced."""
    with fsutil.exclusive(lock_path(out)) as got:
        if not got:
            return False
        _mark_toasted_locked(out, nums, notice_keys, unposted, sitting)
        return True


def sitting_toast_due(out, at):
    """R17: True unless the PC notification has already announced the sitting `at` (approvals.json `sitting_toast` holds the last one it
    did). A PC with no record yet takes the sitting that is current as announced: what waits is told at the next one. When that cannot
    be written (the lock is held), the sitting counts as due, so nothing waits forever."""
    last = Approvals(out).data.get("sitting_toast")
    if last is None:
        return not mark_toasted(out, [], [], sitting=at)
    return str(last) != at


def delivery_pending(out):
    """List sends whose result is ambiguous; the list contains targets and times, never post bodies."""
    out = Path(out)
    with fsutil.exclusive(lock_path(out)) as got:
        if not got:
            return fsutil.BusyList.busy_result()
        ap = Approvals(out)
        rows = []
        for num, item in ap.data.get("items", {}).items():
            if item.get("delivery_unknown"):
                rows.append({"kind": "case", "target": str(num), "part": item.get("delivery_unknown_part", "post"),
                             "at": item.get("delivery_unknown_at")})
        uncertain = ap.data.get("notice_delivery_unknown", {})
        if isinstance(uncertain, list):       # records written by the first delivery-evidence version
            uncertain = {key: None for key in uncertain}
        for key, at in uncertain.items():
            rows.append({"kind": "notice", "target": str(key), "at": at})
        if ap.data.get("outbox_delivery_unknown"):
            rows.append({"kind": "outbox", "target": "outbox", "at": ap.data.get("outbox_delivery_unknown_at")})
        daily = fsutil.read_json(out / "daily_state.json", {})
        pending_brief = daily.get("brief_delivery_unknown")
        if isinstance(pending_brief, dict):
            rows.append({"kind": "brief", "target": "brief", "at": pending_brief.get("at"),
                         "date": pending_brief.get("date")})
        return rows


def retry_delivery(out, kind, target, confirm_not_sent=False, source=None):
    """Release one ambiguous delivery only after an explicit claim that it was not sent.

    This changes no posted/delivered state and does not send anything. The normal
    posting path may retry only after the caller supplies that confirmation.
    """
    if confirm_not_sent is not True:
        return {"status": "confirmation_required"}
    out = Path(out)
    with fsutil.exclusive(lock_path(out)) as got:
        if not got:
            return {"status": "busy"}
        ap = Approvals(out)
        changed = False
        if kind == "case":
            num = str(target)
            item = ap.data.get("items", {}).get(num)
            if item and item.get("delivery_unknown"):
                _clear_case_attempt(item)
                changed = True
        elif kind == "notice":
            uncertain = ap.data.get("notice_delivery_unknown", {})
            if isinstance(uncertain, list):
                uncertain = {key: None for key in uncertain}
            if str(target) in uncertain:
                uncertain.pop(str(target), None)
                ap.data["notice_delivery_unknown"] = uncertain
                ap.data.setdefault("notice_delivery_attempts", {}).pop(str(target), None)
                changed = True
        elif kind == "outbox" and str(target) == "outbox" and ap.data.get("outbox_delivery_unknown"):
            ap.data.pop("outbox_delivery_unknown", None)
            ap.data.pop("outbox_delivery_unknown_at", None)
            ap.data.pop("outbox_delivery_attempt", None)
            changed = True
        elif kind == "brief" and str(target) == "brief":
            state_path = out / "daily_state.json"
            state = fsutil.read_json(state_path, {})
            if state.pop("brief_delivery_unknown", None) is not None:
                fsutil.write_atomic(state_path, json.dumps(state, ensure_ascii=False))
                state_path.with_name(state_path.name + ".bak").unlink(missing_ok=True)
                changed = True
        if not changed:
            return {"status": "not_pending"}
        if not (kind == "brief" and str(target) == "brief"):
            ap.save()
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log = out / "delivery-recovery.log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": at, "kind": kind, "target": str(target), "action": "confirmed_not_sent", **({"source": source} if source else {})},
                               ensure_ascii=False) + "\n")
        return {"status": "released", "kind": kind, "target": str(target)}


def delivery_show(out, kind, target):
    """Return the exact text for a pending ambiguous delivery, for human inspection."""
    out = Path(out)
    ap = Approvals(out)
    if kind == "case":
        item = ap.data.get("items", {}).get(str(target))
        if not item or not item.get("delivery_unknown"):
            return None
        attempt = item.get("delivery_attempt")
        if isinstance(attempt, dict) and isinstance(attempt.get("body"), str):
            return attempt["body"]
        revision = item.get("delivery_unknown_revision", item.get("revision", 1))
        part = item.get("delivery_unknown_part", "post")
        return f"[kimeru #{target} {part} / revision {revision}]\n[attempted body unavailable in legacy delivery state]"
    if kind == "outbox" and str(target) == "outbox" and ap.data.get("outbox_delivery_unknown"):
        attempt = ap.data.get("outbox_delivery_attempt")
        if isinstance(attempt, dict) and isinstance(attempt.get("body"), str):
            return attempt["body"]
        return "[outbox / attempted revision unavailable]\n[attempted body unavailable in legacy delivery state]"
    if kind == "notice":
        uncertain = ap.data.get("notice_delivery_unknown", {})
        if isinstance(uncertain, list):
            uncertain = {key: None for key in uncertain}
        key = str(target)
        if key not in uncertain:
            return None
        attempt = (ap.data.get("notice_delivery_attempts") or {}).get(key)
        if isinstance(attempt, dict) and isinstance(attempt.get("body"), str):
            return attempt["body"]
        return f"[notice {key} / attempted revision unavailable]\n[attempted body unavailable in legacy delivery state]"
    if kind == "brief" and str(target) == "brief":
        state = fsutil.read_json(out / "daily_state.json", {})
        pending = state.get("brief_delivery_unknown")
        if not isinstance(pending, dict):
            return None
        if isinstance(pending.get("body"), str):
            return pending["body"]
        if "body" not in pending and isinstance(pending.get("text"), str):
            return pending["text"]
        return "[brief / attempted revision unavailable]\n[attempted body unavailable in legacy delivery state]"
    return None


def deliver_brief(out, bridge, text, send, date, built_at=None):
    """Post a brief with the same explicit delivery evidence and recovery hold used by daily. `built_at` (ISO, UTC): when the
    brief was put together; it is kept as `brief_at` so that the next brief counts what happened since then."""
    if not send:
        bridge.post(text, False)
        return {"status": "pasted"}
    out = Path(out)
    with fsutil.exclusive(lock_path(out)) as got:
        if not got:
            return {"status": "busy"}
        state_path = out / "daily_state.json"
        state = fsutil.read_json(state_path, {})
        if state.get("brief_delivery_unknown"):
            return {"status": "delivery_unknown"}
        if state.get("brief_date") == date:
            return {"status": "already_delivered"}
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        state["brief_delivery_unknown"] = {"date": date, "at": at, "revision": date, "built_at": built_at or at,
                                           "part": "brief", "identity": date, "body": text, "text": text}
        fsutil.write_atomic(state_path, json.dumps(state, ensure_ascii=False))
        state_path.with_name(state_path.name + ".bak").unlink(missing_ok=True)
        try:
            result = bridge.post(text, True)
            error_data = None
        except Exception as e:
            result = getattr(e, "data", None)
            error_data = result
        if _delivered(result, True):
            state["brief_date"] = date
            state["brief_at"] = built_at or at
            state.pop("brief_delivery_unknown", None)
            fsutil.write_atomic(state_path, json.dumps(state, ensure_ascii=False))
            state_path.with_name(state_path.name + ".bak").unlink(missing_ok=True)
            return {"status": "delivered"}
        if _delivery_unknown(error_data if error_data is not None else result, True):
            fsutil.write_atomic(state_path, json.dumps(state, ensure_ascii=False))
            state_path.with_name(state_path.name + ".bak").unlink(missing_ok=True)
            return {"status": "delivery_unknown"}
        state.pop("brief_delivery_unknown", None)
        fsutil.write_atomic(state_path, json.dumps(state, ensure_ascii=False))
        state_path.with_name(state_path.name + ".bak").unlink(missing_ok=True)
        return {"status": "not_sent"}


def confirm_delivery(out, kind, target, confirm_delivered=False, source="user_confirmation"):
    """Resolve an ambiguous result from the user's explicit delivery confirmation."""
    if confirm_delivered is not True:
        return {"status": "confirmation_required"}
    out = Path(out)
    with fsutil.exclusive(lock_path(out)) as got:
        if not got:
            return {"status": "busy"}
        ap = Approvals(out)
        changed = False
        if kind == "case":
            item = ap.data.get("items", {}).get(str(target))
            if item and item.get("delivery_unknown"):
                part = item.get("delivery_unknown_part", "post")
                uncertain_revision = item.get("delivery_unknown_revision", item.get("revision", 1))
                if uncertain_revision == item.get("revision", 1):
                    if part == "post":
                        item["posted"], item["toasted"] = True, False
                        timing = measurement_for(item)
                        if item.get("delivery_unknown_generation") == timing.get("generation"):
                            timing["posting_source"] = source
                            timing["posting_confirmed_at"] = measurement_now()
                            timing.pop("posted_at", None)  # the original send time is unknown
                    elif part == "copilot_request":
                        item["request_posted"] = True
                # An older revision may have been delivered, but it cannot mark the
                # current revised proposal as posted or approved.
                _clear_case_attempt(item)
                changed = True
        elif kind == "notice":
            uncertain = ap.data.get("notice_delivery_unknown", {})
            if isinstance(uncertain, list):
                uncertain = {key: None for key in uncertain}
            if str(target) in uncertain:
                uncertain.pop(str(target), None)
                ap.data["notice_delivery_unknown"] = uncertain
                ap.data.setdefault("notice_delivery_attempts", {}).pop(str(target), None)
                if str(target) not in ap.data.get("notices", []):
                    ap.data.setdefault("notices", []).append(str(target))
                changed = True
        elif kind == "outbox" and str(target) == "outbox" and ap.data.get("outbox_delivery_unknown"):
            box = list(ap.data.get("outbox") or [])
            attempt = ap.data.get("outbox_delivery_attempt")
            if isinstance(attempt, dict):
                if not isinstance(attempt.get("body"), str) or not box or box[0] != attempt["body"]:
                    return {"status": "attempt_mismatch"}
            if box:
                handoff = re.match(r"^\[kimeru (?:送信用|本文) #(\d+)\]", str(box[0]))
                if handoff:
                    item = ap.data.get("items", {}).get(handoff.group(1))
                    if item:
                        prior = item.get("handoff_unknown") or {}
                        if isinstance(attempt, dict) and (
                                not isinstance(attempt.get("revision"), int)
                                or str(attempt.get("identity")) != handoff.group(1)):
                            return {"status": "attempt_mismatch"}
                        evidence_revision = (attempt.get("revision") if isinstance(attempt, dict)
                                             else prior.get("revision", item.get("revision", 1)))
                        item.setdefault("handoff_evidence", []).append({
                            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "revision": evidence_revision,
                            "identity": (attempt.get("identity") if isinstance(attempt, dict)
                                         else item.get("key")),
                            "source": "user_confirmation"})
                        item.pop("handoff_unknown", None)
            ap.data["outbox"] = box[1:]
            ap.data.pop("outbox_delivery_unknown", None)
            ap.data.pop("outbox_delivery_unknown_at", None)
            ap.data.pop("outbox_delivery_attempt", None)
            changed = True
        elif kind == "brief" and str(target) == "brief":
            state_path = out / "daily_state.json"
            state = fsutil.read_json(state_path, {})
            pending = state.pop("brief_delivery_unknown", None)
            if isinstance(pending, dict):
                state["brief_date"] = pending.get("date")
                state["brief_at"] = pending.get("built_at") or pending.get("at")
                fsutil.write_atomic(state_path, json.dumps(state, ensure_ascii=False))
                state_path.with_name(state_path.name + ".bak").unlink(missing_ok=True)
                changed = True
        if not changed:
            return {"status": "not_pending"}
        # Brief state was saved above; all other state is saved in approvals.json.
        if not (kind == "brief" and str(target) == "brief"):
            ap.save()
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log = out / "delivery-recovery.log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": at, "kind": kind, "target": str(target),
                                "action": "confirmed_delivered", "source": source},
                               ensure_ascii=False) + "\n")
        return {"status": "confirmed", "kind": kind, "target": str(target)}


def close_delivery(out, kind, target, source="reconcile"):
    """Close an old ambiguous delivery that was NOT seen in the chat, without sending it again and without approving anything.

    A case stays pending and counts as posted, so it is not sent now and shows in the morning brief (a reply `OK N` still works);
    a notice or an outbox message is dropped; a brief is released so that a new one can go out. Nothing is marked delivered."""
    out = Path(out)
    with fsutil.exclusive(lock_path(out)) as got:
        if not got:
            return {"status": "busy"}
        ap = Approvals(out)
        changed = False
        if kind == "case":
            item = ap.data.get("items", {}).get(str(target))
            if item and item.get("delivery_unknown"):
                part = item.get("delivery_unknown_part", "post")
                same = item.get("delivery_unknown_revision", item.get("revision", 1)) == item.get("revision", 1)
                if same and part == "post":
                    item["posted"], item["toasted"] = True, True   # not announced on the PC: nothing new was posted
                    timing = measurement_for(item)
                    timing["posting_source"] = "closed_not_delivered"
                    timing.pop("posted_at", None)
                elif same and part == "copilot_request":
                    item["request_posted"] = True
                item["delivery_closed"] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "part": part}
                _clear_case_attempt(item)
                changed = True
        elif kind == "notice":
            uncertain = ap.data.get("notice_delivery_unknown", {})
            if isinstance(uncertain, list):
                uncertain = {key: None for key in uncertain}
            key = str(target)
            if key in uncertain:
                uncertain.pop(key, None)
                ap.data["notice_delivery_unknown"] = uncertain
                ap.data.setdefault("notice_delivery_attempts", {}).pop(key, None)
                if key not in ap.data.get("notices", []):
                    ap.data.setdefault("notices", []).append(key)
                if key not in ap.data.get("notices_toasted", []):
                    ap.data.setdefault("notices_toasted", []).append(key)
                changed = True
        elif kind == "outbox" and str(target) == "outbox" and ap.data.get("outbox_delivery_unknown"):
            box = list(ap.data.get("outbox") or [])
            attempt = ap.data.get("outbox_delivery_attempt")
            if isinstance(attempt, dict) and isinstance(attempt.get("body"), str) and (not box or box[0] != attempt["body"]):
                return {"status": "attempt_mismatch"}
            ap.data["outbox"] = box[1:]
            for name in ("outbox_delivery_unknown", "outbox_delivery_unknown_at", "outbox_delivery_attempt"):
                ap.data.pop(name, None)
            changed = True
        elif kind == "brief" and str(target) == "brief":
            state_path = out / "daily_state.json"
            state = fsutil.read_json(state_path, {})
            if state.pop("brief_delivery_unknown", None) is not None:
                fsutil.write_atomic(state_path, json.dumps(state, ensure_ascii=False))
                state_path.with_name(state_path.name + ".bak").unlink(missing_ok=True)
                changed = True
        if not changed:
            return {"status": "not_pending"}
        if not (kind == "brief" and str(target) == "brief"):
            ap.save()
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log = out / "delivery-recovery.log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": at, "kind": kind, "target": str(target), "action": "closed_not_delivered",
                                "source": source}, ensure_ascii=False) + "\n")
        return {"status": "closed", "kind": kind, "target": str(target)}


def _mark_toasted_locked(out, nums, notice_keys, unposted=None, sitting=None):
    ap = Approvals(out)
    if unposted is not None:
        if unposted:
            ap.data["unposted_announced"] = unposted
        else:
            ap.data.pop("unposted_announced", None)
    if sitting is not None:
        ap.data["sitting_toast"] = str(sitting)
    for n in nums:
        if str(n) in ap.data["items"]:
            ap.data["items"][str(n)]["toasted"] = True
    if notice_keys:
        ap.data["notices_toasted"] = list(ap.data.get("notices_toasted", [])) + list(notice_keys)
    ap.save()


def unposted_count(out):
    """Pending items that could not be posted to the self chat (counted apart from the posted ones)."""
    ap = Approvals(out)
    return sum(1 for it in ap.data["items"].values() if not it["posted"] and it["status"] == "pending")


def toast_enabled():
    import os
    return config.value("toast") != "0"


def _posted_drafts(out, posted):
    """R12: how many of the posted items wait only for a writer's draft, with post_kind_mark=1; None at 0 (the default)."""
    if config.value("post_kind_mark") != "1":
        return None
    items = Approvals(out).data["items"]
    return sum(1 for n in posted if ((items.get(str(n)) or {}).get("record") or {}).get("held_reason") == "draft")


def toast_text(out, posted, notices, unposted=0, sitting=False):
    """Title and body for the PC notification. Counts and numbers only; KIMERU_TOAST=detail adds the
    first item's one-line summary (on this PC only: the routes that leave it never carry a summary).
    `sitting` (R17): the title says that this is the sitting's summary (（まとめ）)."""
    import os
    parts = []
    if posted:
        drafts = _posted_drafts(out, posted)
        parts.append(f"確認待ち {len(posted)} 件" + (f"（うち下書き {drafts}）" if drafts is not None else ""))
    if notices:
        parts.append(f"自動決定の通知 {notices} 件")
    if unposted:
        parts.append(f"投稿できていない確認待ち {unposted} 件")
    body = ("#" + ", #".join(str(n) for n in posted[:5]) if posted else "") or "Teams の自分とのチャットを確認してください"
    if config.value("toast") == "detail" and posted:
        rec = (Approvals(out).data["items"].get(str(posted[0])) or {}).get("record", {})
        body += " " + str(rec.get("summary") or rec.get("advice") or rec.get("graph") or "")[:60]
    return "kimeru: " + " / ".join(parts) + ("（まとめ）" if sitting and parts else ""), body


ACK = {"approved": "承認", "rejected": "却下", "held": "保留", "expired": "期限切れ", "ask_back": "聞き返し",
       "redrafted": "書き直し", "redraft_failed": "書き直せず", "redraft_limit": "修正は上限まで", "redo": "再実行", "redo_ignored": "再実行（無視: 古い回数）", "redo_ahead": "再実行（回数が大きすぎます。その回数の結果が出たら 1 回だけ効きます）", "redo_malformed": "再実行（形が違います）", "closed": "済（閉じました）", "detail": "詳細を返信",
       "reply_ignored": "返信を読めませんでした（投稿が画面から流れたため、もう一度投稿します。承認はしていません）",
       "reposted": "文面を確認して OK し直してください",
       "learned": "学習を採用しました（基準を変えました）", "learn_stale": "学習（基準が変わっていたため採用せず）",
       "sent_confirmed": "送った（催促を止めました）", "reply_send": "送る（結果は自分とのチャットへ）"}


def ack_text(changes):
    """One PC notification line for the replies just applied (a reply is never silently swallowed)."""
    parts = [f"#{c['id']} {ACK.get(c['status'], c['status'])}" for c in changes]
    return "kimeru: 返事を受け付けました", " / ".join(parts[:6]) + (f" ほか {len(parts) - 6} 件" if len(parts) > 6 else "")


def show_toast(title, body):
    """Windows notification on this PC (tools/toast.ps1). Opt out with KIMERU_TOAST=0."""
    import os
    if config.value("toast") == "0":
        return "off"
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(HERE / "tools" / "toast.ps1"), "-Title", title, "-Body", body],
                       capture_output=True, text=True, encoding="utf-8", timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError((r.stdout + r.stderr).strip()[:200])
    return "shown"


def format_notice(rec, linked=None):
    """A notice of an automatic decision, at most 4 lines: what it is about, what was done, a link. `linked` (R19, link.notice_note):
    {"prefix": the 再発火 text in front of the first line, "tail": the `↔` text at the end of the arrow line}; None (link_related=0)
    adds nothing, and the notice is the same text as before."""
    kind = str(rec.get("event_kind") or "")
    what = _clip(_ado_label(rec), 80) if kind.startswith("ado.") else _clip(rec.get("summary") or rec.get("advice") or kind, 80)
    lines = [f"[kimeru 通知] {(linked or {}).get('prefix') or ''}{what}{_others(rec)}"]
    ran = list(dict.fromkeys(WHAT_RAN.get(e["action"].get("type"), "記録") for e in rec.get("executed", [])))
    note = _clip(rec["advice"], 120) if rec.get("advice") else "自動で決定しました"
    tail = (linked or {}).get("tail") or ""
    lines.append("→ " + note + (f"（記録: {', '.join(ran)}）" if ran else "") + (f" {tail}" if tail else ""))
    link = next((l for l in ado_lines(rec) if l.startswith("リンク: ")), "")
    if link:
        lines.append(link[len("リンク: "):])
    if rec.get("needs_human"):   # the same item also waits in its own numbered post (a draft to approve)
        lines.append("下書きは別の投稿で承認待ちです")
    return "\n".join(lines)


def notify_notices(out, bridge, send=False):
    """Post automatic decisions the PM should know about (notices.jsonl), each once. When the lock is held, nothing is done
    and the result has `busy` set."""
    out = Path(out)
    with fsutil.exclusive(lock_path(out)) as got:
        if not got:
            return fsutil.BusyList.busy_result()
        return fsutil.BusyList(_notify_notices_locked(out, bridge, send))


def _notify_notices_locked(out, bridge, send):
    ap = Approvals(out)
    from . import link as linking   # R19 (link_related=1): the records read once for the batch; nothing is read at 0
    related = linking.index(out, ap.data["items"]) if linking.enabled() else None
    done = set(ap.data.setdefault("notices", []))
    uncertain = ap.data.setdefault("notice_delivery_unknown", {})
    if isinstance(uncertain, list):
        uncertain = ap.data["notice_delivery_unknown"] = {key: None for key in uncertain}
    q = out / "notices.jsonl"
    rows = [json.loads(l) for l in q.read_text(encoding="utf-8").splitlines() if l.strip()] if q.exists() else []
    posted = []
    for rec in rows:
        key = _key(rec)
        if key in done or key in uncertain:
            continue
        fsutil.refresh(lock_path(out))
        body = format_notice(rec, linking.notice_note(related, rec))   # R19: None at link_related=0
        if send:
            attempt = _attempt_record("notice", key, "notice", body, rec.get("revision"), key)
            ap.data["notice_delivery_attempts"] = ap.data.get("notice_delivery_attempts") or {}
            ap.data["notice_delivery_attempts"][key] = attempt
            ap.data["notice_delivery_unknown"][key] = attempt["at"]
            ap.save()
        try:
            r = bridge.post(body, send) or {}
        except Exception as e:
            if send and not _delivery_unknown(getattr(e, "data", None), True):
                ap.data["notice_delivery_unknown"].pop(key, None)
                ap.data.setdefault("notice_delivery_attempts", {}).pop(key, None)
                ap.save()
            raise
        if send and not _delivered(r, True):
            if _delivery_unknown(r, True):
                ap.save()
            else:
                ap.data["notice_delivery_unknown"].pop(key, None)
                ap.data.setdefault("notice_delivery_attempts", {}).pop(key, None)
                ap.save()
            raise RuntimeError(f"notice {key} was not posted with verified readback")
        if send:
            ap.data["notices"].append(key); done.add(key)
            ap.data["notice_delivery_unknown"].pop(key, None)
            ap.data.setdefault("notice_delivery_attempts", {}).pop(key, None)
            ap.save()
        posted.append(key)
    return posted


def notify(out, bridge, send=False, real=False):
    """Post every not-yet-posted queue item to the self chat. real=True (the daily and approvals paths): the exact text a
    switched-on kind will write, and where, is fixed and shown in the post (execute.freeze).
    Reads and writes approvals.json under the same lock as collect, and keeps it while it posts (an approval typed by hand meanwhile waits
    for the next cycle instead of being overwritten). When the lock is held, nothing is done and the result has `busy` set."""
    out = Path(out)
    with fsutil.exclusive(lock_path(out)) as got:
        if not got:
            return fsutil.BusyList.busy_result()
        return fsutil.BusyList(_notify_locked(out, bridge, send, real))


def _notify_locked(out, bridge, send, real):
    ap = Approvals(out)
    q = out / "queue.jsonl"
    rows = [json.loads(l) for l in q.read_text(encoding="utf-8").splitlines() if l.strip()] if q.exists() else []
    from . import link as linking   # R32 (merge_recurring=1): a recurrence queued in this cycle joins the waiting item of its case
    for rec in rows:
        key = _key(rec)
        if ap.known(key) or key in ap.data.get("folded", {}):   # a folded line stays in queue.jsonl: it is not folded twice
            continue
        joined = fold_recurring(out, ap, rec, source_key=key) if linking.recurring_enabled() else None
        if joined is not None:
            ap.data.setdefault("folded", {})[key] = joined
            continue
        ap.add(key, rec)
    ap.save()   # an item that cannot be posted now must still exist (and be counted as waiting) after a failed post
    posted = []
    from . import fulltext, memory, promises
    from . import link as linking
    related = None   # R19: the records of the same cases, read once when the first item is posted (link_related=1 only; never read at 0)
    past = None   # R16: the earlier decisions, read once when the first item is posted (memory=1 only; never read at 0)
    promise_snap = None   # R18: the open promises, read once when the first item is posted (promises=1 only; never read at 0)
    fulltext.purge_safe(out)   # a full text kept too long is deleted (a failure is noted in warnings.jsonl; the posts go on); the item is then treated as decided from the preview
    ap = Approvals(out)
    for n, it in ap.data["items"].items():
        if it.get("delivery_unknown"):
            continue
        req = it["record"].get("copilot_request")
        if it["posted"] and (not req or it.get("request_posted")):
            continue
        if not it["posted"]:
            from . import execute, fulltext
            execute.freeze(it["record"], real)
            ap.save()
            fsutil.refresh(lock_path(out))
            marks = hold.post_marks(it, ap.data["items"])   # R09: a day that came back, and the undated holds not named yet
            if past is None and memory.enabled():
                past = memory.index(out, ap.data["items"])
            if promise_snap is None and promises.enabled():
                promise_snap = promises.snapshot(out)
            if related is None and linking.enabled():
                related = linking.index(out, ap.data["items"])
            body = format_post(n, it["record"], fulltext.load(out, it["key"]), marks=marks,
                               precedent=memory.precedent(past, it["record"], n),   # R16: None at memory=0
                               promise=promises.post_note(promise_snap, it["record"], n),   # R18: None at promises=0
                               related=linking.post_note(related, it["record"], n))   # R19: None at link_related=0
            if send:
                attempt = _attempt_record("case", n, "post", body, it.get("revision", 1), it["key"])
                it.update({"delivery_unknown": True, "delivery_unknown_part": "post",
                           "delivery_unknown_at": attempt["at"], "delivery_unknown_revision": it.get("revision", 1),
                           "delivery_unknown_generation": measurement_for(it).get("generation"),
                           "delivery_attempt": attempt})
                ap.save()
            try:
                r = bridge.post(body, send) or {}
            except Exception as e:
                if send and not _delivery_unknown(getattr(e, "data", None), True):
                    _clear_case_attempt(it)
                    ap.save()
                raise
            if send and not _delivered(r, True):
                if _delivery_unknown(r, True):
                    ap.save()
                else:
                    _clear_case_attempt(it)
                    ap.save()
                raise RuntimeError(f"#{n} was not posted with verified readback")
            if send:
                it["posted"], it["toasted"] = True, False
                hold.noted(it, ap.data["items"], marks)
                timing = measurement_for(it)
                if not timing.get("posted_at"):
                    timing["posted_at"] = measurement_now()
                    timing["posting_source"] = "bridge_readback"
                _clear_case_attempt(it)
                ap.save()  # a failure on a later item must not forget what was already sent
            posted.append(int(n))
        if req and not it.get("request_posted"):   # its own message: one long-press copies just this
            req = (fulltext.load(out, it["key"]) or {}).get("request") or req   # the request with the whole text is kept apart from the records
            fsutil.refresh(lock_path(out))
            body = f"[kimeru #{n} Copilot 用]" + chr(10) + req
            if send:
                attempt = _attempt_record("case", n, "copilot_request", body, it.get("revision", 1), it["key"])
                it.update({"delivery_unknown": True, "delivery_unknown_part": "copilot_request",
                           "delivery_unknown_at": attempt["at"], "delivery_unknown_revision": it.get("revision", 1),
                           "delivery_attempt": attempt})
                ap.save()
            try:
                r2 = bridge.post(body, send) or {}
            except Exception as e:
                if send and not _delivery_unknown(getattr(e, "data", None), True):
                    _clear_case_attempt(it)
                    ap.save()
                raise
            if send and not _delivered(r2, True):
                if _delivery_unknown(r2, True):
                    ap.save()
                else:
                    _clear_case_attempt(it)
                    ap.save()
                raise RuntimeError(f"#{n} Copilot request was not posted with verified readback")
            if send:
                it["request_posted"] = True
                _clear_case_attempt(it)
                ap.save()
    ap.save()
    return posted


def parse_result_entry(e):
    """("1", 2) for the timeline entry "X:1:2" (the 2nd result post about #1); ("1", 0) for "X:1" (a post of an earlier version,
    which has no count); None for anything else."""
    if not e.startswith("X:"):
        return None
    num, _, k = e[2:].partition(":")
    try:
        return num, int(k) if k else 0
    except ValueError:
        return num, 0


def fresh_entries(read, x_boundary=True):
    """[(reply line, results seen)] for the replies that come after the most recent visible post with the same number.
    `results seen` is the highest count k of the result posts "[kimeru 実行 #N k]" of that number that stand before the reply on the
    screen (0 when none does).

    Old "OK 1" lines from an earlier run stay in the chat; without this, a new
    item #1 would be approved by them. With a timeline, a reply counts only if
    it appears after the last "[kimeru #1]" post (or, unless x_boundary is False, after the last result post
    "[kimeru 実行 #1 k]", so that an answered `再実行 1` does not act again); if that post is not visible,
    freshness cannot be proven and the reply is ignored.
    """
    tl = scoped_timeline(read)
    if tl is None:  # older bridge without ordering
        return [(line, 0) for line in read.get("replies", [])]
    last_post, last_result, last_detail = {}, {}, {}
    for i, e in enumerate(tl):
        if e.startswith("D:"):   # a detail post "[kimeru 詳細 #N]": it answers the `詳細 N` replies above it, and nothing else
            last_detail[e[2:]] = i
        x = parse_result_entry(e)
        if e.startswith("P:") or (x_boundary and x):   # "X:N:k" is a result post ("[kimeru 実行 #N k]"): a reply before it is answered
            last_post[x[0] if x else e[2:]] = i
        if x:
            last_result[x[0]] = i
    if not x_boundary:   # the approval post may be off the screen: a result post is a boundary too (after it, a reply is new)
        for num, i in last_result.items():
            last_post.setdefault(num, i)
    out = []
    top = {}   # the highest result count seen so far, per item
    for i, e in enumerate(tl):
        x = parse_result_entry(e)
        if e.startswith("P:"):
            top[e[2:]] = 0   # the counts are taken after the last approval post of that number only: when numbers are used again
        if x:                # (a state folder made new), the result posts of the earlier item above it are not this item's
            top[x[0]] = max(top.get(x[0], 0), x[1])
        if e.startswith("R:"):
            r = parse_reply(e[2:]) or parse_redraft(e[2:]) or parse_paste(e[2:]) or parse_label(e[2:]) or hold.parse_hold_until(e[2:]) or _not_needed_reply(e[2:])
            num = r and (r[1] if r[0] in STATUS else r[0])
            if num and i > last_post.get(num, len(tl)):
                r0 = parse_reply(e[2:])
                if r0 and r0[0] == "詳細" and i < last_detail.get(num, -1):
                    continue   # answered already
                out.append((e[2:], top.get(num, 0)))
    return out


def unseen_reply_numbers(read):
    """[(number, reply line)] for the decision replies ("OK 5", "NG 5", 保留, 聞き返し, 修正, 不要) of a number none of whose posts "[kimeru #N]" /
    "[kimeru 実行 #N k]" is on the screen, in order, once per number. fresh_entries leaves such a reply out (freshness cannot be proven);
    this names them, so that it is not dropped without a word. The test reader and a reader with no timeline get nothing; a number
    whose last post is a test post is not listed (scoped_timeline leaves its replies out)."""
    if test_mode():
        return []
    tl = scoped_timeline(read)
    if tl is None:
        return []
    shown = set()
    for e in tl:
        if e.startswith("P:"):
            shown.add(e[2:])
        else:
            x = parse_result_entry(e)
            if x:
                shown.add(x[0])
    out, seen = [], set()
    for e in tl:
        if not e.startswith("R:"):
            continue
        line = e[2:]
        r = parse_reply(line)
        if r and r[0] in ("OK", "NG", "保留", "聞き返し", "不要"):
            num = r[1]
        else:
            r = parse_redraft(line) or hold.parse_hold_until(line)
            num = r and r[0]
        if num and num not in shown and num not in seen:
            seen.add(num)
            out.append((num, line))
    return out


def _redo_lines(read):
    """[(index, reply text)] of the replies that may name a `再実行 N-k`, and the index of the last approval post of each number."""
    tl = scoped_timeline(read)
    if tl is None:
        return [(0, line) for line in read.get("replies", [])], None
    return [(i, e[2:]) for i, e in enumerate(tl) if e.startswith("R:")], {e[2:]: i for i, e in enumerate(tl) if e.startswith("P:")}


def redo_k_entries(read):
    """[(number, k)] for the `再実行 N-k` replies on the screen, in order, once each. Unlike a bare `再実行 N` they do not depend on which
    result posts are visible: k names the result post the PM answered. Only a reply that stands above the most recent visible
    approval post "[kimeru #N]" of the same number is left out (an item that used the number before)."""
    lines, last_post = _redo_lines(read)
    out = []
    for i, line in lines:
        r = parse_redo_k(line)
        if r and (last_post is None or i > last_post.get(r[0], -1)) and r not in out:
            out.append(r)
    return out


def redo_bad_kinds(read):
    """[(number, kind)] for the replies that begin `再実行 N` but are not in a form kimeru reads (same rule as redo_k_entries), once per kind."""
    lines, last_post = _redo_lines(read)
    out = []
    for i, line in lines:
        r = redo_bad_kind(line)
        if r and (last_post is None or i > last_post.get(r[0], -1)) and r not in out:
            out.append(r)
    return out


def redo_bad_entries(read):
    """[number] for the same replies, once per number."""
    out = []
    for n, _ in redo_bad_kinds(read):
        if n not in out:
            out.append(n)
    return out


def fresh_replies(read, x_boundary=True):
    """The reply lines of fresh_entries."""
    return [line for line, _ in fresh_entries(read, x_boundary)]


def fresh_sent(read):
    """[number] for the `送った N` replies on the screen, once each (R28). A reply counts when it comes after the most recent visible
    approval post "[kimeru #N]" of its number, or after the most recent visible nudge "[kimeru 未送信 #N ...]" that names N (the nudge
    is posted 2 hours or more after the OK, when the approval post has often scrolled off; the PM answers under the nudge). A result post
    is no boundary (the send stays true when the result comes). A reply above both does not count, and a nudge for another number is no
    boundary. fresh_entries and the redo boundaries do not look at these lines, so they are the same with or without them.
    [] for an older reader (nothing is proven)."""
    tl = scoped_timeline(read)
    if tl is None:
        return []
    last_post, last_nudge = {}, {}
    for i, e in enumerate(tl):
        if e.startswith("P:"):
            last_post[e[2:]] = i
        elif e.startswith("U:"):   # R28: the nudge's numbers, comma-separated (teams-self.ps1 hands "U:3,5" for "[kimeru 未送信 #3 #5]")
            for num in filter(None, e[2:].split(",")):
                last_nudge[num] = i
    out = []
    for i, e in enumerate(tl):
        if e.startswith("R:"):
            num = parse_sent(e[2:])
            if num is None or num in out:
                continue
            if i > last_post.get(num, len(tl)) or i > last_nudge.get(num, len(tl)):
                out.append(num)
    return out


INSTRUCTION_MAX = 40   # a longer instruction is about one message, not a preference
_INSTRUCTION_LINK = re.compile(r"https?:|ftp:|www\.|@", re.IGNORECASE)


def _plain(text):
    """NFKC, casefolded, without spaces, marks and symbols: the form that is compared and hashed."""
    s = unicodedata.normalize("NFKC", str(text)).casefold()
    return "".join(c for c in s if not c.isspace() and unicodedata.category(c)[0] not in "PSCZ")


def _pairs(s):
    return {s[i:i + 2] for i in range(len(s) - 1)}


def instruction_sig(instruction, event=None):
    """A 12-digit fingerprint of a revision instruction that is a short preference (the same wording written again gives the
    same value), or None. None for a figure, a link, `@`, what writer.TOKENS reads as a date, time, number, ticket or name,
    a wording over 40 characters, and a wording that shares two characters in a row with the colleague's name or text.
    The words themselves are never kept."""
    text = unicodedata.normalize("NFKC", str(instruction or "")).strip()
    plain = _plain(text)
    if not plain or len(text) > INSTRUCTION_MAX or re.search(r"\d", text) or _INSTRUCTION_LINK.search(text):
        return None
    if writer_mod.TOKENS.search(text):
        return None
    ev = event if isinstance(event, dict) else {}
    quoted = _pairs(_plain(ev.get("author") or "")) | _pairs(_plain(ev.get("text") or ""))
    if quoted & _pairs(plain):
        return None
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()[:12]


STALE_ACTION_KEYS = ("drafted_by", "held_for", "writer_warning", "variant", "ask_back", "ask_back_unverified", "unverified")
STALE_RECORD_KEYS = ("writer_error", "copilot_request", "copilot_sources", "memo", "redraft_note", "style_used")


def _redraft(it, instruction, writer, out=None, n=None, now=None):
    """Write the item again with the PM's instruction. Returns the change to log; the item is posted
    again under the same number either way (a failure says so and keeps the previous draft). `now` (R36): the clock of the
    earlier-decisions lookup (the cycle's clock from _collect; None: now)."""
    rec = it["record"]
    it["posted"], it["status"], it["request_posted"] = False, "pending", False
    hold.clear(it)
    if writer is None or not rec.get("material_event"):
        invalidate_measurement_post(it)
        rec["redraft_note"] = "修正できませんでした（writer が設定されていません）。前の下書きのままです"
        return {"status": "redraft_failed", "instruction": instruction}
    limit = config.int_value("redraft_max")
    if limit > 0 and int(it.get("redraft_count") or 0) >= limit:   # T9 (d): past the limit the writer is not called; the post says so
        invalidate_measurement_post(it)
        rec["redraft_note"] = f"修正は {limit} 回までです。前の下書きのままです"
        return {"status": "redraft_limit", "instruction": instruction}
    it["redraft_count"] = int(it.get("redraft_count") or 0) + 1   # a call of the writer, whether it works or not
    before = copy.deepcopy(rec)
    # the draft that was posted (its text, before the stale keys go): the writer gets it as data when redraft_prev=1 (R23)
    previous = {k: a[writer_mod.FIELD[a["type"]]] for k, a in writer_mod.targets(rec)
                if a.get("drafted_by") and isinstance(a.get(writer_mod.FIELD[a["type"]]), str)}
    from . import memory   # local: memory loads notify lazily as well
    # R36 (writer_precedent=1 only; [] at 0, nothing read): the case's earlier decisions, from the record as it stands (n: this item)
    precedent = memory.writer_lines(out, rec, n, now=now) if out is not None else []
    for a in rec.get("actions", []):
        for k in STALE_ACTION_KEYS:
            a.pop(k, None)
    for k in STALE_RECORD_KEYS:
        rec.pop(k, None)
    material = dict(rec["material_event"])
    from . import fulltext
    full = fulltext.load(out, it["key"]) if out is not None else None
    if full:   # the writer works from the whole text while the item waits
        material.update(full.get("material") or {})
        if full.get("text"):
            material["text"] = full["text"]
        if full.get("thread"):
            material["thread"] = full["thread"]
    keep_history = config.value("redraft_history") == "1"   # T9 (c): the earlier instructions of this item go to the writer; kept in the item until it closes
    earlier = [h for h in (it.get("redraft_history") or []) if isinstance(h, str)] if keep_history else []
    drafted = writer_mod.apply(rec, material, writer, instruction, previous=previous, precedent=precedent, history=earlier)
    used = [a for a in drafted if a.get("drafted_by")]
    manual = bool(getattr(writer, "PROMPT_ONLY", False)) and bool(rec.get("copilot_request"))
    if manual:   # writer=m365: nothing is drafted here; a new request (with the instruction and the draft as it stands) goes to the PM
        for _, a in writer_mod.targets(rec):
            if a.get("template_text") is not None:   # back to the template: the pasted draft is in the request now
                a[writer_mod.FIELD[a["type"]]] = a["template_text"]
    if not used and not manual:
        invalidate_measurement_post(it)
        why = rec.get("writer_error") or "使える下書きが返りませんでした"
        rec.clear()
        rec.update(before)
        rec["redraft_note"] = f"修正できませんでした（{str(why)[:60]}）。前の下書きのままです"
        return {"status": "redraft_failed", "instruction": instruction}
    if full and rec.get("copilot_request"):   # the record keeps the request built from the excerpt; the whole text stays in full_text.json
        fulltext.set_request(out, it["key"], fulltext.redact_request(rec, material, instruction, previous=previous if manual else None))
    next_measurement(it)
    if keep_history:
        it["redraft_history"] = (earlier + [str(instruction)[:writer_mod.HISTORY_CHARS]])[-writer_mod.HISTORY_MAX:]
    return {"status": "redrafted", "instruction": instruction}


def _apply_paste(it, text):
    """The PM pasted Copilot's text: it becomes the first message the item would send (reply / post / comment).
    The item is posted again under the same number, with the usual checks (unusable text, dates and numbers
    that are not in the source). Returns the change to log."""
    rec = it["record"]
    it["posted"], it["status"], it["request_posted"] = False, "pending", False
    hold.clear(it)
    text = writer_mod.strip_citations(text)[:MAX_PASTE]
    target = next((a for a in rec.get("actions", []) if a.get("type") in ("teams.reply", "teams.post", "ado.comment")), None)
    if target is None:
        invalidate_measurement_post(it)
        rec["redraft_note"] = "貼り付けた文面を入れる先（返信・投稿・コメント）がありません。前のままです"
        return {"status": "paste_failed", "why": "no target"}
    why = writer_mod.unusable(text)
    if why:
        invalidate_measurement_post(it)
        rec["redraft_note"] = f"貼り付けた文面は使えません（{why}）。前のままです"
        return {"status": "paste_failed", "why": why}
    field = writer_mod.FIELD[target["type"]]
    target.setdefault("template_text", target.get(field))
    target[field] = text
    target["drafted_by"] = "m365（貼り付け）"
    material = writer_mod._material(rec, rec.get("material_event") or {}, None)
    bad = writer_mod.unverified(text, material)
    if bad:
        target["unverified"] = bad
    else:
        target.pop("unverified", None)
    for a in rec.get("actions", []):
        a.pop("held_for", None)
        a.pop("writer_warning", None)
    rec.pop("copilot_request", None)
    rec.pop("redraft_note", None)
    next_measurement(it)
    return {"status": "pasted"}


def _delivered(r, send):
    """A post counts as delivered only with typed, sent and identity-backed readback evidence."""
    if not send or not isinstance(r, dict):
        return False
    readback = r.get("readback")
    return (r.get("ok") is True and r.get("typed") is True and r.get("sent") is True
            and isinstance(readback, dict) and readback.get("matched") is True
            and isinstance(readback.get("message_id"), str) and bool(readback["message_id"].strip()))


def _delivery_unknown(r, send):
    """True when the bridge did not prove either delivery or a definitely unsent result."""
    if not send:
        return False
    if _delivered(r, True):
        return False
    if isinstance(r, dict):
        readback = r.get("readback")
        matched = isinstance(readback, dict) and readback.get("matched") is True
        # Only an explicit no-send result without contrary readback evidence is retryable.
        if r.get("sent") is False and not matched:
            return False
    return True


def _attempt_record(kind, target, part, body, revision=None, identity=None):
    """Immutable evidence of the exact one-message send protected by the caller's state lock."""
    return {"kind": kind, "target": str(target), "part": part, "revision": revision,
            "identity": str(identity if identity is not None else target),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "body": body}


def _clear_case_attempt(item):
    for name in ("delivery_unknown", "delivery_unknown_part", "delivery_unknown_at",
                 "delivery_unknown_revision", "delivery_unknown_generation", "delivery_attempt"):
        item.pop(name, None)


def flush_outbox(ap, bridge, send):
    """Post what waits in the outbox (results, texts ready to copy), once, all together. Only with send=True. What was not
    delivered stays for the next cycle, in order; after the first one that cannot be posted the rest is not tried (Teams is not
    reachable: the attempts grow with the cycles, not with the square of the items). A delivered result post ("[kimeru 実行 #N]")
    is the boundary for the replies before it (see fresh_replies). Returns True when nothing is left."""
    box = list(ap.data.get("outbox") or [])
    if not box or not send:
        return not box
    if ap.data.get("outbox_delivery_unknown"):
        return False
    for k, text in enumerate(box):
        fsutil.refresh(ap.path.with_name("approvals.lock"))
        handoff = re.match(r"^\[kimeru (?:送信用|本文) #(\d+)\]", str(text))
        handoff_item = ap.data.get("items", {}).get(handoff.group(1)) if handoff else None
        revision = handoff_item.get("revision", 1) if handoff_item else None
        identity = handoff.group(1) if handoff else hashlib.sha256(str(text).encode("utf-8")).hexdigest()
        attempt = _attempt_record("outbox", "outbox", "outbox", text, revision, identity)
        ap.data["outbox_delivery_unknown"] = True
        ap.data["outbox_delivery_unknown_at"] = attempt["at"]
        ap.data["outbox_delivery_attempt"] = attempt
        ap.save()
        try:
            result = bridge.post(text, True)
            ok = _delivered(result, True)
            unknown = _delivery_unknown(result, True)
        except Exception as e:
            ok = False
            unknown = _delivery_unknown(getattr(e, "data", None), True)
            result = getattr(e, "data", None)
        if not ok:
            ap.data["outbox"] = box[k:]
            if unknown:
                if handoff and handoff.group(1) in ap.data.get("items", {}):
                    item = ap.data["items"][handoff.group(1)]
                    item.setdefault("handoff_unknown_history", []).append({
                        "at": ap.data["outbox_delivery_unknown_at"], "revision": item.get("revision", 1)})
                    item["handoff_unknown"] = {"at": ap.data["outbox_delivery_unknown_at"],
                                               "revision": item.get("revision", 1)}
            else:
                ap.data.pop("outbox_delivery_unknown", None)
                ap.data.pop("outbox_delivery_unknown_at", None)
                ap.data.pop("outbox_delivery_attempt", None)
            ap.save()
            return False
        if handoff and handoff.group(1) in ap.data.get("items", {}):
            item = ap.data["items"][handoff.group(1)]
            readback = result.get("readback") if isinstance(result, dict) else {}
            item.setdefault("handoff_evidence", []).append({
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "revision": item.get("revision", 1), "message_id": readback.get("message_id", ""),
                "source": "bridge_readback"})
            item.pop("handoff_unknown", None)
        ap.data["outbox"] = box[k + 1:]
        ap.data.pop("outbox_delivery_unknown", None)
        ap.data.pop("outbox_delivery_unknown_at", None)
        ap.data.pop("outbox_delivery_attempt", None)
        ap.save()
    return True


RESULT_POST = re.compile(r"^\[kimeru 実行 #(\d+)(?: \d+)?\]")


def count_result(ap, text):
    """A result post "[kimeru 実行 #N] ..." gets the count of its item in its first line: "[kimeru 実行 #N k] ...", k = 1, 2, 3 ...
    (item["x_seq"] counts the result posts made so far). The reader hands every result post back as "X:N:k", so a `再実行 N`
    reply is judged by which result it follows, not by how many posts or replies the screen happens to show."""
    m = RESULT_POST.match(text)
    it = m and ap.data["items"].get(m.group(1))
    if not it:
        return text
    it["x_seq"] = int(it.get("x_seq", 0)) + 1
    from . import execute
    return f"[kimeru 実行 #{m.group(1)} {it['x_seq']}]" + text[m.end():] + execute.redo_hint(m.group(1), it["x_seq"], text)


def redo_mark(it):
    """The result count that was reached when the last `再実行` of the item acted: a reply that follows no later result post than
    that is already answered. -1 when none acted. An item of an earlier version (redo_seen) has no mark and no counts: it is -1 too,
    so its first new reply acts (at most one run more than before the update; the same-text check guards it) and sets the mark."""
    m = it.get("redo_mark")
    if m is None:
        return -1
    try:
        return int(m)
    except (TypeError, ValueError):
        return -1


def make_post(ap, bridge, send):
    """`post(text)` for execute.py: the text is put in the outbox first; it is posted by one flush_outbox at the end of the step
    (not once per text). Without send it is only pasted (never sent) and stays in the outbox. It never raises.
    A result post is numbered here (count_result)."""
    def post(text):
        text = count_result(ap, text)
        ap.data.setdefault("outbox", []).append(text)
        ap.save()
        if not send:
            try:
                bridge.post(text, False)
            except Exception:
                pass
    return post


def _after_approval(out, ap, num, it, bridge, real=False, send=False):
    """What an approval sets in motion beyond the plan record: with real=True the switched-on kinds are carried out
    (execute.py), and the text of an approved reply comes back alone, ready to copy (it is never sent to the other person)."""
    import os
    from . import execute
    post = make_post(ap, bridge, send)
    ap.save()   # the approval itself is saved before anything is attempted (what is posted is flushed once, at the end of collect)
    result = execute.run_approved(out, ap, num, it, post) if real else []
    if config.value("send_ready_post") != "0":
        execute.send_ready_posts(num, it, post, link=config.value("open_chat_link") == "1",
                                 split=config.value("send_ready_split") == "1")
    return result


class Changes(fsutil.BusyList):
    """The applied changes. `busy` is True when another approvals run holds the lock: nothing was read or applied."""


def _naive_now():
    return datetime.now()


def collect(out, bridge, writer=None, real=False, send=False, now=None, brief_hour=None):
    """Read replies from the self chat and apply them. Returns applied changes. Only one collect runs at a time (a lock file in
    the state folder): the daily cycle and a hand-typed `approvals` could otherwise approve the same "OK N" twice and write twice.
    When the lock is held, nothing is done and the result has `busy` set. `brief_hour` (R09): the hour a day hold lasts until, the
    one the cycle's brief runs at (None: the setting brief_hour)."""
    with fsutil.exclusive(lock_path(out)) as got:
        if not got:
            res = Changes()
            res.busy = True
            return res
        changes = _collect(out, bridge, writer, real, send, now, brief_hour)
        learn.offer_style(out, changes, now or _naive_now())   # R34 (learn=1 only): a card for a wording another item used too; under this lock
        return Changes(changes)


def unwritten(it):
    """An approved ADO comment that was never written (R13): its OK came on a run without --send, so the approval was recorded and
    nothing was executed. The post had shown the text and the target, and `exec` is still empty. The first run with --send writes it."""
    from . import execute
    return bool(it.get("posted")) and it.get("status") == "approved" and not it.get("exec") and any(
        execute.is_gated(a) and a.get("exec_text") for a in (it.get("record") or {}).get("actions", []))


def _collect(out, bridge, writer=None, real=False, send=False, now=None, brief_hour=None):
    """The body of collect (the lock is held).
    real=True (the daily and approvals paths only): an approval carries out the switched-on kinds, and `再実行 N` / `済 N` work.
    send=True: what is posted while collecting (results, texts ready to copy) is sent; otherwise it is only pasted.
    real and send together (the first run with --send after an OK without it): the comments of such OKs are written now, once."""
    ap = Approvals(out)
    from . import execute, memory, promises, sent, work
    clock = now or _naive_now()   # R28: the clock of the nudge and of its watch window (sent.py)

    def waiting(it):   # waiting for an answer, or approved but with an execution that failed / was left unsure (`再実行` / `済`),
        # or approved on a run without --send and not written yet (unwritten: the run with --send writes it)
        if real and send and sent.awaiting(ap.data, it, clock):   # R28: a nudged reply whose send is still watched, or one due for today's nudge
            return True
        if real and send and execute.reply_requested(it):   # S2: a `送る N` read on a run without --send waits for this run (sent below)
            return True
        if real and execute.reply_open(it, clock):   # S2: an approved reply that `送る N` may still send is read for, for REPLY_WINDOW after the OK
            return True
        return it["posted"] and (it["status"] in ("pending", "held") or (
            real and it["status"] == "approved" and (any(execute.unresolved(v) for v in (it.get("exec") or {}).values())
                                                     or (send and unwritten(it)))))
    flushed = True
    if send and ap.data.get("outbox"):
        flushed = flush_outbox(ap, bridge, True)   # results that could not be posted last time
    adopted = []   # R21: an OK on a learn card that was given on a run without real and send is adopted now (learn.py; Teams is not read)
    if real and send:
        for num, it in ap.data["items"].items():
            if learn.is_card(it) and it.get("learn_state") == "pending":
                got = learn.adopt(out, it, now=now or _naive_now(), real=real, send=send)
                if got and got["state"] != "pending":
                    adopted.append({"id": int(num), "status": "learned" if got["state"] == "adopted" else "learn_stale", "learn": got})
        if adopted:
            ap.save(compact=learn.purge_all(ap.data))   # R34: an adopted wording leaves its card; compact, so no .bak keeps it
    if real and send and promises.close_waiting(out, ap.data["items"]):   # R31 (promises=1): an ask-back whose chat has answered ends its wait; read only in decisions.jsonl, before any early return
        ap.save()
    # nothing is waiting for an answer: do not touch Teams at all (reading switches it to the self chat). The one exception is a card
    # of the day's spot check that waits for its answer (R26, spot.py): the reply is read for it, and nothing else is touched
    work_wait = real and send and work.awaiting(ap.data, clock)   # R30 (promises=1): the deadline post is due, or a post of it is recent
    if not work_wait and not any(waiting(it) for it in ap.data["items"].values()):
        if real and send:
            from . import spot
            spot.answers(out, bridge, now or _naive_now())
        return adopted
    post = make_post(ap, bridge, send)
    if real:
        execute.report_unknown(ap, post)
    if real and send:
        for num, it in ap.data["items"].items():
            if unwritten(it):   # the run's write: saved before each call, so a second run never writes it again
                execute.run_approved(out, ap, int(num), it, post)
    writer = writer if writer is not None else writer_mod.get_writer()
    changes = []
    if real and send:   # S2: a `送る N` read on a run without --send is sent now, once (the request was recorded; see execute.reply_request)
        for num, it in ap.data["items"].items():
            if execute.reply_requested(it):
                got = execute.reply_request(out, ap, int(num), it, post, bridge, send=True)
                if got:
                    changes.append({"id": int(num), "status": "reply_send", "real": got})
    read = bridge.read()
    if real and send:   # R26: `正しい S<k>` / `違う S<k>` label their card's decision (spot.py); no approval item is touched by them
        from . import spot
        spot.apply_safe(out, read, now or _naive_now())
    if real and send and sent.enabled():   # R28: `送った N` (below its approval post) stops the nudge; then today's nudge, if one is due
        for num in fresh_sent(read):
            it = ap.data["items"].get(num)
            if it and sent.confirm(it, clock):
                changes.append({"id": int(num), "status": "sent_confirmed"})
        sent.nudge(ap.data, post, clock)   # the post is put in the outbox (make_post) and flushed at the end of this step
    if real and send and work.enabled():   # R30: the work replies below a deadline post (before today's post: they are read against the screen as it was)
        for num, parsed, line in fresh_work(read):
            try:
                work.apply_reply(out, ap, num, parsed, line, clock)
            except work.WorkError:
                continue   # a journal that cannot be read stops this reply only; the cycle goes on
        work.due_notice(ap.data, post, clock)   # the deadline post of the day, once (put in the outbox like the other posts)
    if real:   # a decision reply of a post that has left the screen is not read: say so once and post the item again (nothing is approved)
        unseen_changed = False
        for num, line in unseen_reply_numbers(read):
            it = ap.data["items"].get(num)
            if not it or not it["posted"] or it["status"] not in ("pending", "held"):
                continue
            seen_lines = it.get("reply_ignored_seen") or []
            key = unicodedata.normalize("NFKC", line).strip()
            if key in seen_lines:
                continue
            it["reply_ignored_seen"] = (seen_lines + [key])[-20:]
            unseen_changed = True
            held = it["status"] == "held"   # a held item is told about only: it comes back when its hold ends (hold.py), not now
            try:
                post(f"[kimeru 通知] #{num} への返信を読めませんでした（投稿が画面から流れました）。"
                     + ("保留の間は投稿し直しません" if held else "もう一度投稿します"))   # not "[kimeru #N]": the reader would take this line for the post of #N
            except Exception:   # the notice failing must not stop the cycle; the item is posted again all the same
                pass
            if not held:
                it["posted"], it["request_posted"] = False, False
            changes.append({"id": int(num), "status": "reply_ignored"})
        if unseen_changed:   # nothing changed = no save (a save rewrites approvals.json.bak)
            ap.save()
    fresh = fresh_entries(read)
    replies = [line for line, _ in fresh]
    seen_results = {}   # for `再実行`: the highest result count of the item that stands before the reply (the context of the reply)
    for line, seen in fresh:
        r0 = parse_reply(line)
        if r0 and r0[0] == "再実行":
            seen_results[r0[1]] = max(seen_results.get(r0[1], 0), seen)
    redo_done = set()
    past = {}   # R16: the earlier decisions, read once when a 詳細 asks for them (memory=1 only)

    def history_of(num, it):
        if not memory.enabled():
            return None
        if "idx" not in past:
            past["idx"] = memory.index(out, ap.data["items"])
        return memory.history(past["idx"], it["record"], num)

    def promise_of(num, it):   # R18 (promises=1 only): the chat's open promises in 詳細 N; None at promises=0 (nothing is read)
        from . import promises
        if not promises.enabled():
            return None
        return promises.detail_lines(promises.snapshot(out, now), it["record"], num)

    def run_redo(num, it):   # `再実行`: once per number per step, only while something failed or is unknown; the mark is saved before the run
        redo_done.add(num)
        it["redo_mark"] = int(it.get("x_seq", 0))   # the results made so far; those of this redo come after
        ap.save()   # saved before the run: a stop in the middle must not run the same reply again
        changes.append({"id": int(num), "status": "redo", "real": execute.redo(out, ap, int(num), it, post, bridge=bridge, send=send)})

    def redo_candidate(num):
        it = ap.data["items"].get(num)
        if not real or num in redo_done or not it or it["status"] != "approved"                 or not any(execute.unresolved(v) for v in (it.get("exec") or {}).values()):
            return None
        return it

    for num, k in redo_k_entries(read):   # `再実行 N-k`: acts once, when k is the item's latest result count and no redo has acted since
        it = redo_candidate(num)
        if it is None:
            continue
        cur = int(it.get("x_seq", 0))
        ignored = it.get("redo_ignored") or []
        acted = it.get("redo_acted") or []
        if k in acted:   # one that acted (the reply stays on the screen): nothing more, no record
            continue
        if k != cur:
            # k is older than the latest result post (never acts), or the result post k does not exist yet (acts once, when it does:
            # the result post says "再実行 N-k"). Either is recorded once, and told apart.
            if k not in ignored:
                it["redo_ignored"] = ignored + [k]
                ap.save()
                changes.append({"id": int(num), "status": "redo_ahead" if k > cur else "redo_ignored", "k": k, "latest": cur})
            continue
        if redo_mark(it) >= cur:   # already acted on this result (a `再実行 N` did)
            it["redo_acted"] = acted + [k]
            ap.save()
            continue
        it["redo_acted"] = acted + [k]   # kept so that the reply, still on the screen after the next result post, is not recorded as ignored
        run_redo(num, it)
    for num, kind in redo_bad_kinds(read):   # a reply that begins like `再実行 N-k` but is in a form kimeru does not read: recorded once per form
        it = redo_candidate(num)
        if it is None:
            continue
        kinds = it.get("redo_malformed_kinds") or []
        if kind not in kinds:   # the same line stays on the screen, and what it says is not kept (only a hash of its shape)
            it["redo_malformed_kinds"] = kinds + [kind]
            it["redo_malformed"] = int(it.get("x_seq", 0))
            ap.save()
            changes.append({"id": int(num), "status": "redo_malformed", "latest": it["redo_malformed"]})
    now = now or _naive_now()   # R09: a day hold is counted from this moment (naive local, as the cycle's clock)
    last_hold = {}   # of the replies on the screen, the last 保留 of each number is the one that counts (its day, if it has one)
    for idx, line in enumerate(replies):
        hr = _hold_reply(line)
        if hr:
            last_hold[hr[0]] = idx
    for idx, line in enumerate(replies):
        ps = parse_paste(line)
        if ps:
            it = ap.data["items"].get(ps[0])
            if it and it["posted"] and it["status"] in ("pending", "held"):
                changes.append({"id": int(ps[0]), **_apply_paste(it, ps[1])})
            continue
        rd = parse_redraft(line)
        if rd:
            it = ap.data["items"].get(rd[0])
            if it and it["posted"] and it["status"] in ("pending", "held"):
                changes.append({"id": int(rd[0]), **_redraft(it, rd[1], writer, out, n=int(rd[0]), now=now)})
            continue
        lb = parse_label(line)
        if lb:   # 違う N / 違う N k: a verdict on the decision (learn=1, the daily and approvals paths). It never changes the item
            it = ap.data["items"].get(lb[0])
            if real and it and it["posted"] and labels.enabled():
                labels.record(out, it, lb[1])
            continue
        hr = _hold_reply(line)   # 保留 N, and 保留 N <いつ> (R09): the last one of the number decides the day
        if hr:
            num, day_word = hr
            it = ap.data["items"].get(num)
            if last_hold.get(num) == idx and it and it["posted"] and it["status"] in ("pending", "held"):
                entering = it["status"] != "held"
                it["status"] = "held"
                info = hold.apply(it, day_word, now, entering, brief_hour=brief_hour)
                if entering or info is not None:
                    changes.append({"id": int(num), "status": "held", **(info or {})})
            continue
        r = parse_reply(line) or _not_needed_reply(line)
        if not r:
            continue
        word, num = r
        it = ap.data["items"].get(num)
        if word == "再実行":   # runs again what failed or was left unsure, on an item that is already approved; once per reply
            if not real or num in redo_done or not it or it["status"] != "approved" \
                    or not any(execute.unresolved(v) for v in (it.get("exec") or {}).values()):
                continue
            if seen_results.get(num, 0) <= redo_mark(it):   # no result post newer than the last redo is before this reply: it was answered
                redo_done.add(num)
                continue
            run_redo(num, it)
            continue
        if word == "詳細":   # the full text of the item, once per reply (the detail post is the boundary: fresh_entries); no decision is made
            if it and it["posted"] and num not in redo_done:   # (redo_done doubles as "handled this step" for the numbers asked about)
                redo_done.add(num)
                from . import fulltext
                text = format_detail(int(num), it["record"], fulltext.load(out, it["key"]), precedent=history_of(num, it),
                                     promise=promise_of(num, it))
                if real and labels.enabled():   # the question the route was unsure of, numbered: kept for `違う N k`
                    text = labels.attach(it, int(num), text)
                post(text)
                changes.append({"id": int(num), "status": "detail"})
            continue
        if word == "済":   # the PM saw the comment in ADO: what was not known is closed
            if real and it and it["status"] == "approved" and execute.close_unknown(out, ap, int(num), it):
                changes.append({"id": int(num), "status": "closed"})
            continue
        if word == "送る":   # S2: the approved reply goes to its 1 対 1 chat, once (execute.reply_request); the real run only, never by an OK
            # after REPLY_WINDOW from the OK the word is ignored (nothing sent, nothing answered), even when the chat is read for another reason
            if real and it and it["status"] == "approved" and not execute.reply_window_closed(it, clock):
                got = execute.reply_request(out, ap, int(num), it, post, bridge, send)
                if got:
                    changes.append({"id": int(num), "status": "reply_send", "real": got})
            continue
        if word == "不要" and it is not None and learn.is_card(it):   # R33: a card is no case: nothing answers 不要 on it
            continue
        if not it or not it["posted"] or it["status"] not in ("pending", "held"):
            continue
        if word == "聞き返し":
            # the ask-back draft becomes the reply and is posted again under the same #N; OK N records it
            replies_a = [a for a in it["record"].get("actions", []) if a.get("type") == "teams.reply" and a.get("ask_back")]
            if not replies_a:
                continue          # nothing to ask back: the item stays as it is
            for a in replies_a:
                a["answer_text"], a["text"] = a["text"], a.pop("ask_back")
                a["unverified"] = a.pop("ask_back_unverified", [])
                a["variant"] = "ask_back"
            it["posted"], it["status"], it["request_posted"] = False, "pending", False
            hold.clear(it)
            next_measurement(it)
            changes.append({"id": int(num), "status": "ask_back"})
            continue
        new = STATUS[word]
        if new == it["status"]:
            continue
        if new == "approved" and real and any(execute.is_gated(a) and not (a.get("exec_text") or a.get("exec_skip") or a.get("reply_text") or a.get("reply_skip"))
                                              for a in it["record"].get("actions", [])):   # a reply that was not fixed in its post counts too (S2)
            # posted before execution was switched on: it never showed the text that would be written. Nothing is written;
            # the post is made again with the text and the target, and the PM answers again.
            it["posted"], it["request_posted"] = False, False
            next_measurement(it)
            it["record"]["redraft_note"] = ("実行を有効にする前に投稿された確認待ちのため、書きませんでした。"
                                            "下に書く文面と宛先を示します。確かめて、もう一度 OK と返信してください")
            changes.append({"id": int(num), "status": "reposted"})
            continue
        it["status"] = new
        hold.clear(it)
        learn.on_status(it, new, now)   # R21: a card's own state (OK waits to be adopted; NG keeps the question quiet for 28 days)
        if new == "approved":
            measurement_for(it).setdefault("approval_observed_at", measurement_now())
            promises.open_waiting(it, clock)   # R31 (promises=1): an approved ask-back reply waits for the answer of its chat (item.waiting); other items get nothing
            from . import sent   # R14: with sent_watch=1, the list signature of the reply's chat is kept, to see the send later (sent.py)
            if sent.enabled() and not learn.is_card(it):
                sent.note_approval(it, sent.list_signatures(out), now)
        if new in ("approved", "rejected"):   # the full text is kept only while the item waits
            from . import fulltext
            fulltext.drop_safe(out, it["key"])   # a failure is noted in warnings.jsonl; the approval goes on, purge deletes the text later
            it.pop("detail_question", None)   # the options of `詳細 N` are kept only while the item waits (labels.py)
            it["record"].pop("held_before", None)   # R08: the "（前回 保留）" mark ends with the decision
        ch = {"id": int(num), "status": new}
        if word == "不要":   # R33: the mark the rule counts (learn.py); the words of the line are not kept
            ch.update(learn.mark_not_needed(it, now))
        if new == "approved":
            if real and execute.reply_pending(it):   # S2: the OK of a reply that `送る N` may send: the window starts now (execute.reply_open)
                it["reply_open_at"] = clock.isoformat()
            ch["executed"] = [actions.execute(a, dry_run=True) for a in it["record"].get("actions", [])]
            ch["real"] = _after_approval(out, ap, int(num), it, bridge, real=real, send=send)
            if learn.is_card(it):   # R21: written only with real and send (learn.adopt); otherwise it waits for such a run
                ch["learn"] = learn.adopt(out, it, now=now, real=real, send=send)
        changes.append(ch)
    changes = adopted + changes
    ap.save(compact=learn.purge_all(ap.data))   # R34: a card answered in this run gives up its wording (compact: no .bak keeps it)
    if send and flushed and ap.data.get("outbox"):
        flush_outbox(ap, bridge, True)   # the results of this step, all together (once per cycle)
    if changes:
        from datetime import datetime, timezone
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with (Path(out) / "approvals.log.jsonl").open("a", encoding="utf-8") as f:
            for ch in changes:
                # when, and which decision (the same key as the decision record): old lines without them stay readable
                item = ap.data["items"].get(str(ch.get("id"))) or {}
                # a revision instruction is the PM's own words about a colleague's message: only its length is logged, and for a
                # short preference also a fingerprint, so the same wording written again on another item can be counted
                logged = {k: v for k, v in ch.items() if k != "instruction"}
                if "instruction" in ch:
                    logged["instruction_len"] = len(str(ch["instruction"] or ""))
                    sig = instruction_sig(ch["instruction"], (item.get("record") or {}).get("event"))
                    if sig:
                        logged["instruction_sig"] = sig
                f.write(json.dumps({"at": at, "key": item.get("key"), **logged,
                                    "measurement": dict(measurement_for(item))}, ensure_ascii=False) + "\n")
    return changes
