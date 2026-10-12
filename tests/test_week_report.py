"""`kimeru digest --week`: the week before, the kinds that arrived, the performance rows (Jev and unnamed judges left out, on the
screen and in --share alike), the replies per item, the OKs whose proposal never changed, the weekly medians of the approval
latency and what calibrate still needs. Two weeks of synthetic rows are hand-counted here; nothing is written to the output or
the state folder, and a damaged line or a time in the future is not an error. The post, brief, toast and push texts are pinned
(golden) so that the digest cannot change them."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru import brief, calibrate, learn, notify, push, review, stats, trial
from tests.test_notify import REC
from tests.test_review_stats import MARK_ID, MARK_NAME, MARK_PATH, MARK_TEXT, Base

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
ADO_DRAFT = [{"type": "ado.comment", "id": "4812", "text": "下書きの本文"}]
TEAMS_DRAFT = [{"type": "teams.reply", "text": "下書きの本文"}]
BROKEN = "this is not json\n{\"at\": \n"


def at(days=0, seconds=0):
    return (NOW - timedelta(days=days, seconds=seconds)).isoformat(timespec="seconds")


def decision(graph, event_id, node, kind, days=1, seconds=0, judge="Kev", needs_human=False, actions=None, source="local"):
    """A line of decisions.jsonl as cli writes it. The marks must never reach a shared output: they are in its text only."""
    row = {"graph": graph, "event_id": str(event_id), "node": node, "event_kind": kind,
           "outcome": "advise" if needs_human else "decide", "needs_human": needs_human, "notify": False,
           "at": at(days, seconds), "summary": MARK_TEXT + MARK_NAME, "advice": MARK_TEXT,
           "event": {"kind": kind, "id": str(event_id), "author": MARK_NAME, "text": MARK_TEXT},
           "path": [{"node": "intent", "answer": {"choice": "decision", "confidence": 0.9}, "edge": "decision"}],
           "actions": actions or [], "measurement": {"schema": 1, "source": source, "generation": 1}}
    if judge is not None:
        row["judge"] = {"name": judge, "model": "m-1"}
    return row


def approved(n, rec, status="approved", days=1, seconds=0, latency=30, generation=1):
    """An item of approvals.json as notify keeps it: the post and the approval the PC saw, with the decision's measurement.
    Returns (number, item). The generation is written into the decision too, as notify does."""
    key = review.decision_key(rec)
    rec["measurement"] = {**rec["measurement"], "generation": generation}
    timing = {"schema": 1, "source": rec["measurement"]["source"], "generation": generation, "case_key": key,
              "ready_at": at(days + 1), "first_ready_at": at(days + 1), "posted_at": at(days, seconds + latency),
              "posting_source": "bridge_readback", "approval_observed_at": at(days, seconds)}
    return str(n), {"key": key, "status": status, "posted": True, "record": rec, "measurement": timing}


def approval_row(n, rec, status="approved", days=1, seconds=0):
    """A line of approvals.log.jsonl as `_notify_locked` writes it (call approved() for the record first: it sets the generation)."""
    key = review.decision_key(rec)
    return {"at": at(days, seconds), "key": key, "id": n, "status": status,
            "measurement": {"schema": 1, "source": rec["measurement"]["source"], "generation": rec["measurement"]["generation"],
                            "case_key": key}}


def snapshot(*roots):
    found = {}
    for root in roots:
        for p in sorted(Path(root).rglob("*")):
            if p.is_file():
                found[str(p)] = p.read_bytes()
    return found


class TwoWeeks(Base):
    """This week: eight decisions (one Jev, one with no judge name, one exactly seven days old); the week before: four (one Jev,
    one one second older than this week's start); one older, one in the future. Approvals, replies, reviews and a damaged line
    of each file are added on top."""

    def seed(self):
        k1 = decision("teams-chat-triage", MARK_ID, "decision_today", "teams.chat", days=1)
        k2 = decision("teams-chat-triage", "102", "decision_today", "teams.chat", days=2, needs_human=True)
        ado = decision("ado-workitem", "4812", "triage", "ado.workitem.created", days=3, actions=ADO_DRAFT)
        ado2 = decision("ado-workitem", "4813", "triage", "ado.workitem.created", days=3)
        j1 = decision("teams-chat-triage", "103", "decision_today", "teams.chat", days=1, judge="Jev", actions=TEAMS_DRAFT)
        u1 = decision("monitor-alert", "201", "alert", "monitor.alert", days=4, judge=None, actions=TEAMS_DRAFT)
        k4 = decision("monitor-alert", "202", "alert", "monitor.alert", days=5)
        k5 = decision("teams-chat-triage", "104", "decision_today", "teams.chat", days=7, actions=TEAMS_DRAFT)
        future = decision("teams-chat-triage", "105", "decision_today", "teams.chat", days=-1)
        p1 = decision("teams-chat-triage", "301", "decision_today", "teams.chat", days=8)
        p2 = decision("teams-chat-triage", "302", "decision_today", "teams.chat", days=9, needs_human=True)
        p3 = decision("teams-chat-triage", "303", "decision_today", "teams.chat", days=10, judge="Jev")
        p4 = decision("teams-chat-triage", "304", "decision_today", "teams.chat", days=7, seconds=1)
        old = decision("teams-chat-triage", "305", "decision_today", "teams.chat", days=20)
        sample = decision("teams-chat-triage", "999001", "decision_today", "ado.workitem.created", days=1, actions=ADO_DRAFT)
        demo = decision("teams-chat-triage", "106", "decision_today", "teams.chat", days=1, actions=TEAMS_DRAFT, source="demo")
        fut = decision("teams-chat-triage", "107", "decision_today", "teams.chat", days=-1)
        old_ado = decision("ado-workitem", "4700", "triage", "ado.workitem.created", days=8, actions=ADO_DRAFT)
        old_ado2 = decision("ado-workitem", "4701", "triage", "ado.workitem.created", days=9, actions=ADO_DRAFT)
        items = dict([
            approved(2, k2, status="held", days=6),
            approved(3, ado, days=2, latency=30),
            approved(5, k5, days=1, latency=90, generation=2),      # a proposal that changed: its OK is not "unchanged"
            approved(6, ado2, days=3, latency=60),                  # no draft
            approved(7, j1, days=1, latency=45),                    # Jev
            approved(8, sample, days=1, latency=15),                # the fictional item
            approved(9, demo, days=1, latency=20),                  # demo
            approved(10, k4, status="rejected", days=4),
            approved(11, fut, days=-1, latency=30),                 # approved in the future
            approved(12, old_ado, days=8, latency=120),             # the week before
            approved(13, old_ado2, days=9, latency=240),            # the week before
            approved(14, u1, days=4, latency=40),                   # no judge name: a draft and an OK, left out like Jev's
        ])
        rows = [k1, k2, ado, ado2, j1, u1, k4, k5, future, p1, p2, p3, p4, old]
        (self.out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows) + BROKEN,
                                                  encoding="utf-8")
        log = [approval_row(3, ado, days=2), approval_row(5, k5, days=1), approval_row(6, ado2, days=3),
               approval_row(7, j1, days=1), approval_row(8, sample, days=1), approval_row(9, demo, days=1),
               approval_row(12, old_ado, days=8), approval_row(10, k4, status="rejected", days=4),
               approval_row(2, k2, status="held", days=6), approval_row(3, ado, status="redrafted", days=2),
               approval_row(11, fut, days=-1), approval_row(14, u1, days=4)]
        (self.out / "approvals.log.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in log) + BROKEN,
                                                      encoding="utf-8")
        (self.out / "approvals.json").write_text(json.dumps({"next": 15, "items": items}, ensure_ascii=False), encoding="utf-8")
        for rec, verdict, judge in ((k1, "yes", "Kev"), (k4, "wrong", "Kev"), (j1, "wrong", "Jev"), (u1, "wrong", None)):
            row = {"key": review.decision_key(rec), "verdict": verdict, "at": at(1)}
            if judge:
                row["judge"] = judge
            review._append(review.reviews_path(), row)

    # the lines this week's data must print (hand-counted, see the comments of seed())
    EXPECTED = (
        "判断: 8 件 / 自動で決めた 7 件",                                                    # existing line, for the counts
        "前の 7 日（2026-09-28 から）: 判断 4 件（自動 75%・人 25%）",                        # p1..p4; p4 is one second before this week
        "届いた件数（種類別）: teams 4 件、ado 2 件、alert 2 件",                             # teams: k1 k2 j1 k5; ado: ado ado2
        "1 件あたりの返信: 1.1 回（返信のあった件 8 件）",                                    # 9 replies on 8 items (u1 added); sample left out
        "自動で決めた判断で、正誤に「違う」が付いた件: 1 件（対象 5 件）",                     # k1 ado ado2 k4 k5 (Jev, unnamed left out)
        "提案が一度も変わらなかった OK: 1 件（下書きのある OK 2 件・参考）",                  # ado and k5 have drafts; ado is generation 1; j1 (Jev), u1 (no judge) left out
        "投稿から OK を見るまで（中央値）: 今週 60.0 秒（測れた 3 件・参考）、前の 7 日 180.0 秒（測れた 2 件・参考）",
        "（Jev の判断 1 件は数えていません）",
        "（判断モデルが分からない判断 1 件は数えていません）",
        "calibrate の正誤（判断モデルごと。下限 100 問）: CLM 0 問（あと 100 問）、Kev 0 問（あと 100 問）",
    )

    def test_the_week_before_and_the_kinds_that_arrived_are_counted_with_the_boundaries_right(self):
        self.seed()
        text = stats.week(self.out, now=NOW)
        self.assertIn("判断: 8 件 / 自動で決めた 7 件", text)                        # the exactly-seven-days-old one is this week
        self.assertIn("前の 7 日（2026-09-28 から）: 判断 4 件（自動 75%・人 25%）", text)  # one second older is the week before
        self.assertIn("届いた件数（種類別）: teams 4 件、ado 2 件、alert 2 件", text)

    def test_a_review_that_says_wrong_counts_only_automatic_decisions_of_a_named_judge_other_than_jev(self):
        self.seed()
        self.assertIn("自動で決めた判断で、正誤に「違う」が付いた件: 1 件（対象 5 件）", stats.week(self.out, now=NOW))

    def test_the_replies_per_item_count_every_reply_and_leave_the_fictional_item_out(self):
        self.seed()
        self.assertIn("1 件あたりの返信: 1.1 回（返信のあった件 8 件）", stats.week(self.out, now=NOW))

    def test_an_ok_counts_as_unchanged_only_when_its_item_has_a_draft_and_it_is_neither_jev_demo_nor_the_fictional_item(self):
        self.seed()
        text = stats.week(self.out, now=NOW)
        self.assertIn("提案が一度も変わらなかった OK: 1 件（下書きのある OK 2 件・参考）", text)

    def test_the_weekly_medians_use_the_approval_latency_of_each_week_and_say_how_many_they_measure(self):
        self.seed()
        self.assertIn("投稿から OK を見るまで（中央値）: 今週 60.0 秒（測れた 3 件・参考）、前の 7 日 180.0 秒（測れた 2 件・参考）",
                      stats.week(self.out, now=NOW))

    def test_jev_decisions_are_counted_once_and_named_as_left_out_of_the_performance_rows(self):
        self.seed()
        self.assertIn("（Jev の判断 1 件は数えていません）", stats.week(self.out, now=NOW))

    def test_a_decision_by_an_unnamed_judge_is_left_out_of_the_figures_and_the_digest_says_how_many(self):
        self.seed()
        for share in (False, True):
            text = stats.week(self.out, share=share, now=NOW)
            self.assertIn("（判断モデルが分からない判断 1 件は数えていません）", text)
            self.assertIn("自動で決めた判断で、正誤に「違う」が付いた件: 1 件（対象 5 件）", text)   # u1 is not among the 5

    def test_the_calibrate_count_takes_each_named_judge_other_than_jev_and_says_what_is_still_needed(self):
        self.seed()
        questions = [{"judge": "Kev"}] * 3 + [{"judge": "CLM"}] * 100 + [{"judge": "Jev"}] * 2
        with mock.patch.object(calibrate, "labeled_questions", return_value=questions):
            text = stats.week(self.out, now=NOW)
        self.assertIn("calibrate の正誤（判断モデルごと。下限 100 問）: CLM 100 問（下限に達しています）、Kev 3 問（あと 97 問）", text)
        line = next(x for x in text.splitlines() if x.startswith("calibrate の正誤"))
        self.assertNotIn("Jev", line)

    def test_calibrate_says_how_many_labeled_questions_each_judge_still_needs_without_calling_a_model(self):
        self.seed()
        self.assertIn("calibrate の正誤（判断モデルごと。下限 100 問）: CLM 0 問（あと 100 問）、Kev 0 問（あと 100 問）",
                      stats.week(self.out, now=NOW))

    def test_every_new_line_is_on_the_screen_and_in_the_share_and_the_share_shows_no_text_name_id_or_path(self):
        self.seed()
        screen = stats.week(self.out, now=NOW)
        shared = stats.week(self.out, share=True, now=NOW)
        for line in self.EXPECTED:
            self.assertIn(line, screen)
            self.assertIn(line, shared)
        for mark in (MARK_TEXT, MARK_NAME, MARK_ID, MARK_PATH, str(self.dir.name), "Users"):
            self.assertNotIn(mark, shared)

    def test_the_digest_writes_no_file_in_the_output_or_the_state_folder(self):
        self.seed()
        before = snapshot(self.out, self.state)
        stats.week(self.out, now=NOW)
        stats.week(self.out, share=True, now=NOW)
        self.assertEqual(snapshot(self.out, self.state), before)

    def test_a_line_that_is_json_but_not_an_object_is_left_out_and_only_the_calibrate_count_says_unknown(self):
        self.seed()
        for name in ("decisions.jsonl", "approvals.log.jsonl"):
            with (self.out / name).open("a", encoding="utf-8") as f:
                f.write("[1, 2]\n42\n")
        with review.reviews_path().open("a", encoding="utf-8") as f:
            f.write("[1, 2]\n")
        text = stats.week(self.out, now=NOW)
        self.assertIn("判断: 8 件 / 自動で決めた 7 件", text)
        self.assertIn("前の 7 日（2026-09-28 から）: 判断 4 件（自動 75%・人 25%）", text)
        self.assertIn("自動で決めた判断で、正誤に「違う」が付いた件: 1 件（対象 5 件）", text)
        self.assertIn("calibrate の正誤: 不明（記録かグラフを読めませんでした）", text)

    def test_a_damaged_approvals_file_is_not_an_error_and_the_figures_say_unknown(self):
        self.seed()
        (self.out / "approvals.json").write_text("{broken", encoding="utf-8")
        text = stats.week(self.out, now=NOW)
        self.assertIn("提案が一度も変わらなかった OK: 不明（下書きのある OK 0 件）", text)
        self.assertIn("投稿から OK を見るまで（中央値）: 今週 不明、前の 7 日 不明", text)

    def test_an_older_summary_made_without_the_week_additions_renders_without_them(self):
        self.seed()
        summary = stats.collect(self.out, now=NOW)
        summary.pop("week")
        text = stats.render(summary)
        self.assertNotIn("前の 7 日", text)
        self.assertNotIn("calibrate の正誤", text)

    def test_the_trial_measurement_counts_only_the_oks_seen_by_the_time_it_is_given(self):
        self.seed()
        seen_now = trial.measurement_collect(self.out, [], now=NOW.timestamp())["local"]["approval_latency"]["measured"]
        seen_later = trial.measurement_collect(self.out, [], now=(NOW + timedelta(days=2)).timestamp())["local"]["approval_latency"]["measured"]
        # the OK one day in the future counts only later; the no-name OK (u1) counts here, as every local OK does (only --share leaves them out)
        self.assertEqual((seen_now, seen_later), (8, 9))


class TestFiguresAndTheirReferenceMarks(Base):
    """A figure made of 1 to 4 records is marked as a reference, 5 and more are the figure, and 0 is Unknown (never zero)."""

    def add_auto(self, event_id):
        with (self.out / "decisions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(decision("monitor-alert", event_id, "alert", "monitor.alert", days=1)) + "\n")

    def test_four_automatic_decisions_give_a_reference_wrong_figure_and_the_unknown_draft_figure(self):
        for event_id in ("900", "901", "902", "903"):
            self.add_auto(event_id)
        review._append(review.reviews_path(), {"key": "monitor-alert:900:alert", "verdict": "wrong", "at": at(1), "judge": "Kev"})
        text = stats.week(self.out, now=NOW)
        self.assertIn("自動で決めた判断で、正誤に「違う」が付いた件: 1 件（対象 4 件・参考）", text)
        self.assertIn("提案が一度も変わらなかった OK: 不明（下書きのある OK 0 件）", text)
        self.assertIn("1 件あたりの返信: 不明（返信のあった件 0 件）", text)

    def test_five_automatic_decisions_give_a_figure_without_the_reference_mark(self):
        for event_id in ("900", "901", "902", "903", "904"):
            self.add_auto(event_id)
        self.assertIn("自動で決めた判断で、正誤に「違う」が付いた件: 0 件（対象 5 件）", stats.week(self.out, now=NOW))

    def test_no_automatic_decision_makes_the_wrong_figure_unknown_not_zero(self):
        self.assertIn("自動で決めた判断で、正誤に「違う」が付いた件: 不明（対象 0 件）", stats.week(self.out, now=NOW))


class TestGoldenTexts(unittest.TestCase):
    """The post, brief, toast and push texts as they are now: the week digest changes none of them."""

    def test_the_post_brief_toast_and_push_texts_are_byte_for_byte_the_same(self):
        self.assertEqual(notify.format_post(3, REC), "[kimeru #3] ADO #4812\n→ 優先度を判定できないチケット #4812\nOK 3 / NG 3 / 詳細 3")
        self.assertEqual(push.message([3, 5, 7], 2, 1), "kimeru: 確認待ち 3 件（#3, #5, #7） / 自動決定の通知 2 件 / "
                                                        "投稿できていない確認待ち 1 件。Teams の自分とのチャットを確認してください")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(list(notify.toast_text(Path(d), [3, 5], 2, 1)),
                             ["kimeru: 確認待ち 2 件 / 自動決定の通知 2 件 / 投稿できていない確認待ち 1 件", "#3, #5"])
        self.assertEqual(brief.format_post([{"text": "ADO の依頼を確認"}, {"text": "Teams の確認"}], 4, 2, date="2026-10-12"),
                         "[kimeru brief 2026-10-12] 今日の進め方\n1. ADO の依頼を確認\n2. Teams の確認\nほか 2 件\n"
                         "確認待ち 2 件（OK 番号 / NG 番号 / 詳細 番号 で返信）")


class TestRepeatedInstructionLine(unittest.TestCase):
    def test_a_fingerprint_written_again_on_another_item_is_counted_once_more_in_the_week_report(self):
        def row(days, key, sig):
            return {"at": (NOW - timedelta(days=days)).isoformat(), "key": key, "id": 1, "status": "redrafted",
                    "instruction_len": 6, "instruction_sig": sig}
        rows = [row(1, "item-a", "aaaaaaaaaaaa"),
                row(1, "item-b", "aaaaaaaaaaaa"),     # the same wording on another item: counted
                row(2, "item-b", "aaaaaaaaaaaa"),     # the same item again: not counted
                row(30, "item-c", "aaaaaaaaaaaa"),    # older than seven days: not counted
                row(1, "item-d", "bbbbbbbbbbbb"),     # written once: not counted
                row(1, "item-e", None)]               # no fingerprint: not counted
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "approvals.log.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            text = stats.week(out, now=NOW)
        self.assertIn("同じ修正指示の繰り返し（別の項目で）: 1 件", text)


SIX = ("到着から判断完了まで（PC が見た時刻から）", "到着から承認まで（PC が見た時刻から）",
       "OK の後の送信（自分のチャットの一覧で見た。届いたことの証明ではない）", "正誤のラベルの増分（今週）",
       "採用した学習の前後（今週）", "朝のまとめの上位 3 件")


def noon_of(days_back):
    """(UTC ISO text of noon, local date text) of the local day `days_back` days before NOW. A brief of that day and a reply written
    at noon are on the same local day, whatever the time zone of the machine."""
    day = (NOW - timedelta(days=days_back)).astimezone().date()
    return datetime(day.year, day.month, day.day, 12, 0).astimezone(timezone.utc).isoformat(timespec="seconds"), day.isoformat()


def arrived(n, rec, days, arrival):
    """An approved case that the PC saw arrive `arrival` seconds before its OK (None: no arrival recorded). The first ready time is 60
    seconds before the OK, which was seen `days` days before NOW."""
    n, item = approved(n, rec, status="approved", days=days, latency=30)
    timing = item["measurement"]
    if arrival is not None:
        timing["arrived_at"] = at(days, arrival)
    timing["first_ready_at"] = at(days, 60)
    timing["approval_observed_at"] = at(days, 0)
    return n, item


class TestWeeklyRows(Base):
    """R24: the second stage of the week digest, hand-counted. Six approved cases (one Jev, one nine days old, one with no arrival
    and one whose arrival is after its ready time), their sends and labels, one adoption of the judge CLM with its decisions before
    and after, and the morning brief rows of two days. The figures leave out Jev, the fictional item and an unnamed judge."""

    def seed(self):
        a = decision("teams-chat-triage", "501", "decision_today", "teams.chat", days=1)
        b = decision("teams-chat-triage", "502", "decision_today", "teams.chat", days=2)
        d = decision("teams-chat-triage", "503", "decision_today", "teams.chat", days=3)
        e = decision("teams-chat-triage", "504", "decision_today", "teams.chat", days=1)
        j = decision("teams-chat-triage", "505", "decision_today", "teams.chat", days=1, judge="Jev")
        f = decision("teams-chat-triage", "506", "decision_today", "teams.chat", days=9)
        clm = [decision("monitor-alert", str(900 + i), "alert", "monitor.alert", days=d_, seconds=s, judge="CLM", needs_human=h)
               for i, (d_, s, h) in enumerate([(4, 0, True), (4, 100, False), (5, 0, False), (5, 100, True),
                                                (1, 0, True), (1, 100, False), (2, 0, False), (2, 100, False)])]
        (self.out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in [a, b, d, e, j, f, *clm]),
                                                  encoding="utf-8")
        items = dict([arrived(1, a, 1, 90), arrived(2, b, 2, None), arrived(3, d, 3, 180), arrived(4, e, 1, 10),
                      arrived(5, j, 1, 1000), arrived(6, f, 9, 30)])
        items["1"]["sent_seen"] = {"state": "as_is", "at": at(1)}
        items["3"]["sent_seen"] = {"state": "edited", "at": at(3)}
        items["4"]["sent_seen"] = {"state": "other", "at": at(1)}
        items["5"]["sent_seen"] = {"state": "as_is", "at": at(1)}        # Jev: left out
        items["6"]["sent_seen"] = {"state": "as_is", "at": at(9)}        # the week before: left out
        (self.out / "approvals.json").write_text(json.dumps({"next": 7, "items": items}, ensure_ascii=False), encoding="utf-8")
        card = {"at": noon_of(2)[0], "key": "kimeru.learn:x:5", "id": 5, "status": "approved"}
        log = [{"at": noon_of(2)[0], "key": "teams-chat-triage:901:decision_today", "id": 1, "status": "approved"},
               {"at": noon_of(2)[0], "key": "teams-chat-triage:902:decision_today", "id": 2, "status": "approved"}, card]
        (self.out / "approvals.log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in log), encoding="utf-8")
        (self.out / "work-state.jsonl").write_text(json.dumps({"transition_id": "t1", "at": noon_of(3)[0], "case": "3", "from": "approved",
                                                               "to": "in_progress"}) + "\n", encoding="utf-8")

        def row(rank, key):
            return {"key": key, "kind": "teams", "rank": rank, "harm": 0.5}
        briefs = [{"date": noon_of(2)[1], "backend": "Kev", "rows": [row(1, "approval:1"), row(2, "approval:5"),
                                                                     row(3, "ado:4812"), row(4, "approval:9")]},
                  {"date": noon_of(3)[1], "backend": "Kev", "rows": [row(1, "approval:2"), row(2, "approval:3")]},
                  {"date": noon_of(4)[1], "backend": "Jev", "rows": [row(1, "approval:7")]}]
        (self.out / "brief_history.jsonl").write_text("".join(json.dumps(r) + "\n" for r in briefs), encoding="utf-8")
        # the adoption of CLM's question, three days ago: the windows before and after are three days each
        when = (NOW - timedelta(days=3)).astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
        old = (NOW - timedelta(days=5)).astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
        ledger = [{"id": "L1", "kind": "adopt", "node": "monitor:alert", "judge": "CLM", "adopted_at": when},
                  {"id": "L2", "kind": "adopt", "node": "monitor:alert", "judge": "CLM", "adopted_at": old},
                  {"id": "L2", "kind": "revert", "node": "monitor:alert", "reverted_at": when},
                  {"id": "L3", "kind": "adopt", "node": "teams:intent", "judge": "Jev", "adopted_at": when}]
        learn.ledger_path().write_text("".join(json.dumps(r) + "\n" for r in ledger), encoding="utf-8")
        for rec, verdict, judge, source in ((a, "wrong", "Kev", "teams"), (d, "no", "Kev", "teams"), (a, "yes", "Kev", None),
                                            (j, "wrong", "Jev", "teams"), (f, "wrong", "Kev", "teams")):
            row_ = {"key": review.decision_key(rec), "verdict": verdict, "at": at(9 if rec is f else 1), "judge": judge}
            if source:
                row_["source"] = source
            review._append(review.reviews_path(), row_)

    EXPECTED = (
        "到着から判断完了まで（PC が見た時刻から）: 中央値 75.0 秒（実測 2 件・不明 2 件・参考）",
        "到着から承認まで（PC が見た時刻から）: 中央値 90.0 秒（実測 3 件・不明 1 件・参考）",
        "OK の後の送信（自分のチャットの一覧で見た。届いたことの証明ではない）: as_is 1 件・edited 1 件・other 1 件",
        "正誤のラベルの増分（今週）: 返信 2 件・kimeru review 1 件",
        "採用した学習の前後（今週）: 採用 1 件。同じ長さの窓で PM に回った割合は、採用の前 50%（4 件）、後 25%（4 件）"
        "（基準は採用の前の同じ長さの期間。kimeru はその間も動いていた）",
        "朝のまとめの上位 3 件（承認番号のある件 4 件）で、同じ日に返信か作業の記録があった件: 2 件"
        "（弱い信号。見ただけの件と、ほかの経路の返信は分からない）",
    )

    def test_the_six_rows_count_what_the_week_holds_with_the_arrival_measured_or_unknown(self):
        self.seed()
        text = stats.week(self.out, now=NOW)
        for line in self.EXPECTED:
            self.assertIn(line, text)

    def test_jev_and_the_week_before_are_left_out_of_every_row(self):
        self.seed()
        text = stats.week(self.out, now=NOW)
        six = [l for l in text.splitlines() if l.startswith(SIX)]
        self.assertEqual(len(six), 6)
        self.assertNotIn("Jev", "\n".join(six))
        self.assertNotIn("1000", "\n".join(six))                # Jev's arrival of 1000 seconds would move the median
        self.assertNotIn("採用 2 件", text)                      # the reverted adoption and Jev's are not counted

    def test_the_fictional_item_and_the_demo_move_no_row_and_no_figure(self):
        self.seed()
        before = [l for l in stats.week(self.out, now=NOW).splitlines() if l.startswith(SIX)]
        # the trial's fictional item (number 999001: its key, and an ADO event for one of CLM's decisions) and a demo item. Each is an
        # approved case with a late arrival and an as-is send, has a decision and a reply (wrong, or no), and CLM's decision of each
        # sits in the adoption's window. The reply log row, the work row and the brief row carry the fictional item's number on
        # another day
        sample = decision("teams-chat-triage", "999001", "decision_today", "teams.chat", days=1)
        demo = decision("teams-chat-triage", "910", "decision_today", "teams.chat", days=1, source="demo")
        clm_sample = decision("teams-chat-triage", "999001", "decision_today", "ado.workitem.created", days=1, judge="CLM", needs_human=True)
        clm_demo = decision("monitor-alert", "908", "alert", "monitor.alert", days=1, judge="CLM", needs_human=True, source="demo")
        with (self.out / "decisions.jsonl").open("a", encoding="utf-8") as f:
            f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in [sample, demo, clm_sample, clm_demo]))
        data = json.loads((self.out / "approvals.json").read_text(encoding="utf-8"))
        data["items"].update(dict([arrived(7, sample, 1, 1000), arrived(8, demo, 1, 1000)]))
        data["items"]["7"]["sent_seen"] = {"state": "as_is", "at": at(1)}
        data["items"]["8"]["sent_seen"] = {"state": "as_is", "at": at(1)}
        data["next"] = 9
        (self.out / "approvals.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with (self.out / "approvals.log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": noon_of(5)[0], "key": "teams-chat-triage:999001:decision_today", "id": 7, "status": "approved"}) + "\n")
        with (self.out / "work-state.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"transition_id": "t2", "at": noon_of(5)[0], "case": "7", "from": "approved", "to": "in_progress"}) + "\n")
        with (self.out / "brief_history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"date": noon_of(5)[1], "backend": "Kev", "rows": [{"key": "approval:7", "kind": "teams", "rank": 1,
                                                                                    "harm": 0.5}]}) + "\n")
        for rec, verdict in ((sample, "wrong"), (demo, "no")):
            review._append(review.reviews_path(), {"key": review.decision_key(rec), "verdict": verdict, "at": at(1), "judge": "Kev",
                                                   "source": "teams"})
        text = stats.week(self.out, now=NOW)
        for line in self.EXPECTED:
            self.assertIn(line, text)
        self.assertEqual([l for l in text.splitlines() if l.startswith(SIX)], before)

    def test_a_row_with_no_record_says_unknown_and_nothing_raises(self):
        text = stats.week(self.out, now=NOW)                     # an empty folder
        for line in ("到着から判断完了まで（PC が見た時刻から）: 不明（記録なし）", "到着から承認まで（PC が見た時刻から）: 不明（記録なし）",
                     "OK の後の送信（自分のチャットの一覧で見た。届いたことの証明ではない）: 不明（記録なし）",
                     "正誤のラベルの増分（今週）: 不明（記録なし）", "採用した学習の前後（今週）: 不明（記録なし）",
                     "朝のまとめの上位 3 件で、同じ日に返信か作業の記録があった件: 不明（承認番号のある件 0 件）"):
            self.assertIn(line, text)

    def test_the_six_rows_are_on_the_screen_and_in_the_share_and_the_share_shows_no_text_name_id_path_or_jev_figure(self):
        self.seed()
        screen = stats.week(self.out, now=NOW)
        shared = stats.week(self.out, share=True, now=NOW)
        for line in self.EXPECTED:
            self.assertIn(line, screen)
            self.assertIn(line, shared)
        for mark in (MARK_TEXT, MARK_NAME, MARK_ID, MARK_PATH, str(self.dir.name), "Users"):
            self.assertNotIn(mark, shared)

    def test_the_rows_write_nothing_and_the_six_lines_for_the_brief_are_the_same_six(self):
        self.seed()
        before = snapshot(self.out, self.state)
        lines = stats.weekly_lines(self.out, now=NOW)
        self.assertEqual(snapshot(self.out, self.state), before)
        self.assertEqual(len(lines), 6)
        for line, label in zip(lines, SIX):
            self.assertTrue(line.startswith(label), line)
            self.assertIn(line, stats.week(self.out, now=NOW))


if __name__ == "__main__":
    unittest.main()
