"""A follow-up replaces stale pending material under the same case identity; a thanks or OK after a waiting item is only a
followup line (rule `ack_only`), and nothing else about the item changes."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from kimeru import brief, cli, config, fulltext, graph, notify, plan, push
from kimeru.backends import ReplayBackend
from tests.test_link import alert
from tests.test_notify import FakeBridge
from tests.test_writer import FakeWriter

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)

# R32 (merge_recurring=1): a recurring alert of the same case joins the waiting item
RESOURCE = "/subscriptions/x/resourceGroups/prod/providers/Microsoft.Web/sites/checkout-api"
MERGE_ON = {"KIMERU_MERGE_RECURRING": "1"}
# the posts of two queued Sev3 alerts of one case with the setting 0, captured before R32 (golden)
GOLD_POSTS = [
    "[kimeru #1] checkout-api 5xx rate（Sev3）\n→ 影響度が判定できないアラート checkout-api 5xx rate (Sev3): 5分以内に人が確認\n"
    "起票案: [Alert] checkout-api 5xx rate\nOK 1 / NG 1 / 詳細 1",
    "[kimeru #2] checkout-api 5xx rate（Sev3）\n→ 影響度が判定できないアラート checkout-api 5xx rate (Sev3): 5分以内に人が確認\n"
    "起票案: [Alert] checkout-api 5xx rate\nOK 2 / NG 2 / 詳細 2",
]
GOLD_BRIEF = ("[kimeru brief 2026-10-12] 今日の進め方\n"
              "1. 確認待ち #1: 影響度が判定できないアラート checkout-api 5xx rate (Sev3): 5分以内に人が確認\n"
              "2. 確認待ち #2: 影響度が判定できないアラート checkout-api 5xx rate (Sev3): 5分以内に人が確認\n"
              "確認待ち 2 件（OK 番号 / NG 番号 / 詳細 番号 で返信）")


class UnsureJudge:
    """Every judge answers unsure, so an alert that names no outage goes to page_advice: a queued item the PM must answer."""

    def ask(self, state, questions):
        out = {}
        for qid, spec in questions.items():
            if spec["type"] == "noul":
                out[qid] = {"type": "noul", "noul": 0.5}
            else:
                n = len(spec["criteria"])
                out[qid] = {"type": "score", "score": 1.0, "confidence": 0.2,
                            "probabilities": {str(i): 1.0 / n for i in range(n)}}
        return out


def recurring(alert_id, fired, severity="Sev3", resource=RESOURCE, rule=None):
    """An Azure Monitor alert of the test case; its description names no outage, so it is judged to page_advice."""
    p = alert(alert_id, fired, severity, [resource])
    p["data"]["essentials"]["description"] = "5xx error rate 3% for 10 minutes"
    if rule:
        p["data"]["essentials"]["alertRule"] = rule
    return p


def judged(out, payload):
    return cli.process(payload, GRAPHS, UnsureJudge(), out, PBS, dedup=True)


def seed_next(out, number):
    ap = notify.Approvals(out)
    ap.data["next"] = number
    ap.save()


def items_of(out):
    return notify.Approvals(out).data["items"]


def rows(out, name):
    f = Path(out) / name
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()] if f.exists() else []


def local_hm(iso):
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")


class TestFollowupMerge(unittest.TestCase):
    def test_merge_replaces_stale_fields_scrubs_backup_and_keeps_unknown_hold(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            ap = notify.Approvals(out)
            old_key = "g:old:n"
            old = {"graph": "g", "event_id": "old", "node": "n", "outcome": "advise",
                   "needs_human": True, "advice": "old decision", "summary": "old source",
                   "event": {"kind": "teams.chat", "id": "old", "chat_id": "chat-1", "author": "A"},
                   "material_event": {"text": "old excerpt"}, "copilot_request": "OLD_PRIVATE_REQUEST",
                   "actions": [{"type": "ado.comment", "text": "old draft", "exec_text": "OLD_PRIVATE_EXEC",
                                "exec_key": "OLD_EXEC_KEY", "exec_state": "done"}]}
            old["followups"] = [{"text": "OLD_FOLLOWUP " * 20, "at": "2026-10-02T00:00:00Z"}]
            ap.data["items"]["7"] = {"key": old_key, "revision": 3, "status": "pending", "posted": True,
                                        "record": old, "exec": {"0": {"state": "done", "text": "OLD_PRIVATE_EXEC"}},
                                        "exec_history": [{"text": "OLD_PRIVATE_EXEC"}],
                                        "delivery_unknown": True, "delivery_unknown_part": "post",
                                        "delivery_unknown_at": "2026-10-03T00:00:00Z"}
            ap.data["next"] = 8
            ap.save()
            fulltext.save(out, old_key, {"text": "OLD_PRIVATE_FULL", "author": "A"})
            long_source = "public " * 300 + "TAIL_PRIVATE_SECRET beyond the retained preview"
            safe_preview = ("public " * 30)[:119] + "…"
            new_event = {"kind": "teams.chat", "id": "new", "chat_id": "chat-1", "author": "A",
                         "text": long_source}
            new_result = {"graph": "g", "event_id": "new", "node": "n", "outcome": "decide",
                          "needs_human": False, "advice": "new decision", "summary": "new source",
                          "event": {**new_event, "text": safe_preview}, "material_event": {"text": safe_preview},
                          "actions": [{"type": "log.only", "text": "new action"}], "plan": {"title": "new", "summary": "next"}}
            self.assertTrue(cli._merge_locked(out, new_event, new_result, notify, source_event=new_event))
            saved = notify.Approvals(out).data["items"]["7"]
            self.assertEqual(saved["key"], old_key)
            self.assertEqual(saved["revision"], 4)
            self.assertEqual(saved["record"]["event_id"], "new")
            self.assertEqual([a["type"] for a in saved["record"]["actions"]], ["log.only"])
            self.assertNotIn("copilot_request", saved["record"])
            self.assertNotIn("exec", saved)
            self.assertNotIn("exec_history", saved)
            self.assertFalse(saved["posted"])
            self.assertTrue(saved["delivery_unknown"])
            self.assertEqual(saved["delivery_unknown_revision"], 3)
            self.assertEqual(fulltext.load(out, old_key)["text"], long_source)
            self.assertNotIn("OLD_PRIVATE", (out / "approvals.json").read_text(encoding="utf-8"))
            backup = out / "approvals.json.bak"
            self.assertFalse(backup.exists() and "OLD_PRIVATE" in backup.read_text(encoding="utf-8"))
            for path in out.glob("*.jsonl"):
                self.assertNotIn("OLD_PRIVATE", path.read_text(encoding="utf-8"))
            for path in [out / "approvals.json", backup, out / "merged.jsonl"]:
                if path.exists():
                    self.assertNotIn("TAIL_PRIVATE_SECRET", path.read_text(encoding="utf-8"))
            self.assertIn("public", json.dumps(saved["record"], ensure_ascii=False))
            self.assertLessEqual(len(saved["record"]["followups"][0]["text"]), 120)
            self.assertEqual(notify.confirm_delivery(out, "case", "7", confirm_delivered=True)["status"], "confirmed")
            after_confirm = notify.Approvals(out).data["items"]["7"]
            self.assertFalse(after_confirm["posted"])  # the confirmed post belongs to revision 3, not revision 4
            self.assertEqual(after_confirm["status"], "pending")

    def test_approved_case_does_not_merge_even_if_an_old_delivery_hold_remains(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            ap = notify.Approvals(out)
            event = {"kind": "teams.chat", "id": "old", "chat_id": "chat-1", "author": "A"}
            ap.data["items"]["7"] = {"key": "g:old:n", "status": "approved", "delivery_unknown": True,
                                        "record": {"event": event, "node": "n", "actions": []}}
            ap.save()
            new_event = {**event, "id": "new", "text": "follow-up"}
            result = {"node": "n", "event": new_event, "actions": []}
            self.assertFalse(cli._merge_locked(out, new_event, result, notify, source_event=new_event))

    def test_a_recurring_sev3_alert_joins_the_waiting_item_across_cycles_under_its_number(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, MERGE_ON):
            out = Path(d)
            seed_next(out, 20)
            judged(out, recurring("/subscriptions/x/alerts/a1", "2026-09-23T09:30:00Z"))
            notify.notify(out, FakeBridge(), send=True)
            judged(out, recurring("/subscriptions/x/alerts/a2", "2026-09-23T10:00:00Z"))
            item = items_of(out)["20"]
            self.assertEqual((item["revision"], item["status"], item["posted"]), (2, "pending", False))
            self.assertEqual(item["record"]["followups"][-1]["text"], "再発 " + local_hm(rows(out, "decisions.jsonl")[-1]["at"]))
            self.assertEqual(sorted(items_of(out)), ["20"])          # the recurrence is no number of its own
            self.assertEqual(notify.Approvals(out).data["next"], 21)
            self.assertEqual(len(rows(out, "queue.jsonl")), 1)      # and it is not queued a second time
            judged(out, recurring("/subscriptions/x/alerts/a3", "2026-09-23T10:30:00Z"))
            self.assertEqual(items_of(out)["20"]["revision"], 3)
            self.assertEqual(sorted(items_of(out)), ["20"])
            self.assertEqual([r["revision"] for r in rows(out, "merged.jsonl")], [2, 3])
            bridge = FakeBridge()
            notify.notify(out, bridge, send=True)   # posted again under the same number, as an update (not a thanks-style fold)
            self.assertEqual([t.split("\n")[0] for t, _ in bridge.posts], ["[kimeru #20] （更新）checkout-api 5xx rate（Sev3）"])

    def test_three_recurring_sev3_alerts_in_one_cycle_are_one_item_at_revision_3(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, MERGE_ON):
            out = Path(d)
            seed_next(out, 20)
            for i, fired in enumerate(("2026-09-23T09:30:00Z", "2026-09-23T09:40:00Z", "2026-09-23T09:50:00Z")):
                judged(out, recurring(f"/subscriptions/x/alerts/b{i}", fired))
            self.assertEqual(len(rows(out, "queue.jsonl")), 3)
            bridge = FakeBridge()
            notify.notify(out, bridge, send=True)
            self.assertEqual(sorted(items_of(out)), ["20"])
            self.assertEqual(items_of(out)["20"]["revision"], 3)
            self.assertEqual(len(items_of(out)["20"]["record"]["followups"]), 2)
            self.assertEqual(len(bridge.posts), 1)
            self.assertTrue(bridge.posts[0][0].startswith("[kimeru #20] （更新）"))
            again = FakeBridge()
            notify.notify(out, again, send=True)   # the folded queue lines are not folded a second time
            self.assertEqual(items_of(out)["20"]["revision"], 3)
            self.assertEqual(again.posts, [])

    def test_the_morning_brief_shows_one_waiting_row_for_three_recurrences_in_one_cycle(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, MERGE_ON):
            out = Path(d)
            seed_next(out, 20)
            for i, fired in enumerate(("2026-09-23T09:30:00Z", "2026-09-23T09:40:00Z", "2026-09-23T09:50:00Z")):
                judged(out, recurring(f"/subscriptions/x/alerts/d{i}", fired))
            notify.notify(out, FakeBridge(), send=True)
            self.assertEqual(sorted(items_of(out)), ["20"])
            self.assertEqual(len(rows(out, "queue.jsonl")), 3)   # the two folded lines stay in the queue file
            text, _ranked = brief.build(out, UnsureJudge(), now=datetime.now(timezone.utc), date="2026-10-12")
            shown = [l for l in text.splitlines() if "確認待ち" in l]
            self.assertEqual(len(shown), 2, text)   # the numbered row and the count line, nothing else
            self.assertTrue(shown[0].startswith("1. 確認待ち #20: "), text)
            self.assertTrue(shown[1].startswith("確認待ち 1 件（"), text)
            self.assertNotIn("未投稿", text)
            self.assertNotIn("#21", text)
            self.assertNotIn("#22", text)

    def test_a_recurring_sev1_alert_is_never_joined_and_keeps_its_own_number(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, MERGE_ON):
            out = Path(d)
            seed_next(out, 20)
            judged(out, recurring("/subscriptions/x/alerts/a1", "2026-09-23T09:30:00Z", "Sev1"))
            notify.notify(out, FakeBridge(), send=True)
            judged(out, recurring("/subscriptions/x/alerts/a2", "2026-09-23T10:00:00Z", "Sev1"))
            notify.notify(out, FakeBridge(), send=True)
            self.assertEqual(sorted(items_of(out)), ["20", "21"])
            self.assertNotIn("followups", items_of(out)["20"]["record"])
            self.assertFalse((Path(d) / "merged.jsonl").exists())

    def test_a_recurrence_of_an_approved_item_is_not_joined(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, MERGE_ON):
            out = Path(d)
            seed_next(out, 20)
            judged(out, recurring("/subscriptions/x/alerts/a1", "2026-09-23T09:30:00Z"))
            notify.notify(out, FakeBridge(), send=True)
            ap = notify.Approvals(out)
            ap.data["items"]["20"]["status"] = "approved"   # the PM has answered OK: a new number, the answered item stays as it was
            ap.save()
            judged(out, recurring("/subscriptions/x/alerts/a2", "2026-09-23T10:00:00Z"))
            notify.notify(out, FakeBridge(), send=True)
            self.assertEqual(sorted(items_of(out)), ["20", "21"])
            self.assertEqual(items_of(out)["20"]["status"], "approved")
            self.assertNotIn("followups", items_of(out)["20"]["record"])

    def test_a_recurrence_joins_only_in_the_cases_the_table_allows(self):
        # waiting severity, new severity, waiting status, same node, same case (fingerprint), new notice raised, joins
        cases = [
            ("Sev3", "Sev3", "pending", True, True, False, True),
            ("Sev2", "Sev3", "pending", True, True, False, True),   # a lower severity joins
            ("Sev3", "Sev3", "held", True, True, False, True),      # a held item joins and says it was held before
            ("Sev3", "Sev2", "pending", True, True, False, False),  # the severity rises: an escalation
            ("Sev1", "Sev1", "pending", True, True, False, False),  # Sev0/1 never join
            ("Sev3", "Sev1", "pending", True, True, False, False),
            ("Sev3", "Sev0", "pending", True, True, False, False),
            ("Sev3", "Sev3", "approved", True, True, False, False),  # the PM has answered
            ("Sev3", "Sev3", "pending", False, True, False, False),  # another route
            ("Sev3", "Sev3", "pending", True, False, False, False),  # another rule or resource
            ("Sev3", "Sev3", "pending", True, True, True, False),    # the recurrence raises a notice
        ]
        for old, new, status, same_node, same_case, notice, joins in cases:
            with self.subTest(old=old, new=new, status=status, node=same_node, case=same_case, notice=notice), \
                    tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, MERGE_ON):
                out = Path(d)
                seed_next(out, 20)
                judged(out, recurring("/subscriptions/x/alerts/a1", "2026-09-23T09:30:00Z", old))
                notify.notify(out, FakeBridge(), send=True)
                ap = notify.Approvals(out)
                ap.data["items"]["20"]["status"] = status
                ap.save()
                res = copy.deepcopy(ap.data["items"]["20"]["record"])
                res["event"] = {**res["event"], "severity": new}
                res["at"] = "2026-10-12T01:00:00+00:00"
                if not same_node:
                    res["node"] = "page_planned"
                if not same_case:
                    res["link"] = {**res["link"], "fp": "0" * 12}
                res["notify"] = notice
                joined = notify.fold_recurring(out, ap, res)
                self.assertEqual(joined == "20", joins)
                saved = notify.Approvals(out).data["items"]
                self.assertEqual(sorted(saved), ["20"])
                self.assertEqual(int(saved["20"].get("revision") or 1), 2 if joins else 1)
                if joins:
                    self.assertEqual(saved["20"]["status"], "pending")
                    self.assertEqual(saved["20"]["record"].get("held_before", False), status == "held")

    def test_an_ado_item_with_the_same_number_is_never_joined_even_at_merge_recurring_1(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, MERGE_ON):
            out = Path(d)
            ap = notify.Approvals(out)
            rec = {"graph": "ado-intake", "node": "triage", "event_kind": "ado.workitem.created", "event_id": "4512",
                   "event": {"kind": "ado.workitem.created", "id": "4512", "severity": "Sev3"}, "link": {"fp": "ado:4512"}}
            ap.data["items"]["1"] = {"key": "ado-intake:4512:triage", "status": "pending", "posted": True, "record": rec}
            ap.save()
            again = copy.deepcopy(rec)
            again["event_id"] = "4513"
            self.assertIsNone(notify.fold_recurring(out, ap, again))
            self.assertEqual(notify.Approvals(out).data["items"]["1"]["record"], rec)
            self.assertTrue(notify.Approvals(out).data["items"]["1"]["posted"])

    def test_the_resource_path_and_the_subscription_never_reach_any_record_of_a_recurrence(self):
        secret = "/subscriptions/SENTINEL-SUB-0001/resourceGroups/SENTINEL-RG/providers/Microsoft.Web/sites/checkout-api"
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, MERGE_ON):
            out = Path(d)
            seed_next(out, 20)
            judged(out, recurring("/subscriptions/x/alerts/a1", "2026-09-23T09:30:00Z", resource=secret))
            notify.notify(out, FakeBridge(), send=True)
            judged(out, recurring("/subscriptions/x/alerts/a2", "2026-09-23T10:00:00Z", resource=secret))
            self.assertEqual(items_of(out)["20"]["revision"], 2)
            for path in Path(d).iterdir():
                if path.is_file():
                    self.assertNotIn("SENTINEL", path.read_text(encoding="utf-8", errors="replace"), path.name)


class TestMergeRecurringOffIsTheSameAsBefore(unittest.TestCase):
    def test_the_setting_is_off_by_default_read_forgivingly_and_logged_and_shared_like_the_others(self):
        self.assertEqual(config.SETTINGS["merge_recurring"], ("KIMERU_MERGE_RECURRING", "0"))
        self.assertEqual(config.CHOICES["merge_recurring"], ("0", "1"))
        self.assertIn("merge_recurring", config.LOGGABLE)
        self.assertIn("merge_recurring", config.SHARE_VALUES)
        with mock.patch.dict(os.environ, {"KIMERU_MERGE_RECURRING": "on"}):
            self.assertEqual(config.value("merge_recurring"), "1")
        with mock.patch.dict(os.environ, {"KIMERU_MERGE_RECURRING": "table"}):
            self.assertEqual(config.value("merge_recurring"), "0")

    def test_with_the_setting_0_two_recurring_alerts_stay_two_items_and_the_texts_are_the_golden_ones(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"KIMERU_MERGE_RECURRING": "0"}):
            out = Path(d)
            judged(out, recurring("/subscriptions/x/alerts/a1", "2026-09-23T09:30:00Z"))
            judged(out, recurring("/subscriptions/x/alerts/a2", "2026-09-23T10:30:00Z"))
            bridge = FakeBridge()
            notify.notify(out, bridge, send=True)
            self.assertEqual([text for text, _ in bridge.posts], GOLD_POSTS)
            self.assertEqual(sorted(items_of(out)), ["1", "2"])
            self.assertFalse((out / "merged.jsonl").exists())
            for row in rows(out, "decisions.jsonl"):
                self.assertNotIn("link", row)
            text, _ranked = brief.build(out, UnsureJudge(), now=datetime.now(timezone.utc), date="2026-10-12")
            self.assertEqual(text, GOLD_BRIEF)
            self.assertEqual(notify.toast_text(out, [1, 2], [], unposted=0), ("kimeru: 確認待ち 2 件", "#1, #2"))
            self.assertEqual(push.message([1, 2], [], 0),
                             "kimeru: 確認待ち 2 件（#1, #2）。Teams の自分とのチャットを確認してください")
            self.assertEqual(push.message([], [], 2, unsent=(1,), due_today=(2,)),
                             "kimeru: 投稿できていない確認待ち 2 件 / 未送信 1 件（#1） / 今日が期限 1 件（#2）。"
                             "Teams の自分とのチャットを確認してください")

    def test_the_settings_table_in_the_reference_has_the_row_once(self):
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        self.assertEqual(sum(1 for l in ref.splitlines() if l.startswith("| `KIMERU_MERGE_RECURRING`")), 1)


class TestApprovalPostContext(unittest.TestCase):
    def test_decision_next_missing_effect_and_revision_precede_actions(self):
        rec = {"advice": "いまの判断", "memo": {"next": "利用者数を確認", "missing": ["影響する期間"]},
               "revision": 2,
               "actions": [{"type": "teams.reply", "text": "copy body"},
                           {"type": "ado.comment", "id": "9", "exec_text": "EXACT\nCOMMENT BODY"},
                           {"type": "log.only", "text": "a local note"}]}
        text = notify.format_detail(5, rec)   # the full text (the answer to `詳細 5`) keeps the order and all the parts
        self.assertLess(text.index("いまの判断"), text.index("利用者数を確認"))
        self.assertLess(text.index("利用者数を確認"), text.index("影響する期間"))
        self.assertIn("改訂 2", text)
        self.assertIn("記録", text)
        self.assertIn("コピー用", text)
        self.assertIn("実行設定が有効なら、承認後に ADO へコメントを書き込む", text)
        self.assertIn("EXACT\nCOMMENT BODY", text)
        short = notify.format_post(5, rec)   # the short approval post: the next step first, the exact comment, the revision as "（更新）"
        self.assertLessEqual(len(short.splitlines()), 6)
        self.assertTrue(short.startswith("[kimeru #5] （更新）"))
        self.assertLess(short.index("→ 利用者数を確認"), short.index("EXACT\nCOMMENT BODY"))
        self.assertIn("の #9 にコメントを書きます（上の文面そのまま）", short)
        self.assertNotIn("改訂", short)
        self.assertTrue(short.endswith("OK 5 / NG 5 / 詳細 5"))

    def test_repro_steps_are_kept_and_summary_limited_like_other_free_text(self):
        from unittest import mock
        long = "手順" * 150
        with mock.patch.object(cli.config, "value", return_value="0"):
            result = cli._recorded_event({"kind": "ado.workitem.created", "id": "1", "repro_steps": long})
        self.assertEqual(len(result["repro_steps"]), 120)
        self.assertTrue(result["repro_steps"].endswith("…"))

    def test_long_repro_steps_are_scrubbed_to_2000_and_kept_only_with_pending_full_text(self):
        from unittest import mock
        from kimeru import fulltext
        long = "手順" * 1300
        result = {"needs_human": True, "material_event": {"repro_steps": long}, "actions": []}
        ev = {"text": "preview", "thread": [], "author": "A"}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(cli.config, "value", return_value="1"):
            scrubbed, _ = cli._seal_material(result, [], d, "g:1:n", ev, merged=False)
            self.assertEqual(len(scrubbed["material_event"]["repro_steps"]), 2000)
            held = fulltext.load(d, "g:1:n")
            self.assertEqual(len(held["material"]["repro_steps"]), len(long))


class NoModel:
    """A judge that must not be asked anything: a thanks or OK is settled by the rule before any question."""

    def ask(self, state, questions):
        raise AssertionError(f"the model was asked {sorted(questions)}")


UNSURE_DECISION = ReplayBackend({"intent": {"choice": "decision", "confidence": 0.3, "probabilities": {}}})
CONFIDENT_FYI = ReplayBackend({"intent": {"choice": "fyi", "confidence": 0.9, "probabilities": {}}})
ASKED = "リリース日を決めてください"
THANKS = "ありがとうございます！"


class TestAcknowledgementFold(unittest.TestCase):
    """A thanks or OK from the same person after a waiting item joins that item as one followup line. The record, the
    revision, the posting, the status, the measurement and the full text stay as they were; nothing is asked or written."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)
        self.bridge, self.writer = FakeBridge(), FakeWriter()

    def ev(self, i, text, author="田中"):
        return {"kind": "teams.chat", "id": str(i), "chat_id": "19:same@thread.v2", "author": author, "text": text,
                "mentions_me": True}

    def process(self, ev, backend):
        return cli.process(ev, GRAPHS, backend, self.out, PBS, writer=self.writer, dedup=True)[0]

    def ask_pm(self):
        """The first request: the model is unsure, so it waits for the PM and is posted under #1."""
        self.process(self.ev(1, ASKED), UNSURE_DECISION)
        notify.notify(self.out, self.bridge, send=True)

    def item(self, n="1"):
        return notify.Approvals(self.out).data["items"][n]

    def test_thanks_after_a_waiting_request_only_adds_a_followup_line(self):
        self.ask_pm()
        before = self.item()
        posts, calls, short_post = len(self.bridge.posts), len(self.writer.calls), notify.format_post(1, before["record"])
        self.assertTrue(self.process(self.ev(2, THANKS), NoModel()).get("merged"))
        after = self.item()
        self.assertEqual(after["record"]["node"], "ask_pm")
        self.assertEqual([a["type"] for a in after["record"]["actions"]], ["teams.reply", "ado.create"])
        self.assertEqual(after["record"]["event"]["text"], ASKED)
        self.assertEqual([f["text"] for f in after["record"]["followups"]], [THANKS])
        for field in ("status", "revision", "posted", "toasted", "measurement"):
            self.assertEqual(after.get(field), before.get(field), field)
        notify.notify(self.out, self.bridge, send=True)
        self.assertEqual(len(self.bridge.posts), posts)                 # nothing is posted again
        self.assertEqual(len(self.writer.calls), calls)                 # the writer is not called
        self.assertEqual(notify.format_post(1, after["record"]), short_post)   # the short post the PM sees is byte-identical

    def test_thanks_after_a_hold_keeps_the_item_held(self):
        self.ask_pm()
        notify.collect(self.out, FakeBridge(["保留 1"]), send=False)
        self.assertEqual(self.item()["status"], "held")
        self.assertTrue(self.process(self.ev(2, THANKS), NoModel()).get("merged"))
        self.assertEqual(self.item()["status"], "held")
        self.assertEqual([f["text"] for f in self.item()["record"]["followups"]], [THANKS])

    def test_a_substantive_fyi_from_the_same_person_is_still_posted_again_under_the_same_number(self):
        self.ask_pm()
        self.process(self.ev(2, "資料を共有しました"), CONFIDENT_FYI)
        item = self.item()
        self.assertEqual(item["status"], "pending")
        self.assertFalse(item["posted"])                                # replaced, so it is shown again under #1
        self.assertEqual(item["revision"], 2)
        self.assertEqual([f["text"] for f in item["record"]["followups"]], ["資料を共有しました"])
        notify.notify(self.out, self.bridge, send=True)
        self.assertIn("（更新）", self.bridge.posts[-1][0])

    def test_thanks_calls_no_model_and_no_writer_and_keeps_the_full_text_waiting(self):
        self.ask_pm()
        long_text = "田中さんの長いメッセージ " * 40
        fulltext.save(self.out, self.item()["key"], {"text": long_text, "author": "田中"})
        calls = len(self.writer.calls)
        self.process(self.ev(2, THANKS), NoModel())                    # NoModel fails the test if it is asked
        self.assertEqual(len(self.writer.calls), calls)
        self.assertEqual(fulltext.load(self.out, self.item()["key"])["text"], long_text)

    def test_thanks_does_not_join_an_item_the_pm_already_approved(self):
        self.ask_pm()
        notify.collect(self.out, FakeBridge(["OK 1"]), send=False)
        self.assertEqual(self.item()["status"], "approved")
        self.assertFalse(self.process(self.ev(2, THANKS), NoModel()).get("merged"))
        self.assertNotIn("followups", self.item()["record"])
        self.assertEqual(self.item()["status"], "approved")

    def test_a_continuation_that_notifies_is_not_folded_even_with_thanks_in_it(self):
        self.ask_pm()
        res = self.process(self.ev(2, "ありがとうございます。本番で決済が通りません"), NoModel())
        self.assertFalse(res.get("merged"))
        self.assertTrue(res["notify"])
        self.assertNotIn("followups", self.item()["record"])

    def test_the_followups_keep_the_newest_twenty_lines_and_cut_each_at_120_characters(self):
        self.ask_pm()
        texts = [f"ありがとうございます{'！' * i}" for i in range(1, 24)]
        for i, text in enumerate(texts, start=2):
            self.process(self.ev(i, text), NoModel())
        followups = [f["text"] for f in self.item()["record"]["followups"]]
        self.assertEqual(followups, texts[-20:])
        self.process(self.ev(30, "ありがとうございます" * 13), NoModel())
        last = self.item()["record"]["followups"][-1]["text"]
        self.assertLessEqual(len(last), 120)
        self.assertTrue(last.endswith("…"))


