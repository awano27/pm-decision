"""The executor's safety: once per reply, unknown results, the routes that never execute, the fixed text, the target.
Every call to ADO is mocked, every Teams is a fake: nothing leaves the test."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import contextlib
import io
import json
import re
from datetime import datetime
from pathlib import Path
from unittest import mock

from kimeru import cli, config, daily, demo, execute, fsutil, notify, pull
from tests.test_execute import GRAPHS, PBS, ROOT, WI, Base, Bridge, NoCriteria, SECRET_TOKEN, TENANT


class Safety(Base):
    def cycle(self, bridge, http=None, real=True, send=True, reply=None):
        if reply:
            bridge.replies.append(reply)
        with mock.patch.object(pull, "http_json", http or self.http()), \
                mock.patch.object(execute, "_token", lambda org: SECRET_TOKEN), \
                mock.patch.object(pull, "ado_tenant", lambda org: TENANT):
            return notify.collect(self.out, bridge, real=real, send=send)

    def http_with(self, comments=None, post_error=None):
        """GET lists the comments that are already there; POST answers or fails."""
        def fake(method, url, token, body=None, retries=3):
            self.calls.append((method, url, token, body, retries))
            if method == "GET":
                return {"comments": [{"id": 1, "text": t} for t in (comments or [])]}
            if post_error:
                raise post_error
            return {"id": 555}
        return fake

    def posts_of(self, method):
        return [c for c in self.calls if c[0] == method]

    def state(self):
        return json.loads((self.out / "approvals.json").read_text(encoding="utf-8"))["items"]["1"]

    def rows(self):
        p = self.out / "executions.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


class TestOnceForEachReply(Safety):
    def test_a_redo_reply_acts_once_even_when_the_write_keeps_failing(self):
        self.enable()
        bridge = self.decide_and_post()
        bad = self.http_with(post_error=pull.PullError("HTTP 400 for <url>"))
        self.cycle(bridge, http=bad, reply="OK 1")
        self.assertEqual(len(self.posts_of("POST")), 1)
        self.cycle(bridge, http=bad, reply="再実行 1")
        self.assertEqual(len(self.posts_of("POST")), 2)                # the reply acted once
        for _ in range(3):                                             # three more cycles, no new reply
            self.cycle(bridge, http=bad)
        self.assertEqual(len(self.posts_of("POST")), 2)
        self.cycle(bridge, http=bad, reply="再実行 1")                  # a NEW reply acts again
        self.assertEqual(len(self.posts_of("POST")), 3)

    def test_a_redo_reply_acts_once_even_when_the_result_post_is_not_visible(self):
        self.enable()
        bridge = self.decide_and_post()
        bad = self.http_with(post_error=pull.PullError("HTTP 400 for <url>"))
        self.cycle(bridge, http=bad, reply="OK 1")
        real_read = bridge.read
        bridge.read = lambda: {k: ([e for e in v if not e.startswith("X:")] if k == "timeline" else v) for k, v in real_read().items()}
        self.cycle(bridge, http=bad, reply="再実行 1")
        for _ in range(3):
            self.cycle(bridge, http=bad)
        self.assertEqual(len(self.posts_of("POST")), 2)

    def test_a_result_post_is_the_boundary_for_the_replies_before_it(self):
        read = {"timeline": ["P:1", "R:OK 1", "X:1", "R:再実行 1"]}
        self.assertEqual(notify.fresh_replies(read), ["再実行 1"])
        self.assertEqual(notify.fresh_replies({"timeline": ["P:1", "R:再実行 1", "X:1"]}), [])
        self.assertEqual(notify.fresh_replies({"timeline": ["P:1", "R:再実行 1", "X:1"]}, x_boundary=False), ["再実行 1"])

    def test_a_stop_in_the_middle_of_a_redo_does_not_run_it_again(self):
        self.enable()
        bridge = self.decide_and_post()
        self.cycle(bridge, http=self.http_with(post_error=pull.PullError("HTTP 400 for <url>")), reply="OK 1")
        crash = self.http_with(post_error=KeyboardInterrupt("simulated crash"))
        with self.assertRaises(KeyboardInterrupt):
            self.cycle(bridge, http=crash, reply="再実行 1")
        n = len(self.posts_of("POST"))
        self.cycle(bridge, http=self.http_with())                       # the same reply is still on the screen
        self.assertEqual(len(self.posts_of("POST")), n)                # not run again on its own
        self.assertTrue(any("書けたかどうか分かりません" in p for p in bridge.posts))


class TestUnknownResult(Safety):
    def test_a_timeout_and_a_504_are_unknown_and_the_pm_is_told_to_check_ado(self):
        base = self.out
        for i, err in enumerate((TimeoutError("timed out"), pull.PullError("HTTP 504 for <url>"), pull.PullError("HTTP 502 for <url>"),
                                 pull.PullError("network error for <url>: connection reset"))):
            with self.subTest(err=str(err)):
                self.out = base / f"case{i}"
                self.enable()
                bridge = self.decide_and_post()
                self.cycle(bridge, http=self.http_with(post_error=err), reply="OK 1")
                self.assertEqual(self.state()["exec"]["0"]["state"], "unknown")
                told = [p for p in bridge.posts if p.startswith(("[kimeru 実行 #1]", "[kimeru 実行 #1 "))]
                self.assertTrue(told and "ADO を確かめて" in told[-1])
                self.assertNotIn("直したら", told[-1])
                self.assertNotIn("実行できませんでした", told[-1])

    def test_a_sure_failure_is_still_a_failure(self):
        self.enable()
        bridge = self.decide_and_post()
        self.cycle(bridge, http=self.http_with(post_error=pull.PullError("HTTP 404 for <url>")), reply="OK 1")
        self.assertEqual(self.state()["exec"]["0"]["state"], "failed")
        self.assertTrue(any("直したら" in p for p in bridge.posts))

    def test_a_failure_to_record_after_the_write_is_unknown(self):
        self.enable()
        bridge = self.decide_and_post()
        real_log = execute._log

        def log(out, rec):
            if rec.get("state") == "done":
                raise OSError("disk full")
            return real_log(out, rec)
        with mock.patch.object(execute, "_log", log):
            self.cycle(bridge, reply="OK 1")
        self.assertEqual(len(self.posts_of("POST")), 1)
        self.assertEqual(self.state()["exec"]["0"]["state"], "unknown")
        self.assertTrue(any("記録に失敗" in p and "ADO を確かめて" in p for p in bridge.posts))

    def test_running_is_written_to_the_execution_record(self):
        self.enable()
        bridge = self.decide_and_post()
        self.cycle(bridge, reply="OK 1")
        self.assertEqual([r["state"] for r in self.rows()], ["running", "done"])
        self.assertEqual(self.rows()[-1]["org"], "contoso-not-real")     # the target is in the record
        self.assertEqual(self.rows()[-1]["project"], "Proj")

    def test_the_text_is_looked_for_in_ado_before_a_redo_and_not_written_when_it_is_there(self):
        self.enable()
        bridge = self.decide_and_post()
        self.cycle(bridge, http=self.http_with(post_error=pull.PullError("HTTP 504 for <url>")), reply="OK 1")
        text = self.state()["record"]["actions"][0]["exec_text"]
        self.calls.clear()
        self.cycle(bridge, http=self.http_with(comments=["<div>" + text.replace("\n", "<br>") + "</div>"]), reply="再実行 1")
        self.assertEqual([c[0] for c in self.calls], ["GET"])            # only a read: nothing written
        self.assertEqual(self.state()["exec"]["0"]["state"], "done")
        self.assertTrue(any("すでにあった" in p and "書いていません" in p for p in bridge.posts))

    def test_when_it_cannot_be_looked_up_nothing_is_written(self):
        self.enable()
        bridge = self.decide_and_post()
        self.cycle(bridge, http=self.http_with(post_error=pull.PullError("HTTP 504 for <url>")), reply="OK 1")
        self.calls.clear()

        def http(method, url, token, body=None, retries=3):
            self.calls.append((method, url, token, body, retries))
            raise pull.PullError("HTTP 401 for <url>")
        self.cycle(bridge, http=http, reply="再実行 1")
        self.assertEqual([c[0] for c in self.calls], ["GET"])

    def test_after_the_pm_closes_it_teams_is_not_read_for_it(self):
        self.enable()
        bridge = self.decide_and_post()
        self.cycle(bridge, http=self.http_with(post_error=TimeoutError("timed out")), reply="OK 1")
        reads = []
        real_read = bridge.read
        bridge.read = lambda: (reads.append(1), real_read())[1]
        ch = self.cycle(bridge, reply="済 1")
        self.assertEqual(ch[-1]["status"], "closed")
        self.assertEqual(self.state()["exec"]["0"]["state"], "closed")
        self.assertEqual(reads, [1])                                    # read once, to see the reply
        for _ in range(3):
            self.cycle(bridge)
        self.assertEqual(reads, [1])                                    # not again
        self.assertEqual(len(self.posts_of("POST")), 1)                 # and nothing was written again
        self.assertEqual(self.rows()[-1]["state"], "closed")

    def test_a_close_reply_is_a_reply_word(self):
        self.assertEqual(notify.parse_reply("済 3"), ("済", "3"))
        self.assertEqual(notify.parse_reply("済#12。"), ("済", "12"))


class TestRoutesThatNeverExecute(Safety):
    def enable_and_expect_no_write(self):
        self.enable()

        def boom(*a, **k):
            raise AssertionError("a write to ADO was attempted")
        return mock.patch.object(pull, "http_json", boom), mock.patch.object(execute, "_token", boom)

    def test_the_demo_never_writes_even_when_execution_is_switched_on(self):
        p1, p2 = self.enable_and_expect_no_write()
        with p1, p2, contextlib.redirect_stdout(io.StringIO()):
            demo.run(ROOT / "examples" / "demo_day.json", GRAPHS, NoCriteria(), PBS, cli.process, self.out / "demo", pace=0)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.out / "demo" / "executions.jsonl").exists())

    def test_the_eval_route_never_writes_and_shows_no_promise_to_write(self):
        p1, p2 = self.enable_and_expect_no_write()
        inbox = self.out / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "w.json").write_text(json.dumps(WI), encoding="utf-8")
        bridge = Bridge()
        with p1, p2:                                                   # what eval/day_run.py does: daily.cycle without real=True
            daily.cycle(self.out, inbox, GRAPHS, NoCriteria(), PBS, cli.process, bridge=bridge, send=True, now=datetime(2026, 10, 1, 9, 0))
            bridge.replies.append("OK 1")
            r = daily.cycle(self.out, inbox, GRAPHS, NoCriteria(), PBS, cli.process, bridge=bridge, send=True, now=datetime(2026, 10, 1, 9, 5))
        self.assertIn("#1:approved", r["approvals"])
        self.assertFalse((self.out / "executions.jsonl").exists())
        self.assertFalse(any("そのまま書きます" in p for p in bridge.posts))
        src = (ROOT / "eval" / "day_run.py").read_text(encoding="utf-8")
        self.assertNotIn("real=True", src)

    def test_run_and_watch_never_write(self):
        p1, p2 = self.enable_and_expect_no_write()
        f = self.out / "w.json"
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps(WI), encoding="utf-8")
        with p1, p2, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--out", str(self.out / "o"), "run", str(f)]), 0)
        self.assertFalse((self.out / "o" / "executions.jsonl").exists())

    def test_only_the_daily_and_approvals_paths_ask_for_real_execution(self):
        import ast
        found = []
        for path in list((ROOT / "kimeru").glob("*.py")) + list((ROOT / "eval").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg == "real" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                            found.append((path.name, ast.unparse(node.func)))
        # daily command, notify command, approvals command: nothing else (the demo, eval/, run and watch never ask)
        self.assertEqual(sorted(found), [("cli.py", "daily.cycle"), ("cli.py", "nt.collect"), ("cli.py", "nt.notify")])

    def test_collect_without_real_records_and_writes_nothing(self):
        self.enable()
        bridge = self.decide_and_post()
        ch = self.cycle(bridge, real=False, reply="OK 1")
        self.assertEqual(ch[0]["status"], "approved")
        self.assertEqual(ch[0]["real"], [])
        self.assertEqual(self.calls, [])


class CountingBridge(Bridge):
    def __init__(self):
        super().__init__()
        self.attempts = 0

    def post(self, text, send):
        self.attempts += 1
        return super().post(text, send)


class TestOneCollectAtATime(Safety):
    def test_a_held_lock_means_nothing_is_read_or_approved_and_the_pm_is_told(self):
        self.enable()
        bridge = self.decide_and_post()
        reads = []
        real_read = bridge.read
        bridge.read = lambda: (reads.append(1), real_read())[1]
        with fsutil.exclusive(self.out / "approvals.lock") as got:
            self.assertTrue(got)
            res = self.cycle(bridge, reply="OK 1")                  # the daily cycle, or a second window, is running
        self.assertTrue(res.busy)
        self.assertEqual(list(res), [])
        self.assertEqual(reads, [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.state()["status"], "pending")
        res = self.cycle(bridge)                                    # the lock is released: the same reply is applied, once
        self.assertFalse(res.busy)
        self.assertEqual(len(self.posts_of("POST")), 1)
        self.assertFalse((self.out / "approvals.lock").exists())

    def test_the_command_says_it_did_nothing(self):
        self.enable()
        bridge = self.decide_and_post()
        buf = io.StringIO()
        with fsutil.exclusive(self.out / "approvals.lock"), contextlib.redirect_stdout(buf), \
                mock.patch("kimeru.notify.PowerShellBridge", lambda: bridge):
            self.assertEqual(cli.main(["--out", str(self.out), "approvals"]), 1)   # like `notify`: nothing was done, so it fails
        self.assertIn("another approvals run is in progress", buf.getvalue())
        self.assertIn("nothing was read or applied", buf.getvalue())

    def test_a_lock_left_by_a_dead_process_is_taken_over(self):
        lock = self.out / "approvals.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("2147483646")   # a process id that no process has
        import os
        os.utime(lock, (0, 0))
        with fsutil.exclusive(lock) as got:
            self.assertTrue(got)


class TestEveryPageAndTheStoredForm(Safety):
    def test_the_same_text_on_the_second_page_is_found(self):
        pages = {None: {"comments": [{"id": 1, "text": "別のコメント"}], "continuationToken": "tok 1"},
                 "tok 1": {"comments": [{"id": 2, "text": "<div>あの 文面</div>"}]}}
        urls = []

        def http(method, url, token, body=None, retries=3):
            urls.append(url)
            return pages["tok 1" if "continuationToken=tok%201" in url else None]
        self.assertEqual(execute._existing_comment("https://x/_apis/wit/workItems", "7", "あの 文面", http, "t"), 2)
        self.assertEqual(len(urls), 2)

    def test_a_less_than_sign_stored_as_an_entity_still_matches(self):
        stored = "<div>件数 &lt; 3 のとき &amp; 確認</div>"
        http = lambda method, url, token, body=None, retries=3: {"comments": [{"id": 9, "text": stored}]}
        self.assertEqual(execute._existing_comment("https://x/_apis/wit/workItems", "7", "件数 < 3 のとき & 確認", http, "t"), 9)

    def test_a_tag_like_text_matches_whether_ads_wrapped_it_or_kept_only_the_escapes(self):
        find = lambda stored, text: execute._existing_comment(
            "https://x/_apis/wit/workItems", "7", text, lambda m, u, t, body=None, retries=3: {"comments": [{"id": 9, "text": stored}]}, "t")
        self.assertEqual(find("<div>&lt;b&gt; を使う</div>", "<b> を使う"), 9)           # wrapped in a tag, the `<b>` escaped
        self.assertEqual(find("&lt;b&gt; を使う", "<b> を使う"), 9)                      # only the escapes
        self.assertEqual(find("<div>List&lt;String&gt; を返す</div>", "List<String> を返す"), 9)
        self.assertEqual(find("List&lt;String&gt; を返す", "List<String> を返す"), 9)
        self.assertEqual(find("<p>件数 &lt; 3</p><p>以上</p>", "件数 < 3 以上"), 9)

    def test_a_redo_does_not_write_when_the_text_is_on_a_later_page(self):
        self.enable()
        bridge = self.decide_and_post()
        self.cycle(bridge, http=self.http_with(post_error=pull.PullError("HTTP 504 for <url>")), reply="OK 1")
        text = self.state()["record"]["actions"][0]["exec_text"]
        self.calls.clear()

        def http(method, url, token, body=None, retries=3):
            self.calls.append((method, url, token, body, retries))
            if "continuationToken" not in url:
                return {"comments": [{"id": 1, "text": "無関係"}], "continuationToken": "next"}
            return {"comments": [{"id": 2, "text": text}]}
        self.cycle(bridge, http=http, reply="再実行 1")
        self.assertEqual([c[0] for c in self.calls], ["GET", "GET"])       # nothing written
        self.assertEqual(self.state()["exec"]["0"]["state"], "done")


class TestUnknownStaysUnknown(Safety):
    def test_a_failed_look_up_on_a_redo_keeps_it_unknown_and_the_close_reply_still_works(self):
        self.enable()
        bridge = self.decide_and_post()
        self.cycle(bridge, http=self.http_with(post_error=pull.PullError("HTTP 504 for <url>")), reply="OK 1")

        def http(method, url, token, body=None, retries=3):
            raise pull.PullError("HTTP 401 for <url>")
        self.cycle(bridge, http=http, reply="再実行 1")
        self.assertEqual(self.state()["exec"]["0"]["state"], "unknown")
        told = [p for p in bridge.posts if p.startswith(("[kimeru 実行 #1]", "[kimeru 実行 #1 "))][-1]
        self.assertIn("確かめられませんでした", told)
        self.assertNotIn("直したら", told)
        self.assertNotIn("実行できませんでした", told)
        ch = self.cycle(bridge, reply="済 1")
        self.assertEqual(ch[-1]["status"], "closed")
        self.assertEqual(self.state()["exec"]["0"]["state"], "closed")

    def test_a_failed_sign_in_on_a_redo_is_the_same(self):
        self.enable()
        bridge = self.decide_and_post()
        self.cycle(bridge, http=self.http_with(post_error=TimeoutError("timed out")), reply="OK 1")
        bridge.replies.append("再実行 1")
        with mock.patch.object(execute, "_token", side_effect=RuntimeError("az failed")), \
                mock.patch.object(pull, "http_json", self.http_with()):
            notify.collect(self.out, bridge, real=True, send=True)
        self.assertEqual(self.state()["exec"]["0"]["state"], "unknown")
        self.assertEqual(len(self.posts_of("POST")), 1)


class TestNoRecordedOrigin(Safety):
    def plain(self):
        self.enable()
        plain = json.loads(json.dumps(WI))
        del plain["kimeru_origin"]
        cli.process(plain, GRAPHS, NoCriteria(), self.out, PBS)
        bridge = Bridge()
        notify.notify(self.out, bridge, send=True, real=True)
        return bridge

    def test_the_post_says_it_will_not_write_and_ok_is_a_record_only_not_a_failure(self):
        bridge = self.plain()
        post = bridge.posts[0]
        self.assertIn("書きません（取り込み元が記録されていない）", post)
        self.assertNotIn("そのまま書きます", post)
        ch = self.cycle(bridge, reply="OK 1")
        self.assertEqual(ch[0]["status"], "approved")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.state()["status"], "approved")
        self.assertEqual(self.state()["exec"]["0"]["state"], "skipped")
        self.assertFalse(any("実行できませんでした" in p for p in bridge.posts[1:]))
        reads = []
        real_read = bridge.read
        bridge.read = lambda: (reads.append(1), real_read())[1]
        for _ in range(3):
            self.cycle(bridge)
        self.assertEqual(reads, [])                                 # nothing waits: Teams is not read

    def test_it_is_not_posted_again_as_an_item_from_before_execution_was_switched_on(self):
        bridge = self.plain()
        self.cycle(bridge, reply="OK 1")
        self.assertEqual(sum(1 for p in bridge.posts if p.startswith("[kimeru #1]")), 1)


class TestRedoWhenTheApprovalPostIsOffScreen(Safety):
    def test_a_redo_after_a_result_post_acts_once_without_the_approval_post(self):
        self.enable()
        bridge = self.decide_and_post()
        fail = self.http_with(post_error=pull.PullError("HTTP 404 for <url>"))
        self.cycle(bridge, http=fail, reply="OK 1")
        bridge.posts = [p for p in bridge.posts if not p.startswith("[kimeru #")]    # scrolled out of the chat
        bridge.replies = [r for r in bridge.replies if r != "OK 1"]
        self.calls.clear()
        self.cycle(bridge, http=fail, reply="再実行 1")
        self.assertEqual(len(self.posts_of("POST")), 1)                         # it acted
        for _ in range(3):
            self.cycle(bridge, http=fail)
        self.assertEqual(len(self.posts_of("POST")), 1)                         # and only once


class TestTheRecordAndThePostNameTheOrigin(Safety):
    def test_every_row_has_the_organization_and_project_and_the_post_shows_the_origin(self):
        self.enable()
        bridge = self.decide_and_post()
        self.assertIn("取り込み元の組織 contoso-not-real / プロジェクト Proj", bridge.posts[0])
        self.cycle(bridge, http=self.http_with(post_error=TimeoutError("timed out")), reply="OK 1")
        self.cycle(bridge, reply="済 1")
        rows = self.rows()
        self.assertEqual([r["state"] for r in rows], ["running", "unknown", "closed"])
        for r in rows:
            self.assertEqual((r["org"], r["project"]), ("contoso-not-real", "Proj"))

    def test_the_rows_of_a_failure_and_a_redo_also_have_them(self):
        self.enable()
        bridge = self.decide_and_post()
        self.cycle(bridge, http=self.http_with(post_error=pull.PullError("HTTP 404 for <url>")), reply="OK 1")
        self.cycle(bridge, reply="再実行 1")
        rows = self.rows()
        self.assertGreaterEqual(len(rows), 4)
        self.assertTrue(all(r.get("org") == "contoso-not-real" and r.get("project") == "Proj" for r in rows))


class TestThePostsAreSentTogether(Safety):
    def test_while_teams_cannot_be_posted_to_the_attempts_do_not_grow_with_the_texts(self):
        self.enable()
        bridge = CountingBridge()
        cli.process(WI, GRAPHS, NoCriteria(), self.out, PBS)
        notify.notify(self.out, bridge, send=True, real=True)
        bridge.fail_posts = True
        bridge.attempts = 0
        ap = notify.Approvals(self.out)
        ap.data["outbox"] = [f"[kimeru 実行 #{i}] old" for i in range(20)]     # a backlog from earlier cycles
        ap.save()
        self.cycle(bridge, reply="OK 1")
        self.assertLessEqual(bridge.attempts, 2)                                # not 20 x 20
        ap = notify.Approvals(self.out)
        self.assertGreaterEqual(len(ap.data["outbox"]), 20)                      # nothing was lost
        self.assertFalse(ap.data.get("outbox_delivery_unknown"))                  # fake failure is known to be before send
        bridge.fail_posts = False
        self.cycle(bridge)
        self.assertEqual(notify.Approvals(self.out).data["outbox"], [])


class TestApprovalsBeforeExecutionWasSwitchedOn(Safety):
    def test_an_ok_does_not_write_and_the_post_is_made_again_with_the_text(self):
        self.enable()
        bridge = self.decide_and_post()
        data = json.loads((self.out / "approvals.json").read_text(encoding="utf-8"))
        for a in data["items"]["1"]["record"]["actions"]:               # as an item posted before it was switched on (or by an old version)
            a.pop("exec_text"), a.pop("exec_target")
        (self.out / "approvals.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        ch = self.cycle(bridge, reply="OK 1")
        self.assertEqual(ch[0]["status"], "reposted")
        self.assertEqual(self.calls, [])
        self.assertFalse(self.state()["posted"])
        self.assertEqual(self.state()["status"], "pending")
        notify.notify(self.out, bridge, send=True, real=True)          # made again
        again = bridge.posts[-1]
        self.assertIn("そのまま書きます", again)
        self.assertIn("contoso-not-real", again)                       # the target is in the post
        self.assertIn(execute.SIGNATURE_FIXED.strip(), again)                # and the signature line
        bridge.replies.clear()
        self.cycle(bridge, reply="OK 1")                               # answered again
        self.assertEqual(len(self.posts_of("POST")), 1)


class TestTheTextIsFixedWhenPosted(Safety):
    def test_a_change_of_the_settings_after_the_post_does_not_change_what_is_written(self):
        self.enable()
        bridge = self.decide_and_post()
        shown = bridge.posts[0]
        os_env = {"KIMERU_EXECUTE_SIGNATURE": "0"}
        with mock.patch.dict("os.environ", os_env):
            config.apply([])
            self.cycle(bridge, reply="OK 1")
        body = self.posts_of("POST")[0][3]["text"]
        self.assertIn(body, shown)                                     # exactly what the post showed
        self.assertTrue(body.endswith(execute.SIGNATURE_FIXED))              # the signature line stayed

    def test_the_text_is_saved_with_the_post(self):
        self.enable()
        self.decide_and_post()
        a = self.state()["record"]["actions"][0]
        self.assertEqual(a["exec_text"], execute.comment_text(a))
        self.assertEqual(a["exec_target"], {"org": "contoso-not-real", "project": "Proj"})


class TestTarget(Safety):
    def test_an_item_pulled_from_another_project_is_not_written(self):
        self.enable()
        other = json.loads(json.dumps(WI))
        other["kimeru_origin"]["project"] = "Elsewhere"
        cli.process(other, GRAPHS, NoCriteria(), self.out, PBS)
        bridge = Bridge()
        notify.notify(self.out, bridge, send=True, real=True)
        self.cycle(bridge, reply="OK 1")
        self.assertEqual(self.calls, [])
        told = [p for p in bridge.posts if p.startswith(("[kimeru 実行 #1]", "[kimeru 実行 #1 "))]
        self.assertTrue(told and "違うため、書きません" in told[-1])
        self.assertEqual(self.state()["exec"]["0"]["state"], "failed")

    def test_an_item_with_no_recorded_origin_is_not_written(self):
        self.enable()
        plain = json.loads(json.dumps(WI))
        del plain["kimeru_origin"]
        cli.process(plain, GRAPHS, NoCriteria(), self.out, PBS)
        bridge = Bridge()
        notify.notify(self.out, bridge, send=True, real=True)
        self.cycle(bridge, reply="OK 1")
        self.assertEqual(self.calls, [])
        self.assertTrue(any("取り込み元" in p for p in bridge.posts))

    def test_pull_ado_records_where_the_item_came_from(self):
        inbox = self.out / "inbox"
        inbox.mkdir(parents=True)

        def http(method, url, token, body=None, retries=3):
            if "wiql" in url:
                return {"workItems": [{"id": 7002}]}
            return {"value": [{"id": 7002, "fields": {"System.Title": "t"}}]}
        pull.pull_ado("contoso-not-real", "Proj", inbox, self.out, http=http, token="x")
        payload = json.loads(next(inbox.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(payload["kimeru_origin"], {"org": "contoso-not-real", "project": "Proj"})
        from kimeru import events
        self.assertEqual(events.normalize(payload)[0]["origin"], {"org": "contoso-not-real", "project": "Proj"})
        self.assertNotIn("origin", events.state_of(events.normalize(payload)[0]))   # the judge does not see it


class TestPostsWhileCollecting(Safety):
    def test_collect_without_send_only_pastes(self):
        self.enable()
        bridge = self.decide_and_post()
        self.cycle(bridge, reply="OK 1", send=False)
        self.assertEqual(len(self.posts_of("POST")), 1)                # (the write itself is the approval's effect)
        collected = bridge.posts[1:]
        self.assertTrue(collected)
        self.assertEqual(bridge.sent_flags[1:], [False] * len(collected))   # nothing was sent
        self.assertTrue(notify.Approvals(self.out).data["outbox"])          # and it is still to be sent

    def test_a_result_that_could_not_be_posted_is_posted_again_next_cycle(self):
        self.enable()
        bridge = self.decide_and_post()
        bridge.fail_posts = True
        self.cycle(bridge, reply="OK 1")
        self.assertEqual(len(self.posts_of("POST")), 1)
        self.assertEqual([p for p in bridge.posts if p.startswith("[kimeru 実行")], [])
        self.assertTrue(notify.Approvals(self.out).data["outbox"])
        bridge.fail_posts = False
        self.assertFalse(notify.Approvals(self.out).data.get("outbox_delivery_unknown"))
        self.cycle(bridge)
        self.assertTrue(any(p.startswith(("[kimeru 実行 #1]", "[kimeru 実行 #1 ")) and "実行しました" in p for p in bridge.posts))
        self.assertFalse(notify.Approvals(self.out).data["outbox"])
        self.assertEqual(len(self.posts_of("POST")), 1)
        n = len(bridge.posts)
        self.cycle(bridge)
        self.assertEqual(len(bridge.posts), n)                         # told once


class TestTheApprovalsCommand(Safety):
    def test_approvals_has_a_send_flag(self):
        with mock.patch.object(notify, "PowerShellBridge") as pb, mock.patch.object(notify, "collect", return_value=[]) as col:
            self.assertEqual(cli.main(["--out", str(self.out), "approvals"]), 0)
            self.assertEqual(cli.main(["--out", str(self.out), "approvals", "--send"]), 0)
        self.assertEqual([c.kwargs for c in col.call_args_list], [{"real": True, "send": False}, {"real": True, "send": True}])
