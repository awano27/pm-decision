"""Regression tests for decision review findings."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import unittest
from pathlib import Path
from unittest.mock import patch

from kimeru import events, graph, plan
from kimeru.backends import JevBackend, ReplayBackend

ROOT = Path(__file__).resolve().parent.parent
GRAPHS = graph.load_dir(ROOT / "graphs")


class TestAdoMaterial(unittest.TestCase):
    def setUp(self):
        self.payload = {"resource": {"id": 17, "fields": {
            "System.Title": "障害",
            "System.Description": "概要はここ",
            "Microsoft.VSTS.TCM.ReproSteps": "実際の再現手順はここ",
        }}}

    def test_description_and_repro_steps_survive_normalization_and_state(self):
        event = events.ado_workitem_created(self.payload)[0]
        self.assertEqual(event["description"], "概要はここ")
        self.assertEqual(event["repro_steps"], "実際の再現手順はここ")
        state = events.state_of(event)
        self.assertEqual(state["description"], "概要はここ")
        self.assertEqual(state["repro_steps"], "実際の再現手順はここ")

    def test_repro_steps_alone_can_trigger_critical_safety_rule(self):
        event = {"kind": "ado.workitem.created", "id": "18", "title": "ログイン障害",
                 "description": "", "repro_steps": "本番で全ユーザーがログインできない",
                 "acceptance_criteria": ""}
        result = graph.run(GRAPHS["ado.workitem.created"][0], event, ReplayBackend({
            "ready": {"noul": 0.1}, "priority": {"score": 0.0, "confidence": 0.99},
        }))
        self.assertEqual(result["node"], "set_p1_critical")

    def test_repro_steps_alone_satisfy_explicit_reproduction_rule(self):
        event = {"kind": "ado.workitem.created", "id": "19", "title": "再現手順付きバグ",
                 "description": "", "repro_steps": "再現手順: 保存後に画面を再読み込みする",
                 "acceptance_criteria": ""}
        result = graph.run(GRAPHS["ado.workitem.created"][0], event, ReplayBackend({
            "ready": {"noul": 0.1},
            "priority": {"score": 0.0, "confidence": 0.99},
        }))
        step = next(step for step in result["path"] if step["node"] == "has_repro")
        self.assertEqual(step["edge"], "yes")


class TestOutageScope(unittest.TestCase):
    def setUp(self):
        self.alert = GRAPHS["monitor.alert"][0]
        self.rule = self.alert["nodes"]["critical_outage"]

    def edge(self, title, description=""):
        return graph.match_eval(self.rule, {"rule": title, "description": description})[0]

    def test_scope_alone_does_not_claim_outage(self):
        self.assertEqual(self.edge("All regions are in scope"), "no")

    def test_current_production_failure_still_triggers_safety_rule(self):
        self.assertEqual(self.edge("All regions are currently down"), "yes")
        self.assertEqual(self.edge("全リージョンで現在障害が発生しています"), "yes")

    def test_future_expiry_and_healthy_state_do_not_claim_outage(self):
        self.assertEqual(self.edge("All regions will be down after the certificate expires in 14 days"), "no")
        self.assertEqual(self.edge("All regions are healthy and available"), "no")
        self.assertEqual(self.edge("全ユーザーに障害が出る可能性があります"), "no")
        self.assertEqual(self.edge("全ユーザーは現在正常に利用できます"), "no")

    def test_negated_current_failure_is_not_an_outage(self):
        self.assertEqual(self.edge("全ユーザーは現在障害なく利用できます"), "no")

    def test_tomorrow_error_possibility_is_not_an_outage(self):
        self.assertEqual(self.edge("全リージョンで明日エラーが発生する可能性があります"), "no")

    def test_current_failure_still_matches_when_future_risk_is_also_mentioned(self):
        text = "全リージョンで現在障害が発生していますが、明日は別のエラーが発生する可能性があります"
        self.assertEqual(self.edge(text), "mixed")
        event = {"kind": "monitor.alert", "id": "mixed-1", "rule": "availability",
                 "condition": "Fired", "description": text, "severity": "Sev1"}
        result = graph.run(self.alert, event, ReplayBackend({}))
        self.assertEqual(result["node"], "page_advice")
        self.assertTrue(result["needs_human"])


class TestTypedAnswers(unittest.TestCase):
    def test_jev_backend_rejects_nonfinite_typed_answer(self):
        backend = JevBackend(key_required=False, retries=1, api="http://127.0.0.1:1")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"answers":{"q":{"noul":NaN}}}'

        with patch("kimeru.backends._urlopen", return_value=Response()):
            with self.assertRaises(ValueError):
                backend.ask({}, {"q": {"type": "noul", "instructions": "x"}})

    def test_graph_route_rejects_invalid_value_instead_of_selecting_terminal(self):
        node = {"kind": "judge", "question": {"type": "noul"},
                "routes": {"yes": "automatic", "no": "automatic", "unsure": "review"}}
        for answer in ({"noul": float("nan")}, {"noul": 1.01}, {}):
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                graph.route(node, answer)

    def test_nonfinite_confidence_cannot_pass_threshold(self):
        node = {"kind": "judge", "question": {"type": "noul"},
                "routes": {"yes": "automatic", "no": "automatic", "unsure": "review"}}
        for confidence in (float("nan"), float("inf")):
            with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                graph.route(node, {"noul": 0.99, "confidence": confidence})

    def test_graph_route_rejects_unknown_choice(self):
        node = {"kind": "judge", "question": {"type": "choice", "criteria": {"a": "A", "b": "B"}},
                "routes": {"a": "automatic", "b": "automatic", "unsure": "review"}, "min_conf": 0.5}
        with self.assertRaises(ValueError):
            graph.route(node, {"choice": "made_up", "confidence": 0.99})

    def test_graph_route_rejects_score_above_question_range(self):
        node = {"kind": "judge", "question": {"type": "score", "criteria": ["a", "b", "c"]},
                "routes": {"bands": [[0.75, "low"], [1.6, "middle"], [3, "high"]], "unsure": "review"}}
        self.assertEqual(graph.route(node, {"score": 2.6, "confidence": 0.99}), ("<3", "high"))
        with self.assertRaises(ValueError):
            graph.route(node, {"score": 3.0, "confidence": 0.99})

    def test_plan_rejects_malformed_answer_before_route_selection(self):
        node = {"kind": "plan", "playbooks": ["p"],
                "routes": {"ok": "automatic", "none": "automatic", "unsure": "review"}}
        playbooks = {"p": {"id": "p", "title": "p", "when": "p", "steps": [
            {"id": "one", "title": "one", "desc": "one"},
            {"id": "two", "title": "two", "desc": "two"},
        ]}}
        backend = ReplayBackend({"playbook": {"choice": "missing", "confidence": 0.99}})
        with self.assertRaises(ValueError):
            plan.build(node, {}, backend, playbooks)

    def test_optional_partial_probability_map_remains_supported(self):
        node = {"kind": "judge", "question": {"type": "score", "criteria": ["a", "b", "c"]},
                "routes": {"bands": [[0.75, "low"], [1.6, "middle"], [3, "high"]], "unsure": "review"},
                "min_conf": 0.3, "adjacent_only": True}
        self.assertEqual(graph.route(node, {"score": 1.2, "confidence": 0.8, "probabilities": {"1": 0.8}}),
                         ("<1.6", "middle"))


def _linear_graph(match_nodes):
    nodes = {}
    for i in range(match_nodes):
        target = f"m{i + 1}" if i + 1 < match_nodes else "end"
        nodes[f"m{i}"] = {"kind": "match", "fields": ["text"], "patterns": ["hit"],
                           "routes": {"yes": target, "no": f"stop{i}"}}
        nodes[f"stop{i}"] = {"kind": "decide", "actions": []}
    nodes["end"] = {"kind": "decide", "actions": []}
    return {"name": "depth", "start": "m0" if match_nodes else "end", "nodes": nodes}


class TestGraphBounds(unittest.TestCase):
    def test_execution_path_at_limit_is_valid(self):
        graph.validate(_linear_graph(graph.MAX_STEPS - 1))

    def test_execution_path_over_limit_is_rejected_during_validation(self):
        with self.assertRaises(graph.GraphError):
            graph.validate(_linear_graph(graph.MAX_STEPS))

    def test_large_shallow_graph_validates(self):
        options = {f"o{i}": f"O{i}" for i in range(3000)}
        routes = {key: f"end{i}" for i, key in enumerate(options)}
        routes["unsure"] = "review"
        nodes = {"start": {"kind": "judge", "question": {
            "type": "choice", "instructions": "pick", "criteria": options,
        }, "routes": routes}}
        nodes.update({f"end{i}": {"kind": "decide", "actions": []} for i in range(len(options))})
        nodes["review"] = {"kind": "advise", "advice": "review"}
        graph.validate({"name": "wide", "start": "start", "nodes": nodes})


if __name__ == "__main__":
    unittest.main()
