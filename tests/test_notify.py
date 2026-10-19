try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import config, diagnose, execute, notify, writer
from tests.test_writer import PromptSpy


class FakeBridge:
    def __init__(self, replies=()):
        self.posts, self.replies = [], list(replies)

    def post(self, text, send):
        self.posts.append((text, send))
        return {"ok": True, "typed": True, "sent": bool(send),
                "readback": {"matched": bool(send), "message_id": f"fake-{len(self.posts)}" if send else ""}}

    def read(self):
        return {"ok": True, "posts": [], "replies": self.replies}


REC = {"graph": "workitem-intake", "event_kind": "ado.workitem.created", "event_id": "4812", "node": "triage_pm",
       "outcome": "advise", "needs_human": True, "advice": "優先度を判定できないチケット #4812",
       "actions": [{"type": "ado.update", "id": "4812", "fields": {"Microsoft.VSTS.Common.Priority": 2}}]}


def write_queue(d, *recs):
    (Path(d) / "queue.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")


class TestNotify(unittest.TestCase):
    def test_post_format_is_parseable_by_bridge(self):
        text = notify.format_post(3, REC)
        self.assertTrue(text.startswith("[kimeru #3]"))
        self.assertTrue(text.endswith("OK 3 / NG 3 / 詳細 3"))
        self.assertLessEqual(len(text.splitlines()), 5)
        for word in ("workitem-intake", "改訂", "ado.update", "判断メモなし"):
            self.assertNotIn(word, text)
        full = notify.format_detail(3, REC)               # the answer to `詳細 3`
        self.assertTrue(full.startswith("[kimeru 詳細 #3]"))
        self.assertIn("OK 3 / NG 3 / 保留 3", full)
        self.assertIn("ado.update", full)

    def test_paste_only_does_not_mark_posted(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC)
            b = FakeBridge()
            self.assertEqual(notify.notify(d, b, send=False), [1])
            self.assertEqual(notify.notify(d, b, send=False), [1])  # still unposted
            self.assertFalse(b.posts[0][1])

    def test_box_mismatch_is_not_marked_posted(self):
        class Mismatch(FakeBridge):
            def post(self, text, send):
                super().post(text, send)
                return {"ok": True, "typed": False, "sent": False}
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC)
            with self.assertRaises(RuntimeError):
                notify.notify(d, Mismatch(), send=True)
            self.assertEqual(notify.notify(d, FakeBridge(), send=True), [1])   # retried on the next cycle

    def test_collect_leaves_teams_alone_when_nothing_waits(self):
        class Counting(FakeBridge):
            reads = 0

            def read(self):
                Counting.reads += 1
                return super().read()
        with tempfile.TemporaryDirectory() as d:
            b = Counting(["OK 1"])
            self.assertEqual(notify.collect(d, b), [])            # no approvals.json at all
            write_queue(d, REC)
            notify.notify(d, b, send=False)                        # pasted only: not posted, nothing to wait for
            self.assertEqual(notify.collect(d, b), [])
            self.assertEqual(Counting.reads, 0)

    def test_send_posts_once_and_dedups_queue(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC, REC)
            b = FakeBridge()
            self.assertEqual(notify.notify(d, b, send=True), [1])
            self.assertEqual(notify.notify(d, b, send=True), [])

    def test_approve_executes_proposed_actions(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC)
            notify.notify(d, FakeBridge(), send=True)
            ch = notify.collect(d, FakeBridge(["OK 1"]))
            self.assertEqual(ch[0]["status"], "approved")
            self.assertEqual(ch[0]["executed"][0]["status"], "planned")
            self.assertEqual(notify.collect(d, FakeBridge(["OK 1", "NG 1"])), [])  # final

    def test_hold_then_reject_and_unknown_ids_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC)
            notify.notify(d, FakeBridge(), send=True)
            ch = notify.collect(d, FakeBridge(["保留 1", "OK 99", "hello"]))
            self.assertEqual([c["status"] for c in ch], ["held"])
            ch = notify.collect(d, FakeBridge(["保留 1", "NG 1"]))
            self.assertEqual([c["status"] for c in ch], ["rejected"])
            self.assertNotIn("executed", ch[0])


class TimelineBridge(FakeBridge):
    def __init__(self, timeline):
        super().__init__()
        self.timeline = timeline

    def read(self):
        return {"ok": True, "timeline": self.timeline}


class TestFreshReplies(unittest.TestCase):
    def test_stale_reply_before_new_post_is_ignored(self):
        tl = ["P:1", "R:OK 1", "P:1"]  # old run approved #1, then a new #1 was posted
        self.assertEqual(notify.fresh_replies({"timeline": tl}), [])
        self.assertEqual(notify.fresh_replies({"timeline": tl + ["R:NG 1"]}), ["NG 1"])

    def test_reply_without_visible_post_is_ignored(self):
        self.assertEqual(notify.fresh_replies({"timeline": ["R:OK 7"]}), [])

    def test_legacy_bridge_falls_back_to_replies(self):
        self.assertEqual(notify.fresh_replies({"replies": ["OK 1"]}), ["OK 1"])

    def test_collect_uses_timeline(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC)
            notify.notify(d, FakeBridge(), send=True)
            self.assertEqual(notify.collect(d, TimelineBridge(["P:1", "R:OK 1", "P:1"])), [])
            ch = notify.collect(d, TimelineBridge(["P:1", "R:OK 1", "P:1", "R:OK 1"]))
            self.assertEqual([c["status"] for c in ch], ["approved"])

    def test_unposted_item_ignores_replies(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC)
            notify.notify(d, FakeBridge(), send=False)  # pasted only
            self.assertEqual(notify.collect(d, FakeBridge(["OK 1"])), [])


