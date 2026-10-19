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
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from kimeru import attention, cli, config, control, daily, notify, push, stats
from kimeru.backends import StubBackend
from kimeru.cli import process
from tests.test_daily import GRAPHS, PBS, FakeTeams

ROOT = Path(__file__).resolve().parent.parent
BODY = "本文-マーカーXYZ"


def chat(preview, time, cid="19:a_b@unq.gbl.spaces", title="Sato"):
    return {"id": cid, "kind": "oneOnOne", "title": title, "preview": preview, "time": time, "unread": True, "mention": False}


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
        unreleased = log
        self.assertIn("interrupt=sittings", unreleased)
        self.assertIn("sit_hours", unreleased)


# ---- R27: 休み <いつ> (quiet_words=1): the PC notification and the push wait until that time; the urgent ones do not ----

QUIET_ACK = "[kimeru 休み] 09/28 15:00 まで、緊急でない件の PC の通知と push を止めます。取り込み・判断・投稿と、重大な件の知らせは続きます"


def self_row(preview):
    return {"id": "48:notes", "kind": "self", "title": "自分", "preview": preview, "time": "8:09", "unread": False, "mention": False}


class CallLog(FakeTeams):
    """FakeTeams that keeps the order of what a cycle asks of Teams (names and arguments)."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def chats(self):
        self.calls.append(("chats",))
        return super().chats()

    def readchat(self, chat_id, count=5, preview=None):
        self.calls.append(("readchat", chat_id, count))
        return super().readchat(chat_id, count, preview)

    def post(self, text, send):
        self.calls.append(("post", text, send))
        return super().post(text, send)

    def read(self):
        self.calls.append(("read",))
        return super().read()


class Bridge:
    """The self chat of control.step: only what is posted to it is recorded."""

    def __init__(self):
        self.posts = []

    def post(self, text, send):
        self.posts.append(text)


class TestQuiet(Cycles):
    """R27 (quiet_words=1): my own self-chat line 休み <いつ> holds what is not urgent until that time (no PC notification, no push).
    The urgent items are announced at once, and the held ones go out once, at the first cycle at or after the time."""

    def setUp(self):
        super().setUp()
        os.environ["KIMERU_QUIET_WORDS"] = "1"

    def lines(self, mine, question=None, time="10:15"):
        self.t.chat_list = [self_row(mine)] + ([chat(question, time)] if question else [])

    def quiet_at_ten(self):
        self.lines("メモ", "おはようございます", "9:00")   # the first sighting of the chat is its baseline
        self.cycle(9, 5)
        self.lines("あなた: 休み 15時", "これは何ですか")
        return self.cycle(10, 20)

    def test_a_quiet_line_posts_one_ack_and_holds_a_question_until_its_time(self):
        r = self.quiet_at_ten()
        self.assertEqual(r["notify"], [1])                          # posted to the self chat as before
        self.assertEqual((self.shown, self.sent), ([], []))         # held: no PC notification and no phone push
        self.assertEqual(r["push"], {"webhook": "waiting for the next sitting"})
        self.cycle(14, 0)
        self.assertEqual((self.shown, self.sent), ([], []))
        self.assertEqual([t for t, _ in self.t.posts if t.startswith("[kimeru 休み]")], [QUIET_ACK])

    def test_at_the_first_cycle_after_the_time_the_held_question_is_announced_once(self):
        self.quiet_at_ten()
        self.cycle(14, 0)
        r = self.cycle(15, 5)
        self.assertEqual(r["toast"], "shown")
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件", "#1")])
        self.assertEqual(self.sent, [("webhook", "kimeru: 確認待ち 1 件（#1）。Teams の自分とのチャットを確認してください")])
        self.cycle(15, 10)
        self.cycle(16, 0)
        self.assertEqual(len(self.shown), 1)                        # once
        self.assertEqual(len(self.sent), 1)

    def test_a_sev1_alert_during_a_quiet_is_announced_at_once_and_the_question_still_waits(self):
        self.quiet_at_ten()
        alert = {"kind": "monitor.alert", "id": "s1", "rule": "prod-db storage", "severity": "Sev1", "condition": "Fired",
                 "description": "Storage 92% used on prod-db"}
        self.inbox.mkdir(parents=True, exist_ok=True)
        (self.inbox / "alert.json").write_text(json.dumps(alert, ensure_ascii=False), encoding="utf-8")
        self.cycle(10, 25)
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件", "#2")])
        self.assertEqual(self.sent, [("webhook", "kimeru: 確認待ち 1 件（#2）。Teams の自分とのチャットを確認してください")])
        rec = json.loads((self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(rec["attn"]["urgent"], True)                # the record names the rule, as with sittings
        self.assertIn("severe", rec["attn"]["why"])
        self.cycle(15, 5)
        self.assertEqual(self.shown[1:], [("kimeru: 確認待ち 1 件", "#1")])

    def test_a_priority_one_work_item_during_a_quiet_is_announced_at_once(self):
        self.quiet_at_ten()
        self.inbox.mkdir(parents=True, exist_ok=True)
        item = {"kind": "ado.workitem.created", "id": "4812", "title": "決済が止まっている", "priority": "1",
                "work_item_type": "Bug", "description": "本番で決済が通りません"}
        (self.inbox / "ado.json").write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
        r = self.cycle(10, 25)
        self.assertEqual(r["notices"], 1)
        self.assertEqual(self.shown, [("kimeru: 自動決定の通知 1 件", "Teams の自分とのチャットを確認してください")])
        self.assertEqual(self.sent, [("webhook", "kimeru: 自動決定の通知 1 件。Teams の自分とのチャットを確認してください")])

    def test_under_sittings_the_held_question_goes_out_at_the_time_as_a_sitting_and_the_one_oclock_sitting_waits(self):
        self.sittings()
        self.lines("メモ", "おはようございます", "9:00")
        self.cycle(9, 5)
        self.lines("休み 15時", "これは何ですか")
        self.cycle(10, 20)
        self.cycle(13, 5)
        self.assertEqual((self.shown, self.sent), ([], []))         # the 13:00 sitting does not announce during the quiet
        self.cycle(15, 5)
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件（まとめ）", "#1")])
        self.assertEqual(self.sent, [("webhook", "kimeru: 確認待ち 1 件（#1）。Teams の自分とのチャットを確認してください")])
        self.cycle(15, 10)
        self.cycle(17, 0)
        self.assertEqual(len(self.shown), 1)
        self.assertEqual(len(self.sent), 1)

    def test_after_the_quiet_a_later_question_waits_for_the_next_sitting_as_before(self):
        self.sittings()
        other = "19:c_d@unq.gbl.spaces"   # a second chat: a message in the first chat would be a follow-up, announced at once (R17)
        self.t.chat_list = [self_row("メモ"), chat("おはようございます", "9:00"), chat("おはよう", "9:00", cid=other)]
        self.cycle(9, 5)
        self.t.chat_list = [self_row("休み 15時"), chat("これは何ですか", "10:15"), chat("おはよう", "9:00", cid=other)]
        self.cycle(10, 20)
        self.cycle(15, 5)
        self.t.chat_list = [self_row("休み 15時"), chat("これは何ですか", "10:15"), chat("別の質問です", "15:19", cid=other)]
        self.cycle(15, 20)
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件（まとめ）", "#1")])   # the question of 15:20 waits for the sitting
        self.cycle(17, 0)
        self.assertEqual(self.shown[1:], [("kimeru: 確認待ち 1 件（まとめ）", "#2")])


class TestQuietWordsZero(Cycles):
    """With quiet_words=0 (the default) the line 休み <いつ> means nothing: the calls to Teams, the posts, the toast, the push and the
    records are the same as for any other line, and no quiet is kept."""

    def scenario(self, mine):
        self.out, self.inbox = Path(tempfile.mkdtemp()) / "out", Path(tempfile.mkdtemp()) / "inbox"
        self.t, self.shown, self.sent = CallLog(), [], []
        self.lines_for(self.t, "メモ", "おはようございます", "9:00")   # the first sighting of the chat is its baseline
        self.cycle(9, 5)
        self.lines_for(self.t, mine, "これは何ですか")
        r = self.cycle(10, 20)
        self.cycle(15, 5)
        state = json.loads((self.out / "control.json").read_text(encoding="utf-8"))
        return {"calls": list(self.t.calls), "posts": [p[1] for p in self.t.calls if p[0] == "post"], "shown": self.shown,
                "sent": self.sent, "notify": r["notify"], "toast": r["toast"], "push": r["push"], "mode": state["mode"],
                "until": state["until"], "attn": "attn" in (self.out / "decisions.jsonl").read_text(encoding="utf-8")}

    @staticmethod
    def lines_for(t, mine, question=None, time="10:15"):
        t.chat_list = [self_row(mine)] + ([chat(question, time)] if question else [])

    def test_a_quiet_line_with_quiet_words_zero_does_exactly_what_any_other_line_does(self):
        a = self.scenario("休み 15時")
        b = self.scenario("ふつうの一行")
        self.assertEqual(a, b)
        self.assertEqual((a["mode"], a["until"], a["attn"]), ("run", None, False))
        self.assertFalse(any(p.startswith("[kimeru 休み]") for p in a["posts"]))


class TestQuietLine(unittest.TestCase):
    """R27: the line 休み <いつ> sets the mode quiet with its time (control.observe); it is read only with quiet_words=1 and control_words=1."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {"KIMERU_QUIET_WORDS": "1"})
        env.start()
        self.addCleanup(env.stop)

    def run_line(self, mine, now=datetime(2026, 9, 28, 10, 20), st=None):
        base = st or {"mode": "run", "until": None, "set_at": None, "last_sig": "0" * 40, "ack_pending": False, "reminded_on": None}
        return control.observe(base, control.line_of([self_row(mine)]), now)

    @staticmethod
    def quiet():
        return {"mode": "quiet", "until": "2026-09-28T15:00", "set_at": "2026-09-28T10:20:00", "last_sig": "0" * 40,
                "ack_pending": False, "reminded_on": None}

    def test_a_kyuu_line_with_a_time_sets_a_quiet_until_that_time(self):
        st, ev = self.run_line("休み 15時")
        self.assertEqual((st["mode"], st["until"], ev), ("quiet", "2026-09-28T15:00", "quiet"))
        self.assertTrue(st["ack_pending"])

    def test_the_time_words_are_the_ones_the_hold_reads_and_a_day_word_gives_the_morning_hour(self):
        st, _ = self.run_line("休み 明日")
        self.assertEqual(st["until"], "2026-09-29T08:00")
        st, _ = self.run_line("休み 16時", now=datetime(2026, 9, 28, 16, 30))
        self.assertEqual(st["until"], "2026-09-29T16:00")

    def test_a_bare_or_unreadable_or_embedded_kyuu_line_does_nothing(self):
        for mine in ("休み", "休み 25時", "休みます 15時", "あの人 休み 15時", "休み 15時 ください"):
            st, ev = self.run_line(mine)
            self.assertEqual((st["mode"], st["until"], ev), ("run", None, None), mine)

    def test_with_quiet_words_zero_the_kyuu_line_is_not_read(self):
        with mock.patch.dict(os.environ, {"KIMERU_QUIET_WORDS": "0"}):
            st, ev = self.run_line("休み 15時")
        self.assertEqual((st["mode"], st["until"], ev), ("run", None, None))

    def test_a_stop_during_a_quiet_stops_and_clears_its_time(self):
        st, ev = self.run_line("停止", st=self.quiet())
        self.assertEqual((st["mode"], st["until"], ev), ("stop", None, "stop"))

    def test_a_resume_during_a_quiet_does_not_end_it_and_a_kyuu_line_during_a_stop_is_not_read(self):
        st, ev = self.run_line("再開", st=self.quiet())
        self.assertEqual((st["mode"], st["until"], ev), ("quiet", "2026-09-28T15:00", None))
        st, ev = self.run_line("休み 15時", st=dict(self.quiet(), mode="stop", until=None))
        self.assertEqual((st["mode"], ev), ("stop", None))

    def test_a_newer_kyuu_line_moves_the_time_of_the_quiet(self):
        st, ev = self.run_line("休み 16時", st=self.quiet())
        self.assertEqual((st["until"], ev), ("2026-09-28T16:00", "quiet"))


