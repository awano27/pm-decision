"""Human-in-the-loop over Teams "chat with yourself".

Queue items (advise nodes with queue=true) are posted to the self chat as
"[kimeru #N] ..." and the PM replies from any device (e.g. iPhone) with
"OK N" / "NG N" / "保留 N". Actions attached to an advise node are only
*proposed*; they run (dry-run in v0.1) when approved. A reply drafted by the
writer (writer.py) is shown in full; "修正 N <指示>" redrafts it and posts it again.
"""
import copy
import json
import re
import subprocess
import unicodedata
from pathlib import Path

from . import actions, fsutil
from . import writer as writer_mod

HERE = Path(__file__).resolve().parent.parent
REPLY = re.compile(r"^(OK|NG|保留|聞き返し|再実行)\s*#?(\d+)\s*[.。!！]*$", re.IGNORECASE)
REDRAFT = re.compile(r"^修正\s*#?(\d+)\s*[:：]?\s*(\S.*)$")
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
def parse_paste(line):
    """("3", "受領しました。…") for "下書き 3 受領しました。…"; None otherwise."""
    m = PASTE.match(unicodedata.normalize("NFKC", line).strip())
    return (m.group(1), m.group(2).strip()) if m else None


STATUS = {"OK": "approved", "NG": "rejected", "保留": "held", "聞き返し": "ask_back", "再実行": "redo"}   # 聞き返し / 再実行: not decisions


class PowerShellBridge:
    """Runs tools/teams-self.ps1 (built-in UI Automation, no downloads)."""

    def __init__(self, script=HERE / "tools" / "teams-self.ps1"):
        self.script = str(script)

    def _run(self, *args):
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", self.script, *args],
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        out = json.loads(r.stdout.strip().lstrip("﻿") or "{}")
        if not out.get("ok"):
            raise RuntimeError(out.get("error") or r.stderr.strip()[:300])
        return out

    def post(self, text, send):
        # line breaks travel as literal \n (command-line args); the script expands them
        return self._run("-Action", "post", "-Text", text.replace("\n", "\\n"), *(["-Send"] if send else []))

    def read(self):
        return self._run("-Action", "read")

    def chats(self):
        return self._run("-Action", "chats").get("chats", [])

    def readchat(self, chat_id, count=5):
        """Open one chat, read its last messages (read only) and go back to the chat that was open. Slow (a few seconds)."""
        return self._run("-Action", "readchat", "-ChatId", chat_id, "-Count", str(count))


def format_post(n, rec, full=None):
    """`full` is the full text kept while the item waits (fulltext.load): the post then shows it and the earlier messages."""
    ev = rec.get("event_kind", "")
    lines = [f"[kimeru #{n}] 判断が必要（{rec.get('graph')}）", f"{ev} #{rec.get('event_id')}"]
    rf = rec.get("read_full") or {}
    if rf.get("state") == "preview_only":
        lines.append("⚠ プレビューだけで判断しました（" + str(rf.get("why", ""))[:100] + "）。元のメッセージを Teams で確認してください")
    elif rf.get("state") == "full" and rf.get("note"):
        lines.append("⚠ " + rf["note"])
    if rec.get("advice"):
        lines.append(f"内容: {rec['advice']}")
    if rec.get("actions"):
        lines.append("承認で実行: " + ", ".join(a.get("type", "?") for a in rec["actions"]))
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
    memo = rec.get("memo") or {}
    if memo:
        lines.append("Copilot のメモ:")
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
        lines.append("⚠ Copilot の下書きを作れなかったため定型文です（" + str(rec["writer_error"])[:60] + "）")
    for a in waiting:
        if a.get("writer_warning"):
            lines.append(f"⚠ Copilot の{writer_mod.LABEL.get(a['type'], a['type'])}は使えないため定型文です（{a['writer_warning']}）")
    for a in waiting[:1]:
        lines.append("定型文: " + str(a.get(writer_mod.FIELD[a["type"]]) or ""))
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
    from . import execute
    for a in rec.get("actions", []):
        if execute.is_gated(a):   # a switched-on kind: the PM sees the exact text that will be written
            if not a.get("drafted_by"):
                lines.append(f"{writer_mod.LABEL.get(a['type'], a['type'])}（定型文）:\n{a.get(writer_mod.FIELD.get(a['type'], 'text'), '')}")
            lines.append(f"→ OK {n} で、作業項目 {a.get('id')} に、上の文面のとおり" + ("（末尾に kimeru の 1 行を付けて）" if execute.signature_on() else "")
                         + "コメントを書きます")
    ask = any(a.get("ask_back") for a in drafts)
    lines.append(f"返信: OK {n} / NG {n} / 保留 {n}" + (f" / 修正 {n} <直してほしい点>" if drafts else "")
                 + (f" / 下書き {n} <Copilot の文面>" if rec.get("copilot_request") else "")
                 + (f" / 聞き返し {n}" if ask else ""))
    return "\n".join(lines)


