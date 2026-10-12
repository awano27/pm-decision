"""R33 (learn=1): the PM's `不要 N` (NG, and I did not need to see it) counts per first sentence of a minutes line. Three of them on two
days, with no OK for that sentence, make one card a day; an OK writes out/learned_rules.json, and then a minutes line that is unsure
about its kind and begins with that sentence is recorded only (log.only), with the rule id. Teams items never get a card or a rule."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from kimeru import brief, cli, graph, learn, notify, plan
from kimeru.backends import ReplayBackend
from tests.test_notify import FakeBridge, TimelineBridge

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)
MINUTES = next(g for lst in GRAPHS.values() for g in lst if g["name"] == "minutes-followup")
TEAMS = next(g for lst in GRAPHS.values() for g in lst if g["name"] == "teams-chat-triage")
NOW = datetime(2026, 10, 12, 9, 0)            # naive local, as the daily cycle's clock
DAY2 = NOW + timedelta(days=1)
PHRASE = "見積りの件は後で整理する"
LINE = PHRASE + "。来週の会議で扱う"           # no request mark anywhere
UNSURE_KIND = ReplayBackend({"item_type": {"choice": "decision", "confidence": 0.3, "probabilities": {}}})
CONFIDENT_DECISION = ReplayBackend({"item_type": {"choice": "decision", "confidence": 0.9, "probabilities": {}}})


def line_event(n, text):
    return {"kind": "meeting.item", "id": f"m{n}", "meeting": "週次定例", "item": text}


def run_line(out, n, text, when, backend=UNSURE_KIND, graphs=None):
    """One minutes line judged the way the daily cycle does it (dedup=True), and the result."""
    res = cli.process(line_event(n, text), graphs or GRAPHS, backend, out, PBS, writer=None, dedup=True, now=when)
    return res[0]


def post_new(out):
    """Post what is waiting (the approval posts and the cards); the newest item number."""
    notify.notify(out, FakeBridge(), send=True, real=True)
    return max(int(k) for k in notify.Approvals(out).data["items"])


def answer(out, lines, when):
    return notify.collect(out, TimelineBridge(lines), real=True, send=True, now=when)


def items(out):
    return notify.Approvals(out).data["items"]


def card_number(out):
    return next(int(k) for k, it in items(out).items() if learn.is_card(it))


def rules_file(out):
    return json.loads((Path(out) / "learned_rules.json").read_text(encoding="utf-8"))


def write_rules(out, *rules):
    Path(out).mkdir(parents=True, exist_ok=True)
    data = {"next": len(rules) + 1, "rules": list(rules)}
    (Path(out) / "learned_rules.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def rule(rid="A1", phrase=PHRASE, graph="minutes-followup", node="check_line", at=NOW - timedelta(days=1)):
    return {"id": rid, "graph": graph, "node": node, "phrase": phrase, "adopted_at": at.isoformat(timespec="seconds"),
            "uses": 0, "active": True, "judge": "ReplayBackend"}


class Learn1(unittest.TestCase):
    """learn=1 for the test, in a state folder of its own."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {"KIMERU_LEARN": "1"})
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)

    def not_needed(self, n, when, text=LINE):
        """Item n is judged, posted, and answered `不要 n` at `when`."""
        run_line(self.out, n, text, when)
        num = post_new(self.out)
        answer(self.out, [f"P:{num}", f"R:不要 {num}"], when)
        return num

    def make_rule(self):
        """Three `不要` on two days, then the card's OK: the rule A1 (never by hand)."""
        self.not_needed(1, NOW)
        self.not_needed(2, NOW)
        self.not_needed(3, DAY2)
        self.assertEqual(learn.offer_rule(self.out, UNSURE_KIND, now=DAY2)["state"], "queued")
        card = post_new(self.out)
        answer(self.out, [f"P:{card}", f"R:OK {card}"], DAY2)