class TestQuietState(unittest.TestCase):
    """R27: the quiet is kept in out/control.json (mode quiet and its time) with no text of the line, and the control step
    of a quiet cycle says run (the cycle goes on); the ack is posted once."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.out = Path(self.dir.name) / "out"
        self.bridge = Bridge()
        env = mock.patch.dict(os.environ, {"KIMERU_QUIET_WORDS": "1", "KIMERU_STATE_DIR": self.dir.name})
        env.start()
        self.addCleanup(env.stop)

    def step(self, mine, now):
        return control.step(self.out, self.bridge, [self_row(mine)], now, send=True)

    def test_the_saved_quiet_keeps_its_time_and_no_text_of_the_line_and_the_ack_is_posted_once(self):
        self.assertEqual(self.step("メモ", datetime(2026, 9, 28, 9, 5)), "run")
        self.assertEqual(self.step("休み 15時", datetime(2026, 9, 28, 10, 20)), "run")
        saved = (self.out / "control.json").read_text(encoding="utf-8")
        self.assertIn('"quiet"', saved)
        self.assertIn("2026-09-28T15:00", saved)
        self.assertNotIn("休み", saved)
        self.assertEqual(control.quiet_until(self.out), datetime(2026, 9, 28, 15, 0))
        self.assertEqual(self.bridge.posts, [QUIET_ACK])
        self.assertEqual(self.step("休み 15時", datetime(2026, 9, 28, 10, 25)), "run")   # the same line again changes nothing
        self.assertEqual(self.bridge.posts, [QUIET_ACK])

    def test_with_quiet_words_zero_no_quiet_is_kept_and_the_mode_stays_run(self):
        with mock.patch.dict(os.environ, {"KIMERU_QUIET_WORDS": "0"}):
            self.step("メモ", datetime(2026, 9, 28, 9, 5))
            self.assertEqual(self.step("休み 15時", datetime(2026, 9, 28, 10, 20)), "run")
            self.assertIsNone(control.quiet_until(self.out))
        self.assertEqual(json.loads((self.out / "control.json").read_text(encoding="utf-8"))["mode"], "run")
        self.assertEqual(self.bridge.posts, [])

    def test_with_control_words_zero_a_saved_quiet_is_not_held_because_the_words_mean_nothing(self):
        self.step("メモ", datetime(2026, 9, 28, 9, 5))
        self.step("休み 15時", datetime(2026, 9, 28, 10, 20))
        self.assertEqual(control.quiet_until(self.out), datetime(2026, 9, 28, 15, 0))
        with mock.patch.dict(os.environ, {"KIMERU_CONTROL_WORDS": "0"}):
            self.assertIsNone(control.quiet_until(self.out))


class TestQuietPlan(unittest.TestCase):
    """R27: attention.plan with the time of a quiet holds the items that are not urgent (the R17 judgment, already recorded), and
    judges nothing again. When the time has come, interrupt=all holds nothing, and interrupt=sittings takes that time as a sitting."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.out = Path(self.dir.name) / "out"
        self.out.mkdir()
        env = mock.patch.dict(os.environ, {"KIMERU_QUIET_WORDS": "1", "KIMERU_INTERRUPT": "all"})
        env.start()
        self.addCleanup(env.stop)
        ap = notify.Approvals(self.out)
        self.held = self.posted(ap, {"urgent": False, "why": []})
        self.urgent = self.posted(ap, {"urgent": True, "why": ["severe"]})
        self.no_record = self.posted(ap, None)
        ap.save()

    @staticmethod
    def posted(ap, attn):
        rec = {"summary": "x"}
        if attn is not None:
            rec["attn"] = attn
        n = ap.add("g:n", rec)
        ap.data["items"][str(n)]["posted"] = True
        return n

    def test_during_a_quiet_the_hold_is_the_items_that_are_not_urgent_and_nothing_is_judged_again(self):
        with mock.patch.object(attention, "judge", side_effect=AssertionError("no second urgency judgment")):
            p = attention.plan(self.out, datetime(2026, 9, 28, 10, 20), until=datetime(2026, 9, 28, 15, 0))
        self.assertEqual(p, {"sitting_at": None, "hold": {self.held}})

    def test_once_the_time_has_come_under_interrupt_all_nothing_is_held(self):
        self.assertEqual(attention.plan(self.out, datetime(2026, 9, 28, 15, 5), until=datetime(2026, 9, 28, 15, 0)),
                         {"sitting_at": None, "hold": set()})

    def test_without_a_quiet_nothing_changes_under_interrupt_all(self):
        self.assertEqual(attention.plan(self.out, datetime(2026, 9, 28, 10, 20), until=None), {"sitting_at": None, "hold": set()})

    def test_under_sittings_the_time_of_the_quiet_is_the_sitting_that_announces_the_held_items(self):
        with mock.patch.dict(os.environ, {"KIMERU_INTERRUPT": "sittings", "KIMERU_SIT_HOURS": "9,13,17"}):
            until = datetime(2026, 9, 28, 15, 0)
            self.assertEqual(attention.plan(self.out, datetime(2026, 9, 28, 13, 5), until=until), {"sitting_at": None, "hold": {self.held}})
            self.assertEqual(attention.plan(self.out, datetime(2026, 9, 28, 15, 5), until=until)["sitting_at"], "2026-09-28T15:00")
            self.assertEqual(attention.plan(self.out, datetime(2026, 9, 28, 17, 5), until=until)["sitting_at"], "2026-09-28T17:00")


