"""Fix 14: the `-c` programs of check.ps1 without a double quote (PowerShell 5.1 drops it), `再実行` judged from the result posts after
the last approval post of the number, the migration of items of the earlier version, the reply `再実行 N-k`, a process that cannot be
opened is not a lock owner, a lock file that cannot be created is not "busy", a delete of a full text that fails is not a success.
Every call to ADO is mocked, every Teams is a fake; nothing leaves the test."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import cli, fsutil, fulltext, notify, pull
from tests.test_execute import ROOT
from tests.test_execute_safety import Safety
from tests.test_fix13 import screen

CHECK = ROOT / "tools" / "check.ps1"
REAL_READER = ROOT / "tools" / "teams-self.ps1"
FAKE_READER = ROOT / "tests" / "fake-teams-self.ps1"


def ps_literals(text):
    """The programs that check.ps1 passes as `-c` (the single-quoted PowerShell literal after `'-c',`), as PowerShell hands them over."""
    return [m.group(1).replace("''", "'") for m in re.finditer(r"Py @\('-c', '((?:[^']|'')*)'", text)]


class TestCheckPs1ProgramsHaveNoDoubleQuote(unittest.TestCase):
    def setUp(self):
        self.text = CHECK.read_text(encoding="utf-8-sig")

    def test_the_three_programs_have_no_double_quote_and_are_valid_python(self):
        progs = ps_literals(self.text)
        self.assertEqual(len(progs), 3)               # T22 target, T22 read-back, T15
        for p in progs:
            self.assertNotIn('"', p)                  # Windows PowerShell 5.1 drops it from an element of an argument array
            compile(p, "check.ps1", "exec")
        self.assertTrue(any("ado_names" in p for p in progs) and any("inspect_comment" in p for p in progs)
                        and any("find_exe" in p for p in progs))

    def test_no_py_call_passes_c_in_another_form(self):
        self.assertEqual(len(re.findall(r"'-c'", self.text)), 3)

    def _run(self, exe):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "t.txt"
            f.write_text("kimeru 試験です。件数 < 3 のとき <b> を使い、A & B と書く", encoding="utf-8")
            env = {**os.environ, "KIMERU_STATE_DIR": d}
            extra = ["-ExecutionPolicy", "Bypass"] if "powershell" in Path(exe).name.lower() else []
            r = subprocess.run([exe, "-NoProfile"] + extra + ["-File", str(ROOT / "tests" / "check-py-quoting.ps1"), str(CHECK),
                                                              sys.executable, str(f)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.count("OK "), 3, r.stdout)
        self.assertIn('"found": 1', r.stdout)               # the real inspect_comment ran, against a stored comment (no network)
        for k in ("wrapped_in_tags", "escaped", "same_text_check"):
            self.assertIn(f'"{k}": true', r.stdout)

    @unittest.skipUnless(shutil.which("powershell"), "needs Windows PowerShell 5.1")
    def test_the_programs_run_in_windows_powershell_5_1(self):
        self._run(shutil.which("powershell"))

    @unittest.skipUnless(shutil.which("pwsh"), "needs PowerShell 7")
    def test_the_programs_run_in_powershell_7(self):
        self._run(shutil.which("pwsh"))


class TestTheResultPostRegexIsTheSameInTheRealAndTheFakeReader(unittest.TestCase):
    RESULT = re.compile(r"-match '(\^\\\[kimeru 実行 #[^']*)'")
    REDO = re.compile(r"-match '(\^再実行[^']*-[^']*)'")

    def test_the_result_post_and_the_redo_k_regexes_are_identical(self):
        real = REAL_READER.read_text(encoding="utf-8-sig")
        fake = FAKE_READER.read_text(encoding="utf-8-sig")
        for rx in (self.RESULT, self.REDO):
            a, b = rx.findall(real), rx.findall(fake)
            self.assertEqual(len(a), 1, rx.pattern)
            self.assertEqual(a, b)
        self.assertIn("(?:\\s+(\\d+))?", self.RESULT.findall(real)[0])


def lag(bridge):
    """A screen that is late: the result posts made after the last approval post of #1 are not on it yet."""
    real = bridge.read

    def read():
        r = real()
        tl = r["timeline"]
        last = max(i for i, e in enumerate(tl) if e == "P:1")
        return {**r, "timeline": [e for i, e in enumerate(tl) if not (i > last and e.startswith("X:1:"))]}
    bridge.read = read