class TestPhrase(unittest.TestCase):
    def test_the_phrase_is_the_first_sentence_without_the_request_text(self):
        self.assertEqual(learn.phrase_of({"text": "見積りの件は後で整理する。確認してください"}), "見積りの件は後で整理する")

    def test_a_greeting_sentence_is_skipped(self):
        self.assertEqual(learn.phrase_of({"text": "お疲れ様です。見積りの件は後で整理する"}), "見積りの件は後で整理する")

    def test_digit_runs_become_a_pattern_and_the_group_prefix_is_taken_off(self):
        self.assertEqual(learn.phrase_of({"text": "3月15日までに見積りを出す"}), r"\d+月\d+日までに見積りを出す")
        self.assertEqual(learn.phrase_of({"author": "山田", "text": "山田: 見積りの件は後で整理する"}), "見積りの件は後で整理する")

    def test_a_minutes_line_reads_its_item_and_a_short_or_long_sentence_has_no_phrase(self):
        self.assertEqual(learn.phrase_of({"item": PHRASE}), PHRASE)
        self.assertIsNone(learn.phrase_of({"text": "了解。"}))
        self.assertIsNone(learn.phrase_of({"text": "あ" * 31}))


class TestNotNeededAnswer(Learn1):
    def test_not_needed_is_rejected_and_the_log_keeps_the_mark_but_not_the_words(self):
        num = self.not_needed(1, NOW)
        it = items(self.out)[str(num)]
        self.assertEqual(it["status"], "rejected")
        self.assertIn("not_needed_at", it)
        log = (self.out / "approvals.log.jsonl").read_text(encoding="utf-8")
        row = json.loads(log.splitlines()[-1])
        self.assertTrue(row["not_needed"])
        self.assertEqual(row["node"], "minutes-followup:check_line")
        self.assertEqual(len(row["phrase"]), 12)
        self.assertNotIn(PHRASE, log)
        self.assertNotIn("来週", log)

    def test_a_teams_item_is_rejected_but_gets_no_phrase_mark(self):
        chat = {"kind": "teams.chat", "id": "t1", "chat_id": "c1", "author": "山田", "text": "資料の件、確認をお願いします"}
        cli.process(chat, GRAPHS, ReplayBackend({"intent": {"choice": "decision", "confidence": 0.3, "probabilities": {}}}),
                    self.out, PBS, writer=None, dedup=True, now=NOW)
        num = post_new(self.out)
        answer(self.out, [f"P:{num}", f"R:不要 {num}"], NOW)
        it = items(self.out)[str(num)]
        self.assertEqual(it["status"], "rejected")
        self.assertNotIn("not_needed_at", it)

    def test_a_not_needed_reply_under_a_test_post_is_not_read_by_the_real_reader(self):
        self.assertEqual(notify.fresh_replies({"timeline": ["T:1", "R:不要 1"]}), [])
        self.assertEqual(notify.fresh_replies({"timeline": ["P:1", "R:不要 1"]}), ["不要 1"])

    def test_parse_not_needed_reads_the_number_only(self):
        self.assertEqual(notify.parse_not_needed("不要 3"), "3")
        self.assertEqual(notify.parse_not_needed("不要＃３。"), "3")
        self.assertIsNone(notify.parse_not_needed("不要 3 ください"))
        self.assertIsNone(notify.parse_not_needed("NG 3"))


class TestNotNeededAtLearnZero(unittest.TestCase):
    def test_a_not_needed_reply_does_nothing_at_learn_zero(self):
        with mock.patch.dict(os.environ, {"KIMERU_LEARN": "0"}):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            out = Path(tmp.name)
            run_line(out, 1, LINE, NOW)
            num = post_new(out)
            got = answer(out, [f"P:{num}", f"R:不要 {num}"], NOW)
            self.assertEqual(got, [])
            self.assertEqual(items(out)[str(num)]["status"], "pending")
            self.assertIsNone(notify.parse_reply("不要 3"))


