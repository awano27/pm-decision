"""Jev human handoffs keep the original urgency through real daily notification routing.

The graphs, queue, and notification policies are real; judgment, Teams, toast, and
push are local fakes. All settings and state are isolated from the user's machine.
"""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import unittest
from datetime import datetime

from kimeru import control, daily, notify
from kimeru.backends import BackendUnavailable, StubBackend
from kimeru.cli import process
from tests.test_attention import Cycles, chat, self_row
from tests.test_daily import GRAPHS, PBS


TODAY_REQUEST = "本日の判断をお願いします"
ORDINARY_REQUEST = "この案について判断してください"


class FakeJev(StubBackend):
    NAME = "Jev"

    def __init__(self, level=2, confidence=0.95, unavailable=False):
        self.level = level
        self.confidence = confidence
        self.unavailable = unavailable

    def ask(self, state, questions):
        if "ai_suitability" not in questions:
            return super().ask(state, questions)
        if self.unavailable:
            raise BackendUnavailable("synthetic offline failure")
        return {
            "ai_suitability": {"choice": str(self.level), "confidence": self.confidence},
            "ai_suitability_reason": {
                "choice": "missing_local_context" if self.level <= 3 else "general_knowledge",
                "confidence": 0.95,
            },
        }


class TestSuitabilityAttention(Cycles):
    def setUp(self):
        super().setUp()
        self.backend = FakeJev()
        self.quiet = False

    def cycle(self, h, m=0):
        return daily.cycle(self.out, self.inbox, GRAPHS, self.backend, PBS, process,
                           bridge=self.t, send=True, now=datetime(2026, 9, 28, h, m),
                           toaster=self.toaster)

    def morning_sitting_already_announced(self):
        self.sittings()
        self.chat("おはようございます", "8:30")
        self.cycle(8, 35)
        self.chat("これは何ですか", "8:50")
        self.cycle(8, 55)
        self.assertEqual((self.shown, self.sent), ([], []))
        self.cycle(9, 5)
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件（まとめ）", "#1")])
        self.assertEqual(len(self.sent), 1)
        self.shown.clear()
        self.sent.clear()

    def begin_quiet(self):
        os.environ.update(KIMERU_CONTROL_WORDS="1", KIMERU_QUIET_WORDS="1")
        self.t.chat_list = [self_row("メモ"), chat("おはようございます", "9:00")]
        self.cycle(9, 5)
        self.t.chat_list = [self_row("休み 15時"), chat("おはようございます", "9:00")]
        self.cycle(10)
        self.assertEqual(control.quiet_until(self.out), datetime(2026, 9, 28, 15))
        self.assertTrue(any(text.startswith("[kimeru 休み]") for text, _ in self.t.posts))
        self.assertEqual((self.shown, self.sent), ([], []))
        self.quiet = True

    def request(self, text):
        rows = [self_row("休み 15時")] if self.quiet else []
        self.t.chat_list = rows + [chat(text, "10:15", cid="19:decision-request@unq.gbl.spaces")]
        return self.cycle(10, 20)

    def item(self, number):
        return notify.Approvals(self.out).data["items"][str(number)]

    def latest_decision(self):
        return json.loads((self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])

    def assert_pending_only(self, report, number):
        self.assertEqual(report["notices"], 0)
        self.assertIn(number, report["notify"])
        item = self.item(number)
        self.assertEqual(item["status"], "pending")
        self.assertTrue(item["posted"])
        self.assertEqual(item["record"]["outcome"], "advise")
        self.assertEqual(item["record"]["ai_suitability"]["route"], "human")
        self.assertEqual(item["record"]["executed"], [])
        self.assertNotEqual(item.get("work", {}).get("state"), "done")
        self.assertFalse(any(text.startswith("[kimeru 通知]") for text, _ in self.t.posts))
        self.assertFalse((self.out / "executions.jsonl").exists())

    def assert_announced_now(self, report, number):
        self.assert_pending_only(report, number)
        self.assertEqual(report.get("toast"), "shown")
        self.assertEqual(report["push"], {"webhook": "sent"})
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件", f"#{number}")])
        self.assertEqual(self.sent, [("webhook", f"kimeru: 確認待ち 1 件（#{number}）。Teams の自分とのチャットを確認してください")])

    def assert_waiting(self, report, number):
        self.assert_pending_only(report, number)
        self.assertNotIn("toast", report)
        self.assertEqual(report["push"], {"webhook": "waiting for the next sitting"})
        self.assertEqual((self.shown, self.sent), ([], []))

    def test_today_handoff_is_announced_after_the_morning_sitting_was_used(self):
        self.morning_sitting_already_announced()
        report = self.request(TODAY_REQUEST)
        self.assertEqual(self.item(2)["record"]["node"], "decision_today")
        self.assert_announced_now(report, 2)
        self.cycle(10, 25)
        self.assertEqual((len(self.shown), len(self.sent)), (1, 1), "the same case is announced only once")

    def test_today_handoff_is_announced_during_a_quiet_period(self):
        self.begin_quiet()
        report = self.request(TODAY_REQUEST)
        self.assertEqual(self.item(1)["record"]["node"], "decision_today")
        self.assert_announced_now(report, 1)

    def test_low_confidence_handoff_keeps_the_today_notification(self):
        self.morning_sitting_already_announced()
        self.backend = FakeJev(level=5, confidence=0.69)
        report = self.request(TODAY_REQUEST)
        self.assertEqual(self.item(2)["record"]["ai_suitability"]["reason_code"], "low_confidence")
        self.assert_announced_now(report, 2)

    def test_unavailable_assessment_still_announces_the_today_request_during_quiet(self):
        self.begin_quiet()
        self.backend = FakeJev(unavailable=True)
        report = self.request(TODAY_REQUEST)
        self.assertEqual(self.item(1)["record"]["ai_suitability"]["status"], "unavailable")
        self.assert_announced_now(report, 1)

    def test_ordinary_handoff_waits_until_the_next_sitting(self):
        self.morning_sitting_already_announced()
        report = self.request(ORDINARY_REQUEST)
        self.assertEqual(self.item(2)["record"]["node"], "decision_later")
        self.assert_waiting(report, 2)
        self.cycle(13, 5)
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件（まとめ）", "#2")])
        self.assertEqual(len(self.sent), 1)

    def test_ordinary_handoff_waits_until_quiet_ends(self):
        self.begin_quiet()
        report = self.request(ORDINARY_REQUEST)
        self.assertEqual(self.item(1)["record"]["node"], "decision_later")
        self.assert_waiting(report, 1)
        self.cycle(15, 5)
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件", "#1")])
        self.assertEqual(len(self.sent), 1)

    def test_high_suitability_preserves_the_existing_automatic_notice(self):
        self.morning_sitting_already_announced()
        self.backend = FakeJev(level=5)
        report = self.request(TODAY_REQUEST)
        result = self.latest_decision()
        self.assertEqual(result["node"], "decision_today")
        self.assertEqual(result["ai_suitability"]["route"], "ai")
        self.assertEqual(report["notices"], 1)
        self.assertFalse(result["needs_human"])
        self.assertEqual(self.shown, [("kimeru: 自動決定の通知 1 件", "Teams の自分とのチャットを確認してください")])
        self.assertEqual(report["push"], {"webhook": "sent"})
        self.assertEqual(len(self.sent), 1)

    def test_non_jev_keeps_the_original_notice_path(self):
        self.morning_sitting_already_announced()
        self.backend = StubBackend()
        report = self.request(TODAY_REQUEST)
        result = self.latest_decision()
        self.assertEqual(result["node"], "decision_today")
        self.assertNotIn("ai_suitability", result)
        self.assertEqual(report["notices"], 1)
        self.assertFalse(result["needs_human"])
        self.assertEqual(self.shown, [("kimeru: 自動決定の通知 1 件", "Teams の自分とのチャットを確認してください")])
        self.assertEqual(report["push"], {"webhook": "sent"})

    def test_critical_incident_handoff_still_announces_during_quiet(self):
        self.begin_quiet()
        report = self.request("本番が落ちています")
        self.assertEqual(self.item(1)["record"]["node"], "incident_bug")
        self.assert_announced_now(report, 1)


if __name__ == "__main__":
    unittest.main()
