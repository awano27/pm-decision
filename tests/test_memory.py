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

from kimeru import brief, cli, config, memory, notify, push
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


if __name__ == "__main__":
    unittest.main()
