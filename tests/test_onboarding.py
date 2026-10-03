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


if __name__ == "__main__":
    unittest.main()
