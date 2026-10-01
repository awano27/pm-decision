"""Fix 13: `再実行` judged by which result post a reply follows (not by counts on the screen), a lock owner told by the creation time of
its process, a release that is retried, damaged lock files, full_text.json read while it is replaced, the exit code of `approvals`,
the T22 read-back without the text in the command line. Every call to ADO is mocked, every Teams is a fake; nothing leaves the test."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kimeru import cli, execute, fsutil, fulltext, notify, pull
from tests.test_execute import PBS, GRAPHS, WI, NoCriteria, ROOT
from tests.test_execute_safety import Safety

FAKE = ROOT / "tests" / "fake-teams-self.ps1"
DEAD_PID = 2147483646


def screen(bridge, tail=None, noise=0, fold=True):
    """What a real Teams window shows of the bridge's chat: only the last `tail` entries (None: all of them), consecutive identical
    lines folded into one, and `noise` entries of other items after every result post of #1 (a chat with other business in it)."""
    real = bridge.read

    def read():
        r = real()
        tl = []
        for e in r["timeline"]:
            tl.append(e)
            if e.startswith("X:1:"):
                tl.extend(["P:2", "X:2:1", "P:3", "R:OK 3", "X:3:1", "P:4", "X:4:1", "X:5:1"][:noise])
        if tail:
            tl = tl[-tail:]
        if fold:
            tl = [e for i, e in enumerate(tl) if i == 0 or tl[i - 1] != e]
        return {**r, "timeline": tl}
    bridge.read = read
    return real