class TestPartialSend(unittest.TestCase):
    def test_failure_on_second_post_keeps_first_marked_sent(self):
        rec2 = {**REC, "event_id": "4813"}

        class FailSecond(FakeBridge):
            def post(self, text, send):
                if len(self.posts) == 1:
                    raise notify.BridgeError("compose box not found", {"ok": False, "typed": False, "sent": False})
                return super().post(text, send)
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC, rec2)
            with self.assertRaises(RuntimeError):
                notify.notify(d, FailSecond(), send=True)
            b = FakeBridge()
            self.assertEqual(notify.notify(d, b, send=True), [2])  # #1 is not re-sent
            self.assertEqual(len(b.posts), 1)


class TestReplyNormalization(unittest.TestCase):
    def test_phone_variants(self):
        for s in ("OK 3", "ok 3", "Ok3", "ＯＫ　３", "ｏｋ３", "OK #3", " OK 3 "):
            self.assertEqual(notify.parse_reply(s), ("OK", "3"), s)
        self.assertEqual(notify.parse_reply("ＮＧ　１２"), ("NG", "12"))
        self.assertEqual(notify.parse_reply("保留　４"), ("保留", "4"))
        for s in ("OK", "OK 3 thanks", "返信: OK 3 / NG 3", "OKAY 3"):
            self.assertIsNone(notify.parse_reply(s), s)

    def test_collect_accepts_full_width_reply(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC)
            notify.notify(d, FakeBridge(), send=True)
            ch = notify.collect(d, TimelineBridge(["P:1", "R:ＯＫ　１"]))
            self.assertEqual([c["status"] for c in ch], ["approved"])


class TestAdviseNotExecuted(unittest.TestCase):
    def test_advise_actions_are_not_run_by_process(self):
        from kimeru import graph
        from kimeru.backends import ReplayBackend
        from kimeru.cli import process
        g = {"name": "t", "event": "teams.chat", "start": "a", "nodes": {
            "a": {"kind": "advise", "queue": True, "advice": "x", "actions": [{"type": "teams.reply", "text": "y"}]}}}
        graph.validate(g)
        with tempfile.TemporaryDirectory() as d:
            r = process({"kind": "teams.chat", "id": "1", "text": "hi"}, {"teams.chat": [g]}, ReplayBackend({}), Path(d))
            self.assertEqual(r[0]["executed"], [])
            self.assertTrue((Path(d) / "queue.jsonl").exists())


CHAT_REC = {"graph": "teams-chat-triage", "event_kind": "teams.chat", "event_id": "t1", "node": "reply",
            "revision": 1, "outcome": "decide", "needs_human": True, "advice": "スケジュール変更の判断",
            "event": {"kind": "teams.chat", "id": "t1", "author": "山田", "text": "仕様変更してもよいですか。来週の予定に響きます"},
            "memo": {"summary": "仕様変更の可否", "next": "影響範囲を確認して返事する", "missing": ["期限"]},
            "actions": [{"type": "teams.reply", "to": "山田", "text": "確認して今日中に返信します。", "drafted_by": "fake"}]}


class TestContinuedPostFormat(unittest.TestCase):
    def test_a_first_post_without_a_continuation_is_byte_identical_to_the_golden_text(self):
        self.assertEqual(notify.format_post(3, CHAT_REC),
                         "\n".join(["[kimeru #3] 山田さん: 仕様変更の可否", "→ 影響範囲を確認して返事する",
                                    "案: 確認して今日中に返信します。", "OK 3 / NG 3 / 修正 3 <点> / 詳細 3"]))
        self.assertEqual(notify.format_detail(3, CHAT_REC), "\n".join([
            "[kimeru 詳細 #3] 判断が必要（teams-chat-triage） / 改訂 1", "判断: スケジュール変更の判断",
            "次の一手: 影響範囲を確認して返事する", "不足情報: 期限",
            "OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）", "teams.chat #t1",
            "内容: スケジュール変更の判断", "承認の対象: teams.reply（既定は記録だけ。実行するのは設定で有効にした種類だけ）",
            "文面 LLM のメモ:", "・要点: 仕様変更の可否", "・足りない情報: 期限", "・次の一手: 影響範囲を確認して返事する",
            "返信の下書き（fake）:", "確認して今日中に返信します。", "返信: OK 3 / NG 3 / 保留 3 / 修正 3 <直してほしい点>"]))
        self.assertNotIn("前回 保留", notify.format_post(3, CHAT_REC))

    def test_the_first_request_on_line_one_is_cut_to_forty_characters(self):
        rec = {**CHAT_REC, "revision": 2, "first_message": {"text": "あ" * 200, "at": "2026-10-11T09:00:00+00:00"}}
        first = notify.format_post(3, rec).splitlines()[0]
        self.assertTrue(first.startswith("[kimeru #3] （更新）山田さん: "))
        self.assertLessEqual(len(first) - len("[kimeru #3] （更新）"), 40)

    def test_the_continuation_line_shows_thirty_characters_of_the_newest_message_after_the_arrow_line(self):
        rec = {**CHAT_REC, "revision": 2, "first_message": {"text": "最初の依頼", "at": "2026-10-11T09:00:00+00:00"},
               "event": {**CHAT_REC["event"], "text": "い" * 80}}
        lines = notify.format_post(3, rec).splitlines()
        self.assertTrue(lines[1].startswith("→ "))
        self.assertEqual(lines[2], "続き「" + "い" * 29 + "…」")
        self.assertLessEqual(len(lines), 5)

    def test_a_continued_held_item_says_held_before_at_the_end_of_line_one(self):
        rec = {**CHAT_REC, "revision": 2, "held_before": True,
               "first_message": {"text": "仕様変更の件", "at": "2026-10-11T09:00:00+00:00"}}
        self.assertEqual(notify.format_post(3, rec).splitlines()[0], "[kimeru #3] （更新）山田さん: 仕様変更の件 （前回 保留）")

    def test_the_detail_lists_the_first_message_before_the_continuation(self):
        rec = {**CHAT_REC, "revision": 2, "first_message": {"text": "最初の依頼です", "at": "2026-10-11T09:00:00+00:00"},
               "followups": [{"text": "続きです", "at": "2026-10-11T09:05:00+00:00"}]}
        text = notify.format_detail(3, rec)
        self.assertIn("最初のメッセージ: 最初の依頼です", text)
        self.assertIn("続きのメッセージ: 続きです", text)
        self.assertLess(text.index("最初のメッセージ:"), text.index("続きのメッセージ:"))


