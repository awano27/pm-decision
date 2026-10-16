"""R26 (setting spot_check_per_day, default 0): after the morning brief, up to K of the day's automatic decisions are posted to the
self chat as cards `[kimeru 確認 S<k>]`, and the PM's `正しい S<k>` / `違う S<k>` replies label them (reviews.jsonl, the rows `kimeru
review` writes). The cards live in out/spot.json, never in approvals.json: a card is approved, executed and counted by nothing, and
a reply posts and executes nothing. The first class pins what the texts and the cycle look like at the default (0). Every Teams is a
fake; the model is never called."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import random
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru import brief as brief_mod, config, daily, notify, push, review
from kimeru.backends import StubBackend
from kimeru.cli import process
from tests.test_daily import GRAPHS, PBS, FakeTeams, one_on_one
from tests.test_notify import REC, FakeBridge

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fake-teams-self.ps1"
GRAPH = "teams-chat-triage"
NOW = datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc)   # the spot code takes an aware now, or the naive local one the cycle passes
YESTERDAY = "2026-10-11T16:00:00+00:00"                   # nine hours before NOW: inside the last 24 hours
RULE_ONLY = [{"node": "critical_incident", "edge": "no", "answer": {"matched": False}}]
ASKED = RULE_ONLY + [{"node": "intent", "edge": "decision", "answer": {"choice": "decision", "confidence": 0.9}}]
CARD_FIRST = "[kimeru 確認 S{k}] 自動で決めた件を確かめてください"

# the default texts and the cycle's posts, as the code wrote them before R26 (taken from the worktree before the change)
GOLDEN = {'format_post': '[kimeru #3] ADO #4812\n→ 優先度を判定できないチケット #4812\nOK 3 / NG 3 / 詳細 3',
 'format_detail': '[kimeru 詳細 #3] 判断が必要（workitem-intake） / 改訂 1\n'
                  '判断: 優先度を判定できないチケット #4812\n'
                  '次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                  '不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                  'OK の効果: 記録のみ\n'
                  'ado.workitem.created #4812\n'
                  '対象: #4812\n'
                  '内容: 優先度を判定できないチケット #4812\n'
                  '承認の対象: ado.update（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n'
                  '返信: OK 3 / NG 3 / 保留 3',
 'push_message': 'kimeru: 確認待ち 2 件（#1, #2） / 自動決定の通知 1 件 / 投稿できていない確認待ち 1 件。Teams の自分とのチャットを確認してください',
 'toast': ['kimeru: 確認待ち 2 件 / 自動決定の通知 1 件 / 投稿できていない確認待ち 1 件', '#1, #2'],
 'toast_sitting': ['kimeru: 確認待ち 1 件（まとめ）', '#1'],
 'cycle_posts': [['[kimeru #1] Satoさん: これは何ですか\n'
                  '→ 意図が判定できないチャット: Sato「これは何ですか」\n'
                  '案: 受領しました。内容を確認して返信します。\n'
                  'OK 1 / NG 1 / 詳細 1',
                  True],
                 ['[kimeru brief 2026-09-28] 今日の進め方\n'
                  '1. 確認待ち #1: 意図が判定できないチャット: Sato「これは何ですか」\n'
                  '確認待ち 1 件（OK 番号 / NG 番号 / 詳細 番号 で返信）',
                  True],
                 ['[kimeru 送信用 #1]\n受領しました。内容を確認して返信します。', True]],
 'cycle_reports': [{'pull_teams': 0, 'judge': 0, 'notify': [], 'approvals': []},
                   {'pull_teams': 1, 'judge': 1, 'notify': [1], 'approvals': [], 'brief': 1},
                   {'pull_teams': 0, 'judge': 0, 'notify': [], 'approvals': ['#1:approved']}],
 'state_files': ['approvals.json',
                 'approvals.json.bak',
                 'approvals.log.jsonl',
                 'brief_history.jsonl',
                 'daily.log.jsonl',
                 'daily_state.json',
                 'decisions.jsonl',
                 'processed.txt',
                 'pull_state.json',
                 'pull_state.json.bak',
                 'queue.jsonl']}


def decided(n=1, judge="Kev", path=ASKED, at=YESTERDAY, **fields):
    """An automatic decision the way daily writes it: outcome decide, nothing asked of the PM."""
    rec = {"graph": GRAPH, "event_kind": "teams.chat", "event_id": f"c-{n}", "node": "log_fyi", "outcome": "decide",
           "needs_human": False, "notify": False, "advice": "", "summary": f"来週の件 {n}",
           "event": {"author": "Sato", "text": f"来週の件 {n}"}, "path": path, "judge": {"name": judge, "model": ""},
           "actions": [], "at": at}
    rec.update(fields)
    return rec


def write_rows(out, rows):
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


class SpotChat(FakeBridge):
    """A self chat that is a timeline, as tools/teams-self.ps1 reads it: a card's first line gives "S:k", a post "P:n", and a reply
    line "R:<line>" (the reader's classes, not the screen)."""

    def __init__(self):
        super().__init__()
        self.timeline = []

    def post(self, text, send):
        r = super().post(text, send)
        first = text.split("\n", 1)[0]
        card, post = re.match(r"^\[kimeru 確認 S(\d+)\]", first), re.match(r"^\[kimeru #(\d+)\]", first)
        if send and card:
            self.timeline.append(f"S:{card.group(1)}")
        elif send and post:
            self.timeline.append(f"P:{post.group(1)}")
        return r

    def read(self):
        return {"ok": True, "timeline": list(self.timeline)}

    def say(self, text):
        self.timeline.append("R:" + text)

    def cards(self):
        return [t for t, s in self.posts if s and t.startswith("[kimeru 確認 S")]


class Base(unittest.TestCase):
    PER_DAY = "2"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.out, self.statedir = root / "out", root / "state"
        self.out.mkdir()
        self.statedir.mkdir()
        self.env = mock.patch.dict(os.environ, {"KIMERU_STATE_DIR": str(self.statedir), "KIMERU_SPOT_CHECK_PER_DAY": self.PER_DAY})
        self.env.start()
        self.chat = SpotChat()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def step(self, now=NOW, brief_done=True, real=True, send=True, rng=None):
        from kimeru import spot
        return spot.step(self.out, self.chat, now=now, brief_done=brief_done, real=real, send=send, rng=rng or random.Random(7))

    def reply(self, text, now=NOW, real=True, send=True):
        self.chat.say(text)
        return notify.collect(self.out, self.chat, real=real, send=send, now=now)

    def state(self):
        p = self.out / "spot.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def reviews(self):
        return review.read_jsonl(review.reviews_path())

    def fixtures(self):
        return review.read_jsonl(review.fixtures_path())


class TestTheDefaultChangesNothing(unittest.TestCase):
    """At the setting 0 (or unset) the texts are these, byte for byte. The golden strings were taken before R26 existed."""

    def test_the_setting_is_zero_by_default_and_only_zero_to_five_are_read(self):
        self.assertEqual(config.SETTINGS["spot_check_per_day"], ("KIMERU_SPOT_CHECK_PER_DAY", "0"))
        self.assertEqual(config.CHOICES["spot_check_per_day"], ("0", "1", "2", "3", "4", "5"))
        self.assertIn("spot_check_per_day", config.LOGGABLE)
        self.assertIn("spot_check_per_day", config.SHARE_VALUES)
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("KIMERU_SPOT_CHECK_PER_DAY", None)
            self.assertEqual(config.value("spot_check_per_day"), "0")
        for raw, want in (("5", "5"), (" 3 ", "3"), ("6", "0"), ("-1", "0"), ("x", "0")):
            with mock.patch.dict(os.environ, {"KIMERU_SPOT_CHECK_PER_DAY": raw}):
                self.assertEqual(config.value("spot_check_per_day"), want, raw)

    def test_the_post_the_detail_the_toast_and_the_push_texts_are_as_before(self):
        self.assertEqual(notify.format_post(3, REC), GOLDEN["format_post"])
        self.assertEqual(notify.format_detail(3, REC), GOLDEN["format_detail"])
        self.assertEqual(push.message([1, 2], 1, 1), GOLDEN["push_message"])
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            out.mkdir()
            self.assertEqual(list(notify.toast_text(out, [1, 2], 1, 1, sitting=False)), GOLDEN["toast"])
            self.assertEqual(list(notify.toast_text(out, [1], 0, 0, sitting=True)), GOLDEN["toast_sitting"])

    def test_a_daily_cycle_at_the_setting_zero_or_unset_posts_the_same_texts_and_writes_no_card_file(self):
        for raw in (None, "0"):
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {}):
                os.environ.pop("KIMERU_SPOT_CHECK_PER_DAY", None)
                if raw is not None:
                    os.environ["KIMERU_SPOT_CHECK_PER_DAY"] = raw
                out, inbox, t = Path(d) / "out", Path(d) / "inbox", FakeTeams()
                run = lambda now: daily.cycle(out, inbox, GRAPHS, StubBackend(), PBS, process, bridge=t, send=True, now=now)
                t.chat_list = [one_on_one("おはようございます", "8:00")]
                reports = [run(datetime(2026, 9, 28, 7, 0))]
                t.chat_list = [one_on_one("これは何ですか", "8:05")]
                reports.append(run(datetime(2026, 9, 28, 8, 10)))
                t.timeline.append("R:OK 1")
                reports.append(run(datetime(2026, 9, 28, 8, 15)))
                self.assertEqual([list(p) for p in t.posts], GOLDEN["cycle_posts"], raw)
                self.assertEqual([{k: v for k, v in r.items() if k in ("notify", "brief", "approvals", "judge", "pull_teams")}
                                  for r in reports], GOLDEN["cycle_reports"], raw)
                self.assertEqual(sorted(p.name for p in out.iterdir()), GOLDEN["state_files"], raw)
                self.assertFalse(any("spot" in r for r in reports), raw)