class Approvals:
    def __init__(self, out):
        self.path = Path(out) / "approvals.json"
        self.data = fsutil.read_json(self.path, {"next": 1, "items": {}})

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fsutil.write_atomic(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))

    def known(self, key):
        return any(it["key"] == key for it in self.data["items"].values())

    def add(self, key, rec):
        n = self.data["next"]
        self.data["next"] += 1
        self.data["items"][str(n)] = {"key": key, "status": "pending", "posted": False, "record": rec}
        return n


def _key(rec):
    return f"{rec.get('graph')}:{rec.get('event_id')}:{rec.get('node')}"


def untoasted(out):
    """(numbers of pending items posted but not yet announced on the PC, notice keys likewise)."""
    ap = Approvals(out)
    nums = [int(n) for n, it in ap.data["items"].items()
            if it["posted"] and it["status"] in ("pending", "held") and not it.get("toasted")]
    seen = set(ap.data.get("notices_toasted", []))
    return sorted(nums), [k for k in ap.data.get("notices", []) if k not in seen]


def mark_toasted(out, nums, notice_keys):
    ap = Approvals(out)
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
    return os.environ.get("KIMERU_TOAST", "1") != "0"


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
    if os.environ.get("KIMERU_TOAST") == "detail" and posted:
        rec = (Approvals(out).data["items"].get(str(posted[0])) or {}).get("record", {})
        body += " " + str(rec.get("summary") or rec.get("advice") or rec.get("graph") or "")[:60]
    return "kimeru: " + " / ".join(parts), body


ACK = {"approved": "承認", "rejected": "却下", "held": "保留", "ask_back": "聞き返し",
       "redrafted": "書き直し", "redraft_failed": "書き直せず"}


def ack_text(changes):
    """One PC notification line for the replies just applied (a reply is never silently swallowed)."""
    parts = [f"#{c['id']} {ACK.get(c['status'], c['status'])}" for c in changes]
    return "kimeru: 返事を受け付けました", " / ".join(parts[:6]) + (f" ほか {len(parts) - 6} 件" if len(parts) > 6 else "")


def show_toast(title, body):
    """Windows notification on this PC (tools/toast.ps1). Opt out with KIMERU_TOAST=0."""
    import os
    if os.environ.get("KIMERU_TOAST", "1") == "0":
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
    if rec.get("advice"):
        lines.append(f"内容: {rec['advice']}")
    ran = [e["action"].get("type", "?") for e in rec.get("executed", [])]
    if ran:
        lines.append("記録した行動: " + ", ".join(ran) + "（現在は記録のみ）")
    lines.append("返信は不要です")
    return "\n".join(lines)


