"""R30: the replies `着手 N` / `完了 N` / `完了 N k` / `延期 N <いつ>` change the work of an approved case, and the deadline post
`[kimeru 期限 #N ...]` (once a day, from nudge_hour) lists the work that is due. Only with promises=1; with the default 0 nothing is read or
written, and the posts, the push text and the replies stay as they were (the golden strings below were taken before R30).
The clock is injected (`now`); the chat is a fake, nothing leaves the test."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from kimeru import brief, config, fsutil, notify, promises, push, work
from kimeru.backends import StubBackend
from tests.test_notify import REC, FakeBridge, write_queue

NOW = datetime(2026, 10, 12, 9, 0)                                   # naive local: the cycle's clock (a Monday)
NOW_LOCAL = NOW.astimezone()                                         # the same clock, aware (the brief takes an aware time)
AT = datetime(2026, 10, 12, 8, 0).astimezone().isoformat(timespec="seconds")   # the record's time: local, the same day
REC2 = {**REC, "event_id": "4813", "advice": "別の件"}
GOLDEN_POST = "[kimeru #278] ADO #4812\n→ 優先度を判定できないチケット #4812\nOK 278 / NG 278 / 詳細 278"
GOLDEN_NOTICE = "[kimeru 通知] ADO #4812\n→ 優先度を判定できないチケット #4812\n下書きは別の投稿で承認待ちです"
GOLDEN_PUSH = [
    "kimeru: 確認待ち 1 件（#3）。Teams の自分とのチャットを確認してください",
    "kimeru: 自動決定の通知 1 件 / 投稿できていない確認待ち 2 件 / 未送信 1 件（#4）。Teams の自分とのチャットを確認してください",
    "kimeru: 確認待ち 2 件（#3, #5） / 自動決定の通知 2 件 / 投稿できていない確認待ち 1 件 / 未送信 2 件（#4, #6）。Teams の自分とのチャットを確認してください",
]
GOLDEN_BRIEF = ("[kimeru brief 2026-10-12] 今日の進め方\n"               # the morning brief of the approved case (captured before the review fix)
                "1. 一次回答: 一次回答の下書き（今日）\n"
                "2. 一次回答: 担当者へ確認を依頼（今日）\n"
                "\n"
                "承認済み・未完了の作業:\n"
                "- #1 優先度を判定できないチケット #4812")


def plan_record(event_id="4812"):
    """An approved case with three plan steps: two due today (今日), one later (次スプリント以降)."""
    return {**REC, "event_id": event_id, "at": AT, "plan": {
        "playbook": "pb", "title": "一次回答", "first": "一次回答の下書き", "summary": "",
        "steps": [{"id": "s1", "title": "一次回答の下書き", "need": 1.0, "due": "今日", "due_level": 0},
                  {"id": "s2", "title": "担当者へ確認を依頼", "need": 1.0, "due": "今日", "due_level": 0},
                  {"id": "s3", "title": "来月の計画を見直す", "need": 0.9, "due": "次スプリント以降", "due_level": 2}]}}


class Chat(FakeBridge):
    """The self chat: `timeline` is what the reader hands back; `reads` counts the reads, `posts` what was posted."""

    def __init__(self, timeline=()):
        super().__init__()
        self.timeline = list(timeline)
        self.reads = 0

    def read(self):
        self.reads += 1
        return {"ok": True, "timeline": list(self.timeline)}


def run(out, chat, now=NOW):
    return notify.collect(out, chat, real=True, send=True, now=now)


def approved_case(d, records=None):
    out = Path(d) / "out"
    out.mkdir(parents=True, exist_ok=True)
    write_queue(out, *(records or [plan_record()]))
    notify.notify(out, FakeBridge(), send=True)
    notify.collect(out, Chat(["P:1", "R:OK 1"]), real=True, send=True, now=NOW)
    return out


def journal(out):
    p = Path(out) / "work-state.jsonl"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def item(out, n=1):
    return json.loads((Path(out) / "approvals.json").read_text(encoding="utf-8"))["items"][str(n)]


def notices(bridge):
    return [text for text, _ in bridge.posts if text.startswith("[kimeru 期限")]


def folder(out):
    """Every file of the state folder, by name and bytes: a cycle that writes nothing leaves this equal."""
    return {p.name: p.read_bytes() for p in sorted(Path(out).iterdir()) if p.is_file()}


def brief_text(out, when):
    return brief.build(out, StubBackend(), now=when.astimezone(), date=when.date().isoformat())[0]


class TestTheReplyForms(unittest.TestCase):
    def test_each_work_reply_form_is_read_and_a_step_number_is_kept(self):
        self.assertEqual(notify.parse_work("完了 3 2"), ("完了", "3", 2))
        self.assertEqual(notify.parse_work("完了 3"), ("完了", "3", None))
        self.assertEqual(notify.parse_work("着手 3"), ("着手", "3", None))
        self.assertEqual(notify.parse_work("延期 3 金"), ("延期", "3", "金"))
        self.assertEqual(notify.parse_work("完了　＃３　２。"), ("完了", "3", 2))
        self.assertEqual(notify.parse_work("延期 3 金曜。"), ("延期", "3", "金曜"))

    def test_words_that_are_not_work_replies_are_not_read(self):
        for line in ("着手 3 2", "済 3", "完了", "延期 3", "完了 3 2 3", "終わった 3", "保留 3 金", "OK 3"):
            self.assertIsNone(notify.parse_work(line), line)

    def test_the_reader_line_of_a_deadline_post_is_a_boundary_only_for_the_work_replies_below_it(self):
        self.assertEqual(notify.fresh_work({"timeline": ["P:1", "R:完了 1"]}), [("1", ("完了", "1", None), "完了 1")])
        self.assertEqual(notify.fresh_work({"timeline": ["K:1", "R:着手 1"]}), [("1", ("着手", "1", None), "着手 1")])
        self.assertEqual(notify.fresh_work({"timeline": ["R:完了 1"]}), [])                           # no boundary on the screen
        self.assertEqual(notify.fresh_work({"timeline": ["P:1", "R:完了 1", "K:1"]}), [])              # above the latest post
        self.assertEqual(notify.fresh_work({"timeline": ["P:1", "R:完了 1", "K:2"]}),                  # a post of another number
                         [("1", ("完了", "1", None), "完了 1")])
        self.assertEqual(notify.fresh_work({"timeline": ["T:1", "R:完了 1"]}), [])                    # a test post is no boundary

    def test_the_test_reader_never_takes_the_deadline_post_as_a_boundary(self):
        tl = ["P:1", "K:1", "R:完了 1"]
        self.assertNotIn("K:1", notify.scoped_timeline({"timeline": tl}, test=True))
        self.assertEqual(notify.scoped_timeline({"timeline": tl}, test=False), tl)


class TestDefaultIsUnchanged(unittest.TestCase):
    """promises=0 (the default): the posts, the notices and the push text are the same as before R30 (golden)."""

    def test_the_post_notice_and_push_texts_are_byte_identical(self):
        self.assertEqual(notify.format_post(278, REC), GOLDEN_POST)
        self.assertEqual(notify.format_notice(REC), GOLDEN_NOTICE)
        self.assertEqual(push.message([3], 0, 0), GOLDEN_PUSH[0])
        self.assertEqual(push.message([], 1, 2, [4]), GOLDEN_PUSH[1])
        self.assertEqual(push.message([3, 5], 2, 1, [4, 6]), GOLDEN_PUSH[2])

    def test_the_toast_text_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(notify.toast_text(d, [3], 0), ("kimeru: 確認待ち 1 件", "#3"))
            self.assertEqual(notify.toast_text(d, [], 2), ("kimeru: 自動決定の通知 2 件", "Teams の自分とのチャットを確認してください"))
            self.assertEqual(notify.toast_text(d, [3, 5], 1, sitting=True), ("kimeru: 確認待ち 2 件 / 自動決定の通知 1 件（まとめ）", "#3, #5"))

    def test_work_replies_and_the_deadline_post_do_nothing_at_promises_0(self):
        with tempfile.TemporaryDirectory() as d:
            out = approved_case(d)
            before = item(out)
            chat = Chat(["P:1", "K:1", "R:完了 1 2", "R:延期 1 金", "R:完了 1"])
            self.assertEqual(run(out, chat), [])
            self.assertEqual(chat.reads, 0)                 # nothing is waiting, so the self chat is not read at all
            self.assertEqual(item(out), before)
            self.assertEqual(journal(out), [])
            self.assertEqual(notices(chat), [])
            self.assertEqual(work.due_today_numbers(out, NOW), [])

    def test_the_setting_is_not_read_into_the_case_when_promises_is_0(self):
        self.assertEqual(config.value("promises"), "0")
        self.assertFalse(work.enabled())
        self.assertFalse(work.awaiting({"items": {}}, NOW))

    def test_the_morning_brief_of_an_approved_case_is_the_golden_text_and_a_step_done_with_k_is_not_read_at_0(self):
        with tempfile.TemporaryDirectory() as d:
            out = approved_case(d)
            self.assertEqual(brief_text(out, NOW), GOLDEN_BRIEF)
            data = json.loads((out / "approvals.json").read_text(encoding="utf-8"))
            data["items"]["1"]["work"] = {"steps": {"s2": {"done_at": AT}}}      # as if 完了 1 2 had been read at promises=1
            (out / "approvals.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(brief_text(out, NOW), GOLDEN_BRIEF)                 # at 0 both steps stay, as before R30
            self.assertEqual(work.done_steps(item(out)), frozenset())


class TestTheDeadlinePost(unittest.TestCase):
    def test_the_deadline_post_lists_the_steps_due_today_once_a_day(self):
        with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}), tempfile.TemporaryDirectory() as d:
            out = approved_case(d)
            chat = Chat(["P:1"])
            self.assertEqual(run(out, chat), [])
            self.assertEqual(chat.reads, 1)                  # the due day: the self chat is read, then the post is made
            posted = notices(chat)
            self.assertEqual(len(posted), 1)
            self.assertTrue(posted[0].startswith("[kimeru 期限 #1]"))
            self.assertIn("完了 1 1", posted[0])
            self.assertIn("完了 1 2", posted[0])
            self.assertNotIn("完了 1 3", posted[0])          # the later step is not due
            run(out, chat)
            self.assertEqual(len(notices(chat)), 1)          # the same day: not again
            self.assertEqual(item(out)["work"]["notice_seq"], 1)
            for text, _ in chat.posts:                        # every post of the cycle is a kimeru post (the reader's K: boundary)
                self.assertTrue(text.startswith("[kimeru"), text)

    def test_nothing_is_posted_before_nudge_hour(self):
        with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1", "KIMERU_NUDGE_HOUR": "10"}), tempfile.TemporaryDirectory() as d:
            out = approved_case(d)
            chat = Chat(["P:1"])
            run(out, chat, now=datetime(2026, 10, 12, 9, 30))
            self.assertEqual(notices(chat), [])
            run(out, chat, now=datetime(2026, 10, 12, 10, 0))
            self.assertEqual(len(notices(chat)), 1)

    def test_an_overdue_step_is_listed_again_the_next_morning_until_it_is_done(self):
        with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}), tempfile.TemporaryDirectory() as d:
            out = approved_case(d)
            chat = Chat(["P:1"])
            run(out, chat)                                    # day 1: the post lists both steps
            day1 = notices(chat)[-1]
            self.assertIn("完了 1 1", day1)
            self.assertIn("完了 1 2", day1)
            chat.timeline = ["P:1", "K:1", "R:完了 1 2"]      # the PM answers below that post
            run(out, chat, now=NOW + timedelta(days=1))       # day 2: the reply is read first, then the day's post is made
            day2 = notices(chat)[-1]
            self.assertIn("完了 1 1", day2)                    # step 1 is still open
            self.assertNotIn("完了 1 2", day2)                # step 2 was done: it is gone the next morning

    def test_a_case_done_by_hand_gets_no_deadline_post(self):
        with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}), tempfile.TemporaryDirectory() as d:
            out = approved_case(d)
            work.set_state(out, 1, "done", now=NOW)
            chat = Chat(["P:1"])
            run(out, chat)
            self.assertEqual(notices(chat), [])
            self.assertEqual(chat.reads, 0)


class TestTheReplies(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"})
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.out = approved_case(self.tmp.name)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_a_step_done_leaves_the_others_and_the_case_open(self):
        chat = Chat(["P:1", "K:1", "R:完了 1 2"])
        run(self.out, chat)
        self.assertEqual(item(self.out)["work"]["steps"].keys(), {"s2"})
        self.assertEqual(item(self.out)["work"].get("state"), None)           # the case is not done
        self.assertEqual([row.get("step") for row in journal(self.out)], ["s2"])

    def test_a_done_reply_closes_the_case_and_writes_one_journal_row_however_often_it_stays_on_the_screen(self):
        chat = Chat(["P:1", "K:1", "R:完了 1"])
        run(self.out, chat)
        run(self.out, chat)
        run(self.out, chat)
        self.assertEqual(item(self.out)["work"]["state"], "done")
        rows = journal(self.out)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["from"], rows[0]["to"]), ("Unknown", "done"))

    def test_a_postponement_is_an_absolute_day_that_does_not_move_on_later_cycles(self):
        wednesday = datetime(2026, 10, 14, 9, 0)
        friday = datetime(2026, 10, 16, 9, 0)
        chat = Chat(["P:1", "K:1", "R:延期 1 金"])
        run(self.out, chat, now=wednesday)
        self.assertEqual(item(self.out)["work"]["due"], "2026-10-16")
        run(self.out, chat, now=friday)                       # the same reply is on the screen on the Friday: "金" would now mean next week
        self.assertEqual(item(self.out)["work"]["due"], "2026-10-16")

    def test_a_reply_to_a_case_that_is_not_approved_changes_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = approved_case(tmp.name, records=[plan_record(), REC2])   # #2 is posted, not approved
        before = item(out, 2)
        chat = Chat(["P:1", "P:2", "R:完了 2", "R:完了 2 1", "R:延期 2 金"])
        self.assertEqual(run(out, chat), [])
        self.assertEqual(item(out, 2), before)
        self.assertEqual(journal(out), [])

    def test_a_missing_step_number_changes_nothing(self):
        chat = Chat(["P:1", "K:1", "R:完了 1 9"])
        run(self.out, chat)
        self.assertNotIn("steps", item(self.out)["work"])
        self.assertEqual(journal(self.out), [])

    def test_a_cli_block_is_not_undone_by_a_done_reply_still_on_the_screen(self):
        chat = Chat(["P:1", "K:1", "R:完了 1"])
        run(self.out, chat)
        work.set_state(self.out, 1, "blocked", now=NOW)
        run(self.out, chat)                                   # the reply is still above the same deadline post
        self.assertEqual(item(self.out)["work"]["state"], "blocked")
        chat.timeline = ["P:1", "R:完了 1", "K:1"]           # a newer deadline post below the reply: the reply is no longer new
        run(self.out, chat)
        self.assertEqual(item(self.out)["work"]["state"], "blocked")
        self.assertEqual(len(journal(self.out)), 2)           # the reply's row and the CLI's row, nothing more

    def test_a_reply_outside_a_dry_run_changes_nothing(self):
        chat = Chat(["P:1", "K:1", "R:完了 1"])
        notify.collect(self.out, chat, real=False, send=True, now=NOW)
        notify.collect(self.out, chat, real=True, send=False, now=NOW)
        self.assertNotIn("state", item(self.out).get("work", {}))
        self.assertEqual(notices(chat), [])
        self.assertEqual(chat.reads, 0)

    def test_a_dry_run_or_a_cycle_without_send_leaves_every_file_of_the_state_folder_as_it_was(self):
        chat = Chat(["P:1", "K:1", "R:完了 1 2", "R:完了 1"])
        before = folder(self.out)
        notify.collect(self.out, chat, real=False, send=True, now=NOW)
        notify.collect(self.out, chat, real=True, send=False, now=NOW)
        self.assertEqual(folder(self.out), before)
        self.assertEqual(chat.reads, 0)

    def test_a_busy_approvals_lock_changes_no_step_and_the_reply_is_read_on_the_next_free_cycle(self):
        chat = Chat(["P:1", "K:1", "R:完了 1 2"])
        with fsutil.exclusive(notify.lock_path(self.out)) as got:
            self.assertTrue(got)
            res = notify.collect(self.out, chat, real=True, send=True, now=NOW)
            with self.assertRaises(work.WorkError):
                work.set_state(self.out, 1, "done", now=NOW)
        self.assertTrue(res.busy)                              # nothing was read while the lock was held
        self.assertEqual(chat.reads, 0)
        self.assertNotIn("steps", item(self.out).get("work", {}))
        self.assertEqual(journal(self.out), [])
        run(self.out, chat)                                    # the lock is free: the same reply is applied once
        self.assertEqual(item(self.out)["work"]["steps"].keys(), {"s2"})
        self.assertEqual(len(journal(self.out)), 1)

    def test_the_self_chat_is_read_only_inside_the_window_after_a_deadline_post(self):
        chat = Chat(["P:1"])
        run(self.out, chat)                                   # the deadline post of the day is made
        chat.timeline = ["P:1", "K:1", "R:完了 1 1", "R:完了 1 2"]   # then both steps are answered below it: nothing is due any more
        run(self.out, chat)
        self.assertEqual(item(self.out)["work"]["steps"].keys(), {"s1", "s2"})
        chat.reads = 0
        run(self.out, chat, now=NOW + timedelta(hours=2))
        self.assertEqual(chat.reads, 1)
        chat.reads = 0
        run(self.out, chat, now=NOW + timedelta(hours=25))
        self.assertEqual(chat.reads, 0)

    def test_the_stored_notice_number_is_kept_on_the_case_and_the_day_on_the_file(self):
        chat = Chat(["P:1"])
        run(self.out, chat)
        data = json.loads((self.out / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual(data[work.NOTICE_DAY], "2026-10-12")
        self.assertEqual(item(self.out)["work"]["notice_day"], "2026-10-12")


class TestAStepDoneWithKLeavesTheBriefAndTheLedger(unittest.TestCase):
    """`完了 N k` marks one step of an approved case done. The next morning the brief's step lines and the 約束 section (the promises
    ledger's (c) rows) lose that step and nothing else; the case itself stays open. With promises=1 only."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"})
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.out = approved_case(self.tmp.name)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_after_done_1_2_the_brief_and_the_promise_section_lose_only_step_2_the_next_morning(self):
        day2 = NOW + timedelta(days=1)
        chat = Chat(["P:1"])
        run(self.out, chat)                                       # day 1: the deadline post lists steps 1 and 2
        chat.timeline = ["P:1", "K:1", "R:完了 1 2"]
        before, before_section = brief_text(self.out, day2), promises.brief_section(self.out, day2)
        self.assertIn("担当者へ確認を依頼", before)                # control: nothing is done yet on day 2
        self.assertIn("約束: 超過 2 件", before_section)

        run(self.out, chat, now=day2)                             # day 2: the reply is read, then the day's post is made
        self.assertEqual(item(self.out)["work"]["steps"].keys(), {"s2"})
        self.assertIsNone(item(self.out)["work"].get("state"))    # the case is still open
        self.assertIn("完了 1 1", notices(chat)[-1])
        self.assertNotIn("完了 1 2", notices(chat)[-1])

        after = brief_text(self.out, day2)
        self.assertIn("一次回答の下書き", after)                   # step 1 stays
        self.assertNotIn("担当者へ確認を依頼", after)              # step 2 is gone
        self.assertNotIn("来月の計画を見直す", after)              # the later step was never in the brief
        self.assertIn("- #1 ", after)                             # the case is still listed as open work

        after_section = promises.brief_section(self.out, day2)
        self.assertIn("約束: 超過 1 件", after_section)
        self.assertIn("#1「一次回答の下書き」", after_section)
        self.assertNotIn("担当者へ確認を依頼", after_section)

    def test_a_step_done_with_k_does_not_come_back_on_a_later_day(self):
        chat = Chat(["P:1", "K:1", "R:完了 1 2"])
        run(self.out, chat)                                       # the reply is read first (no post yet), then day 1's post
        for days in (1, 2):
            run(self.out, chat, now=NOW + timedelta(days=days))
            self.assertNotIn("担当者へ確認を依頼", brief_text(self.out, NOW + timedelta(days=days)))
            self.assertNotIn("担当者へ確認を依頼", promises.brief_section(self.out, NOW + timedelta(days=days)))
        self.assertEqual(item(self.out)["work"]["steps"].keys(), {"s2"})
        self.assertEqual(len(journal(self.out)), 1)


class TestPushAndSettings(unittest.TestCase):
    def test_the_push_text_has_the_numbers_of_the_day_only(self):
        self.assertEqual(push.message([], 0, 0, due_today=[3, 5]),
                         "kimeru: 今日が期限 2 件（#3, #5）。Teams の自分とのチャットを確認してください")

    def test_the_nudge_hour_is_a_whole_hour_and_a_bad_value_is_the_default(self):
        with mock.patch.dict(os.environ, {"KIMERU_NUDGE_HOUR": "9"}):
            self.assertEqual(work.nudge_hour(), 9)
        with mock.patch.dict(os.environ, {"KIMERU_NUDGE_HOUR": "25"}):
            self.assertEqual(work.nudge_hour(), 8)
        with mock.patch.dict(os.environ, {"KIMERU_NUDGE_HOUR": "x"}):
            self.assertEqual(work.nudge_hour(), 8)
        self.assertEqual(config.SETTINGS["nudge_hour"], ("KIMERU_NUDGE_HOUR", "8"))


if __name__ == "__main__":
    unittest.main()
