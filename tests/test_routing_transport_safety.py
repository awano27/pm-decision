"""Focused regressions for sentence-local outage exclusions and judge redirects."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import http.server
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from kimeru import graph
from kimeru.backends import JevBackend, ReplayBackend
from kimeru import backends


ROOT = Path(__file__).resolve().parent.parent
ALERT = graph.load(ROOT / "graphs" / "monitor_alert.json")


class TestSentenceScopedOutageExclusions(unittest.TestCase):
    def setUp(self):
        self.node = ALERT["nodes"]["critical_outage"]

    def event(self, description):
        return {"kind": "monitor.alert", "id": "synthetic", "rule": "checkout",
                "severity": "Sev2", "condition": "Fired", "description": description}

    def backend(self):
        return ReplayBackend({"resolved": {"noul": 0.05}, "future_risk": {"noul": 0.05},
                              "impact": {"score": 1.0, "confidence": 0.99}})

    def test_current_outage_survives_unrelated_future_sentence_in_either_order(self):
        descriptions = (
            "Customers cannot pay. The cache could grow tomorrow.",
            "The cache could grow tomorrow. Customers cannot pay.",
        )
        for description in descriptions:
            with self.subTest(description=description):
                self.assertEqual(graph.match_eval(self.node, self.event(description))[0], "yes")
                result = graph.run(ALERT, self.event(description), self.backend())
                self.assertEqual(result["node"], "page")
                self.assertEqual([step["node"] for step in result["path"]],
                                 ["resolved", "critical_outage", "plan_incident"])

    def test_japanese_sentences_are_scoped_the_same_way(self):
        description = "全ユーザーが現在ログインできません。来週、別の障害が起きる可能性があります。"
        self.assertEqual(graph.match_eval(self.node, self.event(description))[0], "yes")
        self.assertEqual(graph.run(ALERT, self.event(description), self.backend())["node"], "page")

    def test_future_only_and_healthy_sentences_do_not_page(self):
        descriptions = (
            "The cache could grow tomorrow.",
            "Customers can pay normally. The service is healthy.",
            "全ユーザーは現在正常に利用できます。",
        )
        for description in descriptions:
            with self.subTest(description=description):
                self.assertEqual(graph.match_eval(self.node, self.event(description))[0], "no")
                self.assertEqual(graph.run(ALERT, self.event(description), self.backend())["node"],
                                 "task_internal")

    def test_same_sentence_mixed_signal_still_routes_to_human(self):
        description = "全リージョンで現在障害が発生していますが、明日は別のエラーが発生する可能性があります。"
        self.assertEqual(graph.match_eval(self.node, self.event(description))[0], "mixed")
        self.assertEqual(graph.run(ALERT, self.event(description), self.backend())["node"], "page_advice")

    def test_default_field_scope_retains_existing_veto_and_mixed_semantics(self):
        node = {"fields": ["text"], "patterns": ["outage"], "exclude": ["tomorrow"],
                "mixed_if": ["production"]}
        self.assertEqual(graph.match_eval(node, {"text": "outage. Tomorrow: another risk."})[0], "no")
        self.assertEqual(graph.match_eval(node, {"text": "production outage. Tomorrow: another risk."})[0],
                         "mixed")

    def test_optional_scope_is_schema_validated(self):
        def candidate(scope):
            return {"name": "test", "start": "m", "nodes": {
                "m": {"kind": "match", "fields": ["text"], "patterns": ["hit"],
                      "exclude_scope": scope, "routes": {"yes": "end", "no": "end"}},
                "end": {"kind": "decide", "actions": []},
            }}

        for scope in ("sentence", "field"):
            graph.validate(candidate(scope))
        for scope in ("paragraph", None, 1):
            with self.subTest(scope=scope), self.assertRaises(graph.GraphError):
                graph.validate(candidate(scope))


class _RedirectServer(http.server.HTTPServer):
    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.seen = []


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        if self.path.startswith("http://synthetic.remote/"):
            location = "http://synthetic.remote/target"
        else:
            location = "/target"
        status = getattr(self.server, "response_status", None)
        if status is None:
            status = int(self.path.split("code=")[-1]) if "code=" in self.path else 302
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):
        pass


class TestJudgeRedirectRejection(unittest.TestCase):
    STATUSES = (301, 302, 303, 307, 308)
    TEST_KEY = "synthetic-test-only-key"

    def setUp(self):
        self.server = _RedirectServer(("127.0.0.1", 0), _RedirectHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, url):
        return urllib.request.Request(url, data=b"{}", method="POST",
                                      headers={"Authorization": "Bearer " + self.TEST_KEY})

    def test_loopback_redirect_statuses_never_make_a_second_request(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        for status in self.STATUSES:
            with self.subTest(status=status):
                before = len(self.server.seen)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    backends._urlopen(self.request(f"{base}/source?code={status}"), timeout=3)
                self.assertEqual(caught.exception.code, status)
                seen = self.server.seen[before:]
                self.assertEqual(len(seen), 1)
                self.assertEqual(seen[0][0], f"/source?code={status}")
                self.assertEqual(seen[0][1], "Bearer " + self.TEST_KEY)

    def test_jev_backend_rejects_redirect_without_exposing_key_or_following_it(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        with mock.patch.dict("os.environ", {"SYNTHETIC_JEV_KEY": self.TEST_KEY}):
            backend = JevBackend(key_env="SYNTHETIC_JEV_KEY", api=base, retries=1)
            for status in self.STATUSES:
                with self.subTest(status=status):
                    self.server.response_status = status
                    before = len(self.server.seen)
                    with self.assertRaises(RuntimeError) as caught:
                        backend.ask({}, {"q": {"type": "noul", "instructions": "synthetic"}})
                    self.assertIn(f"HTTP {status}", str(caught.exception))
                    self.assertNotIn(self.TEST_KEY, str(caught.exception))
                    seen = self.server.seen[before:]
                    self.assertEqual(len(seen), 1)
                    self.assertEqual(seen[0][0], "/systemone")
                    self.assertEqual(seen[0][1], "Bearer " + self.TEST_KEY)

    def test_remote_redirect_statuses_use_configured_proxy_and_never_follow(self):
        proxy = f"http://127.0.0.1:{self.server.server_port}"
        proxy_opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy}), backends._NoRedirect())
        with mock.patch.object(backends, "_REMOTE", proxy_opener):
            for status in self.STATUSES:
                with self.subTest(status=status):
                    before = len(self.server.seen)
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        backends._urlopen(self.request(f"http://synthetic.remote/source?code={status}"), timeout=3)
                    self.assertEqual(caught.exception.code, status)
                    seen = self.server.seen[before:]
                    self.assertEqual(len(seen), 1)
                    self.assertTrue(seen[0][0].startswith("http://synthetic.remote/source?code="))
                    self.assertEqual(seen[0][1], "Bearer " + self.TEST_KEY)

    def test_remote_path_uses_protected_opener_not_global_urlopen(self):
        response = object()
        with mock.patch.object(backends._REMOTE, "open", return_value=response) as remote_open, \
                mock.patch.object(backends._DIRECT, "open") as direct_open, \
                mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("unprotected urlopen")):
            result = backends._urlopen(self.request("https://synthetic.remote/judge"), timeout=7)
        self.assertIs(result, response)
        remote_open.assert_called_once()
        self.assertEqual(remote_open.call_args.kwargs["timeout"], 7)
        direct_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
