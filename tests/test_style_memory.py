"""R34 (learn=1): a revision instruction that is a short writing preference, used again on another item within 30 days, makes one
card. An OK keeps the wording in state/style.json, and the CLI writers (copilot, claude, grok, cmd) get it in their request, as data,
never in the check material and never for m365, m365-auto or codex. An NG keeps nothing: the wording is in neither out/ nor the state
folder (.bak included). With learn=0, or after a revert, nothing changes."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from kimeru import brief, cli, learn, notify, writer
from kimeru.backends import StubBackend
from tests.test_daily import GRAPHS, PBS
from tests.test_notify import FakeBridge, TimelineBridge
from tests.test_writer import PromptSpy

PREF = "結論を先に"          # a short preference: no digit, no link, no mention, no date word, no name
OTHER = "もっと短く"
ANSWER = {"a1": "承知しました。確認して返信します。"}
SIG = notify.instruction_sig(PREF, {"author": "佐藤", "text": "資料の件を確認してもらえますか"})
STYLE_HEAD = "PM の書き方の好み"


def reply_row(n):
    """A posted reply draft of the Teams route. Its material is kept, so `修正 N` can draft it again."""
    ev = {"kind": "teams.chat", "id": f"t{n}", "author": "佐藤", "text": "資料の件を確認してもらえますか"}
    return {"graph": "teams-chat-triage", "event_kind": "teams.chat", "event_id": f"t{n}", "node": "reply", "revision": 1,
            "outcome": "decide", "needs_human": True, "advice": "スケジュール変更の判断", "event": ev, "material_event": ev,
            "memo": {"summary": "資料の確認", "next": "確認して返信する", "missing": []},
            "actions": [{"type": "teams.reply", "to": "佐藤", "text": "確認して返信します。", "template_text": "受領しました。",
                         "drafted_by": "fake"}]}


class StyleCase(unittest.TestCase):
    """learn=1 in a state folder of its own. PromptSpy is a CLI writer (NAME cmd) that keeps every prompt it is sent."""

    def setUp(self):
        state, out = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.addCleanup(out.cleanup)
        self.state, self.out = Path(state.name), Path(out.name)
        env = mock.patch.dict(os.environ, {"KIMERU_LEARN": "1", "KIMERU_STATE_DIR": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        self.spy = PromptSpy([ANSWER])
        self.bridge = FakeBridge()
        self.number = {}   # reply draft n -> its item number (a card takes a number too)

    def queue(self, *ns):
        """The reply drafts of these numbers join the queue and are posted (what the daily cycle's notify step does)."""
        with (self.out / "queue.jsonl").open("a", encoding="utf-8") as f:
            for n in ns:
                f.write(json.dumps(reply_row(n), ensure_ascii=False) + "\n")
        notify.notify(self.out, self.bridge, send=True, real=True)
        items = notify.Approvals(self.out).data["items"]
        for n in ns:   # the item of draft n (a card takes a number too, so the numbers are read back)
            self.number[n] = next(int(k) for k, it in items.items() if it["key"] == f"teams-chat-triage:t{n}:reply")

    def revise(self, n, text):
        """`修正 <item of draft n> <text>` answered by the writer spy; the item is posted again, and so is a card."""
        k = self.number[n]
        changes = notify.collect(self.out, TimelineBridge([f"P:{k}", f"R:修正 {k} {text}"]), writer=self.spy,
                                 real=True, send=True, now=datetime.now())
        notify.notify(self.out, self.bridge, send=True, real=True)
        return changes

    def answer(self, n, word):
        """`OK n` / `NG n` / `詳細 n` as the PM's self-chat line, read by the daily cycle."""
        return notify.collect(self.out, TimelineBridge([f"P:{n}", f"R:{word} {n}"]), writer=self.spy, real=True, send=True,
                              now=datetime.now())

    def cards(self):
        return [(n, it) for n, it in notify.Approvals(self.out).data["items"].items() if learn.is_card(it)]

    def age_log(self, days):
        """The fingerprints already in approvals.log.jsonl are dated `days` ago."""
        p = self.out / "approvals.log.jsonl"
        rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        old = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
        for r in rows:
            if r.get("instruction_sig"):
                r["at"] = old
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def every_file(self):
        """Every file of the records folder and of the state folder, the .bak and .lock files included, as bytes."""
        return [p.read_bytes() for folder in (self.out, self.state) for p in folder.rglob("*") if p.is_file()]


class TestTheCardIsMadeOnTheSecondUse(StyleCase):
    def test_the_first_use_of_a_wording_makes_no_card(self):
        self.queue(1)
        self.revise(1, PREF)
        self.assertEqual(self.cards(), [])

    def test_the_same_wording_on_another_item_within_30_days_makes_one_card_for_that_wording(self):
        self.queue(1, 2)
        self.revise(1, PREF)
        self.revise(2, PREF)
        ((n, card),) = self.cards()
        self.assertEqual(card["key"], f"kimeru.learn:S{SIG}:style")
        self.assertEqual(card["record"]["learn"]["kind"], "style")
        self.assertEqual(card["record"]["learn"]["text"], PREF)
        self.assertEqual(card["record"]["learn"]["scope"], "返信")
        self.assertEqual(card["record"]["advice"], f"学習の提案 S{SIG}")   # the advice is fixed: the wording is not in it
        self.assertTrue(any(f"『{PREF}』" in text for text, _ in self.bridge.posts))   # the card post names the wording to the PM

    def test_a_third_use_adds_no_second_card(self):
        self.queue(1, 2, 3)
        for n in (1, 2, 3):
            self.revise(n, PREF)
        self.assertEqual(len(self.cards()), 1)

    def test_the_same_wording_with_other_spacing_or_punctuation_counts_as_the_same_wording(self):
        self.queue(1, 2)
        self.revise(1, PREF)
        self.revise(2, "結論　を先に。")
        self.assertEqual(len(self.cards()), 1)

    def test_a_use_more_than_30_days_earlier_makes_no_card(self):
        self.queue(1, 2)
        self.revise(1, PREF)
        self.age_log(31)
        self.revise(2, PREF)
        self.assertEqual(self.cards(), [])

    def test_a_second_use_on_the_same_item_makes_no_card(self):
        self.queue(1)
        self.revise(1, PREF)
        self.revise(1, PREF)
        self.assertEqual(self.cards(), [])

    def test_a_wording_with_a_date_a_number_a_link_a_mention_or_a_name_makes_no_card(self):
        texts = ["来週まで", "3件だけ", "https://example.com", "@佐藤 短く", "佐藤さんに短く", "明日までに", "10/5まで"]
        self.queue(*range(1, 2 * len(texts) + 1))
        for i, text in enumerate(texts):
            self.revise(2 * i + 1, text)
            self.revise(2 * i + 2, text)
        self.assertEqual(self.cards(), [])

    def test_with_no_repeated_wording_the_posts_are_the_same_at_learn_one_and_at_learn_zero(self):
        def posts(value):   # the same two revisions in a records folder of their own
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"KIMERU_LEARN": value}):
                self.out, self.bridge, self.number = Path(d), FakeBridge(), {}
                self.queue(1, 2)
                self.revise(1, PREF)
                self.revise(2, OTHER)
                return [t for t, _ in self.bridge.posts]
        self.assertEqual(posts("1"), posts("0"))

    def test_with_learn_off_no_card_is_made_and_the_request_has_no_wording_block(self):
        with mock.patch.dict(os.environ, {"KIMERU_LEARN": "0"}):
            self.queue(1, 2)
            self.revise(1, PREF)
            self.revise(2, PREF)
        self.assertEqual(self.cards(), [])
        self.assertNotIn(STYLE_HEAD, self.spy.prompts[-1])


