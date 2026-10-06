"""Regression coverage for calibration score ranges and aggregate perf privacy."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from kimeru import calibrate, review, stats

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("kimeru_eval_run_eval", ROOT / "eval" / "run_eval.py")
RUN_EVAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUN_EVAL)


class TestScoreLabelBoundaries(unittest.TestCase):
    def setUp(self):
        self.node = {
            "kind": "judge",
            "question": {"type": "score", "criteria": ["low", "middle", "high"]},
            "routes": {"bands": [[3, "done"]], "unsure": "human"},
        }

    def test_calibration_matches_evaluator_for_inclusive_range_edges_and_midpoint(self):
        label = [0.5, 1.5]
        for score, expected in ((0.5, True), (1.0, True), (1.5, True), (0.4999, False), (1.5001, False)):
            with self.subTest(score=score):
                answer = {"score": score, "confidence": 0.9}
                self.assertEqual(calibrate._matches(self.node, answer, "done", label), expected)
                self.assertEqual(RUN_EVAL.judge(self.node, label, answer)[0], expected)

    def test_unsure_score_answer_remains_human_routed(self):
        q = {"node": self.node, "ans": {"score": 1.0, "confidence": 0.1}, "label": [0.5, 1.5]}
        self.assertEqual(calibrate._outcome(q, 1.0, 1.0), "human")
        self.assertFalse(RUN_EVAL.judge(self.node, [0.5, 1.5], q["ans"])[0])


class TestPerformanceProvenance(unittest.TestCase):
    now = datetime.now(timezone.utc).isoformat()

    def row(self, judge_marker=...):
        row = {"at": self.now, "perf": {"calls": 1, "questions": 2, "judge_sec": 3.0, "writer_sec": 1.0}}
        if judge_marker is not ...:
            row["judge"] = judge_marker
        return row

    def collect(self, rows, reviews=()):
        with tempfile.TemporaryDirectory() as tmp:
            def read(path):
                if Path(path).name == "decisions.jsonl":
                    return rows
                if Path(path) == review.reviews_path():
                    return list(reviews)
                return []
            with mock.patch.object(stats.review, "read_jsonl", side_effect=read):
                return stats.collect(tmp, now=datetime.now(timezone.utc))

    def summary(self, judges, perf="TIME_SENTINEL"):
        return {
            "since": "2026-10-01", "decisions": 0, "auto": 0, "human": 0, "rule": 0,
            "critical": 0, "per_graph": {}, "approvals": {}, "approvals_undated": 0,
            "invalid_evidence": 0, "future_evidence": 0, "judges": judges, "models": [],
            "reviewed_yes": 0, "reviewed_no": 0, "reviewed_wrong": 0, "reviewed_left_out": 0,
            "perf": perf,
        }

    def render(self, summary, share):
        env = {"kimeru": "1.0", "writer": "none", "os": "test", "python": "3", "display_scale_percent": None}
        with mock.patch.object(stats, "environment", return_value=env):
            return stats.render(summary, share=share)

    def test_collect_fails_closed_for_missing_or_invalid_provenance(self):
        bad = [
            self.row(),
            self.row(None),
            self.row({}),
            self.row({"name": "   "}),
            self.row({"name": None}),
            self.row({"name": []}),
            self.row([]),
            self.row(["malformed"]),
            self.row("malformed"),
            self.row({"name": " UNKNOWN "}),
            self.row({"name": "jev"}),
        ]
        for row in bad:
            with self.subTest(judge=row.get("judge", "missing")):
                result = self.collect([row])
                self.assertEqual(result["perf"], "")
                raw = row.get("judge")
                name = raw.get("name") if isinstance(raw, dict) else None
                expected = ["Jev"] if isinstance(name, str) and name.strip().casefold() == "jev" else ["unknown"]
                self.assertEqual(result["judges"], expected)

    def test_collect_suppresses_mixed_unknown_and_jev_aggregates_but_keeps_known_judges(self):
        safe = self.row({"name": " Kev "})
        custom = self.row({"name": "LocalModel-2"})
        jev = self.row({"name": "JEV"})
        unknown = self.row({"name": "unknown"})
        self.assertTrue(self.collect([safe])["perf"])
        self.assertTrue(self.collect([safe, custom])["perf"])
        self.assertEqual(self.collect([safe, jev])["perf"], "")
        self.assertEqual(self.collect([safe, unknown])["perf"], "")

    def test_review_metadata_cannot_make_jev_or_unknown_agreement_countable(self):
        kev = {**self.row({"name": "Kev"}), "graph": "g", "event_id": "kev", "node": "n"}
        jev = {**self.row({"name": "Jev"}), "graph": "g", "event_id": "jev", "node": "n"}
        kev_key, jev_key = review.decision_key(kev), review.decision_key(jev)
        rows = [kev, jev]
        reviews = [
            {"key": jev_key, "verdict": "yes", "at": self.now, "judge": " JEV "},
            {"key": kev_key, "verdict": "yes", "at": self.now, "judge": " UNKNOWN "},
            {"key": kev_key, "verdict": "yes", "at": self.now, "judge": {"name": "Kev"}},
            {"key": jev_key, "verdict": "yes", "at": self.now, "judge": "  "},
            {"key": kev_key, "verdict": "yes", "at": self.now, "judge": " "},
            {"key": kev_key, "verdict": "yes", "at": self.now},
            {"key": kev_key, "verdict": "yes", "at": self.now, "judge": "Kev"},
        ]
        result = self.collect(rows, reviews)
        self.assertEqual(result["perf"], "")
        self.assertEqual(result["reviewed_yes"], 3)
        self.assertEqual(result["reviewed_left_out"], 4)

    def test_render_guards_legacy_summaries_in_local_and_shared_output(self):
        unsafe = [[], ["unknown"], [" UnKnOwN "], ["jev"], ["Kev", "JEV"], ["Kev", None], ["Kev", "  "]]
        for share in (False, True):
            for judges in unsafe:
                with self.subTest(share=share, judges=judges):
                    self.assertNotIn("TIME_SENTINEL", self.render(self.summary(judges), share))
            self.assertIn("TIME_SENTINEL", self.render(self.summary(["Kev", "CLM", "StubBackend"]), share))


if __name__ == "__main__":
    unittest.main()
