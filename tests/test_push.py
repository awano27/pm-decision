"""Notification routes: counts and numbers only, one state per route, and nothing goes out unless a route is set up.
Every send is mocked: no webhook, mail or Outlook is ever contacted."""
import json
import os
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock

from kimeru import config, daily, notify, push

SECRET_URL = "https://example.invalid/hooks/SECRET-PATH-123?sig=SECRET-SIG"
BODY = "本文-これは同僚のメッセージ"
SENDER = "送信者-ヤマダ"
SUBJECT = "件名-見積り"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name) / "out"
        self.out.mkdir()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = self.dir.name
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        config.apply([])

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def route(self, names, **extra):
        os.environ["KIMERU_PUSH"] = names
        for k, v in extra.items():
            os.environ[k] = v
        config.apply([])

    def add_items(self, posted=(), unposted=()):
        """Approvals with texts that must never leave the PC."""
        ap = notify.Approvals(self.out)
        for i in list(posted) + list(unposted):
            rec = {"graph": "g", "event_id": str(i), "node": "n", "summary": BODY, "advice": SUBJECT + SENDER, "actions": []}
            n = ap.add(f"g:{i}:n", rec)
            if i in posted:
                ap.data["items"][str(n)]["posted"] = True
        ap.save()


class Recorder:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, route, text):
        self.calls.append((route, text))
        if route in self.fail:
            raise push.PushError("HTTP 500")


class TestNothingByDefault(Base):
    def test_no_route_means_nothing_is_sent(self):
        self.add_items(posted=[1])
        rec = Recorder()
        self.assertEqual(push.run(self.out, sender=rec), {})
        self.assertEqual(rec.calls, [])

    def test_the_default_cycle_touches_no_url(self):
        with mock.patch.object(urllib.request, "urlopen") as u, mock.patch("subprocess.run") as sp:
            self.add_items(posted=[1])
            push.run(self.out)
        u.assert_not_called()
        sp.assert_not_called()


class TestContent(Base):
    def test_only_counts_and_numbers_leave(self):
        self.route("webhook,teams_webhook")
        self.add_items(posted=[1, 2], unposted=[3])
        rec = Recorder()
        push.run(self.out, now=1000.0, sender=rec)
        self.assertEqual(len(rec.calls), 2)
        for _, text in rec.calls:
            for secret in (BODY, SENDER, SUBJECT):
                self.assertNotIn(secret, text)
            self.assertIn("確認待ち 2 件", text)
            self.assertIn("投稿できていない確認待ち 1 件", text)      # counted apart from the posted ones

    def test_detail_mode_does_not_reach_the_routes(self):
        self.route("webhook")
        os.environ["KIMERU_TOAST"] = "detail"
        self.add_items(posted=[1])
        rec = Recorder()
        push.run(self.out, sender=rec)
        self.assertNotIn(BODY, rec.calls[0][1])
        self.assertNotIn(SUBJECT, rec.calls[0][1])

    def test_the_real_payloads_carry_the_line_and_nothing_else(self):
        sent = []

        class R:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0):
            sent.append((req.full_url, req.data.decode("utf-8")))
            return R()

        self.route("teams_webhook,webhook", KIMERU_PUSH_TEAMS_URL=SECRET_URL, KIMERU_PUSH_WEBHOOK_URL=SECRET_URL + "2")
        self.add_items(posted=[1])
        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            res = push.run(self.out, now=1000.0)
        self.assertEqual(res, {"teams_webhook": "sent", "webhook": "sent"})
        card = json.loads(sent[0][1])
        self.assertEqual(card["type"], "message")
        self.assertEqual(card["attachments"][0]["contentType"], "application/vnd.microsoft.card.adaptive")
        self.assertIn("確認待ち 1 件", card["attachments"][0]["content"]["body"][0]["text"])
        self.assertIn("確認待ち 1 件", json.loads(sent[1][1])["text"])

    def test_the_webhook_body_template_is_used_and_checked(self):
        sent = []

        class R:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        self.route("webhook", KIMERU_PUSH_WEBHOOK_URL=SECRET_URL,
                   KIMERU_PUSH_WEBHOOK_BODY='{"channel": "me", "message": "{text}"}')
        self.add_items(posted=[1])
        with mock.patch.object(urllib.request, "urlopen", lambda req, timeout=0: (sent.append(req.data), R())[1]):
            push.run(self.out, now=1000.0)
        self.assertEqual(json.loads(sent[0])["channel"], "me")
        self.assertIn("確認待ち", json.loads(sent[0])["message"])
        os.environ["KIMERU_PUSH_WEBHOOK_BODY"] = "not json {text}"
        config.apply([])
        self.assertEqual(list(push.test(sender=None).values())[0][:6], "failed")


