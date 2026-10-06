try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import html.parser
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kimeru import brief, cli, notify, report


NOW = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)


def write(path, rows):
    Path(path).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def step(title, due_level=0):
    return {"id": title, "title": title, "due": "今日", "due_level": due_level}


def decision(event_id, title, *, at=NOW, needs_human=False, outcome="decide", actions=None):
    return {"graph": "g", "event_id": event_id, "node": "d", "outcome": outcome,
            "needs_human": needs_human, "at": at.isoformat(), "summary": title,
            "path": [{"node": "d", "edge": "ok", "answer": {}}],
            "plan": {"playbook": "pb", "title": "計画", "steps": [step(title)]}, "actions": actions or []}


class TextParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []

    def handle_data(self, data):
        self.text.append(data)


class TestBriefStateViews(unittest.TestCase):
    def test_rejected_and_done_cases_do_not_replay_steps(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [decision("rejected", "却下手順"), decision("done", "完了手順")]
            write(Path(d) / "decisions.jsonl", rows)
            (Path(d) / "approvals.json").write_text(json.dumps({"items": {
                "1": {"key": "g:rejected:d", "status": "rejected", "record": rows[0]},
                "2": {"key": "g:done:d", "status": "approved", "work": {"state": "done"}, "record": rows[1]},
            }}), encoding="utf-8")
            self.assertEqual(brief.collect(d, now=NOW), [])

    def test_pending_cases_remain_confirmation_candidates_without_execution_steps(self):
        with tempfile.TemporaryDirectory() as d:
            rec = decision("pending", "承認前手順")
            write(Path(d) / "decisions.jsonl", [rec])
            (Path(d) / "approvals.json").write_text(json.dumps({"items": {
                "1": {"key": "g:pending:d", "status": "held", "record": rec},
            }}), encoding="utf-8")
            got = brief.collect(d, now=NOW)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["kind"], "approval")
            self.assertIn("確認待ち #1", got[0]["text"])
            self.assertNotIn("承認前手順", got[0]["text"])

    def test_old_approved_unfinished_current_plan_is_included(self):
        with tempfile.TemporaryDirectory() as d:
            old = NOW - timedelta(days=8)
            rec = decision("old", "古い未完了手順", at=old)
            write(Path(d) / "decisions.jsonl", [rec])
            (Path(d) / "approvals.json").write_text(json.dumps({"items": {
                "1": {"key": "g:old:d", "status": "approved", "revision": 1,
                      "work": {"state": "in_progress"}, "record": rec},
            }}), encoding="utf-8")
            got = brief.collect(d, now=NOW)
            self.assertEqual(len(got), 1)
            self.assertIn("古い未完了手順", got[0]["text"])

    def test_actual_cli_merge_suppresses_old_plan_and_uses_current_revision(self):
        with tempfile.TemporaryDirectory() as d:
            original = decision("first", "古い計画")
            key = "g:first:d"
            (Path(d) / "approvals.json").write_text(json.dumps({"next": 2, "items": {
                "1": {"key": key, "status": "pending", "revision": 1, "record": {
                    **original, "event": {"chat_id": "chat-1", "author": "PM"}}},
            }}), encoding="utf-8")
            incoming = decision("followup", "改訂後の計画", at=NOW - timedelta(days=8))
            incoming["event"] = {"chat_id": "chat-1", "author": "PM"}
            ev = {"id": "followup", "kind": "teams.chat", "chat_id": "chat-1", "author": "PM", "text": "補足"}
            self.assertTrue(cli._merge_locked(Path(d), ev, incoming, notify))
            ap = notify.Approvals(d)
            ap.data["items"]["1"]["status"] = "approved"
            ap.data["items"]["1"]["work"] = {"state": "in_progress"}
            ap.save()
            unrelated = decision("followup", "他グラフの同ID", at=NOW)
            unrelated["graph"] = "custom"
            write(Path(d) / "decisions.jsonl", [original, decision("followup", "改訂後の計画", at=NOW), unrelated])
            got = brief.collect(d, now=NOW)
            texts = [row["text"] for row in got]
            self.assertEqual(len(texts), 2)
            self.assertIn("改訂後の計画", texts[0])
            self.assertNotIn("古い計画", " ".join(texts))
            self.assertTrue(any("他グラフの同ID" in text for text in texts))

    def test_empty_rank_with_known_unfinished_work_is_not_no_action(self):
        class Backend:
            def __init__(self):
                self.calls = 0

            def ask(self, state, questions):
                self.calls += 1
                return {}

        with tempfile.TemporaryDirectory() as d:
            rec = decision("old", "期限なし", at=NOW - timedelta(days=8))
            rec["plan"]["steps"] = [step("将来の手順", 2)]
            write(Path(d) / "decisions.jsonl", [rec])
            (Path(d) / "approvals.json").write_text(json.dumps({"items": {
                "1": {"key": "g:old:d", "status": "approved", "record": rec,
                      "work": {"state": "Unknown"}},
            }}), encoding="utf-8")
            backend = Backend()
            text, ranked = brief.build(d, backend, now=NOW)
            self.assertEqual(ranked, [])
            self.assertEqual(backend.calls, 0)
            self.assertIn("実行候補はありません", text)
            self.assertNotIn("対応が必要な項目はありません", text)
            self.assertIn("Unknown", text)


