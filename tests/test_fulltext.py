"""Reading a chat in full: only when enabled and needed, within limits, verified, forgotten after the decision. Teams is always a fake."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
    from .isolate import detail_of
except ImportError:
    import isolate  # noqa: F401
    from isolate import detail_of

import json
import os
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru.cli import _no_action
from kimeru import brief, cli, config, daily, events, fulltext, graph, notify, plan, pull, writer
from kimeru.backends import StubBackend
from tests.test_daily import FakeTeams

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)
BODY = "本文-FULLBODY-ZZ"
Q = "これは何ですか。至急の確認をお願いします"     # a preview long enough (12 characters or more) to identify a chat
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
        self.assertEqual(r["waiting"], 0)                                  # the judge is fine: not a failure
        self.assertEqual(r["left_for_next_cycle"], 2)                      # the others stay in the inbox
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
        self.drop_and_post([self.ev(1, Q)])
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
        self.drop_and_post([self.ev(1, Q)])
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
        self.assertNotIn("元（全文）", posted)                                                    # the short post does not carry it
        self.assertIn("元（全文）", detail_of(self.out, n))                                      # the answer to `詳細 N` does
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
        cli.process({**self.ev(1, "これは何ですか", chat), "author": "田中"}, GRAPHS, StubBackend(), self.out, PBS, dedup=True)
        notify.notify(self.out, self.teams, send=True)
        cli.process({**self.ev(2, "あと、期限は今週です", chat), "author": "田中"}, GRAPHS, StubBackend(), self.out, PBS, dedup=True)
        ap = notify.Approvals(self.out)
        self.assertEqual(len(ap.data["items"]), 1)                        # not a second item
        item = next(iter(ap.data["items"].values()))
        self.assertEqual(item["record"]["followups"][0]["text"], "あと、期限は今週です")
        self.assertFalse(item["posted"])                                  # posted again under the same number
        self.teams.posts.clear()
        notify.notify(self.out, self.teams, send=True)
        self.assertIn("（更新）", self.teams.posts[0][0])
        self.assertIn("続きのメッセージ", detail_of(self.out))
        self.assertEqual(len(self.decisions()), 2)                        # judged, not skipped; only the item is shared

    def test_a_follow_up_is_still_judged_when_it_joins(self):
        chat = "19:same@thread.v2"
        cli.process({**self.ev(1, "これは何ですか", chat), "author": "田中"}, GRAPHS, StubBackend(), self.out, PBS, dedup=True)
        notify.notify(self.out, self.teams, send=True)
        res = cli.process({**self.ev(2, "あと、期限は今週です", chat), "author": "田中"}, GRAPHS, StubBackend(), self.out, PBS, dedup=True)
        self.assertEqual(len(res), 1)                                     # the judge was asked
        self.assertTrue(res[0].get("merged"))

    def test_another_person_in_the_same_group_chat_is_a_matter_of_its_own(self):
        chat = "19:group@thread.v2"
        cli.process({**self.ev(1, "これは何ですか", chat), "author": "田中"}, GRAPHS, StubBackend(), self.out, PBS, dedup=True)
        notify.notify(self.out, self.teams, send=True)
        res = cli.process({**self.ev(2, "本番でエラーが出て、全ユーザーがログインできません", chat), "author": "鈴木"},
                          GRAPHS, StubBackend(), self.out, PBS, dedup=True)
        self.assertFalse(res[0].get("merged"))
        self.assertTrue(res[0]["notify"])                                 # an incident: reaches the PM, is filed as a bug
        self.assertEqual([a["type"] for a in res[0]["actions"]], ["ado.create"])
        self.assertEqual(len(notify.Approvals(self.out).data["items"]), 1)   # the earlier item is not touched
        self.assertNotIn("followups", next(iter(notify.Approvals(self.out).data["items"].values()))["record"])

    def test_a_thanks_from_the_same_person_joins_the_waiting_item(self):
        chat = "19:same@thread.v2"
        cli.process({**self.ev(1, "これは何ですか", chat), "author": "田中"}, GRAPHS, StubBackend(), self.out, PBS, dedup=True)
        notify.notify(self.out, self.teams, send=True)
        res = cli.process({**self.ev(2, "ありがとうございます", chat), "author": "田中"}, GRAPHS, StubBackend(), self.out, PBS, dedup=True)
        self.assertTrue(_no_action(res[0]))
        self.assertTrue(res[0].get("merged"))
        item = next(iter(notify.Approvals(self.out).data["items"].values()))
        self.assertEqual(item["record"]["followups"][0]["text"], "ありがとうございます")

    def test_a_different_destination_from_the_same_person_is_a_matter_of_its_own(self):
        chat = "19:same@thread.v2"
        cli.process({**self.ev(1, "これは何ですか", chat), "author": "田中"}, GRAPHS, StubBackend(), self.out, PBS, dedup=True)
        notify.notify(self.out, self.teams, send=True)
        res = cli.process({**self.ev(2, "本番でエラーが出て、全ユーザーがログインできません", chat), "author": "田中"},
                          GRAPHS, StubBackend(), self.out, PBS, dedup=True)
        self.assertFalse(res[0].get("merged"))                            # it notifies: never folded into the earlier item


class TestBridgeCall(unittest.TestCase):
    def test_powershell_bridge_passes_the_chat_id_and_count(self):
        b = notify.PowerShellBridge()
        with mock.patch.object(b, "_run", return_value={"ok": True, "messages": []}) as run:
            b.readchat("19:abc@thread.v2", 4)
        run.assert_called_once_with("-Action", "readchat", "-ChatId", "19:abc@thread.v2", "-Count", "4")


def _ps_functions(text):
    """{name: body} of every function in the script (brace matching; comments are not part of the body)."""
    out = {}
    for m in re.finditer(r"(?m)^function ([\w-]+)", text):
        i = text.index("{", m.end())
        depth, j = 0, i
        while j < len(text):
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
            if depth == 0:
                break
        out[m.group(1)] = re.sub(r"(?m)(^|\s)#[^\n]*", "", text[i:j])
    return out


def _ps_source(names):
    """The full text (signature and body, comments kept) of the named functions of tools/teams-self.ps1, to be run in a PowerShell that
    has no Teams: only functions that touch no window are loaded this way, with stubs for what they would call."""
    text = (ROOT / "tools" / "teams-self.ps1").read_text(encoding="utf-8-sig")
    parts = []
    for name in names:
        m = re.search(r"(?m)^function " + re.escape(name) + r"\b", text)
        assert m, name
        i = text.index("{", m.end())
        depth, j = 0, i
        while j < len(text):
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
            if depth == 0:
                break
        parts.append(text[m.start():j])
    return "\n".join(parts)


def _run_ps(code, src, env=None):
    """Run `src` (function definitions) and then `code` in PowerShell; every output line "K=V" comes back as {K: V}."""
    if os.name != "nt":
        raise unittest.SkipTest("Windows PowerShell only: tools/teams-self.ps1 loads the Windows UI Automation types")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "t.ps1"
        f.write_text("$ErrorActionPreference = 'Stop'\n[Console]::OutputEncoding = [Text.Encoding]::UTF8\n" + src + "\n" + code, encoding="utf-8-sig")
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean.update(env or {})
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(f)], capture_output=True, text=True,
                           encoding="utf-8", timeout=120, env=clean)
    assert r.returncode == 0, r.stdout + r.stderr
    return dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)


class TestScriptGuards(unittest.TestCase):
    """The script cannot run here; the rules it must keep are checked in its text."""

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "tools" / "teams-self.ps1").read_text(encoding="utf-8-sig")
        i = cls.text.index("if ($Action -eq 'readchat') {\n")
        j = cls.text.index("if ($Action -eq 'chats') {")
        cls.block = cls.text[i:j]
        cls.funcs = _ps_functions(cls.text)

    @classmethod
    def block_text(cls):
        cls.funcs_text()
        return re.sub(r"(?m)(^|\s)#[^\n]*", "", cls.block)

    @classmethod
    def funcs_text(cls):
        if not hasattr(cls, "funcs"):
            cls.setUpClass()
        return cls.funcs

    @classmethod
    def reachable_text(cls):
        cls.funcs_text()
        return cls.reachable(cls)

    def reachable(self):
        """readchat's own text and every function it calls, directly or through another (the whole of it)."""
        seen, todo, body = set(), [re.sub(r"(?m)(^|\s)#[^\n]*", "", self.block)], []
        while todo:
            t = todo.pop()
            body.append(t)
            for name, fn in self.funcs.items():
                if name not in seen and re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", t):
                    seen.add(name)
                    todo.append(fn)
        return seen, "\n".join(body)

    def test_readchat_and_everything_it_calls_never_touch_the_input_box_or_send(self):
        seen, body = self.reachable()
        self.assertTrue({"Select-Chat", "Restore-Original", "Select-Notes", "Get-ChatMessages", "Test-ChatOpen", "Assert-Foreground"} <= seen, seen)
        for bad in ("SendKeys", "SendWait", "SetFocus", "Clipboard", "SetValue", "ValuePattern", "TextPattern", "Paste",
                    "Get-Box", "Send-Box", "Clear-OurBox", "Test-BoxFocused", "Open-SelfChat", "new-message"):
            self.assertNotIn(bad, body, f"{bad} is reachable from readchat")
        self.assertNotIn("Open-SelfChat", seen)

    def test_it_confirms_by_title_and_selection_and_a_title_alone_is_not_enough(self):
        self.assertIn(".Contains(\"| $title |\")", self.funcs["Test-ChatOpen"])     # the title
        self.assertIn("Test-Selected", self.funcs["Test-ChatOpen"])                 # the selection
        self.assertIn("'title'", self.funcs["Test-ChatOpen"])                        # only the title agreed: reported as such
        self.assertIn("$state -eq 'title' -and -not $matched", self.block)          # ... then the text read must match the preview
        self.assertIn("Test-PreviewMatch", self.block)
        self.assertIn(".Length -lt 12", self.block)                                  # a short preview cannot confirm anything there
        self.assertIn("$head.Length -lt 12", self.funcs["Test-PreviewMatch"])         # the same minimum on every route
        self.assertIn("(.{12,})", self.funcs["Test-PreviewMatch"])                   # ... also after "name:" is taken off
        self.assertNotIn("-not $origId -and $head.Length", self.block)                # not only on a screen that reports no selection

    def test_every_way_out_of_readchat_puts_the_chat_back(self):
        blk = re.sub(r"(?m)(^|\s)#[^\n]*", "", self.block)
        self.assertIn("if ($script:InRead) { throw", self.funcs["Fail"])            # a failure does not end the script
        self.assertIn("$script:InRead = $true", blk)
        try_end = blk.index("} catch {")
        restore_at = blk.index("Restore-Original", try_end)
        self.assertLess(try_end, restore_at)                                         # after the try / catch, whatever happened
        self.assertNotIn("exit", blk[:restore_at])                                   # nothing leaves before it
        self.assertNotIn("Restore-Original", blk[:try_end])                          # and it is not only on the success path
        self.assertIn("Restore-Foreground", blk[restore_at:])
        for k in ("restore", "returned", "hadOriginal"):                            # reported on failure too
            self.assertIn(k + " = ", blk[blk.index("if ($errMsg)"):blk.index("exit 2")])

    def test_the_original_chat_is_remembered_by_selection_and_by_title_and_the_self_chat_is_the_fallback(self):
        self.assertIn("Get-SelectedChatId", self.block)
        self.assertIn("Get-WinChatTitle", self.block)
        body = self.funcs["Restore-Original"]
        self.assertLess(body.index("Test-OrigOpen"), body.index("Select-Chat"))
        self.assertLess(body.index("Select-Chat"), body.index("Select-Notes"))       # the original first, the self chat after it
        self.assertIn("'failed'", body)                                              # neither worked: reported (the PC is notified)

    def test_the_keyboard_and_mouse_are_checked_before_each_operation(self):
        sc = self.funcs["Select-Chat"]
        self.assertIn("Assert-Idle", sc)
        self.assertLess(sc.index("Assert-Idle"), sc.index("ScrollIntoView"))
        loop = sc[sc.index("foreach ($how"):]
        self.assertLess(loop.index("Assert-Idle"), loop.index("switch ($how)"))      # before select, invoke and click
        self.assertLess(loop.index("Assert-Idle"), loop.index("Assert-Foreground"))
        self.assertIn("Fail", self.funcs["Assert-Idle"])
        self.assertIn("Test-UserIdle", self.funcs["Select-Notes"])
        self.assertIn("Wait-IdleSoft", self.funcs["Restore-Original"])
        self.assertIn("Mark-OwnInput", sc)                                           # our own click is not mistaken for the person
        self.assertIn(fulltext.BUSY, self.funcs["Assert-Idle"])                       # the words the Python side recognises

    def test_the_window_the_person_was_in_is_brought_back(self):
        self.assertIn("$script:PrevFg = $cur", self.funcs["Assert-Foreground"])
        self.assertIn("SetForegroundWindow($script:PrevFg)", self.funcs["Restore-Foreground"])

    def test_it_takes_the_lock_and_waits_for_a_quiet_keyboard(self):
        lock = next(l for l in self.text.splitlines() if l.startswith("if ($Action -in 'open', 'post', 'send', 'read', 'readchat')"))
        self.assertIn("Enter-UiLock", lock)
        idle = next(l for l in self.text.splitlines() if l.startswith("if ($Action -in 'post', 'send')"))
        self.assertIn("Wait-UserIdle", idle)
        self.assertNotIn("Wait-UserIdle", self.block)             # readchat does not wait for the person: it checks and postpones

    def test_the_self_chat_opener_sends_enter_only_when_a_list_item_has_the_focus(self):
        body = self.funcs["Open-SelfChat"]
        enter = body[body.index("'enter' {"):]
        self.assertLess(enter.index("Test-ListItemFocused"), enter.index("SendWait"))
        f = self.funcs["Test-ListItemFocused"]
        self.assertIn("new-message-*", f)
        self.assertIn("return $false", f)
        self.assertIn("ListItem", f)

    def test_the_script_parses(self):
        if not __import__("shutil").which("powershell"):
            self.skipTest("PowerShell not available")
        for name in ("teams-self.ps1", "check.ps1"):
            cmd = ("& { param($f) $e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($f,[ref]$t,[ref]$e);"
                   "if($e){$e|%{$_.Message};exit 1} } '" + str(ROOT / "tools" / name) + "'")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, name)


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

    def test_a_failed_read_still_reports_how_the_chat_was_put_back(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "chats.json"
            f.write_text(json.dumps({"chats": [{"id": "19:y@thread.v2", "kind": "group", "title": "T", "preview": "P…", "time": "09:00",
                                                 "messages": ["前"], "restore": "self", "error": "could not confirm that the chat is open (the title and the selection did not agree); nothing was read"}]},
                                    ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {"KIMERU_FAKE_CHATS": str(f), "KIMERU_FAKE_CHAT": str(Path(d) / "self.json")}):
                b = notify.PowerShellBridge(script=ROOT / "tests" / "fake-teams-self.ps1")
                r = fulltext.Reader(b)({"chat_id": "19:y@thread.v2", "text": Q + "…"})
        self.assertFalse(r["ok"])
        self.assertEqual((r["restore"], r["returned"]), ("self", False))


class CountingBackend(StubBackend):
    """The stub judge, counting what it is asked about (by a marker in the text)."""

    def __init__(self):
        super().__init__()
        self.texts = []

    def ask(self, state, questions):
        self.texts.append(str(state.get("text", "")))
        return super().ask(state, questions)

    def judged(self, marker):
        return sum(1 for t in self.texts if marker in t)


class CountingM365(writer.M365PromptWriter):
    """No LLM: counts how often a paste-in request (a draft) is written."""

    def __init__(self):
        self.n = 0
        self.seen = []

    def request(self, res, event, instruction=None):
        self.n += 1
        self.seen.append(event.get("text", ""))
        return super().request(res, event, instruction)


def all_text(*dirs):
    """Every file under the given folders, as one string (what is left on the disk)."""
    parts = []
    for d in dirs:
        for f in Path(d).rglob("*"):
            if f.is_file():
                parts.append(f.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def long_text(n=2500):
    """A long request in which every 100-character stretch has its own marker (so a leak of any part can be found)."""
    s = "これは何ですか。"
    i = 0
    while len(s) < n:
        s += f"【区間{i:02d}】" + "詳しく言うと、" * 12
        i += 1
    return s[:n]


class TestPutBackAndRecord(Base):
    """Whether the chat that was open could be put back is recorded whatever the decision was; a chat that could not be put back
    is reported on this PC. (The screen work itself is in the script; its text is checked in TestScriptGuards.)"""

    def run_cycle(self, teams, toasts=None, text=Q, **kw):
        (self.inbox / "a.json").write_text(json.dumps(self.ev(1, text), ensure_ascii=False), encoding="utf-8")
        return daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=teams, send=True,
                           toaster=(lambda a, b: toasts.append((a, b))) if toasts is not None else (lambda a, b: None), **kw)

    def test_a_chat_that_could_not_be_read_is_recorded_with_how_it_was_put_back(self):
        self.enable()

        class Failing(FakeTeams):
            def readchat(self, chat_id, count=5, preview=None):
                raise notify.BridgeError("could not confirm that the chat is open (the title and the selection did not agree); nothing was read",
                                         {"restore": "restored", "returned": True, "hadOriginal": True})

        self.run_cycle(Failing())
        rf = self.decisions()[0]["read_full"]
        self.assertEqual((rf["state"], rf["returned"], rf["restore"]), ("preview_only", True, "restored"))
        self.assertNotIn("note", rf)                       # back where it was: nothing to warn about

    def test_a_failure_in_the_middle_is_recorded_too(self):
        self.enable()

        class Slow(FakeTeams):
            def readchat(self, chat_id, count=5, preview=None):
                raise notify.BridgeError("unexpected error while reading the chat (InvalidOperationException)", {"restore": "restored", "returned": True})

        self.run_cycle(Slow())
        rf = self.decisions()[0]["read_full"]
        self.assertEqual((rf["state"], rf["returned"]), ("preview_only", True))
        self.assertEqual(rf["why"], "unexpected error while reading the chat")   # the reason is the script's own fixed text, without its detail

    def test_a_chat_that_could_not_be_put_back_moves_to_the_self_chat_and_the_pc_is_notified(self):
        self.enable()

        class ToSelf(FakeTeams):
            def readchat(self, chat_id, count=5, preview=None):
                r = super().readchat(chat_id, count)
                return {**r, "returned": False, "restore": "self", "hadOriginal": True}

        teams, toasts = ToSelf(), []
        teams.chat_messages = {"19:c1@thread.v2": [LONG]}
        self.run_cycle(teams, toasts, LONG[:60])
        rf = self.decisions()[0]["read_full"]
        self.assertEqual((rf["state"], rf["returned"], rf["restore"]), ("full", False, "self"))     # recorded although the decision was made
        self.assertIn("自分とのチャットへ移しました", rf["note"])
        self.assertIn("自分とのチャットへ移しました", "\n".join(t for t, _ in teams.posts))
        told = [t for t in toasts if "チャットの表示" in t[0]]
        self.assertEqual(len(told), 1)                                                              # the PC is told
        self.assertIn("自分とのチャット", told[0][1])

    def test_when_even_the_self_chat_cannot_be_opened_the_pc_is_told_to_look(self):
        self.enable()

        class Lost(FakeTeams):
            def readchat(self, chat_id, count=5, preview=None):
                raise notify.BridgeError("the script did not finish in time", {"restore": "failed"})

        toasts = []
        self.run_cycle(Lost(), toasts)
        rf = self.decisions()[0]["read_full"]
        self.assertEqual((rf["state"], rf["returned"], rf["restore"]), ("preview_only", False, "failed"))
        self.assertIn("戻せませんでした", rf["note"])
        told = [t for t in toasts if "チャットの表示" in t[0]]
        self.assertEqual(len(told), 1)
        self.assertIn("Teams を確認", told[0][1])

    def test_no_notification_when_the_chat_was_put_back(self):
        self.enable()
        teams, toasts = FakeTeams(), []
        teams.chat_messages = {"19:c1@thread.v2": [LONG]}
        self.run_cycle(teams, toasts, LONG[:60])
        self.assertEqual([t for t in toasts if "チャットの表示" in t[0]], [])

    def test_the_person_at_the_keyboard_means_nothing_is_done_and_the_file_waits(self):
        self.enable()

        class Busy(FakeTeams):
            def readchat(self, chat_id, count=5, preview=None):
                raise notify.BridgeError("the keyboard / mouse has been in use; stopped (set KIMERU_IDLE_SEC=0 to switch this off)",
                                         {"restore": "none", "returned": True, "hadOriginal": True})

        r = self.run_cycle(Busy())
        self.assertEqual(self.decisions(), [])               # not decided from the preview
        self.assertEqual(r["left_for_next_cycle"], 1)         # it waits for the next cycle
        self.assertEqual(r["waiting"], 0)


class TestShortPreview(Base):
    def test_a_preview_of_three_characters_is_not_read_and_the_preview_decides(self):
        self.enable()
        self.teams.chat_messages = {"19:c1@thread.v2": ["お願いします。この件を確認してください"]}
        (self.inbox / "a.json").write_text(json.dumps(self.ev(1, "お願い…"), ensure_ascii=False), encoding="utf-8")
        daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=self.teams, send=True, toaster=lambda a, b: None)
        self.assertEqual(getattr(self.teams, "opened", []), [])          # nothing was opened
        rf = self.decisions()[0]["read_full"]
        self.assertEqual(rf["state"], "preview_only")
        self.assertIn("短く", rf["why"])
        self.assertIn("プレビューだけで判断しました", "\n".join(t for t, _ in self.teams.posts))

    def test_a_short_preview_matches_almost_any_text_so_it_is_not_accepted(self):
        self.assertFalse(fulltext._fits("はい…", "はいはい、それはそうですね。ところで別の話です"))
        self.assertTrue(fulltext._fits("来週のリリースについて確認…", "来週のリリースについて確認したいことがあります"))
        self.assertTrue(fulltext._fits("田中: 来週のリリースについて確認…", "来週のリリースについて確認したいことがあります"))

    def test_the_minimum_length_is_a_setting(self):
        text = "お願いします。この件を確認してください、よろしく"      # 24 characters
        self.teams.chat_messages = {"19:c1@thread.v2": [text + "。以上です"]}
        self.enable(KIMERU_READ_MIN_PREVIEW="30")                      # stricter than the floor: 24 characters are too few
        fulltext.Reader(self.teams)(self.ev(1, text + "…"))
        self.assertEqual(getattr(self.teams, "opened", []), [])
        self.enable(KIMERU_READ_MIN_PREVIEW="20")
        fulltext.Reader(self.teams)(self.ev(1, text + "…"))
        self.assertEqual(self.teams.opened, ["19:c1@thread.v2"])

    def test_the_floor_is_12_characters_whatever_the_setting(self):
        self.enable(KIMERU_READ_MIN_PREVIEW="2")
        self.teams.chat_messages = {"19:c1@thread.v2": ["お願いします。この件を確認してください"]}
        r = fulltext.Reader(self.teams)(self.ev(1, "お願い…"))
        self.assertFalse(r["ok"])
        self.assertEqual(getattr(self.teams, "opened", []), [])


class TestCutOffByLength(Base):
    def test_the_preview_length_can_decide_that_a_preview_is_cut(self):
        self.assertFalse(fulltext.truncated("あ" * 100))                 # the default: the ellipsis only
        self.enable(KIMERU_PREVIEW_CUT_LEN="80")
        self.assertTrue(fulltext.truncated("あ" * 80))
        self.assertFalse(fulltext.truncated("あ" * 79))
        self.assertTrue(fulltext.truncated("短いが…"))                   # the ellipsis still counts

    def test_a_long_preview_without_a_mark_is_read_when_the_length_says_so(self):
        self.enable(KIMERU_PREVIEW_CUT_LEN="60")
        self.teams.chat_messages = {"19:c1@thread.v2": [BODY * 8 + "以上"]}
        (self.inbox / "a.json").write_text(json.dumps(self.ev(1, (BODY * 8)[:70]), ensure_ascii=False), encoding="utf-8")
        daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=self.teams, send=False)
        self.assertEqual(self.teams.opened, ["19:c1@thread.v2"])


