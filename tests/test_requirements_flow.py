"""The "please work out the requirements for X" request: its own intent, playbook and drafting hint."""
import json
import unittest
from pathlib import Path

from kimeru import events, graph, plan, writer
from kimeru.backends import StubBackend

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPH = {k: v[0] for k, v in graph.load_dir(ROOT / "graphs", PBS).items()}["teams.chat"]


def event(name):
    ev = events.read_inbox_file(ROOT / "examples" / name)
    return ev[0] if isinstance(ev, list) else ev


class TestRequirementsFlow(unittest.TestCase):
    def test_playbook_and_graph_are_wired(self):
        pb = PBS["requirements_review"]
        self.assertGreaterEqual(len(pb["steps"]), 4)
        ids = [s["id"] for s in pb["steps"]]
        self.assertEqual(ids[0], "purpose")          # the first thing to do is to ask why and for whom
        n = GRAPH["nodes"]
        self.assertIn("requirements", n["intent"]["question"]["criteria"])
        self.assertEqual(n["intent"]["routes"]["requirements"], "plan_requirements")
        self.assertEqual(n["plan_requirements"]["playbooks"], ["requirements_review"])
        self.assertEqual(n["plan_requirements"]["routes"]["ok"], "requirements_planned")
        # anything the model is not sure about ends at the PM
        self.assertEqual(n["plan_requirements"]["routes"]["unsure"], "ask_pm")
        self.assertEqual(n["plan_requirements"]["routes"]["none"], "ask_pm")

    def test_request_becomes_a_plan_with_a_reply_and_tasks(self):
        for name in ("teams_requirements.json", "teams_requirements_vague.json"):
            res = graph.run(GRAPH, event(name), StubBackend(), playbooks=PBS)
            self.assertEqual(res["node"], "requirements_planned", name)
            self.assertEqual(res["plan"]["playbook"], "requirements_review")
            kinds = [a["type"] for a in res["actions"]]
            self.assertEqual(kinds.count("teams.reply"), 1)
            self.assertGreaterEqual(kinds.count("ado.create"), 3)
            self.assertIn(res["plan"]["first"], res["actions"][0]["text"])
            self.assertTrue(all("requirements_review" in a["tags"] for a in res["actions"] if a["type"] == "ado.create"))

    def test_the_writer_is_told_what_a_requirements_reply_needs(self):
        ev = event("teams_requirements.json")
        res = graph.run(GRAPH, ev, StubBackend(), playbooks=PBS)
        text = writer._material(res, ev)
        self.assertIn("要件の検討", text)
        self.assertIn("依頼元に聞いてください", text)
        # other requests do not get the hint
        other = graph.run(GRAPH, event("teams_chat.json"), StubBackend(), playbooks=PBS)
        self.assertNotIn("依頼元に聞いてください", writer._material(other, event("teams_chat.json")))

    def test_labeled_fixtures_are_well_formed(self):
        rows = [json.loads(l) for l in (ROOT / "eval" / "fixtures_requirements.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(rows), 10)
        pos = [r for r in rows if r["expect"]["intent"] == "requirements"]
        self.assertEqual(len(pos), 6)
        self.assertTrue(all(r["expect"]["plan_requirements"] == "requirements_review" for r in pos))
        # decoys that mention 要件 but are a decision / a status question / a shared file
        self.assertEqual(sorted(r["expect"]["intent"] for r in rows if r not in pos), ["decision", "decision", "fyi", "status"])
        # the untouched set (never used to pick a threshold or a wording)
        hold = [json.loads(l) for l in (ROOT / "eval" / "fixtures_requirements_holdout.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(hold), 10)
        self.assertEqual(sorted(r["expect"]["intent"] for r in hold if r["expect"]["intent"] != "requirements"),
                         ["decision", "fyi", "incident", "status"])


if __name__ == "__main__":
    unittest.main()
