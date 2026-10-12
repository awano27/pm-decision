"""R29 (C04c): the replies the PM owes a chat (the 借り帳). With sent_watch=1 the morning brief names the chats that wait for a reply, and
each closed debt is recorded in out/owed.jsonl (chat id, times, how, count: no text, no name). A debt closes when my own message shows in
its chat after it was opened (R14's own time), when the PM says NG or 済, or after owed_keep_days. With sent_watch=0 (the default) nothing
is read or written here: the brief is the golden text below, byte for byte. Every time is injected (NOW); no network, no real Teams."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import tempfile
import unittest
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru import brief, config, daily, fsutil, notify, owed
from kimeru.backends import StubBackend
from kimeru.cli import process
from tests.test_daily import GRAPHS, PBS, FakeTeams, one_on_one

CHAT_A, CHAT_B, CHAT_C = "19:sato_a@unq.gbl.spaces", "19:tanaka_b@unq.gbl.spaces", "19:yamada_c@unq.gbl.spaces"
CHAT_D, CHAT_E, CHAT_F = "19:ask_d@unq.gbl.spaces", "19:kato_e@unq.gbl.spaces", "19:ito_f@unq.gbl.spaces"
NOW = datetime(2026, 10, 11, 23, 30, tzinfo=timezone.utc)   # 2026-10-12 08:30 in Japan: the day of the brief
ON, OFF = {"KIMERU_SENT_WATCH": "1"}, {"KIMERU_SENT_WATCH": "0"}
GOLDEN_BRIEF = "[kimeru brief 2026-10-12] 今日の進め方\n1. 確認待ち #1: teams_chat\n確認待ち 1 件（OK 番号 / NG 番号 / 詳細 番号 で返信）\nTeams の自動判断（直近 24 時間）: 2 件（状況の確認（返信は送っていません） 1 件 / 返信不要 1 件）: Yamada、Sato\n\n承認済み・未完了の作業:\n- #2 Unknown"   # the default brief of the fixture below (sent_watch 0), taken from the code before R29
SENTINEL_BODY, SENTINEL_NAME = "SENTINEL-BODY-7731 reply text", "SENTINEL-NAME-7731"
ALLOWED_ROW_KEYS = {"chat_id", "k", "opened_at", "closed_at", "how", "n"}


def at(hours_before):
    """The ISO time (UTC, seconds) that many hours before NOW."""
    return (NOW - timedelta(hours=hours_before)).isoformat(timespec="seconds")


def ev(chat, name, eid, text="fixture text"):
    return {"id": eid, "kind": "teams.chat", "chat_id": chat, "chat_title": name, "author": name, "text": text}


def row(node, outcome, eid, chat, name, when, actions, needs_human=False, text="fixture text"):
    """A decisions.jsonl row as the judgment writes it (only the fields the ledger reads, plus a path)."""
    return {"graph": "teams_chat", "event_id": eid, "event_kind": "teams.chat", "node": node, "outcome": outcome,
            "needs_human": needs_human, "notify": False, "advice": "", "actions": actions, "at": when,
            "event": ev(chat, name, eid, text), "path": [{"node": "intent", "answer": {}, "edge": "unsure"}]}


def reply(chat, text):
    return {"type": "teams.reply", "chat_id": chat, "text": text}


def item(status, r):
    return {"key": f"teams_chat:{r['event_id']}:{r['node']}", "status": status, "posted": True, "record": r}


def fixture_rows():
    return [
        row("reply_status", "decide", "e-1", CHAT_A, "Sato", at(1.5), [reply(CHAT_A, "fixture reply A")]),
        row("decision_later", "decide", "e-2", CHAT_B, "Tanaka", at(3.5),
            [reply(CHAT_B, "fixture reply B"), {"type": "ado.create", "work_item_type": "Task", "title": "判断: fixture", "tags": "kimeru;decision"}]),
        row("log_fyi", "decide", "e-3", CHAT_C, "Yamada", at(2.5), [{"type": "log.only"}]),
        row("ask_pm", "advise", "e-4", CHAT_D, "Ask", at(4.5), [reply(CHAT_D, "fixture reply D")], needs_human=True),
        row("decision_today", "decide", "e-5", CHAT_E, "Kato", at(2), [reply(CHAT_E, "fixture reply E")], needs_human=True),
        row("decision_today", "decide", "e-6", CHAT_F, "Ito", at(5.5), [reply(CHAT_F, "fixture reply F")], needs_human=True),
    ]


def fixture_items(rows):
    """1 waits for the PM's OK (an ask_pm), 2 is approved and not sent yet, 3 was rejected (NG)."""
    return {"1": item("pending", rows[3]), "2": item("approved", rows[4]), "3": item("rejected", rows[5])}


