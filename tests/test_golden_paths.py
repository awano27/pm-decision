"""The expected path of every labeled fixture (eval/golden.json) is the one the current graphs give when only the labels answer.
A change to one route of a graph names the fixtures that moved, before and after; no judge is asked, no Jev number is written,
and the user's local coefficients and this PC's KIMERU_* settings do not change the result."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import importlib.util
import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from kimeru import graph, profiles
from kimeru.backends import ClmBackend, JevBackend, KevBackend, StubBackend

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("kimeru_eval_golden", ROOT / "eval" / "golden.py")
golden = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(golden)


class TestCommittedGoldenPaths(unittest.TestCase):
    def test_the_committed_golden_paths_match_the_current_graphs(self):
        lines = golden.diff(golden.read(), golden.collect())
        self.assertEqual(lines, [], "run `python eval/golden.py --write` after a graph change, then review the lines:\n"
                         + "\n".join(lines))

    def test_the_golden_file_covers_every_labeled_fixture_once(self):
        ids = [fx["id"] for fx in golden.fixtures()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(golden.read()), sorted(ids))

    def test_changing_one_route_in_teams_chat_names_the_fixture_that_moves_with_its_nodes_before_and_after(self):
        graphs = golden.load_graphs()
        graphs["teams.chat"]["nodes"]["intent"]["routes"]["fyi"] = "ask_pm"   # was log_fyi
        lines = golden.diff(golden.read(), golden.collect(graphs=graphs))
        self.assertTrue(any(line.startswith("teams-4: log_fyi -> ask_pm") for line in lines), lines)

    def test_a_route_change_moves_only_the_fixtures_that_went_through_that_route(self):
        graphs = golden.load_graphs()
        graphs["teams.chat"]["nodes"]["intent"]["routes"]["fyi"] = "ask_pm"
        expected = golden.read()
        moved = {line.split(":")[0] for line in golden.diff(expected, golden.collect(graphs=graphs))}
        self.assertTrue(moved)
        for fid in moved:
            self.assertIn("intent[fyi]", expected[fid]["path"], fid)

    def test_changing_the_text_of_an_action_shows_as_actions_changed_not_as_a_node_change(self):
        graphs = golden.load_graphs()
        for action in graphs["teams.chat"]["nodes"]["decision_today"]["actions"]:
            if "text" in action:
                action["text"] = action["text"] + "（変更）"
        lines = golden.diff(golden.read(), golden.collect(graphs=graphs))
        self.assertTrue(lines)
        for line in lines:
            self.assertIn("actions changed", line)
            self.assertNotIn("->", line)


class TestTheWalkAsksNoJudge(unittest.TestCase):
    def test_the_golden_walk_asks_no_model_and_no_backend(self):
        boom = mock.Mock(side_effect=AssertionError("the golden walk must not ask a judge"))
        with mock.patch.object(JevBackend, "ask", boom), mock.patch.object(KevBackend, "ask", boom), \
                mock.patch.object(ClmBackend, "ask", boom), mock.patch.object(StubBackend, "ask", boom):
            now = golden.collect()
        self.assertEqual(len(now), 147)
        self.assertEqual(boom.call_count, 0)

    def test_an_unlabeled_question_gets_an_answer_that_routes_every_judge_to_its_unsure_route(self):
        with profiles.ignoring_overrides(True):
            for kind, gs in graph.load_dir(golden.ROOT / "graphs", golden.load_playbooks()).items():
                for g in gs:
                    for nid, node in g["nodes"].items():
                        if node["kind"] != "judge":
                            continue
                        edge, _ = graph.route(node, golden.unsure_answer(node["question"]))
                        self.assertEqual(edge, "unsure", f"{kind}/{nid}")

    def test_a_local_threshold_file_from_calibrate_does_not_change_the_golden_paths(self):
        baseline = golden.collect()
        path = profiles.override_path()
        path.write_text(json.dumps({"jev": {"conf_scale": 3.0, "noul_scale": 0.1}}), encoding="utf-8")
        try:
            self.assertEqual(profiles.effective()["conf_scale"], 3.0)   # the file is in use outside the golden walk
            self.assertEqual(golden.collect(), baseline)
        finally:
            path.unlink(missing_ok=True)

    def test_this_pcs_kimeru_settings_do_not_change_the_golden_paths(self):
        baseline = golden.collect()
        env = {"KIMERU_TITLE_MAX": "20", "KIMERU_PLAN_LEAN": "1", "KIMERU_BATCH": "1", "KIMERU_BACKEND": "jev"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(golden.collect(), baseline)
            self.assertEqual(os.environ["KIMERU_TITLE_MAX"], "20")   # restored after the walk


class TestGoldenFileShape(unittest.TestCase):
    def test_the_golden_file_holds_only_ids_nodes_edges_and_fingerprints_and_no_fixture_text(self):
        raw = golden.GOLDEN.read_text(encoding="utf-8")
        data = json.loads(raw)
        for fid, rec in data.items():
            self.assertEqual(set(rec), {"node", "actions_sha1", "path"}, fid)
            self.assertIsInstance(rec["node"], str, fid)
            self.assertRegex(rec["actions_sha1"], r"^[0-9a-f]{40}$", fid)
            self.assertIsInstance(rec["path"], list, fid)
            for step in rec["path"]:
                self.assertRegex(step, r"^[^\[\]]+\[[^\[\]]+\]$", fid)
        leaves = []

        def walk(v):
            if isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
            else:
                leaves.append(v)
        walk(data)
        self.assertTrue(all(isinstance(v, str) for v in leaves), "a number (a judge's score or confidence) is in the golden file")
        for fx in golden.fixtures():
            text = str(fx["event"].get("text") or "")
            if len(text) >= 8:
                self.assertNotIn(text, raw, fx["id"])

    def test_writing_then_checking_is_green_and_a_stale_file_fails_the_check(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            target = Path(tmp) / "golden.json"
            with mock.patch.object(golden, "GOLDEN", target):
                self.assertEqual(golden.main([]), 1)              # no file yet: the check fails
                self.assertEqual(golden.main(["--write"]), 0)
                self.assertEqual(golden.main([]), 0)              # the file just written matches
                data = json.loads(target.read_text(encoding="utf-8"))
                data["teams-4"]["node"] = "ask_pm"
                target.write_text(json.dumps(data), encoding="utf-8")
                self.assertEqual(golden.main([]), 1)              # a hand edit is caught

    def test_a_fixture_that_labels_two_plan_nodes_of_one_graph_is_refused(self):
        graphs = golden.load_graphs()
        fx = {"id": "x", "event": {"kind": "teams.chat", "text": "x"}, "expect": {"plan_decision": "a", "plan_requirements": "b"}}
        with self.assertRaises(ValueError):
            golden.walk(fx, graphs, golden.load_playbooks())


if __name__ == "__main__":
    unittest.main()
