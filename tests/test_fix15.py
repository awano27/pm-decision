"""Fix 15: a delete that fails does not stop the notify or the approvals, the daily cycle does not stop on a lock error, and the
details of `再実行 N-k` (an acted reply is not recorded as ignored, a k that is too large stays ignored, every dash is read, a form
that is not read is recorded). Every call to ADO is mocked, every Teams is a fake; nothing leaves the test."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru import cli, daily, fsutil, fulltext, notify, pull
from kimeru.backends import StubBackend
from tests.test_daily import GRAPHS, PBS, FakeTeams, one_on_one
from tests.test_execute import PBS as PBS_X, WI, NoCriteria
from tests.test_execute import GRAPHS as GRAPHS_X
from tests.test_execute_safety import Safety
from tests.test_fix14 import FAKE_READER, REAL_READER, Redo, screen_tail
from tests.test_notify import REC, FakeBridge, write_queue

LONG_AGO = datetime.now(timezone.utc) - timedelta(days=30)


def warnings_of(out):
    p = Path(out) / "warnings.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def full_text_notes(out):
    return [w["full_text"] for w in warnings_of(out) if "full_text" in w]


def stuck(names):
    real = fsutil._unlink_retry
    return mock.patch.object(fsutil, "_unlink_retry", lambda p, tries=25: False if Path(p).name in names else real(p, tries))


def replace_fails():
    real = fsutil._replace

    def replace(src, dst, tries=40):
        if Path(dst).name == "full_text.json":
            raise PermissionError(13, "the file is open in another process")
        return real(src, dst, tries)
    return mock.patch.object(fsutil, "_replace", replace)


def read_fails():
    real = Path.read_text

    def read_text(self, *a, **k):
        if self.name == "full_text.json":
            raise PermissionError(13, "the file is open in another process")
        return real(self, *a, **k)
    return mock.patch.object(Path, "read_text", read_text)


def no_sleep():
    return mock.patch.object(fulltext.time, "sleep", lambda s: None)


class TestAPurgeThatFailsDoesNotStopTheNotify(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name)
        write_queue(self.out, REC)

    def tearDown(self):
        self.dir.cleanup()

    def run_notify(self, ctx):
        bridge = FakeBridge()
        with ctx, no_sleep():
            self.assertEqual(notify.notify(self.out, bridge, send=True), [1])   # the new item is posted whatever the purge did
        self.assertEqual(len(bridge.posts), 1)
        self.assertTrue(notify.Approvals(self.out).data["items"]["1"]["posted"])

    def test_a_file_that_cannot_be_removed(self):
        fulltext.save(self.out, "g:old:n", {"text": "古い全文"}, now=LONG_AGO)
        self.run_notify(stuck({"full_text.json"}))
        self.assertTrue(any("delete failed" in n and "removed" in n for n in full_text_notes(self.out)), full_text_notes(self.out))
        self.assertTrue((self.out / "full_text.json").exists())            # it stays, and the next cycle tries again
        notify.notify(self.out, FakeBridge(), send=True)
        self.assertFalse((self.out / "full_text.json").exists())

    def test_a_file_that_cannot_be_replaced(self):
        fulltext.save(self.out, "g:old:n", {"text": "古い全文"}, now=LONG_AGO)
        fulltext.save(self.out, "g:new:n", {"text": "新しい全文"})
        self.run_notify(replace_fails())
        self.assertTrue(any("delete failed" in n and "replaced" in n for n in full_text_notes(self.out)), full_text_notes(self.out))
        self.assertIsNotNone(fulltext.load(self.out, "g:new:n"))
        notify.notify(self.out, FakeBridge(), send=True)
        self.assertEqual(list(json.loads((self.out / "full_text.json").read_text(encoding="utf-8"))), ["g:new:n"])   # the old one went on the retry

    def test_a_file_that_cannot_be_read_is_not_taken_for_an_empty_one(self):
        fulltext.save(self.out, "g:old:n", {"text": "古い全文"}, now=LONG_AGO)
        self.run_notify(read_fails())
        self.assertTrue(any("delete failed" in n and "read" in n for n in full_text_notes(self.out)), full_text_notes(self.out))
        self.assertTrue((self.out / "full_text.json").exists())            # not deleted as if it were empty
        with self.assertRaises(fulltext.DeleteFailed), read_fails(), no_sleep():
            fulltext.drop(self.out, "g:old:n")                              # and a drop does not say "nothing to drop"

    def test_all_three_are_in_warnings_jsonl(self):
        for ctx, fresh in ((stuck({"full_text.json"}), False), (replace_fails(), True), (read_fails(), True)):
            for f in self.out.glob("full_text.json*"):
                f.unlink()
            fulltext.save(self.out, "g:old:n", {"text": "古い全文"}, now=LONG_AGO)
            if fresh:   # a text that stays, so that the file is written again (a replace) and not removed
                fulltext.save(self.out, "g:new:n", {"text": "新しい全文"})
            with ctx, no_sleep():
                self.assertEqual(fulltext.purge_safe(self.out), [])   # nothing was deleted, and nothing raised
        notes = " | ".join(full_text_notes(self.out))
        for word in ("removed", "replaced", "read"):
            self.assertIn(word, notes)


class TestADropThatFailsDoesNotStopTheApprovals(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name)
        write_queue(self.out, REC, {**REC, "event_id": "4813"}, {**REC, "event_id": "4814"})
        self.assertEqual(notify.notify(self.out, FakeBridge(), send=True), [1, 2, 3])
        self.keys = [it["key"] for it in notify.Approvals(self.out).data["items"].values()]
        for k in self.keys:
            fulltext.save(self.out, k, {"text": "全文 " + k})

    def tearDown(self):
        self.dir.cleanup()

    def log(self):
        p = self.out / "approvals.log.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_the_approvals_go_on_and_every_line_of_the_log_is_there(self):
        with replace_fails(), no_sleep():
            ch = notify.collect(self.out, FakeBridge(["OK 2", "OK 1", "NG 3"]))
        self.assertEqual([(c["id"], c["status"]) for c in ch], [(2, "approved"), (1, "approved"), (3, "rejected")])
        self.assertEqual([(r["id"], r["status"]) for r in self.log()], [(2, "approved"), (1, "approved"), (3, "rejected")])
        self.assertEqual({it["status"] for it in notify.Approvals(self.out).data["items"].values()}, {"approved", "rejected"})
        self.assertEqual(len([n for n in full_text_notes(self.out) if "replaced" in n]), 3)
        for k in self.keys:
            self.assertIsNotNone(fulltext.load(self.out, k))                # still on the disk, and noted
        notify.notify(self.out, FakeBridge(), send=True)                     # the next cycle's purge deletes them: decided items
        for k in self.keys:
            self.assertIsNone(fulltext.load(self.out, k))
        self.assertFalse((self.out / "full_text.json").exists())

    def test_a_text_of_an_item_that_still_waits_is_not_taken_by_that_purge(self):
        with replace_fails(), no_sleep():
            notify.collect(self.out, FakeBridge(["OK 1"]))
        notify.notify(self.out, FakeBridge(), send=True)
        self.assertIsNone(fulltext.load(self.out, self.keys[0]))
        self.assertIsNotNone(fulltext.load(self.out, self.keys[1]))
        self.assertIsNotNone(fulltext.load(self.out, self.keys[2]))


class TestAnExecutedItemIsNotLostByADropThatFails(Safety):
    def test_the_post_to_ado_is_made_the_log_line_is_written_and_the_reply_after_it_is_read(self):
        self.enable()
        bridge = self.decide_and_post()
        key = notify.Approvals(self.out).data["items"]["1"]["key"]
        fulltext.save(self.out, key, {"text": "全文"})
        with stuck({"full_text.json"}):
            ch = self.cycle(bridge, http=self.http_with(), reply="OK 1")
        self.assertEqual(len(self.posts_of("POST")), 1)
        self.assertEqual([c["status"] for c in ch], ["approved"])
        rows = [json.loads(l) for l in (self.out / "approvals.log.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["status"] for r in rows], ["approved"])
        self.assertTrue(any("delete failed" in n for n in full_text_notes(self.out)))
        self.cycle(bridge, http=self.http_with())
        self.assertEqual(len(self.posts_of("POST")), 1)                      # and never twice


def deny_the_lock(path, stale_sec=1800):
    raise fsutil.LockFolderError(f"cannot create the lock file {path}: the folder cannot be written to (check its permissions)")


class TestTheDailyCycleDoesNotStopWhenTheLockCannotBeCreated(unittest.TestCase):
    def test_one_cycle_without_a_lock_keeps_its_records_and_parks_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            out, inbox, t = Path(d) / "out", Path(d) / "inbox", FakeTeams()
            toasts = []
            run = lambda now: daily.cycle(out, inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=t, send=True, now=now,
                                          toaster=lambda title, body: toasts.append(title))
            t.chat_list = [one_on_one("おはようございます", "8:00")]
            run(datetime(2026, 9, 28, 7, 0))                                # the baseline
            ap = notify.Approvals(out)
            ap.data["unposted_announced"] = 2                                # "2 unposted" was announced and none is unposted now: the notice is cleared
            ap.save()
            t.chat_list = [one_on_one("これは何ですか", "8:05")]
            with mock.patch.object(fsutil, "exclusive", deny_the_lock):
                r = run(datetime(2026, 9, 28, 8, 10))                        # no exception
            self.assertEqual(r["judge"], 1)
            self.assertTrue(str(r["notify"]).startswith("error"), r)         # the steps that need the lock fail alone ...
            self.assertTrue(str(r["toast_clear"]).startswith("error"), r)
            self.assertIsInstance(r["brief"], int)                           # ... and the brief, and the record of the cycle, are there
            rows = [json.loads(l) for l in (out / "daily.log.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[-1]["step"], "cycle")
            self.assertEqual(rows[-1]["report"]["judge"], 1)
            done = list((inbox / "done").iterdir())
            self.assertEqual(len(done), 1)
            self.assertFalse([f for f in done if f.name.endswith(".error")], done)
            self.assertTrue(any("merge" in w for w in warnings_of(out)))      # the message was a matter of its own, and that is noted
            r = run(datetime(2026, 9, 28, 8, 20))                            # the next cycle, with the folder fine, does what was left
            self.assertEqual(len(r["notify"]), 1)

    def test_the_approvals_step_is_there_too(self):
        with tempfile.TemporaryDirectory() as d:
            out, inbox, t = Path(d) / "out", Path(d) / "inbox", FakeTeams()
            with mock.patch.object(fsutil, "exclusive", deny_the_lock):
                r = daily.cycle(out, inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=t, send=True, now=datetime(2026, 9, 28, 7, 0))
            self.assertTrue(str(r["approvals"]).startswith("error"), r)

    def test_the_merge_of_a_follow_up_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            with mock.patch.object(fsutil, "exclusive", deny_the_lock):
                self.assertFalse(cli._merge_into_pending(out, {"kind": "teams.chat", "chat_id": "c", "author": "a", "id": "e1"}, {}))
            self.assertTrue(any("merge" in w for w in warnings_of(out)))

    def test_a_log_that_cannot_be_written_does_not_stop_the_cycle(self):
        with tempfile.TemporaryDirectory() as d:
            real = Path.open

            def opener(self, *a, **k):
                if self.name == "daily.log.jsonl":
                    raise PermissionError(13, "denied")
                return real(self, *a, **k)
            with mock.patch.object(Path, "open", opener):
                daily._log(Path(d), {"step": "x"})                           # no exception


class TestTheRedoReplyAfterTheResultPost(Redo):
    def rows(self):
        p = self.out / "approvals.log.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def ignored(self):
        return [r for r in self.rows() if r["status"] == "redo_ignored"]

    def test_a_reply_that_acted_is_never_recorded_as_ignored_later(self):
        bridge, fail = self.start()
        self.cycle(bridge, http=fail, reply="再実行 1-1")
        self.assertEqual(self.n(), 2)
        for _ in range(3):                                                   # the result 2 is out: the reply 1-1 stands above it
            self.cycle(bridge, http=fail)
        self.cycle(bridge, http=fail, reply="再実行 1-2")
        self.assertEqual(self.n(), 3)
        for _ in range(3):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 3)
        self.assertEqual(self.ignored(), [])                                 # nothing was ignored: every reply acted
        self.assertEqual([r["status"] for r in self.rows()].count("redo"), 2)

    def test_a_reply_that_is_really_ignored_is_recorded_once_per_reply(self):
        bridge, fail = self.start()
        self.cycle(bridge, http=fail, reply="再実行 1")                    # result 2
        self.cycle(bridge, http=fail, reply="再実行 1-1")                  # an old count
        for _ in range(3):
            self.cycle(bridge, http=fail)
        self.assertEqual([(r["k"], r["latest"]) for r in self.ignored()], [(1, 2)])
        self.assertEqual(self.n(), 2)

    def test_a_k_larger_than_the_count_stays_ignored_when_the_count_catches_up(self):
        bridge, fail = self.start()
        self.cycle(bridge, http=fail, reply="再実行 1-3")
        self.assertEqual(self.n(), 1)
        self.assertEqual([(r["k"], r["latest"]) for r in self.ignored()], [(3, 1)])
        self.cycle(bridge, http=fail, reply="再実行 1-1")
        self.assertEqual(self.n(), 2)
        self.cycle(bridge, http=fail, reply="再実行 1-2")
        self.assertEqual(self.n(), 3)                                        # the count is 3 now, and the old "1-3" is still above
        for _ in range(4):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 3)                                        # it never acts
        self.assertEqual(len(self.ignored()), 1)                             # and is not recorded again
        self.cycle(bridge, http=fail, reply="再実行 1-3")                  # the same words again do not act either (decided: stays ignored)
        self.assertEqual(self.n(), 3)
        self.assertEqual(len(self.ignored()), 1)

    def test_every_dash_is_read_as_a_dash(self):
        for d in "-ー−‐–－":
            self.assertEqual(notify.parse_redo_k(f"再実行 1{d}2"), ("1", 2), repr(d))
            self.assertEqual(notify.parse_redo_k(f"再実行　#１{d}　２。"), ("1", 2), repr(d))
            self.assertIsNone(notify.parse_redo_bad(f"再実行 1{d}2"))
        self.assertIsNone(notify.parse_redo_k("再実行 1"))
        self.assertIsNone(notify.parse_redo_k("OK 3-1"))

    def test_the_replies_that_are_not_redo_k_are_read_as_before(self):
        words = ("OK", "NG", "保留", "聞き返し", "再実行", "済")
        forms = ("{} 3", "{}3", "{}　３", "{} #3", "{} 3。", "{}3!", "{} ＃３")
        self.assertEqual(len(words) * len(forms), 42)
        for w in words:
            for form in forms:
                self.assertEqual(notify.parse_reply(form.format(w)), (w.upper(), "3"), form.format(w))
        for line in ("再実行 3ー", "再実行ー3", "OK 3ー1", "再実行 3 お願いします"):
            self.assertIsNone(notify.parse_redo_k(line), line)

    def test_a_reply_with_a_dash_of_each_kind_acts_once(self):
        bridge, fail = self.start()
        self.cycle(bridge, http=fail, reply="再実行 1ー1")
        self.assertEqual(self.n(), 2)
        self.cycle(bridge, http=fail, reply="再実行 1−2")
        self.assertEqual(self.n(), 3)
        self.cycle(bridge, http=fail, reply="再実行 1‐3")
        self.assertEqual(self.n(), 4)
        for _ in range(3):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 4)
        self.assertEqual(self.ignored(), [])

    def test_a_form_that_is_not_read_is_recorded_not_dropped(self):
        for line in ("再実行 1〜1", "再実行 1/1", "再実行 1～1"):
            self.assertEqual(notify.parse_redo_bad(line), "1", line)
        self.assertIsNone(notify.parse_redo_bad("再実行 1 お願いします"))
        self.assertEqual(notify.parse_redo_bad("再実行形式 1"), "1")
        bridge, fail = self.start()
        self.cycle(bridge, http=fail, reply="再実行 1〜1")
        for _ in range(3):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 1)                                        # it did not act
        bad = [r for r in self.rows() if r["status"] == "redo_malformed"]
        self.assertEqual(len(bad), 1)                                        # and it was recorded, once
        self.assertEqual(bad[0]["id"], 1)
        self.assertNotIn("1〜1", json.dumps(self.rows(), ensure_ascii=False))   # the text of the reply is not kept
        self.assertIn("redo_malformed", notify.ACK)


class TestTheTailOfTheScreenWithTheRedoKReply(Redo):
    """Replies of the form `再実行 N-k` on a screen that shows the last messages only, with other items between (tests that use the
    bare `再実行 N` pass with a reader that does not know `再実行 N-k`; these do not)."""

    def go(self, tail, noise):
        bridge, fail = self.start()
        screen_tail(bridge, tail, noise)
        for n in range(2, 7):
            self.cycle(bridge, http=fail, reply=f"再実行 1-{n - 1}")        # right after the result it answers
            self.assertEqual(self.n(), n, f"round {n}")
            self.cycle(bridge, http=fail)
            self.cycle(bridge, http=fail)
            self.assertEqual(self.n(), n, f"round {n} again")
        rows = [json.loads(l) for l in (self.out / "approvals.log.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r for r in rows if r["status"] == "redo_ignored"], [])

    def test_last_three(self):
        self.go(3, 0)

    def test_last_ten_with_other_items(self):
        self.go(10, 5)

    def test_a_reply_right_after_the_one_that_acted_and_a_third_that_is_folded(self):
        bridge, fail = self.start()
        screen_tail(bridge, 3)
        self.cycle(bridge, http=fail, reply="再実行 1-1")                   # acts; its result post (count 2) is the newest message
        self.assertEqual(self.n(), 2)
        bridge.replies.append("再実行 1-2")                                  # sent right after that result
        bridge.replies.append("再実行 1-2")                                  # a third, identical: folded into the second on the screen
        self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 3)
        for _ in range(3):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 3)

    def test_the_same_with_the_result_posts_still_in_the_outbox(self):
        """The numbers are used again (the earlier item's result posts stand above the approval post) and this item's own result posts
        have not reached the chat yet: they wait in the outbox."""
        bridge, fail = self.start()
        old = [("P", f"[kimeru 実行 #1 {k}] 前の件の結果") for k in (1, 2, 3)]
        i = next(i for i, (k, t) in enumerate(bridge._chron) if t.startswith("[kimeru #1]"))
        bridge._chron[i:i] = old
        bridge._chron = [(k, t) for k, t in bridge._chron if not (k == "P" and t.startswith("[kimeru 実行 #1 ") and "前の件" not in t)]
        bridge.fail_posts = True                                              # Teams cannot be reached: the results wait in the outbox
        self.cycle(bridge, http=fail, reply="再実行 1")
        self.assertEqual(self.n(), 2)
        for _ in range(4):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 2)
        self.assertTrue(notify.Approvals(self.out).data.get("outbox"))
        self.cycle(bridge, http=fail, reply="再実行 1-2")                  # the PM saw the second result in the phone's notification
        self.assertEqual(self.n(), 3)
        bridge.fail_posts = False
        for _ in range(4):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 3)                                         # delivered at last: no reply acts again
        self.cycle(bridge, http=fail, reply="再実行 1")
        self.assertEqual(self.n(), 4)


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
class TestTheFakeReaderReadsEveryDash(unittest.TestCase):
    def read(self, messages):
        with tempfile.TemporaryDirectory() as d:
            chat = Path(d) / "chat.json"
            chat.write_text(json.dumps({"messages": messages}, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {"KIMERU_FAKE_CHAT": str(chat)}):
                return notify.PowerShellBridge(script=FAKE_READER).read()["timeline"]

    def test_the_six_dashes_are_one_reply_and_other_marks_are_a_form_that_is_not_read(self):
        msgs = ["[kimeru #1] x"] + [f"再実行 1{d}2" for d in "ー−‐–－-"]
        self.assertEqual(self.read(msgs), ["P:1", "R:再実行 1-2"])           # the same reply, folded
        self.assertEqual(self.read(["[kimeru #1] x", "再実行 1〜2"]), ["P:1", "R:再実行形式 1"])
        self.assertEqual(self.read(["[kimeru #1] x", "再実行 1 お願いします"]), ["P:1"])

    def test_the_real_reader_has_the_same_regexes_as_the_fake_one(self):
        real = REAL_READER.read_text(encoding="utf-8-sig")
        fake = FAKE_READER.read_text(encoding="utf-8-sig")
        for rx in (re.compile(r"-match '(\^再実行\\s\*#\?\(\\d\+\)\\s\*\[-[^']*)'"), re.compile(r"-match '(\^再実行\\s\*#\?\(\\d\+\)\\s\*\[\^[^']*)'")):
            a, b = rx.findall(real), rx.findall(fake)
            self.assertEqual((len(a), a), (1, b), rx.pattern)
        self.assertIn("ー−‐", real)


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
class TestRedoKThroughTheFakeTeams(Safety):
    """`再実行 N-k` through PowerShellBridge and tests/fake-teams-self.ps1, which shows only the last messages and folds identical
    lines: a reader that does not know the form would never see these replies."""

    def setUp(self):
        super().setUp()
        self.chat = Path(self.dir.name) / "chat.json"
        os.environ["KIMERU_FAKE_CHAT"] = str(self.chat)

    def say(self, text):
        chat = json.loads(self.chat.read_text(encoding="utf-8-sig")) if self.chat.exists() else {"messages": []}
        chat["messages"].append(text)
        self.chat.write_text(json.dumps(chat, ensure_ascii=False), encoding="utf-8")

    def go(self, tail, noise):
        self.enable()
        cli.process(WI, GRAPHS_X, NoCriteria(), self.out, PBS_X)
        bridge = notify.PowerShellBridge(script=FAKE_READER)
        notify.notify(self.out, bridge, send=True, real=True)
        os.environ["KIMERU_FAKE_TAIL"] = str(tail)
        fail = self.http_with(post_error=pull.PullError("HTTP 404 for <url>"))
        self.say("OK 1")
        self.cycle(bridge, http=fail)
        self.assertEqual(len(self.posts_of("POST")), 1)
        for n in range(2, 6):
            self.say(f"再実行 1-{n - 1}")
            self.cycle(bridge, http=fail)
            self.assertEqual(len(self.posts_of("POST")), n, f"round {n}")
            for m in ("[kimeru #2] a", "[kimeru 実行 #2 1] b", "[kimeru #3] c", "OK 3", "[kimeru 実行 #3 1] d")[:noise]:
                self.say(m)
            self.cycle(bridge, http=fail)
            self.cycle(bridge, http=fail)
            self.assertEqual(len(self.posts_of("POST")), n, f"round {n} again")
        p = self.out / "approvals.log.jsonl"
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r for r in rows if r["status"] == "redo_ignored"], [])

    def test_the_last_three_messages(self):
        self.go(3, 0)

    def test_the_last_ten_messages_with_other_items(self):
        self.go(10, 5)


if __name__ == "__main__":
    unittest.main()
