"""Offline regressions for Jev's evidence-based AI/human decision boundary."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from kimeru import ai_suitability, cli, events, graph, learn
from kimeru.backends import BackendAnswerError, BackendUnavailable, ReplayBackend, StubBackend


ROOT = Path(__file__).resolve().parent.parent
GRAPHS = graph.load_dir(ROOT / "graphs")


class FakeJev(ReplayBackend):
    NAME = "Jev"

    def __init__(self, level=5, confidence=0.95, reason="general_knowledge", reason_confidence=0.95,
                 answers=None):
        super().__init__({
            "ai_suitability": {"choice": str(level), "confidence": confidence},
            "ai_suitability_reason": {"choice": reason, "confidence": reason_confidence},
            **(answers or {}),
        })
        self.calls = []

    def ask(self, state, questions):
        self.calls.append((copy.deepcopy(state), copy.deepcopy(questions)))
        return super().ask(state, questions)


def result(**fields):
    return {"graph": "synthetic", "node": "decision", "path": [], "outcome": "decide",
            "actions": [{"type": "teams.reply", "text": "proposed answer"}],
            "needs_human": False, "notify": False, **fields}


class TestSuitabilityAssessment(unittest.TestCase):
    def test_each_level_has_a_discrete_rating_and_routes_at_four(self):
        for level in range(1, 6):
            with self.subTest(level=level):
                backend = FakeJev(level=level)
                assessment = ai_suitability.assess({"text": "synthetic request"}, backend)
                self.assertEqual(assessment["level"], level)
                self.assertIs(type(assessment["level"]), int)
                self.assertEqual(assessment["route"], "human" if level <= 3 else "ai")
                self.assertEqual(assessment["status"], "assessed")
                self.assertEqual(len(backend.calls), 1)
                self.assertEqual(set(backend.calls[0][1]), {"ai_suitability", "ai_suitability_reason"})

    def test_both_confidences_must_reach_the_fixed_boundary(self):
        for confidence, reason_confidence, expected in (
            (0.70, 0.70, "ai"), (0.6999, 0.99, "human"), (0.99, 0.6999, "human"),
        ):
            with self.subTest(confidence=confidence, reason_confidence=reason_confidence):
                backend = FakeJev(4, confidence, reason_confidence=reason_confidence)
                backend.profile = {"name": "jev", "conf_scale": 0.1, "noul_scale": 0.1}
                assessment = ai_suitability.assess({}, backend)
                self.assertEqual(assessment["level"], 4)
                self.assertEqual(assessment["route"], expected)
                if expected == "human":
                    self.assertEqual(assessment["reason_code"], "low_confidence")

    def test_missing_context_or_authority_overrides_a_high_rating(self):
        for reason in ("missing_local_context", "human_authority", "unclear_request"):
            with self.subTest(reason=reason):
                assessment = ai_suitability.assess({}, FakeJev(5, reason=reason))
                self.assertEqual(assessment["level"], 5)
                self.assertEqual(assessment["route"], "human")
                self.assertEqual(assessment["reason_code"], reason)

    def test_complete_local_evidence_can_be_judged_by_ai(self):
        assessment = ai_suitability.assess(
            {"text": "Aは10円、Bは20円。同条件なら価格が安い方を選んでください。"},
            FakeJev(4, reason="supplied_evidence"),
        )
        self.assertEqual((assessment["level"], assessment["route"]), (4, "ai"))

    def test_missing_and_malformed_answers_fail_closed(self):
        valid = FakeJev().answers
        invalid = [None, [], {}, {"ai_suitability": valid["ai_suitability"]}]
        for answer in (
            {"choice": "6", "confidence": 0.99},
            {"choice": 5, "confidence": 0.99},
            {"choice": "5", "confidence": float("nan")},
            {"choice": "5", "confidence": float("inf")},
            {"choice": "5", "confidence": True},
            {"choice": "5", "confidence": 1.01},
            {"choice": "5"},
            {"choice": "5", "confidence": 0.99, "probabilities": {"6": 1}},
            {"choice": "5", "confidence": 0.99, "probabilities": {"5": float("nan")}},
            {"choice": [], "confidence": 0.99},
        ):
            invalid.append({**valid, "ai_suitability": answer})
        invalid.append({**valid, "ai_suitability_reason": {"choice": "unknown", "confidence": 1}})
        invalid.append({**valid, "ai_suitability_reason": {"choice": "general_knowledge", "confidence": float("nan")}})
        for answers in invalid:
            with self.subTest(answers=answers):
                backend = FakeJev()
                with mock.patch.object(backend, "ask", return_value=answers):
                    assessment = ai_suitability.assess({}, backend)
                self.assertEqual((assessment["status"], assessment["route"]), ("unavailable", "human"))
                self.assertEqual(assessment["reason_code"], "invalid_answer")
                self.assertIsNone(assessment["level"])

    def test_backend_errors_fail_closed_without_retaining_the_error_text(self):
        for error, code in (
            (BackendUnavailable("SYNTHETIC-PRIVATE-DETAIL"), "backend_unavailable"),
            (RuntimeError("SYNTHETIC-PRIVATE-DETAIL"), "backend_unavailable"),
            (TimeoutError("SYNTHETIC-PRIVATE-DETAIL"), "backend_unavailable"),
            (BackendAnswerError("SYNTHETIC-PRIVATE-DETAIL"), "invalid_answer"),
            (KeyError("SYNTHETIC-PRIVATE-DETAIL"), "invalid_answer"),
        ):
            with self.subTest(error=type(error).__name__):
                backend = FakeJev()
                with mock.patch.object(backend, "ask", side_effect=error):
                    assessment = ai_suitability.assess({}, backend)
                self.assertEqual(assessment["route"], "human")
                self.assertEqual(assessment["reason_code"], code)
                self.assertNotIn("SYNTHETIC-PRIVATE-DETAIL", str(assessment))

    def test_summary_explains_score_route_and_unavailable_result(self):
        assessment = ai_suitability.assess({}, FakeJev(4, reason="supplied_evidence"))
        self.assertIn("AI判断適性: 4/5 / AI判断可能", ai_suitability.summary(assessment))
        self.assertIn(assessment["reason"], ai_suitability.summary(assessment))
        backend = FakeJev()
        with mock.patch.object(backend, "ask", return_value={}):
            unavailable = ai_suitability.assess({}, backend)
        self.assertIn("評価不能 / 人に確認", ai_suitability.summary(unavailable))


class TestSuitabilityHandoff(unittest.TestCase):
    def test_human_route_preserves_identity_proposals_and_existing_reasons(self):
        record = result(review_reasons=["existing review reason"], notify=True,
                        plan={"steps": [{"id": "confirm"}]})
        original = copy.deepcopy(record)
        ai_suitability.apply(record, {"text": "local request"}, FakeJev(2, reason="missing_local_context"))
        self.assertEqual(record["outcome"], "advise")
        self.assertTrue(record["needs_human"])
        self.assertTrue(record["review_required"])
        self.assertFalse(record["notify"])
        for key in ("graph", "node", "path", "actions", "plan"):
            self.assertEqual(record[key], original[key])
        self.assertEqual(record["review_reasons"][0], "existing review reason")
        self.assertIn(record["ai_suitability"]["reason"], record["review_reasons"])
        drafted, _ = cli._draft(record, {}, cli.Meter(FakeJev()), None)
        self.assertEqual(drafted["outcome"], "advise")
        self.assertEqual(drafted["actions"], original["actions"])

    def test_high_rating_does_not_clear_existing_review_or_execution_gates(self):
        record = result(outcome="advise", needs_human=True, review_required=True,
                        review_reasons=["approval required"], notify=True)
        original = copy.deepcopy(record)
        ai_suitability.apply(record, {}, FakeJev(5))
        self.assertEqual(record["ai_suitability"]["route"], "ai")
        for key, value in original.items():
            self.assertEqual(record[key], value)

    def test_jev_is_recognized_inside_meter_wrappers(self):
        backend = FakeJev(2)
        wrapped = cli.Meter(cli.Meter(backend))
        record = ai_suitability.apply(result(), {}, wrapped)
        self.assertTrue(record["needs_human"])
        self.assertEqual((wrapped.calls, wrapped.questions), (1, 2))

    def test_other_backends_do_not_gain_a_new_call_or_result_field(self):
        class FakeKev(FakeJev):
            NAME = "Kev"

        class FakeClm(FakeJev):
            NAME = "CLM"

        for backend in (StubBackend(), ReplayBackend({}), FakeKev(), FakeClm()):
            with self.subTest(backend=type(backend).__name__):
                record = result()
                original = copy.deepcopy(record)
                with mock.patch.object(backend, "ask", side_effect=AssertionError("unexpected call")):
                    ai_suitability.apply(record, {}, backend)
                self.assertEqual(record, original)

    def test_only_pure_log_only_outcomes_skip_the_extra_assessment(self):
        for overrides, expected_calls in (
            ({"actions": [{"type": "log.only"}]}, 0),
            ({"actions": [{"type": "log.only"}], "notify": True}, 1),
            ({"actions": [{"type": "log.only"}], "needs_human": True}, 1),
            ({"actions": [{"type": "log.only"}], "review_required": True}, 1),
            ({"actions": [{"type": "log.only"}], "outcome": "advise"}, 1),
            ({"actions": []}, 1),
        ):
            with self.subTest(overrides=overrides):
                backend = FakeJev()
                ai_suitability.apply(result(**overrides), {}, backend)
                self.assertEqual(len(backend.calls), expected_calls)


class TestSuitabilityLearnedRules(unittest.TestCase):
    def test_learned_rule_cannot_discard_a_human_suitability_verdict(self):
        event = {"kind": "meeting.item", "id": "synthetic", "meeting": "週次定例",
                 "item": "見積りの件は後で整理する。来週の会議で扱う"}
        g = GRAPHS[event["kind"]][0]
        now = datetime(2026, 11, 15, 9)
        rules = {"next": 2, "rules": [{"id": "A1", "graph": "minutes-followup", "node": "check_line",
                 "phrase": "見積りの件は後で整理する", "adopted_at": "2026-11-14T09:00:00",
                 "uses": 0, "active": True, "judge": "Jev"}]}
        for level in (2, 5):
            with self.subTest(level=level), tempfile.TemporaryDirectory() as folder:
                out = Path(folder)
                rule_path = out / "learned_rules.json"
                rule_path.write_text(json.dumps(rules), encoding="utf-8")
                backend = FakeJev(level, answers={"item_type": {"choice": "decision", "confidence": 0.3}})
                record = graph.run(g, event, backend)
                self.assertEqual(record["node"], "check_line")
                with mock.patch.dict(os.environ, {"KIMERU_LEARN": "1"}):
                    updated = learn.demote(out, g, record, event, now=now)
                stored_rule = json.loads(rule_path.read_text(encoding="utf-8"))["rules"][0]
                if level == 2:
                    self.assertEqual(updated["outcome"], "advise")
                    self.assertTrue(updated["needs_human"])
                    self.assertEqual(updated["actions"], record["actions"])
                    self.assertNotIn("demoted_by", updated)
                    self.assertEqual(stored_rule["uses"], 0)
                else:
                    self.assertEqual(updated["outcome"], "decide")
                    self.assertEqual(updated["actions"], [{"type": "log.only"}])
                    self.assertEqual(updated["demoted_by"], "A1")
                    self.assertEqual(stored_rule["uses"], 1)


class TestSuitabilityGraphIntegration(unittest.TestCase):
    @staticmethod
    def custom_graph():
        return {"name": "custom-decision", "event": "teams.chat", "start": "decision", "nodes": {
            "decision": {"kind": "decide", "actions": [{"type": "teams.reply", "text": "proposed answer"}]},
        }}

    def test_teams_fyi_keeps_the_original_log_only_path(self):
        backend = FakeJev(answers={"intent": {"choice": "fyi", "confidence": 0.99}})
        record = graph.run(GRAPHS["teams.chat"][0], {"kind": "teams.chat", "text": "共有のみです"}, backend)
        self.assertEqual(record["node"], "log_fyi")
        self.assertNotIn("ai_suitability", record)
        self.assertEqual(len(backend.calls), 1)

    def test_rule_decided_work_item_still_gets_the_human_gate(self):
        event = {"kind": "ado.workitem.created", "id": "synthetic", "title": "ログイン障害",
                 "description": "本番で全ユーザーがログインできない"}
        backend = FakeJev(2, reason="missing_local_context")
        record = graph.run(GRAPHS[event["kind"]][0], event, backend)
        self.assertEqual(record["node"], "set_p1_critical")
        self.assertEqual(record["outcome"], "advise")
        self.assertTrue(record["review_required"])
        self.assertEqual(len(backend.calls), 1)
        self.assertTrue(all("ai_suitability" not in step["node"] for step in record["path"]))

    def test_actual_decision_receives_sanitized_source_state_only(self):
        event = {"kind": "teams.chat", "id": "synthetic", "text": "一般的な比較をお願いします。",
                 "raw": "hidden raw", "thread": "hidden thread", "full": "hidden full",
                 "chat_title": "hidden title", "origin": "hidden origin", "rev": 12,
                 "_kimeru": {"synthetic": "hidden measurement"}}
        backend = FakeJev(answers={"intent": {"choice": "status", "confidence": 0.99}})
        record = graph.run(GRAPHS[event["kind"]][0], event, backend)
        self.assertEqual(record["ai_suitability"]["route"], "ai")
        assessment_state, questions = backend.calls[-1]
        self.assertIn("ai_suitability", questions)
        self.assertEqual(assessment_state, events.state_of(event))
        for hidden in events.JUDGE_HIDDEN:
            self.assertNotIn(hidden, assessment_state)
        self.assertNotIn("actions", assessment_state)
        self.assertNotIn("plan", assessment_state)

    def test_known_clipping_cannot_authorize_ai_even_with_a_high_rating(self):
        event = {"kind": "teams.chat", "text": "一般論の説明。" * 80 + "現場の重要条件は未確認です。"}
        backend = FakeJev(5, answers={"intent": {"choice": "status", "confidence": 0.99}})
        with mock.patch.dict(os.environ, {"KIMERU_JUDGE_TEXT_MAX": "100"}):
            record = graph.run(GRAPHS[event["kind"]][0], event, backend)
        self.assertEqual(record["ai_suitability"]["level"], 5)
        self.assertEqual(record["ai_suitability"]["route"], "human")
        self.assertEqual(record["ai_suitability"]["reason_code"], "truncated_input")
        self.assertTrue(record["review_required"])
        self.assertEqual(len(backend.calls[-1][0]["text"]), 100)
        self.assertNotIn("現場の重要条件", str(backend.calls[-1][0]))

    def test_custom_graph_hands_off_ellipsis_previews_even_with_a_high_rating(self):
        for marker in ("…", "...", "‥"):
            with self.subTest(marker=marker):
                event = {"kind": "teams.chat", "text": "一般的な判断ですが、今回の前提は" + marker + "  "}
                record = graph.run(self.custom_graph(), event, FakeJev(5))
                self.assertEqual(record["ai_suitability"]["level"], 5)
                self.assertEqual(record["ai_suitability"]["route"], "human")
                self.assertEqual(record["ai_suitability"]["reason_code"], "truncated_input")
                self.assertEqual(record["outcome"], "advise")
                self.assertTrue(record["review_required"])
                self.assertTrue(record["needs_human"])

    def test_custom_graph_hands_off_at_configured_preview_length_boundary(self):
        for length, expected in ((19, "ai"), (20, "human"), (21, "human")):
            with self.subTest(length=length), mock.patch.dict(os.environ, {"KIMERU_PREVIEW_CUT_LEN": "20"}):
                event = {"kind": "teams.chat", "text": "あ" * length}
                record = graph.run(self.custom_graph(), event, FakeJev(5))
                self.assertEqual(record["ai_suitability"]["route"], expected)
                self.assertEqual(record["needs_human"], expected == "human")

    def test_known_full_text_does_not_use_the_preview_clipping_heuristic(self):
        for text in ("省略記号で終わる完全なメッセージです…", "あ" * 30):
            with self.subTest(text=text), mock.patch.dict(os.environ, {"KIMERU_PREVIEW_CUT_LEN": "20"}):
                event = {"kind": "teams.chat", "text": text, "full": True}
                record = graph.run(self.custom_graph(), event, FakeJev(5))
                self.assertEqual(record["ai_suitability"]["route"], "ai")
                self.assertEqual(record["outcome"], "decide")
                self.assertFalse(record["needs_human"])

    def test_known_full_text_still_hands_off_when_judge_input_is_capped(self):
        event = {"kind": "teams.chat", "text": "あ" * 150, "full": True}
        with mock.patch.dict(os.environ, {"KIMERU_JUDGE_TEXT_MAX": "100", "KIMERU_PREVIEW_CUT_LEN": "20"}):
            record = graph.run(self.custom_graph(), event, FakeJev(5))
        self.assertEqual(record["ai_suitability"]["route"], "human")
        self.assertEqual(record["ai_suitability"]["reason_code"], "truncated_input")
        self.assertEqual(record["outcome"], "advise")

    def test_capped_ado_evidence_cannot_authorize_ai(self):
        g = {"name": "custom-ado", "event": "ado.workitem.created", "start": "decision", "nodes": {
            "decision": {"kind": "decide", "actions": [{"type": "ado.update", "id": "synthetic",
                          "fields": {"Microsoft.VSTS.Common.Priority": 3}}]},
        }}
        for field in ("description", "repro_steps", "acceptance_criteria", "test_steps"):
            with self.subTest(field=field), mock.patch.dict(os.environ, {"KIMERU_JUDGE_TEXT_MAX": "100"}):
                event = {"kind": "ado.workitem.created", "id": "synthetic", field: "あ" * 120 + "現場条件は未確認"}
                backend = FakeJev(5)
                record = graph.run(g, event, backend)
                self.assertEqual(record["ai_suitability"]["level"], 5)
                self.assertEqual(record["ai_suitability"]["route"], "human")
                self.assertEqual(record["ai_suitability"]["reason_code"], "truncated_input")
                self.assertEqual(record["outcome"], "advise")
                self.assertEqual(len(backend.calls[-1][0][field]), 100)
                self.assertNotIn("現場条件", str(backend.calls[-1][0]))


if __name__ == "__main__":
    unittest.main()
