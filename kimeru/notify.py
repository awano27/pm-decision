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
from datetime import datetime, timezone
import unicodedata
from pathlib import Path

from . import actions, config, fsutil
from . import writer as writer_mod

HERE = Path(__file__).resolve().parent.parent
REPLY = re.compile(r"^(OK|NG|保留|聞き返し|再実行|済)\s*#?(\d+)\s*[.。!！]*$", re.IGNORECASE)
REDRAFT = re.compile(r"^修正\s*#?(\d+)\s*[:：]?\s*(\S.*)$")
REDO_K = re.compile(r"^再実行\s*#?(\d+)\s*-\s*(\d+)\s*[.。!！]*$")   # `再実行 N-k`: run again, k = the result count the PM saw (stated in the result post)
REDO_BAD = re.compile(r"^再実行\s*#?(\d+)\s*[^\w\s.。!！]+\s*\d*\s*[.。!！]*$")   # looks like `再実行 N-k` but with a mark that is not a dash (~ / and the like): recorded, not dropped
DASHES = str.maketrans({c: "-" for c in "ー−‐‑‒–—―─ｰ－"})   # every dash a phone or an IME may type between N and k (NFKC leaves several of them)
PASTE = re.compile(r"^下書き\s*#?(\d+)\s*[:：]?\s*(\S.*)$")   # the PM brings back what Microsoft 365 Copilot wrote
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


STATUS = {"OK": "approved", "NG": "rejected", "保留": "held", "聞き返し": "ask_back", "再実行": "redo", "済": "closed"}   # 聞き返し / 再実行 / 済: not decisions


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
    return text


def _reply_number(line):
    """The item number a reply line is about (any reply form); None for a line that is none."""
    r = parse_reply(line)
    if r:
        return r[1] if r[0] in STATUS else r[0]
    for f in (parse_redraft, parse_paste, parse_redo_k):
        r = f(line)
        if r:
            return r[0]
    return parse_redo_bad(line)


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


class PowerShellBridge:
    """Runs tools/teams-self.ps1 (built-in UI Automation, no downloads)."""

    def __init__(self, script=HERE / "tools" / "teams-self.ps1"):
        self.script = str(script)

    def _run(self, *args):
        # readchat moves the screen, waits for a quiet keyboard and puts the chat back: it needs more than the others (its own limit is 90 s)
        timeout = 300 if "readchat" in args else 120
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
        return self._run("-Action", "chats").get("chats", [])

    def readchat(self, chat_id, count=5, preview=None):
        """Open one chat, read its last messages (read only) and go back to the chat that was open. Slow (a few seconds).
        `preview`: the start of the chat's list preview; a layout that reports no selection is checked against it."""
        return self._run("-Action", "readchat", "-ChatId", chat_id, "-Count", str(count), *(["-Preview", preview] if preview else []))


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


def format_post(n, rec, full=None):
    """`full` is the full text kept while the item waits (fulltext.load): the post then shows it and the earlier messages."""
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
        if kind in execute.SEND_READY:
            effects.append("コピー用文面を自分チャットへ返す（相手には送信しない）")
        elif kind == "ado.comment" and action.get("exec_text"):
            effects.append("実行設定が有効なら、承認後に ADO へコメントを書き込む")
        else:
            effects.append("記録のみ")
    revision = rec.get("revision", 1)
    lines = [f"[kimeru #{n}] 判断が必要（{rec.get('graph')}） / 改訂 {revision}",
             f"判断: {advice}", "次の一手: " + (next_action or no_memo),
             "不足情報: " + (" / ".join(str(v) for v in missing) if missing else ("メモに記載なし" if memo else no_memo)),
             "OK の効果: " + ("; ".join(dict.fromkeys(effects)) if effects else "記録のみ"),
             f"{ev} #{rec.get('event_id')}"] + ado_lines(rec)
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
        lines.append(f"{label}の下書き（{a['drafted_by']}）:\n{a[writer_mod.FIELD[a['type']]]}")
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
    ask = any(a.get("ask_back") for a in drafts)
    lines.append(f"返信: OK {n} / NG {n} / 保留 {n}" + (f" / 修正 {n} <直してほしい点>" if drafts else "")
                 + (f" / 下書き {n} <Copilot の文面>" if rec.get("copilot_request") else "")
                 + (f" / 聞き返し {n}" if ask else ""))
    return "\n".join(lines)


