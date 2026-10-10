try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path

from kimeru import graph, notify, plan
from kimeru.backends import BackendUnavailable, ReplayBackend, StubBackend
from kimeru.cli import process

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = {k: v[0] for k, v in graph.load_dir(ROOT / "graphs", PBS).items()}
ALERT = GRAPHS["monitor.alert"]
ADO = GRAPHS["ado.workitem.created"]
TEAMS = GRAPHS["teams.chat"]

STORAGE = {"kind": "monitor.alert", "id": "s1", "rule": "prod-db storage", "severity": "Sev1", "condition": "Fired",
           "description": "Storage 92% used on prod-db; at current growth it fills within 2 days"}
OUTAGE = {"kind": "monitor.alert", "id": "s0", "rule": "checkout-api 5xx rate", "severity": "Sev0", "condition": "Fired",
          "description": "5xx error rate 23% for 10 minutes; customers cannot complete checkout (service down)"}


def future(horizon):
    return ReplayBackend({"future_risk": {"noul": 0.9}, "horizon": {"score": horizon, "confidence": 0.8}})


class TestSevereAlerts(unittest.TestCase):
    def test_sev1_future_risk_is_notified_on_every_horizon(self):
        for horizon, node in ((1.0, "prevent_week"), (2.2, "prevent_backlog")):
            r = graph.run(ALERT, STORAGE, future(horizon))
            self.assertEqual(r["node"], node)
            self.assertTrue(r["notify"])
            self.assertEqual(r["severe_guard"], "severity=Sev1")

    def test_lower_severity_and_resolved_alerts_keep_their_outcome(self):
        r = graph.run(ALERT, {**STORAGE, "severity": "Sev3"}, future(1.0))
        self.assertEqual((r["node"], r["notify"], r["needs_human"]), ("prevent_week", False, False))
        self.assertNotIn("severe_guard", r)
        r = graph.run(ALERT, {**STORAGE, "condition": "Resolved"}, ReplayBackend({}))
        self.assertEqual((r["node"], r["notify"], r["needs_human"]), ("log_resolved", False, False))

    def test_malformed_answer_takes_the_unsure_route(self):
        r = graph.run(ALERT, OUTAGE, ReplayBackend({"playbook": {"choice": "no-such-playbook", "confidence": 0.9}}),
                      playbooks=PBS)
        self.assertEqual(r["node"], "page")
        self.assertTrue(r["notify"])
        self.assertIn("invalid", r["path"][-1]["answer"])
        r = graph.run(ALERT, {**STORAGE, "severity": "Sev3"}, ReplayBackend({"future_risk": {"noul": 7}, "impact": {"score": 1.0, "confidence": 0.8}}))
        self.assertEqual(r["path"][2], {"node": "future_risk", "answer": r["path"][2]["answer"], "edge": "unsure"})
        self.assertIn("invalid", r["path"][2]["answer"])

    def test_notice_is_posted_even_when_a_draft_waits_for_approval(self):
        class Writer:
            NAME = "fake"

            def draft(self, res, event, instruction=None):
                return {"text": "草案です。", "memo": ""}

        payload = json.loads((ROOT / "examples" / "monitor_alert.json").read_text(encoding="utf-8"))
        for writer in (None, Writer()):
            with tempfile.TemporaryDirectory() as d:
                out = Path(d)
                [res] = process(payload, GRAPHS_ALL, StubBackend(), out, PBS, writer=writer)
                self.assertTrue(res["notify"])
                notices = (out / "notices.jsonl").read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(notices), 1)
                queued = (out / "queue.jsonl").exists() and (out / "queue.jsonl").read_text(encoding="utf-8").strip()
                self.assertEqual(bool(queued), writer is not None)
                self.assertEqual(res["needs_human"], writer is not None)
                text = notify.format_notice(json.loads(notices[0]))
                self.assertEqual("承認待ち" in text, writer is not None)
                self.assertNotIn("返信は不要です", text)
                self.assertLessEqual(len(text.splitlines()), 4)

    def test_judge_down_still_waits_for_the_next_cycle(self):
        class Down:
            def ask(self, state, questions):
                raise BackendUnavailable("kev not up")

        with self.assertRaises(BackendUnavailable):
            graph.run(ALERT, STORAGE, Down())

    def test_batch_falls_back_to_single_questions_after_a_bad_answer(self):
        class Batch:
            def __init__(self):
                self.calls = []

            def ask(self, state, questions):
                self.calls.append(sorted(questions))
                if len(questions) > 1:
                    return {q: {"noul": 7} for q in questions}
                return {"impact": {"score": 3.5, "confidence": 0.9}}

        be = Batch()
        r = graph.run(ALERT, {**STORAGE, "description": "Storage 92% used"}, be, batch=True)
        self.assertEqual(r["node"], "page")   # impact answered alone; the plan without playbooks pages
        self.assertEqual(len([c for c in be.calls if len(c) > 1]), 1)
        self.assertEqual(be.calls[-1], ["impact"])