# R11: a chat the model was unsure of (intent), with a draft; the golden text is what HEAD printed before the setting existed
UNSURE_CHAT = {"graph": "teams-chat-triage", "event_kind": "teams.chat", "event_id": "t2", "node": "ask_pm",
               "outcome": "advise", "needs_human": True, "revision": 1, "advice": "意図が判定できないチャット",
               "event": {"kind": "teams.chat", "id": "t2", "author": "佐藤", "text": "この件、どうしましょう"},
               "memo": {"summary": "進め方の相談", "next": "状況を確認して返事する"},
               "path": [{"node": "intent", "answer": {"choice": "status", "confidence": 0.45,
                         "probabilities": {"decision": 0.2, "requirements": 0.02, "status": 0.62, "incident": 0.03,
                                           "fyi": 0.08, "none": 0.05}}, "edge": "unsure"}],
               "actions": [{"type": "teams.reply", "to": "佐藤", "text": "確認して返信します。", "drafted_by": "fake"}]}
GOLD_POST = "\n".join(["[kimeru #3] 佐藤さん: 進め方の相談", "→ 状況を確認して返事する", "案: 確認して返信します。",
                       "OK 3 / NG 3 / 修正 3 <点> / 詳細 3"])
GOLD_DETAIL = "\n".join([
    "[kimeru 詳細 #3] 判断が必要（teams-chat-triage） / 改訂 1", "判断: 意図が判定できないチャット",
    "次の一手: 状況を確認して返事する", "不足情報: メモに記載なし",
    "OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）", "teams.chat #t2",
    "内容: 意図が判定できないチャット", "承認の対象: teams.reply（既定は記録だけ。実行するのは設定で有効にした種類だけ）",
    "文面 LLM のメモ:", "・要点: 進め方の相談", "・次の一手: 状況を確認して返事する",
    "返信の下書き（fake）:", "確認して返信します。", "返信: OK 3 / NG 3 / 保留 3 / 修正 3 <直してほしい点>"])
LINE = "迷った点: 依頼の種類（近いのは 状況確認）"
POST_LINE = LINE   # no probability number on the post line (R11: the doubt is said in words)


class TestPostReasonLine(unittest.TestCase):
    """R11: post_reason=1 puts one line under the arrow line (and under the header of the detail) that says which question the
    model was unsure of. At 0, and by default, the post and the detail are the golden text, byte for byte."""

    def test_at_zero_the_post_and_the_detail_are_the_golden_text(self):
        with mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "0"}):
            self.assertEqual(notify.format_post(3, UNSURE_CHAT), GOLD_POST)
            self.assertEqual(notify.format_detail(3, UNSURE_CHAT), GOLD_DETAIL)

    def test_by_default_the_post_and_the_detail_are_the_golden_text(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("KIMERU_POST_REASON", None)
            self.assertEqual(notify.format_post(3, UNSURE_CHAT), GOLD_POST)
            self.assertEqual(notify.format_detail(3, UNSURE_CHAT), GOLD_DETAIL)

    def test_at_one_the_unsure_question_is_the_line_under_the_arrow_and_the_post_stays_within_five_lines(self):
        with mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "1"}):
            lines = notify.format_post(3, UNSURE_CHAT).splitlines()
        self.assertEqual(lines, ["[kimeru #3] 佐藤さん: 進め方の相談", "→ 状況を確認して返事する", POST_LINE,
                                 "案: 確認して返信します。", "OK 3 / NG 3 / 修正 3 <点> / 詳細 3"])
        self.assertLessEqual(len(lines), 5)

    def test_at_one_the_detail_has_the_line_under_its_header_and_nothing_else_changes(self):
        with mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "1"}):
            lines = notify.format_detail(3, UNSURE_CHAT).splitlines()
        gold = GOLD_DETAIL.splitlines()
        self.assertEqual(lines, [gold[0], LINE, *gold[1:]])

    def test_a_warning_line_is_kept_and_the_question_line_is_added_beside_it(self):
        rec = {**UNSURE_CHAT, "read_full": {"state": "preview_only"}}
        with mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "0"}):
            base = notify.format_post(3, rec).splitlines()
        with mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "1"}):
            lines = notify.format_post(3, rec).splitlines()
        self.assertIn("⚠ プレビューだけで判断しました（元のメッセージを Teams で確認）", lines)
        self.assertEqual(len(lines), len(base) + 1)
        self.assertEqual([l for l in lines if l != POST_LINE], base)

    def test_the_post_line_adds_the_missing_material_of_the_memo_and_the_detail_does_not(self):
        rec = {**UNSURE_CHAT, "memo": {"next": "確認する", "missing": ["期限", "担当者"]}}
        with mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "1"}):
            post = notify.format_post(3, rec).splitlines()
            detail = notify.format_detail(3, rec).splitlines()
        self.assertIn(POST_LINE + " / 足りない材料: 期限 / 担当者", post)
        self.assertIn(LINE, detail)
        self.assertNotIn(POST_LINE + " / 足りない材料: 期限 / 担当者", detail)

    def test_at_zero_the_missing_material_is_not_added(self):
        rec = {**UNSURE_CHAT, "memo": {"next": "確認する", "missing": ["期限"]}}
        with mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "0"}):
            self.assertNotIn("足りない材料", notify.format_post(3, rec))
            self.assertNotIn("最有力", notify.format_post(3, rec))

    def test_a_record_without_a_path_gets_no_line_and_no_error(self):
        rec = {k: v for k, v in UNSURE_CHAT.items() if k != "path"}
        with mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "1"}):
            self.assertEqual(notify.format_post(3, rec), GOLD_POST)
            self.assertEqual(notify.format_detail(3, rec), GOLD_DETAIL)

    def test_the_line_does_not_change_the_record_it_reads(self):
        rec = copy.deepcopy(UNSURE_CHAT)
        with mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "1"}):
            notify.format_post(3, rec)
            notify.format_detail(3, rec)
        self.assertEqual(rec, UNSURE_CHAT)

    def test_the_line_names_no_graph_node_option_or_action_in_english(self):
        with mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "1"}):
            line = [l for l in notify.format_post(3, UNSURE_CHAT).splitlines() if l.startswith("迷った点")][0]
        self.assertNotRegex(line, r"[A-Za-z0-9_%.]")


