"""Calibration must only reuse labels with matching graph and judge provenance."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import calibrate, graph, review


class CalibrationProvenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self._review_patches = [mock.patch.object(review, "reviews_path", return_value=self.out / "reviews.jsonl"),
                                mock.patch.object(review, "fixtures_path", return_value=self.out / "fixtures.jsonl")]
        for patcher in self._review_patches:
            patcher.start()
        self.node = {"kind": "judge", "question": {"type": "choice", "instructions": "choose", "criteria": {"a": "A", "b": "B"}},
                     "routes": {"a": "end", "b": "end", "unsure": "end"}, "min_conf": 0.6}
        self.g = {"name": "graph", "event": "x.y", "start": "q", "nodes": {"q": self.node, "end": {"kind": "decide", "actions": []}}}
        self.graphs = {"x.y": [self.g]}
        self.pbs = {"pb": {"id": "pb", "title": "Plan", "steps": [{"id": "one", "desc": "do"}]}}

    def tearDown(self):
        for patcher in reversed(self._review_patches):
            patcher.stop()
        self.tmp.cleanup()

    def seed(self, rec, rev=True):
        review._append(self.out / "decisions.jsonl", rec)
        if rev:
            key = review.decision_key(rec)
            review._append(review.reviews_path(), {"key": key, "verdict": "wrong"})
            review._append(review.fixtures_path(), {"id": "user-" + hashlib.sha1(key.encode()).hexdigest()[:8],
                                                    "event": {}, "expect": {"q": "a"}})

    def record(self, **kw):
        version = hashlib.sha1(json.dumps(self.g, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:8]
        rec = {"graph": "graph", "event_id": "1", "event_kind": "x.y", "node": "q",
               "path": [{"node": "q", "answer": {"choice": "a", "confidence": 0.9}}],
               "judge": {"name": "Kev", "model": "model-one", "model_key": hashlib.sha256(b"model-one").hexdigest()},
               "graph_key": f"graph@{version}:x.y:1", "calibration_key": graph.calibration_key(self.g, self.pbs)}
        rec.update(kw)
        return rec

    def test_calibration_fingerprint_ignores_thresholds_but_covers_graph_meaning(self):
        original = json.loads(json.dumps(self.g))
        before = graph.calibration_key(self.g, self.pbs)
        self.seed(self.record())
        changed = json.loads(json.dumps(self.g))
        changed["nodes"]["q"]["min_conf"] = 0.9
        self.assertEqual(before, graph.calibration_key(changed, self.pbs))
        self.assertEqual(len(calibrate.labeled_questions(self.out, {"x.y": [changed]}, self.pbs)), 1)
        changed["nodes"]["q"]["question"]["instructions"] = "different"
        self.assertNotEqual(before, graph.calibration_key(changed, self.pbs))
        self.assertTrue(before.startswith("v1:"))
        self.assertEqual(self.g, original)

    def test_plan_fingerprint_tracks_playbooks_and_nonplan_does_not(self):
        plan = {"name": "p", "start": "p", "nodes": {"p": {"kind": "plan", "playbooks": "*", "routes": {"ok": "end"}},
                                                               "end": {"kind": "decide", "actions": []}}}
        self.assertNotEqual(graph.calibration_key(plan, self.pbs), graph.calibration_key(plan, {"pb": {"id": "pb", "steps": []}}))
        self.assertEqual(graph.calibration_key(self.g, self.pbs), graph.calibration_key(self.g, {"pb": {"id": "other"}}))

    def test_current_calibration_record_is_reused_but_graph_meaning_change_is_excluded(self):
        self.seed(self.record())
        self.assertEqual(len(calibrate.labeled_questions(self.out, self.graphs, self.pbs)), 1)
        changed = json.loads(json.dumps(self.g))
        changed["nodes"]["q"]["question"]["criteria"]["a"] = "changed"
        self.assertEqual(calibrate.labeled_questions(self.out, {"x.y": [changed]}, self.pbs), [])

    def test_legacy_record_requires_exact_graph_version_and_excludes_plan_nodes(self):
        legacy = self.record()
        legacy.pop("calibration_key")
        self.seed(legacy)
        accepted = calibrate.labeled_questions(self.out, self.graphs, self.pbs)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["graph"], self.g["name"])
        rec = self.record(graph_key="graph@wrong:x.y:1")
        rec.pop("calibration_key")
        self.out.joinpath("decisions.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
        self.assertEqual(calibrate.labeled_questions(self.out, self.graphs, self.pbs), [])

    def test_legacy_plan_answer_has_no_reusable_playbook_provenance(self):
        plan_graph = {"name": "plan-graph", "event": "x.y", "start": "p", "nodes": {
            "p": {"kind": "plan", "playbooks": "*", "routes": {"ok": "end"}},
            "end": {"kind": "decide", "actions": []}}}
        version = hashlib.sha1(json.dumps(plan_graph, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:8]
        rec = {"graph": "plan-graph", "event_id": "2", "event_kind": "x.y", "node": "p",
               "graph_key": f"plan-graph@{version}:x.y:2", "judge": {"name": "Kev", "model": "m"},
               "path": [{"node": "p", "answer": {"playbook": "pb", "confidence": 0.9}}]}
        self.seed(rec)
        row = review.read_jsonl(review.fixtures_path())[-1]
        row["expect"] = {"p": "pb"}
        review.fixtures_path().write_text(json.dumps(row) + "\n", encoding="utf-8")
        diagnostics = {}
        self.assertEqual(calibrate.labeled_questions(self.out, {"x.y": [plan_graph]}, self.pbs, diagnostics), [])
        self.assertEqual(diagnostics.get("legacy_plan"), 1)

    def test_duplicate_decision_key_is_excluded_as_ambiguous(self):
        self.seed(self.record())
        self.seed(self.record(path=None, judge={"name": "Kev", "model": "model-two", "model_key": hashlib.sha256(b"model-two").hexdigest()}), rev=False)
        diagnostics = {}
        self.assertEqual(calibrate.labeled_questions(self.out, self.graphs, self.pbs, diagnostics), [])
        self.assertGreater(diagnostics.get("ambiguous", 0), 0)

    def test_same_judge_name_with_multiple_models_is_not_proposed_or_applied(self):
        data = [{"key": str(i), "node": self.node, "node_id": "q", "ans": {"choice": "a", "confidence": 0.9}, "label": "b",
                 "judge": "Kev", "model": f"m{i % 2}", "model_key": hashlib.sha256(f"m{i % 2}".encode()).hexdigest()} for i in range(120)]
        lines = []
        with mock.patch.object(calibrate, "labeled_questions", return_value=data), \
                mock.patch.object(calibrate, "propose") as proposal, mock.patch.object(calibrate, "apply") as apply:
            self.assertIsNone(calibrate.run(self.out, self.graphs, do_apply=True, print_fn=lines.append))
        proposal.assert_not_called()
        apply.assert_not_called()
        self.assertIn("モデル", "\n".join(lines))

    def test_known_judge_without_model_provenance_is_excluded(self):
        rec = self.record(judge={"name": "Kev", "model": ""})
        self.seed(rec)
        diagnostics = {}
        self.assertEqual(calibrate.labeled_questions(self.out, self.graphs, self.pbs, diagnostics), [])
        self.assertEqual(diagnostics.get("unknown_model"), 1)

    def test_run_rejects_missing_or_invalid_model_provenance_in_mock_questions(self):
        for q in ({"judge": "Kev", "model": ""}, {"judge": "Kev", "model_key": "bad"}):
            lines = []
            with self.subTest(q=q), mock.patch.object(calibrate, "labeled_questions", return_value=[{
                "key": "one", "node": self.node, "node_id": "q", "ans": {"choice": "a", "confidence": 0.9},
                "label": "a", **q}]), mock.patch.object(calibrate, "propose") as propose:
                calibrate.run(self.out, self.graphs, min_questions=1, print_fn=lines.append)
            propose.assert_not_called()
            self.assertIn("較正から除外", "\n".join(lines))

    def test_run_hashes_short_legacy_model_and_skips_unknown_questions(self):
        qs = [
            {"key": "known", "node": self.node, "node_id": "q", "ans": {"choice": "a", "confidence": 0.9},
             "label": "a", "judge": "Kev", "model": "legacy-model"},
            {"key": "unknown", "node": self.node, "node_id": "q", "ans": {"choice": "a", "confidence": 0.9},
             "label": "a", "judge": "Kev"},
        ]
        seen = []
        def proposal(jq, *args):
            seen.extend(jq)
            return None, ["no change"]
        lines = []
        with mock.patch.object(calibrate, "labeled_questions", return_value=qs), mock.patch.object(calibrate, "propose", side_effect=proposal):
            calibrate.run(self.out, self.graphs, min_questions=1, print_fn=lines.append)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["model_key"], hashlib.sha256(b"legacy-model").hexdigest())
        self.assertIn("unknown_model", "\n".join(lines))

    def test_full_model_hash_distinguishes_long_ids_with_same_display_prefix(self):
        from kimeru import cli
        class Backend:
            name = "Kev"
            def __init__(self, model): self.model = model
        prefix = "x" * 60
        a, b = cli._judge_info(Backend(prefix + "a")), cli._judge_info(Backend(prefix + "b"))
        self.assertEqual(a["model"], b["model"])
        self.assertNotEqual(a["model_key"], b["model_key"])

    def test_judgment_result_carries_the_model_and_graph_provenance(self):
        from kimeru import cli
        class Backend:
            NAME = "Kev"
            model = "model-one"
            def ask(self, state, questions):
                return {qid: {"choice": "a", "confidence": 0.9} for qid in questions}
        result = cli._judge(self.g, {"kind": "x.y", "id": "1", "text": "x"}, cli.Meter(Backend()), self.pbs)
        self.assertEqual(result["judge"]["name"], "Kev")
        self.assertEqual(result["judge"]["model_key"], hashlib.sha256(b"model-one").hexdigest())
        self.assertEqual(result["calibration_key"], graph.calibration_key(self.g, self.pbs))

    def test_process_preserves_cached_preview_provenance_and_keeps_untagged_cache_unknown(self):
        from kimeru import cli, fulltext
        class Backend:
            NAME = "Kev"
            model = "model-one"
            def ask(self, state, questions):
                return {qid: {"choice": "a", "confidence": 0.9} for qid in questions}
        g = {"name": "provenance", "event": "teams.chat", "start": "q", "nodes": {
            "q": {"kind": "judge", "question": self.node["question"], "routes": {"a": "end", "b": "end", "unsure": "end"}},
            "end": {"kind": "advise", "advice": "review", "queue": True}}}
        ev = {"kind": "teams.chat", "id": "cache", "chat_id": "19:x@thread.v2", "author": "A", "text": "preview text"}
        class BusyReader:
            def __call__(self, _ev):
                raise fulltext.BudgetExhausted("fixture budget")
        class OtherBackend(Backend):
            model = "model-two"
        busy = lambda _ev: {"ok": False, "why": "unavailable"}
        with mock.patch.object(fulltext, "max_defer", return_value=2):
            with self.assertRaises(fulltext.BudgetExhausted):
                cli.process(ev, {"teams.chat": [g]}, Backend(), self.out, self.pbs, reader=BusyReader())
            pending = json.loads((self.out / "read_pending.json").read_text(encoding="utf-8"))
            self.assertEqual(len(pending), 1)
            result = cli.process(ev, {"teams.chat": [g]}, OtherBackend(), self.out, self.pbs, reader=busy)[0]
        self.assertEqual(result["judge"]["model_key"], hashlib.sha256(b"model-one").hexdigest())
        self.assertEqual(result["calibration_key"], graph.calibration_key(g, self.pbs))

        other_out = self.out / "old-cache"
        other_out.mkdir()
        key = f"{g['name']}@{cli._graph_version(g)}:teams.chat:cache"
        old = graph.run(g, ev, Backend(), playbooks=self.pbs)
        cli._PreviewJudgments(other_out).put(key, old)
        with mock.patch.object(fulltext, "max_defer", return_value=2):
            old_result = cli.process(ev, {"teams.chat": [g]}, OtherBackend(), other_out, self.pbs, reader=busy)[0]
        self.assertNotIn("judge", old_result)
        self.assertNotIn("calibration_key", old_result)

        full_out = self.out / "full-cache"
        full_out.mkdir()
        with mock.patch.object(fulltext, "max_defer", return_value=2):
            with self.assertRaises(fulltext.BudgetExhausted):
                cli.process(ev, {"teams.chat": [g]}, Backend(), full_out, self.pbs, reader=BusyReader())
            full_reader = lambda _ev: {"ok": True, "messages": [{"text": "complete text"}], "returned": True, "restore": "unchanged"}
            full_result = cli.process(ev, {"teams.chat": [g]}, OtherBackend(), full_out, self.pbs, reader=full_reader)[0]
        self.assertEqual(full_result["judge"]["model_key"], hashlib.sha256(b"model-two").hexdigest())

    def test_non_string_or_blank_models_are_not_recorded_as_known_judges(self):
        from kimeru import cli, fulltext
        from kimeru.backends import StubBackend
        g = {"name": "model-provenance", "event": "teams.chat", "start": "q", "nodes": {
            "q": {"kind": "judge", "question": self.node["question"], "routes": {"a": "end", "b": "end", "unsure": "end"}},
            "end": {"kind": "advise", "advice": "review", "queue": True}}}
        ev_template = {"kind": "teams.chat", "chat_id": "19:x@thread.v2", "author": "A", "text": "preview text"}
        class Backend(StubBackend):
            NAME = "Kev"
            def __init__(self, model):
                self.model = model
        for i, model in enumerate((None, 123, "   "), 1):
            ev = {**ev_template, "id": str(i)}
            result = cli.process(ev, {"teams.chat": [g]}, Backend(model), self.out, self.pbs,
                                 reader=lambda _ev: {"ok": False, "why": "unavailable"})[0]
            self.assertEqual(result["judge"]["name"], "Kev")
            self.assertEqual(result["judge"]["model"], "")
            self.assertNotIn("model_key", result["judge"])
            key = review.decision_key(result)
            review._append(review.reviews_path(), {"key": key, "verdict": "wrong"})
            review._append(review.fixtures_path(), {"id": "user-" + hashlib.sha1(key.encode()).hexdigest()[:8],
                                                    "event": {}, "expect": {"q": "a"}})
        diagnostics = {}
        self.assertEqual(calibrate.labeled_questions(self.out, {"teams.chat": [g]}, self.pbs, diagnostics), [])
        self.assertEqual(diagnostics.get("unknown_model"), 3)

    def test_currently_fingerprinted_plan_answers_remain_calibratable(self):
        plan_graph = {"name": "plan-graph", "event": "x.y", "start": "p", "nodes": {
            "p": {"kind": "plan", "playbooks": "*", "routes": {"ok": "end"}},
            "end": {"kind": "decide", "actions": []}}}
        version = hashlib.sha1(json.dumps(plan_graph, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:8]
        rec = {"graph": "plan-graph", "event_id": "3", "event_kind": "x.y", "node": "p",
               "graph_key": f"plan-graph@{version}:x.y:3", "calibration_key": graph.calibration_key(plan_graph, self.pbs),
               "judge": {"name": "Kev", "model": "m"},
               "path": [{"node": "p", "answer": {"playbook": "pb", "confidence": 0.9}}]}
        self.seed(rec)
        row = review.read_jsonl(review.fixtures_path())[-1]
        row["expect"] = {"p": "pb"}
        review.fixtures_path().write_text(json.dumps(row) + "\n", encoding="utf-8")
        questions = calibrate.labeled_questions(self.out, {"x.y": [plan_graph]}, self.pbs)
        self.assertEqual([(q["node_id"], q["label"]) for q in questions], [("p", "pb")])
        changed_pbs = {"pb": {"id": "pb", "title": "changed", "steps": [{"id": "one", "desc": "do"}]}}
        self.assertEqual(calibrate.labeled_questions(self.out, {"x.y": [plan_graph]}, changed_pbs), [])


if __name__ == "__main__":
    unittest.main()
