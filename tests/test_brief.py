try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kimeru import brief, notify
from kimeru.backends import StubBackend

NOW = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)


def step(i, title, level):
    return {"id": f"s{i}", "title": title, "due": ["今日", "今週", "次スプリント以降"][level], "due_level": level}


def decision(eid, steps, at=NOW):
    return {"graph": "g", "event_id": eid, "node": "d", "outcome": "decide", "needs_human": False,
            "at": at.isoformat(), "plan": {"playbook": "incident", "title": "障害対応の取りまとめ", "steps": steps}}


def write(d, name, rows):
    (Path(d) / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


class Fixed:
    """Scores by a lookup on item text."""

    def __init__(self, table):
        self.table = table

    def ask(self, state, qs):
        return {q: {"score": self.table.get(state["items"][int(q[1:])], 0.0), "confidence": 0.9} for q in qs}


class TestCollect(unittest.TestCase):
    def test_sources_dedup_and_filters(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "decisions.jsonl", [
                decision("e1", [step(1, "責任者を指名", 0), step(2, "振り返り", 2)]),
                decision("e1", [step(1, "責任者を指名", 0)]),                       # duplicate step
                decision("old", [step(1, "古い手順", 0)], at=NOW - timedelta(days=5)),  # too old
            ])
            q = {"graph": "workitem-intake", "event_id": "4812", "node": "triage_pm", "advice": "優先度を判定できないチケット"}
            write(d, "queue.jsonl", [q])
            items = brief.collect(d, now=NOW)
            texts = [i["text"] for i in items]
            self.assertEqual(sum("責任者を指名" in t for t in texts), 1)
            self.assertFalse(any("振り返り" in t or "古い手順" in t for t in texts))
            self.assertTrue(any(t.startswith("確認待ち（未投稿）") for t in texts))

    def test_yesterdays_today_shows_as_overdue(self):
        with tempfile.TemporaryDirectory() as d:
            at = datetime(2026, 9, 24, 1, 0, tzinfo=timezone.utc)          # Thu 10:00 JST
            write(d, "decisions.jsonl", [decision("e1", [step(1, "責任者を指名", 0), step(2, "影響確認", 1)], at=at)])
            now = at + timedelta(days=1)                                   # Fri
            texts = {i["text"]: i["due_level"] for i in brief.collect(d, now=now)}
            due0, due1 = brief.due_date(at.isoformat(), 0), brief.due_date(at.isoformat(), 1)
            self.assertIn(f"障害対応の取りまとめ: 責任者を指名（{due0.month}/{due0.day} 期限・超過）", texts)
            self.assertEqual(due1.weekday(), 4)                            # "今週" = that Friday
            self.assertEqual(texts[f"障害対応の取りまとめ: 影響確認（今日）"], 0)

    def test_posted_approvals_replace_queue_rows(self):
        with tempfile.TemporaryDirectory() as d:
            q = {"graph": "workitem-intake", "event_id": "4812", "node": "triage_pm", "advice": "優先度を判定できないチケット"}
            write(d, "queue.jsonl", [q])

            class B:
                def post(self, text, send):
                    return {"ok": True, "typed": True, "sent": bool(send),
                            "readback": {"matched": bool(send), "message_id": "brief-fake" if send else ""}}
            notify.notify(d, B(), send=True)
            texts = [i["text"] for i in brief.collect(d, now=NOW)]
            self.assertEqual(texts, ["確認待ち #1: 優先度を判定できないチケット"])


class TestRankAndFormat(unittest.TestCase):
    def test_due_first_then_harm(self):
        items = [{"key": k, "kind": "step", "text": k, "subject": k, "due_level": lv}
                 for k, lv in (("a", 0), ("b", 1), ("c", 0), ("d", 1))]
        r = brief.rank(items, Fixed({"a": 0.5, "b": 2.9, "c": 1.2, "d": 2.1}), top=3)
        self.assertEqual([i["text"] for i in r], ["c", "a", "b"])

    def test_rank_without_top_returns_every_item_in_order_with_its_rank(self):
        items = [{"key": k, "kind": "step", "text": k, "subject": k, "due_level": lv}
                 for k, lv in (("a", 0), ("b", 1), ("c", 0), ("d", 1))]
        r = brief.rank(items, Fixed({"a": 0.5, "b": 2.9, "c": 1.2, "d": 2.1}))
        self.assertEqual([i["text"] for i in r], ["c", "a", "b", "d"])
        self.assertEqual([i["rank"] for i in r], [1, 2, 3, 4])
        self.assertEqual([i["harm"] for i in r], [1.2, 0.5, 2.9, 2.1])

    def test_due_label_hidden_from_jev(self):
        seen = {}

        class Spy(Fixed):
            def ask(self, state, qs):
                seen.update(state)
                return super().ask(state, qs)
        items = [{"key": "k", "kind": "step", "text": "x: y（今日）", "subject": "x: y", "due_level": 0}]
        brief.rank(items, Spy({}))
        self.assertEqual(seen["items"], ["x: y"])

    def test_empty(self):
        self.assertEqual(brief.rank([], StubBackend()), [])
        self.assertIn("ありません", brief.format_post([], 0, 0, "2026-09-25"))

    def test_post_starts_with_kimeru_and_has_no_reply_lookalikes(self):
        text = brief.format_post([{"text": "確認待ち #1: x"}], 5, 2, "2026-09-25")
        self.assertTrue(text.startswith("[kimeru brief 2026-09-25]"))
        self.assertIn("ほか 4 件", text)
        self.assertFalse(any(notify.REPLY.match(l.strip()) for l in text.splitlines()))

    def test_build_end_to_end_with_stub(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "decisions.jsonl", [decision("e1", [step(1, "社内外への一次連絡", 0), step(2, "影響確認", 1)])])
            text, ranked = brief.build(d, StubBackend(), now=NOW)
            self.assertEqual(len(ranked), 2)
            self.assertIn("1. 障害対応の取りまとめ", text)


class TestTheDefaultBriefTextStaysTheSame(unittest.TestCase):
    """R15: with brief_delta and brief_style at their defaults (0 and rank) the morning brief text is the one captured before the
    delta existed. The fixture is captured from the unchanged code; a change to the default text must show here first."""
    GOLDEN = ("[kimeru brief 2026-09-25] 今日の進め方\n"
              "1. 確認待ち #1: 顧客への一次回答の内容を確認してください\n"
              "2. 確認待ち #2: リリース日の判断が必要です\n"
              "3. 障害対応の取りまとめ: 責任者を指名（今日）\n"
              "ほか 1 件\n"
              "確認待ち 2 件（OK 番号 / NG 番号 / 詳細 番号 で返信）")

    def test_build_with_the_default_settings_gives_the_golden_text_and_the_judge_is_asked_once(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "approvals.json").write_text(json.dumps({"items": {
                "1": {"key": "g:e1:n1", "status": "pending", "posted": True, "record": {
                    "graph": "g", "event_id": "e1", "node": "n1", "advice": "顧客への一次回答の内容を確認してください", "notify": True}},
                "2": {"key": "g:e2:n2", "status": "pending", "posted": True, "record": {
                    "graph": "g", "event_id": "e2", "node": "n2", "advice": "リリース日の判断が必要です"}},
            }}, ensure_ascii=False), encoding="utf-8")
            write(d, "decisions.jsonl", [decision("e4", [step(1, "責任者を指名", 0), step(2, "影響確認", 1)])])
            text, ranked = brief.build(d, StubBackend(), now=NOW, date="2026-09-25")
            self.assertEqual(text, self.GOLDEN)
            self.assertEqual([i["key"] for i in ranked], ["approval:1", "approval:2", "step:e4:incident:s1", "step:e4:incident:s2"])
            self.assertNotIn("（新）", text)
            self.assertNotIn("変わらず", text)


class TestTeamsStatusCheckIsNotCalledAReply(unittest.TestCase):
    """teams.reply is never sent (the text goes back to the self chat, to copy), so the brief must not call a status check a reply."""

    def chat(self, eid, node, name):
        return {"graph": "teams_chat", "event_kind": "teams.chat", "event_id": eid, "node": node, "outcome": "decide",
                "needs_human": False, "notify": False, "at": NOW.isoformat(), "event": {"chat_title": name, "author": name}}

    def test_status_check_is_labelled_as_a_check_and_no_line_looks_like_a_reply(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "decisions.jsonl", [self.chat("c1", "reply_status", "佐藤 花子"), self.chat("c2", "log_fyi", "高橋 健")])
            text, _ = brief.build(d, StubBackend(), now=NOW)
            section = brief.teams_section(d, now=NOW)
            self.assertEqual(section, "Teams の自動判断（直近 24 時間）: 2 件（状況の確認（返信は送っていません） 1 件 / 返信不要 1 件）: 高橋 健、佐藤 花子")
            self.assertIn(section, text)
            self.assertNotIn("自動返信", text)
            self.assertFalse(any(notify.REPLY.match(l.strip()) for l in section.splitlines()))


if __name__ == "__main__":
    unittest.main()
