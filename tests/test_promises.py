"""R18 (promises=1): the promises the PM made in an approved reply, the decisions asked for this week and the due steps are shown
in the morning brief, beside a new post of the same chat, and in 詳細 N. A status question from a chat with an overdue promise goes
to the PM. Read only: nothing is written, the rank is asked as before, and with promises=0 every output is byte-identical (golden)."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from kimeru import brief, cli, config, notify, plan, promises, push
from kimeru.backends import ReplayBackend, StubBackend
from tests.test_daily import GRAPHS, PBS
from tests.test_notify import FakeBridge, write_queue

ROOT = Path(__file__).resolve().parent.parent
# local times: the tests do not depend on the machine's time zone
MON = datetime(2026, 10, 12, 9, 0).astimezone()
FRI = datetime(2026, 10, 9, 9, 0).astimezone()
SUN = datetime(2026, 10, 11, 9, 0).astimezone()
WED = datetime(2026, 10, 7, 10, 0).astimezone()
TUE = datetime(2026, 10, 6, 10, 0).astimezone()
WED_DAY = date(2026, 10, 7)
ADVICE = "期限を過ぎた約束がある相手からの状況確認"


def approved_promise(text="金曜までに回答します。", chat="19:c1", observed=WED, state=None):
    """An approved item (number 2) whose reply makes a promise. The OK was observed on `observed` (None: an old item without it)."""
    at = observed or WED
    item = {"key": "teams_chat@x:teams.chat:m-0", "revision": 1, "status": "approved", "posted": True,
            "measurement": {"schema": 1, "source": "local", "generation": 1, "ready_at": at.isoformat()},
            "record": {"graph": "teams_chat", "event_kind": "teams.chat", "event_id": "m-0", "node": "plan_decision",
                       "outcome": "advise", "needs_human": True,
                       "advice": "金曜の件の回答", "at": at.isoformat(timespec="seconds"),
                       "event": {"kind": "teams.chat", "author": "佐藤", "chat_id": chat, "chat_title": "佐藤", "text": "金曜の件は？"},
                       "actions": [{"type": "teams.reply", "chat_id": chat, "text": text}]}}
    if observed is not None:
        item["measurement"]["approval_observed_at"] = observed.isoformat()
    if state:
        item["work"] = {"state": state}
    return item


def write_approvals(d, items):
    nxt = max((int(n) for n in items), default=0) + 1
    (Path(d) / "approvals.json").write_text(json.dumps({"next": nxt, "items": items}, ensure_ascii=False), encoding="utf-8")


def write_decisions(d, rows):
    (Path(d) / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def decision_later(chat="19:c9", score=1.0, at=TUE, eid="m-5"):
    """A decision_later judgment of a request to decide this week (graphs/teams_chat.json), made on `at`."""
    return {"graph": "teams_chat", "event_kind": "teams.chat", "event_id": eid, "node": "decision_later", "outcome": "decide",
            "needs_human": False, "notify": False, "at": at.isoformat(timespec="seconds"),
            "path": [{"node": "urgency", "answer": {"score": score, "confidence": 0.9}, "edge": "1.5"}],
            "event": {"kind": "teams.chat", "author": "高橋", "chat_id": chat, "chat_title": "高橋", "text": "今週中に判断してほしい件"}}


def snapshot_dir(d):
    return {p.name: p.read_bytes() for p in sorted(Path(d).iterdir()) if p.is_file()}


class TestTheDeadlineOfASentence(unittest.TestCase):
    """The commitment table: a first-person commitment verb with a deadline term. A meeting, a question, a negation or a request
    is not a promise. The due date is counted from the day of the OK."""

    CASES = [
        ("金曜までに回答します。", date(2026, 10, 9)),
        ("金曜までに回答いたします", date(2026, 10, 9)),
        ("金曜までに報告致します", date(2026, 10, 9)),
        ("木曜までに送ります", date(2026, 10, 8)),
        ("金曜までに送信します", date(2026, 10, 9)),
        ("本日中に返信します", date(2026, 10, 7)),
        ("今日中に確認します。", date(2026, 10, 7)),
        ("明日中に連絡します", date(2026, 10, 8)),
        ("明日までに対応します", date(2026, 10, 8)),
        ("今週中に判断します", date(2026, 10, 9)),
        ("来週月曜までに回答します", date(2026, 10, 12)),
        ("来週金曜までに回答します", date(2026, 10, 16)),
        ("来週の金曜までに回答します", date(2026, 10, 16)),
        ("来週の月曜までに回答します", date(2026, 10, 12)),
        ("10/15までに回答します", date(2026, 10, 15)),
        ("10月15日までに回答します", date(2026, 10, 15)),
        ("12月3日までに回答します", date(2026, 12, 3)),
        ("15日までに確認します", date(2026, 10, 15)),
        ("5日までに回答します", date(2026, 11, 5)),
        ("木曜までに返します", date(2026, 10, 8)),
        ("今週中に判断します。金曜の件です", date(2026, 10, 9)),
        ("明日の会議で話します", None),
        ("金曜の会議で回答します", None),
        ("打合せで来週金曜までに回答します", None),
        ("MTGで金曜までに回答します", None),
        ("金曜までに回答できますか？", None),
        ("金曜までに確認しますか？", None),
        ("金曜までに回答しません", None),
        ("金曜までに回答してもらえますか", None),
        ("回答は金曜までにお願いします", None),
        ("受領しました。", None),
        ("了解です、金曜までに", None),
        ("今日中に対応してほしい", None),
        ("来週の予定を教えてください", None),
        ("よろしくお願いします", None),
        ("期限は金曜までです。回答します", None),
        ("今週中の判断は難しいかもしれません", None),
        ("金曜日に話しましょう", None),
        ("来週の件、金曜までに回答します", None),   # which week the 金曜 is in is not said
        ("来月15日までに回答します", None),          # 「15日」 after 月 is not a day of this month
        ("2027年1月5日までに回答します", None),      # a year is not read: a safe miss
        ("", None),
    ]

    def test_each_sentence_gives_the_due_date_of_its_deadline_or_nothing(self):
        self.assertGreaterEqual(len(self.CASES), 20)
        for text, due in self.CASES:
            with self.subTest(text=text):
                got = promises.commitment(text, WED_DAY)
                self.assertEqual(got[0] if got else None, due)

    def test_the_label_is_the_deadline_and_the_verb_without_the_ending(self):
        self.assertEqual(promises.commitment("金曜までに回答します。", WED_DAY), (date(2026, 10, 9), "金曜までに回答"))

    def test_a_deadline_today_or_on_a_weekend_day_counts_from_the_same_day(self):
        self.assertEqual(promises.commitment("今週中に判断します", date(2026, 10, 10))[0], date(2026, 10, 10))   # a Saturday
        self.assertEqual(promises.commitment("金曜までに回答します", date(2026, 10, 9))[0], date(2026, 10, 9))

    def test_a_meeting_in_one_sentence_does_not_make_a_promise_of_the_deadline_in_the_next(self):
        self.assertIsNone(promises.commitment("明日の会議で話します。金曜までに資料を出してください", WED_DAY))

    def test_a_next_week_weekday_said_on_a_monday_is_the_monday_or_friday_of_the_week_after(self):
        mon = date(2026, 10, 12)
        self.assertEqual(promises.commitment("来週の月曜までに回答します", mon)[0], date(2026, 10, 19))
        self.assertEqual(promises.commitment("来週の金曜までに回答します", mon)[0], date(2026, 10, 23))


class TestTheLedgerReadsOnlyApprovedRepliesWithAnOkTime(unittest.TestCase):

    def test_an_approved_reply_with_a_deadline_is_a_promise_due_on_that_day(self):
        with tempfile.TemporaryDirectory() as d:
            write_approvals(d, {"2": approved_promise()})
            snap = promises.snapshot(d, MON)
            self.assertEqual(snap["today"], date(2026, 10, 12))
            [row] = snap["rows"]
            self.assertEqual((row["src"], row["n"], row["chat"], row["due"], row["label"]),
                             ("a", "2", "19:c1", date(2026, 10, 9), "金曜までに回答"))
            self.assertEqual(row["made"], date(2026, 10, 7))

    def test_an_item_without_an_approval_time_a_pending_item_and_a_closed_item_are_left_out(self):
        with tempfile.TemporaryDirectory() as d:
            write_approvals(d, {
                "1": approved_promise(observed=None),                       # an old approval: no approval_observed_at
                "2": {**approved_promise(), "status": "pending"},             # not approved yet
                "3": approved_promise(chat="19:c2", state="done"),          # the work is done
                "4": approved_promise(text="受領しました。", chat="19:c3"),   # no promise in the reply
            })
            self.assertEqual(promises.snapshot(d, MON)["rows"], [])

    def test_a_promise_older_than_the_keep_window_is_no_longer_shown(self):
        with tempfile.TemporaryDirectory() as d:
            write_approvals(d, {"2": approved_promise()})
            self.assertEqual(len(promises.snapshot(d, FRI + timedelta(days=promises.PROMISE_KEEP_DAYS))["rows"]), 1)
            self.assertEqual(promises.snapshot(d, FRI + timedelta(days=promises.PROMISE_KEEP_DAYS + 1))["rows"], [])


class TestTheMorningBriefShowsTheOpenPromises(unittest.TestCase):

    def test_an_overdue_promise_is_named_in_a_section_on_monday(self):
        with tempfile.TemporaryDirectory() as d:
            write_approvals(d, {"2": approved_promise()})
            self.assertEqual(promises.brief_section(d, MON), "約束: 超過 1 件\n・#2「金曜までに回答」: 期限切れ 3 日（佐藤）")

    def test_brief_build_with_promises_on_adds_the_section_before_the_unfinished_work(self):
        with tempfile.TemporaryDirectory() as d:
            write_approvals(d, {"2": approved_promise()})
            with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}):
                text, _ = brief.build(d, StubBackend(), now=MON, date="2026-10-12")
            self.assertIn("約束: 超過 1 件\n・#2「金曜までに回答」: 期限切れ 3 日（佐藤）", text)
            self.assertLess(text.index("約束:"), text.index("承認済み・未完了の作業:"))

    def test_a_decision_asked_this_week_is_shown_on_its_day_and_removed_two_days_later(self):
        with tempfile.TemporaryDirectory() as d:
            write_decisions(d, [decision_later()])
            with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}):
                friday, _ = brief.build(d, StubBackend(), now=FRI, date="2026-10-09")
                sunday, _ = brief.build(d, StubBackend(), now=SUN, date="2026-10-11")
            self.assertIn("判断依頼（今週中と判定）: 今日まで（高橋）", friday)
            self.assertNotIn("判断依頼", sunday)

    def test_a_decision_with_no_deadline_or_a_today_urgency_is_not_a_decision_this_week(self):
        with tempfile.TemporaryDirectory() as d:
            write_decisions(d, [decision_later(score=0.0, eid="m-1"), decision_later(score=2.0, eid="m-2")])
            self.assertEqual(promises.snapshot(d, FRI)["rows"], [])

    def test_a_due_step_of_an_approved_item_is_shown_when_it_is_overdue_and_the_unsure_or_later_ones_are_not(self):
        with tempfile.TemporaryDirectory() as d:
            item = approved_promise(text="受領しました。")
            item["record"]["at"] = TUE.isoformat(timespec="seconds")
            item["record"]["plan"] = {"playbook": "incident", "title": "障害対応", "steps": [
                {"id": "s1", "title": "責任者を指名", "due": "今日", "due_level": 0},
                {"id": "s2", "title": "影響確認", "due": plan.DUE_UNSURE, "due_level": 1},
                {"id": "s3", "title": "振り返り", "due": "次スプリント以降", "due_level": 2},
            ]}
            write_approvals(d, {"2": item})
            rows = promises.snapshot(d, MON)["rows"]
            self.assertEqual([(r["src"], r["label"], r["due"]) for r in rows], [("c", "責任者を指名", date(2026, 10, 6))])
            self.assertEqual(promises.brief_section(d, MON), "約束: 超過 1 件\n・#2「責任者を指名」: 期限切れ 6 日（佐藤）")


class TestTheSameChatsNewPostAndItsDetail(unittest.TestCase):

    def new_post(self, chat="19:c1"):
        return {"graph": "teams_chat", "event_kind": "teams.chat", "event_id": "m-9", "node": "plan_decision",
                "advice": "金曜の件の進め方を確認したい", "at": MON.isoformat(timespec="seconds"),
                "actions": [{"type": "teams.reply", "chat_id": chat, "text": "確認して返信します"}],
                "event": {"kind": "teams.chat", "author": "佐藤", "chat_id": chat, "chat_title": "佐藤", "text": "金曜の件、進め方を教えてください"}}

    def test_the_arrow_line_of_a_new_post_from_the_same_chat_ends_with_the_promise(self):
        with tempfile.TemporaryDirectory() as d:
            write_approvals(d, {"2": approved_promise()})
            note = promises.post_note(promises.snapshot(d, MON), self.new_post(), 3)
            self.assertEqual(note, "（約束: 10/7 #2「金曜までに回答」 期限切れ 3 日）")
            body = notify.format_post(3, self.new_post(), promise=note)
            plain = notify.format_post(3, self.new_post())
            self.assertEqual(body.splitlines()[1], "→ 金曜の件の進め方を確認したい " + note)
            self.assertEqual(len(body.splitlines()), len(plain.splitlines()))

    def test_another_chat_or_the_items_own_promise_adds_no_note(self):
        with tempfile.TemporaryDirectory() as d:
            write_approvals(d, {"2": approved_promise()})
            snap = promises.snapshot(d, MON)
            self.assertIsNone(promises.post_note(snap, self.new_post(chat="19:other"), 3))
            self.assertIsNone(promises.post_note(snap, self.new_post(), 2))
            self.assertIsNone(promises.post_note(None, self.new_post(), 3))

    def test_a_detail_reply_in_the_same_chat_shows_the_promise_only_when_the_setting_is_on(self):
        from tests.test_notify import TimelineBridge
        for flag, shown in (("1", True), ("0", False)):
            with tempfile.TemporaryDirectory() as d:
                pending = {"key": "teams_chat@x:teams.chat:m-9", "revision": 1, "status": "pending", "posted": True,
                           "record": self.new_post()}
                write_approvals(d, {"2": approved_promise(), "3": pending})
                bridge = TimelineBridge(["P:3", "R:詳細 3"])   # a reply counts only after the visible post of #3
                with mock.patch.dict(os.environ, {"KIMERU_PROMISES": flag}):
                    notify.collect(d, bridge, send=False, now=MON)
                details = [t for t, _ in bridge.posts if t.startswith("[kimeru 詳細 #3]")]
                self.assertEqual(len(details), 1, flag)
                self.assertEqual("この相手の約束:" in details[0], shown, flag)

    def test_the_detail_lists_the_chats_open_promises_before_the_reply_line(self):
        with tempfile.TemporaryDirectory() as d:
            write_approvals(d, {"2": approved_promise()})
            lines = promises.detail_lines(promises.snapshot(d, MON), self.new_post(), 3)
            self.assertEqual(lines, ["・10/7 #2「金曜までに回答」: 期限切れ 3 日"])
            text = notify.format_detail(3, self.new_post(), None, promise=lines)
            self.assertIn("この相手の約束:\n・10/7 #2「金曜までに回答」: 期限切れ 3 日\n返信: OK 3", text)

    def test_a_new_post_through_notify_ends_with_the_overdue_promise_only_when_the_setting_is_on(self):
        """The real post path (notify.notify, which reads the ledger at the real now). The fixture is dated from the local today: the
        OK 4 days ago, a deadline 2 days ago. At 1 the arrow line ends with the promise; at 0 the post is the plain one and the ledger
        is never read."""
        now = datetime.now().astimezone()
        made, due = (now - timedelta(days=4)).date(), (now - timedelta(days=2)).date()
        reply = f"{due.month}/{due.day}までに回答します。"
        note = f"（約束: {made.month}/{made.day} #2「{due.month}/{due.day}までに回答」 期限切れ 2 日）"
        for flag in ("1", "0"):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as d:
                write_approvals(d, {"2": approved_promise(text=reply, observed=now - timedelta(days=4))})
                write_queue(d, self.new_post())
                bridge = FakeBridge()
                with mock.patch.dict(os.environ, {"KIMERU_PROMISES": flag}), \
                        mock.patch.object(promises, "snapshot", wraps=promises.snapshot) as snapshot:
                    notify.notify(d, bridge, send=False)
                [(body, _)] = bridge.posts
                self.assertEqual(snapshot.called, flag == "1")
                if flag == "1":
                    self.assertEqual(body, notify.format_post(3, self.new_post(), promise=note))
                    self.assertTrue(body.splitlines()[1].endswith(" " + note), body)
                else:
                    self.assertEqual(body, notify.format_post(3, self.new_post()))

    def test_a_decision_asked_this_week_from_the_same_chat_is_neither_a_note_nor_a_detail_line(self):
        with tempfile.TemporaryDirectory() as d:
            write_decisions(d, [decision_later(chat="19:c1")])   # due Friday; a (b) row is in the brief only
            snap = promises.snapshot(d, FRI)
            self.assertEqual([r["src"] for r in snap["rows"]], ["b"])
            self.assertIsNone(promises.post_note(snap, self.new_post(), 3))
            self.assertIsNone(promises.detail_lines(snap, self.new_post(), 3))


class TestAStatusQuestionFromAChatWithAnOverduePromise(unittest.TestCase):
    """ReplayBackend intent=status 0.9: the node stays reply_status; only needs_human and advice may change."""

    def event(self, chat):
        return {"kind": "teams.chat", "id": "m-20", "chat_id": chat, "chat_title": "佐藤", "author": "佐藤",
                "text": "金曜の件はその後どうなりましたか？", "unread": True, "mentions_me": False}

    def judge(self, out, chat, env, now=MON):
        be = ReplayBackend({"intent": {"choice": "status", "confidence": 0.9, "probabilities": {}}})
        with mock.patch.dict(os.environ, env, clear=False):
            return cli.process(self.event(chat), GRAPHS, be, out, PBS, now=now)[0]

    def test_an_overdue_promise_sends_the_status_question_to_the_pm_and_only_needs_human_and_advice_change(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            write_approvals(d, {"2": approved_promise()})
            on = self.judge(out, "19:c1", {"KIMERU_PROMISES": "1"})
            off = self.judge(out, "19:c1", {"KIMERU_PROMISES": "0"})
            self.assertEqual((on["node"], on["outcome"]), ("reply_status", "decide"))
            self.assertTrue(on["needs_human"])
            self.assertEqual(on["advice"], ADVICE)
            self.assertEqual(off["node"], "reply_status")
            self.assertFalse(off["needs_human"])
            same = ("graph", "event_id", "event_kind", "node", "outcome", "notify", "path", "actions")
            self.assertEqual({k: on[k] for k in same}, {k: off[k] for k in same})

    def test_the_pm_gets_one_queue_row_and_one_approval_item_for_it(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            write_approvals(d, {"2": approved_promise()})
            self.judge(out, "19:c1", {"KIMERU_PROMISES": "1"})
            rows = [json.loads(l) for l in (out / "queue.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            before = set(notify.Approvals(out).data["items"])
            notify.notify(out, FakeBridge(), send=False)
            added = [it for n, it in notify.Approvals(out).data["items"].items() if n not in before]
            self.assertEqual(len(added), 1)
            self.assertEqual((added[0]["status"], added[0]["record"]["node"]), ("pending", "reply_status"))

    def test_a_status_question_from_a_chat_without_an_overdue_promise_is_answered_as_before(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            write_approvals(d, {"2": approved_promise()})
            res = self.judge(out, "19:c9", {"KIMERU_PROMISES": "1"})
            self.assertEqual(res["node"], "reply_status")
            self.assertFalse(res["needs_human"])
            self.assertFalse((out / "queue.jsonl").exists())

    def test_an_overdue_decision_this_week_is_not_a_promise_for_the_status_question(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            write_decisions(d, [decision_later(chat="19:c1")])   # due Friday; on Saturday it is one day overdue, but it is no promise
            sat = datetime(2026, 10, 10, 9, 0).astimezone()
            self.assertFalse(self.judge(out, "19:c1", {"KIMERU_PROMISES": "1"}, now=sat)["needs_human"])

    def test_a_promise_that_is_not_yet_due_does_not_send_the_question_to_the_pm(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            write_approvals(d, {"2": approved_promise()})
            self.assertFalse(self.judge(out, "19:c1", {"KIMERU_PROMISES": "1"}, now=WED)["needs_human"])


class TestNothingIsWrittenAndTheRankIsAskedAsBefore(unittest.TestCase):

    def test_reading_the_ledger_and_the_brief_writes_no_byte_and_adds_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            write_approvals(d, {"2": approved_promise()})
            write_decisions(d, [decision_later()])
            before = snapshot_dir(d)
            promises.snapshot(d, FRI)
            promises.brief_section(d, MON)
            with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}):
                brief.build(d, StubBackend(), now=FRI, date="2026-10-09")
            self.assertEqual(snapshot_dir(d), before)

    def test_the_brief_asks_the_rank_once_with_promises_on_and_off(self):
        with tempfile.TemporaryDirectory() as d:
            write_approvals(d, {"2": approved_promise()})
            write_decisions(d, [decision_later()])
            for flag in ("0", "1"):
                with mock.patch.dict(os.environ, {"KIMERU_PROMISES": flag}), mock.patch.object(brief, "rank", return_value=[]) as rank:
                    brief.build(d, StubBackend(), now=MON, date="2026-10-12")
                rank.assert_called_once()

    def test_the_ledger_is_not_an_item_of_the_queue_or_of_the_approvals(self):
        with tempfile.TemporaryDirectory() as d:
            write_approvals(d, {"2": approved_promise()})
            write_decisions(d, [decision_later()])
            with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}):
                brief.build(d, StubBackend(), now=FRI, date="2026-10-09")
            self.assertFalse((Path(d) / "queue.jsonl").exists())
            self.assertEqual(sorted(notify.Approvals(d).data["items"]), ["2"])


# The default outputs of the code before R18, captured from the fixed fixture of golden_fixture (promises=0).
GOLDEN = {
    "brief": "[kimeru brief 2026-10-12] 今日の進め方\n1. 確認待ち #1: 来週の仕様変更の可否を判断してほしい\n"
             "確認待ち 1 件（OK 番号 / NG 番号 / 詳細 番号 で返信）\n\n承認済み・未完了の作業:\n- #2 金曜の件の回答",
    "ranked": [["approval:1", 1]],
    "post": "[kimeru #3] 佐藤さん: 金曜の件、進め方を教えてください\n→ 金曜の件の進め方を確認したい\n案: 確認して返信します\nOK 3 / NG 3 / 詳細 3",
    "detail": "[kimeru 詳細 #3] 判断が必要（teams_chat） / 改訂 1\n判断: 金曜の件の進め方を確認したい\n"
              "次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n"
              "不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n"
              "OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）\nteams.chat #m-9\n内容: 金曜の件の進め方を確認したい\n"
              "承認の対象: teams.reply（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n返信: OK 3 / NG 3 / 保留 3",
    "toast": ["kimeru: 確認待ち 1 件", "#1"],
    "push": "kimeru: 確認待ち 2 件（#1, #3）。Teams の自分とのチャットを確認してください",
}


def golden_fixture(out):
    """The fixed state of the golden: a pending request from chat 19:c1, the approved promise of the same chat, a decision_later row."""
    pending = {"graph": "teams_chat", "event_kind": "teams.chat", "event_id": "m-1", "node": "plan_decision", "outcome": "advise",
               "needs_human": True, "advice": "来週の仕様変更の可否を判断してほしい", "at": WED.isoformat(timespec="seconds"),
               "event": {"kind": "teams.chat", "author": "佐藤", "chat_id": "19:c1", "chat_title": "佐藤",
                         "text": "来週の仕様変更の可否を判断してほしいです"}}
    write_approvals(out, {
        "1": {"key": "teams_chat@x:teams.chat:m-1", "revision": 1, "status": "pending", "posted": True, "record": pending},
        "2": approved_promise(),
    })
    write_decisions(out, [decision_later()])


class TestTheDefaultOutputsStayTheSame(unittest.TestCase):
    """promises=0 (the default): the brief, the post, 詳細 N, the toast and the push text are byte-identical to GOLDEN, with a promise
    and a decision_later row in the state folder."""

    def outputs(self, d):
        new = {"graph": "teams_chat", "event_kind": "teams.chat", "event_id": "m-9", "node": "plan_decision", "outcome": "advise",
               "needs_human": True, "advice": "金曜の件の進め方を確認したい", "at": MON.isoformat(timespec="seconds"),
               "event": {"kind": "teams.chat", "author": "佐藤", "chat_id": "19:c1", "chat_title": "佐藤", "text": "金曜の件、進め方を教えてください"},
               "actions": [{"type": "teams.reply", "chat_id": "19:c1", "text": "確認して返信します"}]}
        text, ranked = brief.build(d, StubBackend(), now=MON, date="2026-10-12")
        on = promises.enabled()   # the same lines notify and 詳細 N add when the setting is on
        note = promises.post_note(promises.snapshot(d, MON), new, 3) if on else None
        detail = promises.detail_lines(promises.snapshot(d, MON), new, 3) if on else None
        return {"brief": text, "ranked": [[i["key"], i["rank"]] for i in ranked],
                "post": notify.format_post(3, new, promise=note), "detail": notify.format_detail(3, new, None, promise=detail),
                "toast": list(notify.toast_text(Path(d), [1], 0)), "push": push.message([1, 3], 0, 0)}

    def test_with_the_default_setting_every_output_is_the_golden_text(self):
        with tempfile.TemporaryDirectory() as d:
            golden_fixture(d)
            with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "0"}):
                self.assertEqual(self.outputs(d), GOLDEN)

    def test_the_default_setting_is_zero_and_nothing_is_read_for_the_promises(self):
        self.assertEqual(config.value("promises"), "0")
        with tempfile.TemporaryDirectory() as d:
            golden_fixture(d)
            with mock.patch.object(promises, "snapshot", side_effect=AssertionError("read at promises=0")):
                self.assertEqual(self.outputs(d), GOLDEN)

    def test_with_the_setting_on_only_the_promise_lines_are_added(self):
        with tempfile.TemporaryDirectory() as d:
            golden_fixture(d)
            with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}):
                got = self.outputs(d)
            self.assertIn("約束: 超過 1 件\n・#2「金曜までに回答」: 期限切れ 3 日（佐藤）", got["brief"])
            self.assertIn("（約束: 10/7 #2「金曜までに回答」 期限切れ 3 日）", got["post"])
            self.assertEqual(got["toast"], GOLDEN["toast"])
            self.assertEqual(got["push"], GOLDEN["push"])
            self.assertEqual(got["ranked"], GOLDEN["ranked"])


class TestTheSettingIsDeclared(unittest.TestCase):

    def test_promises_is_a_zero_or_one_setting_that_is_logged_and_shareable(self):
        self.assertEqual(config.SETTINGS["promises"], ("KIMERU_PROMISES", "0"))
        self.assertEqual(config.CHOICES["promises"], ("0", "1"))
        self.assertIn("promises", config.LOGGABLE)
        self.assertIn("promises", config.SHARE_VALUES)

    def test_the_settings_table_has_one_row_for_it_with_the_default(self):
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        rows = [l for l in ref.splitlines() if l.startswith("| `KIMERU_PROMISES`")]
        self.assertEqual(len(rows), 1)
        self.assertIn("既定 0", rows[0])


if __name__ == "__main__":
    unittest.main()