class TestWhichDecisionsAreChosen(Base):
    def candidates(self, now=NOW):
        from kimeru import spot
        return [r["event_id"] for r in spot.candidates(self.out, now)]

    def test_only_an_automatic_decision_that_a_model_answered_is_a_candidate(self):
        write_rows(self.out, [decided(1), decided(2, path=RULE_ONLY), decided(3, judge="StubBackend"),
                              decided(4, needs_human=True), decided(5, notify=True), decided(6, outcome="advise"),
                              decided(7, at="2026-10-10T20:00:00+00:00"), {k: v for k, v in decided(8).items() if k != "event"},
                              decided(9, judge="Jev"), decided(10, at="2026-10-12T05:00:00+00:00")])
        self.assertEqual(sorted(self.candidates()), ["c-1", "c-9"])

    def test_a_decision_with_a_label_or_whose_newest_judgment_is_a_rule_is_not_a_candidate(self):
        write_rows(self.out, [decided(1), decided(2), decided(2, at="2026-10-11T18:00:00+00:00", path=RULE_ONLY)])
        review._append(review.reviews_path(), {"key": f"{GRAPH}:c-1:log_fyi", "verdict": "yes", "at": YESTERDAY, "judge": "Kev"})
        self.assertEqual(self.candidates(), [])

    def test_at_most_the_setting_is_chosen_and_every_candidate_can_be_chosen(self):
        from kimeru import spot
        write_rows(self.out, [decided(n) for n in range(1, 9)])
        for seed in range(60):
            got = spot.choose(self.out, NOW, 2, random.Random(seed))
            self.assertEqual(len({r["event_id"] for r in got}), 2)
        seen = set()
        for seed in range(200):
            seen |= {r["event_id"] for r in spot.choose(self.out, NOW, 1, random.Random(seed))}
        self.assertEqual(seen, {f"c-{n}" for n in range(1, 9)})
        self.assertEqual(spot.choose(self.out, NOW, 0, random.Random(1)), [])

    def test_a_day_gets_its_cards_once_and_never_more_than_five(self):
        write_rows(self.out, [decided(n) for n in range(1, 9)])
        with mock.patch.dict(os.environ, {"KIMERU_SPOT_CHECK_PER_DAY": "9"}):   # not allowed: the setting reads as 0
            self.assertIsNone(self.step())
        with mock.patch.dict(os.environ, {"KIMERU_SPOT_CHECK_PER_DAY": "5"}):
            self.assertEqual(len(self.step()), 5)
            self.assertEqual(len(self.chat.cards()), 5)
            self.assertEqual(self.step(now=NOW + timedelta(hours=1)), [])
            self.assertEqual(len(self.chat.cards()), 5)

    def test_a_damaged_line_of_the_decisions_file_is_no_candidate_and_the_cards_go_on(self):
        damaged = [{**decided(3), "path": "abc"}, {**decided(4), "path": [1]},
                   {**decided(5), "path": [{"node": "intent", "edge": "decision", "answer": 5}]},
                   {**decided(6), "path": {"node": "intent"}}, {**decided(7), "judge": "Kev"}]
        write_rows(self.out, [None, 42, [1, 2], "text"] + damaged[:2] + [decided(1)] + damaged[2:] + [decided(2)])
        self.assertEqual(sorted(self.candidates()), ["c-1", "c-2"])
        self.assertEqual(self.step(), ["S1:posted", "S2:posted"])
        self.assertEqual(len(self.chat.cards()), 2)