class TestRuleCard(Learn1):
    def test_two_on_two_days_makes_no_card(self):
        self.not_needed(1, NOW)
        self.not_needed(2, DAY2)
        self.assertEqual(learn.offer_rule(self.out, UNSURE_KIND, now=DAY2)["state"], "nothing")

    def test_three_on_one_day_makes_no_card(self):
        for n in (1, 2, 3):
            self.not_needed(n, NOW)
        self.assertEqual(learn.offer_rule(self.out, UNSURE_KIND, now=NOW)["state"], "nothing")

    def test_three_on_two_days_makes_one_card_a_day(self):
        self.not_needed(1, NOW)
        self.not_needed(2, NOW)
        self.not_needed(3, DAY2)
        self.assertEqual(learn.offer_rule(self.out, UNSURE_KIND, now=DAY2)["state"], "queued")
        self.assertEqual(learn.offer_rule(self.out, UNSURE_KIND, now=DAY2)["state"], "this_day")
        card = post_new(self.out)
        self.assertTrue(learn.is_card(items(self.out)[str(card)]))
        post = notify.format_post(card, items(self.out)[str(card)]["record"])
        self.assertIn("迷ったときは記録だけにしますか", post)
        self.assertIn(PHRASE, post)
        self.assertTrue(post.endswith(f"OK {card} / NG {card} / 詳細 {card}"))

    def test_the_rule_file_appears_only_after_the_ok(self):
        self.not_needed(1, NOW)
        self.not_needed(2, NOW)
        self.not_needed(3, DAY2)
        learn.offer_rule(self.out, UNSURE_KIND, now=DAY2)
        card = post_new(self.out)
        self.assertFalse((self.out / "learned_rules.json").exists())
        answer(self.out, [f"P:{card}", f"R:OK {card}"], DAY2)
        data = rules_file(self.out)
        self.assertEqual(data["rules"][0]["id"], "A1")
        self.assertEqual(data["rules"][0]["phrase"], PHRASE)
        self.assertTrue(data["rules"][0]["active"])

    def test_an_ng_keeps_the_sentence_quiet_for_thirty_days(self):
        self.not_needed(1, NOW)
        self.not_needed(2, NOW)
        self.not_needed(3, DAY2)
        learn.offer_rule(self.out, UNSURE_KIND, now=DAY2)
        card = post_new(self.out)
        answer(self.out, [f"P:{card}", f"R:NG {card}"], DAY2)
        later = DAY2 + timedelta(days=3)
        self.not_needed(4, later)
        self.assertEqual(learn.offer_rule(self.out, UNSURE_KIND, now=later)["state"], "nothing")
        self.assertFalse((self.out / "learned_rules.json").exists())


class TestRuleApplies(Learn1):
    def test_the_same_unsure_line_is_only_recorded_and_names_its_rule(self):
        self.make_rule()
        queue_before = (self.out / "queue.jsonl").read_text(encoding="utf-8")
        res = run_line(self.out, 10, LINE, DAY2 + timedelta(hours=1))
        self.assertEqual(res["outcome"], "decide")
        self.assertFalse(res["needs_human"])
        self.assertFalse(res["notify"])
        self.assertEqual(res["actions"], [{"type": "log.only"}])
        self.assertEqual(res["demoted_by"], "A1")
        self.assertEqual(res["via_unsure"], ["item_type"])
        self.assertEqual(res["node"], "check_line")
        self.assertEqual((self.out / "queue.jsonl").read_text(encoding="utf-8"), queue_before)
        self.assertEqual(rules_file(self.out)["rules"][0]["uses"], 1)

    def test_a_request_mark_in_another_sentence_keeps_the_question(self):
        self.make_rule()
        res = run_line(self.out, 10, PHRASE + "。確認してください", DAY2)
        self.assertTrue(res["needs_human"])
        self.assertNotIn("demoted_by", res)

    def test_a_confident_decision_of_the_same_sentence_is_not_touched(self):
        self.make_rule()
        res = run_line(self.out, 10, LINE, DAY2, backend=CONFIDENT_DECISION)
        self.assertEqual(res["node"], "post_decision")
        self.assertNotIn("demoted_by", res)

    def test_a_severe_line_is_never_recorded_only(self):
        self.make_rule()
        g = copy.deepcopy(MINUTES)
        g["severe"] = {"field": "item", "pattern": "重大", "exempt": []}
        res = run_line(self.out, 10, PHRASE + "。重大な遅れの見込み", DAY2, graphs={"meeting.item": [g]})
        self.assertNotIn("demoted_by", res)
        self.assertTrue(res["needs_human"] or res["notify"])

    def test_a_result_that_posts_or_pages_is_never_recorded_only(self):
        self.make_rule()
        res = {"graph": "minutes-followup", "node": "check_line", "outcome": "advise", "needs_human": True, "notify": False,
               "path": [{"node": "item_type", "answer": {}, "edge": "unsure"}], "actions": [{"type": "teams.post", "channel": "x", "text": "t"}],
               "event_id": "m9", "advice": ""}
        ev = line_event(9, LINE)
        self.assertEqual(learn.demote(self.out, MINUTES, res, ev, now=DAY2), res)

    def test_a_teams_question_never_gets_a_rule_even_when_one_is_written_by_hand(self):
        write_rules(self.out, rule(graph="teams-chat-triage", node="ask_pm"))
        res = {"graph": "teams-chat-triage", "node": "ask_pm", "outcome": "advise", "needs_human": True, "notify": False,
               "path": [{"node": "intent", "answer": {}, "edge": "unsure"}], "actions": [], "event_id": "t1", "advice": ""}
        ev = {"kind": "teams.chat", "id": "t1", "chat_id": "c9", "author": "山田", "text": LINE}
        self.assertEqual(learn.demote(self.out, TEAMS, res, ev, now=DAY2), res)

    def test_no_rule_applies_at_learn_zero(self):
        self.make_rule()
        with mock.patch.dict(os.environ, {"KIMERU_LEARN": "0"}):
            res = run_line(self.out, 10, LINE, DAY2)
        self.assertTrue(res["needs_human"])
        self.assertNotIn("demoted_by", res)

    def test_a_rule_unused_for_sixty_days_stops_applying(self):
        write_rules(self.out, rule(at=NOW - timedelta(days=61)))
        res = {"graph": "minutes-followup", "node": "check_line", "outcome": "advise", "needs_human": True, "notify": False,
               "path": [{"node": "item_type", "answer": {}, "edge": "unsure"}], "actions": [{"type": "ado.create"}],
               "event_id": "m9", "advice": ""}
        self.assertEqual(learn.demote(self.out, MINUTES, res, line_event(9, LINE), now=NOW), res)


