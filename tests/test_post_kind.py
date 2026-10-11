"""R12: a post whose only wait is a writer's draft says so in its first line (setting post_kind_mark=1), and the morning brief,
the toast and the week digest split the waiting items into judgments and draft checks. At 0 (the default) every text is the same
as before, and the push message never changes."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru import brief, cli, config, graph, notify, plan, push, stats
from kimeru.backends import StubBackend
from kimeru.cli import process
from tests.test_notify import REC, FakeBridge
from tests.test_writer import MSG, FakeTeams, FakeWriter

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
ON = {"KIMERU_POST_KIND_MARK": "1"}   # the setting for one test; os.environ is restored after it

# the texts at the default (0), written down before the setting existed
GOLD_POST = ("[kimeru #1] 佐藤さん: 来月のリリース日をずらすか判断をお願いします\n"
             "→ スケジュール変更の判断: 1.現行日程に依存するチーム・顧客を確認（今日） → 2.選択肢（延期・スコープ縮小・現状維持）を比較（今日） → 3.日程を決定し記録（今日） → 4.関係者へ変更を通知…\n"
             "案: 佐藤さん、承知しました。\n"
             "OK 1 / NG 1 / 修正 1 <点> / 詳細 1")
GOLD_BRIEF = ("[kimeru brief 2026-10-12] 今日の進め方\n"
              "1. 確認待ち #1: 本日中に判断が必要\n"
              "2. 確認待ち #2: 本日中に判断が必要\n"
              "3. 確認待ち #3: 内容を確認してください\n"
              "確認待ち 3 件（OK 番号 / NG 番号 / 詳細 番号 で返信）")
GOLD_PUSH = "kimeru: 確認待ち 3 件（#1, #2, #3）。Teams の自分とのチャットを確認してください"
GOLD_ADVICE_POST = "[kimeru #3] 内容を確認してください\n→ 内容を確認してください\nOK 3 / NG 3 / 詳細 3"
GOLD_DRAFT_POST = "[kimeru #1] 佐藤さん: 判断をお願いします\n→ 本日中に判断が必要\n案: 佐藤さん、承知しました。\nOK 1 / NG 1 / 修正 1 <点> / 詳細 1"

# a decision that waits for a writer's draft (no queue in its graph, so the draft is what holds it)
DRAFT = {"graph": "teams-chat-triage", "event_kind": "teams.chat", "event_id": "201", "node": "decision_today",
         "outcome": "decide", "needs_human": True, "notify": True, "advice": "本日中に判断が必要",
         "event": {"kind": "teams.chat", "id": "201", "chat_id": "19:c1", "author": "佐藤", "text": "判断をお願いします"},
         "actions": [{"type": "teams.reply", "chat_id": "19:c1", "text": "佐藤さん、承知しました。", "drafted_by": "fake"}],
         "held_reason": "draft"}
# a judgment the model asked the PM about (advise, queued): no draft, so no draft reason
ADVICE = {"graph": "teams-chat-triage", "event_kind": "teams.chat", "event_id": "202", "node": "ask_pm",
          "outcome": "advise", "needs_human": True, "advice": "内容を確認してください", "actions": []}
EV = {"kind": "teams.chat", "id": "m1", "chat_id": "19:c1", "author": "佐藤", "text": "判断をお願いします"}


def queue_of(out, *recs):
    (Path(out) / "queue.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")


def waiting_three(out):
    """Three items wait for the PM (numbers 1, 2, 3): two of them only for a writer's draft, one a judgment."""
    queue_of(out, DRAFT, {**DRAFT, "event_id": "203"}, ADVICE)
    notify.notify(out, FakeBridge(), send=False)


