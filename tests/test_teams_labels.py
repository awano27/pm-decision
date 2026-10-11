"""`違う N` / `違う N k` (setting learn=1): the PM's reply on the phone labels a decision, as a review. The reply never changes
the item (status, posting, execution, work); the label goes to reviews.jsonl and fixtures_user.jsonl, and labels.log.jsonl keeps
only the numbers, the verdict and the time. With learn=0 nothing reacts to it and the posts keep their exact text. Every Teams is a
fake; the model is never called."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import calibrate, config, execute, fsutil, graph, labels, notify, plan, review, stats
from kimeru.backends import JevBackend

try:
    from .test_detail_reply import SelfChat
except ImportError:
    from test_detail_reply import SelfChat

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)
GRAPH = "teams-chat-triage"
KEY = f"{GRAPH}:c-1:ask_pm"
# the route ended on an unsure question (intent), after the rule that found no incident
PATH = [{"node": "critical_incident", "edge": "no", "answer": {"matched": False}},
        {"node": "intent", "edge": "unsure", "answer": {"choice": "decision", "confidence": 0.4}}]
INTENT = list(GRAPHS["teams.chat"][0]["nodes"]["intent"]["question"]["criteria"])   # the options, in the order the question lists them
REAL_READER = ROOT / "tools" / "teams-self.ps1"
FAKE_READER = ROOT / "tests" / "fake-teams-self.ps1"


def decision(judge="Kev", n=1):
    return {"graph": GRAPH, "event_kind": "teams.chat", "event_id": f"c-{n}", "node": "ask_pm", "outcome": "advise",
            "needs_human": True, "advice": "判断の確認が必要: Sato「来週の件」", "revision": 1, "summary": "来週の件",
            "event": {"author": "Sato", "text": "来週の件、どうしますか"}, "path": PATH, "judge": {"name": judge, "model": ""},
            "memo": {}, "actions": [], "at": "2026-10-11T09:00:00+00:00"}


class Base(unittest.TestCase):
    LEARN = "1"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.out, self.state = root / "out", root / "state"
        self.out.mkdir()
        self.state.mkdir()
        self.env = mock.patch.dict(os.environ, {"KIMERU_STATE_DIR": str(self.state), "KIMERU_LEARN": self.LEARN})
        self.env.start()
        self.chat = SelfChat()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def queue(self, *rows):
        """Write the decisions and the queue the daily cycle writes, then post them to the self chat (#1, #2, ...)."""
        text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        (self.out / "decisions.jsonl").write_text(text, encoding="utf-8")
        (self.out / "queue.jsonl").write_text(text, encoding="utf-8")
        self.assertEqual(notify.notify(self.out, self.chat, send=True), list(range(1, len(rows) + 1)))

    def say(self, text):
        """One reply from the PM, then one cycle of the daily path (real=True, send=True)."""
        self.chat.say(text)
        return notify.collect(self.out, self.chat, real=True, send=True)

    def item(self):
        return notify.Approvals(self.out).data["items"]["1"]

    def set_item(self, **fields):
        ap = notify.Approvals(self.out)
        ap.data["items"]["1"].update(fields)
        ap.save()

    def reviews(self):
        return review.read_jsonl(review.reviews_path())

    def fixtures(self):
        return review.read_jsonl(review.fixtures_path())

    def log(self):
        p = self.out / "labels.log.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []

    def detail_text(self):
        return [t for t in self.chat.sent() if t.startswith("[kimeru 詳細 #1]")][-1]


class TestTheGrammar(unittest.TestCase):
    def test_the_forms_of_the_label_reply(self):
        for line, want in (("違う 3", ("3", None)), ("違う3", ("3", None)), ("違う #3", ("3", None)), ("違う　３", ("3", None)),
                           ("違う 3 2", ("3", 2)), ("違う　３　２", ("3", 2)), ("違う 12 3", ("12", 3)),
                           ("違う", None), ("違う 3 お願い", None), ("違う 3 2 3", None), ("この判断は違う 3", None), ("違う 3。", None)):
            self.assertEqual(notify.parse_label(line), want, line)

    def test_a_label_is_read_as_its_number_and_is_never_taken_for_a_reply(self):
        self.assertEqual(notify._reply_number("違う 12 3"), "12")
        self.assertIsNone(notify.parse_reply("違う 3"))
        self.assertIsNone(notify.parse_redo_k("違う 3 2"))

    def test_a_label_is_fresh_after_its_post_and_not_after_a_test_post_or_an_unseen_post(self):
        self.assertEqual(notify.fresh_entries({"timeline": ["P:1", "R:違う 1 2"]}), [("違う 1 2", 0)])
        self.assertEqual(notify.fresh_entries({"timeline": ["T:1", "R:違う 1"]}), [])
        self.assertEqual(notify.fresh_entries({"timeline": ["R:違う 1"]}), [])

    def test_learning_is_off_by_default_and_is_a_yes_or_no_setting(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("KIMERU_LEARN", None)
            self.assertEqual(config.value("learn"), "0")
        self.assertEqual(config.SETTINGS["learn"], ("KIMERU_LEARN", "0"))
        self.assertEqual(config.CHOICES["learn"], ("0", "1"))


class TestWrongFinalResult(Base):
    def test_the_reply_labels_the_decision_and_changes_nothing_of_the_item(self):
        self.queue(decision())
        before = dict(self.item())
        self.assertEqual(self.say("違う 1"), [])
        self.assertEqual([(r["key"], r["verdict"], r["source"]) for r in self.reviews()], [(KEY, "wrong", "teams")])
        self.assertEqual(self.fixtures(), [])                                          # a final result has no question to fix
        after = self.item()
        for field in ("status", "posted", "posted_at", "exec", "record", "toasted"):
            self.assertEqual(after.get(field), before.get(field), field)
        self.assertFalse((self.out / "executions.jsonl").exists())
        self.assertEqual(self.log()[0]["verdict"], "wrong")
        self.assertEqual(set(self.log()[0]) - {"at"}, {"key", "verdict", "k"})          # the numbers, the verdict; no words

    def test_the_same_reply_on_the_next_cycle_is_recorded_once(self):
        self.queue(decision())
        self.say("違う 1")
        self.say("違う 1")                                                              # still on the screen: read again
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(len(self.log()), 1)

    def test_the_labeled_decision_is_not_asked_again_by_kimeru_review(self):
        self.queue(decision())
        self.assertEqual([review.decision_key(r) for r in review.pending(self.out)], [KEY])
        self.say("違う 1")
        self.assertEqual(review.pending(self.out), [])

    def test_the_stats_leave_jevs_wrong_answers_out(self):
        self.queue(decision(judge=JevBackend.NAME))
        self.say("違う 1")
        self.assertEqual(len(self.reviews()), 1)
        s = stats.collect(self.out)
        self.assertEqual(s["reviewed_wrong"], 0)
        self.assertNotIn("最終結果が違う", stats.render(s))
        self.assertNotIn("最終結果が違う", stats.render(s, share=True))

    def test_a_kev_wrong_answer_counts_in_the_stats(self):
        self.queue(decision(judge="Kev"))
        self.say("違う 1")
        self.assertEqual(stats.collect(self.out)["reviewed_wrong"], 1)

    def test_an_approved_item_is_labeled_but_neither_executed_nor_written(self):
        self.queue(decision(n=1), decision(n=2))                                        # #2 keeps waiting: the cycle reads the chat
        self.say("OK 1")
        self.assertEqual(self.item()["status"], "approved")
        with mock.patch.object(execute, "run_approved", side_effect=AssertionError("executed")) as run, \
                mock.patch.object(execute, "redo", side_effect=AssertionError("redone")) as redo:
            self.assertEqual(self.say("違う 1"), [])
        run.assert_not_called()
        redo.assert_not_called()
        self.assertEqual(self.item()["status"], "approved")
        self.assertEqual([r["key"] for r in self.reviews()], [KEY])

    def test_when_nothing_waits_teams_is_not_read(self):
        self.queue(decision())
        self.say("OK 1")
        self.chat.read = mock.Mock(side_effect=AssertionError("read"))
        self.chat.say("違う 1")
        self.assertEqual(notify.collect(self.out, self.chat, real=True, send=True), [])
        self.chat.read.assert_not_called()


class TestTheQuestionAndItsOption(Base):
    def test_the_detail_post_lists_the_unsure_question_and_saves_it(self):
        self.queue(decision())
        self.say("詳細 1")
        text = self.detail_text()
        lines = text.splitlines()
        self.assertTrue(lines[-1].startswith("返信: OK 1"))                               # the section sits above the reply line
        start = next(i for i, l in enumerate(lines) if l.startswith("迷った質問"))
        self.assertLessEqual(len(lines[start:-1]), 5)
        self.assertIn("2. Asks the project manager to work out", "\n".join(lines[start:-1]))
        self.assertEqual(self.item()["detail_question"], {"node": "intent", "options": INTENT, "revision": 1})
        for word in ("unsure", "matched", "intent", "ask_pm", "critical_incident", "revision", "graph", "confidence"):
            self.assertNotIn(word, text)

    def test_the_numbered_option_becomes_the_expected_answer_and_calibration_reads_it(self):
        self.queue(decision())
        self.say("詳細 1")
        self.say("違う 1 2")
        rows = self.fixtures()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["expect"], {"intent": "requirements"})
        self.assertEqual(rows[0]["id"], "user-" + hashlib.sha1(KEY.encode("utf-8")).hexdigest()[:8])
        self.assertEqual([(r["verdict"], r.get("node")) for r in self.reviews()], [("no", "intent")])
        self.assertEqual((self.log()[-1]["verdict"], self.log()[-1]["k"]), ("no", 2))
        qs = calibrate.labeled_questions(self.out, GRAPHS)
        self.assertEqual([(q["graph"], q["node_id"], q["label"], q["key"]) for q in qs], [(GRAPH, "intent", "requirements", KEY)])
        self.assertEqual(self.item()["status"], "pending")

    def test_the_same_label_on_the_next_cycle_is_recorded_once(self):
        self.queue(decision())
        self.say("詳細 1")
        self.say("違う 1 2")
        self.say("違う 1 2")
        self.assertEqual(len(self.fixtures()), 1)
        self.assertEqual(len(self.reviews()), 1)

    def test_a_number_out_of_range_records_the_reason_only(self):
        self.queue(decision())
        self.say("詳細 1")
        self.say("違う 1 9")
        self.assertEqual((self.fixtures(), self.reviews()), ([], []))
        self.assertEqual([(r["verdict"], r["reason"]) for r in self.log()], [("refused", "out_of_range")])
        self.say("違う 1 9")
        self.assertEqual(len(self.log()), 1)

    def test_a_label_without_a_detail_post_records_the_reason_only(self):
        self.queue(decision())
        self.say("違う 1 2")
        self.assertEqual((self.fixtures(), self.reviews()), ([], []))
        self.assertEqual([(r["verdict"], r["reason"]) for r in self.log()], [("refused", "no_detail")])
        self.assertNotIn("detail_question", self.item())

    def test_a_changed_revision_makes_the_option_stale(self):
        self.queue(decision())
        self.say("詳細 1")
        self.set_item(revision=2)
        self.say("違う 1 2")
        self.assertEqual((self.fixtures(), self.reviews()), ([], []))
        self.assertEqual(self.log()[-1]["reason"], "revision_changed")

    def test_a_route_without_an_unsure_question_offers_no_option(self):
        rec = decision()
        rec["path"] = [{"node": "critical_incident", "edge": "no", "answer": {"matched": False}}]
        self.queue(rec)
        self.say("詳細 1")
        self.assertNotIn("detail_question", self.item())
        self.assertNotIn("迷った質問", self.detail_text())
        self.say("違う 1 1")
        self.assertEqual(self.fixtures(), [])

    def test_a_decision_without_its_event_is_not_labeled(self):
        rec = decision()
        rec.pop("event")
        self.queue(rec)
        self.say("違う 1")
        self.assertEqual(self.reviews(), [])
        self.assertEqual(self.log()[-1]["reason"], "no_record")

    def test_approval_drops_the_question_and_the_label_is_still_once(self):
        self.queue(decision(n=1), decision(n=2))
        self.say("詳細 1")
        self.say("OK 1")
        self.assertNotIn("detail_question", self.item())
        self.say("違う 1 2")                                                            # approved: the option is gone, so this is a reason
        self.assertEqual([r["reason"] for r in self.log()], ["no_detail"])
        self.say("違う 1")
        self.say("違う 1")
        self.assertEqual([r["key"] for r in self.reviews()], [KEY])
        self.assertEqual(self.fixtures(), [])



class TestWithLearningOff(Base):
    LEARN = "0"

    def test_the_reply_does_nothing_and_the_item_is_unchanged(self):
        self.queue(decision())
        before = dict(self.item())
        self.say("違う 1")
        self.say("違う 1 2")
        self.assertEqual((self.reviews(), self.fixtures()), ([], []))
        self.assertFalse((self.out / "labels.log.jsonl").exists())
        self.assertEqual(self.item()["status"], "pending")
        self.assertEqual(self.item().get("record"), before.get("record"))

    def test_the_detail_post_is_the_same_text_and_keeps_no_question(self):
        self.queue(decision())
        self.say("詳細 1")
        self.assertEqual(self.detail_text(), notify.format_detail(1, decision(), None))
        self.assertNotIn("detail_question", self.item())


class TestNothingIsWrittenOnTheOtherPaths(Base):
    """A label or a detail request is acted on only by the daily and approvals paths (real=True), only when the lock is free, and a
    reply under a test post (`[kimeru 試験 #N]`) is never read by the real reader."""

    def assert_nothing_written(self):
        self.assertEqual(self.reviews(), [])
        self.assertEqual(self.fixtures(), [])
        self.assertEqual(self.log(), [])
        self.assertNotIn("labeled", self.item())
        self.assertNotIn("detail_question", self.item())

    def say_replies(self):
        self.chat.say("詳細 1")
        self.chat.say("違う 1 2")
        self.chat.say("違う 1")

    def test_a_run_that_is_not_real_reads_the_detail_and_the_labels_and_writes_nothing(self):
        self.queue(decision())
        self.say_replies()
        notify.collect(self.out, self.chat, real=False, send=True)
        self.assertTrue([t for t in self.chat.sent() if t.startswith("[kimeru 詳細 #1]")])   # the reply was read: the detail went out
        self.assert_nothing_written()

    def test_a_label_while_another_run_holds_the_lock_writes_nothing_and_posts_no_detail(self):
        self.queue(decision())
        self.say_replies()
        with fsutil.exclusive(notify.lock_path(self.out)) as got:
            self.assertTrue(got)
            res = notify.collect(self.out, self.chat, real=True, send=True)
        self.assertTrue(res.busy)
        self.assertFalse([t for t in self.chat.sent() if t.startswith("[kimeru 詳細")])
        self.assert_nothing_written()

    def test_a_label_under_a_test_post_of_the_same_number_is_not_read(self):
        self.queue(decision())
        self.chat.timeline.append("T:1")          # "[kimeru 試験 #1]" as tools/check.ps1 posts it: the replies under it are its own
        self.chat.say("違う 1")
        self.chat.say("違う 1 2")
        notify.collect(self.out, self.chat, real=True, send=True)
        self.assert_nothing_written()


class TestTheDetailSection(unittest.TestCase):
    def item(self):
        rec = decision()
        return {"record": rec, "key": KEY, "posted": True, "status": "pending", "revision": 1}

    def test_the_section_is_at_most_five_lines_and_offers_every_option_of_a_six_option_question(self):
        it = self.item()
        text = labels.attach(it, 1, notify.format_detail(1, it["record"], None))
        self.assertEqual(it["detail_question"], {"node": "intent", "options": INTENT, "revision": 1})
        lines = text.splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("迷った質問"))
        section = lines[start:-1]
        self.assertLessEqual(len(section), 5)
        joined = "\n".join(section)
        for n in range(1, len(INTENT) + 1):
            self.assertIn(f"{n}. ", joined)
        self.assertTrue(lines[-1].startswith("返信: OK 1"))

    def test_a_question_with_more_than_eight_options_offers_the_first_eight(self):
        opts = [(str(i), f"option text {i}") for i in range(12)]
        section = labels.section_lines(1, opts)
        self.assertLessEqual(len(section), 5)
        joined = "\n".join(section)
        self.assertIn("8. option text 7", joined)
        self.assertNotIn("9. ", joined)

    def test_no_route_question_means_no_section_and_no_saved_question(self):
        it = self.item()
        it["record"]["path"] = []
        text = notify.format_detail(1, it["record"], None)
        self.assertEqual(labels.attach(it, 1, text), text)
        self.assertNotIn("detail_question", it)


