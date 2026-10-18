"""R20 (control_words): a line of the PM's own self chat that is exactly 停止 stops the daily cycle (nothing is taken from Teams,
judged, posted, run or collected; the time-limit purges go on), and 再開 takes it up again. Only a change of that line moves the
mode: a line that is already there at the first sighting, and a line that stays, do nothing. The state is out/control.json, which
keeps a hash of the line and never its text. With control_words=0 the words mean nothing, and the posts, the brief, the toast and
the push text stay exactly as they were. Every Teams is a fake and `now` is injected."""
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

from kimeru import brief, config, control, daily, execute, fsutil, hold, notify, pull, push, sent
from kimeru.backends import StubBackend
from kimeru.cli import process
from tests.test_daily import GRAPHS, PBS, FakeTeams, one_on_one

T0 = datetime(2026, 9, 28, 7, 0)
ACK = ("[kimeru 停止中] 取り込み・判断・投稿・実行を止めました。重大な知らせも止まります。"
       "停止中に打った OK は再開後に効きます。「再開」で戻ります")
RESUME_ACK = "[kimeru 再開] 停止中の分を取り込みます"
# the texts of a day with one item, as they were before R20 (captured at the commit before it, with control_words=0)
GOLDEN_POSTS = [
    ("[kimeru #1] Satoさん: これは何ですか\n→ 意図が判定できないチャット: Sato「これは何ですか」\n"
     "案: 受領しました。内容を確認して返信します。\nOK 1 / NG 1 / 詳細 1", True),
    ("[kimeru brief 2026-09-28] 今日の進め方\n1. 確認待ち #1: 意図が判定できないチャット: Sato「これは何ですか」\n"
     "確認待ち 1 件（OK 番号 / NG 番号 / 詳細 番号 で返信）", True),
]
GOLDEN_TOASTS = [["kimeru: 確認待ち 1 件", "#1"]]
GOLDEN_REPORT = {"pull_teams": 1, "judge": 1, "notify": [1], "notices": 0, "waiting": 0, "toast": "shown",
                 "approvals": [], "brief": 1}
GOLDEN_PUSH = "kimeru: 確認待ち 2 件（#1, #2） / 自動決定の通知 1 件。Teams の自分とのチャットを確認してください"


def self_row(preview):
    return {"id": "48:notes", "kind": "self", "title": "自分", "preview": preview, "time": "8:09", "unread": False, "mention": False}


class CountingTeams(FakeTeams):
    """FakeTeams that counts what a cycle asks of Teams, and can be made to fail."""

    def __init__(self):
        super().__init__()
        self.list_calls = self.read_calls = 0
        self.fail_list = self.fail_post = False

    def chats(self):
        self.list_calls += 1
        if self.fail_list:
            raise RuntimeError("Teams is not running")
        return super().chats()

    def read(self):
        self.read_calls += 1
        return super().read()

    def post(self, text, send):
        if self.fail_post:
            raise RuntimeError("Teams did not answer")
        return super().post(text, send)

    def acks(self, prefix):
        return [t for t, _ in self.posts if t.startswith(prefix)]