def write_decisions(out):
    """A week of decisions: two that wait for a draft, one judgment that waits for the PM, one automatic."""
    at = (NOW - timedelta(days=1)).isoformat(timespec="seconds")
    base = {"graph": "teams-chat-triage", "event_kind": "teams.chat", "node": "decision_today", "outcome": "decide",
            "notify": False, "at": at, "judge": {"name": "Kev", "model": "m-1"}}
    rows = [{**base, "event_id": "1", "needs_human": True, "held_reason": "draft"},
            {**base, "event_id": "2", "needs_human": True, "held_reason": "draft"},
            {**base, "event_id": "3", "needs_human": True},
            {**base, "event_id": "4", "needs_human": False}]
    (Path(out) / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def judged(res_in):
    """What cli._draft makes of a judgment, with a writer that drafts its reply."""
    res = {"graph": "g", "event_id": "m1", "event_kind": "teams.chat", "node": "n", "path": [], "advice": "x",
           "actions": [{"type": "teams.reply", "chat_id": "19:c1", "text": "受領しました。"}], "outcome": "decide", **res_in}
    out, _ = cli._draft(res, EV, cli.Meter(StubBackend()), FakeWriter())
    return out


class TestDefaultTextsAreUnchanged(unittest.TestCase):
    def test_the_draft_post_is_the_same_as_before_the_setting_existed(self):
        with tempfile.TemporaryDirectory() as d:
            out, t = Path(d), FakeTeams()
            process(MSG, GRAPHS, StubBackend(), out, PBS, writer=FakeWriter())
            notify.notify(out, t, send=True)
            self.assertEqual(t.posts, [GOLD_POST])
            self.assertEqual(t.timeline, ["P:1"])

    def test_the_brief_toast_and_push_say_what_they_said_before(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            waiting_three(out)
            text, _ = brief.build(out, StubBackend(), now=NOW, date="2026-10-12")
            self.assertEqual(text, GOLD_BRIEF)
            self.assertEqual(notify.toast_text(out, [1, 2, 3], 0, 0), ("kimeru: 確認待ち 3 件", "#1, #2, #3"))
            self.assertEqual(push.message([1, 2, 3], 0, 0), GOLD_PUSH)

    def test_the_posts_of_a_judgment_and_of_a_draft_are_the_same_as_before(self):
        self.assertEqual(notify.format_post(3, ADVICE), GOLD_ADVICE_POST)
        self.assertEqual(notify.format_post(1, DRAFT), GOLD_DRAFT_POST)

    def test_the_week_digest_has_no_draft_line_at_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            write_decisions(out)
            self.assertNotIn("下書きの確認", stats.week(out, now=NOW))


class TestDraftWaitIsRecorded(unittest.TestCase):
    def test_a_draft_on_a_decision_without_a_queue_is_recorded_as_a_draft_wait(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            [res] = process(MSG, GRAPHS, StubBackend(), out, PBS, writer=FakeWriter())
            self.assertEqual((res["needs_human"], res.get("held_reason")), (True, "draft"))
            [q] = [json.loads(l) for l in (out / "queue.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(q["held_reason"], "draft")
            [row] = [json.loads(l) for l in (out / "decisions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(row["held_reason"], "draft")

    def test_a_judgment_with_a_draft_is_never_a_draft_wait(self):
        advice = judged({"outcome": "advise", "node": "ask_pm"})
        self.assertNotIn("held_reason", advice)

    def test_a_queued_decision_keeps_its_queue_as_the_reason(self):
        self.assertNotIn("held_reason", judged({"needs_human": True}))

    def test_a_severe_or_unsure_decision_is_never_a_draft_wait(self):
        severe = judged({"notify": True, "severe_guard": "severity=Sev0"})
        unsure = judged({"notify": True, "via_unsure": ["decision_today"]})
        self.assertNotIn("held_reason", severe)
        self.assertNotIn("held_reason", unsure)
        self.assertTrue(severe["needs_human"])   # it still waits for the PM, as before
        self.assertEqual(judged({"notify": True})["held_reason"], "draft")


class TestDraftMarkOnTheSettingOne(unittest.TestCase):
    def test_the_first_line_says_draft_and_the_reader_still_finds_the_number(self):
        with mock.patch.dict(os.environ, ON), tempfile.TemporaryDirectory() as d:
            out, t = Path(d), FakeTeams()
            process(MSG, GRAPHS, StubBackend(), out, PBS, writer=FakeWriter())
            notify.notify(out, t, send=True)
            lines = t.posts[0].splitlines()
            self.assertEqual(lines[0], "[kimeru #1] 下書き: 佐藤さん: 来月のリリース日をずらすか判断をお願いします")
            self.assertTrue(lines[1].startswith("→ 文面だけ確認（判断は自動: スケジュール変更の判断"))
            self.assertEqual(lines[2:], GOLD_POST.splitlines()[2:])   # the draft and the reply line: unchanged
            self.assertIsNotNone(re.match(r"^\[kimeru #(\d+)\]", lines[0]))
            self.assertEqual(t.timeline, ["P:1"])

    def test_a_judgment_post_is_the_same_at_both_settings(self):
        default_rec = notify.format_post(3, REC)
        with mock.patch.dict(os.environ, ON):
            self.assertEqual(notify.format_post(3, ADVICE), GOLD_ADVICE_POST)
            self.assertEqual(notify.format_post(3, REC), default_rec)
            self.assertNotIn("下書き:", notify.format_post(3, ADVICE))

    def test_a_severe_or_unsure_result_never_says_draft_in_its_post(self):
        with mock.patch.dict(os.environ, ON):
            for extra in ({"severe_guard": "severity=Sev0"}, {"via_unsure": ["decision_today"]}):
                post = notify.format_post(1, {**DRAFT, **extra, "held_reason": None})
                self.assertNotIn("下書き:", post, extra)
                self.assertNotIn("文面だけ確認", post, extra)

    def test_the_brief_splits_the_waiting_items_into_judgments_and_draft_checks(self):
        with mock.patch.dict(os.environ, ON), tempfile.TemporaryDirectory() as d:
            out = Path(d)
            waiting_three(out)
            text, _ = brief.build(out, StubBackend(), now=NOW, date="2026-10-12")
            self.assertIn("判断が必要 1 / 下書きの確認 2（OK 番号 / NG 番号 / 詳細 番号 で返信）", text)
            self.assertNotIn("確認待ち 3 件", text)

    def test_the_toast_counts_the_draft_checks_inside_the_waiting_count(self):
        with mock.patch.dict(os.environ, ON), tempfile.TemporaryDirectory() as d:
            out = Path(d)
            waiting_three(out)
            self.assertEqual(notify.toast_text(out, [1, 2, 3], 0, 0), ("kimeru: 確認待ち 3 件（うち下書き 2）", "#1, #2, #3"))

    def test_the_push_message_is_the_same_at_the_setting_one(self):
        with mock.patch.dict(os.environ, ON):
            self.assertEqual(push.message([1, 2, 3], 0, 0), GOLD_PUSH)

    def test_the_week_digest_gives_the_two_numbers_at_the_setting_one(self):
        with mock.patch.dict(os.environ, ON), tempfile.TemporaryDirectory() as d:
            out = Path(d)
            write_decisions(out)
            self.assertIn("判断が必要 1 件 / 下書きの確認 2 件", stats.week(out, now=NOW))

    def test_the_setting_is_an_on_off_switch_that_is_off_by_default_and_logged_like_post_reason(self):
        self.assertEqual(config.SETTINGS["post_kind_mark"], ("KIMERU_POST_KIND_MARK", "0"))
        self.assertEqual(config.CHOICES["post_kind_mark"], ("0", "1"))
        self.assertIn("post_kind_mark", config.LOGGABLE)
        self.assertIn("post_kind_mark", config.SHARE_VALUES)
        with mock.patch.dict(os.environ, {"KIMERU_POST_KIND_MARK": "on"}):
            self.assertEqual(config.value("post_kind_mark"), "1")
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        self.assertEqual(ref.count("| `KIMERU_POST_KIND_MARK` |"), 1)


if __name__ == "__main__":
    unittest.main()
