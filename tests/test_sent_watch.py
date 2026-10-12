"""R14 (C04a): after an OK, the self-chat list shows whether the approved reply went out. Only the list that the pull reads every cycle is
used: an own message that appears there after the OK is compared with the approved text (as_is, edited, other) and only that state and
a time are kept. Nothing is opened, clicked or sent; the text of the list preview is never written down; with sent_watch=0 nothing changes
(the golden strings below are the posts of the default path, taken before the change)."""
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

from kimeru import attention, config, daily, fsutil, notify, pull, push, sent
from kimeru.backends import StubBackend
from kimeru.cli import process
from tests.test_daily import GRAPHS, PBS, FakeTeams, one_on_one

ONE = "19:a_b@unq.gbl.spaces"          # the chat id one_on_one() gives
APPROVED = "承知しました。明日までに確認します。"
ON = {"KIMERU_SENT_WATCH": "1"}
OFF = {"KIMERU_SENT_WATCH": "0"}
T0 = datetime(2026, 10, 12, 7, 0)     # before the brief hour (8): no brief is made in these cycles
REPLY = {"graph": "teams_chat", "event_kind": "teams.chat", "event_id": "m-7", "node": "decision_today", "outcome": "decide",
         "needs_human": True, "advice": "本日中に判断が必要: Sato「明日の件、確認できますか」",
         "actions": [{"type": "teams.reply", "chat_id": ONE, "text": APPROVED}]}
GOLDEN_POSTS = [   # taken from the code before R14 (the default path must keep these bytes)
    "[kimeru #1] 本日中に判断が必要: Sato「明日の件、確認できますか」\n→ 本日中に判断が必要: Sato「明日の件、確認できますか」\n案: 承知しました。明日までに確認します。\nOK 1 / NG 1 / 詳細 1",
    "[kimeru 送信用 #1]\n承知しました。明日までに確認します。",
]
GOLDEN_BRIEF = ("[kimeru brief 2026-10-12] 今日の進め方\n実行候補はありません。未完了作業の状況は下記を確認してください\n\n"
                "承認済み・未完了の作業:\n- #1 本日中に判断が必要: Sato「明日の件、確認できますか」")   # the 5 h cycle's brief with the setting off


def write_reply_queue(out):
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "queue.jsonl").write_text(json.dumps(REPLY, ensure_ascii=False) + "\n", encoding="utf-8")


def cycle(out, inbox, t, now, real=True):
    return daily.cycle(out, inbox, GRAPHS, StubBackend(), PBS, process, bridge=t, send=True, now=now, real=real)


def approve(d, t, preview="明日の件、確認できますか", real=True):
    """The chat's last message is `preview`; the reply is posted; an OK comes from the phone. Returns (out, inbox)."""
    out, inbox = Path(d) / "out", Path(d) / "inbox"
    write_reply_queue(out)
    t.chat_list = [one_on_one(preview, "9:00")]
    cycle(out, inbox, t, T0, real)                                # baseline: the item is posted
    t.timeline.append("R:OK 1")
    cycle(out, inbox, t, T0 + timedelta(minutes=10), real)        # the OK is recorded
    return out, inbox


def own_on_list(out, inbox, t, body, now, real=True):
    """The self chat's list now shows `body` as a message of mine (the list text starts with the name prefix)."""
    t.chat_list = [one_on_one("あなた: " + body, "9:20")]
    return cycle(out, inbox, t, now, real)


def item_one(out):
    return notify.Approvals(out).data["items"]["1"]


def all_text(out):
    """Every file under the state folder, as text (what would be left on disk)."""
    return "\n".join(p.read_bytes().decode("utf-8", errors="ignore") for p in Path(out).rglob("*") if p.is_file())


def chat(cid, kind, preview, time="10:00"):
    return {"id": cid, "kind": kind, "title": "Sato", "preview": preview, "time": time, "unread": True, "mention": False}