class TestQuietDocs(unittest.TestCase):
    def test_the_quiet_setting_has_one_row_in_the_reference_and_the_security_and_changelog_name_it(self):
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        self.assertEqual(sum(1 for l in ref.splitlines() if l.startswith("| `KIMERU_QUIET_WORDS`")), 1)
        sec = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("quiet_words", sec)
        self.assertIn("休み", sec)
        log = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = log
        self.assertIn("quiet_words", unreleased)
        self.assertIn("休み", unreleased)

    def test_the_config_declares_quiet_words_as_a_zero_or_one_setting_that_defaults_to_zero(self):
        self.assertEqual(config.SETTINGS["quiet_words"], ("KIMERU_QUIET_WORDS", "0"))
        self.assertEqual(config.CHOICES["quiet_words"], ("0", "1"))
        self.assertIn("quiet_words", config.LOGGABLE)
        self.assertIn("quiet_words", config.SHARE_VALUES)


# ---- R37: a sender who chases an item that waits for the sitting is announced at once from then on (interrupt=sittings only) ----

NOW = datetime(2026, 9, 28, 10, 20)   # naive local, as the cycle's clock is
NOW_UTC = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)
SITTING_ENV = {"KIMERU_INTERRUPT": "sittings", "KIMERU_SIT_HOURS": "9,13,17"}
CHAT_A, CHAT_B, CHAT_C, CHAT_D = "19:a_b@unq.gbl.spaces", "19:b_c@unq.gbl.spaces", "19:c_d@unq.gbl.spaces", "19:d_e@unq.gbl.spaces"
# the golden texts of `digest --week` (no share), written before the change: two approvals, and six at 9, 9, 13, 13, 13, 17 o'clock
WEEK_TWO = """kimeru の直近 7 日（2026-10-05 から）
判断: 0 件
前の 7 日（2026-09-28 から）: 判断 0 件
承認の結果: OK 2
1 件あたりの返信: 1.0 回（返信のあった件 2 件・参考）
同じ修正指示の繰り返し（別の項目で）: 0 件
自動で決めた判断で、正誤に「違う」が付いた件: 不明（対象 0 件）
提案が一度も変わらなかった OK: 不明（下書きのある OK 0 件）
下書きの使われ方: 不明（決まった下書きの件 0 件）
writer の呼び出し: 0 回
確認待ちの古さ: 確認待ち 0 件（測れた件なし）
投稿から OK を見るまで（中央値）: 今週 不明、前の 7 日 不明
calibrate の正誤（判断モデルごと。下限 100 問）: CLM 0 問（あと 100 問）、Kev 0 問（あと 100 問）
到着から判断完了まで（PC が見た時刻から）: 不明（記録なし）
到着から承認まで（PC が見た時刻から）: 不明（記録なし）
OK の後の送信（自分のチャットの一覧で見た。届いたことの証明ではない）: 不明（記録なし）
正誤のラベルの増分（今週）: 不明（記録なし）
採用した学習の前後（今週）: 不明（記録なし）
朝のまとめの上位 3 件で、同じ日に返信か作業の記録があった件: 不明（承認番号のある件 0 件）
判断の一致率: まだ確かめた判断がありません（kimeru review）"""
WEEK_SIX = """kimeru の直近 7 日（2026-10-05 から）
判断: 0 件
前の 7 日（2026-09-28 から）: 判断 0 件
承認の結果: OK 6
1 件あたりの返信: 1.0 回（返信のあった件 6 件）
同じ修正指示の繰り返し（別の項目で）: 0 件
自動で決めた判断で、正誤に「違う」が付いた件: 不明（対象 0 件）
提案が一度も変わらなかった OK: 不明（下書きのある OK 0 件）
下書きの使われ方: 不明（決まった下書きの件 0 件）
writer の呼び出し: 0 回
確認待ちの古さ: 確認待ち 0 件（測れた件なし）
投稿から OK を見るまで（中央値）: 今週 不明、前の 7 日 不明
calibrate の正誤（判断モデルごと。下限 100 問）: CLM 0 問（あと 100 問）、Kev 0 問（あと 100 問）
到着から判断完了まで（PC が見た時刻から）: 不明（記録なし）
到着から承認まで（PC が見た時刻から）: 不明（記録なし）
OK の後の送信（自分のチャットの一覧で見た。届いたことの証明ではない）: 不明（記録なし）
正誤のラベルの増分（今週）: 不明（記録なし）
採用した学習の前後（今週）: 不明（記録なし）
朝のまとめの上位 3 件で、同じ日に返信か作業の記録があった件: 不明（承認番号のある件 0 件）
判断の一致率: まだ確かめた判断がありません（kimeru review）"""
POST_GOLDEN = "[kimeru #1] 確認の依頼\n→ 内容を確認してください\nOK 1 / NG 1 / 詳細 1"