class TestDraftOnceAndBudget(Base):
    def event_for(self, i, marker):
        return self.ev(i, f"{marker}番の依頼です。これは何ですか")

    def teams_for(self, ids):
        t = FakeTeams()
        t.chat_messages = {f"19:c{i}@thread.v2": [f"{i}番の依頼です。これは何ですか。" + "詳しくは以下の通りです。" * 30] for i in ids}
        return t

    def test_a_confirmation_for_the_pm_is_drafted_once_even_though_it_was_judged_twice(self):
        self.enable()
        w, b = CountingM365(), CountingBackend()
        self.teams = self.teams_for([1])
        cli.process(self.event_for(1, 1), GRAPHS, b, self.out, PBS, writer=w, dedup=True, reader=fulltext.Reader(self.teams))
        self.assertEqual(self.teams.opened, ["19:c1@thread.v2"])         # it was read
        self.assertEqual(w.n, 1)                                          # the writer was asked once
        self.assertIn("以下の通り", w.seen[0])                            # ... for the whole text, not the preview
        base = CountingBackend()
        cli.process(self.event_for(1, 1), GRAPHS, base, self.out / "b", PBS, writer=CountingM365(), dedup=True)
        self.assertEqual(len(b.texts), 2 * len(base.texts))               # judged from the preview, then from the whole text

    def test_a_cut_off_preview_is_read_first_and_also_drafted_once(self):
        self.enable()
        w = CountingM365()
        self.teams = self.teams_for([1])
        cli.process(self.ev(1, "1番の依頼です。これは何ですか…"), GRAPHS, StubBackend(), self.out, PBS, writer=w, dedup=True, reader=fulltext.Reader(self.teams))
        self.assertEqual(w.n, 1)

    def test_an_event_over_the_opening_limit_is_judged_and_drafted_once_however_many_cycles_it_waits(self):
        self.enable(KIMERU_READ_MAX_OPEN="1")
        w, b = CountingM365(), CountingBackend()
        self.teams = self.teams_for([1, 2, 3, 4])
        evs = [self.event_for(i, i) for i in (1, 2, 3, 4)]
        counts = []
        for cycle_no in range(4):
            rd = fulltext.Reader(self.teams)                              # a new budget every cycle
            for e in evs:
                try:
                    cli.process(e, GRAPHS, b, self.out, PBS, writer=w, dedup=True, reader=rd)
                except fulltext.BudgetExhausted:
                    pass
            counts.append((b.judged("4番の依頼"), sum(1 for t in w.seen if "4番の依頼" in t)))
        # event 4 waited in cycles 1, 2 and 3: one judgment (the preview) and no draft; the fourth cycle reads it: the whole
        # text is judged and drafted, once
        self.assertEqual(counts[0], (1, 0))
        self.assertEqual(counts[1], (1, 0))
        self.assertEqual(counts[2], (1, 0))
        self.assertEqual(counts[3], (2, 1))
        self.assertEqual(len(self.decisions()), 4)
        self.assertEqual(w.n, 4)                                          # each event drafted once
        self.assertFalse((self.out / "read_pending.json").exists() and json.loads((self.out / "read_pending.json").read_text(encoding="utf-8")))

    def test_the_pending_judgments_are_not_kept_forever(self):
        pend = cli._PreviewJudgments(self.out)
        pend.put("k", {"a": 1})
        self.assertEqual(pend.get("k"), {"a": 1})
        pend.data["k"]["t"] -= 3 * 24 * 3600
        self.assertIsNone(pend.get("k"))

    def test_a_limit_of_zero_means_the_preview_decides_and_nothing_waits(self):
        for key in ("KIMERU_READ_MAX_OPEN", "KIMERU_READ_BUDGET"):
            with self.subTest(key):
                self.enable(**{key: "0"})
                self.teams = self.teams_for([1])
                out = self.out / key
                res = cli.process(self.event_for(1, 1), GRAPHS, StubBackend(), out, PBS, dedup=True, reader=fulltext.Reader(self.teams))
                self.assertEqual(getattr(self.teams, "opened", []), [])
                self.assertEqual(len(res), 1)                              # decided (not left for the next cycle)
                self.assertEqual(res[0]["read_full"]["state"], "preview_only")
                self.assertNotIn("returned", res[0]["read_full"])          # nothing was opened, so nothing to put back
                self.assertFalse(res[0].get("merged"))
                del os.environ[key]