if __name__ == "__main__":
    unittest.main()

class TestAtomicState(unittest.TestCase):
    def test_a_damaged_approvals_file_recovers_from_the_backup(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC)
            notify.notify(d, FakeBridge(), send=True)
            ap = Path(d) / "approvals.json"
            notify.Approvals(d).save()                                   # second save leaves the first as .bak
            ap.write_text('{"next": 2, "items": {"1": {', encoding="utf-8")   # a crash in the middle of a write
            data = notify.Approvals(d).data
            self.assertEqual(data["next"], 2)
            self.assertIn("1", data["items"])
            ap.write_text("garbage", encoding="utf-8")
            (Path(d) / "approvals.json.bak").write_text("garbage", encoding="utf-8")
            self.assertEqual(notify.Approvals(d).data, {"next": 1, "items": {}})   # nothing readable: start clean, no crash

    def test_reply_with_trailing_full_stop_is_understood(self):
        self.assertEqual(notify.parse_reply("OK 3。"), ("OK", "3"))
        self.assertEqual(notify.parse_reply("ＯＫ　３！"), ("OK", "3"))
        self.assertIsNone(notify.parse_reply("OK 3 4"))


HELD = {"graph": "teams-chat-triage", "event_kind": "teams.chat", "event_id": "t1", "node": "reply",
        "outcome": "decide", "needs_human": True, "advice": "",
        "material_event": {"author": "Sato", "text": "リリースは来週火曜にずらせますか"},
        "copilot_request": "次の件について、私（PM）の名前で送る文面を書いてください。",
        "actions": [{"type": "teams.reply", "to": "Sato", "text": "受領しました。内容を確認して返信します。",
                     "held_for": "m365"}]}


class TestPasteBack(unittest.TestCase):
    """The manual Microsoft 365 route: the PM pastes Copilot's text back as one line "下書き N ..."."""

    def run_paste(self, line):
        import copy
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, copy.deepcopy(HELD))
            notify.notify(d, FakeBridge(), send=True)
            ch = notify.collect(d, TimelineBridge(["P:1", "R:" + line]))
            data = json.loads((Path(d) / "approvals.json").read_text(encoding="utf-8"))["items"]["1"]
            return ch, data

    def test_parse(self):
        self.assertEqual(notify.parse_paste("下書き 3 ご連絡ありがとうございます。"), ("3", "ご連絡ありがとうございます。"))
        self.assertEqual(notify.parse_paste("下書き＃3：了解です"), ("3", "了解です"))
        self.assertIsNone(notify.parse_paste("下書き 3"))
        self.assertIsNone(notify.parse_paste("OK 3"))

    def test_pasted_text_becomes_the_draft_and_is_reposted(self):
        ch, it = self.run_paste("下書き 1 ご連絡ありがとうございます。日程の影響を確認して、改めてご連絡します。[1]")
        self.assertEqual([c["status"] for c in ch], ["pasted"])
        a = it["record"]["actions"][0]
        self.assertEqual(a["text"], "ご連絡ありがとうございます。日程の影響を確認して、改めてご連絡します。")   # citation mark removed
        self.assertEqual(a["drafted_by"], "m365（貼り付け）")
        self.assertNotIn("held_for", a)
        self.assertNotIn("copilot_request", it["record"])
        self.assertFalse(it["posted"])            # posted again under the same number
        self.assertEqual(it["status"], "pending")

    def test_invented_date_in_pasted_text_is_flagged(self):
        _, it = self.run_paste("下書き 1 10/5 までに対応します。")
        self.assertIn("10/5", it["record"]["actions"][0]["unverified"])

    def test_unusable_text_is_refused_and_the_item_keeps_its_state(self):
        ch, it = self.run_paste("下書き 1 申し訳ありませんが、この内容では返信を作成できません。")
        self.assertEqual([c["status"] for c in ch], ["paste_failed"])
        a = it["record"]["actions"][0]
        self.assertEqual(a["text"], "受領しました。内容を確認して返信します。")
        self.assertIn("貼り付けた文面は使えません", it["record"]["redraft_note"])

    def test_post_tells_the_pm_how_to_paste_back(self):
        text = notify.format_post("1", HELD)
        self.assertIn("下書き 1", text)
        self.assertIn("1 行", text)

    def test_the_request_asks_for_one_paragraph(self):
        from kimeru import writer
        req = writer.human_request({"actions": [{"type": "teams.reply", "text": "x", "held_for": "m365"}]},
                                   {"author": "Sato", "text": "確認をお願いします"})
        self.assertIn("改行を入れず", req)