def screen_tail(bridge, tail, noise=0):
    """The last `tail` entries of the chat, identical neighbours folded, `noise` entries of other items after every result post of #1."""
    real = bridge.read

    def read():
        r = real()
        tl = []
        for e in r["timeline"]:
            tl.append(e)
            if e.startswith("X:1:"):
                tl.extend(["P:2", "X:2:1", "P:3", "R:OK 3", "X:3:1", "P:4", "X:4:1", "X:5:1"][:noise])
        tl = tl[-tail:]
        tl = [e for i, e in enumerate(tl) if i == 0 or tl[i - 1] != e]
        return {**r, "timeline": tl}
    bridge.read = read


class Redo(Safety):
    FAIL = pull.PullError("HTTP 404 for <url>")

    def start(self):
        self.enable()
        bridge = self.decide_and_post()
        fail = self.http_with(post_error=self.FAIL)
        self.cycle(bridge, http=fail, reply="OK 1")
        return bridge, fail

    def n(self):
        return len(self.posts_of("POST"))

    def results(self, bridge):
        return [p for p in bridge.posts if p.startswith("[kimeru 実行 #1 ")]


class TestNumbersUsedAgain(Redo):
    def test_the_counts_are_taken_after_the_last_approval_post_of_the_number(self):
        tl = ["P:1", "X:1:1", "X:1:2", "X:1:3", "P:1", "R:再実行 1"]
        self.assertEqual(notify.fresh_entries({"timeline": tl}), [("再実行 1", 0)])
        tl = ["P:1", "X:1:1", "X:1:2", "X:1:3", "P:1", "X:1:1", "R:再実行 1"]
        self.assertEqual(notify.fresh_entries({"timeline": tl}), [("再実行 1", 1)])

    def test_one_reply_runs_once_when_an_earlier_item_of_the_same_number_stands_above_and_the_screen_is_late(self):
        bridge, fail = self.start()
        old = [("P", f"[kimeru 実行 #1 {k}] 前の件の結果") for k in (1, 2, 3)]
        i = next(i for i, (k, t) in enumerate(bridge._chron) if t.startswith("[kimeru #1]"))
        bridge._chron[i:i] = old                           # X:1:1 .. X:1:3 of an earlier item, above this item's approval post
        self.assertEqual(bridge.read()["timeline"][:4], ["X:1:1", "X:1:2", "X:1:3", "P:1"])
        lag(bridge)                                        # and this item's own result posts are not on the screen yet
        self.cycle(bridge, http=fail, reply="再実行 1")
        self.assertEqual(self.n(), 2)
        for _ in range(4):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 2)                      # the earlier version ran again and again here (it saw 3 > its mark)


class TestMigrationFromTheEarlierVersion(Redo):
    def test_an_item_with_redo_seen_acts_on_a_new_reply_and_the_next_one_too(self):
        bridge, fail = self.start()
        ap = notify.Approvals(self.out)
        it = ap.data["items"]["1"]
        for k in ("redo_mark", "x_seq"):
            it.pop(k, None)
        it["redo_seen"] = 1                                # what the earlier version left after a redo that acted
        ap.save()
        bridge._chron = [(k, re.sub(r"^\[kimeru 実行 #1 \d+\]", "[kimeru 実行 #1]", t)) for k, t in bridge._chron]   # its result: no count
        self.cycle(bridge, http=fail, reply="再実行 1")
        self.assertEqual(self.n(), 2)                      # not blocked for ever
        for _ in range(3):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 2)                      # and only once
        self.cycle(bridge, http=fail, reply="再実行 1")
        self.assertEqual(self.n(), 3)