class TestRedoByTheResultItFollows(Safety):
    FAIL = pull.PullError("HTTP 404 for <url>")

    def start(self):
        self.enable()
        bridge = self.decide_and_post()
        fail = self.http_with(post_error=self.FAIL)
        self.cycle(bridge, http=fail, reply="OK 1")            # POST 1 fails: a sure failure; its result is result post 1
        return bridge, fail

    def posts_n(self):
        return len(self.posts_of("POST"))

    def series(self, bridge, fail, redos=2):
        """OK -> result -> redo -> result -> redo ...: every redo reply acts once, however many cycles read it."""
        self.assertEqual(self.posts_n(), 1)
        for n in range(2, redos + 2):
            self.cycle(bridge, http=fail, reply="再実行 1")
            self.assertEqual(self.posts_n(), n)
            for _ in range(3):
                self.cycle(bridge, http=fail)
            self.assertEqual(self.posts_n(), n)

    def test_the_result_post_carries_its_count(self):
        bridge, fail = self.start()
        self.cycle(bridge, http=fail, reply="再実行 1")
        results = [p.split("\n")[0].split("]")[0] for p in bridge.posts if p.startswith(("[kimeru 実行 #1]", "[kimeru 実行 #1 "))]
        self.assertEqual(results, ["[kimeru 実行 #1 1", "[kimeru 実行 #1 2"])
        self.assertEqual(notify.parse_result_entry("X:1:2"), ("1", 2))
        self.assertEqual(notify.parse_result_entry("X:1"), ("1", 0))      # a post of an earlier version has no count
        self.assertIsNone(notify.parse_result_entry("P:1"))

    def test_the_series_acts_each_time_on_a_full_screen(self):
        bridge, fail = self.start()
        self.series(bridge, fail)

    def test_the_series_acts_each_time_when_the_approval_is_off_the_screen(self):
        bridge, fail = self.start()
        bridge.posts = [p for p in bridge.posts if not p.startswith("[kimeru #")]
        bridge.replies = [r for r in bridge.replies if r != "OK 1"]
        self.series(bridge, fail)

    def test_the_series_acts_each_time_on_a_screen_of_the_last_three(self):
        bridge, fail = self.start()
        screen(bridge, tail=3)
        self.series(bridge, fail, redos=3)

    def test_the_series_acts_each_time_on_a_screen_of_the_last_ten_with_other_items_in_it(self):
        bridge, fail = self.start()
        screen(bridge, tail=10, noise=5)
        self.series(bridge, fail, redos=3)

    def test_the_series_acts_each_time_when_identical_lines_are_folded(self):
        bridge, fail = self.start()
        screen(bridge, fold=True)
        self.series(bridge, fail, redos=3)

    def test_a_reply_that_acted_never_acts_again_when_the_result_posts_are_not_visible(self):
        for tail in (None, 3):
            with self.subTest(tail=tail):
                self.tearDown()
                self.setUp()
                bridge, fail = self.start()
                real = bridge.read
                bridge.read = lambda: {k: ([e for e in v if not e.startswith("X:")] if k == "timeline" else v) for k, v in real().items()}
                self.cycle(bridge, http=fail, reply="再実行 1")
                self.assertEqual(self.posts_n(), 2)
                for _ in range(4):
                    self.cycle(bridge, http=fail)
                self.assertEqual(self.posts_n(), 2)
                bridge.read = real                                 # and when they are visible again, the reply is before the result
                for _ in range(3):
                    self.cycle(bridge, http=fail)
                self.assertEqual(self.posts_n(), 2)
                self.cycle(bridge, http=fail, reply="再実行 1")    # a new reply acts
                self.assertEqual(self.posts_n(), 3)

    def test_a_result_post_without_a_count_changes_nothing(self):
        bridge, fail = self.start()
        ap = notify.Approvals(self.out)
        ap.data["items"]["1"].pop("x_seq", None)               # as written by the earlier version: no count
        ap.save()
        bridge._chron = [(k, re.sub(r"^\[kimeru 実行 #1 \d+\]", "[kimeru 実行 #1]", t) if k == "P" else t) for k, t in bridge._chron]
        self.assertIn("X:1:0", bridge.read()["timeline"])
        self.series(bridge, fail, redos=2)

    def test_the_count_of_replies_that_the_earlier_version_kept_is_not_a_mark(self):
        bridge, fail = self.start()
        it = notify.Approvals(self.out).data["items"]["1"]
        self.assertIsNone(it.get("redo_mark"))
        self.assertEqual(notify.redo_mark(it), -1)
        it["redo_seen"] = 1
        self.assertEqual(notify.redo_mark(it), -1)      # fix 14: an item of the earlier version has no mark, so its next reply acts
        it["redo_mark"] = 3
        self.assertEqual(notify.redo_mark(it), 3)

    def test_the_replies_of_fresh_entries_know_which_results_stand_before_them(self):
        tl = ["P:1", "R:OK 1", "X:1:1", "R:再実行 1", "X:1:2", "X:2:1", "R:再実行 1"]
        self.assertEqual(notify.fresh_entries({"timeline": tl}), [("再実行 1", 2)])
        self.assertEqual(notify.fresh_replies({"timeline": tl}), ["再実行 1"])
        self.assertEqual(notify.fresh_entries({"timeline": ["X:1", "R:再実行 1"]}), [("再実行 1", 0)])


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
class TestRedoThroughTheFakeTeams(Safety):
    """The same, through PowerShellBridge and tests/fake-teams-self.ps1, which shows only the last messages and folds identical lines."""

    def setUp(self):
        super().setUp()
        self.chat = Path(self.dir.name) / "chat.json"
        os.environ["KIMERU_FAKE_CHAT"] = str(self.chat)

    def say(self, text):
        chat = json.loads(self.chat.read_text(encoding="utf-8-sig")) if self.chat.exists() else {"messages": []}
        chat["messages"].append(text)
        self.chat.write_text(json.dumps(chat, ensure_ascii=False), encoding="utf-8")

    def test_a_chat_of_the_last_three_messages_acts_on_each_new_reply_once(self):
        self.enable()
        cli.process(WI, GRAPHS, NoCriteria(), self.out, PBS)
        bridge = notify.PowerShellBridge(script=FAKE)
        notify.notify(self.out, bridge, send=True, real=True)
        os.environ["KIMERU_FAKE_TAIL"] = "3"
        fail = self.http_with(post_error=pull.PullError("HTTP 404 for <url>"))

        def cycle(reply=None):
            if reply:
                self.say(reply)
            return self.cycle(bridge, http=fail)
        cycle("OK 1")
        self.assertEqual(len(self.posts_of("POST")), 1)
        for n in (2, 3):
            cycle("再実行 1")
            self.assertEqual(len(self.posts_of("POST")), n)
            cycle()
            cycle()
            self.assertEqual(len(self.posts_of("POST")), n)
        tl = bridge.read()["timeline"]
        self.assertEqual(tl, ["X:1:2", "R:再実行 1", "X:1:3"])   # only the last three messages are on the screen
        self.assertTrue(all(e.startswith(("X:1:", "R:")) for e in tl), tl)