class TestManualRedraft(unittest.TestCase):
    """H6: with writer=m365 (manual) a 修正 N after a pasted draft asks for a new Copilot request; it never fails."""

    def test_revise_after_paste_posts_a_new_request_with_the_instruction_and_the_draft(self):
        pasted = "ご連絡ありがとうございます。日程の影響を確認して、改めてご連絡します。"
        w = writer.M365PromptWriter()
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, copy.deepcopy(HELD))
            b = FakeBridge()
            notify.notify(d, b, send=True)
            notify.collect(d, TimelineBridge(["P:1", "R:下書き 1 " + pasted]), writer=w)
            notify.notify(d, b, send=True)
            n = len(b.posts)
            ch = notify.collect(d, TimelineBridge(["P:1", "R:修正 1 短く"]), writer=w)
            self.assertEqual([c["status"] for c in ch], ["redrafted"])
            it = json.loads((Path(d) / "approvals.json").read_text(encoding="utf-8"))["items"]["1"]
            req = it["record"]["copilot_request"]
            self.assertIn("短く", req)
            self.assertIn(pasted, req)
            self.assertFalse(it["request_posted"])
            self.assertEqual(it["record"]["actions"][0]["held_for"], "m365")
            notify.notify(d, b, send=True)
            self.assertGreater(len(b.posts), n)
            self.assertTrue(any(pasted in t and "短く" in t for t, _ in b.posts[n:]))

    def test_the_manual_route_is_not_counted_against_redraft_max(self):
        # writer=m365 calls no writer, so a second 修正 N still goes through with redraft_max=1
        w = writer.M365PromptWriter()
        with mock.patch.dict(os.environ, {"KIMERU_REDRAFT_MAX": "1"}), tempfile.TemporaryDirectory() as d:
            write_queue(d, copy.deepcopy(HELD))
            b = FakeBridge()
            notify.notify(d, b, send=True)
            notify.collect(d, TimelineBridge(["P:1", "R:下書き 1 ご連絡ありがとうございます。確認します。"]), writer=w)
            notify.notify(d, b, send=True)
            for point in ("短く", "もっと丁寧に", "さらに短く"):
                ch = notify.collect(d, TimelineBridge(["P:1", f"R:修正 1 {point}"]), writer=w)
                self.assertEqual([c["status"] for c in ch], ["redrafted"], point)
                notify.notify(d, b, send=True)
            it = json.loads((Path(d) / "approvals.json").read_text(encoding="utf-8"))["items"]["1"]
            self.assertFalse(it.get("redraft_count"))


REDRAFT_REC = {**CHAT_REC, "material_event": CHAT_REC["event"],
               "actions": [{"type": "teams.reply", "to": "山田", "text": "確認して今日中に返信します。",
                            "template_text": "受領しました。", "drafted_by": "fake"}]}
GOLDEN_FIRST = (
    "[kimeru #1] 山田さん: 仕様変更の可否\n"
    "→ 影響範囲を確認して返事する\n"
    "案: 確認して今日中に返信します。\n"
    "OK 1 / NG 1 / 修正 1 <点> / 詳細 1")   # the first post, taken before R23
GOLDEN_AFTER = (
    "[kimeru #1] 山田さん: 仕様変更してもよいですか。来週の予定に響きます\n"
    "→ スケジュール変更の判断\n"
    "案: 延期の件、承知しました。\n"
    "OK 1 / NG 1 / 修正 1 <点> / 詳細 1")   # the post after 修正 1, taken before R23)   # the post after 修正 1, taken before R23