class TestAnOk(StyleCase):
    def ok_card(self):
        self.queue(1, 2)
        self.revise(1, PREF)
        self.revise(2, PREF)
        ((n, _),) = self.cards()
        self.answer(n, "OK")
        return n

    def test_an_ok_keeps_the_wording_as_one_line_in_the_state_folder(self):
        self.ok_card()
        rows = json.loads((self.state / "style.json").read_text(encoding="utf-8"))["styles"]
        self.assertEqual([(r["id"], r["text"], r["scope"]) for r in rows], [(f"S{SIG}", PREF, "返信")])

    def test_the_next_request_carries_the_wording_as_data_before_the_output_format_and_the_check_material_does_not(self):
        self.ok_card()
        self.queue(3)
        self.revise(3, OTHER)
        prompt = self.spy.prompts[-1]
        self.assertIn(STYLE_HEAD, prompt)
        self.assertLess(prompt.index("PM からの修正指示"), prompt.index(STYLE_HEAD))
        self.assertLess(prompt.index(STYLE_HEAD), prompt.index("出力形式:"))
        self.assertIn(PREF, prompt[prompt.index(STYLE_HEAD):prompt.index("出力形式:")])
        self.assertNotIn(PREF, writer._material(reply_row(3), reply_row(3)["material_event"], OTHER))

    def test_the_morning_brief_and_its_key_points_input_do_not_show_the_wording(self):
        self.queue(1, 2)
        self.revise(1, PREF)
        self.revise(2, PREF)
        self.assertFalse(any(PREF in str(x) for x in brief.collect(self.out, now=datetime.now())))
        text, _ = brief.build(self.out, StubBackend(), now=datetime.now())
        self.assertNotIn(PREF, text)

    def test_the_wording_reaches_only_the_cli_writers_that_are_listed_and_only_for_a_reply(self):
        learn.add_style(f"S{SIG}", PREF, "返信", datetime.now())
        for name, expected in (("claude", True), ("copilot", True), ("grok", True), ("cmd", True),
                               ("m365", False), ("m365-auto", False), ("codex", False)):
            spy = PromptSpy([ANSWER])
            spy.NAME = name
            writer.apply(reply_row(9), reply_row(9)["material_event"], spy, OTHER)
            self.assertEqual(STYLE_HEAD in spy.prompts[-1], expected, name)
        no_reply = {"node": "n", "path": [], "actions": [{"type": "ado.comment", "text": "確認します。", "template_text": "確認します。"}]}
        spy = PromptSpy([{"a1": "確認しました。"}])
        writer.apply(no_reply, {"author": "佐藤", "text": "資料"}, spy, OTHER)
        self.assertNotIn(STYLE_HEAD, spy.prompts[-1])

    def test_a_revert_takes_the_wording_out_of_the_requests_and_out_of_the_state_folder(self):
        self.ok_card()
        lines = []
        self.assertTrue(any(f"S{SIG}" in line for line in learn.listing(self.out)))   # kept, and shown by learn list
        self.assertEqual(learn.dispatch(["revert", f"S{SIG}"], self.out, print_fn=lines.append), 0)
        self.assertFalse(any(PREF.encode("utf-8") in b for b in self.every_file()))
        self.queue(3)
        self.revise(3, OTHER)
        self.assertNotIn(STYLE_HEAD, self.spy.prompts[-1])
        self.assertEqual(learn.dispatch(["revert", f"S{SIG}"], self.out, print_fn=lines.append), 1)   # taken back once

    def test_a_later_use_of_the_kept_wording_adds_no_card(self):
        self.ok_card()
        self.queue(3, 4)
        self.revise(3, PREF)
        self.revise(4, PREF)
        self.assertEqual(len(self.cards()), 1)