class TestTheCardsAfterTheBrief(Base):
    def test_no_card_is_posted_before_the_morning_brief_of_the_day(self):
        write_rows(self.out, [decided(1)])
        self.assertIsNone(self.step(brief_done=False))
        self.assertEqual(self.chat.posts, [])
        self.assertIsNone(self.state())

    def test_nothing_is_posted_unless_it_is_a_real_send(self):
        from kimeru import spot
        write_rows(self.out, [decided(1)])
        self.assertIsNone(self.step(real=False))
        self.assertIsNone(self.step(send=False))
        self.assertIsNone(spot.step(self.out, self.chat, now=NOW, brief_done=True))   # the defaults are no real send either
        self.assertEqual(self.chat.posts, [])
        self.assertIsNone(self.state())

    def test_a_card_waits_while_the_approvals_lock_is_held_by_another_step(self):
        from kimeru import fsutil
        write_rows(self.out, [decided(1)])
        with fsutil.exclusive(notify.lock_path(self.out)) as held:
            self.assertTrue(held)
            self.assertIsNone(self.step())
        self.assertEqual(self.chat.posts, [])
        self.assertIsNone(self.state())
        self.assertEqual(self.step(), ["S1:posted"])

    def test_the_counts_of_the_digest_push_week_brief_work_and_trial_are_the_same_with_the_cards(self):
        import contextlib
        import io
        from kimeru import autorun, cli, stats, work
        write_rows(self.out, [decided(1), decided(2), decided(21, needs_human=True), decided(22, needs_human=True),
                              decided(23, needs_human=True)])
        ap = notify.Approvals(self.out)
        for n in (21, 22, 23):   # #1 waits on the screen, #2 is not posted yet (unposted), #3 is approved (work)
            ap.add(f"{GRAPH}:c-{n}:log_fyi", decided(n, needs_human=True))
        ap.data["items"]["1"].update({"posted": True})
        ap.data["items"]["3"].update({"posted": True, "status": "approved"})
        ap.save()

        def counts():
            with contextlib.redirect_stdout(io.StringIO()) as shown:
                cli.digest(self.out)
            items = notify.Approvals(self.out).data["items"]
            waiting = sorted(int(n) for n, it in items.items() if it["posted"] and it["status"] == "pending")
            return {"approvals": (self.out / "approvals.json").read_bytes(), "digest": shown.getvalue(),
                    "push": push.message(waiting, 0, notify.unposted_count(self.out)),
                    "week": stats.weekly_lines(self.out, now=NOW), "work": work.list_work(self.out),
                    "trial": autorun.build_report(self.out, now=datetime(2026, 10, 12, 10, 0)),
                    "brief": brief_mod.build(self.out, StubBackend(), now=NOW)[0]}

        before = counts()
        self.assertEqual([r["state"] for r in before["work"]], ["Unknown"])   # the approved item is in the work list
        self.assertIn("確認待ち 1 件", before["push"])
        self.step()
        self.step(now=NOW + timedelta(minutes=5))
        self.assertEqual(len(self.chat.cards()), 2)
        self.assertEqual(counts(), before)
        self.assertFalse((self.out / "executions.jsonl").exists())

    def test_the_card_files_keep_no_message_body_and_the_reply_leaves_none_either(self):
        body = "ZZ-BODY-7731 来週の件"
        write_rows(self.out, [decided(1, path=ASKED, event={"author": "Sato", "text": body}, summary=body)])
        self.step(rng=random.Random(3))
        self.reply("正しい S1")
        self.assertIn("ZZ-BODY-7731", self.chat.cards()[0])   # the control: the card itself carries the body
        for name in ("spot.json", "labels.log.jsonl"):
            self.assertNotIn("ZZ-BODY-7731", (self.out / name).read_text(encoding="utf-8"), name)
        self.assertEqual([r["verdict"] for r in self.reviews()], ["yes"])

    def test_a_card_has_its_own_prefix_and_names_no_hash_number(self):
        write_rows(self.out, [decided(1)])
        self.step(rng=random.Random(1))
        (text,) = self.chat.cards()
        self.assertEqual(text.split("\n")[0], CARD_FIRST.format(k=1))
        self.assertNotIn("#", text)
        self.assertEqual(text.split("\n")[-1], "返信: 正しい S1 / 違う S1")
        self.assertEqual(self.chat.timeline, ["S:1"])

    def test_a_failed_post_is_tried_again_on_the_next_cycle(self):
        write_rows(self.out, [decided(1)])
        refused = {"ok": True, "typed": True, "sent": False, "readback": {"matched": False, "message_id": ""}}
        with mock.patch.object(self.chat, "post", return_value=refused):
            self.assertEqual(self.step(), ["S1:not_sent"])
        self.assertIsNone(self.state()["items"][0]["posted_at"])
        self.assertEqual(self.step(now=NOW + timedelta(minutes=5)), ["S1:posted"])
        self.assertEqual(len(self.chat.cards()), 1)

    def test_an_unknown_delivery_is_not_posted_twice(self):
        write_rows(self.out, [decided(1)])
        with mock.patch.object(self.chat, "post", side_effect=RuntimeError("the bridge lost the reply")):
            self.assertEqual(self.step(), ["S1:unknown"])
        self.assertEqual(self.step(now=NOW + timedelta(minutes=5)), [])
        self.assertEqual(self.chat.cards(), [])
        self.assertTrue(self.state()["items"][0]["unknown"])

    def test_the_cards_leave_the_approvals_the_brief_and_the_decisions_as_they_were(self):
        write_rows(self.out, [decided(1), decided(2)])
        decisions = (self.out / "decisions.jsonl").read_bytes()
        brief_before = brief_mod.build(self.out, StubBackend(), now=NOW)[0]
        self.step()
        self.step(now=NOW + timedelta(minutes=5))
        self.assertEqual(len(self.chat.cards()), 2)
        self.assertFalse((self.out / "approvals.json").exists())
        self.assertFalse((self.out / "executions.jsonl").exists())
        self.assertEqual((self.out / "decisions.jsonl").read_bytes(), decisions)
        self.assertEqual(brief_mod.build(self.out, StubBackend(), now=NOW)[0], brief_before)

    def test_a_daily_cycle_posts_the_brief_and_then_the_card_once_a_day(self):
        local = datetime(2026, 9, 28, 8, 10)                     # naive, as the daily cycle gives it
        at = (local.astimezone() - timedelta(hours=2)).isoformat(timespec="seconds")
        write_rows(self.out, [decided(1, at=at)])
        t = FakeTeams()
        inbox = self.out.parent / "inbox"
        run = lambda now: daily.cycle(self.out, inbox, GRAPHS, StubBackend(), PBS, process, bridge=t, send=True, now=now, real=True)
        first = run(local)
        posts = [p for p, _ in t.posts]
        self.assertTrue(posts[0].startswith("[kimeru brief 2026-09-28]"), posts)
        self.assertEqual([p.split("\n")[0] for p in posts[1:]], [CARD_FIRST.format(k=1)])
        self.assertEqual(first["spot"], ["S1:posted"])
        second = run(local + timedelta(minutes=5))
        self.assertEqual(len(t.posts), len(posts))
        self.assertEqual(second["spot"], [])

    def test_the_cards_are_off_in_a_test_run(self):
        write_rows(self.out, [decided(1)])
        with mock.patch.dict(os.environ, {"KIMERU_TEST_MARK": "1"}):
            self.assertIsNone(self.step())
        self.assertEqual(self.chat.posts, [])


