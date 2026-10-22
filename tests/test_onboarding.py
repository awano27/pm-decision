try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import config, onboarding


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name) / "out"
        self.out.mkdir()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = str(Path(self.temp.name) / "state")
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        config.apply([])

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_status_and_dry_run_do_not_write_or_send(self):
        before = set(self.out.iterdir())
        with mock.patch("kimeru.push._sender", side_effect=AssertionError("sent")):
            self.assertEqual(onboarding.dispatch(["status"], self.out), 0)
            self.assertEqual(onboarding.dispatch(["notification-test"], self.out), 0)
        self.assertEqual(set(self.out.iterdir()), before)

    def test_send_uses_fresh_code_only_and_confirmation_is_distinct(self):
        os.environ.update(KIMERU_PUSH="webhook", KIMERU_PUSH_WEBHOOK_URL="https://example.invalid/hook")
        config.apply([])
        sent = []
        with mock.patch("kimeru.push._sender", side_effect=lambda route: lambda text: sent.append((route, text))), \
                mock.patch("kimeru.onboarding.secrets.token_urlsafe", return_value="fresh-token"), \
                mock.patch("kimeru.onboarding.time.time", return_value=1000):
            self.assertEqual(onboarding.dispatch(["notification-test", "--send"], self.out), 0)
            self.assertEqual(onboarding.dispatch(["confirm", "fresh-token"], self.out), 0)
            duplicate = onboarding.dispatch(["confirm", "fresh-token"], self.out)
        self.assertEqual(len(sent), 1)
        self.assertIn("fresh-token", sent[0][1])
        self.assertNotIn("本人", sent[0][1])
        state = json.loads((self.out / "onboarding.json").read_text(encoding="utf-8"))
        self.assertNotIn("fresh-token", json.dumps(state))
        self.assertEqual(state["challenges"][0]["sender_accepted"], True)
        self.assertEqual(state["challenges"][0]["receipt_confirmed"], True)
        self.assertFalse((self.out / "push_state.json").exists())
        self.assertEqual(duplicate, 1)

    def test_absent_route_and_all_failed_are_explicit(self):
        self.assertEqual(onboarding.dispatch(["notification-test", "--send"], self.out), 1)
        os.environ.update(KIMERU_PUSH="webhook", KIMERU_PUSH_WEBHOOK_URL="https://example.invalid/hook")
        config.apply([])
        with mock.patch("kimeru.push._sender", side_effect=RuntimeError("no")):
            self.assertEqual(onboarding.dispatch(["notification-test", "--send"], self.out), 1)

    def test_expired_and_unknown_tokens_fail(self):
        os.environ.update(KIMERU_PUSH="webhook", KIMERU_PUSH_WEBHOOK_URL="https://example.invalid/hook")
        config.apply([])
        with mock.patch("kimeru.push._sender", return_value=lambda text: None), \
                mock.patch("kimeru.onboarding.secrets.token_urlsafe", return_value="old"), \
                mock.patch("kimeru.onboarding.time.time", return_value=0):
            onboarding.dispatch(["notification-test", "--send"], self.out)
        with mock.patch("kimeru.onboarding.time.time", return_value=10**9):
            self.assertEqual(onboarding.dispatch(["confirm", "old"], self.out), 1)
            self.assertEqual(onboarding.dispatch(["confirm", "missing"], self.out), 1)
        with mock.patch("kimeru.push._sender", return_value=lambda text: None), \
                mock.patch("kimeru.onboarding.secrets.token_urlsafe", return_value="future"), \
                mock.patch("kimeru.onboarding.time.time", return_value=2001):
            onboarding.dispatch(["notification-test", "--send"], self.out)
        with mock.patch("kimeru.onboarding.time.time", return_value=2000):
            self.assertEqual(onboarding.dispatch(["confirm", "future"], self.out), 1)


SECRET_URL = "https://example.invalid/hooks/SECRET-PATH-123?sig=SECRET-SIG"
SECRET_KEY = "SECRET-KEY-XYZ"