class TestRedraftMaterial(unittest.TestCase):
    """R23 (redraft_prev) on the 修正 N path of a posted draft: the writer's request carries the posted text as data only when the
    setting is 1. With 0 (the default) the request and both posts are byte-identical to the posts before R23."""

    def revise(self, env_value, answers):
        spy = PromptSpy(answers)
        with mock.patch.dict(os.environ, {"KIMERU_REDRAFT_PREV": env_value}), tempfile.TemporaryDirectory() as d:
            write_queue(d, REDRAFT_REC)
            b = FakeBridge()
            notify.notify(d, b, send=True)
            first = b.posts[-1][0]
            b.replies = ["修正 1 もっと短く"]
            changes = notify.collect(d, b, writer=spy)
            notify.notify(d, b, send=True)
            return spy, changes, first, b.posts[-1][0]

    def test_with_the_setting_off_the_request_and_both_posts_are_as_before(self):
        spy, changes, first, after = self.revise("0", [{"a1": "延期の件、承知しました。"}])
        self.assertEqual(changes, [{"id": 1, "status": "redrafted", "instruction": "もっと短く"}])
        self.assertEqual(spy.prompts, [writer.SYSTEM + "\n\n" + writer._material(copy.deepcopy(REDRAFT_REC), REDRAFT_REC["material_event"], "もっと短く")])
        self.assertEqual(first, GOLDEN_FIRST)
        self.assertEqual(after, GOLDEN_AFTER)

    def test_with_the_setting_on_the_writer_is_given_the_posted_text_as_data_and_the_check_material_does_not_have_it(self):
        spy, changes, _, _ = self.revise("1", [{"a1": "延期の件、承知しました。"}])
        self.assertEqual(changes[0]["status"], "redrafted")
        self.assertIn("前の下書き（PM の指示で直す。他は変えない）", spy.prompts[0])
        self.assertIn("確認して今日中に返信します。", spy.prompts[0])
        self.assertNotIn("確認して今日中に返信します。",
                         writer._material(copy.deepcopy(REDRAFT_REC), REDRAFT_REC["material_event"], "もっと短く"))


class TestRedraftHistory(unittest.TestCase):
    """T9 (c) redraft_history=1: a 修正 N gives the writer the earlier 修正 instructions of the same item (oldest first, 5 at most,
    300 characters each), kept in approvals.json until the item closes. 0 (the default): nothing is passed and nothing is kept."""

    def run_flow(self, setting, instructions, then=()):
        spy = PromptSpy([{"a1": "延期の件、承知しました。"}])
        with mock.patch.dict(os.environ, {"KIMERU_REDRAFT_HISTORY": setting}), tempfile.TemporaryDirectory() as d:
            write_queue(d, REDRAFT_REC)
            b = FakeBridge()
            notify.notify(d, b, send=True)
            for ins in instructions:
                b.replies = ["修正 1 " + ins]
                notify.collect(d, b, writer=spy)
                notify.notify(d, b, send=True)
            kept = []
            for line in then:
                b.replies = [line]
                notify.collect(d, b, writer=spy)
                notify.notify(d, b, send=True)
            files = {f.name: f.read_text(encoding="utf-8") for f in Path(d).glob("approvals.json*")}
            item = json.loads(files["approvals.json"])["items"]["1"]
            return spy, item, files

    def test_off_nothing_is_passed_or_kept(self):
        spy, item, files = self.run_flow("0", ["短く", "丁寧に"])
        self.assertNotIn("短く", spy.prompts[1].replace("PM からの修正指示: 丁寧に", ""))
        self.assertNotIn("redraft_history", item)
        self.assertEqual(spy.prompts[1], writer.SYSTEM + "\n\n" + writer._material(copy.deepcopy(REDRAFT_REC), REDRAFT_REC["material_event"], "丁寧に"))

    def test_on_the_second_request_has_the_first_instruction_and_it_is_kept_in_the_item(self):
        spy, item, _ = self.run_flow("1", ["短く", "丁寧に"])
        self.assertNotIn(writer.HISTORY_HEAD, spy.prompts[0])
        self.assertIn(writer.HISTORY_HEAD, spy.prompts[1])
        self.assertIn("短く", spy.prompts[1].split(writer.HISTORY_HEAD)[1])
        self.assertEqual(item["redraft_history"], ["短く", "丁寧に"])
        self.assertNotIn("短く", writer._material(copy.deepcopy(REDRAFT_REC), REDRAFT_REC["material_event"], "丁寧に"))

    def test_five_at_most_and_300_characters_each(self):
        spy, item, _ = self.run_flow("1", ["あ" * 400] + [f"指示{i}" for i in range(6)] + ["最後"])
        self.assertEqual(item["redraft_history"], [f"指示{i}" for i in range(2, 6)] + ["最後"])
        last = spy.prompts[-1].split(writer.HISTORY_HEAD)[1].split("出力形式")[0]
        self.assertEqual([x for x in ("指示0", "指示1", "指示5") if x in last], ["指示1", "指示5"])
        self.assertNotIn("あ" * 10, last)
        spy2, item2, _ = self.run_flow("1", ["あ" * 400, "次"])
        self.assertIn("あ" * 300, spy2.prompts[1])
        self.assertNotIn("あ" * 301, spy2.prompts[1])

    def test_a_closed_item_loses_the_instructions_and_no_backup_keeps_them(self):
        for closing in ("OK 1", "NG 1"):
            _, item, files = self.run_flow("1", ["秘密の指示ABC"], then=[closing])
            self.assertIn(item["status"], ("approved", "rejected"))
            self.assertNotIn("redraft_history", item)
            for name, text in files.items():
                self.assertNotIn("秘密の指示ABC", text, name)


class TestRedraftMax(unittest.TestCase):
    """T9 (d) redraft_max: past the limit a 修正 N does not call the writer and the post says 修正は N 回までです. 0 = no limit."""

    def run_flow(self, limit, count):
        spy = PromptSpy([{"a1": "延期の件、承知しました。"}])
        env = {"KIMERU_REDRAFT_MAX": limit}
        with mock.patch.dict(os.environ, env), tempfile.TemporaryDirectory() as d:
            write_queue(d, REDRAFT_REC)
            b = FakeBridge()
            notify.notify(d, b, send=True)
            statuses = []
            for i in range(count):
                b.replies = [f"修正 1 指示{i}"]
                statuses.append(notify.collect(d, b, writer=spy)[0]["status"])
                notify.notify(d, b, send=True)
            return spy, statuses, b.posts[-1][0]

    def test_default_has_no_limit(self):
        self.assertEqual(config.SETTINGS["redraft_max"], ("KIMERU_REDRAFT_MAX", "0"))
        self.assertIn("redraft_max", config.LOGGABLE)
        spy, statuses, _ = self.run_flow("0", 4)
        self.assertEqual(statuses, ["redrafted"] * 4)
        self.assertEqual(len(spy.prompts), 4)

    def test_past_the_limit_the_writer_is_not_called_and_the_post_says_so(self):
        spy, statuses, post = self.run_flow("2", 3)
        self.assertEqual(statuses, ["redrafted", "redrafted", "redraft_limit"])
        self.assertEqual(len(spy.prompts), 2)
        self.assertIn("修正は 2 回までです", post)