GRAPHS_ALL = graph.load_dir(ROOT / "graphs", PBS)


class TestSevereWorkItems(unittest.TestCase):
    ITEM = {"kind": "ado.workitem.created", "id": "7", "type": "Bug", "title": "画面の文言が古い",
            "description": "期待: 新しい文言", "acceptance_criteria": "新しい文言が出る", "priority": 1}

    def test_lowering_a_priority_1_item_goes_to_a_person(self):
        r = graph.run(ADO, self.ITEM, ReplayBackend({"priority": {"score": 0.3, "confidence": 0.9}}))
        self.assertEqual(r["node"], "triage_pm")
        self.assertTrue(r["needs_human"])

    def test_a_priority_1_item_is_notified_on_every_path(self):
        vague = {**self.ITEM, "description": "おかしい", "acceptance_criteria": ""}
        r = graph.run(ADO, vague, ReplayBackend({"ready": {"noul": 0.1}}))
        self.assertEqual((r["node"], r["notify"], r["severe_guard"]), ("request_info", True, "priority=1"))
        r = graph.run(ADO, {**vague, "priority": 3}, ReplayBackend({"ready": {"noul": 0.1}}))
        self.assertEqual((r["node"], r["notify"]), ("request_info", False))

    def test_other_priorities_are_decided_as_before(self):
        r = graph.run(ADO, {**self.ITEM, "priority": 2}, ReplayBackend({"priority": {"score": 0.3, "confidence": 0.9}}))
        self.assertEqual((r["node"], r["notify"], r["needs_human"]), ("set_p3", False, False))


class TestSevereChats(unittest.TestCase):
    def chat(self, text):
        return graph.run(TEAMS, {"kind": "teams.chat", "id": "c1", "text": text, "author": "佐藤"},
                         ReplayBackend({"intent": {"choice": "fyi", "confidence": 0.9}}))

    def test_production_outage_in_chat_is_an_incident_without_the_model(self):
        r = self.chat("本番で決済が通らないと顧客から連絡がありました")
        self.assertEqual((r["node"], r["notify"]), ("incident_bug", True))
        self.assertNotIn("intent", [p["node"] for p in r["path"]])

    def test_recovery_or_test_environment_wording(self):
        r = self.chat("本番でログインできなくなっていた件は復旧しました")
        self.assertEqual((r["node"], r["needs_human"]), ("ask_pm", True))
        self.assertEqual(self.chat("テスト環境でログインできなくなっています")["node"], "log_fyi")
        self.assertEqual(self.chat("明日の定例の資料です")["node"], "log_fyi")

    def test_reports_of_a_breakage_are_caught(self):
        for text in ("本番でログインできなくなっています", "本番が落ちています", "多数の顧客から問い合わせが来ています",
                     "ユーザーからログインできないという問い合わせが複数来ています", "production is down",
                     "customers cannot check out"):
            self.assertEqual(self.chat(text)["node"], "incident_bug", text)

    def test_questions_plans_and_negations_are_left_to_the_model(self):
        for text in ("ログインできない人は連絡ください", "本番リリースでエラーが出ないか確認お願いします",
                     "決済できないケースのテストを追加しました", "購入できない場合の仕様は", "顧客からエラーの報告はありません",
                     "本番は停止せずにリリース可能です", "本番反映できない理由を教えて", "no outage expected",
                     "Can all users see the banner?", "本番で決済できないのですか？"):
            self.assertEqual(self.chat(text)["node"], "log_fyi", text)


if __name__ == "__main__":
    unittest.main()
