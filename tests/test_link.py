"""Related items (R19, setting link_related=1): the same case under another name is shown beside it, an alert that comes back
within 24 hours is a 再発火 notice, a duplicate ado.create is marked dup_of, a Teams message that names a pending ADO item shows its
number, and a minutes line of the same text is linked for 35 days. Nothing is merged, dropped or silenced, the judge never sees the link, and with the setting 0 every text and record is
the one captured before the change (golden). No network, no Teams: the bridge is a fake."""
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

from kimeru import config, graph, notify, plan
from kimeru.backends import StubBackend
from kimeru.cli import process
from tests.test_notify import REC, FakeBridge

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)
ALERT_PAYLOAD = json.loads((ROOT / "examples" / "monitor_alert.json").read_text(encoding="utf-8"))

# the records and texts captured with the setting at its default, before R19 (golden)
GOLD_POST = "[kimeru #3] ADO #4812\n→ 優先度を判定できないチケット #4812\nOK 3 / NG 3 / 詳細 3"
GOLD_NOTICE = "[kimeru 通知] ADO #4812\n→ 優先度を判定できないチケット #4812\n下書きは別の投稿で承認待ちです"
GOLD_ALERT_NOTICE = ("[kimeru 通知] checkout-api 5xx rate (Sev1) 5xx error rate 23% for 10 minutes; customers canno…\n"
                     "→ 障害対応の取りまとめ: 1.社内外への一次連絡（今日） → 2.対応責任者（インシデントコマンダー）を指名（今日） → "
                     "3.顧客影響の範囲を確認（今日） → 4.暫定対処（ロールバック等）の判断（今日） → 5.振り返り（ポストモーテム）…"
                     "（記録: 当番呼び出し, チャネル投稿, ADO 起票）")
GOLD_ALERT_KEYS = ["actions", "advice", "at", "event", "event_id", "event_kind", "executed", "graph", "graph_key", "judge",
                   "measurement", "needs_human", "node", "notify", "outcome", "path", "perf", "plan", "summary"]

ADO_ITEM = {"kind": "ado.workitem.created", "id": "4512", "type": "Bug", "title": "決済API の 500 エラー",
            "description": "決済の確定で 500 が返る", "acceptance_criteria": "決済が完了する", "priority": 1}
CHAT_REF = {"kind": "teams.chat", "id": "c-ref", "chat_id": "19:c1", "author": "佐藤", "text": "決済API #4512 でエラー"}


def alert(alert_id, fired, severity="Sev1", resources=None):
    p = copy.deepcopy(ALERT_PAYLOAD)
    p["data"]["essentials"].update({"alertId": alert_id, "firedDateTime": fired, "severity": severity})
    if resources is not None:
        p["data"]["essentials"]["alertTargetIDs"] = resources
    return p


def rows(out, name):
    f = Path(out) / name
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()] if f.exists() else []


def local_hm(iso):
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")


def notice_bodies(out):
    """The texts the automatic notices would post (pasted only: nothing is marked as sent)."""
    bridge = FakeBridge()
    notify.notify_notices(out, bridge, send=False)
    return [text for text, _ in bridge.posts]


def pend(out, res):
    """A decision that waits for the PM, as cli.process queues it and notify posts it: an approval item (number 1)."""
    (Path(out) / "queue.jsonl").write_text(json.dumps({**res, "needs_human": True}, ensure_ascii=False) + "\n", encoding="utf-8")
    notify.notify(out, FakeBridge(), send=True)


def queue_posts(out, *results):
    """The approval posts of `results`, as notify posts them: queued in this order (the first is #1, the next #2 ...); the texts only."""
    (Path(out) / "queue.jsonl").write_text("".join(json.dumps({**r, "needs_human": True}, ensure_ascii=False) + "\n" for r in results),
                                           encoding="utf-8")
    bridge = FakeBridge()
    notify.notify(out, bridge, send=True)
    return [text for text, _ in bridge.posts]


