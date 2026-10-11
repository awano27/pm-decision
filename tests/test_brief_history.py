try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from kimeru import brief, daily, fsutil
from kimeru.backends import StubBackend
from kimeru.cli import process
from tests.test_daily import GRAPHS, PBS, FakeTeams

NOW = datetime(2026, 9, 28, 8, 10)   # a local time, as daily.cycle receives it
DAY = "2026-09-28"
MARK_TEXT, MARK_NAME = "本文ZZTEXT", "人名ZZNAME"

# The brief as it was posted before the history existed (captured from the unchanged code): the first three are named,
# the rest are counted, and the pending count is the same. A change to the post text must show here first.
GOLDEN_BRIEF = ("[kimeru brief 2026-09-28] 今日の進め方\n"
                "1. 確認待ち #1: 顧客から障害の報告 #101\n"
                "2. 確認待ち #2: リリースの判断が必要 #102\n"
                "3. 確認待ち #3: 優先度を判定できないチケット #103\n"
                "ほか 1 件\n"
                "確認待ち 4 件（OK 番号 / NG 番号 / 詳細 番号 で返信）")


def queue_row(eid, advice, **extra):
    return {"graph": "workitem-intake", "event_kind": "ado.workitem.created", "event_id": eid, "node": "triage_pm",
            "outcome": "advise", "needs_human": True, "advice": advice,
            "actions": [{"type": "ado.update", "id": eid, "fields": {"Microsoft.VSTS.Common.Priority": 2}}], **extra}


GOLDEN_ROWS = [queue_row("101", "顧客から障害の報告 #101"), queue_row("102", "リリースの判断が必要 #102"),
               queue_row("103", "優先度を判定できないチケット #103"), queue_row("104", "スケジュールの確認 #104")]


class FakeJev(StubBackend):
    """Reports the name Jev: the history must carry the judge's name, so that no other judge's points are reused."""
    NAME = "Jev"


class Spy(StubBackend):
    def __init__(self):
        self.calls = []

    def ask(self, state, questions):
        self.calls.append(len(questions))
        return super().ask(state, questions)


class NotReadBack(FakeTeams):
    """The post is sent but the self chat does not show it back: delivery is not proven."""

    def post(self, text, send):
        self.posts.append((text, send))
        return {"ok": True, "typed": True, "sent": bool(send), "readback": {"matched": False, "message_id": ""}}


class NotSent(FakeTeams):
    def post(self, text, send):
        self.posts.append((text, send))
        return {"ok": False, "typed": False, "sent": False, "error": "the self chat was not found"}


