"""`kimeru replay`: reviewed cases run again with the recorded answers. The model is not called, nothing is posted, the queue, the
notices and the approvals are not touched, and no text of an event is printed or saved."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from kimeru import backends, cli, replay, review
from tests.test_review_stats import GRAPHS, PBS, Base

TEXT_A = "ZZREPLAYTEXTA リリース日を来週火曜にずらしてよいか判断お願いします。QAが止まっていて至急です"
TEXT_B = "ZZREPLAYTEXTB ログイン画面の改修、今どこまで進んでますか？"
GUARDED = ("queue.jsonl", "notices.jsonl", "approvals.json")


class TestReplay(Base):
    def seed(self):
        self.decide([self.chat(TEXT_A, i="1"), self.chat(TEXT_B, i="2")])
        it = iter(["y", "y"])
        review.run(self.out, GRAPHS, PBS, input_fn=lambda p: next(it), print_fn=lambda *_: None)

    def snapshot(self):
        snap = {}
        for folder in (self.out, self.state):
            for name in GUARDED:
                p = folder / name
                snap[str(p)] = p.read_bytes() if p.exists() else None
        return snap

    def run_cli(self, *extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["--out", str(self.out), "replay", *extra])
        return code, buf.getvalue()

    def test_unchanged_graph_replays_to_the_same_final_action(self):
        self.seed()
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("最終行動が変わった件: 0 件", text)
        self.assertNotIn("記録なし（", text)   # the plan's own answers are kept, so the case that goes through the plan replays too
        self.assertIn("再生した事例: 2 / 2 件", text)
        data = json.loads((self.state / "replay" / "replay.json").read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["cases"], 2)
        self.assertEqual(data["summary"]["no_record"], 0)
        self.assertEqual(data["summary"]["changed"], 0)

    def test_a_changed_action_is_listed_by_id_and_node_and_counted_by_verdict(self):
        self.seed()
        rows = review.read_jsonl(self.out / "decisions.jsonl")
        rows[1]["outcome"] = "an_old_outcome"            # as if the graph then ended somewhere else
        (self.out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("最終行動が変わった件: 1 件", text)
        self.assertIn("an_old_outcome", text)
        self.assertIn("正しいと付いた件のうち変わった: 1 / 2 件", text)
        self.assertIn(review.fixture_row(rows[1], {})["id"], text)

    def test_a_question_without_a_record_says_so_and_only_that_case_stops(self):
        self.seed()
        rows = review.read_jsonl(self.out / "decisions.jsonl")
        rows[1]["path"] = [s for s in rows[1]["path"] if "matched" in (s.get("answer") or {})]   # the judge answers are gone
        (self.out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        _, text = self.run_cli()
        self.assertIn("記録なし", text)
        self.assertIn("再生した事例: 1 / 2 件", text)
        self.assertIn("記録なし（intent）", text)

    def plan_row(self, rows):
        return next(r for r in rows if any(s.get("node", "").startswith("plan_") and s["answer"].get("playbook") for s in r["path"]))

    def write_rows(self, rows):
        (self.out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + chr(10) for r in rows), encoding="utf-8")

    def test_a_case_through_a_plan_keeps_numbers_and_ids_only_and_replays(self):
        self.seed()
        row = self.plan_row(review.read_jsonl(self.out / "decisions.jsonl"))
        step = next(s for s in row["path"] if s["node"].startswith("plan_"))
        self.assertEqual(step["answer"]["playbook"], "schedule_change")
        answers = step["answer"]["answers"]
        self.assertIn("first", answers)
        self.assertTrue(any(k.startswith("due_") and "score" in v for k, v in answers.items()))
        self.assertNotIn("ZZREPLAYTEXT", json.dumps(step, ensure_ascii=False))
        _, text = self.run_cli()
        self.assertNotIn("記録なし（", text)

    def test_changing_the_plan_changes_the_case(self):
        import copy
        self.seed()
        graphs = copy.deepcopy(GRAPHS)
        for lst in graphs.values():
            for g in lst:
                for n in g["nodes"].values():
                    if n["kind"] == "plan":
                        n["routes"]["ok"] = "ask_pm"   # a plan that now ends in a plain question to the PM
        buf = io.StringIO()
        with redirect_stdout(buf):
            s = replay.run(self.out, graphs, PBS)
        self.assertEqual(s["replayed"], 2)
        self.assertEqual(s["changed"], 1)

    def test_a_record_from_before_the_plan_answers_were_kept_says_no_record_and_does_not_fail(self):
        self.seed()
        rows = review.read_jsonl(self.out / "decisions.jsonl")
        for r in rows:
            for s in r["path"]:
                if "steps" in (s.get("answer") or {}):
                    s["answer"].pop("answers", None)
        self.write_rows(rows)
        _, text = self.run_cli()
        self.assertIn("記録なし", text)
        self.assertIn("再生した事例: 1 / 2 件", text)
        self.assertNotIn("失敗（", text)

    def test_the_model_is_not_called_and_the_records_are_byte_for_byte_unchanged(self):
        self.seed()
        for folder in (self.out, self.state):          # present beforehand, so a write would show
            for name in GUARDED:
                p = folder / name
                if not p.exists():
                    p.write_text('{"keep": "me"}\n', encoding="utf-8")
        before = self.snapshot()
        decisions = (self.out / "decisions.jsonl").read_bytes()
        with mock.patch.object(backends.StubBackend, "ask", side_effect=AssertionError("model called")), \
                mock.patch.object(backends.JevBackend, "ask", side_effect=AssertionError("model called")):
            code, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.out / "decisions.jsonl").read_bytes(), decisions)
        self.assertFalse(any(self.out.glob("replay*")))   # nothing is written into --out

    def test_no_text_of_an_event_is_printed_or_saved(self):
        self.seed()
        _, text = self.run_cli()
        saved = (self.state / "replay" / "replay.json").read_text(encoding="utf-8")
        for mark in ("ZZREPLAYTEXT", "リリース日", "ログイン画面", "Sato"):
            self.assertNotIn(mark, text)
            self.assertNotIn(mark, saved)

    def test_nothing_to_replay_writes_nothing(self):
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("再生する事例がありません", text)
        self.assertFalse((self.state / "replay").exists())

    def test_the_result_folder_cannot_be_the_records_folder(self):
        self.seed()
        _, text = self.run_cli("--result", str(self.out))
        self.assertIn("別のフォルダ", text)
        self.assertFalse((self.out / "replay.json").exists())

    def test_a_jev_record_hides_the_right_and_wrong_counts(self):
        self.seed()
        rows = review.read_jsonl(self.out / "decisions.jsonl")
        for r in rows:
            r["judge"] = {"name": "Jev"}
        (self.out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        _, text = self.run_cli()
        self.assertIn("利用規約", text)
        self.assertNotIn("正しいと付いた件のうち", text)
        saved = json.loads((self.state / "replay" / "replay.json").read_text(encoding="utf-8"))
        self.assertNotIn("right_changed", saved["summary"])


if __name__ == "__main__":
    unittest.main()


class TestPlanProbabilitiesFollowTheDueBar(unittest.TestCase):
    def test_a_raised_due_bar_keeps_the_probabilities_the_planner_read(self):
        from kimeru import graph
        ans = {"due_a": {"score": 1.0, "confidence": 0.65, "probabilities": {"0": 0.3, "1": 0.65, "2": 0.05}}}
        self.assertNotIn("probabilities", graph._plan_answers(ans, graph._plan_probs_bar({}, None))["due_a"])   # default bar 0.4
        bar = graph._plan_probs_bar({"due_min_conf": 0.7}, None)
        self.assertGreater(bar, 0.7)
        self.assertEqual(graph._plan_answers(ans, bar)["due_a"]["probabilities"], {"0": 0.3, "1": 0.65, "2": 0.05})
