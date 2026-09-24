import json
import tempfile
import unittest
from pathlib import Path

from kimeru import events, graph
from kimeru.backends import ReplayBackend, StubBackend, jev_questions
from kimeru.cli import process

ROOT = Path(__file__).resolve().parent.parent
GRAPHS = graph.load_dir(ROOT / "graphs")


def ex(name):
    return json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))


class TestEvents(unittest.TestCase):
    def test_detect_all_examples(self):
        for f, kind in [("teams_chat.json", "teams.chat"), ("monitor_alert.json", "monitor.alert"),
                        ("ado_workitem.json", "ado.workitem.created"), ("meeting_minutes.json", "meeting.item")]:
            evs = events.normalize(ex(f))
            self.assertTrue(evs)
            self.assertEqual(evs[0]["kind"], kind)

    def test_teams_html_stripped(self):
        e = events.normalize(ex("teams_chat.json"))[0]
        self.assertNotIn("<p>", e["text"])
        self.assertEqual(e["author"], "Sato")

    def test_minutes_fan_out(self):
        self.assertEqual(len(events.normalize(ex("meeting_minutes.json"))), 5)


class TestGraph(unittest.TestCase):
    def test_all_graphs_valid_and_cover_kinds(self):
        self.assertEqual(set(GRAPHS), set(events.KINDS))

    def test_judge_without_unsure_rejected(self):
        g = {"name": "x", "event": "teams.chat", "start": "a", "nodes": {
            "a": {"kind": "judge", "question": {"type": "noul", "instructions": "?"}, "routes": {"yes": "b", "no": "b"}},
            "b": {"kind": "decide", "actions": []}}}
        with self.assertRaises(graph.GraphError):
            graph.validate(g)

    def test_cycle_rejected(self):
        g = {"name": "x", "event": "teams.chat", "start": "a", "nodes": {
            "a": {"kind": "judge", "question": {"type": "noul", "instructions": "?"},
                  "routes": {"yes": "a", "no": "b", "unsure": "b"}},
            "b": {"kind": "decide", "actions": []}}}
        with self.assertRaises(graph.GraphError):
            graph.validate(g)

    def test_every_path_ends_in_decide_or_advise(self):
        for lst in GRAPHS.values():
            for g in lst:
                for n in g["nodes"].values():
                    self.assertIn(n["kind"], ("judge", "plan", "decide", "advise"))

    def test_noul_middle_goes_unsure(self):
        node = GRAPHS["ado.workitem.created"][0]["nodes"]["ready"]
        self.assertEqual(graph.route(node, {"noul": 0.5})[0], "unsure")
        self.assertEqual(graph.route(node, {"noul": 0.9})[0], "yes")
        self.assertEqual(graph.route(node, {"noul": 0.1})[0], "no")

    def test_low_confidence_choice_goes_to_human(self):
        g = GRAPHS["teams.chat"][0]
        ev = events.normalize(ex("teams_chat.json"))[0]
        be = ReplayBackend({"intent": {"choice": "decision", "confidence": 0.4, "probabilities": {}}})
        r = graph.run(g, ev, be)
        self.assertEqual(r["outcome"], "advise")
        self.assertTrue(r["needs_human"])

    def test_score_bands_with_jev_shape(self):
        g = GRAPHS["monitor.alert"][0]
        ev = events.normalize(ex("monitor_alert.json"))[0]
        be = ReplayBackend({"resolved": {"type": "noul", "noul": 0.02},
                            "future_risk": {"type": "noul", "noul": 0.05},
                            "impact": {"type": "score", "score": 2.61, "confidence": 0.61,
                                       "probabilities": {"2": 0.39, "3": 0.61}}})
        r = graph.run(g, ev, be)
        self.assertEqual(r["node"], "page")
        self.assertIn("checkout-api 5xx rate", r["actions"][0]["summary"])

    def test_future_risk_branch(self):
        g = GRAPHS["monitor.alert"][0]
        ev = {"kind": "monitor.alert", "id": "x", "rule": "cert expiry", "severity": "Sev3",
              "condition": "Fired", "description": "TLS certificate expires in 14 days"}
        cases = [({"score": 1.9, "confidence": 0.8}, "prevent_backlog"),
                 ({"score": 0.9, "confidence": 0.8}, "prevent_week"),
                 ({"score": 0.1, "confidence": 0.9}, "prevent_now"),
                 ({"score": 1.0, "confidence": 0.2}, "prevent_advice")]
        for horizon, want in cases:
            be = ReplayBackend({"resolved": {"noul": 0.02}, "future_risk": {"noul": 0.92}, "horizon": horizon})
            r = graph.run(g, ev, be)
            self.assertEqual(r["node"], want)
        # unsure future_risk falls back to the impact path
        be = ReplayBackend({"resolved": {"noul": 0.02}, "future_risk": {"noul": 0.5},
                            "impact": {"score": 1.0, "confidence": 0.8}})
        self.assertEqual(graph.run(g, ev, be)["node"], "task_internal")

    def test_hints_not_sent_to_jev(self):
        qs = {n: node["question"] for g in sum(GRAPHS.values(), []) for n, node in g["nodes"].items()
              if node["kind"] == "judge"}
        sent = jev_questions(qs)
        self.assertTrue(any("hints" in q for q in qs.values()))
        self.assertTrue(all(set(q) <= {"type", "instructions", "criteria"} for q in sent.values()))


class TestEndToEnd(unittest.TestCase):
    def test_stub_run_all_examples(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            res = []
            for f in ["teams_chat.json", "monitor_alert.json", "ado_workitem.json", "meeting_minutes.json"]:
                res += process(ex(f), GRAPHS, StubBackend(), out)
            self.assertEqual(len(res), 8)
            self.assertTrue(all(e["status"] == "planned" for r in res for e in r["executed"]))
            lines = (out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 8)
            nodes = {r["graph"] + ":" + r["node"] for r in res}
            self.assertIn("teams-chat-triage:decision_planned", nodes)
            self.assertIn("alert-triage:page_planned", nodes)
            self.assertIn("minutes-followup:risk_planned", nodes)
            self.assertIn("workitem-intake:set_p1", nodes)
            self.assertIn("minutes-followup:post_decision", nodes)


if __name__ == "__main__":
    unittest.main()