class TestTheTailOfTheScreen(Redo):
    """The screens of the last three and ten messages: a reply right after a result, the third identical line folded away."""

    def go(self, tail, noise):
        bridge, fail = self.start()
        screen_tail(bridge, tail, noise)
        for n in range(2, 7):
            self.cycle(bridge, http=fail, reply="再実行 1")
            self.assertEqual(self.n(), n, f"round {n}")
            self.cycle(bridge, http=fail)
            self.cycle(bridge, http=fail)
            self.assertEqual(self.n(), n, f"round {n} again")

    def test_last_three(self):
        self.go(3, 0)

    def test_last_three_with_a_reply_right_after_the_result_and_a_third_reply_that_is_folded(self):
        bridge, fail = self.start()
        screen_tail(bridge, 3)
        self.cycle(bridge, http=fail, reply="再実行 1")        # acts at once; its result post is the newest message
        self.assertEqual(self.n(), 2)
        bridge.replies.append("再実行 1")                       # sent right after the result
        bridge.replies.append("再実行 1")                       # and a third identical one: folded into the second on the screen
        self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 3)
        for _ in range(3):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 3)
        self.cycle(bridge, http=fail, reply="再実行 1")
        self.assertEqual(self.n(), 4)

    def test_last_ten_with_other_items(self):
        self.go(10, 5)