class TestTheSettingDefaultIsByteIdentical(unittest.TestCase):
    """The texts of the default setting, as HEAD 537fc3a wrote them (the golden strings were taken before this change)."""

    REC = {"graph": "teams-chat-triage", "event_kind": "teams.chat", "event_id": "c-9", "node": "ask_pm", "outcome": "advise",
           "needs_human": True, "advice": "判断の確認が必要: Sato「来週の件、どうしますか」", "revision": 1,
           "summary": "来週の件、どうしますか", "event": {"author": "Sato", "text": "来週の件、どうしますか"},
           "path": [{"node": "critical_incident", "edge": "no", "answer": {"matched": False}},
                    {"node": "intent", "edge": "unsure", "answer": {"choice": "decision", "confidence": 0.4}}],
           "memo": {}, "actions": [{"type": "teams.reply", "chat_id": "19:x", "text": "確認して返信します。", "drafted_by": "fake"}]}

    def test_the_approval_detail_toast_and_ack_texts_did_not_change(self):
        self.assertEqual(notify.format_post(3, self.REC),
                         "[kimeru #3] Satoさん: 来週の件、どうしますか\n→ 判断の確認が必要: Sato「来週の件、どうしますか」\n"
                         "案: 確認して返信します。\nOK 3 / NG 3 / 修正 3 <点> / 詳細 3")
        self.assertEqual(notify.format_detail(3, self.REC),
                         "[kimeru 詳細 #3] 判断が必要（teams-chat-triage） / 改訂 1\n判断: 判断の確認が必要: Sato「来週の件、どうしますか」\n"
                         "次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n"
                         "不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n"
                         "OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）\nteams.chat #c-9\n"
                         "内容: 判断の確認が必要: Sato「来週の件、どうしますか」\n承認の対象: teams.reply（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n"
                         "返信の下書き（fake）:\n確認して返信します。\n返信: OK 3 / NG 3 / 保留 3 / 修正 3 <直してほしい点>")
        self.assertEqual(notify.toast_text(tempfile.mkdtemp(), [3], [2], 1),
                         ("kimeru: 確認待ち 1 件 / 自動決定の通知 [2] 件 / 投稿できていない確認待ち 1 件", "#3"))
        self.assertEqual(notify.ack_text([{"id": 3, "status": "detail"}]), ("kimeru: 返事を受け付けました", "#3 詳細を返信"))


class TestTheReadersHandTheLabelBack(unittest.TestCase):
    def test_the_real_reader_and_the_fake_reader_both_carry_the_label_grammar(self):
        for path in (REAL_READER, FAKE_READER):
            text = path.read_text(encoding="utf-8-sig")
            self.assertIn(r"^違う\s*#?(\d+)(?:\s+(\d+))?$", text, path.name)


if __name__ == "__main__":
    unittest.main()
