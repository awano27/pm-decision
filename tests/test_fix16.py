"""Fix 16: the posts of the managed-PC check are marked as tests and never answer the daily cycle's items, `再実行 N-k` that is larger than
the count acts once when the count catches up, every form of a malformed redo is recorded once, a full text that cannot be kept does not park
the file, a missing inbox does not stop the cycle, and a delete that keeps failing is shown. Every call to ADO is mocked, every Teams is a
fake; nothing leaves the test."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from kimeru import cli, daily, fsutil, fulltext, notify, writer
from kimeru.backends import StubBackend
from tests import test_fulltext as TF
from tests.test_daily import GRAPHS, PBS, FakeTeams
from tests.test_fix14 import Redo
from tests.test_fix15 import replace_fails, warnings_of
from tests.test_notify import REC, FakeBridge, write_queue

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fake-teams-self.ps1"


class TimelineBridge(FakeBridge):
    """A self chat that is only a timeline (what tools/teams-self.ps1 reads)."""

    def __init__(self, timeline):
        super().__init__()
        self.timeline = list(timeline)

    def read(self):
        return {"ok": True, "timeline": list(self.timeline)}


def state_with_item(out):
    Path(out).mkdir(parents=True, exist_ok=True)
    write_queue(out, REC)
    notify.notify(out, FakeBridge(), send=True)


class TestATestPostIsNotAnApproval(unittest.TestCase):
    def test_the_real_reader_does_not_take_a_reply_under_a_test_post(self):
        self.assertEqual(notify.fresh_entries({"timeline": ["P:123", "T:123", "R:OK 123"]}), [])
        self.assertEqual(notify.fresh_replies({"timeline": ["P:123", "R:OK 123", "T:123"]}), ["OK 123"])      # the reply was for the real post
        self.assertEqual(notify.fresh_replies({"timeline": ["T:123", "P:123", "R:OK 123"]}), ["OK 123"])      # the real post came after the test
        self.assertEqual(notify.fresh_replies({"timeline": ["P:123", "T:123", "P:124", "R:OK 124"]}), ["OK 124"])

    def test_a_test_post_is_neither_a_boundary_nor_an_approval_post_for_the_real_reader(self):
        self.assertEqual(notify.fresh_entries({"timeline": ["T:5", "R:OK 5"]}), [])         # no real post of #5 on the screen: unproven, ignored
        self.assertEqual(notify.fresh_entries({"timeline": ["R:OK 5", "T:5"]}), [])
        self.assertEqual(notify.redo_k_entries({"timeline": ["P:7", "X:7:1", "T:7", "R:再実行 7-1"]}), [])
        self.assertEqual(notify.redo_k_entries({"timeline": ["P:7", "X:7:1", "R:再実行 7-1", "T:7"]}), [("7", 1)])
        self.assertEqual(notify.redo_bad_entries({"timeline": ["P:7", "T:7", "R:再実行 7/1"]}), [])

    def test_every_kind_of_reply_is_kept_from_the_real_item(self):
        for reply in ("OK 123", "NG 123", "保留 123", "再実行 123", "再実行 123-1", "済 123", "聞き返し 123", "修正 123 短く", "下書き 123 了解です"):
            self.assertEqual(notify.fresh_entries({"timeline": ["P:123", "X:123:1", "TX:123:1", f"R:{reply}"]}), [], reply)

    def test_the_test_reader_reads_its_own_posts_and_replies_only(self):
        tl = ["P:123", "T:123", "R:OK 123", "P:124", "R:OK 124"]
        self.assertEqual(notify.scoped_timeline({"timeline": tl}, test=True), ["P:123", "R:OK 123"])
        self.assertEqual(notify.scoped_timeline({"timeline": tl}, test=False), ["P:123", "P:124", "R:OK 124"])
        self.assertEqual(notify.scoped_timeline({"timeline": ["T:5", "TX:5:2", "R:再実行 5-2"]}, test=True), ["P:5", "X:5:2", "R:再実行 5-2"])

    def test_the_test_reader_takes_the_replies_after_its_post_and_not_those_of_a_real_item(self):
        with mock.patch.dict(os.environ, {notify.TEST_ENV: "1"}):
            self.assertEqual(notify.fresh_replies({"timeline": ["P:123", "R:OK 123", "T:123", "R:NG 123"]}), ["NG 123"])
            self.assertEqual(notify.fresh_replies({"timeline": ["T:123", "P:123", "R:OK 123"]}), [])
            self.assertEqual(notify.fresh_replies({"timeline": ["P:123", "R:OK 123"]}), [])     # no test post at all: nothing

    def test_collect_in_both_modes_with_a_real_item_and_a_test_post_of_the_same_number(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "real"
            state_with_item(out)
            tl = ["P:1", "T:1", "R:OK 1"]                      # the screen the managed-PC check makes: a test's "OK 1" under a test post
            self.assertEqual(list(notify.collect(out, TimelineBridge(tl))), [])
            self.assertEqual(notify.Approvals(out).data["items"]["1"]["status"], "pending")
            self.assertEqual(len(list(notify.collect(out, TimelineBridge(["P:1", "T:1", "R:OK 1", "P:1", "R:NG 1"])))), 1)   # the real item is answered

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {notify.TEST_ENV: "1"}):
            out = Path(d) / "test"
            state_with_item(out)                               # the item the check made (its post is a test post on the screen)
            self.assertEqual(list(notify.collect(out, TimelineBridge(["P:1", "R:OK 1"]))), [])       # a real item's reply is not the test's
            ch = list(notify.collect(out, TimelineBridge(["P:1", "T:1", "R:OK 1"])))
            self.assertEqual([(c["id"], c["status"]) for c in ch], [(1, "approved")])

    def test_the_bridge_marks_the_posts_of_a_test_run(self):
        sent = []
        b = notify.PowerShellBridge()
        with mock.patch.object(b, "_run", lambda *a: sent.append(a) or {"ok": True}):
            b.post("[kimeru #5] 判断\n返信", True)
            with mock.patch.dict(os.environ, {notify.TEST_ENV: "1"}):
                b.post("[kimeru #5] 判断\n返信", True)
                b.post("[kimeru 実行 #5 2] 実行しました", True)
                b.post("[kimeru brief 2026-10-02] x", True)
        texts = [a[a.index("-Text") + 1] for a in sent]
        self.assertTrue(texts[0].startswith("[kimeru #5]"))
        self.assertTrue(texts[1].startswith("[kimeru 試験 #5] 判断"))
        self.assertTrue(texts[2].startswith("[kimeru 試験 実行 #5 2] "))
        self.assertTrue(texts[3].startswith("[kimeru brief"))

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
    def test_the_reader_script_hands_back_test_posts_apart(self):
        with tempfile.TemporaryDirectory() as d:
            chat = Path(d) / "chat.json"
            chat.write_text(json.dumps({"messages": ["[kimeru #123] 本番", "[kimeru 試験 #123] 管理PCテスト", "OK 123", "[kimeru 試験 実行 #123 1] 結果",
                                                     "再実行 123-1"]}, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {"KIMERU_FAKE_CHAT": str(chat)}):
                read = notify.PowerShellBridge(script=FAKE).read()
            self.assertEqual(read["timeline"], ["P:123", "T:123", "R:OK 123", "TX:123:1", "R:再実行 123-1"])
            self.assertEqual(notify.fresh_entries(read), [])


class TestAFarRedoActsOnceWhenTheCountCatchesUp(Redo):
    def rows(self):
        p = self.out / "approvals.log.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_it_is_recorded_as_ahead_acts_once_and_never_again(self):
        bridge, fail = self.start()                                           # x_seq = 1
        self.cycle(bridge, http=fail, reply="再実行 1-3")
        self.assertEqual(self.n(), 1)
        self.assertEqual([(r["status"], r["k"], r["latest"]) for r in self.rows() if r["status"] != "approved"], [("redo_ahead", 3, 1)])
        self.cycle(bridge, http=fail, reply="再実行 1-1")
        self.cycle(bridge, http=fail, reply="再実行 1-2")
        self.assertEqual(self.n(), 3)                                         # the count is 3
        self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 4)                                         # the PM's "1-3" acts, once
        for _ in range(4):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 4)
        self.assertEqual([r["status"] for r in self.rows()].count("redo_ahead"), 1)
        self.assertEqual([r["status"] for r in self.rows()].count("redo_ignored"), 0)

    def test_an_old_count_is_told_apart_from_one_that_is_too_large(self):
        bridge, fail = self.start()
        self.cycle(bridge, http=fail, reply="再実行 1-1")                      # acts: the count is 2
        self.cycle(bridge, http=fail, reply="再実行 1-5")
        self.cycle(bridge, http=fail)
        self.cycle(bridge, http=fail, reply="再実行 1-2")                      # acts: the count is 3
        statuses = [r["status"] for r in self.rows()]
        self.assertEqual(statuses.count("redo_ahead"), 1)
        t_old = notify.ack_text([{"id": 1, "status": "redo_ignored"}])[1]
        t_far = notify.ack_text([{"id": 1, "status": "redo_ahead"}])[1]
        self.assertIn("古い回数", t_old)
        self.assertNotIn("古い回数", t_far)
        self.assertIn("大きすぎ", t_far)


class TestEveryFormOfAMalformedRedoIsRecorded(Redo):
    def rows(self):
        p = self.out / "approvals.log.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_three_forms_of_one_item_are_three_records_and_no_reply_text_is_kept(self):
        bridge, fail = self.start()
        for reply in ("再実行 1/2", "再実行 1〜2", "再実行 1 2", "再実行 1/2", "再実行 1/3"):   # the last two are in a form that was recorded
            self.cycle(bridge, http=fail, reply=reply)
        for _ in range(2):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 1)                                         # none of them runs anything
        self.assertEqual([r["status"] for r in self.rows()].count("redo_malformed"), 3)
        for f in self.out.glob("*.json*"):
            text = f.read_text(encoding="utf-8")
            for word in ("1/2", "1〜2", "1 2", "1/3"):
                self.assertNotIn("再実行 " + word, text, f.name)
        self.assertEqual(len(self.state()["redo_malformed_kinds"]), 3)

    def test_the_forms_whose_separator_cannot_be_read_are_malformed_forms(self):
        for line in ("再実行 1一2", "再実行 1_2", "再実行 1 2", "再実行 1ー", "再実行形式 1 /2", "再実行形式 1", "再実行 1/2", "再実行 1〜2", "再実行 1~"):
            self.assertEqual(notify.parse_redo_bad(line), "1", line)
            self.assertIsNotNone(notify.redo_bad_kind(line), line)
        for line in ("再実行 1", "再実行 1-2", "再実行 1ー2", "再実行 1 お願い", "再実行 1。", "OK 1", "再実行"):
            self.assertIsNone(notify.parse_redo_bad(line), line)
        self.assertNotEqual(notify.redo_bad_kind("再実行 1/2"), notify.redo_bad_kind("再実行 1〜2"))
        self.assertEqual(notify.redo_bad_kind("再実行 1/2"), notify.redo_bad_kind("再実行　１／９"))   # the same form, other digits

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
    def test_the_reader_script_hands_these_back_and_not_a_sentence(self):
        with tempfile.TemporaryDirectory() as d:
            chat = Path(d) / "chat.json"
            lines = ["再実行 1一2", "再実行 1_2", "再実行 1 2", "再実行 1ー", "再実行 1/2", "再実行 1 お願い"]
            chat.write_text(json.dumps({"messages": ["[kimeru #1] x"] + lines}, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {"KIMERU_FAKE_CHAT": str(chat)}):
                tl = notify.PowerShellBridge(script=FAKE).read()["timeline"]
            kinds = [notify.redo_bad_kind(e[2:]) for e in tl if e.startswith("R:")]
            self.assertEqual(len([k for k in kinds if k]), 5, tl)             # the sentence is not a form


class TestAFullTextThatCannotBeKept(TF.Base):
    def deny(self):
        real = fsutil.exclusive

        def exclusive(path, *a, **k):
            if Path(path).name == "full_text.lock":
                raise fsutil.LockFolderError("cannot create the lock file: the folder cannot be written to")
            return real(path, *a, **k)
        return mock.patch.object(fsutil, "exclusive", exclusive)

    def test_the_item_is_decided_from_the_preview_and_the_file_is_not_parked(self):
        self.enable()
        long = TF.long_text(2500)
        self.teams.chat_messages = {"19:c1@thread.v2": [long]}
        (self.inbox / "e1.json").write_text(json.dumps(self.ev(1, long[:70] + "…"), ensure_ascii=False), encoding="utf-8")
        with self.deny():
            n = daily.process_inbox(self.inbox, self.out, GRAPHS, StubBackend(), PBS, cli.process,
                                    reader=fulltext.Reader(self.teams))
        self.assertEqual(n, 1)
        done = sorted(p.name for p in (self.inbox / "done").iterdir())
        self.assertEqual(done, ["e1.json"], done)                           # not e1.json.error
        self.assertFalse((self.inbox / "e1.json").exists())
        self.assertTrue(any("save failed" in w.get("full_text", "") for w in warnings_of(self.out)))
        queued = [json.loads(l) for l in (self.out / "queue.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(queued[0]["read_full"]["state"], "preview_only")   # and the post says so
        posted = notify.notify(self.out, self.teams, send=True)
        self.assertEqual(len(posted), 1)
        self.assertIn("プレビューだけで判断しました", next(t for t, _ in self.teams.posts if t.startswith("[kimeru #")))

    def test_the_same_with_a_writer(self):
        self.enable()
        long = TF.long_text(2500)
        self.teams.chat_messages = {"19:c1@thread.v2": [long]}
        with self.deny():
            res = cli.process(self.ev(1, long[:70] + "…"), GRAPHS, StubBackend(), self.out, PBS, writer=writer.M365PromptWriter(), dedup=True,
                              reader=fulltext.Reader(self.teams))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["read_full"]["state"], "preview_only")


class TestAMissingInboxDoesNotStopTheCycle(unittest.TestCase):
    def test_the_cycle_returns_a_report(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "afile"
            blocker.write_text("x", encoding="utf-8")
            out, inbox = Path(d) / "out", blocker / "inbox"                 # an inbox that does not exist and cannot be made
            r = daily.cycle(out, inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=FakeTeams(), send=True, now=datetime(2026, 10, 2, 9, 0))
            self.assertTrue(str(r["waiting"]).startswith("error"), r)
            self.assertTrue(str(r["judge"]).startswith("error"), r)
            self.assertIn("approvals", r)
            rows = [json.loads(l) for l in (out / "daily.log.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[-1]["step"], "cycle")
            self.assertTrue(any("failed steps" in l and "waiting" in l for l in daily.status_lines(out)))


class TestADeleteThatKeepsFailingIsShown(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out, self.inbox = Path(self.dir.name) / "out", Path(self.dir.name) / "inbox"
        self.inbox.mkdir()
        self.out.mkdir()
        write_queue(self.out, REC, {**REC, "event_id": "4813"}, {**REC, "event_id": "4814"})   # the third one still waits: its text stays whatever happens
        self.keys = []
        notify.notify(self.out, FakeBridge(), send=True)
        self.keys = [it["key"] for it in notify.Approvals(self.out).data["items"].values()]
        for k in self.keys:
            fulltext.save(self.out, k, {"text": "全文 " + k})
        with replace_fails(), mock.patch.object(fulltext.time, "sleep", lambda s: None):
            notify.collect(self.out, FakeBridge(["OK 1", "NG 2"]))      # approved and rejected, and the texts could not be deleted
        fulltext.new_cycle()
        (self.out / "warnings.jsonl").unlink(missing_ok=True)             # what the three cycles below write, and only that

    def tearDown(self):
        self.dir.cleanup()

    def cycle(self, minute):
        return daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=FakeTeams(), send=True,
                           now=datetime(2026, 10, 2, 7, minute))

    def test_three_cycles_show_it_in_the_report_and_the_status_and_write_one_line_each(self):
        with replace_fails(), mock.patch.object(fulltext.time, "sleep", lambda s: None):
            for i in range(3):
                r = self.cycle(i * 5)
                self.assertGreaterEqual(r.get("full_text_delete_failed", 0), 1, r)
        full = [w for w in warnings_of(self.out) if "full_text" in w]
        by_what = {}
        for w in full:
            by_what[w["full_text"]] = by_what.get(w["full_text"], 0) + 1
        self.assertTrue(by_what and all(v == 3 for v in by_what.values()), by_what)     # one line per cycle for each failure, not one per attempt
        self.assertTrue(any("could not be deleted" in l for l in daily.status_lines(self.out)))
        r = self.cycle(30)                                                  # the folder is fine again: deleted, and the report is quiet
        self.assertNotIn("full_text_delete_failed", r)
        for k in self.keys[:2]:                                             # the two decided items; the third one still waits
            self.assertIsNone(fulltext.load(self.out, k))
        self.assertIsNotNone(fulltext.load(self.out, self.keys[2]))
        self.assertFalse(any("could not be deleted" in l for l in daily.status_lines(self.out)))

    def test_warnings_jsonl_is_cut_to_its_newest_lines(self):
        fulltext.new_cycle()
        for i in range(2500):
            fulltext._record_failure(self.out, f"full_text.json (case {i} " + "x" * 60 + ")")
        lines = (self.out / "warnings.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertLess(len(lines), 1500)
        self.assertIn("case 2499", lines[-1])
        self.assertFalse((self.out / "warnings.jsonl.bak").exists())

    def test_an_unexpected_exception_of_a_drop_is_noted_too(self):
        fulltext.new_cycle()
        with mock.patch.object(fulltext, "drop", side_effect=ValueError("odd")):
            self.assertFalse(fulltext.drop_safe(self.out, "k"))
        self.assertTrue(any("ValueError" in w.get("full_text", "") for w in warnings_of(self.out)))
        self.assertEqual(fulltext.failures(), 1)
        with mock.patch.object(fulltext, "purge_locked", side_effect=KeyError("odd")):
            self.assertEqual(fulltext.purge_safe(self.out), [])
        self.assertTrue(any("KeyError" in w.get("full_text", "") for w in warnings_of(self.out)))


if __name__ == "__main__":
    unittest.main()
