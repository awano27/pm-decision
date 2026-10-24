"""R16 前例: what kimeru decided before for the same chat, person, work item or rule is shown beside an item, with memory=1
(default 0). The lookup only reads the records (approvals.json, approvals.log.jsonl, the automatic decisions of decisions.jsonl,
merged.jsonl): nothing is written, no model and no Teams is asked, and the judge's state, the brief's subject, the toast and the
push text stay the same. Every Teams is a fake; every clock is injected or taken from the test's own."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru import brief, cli, config, fsutil, memory, notify, push
from kimeru.backends import StubBackend

try:
    from .test_notify import FakeBridge
except ImportError:
    from test_notify import FakeBridge

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)
MEMO_ON = {"KIMERU_MEMORY": "1"}
MEMO_OFF = {"KIMERU_MEMORY": "0"}

# the fixtures the golden texts were captured from (the unchanged code, memory unset)
GOLDEN_ADO = {"graph": "workitem-intake", "event_kind": "ado.workitem.created", "event_id": "4812", "node": "triage_pm",
              "outcome": "advise", "needs_human": True, "advice": "優先度を判定できないチケット #4812",
              "actions": [{"type": "ado.update", "id": "4812", "fields": {"Microsoft.VSTS.Common.Priority": 2}}]}
GOLDEN_CHAT = {"graph": "teams-chat-triage", "event_kind": "teams.chat", "event_id": "19:c1#m-1", "node": "plan_decision",
               "outcome": "decide", "needs_human": True, "advice": "来週の仕様変更の可否を判断してほしい",
               "event": {"kind": "teams.chat", "author": "佐藤", "chat_id": "19:c1", "chat_title": "仕様の相談",
                         "text": "来週の仕様変更は可能でしょうか"},
               "memo": {"summary": "仕様変更の可否", "next": "工数を見積もって返信する"},
               "actions": [{"type": "teams.reply", "text": "工数を確認して改めて返信します"}]}
GOLDEN_POST_ADO = "[kimeru #3] ADO #4812\n→ 優先度を判定できないチケット #4812\nOK 3 / NG 3 / 詳細 3"
GOLDEN_DETAIL_ADO = ("[kimeru 詳細 #3] 判断が必要（workitem-intake） / 改訂 1\n判断: 優先度を判定できないチケット #4812\n"
                     "次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n"
                     "不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\nOK の効果: 記録のみ\n"
                     "ado.workitem.created #4812\n対象: #4812\n内容: 優先度を判定できないチケット #4812\n"
                     "承認の対象: ado.update（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n返信: OK 3 / NG 3 / 保留 3")
GOLDEN_POST_CHAT = ("[kimeru #5] 佐藤さん: 仕様変更の可否\n→ 工数を見積もって返信する\n案: 工数を確認して改めて返信します\n"
                    "OK 5 / NG 5 / 詳細 5")
GOLDEN_DETAIL_CHAT = ("[kimeru 詳細 #5] 判断が必要（teams-chat-triage） / 改訂 1\n判断: 来週の仕様変更の可否を判断してほしい\n"
                      "次の一手: 工数を見積もって返信する\n不足情報: メモに記載なし\nOK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）\n"
                      "teams.chat #19:c1#m-1\n内容: 来週の仕様変更の可否を判断してほしい\n承認の対象: teams.reply（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n"
                      "文面 LLM のメモ:\n・要点: 仕様変更の可否\n・次の一手: 工数を見積もって返信する\n返信: OK 5 / NG 5 / 保留 5")
GOLDEN_BRIEF = ("[kimeru brief 2026-10-12] 今日の進め方\n"
                "1. 確認待ち #1: 来週の仕様変更の可否を判断してほしい\n"
                "2. 確認待ち #2: 来週の仕様変更の可否を判断してほしい\n"
                "確認待ち 2 件（OK 番号 / NG 番号 / 詳細 番号 で返信）\n\n"
                "承認済み・未完了の作業:\n- #3 優先度を判定できないチケット #4812")


def chat(eid, advice="来週の仕様変更の可否を判断してほしい", author="佐藤", chat_id="19:c1", node="plan_decision", **extra):
    """A Teams chat item as the approvals file keeps it (the same fields as GOLDEN_CHAT)."""
    return {**GOLDEN_CHAT, "event_id": eid, "node": node, "advice": advice,
            "event": {**GOLDEN_CHAT["event"], "author": author, "chat_id": chat_id}, **extra}


def key_of(rec):
    return f"{rec['graph']}:{rec['event_id']}:{rec['node']}"


def write_rows(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def write_state(d, numbered, log=(), decisions=(), merged=(), queue=()):
    """numbered: [(number, record, status)] as approvals.json keeps them; log, decisions, merged and queue: the other files."""
    p = Path(d)
    items = {str(n): {"key": key_of(rec), "status": status, "posted": True, "record": rec} for n, rec, status in numbered}
    (p / "approvals.json").write_text(json.dumps({"next": max(int(n) for n in items) + 1, "items": items}, ensure_ascii=False),
                                      encoding="utf-8")
    for name, rows in (("approvals.log.jsonl", log), ("decisions.jsonl", decisions), ("merged.jsonl", merged), ("queue.jsonl", queue)):
        if rows:
            write_rows(p / name, rows)
    return items


def snapshot(d):
    return {p.name: p.read_bytes() for p in sorted(Path(d).iterdir()) if p.is_file()}


def md(iso):
    t = datetime.fromisoformat(iso).astimezone()
    return f"{t.month}/{t.day}"


def at(days_before=0, hour=12):
    """An ISO time (UTC, noon) days before NOW; noon keeps the local date the same from UTC-11 to UTC+11."""
    return (NOW - timedelta(days=days_before)).replace(hour=hour).isoformat(timespec="seconds")


class TestTheDefaultTextsStayTheSame(unittest.TestCase):
    """R16: with memory at its default 0, the approval post, the detail, the morning brief row, the toast and the push text are the
    ones captured before the change (golden). A change to any of them must show here first."""

    def test_the_setting_is_off_by_default_read_forgivingly_and_logged_and_shared_like_the_others(self):
        self.assertEqual(config.SETTINGS["memory"], ("KIMERU_MEMORY", "0"))
        self.assertEqual(config.CHOICES["memory"], ("0", "1"))
        self.assertIn("memory", config.LOGGABLE)
        self.assertIn("memory", config.SHARE_VALUES)
        with mock.patch.dict(os.environ, {"KIMERU_MEMORY": "on"}):
            self.assertEqual(config.value("memory"), "1")
        with mock.patch.dict(os.environ, {"KIMERU_MEMORY": "table"}):
            self.assertEqual(config.value("memory"), "0")
        self.assertEqual(config.value("memory"), "0")

    def test_the_ado_post_and_its_detail_are_the_same_as_before(self):
        self.assertEqual(config.value("memory"), "0")
        self.assertEqual(notify.format_post(3, GOLDEN_ADO), GOLDEN_POST_ADO)
        self.assertEqual(notify.format_detail(3, GOLDEN_ADO), GOLDEN_DETAIL_ADO)

    def test_the_chat_post_and_its_detail_are_the_same_as_before(self):
        self.assertEqual(config.value("memory"), "0")
        self.assertEqual(notify.format_post(5, GOLDEN_CHAT), GOLDEN_POST_CHAT)
        self.assertEqual(notify.format_detail(5, GOLDEN_CHAT), GOLDEN_DETAIL_CHAT)

    def test_the_morning_brief_is_the_same_as_before_and_names_no_same_person(self):
        with tempfile.TemporaryDirectory() as d:
            write_state(d, [(1, chat("19:c1#m-1"), "pending"), (2, chat("19:c1#m-2", node="plan_decision"), "held"),
                            (3, GOLDEN_ADO, "approved")])
            text, _ = brief.build(d, StubBackend(), now=NOW, date="2026-10-12")
            self.assertEqual(text, GOLDEN_BRIEF)

    def test_the_toast_and_the_push_text_are_the_same_with_memory_on_and_off(self):
        with tempfile.TemporaryDirectory() as d:
            write_state(d, [(12, GOLDEN_CHAT, "approved"), (13, GOLDEN_CHAT, "pending")])
            with mock.patch.dict(os.environ, MEMO_OFF):
                off = (notify.toast_text(d, [13], 0), push.message([13], 0, 0))
            with mock.patch.dict(os.environ, MEMO_ON):
                on = (notify.toast_text(d, [13], 0), push.message([13], 0, 0))
            self.assertEqual(on, off)


class TestThePrecedentOnThePost(unittest.TestCase):
    """The approval post with memory=1: the arrow line ends with the precedent of the same chat; nothing else in the post changes."""

    def post(self, on, when):
        with tempfile.TemporaryDirectory() as d:
            old = chat("19:c1#m-1")
            write_state(d, [(12, old, "approved")], log=[{"at": when, "key": key_of(old), "id": 12, "status": "approved"}],
                        queue=[chat("19:c1#m-9", advice="別の依頼の判断")])
            b = FakeBridge()
            with mock.patch.dict(os.environ, MEMO_ON if on else MEMO_OFF):
                notify.notify(d, b, send=True)
            return b.posts[0][0]

    def test_the_arrow_line_ends_with_the_precedent_of_the_same_chat_and_the_post_is_otherwise_the_same(self):
        when = (datetime.now(timezone.utc) - timedelta(days=3)).replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
        on, off = self.post(True, when), self.post(False, when)
        expected = f"（前例: {md(when)} #12 OK）"
        self.assertEqual(len(on.splitlines()), len(off.splitlines()))
        self.assertTrue(on.splitlines()[1].startswith("→ "))
        self.assertTrue(on.splitlines()[1].endswith(" " + expected), on.splitlines()[1])
        self.assertEqual(on.replace(" " + expected, "", 1), off)
        self.assertNotIn("前例", off)

    def test_the_approval_records_and_the_decision_file_are_the_same_with_memory_on_and_off(self):
        """Only the timing fields differ between two runs (they are read from the clock); every record and the decision file match."""
        when = (datetime.now(timezone.utc) - timedelta(days=3)).replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
        kept = {}
        for flag in ("0", "1"):
            with tempfile.TemporaryDirectory() as d:
                old = chat("19:c1#m-1")
                decided = {**chat("19:c1#m-3"), "needs_human": False, "at": when}
                write_state(d, [(12, old, "approved")], log=[{"at": when, "key": key_of(old), "id": 12, "status": "approved"}],
                            decisions=[decided], queue=[chat("19:c1#m-9", advice="別の依頼の判断")])
                before = (Path(d) / "decisions.jsonl").read_bytes()
                with mock.patch.dict(os.environ, {"KIMERU_MEMORY": flag}):
                    notify.notify(d, FakeBridge(), send=True)
                data = json.loads((Path(d) / "approvals.json").read_text(encoding="utf-8"))
                for item in data["items"].values():
                    item.pop("measurement", None)
                kept[flag] = (data, (Path(d) / "decisions.jsonl").read_bytes() == before)
        self.assertTrue(kept["0"][1] and kept["1"][1])
        self.assertEqual(kept["0"][0], kept["1"][0])

    def test_the_precedent_is_at_most_twenty_characters(self):
        with tempfile.TemporaryDirectory() as d:
            old = chat("19:c1#m-1")
            items = write_state(d, [(12, old, "approved"), (13, chat("19:c1#m-9"), "pending")],
                                log=[{"at": at(2), "key": key_of(old), "id": 12, "status": "approved"}])
            pre = memory.precedent(memory.index(d, items, now=NOW), items["13"]["record"], 13)
            self.assertEqual(pre, f"（前例: {md(at(2))} #12 OK）")
            self.assertLessEqual(len(pre), 20)

    def test_a_hold_or_a_redo_is_not_a_precedent_but_shows_in_the_history(self):
        with tempfile.TemporaryDirectory() as d:
            old = chat("19:c1#m-1")
            items = write_state(d, [(12, old, "held"), (13, chat("19:c1#m-9"), "pending")],
                                log=[{"at": at(2), "key": key_of(old), "id": 12, "status": "held"}])
            idx = memory.index(d, items, now=NOW)
            self.assertIsNone(memory.precedent(idx, items["13"]["record"], 13))
            self.assertEqual(memory.history(idx, items["13"]["record"], 13), [f"・{md(at(2))} #12 保留: 佐藤さん: 仕様変更の可否"])

    def test_a_ng_is_a_precedent_too(self):
        with tempfile.TemporaryDirectory() as d:
            old = chat("19:c1#m-1")
            items = write_state(d, [(12, old, "rejected"), (13, chat("19:c1#m-9"), "pending")],
                                log=[{"at": at(2), "key": key_of(old), "id": 12, "status": "rejected"}])
            pre = memory.precedent(memory.index(d, items, now=NOW), items["13"]["record"], 13)
            self.assertEqual(pre, f"（前例: {md(at(2))} #12 NG）")


class TestTheLookupOrderAndWindow(unittest.TestCase):
    def test_the_same_chat_comes_before_the_same_person_and_the_newest_decision_comes_first(self):
        with tempfile.TemporaryDirectory() as d:
            items = write_state(d, [
                (3, chat("19:c1#m-1"), "approved"),                                   # the same chat, older
                (7, chat("19:c9#m-1", chat_id="19:c9"), "rejected"),                  # the same person, another chat, newer
                (12, chat("19:c1#m-2"), "rejected"),                                  # the same chat, newest
                (13, chat("19:c1#m-9"), "pending"),
            ], log=[{"at": at(5), "key": key_of(chat("19:c1#m-1")), "id": 3, "status": "approved"},
                    {"at": at(2), "key": key_of(chat("19:c9#m-1", chat_id="19:c9")), "id": 7, "status": "rejected"},
                    {"at": at(1), "key": key_of(chat("19:c1#m-2")), "id": 12, "status": "rejected"}])
            idx = memory.index(d, items, now=NOW)
            rec = items["13"]["record"]
            self.assertEqual(memory.precedent(idx, rec, 13), f"（前例: {md(at(1))} #12 NG）")
            self.assertEqual(memory.history(idx, rec, 13), [
                f"・{md(at(1))} #12 NG: 佐藤さん: 仕様変更の可否",
                f"・{md(at(5))} #3 OK: 佐藤さん: 仕様変更の可否",
                f"・{md(at(2))} #7 NG: 佐藤さん: 仕様変更の可否"])

    def test_the_history_shows_at_most_three_lines(self):
        with tempfile.TemporaryDirectory() as d:
            numbered = [(n, chat(f"19:c1#m-{n}"), "approved") for n in range(1, 6)] + [(20, chat("19:c1#m-20"), "pending")]
            log = [{"at": at(n), "key": key_of(chat(f"19:c1#m-{n}")), "id": n, "status": "approved"} for n in range(1, 6)]
            items = write_state(d, numbered, log=log)
            lines = memory.history(memory.index(d, items, now=NOW), items["20"]["record"], 20)
            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[0].startswith(f"・{md(at(1))} #1 OK"))

    def test_an_item_is_never_its_own_history(self):
        with tempfile.TemporaryDirectory() as d:
            items = write_state(d, [(13, chat("19:c1#m-9"), "held")],
                                log=[{"at": at(1), "key": key_of(chat("19:c1#m-9")), "id": 13, "status": "held"}])
            self.assertEqual(memory.history(memory.index(d, items, now=NOW), items["13"]["record"], 13), [])

    def test_a_decision_older_than_sixty_days_is_left_out_and_one_of_fifty_nine_days_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            items = write_state(d, [(12, chat("19:c1#m-1"), "approved"), (11, chat("19:c1#m-0"), "approved"),
                                    (13, chat("19:c1#m-9"), "pending")],
                                log=[{"at": at(61), "key": key_of(chat("19:c1#m-1")), "id": 12, "status": "approved"},
                                     {"at": at(59), "key": key_of(chat("19:c1#m-0")), "id": 11, "status": "approved"}])
            lines = memory.history(memory.index(d, items, now=NOW), items["13"]["record"], 13)
            self.assertEqual(lines, [f"・{md(at(59))} #11 OK: 佐藤さん: 仕様変更の可否"])

    def test_an_automatic_decision_shows_only_its_date_and_its_label(self):
        with tempfile.TemporaryDirectory() as d:
            auto = {**chat("19:c1#m-3"), "needs_human": False, "at": at(2), "plan": {"playbook": "incident", "steps": []}}
            items = write_state(d, [(13, chat("19:c1#m-9"), "pending")], decisions=[auto])
            idx = memory.index(d, items, now=NOW)
            self.assertEqual(memory.history(idx, items["13"]["record"], 13), [f"・{md(at(2))} 自動: 障害対応"])
            self.assertIsNone(memory.precedent(idx, items["13"]["record"], 13))

    def test_an_automatic_decision_without_a_playbook_says_it_was_only_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            auto = {**chat("19:c1#m-3"), "needs_human": False, "at": at(2)}
            items = write_state(d, [(13, chat("19:c1#m-9"), "pending")], decisions=[auto])
            self.assertEqual(memory.history(memory.index(d, items, now=NOW), items["13"]["record"], 13),
                             [f"・{md(at(2))} 自動: 記録のみ"])

    def test_a_merged_follow_up_counts_once_under_its_first_key(self):
        with tempfile.TemporaryDirectory() as d:
            auto = {**chat("19:c1#m-3"), "needs_human": False, "at": at(2)}
            again = {**chat("19:c1#m-4"), "needs_human": False, "at": at(1)}
            items = write_state(d, [(13, chat("19:c1#m-9"), "pending")], decisions=[auto, again],
                                merged=[{"at": at(1), "into": key_of(chat("19:c1#m-3")), "event_id": "19:c1#m-4", "revision": 1}])
            self.assertEqual(len(memory.history(memory.index(d, items, now=NOW), items["13"]["record"], 13)), 1)

    def test_a_work_item_or_a_rule_or_a_minutes_line_is_its_own_key(self):
        ado_old = {**GOLDEN_ADO, "event_id": "4812"}
        ado_new = {**GOLDEN_ADO, "event_id": "4812", "node": "triage_other"}
        alert_old = {"graph": "alert-triage", "event_kind": "monitor.alert", "event_id": "a1", "node": "impact", "outcome": "decide",
                     "needs_human": True, "advice": "x", "event": {"kind": "monitor.alert", "rule": "disk-full", "severity": "2"}}
        alert_new = {**alert_old, "event_id": "a2", "node": "impact2"}
        minutes_old = {"graph": "minutes-followup", "event_kind": "meeting.item", "event_id": "m1", "node": "has_due", "outcome": "decide",
                       "needs_human": True, "advice": "y", "event": {"kind": "meeting.item", "item": "リリース判断  を 行う"}}
        minutes_new = {**minutes_old, "event_id": "m2", "node": "has_due2", "event": {"kind": "meeting.item", "item": "リリース判断 を 行う"}}
        with tempfile.TemporaryDirectory() as d:
            items = write_state(d, [(1, ado_old, "approved"), (2, alert_old, "approved"), (3, minutes_old, "approved"),
                                    (4, ado_new, "pending"), (5, alert_new, "pending"), (6, minutes_new, "pending")],
                                log=[{"at": at(3), "key": key_of(ado_old), "id": 1, "status": "approved"},
                                     {"at": at(3), "key": key_of(alert_old), "id": 2, "status": "approved"},
                                     {"at": at(3), "key": key_of(minutes_old), "id": 3, "status": "approved"}])
            idx = memory.index(d, items, now=NOW)
            self.assertEqual(memory.precedent(idx, items["4"]["record"], 4), f"（前例: {md(at(3))} #1 OK）")
            self.assertEqual(memory.precedent(idx, items["5"]["record"], 5), f"（前例: {md(at(3))} #2 OK）")
            self.assertEqual(memory.precedent(idx, items["6"]["record"], 6), f"（前例: {md(at(3))} #3 OK）")

    def test_every_history_line_is_at_most_a_hundred_and_twenty_characters(self):
        long_name = "長い名前" * 30
        long_text = "来週の仕様変更の可否を判断してほしい、" * 20
        with tempfile.TemporaryDirectory() as d:
            old = chat("19:c1#m-1", advice=long_text, author=long_name, first_message={"text": long_text})
            items = write_state(d, [(12, old, "approved"), (13, chat("19:c1#m-9"), "pending")],
                                log=[{"at": at(2), "key": key_of(old), "id": 12, "status": "approved"}])
            lines = memory.history(memory.index(d, items, now=NOW), items["13"]["record"], 13)
            self.assertEqual(len(lines), 1)
            self.assertLessEqual(len(lines[0]), 120)

    def test_an_ado_item_with_no_earlier_decision_has_no_precedent_and_no_history(self):
        with tempfile.TemporaryDirectory() as d:
            items = write_state(d, [(3, GOLDEN_ADO, "pending")])
            idx = memory.index(d, items, now=NOW)
            self.assertIsNone(memory.precedent(idx, GOLDEN_ADO, 3))
            self.assertEqual(memory.history(idx, GOLDEN_ADO, 3), [])


class TestReadingWritesNothingAndAsksNobody(unittest.TestCase):
    def test_the_index_the_precedent_the_history_and_the_brief_count_write_no_file_and_change_no_byte(self):
        with tempfile.TemporaryDirectory() as d:
            old = chat("19:c1#m-1")
            items = write_state(d, [(12, old, "approved"), (13, chat("19:c1#m-9"), "pending")],
                                log=[{"at": at(2), "key": key_of(old), "id": 12, "status": "approved"}],
                                decisions=[{**chat("19:c1#m-3"), "needs_human": False, "at": at(1)}])
            before = snapshot(d)
            idx = memory.index(d, items, now=NOW)
            memory.precedent(idx, items["13"]["record"], 13)
            memory.history(idx, items["13"]["record"], 13)
            memory.same_person_count(items, 13, items["13"]["record"], NOW)
            with contextlib.redirect_stdout(io.StringIO()):
                text = memory.show(d, 13)
            self.assertIn("これまでの経緯:", text)
            self.assertEqual(snapshot(d), before)
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), sorted(before))

    def test_a_missing_state_folder_gives_nothing_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            d = Path(root) / "none"
            d.mkdir()
            self.assertEqual(memory.index(d, {}, now=NOW), {})
            self.assertEqual(list(d.iterdir()), [])


class TestTheBriefRow(unittest.TestCase):
    """The same person's count on a waiting row: with memory=1 only, and the subject the judge sees is the same."""

    def test_the_waiting_row_names_the_same_persons_count_and_the_subject_is_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            write_state(d, [(1, chat("19:c1#m-1", at=at(1)), "pending"), (2, chat("19:c1#m-2", at=at(0)), "held")])
            with mock.patch.dict(os.environ, MEMO_OFF):
                off = brief.collect(d, now=NOW)
            with mock.patch.dict(os.environ, MEMO_ON):
                on = brief.collect(d, now=NOW)
            self.assertEqual([i["subject"] for i in on], [i["subject"] for i in off])
            self.assertEqual([i["text"] for i in on], [t + "（同じ相手 2 件目）" for t in [i["text"] for i in off]])
            self.assertTrue(all("（同じ相手" not in i["text"] for i in off))

    def test_a_single_item_from_a_person_gets_no_count(self):
        with tempfile.TemporaryDirectory() as d:
            write_state(d, [(1, chat("19:c1#m-1", at=at(1)), "pending"), (2, GOLDEN_ADO, "pending")])
            with mock.patch.dict(os.environ, MEMO_ON):
                texts = [i["text"] for i in brief.collect(d, now=NOW)]
            self.assertFalse(any("（同じ相手" in t for t in texts))


