"""Fix 10: one lock for every write of approvals.json, the same-text check, `再実行` on any screen, forgiving `push`, the
warnings of run / watch / demo, setup-managed remove, and the commands in the docs. Every call to ADO is mocked, every Teams is a
fake, PowerShell only runs the restore functions of setup-managed.ps1 on a temporary config file: nothing leaves the test."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import contextlib
import copy
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import cli, config, execute, fsutil, fulltext, notify, pull, push
from tests.test_execute import GRAPHS, PBS, Bridge, NoCriteria, ROOT, WI
from tests.test_execute_safety import Safety


class HookBridge(Bridge):
    """A Teams whose post calls `hook` once, from inside the post (a hand-typed approvals run that starts meanwhile)."""
    hook = None
    hook_on = "[kimeru #2"
    hook_result = None

    def post(self, text, send):
        if self.hook and text.startswith(self.hook_on):
            h, self.hook = self.hook, None
            self.hook_result = h()
        return super().post(text, send)


class TestOneLockForEveryWrite(Safety):
    def second_item(self):
        wi2 = copy.deepcopy(WI)
        wi2["resource"]["id"] = 7003
        cli.process(wi2, GRAPHS, NoCriteria(), self.out, PBS)

    def test_an_approval_typed_by_hand_while_notify_posts_is_not_lost_and_is_written_once(self):
        self.enable()
        first = self.decide_and_post()                       # #1 is posted and waits for an answer
        bridge = HookBridge()
        bridge.posts.extend(first.posts)
        bridge.replies.append("OK 1")
        self.second_item()                                   # #2 is new: notify has a copy of approvals.json in hand while it posts
        seen = {}

        def typed_by_hand():
            with mock.patch.object(pull, "http_json", self.http_with()), mock.patch.object(execute, "_token", lambda org: "t"), \
                    mock.patch.object(pull, "ado_tenant", lambda org: "tenant"):
                seen["res"] = notify.collect(self.out, bridge, real=True, send=True)
            return seen["res"]
        bridge.hook = typed_by_hand
        notify.notify(self.out, bridge, send=True, real=True)
        self.assertTrue(seen["res"].busy)                    # it did nothing: the lock was held
        self.assertEqual(self.posts_of("POST"), [])
        data = json.loads((self.out / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual(data["items"]["1"]["status"], "pending")
        self.assertTrue(data["items"]["2"]["posted"])        # notify's own record is intact
        res = self.cycle(bridge)                             # the next approvals run applies the reply, once
        self.assertEqual([c["status"] for c in res], ["approved"])
        self.assertEqual(len(self.posts_of("POST")), 1)
        self.assertEqual(len([p for p in bridge.posts if p.startswith(("[kimeru 実行 #1]", "[kimeru 実行 #1 "))]), 1)
        self.cycle(bridge)
        self.cycle(bridge)
        self.assertEqual(len(self.posts_of("POST")), 1)
        self.assertEqual(len([p for p in bridge.posts if p.startswith(("[kimeru 実行 #1]", "[kimeru 実行 #1 "))]), 1)
        item = self.state()
        self.assertEqual(item["status"], "approved")
        self.assertEqual(item["exec"]["0"]["state"], "done")

    def test_with_the_lock_held_every_writer_does_nothing_and_says_so(self):
        self.enable()
        bridge = self.decide_and_post()
        self.second_item()
        before = (self.out / "approvals.json").read_bytes()
        posts = list(bridge.posts)
        with fsutil.exclusive(self.out / "approvals.lock") as got:
            self.assertTrue(got)
            res = notify.notify(self.out, bridge, send=True, real=True)
            self.assertTrue(res.busy)
            self.assertEqual(list(res), [])
            self.assertTrue(notify.notify_notices(self.out, bridge, send=True).busy)
            self.assertFalse(notify.mark_toasted(self.out, [1], [], unposted=0))
            self.assertTrue(fulltext.purge(self.out).busy)
            merged = cli._merge_into_pending(self.out, {"kind": "teams.chat", "chat_id": "c", "author": "a", "id": "e1"}, {})
            self.assertFalse(merged)
            self.assertTrue(notify.collect(self.out, bridge, real=True, send=True).busy)
        self.assertEqual((self.out / "approvals.json").read_bytes(), before)   # nothing was written
        self.assertEqual(bridge.posts, posts)                                   # and nothing was posted
        self.assertFalse((self.out / "approvals.lock").exists())
        self.assertEqual(len(notify.notify(self.out, bridge, send=True, real=True)), 1)   # the lock is free again: #2 is posted

    def test_a_lock_is_not_left_behind_after_a_failed_post(self):
        self.enable()
        cli.process(WI, GRAPHS, NoCriteria(), self.out, PBS)
        bridge = Bridge()
        bridge.fail_posts = True
        with self.assertRaises(RuntimeError):
            notify.notify(self.out, bridge, send=True, real=True)
        self.assertFalse((self.out / "approvals.lock").exists())


class TestTheSameTextCheck(unittest.TestCase):
    def test_a_stored_form_wrapped_in_tags_matches_the_approved_text(self):
        p = execute._plain
        self.assertEqual(p("<div>件数 &lt; 3</div>"), p("件数 < 3"))
        self.assertEqual(p("<div>件数 &lt; 3</div>"), p("件数 &lt; 3"))
        self.assertEqual(p("<p>件数 &lt; 3</p><p>以上</p>"), p("件数 < 3 以上"))
        self.assertEqual(p("a &amp;lt; b"), "a &lt; b")                            # an escape is resolved once

    def test_sentences_that_differ_only_between_a_less_than_and_a_greater_than_do_not_match(self):
        p = execute._plain
        self.assertNotEqual(p("件数 < 3 件"), p("件数 < 5 件"))
        self.assertNotEqual(p("a < 3 or b > 5"), p("a < 4 or b > 5"))
        self.assertNotEqual(p("<div>件数 &lt; 3 件</div>"), p("件数 < 5 件"))

    def test_the_lookup_finds_the_stored_form_and_not_the_other_sentence(self):
        stored = "<div>件数 &lt; 3</div>"
        http = lambda method, url, token, body=None, retries=3: {"comments": [{"id": 4, "text": stored}]}
        find = lambda text: execute._existing_comment("https://x/_apis/wit/workItems", "7", text, http, "t")
        self.assertEqual(find("件数 < 3"), 4)
        self.assertIsNone(find("件数 < 5"))


def timeline_of(bridge):
    return bridge.read()["timeline"]


class TestRedoOnEveryScreen(Safety):
    FAIL = pull.PullError("HTTP 404 for <url>")

    def start(self, scroll_out):
        self.enable()
        bridge = self.decide_and_post()
        fail = self.http_with(post_error=self.FAIL)
        self.cycle(bridge, http=fail, reply="OK 1")                # POST 1: fails (a sure failure), result post X
        if scroll_out:
            bridge.posts = [p for p in bridge.posts if not p.startswith("[kimeru #")]      # the approval post is off the screen
            bridge.replies = [r for r in bridge.replies if r != "OK 1"]
        return bridge, fail

    def run_case(self, scroll_out):
        bridge, fail = self.start(scroll_out)
        self.cycle(bridge, http=fail, reply="再実行 1")            # POST 2, result post
        self.assertEqual(len(self.posts_of("POST")), 2)
        for _ in range(2):
            self.cycle(bridge, http=fail)                          # the same reply does not act again
        self.assertEqual(len(self.posts_of("POST")), 2)
        results = len([p for p in bridge.posts if p.startswith(("[kimeru 実行 #1]", "[kimeru 実行 #1 "))])
        self.cycle(bridge, http=fail, reply="再実行 1")            # the second reply after the result post acts
        self.assertEqual(len(self.posts_of("POST")), 3)
        self.assertEqual(len([p for p in bridge.posts if p.startswith(("[kimeru 実行 #1]", "[kimeru 実行 #1 "))]), results + 1)   # and its result is posted
        for _ in range(2):
            self.cycle(bridge, http=fail)
        self.assertEqual(len(self.posts_of("POST")), 3)
        self.cycle(bridge, http=fail, reply="再実行 1")            # and a third, and so on
        self.assertEqual(len(self.posts_of("POST")), 4)
        return bridge

    def test_every_redo_acts_once_when_the_approval_post_is_on_the_screen(self):
        bridge = self.run_case(scroll_out=False)
        self.assertIn("P:1", timeline_of(bridge))

    def test_every_redo_acts_once_when_the_approval_post_is_off_the_screen(self):
        bridge = self.run_case(scroll_out=True)
        self.assertNotIn("P:1", timeline_of(bridge))
        self.assertEqual(len([e for e in timeline_of(bridge) if e.startswith("X:1:")]), 4)

    def test_a_result_post_that_the_screen_does_not_show_does_not_make_a_reply_act_again(self):
        bridge, fail = self.start(scroll_out=False)
        real_read = bridge.read
        bridge.read = lambda: {k: ([e for e in v if not e.startswith("X:")] if k == "timeline" else v) for k, v in real_read().items()}
        self.cycle(bridge, http=fail, reply="再実行 1")
        for _ in range(3):
            self.cycle(bridge, http=fail)
        self.assertEqual(len(self.posts_of("POST")), 2)


class TestARefusedRedoOfAnUnknownResult(Safety):
    def test_it_stays_unknown_and_the_close_reply_still_works(self):
        self.enable()
        bridge = self.decide_and_post()
        self.cycle(bridge, http=self.http_with(post_error=pull.PullError("HTTP 504 for <url>")), reply="OK 1")
        self.assertEqual(self.state()["exec"]["0"]["state"], "unknown")
        os.environ["KIMERU_ADO_PROJECT"] = "Elsewhere"       # the target now differs from where the work item came from: refused
        config.apply([])
        self.calls.clear()
        self.cycle(bridge, reply="再実行 1")
        self.assertEqual(self.calls, [])                     # nothing was sent, nothing was read
        self.assertEqual(self.state()["exec"]["0"]["state"], "unknown")
        told = [p for p in bridge.posts if p.startswith(("[kimeru 実行 #1]", "[kimeru 実行 #1 "))][-1]
        self.assertIn("確かめられませんでした", told)
        self.assertNotIn("書けていません", told)
        self.assertNotIn("直したら", told)
        ch = self.cycle(bridge, reply="済 1")
        self.assertEqual(ch[-1]["status"], "closed")
        self.assertEqual(self.state()["exec"]["0"]["state"], "closed")

    def test_a_refused_redo_of_a_sure_failure_is_still_a_failure(self):
        self.enable()
        bridge = self.decide_and_post()
        self.cycle(bridge, http=self.http_with(post_error=pull.PullError("HTTP 404 for <url>")), reply="OK 1")
        os.environ["KIMERU_ADO_PROJECT"] = "Elsewhere"
        config.apply([])
        self.cycle(bridge, reply="再実行 1")
        self.assertEqual(self.state()["exec"]["0"]["state"], "failed")


class TestPushNamesAreReadForgivingly(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = self.dir.name
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def routes(self, raw):
        os.environ["KIMERU_PUSH"] = raw
        warnings = config.apply([])
        return push.enabled_routes(), warnings

    def test_case_and_spaces_are_read_as_the_route_names(self):
        self.assertEqual(self.routes("teams_webhook,Outlook")[0], ["teams_webhook", "outlook"])
        self.assertEqual(self.routes(" TEAMS_WEBHOOK ")[0], ["teams_webhook"])
        self.assertEqual(self.routes("webhook, webhook ,OUTLOOK")[0], ["webhook", "outlook"])
        self.assertEqual(self.routes("teams_webhook,Outlook")[1], [])

    def test_only_the_unknown_name_is_ignored_and_it_is_recorded_without_its_value(self):
        routes, warnings = self.routes("teams_webhook,foo")
        self.assertEqual(routes, ["teams_webhook"])
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("foo", warnings[0])
        rep = config.report()
        self.assertEqual(rep["warnings"], warnings)                                    # daily writes this to daily.log.jsonl
        routes, warnings = self.routes("https://example.invalid/hook?sig=SECRET")   # a URL written here: never echoed
        self.assertEqual(routes, [])
        self.assertNotIn("SECRET", " ".join(warnings) + json.dumps(config.report()))

    def test_config_set_still_accepts_only_route_names(self):
        with self.assertRaises(config.ConfigError):
            config.set_value("push", "teams_webhook,foo")
        config.set_value("push", "teams_webhook,outlook")


class TestWarningsOfRunWatchAndDemoAreRecorded(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = self.dir.name
        clean["KIMERU_WRITER"] = "not-a-writer"
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        self.out = Path(self.dir.name) / "out"

    def tearDown(self):
        self.env.stop()
        config.apply([])
        self.dir.cleanup()

    def main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = cli.main(["--out", str(self.out), *argv])
        return rc, err.getvalue()

    def recorded(self):
        p = self.out / "warnings.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []

    def test_run_records_the_reason_in_warnings_jsonl(self):
        rc, err = self.main("run", str(ROOT / "examples" / "teams_chat.json"))
        self.assertEqual(rc, 0)
        self.assertIn("writer", err)                                       # shown on the screen, as before
        rows = self.recorded()
        self.assertTrue(any("writer" in r.get("config", "") and "not allowed" in r["config"] for r in rows), rows)
        self.assertNotIn("not-a-writer".upper(), json.dumps(rows))

    def test_watch_once_records_it_too(self):
        inbox = Path(self.dir.name) / "inbox"
        inbox.mkdir()
        rc, _ = self.main("watch", str(inbox), "--once")
        self.assertEqual(rc, 0)
        self.assertTrue(any("writer" in r.get("config", "") for r in self.recorded()))

    def test_demo_records_it_too(self):
        rc, _ = self.main("demo", "--pace", "0")
        self.assertEqual(rc, 0)
        self.assertTrue(any("writer" in r.get("config", "") for r in self.recorded()))

    def test_nothing_is_recorded_when_all_is_well(self):
        os.environ.pop("KIMERU_WRITER")
        self.main("run", str(ROOT / "examples" / "teams_chat.json"))
        self.assertEqual(self.recorded(), [])


POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


@unittest.skipUnless(POWERSHELL, "PowerShell is needed to run the restore functions of setup-managed.ps1")
class TestSetupManagedRemove(unittest.TestCase):
    """Runs only Get-PreviousConfigValues / Restore-ConfigValues of tools\\setup-managed.ps1 on a temporary config file: no
    scheduled task, no environment variable, no shortcut is touched."""

    def restore(self, config_before, previous):
        src = (ROOT / "tools" / "setup-managed.ps1").read_text(encoding="utf-8-sig")
        body = src[src.index("$cfgKeys ="):src.index("function Restore-Previous")]
        body = body[body.index("function Get-PreviousConfigValues"):]
        with tempfile.TemporaryDirectory() as d:
            cfg, prev = Path(d) / "config.json", Path(d) / "setup-previous.json"
            cfg.write_text(json.dumps(config_before), encoding="utf-8")
            prev.write_text(json.dumps(previous), encoding="utf-8")
            script = (f"$ErrorActionPreference='Stop'\n$cfgFile='{cfg}'\n$cfgKeys = @('backend', 'ado_org', 'ado_project')\n{body}\n"
                      f"$prev = Get-Content -Raw -Encoding UTF8 '{prev}' | ConvertFrom-Json\n"
                      "Restore-ConfigValues (Get-PreviousConfigValues $prev)\n")
            ps1 = Path(d) / "t.ps1"
            ps1.write_text(script, encoding="utf-8-sig")
            r = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)], capture_output=True,
                               text=True, encoding="utf-8", timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            return json.loads(cfg.read_text(encoding="utf-8-sig"))

    def test_the_old_record_puts_back_backend_only(self):
        after = self.restore({"backend": "kev", "ado_org": "old-org", "ado_project": "old-proj", "toast": "0"},
                             {"backend": None, "shortcut": False, "configBackend": None})
        self.assertEqual(after, {"ado_org": "old-org", "ado_project": "old-proj", "toast": "0"})   # what install never wrote stays

    def test_the_old_record_with_a_backend_before_puts_that_back(self):
        after = self.restore({"backend": "kev", "ado_org": "o"}, {"backend": None, "shortcut": False, "configBackend": "jev"})
        self.assertEqual(after, {"backend": "jev", "ado_org": "o"})

    def test_the_new_record_puts_back_all_three(self):
        after = self.restore({"backend": "kev", "ado_org": "new-org", "ado_project": "new-proj", "toast": "0"},
                             {"backend": None, "shortcut": False,
                              "configValues": {"backend": None, "ado_org": None, "ado_project": None}})
        self.assertEqual(after, {"toast": "0"})
        after = self.restore({"backend": "kev", "ado_org": "new-org", "ado_project": "new-proj"},
                             {"backend": None, "shortcut": False,
                              "configValues": {"backend": "jev", "ado_org": "old-org", "ado_project": None}})
        self.assertEqual(after, {"backend": "jev", "ado_org": "old-org"})

    def test_no_record_leaves_the_config_alone(self):
        before = {"backend": "kev", "ado_org": "o", "ado_project": "p"}
        self.assertEqual(self.restore(before, {"backend": None, "shortcut": False}), before)


class TestTheDocs(unittest.TestCase):
    def test_every_run_check_command_line_is_intact(self):
        text = (ROOT / "docs" / "managed-pc-check.md").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?<![a-z])un-check\.cmd", "a command broke at a backslash-r")
        lines = text.splitlines()
        self.assertFalse([l for l in lines if l.rstrip().endswith("`.")], "a command line was cut after `.`")
        fenced, bad = False, []
        for l in lines:
            if l.strip().startswith("```"):
                fenced = not fenced
            elif fenced and "run-check.cmd" in l and not l.strip().startswith(".\\run-check.cmd"):
                bad.append(l)
        self.assertEqual(bad, [])
        for l in lines:   # in a sentence, a command is written with its `.\` in front, or as the bare file name
            for m in re.finditer(r"`(\S*?)run-check\.cmd[^`]*`", l):
                self.assertIn(m.group(1), ("", ".\\"), l)
        t20 = [l for l in lines if "run-check.cmd T20" in l]
        self.assertTrue(t20 and all(".\\run-check.cmd T20" in l for l in t20))

    def test_the_push_line_and_the_lock_are_described_as_they_work(self):
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        line = next(l for l in ref.splitlines() if l.startswith("| `KIMERU_PUSH`"))
        self.assertNotIn("最初の 1 回は、既存の分を記録するだけで送りません", line)
        self.assertIn("最初のサイクル", line)
        self.assertIn("新しく投稿された分", line)
        sec = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertNotIn("（同じ OK を 2 回書きません）", sec)
        self.assertIn("approvals.json", sec)
        self.assertIn("ロックが取れなければ", sec)


if __name__ == "__main__":
    unittest.main()
