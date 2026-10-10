"""kimeru delivery reconcile (run-fix-deliveries.cmd -> tools/fix-deliveries.ps1 -> here): settle every delivery that ended as
"unknown" in one run, with no questions. The self chat is read once (read only, the same probe as `kimeru diagnose`) and each
attempted body is looked for with the same matching as the send readback (tools/teams-self.ps1 Test-PostedTextMatch):
  seen in the chat      -> resolved like `delivery confirm`
  not seen              -> resolved like `delivery retry` (the next cycle sends it again)
  not seen and > 24 h   -> closed: not sent again, not approved (a case stays pending and shows in the morning brief)
  the brief, not seen   -> the hold is released, so a new brief can go out (yesterday's is never sent again)
The output holds kind, number and decision only: no body, no title."""

import argparse
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import diagnose, fsutil, notify

RESULT_FILE = "kimeru-fix-deliveries-result.txt"
OLD_AFTER = timedelta(hours=24)
LABEL_MAX, TIME_MAX = 80, 60   # the longest label before / time after the text in a message as the list reads it back
UNAVAILABLE = ("attempted body unavailable", "attempted revision unavailable")


def _u16(s):
    return len(s.encode("utf-16-le")) // 2


def _squash(s):
    return diagnose.norm(s).replace(" ", "")


def posted_match(sent, visible):
    """Test-PostedTextMatch of teams-self.ps1: is the text we sent in the message as the list shows it?
    Whitespace is ignored; the link label read before a URL is dropped; the text must start with [kimeru and may have at most a
    short label before it and a short time after it."""
    a, b = _squash(sent), _squash(visible)
    b = re.sub(r"(?i)(リンク|Link)(?=https?://)", "", b)
    if a == b:
        return True
    if not a.startswith("[kimeru") or _u16(a) < 8:
        return False
    i = b.find(a)
    if i < 0 or _u16(b[:i]) > LABEL_MAX:
        return False
    if "[kimeru" in b[:i]:
        return False
    return _u16(b) - _u16(b[:i]) - _u16(a) <= TIME_MAX


def _age_old(at, now):
    try:
        t = datetime.fromisoformat(str(at))
    except ValueError:
        return True   # no usable time: treated as old, so it is closed and never sent again by surprise
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return now - t > OLD_AFTER


def _body(out, kind, target):
    text = notify.delivery_show(out, kind, target)
    if not isinstance(text, str) or any(u in text for u in UNAVAILABLE):
        return None
    return text


def _label(row, index):
    if row["kind"] == "case":
        return "#" + row["target"]
    if row["kind"] == "notice":
        return f"通知{index}"
    if row["kind"] == "brief":
        return str(row.get("date") or "-")[:10]
    return "-"