class Briefed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.out, self.inbox = self.d / "out", self.d / "inbox"
        self.out.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def add_queue(self, *rows):
        path = self.out / "queue.jsonl"
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(old + "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def cycle(self, bridge=None, send=True, backend=None, now=NOW):
        return daily.cycle(self.out, self.inbox, GRAPHS, backend or StubBackend(), PBS, process,
                           bridge=bridge or FakeTeams(), send=send, now=now)

    def history(self):
        p = self.out / "brief_history.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    def reset_brief_day(self):   # the brief of this day is not yet delivered (as at the first cycle of a day)
        p = self.out / "daily_state.json"
        st = json.loads(p.read_text(encoding="utf-8"))
        st["brief_date"] = ""
        p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


class TestTheBriefTextStaysTheSame(Briefed):
    def test_the_posted_brief_names_the_first_three_and_counts_the_rest(self):
        self.add_queue(*GOLDEN_ROWS)
        bridge = FakeTeams()
        r = self.cycle(bridge=bridge)
        briefs = [t for t, _ in bridge.posts if t.startswith("[kimeru brief")]
        self.assertEqual(briefs, [GOLDEN_BRIEF])
        self.assertEqual(r["brief"], 3)

    def test_build_asks_the_judge_once_for_all_items_and_the_text_is_unchanged(self):
        self.add_queue(*GOLDEN_ROWS)
        self.cycle()
        spy = Spy()
        text, _ = brief.build(self.out, spy, date=DAY)
        self.assertEqual(spy.calls, [4])
        self.assertEqual(text, GOLDEN_BRIEF)


class TestHistoryAfterDelivery(Briefed):
    def setUp(self):
        super().setUp()
        self.add_queue(*GOLDEN_ROWS)

    def test_a_delivered_brief_writes_one_row_for_its_date(self):
        self.cycle()
        rows = self.history()
        self.assertEqual([r["date"] for r in rows], [DAY])

    def test_the_row_lists_every_item_in_rank_order_and_the_first_three_are_the_ones_named(self):
        self.cycle()
        row = self.history()[0]["rows"]
        self.assertEqual(sorted(r["rank"] for r in row), [1, 2, 3, 4])
        by_rank = [r["key"] for r in sorted(row, key=lambda r: r["rank"])]
        self.assertEqual(by_rank, ["approval:1", "approval:2", "approval:3", "approval:4"])
        for r in row:
            self.assertEqual(set(r), {"key", "kind", "sig", "harm", "rank", "notify", "severe", "due_level"})
            self.assertIsInstance(r["harm"], float)
            self.assertIsInstance(r["rank"], int)
            self.assertEqual(r["kind"], "approval")
            self.assertEqual(r["due_level"], 0)

    def test_sig_is_the_first_twelve_hex_digits_of_the_sha1_of_the_subject(self):
        self.cycle()
        row = {r["key"]: r for r in self.history()[0]["rows"]}["approval:1"]
        subject = "Waiting for the PM's approval: 顧客から障害の報告 #101"
        self.assertEqual(row["sig"], hashlib.sha1(subject.encode("utf-8")).hexdigest()[:12])

    def test_the_backend_name_is_written_and_is_jev_for_jev(self):
        self.cycle(backend=FakeJev())
        self.assertEqual(self.history()[0]["backend"], "Jev")

    def test_the_backend_name_is_never_empty(self):
        self.cycle()
        self.assertTrue(self.history()[0]["backend"])

    def test_notify_and_severe_come_from_the_decision_record(self):
        self.add_queue(queue_row("201", "障害の一次対応 #201", notify=True, severe_guard="Sev1 の文言"))
        self.cycle()
        row = {r["key"]: r for r in self.history()[0]["rows"]}
        self.assertEqual((row["approval:5"]["notify"], row["approval:5"]["severe"]), (True, True))
        self.assertEqual((row["approval:1"]["notify"], row["approval:1"]["severe"]), (False, False))

    def test_a_second_delivery_on_the_same_day_replaces_the_row(self):
        self.cycle()
        self.add_queue(queue_row("105", "追加の確認 #105"))
        self.reset_brief_day()
        self.cycle()
        rows = self.history()
        self.assertEqual([r["date"] for r in rows], [DAY])
        self.assertEqual(len(rows[0]["rows"]), 5)

    def test_a_brief_on_another_day_adds_its_own_row(self):
        self.cycle()
        self.cycle(now=NOW + timedelta(days=1))
        self.assertEqual([r["date"] for r in self.history()], [DAY, "2026-09-29"])

    def test_a_brief_pasted_without_send_writes_no_row(self):
        self.cycle(send=False)
        self.assertFalse((self.out / "brief_history.jsonl").exists())

    def test_a_brief_whose_delivery_is_not_proven_writes_no_row(self):
        r = self.cycle(bridge=NotReadBack())
        self.assertTrue(str(r["brief"]).startswith("error"))
        self.assertFalse((self.out / "brief_history.jsonl").exists())

    def test_a_brief_that_was_not_sent_writes_no_row(self):
        r = self.cycle(bridge=NotSent())
        self.assertTrue(str(r["brief"]).startswith("error"))
        self.assertFalse((self.out / "brief_history.jsonl").exists())

    def test_kimeru_brief_build_writes_nothing(self):
        p = self.out / "brief_history.jsonl"
        p.write_text(json.dumps({"date": "2026-09-27", "backend": "Jev", "rows": []}) + "\n", encoding="utf-8")
        before = p.read_bytes()
        brief.build(self.out, StubBackend(), date=DAY)
        self.assertEqual(p.read_bytes(), before)

    def test_a_busy_history_lock_writes_no_row_and_the_brief_still_counts(self):
        with fsutil.exclusive(self.out / "brief_history.lock") as got:
            self.assertTrue(got)
            r = self.cycle()
        self.assertFalse((self.out / "brief_history.jsonl").exists())
        self.assertIn("brief_history", r["busy"])
        self.assertEqual(r["brief"], 3)

    def test_the_row_holds_no_text_or_name_of_the_event(self):
        self.add_queue(queue_row("301", MARK_TEXT + " #301", event={"author": MARK_NAME, "text": MARK_TEXT}))
        bridge = FakeTeams()
        self.cycle(bridge=bridge)
        self.assertTrue(any(MARK_TEXT in t for t, _ in bridge.posts))   # the brief itself does show it
        for name in ("brief_history.jsonl", "daily.log.jsonl", "daily_state.json"):
            text = (self.out / name).read_text(encoding="utf-8")
            self.assertNotIn(MARK_TEXT, text, name)
            self.assertNotIn(MARK_NAME, text, name)


class TestKeepNinetyDays(Briefed):
    def write_history(self, *dates):
        lines = [json.dumps({"date": d, "backend": "StubBackend", "rows": []}, ensure_ascii=False) for d in dates]
        (self.out / "brief_history.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_rows_older_than_ninety_days_are_removed_by_the_daily_cycle(self):
        self.write_history("2026-06-01", "2026-09-27")
        self.cycle(send=False)
        self.assertEqual([r["date"] for r in self.history()], ["2026-09-27"])

    def test_the_day_ninety_days_back_is_kept_and_the_day_before_it_is_removed(self):
        keep = (NOW.date() - timedelta(days=90)).isoformat()
        gone = (NOW.date() - timedelta(days=91)).isoformat()
        self.write_history(gone, keep)
        self.assertEqual(brief.purge_history(self.out, now=NOW), 1)
        self.assertEqual([r["date"] for r in self.history()], [keep])

    def test_a_purge_with_nothing_old_leaves_the_file_as_it_is(self):
        self.write_history("2026-09-27")
        p = self.out / "brief_history.jsonl"
        before = p.read_bytes()
        self.assertEqual(brief.purge_history(self.out, now=NOW), 0)
        self.assertEqual(p.read_bytes(), before)

    def test_a_damaged_line_is_kept_by_the_purge_and_by_the_next_write(self):
        p = self.out / "brief_history.jsonl"
        p.write_text("{broken\n" + json.dumps({"date": "2026-06-01", "backend": "x", "rows": []}) + "\n", encoding="utf-8")
        brief.purge_history(self.out, now=NOW)
        self.assertIn("{broken", p.read_text(encoding="utf-8"))
        brief.record_history(self.out, DAY, StubBackend(), [])
        text = p.read_text(encoding="utf-8")
        self.assertIn("{broken", text)
        self.assertEqual([json.loads(l)["date"] for l in text.splitlines() if l.startswith('{"date"')], [DAY])


class TestRankReturnsEveryItem(unittest.TestCase):
    def test_build_returns_every_ranked_item_while_the_text_names_three(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            out = Path(tmp.name)
            (out / "queue.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in GOLDEN_ROWS),
                                             encoding="utf-8")
            text, ranked = brief.build(out, StubBackend(), date=DAY)
            self.assertEqual(len(ranked), 4)
            self.assertEqual([i["rank"] for i in ranked], [1, 2, 3, 4])
            self.assertIn("ほか 1 件", text)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