class DeviceVerificationTests(unittest.TestCase):
    """Route acceptance and a person's receipt on the phone are separate facts; one confirmation names one route."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name) / "out"
        self.out.mkdir()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = str(Path(self.temp.name) / "state")
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        os.environ.update(KIMERU_PUSH="webhook,teams_webhook", KIMERU_PUSH_WEBHOOK_URL=SECRET_URL + "1",
                          KIMERU_PUSH_TEAMS_URL=SECRET_URL + "2", KIMERU_PUSH_WEBHOOK_KEY=SECRET_KEY)
        config.apply([])
        self.sent = []

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_cmd(self, *argv, now=1000):
        lines = []
        code = onboarding.dispatch(list(argv), self.out, print_fn=lines.append, now=now)
        return code, "\n".join(lines)

    def send(self, fail=(), tokens=("code-w", "code-t"), now=1000):
        def make(route):
            def sender(text):
                self.sent.append((route, text))
                if route in fail:
                    raise RuntimeError(f"cannot reach {SECRET_URL}")
            return sender

        with mock.patch("kimeru.push._sender", side_effect=make), \
                mock.patch("kimeru.onboarding.secrets.token_urlsafe", side_effect=list(tokens)):
            return self.run_cmd("notification-test", "--send", now=now)

    def state(self):
        return json.loads((self.out / "onboarding.json").read_text(encoding="utf-8"))

    def raw(self):
        return (self.out / "onboarding.json").read_bytes()

    # --- per-route confirmation ---

    def test_each_route_gets_its_own_code_and_only_hashes_are_stored(self):
        code, _ = self.send()
        self.assertEqual(code, 0)
        texts = dict(self.sent)
        self.assertEqual(texts["webhook"], "kimeru notification test challenge code-w")
        self.assertEqual(texts["teams_webhook"], "kimeru notification test challenge code-t")
        self.assertNotIn("code-", json.dumps(self.state()))

    def test_one_confirmation_is_not_all_routes_received(self):
        self.send()
        code, out = self.run_cmd("confirm", "code-w", "--device", "phone", now=1100)
        self.assertEqual(code, 0)
        self.assertIn("webhook", out)
        routes = self.state()["challenges"][0]["routes"]
        self.assertTrue(routes["webhook"]["receipt_confirmed"])
        self.assertEqual(routes["webhook"]["device"], "phone")
        self.assertFalse(routes["teams_webhook"]["receipt_confirmed"])
        sm = onboarding.summary(self.out)
        self.assertEqual(sm["routes"]["webhook"]["phone_confirmed"], 1)
        self.assertEqual(sm["routes"]["teams_webhook"]["phone_confirmed"], 0)
        self.assertEqual(sm["device_state"], "PARTIAL")      # one of two enabled routes
        report = self.run_cmd("device-check")[1]
        self.assertIn("PARTIAL", report)
        self.assertIn("スマホで本人が確認していない経路: teams_webhook", report)

    def test_both_routes_confirmed_on_the_phone_is_the_only_confirmed_state(self):
        self.send()
        self.run_cmd("confirm", "code-w", "--device", "phone", now=1100)
        self.run_cmd("confirm", "code-t", "--device", "phone", now=1200)
        self.assertEqual(onboarding.summary(self.out)["device_state"], "CONFIRMED_ON_PHONE")

    def test_a_confirmation_not_made_on_the_phone_leaves_the_state_not_run(self):
        self.send()
        for device in ("pc", "other"):
            self.assertEqual(self.run_cmd("confirm", "code-w" if device == "pc" else "code-t", "--device", device, now=1100)[0], 0)
        self.assertEqual(onboarding.summary(self.out)["device_state"], "NOT_RUN")
        routes = self.state()["challenges"][0]["routes"]      # recorded as confirmed, but not as a phone check
        self.assertEqual((routes["webhook"]["device"], routes["teams_webhook"]["device"]), ("pc", "other"))

    def test_send_acceptance_alone_never_leaves_not_run(self):
        self.send()
        self.assertEqual(onboarding.summary(self.out)["accepted"], 1)
        self.assertEqual(onboarding.summary(self.out)["device_state"], "NOT_RUN")
        status = self.run_cmd("status")[1]
        self.assertIn("NOT_RUN", status)
        report = self.run_cmd("device-check")[1]
        self.assertIn("NOT_RUN", report)
        self.assertIn("端末への到達とは別", report)
        self.assertIn("未確認の範囲", report)

    def test_no_record_at_all_is_not_run(self):
        self.assertEqual(self.run_cmd("status")[0], 0)
        self.assertIn("NOT_RUN", self.run_cmd("status")[1])
        code, report = self.run_cmd("device-check")
        self.assertEqual(code, 0)
        self.assertIn("NOT_RUN", report)
        self.assertIn("試験の記録なし", report)
        self.assertFalse((self.out / "onboarding.json").exists())      # reading records writes nothing

    # --- the four failure cases ---

    def test_expired_challenge_is_refused_and_changes_nothing(self):
        self.send(now=1000)
        before = self.raw()
        code, out = self.run_cmd("confirm", "code-w", "--device", "phone", now=1000 + onboarding.CHALLENGE_TTL + 1)
        self.assertEqual(code, 1)
        self.assertIn("期限", out)
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.run_cmd("confirm", "code-w", "--device", "phone", now=1000 + onboarding.CHALLENGE_TTL)[0], 0)  # the edge is still valid
        self.assertEqual(onboarding.summary(self.out)["routes"]["webhook"]["phone_confirmed"], 1)

    def test_a_code_from_the_future_is_refused(self):
        self.send(now=2000)
        self.assertEqual(self.run_cmd("confirm", "code-w", now=1999)[0], 1)

    def test_wrong_challenge_is_refused_and_changes_nothing(self):
        self.send()
        before = self.raw()
        for wrong in ("code-x", "CODE-W", "code-w ", "", "kimeru notification test challenge code-w"):
            self.assertEqual(self.run_cmd("confirm", wrong, "--device", "phone", now=1100)[0], 1, wrong)
        self.assertEqual(self.raw(), before)
        self.assertEqual(onboarding.summary(self.out)["device_state"], "NOT_RUN")

    def test_a_code_of_an_earlier_run_does_not_confirm_a_later_one(self):
        self.send(tokens=("old-w", "old-t"), now=1000)
        self.send(tokens=("new-w", "new-t"), now=1500)
        self.assertEqual(self.run_cmd("confirm", "new-w", "--device", "phone", now=1600)[0], 0)
        challenges = self.state()["challenges"]
        self.assertFalse(challenges[0]["routes"]["webhook"]["receipt_confirmed"])
        self.assertTrue(challenges[1]["routes"]["webhook"]["receipt_confirmed"])

    def test_double_confirmation_is_idempotent_and_cannot_upgrade(self):
        self.send()
        self.assertEqual(self.run_cmd("confirm", "code-w", "--device", "pc", now=1100)[0], 0)
        before = self.raw()
        code, out = self.run_cmd("confirm", "code-w", "--device", "phone", now=1200)
        self.assertEqual(code, 1)
        self.assertIn("すでに確認済み", out)
        self.assertEqual(self.raw(), before)               # no second record, no new time, no device upgrade
        route = self.state()["challenges"][0]["routes"]["webhook"]
        self.assertEqual((route["device"], route["confirmed_at"]), ("pc", 1100))
        self.assertEqual(onboarding.summary(self.out)["device_state"], "NOT_RUN")
        self.assertEqual(len(self.state()["challenges"]), 1)

    def test_a_double_confirmation_after_the_expiry_still_does_not_rewrite(self):
        self.send()
        self.run_cmd("confirm", "code-w", "--device", "phone", now=1100)
        before = self.raw()
        self.assertEqual(self.run_cmd("confirm", "code-w", "--device", "phone", now=10**9)[0], 1)
        self.assertEqual(self.raw(), before)

    def test_send_failure_is_neither_accepted_nor_received(self):
        code, out = self.send(fail=("teams_webhook",))
        self.assertEqual(code, 0)                           # the other route took it
        self.assertIn("teams_webhook: failed (RuntimeError)", out)
        self.assertIn("受理されなかった経路: teams_webhook", out)
        routes = self.state()["challenges"][0]["routes"]
        self.assertFalse(routes["teams_webhook"]["sender_accepted"])
        self.assertTrue(routes["webhook"]["sender_accepted"])
        refused, msg = self.run_cmd("confirm", "code-t", "--device", "phone", now=1100)      # the person cannot confirm what was never accepted
        self.assertEqual(refused, 1)
        self.assertIn("受理していない", msg)
        self.assertFalse(self.state()["challenges"][0]["routes"]["teams_webhook"]["receipt_confirmed"])
        self.assertEqual(onboarding.summary(self.out)["routes"]["teams_webhook"]["accepted"], 0)
        self.assertEqual(onboarding.summary(self.out)["device_state"], "NOT_RUN")

    def test_every_route_failing_is_a_failure_with_no_acceptance(self):
        code, out = self.send(fail=("webhook", "teams_webhook"))
        self.assertEqual(code, 1)
        self.assertIn("送信失敗", out)
        challenge = self.state()["challenges"][0]
        self.assertFalse(challenge["sender_accepted"])
        self.assertFalse(any(e["sender_accepted"] for e in challenge["routes"].values()))
        self.assertEqual(self.run_cmd("confirm", "code-w", now=1100)[0], 1)
        self.assertEqual(onboarding.summary(self.out)["accepted"], 0)

    # --- nothing secret is sent, stored or shown ---

    def test_notification_and_outputs_hold_no_business_text_url_or_key(self):
        outputs = [self.send(fail=("teams_webhook",))[1]]
        outputs.append(self.run_cmd("confirm", "code-w", "--device", "phone", now=1100)[1])
        outputs.append(self.run_cmd("status")[1])
        outputs.append(self.run_cmd("device-check")[1])
        for _, text in self.sent:
            self.assertRegex(text, r"^kimeru notification test challenge [A-Za-z0-9_-]+$")      # the fixed line and the code, nothing else
        shown = "\n".join(outputs) + json.dumps(self.state(), ensure_ascii=False)
        for secret in ("SECRET", SECRET_KEY, "example.invalid", "code-w", "code-t"):
            self.assertNotIn(secret, shown)

    # --- records written before per-route codes ---

    def legacy_file(self, confirmed):
        import hashlib
        rec = {"token_hash": hashlib.sha256(b"legacy-code").hexdigest(), "created_at": 1000, "sender_accepted": True,
               "receipt_confirmed": confirmed, "results": {"webhook": "accepted", "teams_webhook": "accepted"}}
        if confirmed:
            rec["confirmed_at"] = 1050
        (self.out / "onboarding.json").write_text(json.dumps({"challenges": [rec]}), encoding="utf-8")

    def test_an_old_confirmation_is_route_unknown_and_never_a_phone_check(self):
        self.legacy_file(confirmed=True)
        sm = onboarding.summary(self.out)
        self.assertEqual((sm["confirmed"], sm["legacy_confirmed"]), (1, 1))
        self.assertEqual(sm["device_state"], "NOT_RUN")
        for route in ("webhook", "teams_webhook"):
            self.assertNotIn(route, [r for r, v in sm["routes"].items() if v["phone_confirmed"]])
        report = self.run_cmd("device-check")[1]
        self.assertIn("経路不明", report)
        self.assertIn("NOT_RUN", report)
        self.assertEqual(self.run_cmd("status")[0], 0)

    def test_an_old_unconfirmed_record_can_still_be_confirmed_as_route_unknown(self):
        self.legacy_file(confirmed=False)
        code, out = self.run_cmd("confirm", "legacy-code", "--device", "phone", now=1100)
        self.assertEqual(code, 0)
        self.assertIn("経路不明", out)
        self.assertEqual(onboarding.summary(self.out)["device_state"], "NOT_RUN")
        before = self.raw()
        self.assertEqual(self.run_cmd("confirm", "legacy-code", "--device", "phone", now=1200)[0], 1)
        self.assertEqual(self.raw(), before)

    def test_old_and_new_records_can_be_mixed(self):
        self.legacy_file(confirmed=True)
        self.send(now=1000)
        self.assertEqual(self.run_cmd("confirm", "code-w", "--device", "phone", now=1100)[0], 0)
        sm = onboarding.summary(self.out)
        self.assertEqual((sm["challenges"], sm["confirmed"], sm["legacy_confirmed"]), (2, 2, 1))
        self.assertEqual(sm["routes"]["webhook"]["phone_confirmed"], 1)

    def test_a_damaged_record_does_not_break_the_report(self):
        (self.out / "onboarding.json").write_text(json.dumps({"challenges": ["x", None, {"routes": {"webhook": "bad"}}]}),
                                                  encoding="utf-8")
        self.assertEqual(self.run_cmd("device-check")[0], 0)
        self.assertEqual(onboarding.summary(self.out)["device_state"], "NOT_RUN")

    def test_the_command_is_reachable_from_the_cli(self):
        import contextlib
        import io
        from kimeru import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["--out", str(self.out), "onboarding", "device-check"])
        self.assertEqual(code, 0)
        self.assertIn("NOT_RUN", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