class TestReportStateViews(unittest.TestCase):
    def render_text(self, out, rows, approvals, extra=None):
        write(Path(out) / "decisions.jsonl", rows)
        (Path(out) / "approvals.json").write_text(json.dumps(approvals, ensure_ascii=False), encoding="utf-8")
        if extra:
            for name, data in extra.items():
                if name.endswith(".jsonl"):
                    write(Path(out) / name, data)
                else:
                    (Path(out) / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        parser = TextParser()
        parser.feed(report.build(out, {}))
        return " ".join(parser.text)

    def test_needs_human_is_visible_for_decide_outcome(self):
        with tempfile.TemporaryDirectory() as d:
            row = decision("e1", "確認対象", needs_human=True,
                            actions=[{"type": "ado.comment", "id": "42", "text": "下書き"}])
            text = self.render_text(d, [row], {"items": {"1": {
                "key": "g:e1:d", "status": "pending", "revision": 1, "record": row,
            }}})
            self.assertIn("人の確認", text)
            self.assertIn("返信待ち", text)
            self.assertIn("承認後の計画（外部書き込みは既定で記録のみ）", text)

    def test_verified_ado_execution_and_comment_id_are_reported(self):
        with tempfile.TemporaryDirectory() as d:
            row = decision("e1", "ADO更新", actions=[{"type": "ado.comment", "id": "42", "text": "下書き"}])
            text = self.render_text(d, [row], {"items": {}}, {"executions.jsonl": [
                {"id": 1, "key": "synthetic", "type": "ado.comment", "target": "42", "state": "done",
                 "result": {"comment_id": 100}},
            ]})
            self.assertIn("実行確認済み: 1", text)
            self.assertIn("ADO コメント 100", text)
            self.assertNotIn("ADO 更新・当番呼び出し・Teams 返信は実行予定として記録のみ", text)
            self.assertIn("記録された計画", text)

    def test_old_execution_for_same_target_does_not_attach_to_revised_body(self):
        with tempfile.TemporaryDirectory() as d:
            action = {"type": "ado.comment", "id": "42", "text": "current revised body"}
            row = decision("e1", "更新案", actions=[action])
            row["revision"] = 2
            approvals = {"items": {"1": {"key": "g:e1:d", "status": "approved", "revision": 2,
                                                "record": row, "work": {"state": "in_progress"}}}}
            text = self.render_text(d, [row], approvals, {"executions.jsonl": [
                {"id": 1, "key": "old-frozen-body", "type": "ado.comment", "target": "42", "state": "done",
                 "result": {"comment_id": 100}},
            ]})
            self.assertIn("実行確認済み: 1", text)  # historical evidence remains visible globally
            self.assertIn("予定（dry-run／実行記録との対応は未確認）", text)
            self.assertEqual(text.count("ADO コメント 100"), 1)  # summary only; no current-action attribution

    def test_conflicting_and_legacy_unknown_delivery_states_do_not_expose_body(self):
        with tempfile.TemporaryDirectory() as d:
            row = decision("e1", "確認待ち", needs_human=True)
            approvals = {"items": {"1": {"key": "g:e1:d", "status": "approved", "record": row,
                                                "delivery_unknown": True}},
                         "outbox_delivery_unknown": True,
                         "outbox_delivery_attempt": {"kind": "outbox", "body": "PRIVATE BODY"}}
            state = {"brief_delivery_unknown": {"text": "PRIVATE BRIEF"}}
            text = self.render_text(d, [row], approvals, {"daily_state.json": state,
                                                          "executions.jsonl": [
                {"id": 1, "key": "conflict", "type": "ado.comment", "state": "running", "at": "2026-10-07T00:00:00+00:00"},
            ]})
            self.assertIn("配信結果不明", text)
            self.assertIn("実行中記録", text)
            self.assertNotIn("PRIVATE BODY", text)
            self.assertNotIn("PRIVATE BRIEF", text)
            self.assertNotIn("API実行の確認ではない", text)

    def test_latest_execution_per_key_is_counted_once_and_closed_is_not_api_success(self):
        with tempfile.TemporaryDirectory() as d:
            executions = [
                {"id": 1, "key": "same", "type": "ado.comment", "target": "42", "state": "running",
                 "at": "2026-10-07T00:00:00+00:00"},
                {"id": 1, "key": "same", "type": "ado.comment", "target": "42", "state": "done",
                 "at": "2026-10-07T00:00:01+00:00", "result": {"comment_id": 101}},
                {"id": 2, "key": "closed", "type": "ado.comment", "target": "43", "state": "closed"},
            ]
            text = self.render_text(d, [], {"items": {}}, {"executions.jsonl": executions})
            self.assertIn("実行確認済み: 1", text)
            self.assertIn("本人が確認して終了（API実行の確認ではない）: 1", text)
            self.assertNotIn("実行中記録", text)
            self.assertEqual(text.count("ADO コメント 101"), 1)

    def test_current_revision_has_progress_but_old_decision_is_history_only(self):
        with tempfile.TemporaryDirectory() as d:
            old = decision("first", "旧計画", needs_human=True)
            current = decision("followup", "現計画", needs_human=True)
            current["revision"] = 2
            (Path(d) / "merged.jsonl").write_text(json.dumps({"into": "g:first:d", "event_id": "followup", "revision": 2}) + "\n", encoding="utf-8")
            approvals = {"items": {"1": {"key": "g:first:d", "status": "approved", "revision": 2,
                                                "work": {"state": "in_progress"}, "record": current,
                                                "exec": {"0": {"key": "new-key", "state": "done", "result": {"comment_id": 100}}}}}}
            old["actions"] = [{"type": "ado.comment", "id": "42", "text": "old"}]
            current["actions"] = [{"type": "ado.comment", "id": "42", "text": "new"}]
            unrelated = decision("followup", "別グラフの計画", needs_human=True)
            unrelated["graph"] = "custom"
            text = self.render_text(d, [old, current, unrelated], approvals, {"executions.jsonl": [
                {"id": "1", "key": "new-key", "type": "ado.comment", "target": "42", "state": "done", "result": {"comment_id": 100}},
            ]})
            self.assertIn("改訂 2 で置換済み", text)
            self.assertIn("現計画", text)
            self.assertIn("別グラフの計画", text)
            self.assertIn("作業進捗: in_progress", text)
            self.assertEqual(text.count("ADO コメント 100"), 2)  # one summary, one current-case detail

    def test_reused_event_key_without_revision_is_history_only(self):
        with tempfile.TemporaryDirectory() as d:
            old = decision("same", "改訂前")
            current = decision("same", "改訂後")
            current["revision"] = 2
            current["plan"]["steps"] = [step("改訂後")]
            current["actions"] = [{"type": "ado.comment", "id": "42", "text": "current"}]
            approvals = {"items": {"1": {"key": "g:same:d", "status": "approved", "revision": 2,
                                                "work": {"state": "in_progress"}, "record": current,
                                                "exec": {"0": {"key": "current-key", "state": "done", "result": {"comment_id": 200}}}}}}
            text = self.render_text(d, [old, current], approvals)
            self.assertIn("履歴: ケース #1 の改訂 2 で置換済み", text)
            self.assertIn("改訂後", text)
            self.assertEqual(text.count("実行確認済み（ADO コメント 200）"), 1)


if __name__ == "__main__":
    unittest.main()