def waiting_item(author, revision=1, followups=(), first_ready=datetime(2026, 9, 28, 10, 0), toasted=False):
    """An approvals item the way a cycle keeps a posted, non-urgent chat item: its sender, its followups, its first ready time."""
    ready = first_ready.astimezone().isoformat(timespec="milliseconds")
    return {"key": f"k-{author}-{revision}", "status": "pending", "posted": True, "toasted": toasted, "revision": revision,
            "record": {"node": "ask_pm", "revision": revision, "attn": {"urgent": False, "why": []},
                       "event": {"kind": "teams.chat", "author": author, "chat_id": "19:c@unq", "text": "これは何ですか"},
                       "followups": [{"text": t, "at": "2026-09-28T10:00:00+00:00"} for t in followups]},
            "measurement": {"schema": 1, "source": "test", "generation": 1, "ready_at": ready, "first_ready_at": ready,
                            "case_key": "k"}}


def read_json_file(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class TestChaseBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.out = Path(self.dir.name) / "out"
        env = mock.patch.dict(os.environ, SITTING_ENV)
        env.start()
        self.addCleanup(env.stop)

    def save(self, items, **keys):
        """approvals.json with these items as the numbers 1, 2, ... (and any other top-level keys)."""
        self.out.mkdir(parents=True, exist_ok=True)
        data = {"next": len(items) + 1, "items": {str(i): it for i, it in enumerate(items, 1)}, **keys}
        (self.out / "approvals.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def approvals(self):
        return read_json_file(self.out / "approvals.json")

    def follow_up(self, n, text, revision_up=True):
        """A follow-up of item n as the merge writes it: one more followup row, and the revision up unless it is an acknowledgement."""
        data = self.approvals()
        it = data["items"][str(n)]
        it["record"]["followups"] = list(it["record"].get("followups") or []) + [{"text": text, "at": "2026-09-28T10:10:00+00:00"}]
        if revision_up:
            it["revision"] = int(it["revision"]) + 1
            it["record"]["revision"] = it["revision"]
        (self.out / "approvals.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def hold_at(self, now):
        """One cycle at `now`: what waits for the sitting is held (attention.note). Returns what note returns."""
        return attention.note(self.out, now, attention.waiting(self.out))

    def promote(self, author="田中", n=1):
        """Item n of `author` is held, then chased 45 minutes after its first ready time: the sender is listed."""
        self.save([waiting_item(author, first_ready=datetime(2026, 9, 28, 9, 30))])
        self.hold_at(datetime(2026, 9, 28, 9, 40))
        self.follow_up(n, "資料を追加しました")
        self.assertEqual(self.hold_at(datetime(2026, 9, 28, 10, 15)), 1)


class TestChaseRule(TestChaseBase):
    def test_a_held_item_keeps_its_number_and_revision_and_nothing_is_listed_yet(self):
        self.save([waiting_item("田中", first_ready=datetime(2026, 9, 28, 9, 50))])
        self.assertEqual(attention.waiting(self.out), {1})
        self.assertEqual(self.hold_at(NOW), 0)
        self.assertEqual(self.approvals()["sitting_waited"], {"1": 1})
        self.assertFalse((self.out / "attention.json").exists())

    def test_two_quick_follow_ups_without_a_chase_word_do_not_promote_the_sender(self):
        self.save([waiting_item("田中")])
        self.hold_at(datetime(2026, 9, 28, 10, 0))
        self.follow_up(1, "資料を追加しました")
        self.follow_up(1, "修正版です")
        self.assertEqual(attention.waiting(self.out), set())   # the item is announced at once, as it is today
        self.assertEqual(self.hold_at(datetime(2026, 9, 28, 10, 5)), 0)
        self.assertEqual(attention.senders(self.out), set())
        self.assertFalse((self.out / "attention.json").exists())

    def test_a_follow_up_about_thirty_minutes_after_the_first_ready_time_promotes_the_sender(self):
        self.save([waiting_item("田中", first_ready=datetime(2026, 9, 28, 9, 30))])
        self.hold_at(datetime(2026, 9, 28, 9, 40))
        self.follow_up(1, "資料を追加しました")
        self.assertEqual(self.hold_at(datetime(2026, 9, 28, 10, 15)), 1)   # 45 minutes after the first ready time
        self.assertEqual(attention.senders(self.out), {"田中"})
        self.assertEqual(read_json_file(self.out / "attention.json")["senders"][0]["why"], "minutes")

    def test_a_chasing_word_promotes_the_sender_even_a_few_minutes_after_the_first_ready_time(self):
        self.save([waiting_item("田中")])
        self.hold_at(datetime(2026, 9, 28, 10, 2))
        self.follow_up(1, "まだですか")
        self.assertEqual(self.hold_at(datetime(2026, 9, 28, 10, 5)), 1)
        self.assertEqual(attention.senders(self.out), {"田中"})
        self.assertEqual(read_json_file(self.out / "attention.json")["senders"][0]["why"], "words")

    def test_an_acknowledgement_is_never_a_chase_because_it_moves_no_revision(self):
        self.save([waiting_item("田中", first_ready=datetime(2026, 9, 28, 9, 30))])
        self.hold_at(datetime(2026, 9, 28, 9, 40))
        self.follow_up(1, "ありがとうございます まだですか", revision_up=False)
        self.assertEqual(self.hold_at(datetime(2026, 9, 28, 10, 30)), 0)
        self.assertEqual(attention.senders(self.out), set())

    def test_an_item_already_announced_at_its_sitting_is_not_held_so_a_later_follow_up_is_no_chase(self):
        self.save([waiting_item("田中", first_ready=datetime(2026, 9, 28, 9, 30), toasted=True)])
        self.assertEqual(self.hold_at(datetime(2026, 9, 28, 9, 40)), 0)
        self.assertEqual(self.approvals().get("sitting_waited", {}), {})   # not kept: its notice already went out
        self.follow_up(1, "資料を追加しました")
        self.assertEqual(self.hold_at(datetime(2026, 9, 28, 10, 15)), 0)
        self.assertEqual(attention.senders(self.out), set())

    def test_a_follow_up_on_an_item_that_was_never_held_does_not_promote(self):
        self.save([waiting_item("田中", revision=2, followups=["資料を追加しました"], first_ready=datetime(2026, 9, 28, 9, 30))])
        self.assertEqual(self.hold_at(NOW), 0)
        self.assertEqual(attention.senders(self.out), set())

    def test_a_merge_that_clears_the_item_keeps_the_mark_in_the_approvals_state(self):
        self.save([waiting_item("田中", first_ready=datetime(2026, 9, 28, 9, 30))])
        self.hold_at(datetime(2026, 9, 28, 9, 40))
        data = self.approvals()
        it = data["items"]["1"]
        measurement = it["measurement"]
        it.clear()   # cli._merge_locked: the item is cleared and rebuilt; the mark is not in the item, so it is not lost
        it.update({"measurement": measurement, "revision": 2, "status": "pending", "posted": False, "toasted": False,
                   "record": {"node": "ask_pm", "revision": 2, "attn": {"urgent": False, "why": []},
                              "event": {"kind": "teams.chat", "author": "田中", "chat_id": "19:c@unq"},
                              "followups": [{"text": "資料を追加しました", "at": "2026-09-28T10:10:00+00:00"}]}})
        (self.out / "approvals.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.hold_at(datetime(2026, 9, 28, 10, 20)), 1)
        self.assertEqual(attention.senders(self.out), {"田中"})

    def test_the_promoted_sender_has_its_next_item_announced_at_once_while_another_sender_still_waits(self):
        self.save([waiting_item("田中", first_ready=datetime(2026, 9, 28, 9, 30)), waiting_item("佐藤")])
        self.hold_at(datetime(2026, 9, 28, 9, 40))
        self.follow_up(1, "資料を追加しました")
        self.assertEqual(self.hold_at(datetime(2026, 9, 28, 10, 15)), 1)
        data = self.approvals()
        data["items"]["3"] = waiting_item("田中")   # a new question of the chasing sender
        data["next"] = 4
        (self.out / "approvals.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(attention.waiting(self.out), {2})   # 佐藤 still waits; 田中 is announced at once

    def test_the_note_returns_the_number_of_senders_listed_now(self):
        self.save([waiting_item("田中", first_ready=datetime(2026, 9, 28, 9, 30))])
        self.hold_at(datetime(2026, 9, 28, 9, 40))
        self.follow_up(1, "資料を追加しました")
        result = self.hold_at(datetime(2026, 9, 28, 10, 15))
        self.assertIs(type(result), int)
        self.assertEqual(result, 1)
        self.assertEqual(self.hold_at(datetime(2026, 9, 28, 10, 20)), 0)   # the item no longer waits, so nothing more is listed

    def test_under_interrupt_all_nothing_is_kept_listed_or_held_and_the_brief_is_the_same(self):
        with mock.patch.dict(os.environ, {"KIMERU_INTERRUPT": "all"}):
            self.save([waiting_item("田中", first_ready=datetime(2026, 9, 28, 9, 30))])
            self.assertEqual(self.hold_at(datetime(2026, 9, 28, 9, 40)), 0)
            self.follow_up(1, "まだですか")
            self.assertEqual(self.hold_at(datetime(2026, 9, 28, 10, 20)), 0)
            self.assertNotIn("sitting_waited", self.approvals())
            self.assertFalse((self.out / "attention.json").exists())
            self.assertEqual(attention.senders(self.out), set())
            self.assertEqual(attention.brief_line(self.out, "本文"), "本文")

    def test_a_busy_approvals_lock_skips_the_chase_and_the_next_cycle_tries_it_again(self):
        self.save([waiting_item("田中", first_ready=datetime(2026, 9, 28, 9, 30))])
        self.hold_at(datetime(2026, 9, 28, 9, 40))
        self.follow_up(1, "資料を追加しました")

        class Busy:
            def __enter__(self):
                return False

            def __exit__(self, *exc):
                return False
        with mock.patch("kimeru.fsutil.exclusive", return_value=Busy()):
            self.assertIsNone(self.hold_at(datetime(2026, 9, 28, 10, 15)))
        self.assertEqual(self.approvals()["sitting_waited"], {"1": 1})   # nothing changed: the mark is still there
        self.assertFalse((self.out / "attention.json").exists())
        self.assertEqual(self.hold_at(datetime(2026, 9, 28, 10, 15)), 1)
        self.assertEqual(attention.senders(self.out), {"田中"})


class TestChaseAnnouncement(TestChaseBase):
    def test_a_listed_sender_items_wait_for_no_sitting_and_others_still_do(self):
        self.promote("田中")
        self.assertEqual(attention.senders(self.out), {"田中"})
        data = self.approvals()
        data["items"]["5"] = waiting_item("佐藤")
        data["items"]["6"] = waiting_item("田中", first_ready=datetime(2026, 9, 28, 10, 0))
        data["next"] = 7
        (self.out / "approvals.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(attention.waiting(self.out), {5})

    def test_the_morning_brief_line_names_the_listed_senders_after_the_text_and_never_the_others(self):
        self.promote("田中")
        text = "朝のまとめ\n・件 1\n今日の要点（writer）:\n・要点"
        self.assertEqual(attention.brief_line(self.out, text),
                         text + "\n今後 田中さんはすぐ知らせます（外すには kimeru attention remove）")

    def test_the_brief_line_counts_the_senders_beyond_three_without_naming_them(self):
        for name in ("田中", "佐藤", "鈴木", "高橋"):
            self.promote(name)
        line = attention.brief_line(self.out, "本文").split("\n")[-1]
        self.assertTrue(line.startswith("今後 田中さん・佐藤さん・鈴木さん ほか 1 人はすぐ知らせます"), line)
        self.assertNotIn("高橋", line)


class TestChaseCli(TestChaseBase):
    def run_cli(self, *args):
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            rc = cli.main(["--out", str(self.out), *args])
        return rc, buf.getvalue(), err.getvalue()

    def test_attention_list_names_the_senders_and_remove_takes_one_out_with_its_backup(self):
        self.promote("田中")
        self.promote("佐藤")   # a second write leaves attention.json.bak behind
        self.assertTrue((self.out / "attention.json.bak").exists())
        rc, listed, _ = self.run_cli("attention", "list")
        self.assertEqual(rc, 0)
        self.assertIn("田中", listed)
        self.assertIn("佐藤", listed)
        rc, _, _ = self.run_cli("attention", "remove", "田中")
        self.assertEqual(rc, 0)
        self.assertEqual(attention.senders(self.out), {"佐藤"})
        self.assertFalse((self.out / "attention.json.bak").exists())

    def test_removing_a_name_that_is_not_listed_changes_nothing_and_says_so(self):
        self.promote("田中")
        before = (self.out / "attention.json").read_text(encoding="utf-8")
        rc, _, err = self.run_cli("attention", "remove", "山田")
        self.assertEqual(rc, 1)
        self.assertIn("一致", err)
        self.assertEqual((self.out / "attention.json").read_text(encoding="utf-8"), before)

    def test_attention_list_says_so_when_nobody_is_listed_and_works_under_interrupt_all(self):
        with mock.patch.dict(os.environ, {"KIMERU_INTERRUPT": "all"}):
            rc, listed, _ = self.run_cli("attention", "list")
        self.assertEqual(rc, 0)
        self.assertIn("ありません", listed)


class TestChaseDigest(TestChaseBase):
    def write_log(self, hours):
        self.out.mkdir(parents=True, exist_ok=True)
        rows = [{"at": datetime(2026, 10, 10, h, 30).astimezone().isoformat(timespec="seconds"), "key": f"k{i}", "id": i,
                 "status": "approved"} for i, h in enumerate(hours)]
        (self.out / "approvals.log.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                                                      encoding="utf-8")

    def test_two_approvals_are_too_few_for_a_suggestion_and_the_digest_is_byte_for_byte_the_same(self):
        self.write_log((9, 13))
        self.assertEqual(stats.render(stats.collect(self.out, NOW_UTC), share=False), WEEK_TWO)

    def test_the_week_digest_suggests_three_sit_hours_from_the_approval_times_and_changes_nothing(self):
        self.write_log((9, 9, 13, 13, 13, 17))
        text = stats.render(stats.collect(self.out, NOW_UTC), share=False)
        expected = WEEK_SIX.replace("承認の結果: OK 6\n", "承認の結果: OK 6\nsit_hours の候補: 9,13,17（承認が多かった時刻。自動では変えません）\n")
        self.assertEqual(text, expected)

    def test_the_share_text_never_has_the_suggestion(self):
        self.write_log((9, 9, 13, 13, 13, 17))
        self.assertNotIn("sit_hours の候補", stats.render(stats.collect(self.out, NOW_UTC), share=True))

    def test_a_refusal_or_a_hold_is_not_an_approval_time_so_it_never_moves_the_suggestion(self):
        pairs = [(9, "approved"), (13, "approved"), (17, "approved")] + [(20, "rejected")] * 3 + [(21, "held")] * 3
        self.out.mkdir(parents=True, exist_ok=True)
        rows = [{"at": datetime(2026, 10, 10, h, 30).astimezone().isoformat(timespec="seconds"), "key": f"k{i}", "id": i, "status": s}
                for i, (h, s) in enumerate(pairs)]
        (self.out / "approvals.log.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        text = stats.render(stats.collect(self.out, NOW_UTC), share=False)
        self.assertIn("sit_hours の候補: 9,13,17（", text)

    def test_the_default_interrupt_all_digest_has_no_candidate_line_even_with_six_approvals_at_sitting_hours(self):
        """The default (interrupt=all) changes nothing in `digest --week`: six approvals at 9, 9, 13, 13, 13 and 17 o'clock give the
        golden text WEEK_SIX, and no candidate is computed (reference.md:99: under the default nothing is written and nothing is shown)."""
        self.write_log((9, 9, 13, 13, 13, 17))
        with mock.patch.dict(os.environ, {"KIMERU_INTERRUPT": "all"}):
            summary = stats.collect(self.out, NOW_UTC)
            text = stats.render(summary, share=False)
        self.assertIsNone(summary["sit_hours_candidate"])
        self.assertNotIn("sit_hours の候補", text)
        self.assertEqual(text, WEEK_SIX)
        with mock.patch.dict(os.environ, {"KIMERU_INTERRUPT": "all"}):   # the render gate on its own: a candidate handed to it is not shown
            forced = stats.render({**summary, "sit_hours_candidate": [9, 13, 17]}, share=False)
        self.assertEqual(forced, WEEK_SIX)


class TestChaseGoldenAndCycles(Cycles):
    """The texts the default keeps (written before the change), and what a real cycle keeps under each setting."""

    def test_the_approval_post_text_is_byte_for_byte_the_same(self):
        rec = {"node": "ask_pm", "event": {"kind": "teams.chat", "author": "田中", "text": "これは何ですか"}, "actions": [],
               "revision": 1, "followups": [], "summary": "確認の依頼", "notify": False}
        self.assertEqual(notify.format_post(1, rec), POST_GOLDEN)

    def test_a_default_cycle_keeps_no_held_numbers_and_lists_no_sender(self):
        self.open_day()
        self.chat(f"これは何ですか {BODY}", "10:15")
        self.cycle(10, 20)
        self.assertNotIn("sitting_waited", read_json_file(self.out / "approvals.json"))
        self.assertFalse((self.out / "attention.json").exists())

    def test_under_sittings_a_held_question_keeps_its_number_and_revision_and_nobody_is_listed_yet(self):
        self.sittings()
        self.open_day()
        self.chat(f"これは何ですか {BODY}", "10:15")
        self.cycle(10, 20)
        self.assertEqual(read_json_file(self.out / "approvals.json")["sitting_waited"], {"1": 1})
        self.assertEqual(self.shown, [])
        self.assertFalse((self.out / "attention.json").exists())

    # ---- the chase in real cycles (the times are injected; the chase is by a chasing word, as the 30-minute rule reads the real clock) ----

    def screen(self, *rows):
        """The chat list on screen: one (preview, time, sender, chat id) row per chat. A chat whose preview and time did not change is
        not read again."""
        self.t.chat_list = [chat(preview, time, cid=cid, title=who) for preview, time, who, cid in rows]

    def start_day(self, who):
        """The first cycle of the day: a greeting from `who` in chat A is the baseline, and nothing waits."""
        self.screen(("おはようございます", "9:00", who, CHAT_A))
        return self.cycle(9, 5)

    def list_senders(self, *names):
        """out/attention.json as a chase leaves it (the names are kept here and in the brief, nowhere else)."""
        self.out.mkdir(parents=True, exist_ok=True)
        rows = [{"name": n, "since": "2026-09-28T07:30", "why": "words"} for n in names]
        (self.out / "attention.json").write_text(json.dumps({"senders": rows}, ensure_ascii=False, indent=1), encoding="utf-8")

    def two_held_questions_then_a_chase(self):
        """田中 asks in chat A at 10:15 and in chat B at 10:59 (both held). Her 'まだですか' in chat A at 10:58 is a chase: the cycle at
        11:00 lists 田中, and both items are announced in that cycle (item 2 by the sitting read again after the listing)."""
        self.sittings()
        self.start_day("田中")
        self.screen(("これは何ですか", "10:15", "田中", CHAT_A))
        self.cycle(10, 20)
        self.screen(("これは何ですか", "10:15", "田中", CHAT_A), ("これは何ですか", "10:59", "田中", CHAT_B))
        self.cycle(10, 59)
        self.assertEqual((self.shown, self.sent), ([], []))
        self.screen(("まだですか", "10:58", "田中", CHAT_A), ("これは何ですか", "10:59", "田中", CHAT_B))
        return self.cycle(11, 0)

    def test_after_a_chase_no_text_of_the_cycle_or_the_week_digest_carries_the_senders_name(self):
        self.two_held_questions_then_a_chase()
        self.assertEqual(self.shown, [("kimeru: 確認待ち 2 件", "#1, #2")])
        self.assertEqual(self.sent, [("webhook", "kimeru: 確認待ち 2 件（#1, #2）。Teams の自分とのチャットを確認してください")])
        for title, body in self.shown:
            self.assertNotIn("田中", title + body)
        for _, text in self.sent:
            self.assertNotIn("田中", text)
        self.assertNotIn("田中", stats.week(self.out, share=True))
        self.assertNotIn("田中", (self.out / "daily.log.jsonl").read_text(encoding="utf-8"))
        self.assertEqual([r["name"] for r in read_json_file(self.out / "attention.json")["senders"]], ["田中"])

    def test_a_chase_announces_the_senders_other_held_item_in_the_same_cycle(self):
        self.two_held_questions_then_a_chase()
        self.assertIn("#2", self.shown[0][1])   # item 2 is a question of 田中's that was held: it is announced now, not at 13:00
        self.assertEqual(len(self.shown), 1)

    def test_the_listed_senders_next_item_is_announced_at_once_and_another_senders_waits_for_the_sitting(self):
        self.two_held_questions_then_a_chase()
        self.screen(("まだですか", "10:58", "田中", CHAT_A), ("これは何ですか", "10:59", "田中", CHAT_B),
                    ("これは何ですか", "11:35", "田中", CHAT_C))
        self.cycle(11, 35)
        self.assertEqual(self.shown[1:], [("kimeru: 確認待ち 1 件", "#3")])                # 田中's next item: at once, before 13:00
        self.assertEqual(self.sent[1:], [("webhook", "kimeru: 確認待ち 1 件（#3）。Teams の自分とのチャットを確認してください")])
        self.screen(("まだですか", "10:58", "田中", CHAT_A), ("これは何ですか", "10:59", "田中", CHAT_B),
                    ("これは何ですか", "11:35", "田中", CHAT_C), ("これは何ですか", "11:40", "佐藤", CHAT_D))
        self.cycle(11, 40)
        self.assertEqual(len(self.shown), 2)                                                 # 佐藤's item waits for the sitting
        self.assertEqual(len(self.sent), 2)
        self.cycle(13, 5)
        self.assertEqual(self.shown[2:], [("kimeru: 確認待ち 1 件（まとめ）", "#4")])

    def morning_brief(self):
        """The first cycle at or after the brief hour, with a question from 田中 and one from 佐藤 in it: the brief text, and the lines
        the writer was given (summarize_day is the writer's only call the brief makes)."""
        self.screen(("おはようございます", "7:00", "田中", CHAT_A))
        self.cycle(7, 0)                                    # before the brief hour: the baseline, no brief yet
        self.screen(("これは何ですか", "8:05", "田中", CHAT_A), ("これは何ですか", "8:08", "佐藤", CHAT_D))
        writer_lines = []

        def summarize(writer, lines):
            writer_lines.append(list(lines))
            return ["要点を一つ"]
        with mock.patch("kimeru.writer.summarize_day", side_effect=summarize):
            self.cycle(8, 10)
        briefs = [t for t, _ in self.t.posts if t.startswith("[kimeru brief")]
        self.assertEqual(len(briefs), 1)
        return briefs[0], writer_lines

    def test_under_sittings_the_morning_brief_ends_with_the_listed_senders_line_and_the_writer_never_gets_it(self):
        self.sittings()
        self.list_senders("田中")
        text, lines = self.morning_brief()
        self.assertTrue(lines)                                                               # the writer was asked
        self.assertFalse(any("今後" in line for call in lines for line in call))
        self.assertIn("今後 田中さんはすぐ知らせます（外すには kimeru attention remove）", text)
        self.assertLess(text.index("要点を一つ"), text.index("今後 田中さん"))               # after the writer's points

    def test_under_interrupt_all_the_same_brief_has_no_line_even_when_a_file_lists_a_sender(self):
        self.list_senders("田中")
        text, lines = self.morning_brief()
        self.assertTrue(lines)
        self.assertIn("要点を一つ", text)
        self.assertNotIn("今後", text)
        self.assertFalse(any("今後" in line for call in lines for line in call))

    def test_a_failed_chase_step_logs_the_error_type_and_never_its_message(self):
        self.sittings()
        self.start_day("田中")
        self.screen(("これは何ですか", "10:15", "田中", CHAT_A))
        with mock.patch("kimeru.attention.note", side_effect=ValueError("田中 の本文 SECRETXYZ")):
            r = self.cycle(10, 20)
        log = (self.out / "daily.log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"chase": "ValueError"', log)
        self.assertNotIn("SECRETXYZ", log)
        self.assertNotIn("田中", log)
        self.assertEqual(self.shown, [])                                                     # the cycle goes on: the item waits as before
        self.assertEqual(r["notify"], [1])

    def test_two_quick_follow_ups_in_real_cycles_list_nobody(self):
        self.sittings()
        self.start_day("田中")
        self.screen(("これは何ですか", "10:15", "田中", CHAT_A))
        self.cycle(10, 20)
        self.screen(("資料を追加しました", "10:21", "田中", CHAT_A))
        self.cycle(10, 25)
        self.screen(("修正版です", "10:26", "田中", CHAT_A))
        self.cycle(10, 30)
        self.assertEqual(read_json_file(self.out / "approvals.json")["items"]["1"]["revision"], 3)   # both follow-ups merged into item 1
        self.assertFalse((self.out / "attention.json").exists())
        self.assertEqual(attention.senders(self.out), set())

    # ---- AC2b: under sittings, a severe, notify or critical item is never held, not by the sittings and not by a listed sender ----

    def chase_then_an_ordinary_question(self):
        """After the chase (田中 listed; #1 and #2 announced at 11:00), 佐藤 asks an ordinary question at 11:10: it waits for 13:00 (#3).
        The sittings and the listed sender are the state the next cycles read."""
        self.two_held_questions_then_a_chase()
        self.shown.clear()
        self.sent.clear()
        self.screen(("まだですか", "10:58", "田中", CHAT_A), ("これは何ですか", "10:59", "田中", CHAT_B),
                    ("これは何ですか", "11:10", "佐藤", CHAT_C))
        self.cycle(11, 10)
        self.assertEqual((self.shown, self.sent), ([], []))
        self.assertEqual(attention.waiting(self.out), {3})   # 佐藤's question waits; 田中 is listed, so her items are not waiting

    def sato_chat_at_1130(self, preview):
        """A chat from 佐藤 at 11:30 with `preview`, read in the cycle at 11:30 (the other chats stay on the screen)."""
        self.screen(("まだですか", "10:58", "田中", CHAT_A), ("これは何ですか", "10:59", "田中", CHAT_B),
                    ("これは何ですか", "11:10", "佐藤", CHAT_C), (preview, "11:30", "佐藤", CHAT_D))
        return self.cycle(11, 30)

    def last_decision(self):
        return json.loads((self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])

    def test_a_critical_item_held_for_a_recovery_note_is_toasted_and_pushed_in_the_cycle_it_arrives_while_a_sender_is_listed(self):
        """A critical rule that said mixed (a recovery note) posts an item that waits for a sitting unless it is urgent: it is #4, a
        pending item, not a notice, so the held path is the one that must let it through."""
        self.chase_then_an_ordinary_question()
        self.sato_chat_at_1130("本番が落ちていましたが復旧しました")
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件", "#4")])
        self.assertEqual(self.sent, [("webhook", "kimeru: 確認待ち 1 件（#4）。Teams の自分とのチャットを確認してください")])
        self.assertEqual(self.last_decision()["attn"]["urgent"], True)
        self.assertIn("critical", self.last_decision()["attn"]["why"])
        self.assertEqual(attention.waiting(self.out), {3})                # the ordinary question still waits for 13:00

    def test_a_sev1_alert_is_toasted_and_pushed_in_the_cycle_it_arrives_while_a_sender_is_listed(self):
        self.chase_then_an_ordinary_question()
        alert = {"kind": "monitor.alert", "id": "s1", "rule": "prod-db storage", "severity": "Sev1", "condition": "Fired",
                 "description": "Storage 92% used on prod-db"}
        self.inbox.mkdir(parents=True, exist_ok=True)
        (self.inbox / "alert.json").write_text(json.dumps(alert, ensure_ascii=False), encoding="utf-8")
        self.cycle(11, 30)
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件", "#4")])
        self.assertEqual(self.sent, [("webhook", "kimeru: 確認待ち 1 件（#4）。Teams の自分とのチャットを確認してください")])
        self.assertEqual(self.last_decision()["attn"]["urgent"], True)
        self.assertIn("severe", self.last_decision()["attn"]["why"])
        self.assertEqual(attention.waiting(self.out), {3})

    def test_a_decision_that_asks_for_notice_is_announced_as_a_notice_at_once_while_a_sender_is_listed(self):
        """A decision with notice is decided at once and announced as a notice (a notice is never held); its record says it is urgent
        because of the notice, and the ordinary question of 佐藤 still waits for 13:00."""
        self.chase_then_an_ordinary_question()
        r = self.sato_chat_at_1130("本日の判断をお願いします")
        self.assertEqual(r["notices"], 1)
        self.assertEqual(r["notify"], [])
        self.assertEqual(self.shown, [("kimeru: 自動決定の通知 1 件", "Teams の自分とのチャットを確認してください")])
        self.assertEqual(self.sent, [("webhook", "kimeru: 自動決定の通知 1 件。Teams の自分とのチャットを確認してください")])
        self.assertEqual(self.last_decision()["attn"]["urgent"], True)
        self.assertIn("notify", self.last_decision()["attn"]["why"])
        self.assertEqual(attention.waiting(self.out), {3})


class TestChaseDocs(unittest.TestCase):
    def test_the_reference_security_and_changelog_name_the_attention_command_and_file(self):
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        self.assertIn("kimeru attention list", ref)
        self.assertIn("kimeru attention remove", ref)
        sec = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("attention.json", sec)
        self.assertIn("sitting_waited", sec)
        log = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = log
        self.assertIn("kimeru attention", unreleased)

    def test_the_chase_adds_no_setting_so_the_default_is_the_interrupt_setting(self):
        self.assertFalse([k for k in config.SETTINGS if "attention" in k or "chase" in k])


class TestThePromotionPathInDailyCycles(Cycles):
    """R37 in one run of daily cycles (every time injected, Teams and the push route are fakes): under interrupt=sittings an item of
    田中 waits for the sitting; a later cycle's judge merges her chasing follow-up into it (the revision goes up), so she is listed; her
    next item is announced at once while 佐藤's still waits; a Sev1 alert is never held, before or after the listing."""

    def screen(self, *rows):
        self.t.chat_list = [chat(preview, time, cid=cid, title=who) for preview, time, who, cid in rows]

    def alert(self, name):
        item = {"kind": "monitor.alert", "id": name, "rule": f"prod-db {name}", "severity": "Sev1", "condition": "Fired",
                "description": f"Storage 92% used on {name}"}
        self.inbox.mkdir(parents=True, exist_ok=True)
        (self.inbox / f"{name}.json").write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")

    def approvals(self):
        return read_json_file(self.out / "approvals.json")

    def test_a_chased_item_lists_the_sender_and_her_next_item_is_announced_at_once_and_a_severe_one_is_never_held(self):
        self.sittings()
        self.screen(("おはようございます", "9:00", "田中", CHAT_A))
        self.cycle(9, 5)                                                                       # the baseline of the day
        self.screen(("これは何ですか", "10:15", "田中", CHAT_A))
        self.cycle(10, 20)
        self.assertEqual((self.shown, self.sent), ([], []))                                    # item 1 waits for the 13:00 sitting
        self.assertEqual(attention.waiting(self.out), {1})
        self.assertEqual(self.approvals()["sitting_waited"], {"1": 1})
        self.assertFalse((self.out / "attention.json").exists())                               # nobody is listed from one question
        self.alert("s1")
        self.cycle(10, 25)                                                                     # a Sev1 alert is not deferred
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件", "#2")])
        self.assertEqual(self.sent, [("webhook", "kimeru: 確認待ち 1 件（#2）。Teams の自分とのチャットを確認してください")])
        self.assertEqual(attention.waiting(self.out), {1})                                     # only the question waits, never the alert
        self.shown.clear()
        self.sent.clear()
        self.screen(("まだですか", "10:40", "田中", CHAT_A))
        self.cycle(10, 45)                                                                     # the judge merges the chase into item 1
        item = self.approvals()["items"]["1"]
        self.assertEqual(item["revision"], 2)
        self.assertTrue(any("まだですか" in str(r.get("text")) for r in item["record"]["followups"]))
        sender = read_json_file(self.out / "attention.json")["senders"]
        self.assertEqual([(r["name"], r["why"]) for r in sender], [("田中", "words")])
        self.assertEqual(attention.waiting(self.out), set())                                   # her item no longer waits for the sitting
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件", "#1")])                        # it is announced in the cycle that lists her
        self.shown.clear()
        self.sent.clear()
        self.screen(("まだですか", "10:40", "田中", CHAT_A), ("これは何ですか", "11:35", "田中", CHAT_B))
        self.cycle(11, 35)
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件", "#3")])                        # her next item: at once, before 13:00
        self.assertEqual(self.sent, [("webhook", "kimeru: 確認待ち 1 件（#3）。Teams の自分とのチャットを確認してください")])
        self.shown.clear()
        self.sent.clear()
        self.screen(("まだですか", "10:40", "田中", CHAT_A), ("これは何ですか", "11:35", "田中", CHAT_B),
                    ("これは何ですか", "11:50", "佐藤", CHAT_D))
        self.cycle(11, 50)
        self.assertEqual((self.shown, self.sent), ([], []))                                    # another sender still waits
        self.assertEqual(attention.waiting(self.out), {4})
        self.alert("s2")
        self.cycle(11, 55)                                                                     # a Sev1 alert is not held while she is listed either
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件", "#5")])
        self.assertEqual(attention.waiting(self.out), {4})
        for title, body in self.shown:
            self.assertNotIn("田中", title + body)
        for _, text in self.sent:
            self.assertNotIn("田中", text)

    def test_a_follow_up_without_a_chasing_word_is_announced_as_any_follow_up_but_lists_nobody_so_her_next_item_waits(self):
        self.sittings()
        self.screen(("おはようございます", "9:00", "田中", CHAT_A))
        self.cycle(9, 5)
        self.screen(("これは何ですか", "10:15", "田中", CHAT_A))
        self.cycle(10, 20)
        self.screen(("資料を追加しました", "10:21", "田中", CHAT_A))
        self.cycle(10, 25)
        self.assertEqual(self.approvals()["items"]["1"]["revision"], 2)
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件", "#1")])     # a follow-up is announced at once, as before R37
        self.assertFalse((self.out / "attention.json").exists())          # but two quick lines are no chase: nobody is listed
        self.shown.clear()
        self.screen(("資料を追加しました", "10:21", "田中", CHAT_A), ("これは何ですか", "11:35", "田中", CHAT_B))
        self.cycle(11, 35)
        self.assertEqual(self.shown, [])                                    # her next item waits for the sitting
        self.assertEqual(attention.waiting(self.out), {2})
        self.cycle(13, 5)
        self.assertEqual(self.shown, [("kimeru: 確認待ち 1 件（まとめ）", "#2")])


if __name__ == "__main__":
    unittest.main()
