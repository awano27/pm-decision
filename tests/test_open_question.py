"""R11: with the setting post_reason=1, the approval post and the detail say in one line which question the model was unsure of
(or which rule sent the item to a person). The line is worked out from the recorded path only: no model call, no file written,
no number, no node id, no English word. Every judge and plan node, every choice option and every playbook of the bundled graphs
has a Japanese label, and a label for a node or option that the graphs do not have fails the test."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import copy
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from kimeru import config, events, explain, graph, notify, plan
from kimeru.backends import ReplayBackend

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)
ALL = [g for lst in GRAPHS.values() for g in lst]
BY_NAME = {g["name"]: g for g in ALL}
ADO = BY_NAME["workitem-intake"]
CHAT = BY_NAME["teams-chat-triage"]

DIST = {"decision": 0.2, "requirements": 0.02, "status": 0.62, "incident": 0.03, "fyi": 0.08, "none": 0.05}


def step(node, edge, **answer):
    """One step of a recorded path, written the way graph.run writes it."""
    return {"node": node, "answer": answer, "edge": edge}


def chat(path, **extra):
    return {"graph": "teams-chat-triage", "path": path, **extra}


def ado_bug():
    """A Bug without reproduction steps or acceptance criteria: the readiness question is asked, then the priority."""
    f = {"System.Id": 7, "System.WorkItemType": "Bug", "System.Title": "一覧の並び順", "System.Description": "",
         "Microsoft.VSTS.TCM.ReproSteps": "", "Microsoft.VSTS.Common.AcceptanceCriteria": "", "Microsoft.VSTS.TCM.Steps": "",
         "System.CreatedBy": {"displayName": "青木"}, "System.AreaPath": "P\\Web",
         "System.CreatedDate": "2026-10-08T01:00:00Z", "Microsoft.VSTS.Common.Priority": 2}
    return events.normalize({"eventType": "workitem.created", "resource": {"id": 7, "fields": f}})[0]


SEVERE = object()   # marks the row whose line is the fixed severity sentence (it has no ASCII to check)

CASES = [
    ("a clear nearest option names the question and that option",
     chat([step("intent", "unsure", choice="status", confidence=0.45, probabilities=DIST)]),
     "迷った点: 依頼の種類（近いのは 状況確認）"),
    ("a second option almost as likely is named as almost even",
     chat([step("intent", "unsure", choice="decision", confidence=0.4,
                probabilities={**DIST, "decision": 0.42, "status": 0.4})]),
     "迷った点: 依頼の種類（近いのは 判断・決定。次点の状況確認とほぼ互角）"),
    ("a second option a little behind is named as a little behind",
     chat([step("intent", "unsure", choice="decision", confidence=0.4,
                probabilities={**DIST, "decision": 0.5, "status": 0.35})]),
     "迷った点: 依頼の種類（近いのは 判断・決定。次点の状況確認とはやや差）"),
    ("an answer without probabilities names the chosen option",
     chat([step("intent", "unsure", choice="status", confidence=0.3)]),
     "迷った点: 依頼の種類（近いのは 状況確認）"),
    ("an answer with neither probabilities nor a choice says it was not clear",
     chat([step("intent", "unsure", confidence=0.3)]),
     "迷った点: 依頼の種類（はっきりしませんでした）"),
    ("the last unsure step is the one named (readiness, then priority)",
     {"graph": "workitem-intake", "path": [step("ready", "unsure", noul=0.5),
                                            step("priority", "unsure", score=1.0, confidence=0.1,
                                                 probabilities={"0": 0.4, "1": 0.35, "2": 0.25})]},
     "迷った点: 優先度（中ほどで迷いました）"),
    ("a score the severity guard overruled is named as overruled",
     {"graph": "workitem-intake", "path": [step("priority", "unsure", score=0.0, confidence=0.9,
                                                guard="priority=Sev1 but score 0.00 < 1.0")]},
     "迷った点: 優先度（元の重大度より低く採点したため人へ）"),
    ("an answer that could not be read is named as not read",
     chat([step("intent", "unsure", invalid="不正な形")]),
     "迷った点: 依頼の種類（答えを読めませんでした）"),
    ("a noul answer in the middle is named as not clear",
     {"graph": "workitem-intake", "path": [step("ready", "unsure", noul=0.5)]},
     "迷った点: 受け入れ条件などが揃っているか（はっきりしませんでした）"),
    ("a plan that was unsure of its playbook names the playbook it leaned to",
     chat([step("plan_decision", "unsure", playbook="schedule_change", confidence=0.2, steps=[], answers={})]),
     "迷った点: 次の進め方（近いのは スケジュールの変更）"),
    ("a plan whose nearest playbook is none names no playbook",
     chat([step("plan_decision", "unsure", playbook="none", confidence=0.2)]),
     "迷った点: 次の進め方（はっきりしませんでした）"),
    ("a rule that sent the item to a person on a mixed reading is named as a rule",
     chat([step("critical_incident", "mixed", matched="本番")]),
     explain.MIXED_LINE),
    ("a severity that always reaches the PM is named as the severity",
     chat([step("intent", "unsure", choice="status", confidence=0.45, probabilities=DIST)], severe_guard="severity=Sev1"),
     SEVERE),
    ("a path without any unsure step says nothing",
     chat([step("ack_only", "no", matched=None)]),
     ""),
    ("a record from before the path was kept says nothing",
     {"graph": "teams-chat-triage"},
     ""),
    ("a step that is not a dict is skipped, not an error",
     {"graph": "teams-chat-triage", "path": ["x", None]},
     ""),
    ("a node the graph does not have is not named",
     chat([step("renamed_node", "unsure", noul=0.5)]),
     ""),
]


class TestTheLineForEachPath(unittest.TestCase):
    def test_each_recorded_path_gives_its_sentence(self):
        for case, rec, want in CASES:
            with self.subTest(case=case):
                got = explain.open_question(rec)
                if want is SEVERE:
                    self.assertEqual(got, explain.SEVERE_LINE)
                else:
                    self.assertEqual(got, want)

    def test_no_line_has_a_number_an_english_word_or_a_node_name(self):
        for case, rec, want in CASES:
            got = explain.open_question(rec)
            if not got or want is SEVERE:
                continue
            with self.subTest(case=case):
                self.assertNotRegex(got, r"[A-Za-z0-9%]")
                self.assertNotRegex(got, r"\d+%|0\.\d")

    def test_the_line_is_the_same_when_it_is_worked_out_twice(self):
        for case, rec, want in CASES:
            with self.subTest(case=case):
                self.assertEqual(explain.open_question(rec), explain.open_question(copy.deepcopy(rec)))


class TestLabelsCoverTheBundledGraphs(unittest.TestCase):
    def test_every_judge_and_plan_node_of_the_bundled_graphs_has_a_question_label(self):
        for g in ALL:
            for nid, n in g["nodes"].items():
                if n["kind"] in ("judge", "plan"):
                    with self.subTest(graph=g["name"], node=nid):
                        self.assertIn((g["name"], nid), explain.QUESTION_LABEL)

    def test_every_choice_option_of_a_judge_has_an_option_label(self):
        for g in ALL:
            for nid, n in g["nodes"].items():
                if n["kind"] == "judge" and n["question"]["type"] == "choice":
                    for option in n["question"]["criteria"]:
                        with self.subTest(graph=g["name"], node=nid, option=option):
                            self.assertIn(option, explain.OPTION_LABEL[(g["name"], nid)])

    def test_every_playbook_and_none_has_a_playbook_label(self):
        for pid in PBS:
            with self.subTest(playbook=pid):
                self.assertIn(pid, explain.PLAYBOOK_LABEL)
        self.assertIn("none", explain.PLAYBOOK_LABEL)

    def test_no_label_names_a_node_or_an_option_the_graphs_do_not_have(self):
        nodes = {(g["name"], nid) for g in ALL for nid in g["nodes"]}
        self.assertLessEqual(set(explain.QUESTION_LABEL), nodes)
        options = {(g["name"], nid, o) for g in ALL for nid, n in g["nodes"].items()
                   if n["kind"] == "judge" and n["question"]["type"] == "choice" for o in n["question"]["criteria"]}
        self.assertLessEqual({(k[0], k[1], o) for k, labels in explain.OPTION_LABEL.items() for o in labels}, options)
        self.assertEqual(set(explain.PLAYBOOK_LABEL) - {"none"}, set(PBS))

    def test_every_label_is_japanese_text_without_numbers_or_english_words(self):
        labels = [*explain.QUESTION_LABEL.values(), *explain.PLAYBOOK_LABEL.values(),
                  *(v for labels in explain.OPTION_LABEL.values() for v in labels.values())]
        for label in labels:
            with self.subTest(label=label):
                self.assertNotRegex(label, r"[A-Za-z0-9%]")


class TestTheSettingIsAYesOrNoSetting(unittest.TestCase):
    def test_the_setting_is_off_by_default_and_is_read_as_yes_or_no(self):
        self.assertEqual(config.SETTINGS["post_reason"], ("KIMERU_POST_REASON", "0"))
        self.assertEqual(config.CHOICES["post_reason"], ("0", "1"))
        with mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "on"}):
            self.assertEqual(config.value("post_reason"), "1")

    def test_the_setting_is_logged_and_shared_like_learn(self):
        self.assertIn("post_reason", config.LOGGABLE)
        self.assertIn("post_reason", config.SHARE_VALUES)


class TestAnUnsureAdoItemInThePost(unittest.TestCase):
    """The readiness question is unsure, so the priority question is asked; the priority is the one the line names."""

    def run_bug(self):
        answers = ReplayBackend({
            "ready": {"noul": 0.5},
            "priority": {"score": 1.0, "confidence": 0.1, "probabilities": {"0": 0.4, "1": 0.35, "2": 0.25}}})
        return graph.run(ADO, ado_bug(), answers, playbooks=PBS)

    def test_a_low_confidence_priority_after_an_unsure_readiness_is_the_line_under_the_arrow(self):
        res = self.run_bug()
        self.assertEqual([(s["node"], s["edge"]) for s in res["path"]][-2:], [("ready", "unsure"), ("priority", "unsure")])
        with mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "1"}):
            lines = notify.format_post(5, res).splitlines()
        self.assertTrue(lines[1].startswith("→ "))
        self.assertEqual(lines[2], "迷った点: 優先度（中ほどで迷いました） / 最有力 0.40")   # T7: the post line adds the number
        self.assertLessEqual(len(lines), 5)
        self.assertTrue(lines[-1].startswith("OK 5 / NG 5"))

    def test_without_the_setting_the_post_has_no_line_for_it(self):
        res = self.run_bug()
        text = notify.format_post(5, res)
        self.assertNotIn("迷った点", text)

    def test_the_line_is_worked_out_without_a_backend_call_or_a_written_file(self):
        res = self.run_bug()
        before = copy.deepcopy(res)
        state = os.environ["KIMERU_STATE_DIR"]

        def listing():
            return sorted((root, sorted(files)) for root, _, files in os.walk(state))
        seen = listing()
        with mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "1"}), \
                mock.patch.object(ReplayBackend, "ask", side_effect=AssertionError("the explanation asked the model")):
            notify.format_post(5, res)
            notify.format_detail(5, res)
        self.assertEqual(res, before)
        self.assertEqual(listing(), seen)


if __name__ == "__main__":
    unittest.main()
