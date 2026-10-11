"""R17: the PC notification and the phone push wait for the next sitting (interrupt=sittings, sit_hours), and only an urgent
item is announced at once. With the default (interrupt=all) the texts, the records and the cycle are the same as before, which
the golden tests below pin. Every route is a fake: no Teams, no toast, no webhook is ever reached."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from pathlib import Path
from unittest import mock

from kimeru import attention, cli, config, daily, notify, push
from kimeru.backends import StubBackend
from kimeru.cli import process
from tests.test_daily import GRAPHS, PBS, FakeTeams

ROOT = Path(__file__).resolve().parent.parent
BODY = "本文-マーカーXYZ"


def chat(preview, time, cid="19:a_b@unq.gbl.spaces"):
    return {"id": cid, "kind": "oneOnOne", "title": "Sato", "preview": preview, "time": time, "unread": True, "mention": False}


class TestGoldenDefaults(unittest.TestCase):
    """What the default (interrupt=all) says and records, written before the change: none of it may move."""

    def test_toast_and_push_texts_are_byte_for_byte_the_same_as_before(self):
        out = Path(tempfile.mkdtemp())
        self.assertEqual(notify.toast_text(out, [1], 0, 0), ("kimeru: 確認待ち 1 件", "#1"))
        self.assertEqual(notify.toast_text(out, [1, 2, 3, 4, 5, 6, 7], 2, 1),
                         ("kimeru: 確認待ち 7 件 / 自動決定の通知 2 件 / 投稿できていない確認待ち 1 件", "#1, #2, #3, #4, #5"))
        self.assertEqual(notify.toast_text(out, [4], 0, 1), ("kimeru: 確認待ち 1 件 / 投稿できていない確認待ち 1 件", "#4"))
        self.assertEqual(push.message([3, 4], 2, 1),
                         "kimeru: 確認待ち 2 件（#3, #4） / 自動決定の通知 2 件 / 投稿できていない確認待ち 1 件。Teams の自分とのチャットを確認してください")
        self.assertEqual(push.message([1, 2, 3, 4, 5, 6], 0, 0),
                         "kimeru: 確認待ち 6 件（#1, #2, #3, #4, #5…）。Teams の自分とのチャットを確認してください")
        self.assertEqual(push.message([], 1, 0), "kimeru: 自動決定の通知 1 件。Teams の自分とのチャットを確認してください")

    def test_a_default_cycle_announces_at_once_and_records_no_urgency(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"KIMERU_PUSH": "webhook", "KIMERU_PUSH_MIN_MINUTES": "0",
                                                                             "KIMERU_PUSH_WEBHOOK_URL": "https://example.invalid/h"}), \
                mock.patch("kimeru.push._sender", side_effect=lambda route: (lambda text: sent.append((route, text)))):
            sent, shown = [], []
            out, inbox, t = Path(d) / "out", Path(d) / "inbox", FakeTeams()
            run = lambda now: daily.cycle(out, inbox, GRAPHS, StubBackend(), PBS, process, bridge=t, send=True, now=now,
                                          toaster=lambda a, b: shown.append((a, b)) or "shown")
            t.chat_list = [chat("おはようございます", "7:00")]
            run(datetime(2026, 9, 28, 7, 0))
            t.chat_list = [chat("これは何ですか", "10:15")]
            r = run(datetime(2026, 9, 28, 10, 20))
            self.assertEqual(r["toast"], "shown")
            self.assertEqual(r["push"], {"webhook": "sent"})
            self.assertEqual(shown, [("kimeru: 確認待ち 1 件", "#1")])
            self.assertEqual(sent, [("webhook", "kimeru: 確認待ち 1 件（#1）。Teams の自分とのチャットを確認してください")])
            self.assertNotIn("attn", (out / "decisions.jsonl").read_text(encoding="utf-8"))


class TestUrgency(unittest.TestCase):
    """attention.judge: what makes an item urgent, and nothing else does."""

    def test_a_severe_monitor_alert_is_urgent_by_its_severity(self):
        g = GRAPHS["monitor.alert"][0]
        ev = {"kind": "monitor.alert", "id": "a1", "severity": "Sev1", "condition": "Fired", "description": "x"}
        res = {"node": "page", "path": [], "notify": True, "event_kind": "monitor.alert"}
        self.assertEqual(attention.judge(g, res, ev)["urgent"], True)
        self.assertIn("severe", attention.judge(g, res, ev)["why"])

    def test_a_work_item_at_priority_one_is_urgent_even_when_it_went_to_triage_pm(self):
        g = GRAPHS["ado.workitem.created"][0]
        ev = {"kind": "ado.workitem.created", "id": "4812", "priority": "1", "title": "x"}
        res = {"node": "triage_pm", "path": [{"node": "critical_bug", "answer": {}, "edge": "no"}], "notify": False}
        self.assertTrue(attention.judge(g, res, ev)["urgent"])
        ev2 = {**ev, "priority": "2"}
        self.assertFalse(attention.judge(g, {**res, "node": "set_p2"}, ev2)["urgent"])

    def test_a_critical_rule_that_said_yes_or_mixed_is_urgent_and_one_that_said_no_is_not(self):
        g = GRAPHS["teams.chat"][0]
        ev = {"kind": "teams.chat", "id": "m", "text": "本番が落ちています", "author": "A"}
        for edge in ("yes", "mixed"):
            res = {"node": "ask_pm", "path": [{"node": "critical_incident", "answer": {}, "edge": edge}], "notify": False}
            self.assertTrue(attention.judge(g, res, ev)["urgent"], edge)
        res = {"node": "ask_pm", "path": [{"node": "critical_incident", "answer": {}, "edge": "no"}], "notify": False}
        self.assertFalse(attention.judge(g, res, {**ev, "text": "これは何ですか"})["urgent"])

    def test_a_decision_that_asks_for_notice_and_an_alert_are_urgent(self):
        g = GRAPHS["teams.chat"][0]
        ev = {"kind": "teams.chat", "id": "m", "text": "本日の判断をお願いします", "author": "A"}
        self.assertTrue(attention.judge(g, {"node": "decision_today", "path": [], "notify": True}, ev)["urgent"])
        g2 = GRAPHS["monitor.alert"][0]
        self.assertTrue(attention.judge(g2, {"node": "log_resolved", "path": [], "notify": False},
                                        {"kind": "monitor.alert", "id": "a", "condition": "Resolved"})["urgent"])

    def test_the_urgent_words_are_found_in_the_whole_text_whatever_the_width_and_case(self):
        g = GRAPHS["teams.chat"][0]
        res = {"node": "ask_pm", "path": [], "notify": False}
        for text in ("至急お願いします", "ＡＳＡＰで頼みます", "asap please", "本日中に返事を", "今日中まで", "緊急の件です"):
            self.assertTrue(attention.judge(g, res, {"kind": "teams.chat", "id": "m", "text": text, "author": "A"})["urgent"], text)
        # the word is in a later field of the event too (the whole text, not only the preview)
        self.assertTrue(attention.judge(g, res, {"kind": "teams.chat", "id": "m", "text": "確認お願いします",
                                                 "thread": ["至急で見てください"], "author": "A"})["urgent"])
        self.assertFalse(attention.judge(g, res, {"kind": "teams.chat", "id": "m", "text": "これは何ですか", "author": "A"})["urgent"])

    def test_has_ac_ack_only_and_label_hits_are_never_counted_as_urgent(self):
        for g_name, node in (("ado.workitem.created", "has_ac"), ("teams.chat", "ack_only"), ("meeting.item", "label_decision")):
            g = GRAPHS[g_name][0]
            res = {"node": node, "path": [{"node": node, "answer": {}, "edge": "yes"}], "notify": False}
            ev = {"kind": g_name, "id": "x", "text": "ありがとうございます", "title": "了解"}
            self.assertFalse(attention.judge(g, res, ev)["urgent"], node)

    def test_an_ordinary_question_is_not_urgent(self):
        g = GRAPHS["teams.chat"][0]
        ev = {"kind": "teams.chat", "id": "m", "text": "これは何ですか", "author": "A"}
        self.assertEqual(attention.judge(g, {"node": "ask_pm", "path": [], "notify": False}, ev), {"urgent": False, "why": []})

    def test_the_reasons_name_the_rule_and_never_the_text(self):
        g = GRAPHS["teams.chat"][0]
        ev = {"kind": "teams.chat", "id": "m", "text": f"{BODY} 至急", "author": "A"}
        r = attention.judge(g, {"node": "ask_pm", "path": [], "notify": False}, ev)
        self.assertEqual(r["why"], ["words"])
        self.assertNotIn(BODY, json.dumps(r, ensure_ascii=False))

    def test_a_pattern_that_is_not_valid_falls_back_to_the_default_words_with_a_warning(self):
        g = GRAPHS["teams.chat"][0]
        ev = {"kind": "teams.chat", "id": "m", "text": "至急です", "author": "A"}
        res = {"node": "ask_pm", "path": [], "notify": False}
        with mock.patch.dict(os.environ, {"KIMERU_URGENT_WORDS": "(至急"}):
            self.assertTrue(attention.judge(g, res, ev)["urgent"])
            self.assertTrue(any("urgent_words" in w for w in config.apply([])))
        config.apply([])

    def test_a_record_without_its_urgency_is_announced_at_once(self):
        self.assertTrue(attention.announce_now({"posted": True, "record": {}}))
        self.assertTrue(attention.announce_now({"posted": True, "record": {"attn": {"urgent": "yes"}}}))
        self.assertFalse(attention.announce_now({"posted": True, "record": {"attn": {"urgent": False, "why": []}}}))

    def test_a_follow_up_or_an_item_that_came_back_is_announced_at_once(self):
        calm = {"attn": {"urgent": False, "why": []}}
        self.assertTrue(attention.announce_now({"revision": 2, "record": {**calm, "followups": [{"text": "x"}]}}))
        self.assertFalse(attention.announce_now({"revision": 1, "record": {**calm, "followups": [{"text": "x"}]}}))
        self.assertFalse(attention.announce_now({"revision": 2, "record": calm}))
        self.assertTrue(attention.announce_now({"hold_round": 1, "record": calm}))


class TestSittings(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"KIMERU_INTERRUPT": "sittings", "KIMERU_SIT_HOURS": "9,13,17"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_the_sitting_is_the_last_one_at_or_before_now(self):
        at = lambda h, m=0: attention.boundary(datetime(2026, 9, 28, h, m))
        self.assertEqual(at(9, 5), "2026-09-28T09:00")
        self.assertEqual(at(9), "2026-09-28T09:00")
        self.assertEqual(at(10, 20), "2026-09-28T09:00")
        self.assertEqual(at(13, 5), "2026-09-28T13:00")
        self.assertEqual(at(23, 59), "2026-09-28T17:00")
        self.assertEqual(at(0, 30), "2026-09-27T17:00")          # before the first sitting: yesterday's last one

    def test_the_next_sitting_is_named_for_the_status_line(self):
        self.assertEqual(attention.status_lines(Path(tempfile.mkdtemp()), now=datetime(2026, 9, 28, 13, 5)),
                         ["次のまとめ 17:00・待っている告知 0"])
        self.assertEqual(attention.status_lines(Path(tempfile.mkdtemp()), now=datetime(2026, 9, 28, 18, 0)),
                         ["次のまとめ 明日 09:00・待っている告知 0"])

    def test_a_sit_hours_value_that_is_not_hours_means_no_sittings_at_all(self):
        for bad in ("25", "9,abc", "-1", "9,25"):
            with mock.patch.dict(os.environ, {"KIMERU_SIT_HOURS": bad}):
                self.assertIsNone(attention.boundary(datetime(2026, 9, 28, 10, 20)), bad)
                self.assertEqual(attention.status_lines(Path(tempfile.mkdtemp()), now=datetime(2026, 9, 28, 10, 20)), [], bad)
                self.assertTrue(any("sit_hours" in w for w in config.apply([])), bad)
        config.apply([])

    def test_interrupt_all_has_no_sitting_and_holds_nothing(self):
        with mock.patch.dict(os.environ, {"KIMERU_INTERRUPT": "all"}):
            self.assertEqual(attention.plan(Path(tempfile.mkdtemp()), datetime(2026, 9, 28, 10, 20)),
                             {"sitting_at": None, "hold": set()})

    def test_the_config_accepts_the_new_settings_and_refuses_a_bad_sit_hours_on_set(self):
        self.assertEqual(config.SETTINGS["interrupt"], ("KIMERU_INTERRUPT", "all"))
        self.assertEqual(config.SETTINGS["sit_hours"], ("KIMERU_SIT_HOURS", "9,13,17"))
        self.assertEqual(config.SETTINGS["urgent_words"], ("KIMERU_URGENT_WORDS", "至急|本日中|今日中|大至急|緊急|ASAP"))
        self.assertEqual(config.CHOICES["interrupt"], ("all", "sittings"))
        for key in ("interrupt", "sit_hours"):
            self.assertIn(key, config.LOGGABLE)
            self.assertIn(key, config.SHARE_VALUES)
        self.assertNotIn("urgent_words", config.LOGGABLE)
        self.assertNotIn("urgent_words", config.SHARE_VALUES)
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"KIMERU_STATE_DIR": d}):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = cli.main(["config", "set", "sit_hours", "25"])
            self.assertEqual(rc, 2)
            self.assertFalse((Path(d) / "config.json").exists())
            self.assertEqual(config.sit_hours_list("9, 13,17,"), [9, 13, 17])


class Cycles(unittest.TestCase):
    """daily.cycle with a fake Teams, a recording toast and a recording push route. The times are injected."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.out, self.inbox = Path(self.dir.name) / "out", Path(self.dir.name) / "inbox"
        self.t = FakeTeams()
        self.shown, self.sent = [], []
        self.toast_fails, self.push_fails = False, False
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean.update({"KIMERU_STATE_DIR": self.dir.name, "KIMERU_PUSH": "webhook", "KIMERU_PUSH_MIN_MINUTES": "0",
                      "KIMERU_PUSH_WEBHOOK_URL": "https://example.invalid/h"})
        env = mock.patch.dict(os.environ, clean, clear=True)
        env.start()
        self.addCleanup(env.stop)
        sender = mock.patch("kimeru.push._sender", side_effect=lambda route: (lambda text: self.push_send(route, text)))
        sender.start()
        self.addCleanup(sender.stop)

    def push_send(self, route, text):
        if self.push_fails:
            raise push.PushError("HTTP 500")
        self.sent.append((route, text))

    def toaster(self, title, body):
        if self.toast_fails:
            raise RuntimeError("no toast")
        self.shown.append((title, body))
        return "shown"

    def cycle(self, h, m=0, toast=True):
        return daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, process, bridge=self.t, send=True,
                           now=datetime(2026, 9, 28, h, m), toaster=self.toaster if toast else None)

    def chat(self, preview, time):
        self.t.chat_list = [chat(preview, time)]

    def sittings(self):
        os.environ.update({"KIMERU_INTERRUPT": "sittings", "KIMERU_SIT_HOURS": "9,13,17"})

    def open_day(self):
        """The first cycle of the day, before anything is posted: it records the chat list and the sitting it starts in."""
        self.chat("おはようございます", "9:00")
        return self.cycle(9, 5)


