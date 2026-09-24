import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("run_eval", ROOT / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_eval)


class TestFixtures(unittest.TestCase):
    def test_every_label_targets_a_judge_or_plan_node(self):
        for fx in run_eval.fixtures():
            g = run_eval.GRAPHS[fx["event"]["kind"]]
            for nid, exp in fx["expect"].items():
                n = g["nodes"].get(nid)
                self.assertIsNotNone(n, f"{fx['id']}: {nid} missing")
                self.assertIn(n["kind"], ("judge", "plan"), f"{fx['id']}: {nid}")
                if n["kind"] == "judge" and n["question"]["type"] == "score":
                    self.assertTrue(isinstance(exp, list) and len(exp) == 2, f"{fx['id']}: {nid}")

    def test_requests_are_jev_shaped(self):
        for fx in run_eval.fixtures():
            r = run_eval.request(fx)
            self.assertTrue(r["questions"])
            for q in r["questions"].values():
                self.assertTrue(set(q) <= {"type", "instructions", "criteria"})

    def test_scorer_on_perfect_answers(self):
        fx = run_eval.fixtures()[0]
        g = run_eval.GRAPHS[fx["event"]["kind"]]
        for nid, exp in fx["expect"].items():
            n = g["nodes"][nid]
            if n["kind"] == "plan" or n["question"]["type"] == "choice":
                a = {"choice": exp if isinstance(exp, str) else exp[0], "confidence": 0.99}
            elif n["question"]["type"] == "noul":
                a = {"noul": 0.99 if exp else 0.01}
            else:
                a = {"score": sum(exp) / 2, "confidence": 0.99}
            self.assertTrue(run_eval.judge(n, exp, a)[0], nid)


if __name__ == "__main__":
    unittest.main()