class TestSecretUrl(Base):
    def test_the_url_is_in_no_record_and_no_error(self):
        self.route("teams_webhook", KIMERU_PUSH_TEAMS_URL=SECRET_URL)
        self.add_items(posted=[1])

        def boom(req, timeout=0):
            raise urllib.error.URLError(OSError(f"cannot reach {SECRET_URL}"))

        with mock.patch.object(urllib.request, "urlopen", boom):
            res = push.run(self.out, now=1000.0)
        self.assertNotIn("SECRET", json.dumps(res))
        state = (self.out / "push_state.json").read_text(encoding="utf-8")
        self.assertNotIn("SECRET", state)
        self.assertNotIn("SECRET", json.dumps(config.report()))
        self.assertNotIn("SECRET", "\n".join(push.status_lines(self.out)))
        for e in (urllib.error.HTTPError(SECRET_URL, 404, "x", {}, None),):
            with mock.patch.object(urllib.request, "urlopen", side_effect=e):
                self.assertNotIn("SECRET", json.dumps(push.test()))

    def test_config_show_says_set_or_not_only(self):
        os.environ["KIMERU_PUSH_TEAMS_URL"] = SECRET_URL
        self.assertTrue(config.secrets_status()["KIMERU_PUSH_TEAMS_URL"])
        self.assertNotIn("SECRET", json.dumps(config.secrets_status()))

    def test_a_url_in_the_config_file_is_refused(self):
        (Path(self.dir.name) / "config.json").write_text(json.dumps({"push_teams_url": SECRET_URL}), encoding="utf-8")
        warnings = config.apply([])
        self.assertTrue(any("secret" in w for w in warnings))
        self.assertNotIn("KIMERU_PUSH_TEAMS_URL", os.environ)


class TestPerRoute(Base):
    def test_one_failing_route_does_not_stop_the_others(self):
        self.route("webhook,teams_webhook,outlook", KIMERU_PUSH_MAIL_TO="me@example.invalid")
        self.add_items(posted=[1])
        rec = Recorder(fail={"webhook"})
        res = push.run(self.out, now=1000.0, sender=rec)
        self.assertTrue(res["webhook"].startswith("failed"))
        self.assertEqual((res["teams_webhook"], res["outlook"]), ("sent", "sent"))

    def test_a_route_that_is_not_set_up_fails_by_itself(self):
        self.route("teams_webhook")        # no URL in the environment
        self.add_items(posted=[1])
        res = push.run(self.out, now=1000.0)
        self.assertIn("KIMERU_PUSH_TEAMS_URL is not set", res["teams_webhook"])

    def test_the_same_item_is_not_announced_twice_and_the_interval_holds(self):
        self.route("webhook", KIMERU_PUSH_MIN_MINUTES="5")
        self.add_items(posted=[1])
        rec = Recorder()
        self.assertEqual(push.run(self.out, now=1000.0, sender=rec), {"webhook": "sent"})
        self.assertEqual(push.run(self.out, now=1100.0, sender=rec), {"webhook": "nothing new"})
        self.add_items(posted=[2])
        self.assertEqual(push.run(self.out, now=1100.0, sender=rec), {"webhook": "waiting for the minimum interval"})
        self.assertEqual(push.run(self.out, now=1400.0, sender=rec), {"webhook": "sent"})     # 5 minutes later
        self.assertEqual(len(rec.calls), 2)
        self.assertNotIn("#1", rec.calls[1][1])          # only the new one

    def test_routes_have_separate_state(self):
        self.route("webhook,outlook", KIMERU_PUSH_MAIL_TO="me@example.invalid")
        self.add_items(posted=[1])
        push.run(self.out, now=1000.0, sender=Recorder(fail={"outlook"}))
        rec = Recorder()
        res = push.run(self.out, now=1000.0 + 400, sender=rec)
        self.assertEqual(res["webhook"], "nothing new")      # already told
        self.assertEqual(res["outlook"], "sent")             # was not, is now


