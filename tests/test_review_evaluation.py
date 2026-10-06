"""Offline regression tests for weekly evidence and end-to-end evaluation."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
import importlib.util

from kimeru import stats

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("review_e2e", ROOT / "eval" / "e2e.py")
e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e)


class TestWeeklyEvidence(unittest.TestCase):
    def test_future_and_invalid_timestamps_are_excluded_and_reported(self):
        rows = [
            {"at": "2026-10-02T12:00:00Z", "graph": "teams", "needs_human": False, "path": []},
            {"at": "2026-10-04T12:00:00Z", "graph": "future", "needs_human": False, "path": []},
            {"at": "not-a-date", "graph": "invalid", "needs_human": False, "path": []},
        ]
        def read_jsonl(path):
            return rows if Path(path).name == "decisions.jsonl" else []

        now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(stats.review, "read_jsonl", side_effect=read_jsonl), \
                mock.patch.object(stats.review, "reviews_path", return_value=Path(d) / "reviews.jsonl"):
            summary = stats.collect(d, now=now)
            rendered = stats.render(summary)

        self.assertEqual(summary["decisions"], 1)
        self.assertEqual(summary["invalid_evidence"], 1)
        self.assertEqual(summary["future_evidence"], 1)
        self.assertIn("日時が不正な記録 1 件", rendered)
        self.assertIn("未来の日時の記録 1 件", rendered)


class AnswerBackend:
    def __init__(self, answers):
        self.answers = answers
        self.calls = 0

    def ask(self, state, questions):
        self.calls += 1
        return {q: self.answers[q] for q in questions}


def terminal(name, action):
    return {"kind": "decide", "actions": [{"type": action}]}


class TestFinalActionScoring(unittest.TestCase):
    def run_graph(self, g, answers, labels):
        kind = "test.review"
        fx = {"id": "case", "event": {"kind": kind}, "expect": labels}
        with mock.patch.dict(e2e.GRAPHS, {kind: g}):
            return e2e.run_one(fx, AnswerBackend(answers))

    def test_only_the_terminal_immediately_selected_by_unsure_is_safe_fallback(self):
        g = {"name": "direct", "event": "test.review", "start": "q", "nodes": {
            "q": {"kind": "judge", "question": {"type": "noul", "instructions": "x"},
                  "routes": {"yes": "normal", "no": "normal", "unsure": "safe"}},
            "normal": terminal("normal", "log.only"), "safe": terminal("safe", "log.only")}}
        r = self.run_graph(g, {"q": {"noul": 0.5}}, {"q": True})
        self.assertEqual(r["outcome"], "fallback")

    def test_unsure_followed_by_another_judge_is_not_safe_when_final_action_differs(self):
        g = {"name": "continued", "event": "test.review", "start": "q1", "nodes": {
            "q1": {"kind": "judge", "question": {"type": "noul", "instructions": "x"},
                   "routes": {"yes": "ideal", "no": "ideal", "unsure": "q2"}},
            "q2": {"kind": "judge", "question": {"type": "choice", "instructions": "x", "criteria": {"a": "A", "b": "B"}},
                   "routes": {"a": "safe", "b": "wrong", "unsure": "safe"}},
            "ideal": terminal("ideal", "log.only"), "safe": terminal("safe", "log.only"),
            "wrong": terminal("wrong", "teams.post")}}
        r = self.run_graph(g, {"q1": {"noul": 0.5}, "q2": {"choice": "b", "confidence": 0.9}}, {"q1": True})
        self.assertEqual(r["outcome"], "wrong")

    def test_severe_miss_cannot_be_labeled_safe_fallback(self):
        g = {"name": "severe", "event": "test.review", "start": "q", "nodes": {
            "q": {"kind": "judge", "question": {"type": "noul", "instructions": "x"},
                  "routes": {"yes": "page", "no": "page", "unsure": "safe"}},
            "page": terminal("page", "oncall.page"), "safe": terminal("safe", "log.only")}}
        r = self.run_graph(g, {"q": {"noul": 0.5}}, {"q": True})
        self.assertTrue(r["severe_miss"])
        self.assertEqual(r["outcome"], "wrong")

    def test_any_accepted_choice_label_uses_that_oracle_branch_in_one_walk(self):
        g = {"name": "alternatives", "event": "test.review", "start": "q", "nodes": {
            "q": {"kind": "judge", "question": {"type": "choice", "instructions": "x", "criteria": {"first": "1", "second": "2"}},
                  "routes": {"first": "one", "second": "two", "unsure": "safe"}},
            "one": terminal("one", "log.only"), "two": terminal("two", "teams.post"),
            "safe": terminal("safe", "log.only")}}
        backend = AnswerBackend({"q": {"choice": "second", "confidence": 0.9}})
        fx = {"id": "case", "event": {"kind": "test.review"}, "expect": {"q": ["first", "second"]}}
        with mock.patch.dict(e2e.GRAPHS, {"test.review": g}):
            r = e2e.run_one(fx, backend)
        self.assertEqual(r["outcome"], "correct")
        self.assertEqual(backend.calls, 1)


if __name__ == "__main__":
    unittest.main()