def notify_notices(out, bridge, send=False):
    """Post automatic decisions the PM should know about (notices.jsonl), each once."""
    out = Path(out)
    ap = Approvals(out)
    done = set(ap.data.setdefault("notices", []))
    q = out / "notices.jsonl"
    rows = [json.loads(l) for l in q.read_text(encoding="utf-8").splitlines() if l.strip()] if q.exists() else []
    posted = []
    for rec in rows:
        key = _key(rec)
        if key in done:
            continue
        r = bridge.post(format_notice(rec), send) or {}
        if send and (r.get("typed") is False or r.get("sent") is False):
            raise RuntimeError(f"notice {key} was not posted as planned: {r}")
        if send:
            ap.data["notices"].append(key); done.add(key)
            ap.save()
        posted.append(key)
    return posted


def notify(out, bridge, send=False):
    """Post every not-yet-posted queue item to the self chat."""
    out = Path(out)
    ap = Approvals(out)
    q = out / "queue.jsonl"
    rows = [json.loads(l) for l in q.read_text(encoding="utf-8").splitlines() if l.strip()] if q.exists() else []
    for rec in rows:
        if not ap.known(_key(rec)):
            ap.add(_key(rec), rec)
    ap.save()   # an item that cannot be posted now must still exist (and be counted as waiting) after a failed post
    posted = []
    for n, it in ap.data["items"].items():
        req = it["record"].get("copilot_request")
        if it["posted"] and (not req or it.get("request_posted")):
            continue
        if not it["posted"]:
            from . import fulltext
            r = bridge.post(format_post(n, it["record"], fulltext.load(out, it["key"])), send) or {}
            if send and (r.get("typed") is False or r.get("sent") is False):
                raise RuntimeError(f"#{n} was not posted as planned: {r}")   # stays unposted; retried next cycle
            if send:
                it["posted"], it["toasted"] = True, False
                ap.save()  # a failure on a later item must not forget what was already sent
            posted.append(int(n))
        if req and not it.get("request_posted"):   # its own message: one long-press copies just this
            r2 = bridge.post(f"[kimeru #{n} Copilot 用]" + chr(10) + req, send) or {}
            if send and (r2.get("typed") is False or r2.get("sent") is False):
                raise RuntimeError(f"#{n} Copilot request was not posted as planned: {r2}")
            if send:
                it["request_posted"] = True
                ap.save()
    ap.save()
    return posted


def fresh_replies(read):
    """Replies that come after the most recent visible post with the same number.

    Old "OK 1" lines from an earlier run stay in the chat; without this, a new
    item #1 would be approved by them. With a timeline, a reply counts only if
    it appears after the last "[kimeru #1]" post; if that post is not visible,
    freshness cannot be proven and the reply is ignored.
    """
    tl = read.get("timeline")
    if tl is None:  # older bridge without ordering
        return list(read.get("replies", []))
    last_post = {}
    for i, e in enumerate(tl):
        if e.startswith("P:"):
            last_post[e[2:]] = i
    out = []
    for i, e in enumerate(tl):
        if e.startswith("R:"):
            r = parse_reply(e[2:]) or parse_redraft(e[2:]) or parse_paste(e[2:])
            num = r and (r[1] if r[0] in STATUS else r[0])
            if num and i > last_post.get(num, len(tl)):
                out.append(e[2:])
    return out


STALE_ACTION_KEYS = ("drafted_by", "held_for", "writer_warning", "variant", "ask_back", "ask_back_unverified", "unverified")
STALE_RECORD_KEYS = ("writer_error", "copilot_request", "copilot_sources", "memo", "redraft_note")


def _redraft(it, instruction, writer, out=None):
    """Write the item again with the PM's instruction. Returns the change to log; the item is posted
    again under the same number either way (a failure says so and keeps the previous draft)."""
    rec = it["record"]
    it["posted"], it["status"], it["request_posted"] = False, "pending", False
    if writer is None or not rec.get("material_event"):
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
        material["text"] = full["text"]
        if full.get("thread"):
            material["thread"] = full["thread"]
    drafted = writer_mod.apply(rec, material, writer, instruction)
    used = [a for a in drafted if a.get("drafted_by")]
    if not used:
        why = rec.get("writer_error") or "使える下書きが返りませんでした"
        rec.clear()
        rec.update(before)
        rec["redraft_note"] = f"修正できませんでした（{str(why)[:60]}）。前の下書きのままです"
        return {"status": "redraft_failed", "instruction": instruction}
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
        rec["redraft_note"] = "貼り付けた文面を入れる先（返信・投稿・コメント）がありません。前のままです"
        return {"status": "paste_failed", "why": "no target"}
    why = writer_mod.unusable(text)
    if why:
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
    return {"status": "pasted"}


