import json
import unittest
from pathlib import Path

from kimeru import graph, plan
from kimeru.backends import ReplayBackend, StubBackend, jev_questions

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
EV = {"kind": "teams.chat", "id": "1", "chat_id": "c1", "author": "Sato",
      "text": "リリース日を来週火曜にずらしてよいか判断お願いします。QAが止まっていて至急です"}


class Scripted:
    """Answers the playbook call, then the step batch."""

    def __init__(self, playbook, batch):
        self.playbook, self.batch, self.calls = playbook, batch, []

    def ask(self, state, questions):
        self.calls.append(questions)
        if "playbook" in questions:
            return {"playbook": self.playbook}
        return {q: self.batch.get(q, {"noul": 0.9} if q.startswith("need_") else
                                  {"score": 1.0, "confidence": 0.8}) for q in questions}


NODE = {"kind": "plan", "playbooks": ["schedule_change", "scope_change"], "routes": {"ok": "a", "none": "b", "unsure": "c"}}


class TestPlaybooks(unittest.TestCase):
    def test_all_playbooks_valid(self):
        self.assertGreaterEqual(len(PBS), 8)

    def test_reserved_and_duplicate_ids_rejected(self):
        with self.assertRaises(plan.PlaybookError):
            plan.validate_playbook({"id": "none", "title": "x", "when": "x", "steps": [{"id": "a", "title": "a", "desc": "a"}] * 2})
        with self.assertRaises(plan.PlaybookError):
            plan.validate_playbook({"id": "x", "title": "x", "when": "x",
                                    "steps": [{"id": "a", "title": "a", "desc": "a"}, {"id": "a", "title": "b", "desc": "b"}]})

    def test_jev_payload_has_no_hints_and_fits_limits(self):
        b = Scripted({"choice": "schedule_change", "confidence": 0.9}, {"first": {"choice": "decide", "confidence": 0.9}})
        plan.build(NODE, EV, b, PBS)
        for qs in b.calls:
            sent = jev_questions(qs)
            self.assertTrue(all(set(q) <= {"type", "instructions", "criteria"} for q in sent.values()))
            for q in sent.values():
                if q["type"] == "choice":
                    self.assertLessEqual(len(q["criteria"]), 255)


class TestBuild(unittest.TestCase):
    def test_first_step_moves_to_front_and_unneeded_dropped(self):
        b = Scripted({"choice": "schedule_change", "confidence": 0.95},
                     {"first": {"choice": "blocker_eta", "confidence": 0.86},
                      "need_notify": {"noul": 0.2},
                      "due_blocker_eta": {"score": 0.1, "confidence": 0.9},
                      "due_decide": {"score": 2.6, "confidence": 0.9}})
        edge, p, _ = plan.build(NODE, EV, b, PBS)
        self.assertEqual(edge, "ok")
        ids = [s["id"] for s in p["steps"]]
        self.assertEqual(ids[0], "blocker_eta")
        self.assertNotIn("notify", ids)
        self.assertEqual(p["steps"][0]["due"], "今日")
        self.assertEqual({s["id"]: s["due"] for s in p["steps"]}["decide"], "次スプリント以降")
        self.assertIn("1.遅延原因の復旧見込みを担当者に確認（今日）", p["summary"])

    def test_low_confidence_due_is_flagged_not_guessed(self):
        b = Scripted({"choice": "schedule_change", "confidence": 0.95},
                     {"first": {"choice": "blocker_eta", "confidence": 0.9},
                      "due_decide": {"score": 0.1, "confidence": 0.2}})
        _, p, _ = plan.build(NODE, EV, b, PBS)
        d = {s["id"]: s for s in p["steps"]}["decide"]
        self.assertEqual((d["due"], d["due_level"]), (plan.DUE_UNSURE, 1))

    def test_adjacent_due_split_takes_earlier_as_estimate(self):
        b = Scripted({"choice": "schedule_change", "confidence": 0.95},
                     {"first": {"choice": "blocker_eta", "confidence": 0.9},
                      # Kev-style: today 0.45 / this week 0.44 -> low confidence
                      "due_blocker_eta": {"score": 0.65, "confidence": 0.02, "probabilities": {"0": 0.45, "1": 0.44, "2": 0.11}},
                      # spread across non-neighbours -> still flagged
                      "due_decide": {"score": 1.0, "confidence": 0.1, "probabilities": {"0": 0.45, "1": 0.1, "2": 0.45}}})
        _, p, _ = plan.build(NODE, EV, b, PBS)
        d = {s["id"]: s for s in p["steps"]}
        self.assertEqual((d["blocker_eta"]["due"], d["blocker_eta"]["due_level"]), ("今日（目安）", 0))
        self.assertEqual(d["decide"]["due"], plan.DUE_UNSURE)

    def test_profile_scales_plan_thresholds(self):
        # the mechanism, independent of the tuned values in profiles.py
        class Lenient(Scripted):
            profile = {"conf_scale": 0.8, "noul_scale": 1.0}
        KevLike = Lenient
        # 0.55 < default 0.6 but >= 0.6 * 0.8 = 0.48
        a = {"choice": "schedule_change", "confidence": 0.55}
        self.assertEqual(plan.build(NODE, EV, Scripted(a, {}), PBS)[0], "unsure")
        self.assertEqual(plan.build(NODE, EV, KevLike(a, {"first": {"choice": "decide", "confidence": 0.9}}), PBS)[0], "ok")

    def test_low_confidence_first_keeps_playbook_order(self):
        b = Scripted({"choice": "schedule_change", "confidence": 0.95}, {"first": {"choice": "notify", "confidence": 0.3}})
        _, p, _ = plan.build(NODE, EV, b, PBS)
        self.assertEqual(p["steps"][0]["id"], "blocker_eta")

    def test_none_and_unsure_routes(self):
        self.assertEqual(plan.build(NODE, EV, Scripted({"choice": "none", "confidence": 0.9}, {}), PBS)[0], "none")
        self.assertEqual(plan.build(NODE, EV, Scripted({"choice": "scope_change", "confidence": 0.4}, {}), PBS)[0], "unsure")

    def test_nothing_needed_is_unsure(self):
        pb = {"id": "t", "title": "t", "when": "t", "steps": [
            {"id": "a", "title": "a", "desc": "a", "check": "?"}, {"id": "b", "title": "b", "desc": "b", "check": "?"}]}
        b = Scripted({"choice": "t", "confidence": 0.9}, {"need_a": {"noul": 0.1}, "need_b": {"noul": 0.1},
                                                         "first": {"choice": "a", "confidence": 0.9}})
        node = {**NODE, "playbooks": ["t"]}
        self.assertEqual(plan.build(node, EV, b, {"t": pb})[0], "unsure")