class TestContinuedItem(unittest.TestCase):
    """A continuation that revises a waiting item (the same node) keeps the first request: line 1 names it, the newest message
    follows as "続き「…」", a held item says "（前回 保留）" until the PM answers OK or NG, and the record keeps the first request
    at most 120 characters long (R08)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)
        self.bridge, self.writer = FakeBridge(), FakeWriter()

    def ev(self, i, text, author="田中"):
        return {"kind": "teams.chat", "id": str(i), "chat_id": "19:same@thread.v2", "author": author, "text": text,
                "mentions_me": True}

    def process(self, ev, backend):
        return cli.process(ev, GRAPHS, backend, self.out, PBS, writer=self.writer, dedup=True)[0]

    def ask_pm(self, text=ASKED):
        self.process(self.ev(1, text), UNSURE_DECISION)
        notify.notify(self.out, self.bridge, send=True)

    def item(self, n="1"):
        return notify.Approvals(self.out).data["items"][n]

    def test_a_continuation_posts_the_first_request_on_line_one_and_the_newest_message_on_line_three(self):
        self.ask_pm()
        self.process(self.ev(2, "資料を共有しました"), CONFIDENT_FYI)
        self.assertEqual(self.item()["revision"], 2)
        notify.notify(self.out, self.bridge, send=True)
        text = self.bridge.posts[-1][0]
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("[kimeru #1] （更新）田中さん: リリース日を決めてください"), lines[0])
        self.assertEqual(lines[2], "続き「資料を共有しました」")
        self.assertLessEqual(len(lines), 5)
        self.assertNotIn("⚠", text)

    def test_a_held_item_that_is_continued_says_held_before_and_the_detail_keeps_both_requests(self):
        self.ask_pm()
        notify.collect(self.out, FakeBridge(["保留 1"]), send=False)
        self.assertEqual(self.item()["status"], "held")
        self.process(self.ev(2, "資料を共有しました"), CONFIDENT_FYI)
        item = self.item()
        self.assertEqual(item["status"], "pending")
        self.assertTrue(item["record"]["held_before"])
        self.assertTrue(notify.format_post(1, item["record"]).splitlines()[0].endswith("（前回 保留）"))
        detail = notify.format_detail(1, item["record"], fulltext.load(self.out, item["key"]))
        self.assertIn("最初のメッセージ: リリース日を決めてください", detail)
        self.assertIn("続きのメッセージ: 資料を共有しました", detail)
        self.assertLess(detail.index("最初のメッセージ:"), detail.index("続きのメッセージ:"))

    def test_the_first_request_is_kept_at_120_characters_and_the_held_mark_is_dropped_after_ok_or_ng(self):
        long_first = "リリース日を決めてください。" + "詳しい事情は後で説明します。" * 20
        for answer, word in (("OK 1", "approved"), ("NG 1", "rejected")):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as d:
                self.out, self.bridge = Path(d), FakeBridge()
                self.ask_pm(long_first)
                notify.collect(self.out, FakeBridge(["保留 1"]), send=False)
                self.process(self.ev(2, "資料を共有しました"), CONFIDENT_FYI)
                first = self.item()["record"]["first_message"]["text"]
                self.assertLessEqual(len(first), 120)
                self.assertTrue(first.startswith("リリース日を決めてください"))
                self.assertTrue(self.item()["record"]["held_before"])
                notify.notify(self.out, self.bridge, send=True)
                notify.collect(self.out, FakeBridge([answer]), send=False)
                self.assertEqual(self.item()["status"], word)
                self.assertNotIn("held_before", self.item()["record"])


if __name__ == "__main__":
    unittest.main()
