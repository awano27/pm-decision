try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kimeru import brief, events, graph, notify, plan, pull, writer
from kimeru.backends import ReplayBackend

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = {k: v[0] for k, v in graph.load_dir(ROOT / "graphs", PBS).items()}
ADO = GRAPHS["ado.workitem.created"]
ALERT = GRAPHS["monitor.alert"]
MEETING = GRAPHS["meeting.item"]
NOW = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)

STEPS_XML = ('<steps id="0" last="3"><step id="2" type="ActionStep"><parameterizedString isformatted="true">'
             '&lt;DIV&gt;&lt;P&gt;Open the login page&lt;/P&gt;&lt;/DIV&gt;</parameterizedString>'
             '<parameterizedString isformatted="true">&lt;P&gt;The form is shown&lt;/P&gt;</parameterizedString><description/></step>'
             '<step id="3" type="ActionStep"><parameterizedString/><parameterizedString/></step></steps>')


def item(typ="Bug", title="t", desc="", repro="", ac="", steps="", prio=2, severity=None):
    f = {"System.Id": 7, "System.WorkItemType": typ, "System.Title": title, "System.Description": desc,
         "Microsoft.VSTS.TCM.ReproSteps": repro, "Microsoft.VSTS.Common.AcceptanceCriteria": ac,
         "Microsoft.VSTS.TCM.Steps": steps, "System.CreatedBy": {"displayName": "Aoki"}, "System.AreaPath": "P\\Web",
         "System.CreatedDate": "2026-10-08T01:00:00Z", "Microsoft.VSTS.Common.Priority": prio}
    if severity:
        f["Microsoft.VSTS.Common.Severity"] = severity
    return events.normalize({"eventType": "workitem.created", "resource": {"id": 7, "fields": f}})[0]


def answers(ready=None, score=None):
    a = {}
    if ready is not None:
        a["ready"] = {"noul": ready}
    if score is not None:
        a["priority"] = {"score": score, "confidence": 0.9, "probabilities": {}}
    return ReplayBackend(a)   # a question that is not listed raises KeyError: the graph must not ask it


class TestAdoTypes(unittest.TestCase):
    # (case, event, backend, node, text/advice fragment, notify)
    TABLE = [
        ("Epic is recorded, no question", item("Epic"), answers(), "record_only", "記録のみ", False),
        ("Feature is recorded, no question", item("Feature", desc="Some long plan"), answers(), "record_only", "記録のみ", False),
        ("Epic shouted as production down is still not a bug", item("Epic", title="本番でログインできない"), answers(), "record_only", "記録のみ", False),
        ("priority-1 Feature still reaches the PM", item("Feature", prio=1), answers(), "record_only", "記録のみ", True),
        ("Task without criteria asks for criteria", item("Task"), answers(ready=0.05), "request_info_ac", "受け入れ条件", False),
        ("User Story without criteria", item("User Story"), answers(ready=0.05), "request_info_ac", "受け入れ条件", False),
        ("Bug without steps asks for steps and expected", item("Bug"), answers(ready=0.05), "request_info", "再現手順と期待結果", False),
        ("Test Case without steps asks for steps", item("Test Case"), answers(ready=0.05), "request_info_steps", "テスト手順", False),
        ("no type keeps the generic request", item(None), answers(ready=0.05), "request_info_ac", "受け入れ条件", False),
        ("Test Case with steps is ready", item("Test Case", steps=STEPS_XML), answers(score=1.0), "set_p2", None, False),
        ("Task with criteria is ready", item("Task", ac="ログインできること"), answers(score=0.2), "set_p3", None, False),
    ]

    def test_table(self):
        for case, ev, backend, node, frag, notify_ in self.TABLE:
            r = graph.run(ADO, ev, backend, playbooks=PBS)
            self.assertEqual(r["node"], node, case)
            self.assertEqual(r["notify"], notify_, case)
            if frag:
                self.assertIn(frag, json.dumps(r["actions"] + [r["advice"]], ensure_ascii=False), case)

    def test_requests_are_queued_comments_with_tags(self):
        for node in ("request_info", "request_info_ac", "request_info_steps"):
            n = ADO["nodes"][node]
            self.assertTrue(n["queue"])
            self.assertEqual([a["type"] for a in n["actions"]], ["ado.comment", "ado.update"])

    def test_critical_rule_eligibility_is_unchanged(self):
        crit = ADO["nodes"]["critical_bug"]
        for typ, want in (("Bug", "yes"), (None, "yes"), ("Epic", "no"), ("Task", "no"), ("Feature", "no"), ("Test Case", "no")):
            self.assertEqual(graph.match_eval(crit, item(typ, title="本番でログインできない"))[0], want, typ)

    def test_test_steps_are_read_from_the_xml(self):
        self.assertEqual(events._test_steps(STEPS_XML), "1. Open the login page -> The form is shown")
        self.assertEqual(item("Test Case", steps=STEPS_XML)["test_steps"], "1. Open the login page -> The form is shown")
        for empty in ("", None, '<steps id="0" last="1"><step id="2"><parameterizedString/><parameterizedString/></step></steps>'):
            self.assertEqual(events._test_steps(empty), "")
        self.assertEqual(events._test_steps("<not xml"), "<not xml")

    def test_steps_are_fetched(self):
        self.assertIn("Microsoft.VSTS.TCM.Steps", pull.ADO_FIELDS)


