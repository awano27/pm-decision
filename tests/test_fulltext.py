"""Reading a chat in full: only when enabled and needed, within limits, verified, forgotten after the decision. Teams is always a fake."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from kimeru import brief, cli, config, daily, events, fulltext, graph, notify, plan, pull, writer
from kimeru.backends import StubBackend
from tests.test_daily import FakeTeams

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)
BODY = "本文-FULLBODY-ZZ"
LONG = "これは何ですか。" + "詳しく言うと、" * 60 + "以上です。"        # a message far longer than a preview


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name) / "out"
        self.inbox = Path(self.dir.name) / "inbox"
        self.inbox.mkdir()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = self.dir.name
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        config.apply([])
        self.teams = FakeTeams()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def enable(self, **extra):
        os.environ.update({"KIMERU_READ_FULL": "1", **extra})
        config.apply([])

    def ev(self, i, text, chat=None):
        return {"kind": "teams.chat", "id": str(i), "chat_id": chat or f"19:c{i}@thread.v2", "author": f"相手{i}", "text": text, "mentions_me": True}

    def process(self, evs, reader=None):
        res = []
        for e in evs:
            res += cli.process(e, GRAPHS, StubBackend(), self.out, PBS, dedup=True, reader=reader)
        return res

    def decisions(self):
        p = self.out / "decisions.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


class TestWhenToOpen(Base):
    def test_nothing_is_opened_by_default(self):
        self.teams.chat_messages = {"19:c1@thread.v2": [BODY]}
        self.drop_and_cycle([self.ev(1, "これは何ですか…")])
        self.assertEqual(getattr(self.teams, "opened", []), [])

    def drop_and_cycle(self, evs, send=False, **kw):
        for i, e in enumerate(evs):
            (self.inbox / f"e{i}.json").write_text(json.dumps(e, ensure_ascii=False), encoding="utf-8")
        return daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=self.teams, send=send, **kw)

    def test_enabled_but_not_needed_a_chat_is_left_closed(self):
        self.enable()
        self.teams.chat_messages = {"19:c1@thread.v2": ["ありがとうございました"]}
        self.drop_and_cycle([self.ev(1, "ありがとうございました")])       # not cut off, decided by itself, no notice
        self.assertEqual(getattr(self.teams, "opened", []), [])

    def test_a_cut_off_preview_is_read_before_judging(self):
        self.enable()
        self.teams.chat_messages = {"19:c1@thread.v2": ["前の話", BODY + "詳しい内容です"]}
        self.drop_and_cycle([self.ev(1, BODY + "…")])
        self.assertEqual(self.teams.opened, ["19:c1@thread.v2"])
        rec = self.decisions()[0]
        self.assertEqual(rec["read_full"]["state"], "full")

    def test_a_decision_that_ends_at_the_pm_is_looked_at_in_full(self):
        self.enable()
        self.teams.chat_messages = {"19:c1@thread.v2": [LONG]}
        self.drop_and_cycle([self.ev(1, LONG[:60])])
        self.assertEqual(self.teams.opened, ["19:c1@thread.v2"])           # "これは何ですか" ends at the PM (ask_pm)

    def test_the_limit_per_cycle_holds_and_the_rest_waits_for_the_next_cycle(self):
        self.enable(KIMERU_READ_MAX_OPEN="2")
        chats = {f"19:c{i}@thread.v2": [f"{i}番の依頼です。これは何ですか" + "。" * 3] for i in range(1, 5)}
        self.teams.chat_messages = chats
        r = self.drop_and_cycle([self.ev(i, f"{i}番の依頼です。これは何ですか") for i in range(1, 5)])
        self.assertEqual(len(self.teams.opened), 2)                        # never more than the limit
        self.assertEqual(r["waiting"], 2)                                  # the others stay in the inbox
        self.assertEqual(r["perf"]["left_for_next_cycle"], 2)
        self.drop_and_cycle([])                                            # next cycle
        self.assertEqual(len(self.teams.opened), 4)
        self.assertEqual(len(self.decisions()), 4)                         # each event decided once

    def test_a_time_budget_also_stops_opening(self):
        clock = iter([0.0, 70.0, 71.0, 72.0])
        rd = fulltext.Reader(self.teams, max_open=10, seconds=60, clock=lambda: next(clock))
        self.teams.chat_messages = {"19:c1@thread.v2": ["a long message about something"], "19:c2@thread.v2": ["another long message"]}
        rd(self.ev(1, "a long message about something…"))
        with self.assertRaises(fulltext.BudgetExhausted):
            rd(self.ev(2, "another long message…"))


class TestSafety(Base):
    def test_an_unconfirmed_open_reads_nothing_and_the_post_says_the_decision_is_from_the_preview(self):
        self.enable()

        class Cannot(FakeTeams):
            def readchat(self, chat_id, count=5):
                raise RuntimeError("could not confirm that the chat is open (the title and the selection did not agree); nothing was read " + BODY)

        self.teams = Cannot()
        self.drop_and_post([self.ev(1, "これは何ですか")])
        rec = self.decisions()[0]
        self.assertEqual(rec["read_full"]["state"], "preview_only")
        posts = "\n".join(t for t, _ in self.teams.posts)
        self.assertIn("プレビューだけで判断しました", posts)
        self.assertNotIn(BODY, json.dumps(rec, ensure_ascii=False))     # the reason carries no chat text

    def drop_and_post(self, evs):
        for i, e in enumerate(evs):
            (self.inbox / f"e{i}.json").write_text(json.dumps(e, ensure_ascii=False), encoding="utf-8")
        return daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=self.teams, send=True, toaster=lambda a, b: None)

    def test_text_that_does_not_fit_the_preview_is_not_used(self):
        self.enable()
        self.teams.chat_messages = {"19:c1@thread.v2": ["まったく別のチャットの内容です"]}
        self.drop_and_post([self.ev(1, "これは何ですか")])
        self.assertEqual(self.decisions()[0]["read_full"]["state"], "preview_only")
        self.assertIn("別のチャット", self.decisions()[0]["read_full"]["why"])

    def test_a_failure_to_go_back_is_reported(self):
        self.enable()

        class Stuck(FakeTeams):
            def readchat(self, chat_id, count=5):
                r = super().readchat(chat_id, count)
                r["returned"] = False
                return r

        self.teams = Stuck()
        self.teams.chat_messages = {"19:c1@thread.v2": [LONG]}
        self.drop_and_post([self.ev(1, LONG[:60])])
        self.assertIn("元のチャットへ戻せませんでした", "\n".join(t for t, _ in self.teams.posts))

    def test_the_reader_never_writes(self):
        # the bridge method used is readchat only: there is no post / typing call among what the reader does
        calls = []

        class Spy(FakeTeams):
            def post(self, text, send):
                calls.append("post")
                return super().post(text, send)

        spy = Spy()
        spy.chat_messages = {"19:c1@thread.v2": ["some message text that continues"]}
        fulltext.Reader(spy)(self.ev(1, "some message text…"))
        self.assertEqual(calls, [])


class TestQuietAndStorage(Base):
    def test_a_chat_opened_that_needed_nothing_is_listed_in_the_brief_by_name(self):
        self.enable()
        self.teams.chat_messages = {"19:c1@thread.v2": ["ありがとうございました。またよろしくお願いします"]}
        (self.inbox / "a.json").write_text(json.dumps(self.ev(1, "ありがとうございました。またよろ…"), ensure_ascii=False), encoding="utf-8")
        daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=self.teams, send=False)
        text, _ = brief.build(self.out, StubBackend())
        self.assertIn("開いて読みましたが、対応は不要でした", text)
        self.assertIn("相手1", text)
        self.assertIn("既読", text)

    def test_the_full_text_is_kept_while_waiting_and_deleted_after_the_decision(self):
        self.enable()
        self.teams.chat_messages = {"19:c1@thread.v2": [LONG]}
        (self.inbox / "a.json").write_text(json.dumps(self.ev(1, LONG[:60] + "…"), ensure_ascii=False), encoding="utf-8")
        daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=self.teams, send=True, toaster=lambda a, b: None)
        ap = notify.Approvals(self.out)
        n, item = next(iter(ap.data["items"].items()))
        self.assertEqual(fulltext.load(self.out, item["key"])["text"][:20], LONG[:20])          # kept while it waits
        posted = "\n".join(t for t, _ in self.teams.posts if t.startswith("[kimeru #"))
        self.assertIn("元（全文）", posted)
        rec = self.decisions()[0]                                                                # the permanent records: a summary
        self.assertLessEqual(len(rec["event"]["text"]), fulltext.PERSIST_TEXT)
        self.assertNotIn(LONG[:260], (self.out / "decisions.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn(LONG[:260], (self.out / "queue.jsonl").read_text(encoding="utf-8"))
        self.teams.timeline += [f"R:OK {n}"]
        self.teams.replies = [f"OK {n}"]
        notify.collect(self.out, self.teams)
        self.assertIsNone(fulltext.load(self.out, item["key"]))                                  # forgotten after the approval
        self.assertNotIn(LONG[:260], (self.out / "full_text.json").read_text(encoding="utf-8") if (self.out / "full_text.json").exists() else "")


class TestJudgeAndTitles(Base):
    def test_the_judge_sees_a_cut_text_and_the_writer_sees_the_whole(self):
        ev = self.ev(1, "あ" * 3000)
        self.assertEqual(len(events.state_of(ev)["text"]), 1200)
        os.environ["KIMERU_JUDGE_TEXT_MAX"] = "500"
        config.apply([])
        self.assertEqual(len(events.state_of(ev)["text"]), 500)
        res = graph.run(GRAPHS["teams.chat"][0], self.ev(1, "い" * 1500), StubBackend(), playbooks=PBS)
        self.assertGreater(len(writer._material(res, self.ev(1, "い" * 1500))), 1400)              # the material keeps it

    def test_the_thread_goes_to_the_writer_not_to_the_judge(self):
        ev = {**self.ev(1, "最後のメッセージ"), "thread": ["前のメッセージ"]}
        self.assertNotIn("thread", events.state_of(ev))
        self.assertIn("前のメッセージ", writer._material({"path": [], "node": "n", "actions": []}, ev))

    def test_an_action_title_is_one_short_line(self):
        long_text = "リリース日を来週にずらしてよいか\n判断お願いします。" + "詳しくは以下の通りです。" * 30
        (self.inbox / "a.json").write_text(json.dumps(self.ev(1, long_text + "今週中に判断してください"), ensure_ascii=False), encoding="utf-8")
        cli.process(self.ev(1, long_text + "今週中に判断してください"), GRAPHS, StubBackend(), self.out, PBS)
        titles = [a["title"] for r in self.decisions() for a in r["actions"] if a.get("title")]
        self.assertTrue(titles)
        for t in titles:
            self.assertNotIn("\n", t)
            self.assertLessEqual(len(t), 130)                                                     # the title cap plus the suffix text


class TestDedupAndMerge(Base):
    def test_only_the_displayed_time_changing_is_not_a_new_event(self):
        sec = {}
        chat = {"id": "19:x@unq.gbl.spaces", "kind": "oneOnOne", "title": "X", "preview": "確認をお願いします", "time": "10:05", "mention": False}
        pull.teams_events([chat], sec)                                     # baseline
        self.assertEqual(pull.teams_events([{**chat, "time": "昨日"}], sec), [])
        self.assertEqual(len(pull.teams_events([{**chat, "preview": "確認をお願いします。急ぎです"}], sec)), 1)

    def test_a_follow_up_in_the_same_chat_joins_the_waiting_item(self):
        chat = "19:same@thread.v2"
        cli.process(self.ev(1, "これは何ですか", chat), GRAPHS, StubBackend(), self.out, PBS, dedup=True)
        notify.notify(self.out, self.teams, send=True)
        cli.process(self.ev(2, "あと、期限は今週です", chat), GRAPHS, StubBackend(), self.out, PBS, dedup=True)
        ap = notify.Approvals(self.out)
        self.assertEqual(len(ap.data["items"]), 1)                        # not a second item
        item = next(iter(ap.data["items"].values()))
        self.assertEqual(item["record"]["followups"][0]["text"], "あと、期限は今週です")
        self.assertFalse(item["posted"])                                  # posted again under the same number
        self.teams.posts.clear()
        notify.notify(self.out, self.teams, send=True)
        self.assertIn("続きのメッセージ", self.teams.posts[0][0])
        self.assertEqual(len(self.decisions()), 1)


class TestBridgeCall(unittest.TestCase):
    def test_powershell_bridge_passes_the_chat_id_and_count(self):
        b = notify.PowerShellBridge()
        with mock.patch.object(b, "_run", return_value={"ok": True, "messages": []}) as run:
            b.readchat("19:abc@thread.v2", 4)
        run.assert_called_once_with("-Action", "readchat", "-ChatId", "19:abc@thread.v2", "-Count", "4")


class TestScriptGuards(unittest.TestCase):
    """The script cannot run here; the rules it must keep are checked in its text."""

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "tools" / "teams-self.ps1").read_text(encoding="utf-8-sig")
        i = cls.text.index("if ($Action -eq 'readchat')")
        cls.block = cls.text[i:i + 2500]

    def test_it_never_touches_the_input_box_or_sends(self):
        for bad in ("SendKeys", "Send-Box", "Clear-OurBox", "Paste", "SetValue", "Get-Box"):
            self.assertNotIn(bad, self.block)

    def test_it_confirms_by_title_and_selection_and_goes_back(self):
        self.assertIn("Select-Chat", self.block)
        self.assertIn("Test-ChatOpen", self.text)
        i = self.text.index("function Test-ChatOpen")
        self.assertIn(".Contains(\"| $title |\")", self.text[i:i + 700])     # the title
        self.assertIn("Test-Selected", self.text[i:i + 900])                  # the selection
        self.assertIn("returned", self.block)

    def test_it_takes_the_lock_and_waits_for_a_quiet_keyboard(self):
        self.assertIn("'readchat'", self.text.split("Enter-UiLock }")[0].split("\n")[-1])
        self.assertIn("Wait-UserIdle", self.text.split("Enter-UiLock }")[1].split("\n")[1])


@unittest.skipUnless(__import__("shutil").which("powershell"), "PowerShell not available")
class TestThroughTheFakeScript(unittest.TestCase):
    """PowerShellBridge -> tests/fake-teams-self.ps1: chats and readchat travel the same command line as with the real script."""

    def test_chats_and_readchat(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "chats.json"
            f.write_text(json.dumps({"chats": [{"id": "19:x@thread.v2", "kind": "group", "title": "T", "preview": "P…", "time": "09:00",
                                                 "messages": ["前", "本文", "最後のメッセージ"]}]}, ensure_ascii=False), encoding="utf-8")
            log = Path(d) / "opened.txt"
            with mock.patch.dict(os.environ, {"KIMERU_FAKE_CHATS": str(f), "KIMERU_FAKE_OPENLOG": str(log), "KIMERU_FAKE_CHAT": str(Path(d) / "self.json")}):
                b = notify.PowerShellBridge(script=ROOT / "tests" / "fake-teams-self.ps1")
                chats = b.chats()
                self.assertEqual([c["id"] for c in chats], ["19:x@thread.v2"])
                self.assertEqual(log.exists(), False)                      # listing the chats opens nothing
                r = b.readchat("19:x@thread.v2", 2)
                self.assertEqual([m["text"] for m in r["messages"]], ["本文", "最後のメッセージ"])
                self.assertTrue(r["returned"])
                with self.assertRaises(RuntimeError) as cm:
                    b.readchat("19:none@thread.v2", 2)
                self.assertIn("nothing was opened", str(cm.exception))
            self.assertEqual(log.read_text(encoding="utf-8-sig").split(), ["19:x@thread.v2"])


if __name__ == "__main__":
    unittest.main()
