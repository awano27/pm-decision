try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru import brief, config, daily, notify
from kimeru.backends import StubBackend
from kimeru.cli import process
from tests.test_brief_history import GOLDEN_ROWS
from tests.test_daily import GRAPHS, PBS, FakeTeams

ROOT = Path(__file__).resolve().parent.parent
DELTA = {"KIMERU_BRIEF_DELTA": "1"}
DESK = {"KIMERU_BRIEF_STYLE": "desk"}


def at(n, hour=8, minute=0):
    """The brief of day n (0 = the first day) is built at this UTC time."""
    return datetime(2026, 10, 10 + n, hour, minute, tzinfo=timezone.utc)


def day(n):
    return f"2026-10-{10 + n:02d}"


def approval(n, advice, status="pending", **record):
    """One approvals.json item: the number n, waiting (or closed by `status`)."""
    return str(n), {"key": f"g:e{n}:n", "status": status, "posted": True,
                    "record": {"graph": "g", "event_id": f"e{n}", "node": "n", "advice": advice, **record}}


def log_row(when, status, key="g:e2:n"):
    return {"at": when.isoformat(timespec="seconds"), "key": key, "id": 2, "status": status}


def step(i, title, level):
    return {"id": f"s{i}", "title": title, "due": ["今日", "今週", "次スプリント以降"][level], "due_level": level}


def decision(eid, steps, when):
    return {"graph": "g", "event_id": eid, "node": "d", "outcome": "decide", "needs_human": False,
            "at": when.isoformat(), "plan": {"playbook": "incident", "title": "障害対応の取りまとめ", "steps": steps}}


class Spy(StubBackend):
    """Counts the questions it is asked (one entry per call)."""

    def __init__(self):
        self.calls = []

    def ask(self, state, questions):
        self.calls.append(len(questions))
        return super().ask(state, questions)


class SpyJev(Spy):
    NAME = "Jev"


