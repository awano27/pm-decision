"""S2: `送る N` sends the approved reply to its own 1 対 1 chat, once. An OK alone never sends, and with execute unset nothing is sent and
the texts are the golden texts. The self chat is the fake of test_execute.Bridge; `sendto` is a fake that records every call. Nothing
reaches Teams or the network."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from kimeru import cli, config, execute, notify
from tests.test_execute import WI
from tests.test_execute import Bridge as SelfBridge

PEER = "19:abc@unq.gbl.spaces"   # a made-up chat id (the fixtures never hold a real one)
REPLY_TEXT = "受領しました。本日中に判断して返信します。"
OK_AT = datetime(2026, 10, 15, 9, 0)   # the clock of a run that approves (the 48 hours of REPLY_WINDOW count from it)
ROOT = Path(__file__).resolve().parent.parent
PRE_ATTEMPT = {"ok": False, "error": "another kimeru Teams operation is running; Teams was not touched", "typed": False, "sent": False,
               "readback": {"matched": False, "message_id": ""}, "result": "failed"}   # the answer of tools/teams-self.ps1's Fail before any attempt

# the texts of an approval of a teams.reply with execute unset, captured at the base commit (the item number 7): they must not change
GOLDEN_POST = ('[kimeru #7] 山田さん: 明日の件、判断できますか\n→ 本日中に判断が必要: 山田「明日の件、判断できますか」\n'
               '案: 受領しました。本日中に判断して返信します。\nOK 7 / NG 7 / 詳細 7')
GOLDEN_DETAIL = ('[kimeru 詳細 #7] 判断が必要（teams.chat） / 改訂 1\n判断: 本日中に判断が必要: 山田「明日の件、判断できますか」\n'
                 '次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                 '不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                 'OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）\nteams.chat #c1#1\n'
                 '内容: 本日中に判断が必要: 山田「明日の件、判断できますか」\n'
                 '承認の対象: teams.reply（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n返信: OK 7 / NG 7 / 保留 7')
GOLDEN_READY = ['[kimeru 送信用 #7]\n受領しました。本日中に判断して返信します。',
                '[kimeru 送信用 #7]\n受領しました。本日中に判断して返信します。\n\n'
                '相手とのチャットを開く（実験的）: https://teams.microsoft.com/l/chat/19%3Aabc%40unq.gbl.spaces/conversations',
                '[kimeru 送信用 #7] 山田へ。次の投稿をそのまま送ってください',
                '[kimeru 本文 #7]\n受領しました。本日中に判断して返信します。']
SEND_LINE = "送る 1 で、この文面をそのまま 山田 とのチャットへ送ります（PC の Teams から 1 回）"


def golden_record():
    return {
        "graph": "teams.chat", "event_kind": "teams.chat", "event_id": "c1#1", "node": "decision_today",
        "event": {"kind": "teams.chat", "id": "c1#1", "chat_id": PEER, "chat_kind": "oneOnOne",
                  "author": "山田", "text": "明日の件、判断できますか", "mentions_me": False},
        "actions": [{"type": "teams.reply", "chat_id": PEER, "text": REPLY_TEXT}],
        "advice": "本日中に判断が必要: 山田「明日の件、判断できますか」", "notify": True, "outcome": "decide",
    }


class SendBridge(SelfBridge):
    """The self chat of test_execute.Bridge, plus `sendto`: every call is recorded. `result`, `error` and `restore` are the answer the
    script gives (`restore` says how the chat that was open was put back)."""

    def __init__(self, result="done", error="", restore="restored"):
        super().__init__()
        self.sent, self.result, self.error, self.restore = [], result, error, restore

    def sendto(self, chat_id, text):
        self.sent.append((chat_id, text))
        answer = {"ok": self.result == "done", "result": self.result, "restore": self.restore, "returned": True}
        if self.result == "done":
            return answer
        answer["error"] = self.error or self.result
        raise notify.BridgeError(answer["error"], answer)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name) / "out"
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = self.dir.name
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        config.apply([])
        # nothing in these tests may reach a network: any real HTTP call is a test failure
        self.net = mock.patch("urllib.request.urlopen", side_effect=AssertionError("a real HTTP call was attempted"))
        self.net.start()

    def tearDown(self):
        self.net.stop()
        self.env.stop()
        self.dir.cleanup()

    def enable(self, value="teams.reply"):
        os.environ["KIMERU_EXECUTE"] = value
        config.apply([])

    def add_reply(self, chat_kind="oneOnOne", chat_id=PEER, mention=False, action_chat=None, event_kind="teams.chat"):
        """A teams.chat decision with one reply, kept in the approvals file and not posted yet."""
        rec = golden_record()
        rec["event_kind"] = event_kind
        rec["event"].update({"chat_kind": chat_kind, "chat_id": chat_id, "mentions_me": mention})
        rec["actions"][0]["chat_id"] = action_chat if action_chat is not None else chat_id
        ap = notify.Approvals(self.out)
        n = ap.add("teams.chat:c1#1:decision_today", rec)
        ap.save()
        return n

    def post_it(self):
        """notify makes the approval post (where the text and the chat are fixed, as in a real cycle)."""
        bridge = SendBridge()
        notify.notify(self.out, bridge, send=True, real=True)
        return bridge

    def reply(self, bridge, line, send=True, real=True):
        bridge.replies.append(line)
        return notify.collect(self.out, bridge, real=real, send=send)

    def collect(self, bridge, send=True):
        return notify.collect(self.out, bridge, real=True, send=send)

    def reply_at(self, bridge, line, when):
        """A reply that is on the screen, read by a run whose clock is `when`."""
        bridge.replies.append(line)
        return notify.collect(self.out, bridge, real=True, send=True, now=when)

    def add_other_pending(self):
        """A second decision, posted and still waiting (an ADO item, no reply): the self chat is read for it whatever item 1 holds."""
        other = {"graph": "workitem-intake", "event_kind": "ado.workitem.created", "event_id": "4812", "node": "triage_pm",
                 "outcome": "advise", "needs_human": True, "advice": "優先度を判定できないチケット #4812",
                 "actions": [{"type": "ado.update", "id": "4812", "fields": {"Microsoft.VSTS.Common.Priority": 2}}]}
        ap = notify.Approvals(self.out)
        ap.add("ado.workitem:4812:triage", other)
        ap.save()

    def item(self):
        return json.loads((self.out / "approvals.json").read_text(encoding="utf-8"))["items"]["1"]

    def executions(self):
        p = self.out / "executions.jsonl"
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


class TestDefaultIsUnchanged(Base):
    def test_with_execute_unset_the_approval_post_detail_and_ready_texts_are_the_golden_texts(self):
        rec = golden_record()
        execute.freeze(rec, True)                      # execute unset: nothing is fixed
        self.assertEqual(notify.format_post(7, rec), GOLDEN_POST)
        self.assertEqual(notify.format_detail(7, rec), GOLDEN_DETAIL)
        posts = []
        item = {"record": rec}
        execute.send_ready_posts(7, item, posts.append)
        execute.send_ready_posts(7, item, posts.append, link=True)
        execute.send_ready_posts(7, item, posts.append, split=True)
        self.assertEqual(posts, GOLDEN_READY)

    def test_with_execute_unset_a_send_word_sends_nothing_and_writes_no_record_and_the_post_has_no_send_line(self):
        self.add_reply()
        bridge = self.post_it()
        self.reply(bridge, "OK 1")
        self.assertFalse(any(SEND_LINE in p for p in bridge.posts))            # no send line while switched off
        self.reply(bridge, "送る 1")
        self.reply(bridge, "送る 1")
        self.assertEqual(bridge.sent, [])
        self.assertFalse((self.out / "executions.jsonl").exists())             # the self chat is not read for it (nothing changes by default)

    def test_with_only_ado_comment_switched_on_a_send_word_sends_nothing(self):
        self.enable("ado.comment")
        self.add_reply()
        bridge = self.post_it()
        self.reply(bridge, "OK 1")
        self.reply(bridge, "送る 1")
        self.assertEqual(bridge.sent, [])
        self.assertFalse((self.out / "executions.jsonl").exists())

    def test_with_execute_unset_a_teams_chat_event_is_recorded_as_before_without_chat_kind(self):
        ev = {"kind": "teams.chat", "source": "teams-ui", "id": "c1#1", "chat_id": PEER, "chat_kind": "oneOnOne", "chat_title": "山田",
              "author": "山田", "ts": "10:42", "mentions_me": False, "unread": True, "text": "明日の件、判断できますか"}
        self.assertEqual(cli._recorded_event(ev), {"kind": "teams.chat", "id": "c1#1", "chat_id": PEER, "chat_title": "山田",
                                                   "author": "山田", "mentions_me": False, "text": "明日の件、判断できますか"})


class TestSendOnce(Base):
    def test_a_one_to_one_reply_is_sent_once_after_the_send_word_and_never_again(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        self.assertEqual(bridge.sent, [])                                        # the approval post sends nothing
        self.reply(bridge, "OK 1")
        self.assertEqual(bridge.sent, [])                                        # an OK alone sends nothing
        self.assertTrue(any(p == f"[kimeru 送信用 #1]\n{REPLY_TEXT}\n\n{SEND_LINE}" for p in bridge.posts))
        self.reply(bridge, "送る 1")
        self.assertEqual(bridge.sent, [(PEER, REPLY_TEXT)])
        self.reply(bridge, "送る 1")
        self.reply(bridge, "OK 1")
        self.collect(bridge)
        self.assertEqual(len(bridge.sent), 1)                                    # the same reply, read again: never a second send
        self.assertEqual([(r["type"], r["state"]) for r in self.executions()], [("teams.reply", "running"), ("teams.reply", "done")])
        self.assertIn("[kimeru 実行 #1 1] 送りました（山田 とのチャット）", bridge.posts)

    def test_the_text_and_the_chat_are_the_ones_fixed_when_the_approval_post_was_made(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        ap = notify.Approvals(self.out)
        act = ap.data["items"]["1"]["record"]["actions"][0]
        act["text"], act["chat_id"] = "後から直した文", "19:other@unq.gbl.spaces"
        ap.save()
        self.reply(bridge, "OK 1")
        self.reply(bridge, "送る 1")
        self.assertEqual(bridge.sent, [(PEER, REPLY_TEXT)])

    def test_a_reply_posted_before_the_switch_was_on_is_posted_again_and_its_ok_sends_nothing(self):
        self.add_reply()
        bridge = self.post_it()                        # execute unset: the post fixes no text and no chat
        self.enable()
        ch = self.reply(bridge, "OK 1")
        self.assertEqual(ch[0]["status"], "reposted")  # the PM sees the text and the chat before anything is sent
        self.assertEqual(bridge.sent, [])
        self.reply(bridge, "送る 1")                    # not approved yet: nothing is read as a send
        self.assertEqual(bridge.sent, [])
        notify.notify(self.out, bridge, send=True, real=True)   # posted again, with the text fixed
        self.reply(bridge, "OK 1")
        self.reply(bridge, "送る 1")
        self.assertEqual(bridge.sent, [(PEER, REPLY_TEXT)])

    def test_turning_the_switch_off_after_the_post_sends_nothing(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        self.enable("")
        self.reply(bridge, "OK 1")
        self.reply(bridge, "送る 1")
        self.assertEqual(bridge.sent, [])
        self.assertFalse((self.out / "executions.jsonl").exists())

    def test_a_failed_send_keeps_the_approval_and_is_sent_again_only_by_the_redo(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        bridge.result = "failed"
        self.reply(bridge, "OK 1")
        self.reply(bridge, "送る 1")
        self.assertEqual(len(bridge.sent), 1)
        self.assertEqual(self.item()["status"], "approved")                      # the approval stays
        self.assertEqual(self.item()["exec"]["0"]["state"], "failed")
        self.assertTrue(any(p.startswith("[kimeru 実行 #1 1] 送れませんでした") and "再実行 1" in p for p in bridge.posts))
        self.collect(bridge)
        self.assertEqual(len(bridge.sent), 1)                                    # not sent again by itself
        bridge.result = "done"
        self.reply(bridge, "再実行 1")
        self.assertEqual(len(bridge.sent), 2)                                    # the redo sends it once
        self.assertEqual(self.item()["exec"]["0"]["state"], "done")

    def test_an_unknown_send_waits_for_the_pm_and_is_closed_by_done_without_a_second_send(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        bridge.result = "unknown"
        self.reply(bridge, "OK 1")
        self.reply(bridge, "送る 1")
        self.assertEqual(len(bridge.sent), 1)
        self.assertTrue(any("送れたか分かりません" in p and "済 1" in p and "再実行 1" in p for p in bridge.posts))
        self.reply(bridge, "送る 1")
        self.reply(bridge, "済 1")
        self.assertEqual(len(bridge.sent), 1)
        self.assertEqual(self.item()["exec"]["0"]["state"], "closed")

    def test_an_unknown_send_is_sent_again_once_by_the_redo_word(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        bridge.result = "unknown"
        self.reply(bridge, "OK 1")
        self.reply(bridge, "送る 1")
        bridge.result = "done"
        self.reply(bridge, "再実行 1")
        self.assertEqual(len(bridge.sent), 2)
        self.assertEqual(self.item()["exec"]["0"]["state"], "done")
        self.reply(bridge, "再実行 1")
        self.assertEqual(len(bridge.sent), 2)

    def test_a_failed_send_is_sent_again_once_by_the_count_redo_word(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        bridge.result = "failed"
        self.reply(bridge, "OK 1")
        self.reply(bridge, "送る 1")
        self.assertEqual(len(bridge.sent), 1)
        bridge.result = "done"
        self.reply(bridge, "再実行 1-1")                   # the count of the failed result post
        self.assertEqual(len(bridge.sent), 2)
        self.assertEqual(self.item()["exec"]["0"]["state"], "done")
        self.reply(bridge, "再実行 1-1")                   # the same reply, on the screen again: nothing
        self.assertEqual(len(bridge.sent), 2)

    def test_an_unknown_send_is_sent_again_once_by_the_count_redo_word(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        bridge.result = "unknown"
        self.reply(bridge, "OK 1")
        self.reply(bridge, "送る 1")
        self.assertEqual(len(bridge.sent), 1)
        bridge.result = "done"
        self.reply(bridge, "再実行 1-1")                   # the count of the unknown result post (its hint names this word)
        self.assertEqual(len(bridge.sent), 2)
        self.assertEqual(self.item()["exec"]["0"]["state"], "done")
        self.reply(bridge, "再実行 1-1")
        self.assertEqual(len(bridge.sent), 2)

    def test_with_another_item_pending_a_second_send_word_never_sends_again(self):
        """The chat is read on every run (another item waits), so each `送る 1` is seen: what stops the second one is the guard of
        reply_request, not a chat that is left unread."""
        self.enable()
        self.add_reply()
        self.add_other_pending()
        bridge = self.post_it()
        self.reply(bridge, "OK 1")
        self.reply(bridge, "送る 1")
        self.reply(bridge, "送る 1")
        self.collect(bridge)
        self.assertEqual(bridge.sent, [(PEER, REPLY_TEXT)])
        self.assertEqual([(r["type"], r["state"]) for r in self.executions()], [("teams.reply", "running"), ("teams.reply", "done")])
        self.assertIn("[kimeru 実行 #1 1] 送りました（山田 とのチャット）", bridge.posts)
        self.assertEqual(self.item()["exec"]["0"]["state"], "done")

    def test_a_send_that_stops_inside_the_call_stays_running_and_no_later_run_sends_it_again(self):
        self.enable()
        self.add_reply()
        self.add_other_pending()
        bridge = self.post_it()
        self.reply(bridge, "OK 1")
        with mock.patch.object(bridge, "sendto", side_effect=KeyboardInterrupt("simulated stop inside the send")) as send:
            with self.assertRaises(KeyboardInterrupt):
                self.reply(bridge, "送る 1")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.item()["exec"]["0"]["state"], "running")                 # saved before the call
        self.assertEqual([(r["type"], r["state"]) for r in self.executions()], [("teams.reply", "running")])
        with mock.patch.object(bridge, "sendto", wraps=bridge.sendto) as again:
            self.collect(bridge)                                                       # the next run: the same word is still on the screen
            self.reply(bridge, "送る 1")
        self.assertEqual(again.call_count, 0)
        self.assertEqual(len(self.executions()), 1)                                    # no second row: nothing was sent again
        self.assertTrue(any("送れたか分かりません" in p for p in bridge.posts))         # the PM is told the result is not known


class TestOnlyOneToOne(Base):
    def assert_not_sent(self, **kw):
        self.enable()
        self.add_reply(**kw)
        bridge = self.post_it()
        self.reply(bridge, "OK 1")
        self.assertTrue(any("送れません（グループ／相手が特定できない）" in p for p in bridge.posts))
        self.assertFalse(any(SEND_LINE in p for p in bridge.posts))
        self.reply(bridge, "送る 1")
        self.assertEqual(bridge.sent, [])
        self.assertEqual([(r["state"], r["reason"]) for r in self.executions()], [("skipped", "not_target")])

    def test_a_group_chat_is_not_a_target_and_the_post_says_so(self):
        self.assert_not_sent(chat_kind="group")

    def test_a_message_that_is_not_a_teams_chat_event_is_not_a_target(self):
        self.assert_not_sent(event_kind="ado.workitem.created")

    def test_a_meeting_chat_is_not_a_target(self):
        self.assert_not_sent(chat_kind="meeting")

    def test_a_message_that_mentions_me_is_not_a_target(self):
        self.assert_not_sent(mention=True)

    def test_a_reply_that_names_another_chat_is_not_a_target(self):
        self.assert_not_sent(action_chat="19:other@unq.gbl.spaces")

    def test_a_reply_without_a_chat_id_is_not_a_target(self):
        self.assert_not_sent(chat_id="")

    def test_with_teams_reply_switched_on_the_teams_event_record_keeps_the_chat_kind_the_check_reads(self):
        self.enable()
        kept = cli._recorded_event({"kind": "teams.chat", "id": "c1#1", "chat_id": PEER, "chat_kind": "oneOnOne", "author": "山田", "text": "a"})
        self.assertEqual(kept["chat_kind"], "oneOnOne")


class TestOnlyTheRealRun(Base):
    def test_without_the_real_run_a_send_word_sends_nothing_and_records_nothing(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        self.reply(bridge, "OK 1", real=False)
        self.reply(bridge, "送る 1", real=False)
        self.assertEqual(bridge.sent, [])
        self.assertFalse((self.out / "executions.jsonl").exists())

    def test_without_send_the_send_word_is_recorded_and_the_next_send_run_sends_it_once(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        self.reply(bridge, "OK 1", send=False)
        self.reply(bridge, "送る 1", send=False)
        self.assertEqual(bridge.sent, [])
        self.assertEqual(self.item()["exec"]["0"]["state"], "requested")
        self.assertFalse((self.out / "executions.jsonl").exists())               # nothing was sent, so nothing is recorded yet
        self.collect(bridge)                                                     # the next run with --send
        self.assertEqual(bridge.sent, [(PEER, REPLY_TEXT)])
        self.collect(bridge)
        self.assertEqual(len(bridge.sent), 1)

    def test_a_send_word_read_without_the_real_flag_sends_nothing_and_leaves_the_state_alone(self):
        self.enable()
        self.add_reply()
        self.add_other_pending()                 # the self chat is read for it, so the word is seen by the runs without the real flag
        bridge = self.post_it()
        self.reply(bridge, "OK 1")
        with mock.patch.object(bridge, "sendto", wraps=bridge.sendto) as send:
            self.reply(bridge, "送る 1", real=False)
            notify.collect(self.out, bridge, real=False, send=False)
            notify.collect(self.out, bridge, real=False, send=False)
        self.assertEqual(send.call_count, 0)
        self.assertEqual(bridge.sent, [])
        self.assertEqual(self.item().get("exec", {}), {})
        self.assertFalse((self.out / "executions.jsonl").exists())
        self.assertTrue(all(p.startswith("[kimeru") for p in bridge.posts))

    def test_a_run_without_the_real_flag_does_not_read_the_self_chat_for_an_open_reply(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        self.reply(bridge, "OK 1")                          # approved: the reply waits for 送る 1, which only a real run reads
        with mock.patch.object(bridge, "read", wraps=bridge.read) as read:
            notify.collect(self.out, bridge, real=False, send=True)
        self.assertEqual(read.call_count, 0)
        self.assertEqual(bridge.sent, [])
        self.assertFalse((self.out / "executions.jsonl").exists())

    def test_an_ok_on_a_run_without_the_real_flag_does_not_open_the_reply_window(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        self.reply(bridge, "OK 1", real=False)
        self.assertEqual(self.item()["status"], "approved")
        self.assertNotIn("reply_open_at", self.item())
        self.assertEqual(bridge.sent, [])
        self.assertFalse((self.out / "executions.jsonl").exists())

    def test_without_the_real_flag_a_freeze_fixes_no_reply_and_the_ready_post_has_no_send_line(self):
        self.enable()
        rec = golden_record()
        execute.freeze(rec, False)
        self.assertFalse(any(k in rec["actions"][0] for k in execute.REPLY_KEYS))
        posts = []
        execute.send_ready_posts(7, {"record": rec}, posts.append)
        self.assertEqual(posts, [GOLDEN_READY[0]])

    def test_a_request_recorded_without_send_is_not_sent_by_later_runs_without_send(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        self.reply(bridge, "OK 1", send=False)
        self.reply(bridge, "送る 1", send=False)
        self.assertEqual(self.item()["exec"]["0"]["state"], "requested")
        with mock.patch.object(bridge, "sendto", wraps=bridge.sendto) as send:
            self.collect(bridge, send=False)
            self.collect(bridge, send=False)
        self.assertEqual(send.call_count, 0)
        self.assertEqual(self.item()["exec"]["0"]["state"], "requested")
        self.assertTrue(all(p.startswith("[kimeru") for p in bridge.posts))
        self.collect(bridge)                                    # the run with --send sends it, once
        self.assertEqual(bridge.sent, [(PEER, REPLY_TEXT)])

    def test_a_send_word_under_a_test_post_is_not_sent_by_the_real_run_even_when_the_chat_is_read(self):
        self.enable()
        self.add_reply()
        self.add_other_pending()
        bridge = self.post_it()
        self.reply(bridge, "OK 1")
        real_read = bridge.read

        def read_with_a_test_post():   # the test reader's post "[kimeru 試験 #1]" (tools/check.ps1), and a 送る 1 under it
            got = real_read()
            got["timeline"] = got["timeline"] + ["T:1", "R:送る 1"]
            return got
        bridge.read = read_with_a_test_post
        self.collect(bridge)
        self.assertEqual(bridge.sent, [])
        self.assertFalse((self.out / "executions.jsonl").exists())

    def test_the_run_command_never_reaches_the_send(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        self.reply(bridge, "OK 1")
        f = self.out / "w.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(WI), encoding="utf-8")
        with mock.patch.object(notify.PowerShellBridge, "sendto", side_effect=AssertionError("sendto was called")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--out", str(self.out / "o"), "run", str(f)]), 0)
        self.assertFalse((self.out / "o" / "executions.jsonl").exists())

    def test_a_send_word_above_the_approval_post_is_not_read(self):
        self.assertEqual(notify.fresh_entries({"timeline": ["R:送る 1", "P:1"]}), [])

    def test_a_send_word_under_a_test_post_is_not_read_by_the_real_run(self):
        self.assertEqual(notify.fresh_entries({"timeline": ["P:1", "R:送る 1"]}), [("送る 1", 0)])
        self.assertEqual(notify.fresh_entries({"timeline": ["T:1", "R:送る 1"]}), [])


class TestSendLine(Base):
    def test_the_ready_post_ends_with_the_send_line_when_the_reply_is_switched_on(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        self.reply(bridge, "OK 1")
        self.assertIn(f"[kimeru 送信用 #1]\n{REPLY_TEXT}\n\n{SEND_LINE}", bridge.posts)

    def test_with_the_split_setting_the_line_is_on_the_heading_and_the_body_stays_alone(self):
        self.enable()
        os.environ["KIMERU_SEND_READY_SPLIT"] = "1"
        config.apply([])
        self.add_reply()
        bridge = self.post_it()
        self.reply(bridge, "OK 1")
        self.assertIn(f"[kimeru 送信用 #1] 山田へ。次の投稿をそのまま送ってください\n\n{SEND_LINE}", bridge.posts)
        self.assertIn(f"[kimeru 本文 #1]\n{REPLY_TEXT}", bridge.posts)


class TestBridgeSendTo(unittest.TestCase):
    def test_the_text_goes_to_the_script_in_a_file_and_the_file_is_removed_after_the_call(self):
        seen = {}

        def fake_run(args, **kw):
            path = args[args.index("-TextFile") + 1]
            seen.update(args=list(args), path=path, text=Path(path).read_text(encoding="utf-8"))
            return subprocess.CompletedProcess(args, 0, stdout='{"ok": true, "result": "done"}', stderr="")

        with mock.patch.object(notify.subprocess, "run", side_effect=fake_run):
            got = notify.PowerShellBridge(script="teams-self.ps1").sendto(PEER, "一行目\n二行目")
        self.assertEqual(got["result"], "done")
        self.assertEqual(seen["text"], "一行目\n二行目")
        self.assertNotIn("一行目", " ".join(seen["args"]))                       # the text never reaches the command line
        self.assertIn(PEER, seen["args"])
        self.assertFalse(Path(seen["path"]).exists())

    def test_a_script_answer_that_is_not_done_raises_with_its_result(self):
        answer = subprocess.CompletedProcess([], 2, stdout='{"ok": false, "result": "failed", "error": "wrong chat"}', stderr="")
        with mock.patch.object(notify.subprocess, "run", return_value=answer):
            with self.assertRaises(notify.BridgeError) as cm:
                notify.PowerShellBridge(script="teams-self.ps1").sendto(PEER, "本文")
        self.assertEqual(cm.exception.data["result"], "failed")


class TestAnswerWithTheSwitchOff(Base):
    """With execute unset the self chat is not read for `送る N` by default (the default reads nothing new). When another reason reads
    it, the `送る N` is answered once, with one record, and nothing is sent."""

    def test_a_send_word_read_for_another_reason_gets_one_notice_and_one_record_and_no_send(self):
        self.add_reply()
        self.add_other_pending()
        bridge = self.post_it()
        self.reply(bridge, "OK 1")
        self.reply(bridge, "送る 1")
        self.assertEqual(bridge.sent, [])
        self.assertEqual([(r["state"], r["reason"]) for r in self.executions()], [("skipped", "not_enabled")])
        self.assertEqual(sum("記録だけです" in p for p in bridge.posts), 1)
        self.collect(bridge)                               # the same line, read again: no second notice and no second record
        self.collect(bridge)
        self.assertEqual(len(self.executions()), 1)
        self.assertEqual(sum("記録だけです" in p for p in bridge.posts), 1)
        self.assertEqual(bridge.sent, [])

    def test_a_send_word_sent_after_the_switch_is_turned_on_says_the_text_was_not_fixed_and_sends_nothing(self):
        self.add_reply()
        self.add_other_pending()
        bridge = self.post_it()
        self.reply(bridge, "OK 1")
        self.reply(bridge, "送る 1")
        self.enable()
        self.reply(bridge, "送る 1")                        # a new line: the approval was made while switched off, so no text was fixed
        self.assertEqual(bridge.sent, [])
        self.assertEqual([(r["state"], r["reason"]) for r in self.executions()],
                         [("skipped", "not_enabled"), ("skipped", "not_frozen")])
        self.assertEqual(sum("返信を有効にする前に出されたため" in p for p in bridge.posts), 1)
        self.assertEqual(sum("送信用の投稿の文面を、手で送ってください" in p for p in bridge.posts), 1)


class TestReplyWindow(Base):
    """`送る N` is read for REPLY_WINDOW (48 hours) after the OK, and only then (with execute teams.reply). After it the self chat is not
    read for the item at all, so nothing answers a later `送る N`; and when another reason reads the self chat, a `送る N` after the
    window is still ignored (nothing sent, nothing recorded, nothing told). While the window is open, every run reads the self chat."""

    def test_a_send_word_at_forty_seven_hours_after_the_ok_is_sent_once(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        self.reply_at(bridge, "OK 1", OK_AT)
        self.reply_at(bridge, "送る 1", OK_AT + timedelta(hours=47))
        self.assertEqual(bridge.sent, [(PEER, REPLY_TEXT)])

    def test_a_send_word_at_forty_nine_hours_after_the_ok_is_not_read_and_sends_nothing(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        self.reply_at(bridge, "OK 1", OK_AT)
        bridge.replies.append("送る 1")                     # on the screen, after the window
        with mock.patch.object(bridge, "read", wraps=bridge.read) as read:
            notify.collect(self.out, bridge, real=True, send=True, now=OK_AT + timedelta(hours=49))
        self.assertEqual(read.call_count, 0)
        self.assertEqual(bridge.sent, [])
        self.assertFalse((self.out / "executions.jsonl").exists())

    def test_while_the_window_is_open_each_run_reads_the_self_chat(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        self.reply_at(bridge, "OK 1", OK_AT)
        with mock.patch.object(bridge, "read", wraps=bridge.read) as read:
            notify.collect(self.out, bridge, real=True, send=True, now=OK_AT + timedelta(hours=1))
        self.assertEqual(read.call_count, 1)               # the extra read that docs/execute-teams-reply.md names

    def test_the_window_does_not_open_with_the_switch_off(self):
        self.add_reply()
        bridge = self.post_it()
        self.reply_at(bridge, "OK 1", OK_AT)
        self.assertNotIn("reply_open_at", self.item())
        self.assertFalse(execute.reply_open(self.item(), OK_AT + timedelta(hours=1)))

    def test_a_non_target_is_recorded_inside_the_window_and_never_sent(self):
        self.enable()
        self.add_reply(chat_kind="group")
        bridge = self.post_it()
        self.reply_at(bridge, "OK 1", OK_AT)
        self.reply_at(bridge, "送る 1", OK_AT + timedelta(hours=1))
        self.assertEqual(bridge.sent, [])
        self.assertEqual([(r["state"], r["reason"]) for r in self.executions()], [("skipped", "not_target")])

    def test_a_send_word_at_forty_seven_hours_is_sent_when_another_item_makes_the_run_read_the_self_chat(self):
        self.enable()
        self.add_reply()
        self.add_other_pending()
        bridge = self.post_it()
        self.reply_at(bridge, "OK 1", OK_AT)
        self.reply_at(bridge, "送る 1", OK_AT + timedelta(hours=47))
        self.assertEqual(bridge.sent, [(PEER, REPLY_TEXT)])

    def test_a_send_word_at_forty_nine_hours_is_ignored_when_another_item_makes_the_run_read_the_self_chat(self):
        """The window is kept even when the self chat is read for another reason: the word is not sent, not recorded, and nothing
        is told (the docs say a 送る N after 48 hours gets no answer)."""
        self.enable()
        self.add_reply()
        self.add_other_pending()
        bridge = self.post_it()
        self.reply_at(bridge, "OK 1", OK_AT)
        n_posts = len(bridge.posts)
        with mock.patch.object(bridge, "sendto", wraps=bridge.sendto) as send:
            self.reply_at(bridge, "送る 1", OK_AT + timedelta(hours=49))
        self.assertEqual(send.call_count, 0)
        self.assertEqual(bridge.sent, [])
        self.assertEqual(self.item().get("exec", {}), {})
        self.assertFalse((self.out / "executions.jsonl").exists())
        self.assertEqual(len(bridge.posts), n_posts)

    def test_the_window_closes_after_forty_eight_hours_and_never_for_an_item_without_one(self):
        item = {"status": "approved", "reply_open_at": OK_AT.isoformat()}
        self.assertFalse(execute.reply_window_closed(item, OK_AT + timedelta(hours=48)))
        self.assertTrue(execute.reply_window_closed(item, OK_AT + timedelta(hours=48, seconds=1)))
        self.assertFalse(execute.reply_window_closed({"status": "approved"}, OK_AT + timedelta(days=30)))


class TestFailuresAndWhatIsShown(Base):
    def test_a_draft_in_the_box_is_a_failure_that_is_not_sent_again_by_a_send_word(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        bridge.result, bridge.error = "failed", "the input box of that chat holds a draft; nothing pasted"
        self.reply(bridge, "OK 1")
        self.reply(bridge, "送る 1")
        self.assertTrue(any("送れませんでした（相手の入力欄に下書きがあるため、何も貼っていません）" in p for p in bridge.posts))
        self.reply(bridge, "送る 1")                        # a send word after a failure sends nothing: only 再実行 does
        self.assertEqual(len(bridge.sent), 1)
        self.assertEqual(self.item()["exec"]["0"]["state"], "failed")

    def _restore_case(self, result, error):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        bridge.result, bridge.error, bridge.restore = result, error, "failed"
        self.reply(bridge, "OK 1")
        self.reply(bridge, "送る 1")
        return bridge

    def test_a_chat_that_could_not_be_put_back_is_said_after_a_failed_send(self):
        bridge = self._restore_case("failed", "wrong chat")
        self.assertTrue(any("送れませんでした" in p and "開いていたチャットへ戻せませんでした" in p for p in bridge.posts))

    def test_a_chat_that_could_not_be_put_back_is_said_after_an_unknown_send(self):
        bridge = self._restore_case("unknown", "not seen in the chat list")
        self.assertTrue(any("送れたか分かりません" in p and "開いていたチャットへ戻せませんでした" in p for p in bridge.posts))

    def test_a_chat_that_could_not_be_put_back_is_said_after_a_done_send_too(self):
        bridge = self._restore_case("done", "")
        self.assertTrue(any("送りました" in p and "開いていたチャットへ戻せませんでした" in p for p in bridge.posts))

    def test_the_ready_post_shows_the_text_that_is_sent_even_after_the_text_was_edited(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        ap = notify.Approvals(self.out)
        ap.data["items"]["1"]["record"]["actions"][0]["text"] = "後から直した文"
        ap.save()
        self.reply(bridge, "OK 1")
        self.assertTrue(any(p.startswith(f"[kimeru 送信用 #1]\n{REPLY_TEXT}\n\n") for p in bridge.posts))
        self.assertFalse(any("後から直した文" in p for p in bridge.posts))
        self.reply(bridge, "送る 1")
        self.assertEqual(bridge.sent, [(PEER, REPLY_TEXT)])


class TestReplyWord(unittest.TestCase):
    def test_send_is_a_reply_word_with_or_without_the_hash_and_full_width_digits(self):
        self.assertEqual(notify.parse_reply("送る 12"), ("送る", "12"))
        self.assertEqual(notify.parse_reply("送る#3。"), ("送る", "3"))
        self.assertEqual(notify.parse_reply("送る　２"), ("送る", "2"))


class ScriptAnswerBridge(SendBridge):
    """The real PowerShellBridge.sendto, with the script's answer given as JSON (exit code 0 when done, 2 otherwise)."""

    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def sendto(self, chat_id, text):
        self.sent.append((chat_id, text))
        run = subprocess.CompletedProcess([], 0 if self.answer.get("ok") else 2, stdout=json.dumps(self.answer), stderr="")
        with mock.patch.object(notify.subprocess, "run", return_value=run):
            return notify.PowerShellBridge(script="teams-self.ps1").sendto(chat_id, text)