class TestNoFullTextLeftBehind(Base):
    def setUp(self):
        super().setUp()
        self.long = long_text(2500)
        self.marks = {f"【区間{i:02d}】": self.long.index(f"【区間{i:02d}】") for i in range(20) if f"【区間{i:02d}】" in self.long}

    def flow(self):
        self.enable()
        self.teams.chat_messages = {"19:c1@thread.v2": [self.long]}
        w = writer.M365PromptWriter()
        cli.process(self.ev(1, self.long[:70] + "…"), GRAPHS, StubBackend(), self.out, PBS, writer=w, dedup=True, reader=fulltext.Reader(self.teams))
        return notify.notify(self.out, self.teams, send=True)

    def test_a_2500_character_text_is_in_no_jsonl_file_and_the_request_still_carries_it(self):
        posted = self.flow()
        self.assertEqual(len(posted), 1)
        late = next(m for m, at in self.marks.items() if 300 < at < 1500)      # a stretch beyond the excerpt (200 characters)
        for f in Path(self.out).rglob("*.jsonl"):
            self.assertNotIn(late, f.read_text(encoding="utf-8"), f.name)
        approvals = (self.out / "approvals.json").read_text(encoding="utf-8")
        self.assertNotIn(late, approvals)                                       # the record holds the excerpt
        self.assertIn(late, (self.out / "full_text.json").read_text(encoding="utf-8"))   # the whole text waits here, apart
        request = next(t for t, _ in self.teams.posts if "Copilot 用" in t)
        self.assertIn(late, request)                                            # what the person pastes into Copilot has the whole text

    def test_after_the_approval_no_file_holds_any_part_of_it(self):
        self.flow()
        n = next(iter(notify.Approvals(self.out).data["items"]))
        self.teams.timeline += [f"R:OK {n}"]
        self.teams.replies = [f"OK {n}"]
        notify.collect(self.out, self.teams)
        left = all_text(self.out, self.dir.name)
        for mark, at in self.marks.items():
            if at > 250:                       # the excerpt keeps the first 200 characters, nothing else
                self.assertNotIn(mark, left, mark)
        self.assertIsNone(fulltext.load(self.out, next(iter(notify.Approvals(self.out).data["items"].values()))["key"]))


