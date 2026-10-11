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

from kimeru import config, daily, fsutil, notify, pull, sent
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


if __name__ == "__main__":
    unittest.main()
