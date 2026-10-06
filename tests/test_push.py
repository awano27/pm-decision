"""Notification routes: counts and numbers only, one state per route, and nothing goes out unless a route is set up.
Every send is mocked: no webhook, mail or Outlook is ever contacted."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

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
        push.run(self.out, now=0.0, sender=lambda r, t: None)      # the first run only records the baseline (sends nothing)

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
        with mock.patch.object(push, "_urlopen") as u, mock.patch("subprocess.run") as sp:
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
        with mock.patch.object(push, "_urlopen", fake_urlopen):
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
        with mock.patch.object(push, "_urlopen", lambda req, timeout=0: (sent.append(req.data), R())[1]):
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

        with mock.patch.object(push, "_urlopen", boom):
            res = push.run(self.out, now=1000.0)
        self.assertNotIn("SECRET", json.dumps(res))
        state = (self.out / "push_state.json").read_text(encoding="utf-8")
        self.assertNotIn("SECRET", state)
        self.assertNotIn("SECRET", json.dumps(config.report()))
        self.assertNotIn("SECRET", "\n".join(push.status_lines(self.out)))
        for e in (urllib.error.HTTPError(SECRET_URL, 404, "x", {}, None),):
            with mock.patch.object(push, "_urlopen", side_effect=e):
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
        self.route("webhook,teams_webhook,outlook")
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
        self.route("webhook,outlook")
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

        with mock.patch.object(push, "_urlopen", fake):
            r = self.cycle()
        self.assertTrue(r["push"]["webhook"].startswith("failed"))
        self.assertEqual(r["push"]["teams_webhook"], "sent")
        self.assertEqual(len(calls), 2)                      # both were tried; the first failed, the second sent

    def test_no_push_without_send(self):
        self.route("webhook", KIMERU_PUSH_WEBHOOK_URL=SECRET_URL)
        self.add_items(posted=[1])
        with mock.patch.object(push, "_urlopen") as u:
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


class Ok:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestEndlessAndBaseline(Base):
    def add_many(self, n):
        ap = notify.Approvals(self.out)
        start = ap.data["next"] + 1000
        for i in range(start, start + n):
            num = ap.add(f"g:{i}:n", {"graph": "g", "event_id": str(i), "node": "n", "summary": "", "advice": "", "actions": []})
            ap.data["items"][str(num)]["posted"] = True
        ap.data.setdefault("notices", []).extend(f"k{start}-{i}" for i in range(n))
        ap.save()

    def test_250_pending_and_250_notices_are_announced_once_then_never_again(self):
        self.route("webhook")
        self.add_many(250)
        rec = Recorder()
        t = 1000.0
        results = []
        for _ in range(6):
            results.append(push.run(self.out, now=t, sender=rec)["webhook"])
            t += 3600
        self.assertEqual(results, ["sent"] + ["nothing new"] * 5)
        self.assertEqual(len(rec.calls), 1)
        self.assertIn("250", rec.calls[0][1])
        self.add_many(3)                                      # only the new ones go out afterwards
        push.run(self.out, now=t, sender=rec)
        self.assertEqual(len(rec.calls), 2)
        self.assertIn("自動決定の通知 3 件", rec.calls[1][1])
        self.assertEqual(push.run(self.out, now=t + 3600, sender=rec), {"webhook": "nothing new"})

    def test_what_was_there_before_the_route_was_turned_on_is_not_sent(self):
        self.add_many(5)
        self.route("webhook")                                # the baseline run happens here
        rec = Recorder()
        self.assertEqual(push.run(self.out, now=1000.0, sender=rec), {"webhook": "nothing new"})
        self.assertEqual(rec.calls, [])
        self.add_items(posted=[1])
        self.assertEqual(push.run(self.out, now=1000.0, sender=rec), {"webhook": "sent"})
        self.assertNotIn("自動決定", rec.calls[0][1])

    def test_the_first_run_itself_sends_nothing(self):
        self.add_many(5)
        os.environ["KIMERU_PUSH"] = "webhook"
        config.apply([])
        rec = Recorder()
        self.assertIn("baseline", push.run(self.out, now=1000.0, sender=rec)["webhook"])
        self.assertEqual(rec.calls, [])


class TestUrlNeverShown(Base):
    BAD = ("hooks.example.invalid/SECRET-PATH-123?sig=SECRET-SIG", "https://example.invalid/SECRET PATH?sig=SECRET-SIG",
           "ftp://example.invalid/SECRET-PATH", "https:///SECRET-PATH", "https://[SECRET-PATH")

    def test_a_malformed_url_shows_no_part_of_itself(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from kimeru import cli
        for bad in self.BAD:
            self.route("teams_webhook,webhook", KIMERU_PUSH_TEAMS_URL=bad, KIMERU_PUSH_WEBHOOK_URL=bad)
            with mock.patch.object(push, "_urlopen") as u:
                res = push.test()
            u.assert_not_called()
            self.assertEqual(set(res), {"teams_webhook", "webhook"})
            self.assertTrue(all(v.startswith("failed") for v in res.values()), res)
            self.assertNotIn("SECRET", json.dumps(res, ensure_ascii=False))
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                cli.main(["--out", str(self.out), "push", "test"])
            self.assertNotIn("SECRET", buf.getvalue())

    def test_an_unexpected_error_text_holding_the_url_is_not_shown(self):
        self.route("teams_webhook", KIMERU_PUSH_TEAMS_URL=SECRET_URL)
        with mock.patch.object(push, "_urlopen", side_effect=RuntimeError(SECRET_URL)):
            self.assertNotIn("SECRET", json.dumps(push.test()))

    def test_http_is_refused_with_a_reason_and_nothing_is_sent(self):
        self.route("webhook,teams_webhook", KIMERU_PUSH_WEBHOOK_URL="http://example.invalid/SECRET", KIMERU_PUSH_TEAMS_URL="http://example.invalid/SECRET")
        self.add_items(posted=[1])
        with mock.patch.object(push, "_urlopen") as u:
            res = push.test()
            res2 = push.run(self.out, now=1000.0)
        u.assert_not_called()
        for r in list(res.values()) + list(res2.values()):
            self.assertIn("https", r)
            self.assertNotIn("SECRET", r)

    def test_push_test_tries_the_other_routes_after_an_unexpected_error(self):
        self.route("teams_webhook,webhook,outlook")
        seen = []

        def sender(route, text):
            seen.append(route)
            if route == "teams_webhook":
                raise RuntimeError(SECRET_URL)

        res = push.test(sender=sender)
        self.assertEqual(seen, ["teams_webhook", "webhook", "outlook"])
        self.assertTrue(res["teams_webhook"].startswith("failed"))
        self.assertNotIn("SECRET", json.dumps(res))
        self.assertEqual((res["webhook"], res["outlook"]), ("sent", "sent"))

    def test_daily_log_holds_no_url(self):
        c = TestInTheCycle("cycle")
        c.dir, c.out = self.dir, self.out
        self.route("webhook,teams_webhook", KIMERU_PUSH_WEBHOOK_URL=SECRET_URL, KIMERU_PUSH_TEAMS_URL="http://example.invalid/SECRET-HTTP")
        self.add_items(posted=[1])
        with mock.patch.object(push, "_urlopen", side_effect=urllib.error.URLError(SECRET_URL)):
            c.cycle()
        log = self.out / "daily.log.jsonl"
        self.assertTrue(log.exists())
        self.assertIn("push", log.read_text(encoding="utf-8"))
        text = "\n".join(p.read_text(encoding="utf-8") for p in self.out.iterdir() if p.is_file())
        self.assertNotIn("SECRET", text)


class TestOutlookRecipient(Base):
    def test_the_recipient_is_not_configurable(self):
        (Path(self.dir.name) / "config.json").write_text(json.dumps({"push_mail_to": "other@example.invalid", "push": "outlook"}), encoding="utf-8")
        os.environ["KIMERU_PUSH_MAIL_TO"] = "other@example.invalid"
        warnings = config.apply([])
        self.assertTrue(any("push_mail_to" in w for w in warnings))     # the old setting is unknown now
        self.assertNotIn("push_mail_to", config.SETTINGS)
        with mock.patch("subprocess.run") as sp:
            sp.return_value = mock.Mock(returncode=0, stdout='{"ok":true,"verified":true}')
            self.assertEqual(push.test(), {"outlook": "sent"})
        args = sp.call_args[0][0]
        self.assertNotIn("-To", args)
        self.assertFalse(any("other@" in str(a) for a in args))
        script = (Path(push.HERE) / "tools" / "outlook-mail.ps1").read_text(encoding="utf-8")
        self.assertNotIn("$To", script)
        self.assertIn("Session.CurrentUser", script)


class TestWebhookKey(Base):
    def capture(self):
        sent = []
        return sent, mock.patch.object(push, "_urlopen", lambda req, timeout=0: (sent.append((req.headers, req.data.decode("utf-8"))), Ok())[1])

    def test_the_key_goes_in_a_header_by_default(self):
        self.route("webhook", KIMERU_PUSH_WEBHOOK_URL=SECRET_URL, KIMERU_PUSH_WEBHOOK_KEY="KEY-123")
        sent, patch = self.capture()
        with patch:
            self.assertEqual(push.test(), {"webhook": "sent"})
        self.assertEqual(sent[0][0].get("Authorization"), "Bearer KEY-123")
        self.assertNotIn("KEY-123", sent[0][1])

    def test_the_key_can_be_placed_in_the_body_and_is_never_shown(self):
        self.route("webhook", KIMERU_PUSH_WEBHOOK_URL=SECRET_URL, KIMERU_PUSH_WEBHOOK_KEY='KEY"123',
                   KIMERU_PUSH_WEBHOOK_BODY='{"k": "{key}", "m": "{text}"}')
        sent, patch = self.capture()
        with patch:
            push.test()
        self.assertEqual(json.loads(sent[0][1])["k"], 'KEY"123')
        self.assertNotIn("Authorization", sent[0][0])
        rows = dict((k, v) for k, v, _ in config.rows())
        self.assertEqual(rows["push_webhook_body"], "(set)")
        self.assertNotIn("123", json.dumps(rows))
        self.assertTrue(config.secrets_status()["KIMERU_PUSH_WEBHOOK_KEY"])
        self.assertNotIn("123", json.dumps(config.secrets_status()))
        self.assertNotIn("123", json.dumps(config.report()))


class TestRedirectIsNotFollowed(Base):
    def test_a_redirect_is_a_failure_and_the_key_is_never_sent_on(self):
        import email.message
        import io
        import urllib.response
        self.route("webhook", KIMERU_PUSH_WEBHOOK_URL=SECRET_URL, KIMERU_PUSH_WEBHOOK_KEY="KEY-123")
        seen = []

        def https_open(handler, req):
            seen.append((req.full_url, dict(req.header_items())))
            h = email.message.Message()
            h["Location"] = "http://other.invalid/elsewhere"
            r = urllib.response.addinfourl(io.BytesIO(b""), h, req.full_url, 302)
            r.msg = "Found"
            return r

        def http_open(handler, req):
            raise AssertionError("the redirect was followed")
        with mock.patch.object(urllib.request.HTTPSHandler, "https_open", https_open), \
                mock.patch.object(urllib.request.HTTPHandler, "http_open", http_open):
            res = push.test()
        self.assertEqual(len(seen), 1)                              # one request, to the address that was set
        self.assertEqual(res, {"webhook": "failed: HTTP 302"})
        self.assertNotIn("SECRET", json.dumps(res))
        self.assertNotIn("other.invalid", json.dumps(res))


class TestPushSettingValue(Base):
    def test_a_url_is_refused_and_not_written_or_shown(self):
        with self.assertRaises(config.ConfigError) as cm:
            config.set_value("push", "https://example.invalid")
        self.assertNotIn("example.invalid", str(cm.exception))
        self.assertFalse(config.path().exists())
        for bad in ("teams_webhook,https://example.invalid/x", "Webhook2", "mail"):
            with self.assertRaises(config.ConfigError):
                config.set_value("push", bad)

    def test_route_names_and_an_empty_value_are_accepted(self):
        config.set_value("push", "teams_webhook, outlook")
        self.assertEqual(json.loads(config.path().read_text(encoding="utf-8"))["push"], "teams_webhook, outlook")
        config.set_value("push", "")

    def test_a_url_in_the_environment_is_never_shown(self):
        os.environ["KIMERU_PUSH"] = SECRET_URL
        warnings = config.apply([])
        self.assertEqual(config.value("push"), "")
        self.assertEqual(push.enabled_routes(), [])
        shown = json.dumps(config.rows()) + json.dumps(config.share_rows()) + json.dumps(config.report()) + json.dumps(warnings)
        self.assertNotIn("SECRET", shown)
        self.assertNotIn("example.invalid", shown)


class TestOutlookOwnAddress(Base):
    def test_a_mail_that_the_script_did_not_verify_counts_as_not_sent(self):
        self.route("outlook")
        with mock.patch("subprocess.run") as sp:
            sp.return_value = mock.Mock(returncode=0, stdout='{"ok":true}')
            self.assertIn("failed", push.test()["outlook"])
            sp.return_value = mock.Mock(returncode=2, stdout='{"ok":false}')
            self.assertIn("failed", push.test()["outlook"])
            sp.return_value = mock.Mock(returncode=0, stdout='{"ok":true,"verified":true}')
            self.assertEqual(push.test(), {"outlook": "sent"})

    def test_the_script_uses_the_address_and_compares_before_it_sends(self):
        script = (Path(push.HERE) / "tools" / "outlook-mail.ps1").read_text(encoding="utf-8")
        self.assertNotIn("Recipients.Add($me.Name)", script)
        self.assertIn("Recipients.Add($mine)", script)
        self.assertIn("resolved address is not your own", script)
        self.assertLess(script.index("resolved address is not your own"), script.index(".Send()"))
        self.assertIn('"verified":true', script)


class TestEnabledMoment(Base):
    def test_what_came_while_all_routes_were_off_is_not_sent_when_they_come_back(self):
        os.environ.update({"KIMERU_PUSH": "webhook", "KIMERU_PUSH_WEBHOOK_URL": SECRET_URL})
        config.apply([])
        self.add_items(posted=[1])
        push.begin_cycle(self.out, now=100.0)
        push.run(self.out, now=100.0, sender=lambda r, t: None)
        os.environ["KIMERU_PUSH"] = ""                        # every route off
        config.apply([])
        self.add_items(posted=[2])
        push.begin_cycle(self.out, now=200.0)                 # a cycle runs while it is off (run() is not called then)
        self.assertEqual(push.status_lines(self.out)[0].startswith("push routes: none"), True)
        os.environ["KIMERU_PUSH"] = "webhook"
        config.apply([])
        self.add_items(posted=[3])                             # also came while it was off
        push.begin_cycle(self.out, now=300.0)                 # first cycle after: baseline
        rec = Recorder()
        self.assertEqual(push.run(self.out, now=1000.0, sender=rec), {"webhook": "nothing new"})
        self.assertEqual(rec.calls, [])

    def test_an_item_posted_in_the_first_cycle_after_turning_it_on_is_sent(self):
        self.add_items(posted=[1])
        os.environ.update({"KIMERU_PUSH": "webhook", "KIMERU_PUSH_WEBHOOK_URL": SECRET_URL})
        config.apply([])
        push.begin_cycle(self.out, now=100.0)                 # the start of the cycle: #1 is old
        self.add_items(posted=[2])                             # this cycle's notify posts #2
        rec = Recorder()
        self.assertEqual(push.run(self.out, now=1000.0, sender=rec), {"webhook": "sent"})
        self.assertIn("#2", rec.calls[0][1])
        self.assertNotIn("#1", rec.calls[0][1])

    def test_a_cycle_with_send_starts_by_recording_the_moment(self):
        src = (Path(push.HERE) / "kimeru" / "daily.py").read_text(encoding="utf-8")
        self.assertLess(src.index("push.begin_cycle(out)"), src.index("pull.pull_teams(inbox"))


if __name__ == "__main__":
    unittest.main()