class TestSittingCycle(Cycles):
    def test_a_question_posted_between_sittings_is_announced_once_at_the_next_sitting(self):
        self.sittings()
        self.open_day()
        self.assertEqual((self.shown, self.sent), ([], []))
        self.chat(f"これは何ですか {BODY}", "10:15")
        r = self.cycle(10, 20)
        self.assertEqual(r["notify"], [1])                              # posted to the self chat as before
        self.assertEqual(self.shown, [])                                 # no PC notification
        self.assertEqual(self.sent, [])                                  # no phone push
        self.assertEqual(r["push"], {"webhook": "waiting for the next sitting"})
        self.assertIn("待っている告知 1", "\n".join(attention.status_lines(self.out, now=datetime(2026, 9, 28, 10, 25))))
        self.cycle(10, 25)
        self.assertEqual((self.shown, self.sent), ([], []))
        r = self.cycle(13, 5)
        self.assertEqual(r["toast"], "shown")
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件（まとめ）", "#1")])
        self.assertEqual(self.sent, [("webhook", "kimeru: 確認待ち 1 件（#1）。Teams の自分とのチャットを確認してください")])
        self.cycle(13, 10)
        self.cycle(13, 20)
        self.assertEqual(len(self.shown), 1)                              # once
        self.assertEqual(len(self.sent), 1)

    def test_no_text_of_the_message_reaches_the_toast_or_the_push(self):
        self.sittings()
        self.open_day()
        self.chat(f"これは何ですか {BODY}", "10:15")
        self.cycle(10, 20)
        self.cycle(13, 5)
        shown = json.dumps(self.shown + self.sent, ensure_ascii=False)
        self.assertNotIn(BODY, shown)
        self.assertIn("#1", shown)

    def test_an_urgent_word_is_announced_at_once_and_not_held(self):
        self.sittings()
        self.open_day()
        self.chat("至急 これは何ですか", "10:15")
        r = self.cycle(10, 20)
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件", "#1")])
        self.assertEqual(self.sent, [("webhook", "kimeru: 確認待ち 1 件（#1）。Teams の自分とのチャットを確認してください")])
        self.assertEqual(r["push"], {"webhook": "sent"})
        rec = json.loads((self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(rec["attn"], {"urgent": True, "why": ["words"]})

    def test_a_sev1_alert_at_ten_is_announced_at_once(self):
        self.sittings()
        self.open_day()
        self.chat("これは何ですか", "10:15")
        self.cycle(10, 20)
        alert = {"kind": "monitor.alert", "id": "s1", "rule": "prod-db storage", "severity": "Sev1", "condition": "Fired",
                 "description": "Storage 92% used on prod-db"}
        self.inbox.mkdir(parents=True, exist_ok=True)
        (self.inbox / "alert.json").write_text(json.dumps(alert, ensure_ascii=False), encoding="utf-8")
        self.cycle(10, 25)
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件", "#2")])  # the alert (#2) at once; the question (#1) still waits
        self.assertEqual(self.sent, [("webhook", "kimeru: 確認待ち 1 件（#2）。Teams の自分とのチャットを確認してください")])
        rec = json.loads((self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(rec["attn"]["urgent"], True)
        self.assertIn("alert", rec["attn"]["why"])

    def test_a_priority_one_work_item_is_announced_at_once(self):
        self.sittings()
        self.open_day()
        self.inbox.mkdir(parents=True, exist_ok=True)
        item = {"kind": "ado.workitem.created", "id": "4812", "title": "決済が止まっている", "priority": "1",
                "work_item_type": "Bug", "description": "本番で決済が通りません"}
        (self.inbox / "ado.json").write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
        r = self.cycle(10, 20)
        self.assertEqual(r["notices"], 1)                                 # the priority-one item is decided at once: a notice (never held)
        self.assertEqual(self.shown, [("kimeru: 自動決定の通知 1 件", "Teams の自分とのチャットを確認してください")])
        self.assertEqual(self.sent, [("webhook", "kimeru: 自動決定の通知 1 件。Teams の自分とのチャットを確認してください")])
        rec = json.loads((self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(rec["attn"]["urgent"], True)
        self.assertIn("severe", rec["attn"]["why"])

    def test_a_failed_toast_at_one_is_announced_again_at_ten_past(self):
        self.sittings()
        self.open_day()
        self.chat("これは何ですか", "10:15")
        self.cycle(10, 20)
        self.toast_fails = True
        r = self.cycle(13, 5)
        self.assertTrue(r["toast"].startswith("failed:"))
        self.assertEqual(r["push"], {"webhook": "sent"})                 # the phone route is its own
        self.toast_fails = False
        r = self.cycle(13, 10)
        self.assertEqual(r["toast"], "shown")
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件（まとめ）", "#1")])
        self.assertEqual(len(self.sent), 1)                              # the push went once

    def test_a_failed_push_at_one_is_tried_again_at_ten_past(self):
        self.sittings()
        self.open_day()
        self.chat("これは何ですか", "10:15")
        self.cycle(10, 20)
        self.push_fails = True
        r = self.cycle(13, 5)
        self.assertTrue(r["push"]["webhook"].startswith("failed:"))
        self.assertEqual(len(self.shown), 1)
        self.push_fails = False
        r = self.cycle(13, 10)
        self.assertEqual(r["push"], {"webhook": "sent"})
        self.assertEqual(len(self.sent), 1)

    def test_turning_the_toast_off_does_not_use_up_the_sitting(self):
        self.sittings()
        self.open_day()
        self.chat("これは何ですか", "10:15")
        self.cycle(10, 20)
        with mock.patch.dict(os.environ, {"KIMERU_TOAST": "0"}):
            r = self.cycle(13, 5)
        self.assertNotIn("toast", r)
        self.assertEqual(self.shown, [])
        self.assertEqual(r["push"], {"webhook": "sent"})
        r = self.cycle(13, 10)                                           # the PC notification is on again: the 13:00 sitting is still due
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件（まとめ）", "#1")])

    def test_a_sitting_with_nothing_to_tell_is_used_up_so_later_items_wait_for_the_next_one(self):
        self.sittings()
        self.open_day()
        self.cycle(13, 5)                                                 # nothing waits: the 13:00 sitting passes quietly
        self.chat("これは何ですか", "13:30")
        r = self.cycle(13, 35)
        self.assertEqual(self.shown, [])
        self.assertEqual(r["push"], {"webhook": "waiting for the next sitting"})
        self.cycle(17, 5)
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件（まとめ）", "#1")])

    def test_a_record_of_a_later_sitting_does_not_hold_the_waiting_items_forever(self):
        self.sittings()
        self.open_day()
        self.chat("これは何ですか", "10:15")
        self.cycle(10, 20)
        ap = notify.Approvals(self.out)
        ap.data["sitting_toast"] = "2026-09-29T09:00"                    # a clock that went back: the record is not this sitting
        ap.save()
        self.cycle(13, 5)
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件（まとめ）", "#1")])

    def test_the_status_lines_name_the_next_sitting_only_in_sittings_mode(self):
        self.open_day()
        self.assertFalse(any(l.startswith("次のまとめ") for l in daily.status_lines(self.out)))
        self.sittings()
        self.assertTrue(any(l.startswith("次のまとめ ") for l in daily.status_lines(self.out)))

    def test_interrupt_all_announces_at_once_as_before(self):
        self.open_day()
        self.chat("これは何ですか", "10:15")
        r = self.cycle(10, 20)
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件", "#1")])
        self.assertEqual(self.sent, [("webhook", "kimeru: 確認待ち 1 件（#1）。Teams の自分とのチャットを確認してください")])
        self.assertEqual(r["push"], {"webhook": "sent"})
        self.assertNotIn("attn", (self.out / "decisions.jsonl").read_text(encoding="utf-8"))

    def test_the_sittings_record_the_urgency_of_each_decision(self):
        self.sittings()
        self.open_day()
        self.chat("これは何ですか", "10:15")
        self.cycle(10, 20)
        rec = json.loads((self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(rec["attn"], {"urgent": False, "why": []})

    def test_a_route_that_is_turned_on_between_sittings_waits_for_the_next_one(self):
        self.sittings()
        os.environ.pop("KIMERU_PUSH", None)                              # no route at first
        self.open_day()
        self.chat("これは何ですか", "10:15")
        self.cycle(10, 20)                                               # #1 is posted while the route is off: never sent (as before)
        os.environ["KIMERU_PUSH"] = "webhook"
        r = self.cycle(10, 25)                                           # the route starts from here: the sitting of 9:00 is its baseline
        self.assertEqual(self.sent, [])
        self.t.chat_list = [chat("これは何ですか 2件目", "10:28", cid="19:c2@unq.gbl.spaces")]   # another chat: a new item (not a follow-up)
        r = self.cycle(10, 30)
        self.assertEqual(r["push"], {"webhook": "waiting for the next sitting"})
        self.assertEqual(self.sent, [])
        self.cycle(13, 5)
        self.assertEqual(self.sent, [("webhook", "kimeru: 確認待ち 1 件（#2）。Teams の自分とのチャットを確認してください")])


class TestPushHold(unittest.TestCase):
    """push.run(hold=...): the numbers held for a route are not sent and are not recorded as sent."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.out = Path(self.dir.name) / "out"
        self.out.mkdir()
        env = mock.patch.dict(os.environ, {"KIMERU_PUSH": "webhook", "KIMERU_PUSH_MIN_MINUTES": "0",
                                           "KIMERU_STATE_DIR": self.dir.name}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.sent = []
        push.run(self.out, now=0.0, sender=lambda r, t: None)          # the baseline: nothing before this is sent
        ap = notify.Approvals(self.out)
        for i in (1, 2):
            n = ap.add(f"g:{i}:n", {"graph": "g", "event_id": str(i), "node": "n", "summary": BODY, "advice": BODY, "actions": []})
            ap.data["items"][str(n)]["posted"] = True
        ap.save()

    def route_run(self, hold, now):
        return push.run(self.out, now=now, sender=lambda r, t: self.sent.append((r, t)), hold=hold)

    def test_a_held_number_is_not_sent_and_is_sent_once_it_is_released(self):
        self.assertEqual(self.route_run({"webhook": {2}}, 100.0), {"webhook": "sent"})
        self.assertEqual(self.sent, [("webhook", "kimeru: 確認待ち 1 件（#1）。Teams の自分とのチャットを確認してください")])
        self.assertEqual(self.route_run({"webhook": {2}}, 200.0), {"webhook": "waiting for the next sitting"})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.route_run({}, 300.0), {"webhook": "sent"})
        self.assertEqual(self.sent[-1], ("webhook", "kimeru: 確認待ち 1 件（#2）。Teams の自分とのチャットを確認してください"))

    def test_only_counts_and_numbers_leave_for_a_held_run(self):
        self.route_run({"webhook": {1}}, 100.0)
        self.assertNotIn(BODY, json.dumps(self.sent, ensure_ascii=False))
        self.assertNotIn("自分とのチャット", self.sent[0][1].split("。")[0])

    def test_everything_held_waits_and_nothing_is_sent(self):
        self.assertEqual(self.route_run({"webhook": {1, 2}}, 100.0), {"webhook": "waiting for the next sitting"})
        self.assertEqual(self.sent, [])


class TestDocsAndSecurity(unittest.TestCase):
    def test_each_new_setting_has_one_row_in_the_reference_and_the_security_note_names_the_sittings(self):
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        for env in ("KIMERU_INTERRUPT", "KIMERU_SIT_HOURS", "KIMERU_URGENT_WORDS"):
            self.assertEqual(sum(1 for l in ref.splitlines() if l.startswith(f"| `{env}`")), 1, env)
        sec = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("interrupt=sittings", sec)

    def test_the_changelog_names_the_change_under_the_unreleased_heading(self):
        log = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = log.split("## 未リリース", 1)[1].split("\n## ", 1)[0]
        self.assertIn("interrupt=sittings", unreleased)
        self.assertIn("sit_hours", unreleased)


if __name__ == "__main__":
    unittest.main()
