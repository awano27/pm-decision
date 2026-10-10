"""A follow-up replaces stale pending material under the same case identity."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path

from kimeru import cli, fulltext, notify


class TestFollowupMerge(unittest.TestCase):
    def test_merge_replaces_stale_fields_scrubs_backup_and_keeps_unknown_hold(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            ap = notify.Approvals(out)
            old_key = "g:old:n"
            old = {"graph": "g", "event_id": "old", "node": "n", "outcome": "advise",
                   "needs_human": True, "advice": "old decision", "summary": "old source",
                   "event": {"kind": "teams.chat", "id": "old", "chat_id": "chat-1", "author": "A"},
                   "material_event": {"text": "old excerpt"}, "copilot_request": "OLD_PRIVATE_REQUEST",
                   "actions": [{"type": "ado.comment", "text": "old draft", "exec_text": "OLD_PRIVATE_EXEC",
                                "exec_key": "OLD_EXEC_KEY", "exec_state": "done"}]}
            old["followups"] = [{"text": "OLD_FOLLOWUP " * 20, "at": "2026-10-02T00:00:00Z"}]
            ap.data["items"]["7"] = {"key": old_key, "revision": 3, "status": "pending", "posted": True,
                                        "record": old, "exec": {"0": {"state": "done", "text": "OLD_PRIVATE_EXEC"}},
                                        "exec_history": [{"text": "OLD_PRIVATE_EXEC"}],
                                        "delivery_unknown": True, "delivery_unknown_part": "post",
                                        "delivery_unknown_at": "2026-10-03T00:00:00Z"}
            ap.data["next"] = 8
            ap.save()
            fulltext.save(out, old_key, {"text": "OLD_PRIVATE_FULL", "author": "A"})
            long_source = "public " * 300 + "TAIL_PRIVATE_SECRET beyond the retained preview"
            safe_preview = ("public " * 30)[:119] + "…"
            new_event = {"kind": "teams.chat", "id": "new", "chat_id": "chat-1", "author": "A",
                         "text": long_source}
            new_result = {"graph": "g", "event_id": "new", "node": "n", "outcome": "decide",
                          "needs_human": False, "advice": "new decision", "summary": "new source",
                          "event": {**new_event, "text": safe_preview}, "material_event": {"text": safe_preview},
                          "actions": [{"type": "log.only", "text": "new action"}], "plan": {"title": "new", "summary": "next"}}
            self.assertTrue(cli._merge_locked(out, new_event, new_result, notify, source_event=new_event))
            saved = notify.Approvals(out).data["items"]["7"]
            self.assertEqual(saved["key"], old_key)
            self.assertEqual(saved["revision"], 4)
            self.assertEqual(saved["record"]["event_id"], "new")
            self.assertEqual([a["type"] for a in saved["record"]["actions"]], ["log.only"])
            self.assertNotIn("copilot_request", saved["record"])
            self.assertNotIn("exec", saved)
            self.assertNotIn("exec_history", saved)
            self.assertFalse(saved["posted"])
            self.assertTrue(saved["delivery_unknown"])
            self.assertEqual(saved["delivery_unknown_revision"], 3)
            self.assertEqual(fulltext.load(out, old_key)["text"], long_source)
            self.assertNotIn("OLD_PRIVATE", (out / "approvals.json").read_text(encoding="utf-8"))
            backup = out / "approvals.json.bak"
            self.assertFalse(backup.exists() and "OLD_PRIVATE" in backup.read_text(encoding="utf-8"))
            for path in out.glob("*.jsonl"):
                self.assertNotIn("OLD_PRIVATE", path.read_text(encoding="utf-8"))
            for path in [out / "approvals.json", backup, out / "merged.jsonl"]:
                if path.exists():
                    self.assertNotIn("TAIL_PRIVATE_SECRET", path.read_text(encoding="utf-8"))
            self.assertIn("public", json.dumps(saved["record"], ensure_ascii=False))
            self.assertLessEqual(len(saved["record"]["followups"][0]["text"]), 120)
            self.assertEqual(notify.confirm_delivery(out, "case", "7", confirm_delivered=True)["status"], "confirmed")
            after_confirm = notify.Approvals(out).data["items"]["7"]
            self.assertFalse(after_confirm["posted"])  # the confirmed post belongs to revision 3, not revision 4
            self.assertEqual(after_confirm["status"], "pending")

    def test_approved_case_does_not_merge_even_if_an_old_delivery_hold_remains(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            ap = notify.Approvals(out)
            event = {"kind": "teams.chat", "id": "old", "chat_id": "chat-1", "author": "A"}
            ap.data["items"]["7"] = {"key": "g:old:n", "status": "approved", "delivery_unknown": True,
                                        "record": {"event": event, "node": "n", "actions": []}}
            ap.save()
            new_event = {**event, "id": "new", "text": "follow-up"}
            result = {"node": "n", "event": new_event, "actions": []}
            self.assertFalse(cli._merge_locked(out, new_event, result, notify, source_event=new_event))


class TestApprovalPostContext(unittest.TestCase):
    def test_decision_next_missing_effect_and_revision_precede_actions(self):
        rec = {"advice": "いまの判断", "memo": {"next": "利用者数を確認", "missing": ["影響する期間"]},
               "revision": 2,
               "actions": [{"type": "teams.reply", "text": "copy body"},
                           {"type": "ado.comment", "id": "9", "exec_text": "EXACT\nCOMMENT BODY"},
                           {"type": "log.only", "text": "a local note"}]}
        text = notify.format_detail(5, rec)   # the full text (the answer to `詳細 5`) keeps the order and all the parts
        self.assertLess(text.index("いまの判断"), text.index("利用者数を確認"))
        self.assertLess(text.index("利用者数を確認"), text.index("影響する期間"))
        self.assertIn("改訂 2", text)
        self.assertIn("記録", text)
        self.assertIn("コピー用", text)
        self.assertIn("実行設定が有効なら、承認後に ADO へコメントを書き込む", text)
        self.assertIn("EXACT\nCOMMENT BODY", text)
        short = notify.format_post(5, rec)   # the short approval post: the next step first, the exact comment, the revision as "（更新）"
        self.assertLessEqual(len(short.splitlines()), 6)
        self.assertTrue(short.startswith("[kimeru #5] （更新）"))
        self.assertLess(short.index("→ 利用者数を確認"), short.index("EXACT\nCOMMENT BODY"))
        self.assertIn("の #9 にコメントを書きます（上の文面そのまま）", short)
        self.assertNotIn("改訂", short)
        self.assertTrue(short.endswith("OK 5 / NG 5 / 詳細 5"))

    def test_repro_steps_are_kept_and_summary_limited_like_other_free_text(self):
        from unittest import mock
        long = "手順" * 150
        with mock.patch.object(cli.config, "value", return_value="0"):
            result = cli._recorded_event({"kind": "ado.workitem.created", "id": "1", "repro_steps": long})
        self.assertEqual(len(result["repro_steps"]), 120)
        self.assertTrue(result["repro_steps"].endswith("…"))

    def test_long_repro_steps_are_scrubbed_to_2000_and_kept_only_with_pending_full_text(self):
        from unittest import mock
        from kimeru import fulltext
        long = "手順" * 1300
        result = {"needs_human": True, "material_event": {"repro_steps": long}, "actions": []}
        ev = {"text": "preview", "thread": [], "author": "A"}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(cli.config, "value", return_value="1"):
            scrubbed, _ = cli._seal_material(result, [], d, "g:1:n", ev, merged=False)
            self.assertEqual(len(scrubbed["material_event"]["repro_steps"]), 2000)
            held = fulltext.load(d, "g:1:n")
            self.assertEqual(len(held["material"]["repro_steps"]), len(long))


if __name__ == "__main__":
    unittest.main()
