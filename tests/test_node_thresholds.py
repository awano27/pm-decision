"""R21: a threshold entry for one question (thresholds.json `nodes`) changes only that question's route, only for the judge
profiles, only for the questions whose unsure route goes straight to a person; a broken entry is ignored, and the graph files
are never changed."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import calibrate, cli, config, fsutil, graph, plan, profiles

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)
BY_NAME = {g["name"]: g for lst in GRAPHS.values() for g in lst}
INTENT = BY_NAME["teams-chat-triage"]["nodes"]["intent"]
ANSWER = {"choice": "fyi", "confidence": 0.65}   # decided at the default bar (0.6), held back at 0.72


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name) / "state"
        self.state.mkdir()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = str(self.state)
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        config.apply([])

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def write_thresholds(self, data):
        p = profiles.override_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def route_intent(self, profile):
        return graph.route(INTENT, ANSWER, profiles.for_node(profile, BY_NAME["teams-chat-triage"], "intent"))[0]


class TestNodeEntriesChangeOnlyTheirQuestion(Base):
    def test_an_entry_for_one_question_makes_only_that_route_unsure(self):
        kev = profiles.PROFILES["kev"]
        self.assertEqual(self.route_intent(kev), "fyi")
        self.write_thresholds({"nodes": {"teams-chat-triage:intent": {"conf_scale": 1.2}}})
        self.assertEqual(self.route_intent(kev), "unsure")
        # a question without an entry keeps the profile's own coefficients (the same object values)
        urgency_prof = profiles.for_node(kev, BY_NAME["teams-chat-triage"], "urgency")
        self.assertEqual((urgency_prof["conf_scale"], urgency_prof["noul_scale"]), (kev["conf_scale"], kev["noul_scale"]))

    def test_the_node_entry_is_read_with_the_graph_name_and_not_another_graph_of_the_same_node_id(self):
        self.write_thresholds({"nodes": {"monitor-alert:intent": {"conf_scale": 3.0}}})
        self.assertEqual(self.route_intent(profiles.PROFILES["kev"]), "fyi")

    def test_the_graph_bytes_and_the_graph_version_do_not_change(self):
        before_bytes = {p.name: p.read_bytes() for p in (ROOT / "graphs").glob("*.json")}
        before_version = {g["name"]: cli._graph_version(g) for lst in GRAPHS.values() for g in lst}
        self.write_thresholds({"nodes": {"teams-chat-triage:intent": {"conf_scale": 1.5}}})
        self.route_intent(profiles.PROFILES["kev"])
        self.assertEqual({p.name: p.read_bytes() for p in (ROOT / "graphs").glob("*.json")}, before_bytes)
        self.assertEqual({g["name"]: cli._graph_version(g) for lst in GRAPHS.values() for g in lst}, before_version)

    def test_the_stub_profile_and_unknown_judges_are_never_scaled_by_a_node_entry(self):
        self.write_thresholds({"nodes": {"teams-chat-triage:intent": {"conf_scale": 3.0}}})
        for name in ("stub", "unknown"):
            prof = {"name": name, "conf_scale": 1.0, "noul_scale": 1.0}
            self.assertEqual(profiles.for_node(prof, BY_NAME["teams-chat-triage"], "intent")["conf_scale"], 1.0)

    def test_eval_scripts_ignore_node_entries_too(self):
        self.write_thresholds({"nodes": {"teams-chat-triage:intent": {"conf_scale": 3.0}}})
        with profiles.ignoring_overrides():
            self.assertEqual(self.route_intent(profiles.PROFILES["kev"]), "fyi")

    def test_a_node_entry_is_laid_over_the_global_coefficients_of_the_same_judge(self):
        self.write_thresholds({"kev": {"conf_scale": 1.1, "noul_scale": 0.95},
                               "nodes": {"teams-chat-triage:intent": {"conf_scale": 1.2}}})
        prof = profiles.for_node(profiles.PROFILES["kev"], BY_NAME["teams-chat-triage"], "intent")
        self.assertAlmostEqual(prof["conf_scale"], 1.1 * 1.2)
        self.assertEqual(prof["noul_scale"], 0.95)


class TestOnlyQuestionsWhoseUnsureGoesToAPersonAreLearnable(Base):
    def test_the_learnable_questions_of_the_shipped_graphs_are_the_judges_that_send_unsure_to_a_person(self):
        found = sorted((g["name"], nid) for g in BY_NAME.values() for nid in g["nodes"] if profiles.learnable(g, nid))
        self.assertEqual(found, [("alert-triage", "horizon"), ("alert-triage", "impact"), ("minutes-followup", "item_type"),
                                 ("teams-chat-triage", "intent"), ("workitem-intake", "priority")])

    def test_ready_noise_urgency_and_future_risk_are_never_overridden(self):
        self.write_thresholds({"nodes": {
            "workitem-intake:ready": {"noul_scale": 3.0, "conf_scale": 3.0},
            "alert-triage:noise_check": {"noul_scale": 3.0},
            "teams-chat-triage:urgency": {"conf_scale": 3.0},
            "alert-triage:future_risk": {"noul_scale": 3.0}}})
        for gname, nid in (("workitem-intake", "ready"), ("alert-triage", "noise_check"),
                           ("teams-chat-triage", "urgency"), ("alert-triage", "future_risk")):
            g = BY_NAME[gname]
            self.assertFalse(profiles.learnable(g, nid))
            self.assertEqual(profiles.for_node(profiles.PROFILES["kev"], g, nid), profiles.effective(profiles.PROFILES["kev"]))

    def test_plan_and_match_nodes_are_never_learnable(self):
        g = BY_NAME["teams-chat-triage"]
        self.assertFalse(profiles.learnable(g, "plan_decision"))
        self.assertFalse(profiles.learnable(g, "critical_incident"))
        self.assertFalse(profiles.learnable(None, "intent"))


class TestBrokenEntriesAreIgnored(Base):
    def test_a_malformed_node_entry_never_raises_and_changes_nothing(self):
        bad = {"nodes": {
            "teams-chat-triage:intent": "x",
            "teams-chat-triage:priority": None,
            "workitem-intake:priority": True,
            "minutes-followup:item_type": {"conf_scale": float("nan")},
            "alert-triage:impact": {"conf_scale": 9},
            "alert-triage:horizon": {"unknown_key": 1.2},
            "no-such-graph:nothing": {"conf_scale": 1.5},
            "monitor-alert:impact": [1, 2],
        }}
        self.write_thresholds(bad)
        self.assertEqual(profiles.node_overrides(), {"no-such-graph:nothing": {"conf_scale": 1.5}})
        self.assertEqual(self.route_intent(profiles.PROFILES["kev"]), "fyi")
        for gname, nid in (("workitem-intake", "priority"), ("minutes-followup", "item_type"), ("alert-triage", "impact")):
            self.assertEqual(profiles.for_node(profiles.PROFILES["kev"], BY_NAME[gname], nid),
                             profiles.effective(profiles.PROFILES["kev"]))

    def test_a_broken_file_is_no_entry(self):
        profiles.override_path().parent.mkdir(parents=True, exist_ok=True)
        profiles.override_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(profiles.node_overrides(), {})
        self.assertEqual(self.route_intent(profiles.PROFILES["kev"]), "fyi")

    def test_the_global_overrides_do_not_read_the_nodes_key_as_a_profile(self):
        self.write_thresholds({"nodes": {"teams-chat-triage:intent": {"conf_scale": 1.2}}})
        self.assertEqual(profiles.overrides(), {})


class TestCalibrateKeepsNodeEntries(Base):
    def test_apply_keeps_the_node_entries(self):
        self.write_thresholds({"nodes": {"teams-chat-triage:intent": {"conf_scale": 1.2}}})
        calibrate.apply("kev", {"conf_scale": 1.1, "noul_scale": 0.95})
        data = json.loads(profiles.override_path().read_text(encoding="utf-8"))
        self.assertEqual(data["nodes"], {"teams-chat-triage:intent": {"conf_scale": 1.2}})
        self.assertEqual(data["kev"], {"conf_scale": 1.1, "noul_scale": 0.95})

    def test_revert_removes_the_coefficients_and_keeps_the_node_entries(self):
        self.write_thresholds({"nodes": {"teams-chat-triage:intent": {"conf_scale": 1.2}}})
        calibrate.apply("kev", {"conf_scale": 1.1, "noul_scale": 0.95})
        self.assertTrue(calibrate.revert())
        self.assertTrue(profiles.override_path().exists())
        data = json.loads(profiles.override_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"nodes": {"teams-chat-triage:intent": {"conf_scale": 1.2}}})
        self.assertEqual(self.route_intent(profiles.PROFILES["kev"]), "unsure")

    def test_revert_without_node_entries_removes_the_file_as_before(self):
        calibrate.apply("kev", {"conf_scale": 1.1, "noul_scale": 0.95})
        self.assertTrue(calibrate.revert())
        self.assertFalse(profiles.override_path().exists())
        self.assertFalse(calibrate.revert())

    def test_apply_and_revert_write_the_file_through_the_atomic_writer(self):
        self.write_thresholds({"nodes": {"teams-chat-triage:intent": {"conf_scale": 1.2}}})
        with mock.patch.object(fsutil, "write_atomic", wraps=fsutil.write_atomic) as writer:
            calibrate.apply("kev", {"conf_scale": 1.1, "noul_scale": 0.95})
            self.assertTrue(calibrate.revert())
        self.assertEqual(writer.call_count, 2)

    def test_a_failed_revert_write_keeps_the_file_as_it_was(self):
        self.write_thresholds({"kev": {"conf_scale": 1.1, "noul_scale": 0.95},
                               "nodes": {"teams-chat-triage:intent": {"conf_scale": 1.2}}})
        before = profiles.override_path().read_text(encoding="utf-8")
        with mock.patch.object(fsutil, "write_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                calibrate.revert()
        self.assertEqual(profiles.override_path().read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