def reconcile(out, bridge=None, now=None):
    """Returns {"status": "busy" | "none" | "no_read" | "done", "lines": [...], "counts": {...}, "note": str}."""
    out = Path(out)
    now = now or datetime.now(timezone.utc)
    rows = notify.delivery_pending(out)
    if getattr(rows, "busy", False):
        return {"status": "busy", "lines": [], "counts": {}}
    if not rows:
        return {"status": "none", "lines": [], "counts": {}}
    msgs, note = None, ""
    with fsutil.exclusive(notify.lock_path(out)) as got:
        if not got:
            return {"status": "busy", "lines": [], "counts": {}}
        bridge = bridge or notify.PowerShellBridge()
        try:
            probe = bridge.probe()
        except Exception as e:   # Teams not running, the self chat not found, a timeout: nothing is changed
            return {"status": "no_read", "lines": [], "counts": {},
                    "note": diagnose.error_text(f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")}
        visible = []
        for m in (probe.get("messages") if isinstance(probe, dict) else None) or []:
            if isinstance(m, dict):
                visible += [str(m.get("text") or ""), str(m.get("joined") or "")]
        plan = []
        notices = 0
        for row in rows:
            if row["kind"] == "notice":
                notices += 1
            body = _body(out, row["kind"], row["target"])
            old = _age_old(row.get("at"), now)
            if body is None:
                decision = "unchanged" if not old else "close"   # nothing to compare: only an old one is closed
            elif any(posted_match(body, v) for v in visible):
                decision = "found"
            elif row["kind"] == "brief":
                decision = "retry"   # the hold is released: a new brief, never yesterday's again
            else:
                decision = "close" if old else "retry"
            plan.append((row, notices, decision))
    counts = {"found": 0, "retry": 0, "close": 0, "unchanged": 0, "busy": 0}
    lines = []
    for row, idx, decision in plan:
        kind, target = row["kind"], row["target"]
        if decision == "found":
            res = notify.confirm_delivery(out, kind, target, confirm_delivered=True, source="reconcile_readback")
            ok = res.get("status") == "confirmed"
        elif decision == "retry":
            res = notify.retry_delivery(out, kind, target, confirm_not_sent=True, source="reconcile")
            ok = res.get("status") == "released"
        elif decision == "close":
            res = notify.close_delivery(out, kind, target)
            ok = res.get("status") == "closed"
        else:
            res, ok = {"status": "unchanged"}, False
        if res.get("status") == "busy":
            counts["busy"] += 1
            lines.append(f"- {kind} {_label(row, idx)} {row.get('part', kind)}: 変更なし（approvals.lock を他の実行が使用中）")
            break
        shown = decision if ok else "unchanged"
        counts[shown] += 1
        jp = {"found": "届いていた（確認済みにした）", "retry": "見当たらない（次のサイクルで送り直す）",
              "close": "見当たらない・24時間より古い（送り直さず閉じた）", "unchanged": "変更なし"}[shown]
        if kind == "brief" and shown == "retry":
            jp = "見当たらない（保留を解いた。新しい朝のまとめが出る）"
        if kind == "brief" and shown == "close":
            jp = "見当たらない（保留を解いた。新しい朝のまとめが出る）"
        lines.append(f"- {kind} {_label(row, idx)} {row.get('part', kind)}: {jp}")
    return {"status": "done", "lines": lines, "counts": counts, "note": note}


def report(result):
    s = result["status"]
    if s == "busy":
        return ["配信の整理: approvals.lock を他の実行が使っています（自動運転のサイクル中かもしれません）。何も変えていません。少し待ってからもう一度実行してください"]
    if s == "none":
        return ["配信の整理: 配信結果が不明の件はありません"]
    if s == "no_read":
        return ["配信の整理: 自分とのチャットを読めませんでした（" + (result.get("note") or "原因不明") + "）。何も変えていません。Teams を起動して、もう一度実行してください"]
    c = result["counts"]
    head = (f"配信の整理: 届いていた {c['found']} 件 / 送り直す {c['retry']} 件 / 閉じた {c['close']} 件 / 変更なし {c['unchanged']} 件"
            + (f" / 使用中で未処理あり" if c.get("busy") else ""))
    tail = []
    if c["close"]:
        tail.append("閉じた case は確認待ちのままで、朝のまとめに出ます（承認にはしていません）")
    if c["retry"]:
        tail.append("送り直す件は、次の自動運転のサイクルで投稿されます")
    return [head, *result["lines"], *tail]


def drive_copy(text, folders, print_fn):
    for d in folders:
        if d.is_dir():
            try:
                (d / RESULT_FILE).write_text(text, encoding="utf-8-sig", newline="")
                print_fn(f"Google ドライブへコピーしました: {d / RESULT_FILE}")
            except OSError as e:
                print_fn(f"Google ドライブへのコピーに失敗しました: {type(e).__name__}")
            return
    print_fn("Google ドライブの kimeru-release フォルダは見つかりませんでした（コピーしていません）")


def dispatch(argv, out, print_fn=print, bridge=None, folders=None, now=None):
    p = argparse.ArgumentParser(prog="kimeru delivery reconcile")
    p.add_argument("--write", help="also write the result to this file (UTF-8)")
    p.add_argument("--drive", action="store_true", help="copy the file into a Google Drive folder named kimeru-release")
    try:
        a = p.parse_args(list(argv))
    except SystemExit as e:
        return int(e.code or 0)
    if os.name != "nt" and bridge is None:
        print_fn("配信の整理: 自分とのチャットを読めるのは Windows だけです。何も変えていません")
        return 1
    result = reconcile(out, bridge, now)
    lines = report(result)
    text = "\r\n".join(lines) + "\r\n"
    for line in lines:
        print_fn(line)
    if a.write:
        Path(a.write).write_text(text, encoding="utf-8-sig", newline="")
        if a.drive:
            drive_copy(text, folders if folders is not None else diagnose.drive_folders(), print_fn)
    return 0 if result["status"] in ("done", "none") else 1