class TestKeepPeriod(Base):
    def wait_for_the_pm(self):
        self.enable()
        self.teams.chat_messages = {"19:c1@thread.v2": [LONG]}
        (self.inbox / "a.json").write_text(json.dumps(self.ev(1, LONG[:60] + "…"), ensure_ascii=False), encoding="utf-8")
        daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=self.teams, send=True, toaster=lambda a, b: None)
        return next(iter(notify.Approvals(self.out).data["items"].values()))["key"]

    def test_the_full_text_of_a_waiting_item_is_deleted_after_seven_days(self):
        key = self.wait_for_the_pm()
        self.assertIsNotNone(fulltext.load(self.out, key))
        soon = datetime.now(timezone.utc) + timedelta(days=6)
        self.assertEqual(fulltext.purge(self.out, now=soon), [])
        self.assertIsNotNone(fulltext.load(self.out, key, now=soon))
        later = datetime.now(timezone.utc) + timedelta(days=8)
        self.assertIsNone(fulltext.load(self.out, key, now=later))            # treated as gone at once
        self.assertEqual(fulltext.purge(self.out, now=later), [key])
        self.assertFalse((self.out / "full_text.json").exists() or (self.out / "full_text.json.bak").exists())   # no copy is left
        rec = next(iter(notify.Approvals(self.out).data["items"].values()))["record"]
        self.assertEqual(rec["read_full"]["state"], "preview_only")           # decided from the preview from now on
        self.assertIn("保存期限", rec["read_full"]["why"])
        self.teams.posts.clear()
        rec2 = dict(rec)
        self.assertIn("プレビューだけで判断しました", notify.format_post(1, rec2, fulltext.load(self.out, key)))

    def test_the_period_is_a_setting_and_the_daily_cycle_does_the_deleting(self):
        key = self.wait_for_the_pm()
        data = json.loads((self.out / "full_text.json").read_text(encoding="utf-8"))
        data[key]["at"] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
        (self.out / "full_text.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNotNone(fulltext.load(self.out, key))                     # 3 days: within 7
        self.enable(KIMERU_FULL_TEXT_KEEP_DAYS="2")
        r = daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=self.teams, send=False)
        self.assertEqual(r.get("full_text_purged"), 1)
        self.assertIsNone(fulltext.load(self.out, key))

    def test_a_full_text_entry_without_a_date_does_not_stay_forever(self):
        fulltext.save(self.out, "k", {"text": "t"})
        data = json.loads((self.out / "full_text.json").read_text(encoding="utf-8"))
        del data["k"]["at"]
        (self.out / "full_text.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(fulltext.load(self.out, "k"))
        self.assertEqual(fulltext.purge(self.out), ["k"])


class TestRedraftKeepsTheWholeTextApart(Base):
    def test_a_redraft_request_holds_the_excerpt_in_the_record_and_the_whole_text_in_full_text_json(self):
        self.enable()
        long = long_text(2500)
        self.teams.chat_messages = {"19:c1@thread.v2": [long]}
        w = writer.M365PromptWriter()
        cli.process(self.ev(1, long[:70] + "…"), GRAPHS, StubBackend(), self.out, PBS, writer=w, dedup=True, reader=fulltext.Reader(self.teams))
        notify.notify(self.out, self.teams, send=True)
        ap = notify.Approvals(self.out)
        it = next(iter(ap.data["items"].values()))
        late = "【区間05】"
        self.assertIn(late, long)
        notify._redraft(it, "もっと短く", w, self.out)
        ap.save()
        self.assertNotIn(late, (self.out / "approvals.json").read_text(encoding="utf-8"))
        self.assertIn(late, (self.out / "full_text.json").read_text(encoding="utf-8"))


class TestBriefMentionsWhatWasOpened(Base):
    def test_a_chat_opened_but_not_read_and_a_chat_not_put_back_are_in_the_brief(self):
        self.enable()

        class Lost(FakeTeams):
            def readchat(self, chat_id, count=5, preview=None):
                raise notify.BridgeError("the script did not finish in time", {"restore": "failed"})

        (self.inbox / "a.json").write_text(json.dumps(self.ev(1, "ありがとうございました。またよろ…"), ensure_ascii=False), encoding="utf-8")
        daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=Lost(), send=False)
        text, _ = brief.build(self.out, StubBackend())
        self.assertIn("開いて読みましたが、対応は不要でした", text)                    # it was opened, so it is read in Teams now
        self.assertIn("元へ戻せなかった件が 1 件", text)


