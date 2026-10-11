"""A follow-up replaces stale pending material under the same case identity; a thanks or OK after a waiting item is only a
followup line (rule `ack_only`), and nothing else about the item changes."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path

from kimeru import cli, fulltext, graph, notify, plan
from kimeru.backends import ReplayBackend
from tests.test_notify import FakeBridge
from tests.test_writer import FakeWriter

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)


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


class NoModel:
    """A judge that must not be asked anything: a thanks or OK is settled by the rule before any question."""

    def ask(self, state, questions):
        raise AssertionError(f"the model was asked {sorted(questions)}")


UNSURE_DECISION = ReplayBackend({"intent": {"choice": "decision", "confidence": 0.3, "probabilities": {}}})
CONFIDENT_FYI = ReplayBackend({"intent": {"choice": "fyi", "confidence": 0.9, "probabilities": {}}})
ASKED = "リリース日を決めてください"
THANKS = "ありがとうございます！"


class TestAcknowledgementFold(unittest.TestCase):
    """A thanks or OK from the same person after a waiting item joins that item as one followup line. The record, the
    revision, the posting, the status, the measurement and the full text stay as they were; nothing is asked or written."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)
        self.bridge, self.writer = FakeBridge(), FakeWriter()

    def ev(self, i, text, author="田中"):
        return {"kind": "teams.chat", "id": str(i), "chat_id": "19:same@thread.v2", "author": author, "text": text,
                "mentions_me": True}

    def process(self, ev, backend):
        return cli.process(ev, GRAPHS, backend, self.out, PBS, writer=self.writer, dedup=True)[0]

    def ask_pm(self):
        """The first request: the model is unsure, so it waits for the PM and is posted under #1."""
        self.process(self.ev(1, ASKED), UNSURE_DECISION)
        notify.notify(self.out, self.bridge, send=True)

    def item(self, n="1"):
        return notify.Approvals(self.out).data["items"][n]

    def test_thanks_after_a_waiting_request_only_adds_a_followup_line(self):
        self.ask_pm()
        before = self.item()
        posts, calls, short_post = len(self.bridge.posts), len(self.writer.calls), notify.format_post(1, before["record"])
        self.assertTrue(self.process(self.ev(2, THANKS), NoModel()).get("merged"))
        after = self.item()
        self.assertEqual(after["record"]["node"], "ask_pm")
        self.assertEqual([a["type"] for a in after["record"]["actions"]], ["teams.reply", "ado.create"])
        self.assertEqual(after["record"]["event"]["text"], ASKED)
        self.assertEqual([f["text"] for f in after["record"]["followups"]], [THANKS])
        for field in ("status", "revision", "posted", "toasted", "measurement"):
            self.assertEqual(after.get(field), before.get(field), field)
        notify.notify(self.out, self.bridge, send=True)
        self.assertEqual(len(self.bridge.posts), posts)                 # nothing is posted again
        self.assertEqual(len(self.writer.calls), calls)                 # the writer is not called
        self.assertEqual(notify.format_post(1, after["record"]), short_post)   # the short post the PM sees is byte-identical

    def test_thanks_after_a_hold_keeps_the_item_held(self):
        self.ask_pm()
        notify.collect(self.out, FakeBridge(["保留 1"]), send=False)
        self.assertEqual(self.item()["status"], "held")
        self.assertTrue(self.process(self.ev(2, THANKS), NoModel()).get("merged"))
        self.assertEqual(self.item()["status"], "held")
        self.assertEqual([f["text"] for f in self.item()["record"]["followups"]], [THANKS])

    def test_a_substantive_fyi_from_the_same_person_is_still_posted_again_under_the_same_number(self):
        self.ask_pm()
        self.process(self.ev(2, "資料を共有しました"), CONFIDENT_FYI)
        item = self.item()
        self.assertEqual(item["status"], "pending")
        self.assertFalse(item["posted"])                                # replaced, so it is shown again under #1
        self.assertEqual(item["revision"], 2)
        self.assertEqual([f["text"] for f in item["record"]["followups"]], ["資料を共有しました"])
        notify.notify(self.out, self.bridge, send=True)
        self.assertIn("（更新）", self.bridge.posts[-1][0])

    def test_thanks_calls_no_model_and_no_writer_and_keeps_the_full_text_waiting(self):
        self.ask_pm()
        long_text = "田中さんの長いメッセージ " * 40
        fulltext.save(self.out, self.item()["key"], {"text": long_text, "author": "田中"})
        calls = len(self.writer.calls)
        self.process(self.ev(2, THANKS), NoModel())                    # NoModel fails the test if it is asked
        self.assertEqual(len(self.writer.calls), calls)
        self.assertEqual(fulltext.load(self.out, self.item()["key"])["text"], long_text)

    def test_thanks_does_not_join_an_item_the_pm_already_approved(self):
        self.ask_pm()
        notify.collect(self.out, FakeBridge(["OK 1"]), send=False)
        self.assertEqual(self.item()["status"], "approved")
        self.assertFalse(self.process(self.ev(2, THANKS), NoModel()).get("merged"))
        self.assertNotIn("followups", self.item()["record"])
        self.assertEqual(self.item()["status"], "approved")

    def test_a_continuation_that_notifies_is_not_folded_even_with_thanks_in_it(self):
        self.ask_pm()
        res = self.process(self.ev(2, "ありがとうございます。本番で決済が通りません"), NoModel())
        self.assertFalse(res.get("merged"))
        self.assertTrue(res["notify"])
        self.assertNotIn("followups", self.item()["record"])

    def test_the_followups_keep_the_newest_twenty_lines_and_cut_each_at_120_characters(self):
        self.ask_pm()
        texts = [f"ありがとうございます{'！' * i}" for i in range(1, 24)]
        for i, text in enumerate(texts, start=2):
            self.process(self.ev(i, text), NoModel())
        followups = [f["text"] for f in self.item()["record"]["followups"]]
        self.assertEqual(followups, texts[-20:])
        self.process(self.ev(30, "ありがとうございます" * 13), NoModel())
        last = self.item()["record"]["followups"][-1]["text"]
        self.assertLessEqual(len(last), 120)
        self.assertTrue(last.endswith("…"))


