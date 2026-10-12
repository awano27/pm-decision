"""R31 (promises=1): an approved ask-back (`聞き返し N` made into the reply, then `OK N`) waits for the answer of its chat. The next new
message of that chat is the answer: the judge annotates it with answers_case (the judgment itself does not change), a post that goes to
the PM starts with （#N への返事？）, a decided-by-itself answer is named in the next morning brief, and the wait ends at the next
collect that reads the lock free. Nothing is written by the judgment path; with promises=0 every output is byte-identical (golden)."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru import brief, cli, fsutil, notify, promises, push
from kimeru.backends import ReplayBackend, StubBackend
from tests.test_daily import GRAPHS, PBS
from tests.test_notify import FakeBridge, TimelineBridge

# local times: the tests do not depend on the machine's time zone
MON = datetime(2026, 10, 12, 9, 0).astimezone()
ASK_CHAT = "19:c1"
OTHER_CHAT = "19:c9"

# The default outputs of the code before R31, captured from golden_fixture (promises=0). Filled from the code at HEAD.
GOLDEN = {'brief': '[kimeru brief 2026-10-12] 今日の進め方\n'
          '1. 確認待ち #4: 金曜の件の進め方を確認したい\n'
          '確認待ち 1 件（OK 番号 / NG 番号 / 詳細 番号 で返信）\n'
          'Teams の自動判断（直近 24 時間）: 1 件（返信不要 1 件）: 佐藤\n'
          '\n'
          '承認済み・未完了の作業:\n'
          '- #3 金曜の件の進め方を確認したい',
 'ranked': [['approval:4', 1]],
 'post': '[kimeru #4] 佐藤さん: 金曜の件、進め方を教えてください\n→ 金曜の件の進め方を確認したい\n案: 確認して返信します\nOK 4 / NG 4 / 詳細 4',
 'detail': '[kimeru 詳細 #4] 判断が必要（teams_chat） / 改訂 1\n'
           '判断: 金曜の件の進め方を確認したい\n'
           '次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
           '不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
           'OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）\n'
           'teams.chat #m-9\n'
           '内容: 金曜の件の進め方を確認したい\n'
           '承認の対象: teams.reply（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n'
           '返信: OK 4 / NG 4 / 保留 4',
 'toast': ['kimeru: 確認待ち 1 件', '#4'],
 'push': 'kimeru: 確認待ち 1 件（#4）。Teams の自分とのチャットを確認してください'}


def ask_back_item(status="pending", chat=ASK_CHAT, variant="ask_back", eid="m-7", number_text="金曜の件、どの案で進めるか教えてください。"):
    """An item whose reply is an ask-back (`聞き返し N` turned the draft into the reply with variant ask_back), or a plain reply
    (variant None)."""
    action = {"type": "teams.reply", "chat_id": chat, "text": number_text}
    if variant:
        action["variant"] = variant
    return {"key": f"teams_chat@x:teams.chat:{eid}", "revision": 1, "status": status, "posted": True,
            "record": {"graph": "teams_chat", "event_kind": "teams.chat", "event_id": eid, "node": "plan_decision", "outcome": "advise",
                       "needs_human": True, "advice": "金曜の件の進め方を確認したい", "at": MON.isoformat(timespec="seconds"),
                       "event": {"kind": "teams.chat", "author": "佐藤", "chat_id": chat, "chat_title": "佐藤",
                                 "text": "金曜の件の進め方を教えてください"},
                       "actions": [action]}}


def waiting_item(status="approved"):
    """Item #3 approved with its wait open (the state R31 leaves after `OK 3` on an ask-back)."""
    item = ask_back_item(status=status)
    item["waiting"] = {"chat_id": ASK_CHAT, "since": MON.isoformat(timespec="seconds")}
    return item


def write_items(d, items):
    nxt = max((int(n) for n in items), default=0) + 1
    (Path(d) / "approvals.json").write_text(json.dumps({"next": nxt, "items": items}, ensure_ascii=False), encoding="utf-8")


def items_of(d):
    return json.loads((Path(d) / "approvals.json").read_text(encoding="utf-8"))["items"]