class TestApprovalsByHandFailsWhenBusy(Safety):
    def test_a_held_lock_is_exit_code_1_with_the_same_words_as_notify(self):
        self.enable()
        bridge = self.decide_and_post()
        outs = {}
        for cmd in ("notify", "approvals"):
            buf = io.StringIO()
            with fsutil.exclusive(self.out / "approvals.lock") as got, contextlib.redirect_stdout(buf), \
                    mock.patch("kimeru.notify.PowerShellBridge", lambda: bridge):
                self.assertTrue(got)
                code = cli.main(["--out", str(self.out), cmd] + (["--send"] if cmd == "notify" else []))
            self.assertEqual(code, 1, cmd)
            outs[cmd] = buf.getvalue()
            self.assertIn("another approvals run is in progress", buf.getvalue())
        self.assertIn("nothing was posted", outs["notify"])
        self.assertIn("nothing was read or applied", outs["approvals"])
        self.assertEqual(outs["notify"].split(":")[0], outs["approvals"].split(":")[0])


def _age(path, sec):
    t = os.stat(path).st_mtime - sec
    os.utime(path, (t, t))


class TestTheOwnerOfALockIsTheSameProcess(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.lock = Path(self.dir.name) / "approvals.lock"
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    def tearDown(self):
        self.child.kill()
        self.child.wait()
        self.dir.cleanup()

    def test_the_creation_time_of_a_process_is_the_same_while_it_lives_and_is_written_in_the_lock(self):
        a = fsutil.proc_start(self.child.pid)
        if a is None:
            self.skipTest("the creation time of a process cannot be read here")
        self.assertEqual(a, fsutil.proc_start(self.child.pid))
        with fsutil.exclusive(self.lock):
            self.assertEqual(self.lock.read_text(), f"{os.getpid()}:{fsutil.proc_start(os.getpid())}")

    def test_a_live_process_with_another_creation_time_is_not_the_owner_so_an_old_lock_is_taken_over_at_once(self):
        start = fsutil.proc_start(self.child.pid)
        if start is None:
            self.skipTest("the creation time of a process cannot be read here")
        self.lock.write_text(f"{self.child.pid}:{start + 12345}")      # the id of a live process, the time of another one that had it
        _age(self.lock, 10 * 86400)
        with fsutil.exclusive(self.lock) as got:
            self.assertTrue(got)
        self.lock.write_text(f"{self.child.pid}:{start + 12345}")      # and it is just as true for a new lock file
        with fsutil.exclusive(self.lock) as got:
            self.assertTrue(got)

    def test_the_same_process_keeps_its_lock_however_old_the_file_is(self):
        start = fsutil.proc_start(self.child.pid)
        if start is None:
            self.skipTest("the creation time of a process cannot be read here")
        self.lock.write_text(f"{self.child.pid}:{start}")
        _age(self.lock, 1801)
        with fsutil.exclusive(self.lock) as got:
            self.assertFalse(got)
        _age(self.lock, 10 * 86400)
        with fsutil.exclusive(self.lock) as got:
            self.assertFalse(got)
        self.assertEqual(self.lock.read_text(), f"{self.child.pid}:{start}")
        with fsutil.exclusive(self.lock.with_name("other.lock")) as mine, \
                fsutil.exclusive(self.lock.with_name("other.lock")) as again:   # this very process holds it: not taken by itself
            self.assertTrue(mine)
            self.assertFalse(again)

    def test_where_the_creation_time_cannot_be_read_the_age_decides_again(self):
        with mock.patch.object(fsutil, "proc_start", lambda pid: None):
            with fsutil.exclusive(self.lock):
                self.assertEqual(self.lock.read_text(), str(os.getpid()))   # nothing but the id was written
            self.lock.write_text(str(self.child.pid))
            with fsutil.exclusive(self.lock) as got:
                self.assertFalse(got)                                      # new
            _age(self.lock, 1801)
            with fsutil.exclusive(self.lock) as got:
                self.assertTrue(got)                                       # old: taken over (the old rule)
            self.lock.write_text(str(DEAD_PID))
            with fsutil.exclusive(self.lock) as got:
                self.assertTrue(got)                                       # a process that is gone: at once

    def test_a_lock_of_the_old_form_without_a_creation_time_still_works(self):
        self.lock.write_text(str(DEAD_PID))
        with fsutil.exclusive(self.lock) as got:
            self.assertTrue(got)


class TestAReleaseThatFails(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.lock = Path(self.dir.name) / "approvals.lock"

    def tearDown(self):
        self.dir.cleanup()

    def test_a_release_is_retried_while_another_process_has_the_file_open(self):
        real = pathlib.Path.unlink
        fails = {"n": 0}

        def flaky(self_, *a, **k):
            if self_.name == "approvals.lock" and fails["n"] < 3:
                fails["n"] += 1
                raise PermissionError(13, "in use")
            return real(self_, *a, **k)
        with mock.patch.object(pathlib.Path, "unlink", flaky), mock.patch.object(fsutil.time, "sleep", lambda s: None):
            with fsutil.exclusive(self.lock) as got:
                self.assertTrue(got)
        self.assertEqual(fails["n"], 3)
        self.assertFalse(self.lock.exists())

    def test_a_lock_that_could_not_be_released_is_taken_over_by_the_same_process_next_time(self):
        real = pathlib.Path.unlink

        def stuck(self_, *a, **k):
            if self_.name == "approvals.lock":
                raise PermissionError(13, "in use")
            return real(self_, *a, **k)
        with mock.patch.object(pathlib.Path, "unlink", stuck), mock.patch.object(fsutil.time, "sleep", lambda s: None):
            with fsutil.exclusive(self.lock) as got:
                self.assertTrue(got)
        self.assertTrue(self.lock.exists())                            # the release failed: the file stays, with this process as the owner
        with fsutil.exclusive(self.lock) as got:                       # next time: its own leftover
            self.assertTrue(got)
        self.assertFalse(self.lock.exists())
        self.lock.write_text(fsutil.lock_text())
        with fsutil.exclusive(self.lock) as got:
            self.assertTrue(got)

    def test_a_held_lock_is_not_a_leftover_even_for_the_process_that_holds_it(self):
        with fsutil.exclusive(self.lock) as got:
            self.assertTrue(got)
            with fsutil.exclusive(self.lock) as again:
                self.assertFalse(again)
            self.assertTrue(self.lock.exists())

    def test_two_processes_taking_and_giving_back_the_lock_never_get_stuck(self):
        child = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from kimeru import fsutil\n"
            "d = Path(sys.argv[2]); n = int(sys.argv[3])\n"
            "lock, counter = d / 'x.lock', d / 'counter.txt'\n"
            "for i in range(n):\n"
            "    end = time.time() + 20\n"
            "    while True:\n"
            "        with fsutil.exclusive(lock) as got:\n"
            "            if got:\n"
            "                c = int(counter.read_text() or 0) if counter.exists() else 0\n"
            "                counter.write_text(str(c + 1))\n"
            "                break\n"
            "        if time.time() > end:\n"
            "            sys.exit(3)\n"
            "        time.sleep(0.001)\n")
        n = 600
        ps = [subprocess.Popen([sys.executable, "-c", child, str(ROOT), self.dir.name, str(n)]) for _ in range(2)]
        codes = [p.wait(timeout=120) for p in ps]
        self.assertEqual(codes, [0, 0])                                # nobody waited in vain for a lock that no one held
        self.assertEqual(int((Path(self.dir.name) / "counter.txt").read_text()), 2 * n)   # and the lock really excluded
        self.assertEqual([p.name for p in Path(self.dir.name).iterdir()], ["counter.txt"])


class TestADamagedLockFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.lock = Path(self.dir.name) / "approvals.lock"

    def tearDown(self):
        self.dir.cleanup()

    def test_numbers_that_are_not_ids_do_not_crash_and_the_file_is_a_damaged_lock(self):
        for text in ("abc", "99999999999", "4294967296", "", "1:2:3", "12:abc", "0", "-5", "7:99999999999999999999999"):
            with self.subTest(text=text):
                self.lock.write_text(text)
                self.assertIsNone(fsutil._lock_info(self.lock))
                with fsutil.exclusive(self.lock) as got:
                    self.assertFalse(got)                               # a new damaged file: someone may be writing it
                _age(self.lock, 1801)
                with fsutil.exclusive(self.lock) as got:
                    self.assertTrue(got)                                # an old one: taken over
                self.assertFalse(self.lock.exists())

    def test_the_system_calls_never_get_a_value_they_cannot_take(self):
        for pid in (99999999999, 2 ** 40, 2 ** 63, -1, 0, "abc", None, 1.5e30):
            self.assertFalse(fsutil.pid_alive(pid), pid)
            self.assertIsNone(fsutil.proc_start(pid), pid)
        self.lock.write_bytes(b"\xff\xfe12")
        self.assertIsNone(fsutil._lock_info(self.lock))


class TestFullTextIsReadWhileItIsReplaced(unittest.TestCase):
    def test_a_load_during_the_writes_of_another_process_never_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            fulltext.save(out, "g:keep:n", {"text": "残す全文"})
            stop = out / "stop"
            child = (
                "import sys\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from kimeru import fulltext\n"
                "out = Path(sys.argv[2]); stop = out / 'stop'; i = 0\n"
                "while not stop.exists():\n"
                "    fulltext.save(out, 'g:w%d:n' % (i % 4), {'text': 'x' * 2000})\n"
                "    i += 1\n"
                "    with open(out / 'ticks', 'ab') as f: f.write(b'x')\n")
            p = subprocess.Popen([sys.executable, "-c", child, str(ROOT), str(out)])
            try:
                time.sleep(0.3)
                ticks = out / "ticks"
                before = ticks.stat().st_size if ticks.exists() else 0
                missing, loads, end = 0, 0, time.time() + 40
                while True:   # at least 1500 reads, and until the writer has saved at least 30 times while they ran
                    missing += fulltext.load(out, "g:keep:n") is None
                    loads += 1
                    saves = (ticks.stat().st_size if ticks.exists() else 0) - before
                    if (loads >= 1500 and saves >= 30) or time.time() > end:
                        break
            finally:
                stop.write_text("1")
                p.wait(timeout=60)
            self.assertGreaterEqual(saves, 30, "the reads must overlap many saves, or the test proves nothing")
            self.assertEqual(missing, 0)
            self.assertEqual(p.returncode, 0)


class TestTheT22ReadBackKeepsTheTextOutOfTheCommandLine(unittest.TestCase):
    def test_check_ps1_hands_the_text_over_in_a_file(self):
        t = (ROOT / "tools" / "check.ps1").read_text(encoding="utf-8-sig")
        i = t.index("# ---- T22")
        blk = t[i:t.index("# ---- T21", i)]
        line = next(l for l in blk.splitlines() if "inspect_comment" in l)
        self.assertNotIn("$t22text", line)                            # not an argument of the command
        self.assertIn("$t22file", line)
        self.assertIn("WriteAllText($t22file, $t22text", blk)
        self.assertIn("open(sys.argv[3]", line)


if __name__ == "__main__":
    unittest.main()