class CycleCase(unittest.TestCase):
    """A state folder, a fake Teams and a recording toast. `cycle` runs one daily cycle at the time it is given."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out, self.inbox = Path(tmp.name) / "out", Path(tmp.name) / "inbox"
        self.t, self.toasts, self.judged = CountingTeams(), [], []

    def cycle(self, now, chats=None, send=True):
        if chats is not None:
            self.t.chat_list = chats

        def judge(*a, **k):   # the judge step: counted, so a cycle that judged would show
            self.judged.append(1)
            return process(*a, **k)
        return daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, judge, bridge=self.t, send=send, now=now,
                           toaster=lambda title, body: self.toasts.append((title, body)))

    def control(self):
        p = self.out / "control.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def spies(self):
        """Every step that takes, judges, posts, runs or collects, replaced by a counter."""
        spies = {}
        for obj, name in [(pull, "pull_teams"), (pull, "_drop"), (pull, "pull_ado"), (pull, "pull_alerts"), (notify, "notify"),
                          (notify, "notify_notices"), (notify, "collect"), (notify, "deliver_brief"), (push, "run"),
                          (push, "begin_cycle"), (execute, "run_approved"), (hold, "resurface"), (sent, "observe"), (brief, "build")]:
            p = mock.patch.object(obj, name)
            spies[name] = p.start()
            self.addCleanup(p.stop)
        return spies


class TestTheLineStopsAndResumes(CycleCase):
    def test_the_first_sighting_of_a_stop_line_stops_nothing(self):
        r = self.cycle(T0, [one_on_one("おはようございます", "6:58"), self_row("あなた: 停止")])
        self.assertNotIn("mode", r)
        self.assertIn("pull_teams", r)
        self.assertEqual(self.control()["mode"], "run")
        self.assertEqual(self.t.acks("[kimeru 停止中]"), [])
        self.assertEqual(self.toasts, [])

    def test_a_stop_line_makes_the_next_cycle_read_the_list_once_and_do_nothing_else(self):
        self.cycle(T0, [one_on_one("おはようございます", "6:58"), self_row("あなた: 別の話")])
        ex = self.out / "executions.jsonl"
        ex.write_text('{"id": 1}\n', encoding="utf-8")
        full = self.out / "full_text.json"
        full.write_text(json.dumps({"old": {"text": "x", "thread": [], "title": "", "at": "2020-01-01T00:00:00+00:00"}}),
                        encoding="utf-8")
        old = self.inbox / "done" / "old.json"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text("{}", encoding="utf-8")
        long_ago = (T0 - timedelta(days=60)).timestamp()
        os.utime(old, (long_ago, long_ago))
        spies, ex_before, reads = self.spies(), ex.read_bytes(), self.t.list_calls
        r = self.cycle(T0 + timedelta(minutes=5), [one_on_one("これは何ですか", "7:04"), self_row("あなた: 停止")])
        self.assertEqual(r["mode"], "stop")
        self.assertEqual(r["waiting"], 0)
        self.assertEqual(self.t.list_calls - reads, 1)
        for name, spy in spies.items():
            self.assertEqual(spy.call_count, 0, name)
        self.assertEqual(self.judged, [])
        self.assertEqual(self.t.read_calls, 0)
        self.assertEqual(getattr(self.t, "opened", []), [])
        self.assertEqual(self.t.posts[-1:], [(ACK, True)])
        self.assertEqual(ex.read_bytes(), ex_before)
        self.assertFalse(old.exists())
        self.assertNotIn("old", json.loads(full.read_text(encoding="utf-8")) if full.exists() else {})

    def test_the_stop_ack_is_posted_once_and_again_only_while_the_post_fails(self):
        self.cycle(T0, [self_row("あなた: 別の話")])
        self.t.fail_post = True
        r = self.cycle(T0 + timedelta(minutes=5), [self_row("あなた: 停止")])
        self.assertEqual(r["mode"], "stop")
        self.assertTrue(self.control()["ack_pending"])
        self.t.fail_post = False
        self.cycle(T0 + timedelta(minutes=10), [self_row("あなた: 停止")])
        self.cycle(T0 + timedelta(minutes=15), [self_row("あなた: 停止")])
        self.assertEqual(self.t.acks("[kimeru 停止中]"), [ACK])
        self.assertFalse(self.control()["ack_pending"])
        self.assertEqual(len(self.toasts), 1)

    def test_the_stop_toast_is_shown_once_at_the_stop(self):
        self.cycle(T0, [self_row("あなた: 別の話")])
        self.cycle(T0 + timedelta(minutes=5), [self_row("あなた: 停止")])
        self.cycle(T0 + timedelta(minutes=10), [self_row("あなた: 停止")])
        self.assertEqual(len(self.toasts), 1)
        self.assertIn("停止", self.toasts[0][0] + self.toasts[0][1])

    def test_the_ack_appearing_on_the_list_does_not_resume_and_a_resume_line_takes_the_work_up_again(self):
        self.cycle(T0, [one_on_one("おはようございます", "6:58"), self_row("あなた: 別の話")])
        self.cycle(T0 + timedelta(minutes=5), [one_on_one("おはようございます", "6:58"), self_row("あなた: 停止")])
        r = self.cycle(T0 + timedelta(minutes=10), [one_on_one("これは何ですか", "7:08"), self_row(ACK)])
        self.assertEqual(r["mode"], "stop")
        r = self.cycle(T0 + timedelta(minutes=20), [one_on_one("これは何ですか", "7:08"), self_row("あなた: 再開")])
        self.assertNotIn("mode", r)
        self.assertEqual(self.control()["mode"], "run")
        self.assertEqual(r["judge"], 1)                       # the message that came while stopped is taken now
        self.assertEqual(len(r["notify"]), 1)
        self.assertEqual(self.t.acks("[kimeru 再開]"), [RESUME_ACK])
        self.assertFalse(any(ch.isdigit() for ch in RESUME_ACK))   # the resume ack carries no count

    def test_a_stop_line_that_was_already_there_at_the_first_sighting_stays_harmless_while_it_stays(self):
        self.cycle(T0, [self_row("あなた: 停止")])
        for k in range(1, 4):
            r = self.cycle(T0 + timedelta(minutes=5 * k), [self_row("あなた: 停止")])
            self.assertNotIn("mode", r)
        self.assertEqual(self.t.acks("[kimeru 停止中]"), [])
        self.assertEqual(self.control()["mode"], "run")

    def test_a_colleague_line_or_a_sentence_with_the_word_does_nothing(self):
        self.cycle(T0, [self_row("あなた: 別の話"), one_on_one("おはようございます", "6:58")])
        colleague = {"id": "19:c@unq.gbl.spaces", "kind": "oneOnOne", "title": "Sato", "preview": "停止", "time": "7:01",
                     "unread": True, "mention": False}
        rows = [self_row("あなた: ECS停止の件"), self_row("あなた: 停止中です"), self_row("あなた: 停止。"),
                self_row("あなた: 停止 ご確認ください"), colleague]
        for k, row in enumerate(rows):
            r = self.cycle(T0 + timedelta(minutes=5 * (k + 1)), [row, one_on_one("おはようございます", "6:58")])
            self.assertNotIn("mode", r)
        self.assertEqual(self.control()["mode"], "run")
        self.assertEqual(self.t.acks("[kimeru 停止中]"), [])

    def test_without_a_self_row_in_the_list_nothing_is_recorded_and_nothing_stops(self):
        r = self.cycle(T0, [one_on_one("停止", "6:58")])
        self.assertNotIn("mode", r)
        self.assertFalse((self.out / "control.json").exists())

    def test_a_stopped_cycle_whose_list_cannot_be_read_reports_no_error_and_stays_stopped(self):
        self.cycle(T0, [self_row("あなた: 別の話")])
        self.cycle(T0 + timedelta(minutes=5), [self_row("あなた: 停止")])
        self.t.fail_list = True
        spies = self.spies()
        r = self.cycle(T0 + timedelta(minutes=10), [self_row("あなた: 再開")])
        self.assertEqual(r["mode"], "stop")
        self.assertFalse([k for k, v in r.items() if isinstance(v, str) and v.startswith("error")])
        self.assertEqual(self.t.read_calls, 0)
        for name, spy in spies.items():
            self.assertEqual(spy.call_count, 0, name)

    def test_while_the_control_lock_is_held_the_cycle_does_nothing_and_the_line_is_handled_next_time(self):
        self.cycle(T0, [self_row("あなた: 別の話")])
        with fsutil.exclusive(self.out / "control.lock") as got:
            self.assertTrue(got)
            r = self.cycle(T0 + timedelta(minutes=5), [self_row("あなた: 停止")])
        self.assertEqual(r["mode"], "busy")
        self.assertEqual(self.control()["mode"], "run")
        self.assertEqual(self.t.posts, [])
        r = self.cycle(T0 + timedelta(minutes=10), [self_row("あなた: 停止")])
        self.assertEqual(r["mode"], "stop")
        self.assertEqual(self.t.acks("[kimeru 停止中]"), [ACK])

    def test_a_damaged_control_file_and_its_copy_stop_the_cycle_until_the_line_resumes(self):
        self.out.mkdir(parents=True)
        (self.out / "control.json").write_text("{broken", encoding="utf-8")
        (self.out / "control.json.bak").write_text("{also broken", encoding="utf-8")
        r = self.cycle(T0, [self_row("あなた: 別の話")])
        self.assertEqual(r["mode"], "stop")
        self.assertEqual(self.t.acks("[kimeru 停止中]"), [ACK])
        r = self.cycle(T0 + timedelta(minutes=5), [self_row("あなた: 再開")])
        self.assertNotIn("mode", r)
        self.assertEqual(self.control()["mode"], "run")

    def test_a_damaged_control_file_falls_back_to_its_copy(self):
        self.out.mkdir(parents=True)
        (self.out / "control.json").write_text("{broken", encoding="utf-8")
        (self.out / "control.json.bak").write_text(json.dumps({"mode": "stop", "last_sig": "0" * 40}), encoding="utf-8")
        r = self.cycle(T0, [self_row("あなた: 別の話")])
        self.assertEqual(r["mode"], "stop")
        self.assertEqual(self.t.acks("[kimeru 停止中]"), [])

    def test_the_state_file_keeps_no_text_of_the_line(self):
        self.cycle(T0, [self_row("あなた: 別の話")])
        self.cycle(T0 + timedelta(minutes=5), [self_row("あなた: 停止")])
        raw = (self.out / "control.json").read_text(encoding="utf-8")
        for word in ("停止", "別の話", "あなた"):
            self.assertNotIn(word, raw)
        self.assertEqual(json.loads(raw)["mode"], "stop")
        self.assertEqual(set(json.loads(raw)), {"mode", "until", "set_at", "last_sig", "ack_pending", "reminded_on"})

    def test_the_reminder_comes_once_a_day_with_the_hours_and_the_stop_toast_is_not_repeated(self):
        self.cycle(T0, [self_row("あなた: 別の話")])
        self.cycle(T0 + timedelta(minutes=5), [self_row("あなた: 停止")])
        self.cycle(T0 + timedelta(minutes=40), [self_row("あなた: 停止")])
        self.assertEqual(self.t.acks("[kimeru 停止中 "), [])
        self.cycle(datetime(2026, 9, 29, 7, 10), [self_row("あなた: 停止")])
        self.assertEqual(self.t.acks("[kimeru 停止中 "), ["[kimeru 停止中 24 時間] 「再開」で戻ります"])
        self.cycle(datetime(2026, 9, 29, 7, 20), [self_row("あなた: 停止")])
        self.assertEqual(len(self.t.acks("[kimeru 停止中 ")), 1)
        self.assertEqual(len(self.toasts), 1)


class TestTheLineIsReadFromTheSelfChatTimelineWhenTheListHasNoSelfRow(CycleCase):
    """The list may not show the self chat at all (on a real PC it showed only meetings); the self chat's own timeline then carries the
    line: the reader hands 停止 / 再開 back as C: entries, and the last one is my line."""

    def rows(self, preview="おはようございます"):
        return [one_on_one(preview, "6:58")]   # no self row

    def cycle_tl(self, now, timeline, send=True):
        self.t.timeline = list(timeline)
        return self.cycle(now, self.rows(), send=send)

    def test_line_of_timeline_takes_the_last_control_entry_and_the_same_sig_as_the_list_row(self):
        self.assertEqual(control.line_of_timeline(["P:1", "C:停止", "R:OK 1", "C:再開", "P:2"])[1], "再開")
        self.assertEqual(control.line_of_timeline(["C:再開", "C:停止"])[1], "停止")
        self.assertEqual(control.line_of_timeline(["C:停止"]), control.line_of([self_row("あなた: 停止")]))
        self.assertEqual(control.line_of_timeline(["C:休み 15時"]), control.line_of([self_row("休み 15時")]))
        self.assertEqual(control.line_of_timeline(["P:1", "R:OK 1"])[1], None)   # entries but no control entry: a line that says nothing
        self.assertIsNone(control.line_of_timeline([]))
        self.assertIsNone(control.line_of_timeline(None))

    def test_the_last_entry_wins_so_a_stop_then_a_resume_in_one_look_is_a_resume(self):
        self.cycle_tl(T0, ["P:1"])
        r = self.cycle_tl(T0 + timedelta(minutes=5), ["P:1", "C:停止", "C:再開"])
        self.assertNotIn("mode", r)
        self.assertEqual(self.control()["mode"], "run")
        self.assertEqual(self.t.acks("[kimeru 停止中]"), [])

    def test_a_stop_in_the_timeline_stops_and_a_resume_there_resumes_with_the_stopped_cycle_reading_only_the_timeline(self):
        self.cycle_tl(T0, ["P:1"])
        self.assertEqual(self.control()["mode"], "run")
        r = self.cycle_tl(T0 + timedelta(minutes=5), ["P:1", "C:停止"])
        self.assertEqual(r["mode"], "stop")
        self.assertEqual(self.t.posts[-1:], [(ACK, True)])
        self.assertEqual(len(self.toasts), 1)
        # stopped: one list read and one timeline read, nothing else
        spies = self.spies()
        lists, reads, posted = self.t.list_calls, self.t.read_calls, len(self.t.posts)
        r = self.cycle_tl(T0 + timedelta(minutes=10), ["P:1", "C:停止", "R:OK 1"])
        self.assertEqual(r["mode"], "stop")
        self.assertEqual((self.t.list_calls - lists, self.t.read_calls - reads), (1, 1))
        self.assertEqual(len(self.t.posts), posted)
        self.assertEqual(self.judged, [])
        for name, spy in spies.items():
            self.assertEqual(spy.call_count, 0, name)
        mock.patch.stopall()
        r = self.cycle_tl(T0 + timedelta(minutes=20), ["P:1", "C:停止", "R:OK 1", "C:再開"])
        self.assertNotIn("mode", r)
        self.assertEqual(self.control()["mode"], "run")
        self.assertEqual(self.t.acks("[kimeru 再開]"), [RESUME_ACK])

    def test_a_stop_that_was_already_in_the_timeline_at_the_first_sighting_stays_harmless(self):
        for k in range(3):
            r = self.cycle_tl(T0 + timedelta(minutes=5 * k), ["P:1", "C:停止"])
            self.assertNotIn("mode", r)
        self.assertEqual(self.control()["mode"], "run")
        self.assertEqual(self.t.acks("[kimeru 停止中]"), [])

    def test_a_timeline_that_could_not_be_read_or_is_empty_decides_nothing_as_before(self):
        self.t.read = lambda: (_ for _ in ()).throw(RuntimeError("self chat not found"))
        r = self.cycle(T0, self.rows())
        self.assertNotIn("mode", r)
        self.assertFalse((self.out / "control.json").exists())
        self.assertFalse([k for k, v in r.items() if isinstance(v, str) and v.startswith("error")])

    def test_a_stopped_state_stays_when_the_timeline_cannot_be_read(self):
        self.cycle_tl(T0, ["P:1"])
        self.cycle_tl(T0 + timedelta(minutes=5), ["P:1", "C:停止"])
        self.t.read = lambda: (_ for _ in ()).throw(RuntimeError("self chat not found"))
        r = self.cycle(T0 + timedelta(minutes=10), self.rows())
        self.assertEqual(r["mode"], "stop")

    def test_the_list_row_is_preferred_and_the_timeline_is_not_read(self):
        self.t.timeline = ["P:1", "C:停止"]
        self.cycle(T0, [self_row("あなた: 別の話"), one_on_one("おはようございます", "6:58")])
        self.assertEqual(self.control()["mode"], "run")
        r = self.cycle(T0 + timedelta(minutes=5), [self_row("あなた: 別の話"), one_on_one("おはようございます", "6:58")])
        self.assertNotIn("mode", r)   # a 停止 in the timeline does not stop while the list row says something else
        self.assertEqual(self.t.acks("[kimeru 停止中]"), [])

    def test_the_stopped_cycle_with_a_list_row_does_not_read_the_timeline(self):
        self.cycle(T0, [self_row("あなた: 別の話")])
        self.cycle(T0 + timedelta(minutes=5), [self_row("あなた: 停止")])
        reads = self.t.read_calls
        self.cycle(T0 + timedelta(minutes=10), [self_row("あなた: 停止")])
        self.assertEqual(self.t.read_calls, reads)

    def test_a_quiet_line_in_the_timeline_is_read_like_the_one_in_the_list_row(self):
        with mock.patch.dict(os.environ, {"KIMERU_QUIET_WORDS": "1"}):
            self.cycle_tl(T0, ["P:1"])
            self.cycle_tl(T0 + timedelta(minutes=5), ["P:1", "C:休み 15時"])
            self.assertEqual(self.control()["mode"], "quiet")
            self.assertEqual(self.control()["until"], "2026-09-28T15:00")

    def test_with_control_words_zero_the_timeline_is_not_read_for_the_words(self):
        with mock.patch.dict(os.environ, {"KIMERU_CONTROL_WORDS": "0"}):
            self.cycle_tl(T0, ["P:1", "C:停止"])
            r = self.cycle_tl(T0 + timedelta(minutes=5), ["P:1", "C:停止", "C:再開", "C:停止"])
        self.assertNotIn("mode", r)
        self.assertFalse((self.out / "control.json").exists())


class TestControlWordsZeroAndTheDefault(CycleCase):
    def test_with_control_words_zero_the_words_do_nothing_and_no_state_file_is_written(self):
        with mock.patch.dict(os.environ, {"KIMERU_CONTROL_WORDS": "0"}):
            self.cycle(T0, [self_row("あなた: 別の話"), one_on_one("おはようございます", "6:58")])
            reads = self.t.list_calls
            r = self.cycle(T0 + timedelta(minutes=5), [self_row("あなた: 停止"), one_on_one("これは何ですか", "7:04")])
        self.assertNotIn("mode", r)
        self.assertEqual(self.t.list_calls - reads, 1)
        self.assertFalse((self.out / "control.json").exists())
        self.assertEqual(len(r["notify"]), 1)

    def test_the_default_reads_the_list_once_per_cycle_and_does_not_read_more(self):
        self.cycle(T0, [self_row("あなた: 別の話"), one_on_one("おはようございます", "6:58")])
        reads = self.t.list_calls
        self.cycle(T0 + timedelta(minutes=5), [self_row("あなた: 別の話"), one_on_one("これは何ですか", "7:04")])
        self.assertEqual(self.t.list_calls - reads, 1)

    def test_the_setting_is_declared_as_a_zero_or_one_setting_that_defaults_to_one(self):
        self.assertEqual(config.SETTINGS["control_words"], ("KIMERU_CONTROL_WORDS", "1"))
        self.assertEqual(config.CHOICES["control_words"], ("0", "1"))
        self.assertIn("control_words", config.LOGGABLE)
        self.assertIn("control_words", config.SHARE_VALUES)
        self.assertEqual(config.value("control_words"), "1")

    def test_the_words_are_not_reply_numbers_and_the_reply_reader_does_not_take_them(self):
        self.assertIsNone(notify.parse_reply("停止"))
        self.assertIsNone(notify.parse_reply("再開"))
        self.assertEqual(notify.fresh_replies({"timeline": ["P:1", "R:OK 1"]}), ["OK 1"])

    def test_the_pull_takes_the_list_it_is_given_and_does_not_read_teams_again(self):
        t = CountingTeams()
        with tempfile.TemporaryDirectory() as d:
            n = pull.pull_teams(Path(d) / "inbox", Path(d) / "out", bridge=t, now=T0,
                                chats=[one_on_one("おはようございます", "6:58")])
        self.assertEqual((n, t.list_calls), (0, 0))


class TestTheTextsStayTheSameWhenTheSettingIsZero(CycleCase):
    """The golden texts: the same day gives the same posts, brief, toast, report and push text as before R20."""

    def run_day(self, preview):
        self.cycle(datetime(2026, 9, 28, 7, 0), [one_on_one("おはようございます", "8:00"), self_row(preview)])
        r = self.cycle(datetime(2026, 9, 28, 8, 10), [one_on_one("これは何ですか", "8:05"), self_row(preview)])
        r.pop("perf", None)
        return r

    def test_with_control_words_zero_the_posts_brief_toast_and_report_are_byte_for_byte_as_before(self):
        with mock.patch.dict(os.environ, {"KIMERU_CONTROL_WORDS": "0"}):
            r = self.run_day("あなた: 停止")
        self.assertEqual(self.t.posts, GOLDEN_POSTS)
        self.assertEqual(self.toasts, [tuple(x) for x in GOLDEN_TOASTS])
        self.assertEqual(r, GOLDEN_REPORT)
        self.assertFalse((self.out / "control.json").exists())

    def test_with_the_default_a_line_that_is_not_a_word_gives_the_same_texts(self):
        r = self.run_day("あなた: 雑談")
        self.assertEqual(self.t.posts, GOLDEN_POSTS)
        self.assertEqual(self.toasts, [tuple(x) for x in GOLDEN_TOASTS])
        self.assertEqual(r, GOLDEN_REPORT)

    def test_the_push_text_for_two_waiting_items_and_a_notice_is_unchanged(self):
        self.assertEqual(push.message([1, 2], 1, 0), GOLDEN_PUSH)


if __name__ == "__main__":
    unittest.main()