class Recording(StubBackend):
    """The keyword judge, which keeps what it was asked (the state it was given)."""

    def __init__(self):
        self.seen = []

    def ask(self, state, questions):
        self.seen.append(json.dumps(state, ensure_ascii=False))
        return super().ask(state, questions)


class TestTheJudgeNeverSeesIt(StyleCase):
    def test_a_later_message_is_judged_without_the_wording(self):
        self.queue(1, 2)
        self.revise(1, PREF)
        self.revise(2, PREF)
        ((n, _),) = self.cards()
        self.answer(n, "OK")
        backend = Recording()
        chat = {"kind": "teams.chat", "id": "t9", "chat_id": "c9", "author": "鈴木", "text": "資料の件を確認してもらえますか"}
        cli.process(chat, GRAPHS, backend, self.out, PBS, writer=None, dedup=False, now=datetime.now())
        self.assertTrue(backend.seen)
        self.assertFalse(any(PREF in s for s in backend.seen))


class TestANg(StyleCase):
    def ng_card(self):
        self.queue(1, 2)
        self.revise(1, PREF)
        self.revise(2, PREF)
        ((n, _),) = self.cards()
        self.answer(n, "NG")
        return n

    def test_an_ng_leaves_the_wording_in_neither_out_nor_the_state_folder_and_not_in_a_bak_file(self):
        n = self.ng_card()
        self.assertEqual(notify.Approvals(self.out).data["items"][str(n)]["status"], "rejected")
        self.assertFalse(any(PREF.encode("utf-8") in b for b in self.every_file()))
        self.assertFalse((self.state / "style.json").exists())

    def test_the_wording_was_shown_to_the_PM_on_the_card_before_the_NG(self):
        self.ng_card()
        self.assertTrue(any(f"『{PREF}』" in text for text, _ in self.bridge.posts))

    def test_after_an_ng_the_next_request_has_no_wording_block(self):
        self.ng_card()
        self.queue(3)
        self.revise(3, OTHER)
        self.assertNotIn(STYLE_HEAD, self.spy.prompts[-1])

    def test_an_ng_also_takes_the_wording_out_of_the_outbox_of_the_card(self):
        self.queue(1, 2)
        self.revise(1, PREF)
        self.revise(2, PREF)
        ((n, _),) = self.cards()
        p = self.out / "approvals.json"   # a 詳細 N of the card that was not sent yet waits in the outbox
        data = json.loads(p.read_text(encoding="utf-8"))
        data["outbox"] = [f"[kimeru 詳細 #{n}] 学習の提案 S{SIG}\n変えること: 『{PREF}』"]
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.answer(n, "NG")
        self.assertFalse(any(PREF.encode("utf-8") in b for b in self.every_file()))
        self.assertEqual(notify.Approvals(self.out).data.get("outbox", []), [])