class TestWriterInputs(unittest.TestCase):
    def test_ado_event_keys_reach_the_writer(self):
        ev = item("Bug", title="ログイン不可", desc="d", repro="r" * 900, ac="受け入れ: x")
        res = {"path": [], "node": "request_info", "actions": [{"type": "ado.comment", "id": "7", "text": "固定文"}]}
        text = writer._material(res, ev)
        for needle in ('"type": "Bug"', '"created_by": "Aoki"', '"area": "P\\\\Web"', '"acceptance_criteria": "受け入れ: x"'):
            self.assertIn(needle, text)
        self.assertNotIn("work_item_type", text)
        self.assertEqual(text.count("r" * 600), 1)   # repro steps are cut at 600
        self.assertNotIn("r" * 601, text)

    def test_request_for_a_person_names_the_kind_and_the_author(self):
        ev = item("Test Case", steps=STEPS_XML)
        res = {"path": [], "node": "n", "advice": "a", "actions": [{"type": "ado.comment", "text": "x"}]}
        req = writer.human_request(res, ev)
        self.assertIn("種別: Test Case", req)
        self.assertIn("起票者: Aoki", req)
        self.assertIn("テスト手順（書かれている分）: 1. Open the login page", req)


IMPACT = {"score": 0.2, "confidence": 0.9, "probabilities": {}}


class TestViaUnsure(unittest.TestCase):
    ALERT_EV = {"kind": "monitor.alert", "id": "a1", "rule": "disk", "severity": "Sev3", "condition": "Fired", "description": "d"}
    MEET_EV = {"kind": "meeting.item", "id": "m1", "meeting": "定例", "item": "見積を見直す"}

    def test_table(self):
        unsure = {"noul": 0.5}
        table = [   # (case, graph, event, answers, node, via_unsure)
            ("noise_check unsure -> task", ALERT, self.ALERT_EV, {"future_risk": {"noul": 0.1}, "impact": IMPACT, "noise_check": unsure}, "task_internal", ["noise_check"]),
            ("noise_check sure", ALERT, self.ALERT_EV, {"future_risk": {"noul": 0.1}, "impact": IMPACT, "noise_check": {"noul": 0.05}}, "task_internal", None),
            ("has_owner unsure", MEETING, self.MEET_EV, {"item_type": {"choice": "action", "confidence": 0.9}, "has_owner": unsure}, "ask_owner", ["has_owner"]),
            ("has_due unsure", MEETING, self.MEET_EV, {"item_type": {"choice": "action", "confidence": 0.9}, "has_owner": {"noul": 0.95}, "has_due": unsure}, "ask_owner", ["has_due"]),
            ("item_type unsure goes to the PM (not silent)", MEETING, self.MEET_EV, {"item_type": {"choice": "action", "confidence": 0.1}}, "check_line", ["item_type"]),
        ]
        for case, g, ev, ans, node, via in table:
            r = graph.run(g, ev, ReplayBackend(ans), playbooks=PBS)
            self.assertEqual(r["node"], node, case)
            self.assertEqual(r.get("via_unsure"), via, case)

    def test_severe_alert_keeps_its_guard_and_notice(self):
        ev = {**self.ALERT_EV, "severity": "Sev1"}
        r = graph.run(ALERT, ev, ReplayBackend({"future_risk": {"noul": 0.1}, "impact": IMPACT, "noise_check": {"noul": 0.5}}), playbooks=PBS)
        self.assertTrue(r["notify"] or r["needs_human"])

    def test_ado_chain_through_ready_unsure(self):
        r = graph.run(ADO, item("Bug"), answers(ready=0.5, score=0.2), playbooks=PBS)
        self.assertEqual((r["node"], r["via_unsure"]), ("set_p3", ["ready"]))


