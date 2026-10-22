"""ADO write acknowledgements must identify the comment that was created."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
from pathlib import Path
from unittest import mock

from kimeru import cli, config, execute, notify, pull
from tests.test_execute import GRAPHS, PBS, WI, Base, Bridge, NoCriteria, SECRET_TOKEN, TENANT


class TestAdoAcknowledgement(Base):
    def ready(self):
        self.enable()
        cli.process(WI, GRAPHS, NoCriteria(), self.out, PBS)
        bridge = Bridge()
        notify.notify(self.out, bridge, send=True, real=True)
        return bridge

    def cycle(self, bridge, http=None, reply=None):
        if reply:
            bridge.replies.append(reply)
        with mock.patch.object(pull, "http_json", http or self.http()), \
                mock.patch.object(execute, "_token", lambda org: SECRET_TOKEN), \
                mock.patch.object(pull, "ado_tenant", lambda org: TENANT):
            return notify.collect(self.out, bridge, real=True, send=True)

    def state(self):
        return json.loads((self.out / "approvals.json").read_text(encoding="utf-8"))["items"]["1"]

    def response_http(self, response, comments=None):
        def fake(method, url, token, body=None, retries=3):
            self.calls.append((method, url, token, body, retries))
            if method == "GET":
                return {"comments": [{"id": 4, "text": t} for t in (comments or [])]}
            return response
        return fake

    def test_missing_or_invalid_acknowledgement_is_unknown_and_never_auto_reposts(self):
        malformed = [
            {}, None, [], "not an object", {"id": 0}, {"id": True},
            {"id": "9"}, {"id": 1.0}, {"id": 2**31}, {"id": 3, "commentId": 4},
            {"id": 3, "workItemId": "7002"}, {"id": 3, "workItemId": 7003},
            {"id": 3, "workItemId": True}, {"id": 3, "workItemId": 7002.0},
            {"id": 0, "private": "raw-ado-response-must-not-leak"},
        ]
        for i, response in enumerate(malformed):
            with self.subTest(response=response):
                self.out = Path(self.dir.name) / f"case{i}"
                self.calls.clear()
                bridge = self.ready()
                http = self.response_http(response)
                self.cycle(bridge, http=http, reply="OK 1")
                self.assertEqual(self.state()["exec"]["0"]["state"], "unknown")
                result_posts = [p for p in bridge.posts if p.startswith("[kimeru 実行 #")]
                self.assertTrue(result_posts)
                self.assertIn("書けたかどうか分かりません", result_posts[-1])
                self.assertNotIn("実行しました", result_posts[-1])
                saved = (self.out / "approvals.json").read_text(encoding="utf-8") \
                    + (self.out / "executions.jsonl").read_text(encoding="utf-8") \
                    + "\n".join(bridge.posts)
                self.assertNotIn("raw-ado-response-must-not-leak", saved)
                self.cycle(bridge, http=http)
                posts = [c for c in self.calls if c[0] == "POST"]
                self.assertEqual(len(posts), 1)
                self.assertEqual(posts[0][4], 1)

    def test_valid_id_aliases_and_consistent_pair_complete(self):
        for i, response in enumerate(({"id": 10}, {"commentId": 11}, {"id": 12, "commentId": 12, "workItemId": 7002})):
            with self.subTest(response=response):
                self.out = Path(self.dir.name) / f"valid{i}"
                bridge = self.ready()
                self.cycle(bridge, http=self.response_http(response), reply="OK 1")
                self.assertEqual(self.state()["exec"]["0"]["state"], "done")
                result_posts = [p for p in bridge.posts if p.startswith("[kimeru 実行 #")]
                self.assertTrue(any("実行しました" in p for p in result_posts))

    def test_explicit_retry_reads_matching_comment_without_another_post(self):
        bridge = self.ready()
        initial = self.response_http({})
        self.cycle(bridge, http=initial, reply="OK 1")
        text = self.state()["record"]["actions"][0]["exec_text"]
        self.calls.clear()
        retry = self.response_http({"id": 44}, comments=[text])
        self.cycle(bridge, http=retry, reply="再実行 1")
        self.assertEqual([c[0] for c in self.calls], ["GET"])
        self.assertEqual(self.state()["exec"]["0"]["state"], "done")

    def test_pre_send_refusal_remains_failed(self):
        bridge = self.ready()
        def refuse(org):
            raise execute._Refused("blocked before send")
        with mock.patch.object(execute, "_token", refuse), \
                mock.patch.object(pull, "http_json", self.response_http({"id": 20})), \
                mock.patch.object(pull, "ado_tenant", lambda org: TENANT):
            bridge.replies.append("OK 1")
            notify.collect(self.out, bridge, real=True, send=True)
        self.assertEqual(self.state()["exec"]["0"]["state"], "failed")
        self.assertEqual([c[0] for c in self.calls], [])