class TestACardIsNoCase(Base):
    PER_DAY = "1"

    def test_a_follow_up_of_the_same_chat_does_not_join_a_card(self):
        from kimeru import cli
        event = {"kind": "teams.chat", "chat_id": "19:c-1", "author": "Sato", "title": "Sato"}
        write_rows(self.out, [decided(1, path=ASKED, event={**event, "text": "来週の件 1"})])
        self.step(rng=random.Random(4))
        before = (self.out / "spot.json").read_bytes()
        followup = {**event, "id": "m-2", "text": "追加で一点だけ"}
        self.assertFalse(cli._merge_into_pending(self.out, followup, {"notify": False, "event": {}}))
        self.assertEqual((self.out / "spot.json").read_bytes(), before)
        self.assertFalse((self.out / "merged.jsonl").exists())
        self.assertFalse((self.out / "approvals.json").exists())


class TestTheRepliesLabelTheDecision(Base):
    PER_DAY = "1"

    def setUp(self):
        super().setUp()
        write_rows(self.out, [decided(1, path=ASKED)])
        self.step(rng=random.Random(3))

    def test_a_correct_reply_labels_the_decision_yes_with_its_example(self):
        self.reply("正しい S1")
        self.assertEqual([(r["key"], r["verdict"], r["source"]) for r in self.reviews()], [(f"{GRAPH}:c-1:log_fyi", "yes", "teams")])
        self.assertEqual([f["expect"] for f in self.fixtures()], [{"intent": "decision"}])
        self.assertEqual(self.state()["items"][0]["answer"]["verdict"], "yes")

    def test_a_wrong_reply_labels_the_final_result_wrong_and_adds_no_example(self):
        self.reply("違う S1")
        self.assertEqual([(r["key"], r["verdict"], r["source"]) for r in self.reviews()], [(f"{GRAPH}:c-1:log_fyi", "wrong", "teams")])
        self.assertEqual(self.fixtures(), [])

    def test_the_first_answer_counts_and_a_later_one_on_the_screen_is_not_recorded_again(self):
        self.reply("正しい S1")
        self.reply("正しい S1", now=NOW + timedelta(minutes=2))
        self.reply("違う S1", now=NOW + timedelta(minutes=4))
        self.assertEqual([r["verdict"] for r in self.reviews()], ["yes"])
        self.assertEqual(len(self.fixtures()), 1)

    def test_a_reply_is_read_only_on_a_real_run_that_sends(self):
        self.chat.say("正しい S1")
        notify.collect(self.out, self.chat, real=False, send=True, now=NOW)
        notify.collect(self.out, self.chat, real=True, send=False, now=NOW)
        self.assertEqual(self.reviews(), [])
        self.assertIsNone(self.state()["items"][0]["answer"])

    def test_a_reply_above_its_card_or_with_no_card_on_the_screen_is_not_read(self):
        self.chat.timeline = ["R:正しい S1", "S:1"]
        notify.collect(self.out, self.chat, real=True, send=True, now=NOW)
        self.chat.timeline = ["R:正しい S1"]
        notify.collect(self.out, self.chat, real=True, send=True, now=NOW)
        self.assertEqual(self.reviews(), [])

    def test_a_failing_answer_is_noted_and_the_approvals_step_goes_on(self):
        self.chat.say("正しい S1")
        with mock.patch("kimeru.labels.record_spot", side_effect=RuntimeError("disk")):
            notify.collect(self.out, self.chat, real=True, send=True, now=NOW)
        self.assertIsNone(self.state()["items"][0]["answer"])
        self.assertEqual(self.reviews(), [])
        self.assertEqual([json.loads(l)["spot"] for l in (self.out / "warnings.jsonl").read_text(encoding="utf-8").splitlines()],
                         ["apply (RuntimeError)"])
        self.chat.timeline = []
        with mock.patch.object(self.chat, "read", side_effect=RuntimeError("no screen")):
            notify.collect(self.out, self.chat, real=True, send=True, now=NOW)
        self.assertEqual(json.loads(self.out.joinpath("warnings.jsonl").read_text(encoding="utf-8").splitlines()[-1])["spot"],
                         "read (RuntimeError)")
        self.chat.timeline = ["S:1", "R:正しい S1"]
        notify.collect(self.out, self.chat, real=True, send=True, now=NOW)            # the next cycle takes it
        self.assertEqual([r["verdict"] for r in self.reviews()], ["yes"])

    def test_a_card_expires_after_three_days_and_its_reply_is_ignored(self):
        self.reply("正しい S1", now=NOW + timedelta(days=4))
        self.assertEqual(self.reviews(), [])

    def test_ok_and_ng_never_answer_a_card_and_a_card_reply_is_no_approval_reply(self):
        self.reply("OK 1")
        self.reply("NG 1")
        self.reply("違う 1")
        self.assertIsNone(self.state()["items"][0]["answer"])
        self.assertEqual(self.reviews(), [])
        self.assertFalse((self.out / "approvals.json").exists())

    def test_a_reply_posts_and_executes_nothing(self):
        before = len(self.chat.posts)
        self.reply("正しい S1")
        self.assertEqual(len(self.chat.posts), before)
        self.assertFalse((self.out / "executions.jsonl").exists())
        self.assertFalse((self.out / "approvals.json").exists())

    def test_a_card_reply_never_labels_an_approval_number(self):
        with mock.patch.dict(os.environ, {"KIMERU_LEARN": "1"}):   # learn=1: `違う N` labels #N (R03), so `違う S<k>` must not
            write_rows(self.out, [decided(1, path=ASKED)] + [decided(n + 10, needs_human=True) for n in (1, 2, 3)])
            ap = notify.Approvals(self.out)
            for n in (1, 2, 3):
                ap.add(f"{GRAPH}:c-{n + 10}:log_fyi", decided(n + 10, needs_human=True))
                ap.data["items"][str(n)].update({"posted": True, "status": "pending"})
            ap.save()
            self.chat.timeline = ["S:1", "P:1", "P:2", "P:3"]   # the approval posts stand on the screen above the replies
            self.reply("違う S3")
            self.reply("違う S1")
            items = notify.Approvals(self.out).data["items"]
            self.assertTrue(all("labeled" not in items[str(n)] for n in (1, 2, 3)))
            self.assertEqual([r["key"] for r in self.reviews()], [f"{GRAPH}:c-1:log_fyi"])
            # the control, in the same setup: `違う 3` does label #3, so the label path is live and only `S` is kept out of it
            self.reply("違う 3")
            items = notify.Approvals(self.out).data["items"]
            self.assertEqual(items["3"].get("labeled"), {"revision": 1})
            self.assertEqual([(r["key"], r["verdict"]) for r in self.reviews()],
                             [(f"{GRAPH}:c-1:log_fyi", "wrong"), (f"{GRAPH}:c-13:log_fyi", "wrong")])

    def test_the_fresh_reading_treats_a_card_as_neither_an_approval_post_nor_a_reply_to_one(self):
        fe = notify.fresh_entries
        self.assertEqual(fe({"timeline": ["S:3", "R:OK 3"]}), [])
        self.assertEqual(fe({"timeline": ["P:3", "S:3", "R:OK 3"]}), [("OK 3", 0)])
        self.assertEqual(fe({"timeline": ["P:3", "S:3", "R:違う S3", "R:正しい S3"]}), [])
        self.assertIsNone(notify.parse_label("違う S3"))
        self.assertIsNone(notify.parse_reply("OK S3"))
        self.assertIsNone(notify._reply_number("正しい S3"))

    def test_a_daily_cycle_reads_the_answer_to_a_card_and_labels_it(self):
        t = FakeTeams()
        t.timeline = ["S:1", "R:違う S1"]
        inbox = self.out.parent / "inbox"
        daily.cycle(self.out, inbox, GRAPHS, StubBackend(), PBS, process, bridge=t, send=True, now=datetime(2026, 10, 12, 2, 0), real=True)
        self.assertEqual([r["verdict"] for r in self.reviews()], ["wrong"])
        self.assertEqual(self.state()["items"][0]["answer"]["verdict"], "wrong")