class TestRedoIsNeverTheFirstSend(Base):
    """`再実行 N` and `再実行 N-k` send a reply only when its earlier send failed or is unknown. A reply that was never requested with
    `送る N` is not sent by them, and nothing is recorded for it."""

    def test_a_redo_word_on_a_reply_that_was_never_requested_sends_nothing_and_records_nothing(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        self.reply(bridge, "OK 1")                        # approved: the reply waits for 送る 1
        self.reply(bridge, "再実行 1")
        self.reply(bridge, "再実行 1-1")
        self.assertEqual(bridge.sent, [])
        self.assertFalse((self.out / "executions.jsonl").exists())
        self.assertEqual(self.item().get("exec", {}), {})

    def test_the_redo_call_on_a_frozen_reply_with_no_state_sends_nothing_and_records_nothing(self):
        self.enable()
        self.add_reply()
        bridge = self.post_it()
        self.reply(bridge, "OK 1")
        ap = notify.Approvals(self.out)
        posts = []
        res = execute.redo(self.out, ap, 1, ap.data["items"]["1"], posts.append, bridge=bridge, send=True)
        self.assertEqual(res, [])
        self.assertEqual(bridge.sent, [])
        self.assertEqual(posts, [])
        self.assertFalse((self.out / "executions.jsonl").exists())
        self.assertEqual(ap.data["items"]["1"].get("exec", {}), {})


class TestAFailureBeforeAnAttemptIsFailed(Base):
    """A failure before anything is pasted or sent (the UI lock timed out, no Teams window) is answered with result failed. The reply is
    failed (the approval stays, `再実行` sends it once) and is never unknown. The answer is the one of tools/teams-self.ps1's Fail."""

    def test_the_script_answer_before_any_attempt_maps_to_failed_and_never_unknown(self):
        outcome, error, note = execute._sent_outcome(ScriptAnswerBridge(PRE_ATTEMPT), PEER, REPLY_TEXT)
        self.assertEqual((outcome, note), ("failed", ""))
        self.assertIn("Teams was not touched", error)

    def test_a_reply_that_fails_before_any_attempt_is_failed_and_the_redo_word_sends_it_once(self):
        self.enable()
        self.add_reply()
        bridge = ScriptAnswerBridge(PRE_ATTEMPT)
        notify.notify(self.out, bridge, send=True, real=True)
        self.reply(bridge, "OK 1")
        self.reply(bridge, "送る 1")
        self.assertEqual(self.item()["exec"]["0"]["state"], "failed")
        self.assertTrue(any(p.startswith("[kimeru 実行 #1 1] 送れませんでした") for p in bridge.posts))
        self.assertFalse(any("送れたか分かりません" in p for p in bridge.posts))
        bridge.answer = {"ok": True, "result": "done", "restore": "unchanged", "returned": True}
        self.reply(bridge, "再実行 1")
        self.assertEqual(len(bridge.sent), 2)
        self.assertEqual(self.item()["exec"]["0"]["state"], "done")


class TestTheScriptsAnswerBeforeAnAttempt(unittest.TestCase):
    def test_the_real_script_fail_answers_result_failed_for_sendto(self):
        text = (ROOT / "tools" / "teams-self.ps1").read_text(encoding="utf-8")
        fail = text[text.index("function Fail($msg) {"):]
        fail = fail[:fail.index("\n}\n")]
        self.assertIn("if ($Action -eq 'sendto') { $ev['result'] = 'failed' }", fail)


class TestToastLabelOfTheSendWord(unittest.TestCase):
    def test_the_send_word_change_is_named_in_japanese_on_the_pc_toast(self):
        body = notify.ack_text([{"id": 1, "status": "reply_send", "real": []}])[1]
        self.assertEqual(body, "#1 送る（結果は自分とのチャットへ）")
        self.assertNotIn("reply_send", body)


if __name__ == "__main__":
    unittest.main()