def write_state(out, rows=(), items=None, own=None):
    """The state folder's records: decisions.jsonl, approvals.json, and pull_state.json with R14's own time per chat (time only)."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    items = items or {}
    (out / "approvals.json").write_text(json.dumps({"next": len(items) + 1, "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    if own:
        sigs = {cid: {"p": "preview-hash", "t": "10:00", "n": 1, "own": when} for cid, when in own.items()}
        (out / "pull_state.json").write_text(json.dumps({"teams": {"sigs": sigs}}, ensure_ascii=False), encoding="utf-8")


def owed_rows(out):
    p = Path(out) / "owed.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def snapshot(out):
    """Every file under the state folder with its bytes (what would be left on disk)."""
    return {str(p.relative_to(out)): p.read_bytes() for p in sorted(Path(out).rglob("*")) if p.is_file()}


def brief_text(out, env=None):
    with mock.patch.dict(os.environ, env or {}):
        text, _ = brief.build(out, StubBackend(), date="2026-10-12", now=NOW)
    return text


class TestTheTerminalsThatOweAReply(unittest.TestCase):
    def test_each_terminal_with_a_teams_reply_is_a_debt_until_it_closes(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture_rows()
            write_state(d, rows, fixture_items(rows))
            open_debts = owed.ledger(d, now=NOW)["open"]
            self.assertEqual(sorted(x["node"] for x in open_debts), ["ask_pm", "decision_later", "decision_today", "reply_status"])
            state = {x["node"]: x["state"] for x in open_debts}
            self.assertEqual(state, {"reply_status": "未返信", "decision_later": "未返信", "ask_pm": "確認待ち",
                                     "decision_today": "承認済み・未送信"})

    def test_log_fyi_and_ack_only_are_never_debts(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [row("log_fyi", "decide", "e-7", CHAT_C, "Yamada", at(1), [{"type": "log.only"}]),
                    row("ack_only", "decide", "e-8", CHAT_C, "Yamada", at(1), [reply(CHAT_C, "fixture thanks")])]
            write_state(d, rows)
            self.assertEqual(owed.ledger(d, now=NOW)["open"], [])

    def test_a_decision_with_only_a_work_item_is_no_debt(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [row("incident_bug", "decide", "e-9", CHAT_A, "Sato", at(1),
                        [{"type": "ado.create", "work_item_type": "Bug", "title": "[Teams報告] fixture", "tags": "kimeru;from-teams"}])]
            write_state(d, rows)
            self.assertEqual(owed.ledger(d, now=NOW)["open"], [])


class TestAnOwnPreviewClosesADebt(unittest.TestCase):
    def test_my_message_after_the_debt_closes_it_as_own_preview_and_the_row_has_no_text(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture_rows()
            write_state(d, rows, fixture_items(rows), own={CHAT_A: at(0.5)})
            self.assertNotIn("reply_status", [x["node"] for x in owed.ledger(d, now=NOW)["open"]])
            self.assertEqual(owed.settle(d, now=NOW), 2)                # the own preview of A, and the NG of F
            got = [r for r in owed_rows(d) if r["how"] == "own_preview"]
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["chat_id"], CHAT_A)
            self.assertEqual(got[0]["closed_at"], at(0.5))
            self.assertEqual(got[0]["n"], 1)
            self.assertEqual(set(got[0]), ALLOWED_ROW_KEYS)
            text = (Path(d) / "owed.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("fixture reply", text)
            self.assertNotIn("Sato", text)

    def test_my_message_from_before_the_debt_does_not_close_it(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture_rows()
            write_state(d, rows, fixture_items(rows), own={CHAT_A: at(3)})   # A was opened 1.5 hours ago; my message is older
            self.assertIn("reply_status", [x["node"] for x in owed.ledger(d, now=NOW)["open"]])

    def test_my_message_in_another_chat_closes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture_rows()
            write_state(d, rows, fixture_items(rows), own={"19:other@unq.gbl.spaces": at(0.1)})
            self.assertIn("reply_status", [x["node"] for x in owed.ledger(d, now=NOW)["open"]])

    def test_a_second_run_writes_no_second_row_for_the_same_debt(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture_rows()
            write_state(d, rows, fixture_items(rows), own={CHAT_A: at(0.5)})
            owed.settle(d, now=NOW)
            first = (Path(d) / "owed.jsonl").read_bytes()
            self.assertEqual(owed.settle(d, now=NOW), 0)
            self.assertEqual((Path(d) / "owed.jsonl").read_bytes(), first)


class TestNgAndDoneCloseADebt(unittest.TestCase):
    def test_a_rejected_reply_is_recorded_as_ng_once_and_is_not_a_debt(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture_rows()
            write_state(d, rows, fixture_items(rows))
            self.assertNotIn(CHAT_F, [x["chat"] for x in owed.ledger(d, now=NOW)["open"]])
            self.assertEqual([r["how"] for r in owed.ledger(d, now=NOW)["closed"] if r["chat"] == CHAT_F], ["ng"])
            owed.settle(d, now=NOW)
            self.assertEqual([(r["chat_id"], r["how"]) for r in owed_rows(d)], [(CHAT_F, "ng")])

    def test_a_rejection_older_than_the_keep_days_is_not_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            r = row("decision_today", "decide", "e-old", CHAT_F, "Ito", at(24 * 10), [reply(CHAT_F, "old")], needs_human=True)
            write_state(d, [r], {"1": item("rejected", r)})
            self.assertEqual(owed.settle(d, now=NOW), 0)
            self.assertFalse((Path(d) / "owed.jsonl").exists())

    def test_a_debt_marked_done_with_the_pm_closes_as_done(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture_rows()
            write_state(d, rows, {"1": item("closed", rows[3])})
            self.assertNotIn("ask_pm", [x["node"] for x in owed.ledger(d, now=NOW)["open"]])
            owed.settle(d, now=NOW)
            self.assertEqual([r["how"] for r in owed_rows(d)], ["done"])


class TestAnUnansweredDebtExpires(unittest.TestCase):
    def test_a_debt_stays_open_for_five_days_then_expires_at_that_time(self):
        with tempfile.TemporaryDirectory() as d:
            r = row("reply_status", "decide", "e-10", CHAT_A, "Sato", at(24 * 4), [reply(CHAT_A, "x")])
            write_state(d, [r])
            self.assertEqual(len(owed.ledger(d, now=NOW)["open"]), 1)
            later = NOW + timedelta(days=2)                              # 6 days after the decision
            self.assertEqual(owed.ledger(d, later)["open"], [])
            closed = owed.ledger(d, later)["closed"]
            self.assertEqual([(c["how"], c["closed"]) for c in closed], [("expired", datetime.fromisoformat(at(24 * 4)) + timedelta(days=5))])
            self.assertEqual(owed.settle(d, now=later), 1)
            self.assertEqual(owed_rows(d)[0]["how"], "expired")

    def test_owed_keep_days_shortens_the_debt(self):
        with tempfile.TemporaryDirectory() as d:
            r = row("reply_status", "decide", "e-11", CHAT_A, "Sato", at(24 * 3), [reply(CHAT_A, "x")])
            write_state(d, [r])
            self.assertEqual(len(owed.ledger(d, now=NOW)["open"]), 1)
            with mock.patch.dict(os.environ, {"KIMERU_OWED_KEEP_DAYS": "2"}):
                self.assertEqual(owed.ledger(d, now=NOW)["open"], [])

    def test_an_invalid_keep_days_is_five(self):
        for value in ("0", "x", "-3"):
            with mock.patch.dict(os.environ, {"KIMERU_OWED_KEEP_DAYS": value}):
                self.assertEqual(owed.keep_days(), 5)


class TestTheBriefAndTheCommand(unittest.TestCase):
    def test_the_brief_with_sent_watch_names_the_waiting_chats_oldest_first(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture_rows()
            write_state(d, rows, fixture_items(rows))
            section = owed.brief_section(d, now=NOW)
            self.assertEqual(section.split("\n"), [
                "返事を待っている人: 4 件",
                "・Tanaka 3 時間（1 件・未返信）",
                "・Kato 2 時間（1 件・承認済み・未送信）",
                "・Sato 1 時間（1 件・未返信）",
                "ほか 1 件（ask_pm は雑談も入るため、名前は出しません）",
            ])
            text = brief_text(d, ON)
            self.assertIn(section, text)
            self.assertNotIn("状況の自動返信", text)
            self.assertNotIn("fixture reply", text)

    def test_the_section_names_at_most_five_chats_and_counts_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [row("reply_status", "decide", f"e-{i}", f"19:c{i}@unq.gbl.spaces", f"N{i}", at(1 + i * 0.5), [reply(f"19:c{i}@unq.gbl.spaces", "x")])
                    for i in range(7)]
            write_state(d, rows)
            lines = owed.brief_section(d, now=NOW).split("\n")
            self.assertEqual(lines[0], "返事を待っている人: 7 件")
            self.assertEqual([l.split(" ")[0] for l in lines[1:6]], ["・N6", "・N5", "・N4", "・N3", "・N2"])
            self.assertEqual(lines[6], "ほか 2 件")

    def test_no_line_of_the_section_is_a_reply_word(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture_rows()
            write_state(d, rows, fixture_items(rows))
            for line in owed.brief_section(d, now=NOW).split("\n"):
                self.assertIsNone(notify.parse_reply(line), line)

    def test_the_command_lists_the_debts_and_says_so_when_the_setting_is_off(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture_rows()
            write_state(d, rows, fixture_items(rows))
            with mock.patch.dict(os.environ, OFF):
                self.assertIn("sent_watch=1", owed.cli_text(d, now=NOW))
            with mock.patch.dict(os.environ, ON):
                text = owed.cli_text(d, now=NOW)
            self.assertIn("Tanaka", text)
            self.assertIn("未返信", text)
            self.assertNotIn("fixture reply", text)

    def test_the_command_writes_nothing_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture_rows()
            write_state(d, rows, fixture_items(rows), own={CHAT_A: at(0.5)})
            before = snapshot(d)
            with mock.patch.dict(os.environ, ON):
                owed.cli_text(d, now=NOW)
            self.assertEqual(snapshot(d), before)


class TestTheDefaultBriefIsUnchanged(unittest.TestCase):
    def test_the_brief_with_the_setting_off_is_the_golden_text_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture_rows()
            write_state(d, rows, fixture_items(rows))
            self.assertEqual(brief_text(d), GOLDEN_BRIEF)
            self.assertEqual(brief_text(d, OFF), GOLDEN_BRIEF)

    def test_with_the_setting_on_the_brief_is_the_golden_text_plus_only_the_section(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture_rows()
            write_state(d, rows, fixture_items(rows))
            section = owed.brief_section(d, now=NOW)
            expected = GOLDEN_BRIEF.replace("Yamada、Sato\n\n承認済み", "Yamada、Sato\n" + section + "\n\n承認済み")
            self.assertNotEqual(expected, GOLDEN_BRIEF)
            self.assertEqual(brief_text(d, ON), expected)


class TestNothingIsWrittenOnTheNonExecutingPaths(unittest.TestCase):
    def test_a_cycle_without_send_or_without_real_writes_no_owed_file(self):
        now = datetime(2026, 10, 12, 7, 0)
        for send, real in ((False, True), (True, False)):
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
                rows = fixture_rows()
                write_state(Path(d) / "out", rows, fixture_items(rows))   # the settle would record the NG of F
                daily.cycle(Path(d) / "out", Path(d) / "inbox", GRAPHS, StubBackend(), PBS, process, bridge=FakeTeams(),
                            send=send, now=now, real=real, brief_hour=24)
                self.assertFalse((Path(d) / "out" / "owed.jsonl").exists(), (send, real))

    def test_a_busy_owed_lock_writes_nothing_and_settle_says_busy(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture_rows()
            write_state(d, rows, fixture_items(rows))
            with fsutil.exclusive(Path(d) / "owed.lock") as got:
                self.assertTrue(got)
                self.assertIsNone(owed.settle(d, now=NOW))
            self.assertFalse((Path(d) / "owed.jsonl").exists())


class TestTheRecordHoldsNoBodyOrName(unittest.TestCase):
    def test_the_owed_file_has_only_ids_times_counts_and_how_and_settle_changes_no_other_record(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture_rows()
            rows[0] = row("reply_status", "decide", "e-1", CHAT_A, SENTINEL_NAME, at(1.5), [reply(CHAT_A, SENTINEL_BODY)],
                          text=SENTINEL_BODY)
            write_state(d, rows, fixture_items(rows), own={CHAT_A: at(0.5)})
            before = {n: b for n, b in snapshot(d).items() if n in ("approvals.json", "decisions.jsonl", "pull_state.json")}
            owed.settle(d, now=NOW)
            self.assertTrue(owed_rows(d))
            for r in owed_rows(d):
                self.assertEqual(set(r), ALLOWED_ROW_KEYS)
            text = (Path(d) / "owed.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(SENTINEL_BODY, text)
            self.assertNotIn(SENTINEL_NAME, text)
            after = {n: b for n, b in snapshot(d).items() if n in ("approvals.json", "decisions.jsonl", "pull_state.json")}
            self.assertEqual(after, before)


class TestRetentionOfTheOwedFile(unittest.TestCase):
    def test_a_row_closed_more_than_ninety_days_ago_is_dropped_and_a_newer_one_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            old = {"chat_id": CHAT_A, "k": "aaaaaaaaaaaa", "opened_at": at(24 * 101), "closed_at": at(24 * 100), "how": "expired", "n": 1}
            new = {"chat_id": CHAT_B, "k": "bbbbbbbbbbbb", "opened_at": at(24 * 81), "closed_at": at(24 * 80), "how": "expired", "n": 1}
            (Path(d) / "owed.jsonl").write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n", encoding="utf-8")
            write_state(d)
            owed.settle(d, now=NOW)
            self.assertEqual(owed_rows(d), [new])

    def test_a_closure_older_than_ninety_days_is_never_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            r = row("reply_status", "decide", "e-12", CHAT_A, "Sato", at(24 * 200), [reply(CHAT_A, "x")])
            write_state(d, [r])
            self.assertEqual(owed.settle(d, now=NOW), 0)
            self.assertFalse((Path(d) / "owed.jsonl").exists())


class TestTheSettingsAreDocumented(unittest.TestCase):
    def test_owed_keep_days_is_a_setting_with_the_default_five(self):
        self.assertEqual(config.SETTINGS["owed_keep_days"], ("KIMERU_OWED_KEEP_DAYS", "5"))
        ref = (Path(__file__).resolve().parent.parent / "docs" / "reference.md").read_text(encoding="utf-8")
        self.assertEqual(sum(1 for l in ref.splitlines() if l.startswith("| `KIMERU_OWED_KEEP_DAYS`")), 1)


class TestTheDailyCycleRecordsMyReplyAfterAnOk(unittest.TestCase):
    def test_my_reply_after_the_ok_closes_the_debt_inside_the_cycle_and_posts_nothing_new(self):
        base = datetime.now()                                    # the cycle's clock is local
        # the pull stamps my own message with the PC's clock (not the cycle's `now`), so the decision is set an hour before it
        decided = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            out, inbox = Path(d) / "out", Path(d) / "inbox"
            out.mkdir()
            rec = {"graph": "teams_chat", "event_kind": "teams.chat", "event_id": "m-9", "node": "decision_today", "outcome": "decide",
                   "needs_human": True, "notify": False, "advice": "本日中に判断が必要: Sato「明日の件、確認できますか」", "at": decided,
                   "actions": [{"type": "teams.reply", "chat_id": "19:a_b@unq.gbl.spaces", "text": "承知しました。明日までに確認します。"}],
                   "event": ev("19:a_b@unq.gbl.spaces", "Sato", "m-9")}
            (out / "queue.jsonl").write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
            t = FakeTeams()
            t.chat_list = [one_on_one("明日の件、確認できますか", "9:00")]

            def cycle_at(when, send=True):
                return daily.cycle(out, inbox, GRAPHS, StubBackend(), PBS, process, bridge=t, send=send, now=when, real=True, brief_hour=24)

            cycle_at(base - timedelta(hours=2))                  # the item is posted
            t.timeline.append("R:OK 1")
            cycle_at(base - timedelta(hours=1, minutes=50))      # the OK is recorded (approved)
            self.assertEqual(owed_rows(out), [])                 # approved, not seen yet: still a debt
            t.chat_list = [one_on_one("あなた: 承知しました。明日までに確認します。", "9:20")]
            cycle_at(base + timedelta(minutes=30))               # my own message shows in the chat list
            got = owed_rows(out)
            self.assertEqual([(r["chat_id"], r["how"], r["n"]) for r in got], [("19:a_b@unq.gbl.spaces", "own_preview", 1)])
            self.assertTrue(all(p[0].startswith("[kimeru") for p in t.posts), t.posts)


if __name__ == "__main__":
    unittest.main()
