"""ado.update: the approved priority change of a work item, carried out only at the revision the approval post showed. Every call to
ADO goes to an in-memory fake: nothing leaves the test."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
from pathlib import Path

from kimeru import events, execute, notify, pull, work
from tests.test_execute import SECRET_TOKEN, Base, Bridge

PRIORITY = execute.PRIORITY
ORIGIN = {"org": "contoso-not-real", "project": "Proj"}


def record(fields=None, rev=7, priority=3, origin=True):
    ev = {"kind": "ado.workitem.created", "id": "7002", "title": "決済で 500", "priority": priority,
          **({"rev": rev} if rev is not None else {}), "origin": ORIGIN if origin else None}
    return {"graph": "workitem-intake", "event_kind": "ado.workitem.created", "event_id": "7002", "node": "critical_bug",
            "outcome": "advise", "needs_human": True, "advice": "重大なバグの可能性", "event": ev,
            "actions": [{"type": "ado.update", "id": "7002", "fields": {PRIORITY: 1} if fields is None else fields}]}


class FakeAdo:
    """One work item. GET answers its revision and priority; PATCH applies a JSON Patch that tests the revision first."""

    def __init__(self, rev=7, priority=3, get_fails=False, patch_raises=None, applied_before_raise=False, answer=None):
        self.rev, self.priority, self.calls = rev, priority, []
        self.get_fails, self.patch_raises, self.applied_before_raise, self.answer = get_fails, patch_raises, applied_before_raise, answer

    def __call__(self, method, url, token, body=None, retries=3, content_type="application/json"):
        self.calls.append({"method": method, "url": url, "token": token, "body": body, "retries": retries, "content_type": content_type})
        if method == "GET":
            if self.get_fails:
                raise pull.PullError("HTTP 401 for https://dev.azure.com/x (token expired? run `az login`)")
            return {"id": 7002, "rev": self.rev, "fields": {PRIORITY: self.priority}}
        assert method == "PATCH", method
        test, add = body
        if test != {"op": "test", "path": "/rev", "value": self.rev}:
            raise pull.PullError("HTTP 400 for https://dev.azure.com/x")   # ADO refuses the whole patch: nothing is written
        if self.patch_raises and not self.applied_before_raise:
            raise self.patch_raises
        self.priority, self.rev = add["value"], self.rev + 1
        if self.patch_raises:
            raise self.patch_raises
        return self.answer if self.answer is not None else {"id": 7002, "rev": self.rev, "fields": {PRIORITY: self.priority}}

    def methods(self):
        return [c["method"] for c in self.calls]


class UpdateBase(Base):
    def post(self, rec=None, enable=True, kinds="ado.update"):
        if enable:
            self.enable(KIMERU_EXECUTE=kinds)
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "queue.jsonl").write_text(json.dumps(rec or record(), ensure_ascii=False) + "\n", encoding="utf-8")
        bridge = Bridge()
        notify.notify(self.out, bridge, send=True, real=True)
        return bridge

    def item(self):
        return notify.Approvals(self.out).data["items"]["1"]

    def action(self):
        return self.item()["record"]["actions"][0]

    def state(self):
        return (self.item().get("exec") or {}).get("0", {}).get("state")

    def reply(self, bridge, text, ado):
        bridge.replies.append(text)
        return self.approve_again(bridge, ado)

    def approve_again(self, bridge, ado):
        from unittest import mock
        with mock.patch.object(pull, "http_json", ado), mock.patch.object(execute, "_token", lambda org: SECRET_TOKEN), \
                mock.patch.object(pull, "ado_tenant", lambda org: "t"):
            return notify.collect(self.out, bridge, real=True, send=True)


class TestOffUnlessSwitchedOn(UpdateBase):
    def test_default_records_the_plan_and_calls_nothing(self):
        bridge = self.post(enable=False)
        self.assertNotIn("優先度を", bridge.posts[0])
        ado = FakeAdo()
        self.reply(bridge, "OK 1", ado)
        self.assertEqual(ado.calls, [])
        self.assertEqual(self.item()["status"], "approved")

    def test_switching_on_comments_only_leaves_the_priority_a_plan(self):
        bridge = self.post(kinds="ado.comment")
        ado = FakeAdo()
        self.reply(bridge, "OK 1", ado)
        self.assertEqual(ado.calls, [])


class TestApprovedChange(UpdateBase):
    def test_the_post_shows_the_change_and_ok_writes_it_once_at_that_revision(self):
        bridge = self.post()
        self.assertIn("作業項目 7002（取り込み元の組織 contoso-not-real / プロジェクト Proj）の優先度を 3 → 1 に変更します", bridge.posts[0])
        self.assertIn("判断したときの改訂 7", bridge.posts[0])
        ado = FakeAdo()
        changes = self.reply(bridge, "OK 1", ado)
        self.assertEqual([c["status"] for c in changes], ["approved"])
        self.assertEqual(ado.methods(), ["GET", "PATCH"])
        patch = ado.calls[1]
        self.assertEqual(patch["body"], [{"op": "test", "path": "/rev", "value": 7}, {"op": "add", "path": f"/fields/{PRIORITY}", "value": 1}])
        self.assertEqual((patch["content_type"], patch["retries"]), ("application/json-patch+json", 1))
        self.assertTrue(patch["url"].startswith("https://dev.azure.com/contoso-not-real/Proj/_apis/wit/workitems/7002?"))
        self.assertEqual(self.state(), "done")
        self.assertTrue(any("優先度を 3 → 1 に変更（改訂 8）" in p for p in bridge.posts))
        self.approve_again(bridge, ado)   # the same reply read again: nothing more
        self.assertEqual(ado.methods(), ["GET", "PATCH"])
        for name in ("executions.jsonl", "approvals.json"):
            self.assertNotIn(SECRET_TOKEN, (self.out / name).read_text(encoding="utf-8"))

    def test_writing_the_priority_does_not_complete_the_work(self):
        bridge = self.post()
        self.reply(bridge, "OK 1", FakeAdo())
        self.assertEqual([r["state"] for r in work.list_work(self.out)], ["Unknown"])

    def test_a_value_already_there_is_not_written_again(self):
        bridge = self.post()
        ado = FakeAdo(rev=9, priority=1)
        self.reply(bridge, "OK 1", ado)
        self.assertEqual(ado.methods(), ["GET"])
        self.assertEqual(self.state(), "done")
        self.assertTrue(self.item()["exec"]["0"]["result"]["existing"])
        self.assertTrue(any("優先度はすでに 1 だった" in p for p in bridge.posts))

    def test_only_the_priority_is_written(self):
        bridge = self.post(record(fields={PRIORITY: 2, "System.Tags": "kimeru;triaged"}))
        self.assertIn("ほかの項目（System.Tags）は書きません", bridge.posts[0])
        ado = FakeAdo()
        self.reply(bridge, "OK 1", ado)
        self.assertEqual(ado.calls[1]["body"][1:], [{"op": "add", "path": f"/fields/{PRIORITY}", "value": 2}])


class TestReapproval(UpdateBase):
    def test_a_revision_that_moved_writes_nothing_and_asks_again_with_what_ado_has_now(self):
        bridge = self.post()
        ado = FakeAdo(rev=9, priority=2)
        changes = self.reply(bridge, "OK 1", ado)
        self.assertEqual([c["status"] for c in changes], ["approved", "reapproval"])   # the OK itself stays logged as an OK
        self.assertEqual(len(self.item().get("measurement_history") or []), 1)   # the post made again is a new generation
        logged = [json.loads(l)["status"] for l in (self.out / "approvals.log.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(logged[-2:], ["approved", "reapproval"])
        self.assertEqual(ado.methods(), ["GET"])
        self.assertEqual(self.state(), "stale")
        it = self.item()
        self.assertEqual((it["status"], it["posted"]), ("pending", False))
        self.assertTrue(any("改訂 7 → 9" in p and "優先度は変更していません" in p for p in bridge.posts))
        notify.notify(self.out, bridge, send=True, real=True)   # posted again with the revision and value ADO has now
        again = [p for p in bridge.posts if p.startswith("[kimeru #1]")][-1]
        self.assertIn("優先度を 2 → 1 に変更します", again)
        self.assertIn("判断したときの改訂 9", again)
        self.approve_again(bridge, ado)   # the earlier OK sits above the new post: it does not count
        self.assertEqual(ado.methods(), ["GET"])
        self.assertEqual(self.item()["status"], "pending")
        self.reply(bridge, "OK 1", ado)
        self.assertEqual(ado.methods(), ["GET", "GET", "PATCH"])
        self.assertEqual(ado.calls[-1]["body"][0], {"op": "test", "path": "/rev", "value": 9})
        self.assertEqual(self.state(), "done")

    def test_a_change_edited_after_the_post_is_refused_without_calling_ado(self):
        bridge = self.post()
        path = self.out / "approvals.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["items"]["1"]["record"]["actions"][0]["exec_update"]["to"] = 4
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        ado = FakeAdo()
        self.reply(bridge, "OK 1", ado)
        self.assertEqual(ado.calls, [])
        self.assertEqual(self.state(), "failed")
        self.assertTrue(any("記録が一致しない" in p for p in bridge.posts))

    def test_a_target_changed_in_the_settings_is_refused(self):
        bridge = self.post()
        os.environ["KIMERU_ADO_PROJECT"] = "Other"
        from kimeru import config
        config.apply([])
        ado = FakeAdo()
        self.reply(bridge, "OK 1", ado)
        self.assertEqual(ado.calls, [])
        self.assertEqual(self.state(), "failed")


class TestUnknownAndFailure(UpdateBase):
    def test_an_unanswered_write_is_unknown_never_resent_and_a_redo_reads_first(self):
        bridge = self.post()
        ado = FakeAdo(patch_raises=TimeoutError("timed out"), applied_before_raise=True)
        self.reply(bridge, "OK 1", ado)
        self.assertEqual(self.state(), "unknown")
        self.assertTrue(any("反映されたかどうか分かりません" in p for p in bridge.posts))
        self.approve_again(bridge, ado)   # the next cycle: nothing again by itself
        self.assertEqual(ado.methods(), ["GET", "PATCH"])
        ado.patch_raises = None
        self.reply(bridge, "再実行 1", ado)
        self.assertEqual(ado.methods(), ["GET", "PATCH", "GET"])   # it had been written: read, not written twice
        self.assertEqual(self.state(), "done")

    def test_a_redo_after_an_unknown_that_was_not_written_writes_once(self):
        bridge = self.post()
        ado = FakeAdo(patch_raises=TimeoutError("timed out"), applied_before_raise=False)
        self.reply(bridge, "OK 1", ado)
        ado.patch_raises = None
        self.reply(bridge, "再実行 1", ado)
        self.assertEqual(ado.methods(), ["GET", "PATCH", "GET", "PATCH"])
        self.assertEqual((self.state(), ado.priority), ("done", 1))

    def test_done_reply_closes_an_unknown_result(self):
        bridge = self.post()
        self.reply(bridge, "OK 1", FakeAdo(patch_raises=TimeoutError("timed out")))
        self.reply(bridge, "済 1", FakeAdo())
        self.assertEqual(self.state(), "closed")
        rows = [json.loads(l) for l in (self.out / "executions.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[-1]["type"], "ado.update")
        self.assertEqual(rows[-1]["state"], "closed")

    def test_a_response_without_the_new_revision_and_value_is_unknown(self):
        for answer in ({"id": 7002}, {"id": 7002, "rev": 8, "fields": {PRIORITY: 3}}, {"id": 1, "rev": 8, "fields": {PRIORITY: 1}}, []):
            with self.subTest(answer=answer):
                self.tearDown()
                self.setUp()
                bridge = self.post()
                self.reply(bridge, "OK 1", FakeAdo(answer=answer))
                self.assertEqual(self.state(), "unknown")

    def test_a_failed_read_writes_nothing(self):
        bridge = self.post()
        ado = FakeAdo(get_fails=True)
        self.reply(bridge, "OK 1", ado)
        self.assertEqual(ado.methods(), ["GET"])
        self.assertEqual(self.state(), "failed")

    def test_a_patch_ado_refuses_is_a_sure_failure(self):
        bridge = self.post()
        ado = FakeAdo(patch_raises=pull.PullError("HTTP 400 for https://dev.azure.com/x"))
        self.reply(bridge, "OK 1", ado)
        self.assertEqual(self.state(), "failed")
        self.assertTrue(any("変更していません" in p for p in bridge.posts))


class TestNotCarriedOut(UpdateBase):
    def test_what_kimeru_cannot_write_is_recorded_only(self):
        cases = {"tags only": record(fields={"System.Tags": "kimeru;needs-info"}),
                 "no revision": record(rev=None), "no origin": record(origin=False)}
        for bad in (5, 0, True, "1", 2.0):
            cases[f"priority {bad!r}"] = record(fields={PRIORITY: bad})
        for name, rec in cases.items():
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                bridge = self.post(rec)
                self.assertIn("では、作業項目 7002 に書きません", bridge.posts[0])
                ado = FakeAdo()
                self.reply(bridge, "OK 1", ado)
                self.assertEqual(ado.calls, [])
                self.assertEqual(self.state(), "skipped")
                self.assertEqual(self.item()["status"], "approved")
                told = [p for p in bridge.posts if p.startswith(("[kimeru 実行 #1]", "[kimeru 実行 #1 "))]
                self.assertEqual(len(told), 0 if name == "tags only" else 1)   # a tag-only change is a plan by design: no result post


class TestLabels(UpdateBase):
    def test_reapproval_and_stale_have_their_own_words(self):
        from kimeru import report
        self.assertIn("reapproval", notify.ACK)
        self.assertIn("reapproval", report.LOGGED)
        self.assertNotEqual(report._execution_label({"state": "stale"}), report._execution_label({"state": "unknown"}))


class TestRevisionFromThePull(UpdateBase):
    def test_the_pull_keeps_the_revision_and_the_judge_never_sees_it(self):
        def http(method, url, token, body=None, retries=3):
            if "wiql" in url:
                return {"workItems": [{"id": 7002}]}
            return {"value": [{"id": 7002, "rev": 12, "fields": {"System.Title": "t", PRIORITY: 2}}]}
        inbox = Path(self.dir.name) / "inbox"
        self.assertEqual(pull.pull_ado("contoso-not-real", "Proj", inbox, self.out, http=http, token="t"), 1)
        payload = json.loads(next(inbox.glob("*.json")).read_text(encoding="utf-8"))
        ev = events.normalize(payload)[0]
        self.assertEqual((ev["rev"], ev["priority"]), (12, 2))
        self.assertNotIn("rev", events.state_of(ev))