class TestRevert(Learn1):
    def test_revert_takes_the_rule_back_and_the_line_goes_to_the_pm_again(self):
        self.make_rule()
        lines = []
        self.assertEqual(learn.dispatch(["revert", "A1"], self.out, print_fn=lines.append), 0)
        self.assertFalse(rules_file(self.out)["rules"][0]["active"])
        self.assertTrue(run_line(self.out, 10, LINE, DAY2)["needs_human"])
        self.assertEqual(learn.dispatch(["revert", "A1"], self.out, print_fn=lines.append), 1)

    def test_a_followup_from_the_same_chat_within_a_day_stops_the_rule_and_goes_to_the_pm(self):
        write_rules(self.out, rule())
        row = {"at": (NOW - timedelta(hours=1)).isoformat(timespec="seconds"), "demoted_by": "A1",
               "event": {"kind": "teams.chat", "chat_id": "c1"}}
        (self.out / "decisions.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        res = {"graph": "minutes-followup", "node": "check_line", "outcome": "advise", "needs_human": True, "notify": False,
               "path": [{"node": "item_type", "answer": {}, "edge": "unsure"}], "actions": [], "event_id": "x", "advice": "分類"}
        ev = {"kind": "teams.chat", "id": "x", "chat_id": "c1", "author": "山田", "text": LINE}
        got = learn.demote(self.out, MINUTES, res, ev, now=NOW)
        self.assertNotIn("demoted_by", got)
        self.assertTrue(got["needs_human"])
        self.assertIn("規則を戻しました", got["advice"])
        rule_row = rules_file(self.out)["rules"][0]
        self.assertFalse(rule_row["active"])
        self.assertEqual(rule_row["stopped_reason"], "followup")

    def test_a_followup_after_a_day_does_not_stop_the_rule(self):
        write_rules(self.out, rule())
        row = {"at": (NOW - timedelta(hours=25)).isoformat(timespec="seconds"), "demoted_by": "A1",
               "event": {"kind": "teams.chat", "chat_id": "c1"}}
        (self.out / "decisions.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        res = {"graph": "minutes-followup", "node": "check_line", "outcome": "advise", "needs_human": True, "notify": False,
               "path": [{"node": "item_type", "answer": {}, "edge": "unsure"}], "actions": [], "event_id": "x", "advice": ""}
        ev = {"kind": "teams.chat", "id": "x", "chat_id": "c1", "author": "山田", "text": LINE}
        got = learn.demote(self.out, MINUTES, res, ev, now=NOW)
        self.assertEqual(got["demoted_by"], "A1")
        self.assertTrue(rules_file(self.out)["rules"][0]["active"])


