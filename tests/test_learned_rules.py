"""R33 (learn=1): the PM's `不要 N` (NG, and I did not need to see it) counts per first sentence of a minutes line. Three of them on two
days, with no OK for that sentence, make one card a day; an OK writes out/learned_rules.json, and then a minutes line that is unsure
about its kind and begins with that sentence is recorded only (log.only), with the rule id. Teams items never get a card or a rule."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import copy
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru import brief, cli, execute, fulltext, graph, learn, notify, plan, stats
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


def stamp_last_decision(out, when):
    """process() writes the real clock into a decision's `at`; the 24-hour window is tested on the injected clock, so the newest
    decisions line is stamped with `when` (a naive local time, as the daily cycle's clock)."""
    p = Path(out) / "decisions.jsonl"
    rows = p.read_text(encoding="utf-8").splitlines()
    row = json.loads(rows[-1])
    row["at"] = when.isoformat(timespec="seconds")
    rows[-1] = json.dumps(row, ensure_ascii=False)
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")


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
        self.assertEqual(notify.fresh_replies({"timeline": ["P:1", "T:1", "R:不要 1"]}), [])   # R33 review fix 2: a reply after a test post
        self.assertEqual(notify.scoped_timeline({"timeline": ["P:1", "T:1", "R:不要 1"]}, test=True), ["P:1", "R:不要 1"])
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

    def test_every_request_mark_in_another_sentence_keeps_the_question(self):
        """Each entry of learn.RULE_MARKS (read from the module, so a mark added later is covered), plus the seven marks pinned here
        (a mark dropped from the constant must fail its own subTest, not vanish from the loop) and a longer literal: the line begins
        with the rule's phrase, the mark is in the second sentence, and the question stays."""
        pinned = ("？", "?", "ください", "お願い", "決め", "至急", "本日中", "至急確認ください")
        marks = list(dict.fromkeys(learn.RULE_MARKS + pinned))
        self.make_rule()
        for n, mark in enumerate(marks, start=10):
            with self.subTest(mark=mark):
                res = run_line(self.out, n, PHRASE + "。来週の会議で扱う" + mark, DAY2)
                self.assertIs(res["needs_human"], True)
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

    def test_a_result_that_pages_comments_or_is_gated_is_never_recorded_only_and_no_rule_is_used(self):
        """AC4: oncall.page, teams.post, ado.comment and a gated kind (execute.is_gated) keep the question. The result comes back as
        it was, and learned_rules.json is not written (the rule's uses stay 0)."""
        self.make_rule()
        path = [{"node": "item_type", "answer": {}, "edge": "unsure"}]
        cases = [("oncall.page", {}), ("teams.post", {"channel": "x", "text": "t"}), ("ado.comment", {"id": "1", "text": "t"})]
        for kind, extra in cases:
            with self.subTest(action=kind):
                res = {"graph": "minutes-followup", "node": "check_line", "outcome": "advise", "needs_human": True, "notify": False,
                       "path": path, "actions": [{"type": kind, **extra}], "event_id": "m9", "advice": ""}
                self.assertIs(learn.demote(self.out, MINUTES, res, line_event(9, LINE), now=DAY2), res)
                self.assertEqual(rules_file(self.out)["rules"][0]["uses"], 0)
        before = (self.out / "learned_rules.json").read_bytes()
        # a kind outside RULE_NEVER_ACTIONS that the PM switched on for real execution: gated by execute.is_gated
        with self.subTest(action="gated"), mock.patch.object(execute, "enabled_types", return_value={"ado.create"}):
            res = {"graph": "minutes-followup", "node": "check_line", "outcome": "advise", "needs_human": True, "notify": False,
                   "path": path, "actions": [{"type": "ado.create", "title": "t"}], "event_id": "m9", "advice": ""}
            self.assertIs(learn.demote(self.out, MINUTES, res, line_event(9, LINE), now=DAY2), res)
        self.assertEqual((self.out / "learned_rules.json").read_bytes(), before)
        self.assertEqual(rules_file(self.out)["rules"][0]["uses"], 0)

    def test_a_rule_leaves_the_graph_files_and_their_versions_alone(self):
        def digest():
            files = {p.name: hashlib.sha1(p.read_bytes()).hexdigest() for p in sorted((ROOT / "graphs").glob("*.json"))}
            return files, {g["name"]: cli._graph_version(g) for lst in GRAPHS.values() for g in lst}
        before = digest()
        self.make_rule()
        self.assertEqual(run_line(self.out, 10, LINE, DAY2 + timedelta(hours=1)).get("demoted_by"), "A1")
        self.assertEqual(digest(), before)

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

    def test_a_reverted_sentence_is_not_offered_again_the_next_day(self):
        """R33 review fix 2: a revert keeps its sentence quiet for RULE_NG_DAYS, as an NG on its card does (the 不要 marks of
        make_rule are still within RULE_DAYS here, so only the stopped rule keeps the card back)."""
        self.make_rule()
        self.assertEqual(learn.revert_rule(self.out, "A1", now=DAY2), "reverted")
        self.assertEqual(learn.offer_rule(self.out, UNSURE_KIND, now=DAY2 + timedelta(days=1))["state"], "nothing")
        self.assertFalse(rules_file(self.out)["rules"][0]["active"])

    def test_a_reverted_sentence_can_be_offered_again_after_thirty_days(self):
        self.make_rule()
        learn.revert_rule(self.out, "A1", now=DAY2)
        later = DAY2 + timedelta(days=31)
        self.not_needed(4, later)
        self.not_needed(5, later)
        self.not_needed(6, later + timedelta(days=1))
        self.assertEqual(learn.offer_rule(self.out, UNSURE_KIND, now=later + timedelta(days=1))["state"], "queued")

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

    def test_a_stopped_followup_goes_to_the_pm_even_when_the_graph_decided_it(self):
        """The chat signal is synthetic here: a minutes line carries no chat_id, so only a chat line can stop a rule (see demote)."""
        write_rules(self.out, rule())
        row = {"at": (NOW - timedelta(hours=1)).isoformat(timespec="seconds"), "demoted_by": "A1",
               "event": {"kind": "teams.chat", "chat_id": "c1"}}
        (self.out / "decisions.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        res = {"graph": "minutes-followup", "node": "post_decision", "outcome": "decide", "needs_human": False, "notify": False,
               "path": [{"node": "item_type", "answer": {}, "edge": "decision"}], "actions": [{"type": "log.only"}],
               "event_id": "x", "advice": "決定"}
        ev = {"kind": "teams.chat", "id": "x", "chat_id": "c1", "author": "山田", "text": LINE}
        got = learn.demote(self.out, MINUTES, res, ev, now=NOW)
        self.assertEqual((got["outcome"], got["needs_human"], got["notify"]), ("advise", True, False))
        self.assertEqual(got["actions"], [{"type": "log.only"}])
        self.assertIn("規則を戻しました", got["advice"])
        self.assertFalse(rules_file(self.out)["rules"][0]["active"])

    def test_a_line_whose_rules_cannot_be_locked_goes_to_the_pm_too(self):
        write_rules(self.out, rule())
        row = {"at": (NOW - timedelta(hours=1)).isoformat(timespec="seconds"), "demoted_by": "A1",
               "event": {"kind": "teams.chat", "chat_id": "c1"}}
        (self.out / "decisions.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        res = {"graph": "minutes-followup", "node": "post_decision", "outcome": "decide", "needs_human": False, "notify": False,
               "path": [], "actions": [], "event_id": "x", "advice": "決定"}
        ev = {"kind": "teams.chat", "id": "x", "chat_id": "c1", "author": "山田", "text": LINE}
        with mock.patch.object(learn, "_stop_rules", return_value=None):
            got = learn.demote(self.out, MINUTES, res, ev, now=NOW)
        self.assertEqual((got["outcome"], got["needs_human"]), ("advise", True))
        self.assertTrue(rules_file(self.out)["rules"][0]["active"])

    def test_the_chat_is_read_only_while_a_rule_is_on(self):
        res = {"graph": "minutes-followup", "node": "post_decision", "outcome": "decide", "needs_human": False, "notify": False,
               "path": [], "actions": [], "event_id": "x", "advice": ""}
        ev = {"kind": "teams.chat", "id": "x", "chat_id": "c1", "author": "山田", "text": LINE}
        with mock.patch.object(learn, "_recorded_only_ids", return_value=set()) as scan:
            self.assertIs(learn.demote(self.out, MINUTES, res, ev, now=NOW), res)   # no rules file at all
            self.assertFalse(scan.called)
            write_rules(self.out, rule(at=NOW - timedelta(days=1)))
            learn.demote(self.out, MINUTES, res, ev, now=NOW)                       # a rule that is on
            self.assertTrue(scan.called)


class TestFollowUpOnTheMinutesPath(Learn1):
    """R33 AC3 on the minutes path, the only path the allow list lets a rule decide: a new line of the same meeting (its title, the
    `meeting` of the recorded event) within 24 hours after a recorded-only line stops the rule, and that line goes to the PM with
    （規則を戻しました）. A Teams chat_id is the same mechanism for a chat (source_of) and is not reached from here."""

    def test_a_new_line_of_the_same_meeting_within_a_day_stops_the_rule_and_goes_to_the_pm(self):
        self.make_rule()
        self.assertEqual(run_line(self.out, 10, LINE, DAY2 + timedelta(hours=1)).get("demoted_by"), "A1")
        stamp_last_decision(self.out, DAY2 + timedelta(hours=1))
        got = run_line(self.out, 11, "別の件です。来週までに確認する", DAY2 + timedelta(hours=2))
        self.assertEqual((got["outcome"], got["needs_human"], got["notify"]), ("advise", True, False))
        self.assertNotIn("demoted_by", got)
        self.assertIn("（規則を戻しました）", got["advice"])
        rule_row = rules_file(self.out)["rules"][0]
        self.assertEqual((rule_row["active"], rule_row["stopped_reason"]), (False, "followup"))
        bridge = FakeBridge()
        notify.notify(self.out, bridge, send=True, real=True)
        posted = [text for text, _ in bridge.posts if "（規則を戻しました）" in text]
        self.assertEqual(len(posted), 1)
        self.assertTrue(posted[0].startswith("[kimeru #"))

    def test_a_line_of_another_meeting_within_a_day_does_not_stop_the_rule(self):
        self.make_rule()
        run_line(self.out, 10, LINE, DAY2 + timedelta(hours=1))
        stamp_last_decision(self.out, DAY2 + timedelta(hours=1))
        other = {"kind": "meeting.item", "id": "m11", "meeting": "別の定例", "item": LINE}
        got = cli.process(other, GRAPHS, UNSURE_KIND, self.out, PBS, writer=None, dedup=True, now=DAY2 + timedelta(hours=2))[0]
        self.assertEqual(got.get("demoted_by"), "A1")
        self.assertTrue(rules_file(self.out)["rules"][0]["active"])

    def test_a_line_of_the_same_meeting_after_a_day_does_not_stop_the_rule(self):
        self.make_rule()
        run_line(self.out, 10, LINE, DAY2 + timedelta(hours=1))
        stamp_last_decision(self.out, DAY2 + timedelta(hours=1))
        self.assertEqual(run_line(self.out, 11, LINE, DAY2 + timedelta(hours=26)).get("demoted_by"), "A1")
        self.assertTrue(rules_file(self.out)["rules"][0]["active"])

    def test_a_follow_up_that_stops_a_rule_keeps_its_sentence_quiet_for_thirty_days(self):
        self.make_rule()
        run_line(self.out, 10, LINE, DAY2 + timedelta(hours=1))
        stamp_last_decision(self.out, DAY2 + timedelta(hours=1))
        when = DAY2 + timedelta(hours=2)
        run_line(self.out, 11, "別の件です。来週までに確認する", when)
        num = post_new(self.out)
        answer(self.out, [f"P:{num}", f"R:NG {num}"], when)
        self.assertEqual(learn.offer_rule(self.out, UNSURE_KIND, now=DAY2 + timedelta(days=1))["state"], "nothing")

    def test_bullets_of_one_payload_are_not_follow_ups_of_each_other(self):
        """R33 review fix 2: one minutes file is one payload, so its bullets are judged in one run. The bullet that names the rule is
        recorded only, a bullet that does not name it goes to the PM, and a later bullet that names it is recorded only too: the rule
        stays on. Only a line of a later run stops it (the test above)."""
        self.make_rule()
        body = {"title": "週次定例", "date": DAY2.date().isoformat(),
                "text": f"- {LINE}\n- 別の件です。来週までに確認する\n- {PHRASE}。金曜の定例で扱う"}
        got = cli.process(body, GRAPHS, UNSURE_KIND, self.out, PBS, writer=None, dedup=True, now=DAY2 + timedelta(hours=1))
        self.assertEqual([r.get("demoted_by") for r in got], ["A1", None, "A1"])
        self.assertFalse(any("（規則を戻しました）" in (r.get("advice") or "") for r in got))
        rule_row = rules_file(self.out)["rules"][0]
        self.assertEqual((rule_row["active"], rule_row["uses"], rule_row.get("stopped_reason")), (True, 2, None))


class TestTheDailyCycleQueuesTheRuleCard(Learn1):
    """R33 AC1 on the daily path: the cycle's learn_rule step (daily.cycle, real=True) is what makes the card."""

    def cycle(self, now):
        from kimeru import daily
        bridge = FakeBridge()
        daily.cycle(self.out, self.out / "inbox", GRAPHS, UNSURE_KIND, PBS, lambda *a, **k: [], bridge=bridge, send=True,
                    now=now, real=True, brief_hour=24)
        return [text for text, _ in bridge.posts if "迷ったときは記録だけにしますか" in text]

    def rule_cards(self):
        return sum(1 for it in items(self.out).values() if ((it.get("record") or {}).get("learn") or {}).get("rule"))

    def test_three_not_needed_on_two_days_post_one_rule_card_in_the_daily_cycle(self):
        self.not_needed(1, NOW)
        self.not_needed(2, NOW)
        self.not_needed(3, DAY2)
        posted = self.cycle(DAY2 + timedelta(hours=1))
        self.assertEqual(len(posted), 1)
        self.assertTrue(posted[0].startswith("[kimeru #"))
        self.cycle(DAY2 + timedelta(hours=2))
        self.cycle(DAY2 + timedelta(days=1))   # the card still waits: no second card on the next day
        self.assertEqual(self.rule_cards(), 1)


class TestNotNeededDropsTheWholeText(Learn1):
    def test_a_not_needed_answer_deletes_the_whole_text_kept_for_a_teams_item(self):
        cli.process({"kind": "teams.chat", "id": "t1", "chat_id": "c1", "author": "山田", "text": "資料の件、確認をお願いします"},
                    GRAPHS, ReplayBackend({"intent": {"choice": "decision", "confidence": 0.3, "probabilities": {}}}),
                    self.out, PBS, writer=None, dedup=True, now=NOW)
        num = post_new(self.out)
        key = items(self.out)[str(num)]["key"]
        fulltext.save(self.out, key, {"text": "全文だけの一文です。見積りの件は後で整理する", "thread": []})
        self.assertIsNotNone(fulltext.load(self.out, key))
        answer(self.out, [f"P:{num}", f"R:不要 {num}"], NOW)
        self.assertEqual(items(self.out)[str(num)]["status"], "rejected")
        self.assertIsNone(fulltext.load(self.out, key))
        for name in ("full_text.json", "approvals.log.jsonl"):
            p = self.out / name
            self.assertNotIn("全文だけの一文", p.read_text(encoding="utf-8") if p.exists() else "")


class TestDigestCountsTheRuleApart(Learn1):
    def test_a_recorded_only_line_is_counted_apart_and_jev_is_left_out_as_before(self):
        now = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)

        def row(eid, judge, **extra):
            r = {"graph": "minutes-followup", "event_id": eid, "node": "check_line", "event_kind": "meeting.item",
                 "outcome": "decide", "needs_human": False, "notify": False, "at": (now - timedelta(days=1)).isoformat(timespec="seconds"),
                 "path": [{"node": "item_type", "answer": {}, "edge": "unsure"}], "actions": [], "summary": "週次定例: 件",
                 "judge": {"name": judge, "model": ""}, "measurement": {"schema": 1, "source": "local", "generation": 1}}
            r.update(extra)
            return r
        rows = [row("a1", "Kev"), row("a2", "Kev", demoted_by="A1", actions=[{"type": "log.only"}]), row("a3", "Jev", demoted_by="A1")]
        (self.out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        label = "規則で記録だけにした件（自動の正誤には入れていません）: 1 件"
        text = stats.week(self.out, now=now)
        self.assertIn(label, text)
        self.assertIn("自動で決めた判断で、正誤に「違う」が付いた件: 0 件（対象 1 件・参考）", text)
        self.assertIn("（Jev の判断 1 件は数えていません）", text)
        self.assertIn(label, stats.week(self.out, share=True, now=now))


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

    def test_the_built_morning_brief_names_the_recorded_only_lines_with_their_rule(self):
        """R33 review fix 2: brief.build puts the rule section in the brief (rule_section alone is not the brief)."""
        rows = [{"at": (NOW - timedelta(hours=1)).isoformat(timespec="seconds"), "graph": "minutes-followup",
                 "event_kind": "meeting.item", "event_id": f"m{i}", "node": "check_line", "outcome": "decide",
                 "needs_human": False, "notify": False, "via_unsure": ["item_type"], "demoted_by": "A1",
                 "summary": f"週次定例: 件 {i}"} for i in range(6)]
        (self.out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        text, _ = brief.build(self.out, UNSURE_KIND, now=NOW)
        self.assertIn("規則で記録だけにした件\n", text)
        self.assertEqual(text.count("→ 規則 A1（記録のみ）"), 5)
        self.assertIn("ほか 1 件", text)

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