class TestBridgeErrors(unittest.TestCase):
    def test_a_failure_of_the_script_carries_what_it_says_about_putting_the_chat_back(self):
        b = notify.PowerShellBridge()
        fake = mock.Mock(returncode=2, stdout=json.dumps({"ok": False, "error": "x", "restore": "self", "returned": False}), stderr="")
        with mock.patch("subprocess.run", return_value=fake):
            with self.assertRaises(notify.BridgeError) as cm:
                b.readchat("19:abc@thread.v2", 3, preview="来週のリリースについて")
        self.assertEqual(cm.exception.data["restore"], "self")

    def test_a_script_that_timed_out_could_not_put_the_chat_back(self):
        import subprocess as sp
        b = notify.PowerShellBridge()
        with mock.patch("subprocess.run", side_effect=sp.TimeoutExpired("x", 300)):
            with self.assertRaises(notify.BridgeError) as cm:
                b.readchat("19:abc@thread.v2", 3)
        self.assertEqual(cm.exception.data["restore"], "failed")

    def test_the_start_of_the_preview_is_passed_to_the_script(self):
        b = notify.PowerShellBridge()
        with mock.patch.object(b, "_run", return_value={"ok": True, "messages": []}) as run:
            b.readchat("19:abc@thread.v2", 4, preview="来週のリリース")
        run.assert_called_once_with("-Action", "readchat", "-ChatId", "19:abc@thread.v2", "-Count", "4", "-Preview", "来週のリリース")

    def test_the_reader_passes_only_the_start_of_the_preview(self):
        seen = {}

        class Spy(FakeTeams):
            def readchat(self, chat_id, count=5, preview=None):
                seen["preview"] = preview
                return super().readchat(chat_id, count)

        spy = Spy()
        spy.chat_messages = {"19:c1@thread.v2": ["田中: 来週のリリースについて確認したいことがあります。よろしくお願いします。"]}
        r = fulltext.Reader(spy)({"chat_id": "19:c1@thread.v2", "text": "田中: 来週のリリースについて確認したいことがあります。よろしくお願いします。…"})
        self.assertTrue(r["ok"])
        self.assertLessEqual(len(seen["preview"]), 40)
        self.assertNotIn("…", seen["preview"])


class TestNotWhenThePersonIsWorking(Base):
    """Teams in front / the focus in an input box: nothing is opened, the event waits (the script checks it read only, before anything)."""

    def in_use_teams(self):
        class InUse(FakeTeams):
            def readchat(self, chat_id, count=5, preview=None):
                self.opened = getattr(self, "opened", []) + [chat_id]
                raise notify.BridgeError("Teams is in use (it is in front, or an input box has the focus); nothing was opened",
                                         {"restore": "none", "returned": True, "hadOriginal": False})
        return InUse()

    def run_cycle(self, teams, toasts=None, text=Q):
        (self.inbox / "a.json").write_text(json.dumps(self.ev(1, text), ensure_ascii=False), encoding="utf-8")
        return daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=teams, send=True,
                           toaster=(lambda a, b: toasts.append((a, b))) if toasts is not None else (lambda a, b: None))

    def test_teams_in_front_or_the_focus_in_an_input_box_postpones_and_decides_nothing(self):
        self.enable()
        toasts = []
        r = self.run_cycle(self.in_use_teams(), toasts)
        self.assertEqual(self.decisions(), [])
        self.assertEqual((r["left_for_next_cycle"], r["waiting"]), (1, 0))
        self.assertEqual([t for t in toasts if "チャットの表示" in t[0]], [])      # nothing was changed: no "may be left open" notice

    def test_the_reader_raises_deferred_for_it(self):
        self.enable()
        with self.assertRaises(fulltext.Deferred):
            fulltext.Reader(self.in_use_teams())(self.ev(1, Q + "…"))

    def test_the_script_checks_it_before_it_touches_anything(self):
        blk = TestScriptGuards.block_text()
        self.assertLess(blk.index("Test-TeamsInUse"), blk.index("Show-ChatApp"))
        self.assertLess(blk.index("Test-TeamsInUse"), blk.index("Select-Chat"))
        self.assertLess(blk.index("Assert-Idle"), blk.index("Show-ChatApp"))
        self.assertIn(fulltext.IN_USE, blk)