class TestContinuedItem(unittest.TestCase):
    """A continuation that revises a waiting item (the same node) keeps the first request: line 1 names it, the newest message
    follows as "続き「…」", a held item says "（前回 保留）" until the PM answers OK or NG, and the record keeps the first request
    at most 120 characters long (R08)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)
        self.bridge, self.writer = FakeBridge(), FakeWriter()

    def ev(self, i, text, author="田中"):
        return {"kind": "teams.chat", "id": str(i), "chat_id": "19:same@thread.v2", "author": author, "text": text,
                "mentions_me": True}

    def process(self, ev, backend):
        return cli.process(ev, GRAPHS, backend, self.out, PBS, writer=self.writer, dedup=True)[0]

    def ask_pm(self, text=ASKED):
        self.process(self.ev(1, text), UNSURE_DECISION)
        notify.notify(self.out, self.bridge, send=True)

    def item(self, n="1"):
        return notify.Approvals(self.out).data["items"][n]

    def test_a_continuation_posts_the_first_request_on_line_one_and_the_newest_message_on_line_three(self):
        self.ask_pm()
        self.process(self.ev(2, "資料を共有しました"), CONFIDENT_FYI)
        self.assertEqual(self.item()["revision"], 2)
        notify.notify(self.out, self.bridge, send=True)
        text = self.bridge.posts[-1][0]
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("[kimeru #1] （更新）田中さん: リリース日を決めてください"), lines[0])
        self.assertEqual(lines[2], "続き「資料を共有しました」")
        self.assertLessEqual(len(lines), 5)
        self.assertNotIn("⚠", text)

    def test_a_held_item_that_is_continued_says_held_before_and_the_detail_keeps_both_requests(self):
        self.ask_pm()
        notify.collect(self.out, FakeBridge(["保留 1"]), send=False)
        self.assertEqual(self.item()["status"], "held")
        self.process(self.ev(2, "資料を共有しました"), CONFIDENT_FYI)
        item = self.item()
        self.assertEqual(item["status"], "pending")
        self.assertTrue(item["record"]["held_before"])
        self.assertTrue(notify.format_post(1, item["record"]).splitlines()[0].endswith("（前回 保留）"))
        detail = notify.format_detail(1, item["record"], fulltext.load(self.out, item["key"]))
        self.assertIn("最初のメッセージ: リリース日を決めてください", detail)
        self.assertIn("続きのメッセージ: 資料を共有しました", detail)
        self.assertLess(detail.index("最初のメッセージ:"), detail.index("続きのメッセージ:"))

    def test_the_first_request_is_kept_at_120_characters_and_the_held_mark_is_dropped_after_ok_or_ng(self):
        long_first = "リリース日を決めてください。" + "詳しい事情は後で説明します。" * 20
        for answer, word in (("OK 1", "approved"), ("NG 1", "rejected")):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as d:
                self.out, self.bridge = Path(d), FakeBridge()
                self.ask_pm(long_first)
                notify.collect(self.out, FakeBridge(["保留 1"]), send=False)
                self.process(self.ev(2, "資料を共有しました"), CONFIDENT_FYI)
                first = self.item()["record"]["first_message"]["text"]
                self.assertLessEqual(len(first), 120)
                self.assertTrue(first.startswith("リリース日を決めてください"))
                self.assertTrue(self.item()["record"]["held_before"])
                notify.notify(self.out, self.bridge, send=True)
                notify.collect(self.out, FakeBridge([answer]), send=False)
                self.assertEqual(self.item()["status"], word)
                self.assertNotIn("held_before", self.item()["record"])


if __name__ == "__main__":
    unittest.main()