class Briefs(unittest.TestCase):
    """Days of morning briefs in one state folder. Each day is built and written to the history as a delivered brief is."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.since = None   # the last brief's time (daily_state brief_at): unknown before the first day

    def tearDown(self):
        self.tmp.cleanup()

    def set_items(self, *items):
        (self.out / "approvals.json").write_text(json.dumps({"items": dict(items)}, ensure_ascii=False), encoding="utf-8")

    def set_decisions(self, *rows):
        (self.out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def log(self, *rows):
        with (self.out / "approvals.log.jsonl").open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def deliver(self, n, backend=None):
        backend = backend or StubBackend()
        built = at(n)
        text, ranked = brief.build(self.out, backend, now=built, date=day(n), since=self.since)
        brief.record_history(self.out, day(n), backend, ranked)
        self.since = built.isoformat(timespec="seconds")
        return text, ranked


class TestTheSecondDayShowsContinuingAndTheClosedOne(Briefs):
    def test_the_first_day_marks_every_numbered_row_new_and_closes_nothing(self):
        self.set_items(approval(1, "依頼 A の内容"), approval(2, "依頼 B の内容"))
        with mock.patch.dict(os.environ, DELTA):
            text, _ = self.deliver(0)
        self.assertIn("直近 24 時間: OK 0 / NG 0 / 保留 0 / 修正 0\n閉じた: 0 件\n", text)
        self.assertIn("確認待ち #1: 依頼 A の内容（新）", text)
        self.assertIn("確認待ち #2: 依頼 B の内容（新）", text)

    def test_the_second_day_says_continuing_for_the_open_one_and_counts_the_one_that_closed(self):
        self.set_items(approval(1, "依頼 A の内容"), approval(2, "依頼 B の内容"))
        with mock.patch.dict(os.environ, DELTA):
            self.deliver(0)
            self.set_items(approval(1, "依頼 A の内容"), approval(2, "依頼 B の内容", status="approved"))
            self.log(log_row(at(0, 9), "approved"))
            text, _ = self.deliver(1)
        self.assertIn("前回のまとめ以降: OK 1 / NG 0 / 保留 0 / 修正 0\n閉じた: 1 件\n", text)
        self.assertIn("確認待ち #1: 依頼 A の内容（継続 2 日目）", text)
        self.assertNotIn("確認待ち #2", text)   # the closed one is no longer waiting (its unfinished work is listed apart)


class TestTheReplyCountIsTheLogRowsSinceTheLastBrief(Briefs):
    def test_the_reply_count_is_the_number_of_log_rows_since_the_last_brief(self):
        self.set_items(approval(1, "依頼 A の内容"))
        with mock.patch.dict(os.environ, DELTA):
            self.deliver(0)
            self.log(log_row(at(0, 7), "held"),                       # before the last brief: not counted
                     log_row(at(0, 9), "approved"),
                     log_row(at(0, 10), "rejected"),
                     log_row(at(0, 11), "redrafted"),
                     log_row(at(0, 12), "held"),
                     log_row(at(0, 13), "approved", key="g:999001:n"))  # the fictional trial item: not counted
            text, _ = self.deliver(1)
        self.assertIn("前回のまとめ以降: OK 1 / NG 1 / 保留 1 / 修正 1", text)


class TestTheJudgeIsAskedOnlyAboutNewItems(Briefs):
    def test_a_day_with_nothing_new_asks_the_judge_zero_times(self):
        self.set_items(approval(1, "依頼 A の内容"), approval(2, "依頼 B の内容"))
        spy = Spy()
        with mock.patch.dict(os.environ, DELTA):
            self.deliver(0, spy)
            self.deliver(1, spy)
        self.assertEqual(spy.calls, [2])   # day 0 asks for both; day 1 asks for none

    def test_only_the_new_item_is_asked_on_the_next_day(self):
        self.set_items(approval(1, "依頼 A の内容"), approval(2, "依頼 B の内容"))
        spy = Spy()
        with mock.patch.dict(os.environ, DELTA):
            self.deliver(0, spy)
            self.set_items(approval(1, "依頼 A の内容"), approval(2, "依頼 B の内容"), approval(3, "依頼 C の内容"))
            self.deliver(1, spy)
        self.assertEqual(spy.calls, [2, 1])

    def test_another_judge_gives_no_points_to_reuse(self):
        self.set_items(approval(1, "依頼 A の内容"), approval(2, "依頼 B の内容"))
        with mock.patch.dict(os.environ, DELTA):
            self.deliver(0, SpyJev())
            spy = Spy()
            self.deliver(1, spy)
        self.assertEqual(spy.calls, [2])   # the Jev points of yesterday are not reused by the stub

    def test_the_rank_is_the_same_with_the_reused_points_as_without_them(self):
        self.set_items(approval(1, "依頼 A の内容"), approval(2, "依頼 B の内容"))
        with mock.patch.dict(os.environ, DELTA):
            _, first = self.deliver(0)
            _, again = self.deliver(1)
        self.assertEqual([i["key"] for i in first], [i["key"] for i in again])
        self.assertEqual([i["harm"] for i in first], [i["harm"] for i in again])


class TestAnItemUnchangedForFourDaysIsFolded(Briefs):
    def test_a_numbered_row_unchanged_on_the_fourth_day_is_folded_and_counted_by_number(self):
        self.set_items(approval(1, "依頼 A の内容"), approval(2, "依頼 B の内容"))
        with mock.patch.dict(os.environ, DELTA):
            for n in range(3):
                text, _ = self.deliver(n)
            self.assertIn("確認待ち #1: 依頼 A の内容（継続 3 日目）", text)
            text, _ = self.deliver(3)
        self.assertIn("変わらず 2 件（#1, #2）", text)
        self.assertNotIn("確認待ち #1", text)
        self.assertNotIn("対応が必要な項目はありません", text)
        self.assertIn("確認待ち 2 件", text)   # the waiting count still counts both

    def test_a_row_whose_text_changed_is_not_folded_while_the_others_are(self):
        self.set_items(approval(1, "依頼 A の内容"), approval(2, "依頼 B の内容"))
        with mock.patch.dict(os.environ, DELTA):
            for n in range(3):
                self.deliver(n)
            self.set_items(approval(1, "依頼 A の内容（改訂）"), approval(2, "依頼 B の内容"))
            text, _ = self.deliver(3)
        self.assertIn("変わらず 1 件（#2）", text)
        self.assertIn("確認待ち #1: 依頼 A の内容（改訂）（継続 4 日目）", text)

    def test_a_row_marked_for_notice_or_severe_is_never_folded(self):
        self.set_items(approval(1, "依頼 A の内容", notify=True), approval(2, "依頼 B の内容", severe_guard=True))
        with mock.patch.dict(os.environ, DELTA):
            for n in range(3):
                self.deliver(n)
            text, _ = self.deliver(3)
        self.assertNotIn("変わらず", text)
        self.assertIn("確認待ち #1: 依頼 A の内容（継続 4 日目）", text)
        self.assertIn("確認待ち #2: 依頼 B の内容（継続 4 日目）", text)

    def test_a_step_without_a_number_is_never_folded(self):
        with mock.patch.dict(os.environ, DELTA):
            for n in range(4):
                self.set_decisions(decision("e4", [step(1, "責任者を指名", 0)], at(n, 7)))
                text, _ = self.deliver(n)
        self.assertNotIn("変わらず", text)
        self.assertIn("障害対応の取りまとめ: 責任者を指名（今日）", text)


class TestTheLinesAreNotReplies(Briefs):
    def test_no_line_of_the_delta_brief_is_read_as_a_reply(self):
        self.set_items(approval(1, "依頼 A の内容"), approval(2, "依頼 B の内容"))
        texts = []
        with mock.patch.dict(os.environ, DELTA):
            for n in range(4):
                texts.append(self.deliver(n)[0])
        self.assertTrue(any("変わらず" in t for t in texts))   # the folded line is among the checked lines
        for text in texts:
            for line in text.splitlines():
                for parser in (notify.parse_reply, notify.parse_redraft, notify.parse_paste, notify.parse_redo_k, notify.parse_redo_bad):
                    self.assertIsNone(parser(line), (parser.__name__, line))


class TestTheDeskStyle(Briefs):
    def test_desk_lists_by_severity_then_due_and_asks_the_judge_nothing(self):
        # decided on a Monday: 今週 is a later day (on a weekend 今週 is the same day, so it would count as today)
        self.set_items(approval(1, "依頼 A の内容", notify=True), approval(2, "依頼 B の内容"))
        self.set_decisions(decision("e4", [step(1, "責任者を指名", 1), step(2, "影響確認", 0)], at(2)))
        spy = Spy()
        with mock.patch.dict(os.environ, DESK):
            text, ranked = brief.build(self.out, spy, now=at(2), date=day(2))
        self.assertEqual(spy.calls, [])
        self.assertEqual(text.splitlines()[1:4], ["1. 確認待ち #1: 依頼 A の内容",
                                                  "2. 確認待ち #2: 依頼 B の内容",
                                                  "3. 障害対応の取りまとめ: 影響確認（今日）"])
        self.assertIn("ほか 1 件", text.splitlines())
        self.assertEqual([r["rank"] for r in ranked], [1, 2, 3, 4])

    def test_desk_with_the_delta_still_folds_and_asks_nothing(self):
        self.set_items(approval(1, "依頼 A の内容"))
        spy = Spy()
        with mock.patch.dict(os.environ, {**DELTA, **DESK}):
            for n in range(4):
                text, ranked = brief.build(self.out, spy, now=at(n), date=day(n))
                brief.record_history(self.out, day(n), spy, ranked)
        self.assertEqual(spy.calls, [])
        self.assertIn("変わらず 1 件（#1）", text)


class TestTheDeskCycleAsksNoJudgeAndNoWriter(Briefs):
    def cycle(self, backend, bridge, now):
        return daily.cycle(self.out, self.out / "inbox", GRAPHS, backend, PBS, process, bridge=bridge, send=True, now=now)

    def queue(self):
        (self.out / "queue.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in GOLDEN_ROWS), encoding="utf-8")

    def test_a_desk_brief_is_posted_without_the_judge_or_the_writer(self):
        self.queue()
        spy, bridge = Spy(), FakeTeams()
        with mock.patch.dict(os.environ, DESK), mock.patch("kimeru.writer.summarize_day") as summarize:
            r = self.cycle(spy, bridge, now=datetime(2026, 10, 10, 8, 10))
        briefs = [t for t, _ in bridge.posts if t.startswith("[kimeru brief")]
        self.assertEqual(len(briefs), 1)
        self.assertEqual(spy.calls, [])
        summarize.assert_not_called()   # the brief's 今日の要点 is the writer's only call the brief makes
        self.assertEqual(r["brief"], 3)

    def test_the_rank_brief_still_goes_to_the_writer_as_before(self):
        self.queue()
        with mock.patch("kimeru.writer.summarize_day") as summarize:
            self.cycle(Spy(), FakeTeams(), now=datetime(2026, 10, 10, 8, 10))
        summarize.assert_called()


class TestSettingsAndDocs(unittest.TestCase):
    def test_both_settings_are_off_or_rank_by_default_and_logged_and_shared_like_the_others(self):
        self.assertEqual(config.SETTINGS["brief_delta"], ("KIMERU_BRIEF_DELTA", "0"))
        self.assertEqual(config.SETTINGS["brief_style"], ("KIMERU_BRIEF_STYLE", "rank"))
        self.assertEqual(config.CHOICES["brief_delta"], ("0", "1"))
        self.assertEqual(config.CHOICES["brief_style"], ("rank", "desk"))
        for key in ("brief_delta", "brief_style"):
            self.assertIn(key, config.LOGGABLE)
            self.assertIn(key, config.SHARE_VALUES)
        with mock.patch.dict(os.environ, {"KIMERU_BRIEF_DELTA": "on", "KIMERU_BRIEF_STYLE": "DESK"}):
            self.assertEqual(config.value("brief_delta"), "1")
            self.assertEqual(config.value("brief_style"), "desk")
        with mock.patch.dict(os.environ, {"KIMERU_BRIEF_STYLE": "table"}):
            self.assertEqual(config.value("brief_style"), "rank")

    def test_the_reference_has_one_row_for_each_setting_and_security_names_the_desk_style(self):
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        for env in ("KIMERU_BRIEF_DELTA", "KIMERU_BRIEF_STYLE"):
            self.assertEqual(sum(1 for l in ref.splitlines() if l.startswith(f"| `{env}`")), 1, env)
        sec = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("brief_style=desk", sec)


if __name__ == "__main__":
    unittest.main()