class TestOrdinalGuard(unittest.TestCase):
    PRIO = json.loads((ROOT / "graphs" / "ado_workitem.json").read_text(encoding="utf-8"))["nodes"]["priority"]

    def test_adjacent_split_is_decided(self):
        a = {"score": 1.56, "confidence": 0.33, "probabilities": {"0": 0.0, "1": 0.44, "2": 0.56}}
        self.assertEqual(graph.route(self.PRIO, a)[1], "set_p2")

    def test_non_adjacent_split_goes_to_human(self):
        a = {"score": 1.0, "confidence": 0.35, "probabilities": {"0": 0.45, "1": 0.1, "2": 0.45}}
        self.assertEqual(graph.route(self.PRIO, a), ("unsure", "triage_pm"))

    def test_flat_distribution_goes_to_human(self):
        a = {"score": 1.0, "confidence": 0.05, "probabilities": {"0": 0.34, "1": 0.33, "2": 0.33}}
        self.assertEqual(graph.route(self.PRIO, a)[0], "unsure")


class TestGraphIntegration(unittest.TestCase):
    def test_teams_decision_creates_task_per_step(self):
        g = json.loads((ROOT / "graphs" / "teams_chat.json").read_text(encoding="utf-8"))
        graph.validate(g, PBS)
        r = graph.run(g, EV, StubBackend(), playbooks=PBS)
        self.assertEqual(r["node"], "decision_planned")
        self.assertEqual(r["plan"]["playbook"], "schedule_change")
        tasks = [a for a in r["actions"] if a["type"] == "ado.create"]
        self.assertEqual(len(tasks), len(r["plan"]["steps"]))
        self.assertIn(r["plan"]["first"], r["actions"][0]["text"])
        self.assertTrue(all(t["due"] in plan.DUE_LABELS for t in tasks))

    def test_unknown_playbook_reference_rejected(self):
        g = {"name": "x", "event": "teams.chat", "start": "p", "nodes": {
            "p": {"kind": "plan", "playbooks": ["nope"], "routes": {"ok": "d", "none": "d", "unsure": "d"}},
            "d": {"kind": "decide", "actions": []}}}
        with self.assertRaises(graph.GraphError):
            graph.validate(g, PBS)

    def test_plan_without_playbooks_falls_back(self):
        g = json.loads((ROOT / "graphs" / "teams_chat.json").read_text(encoding="utf-8"))
        r = graph.run(g, EV, StubBackend())
        self.assertIn(r["node"], ("decision_today", "decision_later"))
        self.assertNotIn("plan", r)


if __name__ == "__main__":
    unittest.main()