def write_decisions(d, rows):
    (Path(d) / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def answer_row(n=3, at=MON + timedelta(hours=1), chat=ASK_CHAT, needs_human=False, eid="m-21"):
    """A decisions row of the answer to ask-back #n, as process writes it: decided by itself (log_fyi) or sent to the PM."""
    row = {"graph": "teams_chat", "event_kind": "teams.chat", "event_id": eid, "node": "log_fyi" if not needs_human else "ask_pm",
           "outcome": "decide" if not needs_human else "advise", "needs_human": needs_human, "notify": False,
           "at": at.isoformat(timespec="seconds"), "actions": [{"type": "log.only"}] if not needs_human else [],
           "event": {"kind": "teams.chat", "author": "佐藤", "chat_id": chat, "chat_title": "佐藤", "text": "A案でお願いします。"},
           "answers_case": n}
    return row


def graph_time(t):
    """A time in the form Microsoft Graph sends it: UTC, seven fraction digits, Z."""
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".1234567Z"


def answer_event(ts, chat=ASK_CHAT, eid="m-21", text="A案でお願いします。"):
    return {"kind": "teams.chat", "id": eid, "chat_id": chat, "chat_title": "佐藤", "author": "佐藤", "ts": ts,
            "text": text, "unread": True, "mentions_me": False}


def judge(out, ev, intent="fyi", now=MON + timedelta(hours=2)):
    be = ReplayBackend({"intent": {"choice": intent, "confidence": 0.9, "probabilities": {}}})
    return cli.process(ev, GRAPHS, be, Path(out), PBS, now=now)[0]


def approve(d, n=3, flag="1"):
    """`OK N` on the self chat, read by a collect that does not send (approval recorded, nothing executed)."""
    with mock.patch.dict(os.environ, {"KIMERU_PROMISES": flag}):
        notify.collect(d, TimelineBridge([f"P:{n}", f"R:OK {n}"]), send=False, now=MON)


def file_bytes(d, *names):
    return {n: (Path(d) / n).read_bytes() if (Path(d) / n).exists() else None for n in names}


class TestTheApprovalOfAnAskBackOpensAWait(unittest.TestCase):
    """`OK N` on an ask-back reply opens item.waiting = {chat_id, since}; a plain reply opens none; promises=0 opens none."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name

    def test_approving_an_ask_back_reply_opens_a_wait_for_its_chat_when_promises_is_on(self):
        write_items(self.d, {"3": ask_back_item()})
        approve(self.d, flag="1")
        item = items_of(self.d)["3"]
        self.assertEqual(item["status"], "approved")
        self.assertEqual(item["waiting"], {"chat_id": ASK_CHAT, "since": MON.isoformat(timespec="seconds")})

    def test_with_promises_off_the_approval_of_an_ask_back_opens_no_wait(self):
        write_items(self.d, {"3": ask_back_item()})
        approve(self.d, flag="0")
        item = items_of(self.d)["3"]
        self.assertEqual(item["status"], "approved")
        self.assertNotIn("waiting", item)

    def test_a_plain_reply_that_is_not_an_ask_back_opens_no_wait(self):
        write_items(self.d, {"3": ask_back_item(variant=None)})
        approve(self.d, flag="1")
        self.assertNotIn("waiting", items_of(self.d)["3"])


class TestTheNextMessageOfTheChatIsTheAnswer(unittest.TestCase):
    """The first new message of the wait's chat, after the approval, gets answers_case = the item's number. Nothing else is annotated."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        write_items(self.d, {"3": waiting_item()})

    def test_the_first_message_of_the_chat_after_the_approval_gets_answers_case_3(self):
        with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}):
            res = judge(self.d, answer_event(graph_time(MON + timedelta(hours=1))))
        self.assertEqual(res["answers_case"], 3)
        self.assertEqual(res["node"], "log_fyi")

    def test_a_message_from_before_the_approval_gets_no_answers_case(self):
        with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}):
            res = judge(self.d, answer_event(graph_time(MON - timedelta(hours=1))))
        self.assertNotIn("answers_case", res)

    def test_a_message_from_another_chat_gets_no_answers_case(self):
        with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}):
            res = judge(self.d, answer_event(graph_time(MON + timedelta(hours=1)), chat=OTHER_CHAT))
        self.assertNotIn("answers_case", res)

    def test_a_second_message_after_the_answer_gets_no_answers_case(self):
        with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}):
            first = judge(self.d, answer_event(graph_time(MON + timedelta(hours=1)), eid="m-21"))
            second = judge(self.d, answer_event(graph_time(MON + timedelta(hours=1, minutes=5)), eid="m-22"))
        self.assertEqual(first["answers_case"], 3)
        self.assertNotIn("answers_case", second)

    def test_the_judgment_writes_no_byte_of_approvals_json_and_the_approved_item_gets_no_followup(self):
        before = file_bytes(self.d, "approvals.json")
        with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}):
            judge(self.d, answer_event(graph_time(MON + timedelta(hours=1))))
        self.assertEqual(file_bytes(self.d, "approvals.json"), before)
        item = items_of(self.d)["3"]
        self.assertEqual(item["status"], "approved")
        self.assertNotIn("followups", item["record"])


