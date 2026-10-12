"""R22: the earlier messages of the same chat reach the judge only when judge_context=1 and the judge is Kev or CLM on this PC
(a loopback address). With the default 0 the judge is asked exactly what it was asked before, and the only thing that is
written about the earlier messages is their count (context_used). Every judge here is a fake or a Kev that answers from the
stub's keywords: no network, no real Teams, no setting of the machine."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru import cli, config, events, graph, plan
from kimeru import writer as writer_mod
from kimeru.backends import ClmBackend, JevBackend, KevBackend, ReplayBackend, StubBackend, is_local_judge
from kimeru.cli import Meter
from tests.test_daily import GRAPHS, PBS

ROOT = Path(__file__).resolve().parent.parent
TEAMS = GRAPHS["teams.chat"][0]
NOW = datetime(2026, 10, 12, 3, 0, tzinfo=timezone.utc)   # the same date in UTC and in Japan
TODAY = "2026-10-12"
CHAT = "19:synthetic-chat-1@unq.gbl.spaces"
EVENT_STATE = {"kind": "teams.chat", "source": "teams", "id": "cur-1", "ts": "2026-10-12T03:00:00+00:00", "chat_id": CHAT,
               "author": "Sato", "mentions_me": True, "text": "よろしくお願いします"}


def ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat(timespec="seconds")


def teams_event(eid="cur-1", text="よろしくお願いします", author="Sato", chat=CHAT):
    return {"kind": "teams.chat", "source": "teams", "id": eid, "ts": ago(0), "chat_id": chat,
            "author": author, "mentions_me": True, "text": text}


def seen_row(eid, hours, body, author="Sato", chat=CHAT):
    """One line of decisions.jsonl as process writes it (the event kept as a record, the body as given)."""
    return {"at": ago(hours), "node": "intent", "outcome": "advise",
            "event": {"kind": "teams.chat", "id": eid, "chat_id": chat, "author": author, "text": body}}


def fresh(root, name, rows=()):
    out = Path(root) / name
    out.mkdir(parents=True, exist_ok=True)
    if rows:
        with (out / "decisions.jsonl").open("a", encoding="utf-8") as f:
            f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return out


def settings(**kv):
    """KIMERU_* values for one block (the isolation drops the machine's own)."""
    return mock.patch.dict(os.environ, {f"KIMERU_{k.upper()}": v for k, v in kv.items()})


def spy_of(base):
    """A judge that records every (state, questions) it is asked and answers with the stub's keywords."""
    class Spy(base):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.calls = []

        def ask(self, state, questions):
            self.calls.append((copy.deepcopy(state), copy.deepcopy(questions)))
            return StubBackend().ask(state, questions)
    return Spy


SpyKev = spy_of(KevBackend)      # the real Kev class: local when its address is this PC
SpyStub = spy_of(StubBackend)


class SpyJev(SpyStub):
    NAME = JevBackend.NAME


class KevByNameOnly(SpyStub):
    NAME = "Kev"


def run(backend, out, ev, dedup=False):
    return cli.process(ev, GRAPHS, backend, out, PBS, now=NOW, dedup=dedup)


def files_with(out, text):
    """How many times `text` occurs in the files of the state folder (every file, every line)."""
    n = 0
    for p in out.rglob("*"):
        if p.is_file():
            n += p.read_bytes().decode("utf-8", "replace").count(text)
    return n


class TestTheDefaultIsTheEventAlone(unittest.TestCase):
    def test_judge_context_is_off_by_default_and_takes_only_0_or_1(self):
        self.assertEqual(config.value("judge_context"), "0")
        self.assertEqual(config.normalize("judge_context", "on"), ("1", True))
        self.assertFalse(config.normalize("judge_context", "maybe")[1])

    def test_with_the_default_a_local_judge_is_asked_the_event_and_its_own_questions_as_before(self):
        with tempfile.TemporaryDirectory() as d:
            out = fresh(d, "out", [seen_row("old-1", 1, "前の依頼")])
            spy = SpyKev()
            [res] = run(spy, out, teams_event())
            self.assertTrue(spy.calls)
            for state, questions in spy.calls:
                self.assertEqual(state, EVENT_STATE)
                for qid, q in questions.items():
                    self.assertEqual(q, TEAMS["nodes"][qid]["question"])
            self.assertNotIn("context_used", res)

    def test_a_setting_of_0_writes_no_context_field_into_the_record(self):
        with tempfile.TemporaryDirectory() as d, settings(judge_context="0"):
            out = fresh(d, "out", [seen_row("old-1", 1, "前の依頼")])
            [res] = run(SpyKev(), out, teams_event())
            row = json.loads((out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertNotIn("context_used", row)
            self.assertNotIn("context_used", res)


class TestWhoGetsTheEarlierMessages(unittest.TestCase):
    def test_only_kev_or_clm_at_a_loopback_address_is_a_local_judge(self):
        self.assertTrue(is_local_judge(KevBackend()))
        self.assertTrue(is_local_judge(ClmBackend()))
        self.assertTrue(is_local_judge(Meter(KevBackend())))
        self.assertFalse(is_local_judge(JevBackend(key_required=False)))
        self.assertFalse(is_local_judge(JevBackend(key_required=False, api="http://127.0.0.1:9/v1")))
        self.assertFalse(is_local_judge(StubBackend()))
        self.assertFalse(is_local_judge(ReplayBackend({})))
        self.assertFalse(is_local_judge(KevByNameOnly()))
        with settings(kev_url="http://10.20.30.40:8009/v1", kev_url_remote_ok="1"):
            self.assertFalse(is_local_judge(KevBackend()))

    def test_a_judge_on_this_pc_gets_the_earlier_messages_and_today_and_the_others_get_the_event_alone(self):
        rows = [seen_row("e1", 3, "一つ目"), seen_row("e2", 2, "二つ目")]
        with tempfile.TemporaryDirectory() as d, settings(judge_context="1"):
            kev, jev, stub = SpyKev(), SpyJev(), SpyStub()
            for name, judge in (("kev", kev), ("jev", jev), ("stub", stub)):
                run(judge, fresh(d, name, rows), teams_event())
            state = kev.calls[0][0]
            self.assertEqual(state["earlier"], ["Sato: 一つ目", "Sato: 二つ目"])
            self.assertEqual(state["today"], TODAY)
            self.assertTrue(any("earlier" in q["instructions"] for q in kev.calls[0][1].values()))
            for judge in (jev, stub):
                for state, questions in judge.calls:
                    self.assertEqual(state, EVENT_STATE)
                    for qid, q in questions.items():
                        self.assertEqual(q, TEAMS["nodes"][qid]["question"])

    def test_a_kev_at_a_remote_address_gets_the_event_alone_even_when_allowed_to_be_remote(self):
        with tempfile.TemporaryDirectory() as d, settings(judge_context="1", kev_url="http://10.20.30.40:8009/v1",
                                                          kev_url_remote_ok="1"):
            remote = SpyKev()
            run(remote, fresh(d, "out", [seen_row("e1", 1, "前の依頼")]), teams_event())
            for state, _ in remote.calls:
                self.assertEqual(state, EVENT_STATE)

    def test_a_judge_that_is_not_local_never_reads_the_decisions_file(self):
        with tempfile.TemporaryDirectory() as d, settings(judge_context="1"):
            out = fresh(d, "out", [seen_row("e1", 1, "前の依頼")])
            with mock.patch.object(cli, "_earlier_messages", side_effect=AssertionError("read")):
                [res] = run(SpyStub(), out, teams_event())
                run(KevByNameOnly(), out, teams_event())
            self.assertNotIn("context_used", res)


class TestTheStateStaysWithinTheCap(unittest.TestCase):
    def test_state_of_without_context_is_the_event_minus_the_hidden_keys(self):
        ev = {**teams_event(), "raw": {"x": 1}, "thread": ["hidden"], "_kimeru": {"observed": 1}}
        self.assertEqual(events.state_of(ev), EVENT_STATE)
        self.assertEqual(events.state_of(ev, None), EVENT_STATE)

    def test_the_text_is_cut_so_that_the_text_and_the_earlier_messages_stay_within_judge_text_max(self):
        earlier = ["Sato: " + "い" * 120, "Ito: " + "う" * 120]
        for cap in ("100", "400", "1200", "5000"):
            with settings(judge_text_max=cap):
                st = events.state_of(teams_event(text="あ" * 6000), {"earlier": earlier, "today": TODAY})
                self.assertLessEqual(len(st["text"]) + sum(len(e) for e in st["earlier"]), int(cap), cap)
                self.assertLessEqual(sum(len(e) for e in st["earlier"]), min(300, int(cap) // 4), cap)
                self.assertEqual(st["today"], TODAY)

    def test_an_entry_that_does_not_fit_the_budget_is_left_out_and_the_oldest_go_first(self):
        self.assertEqual(events.fit_earlier(["a" * 120, "b" * 120, "c" * 120], 300), ["b" * 120, "c" * 120])
        self.assertEqual(events.fit_earlier(["a" * 120], 100), [])
        self.assertEqual(events.fit_earlier(None, 300), [])


class TestTheEarlierMessagesOfTheChat(unittest.TestCase):
    def test_the_newest_two_of_the_same_chat_within_72_hours_come_oldest_first(self):
        rows = [seen_row("old", 80, "七十二時間より前"),
                seen_row("other", 2, "別のチャット", chat="19:synthetic-other@unq.gbl.spaces"),
                seen_row("b", 5, "五時間前"),
                seen_row("cur-1", 4, "同じ件（もう一度の処理）"),
                seen_row("c", 1, "一時間前"),
                seen_row("d", 0.5, "三十分前")]
        with tempfile.TemporaryDirectory() as d, settings(judge_context="1"):
            spy = SpyKev()
            run(spy, fresh(d, "out", rows), teams_event("cur-1"))
            self.assertEqual(spy.calls[0][0]["earlier"], ["Sato: 一時間前", "Sato: 三十分前"])

    def test_an_excerpt_is_at_most_120_characters_and_nothing_past_it_is_sent_even_when_the_record_is_full(self):
        body = "甲" * 130 + "SENTINELPAST120"
        with tempfile.TemporaryDirectory() as d, settings(judge_context="1", record_event_full="1"):
            out = fresh(d, "out", [seen_row("e1", 1, body)])
            (out / "full_text.json").write_text(json.dumps({"text": "FULLTEXTSENTINEL"}), encoding="utf-8")
            spy = SpyKev()
            run(spy, out, teams_event())
            [entry] = spy.calls[0][0]["earlier"]
            self.assertLessEqual(len(entry), 120)
            self.assertNotIn("SENTINEL", entry)
            self.assertNotIn("FULLTEXTSENTINEL", json.dumps(spy.calls, ensure_ascii=False))

    def test_a_damaged_or_unexpected_line_is_skipped_and_the_judgment_still_goes_on(self):
        with tempfile.TemporaryDirectory() as d, settings(judge_context="1"):
            out = fresh(d, "out", [seen_row("e1", 1, "前の依頼")])
            with (out / "decisions.jsonl").open("a", encoding="utf-8") as f:
                f.write("{broken json\n[1, 2]\n\n")
                f.write(json.dumps({"at": "not a time", "event": {"kind": "teams.chat", "id": "x", "chat_id": CHAT}}) + "\n")
                f.write(json.dumps({"at": ago(1)}) + "\n")
                f.write(json.dumps({"at": ago(1), "event": "text"}) + "\n")
                f.write(json.dumps(seen_row("e2", 0.5, "後の依頼")) + "\n")
            with (out / "decisions.jsonl").open("ab") as f:
                f.write(b"\xff\xfe not utf-8\n")
            spy = SpyKev()
            [res] = run(spy, out, teams_event())
            self.assertEqual(spy.calls[0][0]["earlier"], ["Sato: 前の依頼", "Sato: 後の依頼"])
            self.assertEqual(res["context_used"], {"earlier": 2})

    def test_only_the_end_of_decisions_is_read_and_its_first_partial_line_is_dropped(self):
        filler = [seen_row(f"f{i}", 30, "別の件の長い文" * 6, chat="19:synthetic-other@unq.gbl.spaces") for i in range(40)]
        with tempfile.TemporaryDirectory() as d, settings(judge_context="1"), \
                mock.patch.object(cli, "CONTEXT_TAIL", 1000):
            out = fresh(d, "out", filler + [seen_row("e1", 2, "一つ目"), seen_row("e2", 0.5, "二つ目")])
            spy = SpyKev()
            run(spy, out, teams_event())
            self.assertEqual(spy.calls[0][0]["earlier"], ["Sato: 一つ目", "Sato: 二つ目"])

    def test_with_no_decisions_file_the_earlier_messages_are_empty_and_today_is_still_given(self):
        with tempfile.TemporaryDirectory() as d, settings(judge_context="1"):
            spy = SpyKev()
            run(spy, fresh(d, "out"), teams_event())
            self.assertEqual(spy.calls[0][0]["earlier"], [])
            self.assertEqual(spy.calls[0][0]["today"], TODAY)

    def test_the_earlier_text_is_written_to_no_record_and_only_its_count_is_kept(self):
        with tempfile.TemporaryDirectory() as d, settings(judge_context="1"):
            out = fresh(d, "out", [seen_row("e1", 1, "SENTINELROW7 前の依頼")])
            [res] = run(SpyKev(), out, teams_event(), dedup=True)
            self.assertEqual(res["context_used"], {"earlier": 1})
            self.assertEqual(files_with(out, "SENTINELROW7"), 1)   # only the row that was already there
            self.assertNotIn("Sato: SENTINELROW7", json.dumps(res, ensure_ascii=False))
            self.assertNotIn("earlier", res)   # the count only, under context_used
            self.assertNotIn("today", res)


class TestTheInstructionsAndThePlaybooks(unittest.TestCase):
    def test_with_context_a_question_without_earlier_messages_is_returned_as_it_is(self):
        q = TEAMS["nodes"]["intent"]["question"]
        self.assertIs(graph.with_context(q, {"text": "x"}), q)

    def test_the_earlier_note_goes_to_every_question_and_the_today_note_only_to_a_deadline_question(self):
        state = {"earlier": ["Sato: x"], "today": TODAY}
        urgency_before = copy.deepcopy(TEAMS["nodes"]["urgency"]["question"])
        intent_before = copy.deepcopy(TEAMS["nodes"]["intent"]["question"])
        u = graph.with_context(TEAMS["nodes"]["urgency"]["question"], state)
        i = graph.with_context(TEAMS["nodes"]["intent"]["question"], state)
        self.assertTrue(u["instructions"].startswith(urgency_before["instructions"]))
        self.assertIn("earlier", u["instructions"])
        self.assertTrue(u["instructions"].endswith(f"Today is {TODAY}."))
        self.assertEqual(u["criteria"], urgency_before["criteria"])
        self.assertIn("earlier", i["instructions"])
        self.assertNotIn("Today is", i["instructions"])
        self.assertEqual(TEAMS["nodes"]["urgency"]["question"], urgency_before)   # the graph's own dict is not changed
        self.assertEqual(TEAMS["nodes"]["intent"]["question"], intent_before)

    def test_the_batch_mode_gives_the_notes_to_every_question_of_its_one_call(self):
        state = events.state_of(teams_event(), {"earlier": ["Sato: x"], "today": TODAY})
        spy = SpyStub()
        graph.run(TEAMS, teams_event(), spy, state=state, playbooks=PBS, batch=True)
        self.assertTrue(spy.calls)
        for _, questions in spy.calls:
            for q in questions.values():
                self.assertIn("earlier", q["instructions"])
        plain = SpyStub()
        graph.run(TEAMS, teams_event(), plain, playbooks=PBS, batch=True)
        for _, questions in plain.calls:
            for qid, q in questions.items():
                self.assertEqual(q, TEAMS["nodes"][qid]["question"])

    def test_the_playbook_questions_carry_the_earlier_note_too(self):
        spy = SpyStub()
        plan.build(TEAMS["nodes"]["plan_decision"], {"earlier": ["Sato: x"], "today": TODAY, "text": "y"}, spy, PBS)
        self.assertTrue(spy.calls)
        for _, questions in spy.calls:
            for q in questions.values():
                self.assertIn("earlier", q["instructions"])

    def test_a_run_with_context_changes_neither_the_graph_nor_the_playbooks_nor_its_version(self):
        graph_before, pbs_before, version = copy.deepcopy(TEAMS), copy.deepcopy(PBS), cli._graph_version(TEAMS)
        with tempfile.TemporaryDirectory() as d, settings(judge_context="1"):
            run(SpyKev(), fresh(d, "out", [seen_row("e1", 1, "前の依頼")]), teams_event(), dedup=True)
            self.assertEqual(TEAMS, graph_before)
            self.assertEqual(PBS, pbs_before)
            self.assertEqual(cli._graph_version(TEAMS), version)


class TestTheDecisionsOfAContextRun(unittest.TestCase):
    def test_the_processed_key_is_the_same_as_without_context(self):
        with tempfile.TemporaryDirectory() as d, settings(judge_context="1"):
            out = fresh(d, "out", [seen_row("e1", 1, "前の依頼")])
            run(SpyKev(), out, teams_event(), dedup=True)
            key = f"{TEAMS['name']}@{cli._graph_version(TEAMS)}:teams.chat:cur-1".replace(" ", "_")
            self.assertEqual((out / "processed.txt").read_text(encoding="utf-8").split(), [key])

    def test_the_match_rule_and_the_severe_guard_do_not_depend_on_the_context(self):
        text = "本番で決済が通らないと顧客から連絡がありました"
        rows = [seen_row("e1", 1, "前の依頼")]
        with tempfile.TemporaryDirectory() as d:
            with settings(judge_context="0"):
                off = run(SpyKev(), fresh(d, "off", rows), teams_event("cur-2", text))[0]
            with settings(judge_context="1"):
                on = run(SpyKev(), fresh(d, "on", rows), teams_event("cur-2", text))[0]
            self.assertEqual(off["path"][0], on["path"][0])
            self.assertEqual(off["path"][0]["node"], TEAMS["start"])
            self.assertEqual(off.get("severe_guard"), on.get("severe_guard"))

    def test_the_text_sent_to_a_local_judge_is_cut_by_the_budget_and_the_rest_of_the_cap_stays_free(self):
        with tempfile.TemporaryDirectory() as d, settings(judge_context="1"):
            out = fresh(d, "out", [seen_row("e1", 1, "甲" * 200), seen_row("e2", 0.5, "乙" * 200)])
            spy = SpyKev()
            run(spy, out, teams_event(text="あ" * 5000))
            st = spy.calls[0][0]
            self.assertEqual(len(st["text"]), 900)
            self.assertLessEqual(len(st["text"]) + sum(len(e) for e in st["earlier"]), 1200)


def raw_payloads():
    """One raw payload of each other source, and a chat message whose payload has no chat id (the normalizer leaves it out)."""
    return {
        "ado": {"eventType": "workitem.created", "resource": {"id": 501, "fields": {
            "System.Title": "テスト項目", "System.WorkItemType": "Bug", "System.CreatedBy": {"displayName": "Ito"},
            "System.Description": "<p>説明</p>", "System.CreatedDate": ago(0)}}},
        "alert": {"schemaId": "azureMonitorCommonAlertSchema", "data": {"essentials": {
            "alertId": "al-1", "alertRule": "rule-1", "severity": "Sev1", "monitorCondition": "Fired",
            "firedDateTime": ago(0), "description": "d"}}},
        "minutes": {"title": "定例 2026-10-12", "date": TODAY, "text": "- 次回までに確認する"},
        "chat_without_id": {"id": "cur-1", "createdDateTime": ago(0), "from": {"user": {"displayName": "Sato"}},
                            "body": {"content": "よろしくお願いします"}, "mentions": [{}]},
    }


class TestTheOtherSourcesAndAChatWithoutAChatIdAreJudgedAsBefore(unittest.TestCase):
    def judged(self, payload, value):
        """(what the judge was asked, the node and outcome of each result, the results) for one payload under judge_context=value.
        The folder holds the same earlier rows each time, so a run that wrongly read them would show in the questions."""
        rows = [seen_row("e1", 1, "前の依頼"), seen_row("e2", 0.5, "後の依頼")]
        with tempfile.TemporaryDirectory() as d, settings(judge_context=value):
            spy = SpyKev()
            results = run(spy, fresh(d, "out", rows), payload)
        return spy.calls, [(r.get("node"), r.get("outcome")) for r in results], results

    def test_an_ado_item_an_alert_a_meeting_item_and_a_chat_without_a_chat_id_get_the_same_questions_and_answers(self):
        cases = {**raw_payloads(), "chat_id_empty": {**teams_event(), "chat_id": ""},
                 "chat_id_missing": {k: v for k, v in teams_event().items() if k != "chat_id"}}
        for name, payload in cases.items():
            with self.subTest(name):
                off_calls, off_nodes, _ = self.judged(payload, "0")
                on_calls, on_nodes, on_results = self.judged(payload, "1")
                self.assertTrue(off_calls)
                self.assertEqual(on_calls, off_calls)
                self.assertEqual(on_nodes, off_nodes)
                for state, questions in on_calls:
                    self.assertNotIn("earlier", state)
                    self.assertNotIn("today", state)
                    self.assertNotIn("Today is", json.dumps(questions, ensure_ascii=False))
                for result in on_results:
                    self.assertNotIn("context_used", result)

    def test_the_same_earlier_rows_do_reach_a_chat_with_a_chat_id_so_the_check_above_is_not_empty(self):
        calls, _, results = self.judged(teams_event(), "1")
        self.assertEqual(calls[0][0]["earlier"], ["Sato: 前の依頼", "Sato: 後の依頼"])
        self.assertEqual(results[0]["context_used"], {"earlier": 2})

    def test_the_guard_gives_no_context_to_a_source_other_than_a_chat_or_to_a_chat_without_an_id(self):
        with tempfile.TemporaryDirectory() as d, settings(judge_context="1"):
            out = fresh(d, "out", [seen_row("e1", 1, "前の依頼")])
            for ev in ({"kind": "ado.workitem.created", "id": "501", "chat_id": CHAT},
                       {"kind": "monitor.alert", "id": "al-1", "chat_id": CHAT},
                       {"kind": "meeting.item", "id": "m-1", "chat_id": CHAT},
                       {**teams_event(), "chat_id": None}):
                with self.subTest(kind=ev["kind"], chat_id=ev.get("chat_id")):
                    self.assertIsNone(cli._judge_context(SpyKev(), out, ev, NOW))
            self.assertIsNotNone(cli._judge_context(SpyKev(), out, teams_event(), NOW))


class TestTheThreadAndTheWriterNeverGetTheEarlierText(unittest.TestCase):
    def test_a_thread_on_the_seeded_row_or_on_the_current_event_never_reaches_the_judge(self):
        old = seen_row("e1", 1, "前の依頼")
        old["event"]["thread"] = ["THREADSENTINEL-ROW"]
        cur = {**teams_event(), "thread": ["THREADSENTINEL-CUR"]}
        with tempfile.TemporaryDirectory() as d, settings(judge_context="1"):
            spy = SpyKev()
            run(spy, fresh(d, "out", [old]), cur)
        self.assertEqual(spy.calls[0][0]["earlier"], ["Sato: 前の依頼"])
        self.assertNotIn("THREADSENTINEL", json.dumps(spy.calls, ensure_ascii=False))

    def test_the_writer_is_handed_the_same_material_with_context_and_without_and_never_the_earlier_text(self):
        rows = [seen_row("e1", 1, "WRITERSENTINEL 前の依頼")]
        off, on, judge = RecordingWriter(), RecordingWriter(), SpyKev()
        with tempfile.TemporaryDirectory() as d:
            with settings(judge_context="0"):
                cli.process(teams_event(), GRAPHS, SpyKev(), fresh(d, "off", rows), PBS, writer=off, now=NOW)
            with settings(judge_context="1"):
                cli.process(teams_event(), GRAPHS, judge, fresh(d, "on", rows), PBS, writer=on, now=NOW)
        self.assertIn("Sato: WRITERSENTINEL 前の依頼", judge.calls[0][0]["earlier"])   # the judge did get the earlier message
        self.assertTrue(on.seen)
        self.assertEqual(on.seen, off.seen)
        for seen in on.seen:
            self.assertNotIn("WRITERSENTINEL", seen)
            self.assertNotIn("Today is", seen)
            self.assertNotIn('"earlier"', seen)
            self.assertNotIn('"today"', seen)


class RecordingWriter:
    """Records what the drafting model is handed (the result, the event, the instruction) and drafts the texts that are wanted."""
    NAME = "fake"

    def __init__(self):
        self.seen = []

    def draft(self, res, event, instruction=None):
        self.seen.append(json.dumps({"res": res, "event": event, "instruction": instruction}, ensure_ascii=False, default=str))
        return {key: "佐藤さん、承知しました。" if a["type"] == "teams.reply" else f"説明: {a.get('title')}"
                for key, a in writer_mod.targets(res)}


class TestTheSettingsAreDocumented(unittest.TestCase):
    def test_the_settings_table_and_the_security_notes_name_judge_context(self):
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        self.assertEqual(sum(1 for line in ref.splitlines() if line.startswith("| `KIMERU_JUDGE_CONTEXT`")), 1)
        self.assertIn("judge_context", (ROOT / "SECURITY.md").read_text(encoding="utf-8"))
        self.assertIn("judge_context", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