class TestTheDetailAndTheTerminal(unittest.TestCase):
    def test_the_detail_with_memory_on_shows_the_history_just_above_the_reply_line(self):
        with tempfile.TemporaryDirectory() as d:
            old = chat("19:c1#m-1")
            write_state(d, [(12, old, "approved"), (13, chat("19:c1#m-9"), "pending")],
                        log=[{"at": at(2), "key": key_of(old), "id": 12, "status": "approved"}])
            with mock.patch.dict(os.environ, MEMO_OFF):
                off = self.detail(d)
            with mock.patch.dict(os.environ, MEMO_ON):
                on = self.detail(d)
            lines = on.splitlines()
            self.assertEqual(lines[-1], "返信: OK 13 / NG 13 / 保留 13")
            self.assertEqual(lines[-3:-1], ["これまでの経緯:", f"・{md(at(2))} #12 OK: 佐藤さん: 仕様変更の可否"])
            block = f"これまでの経緯:\n・{md(at(2))} #12 OK: 佐藤さん: 仕様変更の可否\n"
            self.assertEqual(on.replace(block, ""), off)
            self.assertNotIn("これまでの経緯", off)

    def detail(self, d):
        b = FakeBridge(replies=["詳細 13"])
        notify.collect(d, b, send=True)
        return b.posts[-1][0]

    def test_the_kimeru_memory_command_shows_the_history_with_the_setting_off(self):
        with tempfile.TemporaryDirectory() as d:
            old = chat("19:c1#m-1")
            write_state(d, [(12, old, "approved"), (13, chat("19:c1#m-9"), "pending"), (14, GOLDEN_ADO, "pending")],
                        log=[{"at": at(2), "key": key_of(old), "id": 12, "status": "approved"}])
            with mock.patch.dict(os.environ, MEMO_OFF):
                text = memory.show(d, 13)
                none = memory.show(d, 14)
                missing = memory.show(d, 99)
            self.assertEqual(text, f"[kimeru 経緯 #13] 佐藤さん: 仕様変更の可否\n"
                                   f"これまでの経緯:\n・{md(at(2))} #12 OK: 佐藤さん: 仕様変更の可否")
            self.assertIn("同じチャット・相手・作業項目・規則の過去の件はありません", none)
            self.assertEqual(missing, "#99 は承認の記録にありません")

    def test_the_command_is_read_only_and_parses_a_number(self):
        self.assertIn("memory", cli.READ_ONLY)
        with tempfile.TemporaryDirectory() as d:
            write_state(d, [(13, chat("19:c1#m-9"), "pending")])
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                rc = cli.main(["--out", d, "memory", "13"])
            self.assertEqual(rc, 0)
            self.assertIn("[kimeru 経緯 #13]", buf.getvalue())

    def test_the_documents_name_the_setting_and_the_precedent_in_the_paste_row(self):
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        self.assertEqual(sum(1 for l in ref.splitlines() if l.startswith("| `KIMERU_MEMORY`")), 1)
        sec = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("前例", sec)
        self.assertIn("memory=1", sec)