class TestTheRedoReplyOfTheResultPost(Redo):
    def hint(self, bridge):
        last = self.results(bridge)[-1]
        return last, int(re.match(r"\[kimeru 実行 #1 (\d+)\]", last).group(1))

    def test_the_result_post_states_the_reply_and_the_two_conditions_under_which_a_reply_is_lost(self):
        bridge, fail = self.start()
        last, k = self.hint(bridge)
        self.assertEqual(k, 1)
        self.assertIn("「再実行 1-1」と返信してください", last)
        self.assertIn("この投稿より前に送った返信", last)
        self.assertIn("この投稿が画面から流れたあとの「再実行 1」", last)
        self.assertEqual(last.count("\n"), 0)                # one line: the reader takes the first line of a post
        self.assertEqual(notify.parse_redo_k("再実行 1-1"), ("1", 1))
        self.assertIsNone(notify.parse_redo_k("再実行 1"))
        self.assertIsNone(notify.parse_reply("再実行 1-1"))

    def test_a_post_that_offers_no_redo_has_no_hint(self):
        self.enable()
        bridge = self.decide_and_post()
        self.cycle(bridge, reply="OK 1")                     # a success
        self.assertNotIn("再実行", self.results(bridge)[-1])

    def test_the_reply_of_the_latest_result_acts_once_whatever_the_screen_shows(self):
        bridge, fail = self.start()
        screen_tail(bridge, 2)                               # the result posts of the item are hardly visible
        self.cycle(bridge, http=fail, reply="再実行 1-1")
        self.assertEqual(self.n(), 2)
        for _ in range(4):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 2)
        self.assertIn("再実行 1-2", self.hint(bridge)[0])
        self.cycle(bridge, http=fail, reply="再実行 1-2")
        self.assertEqual(self.n(), 3)

    def test_the_reply_of_an_older_result_is_ignored_and_recorded_once(self):
        bridge, fail = self.start()
        self.cycle(bridge, http=fail, reply="再実行 1")        # result 2 now
        self.assertEqual(self.n(), 2)
        self.cycle(bridge, http=fail, reply="再実行 1-1")
        for _ in range(3):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.n(), 2)                        # 1-1 is not the latest result (2)
        rows = [json.loads(l) for l in (self.out / "approvals.log.jsonl").read_text(encoding="utf-8").splitlines()]
        ignored = [r for r in rows if r["status"] == "redo_ignored"]
        self.assertEqual(len(ignored), 1)
        self.assertEqual((ignored[0]["k"], ignored[0]["latest"]), (1, 2))

    def test_a_reply_of_an_item_that_used_the_number_before_is_left_out(self):
        self.assertEqual(notify.redo_k_entries({"timeline": ["P:1", "R:再実行 1-1", "P:1"]}), [])
        self.assertEqual(notify.redo_k_entries({"timeline": ["R:再実行 1-1", "P:2"]}), [("1", 1)])
        self.assertEqual(notify.redo_k_entries({"timeline": ["R:再実行 1-1", "R:再実行 1-1"]}), [("1", 1)])

    def test_the_other_replies_are_read_as_before(self):
        for line, want in (("OK 3", ("OK", "3")), ("ＯＫ　３", ("OK", "3")), ("ng 3", ("NG", "3")), ("保留 3", ("保留", "3")),
                           ("聞き返し 3", ("聞き返し", "3")), ("再実行 3", ("再実行", "3")), ("済 3", ("済", "3")),
                           ("再実行 #3.", ("再実行", "3")), ("再実行 3-", None), ("OK 3-1", None), ("3", None)):
            self.assertEqual(notify.parse_reply(line), want, line)
        self.assertEqual(notify.parse_redraft("修正 3 もっと短く"), ("3", "もっと短く"))
        self.assertEqual(notify.parse_paste("下書き 3 受領しました"), ("3", "受領しました"))
        self.assertIsNone(notify.parse_redo_k("OK 3-1"))
        self.assertEqual(notify.parse_redo_k("再実行　３－２"), ("3", 2))


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
class TestTheFakeReaderKnowsTheRedoReply(unittest.TestCase):
    def test_a_redo_k_reply_is_on_the_timeline(self):
        with tempfile.TemporaryDirectory() as d:
            chat = Path(d) / "chat.json"
            chat.write_text(json.dumps({"messages": ["[kimeru #1] x", "再実行 1-2", "再実行 1"]}, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {"KIMERU_FAKE_CHAT": str(chat)}):
                self.assertEqual(notify.PowerShellBridge(script=FAKE_READER).read()["timeline"], ["P:1", "R:再実行 1-2", "R:再実行 1"])


class TestAProcessThatCannotBeOpenedIsNotALockOwner(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.lock = Path(self.dir.name) / "approvals.lock"

    def tearDown(self):
        self.dir.cleanup()

    def _denied_pid(self):
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        for pid in range(4, 4000, 4):
            h = k32.OpenProcess(0x1000, False, pid)
            if h:
                k32.CloseHandle(ctypes.c_void_p(h))
            elif k32.GetLastError() == 5:
                return pid
        return None

    @unittest.skipUnless(os.name == "nt", "Windows")
    def test_a_real_process_that_windows_refuses_to_open(self):
        pid = self._denied_pid()
        if pid is None:
            self.skipTest("every process here can be opened")
        self.assertFalse(fsutil.pid_alive(pid))
        self.lock.write_text(f"{pid}:123456")
        with fsutil.exclusive(self.lock) as got:
            self.assertTrue(got)                            # taken over at once, though the file is new
        self.lock.write_text(str(pid))                      # a lock of the old form too
        with fsutil.exclusive(self.lock) as got:
            self.assertTrue(got)

    def test_with_the_check_mocked_the_lock_is_taken_over_at_once_even_when_10_days_old(self):
        with mock.patch.object(fsutil, "pid_alive", lambda pid: False):
            for text in ("4242:99", "4242"):
                self.lock.write_text(text)
                t = os.stat(self.lock).st_mtime - 10 * 86400
                os.utime(self.lock, (t, t))
                with fsutil.exclusive(self.lock) as got:
                    self.assertTrue(got)

    @unittest.skipIf(os.name == "nt", "POSIX: kill answers PermissionError for the process of another user")
    def test_a_permission_error_from_kill_is_not_alive(self):
        with mock.patch.object(fsutil.os, "kill", mock.Mock(side_effect=PermissionError)):
            self.assertFalse(fsutil.pid_alive(4242))

    def test_a_lock_of_this_same_process_that_is_held_is_not_taken_over(self):
        with fsutil.exclusive(self.lock) as got:
            self.assertTrue(got)
            with fsutil.exclusive(self.lock) as again:
                self.assertFalse(again)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            start = fsutil.proc_start(child.pid)
            self.lock.write_text(f"{child.pid}:{start}" if start else str(child.pid))
            with fsutil.exclusive(self.lock) as got:
                self.assertFalse(got)                       # a process that can be looked at, and runs: its lock stays
        finally:
            child.kill()
            child.wait()


class TestALockFileThatCannotBeCreated(Safety):
    def test_a_folder_that_cannot_be_written_is_not_busy(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)

        def denied(path, *a, **k):
            raise PermissionError(13, "denied")
        with mock.patch.object(fsutil.os, "open", denied), mock.patch.object(fsutil.time, "sleep", lambda s: None):
            with self.assertRaises(fsutil.LockFolderError) as cm:
                with fsutil.exclusive(d / "approvals.lock"):
                    pass
        self.assertIn("cannot be written", str(cm.exception))

    def test_a_moment_of_contention_is_still_busy(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        real = os.open

        def denied_for_the_lock(path, *a, **k):
            if str(path).endswith("approvals.lock"):
                raise PermissionError(13, "being deleted")
            return real(path, *a, **k)
        with mock.patch.object(fsutil.os, "open", denied_for_the_lock), mock.patch.object(fsutil.time, "sleep", lambda s: None):
            with fsutil.exclusive(d / "approvals.lock") as got:
                self.assertFalse(got)                       # the folder is writable: someone else, or a delete in progress

    def test_the_command_says_the_folder_not_a_run_in_progress(self):
        self.enable()
        bridge = self.decide_and_post()

        def denied(path, *a, **k):
            raise PermissionError(13, "denied")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(fsutil.os, "open", denied), mock.patch.object(fsutil.time, "sleep", lambda s: None), \
                mock.patch("kimeru.notify.PowerShellBridge", lambda: bridge), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--out", str(self.out), "approvals"])
        self.assertEqual(code, 1)
        self.assertNotIn("in progress", out.getvalue() + err.getvalue())
        self.assertIn("cannot create the lock file", err.getvalue())
        self.assertIn("cannot be written", err.getvalue())


class TestADeleteThatFailsIsNotASuccess(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name)
        fulltext.save(self.out, "g:a:n", {"text": "全文A"})
        fulltext.save(self.out, "g:b:n", {"text": "全文B"})

    def tearDown(self):
        self.dir.cleanup()

    def stuck(self, names):
        real = fsutil._unlink_retry
        return mock.patch.object(fsutil, "_unlink_retry", lambda p, tries=25: False if Path(p).name in names else real(p, tries))

    def warnings(self):
        p = self.out / "warnings.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_dropping_the_last_text_when_the_file_cannot_be_removed(self):
        fulltext.drop(self.out, "g:a:n")
        with self.stuck({"full_text.json"}):
            with self.assertRaises(fulltext.DeleteFailed):
                fulltext.drop(self.out, "g:b:n")
        self.assertEqual(len(self.warnings()), 1)
        self.assertIn("delete failed", self.warnings()[0]["full_text"])
        self.assertIsNotNone(fulltext.load(self.out, "g:b:n"))   # it stays, and the next cycle tries again
        self.assertTrue(fulltext.drop(self.out, "g:b:n"))
        self.assertIsNone(fulltext.load(self.out, "g:b:n"))
        self.assertEqual([p.name for p in self.out.iterdir() if p.name.startswith("full_text")], [])

    def test_the_old_copy_that_cannot_be_removed_is_an_error_too_and_is_tried_before_the_new_file_is_written(self):
        with self.stuck({"full_text.json.bak"}):
            with self.assertRaises(fulltext.DeleteFailed):
                fulltext.drop(self.out, "g:a:n")
        self.assertIsNotNone(fulltext.load(self.out, "g:a:n"))   # nothing was changed: the retry finds the text and drops it
        self.assertTrue(fulltext.drop(self.out, "g:a:n"))

    def test_purge_does_not_report_a_text_it_could_not_delete(self):
        fulltext.drop(self.out, "g:a:n")
        long_ago = fulltext.datetime.now(fulltext.timezone.utc) + fulltext.timedelta(days=30)
        with self.stuck({"full_text.json"}), self.assertRaises(fulltext.DeleteFailed):
            fulltext.purge_locked(self.out, now=long_ago)
        self.assertEqual(len(self.warnings()), 1)
        self.assertEqual(fulltext.purge_locked(self.out, now=long_ago), ["g:b:n"])


if __name__ == "__main__":
    unittest.main()
