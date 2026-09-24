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

    def test_posted_approvals_replace_queue_rows(self):
        with tempfile.TemporaryDirectory() as d:
            q = {"graph": "workitem-intake", "event_id": "4812", "node": "triage_pm", "advice": "優先度を判定できないチケット"}
            write(d, "queue.jsonl", [q])

            class B:
                def post(self, text, send): return {"ok": True}
            notify.notify(d, B(), send=True)
            texts = [i["text"] for i in brief.collect(d, now=NOW)]
            self.assertEqual(texts, ["確認待ち #1: 優先度を判定できないチケット"])


class TestRankAndFormat(unittest.TestCase):
    def test_due_first_then_harm(self):
        items = [{"key": k, "kind": "step", "text": k, "subject": k, "due_level": lv}
                 for k, lv in (("a", 0), ("b", 1), ("c", 0), ("d", 1))]
        r = brief.rank(items, Fixed({"a": 0.5, "b": 2.9, "c": 1.2, "d": 2.1}), top=3)
        self.assertEqual([i["text"] for i in r], ["c", "a", "b"])

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


if __name__ == "__main__":
    unittest.main()