class TestWriterLinesWithText(unittest.TestCase):
    """K02 (writer_precedent=1 and writer_precedent_text=1): a precedent line is followed by the reply the PM approved then."""

    BOTH = {"KIMERU_WRITER_PRECEDENT": "1", "KIMERU_WRITER_PRECEDENT_TEXT": "1"}

    def drafted(self, eid, text, drafted_by="copilot", kind="teams.reply", **extra):
        return chat(eid, actions=[{"type": kind, "text": text, "drafted_by": drafted_by}], **extra)

    def lines(self, rows, env=None, n=13):
        with mock.patch.dict(os.environ, env or self.BOTH), tempfile.TemporaryDirectory() as d:
            numbered = [(m, rec, st) for m, rec, st, _ in rows]
            log = [{"at": at(days), "key": key_of(rec), "id": m, "status": st} for m, rec, st, days in rows]
            write_state(d, numbered, log=log)
            return memory.writer_lines_with_text(d, chat("19:c1#m-13"), n, now=NOW)

    def test_an_approved_reply_follows_its_line_and_is_cut_at_200_characters(self):
        long = "確認して返信します。" * 30
        lines = self.lines([(12, self.drafted("19:c1#m-12", long), "approved", 3)])
        self.assertEqual(len(lines), 2)
        self.assertIn("#12 OK", lines[0])
        self.assertTrue(lines[1].startswith("　返信: 確認して"))
        self.assertEqual(len(lines[1]), len("　返信: ") + 200)
        self.assertTrue(lines[1].endswith("…"))

    def test_only_an_approved_item_with_a_drafted_reply_gets_a_text_line(self):
        rows = [(10, self.drafted("19:c1#m-10", "保留の文面"), "held", 4),
                (11, self.drafted("19:c1#m-11", "修正中の文面"), "redrafted", 3),
                (12, chat("19:c1#m-12"), "approved", 2)]   # approved, but the reply was not drafted by the writer
        lines = self.lines(rows)
        self.assertEqual(len(lines), 2)
        self.assertFalse(any("返信:" in line for line in lines))

    def test_an_ado_comment_counts_and_a_subscription_path_does_not(self):
        ok = self.drafted("19:c1#m-11", "コメントを返しました", kind="ado.comment")
        bad = self.drafted("19:c1#m-12", "/subscriptions/abc の件です")
        lines = self.lines([(11, ok, "approved", 4), (12, bad, "approved", 3)])
        self.assertEqual([line for line in lines if "返信:" in line], ["　返信: コメントを返しました"])

    def test_with_the_text_setting_off_nothing_is_added_and_with_precedent_off_nothing_is_read(self):
        rows = [(12, self.drafted("19:c1#m-12", "確認して返信します"), "approved", 3)]
        self.assertEqual(len(self.lines(rows)), 2)
        self.assertEqual(self.lines(rows, {"KIMERU_WRITER_PRECEDENT": "1", "KIMERU_WRITER_PRECEDENT_TEXT": "0"}), [])
        with mock.patch.object(memory, "index", side_effect=AssertionError("read")), \
                mock.patch.object(memory.fsutil, "read_json", side_effect=AssertionError("read")):
            self.assertEqual(self.lines(rows, {"KIMERU_WRITER_PRECEDENT": "0", "KIMERU_WRITER_PRECEDENT_TEXT": "1"}), [])

    def test_a_revised_then_approved_item_gets_its_reply_once_under_the_ok_line(self):
        rec = self.drafted("19:c1#m-12", "確認して返信します")
        lines = self.lines([(12, rec, "redrafted", 4), (12, rec, "approved", 3)])
        self.assertEqual(len(lines), 3)
        self.assertIn("#12 OK", lines[0])
        self.assertEqual(lines[1], "　返信: 確認して返信します")
        self.assertIn("#12 修正", lines[2])
        self.assertEqual(sum(1 for line in lines if line.startswith("　返信:")), 1)

    def test_reading_the_text_lines_changes_no_file_of_the_state_folder(self):
        rows = [(12, self.drafted("19:c1#m-12", "確認して返信します"), "approved", 3)]
        with mock.patch.dict(os.environ, self.BOTH), tempfile.TemporaryDirectory() as d:
            write_state(d, [(m, rec, st) for m, rec, st, _ in rows],
                        log=[{"at": at(days), "key": key_of(rec), "id": m, "status": st} for m, rec, st, days in rows],
                        decisions=[{"at": at(3), "key": key_of(rows[0][1]), "id": 12}])
            before = snapshot(d)
            self.assertEqual(len(memory.writer_lines_with_text(d, chat("19:c1#m-13"), 13, now=NOW)), 2)
            self.assertEqual(len(memory.writer_lines(d, chat("19:c1#m-13"), 13, now=NOW)), 2)
            self.assertEqual(snapshot(d), before)
            self.assertIn("approvals.json", before)
            self.assertIn("decisions.jsonl", before)

    def test_writer_lines_switches_on_the_text_setting_by_itself(self):
        rows = [(12, self.drafted("19:c1#m-12", "確認して返信します"), "approved", 3)]
        self.assertEqual(len(self.lines(rows)), 2)
        with mock.patch.dict(os.environ, self.BOTH), tempfile.TemporaryDirectory() as d:
            numbered = [(m, rec, st) for m, rec, st, _ in rows]
            write_state(d, numbered, log=[{"at": at(days), "key": key_of(rec), "id": m, "status": st} for m, rec, st, days in rows])
            self.assertEqual(len(memory.writer_lines(d, chat("19:c1#m-13"), 13, now=NOW)), 2)
            with mock.patch.dict(os.environ, {"KIMERU_WRITER_PRECEDENT_TEXT": "0"}):
                self.assertEqual(len(memory.writer_lines(d, chat("19:c1#m-13"), 13, now=NOW)), 1)

    def test_the_setting_is_off_by_default_and_listed_like_the_others(self):
        self.assertEqual(config.SETTINGS["writer_precedent_text"], ("KIMERU_WRITER_PRECEDENT_TEXT", "0"))
        self.assertEqual(config.CHOICES["writer_precedent_text"], ("0", "1"))
        self.assertIn("writer_precedent_text", config.LOGGABLE)
        self.assertIn("writer_precedent_text", config.SHARE_VALUES)


