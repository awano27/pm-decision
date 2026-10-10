"""kimeru delivery reconcile (run-fix-deliveries.cmd): every unknown delivery is settled from one read of the self chat, with a fake
probe and made-up data folders. Nothing is posted, nothing private reaches the output."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401
try:
    from . import hidden_words
except ImportError:
    import hidden_words

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kimeru import fsutil, notify, reconcile

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)
SECRETS = ("架空の題名ZQX", "fakeorg-zqx", "山田架空")


def _at(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def post_body(n):
    return f"[kimeru #{n}] 判断が必要\n判断: 架空の題名ZQX を確認\n元: 山田架空 fakeorg-zqx\n返信: OK {n} / NG {n}"


def case(n, minutes, part="post", body=None):
    return {"key": f"k{n}", "posted": False, "status": "pending", "revision": 1, "record": {"summary": "架空の題名ZQX"},
            "delivery_unknown": True, "delivery_unknown_part": part, "delivery_unknown_at": _at(minutes),
            "delivery_unknown_revision": 1,
            "delivery_attempt": {"kind": "case", "target": str(n), "part": part, "revision": 1, "identity": f"k{n}",
                                 "at": _at(minutes), "body": body or post_body(n)}}


def make_folder(d, brief=True):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    notice = "ado_workitem:1:set_p1"
    ap = {"next": 9, "items": {"3": case(3, 30), "4": case(4, 60), "5": case(5, 60 * 30)},
          "notice_delivery_unknown": {notice: _at(20)},
          "notice_delivery_attempts": {notice: {"at": _at(20), "body": "[kimeru 通知] 自動で決定しました 架空の題名ZQX"}},
          "outbox": ["[kimeru 実行 #3] fakeorg-zqx に書きました"], "outbox_delivery_unknown": True,
          "outbox_delivery_unknown_at": _at(10),
          "outbox_delivery_attempt": {"at": _at(10), "body": "[kimeru 実行 #3] fakeorg-zqx に書きました"}}
    (d / "approvals.json").write_text(json.dumps(ap, ensure_ascii=False), encoding="utf-8")
    if brief:
        (d / "daily_state.json").write_text(json.dumps({"brief_delivery_unknown": {
            "date": "2026-10-09", "at": _at(120), "part": "brief", "body": "[kimeru brief 2026-10-09] 今日の進め方 架空の題名ZQX"}},
            ensure_ascii=False), encoding="utf-8")
    return d


def screen(*texts):
    return {"ok": True, "how": "ids", "rows": 3, "selfTitle": True,
            "messages": [{"text": t, "joined": t, "raw_lines": 1, "fragments": 1, "top": 0.1, "height": 0.1} for t in texts]}


class FakeBridge:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def probe(self):
        self.calls.append("probe")
        if self.error:
            raise self.error
        return self.result

    def post(self, *a, **k):
        raise AssertionError("post called")

    read = chats = readchat = post


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="kimeru-recon-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        make_folder(self.dir)

    def ap(self):
        return json.loads((self.dir / "approvals.json").read_text(encoding="utf-8"))

    def state(self):
        return json.loads((self.dir / "daily_state.json").read_text(encoding="utf-8"))

    def run_it(self, bridge, **kw):
        return reconcile.reconcile(self.dir, bridge, NOW, **kw)

    def lines(self, result):
        return "\n".join(reconcile.report(result))


class TestMatching(unittest.TestCase):
    def test_the_same_rules_as_the_send_readback(self):
        body = "[kimeru #7] 確認\nhttps://example.test/a\n返信: OK 7"
        self.assertTrue(reconcile.posted_match(body, body))
        wrapped = "山田架空 送信済み [kimeru #7] 確認 リンクhttps://example.test/a 返信: OK 7 今日の 10:33."
        self.assertTrue(reconcile.posted_match(body, wrapped))
        self.assertTrue(reconcile.posted_match(body, "Sent [kimeru #7]確認Linkhttps://example.test/a返信:OK 7 Today at 10:33."))
        self.assertFalse(reconcile.posted_match(body, "x" * 81 + body))                     # label too long
        self.assertFalse(reconcile.posted_match(body, body + "y" * 61))                    # tail too long
        self.assertFalse(reconcile.posted_match(body, "[kimeru #6] " + body))              # another kimeru post before it
        self.assertFalse(reconcile.posted_match("確認 OK 7 だけ", "前 確認 OK 7 だけ 後"))     # not a kimeru text: exact only
        self.assertFalse(reconcile.posted_match("[kimeru", "[kimeru #1] x"))               # too short for a containment match
        self.assertFalse(reconcile.posted_match(body, body.replace("OK 7", "OK 8")))


class TestReconcile(Base):
    def test_found_is_confirmed_not_found_is_retried_old_is_closed(self):
        seen = "山田架空 送信済み " + post_body(3).replace("\n", " ") + " 今日の 10:33."
        notice_seen = "[kimeru 通知] 自動で決定しました 架空の題名ZQX"
        b = FakeBridge(screen("雑談", seen, notice_seen))
        res = self.run_it(b)
        self.assertEqual(b.calls, ["probe"])
        self.assertEqual(res["status"], "done")
        ap = self.ap()
        i3, i4, i5 = ap["items"]["3"], ap["items"]["4"], ap["items"]["5"]
        # #3: seen -> confirmed like `delivery confirm`
        self.assertTrue(i3["posted"])
        self.assertNotIn("delivery_unknown", i3)
        self.assertEqual(i3["status"], "pending")
        # #4: not seen, young -> released like `delivery retry`: unposted, so the next cycle sends it again
        self.assertFalse(i4["posted"])
        self.assertNotIn("delivery_unknown", i4)
        self.assertNotIn("delivery_attempt", i4)
        # #5: not seen, older than 24 h -> closed: not sent again, not approved, still pending
        self.assertTrue(i5["posted"])
        self.assertEqual(i5["status"], "pending")
        self.assertNotIn("delivery_unknown", i5)
        self.assertIn("delivery_closed", i5)
        self.assertNotEqual(i5["status"], "approved")
        # notice: seen -> confirmed (marked as announced)
        self.assertEqual(ap["notice_delivery_unknown"], {})
        self.assertTrue(ap["notices"])
        # outbox: not seen, young -> kept in the outbox for the next flush
        self.assertNotIn("outbox_delivery_unknown", ap)
        self.assertEqual(ap["outbox"], ["[kimeru 実行 #3] fakeorg-zqx に書きました"])
        c = res["counts"]
        self.assertEqual((c["found"], c["retry"], c["close"], c["unchanged"]), (2, 3, 1, 0))
        # the brief: not seen -> the hold is released and today is not marked delivered (a new brief may go out)
        self.assertNotIn("brief_delivery_unknown", self.state())
        self.assertNotIn("brief_date", self.state())
        self.assertEqual(notify.delivery_pending(self.dir), [])
        log = [json.loads(l) for l in (self.dir / "delivery-recovery.log.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted(r["action"] for r in log).count("closed_not_delivered"), 1)
        self.assertTrue(all(r.get("source", "").startswith("reconcile") for r in log))

    def test_a_found_brief_marks_the_day_as_delivered(self):
        b = FakeBridge(screen("今日の 10:00 [kimeru brief 2026-10-09] 今日の進め方 架空の題名ZQX"))
        self.run_it(b)
        self.assertEqual(self.state().get("brief_date"), "2026-10-09")
        self.assertNotIn("brief_delivery_unknown", self.state())

    def test_an_old_notice_and_outbox_are_closed_without_sending(self):
        ap = self.ap()
        ap["notice_delivery_unknown"] = {k: _at(60 * 40) for k in ap["notice_delivery_unknown"]}
        ap["outbox_delivery_unknown_at"] = _at(60 * 40)
        ap["outbox_delivery_attempt"]["at"] = _at(60 * 40)
        (self.dir / "approvals.json").write_text(json.dumps(ap, ensure_ascii=False), encoding="utf-8")
        res = self.run_it(FakeBridge(screen("雑談")))
        ap = self.ap()
        self.assertEqual(ap["outbox"], [])
        self.assertNotIn("outbox_delivery_unknown", ap)
        self.assertEqual(ap["notice_delivery_unknown"], {})
        self.assertEqual(res["counts"]["close"], 3)   # #5, the notice, the outbox
        self.assertEqual(res["counts"]["found"], 0)

    def test_a_closed_case_is_not_posted_by_the_next_cycle(self):
        self.run_it(FakeBridge(screen("雑談")))

        class Bridge(FakeBridge):
            posted = []

            def post(self, text, send):
                Bridge.posted.append(text)
                return {"ok": True}
        notify.notify(self.dir, Bridge(), send=False)
        self.assertFalse(any("#5]" in t for t in Bridge.posted))
        self.assertTrue(any("#4]" in t for t in Bridge.posted))   # the young one is sent again

    def test_the_lock_busy_changes_nothing(self):
        before = {p.name: p.read_bytes() for p in self.dir.iterdir()}
        with fsutil.exclusive(notify.lock_path(self.dir)) as got:
            self.assertTrue(got)
            b = FakeBridge(screen("x"))
            res = self.run_it(b)
        self.assertEqual(res["status"], "busy")
        self.assertEqual(b.calls, [])
        self.assertIn("approvals.lock", self.lines(res))
        after = {p.name: p.read_bytes() for p in self.dir.iterdir() if p.name != "approvals.lock"}
        self.assertEqual({k: v for k, v in before.items() if k != "approvals.lock"}, after)

    def test_a_read_failure_changes_nothing(self):
        before = (self.dir / "approvals.json").read_bytes()
        res = self.run_it(FakeBridge(error=RuntimeError("Teams window not found")))
        self.assertEqual(res["status"], "no_read")
        self.assertEqual((self.dir / "approvals.json").read_bytes(), before)
        self.assertIn("読めませんでした", self.lines(res))

    def test_nothing_to_do(self):
        d = Path(tempfile.mkdtemp(prefix="kimeru-recon-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        b = FakeBridge(screen("x"))
        res = reconcile.reconcile(d, b, NOW)
        self.assertEqual(res["status"], "none")
        self.assertEqual(b.calls, [])

    def test_no_body_means_no_comparison(self):
        ap = self.ap()
        ap["items"]["3"]["delivery_attempt"] = {}
        ap["items"]["5"]["delivery_attempt"] = {}
        (self.dir / "approvals.json").write_text(json.dumps(ap, ensure_ascii=False), encoding="utf-8")
        res = self.run_it(FakeBridge(screen(post_body(3))))
        ap = self.ap()
        self.assertTrue(ap["items"]["3"]["delivery_unknown"])   # young and unreadable: left alone
        self.assertNotIn("delivery_unknown", ap["items"]["5"])  # old: closed
        self.assertEqual(res["counts"]["unchanged"], 1)

    def test_a_posted_message_is_never_sent(self):
        b = FakeBridge(screen("x"))
        self.run_it(b)   # FakeBridge.post raises: reconcile only probes


class TestOutput(Base):
    def test_numbers_only(self):
        res = self.run_it(FakeBridge(screen(post_body(3), "x")))
        text = self.lines(res)
        for s in SECRETS:
            self.assertNotIn(s, text)
        self.assertEqual(hidden_words.found(text), [])
        self.assertIn("- case #3 post: 届いていた", text)
        self.assertIn("- case #4 post: 見当たらない（次のサイクルで送り直す）", text)
        self.assertIn("- case #5 post: 見当たらない・24時間より古い", text)
        self.assertIn("- notice 通知1 notice: ", text)
        self.assertIn("- brief 2026-10-09 brief: ", text)

    def test_dispatch_writes_and_copies_the_file(self):
        out, drive = self.dir / "r.txt", self.dir / "drive"
        drive.mkdir()
        printed = []
        code = reconcile.dispatch(["--write", str(out), "--drive"], self.dir, printed.append, FakeBridge(screen(post_body(3))),
                                  [drive], NOW)
        self.assertEqual(code, 0)
        text = out.read_text(encoding="utf-8-sig")
        self.assertIn("配信の整理: 届いていた", text)
        self.assertEqual((drive / reconcile.RESULT_FILE).read_text(encoding="utf-8-sig"), text)
        for s in SECRETS:
            self.assertNotIn(s, text + "\n".join(printed))

    def test_the_command_line_has_the_subcommand(self):
        d = Path(tempfile.mkdtemp(prefix="kimeru-recon-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        r = subprocess.run([os.sys.executable, "-m", "kimeru", "--out", str(d), "delivery", "reconcile"], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8", timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("配信結果が不明の件はありません", r.stdout)


class TestScripts(unittest.TestCase):
    def test_the_launcher_and_the_script(self):
        cmd = (ROOT / "run-fix-deliveries.cmd").read_bytes()
        cmd.decode("ascii")
        self.assertIn(rb"tools\fix-deliveries.ps1", cmd)
        ps = (ROOT / "tools" / "fix-deliveries.ps1").read_bytes()
        self.assertTrue(ps.startswith(b"\xef\xbb\xbf"), "PowerShell 5.1 needs the BOM")
        text = ps.decode("utf-8-sig")
        for bad in ("Read-Host", "-Send", "'post'", "Invoke-WebRequest"):
            self.assertNotIn(bad, text)
        self.assertIn("kimeru-fix-deliveries-result.txt", (ROOT / ".gitignore").read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell only")
    def test_the_script_runs_under_windows_powershell(self):
        d = Path(tempfile.mkdtemp(prefix="kimeru-recon-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "data").mkdir()
        res = d / "result.txt"
        env = {**os.environ, "KIMERU_STATE_DIR": str(d / "data")}
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(ROOT / "tools" / "fix-deliveries.ps1"), "-NoClipboard", "-NoDrive", "-ResultFile", str(res)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("配信結果が不明の件はありません", res.read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