class SpyStub(StubBackend):
    """The judge's stub that also keeps every state it was asked about."""

    def __init__(self):
        self.states = []

    def ask(self, state, questions):
        self.states.append(json.loads(json.dumps(state, ensure_ascii=False, sort_keys=True)))
        return super().ask(state, questions)


class TestTheSettingIsOffAndTheTextsStayTheSame(unittest.TestCase):
    def test_the_setting_is_off_by_default_read_forgivingly_and_logged_and_shared_like_the_others(self):
        self.assertEqual(config.SETTINGS["link_related"], ("KIMERU_LINK_RELATED", "0"))
        self.assertEqual(config.CHOICES["link_related"], ("0", "1"))
        self.assertIn("link_related", config.LOGGABLE)
        self.assertIn("link_related", config.SHARE_VALUES)
        with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": "on"}):
            self.assertEqual(config.value("link_related"), "1")
        with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": "table"}):
            self.assertEqual(config.value("link_related"), "0")

    def test_with_the_setting_0_the_post_the_notice_and_the_record_are_the_golden_texts(self):
        from kimeru import link
        self.assertFalse(link.enabled())
        self.assertEqual(notify.format_post(3, REC), GOLD_POST)
        self.assertEqual(notify.format_notice(REC), GOLD_NOTICE)
        with tempfile.TemporaryDirectory() as d:
            res = process(copy.deepcopy(ALERT_PAYLOAD), GRAPHS, StubBackend(), Path(d), PBS, dedup=True)
            self.assertNotIn("link", res[0])
            self.assertEqual(notify.format_notice(rows(d, "notices.jsonl")[0]), GOLD_ALERT_NOTICE)
            record = rows(d, "decisions.jsonl")[0]
            self.assertEqual(sorted(record), GOLD_ALERT_KEYS)
            self.assertNotIn("link", record)
            self.assertFalse((Path(d) / "merged.jsonl").exists())

    def test_with_the_setting_1_the_judge_is_asked_the_same_questions_and_sees_no_link(self):
        spies = []
        for value in ("0", "1"):
            spy = SpyStub()
            spies.append(spy)
            with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": value}), tempfile.TemporaryDirectory() as d:
                process(alert("/subscriptions/x/alerts/a1", "2026-09-23T09:30:00Z"), GRAPHS, spy, Path(d), PBS, dedup=True)
                process(copy.deepcopy(ADO_ITEM), GRAPHS, spy, Path(d), PBS, dedup=True)
        self.assertTrue(spies[0].states)
        self.assertEqual(spies[0].states, spies[1].states)
        for state in spies[1].states:
            for key in ("link", "fp", "refs", "target", "dup_of"):
                self.assertNotIn(key, state)