def _after_approval(out, ap, num, it, bridge):
    """What an approval sets in motion beyond the plan record: the switched-on kinds are carried out (execute.py), and the
    text of an approved reply comes back alone, ready to copy (it is never sent to the other person)."""
    import os
    from . import execute
    post = lambda t: bridge.post(t, True)
    ap.save()   # the approval itself is saved before anything is attempted
    real = execute.run_approved(out, ap, num, it, post)
    if os.environ.get("KIMERU_SEND_READY", "1") != "0":
        execute.send_ready_posts(num, it, post, link=os.environ.get("KIMERU_OPEN_CHAT_LINK", "0") == "1")
    return real


def collect(out, bridge, writer=None):
    """Read replies from the self chat and apply them. Returns applied changes."""
    ap = Approvals(out)
    # nothing is waiting for an answer: do not touch Teams at all (reading switches it to the self chat)
    def waiting(it):   # waiting for an answer, or approved but with an execution that failed / was left unsure (`再実行`)
        return it["posted"] and (it["status"] in ("pending", "held") or (
            it["status"] == "approved" and any(v.get("state") in ("failed", "running") for v in (it.get("exec") or {}).values())))
    if not any(waiting(it) for it in ap.data["items"].values()):
        return []
    from . import execute
    execute.report_unknown(ap, lambda text: bridge.post(text, True))
    writer = writer if writer is not None else writer_mod.get_writer()
    changes = []
    for line in fresh_replies(bridge.read()):
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
        if word == "再実行":   # runs again what failed or was left unsure, on an item that is already approved
            from . import execute
            if it and it["status"] == "approved" and it.get("exec") and any(v.get("state") in ("failed", "running") for v in it["exec"].values()):
                changes.append({"id": int(num), "status": "redo",
                                "real": execute.redo(out, ap, int(num), it, lambda t: bridge.post(t, True))})
            continue
        if not it or not it["posted"] or it["status"] not in ("pending", "held"):
            continue
        if word == "聞き返し":
            # the ask-back draft becomes the reply and is posted again under the same #N; OK N records it
            replies = [a for a in it["record"].get("actions", []) if a.get("type") == "teams.reply" and a.get("ask_back")]
            if not replies:
                continue          # nothing to ask back: the item stays as it is
            for a in replies:
                a["answer_text"], a["text"] = a["text"], a.pop("ask_back")
                a["unverified"] = a.pop("ask_back_unverified", [])
                a["variant"] = "ask_back"
            it["posted"], it["status"], it["request_posted"] = False, "pending", False
            changes.append({"id": int(num), "status": "ask_back"})
            continue
        new = STATUS[word]
        if new == it["status"]:
            continue
        it["status"] = new
        if new in ("approved", "rejected"):   # the full text is kept only while the item waits
            from . import fulltext
            fulltext.drop(out, it["key"])
        ch = {"id": int(num), "status": new}
        if new == "approved":
            ch["executed"] = [actions.execute(a, dry_run=True) for a in it["record"].get("actions", [])]
            ch["real"] = _after_approval(out, ap, int(num), it, bridge)
        changes.append(ch)
    ap.save()
    if changes:
        from datetime import datetime, timezone
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with (Path(out) / "approvals.log.jsonl").open("a", encoding="utf-8") as f:
            for ch in changes:
                # when, and which decision (the same key as the decision record): old lines without them stay readable
                item = ap.data["items"].get(str(ch.get("id"))) or {}
                f.write(json.dumps({"at": at, "key": item.get("key"), **ch}, ensure_ascii=False) + "\n")
    return changes