class TestTheIndexIsReadOnceAndTheTailIsSaid(unittest.TestCase):
    """K06: the logs are read again only when a file changed; the same answers with and without the reuse; a decisions.jsonl cut at
    8 MB says from which day it was read (only in `kimeru memory N`)."""

    ON = {"KIMERU_WRITER_PRECEDENT": "1"}

    def setUp(self):
        memory._cache.clear()
        self.addCleanup(memory._cache.clear)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    def state(self):
        old = chat("19:c1#m-1")
        auto = {"graph": "teams-chat-triage", "event_kind": "teams.chat", "event_id": "19:c1#m-5", "node": "plan_decision",
                "at": at(4), "plan": {}, "event": {"chat_id": "19:c1", "author": "佐藤"}}
        write_state(self.d, [(12, old, "approved"), (13, chat("19:c1#m-9"), "pending")],
                    log=[{"at": at(2), "key": key_of(old), "id": 12, "status": "approved"}], decisions=[auto],
                    merged=[{"into": "x", "event_id": "y"}])

    def spy(self):
        """(opened, read, patches): every Path.open and every fsutil.read_json call made while the patches are active."""
        opened, read = [], []
        real_open, real_read = Path.open, fsutil.read_json

        def counting_open(p, *a, **k):
            opened.append(str(p))
            return real_open(p, *a, **k)

        def counting_read(p, *a, **k):
            read.append(str(p))
            return real_read(p, *a, **k)
        return opened, read, (mock.patch.object(Path, "open", counting_open), mock.patch.object(fsutil, "read_json", counting_read))

    def lines(self):
        return memory.writer_lines(self.d, chat("19:c1#m-9"), 13, now=NOW)

    def test_a_second_writer_lines_for_the_same_records_opens_no_file(self):
        self.state()
        with mock.patch.dict(os.environ, self.ON):
            first = self.lines()
            opened, read, (p1, p2) = self.spy()
            with p1, p2:
                second = self.lines()
        self.assertEqual(len(first), 2)
        self.assertEqual(second, first)
        self.assertEqual((opened, read), ([], []))

    def test_one_more_byte_in_approvals_json_is_read_again(self):
        self.state()
        p = self.d / "approvals.json"
        with mock.patch.dict(os.environ, self.ON):
            self.lines()
            p.write_bytes(p.read_bytes() + b" ")
            opened, read, (p1, p2) = self.spy()
            with p1, p2:
                self.lines()
        self.assertEqual(read, [str(p)])
        self.assertNotIn(str(self.d / "approvals.log.jsonl"), opened)   # the logs have their own key

    def test_a_rewrite_of_the_same_size_is_read_again_because_the_mtime_is_in_the_key(self):
        self.state()
        p = self.d / "approvals.json"
        with mock.patch.dict(os.environ, self.ON):
            self.assertEqual(len(self.lines()), 2)
            before = p.stat()
            text = p.read_text(encoding="utf-8")
            p.write_text(text.replace('"approved"', '"approvee"', 1), encoding="utf-8")   # the same number of bytes
            os.utime(p, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
            self.assertEqual(p.stat().st_size, before.st_size)
            opened, read, (p1, p2) = self.spy()
            with p1, p2:
                self.lines()
        self.assertEqual(read, [str(p)])

    def test_a_save_of_approvals_json_alone_costs_index_no_read_json(self):
        self.state()
        p = self.d / "approvals.json"
        with mock.patch.dict(os.environ, self.ON):
            items = json.loads(p.read_text(encoding="utf-8"))["items"]
            memory.index(self.d, items, now=NOW)
            p.write_bytes(p.read_bytes() + b" ")
            opened, read, (p1, p2) = self.spy()
            with p1, p2:
                memory.index(self.d, items, now=NOW)
        self.assertEqual(read, [])
        self.assertEqual(opened, [])

    def test_an_approvals_json_that_exists_but_cannot_be_read_is_not_kept(self):
        self.state()
        with mock.patch.dict(os.environ, self.ON):
            with mock.patch.object(fsutil, "read_json", lambda path, default: default):   # e.g. a sharing violation
                self.assertEqual(len(self.lines()), 1)   # only the automatic line: no items
            self.assertEqual(len(self.lines()), 2)

    def test_index_history_and_writer_lines_are_the_same_with_and_without_the_reuse(self):
        self.state()
        rec = chat("19:c1#m-9")
        items = json.loads((self.d / "approvals.json").read_text(encoding="utf-8"))["items"]
        with mock.patch.dict(os.environ, self.ON):
            cold = (memory.index(self.d, items, now=NOW), self.lines())
            warm = (memory.index(self.d, items, now=NOW), self.lines())
            memory._cache.clear()
            again = memory.index(self.d, items, now=NOW)
        self.assertEqual(cold, warm)
        self.assertEqual(cold[0], again)
        self.assertEqual(memory.history(cold[0], rec, 13), memory.history(again, rec, 13))
        self.assertEqual(len(cold[0]["chat:19:c1"]), 2)

    def test_missing_files_have_a_key_and_a_file_that_appears_is_read(self):
        with mock.patch.dict(os.environ, self.ON):
            self.assertEqual(self.lines(), [])
            self.assertEqual(memory.index(self.d, {}, now=NOW), {})
            self.state()
            self.assertEqual(len(self.lines()), 2)

    def pad(self, days_before, megabytes):
        """decisions.jsonl rows of a decision that needs a human (the lookup skips them), dated days_before, `megabytes` MB in all."""
        row = json.dumps({"at": at(days_before), "needs_human": True, "pad": "x" * 900}) + "\n"
        with (self.d / "decisions.jsonl").open("a", encoding="utf-8") as f:
            for _ in range(int(megabytes * 1024 * 1024 // len(row.encode("utf-8"))) + 1):
                f.write(row)

    def test_a_decisions_log_cut_at_8_mb_says_from_which_day_it_was_read(self):
        self.state()
        (self.d / "decisions.jsonl").unlink()
        self.pad(20, 8.2)
        self.assertGreater((self.d / "decisions.jsonl").stat().st_size, 8 * 1024 * 1024)
        self.assertEqual(memory.TAIL_BYTES, 8 * 1024 * 1024)
        lines = memory.show(self.d, 13, now=NOW).splitlines()
        self.assertEqual(lines[-1], f"（自動決定は {md(at(20))} 以降の分だけ読めました）")
        self.assertEqual(sum("自動決定は" in line for line in lines), 1)

    def test_no_note_under_8_mb_or_when_the_oldest_row_read_is_older_than_60_days(self):
        self.state()
        (self.d / "decisions.jsonl").unlink()
        self.pad(20, 0.1)
        self.assertNotIn("自動決定は", memory.show(self.d, 13, now=NOW))
        (self.d / "decisions.jsonl").unlink()
        self.pad(70, 8.2)
        self.assertGreater((self.d / "decisions.jsonl").stat().st_size, memory.TAIL_BYTES)
        self.assertNotIn("自動決定は", memory.show(self.d, 13, now=NOW))

    def test_the_note_is_not_in_the_detail_or_the_brief(self):
        self.state()
        (self.d / "decisions.jsonl").unlink()
        self.pad(20, 8.2)
        with mock.patch.dict(os.environ, MEMO_ON):
            b = FakeBridge(replies=["詳細 13"])
            notify.collect(self.d, b, send=True)
            text, _ = brief.build(self.d, StubBackend(), now=NOW, date="2026-10-12")
        self.assertFalse(any("自動決定は" in t[0] for t in b.posts) or "自動決定は" in text)


if __name__ == "__main__":
    unittest.main()