class TestAnAlertThatComesBackIsARefire(unittest.TestCase):
    def test_a_second_alert_of_the_same_rule_and_resource_is_a_refire_notice_and_keeps_two_records(self):
        with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": "1"}), tempfile.TemporaryDirectory() as d:
            out = Path(d)
            process(alert("/subscriptions/x/alerts/a1", "2026-09-23T09:30:00Z"), GRAPHS, StubBackend(), out, PBS, dedup=True)
            process(alert("/subscriptions/x/alerts/a2", "2026-09-23T10:00:00Z"), GRAPHS, StubBackend(), out, PBS, dedup=True)
            self.assertEqual(len(rows(out, "decisions.jsonl")), 2)
            self.assertEqual(len(rows(out, "notices.jsonl")), 2)
            first, second = notice_bodies(out)
            self.assertFalse(first.startswith("[kimeru 通知] 再発火"))
            prefix = f"[kimeru 通知] 再発火 2 回目（初回 {local_hm(rows(out, 'decisions.jsonl')[0]['at'])}）（Bug は初回で記録済み）: "
            self.assertTrue(second.startswith(prefix), second)
            self.assertEqual(len(second.splitlines()), len(first.splitlines()))

    def test_a_refire_of_a_sev1_alert_keeps_its_notice_and_adds_only_the_prefix(self):
        with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": "1"}), tempfile.TemporaryDirectory() as d:
            out = Path(d)
            process(alert("/subscriptions/x/alerts/a1", "2026-09-23T09:30:00Z", "Sev1"), GRAPHS, StubBackend(), out, PBS, dedup=True)
            process(alert("/subscriptions/x/alerts/a2", "2026-09-23T10:00:00Z", "Sev1"), GRAPHS, StubBackend(), out, PBS, dedup=True)
            first, second = notice_bodies(out)
            head, _, rest = second.partition("\n")
            self.assertIn("再発火 2 回目", head)
            self.assertEqual(rest, first.partition("\n")[2])
            self.assertIn("Sev1", head)

    def test_linking_never_drops_a_notice_or_changes_its_notified_flag(self):
        with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": "1"}), tempfile.TemporaryDirectory() as d:
            out = Path(d)
            a = process(alert("/subscriptions/x/alerts/a1", "2026-09-23T09:30:00Z"), GRAPHS, StubBackend(), out, PBS, dedup=True)
            b = process(alert("/subscriptions/x/alerts/a2", "2026-09-23T10:00:00Z"), GRAPHS, StubBackend(), out, PBS, dedup=True)
            self.assertEqual([a[0]["notify"], b[0]["notify"]], [True, True])
            self.assertEqual(len(rows(out, "notices.jsonl")), 2)

    def test_a_refire_counts_only_a_same_or_lower_severity_within_24_hours(self):
        from kimeru import link
        now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        fp = "f" * 12
        rec = {"graph_key": "g:new", "graph": "alert-triage", "event_kind": "monitor.alert", "event_id": "a2", "node": "page",
               "event": {"kind": "monitor.alert", "severity": "Sev1", "rule": "r1"}, "outcome": "decide", "notify": True,
               "link": {"fp": fp, "refs": [], "target": "r1"}}
        cases = ((now - timedelta(hours=1), "Sev1", "再発火 2 回目"),     # the same severity, an hour ago: a refire
                 (now - timedelta(hours=25), "Sev1", ""),                # older than 24 hours
                 (now - timedelta(hours=1), "Sev2", ""))                 # a lower severity before: this one is an escalation
        for at, sev, want in cases:
            with tempfile.TemporaryDirectory() as d:
                row = {"graph": "alert-triage", "graph_key": "g:old", "event_kind": "monitor.alert", "event_id": "a1", "node": "page",
                       "at": at.isoformat(timespec="seconds"), "event": {"severity": sev, "rule": "r1"}, "link": rec["link"]}
                (Path(d) / "decisions.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
                note = link.notice_note(link.index(d, {}, now=now), rec)
                if want:
                    self.assertTrue(note["prefix"].startswith(want), (at, sev, note))
                else:
                    self.assertEqual(note["prefix"], "", (at, sev, note))


class TestAChatThatNamesAnAdoItem(unittest.TestCase):
    def process_pending_ado_then_chat(self, out, chat):
        pend(out, process(copy.deepcopy(ADO_ITEM), GRAPHS, StubBackend(), out, PBS, dedup=True)[0])
        return process(copy.deepcopy(chat), GRAPHS, StubBackend(), out, PBS, dedup=True)

    def test_a_chat_naming_a_pending_ado_item_shows_the_arrow_beside_its_text(self):
        with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": "1"}), tempfile.TemporaryDirectory() as d:
            out = Path(d)
            res = self.process_pending_ado_then_chat(out, CHAT_REF)
            self.assertEqual(res[0]["link"]["refs"], ["ado:4512"])
            [chat_text] = [t for t in notice_bodies(out) if t.startswith("[kimeru 通知] 佐藤")]
            self.assertIn("↔ ADO #4512（#1）", chat_text.splitlines()[1])
            self.assertLessEqual(len(chat_text.splitlines()), 4)

    def test_a_number_that_names_no_item_links_nothing(self):
        with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": "1"}), tempfile.TemporaryDirectory() as d:
            out = Path(d)
            chat = {**CHAT_REF, "text": "確認 #12 をお願いします"}
            res = self.process_pending_ado_then_chat(out, chat)
            self.assertNotIn("link", res[0])
            self.assertNotIn("↔", "\n".join(notice_bodies(out)))

    def test_linking_changes_only_the_arrow_line(self):
        def chat_notice(setting):
            with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": setting}), tempfile.TemporaryDirectory() as d:
                self.process_pending_ado_then_chat(Path(d), CHAT_REF)
                [text] = [t for t in notice_bodies(Path(d)) if t.startswith("[kimeru 通知] 佐藤")]
                return text

        off, on = chat_notice("0"), chat_notice("1")
        self.assertEqual(len(on.splitlines()), len(off.splitlines()))
        self.assertEqual(on.splitlines()[0], off.splitlines()[0])
        self.assertTrue(on.splitlines()[1].startswith(off.splitlines()[1]), (on, off))
        self.assertEqual(on.splitlines()[2:], off.splitlines()[2:])

    def approval_posts(self, out, chat=CHAT_REF):
        """The approval posts of the ADO item (#1) and of the chat that names it (#2), as notify posts them."""
        ado = process(copy.deepcopy(ADO_ITEM), GRAPHS, StubBackend(), out, PBS, dedup=True)[0]
        res = process(copy.deepcopy(chat), GRAPHS, StubBackend(), out, PBS, dedup=True)[0]
        return queue_posts(out, ado, res)

    def test_the_approval_post_of_a_chat_that_names_a_pending_ado_item_ends_its_arrow_line_with_the_item(self):
        with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": "1"}), tempfile.TemporaryDirectory() as d:
            ado_post, chat_post = self.approval_posts(Path(d))
            lines = chat_post.splitlines()
            self.assertTrue(lines[0].startswith("[kimeru #2]"), chat_post)
            self.assertTrue(lines[1].endswith(" ↔ ADO #4512（#1）"), chat_post)
            self.assertLessEqual(len(lines), 5)
            self.assertNotIn("↔", ado_post)

    def test_with_the_setting_0_the_approval_posts_have_no_arrow_and_linking_changes_only_the_arrow_line(self):
        def posts(setting):
            with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": setting}), tempfile.TemporaryDirectory() as d:
                return self.approval_posts(Path(d))

        off, on = posts("0"), posts("1")
        self.assertFalse(any("↔" in text for text in off), off)
        self.assertEqual(on[0], off[0])
        off_lines, on_lines = off[1].splitlines(), on[1].splitlines()
        self.assertEqual(len(on_lines), len(off_lines))
        self.assertEqual(on_lines[0], off_lines[0])
        self.assertEqual(on_lines[1], f"{off_lines[1]} ↔ ADO #4512（#1）")
        self.assertEqual(on_lines[2:], off_lines[2:])

    def test_the_approval_post_of_a_sev1_refire_says_the_same_case_and_keeps_its_other_lines(self):
        def posts(setting):
            with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": setting}), tempfile.TemporaryDirectory() as d:
                out = Path(d)
                first = process(alert("/subscriptions/x/alerts/a1", "2026-09-23T09:30:00Z", "Sev1"), GRAPHS, StubBackend(), out, PBS,
                                dedup=True)[0]
                second = process(alert("/subscriptions/x/alerts/a2", "2026-09-23T10:00:00Z", "Sev1"), GRAPHS, StubBackend(), out, PBS,
                                 dedup=True)[0]
                return queue_posts(out, first, second)

        off, on = posts("0"), posts("1")
        self.assertFalse(any("↔" in text for text in off), off)
        self.assertEqual(on[0], off[0])
        off_lines, on_lines = off[1].splitlines(), on[1].splitlines()
        self.assertLessEqual(len(on_lines), 5)
        self.assertEqual(len(on_lines), len(off_lines))
        self.assertEqual(on_lines[0], off_lines[0])
        self.assertTrue(on_lines[1].startswith(off_lines[1] + " ↔ 同じ件の可能性: "), on_lines[1])
        self.assertRegex(on_lines[1], r"アラート .+（#1）$")
        self.assertEqual(on_lines[2:], off_lines[2:])


class TestAMinutesLineComesBackWithin35Days(unittest.TestCase):
    NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)

    def write_old_row(self, d, kind, days_ago, link_value):
        at = (self.NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")
        row = {"graph": "weekly", "graph_key": "weekly:old", "event_kind": kind, "event_id": "m1", "node": "triage", "at": at,
               "event": {"kind": kind}, "link": link_value}
        (Path(d) / "decisions.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        return row

    def test_a_minutes_line_of_the_same_text_is_linked_with_its_date_within_35_days_and_nothing_else_is(self):
        from kimeru import link
        fp = "e" * 12

        def post_note(kind, days_ago, refs, fp_value):
            with tempfile.TemporaryDirectory() as d:
                target = {"fp": fp_value, "refs": refs, "target": "決済の遅延リスク"}
                self.write_old_row(d, kind, days_ago, target)
                new = {"graph": "weekly", "graph_key": "weekly:new", "event_kind": kind, "event_id": "m2", "node": "triage",
                       "event": {"kind": kind}, "link": target}
                return link.post_note(link.index(d, {}, now=self.NOW), new)

        old_at = (self.NOW - timedelta(days=20)).astimezone()
        self.assertEqual(post_note("meeting.item", 20, [], fp),
                         f"↔ 同じ件の可能性: {old_at.month}/{old_at.day} {old_at.strftime('%H:%M')} 議事録 決済の遅延リスク")
        self.assertIsNone(post_note("meeting.item", 36, [], fp))         # older than 35 days
        self.assertIsNone(post_note("monitor.alert", 20, [], fp))        # an alert counts for 7 days only
        self.assertIsNone(post_note("meeting.item", 20, ["ado:4512"], None))   # the ADO numbers of an old record do not count
        self.assertEqual(post_note("meeting.item", 2, ["ado:4512"], None), "↔ 議事録 決済の遅延リスク")

    def test_an_ado_number_of_an_old_record_is_not_a_known_item(self):
        from kimeru import link
        with tempfile.TemporaryDirectory() as d:
            self.write_old_row(d, "ado.workitem.created", 20, {"fp": "ado:4512", "refs": [], "target": "決済 API"})
            self.assertNotIn("4512", link.index(d, {}, now=self.NOW)["known"])
            self.assertEqual(len(link.index(d, {}, now=self.NOW)["rows"]), 0)


class TestTheRecordKeepsNoIdentifiers(unittest.TestCase):
    def test_link_and_target_keep_no_subscription_id_and_no_arm_path(self):
        from kimeru import link
        arm = "/subscriptions/0f1e2d3c-1111-2222-3333-444455556666/resourceGroups/prod/providers/Microsoft.Web/sites/checkout-api"
        with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": "1"}), tempfile.TemporaryDirectory() as d:
            process(alert("/subscriptions/x/alerts/a1", "2026-09-23T09:30:00Z", resources=[arm]), GRAPHS, StubBackend(), Path(d), PBS,
                    dedup=True)
            record = rows(d, "decisions.jsonl")[0]
            text = json.dumps(record["link"], ensure_ascii=False)
            self.assertNotIn("0f1e2d3c", text)
            self.assertNotIn("/subscriptions/", text)
            self.assertNotIn("sites/", text)
            self.assertEqual(record["link"]["target"], "checkout-api 5xx rate")
            self.assertRegex(record["link"]["fp"], r"^[0-9a-f]{12}$")
        self.assertEqual(link.target_of({"kind": "monitor.alert", "rule": f"x {arm}"}), "")

    def test_the_fingerprint_of_an_alert_is_the_rule_and_the_last_part_of_its_resource(self):
        from kimeru import link
        one = {"kind": "monitor.alert", "rule": "r1", "resources": ["/subscriptions/a/resourceGroups/g/providers/P/sites/web-1"]}
        same = {"kind": "monitor.alert", "rule": "r1", "resources": ["/subscriptions/b/resourceGroups/h/providers/P/sites/web-1"]}
        other = {"kind": "monitor.alert", "rule": "r1", "resources": ["/subscriptions/a/resourceGroups/g/providers/P/sites/web-2"]}
        self.assertEqual(link.fingerprint(one), link.fingerprint(same))
        self.assertNotEqual(link.fingerprint(one), link.fingerprint(other))
        self.assertEqual(link.fingerprint({"kind": "ado.workitem.created", "id": "4512"}), "ado:4512")
        self.assertEqual(link.fingerprint({"kind": "meeting.item", "item": "決済 の  件"}),
                         link.fingerprint({"kind": "meeting.item", "item": "決済 の 件"}))
        self.assertIsNone(link.fingerprint({"kind": "teams.chat", "text": "x"}))

    def test_refs_keep_only_the_ado_numbers_that_exist(self):
        from kimeru import link
        ev = {"kind": "teams.chat", "text": "決済API #4512 と ADO 77 と #12、#4512 もう一度"}
        self.assertEqual(link.refs_of(ev, {"4512", "77"}), ["ado:4512", "ado:77"])
        self.assertEqual(link.refs_of(ev, {"12"}), ["ado:12"])
        self.assertEqual(link.refs_of(ev, set()), [])


class TestNothingIsMergedOrDropped(unittest.TestCase):
    def test_linking_leaves_every_waiting_item_waiting_and_no_arm_path_in_out(self):
        sentinel = "ZZSUBSCRIPTION-SENTINEL"
        arm = f"/subscriptions/{sentinel}/resourceGroups/prod/providers/Microsoft.Web/sites/checkout-api"

        def run(setting):
            with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": setting}), tempfile.TemporaryDirectory() as d:
                out = Path(d)
                pend(out, process(copy.deepcopy(ADO_ITEM), GRAPHS, StubBackend(), out, PBS, dedup=True)[0])
                process(copy.deepcopy(CHAT_REF), GRAPHS, StubBackend(), out, PBS, dedup=True)
                for alert_id, fired in (("/subscriptions/x/alerts/a1", "2026-09-23T09:30:00Z"),
                                        ("/subscriptions/x/alerts/a2", "2026-09-23T10:00:00Z")):
                    process(alert(alert_id, fired, "Sev1", resources=[arm]), GRAPHS, StubBackend(), out, PBS, dedup=True)
                items = json.loads((out / "approvals.json").read_text(encoding="utf-8"))["items"]
                text = "".join(p.read_text(encoding="utf-8", errors="replace") for p in out.rglob("*") if p.is_file())
                return {k: v["status"] for k, v in items.items()}, text, len(rows(out, "notices.jsonl"))

        off_items, _, off_notices = run("0")
        on_items, on_text, on_notices = run("1")
        self.assertEqual(on_items, off_items)
        self.assertEqual(on_items, {"1": "pending"})
        self.assertEqual(on_notices, off_notices)
        self.assertNotIn(sentinel, on_text)

    def test_linking_writes_no_merge_and_no_second_approval_for_a_refire(self):
        with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": "1"}), tempfile.TemporaryDirectory() as d:
            out = Path(d)
            process(alert("/subscriptions/x/alerts/a1", "2026-09-23T09:30:00Z", "Sev1"), GRAPHS, StubBackend(), out, PBS, dedup=True)
            process(alert("/subscriptions/x/alerts/a2", "2026-09-23T10:00:00Z", "Sev1"), GRAPHS, StubBackend(), out, PBS, dedup=True)
            self.assertFalse((out / "merged.jsonl").exists())
            self.assertFalse((out / "approvals.json").exists())

    def test_a_duplicate_ado_create_is_marked_dup_of_and_stays_a_dry_run(self):
        with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": "1"}), tempfile.TemporaryDirectory() as d:
            out = Path(d)
            first = process(alert("/subscriptions/x/alerts/a1", "2026-09-23T09:30:00Z", "Sev1"), GRAPHS, StubBackend(), out, PBS,
                            dedup=True)
            second = process(alert("/subscriptions/x/alerts/a2", "2026-09-23T10:00:00Z", "Sev1"), GRAPHS, StubBackend(), out, PBS,
                             dedup=True)
            creates_first = [a for a in first[0]["actions"] if a.get("type") == "ado.create"]
            creates_second = [a for a in second[0]["actions"] if a.get("type") == "ado.create"]
            self.assertTrue(creates_first and creates_second)
            self.assertNotIn("dup_of", creates_first[0])
            self.assertEqual(creates_second[0]["dup_of"], local_hm(first[0]["at"]))
            statuses = [e["status"] for e in first[0]["executed"] if e["action"].get("type") == "ado.create"]
            self.assertEqual(statuses, [e["status"] for e in second[0]["executed"] if e["action"].get("type") == "ado.create"])
            self.assertTrue(all(s != "done" for s in statuses), statuses)


class TestARepeatedRiskLineAndARefireWhoseBugIsRecorded(unittest.TestCase):
    """The rest of R19's "しくみ": the advice of a minutes risk line seen before (within 35 days) starts with 既出のリスク（N 回目、初回
    M/D）, and the notice of a refire says the Bug was recorded by the first firing. Only with link_related=1."""
    NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    RISK = "リスク: 決済APIの遅延が出る可能性"

    @staticmethod
    def minutes(date, text=RISK):
        return {"title": f"週次定例 {date}", "date": date, "text": f"- {text}"}

    @staticmethod
    def md(iso):
        local = datetime.fromisoformat(iso).astimezone()
        return f"{local.month}/{local.day}"

    def old_row(self, out, days_ago, ev, node, actions=(), severity=None):
        from kimeru import link
        at = (self.NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")
        row = {"graph": "g", "graph_key": f"g:old{days_ago}", "event_kind": ev["kind"], "event_id": f"old{days_ago}", "node": node,
               "at": at, "event": {"kind": ev["kind"], **({"severity": severity} if severity else {})},
               "link": {"fp": link.fingerprint(ev), "refs": [], "target": "x"}, "actions": list(actions)}
        with (Path(out) / "decisions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_the_same_risk_line_in_the_next_weeks_is_marked_with_its_count_and_first_date_and_keeps_its_advice(self):
        with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": "1"}), tempfile.TemporaryDirectory() as d:
            out = Path(d)
            res = [process(self.minutes(f"2026-10-{day:02d}"), GRAPHS, StubBackend(), out, PBS, dedup=True)[0] for day in (1, 8, 15)]
            plain = res[0]["advice"]
            first_day = self.md(res[0]["at"])
            self.assertFalse(plain.startswith("既出のリスク"))
            self.assertEqual(res[1]["advice"], f"既出のリスク（2 回目、初回 {first_day}）: {plain.replace('2026-10-01', '2026-10-08')}")
            self.assertEqual(res[2]["advice"], f"既出のリスク（3 回目、初回 {first_day}）: {plain.replace('2026-10-01', '2026-10-15')}")
            self.assertEqual(len(rows(out, "decisions.jsonl")), 3)           # every decision is still recorded
            self.assertEqual([r["advice"] for r in rows(out, "decisions.jsonl")], [r["advice"] for r in res])
            other = process(self.minutes("2026-10-22", "リスク: 別の件の遅延が出る可能性"), GRAPHS, StubBackend(), out, PBS, dedup=True)[0]
            self.assertFalse(other["advice"].startswith("既出のリスク"))        # another text is another case

    def test_with_the_setting_0_the_risk_advice_is_the_same_whatever_came_before(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            first = process(self.minutes("2026-10-01"), GRAPHS, StubBackend(), out, PBS, dedup=True)[0]
            second = process(self.minutes("2026-10-08"), GRAPHS, StubBackend(), out, PBS, dedup=True)[0]
            self.assertNotIn("既出", first["advice"] + second["advice"])
            self.assertNotIn("link", second)
            self.assertEqual(first["advice"].replace("2026-10-01", "2026-10-08"), second["advice"])

    def test_only_a_risk_line_within_35_days_and_with_an_advice_is_marked(self):
        from kimeru import link
        ev = {"kind": "meeting.item", "id": "m9", "meeting": "週次定例", "item": self.RISK}
        for days_ago, node, advice, marked in ((20, "risk_item", "リスク登録済み", True), (20, "risk_planned", "計画", True),
                                              (36, "risk_item", "リスク登録済み", False), (20, "create_task", "タスク", False),
                                              (20, "risk_item", None, False)):
            with self.subTest(days_ago=days_ago, node=node, advice=advice), tempfile.TemporaryDirectory() as d:
                self.old_row(d, days_ago, ev, "risk_item")
                res = {"graph": "g", "graph_key": "g:new", "node": node, "event_kind": "meeting.item", "actions": [], "advice": advice}
                link.attach(res, ev, d, now=self.NOW)
                if marked:
                    old = (self.NOW - timedelta(days=days_ago)).astimezone()
                    self.assertEqual(res["advice"], f"既出のリスク（2 回目、初回 {old.month}/{old.day}）: {advice}")
                else:
                    self.assertEqual(res["advice"], advice)

    def test_a_refire_whose_first_firing_planned_a_bug_says_the_bug_is_recorded_and_still_posts_the_notice(self):
        with mock.patch.dict(os.environ, {"KIMERU_LINK_RELATED": "1"}), tempfile.TemporaryDirectory() as d:
            out = Path(d)
            for n, fired in enumerate(("2026-09-23T09:30:00Z", "2026-09-23T10:00:00Z", "2026-09-23T10:30:00Z"), 1):
                process(alert(f"/subscriptions/x/alerts/a{n}", fired, "Sev1"), GRAPHS, StubBackend(), out, PBS, dedup=True)
            first_rows = rows(out, "decisions.jsonl")
            self.assertTrue(any(a.get("type") == "ado.create" and a.get("work_item_type") == "Bug" for a in first_rows[0]["actions"]))
            first, second, third = notice_bodies(out)
            hm = local_hm(first_rows[0]["at"])
            self.assertFalse(first.startswith("[kimeru 通知] 再発火"))
            self.assertTrue(second.startswith(f"[kimeru 通知] 再発火 2 回目（初回 {hm}）（Bug は初回で記録済み）: "), second)
            self.assertTrue(third.startswith(f"[kimeru 通知] 再発火 3 回目（初回 {hm}）（Bug は初回で記録済み）: "), third)
            self.assertEqual(second.partition("\n")[2], first.partition("\n")[2])   # Sev1: the notice is whole, only its prefix grew
            self.assertEqual(len(second.splitlines()), len(first.splitlines()))

    def test_a_refire_whose_first_firing_planned_no_bug_has_the_old_prefix(self):
        from kimeru import link
        ev = {"kind": "monitor.alert", "id": "a1", "rule": "disk usage", "resources": ["/subscriptions/x/resourceGroups/r/providers/p/vm1"]}
        fp = link.fingerprint(ev)
        task = [{"type": "ado.create", "work_item_type": "Task", "title": "t"}]
        bug = [{"type": "ado.create", "work_item_type": "Bug", "title": "b"}]
        for actions, tail in ((task, ""), ([], ""), (bug, "（Bug は初回で記録済み）")):
            with self.subTest(tail=tail, actions=actions), tempfile.TemporaryDirectory() as d:
                self.old_row(d, 0.02, ev, "task_internal", actions, severity="Sev3")
                rec = {"event_kind": "monitor.alert", "link": {"fp": fp}, "event": {"severity": "Sev3"}, "graph_key": "g:new",
                       "at": self.NOW.isoformat(timespec="seconds")}
                note = link.notice_note(link.index(d, {}, now=self.NOW), rec)
                stamp = (self.NOW - timedelta(days=0.02)).astimezone().strftime("%H:%M")
                self.assertEqual(note["prefix"], f"再発火 2 回目（初回 {stamp}）{tail}: ")
        self.assertIsNone(link.notice_note(None, rec))                     # the setting 0: the notice is as before


if __name__ == "__main__":
    unittest.main()