def rec(eid, kind, node, summary, at, via=None, **kw):
    r = {"graph": "g", "event_id": eid, "event_kind": kind, "node": node, "outcome": "decide", "needs_human": False,
         "notify": False, "summary": summary, "at": at.isoformat(), "actions": [], "event": {"type": kw.pop("type", None)}}
    if via:
        r["via_unsure"] = via
    r.update(kw)
    return r


def write(d, rows):
    (Path(d) / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


class TestUnsureSection(unittest.TestCase):
    def test_lines_limit_and_exclusions(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [rec(f"m{i}", "meeting.item", "ask_owner", f"定例: 項目{i}", NOW - timedelta(hours=i), ["has_owner"]) for i in range(7)]
            rows += [rec("n1", "teams.chat", "ask_pm", "x", NOW, ["intent"], needs_human=True),           # a person was asked
                     rec("n2", "monitor.alert", "page", "y", NOW, ["plan_incident"], notify=True),       # a notice went out
                     rec("n3", "meeting.item", "ask_owner", "sure one", NOW),                            # not unsure
                     rec("old", "meeting.item", "ask_owner", "定例: 古い", NOW - timedelta(days=3), ["has_owner"])]
            write(d, rows)
            text = brief.unsure_section(d, now=NOW)
            lines = text.split("\n")
            self.assertEqual(lines[0], "迷って自動で決めた件（確かめてください）")
            self.assertEqual(len(lines), 1 + 5 + 1)
            self.assertEqual(lines[-1], "ほか 2 件")
            self.assertIn("議事録", lines[1])
            self.assertIn("担当・期限を確認する扱いにした", lines[1])
            self.assertNotIn("sure one", text)
            for word in ("unsure", "has_owner", "ask_owner", "via_"):
                self.assertNotIn(word, text)

    def test_ado_priority_decided_through_unsure_is_listed_once(self):
        with tempfile.TemporaryDirectory() as d:
            act = [{"type": "ado.update", "fields": {"Microsoft.VSTS.Common.Priority": 3}}]
            write(d, [rec("7", "ado.workitem.created", "set_p3", "#7 ヘルプの誤字", NOW, ["ready"], actions=act, type="Bug")])
            self.assertIn("ADO #7 ヘルプの誤字 → P3 にした", brief.unsure_section(d, now=NOW))
            self.assertEqual(brief.ado_section(d, now=NOW), "")
            self.assertEqual(brief.teams_section(d, now=NOW), "")

    def test_none(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, [rec("m", "meeting.item", "ask_owner", "s", NOW)])
            self.assertEqual(brief.unsure_section(d, now=NOW), "")

    def test_build_puts_it_in_the_post(self):
        from kimeru.backends import StubBackend
        with tempfile.TemporaryDirectory() as d:
            write(d, [rec("m1", "meeting.item", "risk_item", "定例: 遅れの懸念", NOW, ["plan_risk"])])
            text, _ = brief.build(d, StubBackend(), now=NOW)
            self.assertIn("迷って自動で決めた件（確かめてください）\n・議事録 定例: 遅れの懸念 → リスクとして記録した", text)


class TestEpicInBrief(unittest.TestCase):
    def test_record_only_is_listed_in_the_ado_section(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, [rec("9", "ado.workitem.created", "record_only", "#9 大きな計画", NOW, type="Epic",
                          actions=[{"type": "log.only"}], title="大きな計画")])
            text = brief.ado_section(d, now=NOW)
            self.assertIn("記録のみ 1 件", text)
            self.assertIn("[Epic]", text)


class TestSinceLastBrief(unittest.TestCase):
    def rows(self):
        a = lambda h, eid, node="set_p3": rec(eid, "ado.workitem.created", node, f"#{eid} t", NOW - timedelta(hours=h),
                                              actions=[{"type": "ado.update", "fields": {"Microsoft.VSTS.Common.Priority": 3}}], title="t")
        t = lambda h, eid: rec(eid, "teams.chat", "log_fyi", "a: b", NOW - timedelta(hours=h), event={"chat_title": f"chat{eid}"})
        return [a(40, "old"), a(30, "mid"), a(10, "new"), t(30, "tmid"), t(2, "tnew")]

    def test_table(self):
        # (last brief hours ago, ado ids expected, teams chats expected)
        table = [(None, {"new"}, {"chattnew"}),            # unknown: the last 24 hours
                 (26, {"new"}, {"chattnew"}),                      # the brief came 26 h ago: items from 30 h ago were in it already
                 (36, {"mid", "new"}, {"chattmid", "chattnew"}),  # the hour shifted: what a 24 h window would miss is counted
                 (5, set(), {"chattnew"}),                  # a brief 5 h ago: nothing since for ADO, one chat
                 ("garbage", {"new"}, {"chattnew"}),
                 (-3, {"new"}, {"chattnew"})]              # a time in the future is not trusted
        with tempfile.TemporaryDirectory() as d:
            write(d, self.rows())
            for hours, ado_ids, chats in table:
                since = None if hours is None else (hours if isinstance(hours, str) else (NOW - timedelta(hours=hours)).isoformat())
                found = {r["id"] for r in brief.ado_auto(d, now=NOW, since=since)}
                self.assertEqual(found, ado_ids, hours)
                text = brief.teams_section(d, now=NOW, since=since)
                for c in ("chattmid", "chattnew"):
                    self.assertEqual(c in text, c in chats, (hours, c))

    def test_header_says_which_window(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, self.rows())
            self.assertIn("直近 24 時間", brief.ado_section(d, now=NOW))
            self.assertIn("前回のまとめ以降", brief.ado_section(d, now=NOW, since=(NOW - timedelta(hours=36)).isoformat()))

    def test_the_window_is_capped_at_a_week(self):
        start, known = brief.window_start(NOW, 1, NOW - timedelta(days=30))
        self.assertEqual((start, known), (NOW - timedelta(days=7), True))

    def test_delivery_stores_the_build_time(self):
        class Sent:
            def post(self, text, send):
                return {"ok": True, "typed": True, "sent": True, "readback": {"matched": True, "message_id": "m1"}}
        from tests import test_delivery_attempts as t   # the same delivered-result shape as the delivery tests
        with tempfile.TemporaryDirectory() as d:
            class Delivered:
                def post(self, text, send):
                    return t.sent_result()
            at = "2026-10-10T07:59:00+00:00"
            self.assertEqual(notify.deliver_brief(d, Delivered(), "b", True, "2026-10-10", built_at=at)["status"], "delivered")
            state = json.loads((Path(d) / "daily_state.json").read_text(encoding="utf-8"))
            self.assertEqual((state["brief_date"], state["brief_at"]), ("2026-10-10", at))

    def test_confirmed_unknown_delivery_keeps_the_build_time(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "daily_state.json").write_text(json.dumps({"brief_delivery_unknown": {
                "date": "2026-10-10", "at": "2026-10-10T08:01:00+00:00", "built_at": "2026-10-10T07:59:00+00:00", "text": "b"}}), encoding="utf-8")
            self.assertEqual(notify.confirm_delivery(d, "brief", "brief", confirm_delivered=True)["status"], "confirmed")
            state = json.loads((Path(d) / "daily_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["brief_at"], "2026-10-10T07:59:00+00:00")


class TestDailyUsesTheLastBrief(unittest.TestCase):
    def test_the_brief_stores_its_time_and_the_next_one_counts_from_it(self):
        from kimeru import daily
        from kimeru.backends import StubBackend
        from kimeru.cli import process
        from tests.test_daily import FakeTeams
        seen = []
        real = brief.build

        def spy(out, backend, **kw):
            seen.append(kw.get("since"))
            return real(out, backend, **kw)
        with tempfile.TemporaryDirectory() as d:
            out, inbox = Path(d) / "out", Path(d) / "inbox"
            brief.build = spy
            try:
                daily.cycle(out, inbox, GRAPHS, StubBackend(), PBS, process, bridge=FakeTeams(), send=True, now=datetime(2026, 9, 28, 8, 10))
                state = json.loads((out / "daily_state.json").read_text(encoding="utf-8"))
                self.assertEqual(seen, [None])                       # first brief: unknown, so the last 24 hours
                self.assertEqual(datetime.fromisoformat(state["brief_at"]).tzinfo is not None, True)
                (out / "daily_state.json").write_text(json.dumps({**state, "brief_date": "2026-09-27"}), encoding="utf-8")
                daily.cycle(out, inbox, GRAPHS, StubBackend(), PBS, process, bridge=FakeTeams(), send=True, now=datetime(2026, 9, 28, 9, 30))
                self.assertEqual(seen[1], state["brief_at"])         # the next one counts from the stored time
            finally:
                brief.build = real


if __name__ == "__main__":
    unittest.main()