class TestSameTitleAndShortMatch(Base):
    def test_a_chat_with_the_same_title_is_not_read_the_screen_is_not_moved_and_the_preview_decides(self):
        self.enable()

        class SameTitle(FakeTeams):
            def readchat(self, chat_id, count=5, preview=None):
                self.opened = getattr(self, "opened", []) + [chat_id]
                # the script did not touch the screen: nothing was operated, so nothing was put back
                raise notify.BridgeError("the chat that is open has the title of the chat to read, so they cannot be told apart on this screen; nothing was opened, nothing was read",
                                         {"restore": "none", "returned": True, "hadOriginal": True})

        teams, toasts = SameTitle(), []
        (self.inbox / "a.json").write_text(json.dumps(self.ev(1, Q), ensure_ascii=False), encoding="utf-8")
        r = daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=teams, send=True, toaster=lambda a, b: toasts.append((a, b)))
        rf = self.decisions()[0]["read_full"]
        self.assertEqual((rf["state"], rf["restore"], rf["returned"]), ("preview_only", "none", True))
        self.assertIn("nothing was opened", rf["why"])                                 # the script's own fixed sentence, no chat text
        self.assertNotIn("note", rf)                                                  # nothing was moved: no notice
        self.assertEqual([t for t in toasts if "チャットの表示" in t[0]], [])
        self.assertEqual(r.get("left_for_next_cycle", 0), 0)                           # not put off: the preview decided at once
        self.assertFalse((self.out / "read_pending.json").exists() and json.loads((self.out / "read_pending.json").read_text(encoding="utf-8")))
        self.assertEqual(len(teams.opened), 1)                                         # asked once

    def test_the_script_leaves_the_screen_alone_when_the_open_chat_is_the_one_to_read(self):
        blk = TestScriptGuards.block_text()
        cond = "if (-not $origId -and (Test-ChatOpen $w $ChatId $title))"       # the same predicate as the check that a chat opened
        self.assertIn(cond, blk)
        self.assertLess(blk.index(cond), blk.index("Select-Chat"))
        self.assertNotIn("Restore-Original $null ''", blk)                           # no move to the self chat for it
        self.assertNotIn("$sameTitle", blk)
        self.assertNotIn("$origTitle -eq $title", blk)                               # not the exact-match test that a longer title slips through
        self.assertLess(blk.index("$script:Acted = $false"), blk.index(cond))

    def test_the_same_title_test_is_the_open_test_and_a_window_title_with_one_more_separator_is_caught(self):
        src = _ps_source(("Test-ChatOpen",))
        out = _run_ps("""
function Get-ChatItems($w) { @() }
function Test-Selected($i) { $false }
function Get-ChatId($i) { $null }
function W($n) { [pscustomobject]@{ Current = [pscustomobject]@{ Name = $n } } }
"A=" + (Test-ChatOpen (W 'チャット | 週次会議 | Microsoft Teams') 'x' '週次会議')
"B=" + (Test-ChatOpen (W 'チャット | 週次会議 | 定例 | Microsoft Teams') 'x' '週次会議')
"C=" + (Test-ChatOpen (W 'チャット | 別の会議 | Microsoft Teams') 'x' '週次会議')
""", src)
        self.assertEqual(out, {"A": "title", "B": "title", "C": ""})

    def test_a_six_character_match_does_not_read_and_twelve_does(self):
        self.enable(KIMERU_READ_MIN_PREVIEW="6")                                      # the setting cannot go below 12
        self.teams.chat_messages = {"19:c1@thread.v2": ["来週の会議の件について確認です"]}
        self.assertFalse(fulltext.Reader(self.teams)(self.ev(1, "来週の会議の件…"))["ok"])      # 7 characters
        self.assertEqual(getattr(self.teams, "opened", []), [])
        self.assertTrue(fulltext.Reader(self.teams)(self.ev(1, "来週の会議の件について確認です…"))["ok"])   # 15 characters
        self.assertEqual(self.teams.opened, ["19:c1@thread.v2"])

    def test_after_the_name_is_taken_off_the_match_is_still_12_characters(self):
        self.assertFalse(fulltext._fits("田中: 来週の会議の件…", "来週の会議の件について確認です"))          # 7 characters remain
        self.assertTrue(fulltext._fits("田中: 来週の会議の件について確認…", "来週の会議の件について確認です"))
        self.assertIn("(.{12,})", TestScriptGuards.funcs_text()["Test-PreviewMatch"])

    def test_a_name_and_a_colon_of_either_width_are_taken_off_before_the_gate(self):
        for pv, body in (("田中太郎：はい了解しました", "はい了解しました"), ("田中太郎: はい了解しました", "はい了解しました"),
                         ("田中太郎:はい了解しました…", "はい了解しました"), ("田中太郎：　はい了解しました", "はい了解しました"),
                         ("来週の会議の件について確認です", "来週の会議の件について確認です")):
            self.assertEqual(fulltext._body(pv), body, pv)

    def test_a_full_width_name_prefix_leaves_fewer_than_12_characters_so_nothing_is_opened(self):
        self.enable()
        self.teams.chat_messages = {"19:c1@thread.v2": ["田中太郎：はい了解しました、確認して連絡します"]}
        r = fulltext.Reader(self.teams)(self.ev(1, "田中太郎：はい了解しました…"))              # 13 characters with the name, 8 without it
        self.assertFalse(r["ok"])
        self.assertEqual(getattr(self.teams, "opened", []), [])
        r = fulltext.Reader(self.teams)(self.ev(1, "田中太郎：はい了解しました、確認して連絡します…"))   # 20 characters without the name
        self.assertTrue(r["ok"])
        self.assertEqual(self.teams.opened, ["19:c1@thread.v2"])


class TestEveryWayOutGoesThroughTheSelfChat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean.update({"KIMERU_STATE_DIR": self.dir, "KIMERU_READ_FULL": "1"})
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_a_time_out_or_an_interruption_while_putting_the_original_back_still_tries_the_self_chat(self):
        body = TestScriptGuards.funcs_text()["Restore-Original"]
        tried = body[body.index("Select-Chat"):body.index("Select-Notes")]
        self.assertIn("} catch {}", tried)                                              # Select-Chat may throw (Test-Deadline, Assert-Idle): caught here
        self.assertNotIn("return 'failed'", tried)                                      # ... and does not end the function

    def test_a_fixed_link_that_brings_teams_to_the_front_is_put_back_too(self):
        sn = TestScriptGuards.funcs_text()["Select-Notes"]
        self.assertLess(sn.index("Save-Foreground"), sn.index("Start-Process"))
        self.assertIn("SetForegroundWindow($script:PrevFg)", TestScriptGuards.funcs_text()["Restore-Foreground"])
        blk = TestScriptGuards.block_text()
        self.assertLess(blk.index("Restore-Original", blk.index("} catch {")), blk.index("Restore-Foreground"))

    def test_nothing_changed_means_no_may_be_left_open(self):
        f = TestScriptGuards.funcs_text()["Restore-Original"]
        self.assertLess(f.index("Test-OrigOpen"), f.index("Wait-IdleSoft"))            # read only, before waiting for a quiet keyboard
        self.assertIn("$script:Acted", TestScriptGuards.block_text())                   # nothing operated: 'none'
        self.assertIn("$script:Acted = $true", TestScriptGuards.funcs_text()["Select-Chat"])

    def stuck_teams(self):
        class Stuck(FakeTeams):
            def readchat(self, chat_id, count=5, preview=None):
                r = super().readchat(chat_id, count)
                return {**r, "returned": False, "restore": "failed", "hadOriginal": True}
        t = Stuck()
        t.chat_messages = {"19:c1@thread.v2": [LONG]}
        return t

    def cycle(self, teams, toaster, env=None, send=True):
        with mock.patch.dict(os.environ, env or {}):
            config.apply([])
            inbox = Path(self.dir) / "inbox"
            inbox.mkdir(exist_ok=True)
            (inbox / "a.json").write_text(json.dumps({"kind": "teams.chat", "id": "1", "chat_id": "19:c1@thread.v2", "author": "相手1",
                                                      "text": LONG[:60], "mentions_me": True}, ensure_ascii=False), encoding="utf-8")
            return daily.cycle(Path(self.dir) / "out", inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=teams, send=send, toaster=toaster)

    def test_the_notice_comes_with_the_toast_switched_off_as_a_post_to_the_self_chat(self):
        toasts, teams = [], self.stuck_teams()
        self.cycle(teams, lambda a, b: toasts.append((a, b)), {"KIMERU_TOAST": "0"})
        self.assertEqual([t for t in toasts if "チャットの表示" in t[0]], [])          # the PC notification is off
        self.assertEqual(len([t for t, _ in teams.posts if t.startswith("[kimeru 通知]") and "開いたままの可能性" in t]), 1)

    def test_the_notice_falls_back_to_the_self_chat_when_the_pc_notification_fails(self):
        teams = self.stuck_teams()

        def boom(a, b):
            raise OSError("no notification")
        self.cycle(teams, boom, {"KIMERU_TOAST": "1"})
        self.assertEqual(len([t for t, _ in teams.posts if t.startswith("[kimeru 通知]") and "開いたままの可能性" in t]), 1)

    def test_a_cycle_that_does_not_post_only_logs_it(self):
        teams = self.stuck_teams()
        self.cycle(teams, lambda a, b: None, {"KIMERU_TOAST": "0"}, send=False)
        self.assertEqual([t for t, _ in teams.posts if t.startswith("[kimeru 通知]")], [])
        self.assertIn("read_restore", (Path(self.dir) / "out" / "daily.log.jsonl").read_text(encoding="utf-8"))