class TestPostChecked(unittest.TestCase):
    """T9 (b) post_checked=1: the post of a drafted text names, in at most two lines, what of the draft the event itself has
    (確かめた点) and what only kimeru's judgment or template has (kimeru の提案). 0 (the default) adds nothing."""
    EVENT = {"kind": "teams.chat", "author": "山田", "text": "#123 の仕様変更を 10/15 までにお願いします。https://example.com/x を見てください。"}

    def rec(self, text, template="受領しました。期限は 10/20 です。"):
        return {"graph": "g", "event_kind": "teams.chat", "event_id": "c1", "node": "n", "path": [], "advice": "期限は 10/20 に設定",
                "material_event": dict(self.EVENT), "event": dict(self.EVENT), "needs_human": True,
                "actions": [{"type": "teams.reply", "to": "山田", "text": text, "template_text": template, "drafted_by": "fake"}]}

    def post(self, value, rec):
        with mock.patch.dict(os.environ, {"KIMERU_POST_CHECKED": value}):
            return notify.format_post(1, rec)

    TEXT = "#123 を 10/15 までに対応します。https://example.com/x を確認し、10/20 に再連絡します。"

    def test_default_adds_nothing(self):
        self.assertNotIn("確かめた点", self.post("0", self.rec(self.TEXT)))
        self.assertNotIn("kimeru の提案", self.post("0", self.rec(self.TEXT)))

    def test_event_facts_and_kimeru_facts_are_on_separate_lines(self):
        base = self.post("0", self.rec(self.TEXT)).split("\n")
        got = self.post("1", self.rec(self.TEXT)).split("\n")
        self.assertEqual(len(got), len(base) + 2)
        checked = next(l for l in got if l.startswith("確かめた点: "))
        own = next(l for l in got if l.startswith("kimeru の提案: "))
        for t in ("#123", "10/15", "https://example.com/x"):
            self.assertIn(t, checked)
        self.assertNotIn("10/20", checked)
        self.assertIn("10/20", own)
        self.assertNotIn("10/15", own)

    def test_at_most_five_points_and_no_percentage_of_the_judgment(self):
        rec = self.rec("10/1、10/2、10/3、10/4、10/5、10/6、10/7 と 80%")
        rec["material_event"]["text"] = "10/1、10/2、10/3、10/4、10/5、10/6、10/7"
        rec["advice"] = "確度 80%"
        line = next(l for l in self.post("1", rec).split("\n") if l.startswith("確かめた点: "))
        self.assertEqual(line.count("10/"), 5)
        self.assertNotIn("80%", "".join(l for l in self.post("1", rec).split(chr(10)) if l.startswith("kimeru の提案")))
        self.assertNotIn("kimeru の提案", self.post("1", rec))

    def test_nothing_is_added_without_a_draft(self):
        rec = self.rec(self.TEXT)
        rec["actions"][0].pop("drafted_by")
        self.assertNotIn("確かめた点", self.post("1", rec))


