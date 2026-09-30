"""Fix 12: a lock that a live process keeps is never taken over, the same-text check of an ADO comment, `再実行` on a screen that lags
or loses its boundaries, one temporary file per writer, full_text.json under a lock, the shape of a busy daily report.
Every call to ADO is mocked, every Teams is a fake; nothing leaves the test."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import contextlib
import html
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from kimeru import cli, daily, execute, fsutil, fulltext, pull
from tests.test_execute import GRAPHS, PBS, WI, Bridge, NoCriteria, ROOT
from tests.test_execute_safety import Safety

DEAD_PID = 2147483646   # no process has this id


def _age(path, sec):
    t = os.stat(path).st_mtime - sec
    os.utime(path, (t, t))


class TestALockOfALiveProcessIsKept(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.lock = Path(self.dir.name) / "approvals.lock"

    def tearDown(self):
        self.dir.cleanup()

    def test_an_old_lock_of_a_live_process_is_not_taken_over(self):
        with fsutil.exclusive(self.lock) as got:
            self.assertTrue(got)
            _age(self.lock, 1801)                              # 30 minutes and a second: the old rule would take it over
            with fsutil.exclusive(self.lock) as other:
                self.assertFalse(other)
            self.assertEqual(self.lock.read_text(), str(os.getpid()))
        self.assertFalse(self.lock.exists())

    def test_an_old_lock_of_another_live_process_is_not_taken_over_and_a_dead_one_is(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self.lock.write_text(str(p.pid))
            _age(self.lock, 7200)
            with fsutil.exclusive(self.lock) as got:
                self.assertFalse(got)
            self.assertTrue(self.lock.exists())
        finally:
            p.kill()
            p.wait()
        with fsutil.exclusive(self.lock) as got:               # the owner is gone now: taken over at once
            self.assertTrue(got)

    def test_the_lock_of_a_dead_process_is_taken_over_at_once_even_when_it_is_new(self):
        self.lock.write_text(str(DEAD_PID))
        with fsutil.exclusive(self.lock) as got:
            self.assertTrue(got)
            self.assertEqual(self.lock.read_text(), str(os.getpid()))
        self.assertFalse(self.lock.exists())

    def test_a_lock_without_a_readable_owner_falls_back_to_its_age(self):
        self.lock.write_text("")
        with fsutil.exclusive(self.lock) as got:
            self.assertFalse(got)                              # new: someone is just writing its id
        _age(self.lock, 1801)
        with fsutil.exclusive(self.lock) as got:
            self.assertTrue(got)

    def test_a_release_never_removes_the_lock_of_another_process(self):
        with fsutil.exclusive(self.lock) as got:
            self.assertTrue(got)
            self.lock.write_text(str(DEAD_PID + 1))            # another process holds it now (this one was taken over)
        self.assertTrue(self.lock.exists())
        self.assertEqual(self.lock.read_text(), str(DEAD_PID + 1))

    def test_the_heartbeat_marks_the_held_lock_as_in_use(self):
        with fsutil.exclusive(self.lock):
            _age(self.lock, 5000)
            old = os.stat(self.lock).st_mtime
            fsutil.heartbeat()
            self.assertGreater(os.stat(self.lock).st_mtime, old + 4000)

    def test_a_process_is_found_alive_without_being_signalled(self):
        self.assertTrue(fsutil.pid_alive(os.getpid()))
        self.assertFalse(fsutil.pid_alive(DEAD_PID))
        self.assertFalse(fsutil.pid_alive("x"))


class TestNotifyByHandSaysWhenItCannotPost(Safety):
    def test_a_held_lock_means_a_message_and_a_failing_exit_code_and_no_post(self):
        self.enable()
        cli.process(WI, GRAPHS, NoCriteria(), self.out, PBS)
        bridge = Bridge()
        buf = io.StringIO()
        with fsutil.exclusive(self.out / "approvals.lock") as got, contextlib.redirect_stdout(buf), \
                mock.patch("kimeru.notify.PowerShellBridge", lambda: bridge):
            self.assertTrue(got)
            code = cli.main(["--out", str(self.out), "notify", "--send"])
        self.assertNotEqual(code, 0)
        self.assertIn("another approvals run is in progress", buf.getvalue())
        self.assertIn("nothing was posted", buf.getvalue())
        self.assertNotRegex(buf.getvalue(), r"posted: \[")      # what tools/check.ps1 reads as a post
        self.assertEqual(bridge.posts, [])

    def test_a_free_lock_posts_and_exits_zero(self):
        self.enable()
        cli.process(WI, GRAPHS, NoCriteria(), self.out, PBS)
        bridge = Bridge()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch("kimeru.notify.PowerShellBridge", lambda: bridge):
            code = cli.main(["--out", str(self.out), "notify", "--send"])
        self.assertEqual(code, 0)
        self.assertRegex(buf.getvalue(), r"posted: \[1\]")


class TestTheSameTextCheckOfFix12(unittest.TestCase):
    def find(self, stored, text):
        http = lambda method, url, token, body=None, retries=3: {"comments": [{"id": 4, "text": stored}]}
        return execute._existing_comment("https://x/_apis/wit/workItems", "7", text, http, "t")

    def test_tag_like_texts_match_in_both_stored_forms(self):
        for text, escaped in (("<b> を使う", "&lt;b&gt; を使う"), ("List<String> を返す", "List&lt;String&gt; を返す"),
                              ("件数 < 3 のとき", "件数 &lt; 3 のとき"), ("a & b", "a &amp; b")):
            self.assertEqual(self.find(f"<div>{escaped}</div>", text), 4, text)   # wrapped in a tag
            self.assertEqual(self.find(escaped, text), 4, text)                   # the escapes only
            self.assertEqual(self.find(html.escape(text, quote=False), text), 4, text)   # what kimeru sends

    def test_sentences_that_differ_between_a_less_than_and_a_greater_than_do_not_match(self):
        a, b = "A<X または Y>D", "A<B または C>D"
        self.assertIsNone(self.find(html.escape(b, quote=False), a))
        self.assertIsNone(self.find(f"<div>{html.escape(b, quote=False)}</div>", a))
        self.assertEqual(self.find(html.escape(a, quote=False), a), 4)

    def test_the_approved_side_keeps_a_tag_and_the_stored_side_loses_its_tags(self):
        self.assertEqual(execute._plain_approved("<b> と List<String>"), "<b> と List<String>")
        self.assertEqual(execute._plain("<div>&lt;b&gt; と List&lt;String&gt;</div>"), "<b> と List<String>")
        self.assertEqual(execute._plain("&lt;b&gt;"), "<b>")                       # an escaped tag is text
        self.assertEqual(execute._plain("<b>太字</b>"), "太字")                      # a real tag is not

    def test_the_comment_is_sent_as_plain_text(self):
        from kimeru import config
        with mock.patch.dict(os.environ, {"KIMERU_ADO_ORG": "contoso-not-real", "KIMERU_ADO_PROJECT": "Proj"}):
            config.apply([])
            sent = []

            def http(method, url, token, body=None, retries=3):
                sent.append((method, body))
                return {"id": 5}
            item = {"record": {"event": {"origin": {"org": "contoso-not-real", "project": "Proj"}}}}
            text = "件数 < 3 のとき <b> と List<String> & 確認\n2 行目"
            execute._write_ado_comment({"id": "7", "exec_text": text}, item, http, lambda org: "t")
        body = sent[0][1]["text"]
        self.assertNotIn("<", body)
        self.assertIn("&lt;b&gt;", body)
        self.assertIn("&amp;", body)
        self.assertEqual(html.unescape(body), text)                               # the line break is sent as it is


def hide(bridge, prefixes):
    """A screen that does not show the timeline entries that start with one of `prefixes` (the post is made all the same)."""
    real = bridge.read
    bridge.read = lambda: {k: ([e for e in v if not e.startswith(prefixes)] if k == "timeline" else v) for k, v in real().items()}
    return real


class TestARedoActsOncePerReply(Safety):
    FAIL = pull.PullError("HTTP 404 for <url>")

    def start(self):
        self.enable()
        bridge = self.decide_and_post()
        fail = self.http_with(post_error=self.FAIL)
        self.cycle(bridge, http=fail, reply="OK 1")            # POST 1 fails: a sure failure
        return bridge, fail

    def posts_n(self):
        return len(self.posts_of("POST"))

    def test_a_result_posted_at_the_start_of_the_next_cycle_on_a_screen_that_lags_does_not_act_again(self):
        bridge, fail = self.start()
        bridge.fail_posts = True                                # the result of the redo cannot be posted: it waits in the outbox
        self.cycle(bridge, http=fail, reply="再実行 1")         # POST 2
        self.assertEqual(self.posts_n(), 2)
        bridge.fail_posts = False
        real = bridge.read

        def lagging():   # the newest result post is not on the screen yet: only the old one
            r = real()
            tl = r["timeline"]
            last = max(i for i, e in enumerate(tl) if e == "X:1")
            return {**r, "timeline": [e for i, e in enumerate(tl) if i != last]}
        bridge.read = lagging
        # this cycle posts the result first (the flush at its start), then reads a screen that does not show it yet
        self.cycle(bridge, http=fail)
        self.assertEqual(self.posts_n(), 2)
        for _ in range(2):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.posts_n(), 2)
        bridge.read = real                                      # the screen catches up: the result is there, the reply is before it
        for _ in range(2):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.posts_n(), 2)
        self.cycle(bridge, http=fail, reply="再実行 1")         # a new reply after the result acts
        self.assertEqual(self.posts_n(), 3)
        self.cycle(bridge, http=fail)
        self.assertEqual(self.posts_n(), 3)

    def test_boundaries_that_leave_the_screen_and_come_back_do_not_make_the_same_reply_act_again(self):
        bridge, fail = self.start()
        bridge.fail_posts = True                                # the result is not posted: no result post follows the reply
        self.cycle(bridge, http=fail, reply="再実行 1")
        self.assertEqual(self.posts_n(), 2)
        real = hide(bridge, ("P:", "X:"))                       # the approval post and the result posts are off the screen
        for _ in range(2):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.posts_n(), 2)
        bridge.read = real                                      # they come back, and the same reply stands after them (no result yet)
        for _ in range(2):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.posts_n(), 2)
        bridge.fail_posts = False                               # now the result is posted
        for _ in range(3):
            self.cycle(bridge, http=fail)
        self.assertEqual(self.posts_n(), 2)
        self.cycle(bridge, http=fail, reply="再実行 1")
        self.assertEqual(self.posts_n(), 3)

    def test_the_series_ok_result_redo_result_redo_acts_three_times_on_and_off_the_screen(self):
        for scroll_out in (False, True):
            with self.subTest(scroll_out=scroll_out):
                self.tearDown()
                self.setUp()
                bridge, fail = self.start()
                if scroll_out:
                    bridge.posts = [p for p in bridge.posts if not p.startswith("[kimeru #")]
                    bridge.replies = [r for r in bridge.replies if r != "OK 1"]
                for n in (2, 3):
                    self.cycle(bridge, http=fail, reply="再実行 1")
                    self.assertEqual(self.posts_n(), n)
                    for _ in range(2):
                        self.cycle(bridge, http=fail)
                    self.assertEqual(self.posts_n(), n)


class TestOneTemporaryFilePerWriter(unittest.TestCase):
    def test_two_writers_at_once_never_fail_to_replace(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "approvals.json"
            errors = []

            def writer(tag):
                try:
                    for i in range(150):
                        fsutil.write_atomic(path, json.dumps({"w": tag, "i": i}))
                except Exception as e:   # noqa: BLE001
                    errors.append(repr(e))
            ts = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(errors, [])
            self.assertIn("w", json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual([p.name for p in Path(d).iterdir() if ".tmp" in p.name], [])

    def test_the_temporary_name_is_per_process(self):
        with tempfile.TemporaryDirectory() as d:
            seen = []
            real = os.replace
            with mock.patch("os.replace", lambda a, b: (seen.append(Path(a).name), real(a, b))[1]):
                fsutil.write_atomic(Path(d) / "x.json", "{}")
            tmp = [n for n in seen if ".tmp" in n]
            self.assertTrue(tmp and str(os.getpid()) in tmp[0])


class TestFullTextSurvivesTwoWriters(unittest.TestCase):
    def test_the_daily_judgment_and_an_approvals_run_at_once_lose_no_full_text(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            for i in range(5):
                fulltext.save(out, f"g:old{i}:n", {"text": "長い" * 10}, request=f"req{i}")
            errors = []

            def judge():   # the judgment step (outside the approvals lock): saves the full text of new items
                try:
                    for i in range(40):
                        fulltext.save(out, f"g:new{i}:n", {"text": f"全文{i}"})
                except Exception as e:   # noqa: BLE001
                    errors.append(repr(e))

            def approvals():   # a hand-typed approvals run (inside the approvals lock): drops, and sets a request
                try:
                    for i in range(5):
                        fulltext.set_request(out, f"g:old{i}:n", f"new-req{i}")
                        fulltext.drop(out, f"g:old{i}:n")
                except Exception as e:   # noqa: BLE001
                    errors.append(repr(e))
            ts = [threading.Thread(target=judge), threading.Thread(target=approvals)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(errors, [])
            data = json.loads((out / "full_text.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(k for k in data if k.startswith("g:new")), sorted(f"g:new{i}:n" for i in range(40)))
            self.assertEqual([k for k in data if k.startswith("g:old")], [])
            self.assertEqual([p.name for p in out.iterdir() if ".tmp" in p.name or p.name.endswith(".lock")], [])


def _load_day_run():
    spec = importlib.util.spec_from_file_location("day_run_under_test", ROOT / "eval" / "day_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestABusyDailyReport(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name) / "out"
        self.inbox = Path(self.dir.name) / "inbox"
        self.inbox.mkdir()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = self.dir.name
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def test_the_steps_keep_their_shape_and_the_skipped_ones_are_named(self):
        from tests.test_daily import FakeTeams
        from kimeru.backends import StubBackend
        t = FakeTeams()
        self.out.mkdir()
        with fsutil.exclusive(self.out / "approvals.lock") as got:
            self.assertTrue(got)
            r = daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=t, send=True, toaster=lambda a, b: None)
        self.assertIsInstance(r["notify"], list)
        self.assertIsInstance(r["notices"], int)
        self.assertIsInstance(r["approvals"], list)
        self.assertEqual(set(r["busy"]), {"notify", "notices", "approvals", "full_text_purge"})
        lines = daily.status_lines(self.out)
        self.assertTrue(any("another run held approvals.lock" in l and "approvals" in l for l in lines), lines)

    def test_day_run_reads_every_shape_without_failing(self):
        day_run = _load_day_run()
        self.assertEqual(day_run.as_list({"posted": [1], "busy": "x"}), [])
        self.assertEqual(day_run.as_list("busy: x"), [])
        self.assertEqual(day_run.as_list(None), [])
        self.assertEqual(day_run.as_list([1, 2]), [1, 2])
        self.assertFalse(2 in day_run.as_list({"busy": "x"}))


class TestEveryCommandLineInTheDocsIsIntact(unittest.TestCase):
    FILES = [ROOT / "README.md", ROOT / "SECURITY.md"] + sorted((ROOT / "docs").glob("*.md"))

    def test_run_check_command_lines_are_not_broken(self):
        for f in self.FILES:
            text = f.read_text(encoding="utf-8")
            name = f.name
            self.assertNotRegex(text, r"(?<![a-z])un-check\.cmd", f"{name}: a command broke at a backslash-r")
            self.assertIsNone(re.search(r"\r(?!\n)", text), f"{name}: a lone carriage return")
            fenced = False
            for l in text.splitlines():
                if l.strip().startswith("```"):
                    fenced = not fenced
                elif fenced and "run-check.cmd" in l and not l.strip().startswith((".\\run-check.cmd", "run-check.cmd")):
                    self.fail(f"{name}: {l}")
                for m in re.finditer(r"`(\S*?)run-check\.cmd[^`]*`", l):
                    self.assertIn(m.group(1), ("", ".\\"), f"{name}: {l}")
                self.assertFalse("run-check.cmd" in l and l.rstrip().endswith("`."), f"{name}: cut after `.`: {l}")


if __name__ == "__main__":
    unittest.main()