class TestPuttingOff(Base):
    def busy_teams(self):
        class Busy(FakeTeams):
            def readchat(self, chat_id, count=5, preview=None):
                self.opened = getattr(self, "opened", []) + [chat_id]
                raise notify.BridgeError("Teams is in use (it is in front, or an input box has the focus); nothing was opened",
                                         {"restore": "none", "returned": True, "hadOriginal": False})
        return Busy()

    def attempt(self, teams, ev=None):
        try:
            return cli.process(ev or self.ev(1, Q), GRAPHS, StubBackend(), self.out, PBS, dedup=True, reader=fulltext.Reader(teams))
        except fulltext.BudgetExhausted:
            return None

    def test_after_the_limit_the_preview_decides_and_nothing_is_opened_again(self):
        self.enable(KIMERU_READ_MAX_DEFER="3")
        teams = self.busy_teams()
        for _ in range(3):
            self.assertIsNone(self.attempt(teams))                      # put off three times
        self.assertEqual(len(teams.opened), 3)
        res = self.attempt(teams)
        self.assertEqual(len(teams.opened), 3)                          # the fourth try does not open
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["read_full"]["state"], "preview_only")
        self.assertIn("延期が上限", res[0]["read_full"]["why"])
        pending = self.out / "read_pending.json"
        self.assertFalse(pending.exists() and json.loads(pending.read_text(encoding="utf-8")))

    def test_the_limit_also_holds_for_a_cut_off_preview(self):
        self.enable(KIMERU_READ_MAX_DEFER="2")
        teams = self.busy_teams()
        ev = self.ev(1, BODY + "…")
        self.assertIsNone(self.attempt(teams, ev))
        self.assertIsNone(self.attempt(teams, ev))
        res = self.attempt(teams, ev)
        self.assertEqual(len(teams.opened), 2)
        self.assertEqual(res[0]["read_full"]["state"], "preview_only")

    def test_the_time_limit_counts_from_the_first_delay(self):
        pend = cli._PreviewJudgments(self.out)
        pend.put("k", {"a": 1})
        first = pend.data["k"]["t"] - 23 * 3600
        pend.data["k"]["t"] = first                                     # the first delay was 23 hours ago
        pend.put("k")
        self.assertEqual(pend.data["k"]["t"], first)                    # a later delay does not move it
        self.assertEqual(pend.deferrals("k"), 2)
        self.assertEqual(pend.get("k"), {"a": 1})
        pend.data["k"]["t"] = first - 2 * 3600
        self.assertEqual((pend.get("k"), pend.deferrals("k")), (None, 0))

    def test_the_defaults(self):
        self.assertEqual(fulltext.max_defer(), 12)
        self.assertEqual(config.value("read_idle_sec"), "30")
        self.assertEqual(config.value("read_click"), "0")
        self.assertEqual(config.value("read_min_preview"), "12")


class TestScriptStaysReadOnlyEvenBeforeTheClick(unittest.TestCase):
    def test_bringing_teams_to_the_front_and_clicking_are_not_on_the_default_path(self):
        seen, body = TestScriptGuards.reachable_text()
        parts = TestScriptGuards.funcs_text()
        # the only readers of these are: the click branch of Select-Chat (behind read_click) and the functions that put the window back
        rest = body
        for name in ("Select-Chat", "Assert-Foreground", "Restore-Foreground", "Save-Foreground"):
            rest = rest.replace(parts[name], "")
        for bad in ("SetCursorPos", "mouse_event", "SetForegroundWindow", "ShowWindow", "Assert-Foreground"):
            self.assertNotIn(bad, rest, f"{bad} is reachable from readchat outside the click branch")
        sc = parts["Select-Chat"]
        self.assertLess(sc.index("Test-ReadClickAllowed"), sc.index("'click'"))
        self.assertIn("$hows += 'click'", sc)
        self.assertIn("'select', 'invoke'", sc)
        self.assertIn("KIMERU_READ_CLICK", parts["Test-ReadClickAllowed"])

    def test_the_idle_threshold_of_a_read_is_read_idle_sec_and_idle_sec_has_no_say(self):
        self.assertIn("Get-IdleNeed", TestScriptGuards.funcs_text()["Test-UserIdle"])
        src = "$script:Restoring = $false\n" + _ps_source(("Get-ReadIdleNeed", "Get-IdleNeed"))

        def need(action, fallback=3, restoring=False, **env):
            code = f"$Action = '{action}'; $script:Restoring = ${str(restoring).lower()}; 'N=' + (Get-IdleNeed {fallback})"
            return int(_run_ps(code, src, env)["N"])
        self.assertEqual(need("readchat"), 30)                                      # nothing set: 30
        self.assertEqual(need("readchat", KIMERU_IDLE_SEC="0"), 30)                 # idle_sec=0 does not switch the check off for a read
        self.assertEqual(need("readchat", KIMERU_IDLE_SEC="4"), 30)
        self.assertEqual(need("readchat", KIMERU_IDLE_SEC="0", KIMERU_READ_IDLE_SEC="45"), 45)
        self.assertEqual(need("readchat", KIMERU_READ_IDLE_SEC="0"), 0)             # only read_idle_sec=0 does
        self.assertEqual(need("readchat", KIMERU_READ_IDLE_SEC="x"), 30)            # not a number: the default, not a crash
        self.assertEqual(need("post", fallback=4), 4)                               # the other operations keep idle_sec
        self.assertEqual(need("post", fallback=4, KIMERU_IDLE_SEC="0"), 0)

    def test_putting_the_chat_back_has_a_short_threshold_of_its_own(self):
        src = "$script:Restoring = $false\n" + _ps_source(("Get-ReadIdleNeed", "Get-IdleNeed"))

        def need(restoring, **env):
            code = f"$Action = 'readchat'; $script:Restoring = ${str(restoring).lower()}; 'N=' + (Get-IdleNeed 3)"
            return int(_run_ps(code, src, env)["N"])
        self.assertEqual((need(False), need(True)), (30, 3))                         # before reading 30 s; putting back 3 s
        self.assertEqual(need(True, KIMERU_IDLE_SEC="0"), 3)
        self.assertEqual(need(True, KIMERU_READ_IDLE_SEC="60"), 3)                   # independent of the read threshold
        self.assertEqual(need(True, KIMERU_READ_IDLE_SEC="0"), 0)                    # ... but never above it
        body = TestScriptGuards.funcs_text()["Restore-Original"]
        self.assertIn("$script:Restoring = $true", body)
        self.assertIn("Wait-IdleSoft 3 20", body)                                    # 3 s of quiet, 20 s of waiting
        self.assertLess(body.index("$script:Restoring = $true"), body.index("Wait-IdleSoft"))
        self.assertIn("KIMERU_READ_IDLE_SEC=0", TestScriptGuards.funcs_text()["Assert-Idle"])   # the message names the setting that applies
        self.assertIn(fulltext.BUSY, TestScriptGuards.funcs_text()["Assert-Idle"])

    def test_t24_uses_the_scripts_own_verdict(self):
        t = (ROOT / "tools" / "check.ps1").read_text(encoding="utf-8-sig")
        i = t.index("# ---- T24")
        blk = t[i:t.index("# ---- T22", i)]
        self.assertIn("previewMatched", blk)
        self.assertNotIn(".Contains($head)", blk)