class TestBriefAndList(Learn1):
    def test_the_brief_lists_at_most_five_recorded_only_lines_with_their_rule(self):
        rows = [{"at": (NOW - timedelta(hours=1)).isoformat(timespec="seconds"), "graph": "minutes-followup",
                 "event_kind": "meeting.item", "event_id": f"m{i}", "node": "check_line", "outcome": "decide",
                 "needs_human": False, "notify": False, "via_unsure": ["item_type"], "demoted_by": "A1",
                 "summary": f"週次定例: 件 {i}"} for i in range(6)]
        (self.out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        text = brief.rule_section(self.out, now=NOW)
        lines = text.splitlines()
        self.assertEqual(lines[0], "規則で記録だけにした件")
        self.assertEqual(len(lines), 1 + 5 + 1)
        self.assertTrue(all("規則 A1" in line for line in lines[1:6]))
        self.assertEqual(lines[-1], "ほか 1 件")
        self.assertEqual(brief.unsure_auto(self.out, now=NOW), [])

    def test_the_listing_names_the_rule_and_its_state(self):
        write_rules(self.out, rule())
        listing = "\n".join(learn.listing(self.out))
        self.assertIn("規則 A1", listing)
        self.assertIn(PHRASE, listing)


class TestAnIncidentStaysAnIncident(Learn1):
    def test_a_learned_sentence_in_front_of_a_breakage_still_gives_incident_bug(self):
        from tests.test_judge_rules import CHAT_INCIDENT   # the fixture only (its test cases are not run from here)
        text = PHRASE + "。" + CHAT_INCIDENT[0]
        write_rules(self.out, rule(graph="teams-chat-triage", node="incident_bug"))
        ev = {"kind": "teams.chat", "id": "c1", "chat_id": "chat1", "author": "山田", "text": text}
        res = cli.process(ev, GRAPHS, ReplayBackend({"intent": {"choice": "fyi", "confidence": 0.9}}), self.out, PBS,
                          writer=None, dedup=True, now=DAY2)[0]
        self.assertEqual((res["node"], res["notify"]), ("incident_bug", True))
        self.assertNotIn("demoted_by", res)


class TestDefaultUnchanged(unittest.TestCase):
    """learn=0 (the default): the approval post, the detail and the brief's unsure lines are the same bytes as before R33."""

    def test_the_minutes_line_texts_do_not_change_at_learn_zero(self):
        with mock.patch.dict(os.environ, {"KIMERU_LEARN": "0"}):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            out = Path(tmp.name)
            ev = {"kind": "meeting.item", "id": "m1", "meeting": "週次定例", "item": "取引先への見積りを確認する件について相談した"}
            res = cli.process(ev, GRAPHS, ReplayBackend({"item_type": {"choice": "decision", "confidence": 0.3, "probabilities": {}}}),
                              out, PBS, writer=None, dedup=False)
            self.assertEqual({k: res[0].get(k) for k in ("node", "outcome", "needs_human", "notify", "via_unsure")},
                             {"node": "check_line", "outcome": "advise", "needs_human": True, "notify": False, "via_unsure": ["item_type"]})
            notify.notify(out, FakeBridge(), send=False)
            b = FakeBridge()
            notify.notify(out, b, send=True)
            self.assertEqual(b.posts[0][0],
                             '[kimeru #1] 週次定例: 取引先への見積りを確認する件について相談した\n'
                             '→ 分類できない議事録行（週次定例）: 取引先への見積りを確認する件について相談した\n'
                             '起票案: 取引先への見積りを確認する件について相談した\nOK 1 / NG 1 / 詳細 1')
            self.assertEqual(notify.format_detail(1, notify.Approvals(out).data["items"]["1"]["record"]),
                             '[kimeru 詳細 #1] 判断が必要（minutes-followup） / 改訂 1\n'
                             '判断: 分類できない議事録行（週次定例）: 取引先への見積りを確認する件について相談した\n'
                             '次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                             '不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                             'OK の効果: 記録のみ\nmeeting.item #m1\n'
                             '内容: 分類できない議事録行（週次定例）: 取引先への見積りを確認する件について相談した\n'
                             '承認の対象: ado.create（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n'
                             '返信: OK 1 / NG 1 / 保留 1')
            self.assertEqual(brief.unsure_section(out, now=NOW), "")


class TestReaderKnowsTheWord(unittest.TestCase):
    def test_the_real_reader_and_the_fake_reader_both_hand_back_the_word(self):
        for path in (ROOT / "tools" / "teams-self.ps1", ROOT / "tests" / "fake-teams-self.ps1"):
            text = path.read_text(encoding="utf-8-sig")
            self.assertIn("(OK|NG|保留|聞き返し|詳細|再実行|済|不要)", text, path.name)


if __name__ == "__main__":
    unittest.main()