class Approvals:
    def __init__(self, out):
        self.path = Path(out) / "approvals.json"
        self.data = fsutil.read_json(self.path, {"next": 1, "items": {}})

    def save(self, compact=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
    item["measurement"] = current
    item["record"]["measurement"] = dict(current)


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


def mark_toasted(out, nums, notice_keys, unposted=None):
    """Record what the PC notification announced. False (nothing changed, so it is announced again next time) when the lock is held."""
    with fsutil.exclusive(lock_path(out)) as got:
        if not got:
            return False
        _mark_toasted_locked(out, nums, notice_keys, unposted)
        return True


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


def retry_delivery(out, kind, target, confirm_not_sent=False):
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
            f.write(json.dumps({"at": at, "kind": kind, "target": str(target), "action": "confirmed_not_sent"},
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


def deliver_brief(out, bridge, text, send, date):
    """Post a brief with the same explicit delivery evidence and recovery hold used by daily."""
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
        state["brief_delivery_unknown"] = {"date": date, "at": at, "revision": date,
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


def confirm_delivery(out, kind, target, confirm_delivered=False):
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
                            timing["posting_source"] = "user_confirmation"
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
                handoff = re.match(r"^\[kimeru 送信用 #(\d+)\]", str(box[0]))
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
                                "action": "confirmed_delivered", "source": "user_confirmation"},
                               ensure_ascii=False) + "\n")
        return {"status": "confirmed", "kind": kind, "target": str(target)}


def _mark_toasted_locked(out, nums, notice_keys, unposted=None):
    ap = Approvals(out)
    if unposted is not None:
        if unposted:
            ap.data["unposted_announced"] = unposted
        else:
            ap.data.pop("unposted_announced", None)
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


def toast_text(out, posted, notices, unposted=0):
    """Title and body for the PC notification. Counts and numbers only; KIMERU_TOAST=detail adds the
    first item's one-line summary (on this PC only: the routes that leave it never carry a summary)."""
    import os
    parts = []
    if posted:
        parts.append(f"確認待ち {len(posted)} 件")
    if notices:
        parts.append(f"自動決定の通知 {notices} 件")
    if unposted:
        parts.append(f"投稿できていない確認待ち {unposted} 件")
    body = ("#" + ", #".join(str(n) for n in posted[:5]) if posted else "") or "Teams の自分とのチャットを確認してください"
    if config.value("toast") == "detail" and posted:
        rec = (Approvals(out).data["items"].get(str(posted[0])) or {}).get("record", {})
        body += " " + str(rec.get("summary") or rec.get("advice") or rec.get("graph") or "")[:60]
    return "kimeru: " + " / ".join(parts), body


ACK = {"approved": "承認", "rejected": "却下", "held": "保留", "ask_back": "聞き返し",
       "redrafted": "書き直し", "redraft_failed": "書き直せず", "redo": "再実行", "redo_ignored": "再実行（無視: 古い回数）", "redo_ahead": "再実行（回数が大きすぎます。その回数の結果が出たら 1 回だけ効きます）", "redo_malformed": "再実行（形が違います）", "closed": "済（閉じました）",
       "reposted": "文面を確認して OK し直してください"}


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


def format_notice(rec):
    lines = [f"[kimeru 通知] 自動で決定しました（{rec.get('graph')}）"]
    if rec.get("summary"):
        lines.append(f"元: {str(rec['summary'])[:200]}")
    lines.extend(ado_lines(rec))
    if rec.get("advice"):
        lines.append(f"内容: {rec['advice']}")
    ran = [e["action"].get("type", "?") for e in rec.get("executed", [])]
    if ran:
        lines.append("記録した行動: " + ", ".join(ran) + "（現在は記録のみ）")
    if rec.get("needs_human"):   # the same item also waits in its own numbered post (a draft to approve)
        lines.append("文面の下書きは、番号付きの投稿で承認を待っています")
    elif str(rec.get("event_kind") or "").startswith("ado.") and rec.get("advice"):
        lines.append("判断は ADO 側で進めてください。この通知に返信しても何も起きません")   # the advice asks for a decision
    else:
        lines.append("返信は不要です")
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
        body = format_notice(rec)
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
    for rec in rows:
        if not ap.known(_key(rec)):
            ap.add(_key(rec), rec)
    ap.save()   # an item that cannot be posted now must still exist (and be counted as waiting) after a failed post
    posted = []
    from . import fulltext
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
            body = format_post(n, it["record"], fulltext.load(out, it["key"]))
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
    last_post, last_result = {}, {}
    for i, e in enumerate(tl):
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
            r = parse_reply(e[2:]) or parse_redraft(e[2:]) or parse_paste(e[2:])
            num = r and (r[1] if r[0] in STATUS else r[0])
            if num and i > last_post.get(num, len(tl)):
                out.append((e[2:], top.get(num, 0)))
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


STALE_ACTION_KEYS = ("drafted_by", "held_for", "writer_warning", "variant", "ask_back", "ask_back_unverified", "unverified")
STALE_RECORD_KEYS = ("writer_error", "copilot_request", "copilot_sources", "memo", "redraft_note")


def _redraft(it, instruction, writer, out=None):
    """Write the item again with the PM's instruction. Returns the change to log; the item is posted
    again under the same number either way (a failure says so and keeps the previous draft)."""
    rec = it["record"]
    it["posted"], it["status"], it["request_posted"] = False, "pending", False
    if writer is None or not rec.get("material_event"):
        invalidate_measurement_post(it)
        rec["redraft_note"] = "修正できませんでした（writer が設定されていません）。前の下書きのままです"
        return {"status": "redraft_failed", "instruction": instruction}
    before = copy.deepcopy(rec)
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
    drafted = writer_mod.apply(rec, material, writer, instruction)
    used = [a for a in drafted if a.get("drafted_by")]
    if not used:
        invalidate_measurement_post(it)
        why = rec.get("writer_error") or "使える下書きが返りませんでした"
        rec.clear()
        rec.update(before)
        rec["redraft_note"] = f"修正できませんでした（{str(why)[:60]}）。前の下書きのままです"
        return {"status": "redraft_failed", "instruction": instruction}
    if full and rec.get("copilot_request"):   # the record keeps the request built from the excerpt; the whole text stays in full_text.json
        fulltext.set_request(out, it["key"], fulltext.redact_request(rec, material, instruction))
    next_measurement(it)
    return {"status": "redrafted", "instruction": instruction}


def _apply_paste(it, text):
    """The PM pasted Copilot's text: it becomes the first message the item would send (reply / post / comment).
    The item is posted again under the same number, with the usual checks (unusable text, dates and numbers
    that are not in the source). Returns the change to log."""
    rec = it["record"]
    it["posted"], it["status"], it["request_posted"] = False, "pending", False
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
        handoff = re.match(r"^\[kimeru 送信用 #(\d+)\]", str(text))
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
        execute.send_ready_posts(num, it, post, link=config.value("open_chat_link") == "1")
    return result


class Changes(fsutil.BusyList):
    """The applied changes. `busy` is True when another approvals run holds the lock: nothing was read or applied."""


def collect(out, bridge, writer=None, real=False, send=False):
    """Read replies from the self chat and apply them. Returns applied changes. Only one collect runs at a time (a lock file in
    the state folder): the daily cycle and a hand-typed `approvals` could otherwise approve the same "OK N" twice and write twice.
    When the lock is held, nothing is done and the result has `busy` set."""
    with fsutil.exclusive(lock_path(out)) as got:
        if not got:
            res = Changes()
            res.busy = True
            return res
        return Changes(_collect(out, bridge, writer, real, send))


def _collect(out, bridge, writer=None, real=False, send=False):
    """The body of collect (the lock is held).
    real=True (the daily and approvals paths only): an approval carries out the switched-on kinds, and `再実行 N` / `済 N` work.
    send=True: what is posted while collecting (results, texts ready to copy) is sent; otherwise it is only pasted."""
    ap = Approvals(out)
    from . import execute

    def waiting(it):   # waiting for an answer, or approved but with an execution that failed / was left unsure (`再実行` / `済`)
        return it["posted"] and (it["status"] in ("pending", "held") or (
            real and it["status"] == "approved" and any(execute.unresolved(v) for v in (it.get("exec") or {}).values())))
    flushed = True
    if send and ap.data.get("outbox"):
        flushed = flush_outbox(ap, bridge, True)   # results that could not be posted last time
    # nothing is waiting for an answer: do not touch Teams at all (reading switches it to the self chat)
    if not any(waiting(it) for it in ap.data["items"].values()):
        return []
    post = make_post(ap, bridge, send)
    if real:
        execute.report_unknown(ap, post)
    writer = writer if writer is not None else writer_mod.get_writer()
    changes = []
    read = bridge.read()
    fresh = fresh_entries(read)
    replies = [line for line, _ in fresh]
    seen_results = {}   # for `再実行`: the highest result count of the item that stands before the reply (the context of the reply)
    for line, seen in fresh:
        r0 = parse_reply(line)
        if r0 and r0[0] == "再実行":
            seen_results[r0[1]] = max(seen_results.get(r0[1], 0), seen)
    redo_done = set()

    def run_redo(num, it):   # `再実行`: once per number per step, only while something failed or is unknown; the mark is saved before the run
        redo_done.add(num)
        it["redo_mark"] = int(it.get("x_seq", 0))   # the results made so far; those of this redo come after
        ap.save()   # saved before the run: a stop in the middle must not run the same reply again
        changes.append({"id": int(num), "status": "redo", "real": execute.redo(out, ap, int(num), it, post)})

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
    for line in replies:
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
                changes.append({"id": int(rd[0]), **_redraft(it, rd[1], writer, out)})
            continue
        r = parse_reply(line)
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
        if word == "済":   # the PM saw the comment in ADO: what was not known is closed
            if real and it and it["status"] == "approved" and execute.close_unknown(out, ap, int(num), it):
                changes.append({"id": int(num), "status": "closed"})
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
            next_measurement(it)
            changes.append({"id": int(num), "status": "ask_back"})
            continue
        new = STATUS[word]
        if new == it["status"]:
            continue
        if new == "approved" and real and any(execute.is_gated(a) and not a.get("exec_text") and not a.get("exec_skip") for a in it["record"].get("actions", [])):
            # posted before execution was switched on: it never showed the text that would be written. Nothing is written;
            # the post is made again with the text and the target, and the PM answers again.
            it["posted"], it["request_posted"] = False, False
            next_measurement(it)
            it["record"]["redraft_note"] = ("実行を有効にする前に投稿された確認待ちのため、書きませんでした。"
                                            "下に書く文面と宛先を示します。確かめて、もう一度 OK と返信してください")
            changes.append({"id": int(num), "status": "reposted"})
            continue
        it["status"] = new
        if new == "approved":
            measurement_for(it).setdefault("approval_observed_at", measurement_now())
        if new in ("approved", "rejected"):   # the full text is kept only while the item waits
            from . import fulltext
            fulltext.drop_safe(out, it["key"])   # a failure is noted in warnings.jsonl; the approval goes on, purge deletes the text later
        ch = {"id": int(num), "status": new}
        if new == "approved":
            ch["executed"] = [actions.execute(a, dry_run=True) for a in it["record"].get("actions", [])]
            ch["real"] = _after_approval(out, ap, int(num), it, bridge, real=real, send=send)
        changes.append(ch)
    ap.save()
    if send and flushed and ap.data.get("outbox"):
        flush_outbox(ap, bridge, True)   # the results of this step, all together (once per cycle)
    if changes:
        from datetime import datetime, timezone
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with (Path(out) / "approvals.log.jsonl").open("a", encoding="utf-8") as f:
            for ch in changes:
                # when, and which decision (the same key as the decision record): old lines without them stay readable
                item = ap.data["items"].get(str(ch.get("id"))) or {}
                # a revision instruction is the PM's own words about a colleague's message: only its length is logged
                logged = {k: v for k, v in ch.items() if k != "instruction"}
                if "instruction" in ch:
                    logged["instruction_len"] = len(str(ch["instruction"] or ""))
                f.write(json.dumps({"at": at, "key": item.get("key"), **logged,
                                    "measurement": dict(measurement_for(item))}, ensure_ascii=False) + "\n")
    return changes
