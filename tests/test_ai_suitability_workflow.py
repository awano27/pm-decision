"""Offline integration coverage for Jev suitability, persistence, and approval gates."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import copy
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from kimeru import cli, config, events, graph, notify, writer
from kimeru.backends import JevBackend, ReplayBackend


ASSESSMENT_IDS = {"ai_suitability", "ai_suitability_reason"}


class _Response:
    def __init__(self, answers):
        self.body = json.dumps({"answers": answers}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class _Transport:
    """Exercises the real Jev serializer and answer parser without any network."""
    def __init__(self, levels=(2,), reason=None, fail_assessment=False):
        self.levels = list(levels)
        self.reason = reason
        self.fail_assessment = fail_assessment
        self.requests = []
        self.assessments = []

    def __call__(self, request, timeout):
        body = json.loads(request.data)
        self.requests.append(body)
        questions = body["questions"]
        if set(questions) == {"classify"}:
            return _Response({"classify": {"type": "noul", "noul": 0.95}})
        if set(questions) == {"item_type"}:
            return _Response({"item_type": {"type": "choice", "choice": "decision", "confidence": 0.3}})
        if set(questions) != ASSESSMENT_IDS:
            raise AssertionError(f"unexpected question ids: {sorted(questions)}")
        self.assessments.append(body)
        if self.fail_assessment:
            raise urllib.error.HTTPError(request.full_url, 503, "synthetic unavailable", {},
                                         io.BytesIO(b"SYNTHETIC_PRIVATE_DIAGNOSTIC"))
        level = self.levels[min(len(self.assessments) - 1, len(self.levels) - 1)]
        reason = self.reason or ("missing_local_context" if level <= 3 else "general_knowledge")
        return _Response({
            "ai_suitability": {"type": "choice", "choice": str(level), "confidence": 0.95},
            "ai_suitability_reason": {"type": "choice", "choice": reason, "confidence": 0.95},
        })


class _Writer:
    NAME = "synthetic-writer"

    def draft(self, result, event, instruction=None):
        return {key: "一般的な考え方を整理しました。比較した内容を確認してからご利用ください。"
                for key, _action in writer.targets(result)}


class _Bridge:
    def __init__(self):
        self.posts = []

    def post(self, text, send):
        self.posts.append((text, send))
        return {"ok": True, "typed": True, "sent": False, "readback": {"matched": False}}


class TestSuitabilityWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "out"
        self.out.mkdir()
        clean = {key: value for key, value in os.environ.items() if not key.startswith("KIMERU_")}
        clean.update(KIMERU_STATE_DIR=str(self.root / "state"), KIMERU_WRITER="", KIMERU_PUSH="")
        env = mock.patch.dict(os.environ, clean, clear=True)
        env.start()
        self.addCleanup(env.stop)
        config.apply([])
        self.event = {"kind": "teams.chat", "id": "synthetic-request", "chat_id": "synthetic-chat",
                      "author": "テスト依頼者", "text": "一般的な比較方法を判断してください。"}
        self.g = {
            "name": "suitability-workflow", "event": "teams.chat", "start": "classify",
            "nodes": {
                "classify": {"kind": "judge", "question": {"type": "noul", "instructions": "Is a reply requested?"},
                             "routes": {"yes": "reply", "no": "reply", "unsure": "human"}},
                "reply": {"kind": "decide", "actions": [{"type": "teams.reply", "chat_id": "{event.chat_id}",
                                                          "text": "一般的な比較の観点を提示します。"}]},
                "human": {"kind": "advise", "queue": True, "actions": []},
            },
        }
        graph.validate(self.g)
        self.backend = JevBackend(model="synthetic-jev", key_env="KIMERU_TEST_UNUSED_API_KEY",
                                  key_required=False, retries=1, api="https://example.invalid/v1")

    def process(self, transport, *, event=None, draft_writer=None, reader=None, now=None):
        with mock.patch("kimeru.backends._urlopen", side_effect=transport), \
                mock.patch("kimeru.writer.get_writer", return_value=None):
            return cli.process(event or self.event, {self.g["event"]: [self.g]}, self.backend, self.out,
                               playbooks={}, writer=draft_writer, reader=reader, now=now)[0]

    def rows(self, name):
        return [json.loads(line) for line in (self.out / name).read_text(encoding="utf-8").splitlines()]

    def register_pending(self, result):
        bridge = _Bridge()
        notify.notify(self.out, bridge, send=False)
        self.assertTrue(bridge.posts)
        self.assertTrue(all(send is False for _text, send in bridge.posts))
        return next(item for item in notify.Approvals(self.out).data["items"].values()
                    if item["record"]["event_id"] == result["event_id"])

    def test_local_information_gap_is_queued_with_assessment_and_no_executed_actions(self):
        self.event["text"] = "この案件の予算を増やしてよいか判断してください。契約条件はまだ共有していません。"
        transport = _Transport(levels=(2,))
        with mock.patch("kimeru.actions.execute", side_effect=AssertionError("action must be held")):
            result = self.process(transport)
        self.assertEqual(result["outcome"], "advise")
        self.assertEqual(result["node"], "reply", "case identity retains the graph terminal")
        self.assertEqual(result["executed"], [])
        self.assertTrue(result["review_required"])
        self.assertEqual(result["ai_suitability"]["level"], 2)
        self.assertEqual(result["ai_suitability"]["route"], "human")
        for name in ("decisions.jsonl", "queue.jsonl"):
            [record] = self.rows(name)
            self.assertEqual(record["ai_suitability"], result["ai_suitability"])
            self.assertEqual(record["executed"], [])
        case = self.register_pending(result)
        self.assertEqual(case["status"], "pending")
        self.assertFalse(case["posted"])
        self.assertEqual(case["record"]["ai_suitability"], result["ai_suitability"])

    def test_general_reasoning_can_proceed_as_an_existing_dry_run(self):
        result = self.process(_Transport(levels=(5,)))
        self.assertEqual(result["ai_suitability"]["route"], "ai")
        self.assertEqual(result["outcome"], "decide")
        self.assertFalse(result["needs_human"])
        self.assertEqual([action["status"] for action in result["executed"]], ["planned"])
        self.assertFalse((self.out / "queue.jsonl").exists())

    def test_high_rating_preserves_existing_queued_terminal_and_pending_case(self):
        self.g["nodes"]["reply"].update(kind="advise", queue=True)
        for level in (4, 5):
            with self.subTest(level=level):
                self.event["id"] = f"high-{level}"
                result = self.process(_Transport(levels=(level,), reason="supplied_evidence"))
                self.assertEqual(result["ai_suitability"]["route"], "ai")
                self.assertEqual(result["outcome"], "advise")
                self.assertTrue(result["needs_human"])
                self.assertEqual(result["executed"], [])
                case = self.register_pending(result)
                self.assertEqual(case["status"], "pending")

    def test_high_rating_does_not_release_writer_drafts(self):
        result = self.process(_Transport(levels=(5,)), draft_writer=_Writer())
        self.assertEqual(result["ai_suitability"]["route"], "ai")
        self.assertTrue(result["needs_human"])
        self.assertEqual(result["executed"], [])
        self.assertEqual(result["actions"][0]["drafted_by"], _Writer.NAME)
        self.assertTrue((self.out / "queue.jsonl").exists())

    def test_high_rating_does_not_release_an_execution_gated_action(self):
        self.g["nodes"]["reply"]["actions"] = [{"type": "ado.comment", "id": "synthetic-item", "text": "確認用の提案"}]
        with mock.patch("kimeru.execute.enabled_types", return_value={"ado.comment"}), \
                mock.patch("kimeru.actions.execute", side_effect=AssertionError("approval still required")):
            result = self.process(_Transport(levels=(5,)))
        self.assertEqual(result["ai_suitability"]["route"], "ai")
        self.assertTrue(result["needs_human"])
        self.assertEqual(result["executed"], [])
        [record] = self.rows("queue.jsonl")
        self.assertEqual(record["actions"][0]["type"], "ado.comment")

    def test_assessment_service_failure_holds_locally_classified_action(self):
        transport = _Transport(fail_assessment=True)
        with mock.patch("kimeru.actions.execute", side_effect=AssertionError("failed assessment must hold action")):
            result = self.process(transport)
        self.assertEqual(result["ai_suitability"]["status"], "unavailable")
        self.assertEqual(result["ai_suitability"]["route"], "human")
        self.assertEqual(result["executed"], [])
        self.assertEqual(len(transport.assessments), 1)
        self.assertEqual(self.register_pending(result)["status"], "pending")
        self.assertNotIn("SYNTHETIC_PRIVATE_DIAGNOSTIC", json.dumps(self.rows("queue.jsonl")))

    def test_real_jev_transport_batches_assessment_and_excludes_hidden_event_fields(self):
        event = {**self.event, "raw": {"secret": "RAW_PRIVATE"}, "thread": ["THREAD_PRIVATE"],
                 "origin": {"org": "ORIGIN_PRIVATE"}, "rev": 42, "chat_title": "TITLE_PRIVATE"}
        transport = _Transport(levels=(5,))
        result = self.process(transport, event=event)
        self.assertEqual(len(transport.requests), 2, "classification plus one assessment request")
        [assessment] = transport.assessments
        self.assertEqual(set(assessment["questions"]), ASSESSMENT_IDS)
        self.assertEqual(set(assessment["questions"]["ai_suitability"]["criteria"]), {"1", "2", "3", "4", "5"})
        self.assertEqual(assessment["state"]["text"], event["text"])
        for field in ("raw", "thread", "origin", "rev", "chat_title"):
            self.assertNotIn(field, assessment["state"])
        self.assertNotIn("PRIVATE", json.dumps(transport.requests))
        self.assertEqual(result["judge"]["name"], "Jev")
        self.assertEqual(result["ai_suitability"]["level"], 5)

    def test_full_text_reassesses_available_facts_before_final_result(self):
        preview = {**self.event, "text": "この比較の判断に必要な条件を確認してください。"}
        complete = {**preview, "text": "比較条件は価格のみです。提示した価格を比較して安い方を選んでください。", "full": True}
        transport = _Transport(levels=(2, 4), reason="supplied_evidence")
        with mock.patch("kimeru.fulltext.deepen", return_value=(complete, {"state": "full"})) as deepen:
            result = self.process(transport, event=preview, reader=object())
        deepen.assert_called_once()
        self.assertEqual(len(transport.assessments), 2)
        self.assertEqual([body["state"]["text"] for body in transport.assessments], [preview["text"], complete["text"]])
        self.assertEqual(result["ai_suitability"]["level"], 4)
        self.assertEqual(result["ai_suitability"]["route"], "ai")
        self.assertFalse(result["needs_human"])

    def test_clipped_preview_is_held_when_no_reader_is_available(self):
        for suffix in ("…", "...", "‥"):
            with self.subTest(suffix=suffix):
                event = {**self.event, "id": f"clipped-{suffix}", "text": "比較条件を整理して判断してください" + suffix}
                with mock.patch("kimeru.actions.execute", side_effect=AssertionError("clipped preview must be held")):
                    result = self.process(_Transport(levels=(5,)), event=event)
                self.assertEqual(result["ai_suitability"]["level"], 5)
                self.assertEqual(result["ai_suitability"]["reason_code"], "truncated_input")
                self.assertEqual(result["ai_suitability"]["route"], "human")
                self.assertEqual(result["executed"], [])
                self.assertTrue(result["needs_human"])
                self.assertEqual(self.rows("queue.jsonl")[-1]["event_id"], event["id"])

    def test_clipped_preview_is_held_when_full_read_fails(self):
        event = {**self.event, "text": "比較条件を整理して判断してください…"}
        with mock.patch("kimeru.fulltext.deepen", return_value=(event, {"state": "preview_only", "why": "synthetic failure"})) as deepen, \
                mock.patch("kimeru.actions.execute", side_effect=AssertionError("failed read must keep action held")):
            result = self.process(_Transport(levels=(5,)), event=event, reader=object())
        deepen.assert_called_once()
        self.assertEqual(result["read_full"]["state"], "preview_only")
        self.assertEqual(result["ai_suitability"]["reason_code"], "truncated_input")
        self.assertEqual(result["outcome"], "advise")
        self.assertEqual(result["executed"], [])

    def test_clipped_preview_is_held_when_read_deferrals_are_exhausted(self):
        event = {**self.event, "text": "比較条件を整理して判断してください…"}
        key = f"{self.g['name']}@{cli._graph_version(self.g)}:{event['kind']}:{event['id']}"
        cli._PreviewJudgments(self.out).put(key)
        with mock.patch("kimeru.fulltext.max_defer", return_value=1), \
                mock.patch("kimeru.fulltext.deepen", side_effect=AssertionError("deferral limit reached")), \
                mock.patch("kimeru.actions.execute", side_effect=AssertionError("giving up reading must not authorize action")):
            result = self.process(_Transport(levels=(5,)), event=event, reader=object())
        self.assertEqual(result["read_full"]["state"], "preview_only")
        self.assertEqual(result["ai_suitability"]["reason_code"], "truncated_input")
        self.assertEqual(result["ai_suitability"]["route"], "human")
        self.assertEqual(result["executed"], [])

    def test_successfully_read_full_text_is_not_mistaken_for_a_long_preview(self):
        preview = {**self.event, "text": "比較条件を整理して判断してください…"}
        complete = {**preview, "text": "比較条件は価格のみです。提示した価格を比較して安い方を選んでください。", "full": True}
        self.assertGreater(len(complete["text"]), 25)
        with mock.patch("kimeru.fulltext._int", side_effect=lambda key, default: 25 if key == "preview_cut_len" else default), \
                mock.patch("kimeru.fulltext.deepen", return_value=(complete, {"state": "full"})):
            result = self.process(_Transport(levels=(4,), reason="supplied_evidence"), event=preview, reader=object())
        self.assertEqual(result["read_full"]["state"], "full")
        self.assertEqual(result["ai_suitability"]["level"], 4)
        self.assertEqual(result["ai_suitability"]["route"], "ai")
        self.assertFalse(result["needs_human"])
        self.assertEqual([action["status"] for action in result["executed"]], ["planned"])

    def test_verified_full_text_still_needs_review_when_judge_input_is_capped(self):
        event = {**self.event, "text": "比較条件の説明。" * (events.text_cap() // 8 + 20), "full": True}
        transport = _Transport(levels=(5,))
        with mock.patch("kimeru.actions.execute", side_effect=AssertionError("capped full text must be held")):
            result = self.process(transport, event=event)
        self.assertLess(len(transport.assessments[0]["state"]["text"]), len(event["text"]))
        self.assertEqual(result["ai_suitability"]["reason_code"], "truncated_input")
        self.assertEqual(result["ai_suitability"]["route"], "human")
        self.assertEqual(result["executed"], [])

    def test_learned_record_only_rule_cannot_demote_negative_suitability(self):
        now = datetime(2026, 11, 15, 9, 0)
        phrase = "見積りの件は後で整理する"
        self.g = {"name": "minutes-followup", "event": "meeting.item", "start": "item_type", "nodes": {
            "item_type": {"kind": "judge", "question": {"type": "choice", "instructions": "What kind of minutes item is this?",
                            "criteria": {"decision": "Decision", "status": "Status"}},
                          "routes": {"decision": "record", "status": "record", "unsure": "check_line"}},
            "record": {"kind": "decide", "actions": [{"type": "log.only"}]},
            "check_line": {"kind": "advise", "queue": True, "actions": [{"type": "log.only"}]},
        }}
        graph.validate(self.g)
        rule = {"id": "A1", "graph": "minutes-followup", "node": "check_line", "phrase": phrase,
                "adopted_at": (now - timedelta(days=1)).isoformat(), "uses": 0, "active": True, "judge": "Jev"}
        rules_path = self.out / "learned_rules.json"
        rules_path.write_text(json.dumps({"rules": [rule]}, ensure_ascii=False), encoding="utf-8")
        event = {"kind": "meeting.item", "id": "unsure-local", "meeting": "定例会議", "item": phrase + "。来週の会議で扱う"}
        with mock.patch("kimeru.learn.enabled", return_value=True):
            with mock.patch("kimeru.actions.execute", side_effect=AssertionError("human judgment cannot be suppressed")):
                result = self.process(_Transport(levels=(2,)), event=event, now=now)
            self.assertEqual(result["ai_suitability"]["route"], "human")
            self.assertEqual(result["outcome"], "advise")
            self.assertTrue(result["needs_human"])
            self.assertNotIn("demoted_by", result)
            self.assertEqual(result["executed"], [])
            self.assertEqual(self.rows("queue.jsonl")[-1]["ai_suitability"], result["ai_suitability"])
            self.assertEqual(json.loads(rules_path.read_text(encoding="utf-8"))["rules"][0]["uses"], 0)
            # The same fixture can still use the previously approved record-only rule when no new human gate rejects it.
            allowed = self.process(_Transport(levels=(5,)), event={**event, "id": "known-general"}, now=now)
        self.assertEqual(allowed["demoted_by"], "A1")
        self.assertEqual(allowed["outcome"], "decide")
        self.assertFalse(allowed["needs_human"])
        self.assertEqual(json.loads(rules_path.read_text(encoding="utf-8"))["rules"][0]["uses"], 1)

    def test_legacy_preview_cache_cannot_bypass_current_jev_assessment(self):
        self.g["nodes"]["reply"]["notify"] = True
        for previous_judge in ("Jev", "Kev"):
            with self.subTest(previous_judge=previous_judge):
                event = {**self.event, "id": f"legacy-{previous_judge}"}
                cached = graph.run(self.g, event, ReplayBackend({"classify": {"noul": 0.95}}))
                cached["judge"] = {"name": previous_judge}
                self.assertNotIn("ai_suitability", cached)
                key = f"{self.g['name']}@{cli._graph_version(self.g)}:{event['kind']}:{event['id']}"
                cli._PreviewJudgments(self.out).put(key, copy.deepcopy(cached))
                transport = _Transport(levels=(2,))
                with mock.patch("kimeru.fulltext.deepen", return_value=(event, {"state": "preview_only", "why": "synthetic"})):
                    result = self.process(transport, event=event, reader=object())
                self.assertEqual(len(transport.assessments), 1, "cached preview must receive the new assessment")
                self.assertEqual(result["ai_suitability"]["route"], "human")
                self.assertEqual(result["outcome"], "advise")
                self.assertEqual(result["executed"], [])


if __name__ == "__main__":
    unittest.main()