class TestTheStyleFile(StyleCase):
    def test_at_most_eight_lines_are_kept_and_the_oldest_goes_first(self):
        for i in range(9):
            learn.add_style(f"S{i:012x}", f"書き方{chr(0x3041 + i)}", "下書き", datetime.now())
        ids = [r["id"] for r in learn.style_rows()]
        self.assertEqual(len(ids), learn.STYLE_MAX)
        self.assertEqual(ids[0], f"S{1:012x}")
        self.assertEqual(ids[-1], f"S{8:012x}")

    def test_a_damaged_style_file_has_no_lines_and_breaks_nothing(self):
        (self.state / "style.json").write_text("{ broken", encoding="utf-8")
        self.assertEqual(learn.style_rows(), [])
        self.assertEqual(learn.style_for_request("claude", {"teams.reply"}), [])

    def test_nothing_is_read_or_used_at_learn_zero(self):
        learn.add_style(f"S{SIG}", PREF, "返信", datetime.now())
        with mock.patch.dict(os.environ, {"KIMERU_LEARN": "0"}):
            self.assertEqual(learn.style_for_request("claude", {"teams.reply"}), [])
            spy = PromptSpy([ANSWER])
            writer.apply(reply_row(5), reply_row(5)["material_event"], spy, OTHER)
            self.assertNotIn(STYLE_HEAD, spy.prompts[-1])


if __name__ == "__main__":
    unittest.main()