class TestTheSelfChatIsReadOnlyWhileACardWaits(Base):
    PER_DAY = "1"

    def test_the_self_chat_is_read_only_while_a_card_waits_for_its_answer(self):
        write_rows(self.out, [decided(1, path=ASKED)])
        reads = []
        read = self.chat.read
        self.chat.read = lambda: reads.append(1) or read()
        notify.collect(self.out, self.chat, real=True, send=True, now=NOW)
        self.assertEqual(len(reads), 0)                      # no card: Teams is not read
        self.step(rng=random.Random(3))
        notify.collect(self.out, self.chat, real=True, send=True, now=NOW + timedelta(minutes=5))
        self.assertEqual(len(reads), 1)                      # a card waits for its answer
        self.reply("正しい S1", now=NOW + timedelta(minutes=10))
        self.assertEqual(len(reads), 2)
        self.assertEqual([r["verdict"] for r in self.reviews()], ["yes"])
        notify.collect(self.out, self.chat, real=True, send=True, now=NOW + timedelta(minutes=15))
        self.assertEqual(len(reads), 2)                      # answered: not read again


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
class TestTheReadersTellACardFromAPost(unittest.TestCase):
    def test_the_fake_reader_gives_a_card_an_s_entry_and_its_reply_a_reply_line(self):
        with tempfile.TemporaryDirectory() as d:
            chat = Path(d) / "chat.json"
            card = "[kimeru 確認 S2] 自動で決めた件を確かめてください\n・Teams 来週の件\n返信: 正しい S2 / 違う S2"
            chat.write_text(json.dumps({"messages": [card, "正しい S2", "[kimeru #2] 確認\nOK 2"]}, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {"KIMERU_FAKE_CHAT": str(chat)}):
                got = notify.PowerShellBridge(script=str(FAKE)).read()
        self.assertEqual(got["timeline"], ["S:2", "R:正しい S2", "P:2"])


class TestDocs(unittest.TestCase):
    def test_the_setting_has_one_row_in_the_reference_and_the_security_note_names_the_card_file(self):
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        self.assertEqual(sum(1 for l in ref.splitlines() if l.startswith("| `KIMERU_SPOT_CHECK_PER_DAY`")), 1)
        self.assertIn("`spot.json`", (ROOT / "SECURITY.md").read_text(encoding="utf-8"))
        log = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("spot_check_per_day", log.split("\n## ", 1)[1].split("\n## ", 1)[0])


if __name__ == "__main__":
    unittest.main()
