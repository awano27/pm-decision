try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import unittest
from pathlib import Path

from kimeru import graph, plan
from kimeru.backends import StubBackend

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
ADO = graph.load_dir(ROOT / "graphs", PBS)["ado.workitem.created"][0]


class LowAnswers(StubBackend):
    """Model that would push everything to the lowest priority: a rule miss shows up as P3."""

    def ask(self, state, questions):
        out = super().ask(state, questions)
        for q, v in questions.items():
            if v.get("type") == "score":
                out[q] = {"score": 0.0, "confidence": 0.99, "probabilities": {"0": 0.99}}
        return out


def run(title, description=""):
    ev = {"kind": "ado.workitem.created", "id": "w1", "work_item_type": "Bug", "title": title,
          "description": description, "acceptance_criteria": ""}
    return graph.run(ADO, ev, LowAnswers(), playbooks=PBS)


class TestCriticalRule(unittest.TestCase):
    def test_test_env_note_in_other_field_does_not_cancel_production_outage(self):
        r = run("本番で全ユーザーがログインできない", "テスト環境での再現手順を追記")
        self.assertEqual(r["path"][0]["edge"], "yes")
        self.assertEqual(r["node"], "set_p1_critical")

    def test_production_and_test_env_in_same_field_goes_to_a_person(self):
        r = run("本番で全ユーザーがログインできない。テスト環境でも再現する")
        self.assertEqual(r["path"][0]["edge"], "mixed")
        self.assertTrue(r["needs_human"])

    def test_test_env_only_crash_is_not_p1(self):
        r = run("テスト環境でアプリがクラッシュする")
        self.assertEqual(r["path"][0]["edge"], "no")

    def test_mixed_route_is_validated(self):
        g = {"name": "t", "start": "m", "nodes": {
            "m": {"kind": "match", "fields": ["text"], "patterns": ["a"], "mixed_if": ["b"],
                  "routes": {"yes": "d", "no": "d"}},
            "d": {"kind": "decide", "actions": []}}}
        with self.assertRaises(graph.GraphError):
            graph.validate(g)


class TestValidate(unittest.TestCase):
    def judge(self, q, routes, **extra):
        return {"name": "t", "start": "j", "nodes": {
            "j": {"kind": "judge", "question": q, "routes": routes, **extra},
            "d": {"kind": "decide", "actions": []}}}

    def test_rejects_malformed_questions(self):
        bad = [
            self.judge({"type": "choice", "instructions": "x", "criteria": {}}, {"unsure": "d"}),
            self.judge({"type": "choice", "instructions": "x", "criteria": {"a": "A"}}, {"a": "d", "unsure": "d"}),
            self.judge({"type": "noul", "instructions": ""}, {"yes": "d", "no": "d", "unsure": "d"}),
            self.judge({"type": "score", "instructions": "x", "criteria": ["a", "b", "c"]},
                       {"bands": [[1.5, "d"], [1.0, "d"]], "unsure": "d"}),
            self.judge({"type": "score", "instructions": "x", "criteria": ["a", "b", "c"]},
                       {"bands": [[1.5, "d"]], "unsure": "d"}),                      # top level 2 not covered
            self.judge({"type": "noul", "instructions": "x"}, {"yes": "d", "no": "d", "unsure": "d"}, min_conf=1.5),
        ]
        for g in bad:
            with self.assertRaises(graph.GraphError, msg=json.dumps(g["nodes"]["j"], ensure_ascii=False)):
                graph.validate(g)

    def test_accepts_well_formed(self):
        graph.validate(self.judge({"type": "score", "instructions": "x", "criteria": ["a", "b", "c"]},
                                  {"bands": [[0.5, "d"], [2, "d"]], "unsure": "d"}, min_conf=0.4))


class TestSeverityGuard(unittest.TestCase):
    ALERT = graph.load_dir(ROOT / "graphs", PBS)["monitor.alert"][0]

    def run_alert(self, severity):
        ev = {"kind": "monitor.alert", "id": "a", "rule": "order-api availability", "severity": severity,
              "condition": "Fired", "description": "availability test failing in 4 of 5 regions", "context": {}}
        return graph.run(self.ALERT, ev, LowAnswers(), playbooks=PBS)

    def test_sev1_rated_low_goes_to_a_person(self):
        r = self.run_alert("Sev1")
        impact = next(s for s in r["path"] if s["node"] == "impact")
        self.assertEqual(impact["edge"], "unsure")
        self.assertIn("Sev1", impact["answer"]["guard"])
        self.assertTrue(r["needs_human"])

    def test_sev3_rated_low_is_left_to_the_model(self):
        r = self.run_alert("Sev3")
        impact = next(s for s in r["path"] if s["node"] == "impact")
        self.assertNotEqual(impact["edge"], "unsure")


if __name__ == "__main__":
    unittest.main()