class TestUseOfTeamsAndPageSwitch(unittest.TestCase):
    """Test-TeamsInUse and Show-ChatApp, run in a PowerShell with stubs (no Teams, no window)."""

    STUBS = """
function Get-TeamsPids { @(100, 101) }
function Get-ForegroundPid { [int]$script:Fg }
function Get-FocusInfo { if ($script:Boom) { throw 'x' }; $script:Focus }
function Info($type, $top) { [pscustomobject]@{ Type = $type; ElemPid = 555; Hwnd = 7; Top = 9; TopPid = $top } }
function Case($name, $fg, $focus) { $script:Fg = $fg; $script:Focus = $focus; $name + '=' + (Test-TeamsInUse $null) }
"""

    def in_use(self, cases):
        return _run_ps("\n".join(cases), self.STUBS + "\n" + _ps_source(("Test-TeamsInUse",)))

    def test_in_use_when_the_focus_cannot_be_read_or_a_teams_window_is_in_front_or_an_input_box_of_one_has_the_focus(self):
        r = self.in_use([
            "Case 'nofocus' 999 $null",                                    # FocusedElement is null: in use
            "Case 'popout' 100 (Info 'ControlType.Button' 999)",           # a window of Teams is in front (the pop-out chat too): in use
            "Case 'popout2' 101 $null",
            "Case 'edit' 999 (Info 'ControlType.Edit' 101)",               # the input box of a Teams window (decided by its window, not by the element's ProcessId)
            "Case 'doc' 999 (Info 'ControlType.Document' 100)",
            "Case 'notype' 999 (Info 'ControlType.Button' 100)",           # a Teams window but not an input box, and Teams not in front
            "Case 'other' 999 (Info 'ControlType.Edit' 4242)",             # an input box of some other program
            "Case 'nowindow' 999 (Info 'ControlType.Edit' 0)",             # the window that holds the focus is unknown: cannot tell
            "$script:Boom = $true; Case 'boom' 999 (Info 'ControlType.Button' 100)",   # a failure: in use
        ])
        self.assertEqual(r, {"nofocus": "True", "popout": "True", "popout2": "True", "edit": "True", "doc": "True",
                             "notype": "False", "other": "False", "nowindow": "True", "boom": "True"})

    def test_the_element_process_id_is_not_what_decides_and_diag_prints_the_numbers(self):
        src = TestScriptGuards.funcs_text()
        self.assertNotIn("$f.Current.ProcessId", src["Test-TeamsInUse"])
        self.assertIn("TopPid", src["Test-TeamsInUse"])
        self.assertIn("GetAncestor", src["Get-FocusInfo"])
        self.assertIn("ProcessId", src["Get-FocusInfo"])
        text = (ROOT / "tools" / "teams-self.ps1").read_text(encoding="utf-8-sig")
        diag = text[text.index("if ($Action -eq 'diag') {"):text.index("function Get-PreviewHead")]
        self.assertIn("Get-FocusInfo", diag)                                         # printed for T24: the values to decide on a real PC
        self.assertIn("teamsInUse", diag)
        t24 = (ROOT / "tools" / "check.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("T24-diag", t24)

    SHOW = """
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$CT = @{ Button = 'B'; TabItem = 'T'; ListItem = 'L' }
function Find-All($root, $type) { if ($type -eq 'B') { @($script:Btn) } else { @() } }
function MakeBtn($invokeFails, $selectFails) {
  $b = [pscustomobject]@{ Current = [pscustomobject]@{ Name = 'チャット' }; InvokeFails = $invokeFails; SelectFails = $selectFails }
  $b | Add-Member -MemberType ScriptMethod -Name GetCurrentPattern -Value {
    param($p)
    $o = [pscustomobject]@{}
    if ($p -eq [System.Windows.Automation.InvokePattern]::Pattern) { if ($this.InvokeFails) { throw 'no invoke' }; $o | Add-Member ScriptMethod Invoke { } }
    else { if ($this.SelectFails) { throw 'no select' }; $o | Add-Member ScriptMethod Select { } }
    $o
  }
  $b
}
function Run($name, $btn) { $script:Acted = $false; $script:PageSwitched = $false; $script:Btn = $btn; $r = Show-ChatApp $null; "$name=$r/$script:Acted/$script:PageSwitched" }
"""

    def test_switching_the_page_counts_as_an_operation_and_a_failed_switch_does_not(self):
        r = _run_ps("""
Run 'invoke' (MakeBtn $false $false)
Run 'select' (MakeBtn $true $false)
Run 'none' (MakeBtn $true $true)
Run 'nobutton' $null
""", self.SHOW + "\n" + _ps_source(("Show-ChatApp",)))
        self.assertEqual(r, {"invoke": "True/True/True", "select": "True/True/True", "none": "False/False/False", "nobutton": "False/False/False"})

    def test_a_failure_after_the_page_switch_goes_through_the_put_back_and_says_the_original_was_not_restored(self):
        blk = TestScriptGuards.block_text()
        self.assertLess(blk.index("Show-ChatApp"), blk.index("Fail 'the chat is not in the list"))   # the switch comes before later failures
        put_back = blk[blk.index("$restore = 'none'"):]
        self.assertIn("if ($script:Acted)", put_back)                                                     # Acted (set by Show-ChatApp) -> put back
        self.assertIn("Restore-Original $origId $origTitle", put_back)
        self.assertIn("$script:PageSwitched -and $restore -in 'restored', 'unchanged'", put_back)        # the page itself cannot be gone back to
        self.assertIn("$restore = 'failed'", put_back)
        self.assertLess(put_back.index("$had ="), put_back.index("$script:PageSwitched", put_back.index("$had =")))   # ... and there was an original
        sc = TestScriptGuards.funcs_text()["Select-Chat"]
        self.assertLess(sc.index("switch ($how)"), sc.index("$script:Acted = $true"))                   # only after an operation went through
        self.assertLess(sc.index("$script:Acted = $true"), sc.index("} catch { continue }"))


class TestNeverPutOff(Base):
    """read_max_defer=0: no putting off. The chat is tried once; when it cannot be read now, the preview decides."""

    def failing(self, message):
        class Failing(FakeTeams):
            def readchat(self, chat_id, count=5, preview=None):
                self.opened = getattr(self, "opened", []) + [chat_id]
                raise notify.BridgeError(message, {"restore": "none", "returned": True, "hadOriginal": False})
        return Failing()

    def attempt(self, teams, ev):
        return cli.process(ev, GRAPHS, StubBackend(), self.out, PBS, dedup=True, reader=fulltext.Reader(teams))

    def test_the_first_try_reads_and_a_readable_chat_is_used(self):
        self.enable(KIMERU_READ_MAX_DEFER="0")
        self.teams.chat_messages = {"19:c1@thread.v2": ["前の話", BODY + Q]}
        res = self.attempt(self.teams, self.ev(1, Q + "…"))
        self.assertEqual(self.teams.opened, ["19:c1@thread.v2"])                  # 0 does not mean "do not read"
        self.assertEqual(res[0]["read_full"]["state"], "full")

    def test_when_it_cannot_be_read_now_the_preview_decides_on_the_spot(self):
        self.enable(KIMERU_READ_MAX_DEFER="0")
        n = 0
        for text in (Q + "…", Q):                                                  # a cut-off preview, and one that ends at the PM
            for message in ("the keyboard / mouse has been in use; stopped",
                            "Teams is in use (it is in front, or an input box has the focus); nothing was opened"):
                teams, n = self.failing(message), n + 1
                res = self.attempt(teams, self.ev(n, text))                        # (no BudgetExhausted: nothing is put off)
                self.assertEqual(len(teams.opened), 1)                             # it tried once
                self.assertEqual(res[0]["read_full"]["state"], "preview_only")
                self.assertIn("read_max_defer=0", res[0]["read_full"]["why"])
        pending = self.out / "read_pending.json"
        self.assertFalse(pending.exists() and json.loads(pending.read_text(encoding="utf-8")))

    def test_the_count_limit_does_not_put_it_off_either(self):
        self.enable(KIMERU_READ_MAX_DEFER="0", KIMERU_READ_MAX_OPEN="1")
        reader = fulltext.Reader(self.teams)
        self.teams.chat_messages = {"19:c1@thread.v2": [BODY + Q], "19:c2@thread.v2": [BODY + Q]}
        r1 = cli.process(self.ev(1, Q + "…"), GRAPHS, StubBackend(), self.out, PBS, dedup=True, reader=reader)
        r2 = cli.process(self.ev(2, Q + "…"), GRAPHS, StubBackend(), self.out, PBS, dedup=True, reader=reader)   # over the limit of 1
        self.assertEqual(r1[0]["read_full"]["state"], "full")
        self.assertEqual(r2[0]["read_full"]["state"], "preview_only")
        self.assertEqual(self.teams.opened, ["19:c1@thread.v2"])

    def test_the_default_still_puts_it_off(self):
        self.enable()
        teams = self.failing("Teams is in use (it is in front, or an input box has the focus); nothing was opened")
        with self.assertRaises(fulltext.BudgetExhausted):
            self.attempt(teams, self.ev(1, Q + "…"))


if __name__ == "__main__":
    unittest.main()
