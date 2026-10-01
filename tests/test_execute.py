"""Approved actions carried out for real (ADO comments only). Every call to ADO is mocked: nothing leaves the test."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import cli, config, execute, graph, notify, plan, pull
from kimeru.backends import StubBackend

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)

SECRET_TOKEN = "SECRET-TOKEN-ZZ"
TENANT = "11111111-2222-3333-4444-555555555555"
TEXT = "受け入れ条件を追記してください。"
WI = {"eventType": "workitem.created", "resource": {"id": 7002, "fields": {
    "System.WorkItemType": "Task", "System.Title": "レポート画面の並び順を変えたい", "System.CreatedBy": {"displayName": "Ito"},
    "System.Description": ""}}, "kimeru_origin": {"org": "contoso-not-real", "project": "Proj"}}


class NoCriteria(StubBackend):
    """The model finds no acceptance criteria in the work item: the graph asks the author for them (an ado.comment)."""

    def ask(self, state, questions):
        out = super().ask(state, questions)
        if "ready" in out:
            out["ready"] = {"noul": 0.05}
        return out


class _Tracked(list):
    """A list of posts (or replies) that tells the bridge, in order, what was added: the chat keeps them in one timeline."""

    def __init__(self, owner, kind, items=()):
        super().__init__(items)
        self._owner, self._kind = owner, kind

    def append(self, x):
        super().append(x)
        self._owner._chron.append((self._kind, x))

    def extend(self, xs):
        for x in xs:
            self.append(x)


class Bridge:
    """A fake self chat: what was posted, and the replies to hand back. The timeline is chronological (a reply that comes
    before a result post is before it on the screen), as the real chat is."""

    def __init__(self):
        self._chron = []
        self._posts, self._replies = _Tracked(self, "P"), _Tracked(self, "R")
        self.timeline, self.fail_posts = [], False
        self.sent_flags = []

    def _set(self, kind, items):
        items = list(items)
        keep, j = [], 0
        for k, t in self._chron:   # what is still there keeps its place; what is new goes to the end
            if k == kind and j < len(items) and items[j] == t:
                keep.append((k, t))
                j += 1
            elif k != kind:
                keep.append((k, t))
        keep.extend((kind, t) for t in items[j:])
        self._chron = keep
        return _Tracked(self, kind, items)

    posts = property(lambda self: self._posts, lambda self, v: setattr(self, "_posts", self._set("P", v)))
    replies = property(lambda self: self._replies, lambda self, v: setattr(self, "_replies", self._set("R", v)))

    def post(self, text, send):
        if self.fail_posts:
            raise RuntimeError("Teams is not reachable")
        self.posts.append(text)
        self.sent_flags.append(bool(send))
        return {"ok": True, "typed": True, "sent": bool(send)}

    def read(self):
        tl = []
        for kind, text in self._chron:   # what tools/teams-self.ps1 reads: "P:N" for an approval post, "X:N" for a result post
            if kind == "R":
                tl.append("R:" + text)
            elif text.startswith("[kimeru #"):
                tl.append("P:" + text[len("[kimeru #"):].split("]")[0])
            elif text.startswith("[kimeru 実行 #"):
                head = text[len("[kimeru 実行 #"):].split("]")[0].split()   # "N" or "N k"
                tl.append("X:%s:%s" % (head[0], head[1] if len(head) > 1 else 0))
        return {"timeline": tl, "replies": list(self.replies)}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name) / "out"
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = self.dir.name
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        config.apply([])
        self.calls = []
        # nothing in these tests may reach a network: any real HTTP call is a test failure
        self.net = mock.patch("urllib.request.urlopen", side_effect=AssertionError("a real HTTP call was attempted"))
        self.net.start()

    def tearDown(self):
        self.net.stop()
        self.env.stop()
        self.dir.cleanup()

    def enable(self, **extra):
        os.environ.update({"KIMERU_EXECUTE": "ado.comment", "KIMERU_ADO_ORG": "contoso-not-real", "KIMERU_ADO_PROJECT": "Proj", **extra})
        config.apply([])

    def http(self, fail=None, crash=False):
        def fake(method, url, token, body=None, retries=3):
            self.calls.append((method, url, token, body, retries))
            if crash:
                raise KeyboardInterrupt("simulated crash")
            if fail:
                raise pull.PullError(fail)
            return {"id": 555}
        return fake

    def decide_and_post(self):
        cli.process(WI, GRAPHS, NoCriteria(), self.out, PBS)
        bridge = Bridge()
        notify.notify(self.out, bridge, send=True, real=True)
        return bridge

    def collect(self, bridge, real=True, send=True):
        return notify.collect(self.out, bridge, real=real, send=send)

    def approve(self, bridge, n=1, word="OK", http=None, token=None):
        bridge.replies.append(f"{word} {n}")
        with mock.patch.object(pull, "http_json", http or self.http()), \
                mock.patch.object(execute, "_token", lambda org: token or SECRET_TOKEN), \
                mock.patch.object(pull, "ado_tenant", lambda org: TENANT):
            return notify.collect(self.out, bridge, real=True, send=True)


class TestDefaultWritesNothing(Base):
    def test_approving_records_a_plan_and_touches_nothing_outside(self):
        ap = notify.Approvals(self.out)
        rec = {"graph": "g", "event_id": "1", "node": "n", "actions": [{"type": "ado.comment", "id": "7002", "text": "a"}]}
        n = ap.add("g:1:n", rec)
        ap.data["items"][str(n)]["posted"] = True
        ap.save()
        bridge = Bridge()
        bridge.posts.append(f"[kimeru #{n}] x")
        ch = self.approve(bridge, n=n)
        self.assertEqual(ch[0]["status"], "approved")
        self.assertEqual(ch[0]["real"], [])                          # nothing was executed
        self.assertEqual(self.calls, [])
        self.assertFalse((self.out / "executions.jsonl").exists())


class TestAdoComment(Base):
    def test_it_is_written_once_after_the_approval_and_never_again(self):
        self.enable()
        bridge = self.decide_and_post()
        self.assertEqual(self.calls, [])                             # posting for approval writes nothing
        self.approve(bridge)
        self.assertEqual(len(self.calls), 1)
        method, url, token, body, retries = self.calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("/_apis/wit/workItems/7002/comments", url)
        self.assertEqual(retries, 1)                                 # no automatic second try
        self.approve(bridge)                                         # collect() again: the same reply is still in the chat
        self.collect(bridge)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(any("実行しました" in p and "7002" in p for p in bridge.posts))

    def test_the_text_is_the_shown_text_plus_one_signature_line_and_nothing_else(self):
        self.enable()
        bridge = self.decide_and_post()
        shown = bridge.posts[0]
        template = next(a for a in GRAPHS["ado.workitem.created"][0]["nodes"]["request_info"]["actions"] if a["type"] == "ado.comment")["text"]
        self.assertIn(template + execute.SIGNATURE, shown)           # the approval post shows the full text, signature line included
        self.approve(bridge)
        body = self.calls[0][3]
        self.assertEqual(set(body), {"text"})                        # no drafted_by / template_text / unverified ...
        self.assertEqual(body["text"], template + execute.SIGNATURE)  # word for word, plus the one signature line

    def test_the_signature_can_be_switched_off(self):
        self.enable(KIMERU_EXECUTE_SIGNATURE="0")
        bridge = self.decide_and_post()
        self.approve(bridge)
        self.assertNotIn("本人が承認", self.calls[0][3]["text"])

    def test_a_switched_on_kind_waits_for_approval_even_without_a_writer(self):
        self.enable()
        res = cli.process(WI, GRAPHS, NoCriteria(), self.out, PBS)[0]
        self.assertTrue(res["needs_human"])
        self.assertEqual(self.calls, [])
        queue = (self.out / "queue.jsonl").read_text(encoding="utf-8")
        self.assertIn("ado.comment", queue)

    def test_the_post_shows_what_will_be_written(self):
        self.enable()
        bridge = self.decide_and_post()
        self.assertIn("作業項目 7002", bridge.posts[0])
        self.assertIn("そのまま書きます", bridge.posts[0])


class TestNoDoubleWrite(Base):
    def test_a_crash_in_the_middle_is_not_repeated_and_the_pm_is_told(self):
        self.enable()
        bridge = self.decide_and_post()
        with self.assertRaises(KeyboardInterrupt):
            self.approve(bridge, http=self.http(crash=True))
        data = json.loads((self.out / "approvals.json").read_text(encoding="utf-8"))["items"]["1"]
        self.assertEqual(data["exec"]["0"]["state"], "running")     # saved before the call
        self.assertEqual(data["status"], "approved")
        self.calls.clear()
        bridge.replies.clear()
        ch = self.approve(bridge, word="OK")                         # the next cycle: same reply still visible
        self.assertEqual(self.calls, [])                             # not written again on its own
        self.assertEqual(ch, [])
        self.assertTrue(any("書けたかどうか分かりません" in p for p in bridge.posts))   # the PM was told (once)
        ap = notify.Approvals(self.out)
        execute.run_approved(self.out, ap, 1, ap.data["items"]["1"], lambda t: None)   # a later run: still not written
        self.assertEqual(self.calls, [])

    def test_the_pm_is_told_once_that_a_stopped_execution_has_an_unknown_result(self):
        self.enable()
        bridge = self.decide_and_post()
        with self.assertRaises(KeyboardInterrupt):
            self.approve(bridge, http=self.http(crash=True))
        bridge.replies.clear()
        self.collect(bridge)
        self.collect(bridge)
        told = [p for p in bridge.posts if "書けたかどうか分かりません" in p]
        self.assertEqual(len(told), 1)                               # once
        self.assertEqual(len(self.calls), 1)                         # the crashed call only: never repeated
        self.assertNotIn("[kimeru #", told[0])                       # not mistaken for a new approval post

    def test_a_failure_keeps_the_approval_and_the_pm_can_run_it_again(self):
        self.enable()
        bridge = self.decide_and_post()
        self.approve(bridge, http=self.http(fail="HTTP 500 for <url>"))
        data = json.loads((self.out / "approvals.json").read_text(encoding="utf-8"))["items"]["1"]
        self.assertEqual(data["status"], "approved")                 # the approval stays
        self.assertEqual(data["exec"]["0"]["state"], "failed")
        self.assertTrue(any("実行できませんでした" in p for p in bridge.posts))
        before = len(self.calls)
        bridge.replies.clear()
        self.collect(bridge)
        self.assertEqual(len(self.calls), before)                    # not run again by itself
        ch = self.approve(bridge, word="再実行")
        self.assertEqual([c[0] for c in self.calls[before:]], ["GET", "POST"])   # ADO is asked first whether the text is there
        data = json.loads((self.out / "approvals.json").read_text(encoding="utf-8"))["items"]["1"]
        self.assertEqual(data["exec"]["0"]["state"], "done")
        self.assertEqual(ch[-1]["status"], "redo")

    def test_one_failing_action_does_not_stop_the_others_from_being_saved(self):
        self.enable()
        ap = notify.Approvals(self.out)
        rec = {"graph": "g", "event_id": "1", "node": "n", "actions": [
            {"type": "ado.comment", "id": "7001", "text": "a"}, {"type": "ado.comment", "id": "7002", "text": "b"}]}
        n = ap.add("g:1:n", rec)
        ap.data["items"][str(n)]["posted"] = True
        ap.data["items"][str(n)]["status"] = "approved"
        for a in rec["actions"]:
            a["exec_text"] = a["text"]
        rec["event"] = {"origin": {"org": "contoso-not-real", "project": "Proj"}}
        ap.save()
        seen = []

        def http(method, url, token, body=None, retries=3):
            seen.append(url)
            if "7001" in url:
                raise pull.PullError("HTTP 404 for <url>")
            return {"id": 9}

        posts = []
        with mock.patch.object(execute, "_token", lambda org: SECRET_TOKEN):
            res = execute.run_approved(self.out, ap, n, ap.data["items"][str(n)], posts.append, http=http)
        self.assertEqual([r["state"] for r in res], ["failed", "done"])
        saved = json.loads((self.out / "approvals.json").read_text(encoding="utf-8"))["items"][str(n)]["exec"]
        self.assertEqual((saved["0"]["state"], saved["1"]["state"]), ("failed", "done"))


class TestSecretsStayOut(Base):
    def test_no_token_tenant_or_query_reaches_a_record_or_a_message(self):
        self.enable()
        bridge = self.decide_and_post()
        boom = f"HTTP 401 for https://dev.azure.com/x/_apis/wit?tenant={TENANT}&sig=SECRET-SIG Bearer {SECRET_TOKEN}"
        self.approve(bridge, http=self.http(fail=boom))
        everything = "\n".join(bridge.posts) + (self.out / "executions.jsonl").read_text(encoding="utf-8") \
            + (self.out / "approvals.json").read_text(encoding="utf-8")
        for secret in (SECRET_TOKEN, TENANT, "SECRET-SIG", "?tenant"):
            self.assertNotIn(secret, everything)

    def test_a_missing_organization_is_a_failure_with_a_clear_reason(self):
        os.environ["KIMERU_EXECUTE"] = "ado.comment"
        config.apply([])
        bridge = self.decide_and_post()
        self.approve(bridge)
        self.assertTrue(any("組織とプロジェクト" in p for p in bridge.posts))
        self.assertEqual(self.calls, [])


class TestOtherKinds(Base):
    def test_an_unsupported_kind_is_reported_and_stays_record_only(self):
        os.environ["KIMERU_EXECUTE"] = "ado.update,teams.reply"
        config.apply([])
        self.assertEqual(execute.enabled_types(), set())
        self.assertEqual(execute.unsupported(), ["ado.update", "teams.reply"])
        from contextlib import redirect_stdout
        import io
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["config", "set", "execute", "ado.update"])
        self.assertIn("未対応", buf.getvalue())
        bridge = self.decide_and_post()
        self.approve(bridge)
        self.assertEqual(self.calls, [])

    def test_an_approved_reply_comes_back_as_its_text_alone_and_is_not_sent(self):
        ap = notify.Approvals(self.out)
        rec = {"graph": "g", "event_id": "1", "node": "n",
               "actions": [{"type": "teams.reply", "chat_id": "19:abc@thread.v2", "text": "承知しました。確認して返信します。"}]}
        n = ap.add("g:1:n", rec)
        it = ap.data["items"][str(n)]
        posts = []
        execute.send_ready_posts(n, it, posts.append)
        self.assertEqual(posts, [f"[kimeru 送信用 #{n}]\n承知しました。確認して返信します。"])
        self.assertTrue(posts[0].startswith("[kimeru"))              # the self-chat guard accepts only such posts
        posts.clear()
        execute.send_ready_posts(n, it, posts.append, link=True)
        self.assertIn("https://teams.microsoft.com/l/chat/19%3Aabc%40thread.v2/conversations", posts[0])
        self.assertNotIn("承知しました", posts[0].split("実験的")[1])   # the link carries no text

    def test_approving_a_reply_posts_the_ready_text_by_default(self):
        bridge = Bridge()
        cli.process({"kind": "teams.chat", "id": "1", "chat_id": "19:c", "author": "X", "text": "これは何ですか", "mentions_me": True},
                    GRAPHS, StubBackend(), self.out, PBS)
        notify.notify(self.out, bridge, send=True)
        self.approve(bridge)
        self.assertTrue(any(p.startswith("[kimeru 送信用 #1]") for p in bridge.posts))
        self.assertEqual(self.calls, [])


class TestValidate(unittest.TestCase):
    def test_an_unknown_action_type_is_refused(self):
        g = {"name": "t", "event": "x.y", "start": "a", "nodes": {"a": {"kind": "decide", "actions": [{"type": "email.send"}]}}}
        with self.assertRaises(graph.GraphError):
            graph.validate(g)

    def test_the_shipped_graphs_pass(self):
        graph.load_dir(ROOT / "graphs", PBS)


class TestReplyWord(unittest.TestCase):
    def test_redo_is_a_reply_word(self):
        self.assertEqual(notify.parse_reply("再実行 12"), ("再実行", "12"))
        self.assertEqual(notify.parse_reply("再実行#3。"), ("再実行", "3"))


if __name__ == "__main__":
    unittest.main()