class TestTheJudgmentDoesNotChangeWithTheWait(unittest.TestCase):
    """AC2: the node and the actions are the same with and without the wait (ReplayBackend), and promises=0 adds no field at all."""

    def judged(self, item, flag):
        with tempfile.TemporaryDirectory() as d:
            write_items(d, {"3": item})
            with mock.patch.dict(os.environ, {"KIMERU_PROMISES": flag}):
                return judge(d, answer_event(graph_time(MON + timedelta(hours=1))))

    def test_the_node_and_actions_are_the_same_with_and_without_the_wait(self):
        with_wait = self.judged(waiting_item(), "1")
        without = self.judged({k: v for k, v in waiting_item().items() if k != "waiting"}, "1")
        self.assertEqual(with_wait["node"], "log_fyi")
        for key in ("node", "outcome", "needs_human", "notify", "actions", "advice"):
            self.assertEqual(with_wait.get(key), without.get(key), key)
        self.assertEqual(with_wait["answers_case"], 3)
        self.assertNotIn("answers_case", without)

    def test_with_promises_off_a_judgment_has_no_answers_case_even_with_a_wait_in_the_state(self):
        res = self.judged(waiting_item(), "0")
        self.assertNotIn("answers_case", res)


class TestTheWaitEndsAtTheNextCollect(unittest.TestCase):
    """The wait ends in a collect that holds the lock and runs for real (real=True, send=True), by reading decisions.jsonl. A busy
    lock or a run that does not send leaves it open and writes nothing; the next free collect ends it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        write_items(self.d, {"3": waiting_item()})
        write_decisions(self.d, [answer_row(3)])

    def collect(self, real=True, send=True):
        with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}):
            return notify.collect(self.d, TimelineBridge([]), real=real, send=send, now=MON + timedelta(hours=2))

    def test_the_next_free_collect_that_sends_ends_the_wait_and_keeps_the_item_approved(self):
        self.collect()
        item = items_of(self.d)["3"]
        self.assertNotIn("waiting", item)
        self.assertEqual(item["status"], "approved")

    def test_a_busy_lock_leaves_the_wait_open_and_the_next_free_collect_ends_it(self):
        with fsutil.exclusive(notify.lock_path(self.d)) as got:
            self.assertTrue(got)
            res = self.collect()
        self.assertTrue(res.busy)
        self.assertIn("waiting", items_of(self.d)["3"])
        self.collect()
        self.assertNotIn("waiting", items_of(self.d)["3"])

    def test_a_collect_that_does_not_send_ends_nothing_and_writes_no_byte(self):
        before = file_bytes(self.d, "approvals.json", "decisions.jsonl")
        self.collect(real=False, send=False)
        self.collect(real=True, send=False)
        self.assertEqual(file_bytes(self.d, "approvals.json", "decisions.jsonl"), before)
        self.assertIn("waiting", items_of(self.d)["3"])


class TestTheAnswerShowsInThePostAndInTheBrief(unittest.TestCase):
    """An answer that goes to the PM: its post starts with （#3 への返事？）. An answer decided by itself: 「返事が来ました: #3」 in the
    morning brief of its day and of the next day only, and only with promises=1."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name

    def test_an_answer_that_needs_the_pm_starts_its_post_with_the_ask_back_mark(self):
        write_items(self.d, {"3": waiting_item()})
        with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}):
            res = judge(self.d, answer_event(graph_time(MON + timedelta(hours=1))), intent="none")
            self.assertEqual(res["answers_case"], 3)
            self.assertTrue(res["needs_human"])
            bridge = FakeBridge()
            notify.notify(self.d, bridge, send=False)
        posts = [t for t, _ in bridge.posts if t.startswith("[kimeru #")]
        self.assertEqual(len(posts), 1, bridge.posts)
        self.assertTrue(posts[0].startswith("[kimeru #4] （#3 への返事？）"), posts[0])

    def test_an_answer_decided_by_itself_is_named_in_the_brief_of_its_day_and_the_next_day(self):
        write_decisions(self.d, [answer_row(3, at=MON + timedelta(hours=1))])
        with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}):
            same_day, _ = brief.build(self.d, StubBackend(), now=MON + timedelta(hours=2), date="2026-10-12")
            next_day, _ = brief.build(self.d, StubBackend(), now=MON + timedelta(days=1), date="2026-10-13")
            two_days, _ = brief.build(self.d, StubBackend(), now=MON + timedelta(days=2), date="2026-10-14")
        self.assertIn("返事が来ました: #3", same_day)
        self.assertIn("返事が来ました: #3", next_day)
        self.assertNotIn("返事が来ました", two_days)

    def test_an_answer_that_went_to_the_pm_is_not_named_as_answered_in_the_brief(self):
        write_decisions(self.d, [answer_row(3, at=MON + timedelta(hours=1), needs_human=True)])
        with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "1"}):
            text, _ = brief.build(self.d, StubBackend(), now=MON + timedelta(hours=2), date="2026-10-12")
        self.assertNotIn("返事が来ました", text)

    def test_with_promises_off_the_brief_names_no_answer(self):
        write_decisions(self.d, [answer_row(3, at=MON + timedelta(hours=1))])
        with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "0"}):
            text, _ = brief.build(self.d, StubBackend(), now=MON + timedelta(hours=2), date="2026-10-12")
        self.assertNotIn("返事が来ました", text)