class TestTheSendIsSeenOnTheList(unittest.TestCase):
    def test_the_approval_keeps_the_list_signature_of_the_reply_chat(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, _ = approve(d, t)
            base = item_one(out)["sent_base"]
            self.assertEqual(base["chat_id"], ONE)
            pull_sigs = fsutil.read_json(Path(out) / "pull_state.json", {})["teams"]["sigs"]
            self.assertEqual(base["p"], pull_sigs[ONE]["p"])

    def test_the_own_message_after_the_ok_that_is_the_approved_text_is_as_is(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t)
            own_on_list(out, inbox, t, APPROVED, T0 + timedelta(minutes=20))
            seen = item_one(out)["sent_seen"]
            self.assertEqual((seen["state"], seen["ratio"]), ("as_is", 1.0))

    def test_an_own_message_with_a_changed_time_word_is_edited(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t)
            own_on_list(out, inbox, t, "承知しました。明日の午前までに確認します。", T0 + timedelta(minutes=20))
            self.assertEqual(item_one(out)["sent_seen"]["state"], "edited")

    def test_another_sentence_of_mine_after_the_ok_is_other(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t)
            own_on_list(out, inbox, t, "ランチ行きます？", T0 + timedelta(minutes=20))
            self.assertEqual(item_one(out)["sent_seen"]["state"], "other")

    def test_a_send_within_the_three_days_is_seen_and_one_after_them_is_not(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t)
            own_on_list(out, inbox, t, APPROVED, T0 + timedelta(days=4))
            self.assertNotIn("sent_seen", item_one(out))
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t)
            own_on_list(out, inbox, t, APPROVED, T0 + timedelta(days=2))
            self.assertEqual(item_one(out)["sent_seen"]["state"], "as_is")

    def test_my_own_message_already_on_the_list_before_the_ok_is_not_a_send(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t, preview="あなた: 承知しました。明日までに確認します。")
            cycle(out, inbox, t, T0 + timedelta(minutes=20))         # the same list again
            self.assertNotIn("sent_seen", item_one(out))

    def test_the_own_message_is_not_an_event_and_the_chat_is_not_opened(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t)
            r = own_on_list(out, inbox, t, APPROVED, T0 + timedelta(minutes=20))
            self.assertEqual(r["pull_teams"], 0)
            self.assertEqual(list(inbox.glob("*.json")), [])
            self.assertEqual(getattr(t, "opened", []), [])

    def test_the_list_text_is_not_written_to_any_file_of_the_state_folder(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t)
            own_on_list(out, inbox, t, "ランチ行きます？", T0 + timedelta(minutes=20))
            text = all_text(out)
            self.assertIn("sent_seen", text)                          # the state is kept
            self.assertNotIn("ランチ行きます", text)                   # the text of the list is not
            self.assertNotIn("あなた", text)

    def test_a_seen_send_is_not_seen_again(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t)
            own_on_list(out, inbox, t, APPROVED, T0 + timedelta(minutes=20))
            own_on_list(out, inbox, t, "ランチ行きます？", T0 + timedelta(minutes=30))
            self.assertEqual(item_one(out)["sent_seen"]["state"], "as_is")

    def test_the_paste_only_path_does_not_look_at_the_list_for_sends(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t)
            own_on_list(out, inbox, t, APPROVED, T0 + timedelta(minutes=20), real=False)
            self.assertNotIn("sent_seen", item_one(out))

    def test_a_locked_approvals_file_is_not_written_and_nothing_is_reported_as_seen(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, _ = approve(d, t)
            before = (Path(out) / "approvals.json").read_bytes()
            with fsutil.exclusive(notify.lock_path(out)) as got:
                self.assertTrue(got)
                res = sent.observe(out, [(ONE, APPROVED)], now=T0 + timedelta(minutes=20))
            self.assertTrue(res.busy)
            self.assertEqual((Path(out) / "approvals.json").read_bytes(), before)

    def test_the_observation_writes_nothing_when_the_setting_is_off(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, OFF):
            t = FakeTeams()
            out, inbox = approve(d, t)
            before = (Path(out) / "approvals.json").read_bytes()
            own_on_list(out, inbox, t, APPROVED, T0 + timedelta(minutes=20))
            self.assertEqual((Path(out) / "approvals.json").read_bytes(), before)
            self.assertEqual(sent.observe(out, [(ONE, APPROVED)], now=T0 + timedelta(minutes=20)), [])


class TestTheTextClassesOfASend(unittest.TestCase):
    def test_the_same_text_is_as_is(self):
        self.assertEqual(sent.classify(APPROVED, APPROVED), ("as_is", 1.0))

    def test_a_list_text_cut_with_an_ellipsis_that_starts_the_approved_text_is_as_is(self):
        self.assertEqual(sent.classify(APPROVED, "承知しました。明日まで…")[0], "as_is")
        self.assertEqual(sent.classify(APPROVED, "承知しました。明日まで...")[0], "as_is")

    def test_a_changed_word_is_edited_and_another_sentence_is_other(self):
        self.assertEqual(sent.classify(APPROVED, "承知しました。明日の午前までに確認します。")[0], "edited")
        self.assertEqual(sent.classify(APPROVED, "ランチ行きます？")[0], "other")

    def test_full_width_and_half_width_forms_are_the_same_text(self):
        self.assertEqual(sent.classify("ランチ行きますか?", "ランチ行きますか？")[0], "as_is")

    def test_an_empty_list_text_is_other(self):
        self.assertEqual(sent.classify(APPROVED, "…"), ("other", 0.0))


class TestTheOwnMessagesOnRequest(unittest.TestCase):
    TIME = datetime(2026, 10, 12, 7, 20)

    def test_an_own_message_is_handed_over_with_its_chat_and_body_only_in_memory(self):
        sec, own = {}, []
        pull.teams_events([chat(ONE, "oneOnOne", "明日の件")], sec)
        evs = pull.teams_events([chat(ONE, "oneOnOne", "あなた: 承知しました。", "7:20")], sec, own_out=own, now=self.TIME)
        self.assertEqual(evs, [])
        self.assertEqual(own, [(ONE, "承知しました。")])
        self.assertIn("own", sec["sigs"][ONE])
        self.assertNotIn("承知しました", json.dumps(sec, ensure_ascii=False))

    def test_without_the_request_the_own_message_leaves_no_trace(self):
        sec = {}
        pull.teams_events([chat(ONE, "oneOnOne", "明日の件")], sec)
        self.assertEqual(pull.teams_events([chat(ONE, "oneOnOne", "あなた: 承知しました。", "7:20")], sec), [])
        self.assertNotIn("own", sec["sigs"][ONE])

    def test_an_own_message_already_on_the_list_at_the_baseline_is_not_handed_over(self):
        sec, own = {}, []
        pull.teams_events([chat(ONE, "oneOnOne", "あなた: 承知しました。")], sec, own_out=own, now=self.TIME)
        self.assertEqual(own, [])


class TestWhenTheSettingIsOffTheDefaultPathIsUnchanged(unittest.TestCase):
    def test_the_posts_of_an_approval_and_its_ready_text_are_the_golden_strings(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, OFF):
            t = FakeTeams()
            out, inbox = approve(d, t)
            own_on_list(out, inbox, t, APPROVED, T0 + timedelta(minutes=20))
            self.assertEqual([p for p, _ in t.posts], GOLDEN_POSTS)

    def test_the_default_keeps_no_sent_keys_and_no_own_signature(self):
        with tempfile.TemporaryDirectory() as d:          # no setting at all: the default is 0
            t = FakeTeams()
            out, inbox = approve(d, t)
            own_on_list(out, inbox, t, APPROVED, T0 + timedelta(minutes=20))
            item = item_one(out)
            self.assertNotIn("sent_base", item)
            self.assertNotIn("sent_seen", item)
            sigs = fsutil.read_json(Path(out) / "pull_state.json", {})["teams"]["sigs"]
            self.assertTrue(all("own" not in v for v in sigs.values()))
            self.assertEqual(getattr(t, "opened", []), [])
            self.assertEqual(list(inbox.glob("*.json")), [])


class TestSettingsAndDocs(unittest.TestCase):
    def test_the_switch_is_off_by_default_and_logged_like_post_kind_mark(self):
        self.assertEqual(config.SETTINGS["sent_watch"], ("KIMERU_SENT_WATCH", "0"))
        self.assertEqual(config.CHOICES["sent_watch"], ("0", "1"))
        self.assertIn("sent_watch", config.LOGGABLE)
        self.assertIn("sent_watch", config.SHARE_VALUES)
        with mock.patch.dict(os.environ, {"KIMERU_SENT_WATCH": "on"}):
            self.assertEqual(config.value("sent_watch"), "1")

    def test_the_window_setting_is_three_days_and_a_value_that_is_not_a_number_keeps_it(self):
        self.assertEqual(config.SETTINGS["sent_watch_days"], ("KIMERU_SENT_WATCH_DAYS", "3"))
        self.assertIn("sent_watch_days", config.SHARE_VALUES)
        self.assertEqual(sent.days(), 3)
        with mock.patch.dict(os.environ, {"KIMERU_SENT_WATCH_DAYS": "x"}):
            self.assertEqual(sent.days(), 3)
        with mock.patch.dict(os.environ, {"KIMERU_SENT_WATCH_DAYS": "5"}):
            self.assertEqual(sent.days(), 5)

    def test_the_reference_has_one_row_for_each_new_setting(self):
        ref = (Path(__file__).resolve().parent.parent / "docs" / "reference.md").read_text(encoding="utf-8")
        self.assertEqual(ref.count("| `KIMERU_SENT_WATCH` |"), 1)
        self.assertEqual(ref.count("| `KIMERU_SENT_WATCH_DAYS` |"), 1)


# ---- R28 (C04b): an approved reply whose send is not seen gets one nudge a day (once an item, after sent_nudge_hours); `送った N` stops
# it. Only with sent_watch=1; the default posts, the record and the push text are the same as before (the golden tests below).

ROOT = Path(__file__).resolve().parent.parent
GOLD_NUDGE = "承認した返信が、自分とのチャットの一覧にまだ見えません。送り終えたら「送った N」と返信してください（例: 送った 3）。"
PUSH_ENV = {"KIMERU_PUSH": "webhook", "KIMERU_PUSH_WEBHOOK_URL": "https://example.invalid/kimeru"}


def watched(num, at, **extra):
    """An approved item whose reply chat was on the list at the OK (what note_approval keeps as sent_base)."""
    item = {"key": f"k{num}", "status": "approved", "posted": True, "record": {"actions": []},
            sent.BASE: {"chat_id": ONE, "p": "h", "approved_at": at.astimezone(timezone.utc).isoformat(timespec="seconds")}}
    item.update(extra)
    return item


def nudges(t):
    return [p for p, _ in t.posts if p.startswith("[kimeru 未送信")]


def tree(out):
    """Every file under the state folder, by its name, with its bytes (what a run that must not write leaves behind)."""
    return {str(p.relative_to(out)): p.read_bytes() for p in Path(out).rglob("*") if p.is_file()}


class TestTheNudgeIsOnceADayAndOnceAnItem(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, ON)
        patch.start()
        self.addCleanup(patch.stop)
        self.posts = []

    def post(self, text):
        self.posts.append(text)

    def test_an_unseen_reply_is_nudged_two_hours_after_the_ok_and_not_before(self):
        data = {"items": {"3": watched(3, T0)}}
        self.assertEqual(sent.nudge(data, self.post, T0 + timedelta(hours=1, minutes=59)), [])
        self.assertEqual(sent.nudge(data, self.post, T0 + timedelta(hours=2)), ["3"])
        self.assertEqual(self.posts, ["[kimeru 未送信 #3]\n" + GOLD_NUDGE])
        self.assertIn("nudged_at", data["items"]["3"])
        self.assertEqual(data["nudge_day"], "2026-10-12")

    def test_the_nudge_is_one_post_a_day_and_the_next_day_names_only_the_items_not_yet_nudged(self):
        data = {"items": {"3": watched(3, T0), "5": watched(5, T0 + timedelta(hours=1))}}
        self.assertEqual(sent.nudge(data, self.post, T0 + timedelta(hours=2)), ["3"])     # 5 is due at 10:00, today's post is out
        self.assertEqual(sent.nudge(data, self.post, T0 + timedelta(hours=3)), [])
        self.assertEqual(sent.nudge(data, self.post, T0 + timedelta(days=1, hours=3)), ["5"])
        self.assertEqual(sent.nudge(data, self.post, T0 + timedelta(days=2, hours=3)), [])
        self.assertEqual(self.posts, ["[kimeru 未送信 #3]\n" + GOLD_NUDGE, "[kimeru 未送信 #5]\n" + GOLD_NUDGE])

    def test_a_reply_whose_send_is_seen_or_confirmed_is_never_nudged(self):
        data = {"items": {"3": watched(3, T0, **{sent.SEEN: {"state": "as_is"}}),
                          "5": watched(5, T0, **{sent.CONFIRMED: {"at": "x"}})}}
        self.assertEqual(sent.nudge(data, self.post, T0 + timedelta(hours=5)), [])
        self.assertEqual(self.posts, [])

    def test_only_an_approved_reply_with_a_watched_chat_inside_the_window_is_nudged(self):
        data = {"items": {"4": watched(4, T0, status="pending"),
                          "6": {"key": "k6", "status": "approved", "posted": True, "record": {}}}}
        self.assertEqual(sent.nudge(data, self.post, T0 + timedelta(hours=5)), [])
        data = {"items": {"3": watched(3, T0)}}
        self.assertEqual(sent.nudge(data, self.post, T0 + timedelta(days=3, minutes=1)), [])
        self.assertEqual(sent.nudge(data, self.post, T0 + timedelta(days=3, minutes=-1)), ["3"])

    def test_with_the_setting_off_nothing_is_nudged_and_the_state_is_not_touched(self):
        data = {"items": {"3": watched(3, T0)}}
        with mock.patch.dict(os.environ, OFF):
            self.assertEqual(sent.nudge(data, self.post, T0 + timedelta(hours=5)), [])
            self.assertFalse(sent.awaiting(data, data["items"]["3"], T0 + timedelta(hours=5)))
        self.assertEqual(data, {"items": {"3": watched(3, T0)}})
        self.assertEqual(self.posts, [])

    def test_the_hours_setting_is_a_whole_number_of_one_or_more_and_otherwise_two(self):
        for raw, want in (("3", 3), ("1", 1), ("0", 2), ("-4", 2), ("x", 2), ("", 2)):
            with mock.patch.dict(os.environ, {"KIMERU_SENT_NUDGE_HOURS": raw}):
                self.assertEqual(sent.nudge_hours(), want, raw)

    def test_a_read_is_asked_for_only_while_a_send_may_still_be_seen_or_confirmed(self):
        data = {"items": {"3": watched(3, T0), "5": watched(5, T0 + timedelta(hours=1))}}
        self.assertFalse(sent.awaiting(data, data["items"]["3"], T0 + timedelta(hours=1)))   # not due yet
        self.assertTrue(sent.awaiting(data, data["items"]["3"], T0 + timedelta(hours=2)))    # due, no nudge today
        self.assertEqual(sent.nudge(data, self.post, T0 + timedelta(hours=2)), ["3"])
        self.assertTrue(sent.awaiting(data, data["items"]["3"], T0 + timedelta(hours=2, minutes=30)))   # nudged, not seen
        self.assertFalse(sent.awaiting(data, data["items"]["5"], T0 + timedelta(hours=3)))    # due, but today's post is out
        self.assertTrue(sent.awaiting(data, data["items"]["5"], T0 + timedelta(days=1, hours=3)))
        data["items"]["3"][sent.CONFIRMED] = {"at": "x"}
        self.assertFalse(sent.awaiting(data, data["items"]["3"], T0 + timedelta(hours=4)))

    def test_an_open_nudge_counts_for_the_phone_only_while_the_setting_is_on(self):
        item = watched(3, T0, **{sent.NUDGED: "x"})
        self.assertTrue(sent.nudged_open(item, T0 + timedelta(hours=3)))
        with mock.patch.dict(os.environ, OFF):
            self.assertFalse(sent.nudged_open(item, T0 + timedelta(hours=3)))


class TestASendReplyIsReadOnlyBelowItsOwnApproval(unittest.TestCase):
    def test_a_send_reply_counts_only_below_the_approval_post_of_its_number(self):
        self.assertEqual(notify.fresh_sent({"timeline": ["P:1", "R:送った 1"]}), ["1"])
        self.assertEqual(notify.fresh_sent({"timeline": ["R:送った 1", "P:1"]}), [])
        self.assertEqual(notify.fresh_sent({"timeline": ["R:送った 1"]}), [])                  # the approval post is not on the screen
        self.assertEqual(notify.fresh_sent({"timeline": ["P:1", "X:1:1", "R:送った 1"]}), ["1"])   # a result post is no boundary
        self.assertEqual(notify.fresh_sent({"timeline": ["P:1", "T:1", "R:送った 1"]}), [])      # a test post of the same number
        self.assertEqual(notify.fresh_sent({"timeline": ["P:1", "R:送った 1", "R:送った 1"]}), ["1"])
        self.assertEqual(notify.fresh_sent({"timeline": ["P:2", "R:送った 1"]}), [])
        self.assertEqual(notify.fresh_sent({"replies": ["送った 1"]}), [])                       # an older reader: nothing is proven

    def test_the_real_and_the_test_reader_each_keep_their_own_send_replies(self):
        self.assertEqual(notify.scoped_timeline({"timeline": ["T:1", "R:送った 1"]}), [])
        self.assertEqual(notify.scoped_timeline({"timeline": ["T:1", "R:送った 1"]}, test=True), ["P:1", "R:送った 1"])
        self.assertEqual(notify._reply_number("送った 1"), "1")

    def test_the_send_reply_grammar_takes_the_forms_a_phone_types_and_no_other_line(self):
        for line in ("送った 3", "送った3", "送った #3", "送った ３", "送った 3。"):
            self.assertEqual(notify.parse_sent(line), "3", line)
        for line in ("送った", "送ったかも 3", "送った 3 ありがとう", "OK 3", "送信 3", "送った 3-1"):
            self.assertIsNone(notify.parse_sent(line), line)

    def test_a_send_reply_changes_no_reply_or_redo_boundary_wherever_it_stands(self):
        base = ["P:7", "X:7:1", "R:再実行 7-1", "R:OK 7", "R:再実行 7-2"]
        for at in range(len(base) + 1):
            tl = base[:at] + ["R:送った 7"] + base[at:]
            self.assertEqual(notify.fresh_entries({"timeline": tl}), notify.fresh_entries({"timeline": base}))
            self.assertEqual(notify.redo_k_entries({"timeline": tl}), notify.redo_k_entries({"timeline": base}))
            self.assertEqual(notify.redo_bad_kinds({"timeline": tl}), notify.redo_bad_kinds({"timeline": base}))
            self.assertEqual(notify.fresh_replies({"timeline": tl}), notify.fresh_replies({"timeline": base}))


class TestASendReplyStopsTheNudge(unittest.TestCase):
    def test_the_nudge_goes_to_the_self_chat_once_and_the_reply_chat_is_not_opened(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t)
            cycle(out, inbox, t, T0 + timedelta(hours=2, minutes=20))
            self.assertEqual(nudges(t), ["[kimeru 未送信 #1]\n" + GOLD_NUDGE])
            self.assertTrue(all(p.startswith("[kimeru") for p, _ in t.posts))
            self.assertEqual(getattr(t, "opened", []), [])
            self.assertIn("nudged_at", item_one(out))
            cycle(out, inbox, t, T0 + timedelta(hours=5))
            self.assertEqual(len(nudges(t)), 1)

    def test_a_send_reply_after_the_nudge_confirms_it_and_changes_nothing_else(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t)
            cycle(out, inbox, t, T0 + timedelta(hours=2, minutes=20))
            before = item_one(out)
            t.timeline.append("R:送った 1")
            cycle(out, inbox, t, T0 + timedelta(hours=2, minutes=40))
            after = item_one(out)
            self.assertIn("sent_confirmed", after)
            self.assertEqual({k: v for k, v in after.items() if k != "sent_confirmed"}, before)
            self.assertEqual(after["status"], "approved")
            self.assertIn('"status": "sent_confirmed"', (Path(out) / "approvals.log.jsonl").read_text(encoding="utf-8"))
            cycle(out, inbox, t, T0 + timedelta(days=1, hours=3))
            self.assertEqual(len(nudges(t)), 1)

    def test_a_send_reply_before_the_nudge_time_means_no_nudge_at_all(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t)
            t.timeline.append("R:送った 1")
            cycle(out, inbox, t, T0 + timedelta(hours=3))
            self.assertEqual(nudges(t), [])
            self.assertIn("sent_confirmed", item_one(out))

    def test_with_the_setting_off_a_send_reply_and_a_long_wait_change_no_post_and_no_record(self):
        def flow(d, with_reply):
            t = FakeTeams()
            out, inbox = approve(d, t)
            if with_reply:
                t.timeline.append("R:送った 1")
            cycle(out, inbox, t, T0 + timedelta(hours=5))
            return t, out
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b, mock.patch.dict(os.environ, OFF):
            ta, outa = flow(a, True)
            tb, _ = flow(b, False)
            self.assertEqual(ta.posts, tb.posts)
            self.assertEqual([p for p, _ in ta.posts if not p.startswith("[kimeru brief")], GOLDEN_POSTS)   # the literal bytes (setting 0)
            self.assertEqual([p for p, _ in ta.posts if p.startswith("[kimeru brief")], [GOLDEN_BRIEF])
            self.assertEqual(nudges(ta), [])
            self.assertFalse({"nudged_at", "sent_confirmed"} & set(item_one(outa)))
            self.assertNotIn("nudge_day", json.loads((Path(outa) / "approvals.json").read_text(encoding="utf-8")))

    def test_a_run_that_is_not_real_or_does_not_send_writes_no_nudge_and_reads_no_chat(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t)
            t.timeline.append("R:送った 1")
            before, posts = tree(out), len(t.posts)
            with mock.patch.object(t, "read", wraps=t.read) as read:
                for real, send in ((False, True), (True, False)):
                    notify.collect(out, t, real=real, send=send, now=T0 + timedelta(hours=5))
                self.assertEqual(read.call_count, 0)
            self.assertEqual(tree(out), before)                       # no file is created, changed or removed (approvals, executions, locks)
            self.assertEqual(len(t.posts), posts)
            self.assertEqual(nudges(t), [])
            self.assertTrue(all(p.startswith("[kimeru") for p, _ in t.posts))

    def test_a_locked_approvals_file_gets_no_nudge_and_no_write(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, _ = approve(d, t)
            before, posts = tree(out), len(t.posts)
            with fsutil.exclusive(notify.lock_path(out)) as got:
                self.assertTrue(got)
                res = notify.collect(out, t, real=True, send=True, now=T0 + timedelta(hours=5))
            self.assertTrue(res.busy)
            self.assertEqual(tree(out), before)
            self.assertEqual(len(t.posts), posts)
            self.assertEqual(nudges(t), [])


class TestThePhoneLineForAnUnsentReply(unittest.TestCase):
    def test_the_line_names_the_count_and_the_numbers_and_nothing_else(self):
        self.assertEqual(push.message([], 0, 0, [3, 5]), "kimeru: 未送信 2 件（#3, #5）。Teams の自分とのチャットを確認してください")

    def test_without_unsent_replies_the_line_is_the_same_as_before(self):
        self.assertEqual(push.message([1], 1, 0), "kimeru: 確認待ち 1 件（#1） / 自動決定の通知 1 件。Teams の自分とのチャットを確認してください")
        self.assertEqual(push.message([1], 1, 2, []), push.message([1], 1, 2))
        self.assertEqual(push.message([], 0, 0), "")

    def test_the_unsent_numbers_follow_the_nudge_and_stop_at_a_send_reply(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t)
            self.assertEqual(push.unsent_numbers(out, now=(T0 + timedelta(hours=5)).timestamp()), [])
            cycle(out, inbox, t, T0 + timedelta(hours=2, minutes=20))
            self.assertEqual(push.unsent_numbers(out, now=(T0 + timedelta(hours=3)).timestamp()), [1])
            t.timeline.append("R:送った 1")
            cycle(out, inbox, t, T0 + timedelta(hours=3, minutes=10))
            self.assertEqual(push.unsent_numbers(out, now=(T0 + timedelta(hours=3, minutes=20)).timestamp()), [])

    def test_the_phone_is_told_once_about_a_nudged_reply_and_no_name_or_text_leaves(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, _ = approve(d, t)                         # the route is not on yet, so these cycles never push
            told = []

            def sender(route, text):
                told.append((route, text))
            with mock.patch.dict(os.environ, PUSH_ENV):
                push.run(out, now=(T0 + timedelta(hours=1)).timestamp(), sender=sender)                   # the baseline
                notify.collect(out, t, real=True, send=True, now=T0 + timedelta(hours=2, minutes=20))   # the nudge
                push.run(out, now=(T0 + timedelta(hours=2, minutes=30)).timestamp(), sender=sender)
                push.run(out, now=(T0 + timedelta(hours=2, minutes=40)).timestamp(), sender=sender)
        self.assertEqual(told, [("webhook", "kimeru: 未送信 1 件（#1）。Teams の自分とのチャットを確認してください")])
        self.assertNotIn(APPROVED, told[0][1])


class TestASendReplyBelowTheNudgeCounts(unittest.TestCase):
    """The nudge is posted 2 hours or more after the OK, when the approval post has often scrolled off. A `送った N` below the visible
    nudge of N counts; a reply above the nudge, or below a nudge for another number, does not."""
    def test_a_send_reply_below_the_nudge_counts_when_the_approval_post_is_off_the_screen(self):
        self.assertEqual(notify.fresh_sent({"timeline": ["U:1", "R:送った 1"]}), ["1"])
        self.assertEqual(notify.fresh_sent({"timeline": ["R:送った 1", "U:1"]}), [])                   # above the nudge
        self.assertEqual(notify.fresh_sent({"timeline": ["P:1", "U:1", "R:送った 1"]}), ["1"])          # approval post and nudge

    def test_a_nudge_for_another_number_is_no_boundary(self):
        self.assertEqual(notify.fresh_sent({"timeline": ["U:2", "R:送った 1"]}), [])
        self.assertEqual(notify.fresh_sent({"timeline": ["U:1", "R:送った 2"]}), [])
        self.assertEqual(notify.fresh_sent({"timeline": ["U:2,3", "R:送った 3"]}), ["3"])             # one nudge may name several
        self.assertEqual(notify.fresh_sent({"timeline": ["U:1", "R:送った 1", "U:2", "R:送った 2"]}), ["1", "2"])

    def test_a_test_post_is_no_boundary_and_a_reply_after_it_is_the_tests(self):
        self.assertEqual(notify.fresh_sent({"timeline": ["T:1", "R:送った 1"]}), [])
        self.assertEqual(notify.fresh_sent({"timeline": ["U:1", "T:1", "R:送った 1"]}), [])   # a reply after a test post of its number is the test's
        self.assertEqual(notify.scoped_timeline({"timeline": ["U:1", "T:1"]}, test=True), ["P:1"])   # the test reader keeps its own posts only

    def test_the_nudge_entry_is_kept_by_the_real_reader_and_dropped_by_the_test_reader(self):
        self.assertEqual(notify.scoped_timeline({"timeline": ["U:1"]}), ["U:1"])
        self.assertEqual(notify.scoped_timeline({"timeline": ["U:1"]}, test=True), [])


class TestTheNudgeEntryChangesNoReplyOrRedoBoundary(unittest.TestCase):
    BASES = (["P:7", "X:7:1", "R:再実行 7-1", "R:OK 7", "R:再実行 7-2"],
             ["P:7", "T:7", "R:OK 7", "X:7:2", "R:再実行 7-2", "R:送った 7"],
             ["P:7", "R:OK 7", "X:7:1", "R:再実行 7-1", "R:保留 7 明日"])

    def test_a_nudge_entry_changes_no_reply_or_redo_boundary_wherever_it_stands(self):   # the reply rules of fresh_entries are not touched
        for base in self.BASES:
            for nudge in ("U:7", "U:7,8", "U:8"):
                for at in range(len(base) + 1):
                    tl = base[:at] + [nudge] + base[at:]
                    for x_boundary in (True, False):
                        self.assertEqual(notify.fresh_entries({"timeline": tl}, x_boundary), notify.fresh_entries({"timeline": base}, x_boundary))
                    self.assertEqual(notify.fresh_replies({"timeline": tl}), notify.fresh_replies({"timeline": base}))
                    self.assertEqual(notify.redo_k_entries({"timeline": tl}), notify.redo_k_entries({"timeline": base}))
                    self.assertEqual(notify.redo_bad_kinds({"timeline": tl}), notify.redo_bad_kinds({"timeline": base}))
                    self.assertEqual(notify.redo_bad_entries({"timeline": tl}), notify.redo_bad_entries({"timeline": base}))


class TestTheNudgeStateIsSavedWithItsOutboxText(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, ON)
        patch.start()
        self.addCleanup(patch.stop)

    def new_out(self, d):
        out = Path(d) / "out"
        ap = notify.Approvals(out)
        ap.data["items"]["3"] = watched(3, T0)
        ap.save()
        return out

    def test_the_nudge_state_and_its_outbox_text_are_saved_in_one_save(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.new_out(d)
            ap = notify.Approvals(out)
            post = notify.make_post(ap, FakeTeams(), send=True)
            self.assertEqual(sent.nudge(ap.data, post, T0 + timedelta(hours=2)), ["3"])
            saved = notify.Approvals(out).data
            self.assertEqual(saved["outbox"], ["[kimeru 未送信 #3]\n" + GOLD_NUDGE])
            self.assertEqual(saved["nudge_day"], "2026-10-12")
            self.assertIn("nudged_at", saved["items"]["3"])
            self.assertEqual(sent.nudge(saved, post, T0 + timedelta(hours=3)), [])                 # the same day: no second nudge

    def test_a_run_that_stops_after_the_outbox_save_keeps_the_state_and_posts_once(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.new_out(d)
            ap = notify.Approvals(out)
            post = notify.make_post(ap, FakeTeams(), send=True)

            def post_then_stop(text):
                post(text)   # the outbox text is saved here, and with it the state (sent.nudge sets it before the post)
                raise RuntimeError("the run stops before its last save")
            with self.assertRaises(RuntimeError):
                sent.nudge(ap.data, post_then_stop, T0 + timedelta(hours=2))
            saved = notify.Approvals(out).data
            self.assertEqual(saved["outbox"], ["[kimeru 未送信 #3]\n" + GOLD_NUDGE])
            self.assertIn("nudged_at", saved["items"]["3"])
            self.assertEqual(saved["nudge_day"], "2026-10-12")
            ap2 = notify.Approvals(out)
            self.assertEqual(sent.nudge(ap2.data, notify.make_post(ap2, FakeTeams(), send=True), T0 + timedelta(hours=3)), [])
            self.assertEqual(len(notify.Approvals(out).data["outbox"]), 1)

    def test_a_save_that_fails_leaves_neither_the_text_nor_the_state_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.new_out(d)
            ap = notify.Approvals(out)
            post = notify.make_post(ap, FakeTeams(), send=True)
            with mock.patch.object(ap, "save", side_effect=OSError("the disk is full")):
                with self.assertRaises(OSError):
                    sent.nudge(ap.data, post, T0 + timedelta(hours=2))
            saved = notify.Approvals(out).data
            self.assertNotIn("outbox", saved)
            self.assertNotIn("nudge_day", saved)
            self.assertNotIn("nudged_at", saved["items"]["3"])
            ap2 = notify.Approvals(out)                                                            # the next run posts it once
            self.assertEqual(sent.nudge(ap2.data, notify.make_post(ap2, FakeTeams(), send=True), T0 + timedelta(hours=2)), ["3"])
            self.assertEqual(notify.Approvals(out).data["outbox"], ["[kimeru 未送信 #3]\n" + GOLD_NUDGE])


class TestTheNudgeHoursInACycle(unittest.TestCase):
    def test_a_cycle_with_three_hours_nudges_after_three_hours_and_not_before(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {**ON, "KIMERU_SENT_NUDGE_HOURS": "3"}):
            t = FakeTeams()
            out, inbox = approve(d, t)                                                             # the OK at 07:10
            cycle(out, inbox, t, T0 + timedelta(hours=2, minutes=59))                              # 2 h 49 min after the OK
            self.assertEqual(nudges(t), [])
            cycle(out, inbox, t, T0 + timedelta(hours=3, minutes=15))                              # 3 h 5 min after the OK
            self.assertEqual(nudges(t), ["[kimeru 未送信 #1]\n" + GOLD_NUDGE])
            self.assertTrue(all(p.startswith("[kimeru") for p, _ in t.posts))


class TestAnUnsentNudgeWaitsLikeTheNotUrgentItems(unittest.TestCase):
    def test_a_nudge_is_held_during_a_quiet_and_sent_when_it_ends(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            t = FakeTeams()
            out, inbox = approve(d, t)                                                             # the route is not on yet: no push here
            told = []

            def sender(route, text):
                told.append((route, text))
            until = T0 + timedelta(hours=3)                                                        # a 休み until 10:00
            with mock.patch.dict(os.environ, PUSH_ENV):
                push.run(out, now=(T0 + timedelta(hours=1)).timestamp(), sender=sender)            # the baseline
                notify.collect(out, t, real=True, send=True, now=T0 + timedelta(hours=2, minutes=20))   # the nudge
                self.assertEqual(nudges(t), ["[kimeru 未送信 #1]\n" + GOLD_NUDGE])
                during = attention.plan(out, T0 + timedelta(hours=2, minutes=30), until=until)
                self.assertEqual(during, {"sitting_at": None, "hold": {1}})
                push.run(out, now=(T0 + timedelta(hours=2, minutes=30)).timestamp(), sender=sender, hold={"webhook": during["hold"]})
                self.assertEqual(told, [])                                                         # held: not sent, not recorded as sent
                after = attention.plan(out, T0 + timedelta(hours=3, minutes=5), until=until)
                self.assertEqual(after, {"sitting_at": None, "hold": set()})
                push.run(out, now=(T0 + timedelta(hours=3, minutes=5)).timestamp(), sender=sender, hold={"webhook": after["hold"]})
            self.assertEqual(told, [("webhook", "kimeru: 未送信 1 件（#1）。Teams の自分とのチャットを確認してください")])

    def test_without_the_setting_no_nudged_reply_is_held(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            ap = notify.Approvals(out)
            ap.data["items"]["3"] = watched(3, T0, **{sent.NUDGED: "2026-10-12T09:20:00+00:00"})
            ap.save()
            with mock.patch.dict(os.environ, OFF):
                self.assertEqual(attention.unsent(out, T0 + timedelta(hours=3)), set())
            with mock.patch.dict(os.environ, ON):
                self.assertEqual(attention.unsent(out, T0 + timedelta(hours=3)), {3})


class TestTheReadersHandTheNudgeBack(unittest.TestCase):
    def test_both_readers_hand_the_nudge_back_as_a_u_entry_with_its_numbers_only(self):
        for name in ("tools/teams-self.ps1", "tests/fake-teams-self.ps1"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("未送信((?:\\s+#\\d+)+)\\]", text, name)
            self.assertIn("'U:' + ($nums -join ',')", text, name)


class TestTheNudgeSettingAndDocs(unittest.TestCase):
    def test_the_hours_setting_is_a_number_logged_and_shared_like_its_window(self):
        self.assertEqual(config.SETTINGS["sent_nudge_hours"], ("KIMERU_SENT_NUDGE_HOURS", "2"))
        self.assertIn("sent_nudge_hours", config.LOGGABLE)
        self.assertIn("sent_nudge_hours", config.SHARE_VALUES)
        self.assertNotIn("sent_nudge_hours", config.CHOICES)
        self.assertEqual(notify.ACK["sent_confirmed"], "送った（催促を止めました）")

    def test_the_docs_have_one_row_for_it_name_the_new_fields_and_list_the_reply(self):
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        self.assertEqual(ref.count("| `KIMERU_SENT_NUDGE_HOURS` |"), 1)
        sec = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        for field in ("nudged_at", "sent_confirmed", "nudge_day"):
            self.assertIn(f"`{field}`", sec)
        self.assertIn("| `送った N` |", (ROOT / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