class TestRest(Base):
    def test_repeated_failures_rest_the_route_and_it_resumes(self):
        self.route("webhook", KIMERU_PUSH_MIN_MINUTES="0")
        self.add_items(posted=[1])
        rec = Recorder(fail={"webhook"})
        t = 1000.0
        for _ in range(push.FAILS_BEFORE_REST):
            res = push.run(self.out, now=t, sender=rec)
            t += 10
        self.assertTrue(res["webhook"].startswith("failed"))
        n = len(rec.calls)
        res = push.run(self.out, now=t, sender=rec)
        self.assertTrue(res["webhook"].startswith("resting"))
        self.assertEqual(len(rec.calls), n)                  # not even tried
        self.assertIn("resting", "\n".join(push.status_lines(self.out, now=t)))
        ok = Recorder()
        res = push.run(self.out, now=t + push.REST_BASE + 1, sender=ok)
        self.assertEqual(res["webhook"], "sent")
        self.assertEqual(len(ok.calls), 1)


class TestTestCommand(Base):
    def test_push_test_sends_only_the_fixed_line(self):
        self.route("webhook,teams_webhook")
        rec = Recorder()
        res = push.test(sender=rec)
        self.assertEqual(res, {"webhook": "sent", "teams_webhook": "sent"})
        self.assertEqual({t for _, t in rec.calls}, {"kimeru: 試験の通知です（件数も本文もありません）"})

    def test_without_a_route_it_says_how_to_set_one_up(self):
        self.assertIn("config set push", list(push.test().values())[0])


class TestInTheCycle(Base):
    def cycle(self, toaster=None, send=True):
        from tests.test_daily import FakeTeams
        from kimeru import cli, graph, plan
        pbs = plan.load_playbooks(Path(__file__).resolve().parent.parent / "playbooks")
        gs = {k: v[0] for k, v in graph.load_dir(Path(__file__).resolve().parent.parent / "graphs", pbs).items()}
        from kimeru.backends import StubBackend
        inbox = Path(self.dir.name) / "inbox"
        inbox.mkdir(exist_ok=True)
        return daily.cycle(self.out, inbox, gs, StubBackend(), pbs, cli.process, bridge=FakeTeams(), send=send,
                           toaster=toaster, now=datetime(2026, 9, 28, 9, 0))

    def test_a_failing_route_is_not_a_failed_cycle_and_others_still_get_it(self):
        self.route("webhook,teams_webhook", KIMERU_PUSH_WEBHOOK_URL=SECRET_URL, KIMERU_PUSH_TEAMS_URL=SECRET_URL)
        self.add_items(posted=[1])
        calls = []

        def fake(req, timeout=0):
            calls.append(req.data)
            if len(calls) == 1:
                raise urllib.error.URLError("x")

            class R:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()

        with mock.patch.object(urllib.request, "urlopen", fake):
            r = self.cycle()
        self.assertTrue(r["push"]["webhook"].startswith("failed"))
        self.assertEqual(r["push"]["teams_webhook"], "sent")
        self.assertEqual(len(calls), 2)                      # both were tried; the first failed, the second sent

    def test_no_push_without_send(self):
        self.route("webhook", KIMERU_PUSH_WEBHOOK_URL=SECRET_URL)
        self.add_items(posted=[1])
        with mock.patch.object(urllib.request, "urlopen") as u:
            self.cycle(send=False)
        u.assert_not_called()

    def test_turning_the_toast_off_does_not_mark_items_as_announced(self):
        os.environ["KIMERU_TOAST"] = "0"
        self.add_items(posted=[1])
        shown = []
        self.cycle(toaster=lambda a, b: shown.append((a, b)))
        self.assertEqual(shown, [])
        nums, _ = notify.untoasted(self.out)
        self.assertEqual(nums, [1])                          # still waiting to be announced
        os.environ["KIMERU_TOAST"] = "1"
        self.cycle(toaster=lambda a, b: shown.append((a, b)))
        self.assertEqual(len(shown), 1)


if __name__ == "__main__":
    unittest.main()
