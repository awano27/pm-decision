import importlib.util
import unittest
from pathlib import Path

from kimeru.backends import StubBackend

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("e2e", ROOT / "eval" / "e2e.py")
e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e)


class PerfectBackend:
    """Answers every labeled question with the label; everything else like the stub."""

    def __init__(self, labels):
        self.labels, self.stub = labels, StubBackend()

    def ask(self, state, questions):
        return {q: (e2e.ideal_answer(e2e.GRAPHS[state["kind"]]["nodes"][q], self.labels[q])
                    if q in self.labels and e2e.GRAPHS[state["kind"]]["nodes"].get(q, {}).get("kind") == "judge"
                    else self.stub.ask(state, {q: v})[q]) for q, v in questions.items()}


class TestE2E(unittest.TestCase):
    def test_runs_on_every_fixture(self):
        for fx in e2e.fixtures():
            r = e2e.run_one(fx, StubBackend())
            self.assertIn(r["outcome"], ("correct", "human", "fallback", "wrong"))
            self.assertTrue(r["model"] and r["ideal"])

    def test_perfect_judge_is_correct_on_judge_only_fixtures(self):
        for fx in e2e.fixtures():
            g = e2e.GRAPHS[fx["event"]["kind"]]
            if any(g["nodes"][k]["kind"] == "plan" for k in fx["expect"]):
                continue  # plan answers come from the stub here
            r = e2e.run_one(fx, PerfectBackend(fx["expect"]))
            self.assertEqual(r["model"], r["ideal"], fx["id"])

    def test_safety_net_counts_as_severe_action(self):
        fx = next(f for f in e2e.fixtures() if f["id"] == "ado-4")    # all users cannot log in
        r = e2e.run_one(fx, StubBackend())
        self.assertEqual(r["model"], "set_p1_critical")
        self.assertFalse(r["severe_miss"])


class MissBackend(StubBackend):
    """Confidently misses a near-term risk: not a future risk, lowest impact, "just noise"."""

    def ask(self, state, questions):
        out = super().ask(state, questions)
        for q, v in questions.items():
            if v.get("type") == "noul":
                out[q] = {"noul": 0.99 if q == "noise_check" else 0.01}
            elif v.get("type") == "score":
                out[q] = {"score": 0.0, "confidence": 0.99, "probabilities": {"0": 0.99}}
        return out


class TestScoring(unittest.TestCase):
    def test_unqueued_advise_is_not_counted_as_human(self):
        fx = next(f for f in e2e.fixtures() if f["id"] == "alert-9")   # disk full in ~6h -> prevent_now
        r = e2e.run_one(fx, MissBackend())
        self.assertEqual((r["model"], r["ideal"]), ("tune_rule", "prevent_now"))
        self.assertEqual(r["outcome"], "wrong")
        self.assertTrue(r["severe_miss"])

    def test_same_terminal_with_other_actions_is_not_correct(self):
        a = {"node": "x", "actions": [{"type": "ado.create", "title": "A"}]}
        b = {"node": "x", "actions": [{"type": "ado.create", "title": "B"}]}
        self.assertNotEqual(e2e._acts(a), e2e._acts(b))


if __name__ == "__main__":
    unittest.main()