class TestSendReadySplit(unittest.TestCase):
    """S1 (send_ready_split=1): an approved reply comes back to the self chat as two posts, the heading (to whom, and the chat link
    with open_chat_link=1) and the body alone under its own marker line. With the setting at 0 the posts keep their old bytes."""
    TEXT = "承知しました。確認して返信します。"
    CHAT = "19:fake-chat@thread.v2"
    LINK = "https://teams.microsoft.com/l/chat/19%3Afake-chat%40thread.v2/conversations"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"KIMERU_STATE_DIR": self.dir.name})
        self.env.start()
        self.set_split(None)

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def set_split(self, value, **extra):
        os.environ.pop("KIMERU_SEND_READY_SPLIT", None)
        if value is not None:
            os.environ["KIMERU_SEND_READY_SPLIT"] = value
        os.environ.update(extra)
        config.apply([])

    def rec(self, author="架空の相手ZQ", text=TEXT):
        return {"graph": "teams-chat", "event_kind": "teams.chat", "event_id": "c1", "node": "reply", "outcome": "advise",
                "needs_human": True, "advice": "返信を下書きしました",
                "event": {"kind": "teams.chat", "author": author, "chat_id": self.CHAT, "text": "確認できますか"},
                "actions": [{"type": "teams.reply", "chat_id": self.CHAT, "text": text}]}

    def test_the_setting_is_a_plain_flag_that_is_off_by_default(self):
        self.assertEqual(config.value("send_ready_split"), "0")
        self.assertEqual(config.SETTINGS["send_ready_split"], ("KIMERU_SEND_READY_SPLIT", "0"))
        self.assertEqual(config.CHOICES["send_ready_split"], ("0", "1"))
        self.assertIn("send_ready_split", config.SHARE_VALUES)
        self.assertIn("send_ready_split", config.LOGGABLE)
        self.set_split("1")
        self.assertEqual(config.value("send_ready_split"), "1")

    def test_default_posts_keep_the_golden_bytes_with_and_without_the_chat_link(self):
        posts = []
        execute.send_ready_posts(3, {"record": self.rec()}, posts.append)
        self.assertEqual(posts, ["[kimeru 送信用 #3]\n承知しました。確認して返信します。"])
        posts.clear()
        execute.send_ready_posts(3, {"record": self.rec()}, posts.append, link=True)
        self.assertEqual(posts, ["[kimeru 送信用 #3]\n承知しました。確認して返信します。\n\n"
                                 "相手とのチャットを開く（実験的）: " + self.LINK])

    def test_split_gives_the_heading_then_the_body_under_its_own_marker(self):
        posts = []
        execute.send_ready_posts(3, {"record": self.rec()}, posts.append, split=True)
        self.assertEqual(posts, ["[kimeru 送信用 #3] 架空の相手ZQへ。次の投稿をそのまま送ってください",
                                 "[kimeru 本文 #3]\n承知しました。確認して返信します。"])

    def test_split_names_the_counterpart_in_one_line_and_falls_back_to_a_plain_word(self):
        posts = []
        execute.send_ready_posts(3, {"record": self.rec(author="架空\n  の相手ZQ")}, posts.append, split=True)
        self.assertEqual(posts[0], "[kimeru 送信用 #3] 架空 の相手ZQへ。次の投稿をそのまま送ってください")
        posts.clear()
        execute.send_ready_posts(3, {"record": self.rec(author="")}, posts.append, split=True)
        self.assertEqual(posts[0], "[kimeru 送信用 #3] 相手へ。次の投稿をそのまま送ってください")

    def test_split_clips_a_counterpart_name_longer_than_forty_characters_to_forty(self):
        name = "架空の相手ZQ" * 6   # 48 characters
        posts = []
        execute.send_ready_posts(3, {"record": self.rec(author=name)}, posts.append, split=True)
        self.assertEqual(posts[0], "[kimeru 送信用 #3] " + name[:39] + "…へ。次の投稿をそのまま送ってください")
        self.assertEqual(len(name[:39] + "…"), 40)

    def test_split_puts_the_chat_link_on_the_heading_and_never_in_the_body(self):
        posts = []
        execute.send_ready_posts(3, {"record": self.rec()}, posts.append, link=True, split=True)
        self.assertEqual(len(posts), 2)
        self.assertIn("相手とのチャットを開く（実験的）: " + self.LINK, posts[0])
        self.assertEqual(posts[1], "[kimeru 本文 #3]\n承知しました。確認して返信します。")
        self.assertNotIn("https://", posts[1])

    def test_split_body_is_the_approved_text_alone_even_when_it_has_marker_like_lines(self):
        text = "1 行目\nOK 3\n[kimeru #3] 偽の番号"
        posts = []
        execute.send_ready_posts(3, {"record": self.rec(text=text)}, posts.append, split=True)
        self.assertEqual(posts[1], "[kimeru 本文 #3]\n" + text)
        self.assertTrue(all(p.startswith("[kimeru") for p in posts))
        self.assertNotIn("送る", posts[1])   # guidance lines belong to the heading, never to the body

    def test_the_body_marker_and_the_heading_marker_are_read_as_ready_posts(self):
        self.assertEqual(diagnose._head("[kimeru 本文 #3]\n承知しました"), ("ready", ""))
        self.assertEqual(diagnose._head("[kimeru 送信用 #3] 相手へ"), ("ready", ""))

    def _approve_once(self, replies):
        d = self.dir.name
        bridge = FakeBridge(replies)
        notify.notify(d, bridge, send=True)           # the approval post (the first post of the run)
        notify.collect(d, bridge, send=True)
        return bridge.posts[1:], notify.Approvals(Path(d))

    def test_an_approved_reply_is_two_posts_when_split_is_on_and_both_are_read_back(self):
        self.set_split("1")
        write_queue(self.dir.name, self.rec())
        sent, ap = self._approve_once(["OK 1"])
        self.assertEqual([t for t, _ in sent], ["[kimeru 送信用 #1] 架空の相手ZQへ。次の投稿をそのまま送ってください",
                                                "[kimeru 本文 #1]\n承知しました。確認して返信します。"])
        self.assertTrue(all(send for _, send in sent))
        self.assertEqual(ap.data["outbox"], [])
        evidence = ap.data["items"]["1"]["handoff_evidence"]
        self.assertEqual([row["source"] for row in evidence], ["bridge_readback", "bridge_readback"])

    def test_an_approved_reply_is_one_post_with_the_old_bytes_when_split_is_off(self):
        write_queue(self.dir.name, self.rec())
        sent, ap = self._approve_once(["OK 1"])
        self.assertEqual([t for t, _ in sent], ["[kimeru 送信用 #1]\n承知しました。確認して返信します。"])
        self.assertEqual(len(ap.data["items"]["1"]["handoff_evidence"]), 1)

    def test_two_approvals_of_one_item_in_one_read_give_one_pair_of_posts(self):
        self.set_split("1")
        write_queue(self.dir.name, self.rec())
        sent, _ = self._approve_once(["OK 1", "ＯＫ １"])
        self.assertEqual(len(sent), 2)
        self.assertEqual(sum(t.startswith("[kimeru 送信用 #1]") for t, _ in sent), 1)
        self.assertEqual(sum(t.startswith("[kimeru 本文 #1]") for t, _ in sent), 1)


