try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from kimeru import pull


REDIRECT_CODES = (301, 302, 303, 307, 308)
FAKE_TOKEN = "r4-test-token-only"


class RecordingHandler(BaseHTTPRequestHandler):
    requests = []
    redirect_to = None
    redirect_code = None
    response_body = b'{"ok": true}'

    def log_message(self, format, *args):
        pass

    def _record(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).requests.append((self.command, self.path, dict(self.headers), body))

    def _respond(self):
        self._record()
        if self.redirect_to:
            self.send_response(self.redirect_code)
            self.send_header("Location", self.redirect_to)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.response_body)))
        self.end_headers()
        self.wfile.write(self.response_body)

    do_GET = _respond
    do_POST = _respond


class PullRedirectSafetyTests(unittest.TestCase):
    def start_server(self, *, redirect_to=None, redirect_code=None):
        handler = type("IsolatedRecordingHandler", (RecordingHandler,), {
            "requests": [], "redirect_to": redirect_to, "redirect_code": redirect_code,
        })
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, f"http://127.0.0.1:{server.server_port}", handler

    def stop_server(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "server thread did not stop")

    def test_refuses_every_redirect_for_get_and_post_without_forwarding(self):
        for method in ("GET", "POST"):
            for code in REDIRECT_CODES:
                with self.subTest(method=method, code=code):
                    target, target_thread, target_url, target_handler = self.start_server()
                    source, source_thread, source_url, source_handler = self.start_server(
                        redirect_to=target_url + "/received", redirect_code=code)
                    try:
                        body = {"data": "must stay at source"} if method == "POST" else None
                        with mock.patch.object(pull.time, "sleep") as sleep:
                            with self.assertRaises(pull.PullError) as caught:
                                pull.http_json(method, source_url + "/redirect?secret=query", FAKE_TOKEN, body)
                        self.assertIn(str(code), str(caught.exception))
                        self.assertNotIn("secret", str(caught.exception))
                        self.assertNotIn("query", str(caught.exception))
                        self.assertEqual(len(source_handler.requests), 1, "source must be contacted once")
                        self.assertEqual(len(target_handler.requests), 0, "redirect target must receive nothing")
                        request = source_handler.requests[0]
                        self.assertEqual(request[2].get("Authorization"), "Bearer " + FAKE_TOKEN)
                        self.assertEqual(request[3], json.dumps(body).encode("utf-8") if body is not None else b"")
                        self.assertEqual(sleep.call_count, 0, "redirect must not be retried")
                    finally:
                        self.stop_server(source, source_thread)
                        self.stop_server(target, target_thread)

    def test_normal_json_get_and_post_body(self):
        server, thread, url, handler = self.start_server()
        try:
            self.assertEqual(pull.http_json("GET", url + "/get", FAKE_TOKEN), {"ok": True})
            post_body = {"query": "SELECT 1"}
            self.assertEqual(pull.http_json("POST", url + "/post", FAKE_TOKEN, post_body), {"ok": True})
            self.assertEqual([item[0] for item in handler.requests], ["GET", "POST"])
            self.assertEqual(handler.requests[1][3], json.dumps(post_body).encode("utf-8"))
            self.assertEqual(handler.requests[1][2].get("Authorization"), "Bearer " + FAKE_TOKEN)
        finally:
            self.stop_server(server, thread)

    def test_429_retries_and_closes_error_response(self):
        calls = []
        class RetryHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass
            def do_GET(self):
                calls.append(self.path)
                if len(calls) == 1:
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    self.wfile.write(b"private response body")
                else:
                    payload = b'{"ok": true}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
        server = ThreadingHTTPServer(("127.0.0.1", 0), RetryHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(pull.time, "sleep") as sleep:
                self.assertEqual(pull.http_json("GET", f"http://127.0.0.1:{server.server_port}/retry", FAKE_TOKEN), {"ok": True})
            self.assertEqual(calls, ["/retry", "/retry"])
            sleep.assert_called_once_with(0.0)
        finally:
            self.stop_server(server, thread)


if __name__ == "__main__":
    unittest.main()