def golden_fixture(d):
    """The fixed state of the golden: approved ask-back #3 (its answer is in decisions.jsonl), a pending plan request #4 of the same chat."""
    pending = {"key": "teams_chat@x:teams.chat:m-9", "revision": 1, "status": "pending", "posted": True,
               "record": {"graph": "teams_chat", "event_kind": "teams.chat", "event_id": "m-9", "node": "plan_decision", "outcome": "advise",
                          "needs_human": True, "advice": "金曜の件の進め方を確認したい", "at": MON.isoformat(timespec="seconds"),
                          "event": {"kind": "teams.chat", "author": "佐藤", "chat_id": ASK_CHAT, "chat_title": "佐藤",
                                    "text": "金曜の件、進め方を教えてください"},
                          "actions": [{"type": "teams.reply", "chat_id": ASK_CHAT, "text": "確認して返信します"}]}}
    write_items(d, {"3": ask_back_item(status="approved"), "4": pending})
    write_decisions(d, [answer_row(3, at=MON + timedelta(hours=1))])


def golden_outputs(d):
    new = items_of(d)["4"]["record"]
    text, ranked = brief.build(d, StubBackend(), now=MON, date="2026-10-12")
    return {"brief": text, "ranked": [[i["key"], i["rank"]] for i in ranked],
            "post": notify.format_post(4, new), "detail": notify.format_detail(4, new, None),
            "toast": list(notify.toast_text(Path(d), [4], 0)), "push": push.message([4], 0, 0)}


class TestTheDefaultOutputsStayTheSame(unittest.TestCase):
    """promises=0 (the default): the brief, the post, 詳細 N, the toast and the push text are byte-identical to GOLDEN, with an
    ask-back answer in the state folder. The default approval and judgment add no field."""

    def test_with_the_default_setting_every_output_is_the_golden_text(self):
        with tempfile.TemporaryDirectory() as d:
            golden_fixture(d)
            with mock.patch.dict(os.environ, {"KIMERU_PROMISES": "0"}):
                self.assertEqual(golden_outputs(d), GOLDEN)

    def test_the_default_approval_and_judgment_add_no_wait_and_no_answers_case(self):
        with tempfile.TemporaryDirectory() as d:
            write_items(d, {"3": ask_back_item()})
            approve(d, flag="0")
            self.assertNotIn("waiting", items_of(d)["3"])
            res = judge(d, answer_event(graph_time(MON + timedelta(hours=1))), now=MON + timedelta(hours=2))
            self.assertNotIn("answers_case", res)


if __name__ == "__main__":
    unittest.main()
