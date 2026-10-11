"""R09: `保留 N <いつ>` holds #N until a day; the first daily cycle on that day posts #N again under the same number (once), and nothing
is approved, rejected or executed by it. A plain `保留 N` and every other reply keep their text, the brief, the toast and the push
message byte for byte (the golden strings below were taken before the change). Teams is a fake, time is injected, nothing leaves the test."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from kimeru import brief, cli, config, daily, fsutil, hold, notify, push
from kimeru.backends import StubBackend
from kimeru.cli import process
from tests.test_daily import GRAPHS, PBS, FakeTeams
from tests.test_notify import REC, write_queue

WED = datetime(2026, 10, 14, 10, 0)     # a Wednesday
THU_BRIEF = datetime(2026, 10, 15, 8, 5)  # the Thursday after the brief hour
FRI = datetime(2026, 10, 16, 10, 0)
REC2 = dict(REC, event_id="4813", advice="別の確認待ち #4813")


def out_of(d):
    out = Path(d) / "out"
    out.mkdir(parents=True, exist_ok=True)
    return out


def item(out, n="1"):
    return json.loads((Path(out) / "approvals.json").read_text(encoding="utf-8"))["items"][n]


def put_item(out, **fields):
    """A posted, held item #1 written straight into approvals.json (for the brief and the push tests)."""
    data = {"next": 2, "items": {"1": {"key": "k1", "status": "held", "posted": True, "record": REC, **fields}}}
    (Path(out) / "approvals.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def cycle(d, t, now, send=True, brief_hour=8):
    return daily.cycle(Path(d) / "out", Path(d) / "inbox", GRAPHS, StubBackend(), PBS, process, bridge=t, send=send, now=now,
                       brief_hour=brief_hour)


def posts_of(t, number):
    return [text for text, _ in t.posts if text.startswith(f"[kimeru #{number}]")]


class TestTheHoldReplyIsRead(unittest.TestCase):
    def test_a_hold_with_a_word_is_read_from_the_reply_line(self):
        self.assertEqual(hold.parse_hold_until("保留 3 明日"), ("3", "明日"))
        self.assertEqual(hold.parse_hold_until("保留　３　金曜"), ("3", "金曜"))   # a phone's full-width text
        self.assertEqual(hold.parse_hold_until("保留 3 明日。"), ("3", "明日"))
        self.assertIsNone(hold.parse_hold_until("保留 3 。"))                      # the plain hold, not a word
        self.assertIsNone(hold.parse_hold_until("OK 3 明日"))
        self.assertIsNone(hold.parse_hold_until("保留 3 明日 です"))               # one word only

    def test_the_day_words_mean_that_day_at_the_brief_hour(self):
        cases = {"明日": datetime(2026, 10, 15, 8), "明後日": datetime(2026, 10, 16, 8),
                 "金": datetime(2026, 10, 16, 8), "金曜": datetime(2026, 10, 16, 8), "金曜日": datetime(2026, 10, 16, 8),
                 "水": datetime(2026, 10, 21, 8), "月": datetime(2026, 10, 19, 8), "来週": datetime(2026, 10, 19, 8),
                 "3日後": datetime(2026, 10, 17, 8), "7日後": datetime(2026, 10, 21, 8),
                 "10/20": datetime(2026, 10, 20, 8), "10月20日": datetime(2026, 10, 20, 8)}
        for word, want in cases.items():
            self.assertEqual(hold.parse_when(word, WED, brief_hour=8, keep_days=7), want, word)

    def test_a_friday_said_on_a_friday_is_the_friday_of_next_week(self):
        self.assertEqual(hold.parse_when("金", FRI, brief_hour=8, keep_days=7), datetime(2026, 10, 23, 8))

    def test_a_time_of_day_is_today_when_still_ahead_and_tomorrow_when_passed(self):
        self.assertEqual(hold.parse_when("15時", WED, brief_hour=8, keep_days=7), datetime(2026, 10, 14, 15))
        self.assertEqual(hold.parse_when("15:30", WED, brief_hour=8, keep_days=7), datetime(2026, 10, 14, 15, 30))
        self.assertEqual(hold.parse_when("9時", WED, brief_hour=8, keep_days=7), datetime(2026, 10, 15, 9))

    def test_a_word_that_is_not_read_or_too_far_ahead_gives_no_date(self):
        for word in ("来月", "3日", "土", "", "8日後", "10/13", "あとで"):
            self.assertIsNone(hold.parse_when(word, WED, brief_hour=8, keep_days=7), word)


class TestTheHoldFollowsTheReplies(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = out_of(self.tmp.name)
        self.t = FakeTeams()
        write_queue(self.out, REC)
        notify.notify(self.out, self.t, send=True)          # #1 is posted: the timeline has P:1
        self.assertEqual(self.t.timeline, ["P:1"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_same_reply_read_on_later_days_does_not_move_the_date(self):
        self.t.timeline.append("R:保留 1 明日")
        self.assertEqual(notify.collect(self.out, self.t, send=True, now=WED),
                         [{"id": 1, "status": "held", "until": "2026-10-15"}])
        self.assertEqual(item(self.out)["held_until"], "2026-10-15T08:00")
        for later in (datetime(2026, 10, 15, 7, 0), datetime(2026, 10, 16, 9, 0), datetime(2026, 10, 17, 9, 0)):
            self.assertEqual(notify.collect(self.out, self.t, send=True, now=later), [], later)
            self.assertEqual(item(self.out)["held_until"], "2026-10-15T08:00", later)
        self.assertEqual(item(self.out)["hold_count"], 1)

    def test_a_changed_day_is_applied_once_and_the_last_hold_reply_decides(self):
        self.t.timeline.append("R:保留 1 金")
        notify.collect(self.out, self.t, send=True, now=WED)
        self.assertEqual(item(self.out)["held_until"], "2026-10-16T08:00")
        self.t.timeline.append("R:保留 1 明日")                       # on the screen after 金: the last hold reply
        notify.collect(self.out, self.t, send=True, now=WED)
        self.assertEqual(item(self.out)["held_until"], "2026-10-15T08:00")
        notify.collect(self.out, self.t, send=True, now=WED)
        notify.collect(self.out, self.t, send=True, now=datetime(2026, 10, 15, 7, 0))
        self.assertEqual(item(self.out)["held_until"], "2026-10-15T08:00")
        self.assertEqual(item(self.out)["hold_count"], 2)              # 金, then 明日: each applied once

    def test_a_hold_reply_under_a_test_post_does_not_apply(self):
        self.t.timeline += ["T:1", "R:保留 1 明日"]
        self.assertEqual(notify.collect(self.out, self.t, send=True, now=WED), [])
        self.assertEqual(item(self.out)["status"], "pending")
        self.assertIsNone(item(self.out).get("held_until"))

    def test_a_plain_hold_after_a_day_hold_clears_the_day(self):
        self.t.timeline.append("R:保留 1 明日")
        notify.collect(self.out, self.t, send=True, now=WED)
        self.t.timeline.append("R:保留 1")
        notify.collect(self.out, self.t, send=True, now=datetime(2026, 10, 15, 7, 0))
        self.assertEqual(item(self.out)["status"], "held")
        self.assertIsNone(item(self.out).get("held_until"))
        self.assertEqual(hold.resurface(self.out, datetime(2026, 10, 20, 9, 0)), [])   # nothing comes back on its own

    def test_a_plain_hold_after_a_merge_does_not_come_back_at_once(self):
        self.t.timeline.append("R:保留 1 明日")
        notify.collect(self.out, self.t, send=True, now=WED)
        ap = notify.Approvals(self.out)
        ap.data["items"]["1"]["hold_round"] = 1                  # it came back once before
        ap.data["items"]["1"]["record"]["event"] = {"kind": "teams.chat", "id": "c1", "chat_id": "chat-1", "author": "A"}
        ap.save()
        before = item(self.out)
        ev = {"kind": "teams.chat", "id": "c2", "chat_id": "chat-1", "author": "A", "text": "続きの依頼"}
        res = {"graph": "workitem-intake", "event_id": "c2", "node": "triage_pm", "outcome": "advise", "needs_human": True,
               "advice": "続きの判断", "event": ev, "actions": []}
        self.assertTrue(cli._merge_locked(self.out, ev, res, notify, source_event=ev))   # the followup joins the held item
        merged = item(self.out)
        self.assertEqual((merged["status"], merged["posted"]), ("pending", False))
        self.assertNotIn("held_until", merged)                   # the day goes with the merge; the counts stay
        self.assertEqual((merged["held_at"], merged["hold_count"], merged["hold_round"]), (before["held_at"], 1, 1))
        notify.notify(self.out, self.t, send=True)               # posted again under #1
        self.assertEqual(len(posts_of(self.t, 1)), 2)
        self.t.timeline.append("R:保留 1")                        # a plain hold after the merge
        self.assertEqual(notify.collect(self.out, self.t, send=True, now=datetime(2026, 10, 15, 9, 0)), [{"id": 1, "status": "held"}])
        self.assertEqual(item(self.out)["status"], "held")
        self.assertNotIn("held_until", item(self.out))
        self.assertEqual(hold.resurface(self.out, datetime(2026, 10, 15, 9, 0)), [])   # nothing comes back at once
        self.assertEqual(len(posts_of(self.t, 1)), 2)            # and no post for it

    def test_a_day_hold_lasts_until_the_hour_the_cycles_brief_runs_at(self):
        self.t.timeline.append("R:保留 1 明日")
        cycle(self.tmp.name, self.t, WED, brief_hour=9)
        self.assertEqual(item(self.out)["held_until"], "2026-10-15T09:00")

    def test_a_day_beyond_the_keep_days_is_a_hold_without_a_date_and_it_is_noted(self):
        self.t.timeline.append("R:保留 1 8日後")
        self.assertEqual(notify.collect(self.out, self.t, send=True, now=WED), [{"id": 1, "status": "held", "until": None}])
        self.assertIsNone(item(self.out).get("held_until"))
        self.assertTrue(item(self.out).get("hold_note"))
        self.assertEqual(hold.resurface(self.out, datetime(2026, 10, 30, 9, 0)), [])

    def test_a_held_item_comes_back_on_its_day_and_the_old_reply_does_not_hold_it_again(self):
        self.t.timeline.append("R:保留 1 明日")
        cycle(self.tmp.name, self.t, WED)                         # the hold is read in this cycle
        self.assertEqual(item(self.out)["status"], "held")
        self.assertEqual(len(posts_of(self.t, 1)), 1)
        cycle(self.tmp.name, self.t, datetime(2026, 10, 15, 7, 50))  # before the day: nothing comes back
        self.assertEqual(len(posts_of(self.t, 1)), 1)
        ledger = (self.out / "executions.jsonl")
        before = ledger.read_bytes() if ledger.exists() else None
        r = cycle(self.tmp.name, self.t, THU_BRIEF)               # the day has come: posted again in this same cycle
        self.assertEqual(r["hold_resurfaced"], ["1"])
        again = posts_of(self.t, 1)
        self.assertEqual(len(again), 2)
        self.assertTrue(again[1].splitlines()[0].endswith("（保留から・2 日目）"), again[1])
        self.assertEqual(ledger.read_bytes() if ledger.exists() else None, before)   # a re-post writes no execution
        self.assertEqual(self.t.timeline[-1], "P:1")               # the new post is the boundary: the old reply is above it
        self.assertEqual(item(self.out)["status"], "pending")
        cycle(self.tmp.name, self.t, datetime(2026, 10, 15, 8, 12))  # the old 保留 1 明日 is not read again
        self.assertEqual(item(self.out)["status"], "pending")
        self.assertEqual(len(posts_of(self.t, 1)), 2)               # once only
        self.t.timeline.append("R:OK 1")
        r = cycle(self.tmp.name, self.t, datetime(2026, 10, 15, 8, 15))
        self.assertEqual(r["approvals"], ["#1:approved"])

    def test_a_cycle_without_send_does_not_bring_it_back(self):
        self.t.timeline.append("R:保留 1 明日")
        cycle(self.tmp.name, self.t, WED)
        cycle(self.tmp.name, self.t, THU_BRIEF, send=False)
        self.assertEqual(len(posts_of(self.t, 1)), 1)
        self.assertEqual(item(self.out)["status"], "held")
        self.assertTrue(item(self.out)["posted"])

    def test_a_busy_lock_brings_nothing_back_and_writes_nothing(self):
        put_item(self.out, held_until="2026-10-15T08:00", held_at="2026-10-14T10:00")
        before = (self.out / "approvals.json").read_text(encoding="utf-8")
        with fsutil.exclusive(notify.lock_path(self.out)) as got:
            self.assertTrue(got)
            res = hold.resurface(self.out, THU_BRIEF)
        self.assertTrue(res.busy)
        self.assertEqual((self.out / "approvals.json").read_text(encoding="utf-8"), before)

    def test_the_undated_hold_is_named_once_on_the_next_approval_post(self):
        self.t.timeline.append("R:保留 1 来月")
        notify.collect(self.out, self.t, send=True, now=WED)
        write_queue(self.out, REC, REC2)
        notify.notify(self.out, self.t, send=True)                # #2 is the next approval post
        first = posts_of(self.t, 2)[0].splitlines()[0]
        self.assertTrue(first.endswith("（保留（日付なし）: #1）"), first)
        self.assertFalse(item(self.out, "1").get("hold_note"))          # shown: not again
        with open(self.out / "queue.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(REC, event_id="4814", advice="さらに別の件"), ensure_ascii=False) + "\n")
        notify.notify(self.out, self.t, send=True)                # #3: the note was shown once
        self.assertNotIn("保留（日付なし）", posts_of(self.t, 3)[0])


class TestTheResurfacedPost(unittest.TestCase):
    def test_the_first_line_says_which_day_of_the_hold_it_is_and_nothing_else_changes(self):
        plain = notify.format_post(3, REC)
        again = notify.format_post(3, REC, marks={"day": 2})
        self.assertEqual(again.splitlines()[0], plain.splitlines()[0] + " （保留から・2 日目）")
        self.assertEqual(again.splitlines()[1:], plain.splitlines()[1:])

    def test_the_undated_note_is_on_the_first_line(self):
        self.assertEqual(notify.format_post(5, REC, marks={"undated": ["3"]}).splitlines()[0],
                         "[kimeru #5] ADO #4812 （保留（日付なし）: #3）")


class TestThePhoneIsToldOnce(unittest.TestCase):
    def test_the_re_post_is_announced_once_under_a_new_key_and_names_no_day(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"KIMERU_PUSH": "webhook"}):
            out = out_of(d)
            t = FakeTeams()
            write_queue(out, REC)
            notify.notify(out, t, send=True)
            sent = []

            def sender(route, text):
                sent.append(text)
            t0 = 1_800_000_000.0
            push.begin_cycle(out, now=t0)                       # baseline: #1 is pending, nothing old is sent
            t.timeline.append("R:保留 1 明日")
            notify.collect(out, t, send=True, now=WED)
            push.run(out, now=t0 + 400, sender=sender)           # held: the same item, nothing new
            self.assertEqual(sent, [])
            self.assertEqual(hold.resurface(out, THU_BRIEF), ["1"])
            notify.notify(out, t, send=True)                     # posted again, now under the key #1r1
            push.run(out, now=t0 + 800, sender=sender)
            push.run(out, now=t0 + 1200, sender=sender)          # and not again
            self.assertEqual(sent, ["kimeru: 確認待ち 1 件（#1）。Teams の自分とのチャットを確認してください"])


class TestTheBriefNamesADayHold(unittest.TestCase):
    def test_a_dated_hold_and_a_long_undated_hold_are_named_and_the_default_is_unchanged(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"KIMERU_HOLD_STALE_DAYS": "2"}):
            out = out_of(d)
            put_item(out, held_until="2026-10-15T08:00", held_at="2026-10-14T10:00")
            self.assertEqual([i["text"] for i in brief.collect(out, now=WED)],
                             ["保留 #1（10/15 に再表示）: 優先度を判定できないチケット #4812"])
            put_item(out, held_at="2026-10-11T09:00", hold_word=None)
            self.assertEqual([i["text"] for i in brief.collect(out, now=WED)],
                             ["長く保留: #1（3 日）: 優先度を判定できないチケット #4812"])
        with tempfile.TemporaryDirectory() as d:
            out = out_of(d)
            put_item(out, held_at="2026-10-11T09:00")            # stale 0 (the default): the same line as before
            self.assertEqual([i["text"] for i in brief.collect(out, now=WED)], ["確認待ち #1: 優先度を判定できないチケット #4812"])


class TestTheDefaultIsByteIdentical(unittest.TestCase):
    """Golden strings taken from the code before R09: a plain 保留 and every other reply must post, ack, notify and brief the same."""

    def test_the_approval_post_the_toast_the_push_and_the_brief_are_the_same_text(self):
        self.assertEqual(notify.format_post(3, REC), "[kimeru #3] ADO #4812\n→ 優先度を判定できないチケット #4812\nOK 3 / NG 3 / 詳細 3")
        self.assertEqual(notify.ack_text([{"id": 3, "status": "held"}]), ("kimeru: 返事を受け付けました", "#3 保留"))
        self.assertEqual(push.message([3], 0, 0), "kimeru: 確認待ち 1 件（#3）。Teams の自分とのチャットを確認してください")
        with tempfile.TemporaryDirectory() as d:
            out = out_of(d)
            put_item(out)
            items = brief.collect(out, now=WED)
            self.assertEqual([i["text"] for i in items], ["確認待ち #1: 優先度を判定できないチケット #4812"])
            self.assertEqual(brief.format_post(items, len(items), 1, "2026-10-12"),
                             "[kimeru brief 2026-10-12] 今日の進め方\n1. 確認待ち #1: 優先度を判定できないチケット #4812\n"
                             "確認待ち 1 件（OK 番号 / NG 番号 / 詳細 番号 で返信）")

    def test_the_setting_is_a_day_count_that_is_zero_by_default_and_is_logged_and_shared(self):
        self.assertEqual(config.SETTINGS["hold_stale_days"], ("KIMERU_HOLD_STALE_DAYS", "0"))
        self.assertIn("hold_stale_days", config.LOGGABLE)
        self.assertIn("hold_stale_days", config.SHARE_VALUES)


if __name__ == "__main__":
    unittest.main()
