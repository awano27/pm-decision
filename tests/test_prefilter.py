"""T10: the sieve before the judge (kimeru/prefilter.py). prefilter=0 only records the rule that hit (a new field, only on a hit);
prefilter=1 does not ask the judge for a hit. The graph's severe and critical rules come first, Teams text is never sieved
(only the sender), and the digest shows the hits by rule as numbers. No network, no real Teams."""
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

from kimeru import cli, config, prefilter, stats
from kimeru.backends import StubBackend
from tests.test_daily import GRAPHS, PBS

NOW = datetime(2026, 10, 12, 3, 0, tzinfo=timezone.utc)


class Spy(StubBackend):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def ask(self, state, questions):
        self.calls += 1
        return super().ask(state, questions)


def settings(**kv):
    return mock.patch.dict(os.environ, {f"KIMERU_{k.upper()}": v for k, v in kv.items()})


def chat(eid, text, author="Sato"):
    return {"kind": "teams.chat", "source": "teams", "id": eid, "ts": "2026-10-12T03:00:00+00:00", "chat_id": None,
            "author": author, "mentions_me": False, "text": text}


def ado(eid, **kw):
    ev = {"kind": "ado.workitem.created", "source": "azure-devops", "id": eid, "ts": "2026-10-12T03:00:00+00:00", "type": "Bug",
          "title": "表示が崩れる", "area": "Web", "created_by": "Sato", "priority": 2, "severity": "3 - Medium",
          "description": "", "repro_steps": "", "acceptance_criteria": "", "origin": None}
    ev.update(kw)
    return ev


def minutes(eid, item):
    return {"kind": "meeting.item", "source": "minutes", "id": eid, "ts": "2026-10-12", "meeting": "定例", "index": 0, "item": item}


def alert(eid, severity="Sev1"):
    return {"kind": "monitor.alert", "source": "azure-monitor", "id": eid, "ts": "2026-10-12T03:00:00Z", "rule": "cpu",
            "severity": severity, "description": "CPU high", "resource": "/subscriptions/x/vm1", "condition": "Fired",
            "status": "Fired"}


def run(ev, **kv):
    spy = Spy()
    with tempfile.TemporaryDirectory() as d, settings(**kv):
        [res] = cli.process(copy.deepcopy(ev), GRAPHS, spy, Path(d), PBS, now=NOW)
        row = json.loads((Path(d) / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    return res, row, spy


def core(res):
    return {k: res[k] for k in ("graph", "outcome", "node", "path", "advice", "actions", "needs_human", "notify")}


HITS = [("bot", chat("c1", "ビルド完了", "AlertBot")), ("ado_template", ado("5")), ("minutes_none", minutes("m1", "特になし"))]
NAMES = {"bot_names": "AlertBot"}


class TestDefaultKeepsTheJudgment(unittest.TestCase):
    def test_the_settings_are_off_and_empty_by_default(self):
        self.assertEqual(config.value("prefilter"), "0")
        self.assertEqual(config.value("bot_names"), "")

    def test_a_hit_at_0_changes_nothing_but_the_new_field(self):
        for rule, ev in HITS:
            with self.subTest(rule=rule):
                base, base_row, base_spy = run(ev)   # bot_names empty: the bot rule does not exist; the others hit
                res, row, spy = run(ev, prefilter="0", **NAMES)
                self.assertEqual(core(res), core(base))
                self.assertEqual(spy.calls, base_spy.calls)
                self.assertGreater(spy.calls, 0)
                self.assertEqual(row["prefilter"], {"rule": rule, "applied": False})
                if rule == "bot":
                    self.assertNotIn("prefilter", base_row)

    def test_an_event_no_rule_names_has_no_new_field(self):
        for ev in (chat("c2", "ビルド完了", "Sato"), ado("6", description="ログインできない"), minutes("m2", "来週リリース"), alert("a1")):
            res, row, _ = run(ev, prefilter="1", **NAMES)
            self.assertNotIn("prefilter", row)
            self.assertNotIn("prefilter", res)


class TestAtOneTheJudgeIsNotAsked(unittest.TestCase):
    def test_a_hit_is_recorded_without_a_question(self):
        for rule, ev in HITS:
            with self.subTest(rule=rule):
                res, row, spy = run(ev, prefilter="1", **NAMES)
                self.assertEqual(spy.calls, 0)
                self.assertEqual(row["prefilter"], {"rule": rule, "applied": True})

    def test_the_recorded_nodes(self):
        self.assertEqual(run(HITS[0][1], prefilter="1", **NAMES)[0]["node"], "log_fyi")
        self.assertEqual(run(HITS[2][1], prefilter="1", **NAMES)[0]["node"], "log_info")
        self.assertEqual(run(ado("5"), prefilter="1")[0]["node"], "request_info")
        self.assertEqual(run(ado("5", type="User Story"), prefilter="1")[0]["node"], "request_info_ac")
        self.assertEqual(run(ado("5", type="Test Case"), prefilter="1")[0]["node"], "request_info_steps")
        self.assertTrue(run(ado("5"), prefilter="1")[0]["needs_human"])

    def test_a_planning_item_and_a_filled_item_are_not_a_template(self):
        self.assertNotIn("prefilter", run(ado("7", type="Epic"), prefilter="1")[1])
        self.assertNotIn("prefilter", run(ado("8", repro_steps="1. 開く"), prefilter="1")[1])

    def test_the_none_words(self):
        for item in ("特になし", "なし", "該当なし", "特になし。"):
            self.assertEqual(prefilter.rule_of(minutes("m", item)), "minutes_none", item)
        for item in ("特になしだが来週確認", "なしでも可", "リリース"):
            self.assertIsNone(prefilter.rule_of(minutes("m", item)), item)


class TestSeriousItemsAreNeverSieved(unittest.TestCase):
    def test_a_bot_post_about_a_production_outage_is_judged(self):
        ev = chat("c3", "本番でログインできなくなっています", "AlertBot")
        for v in ("0", "1"):
            res, row, spy = run(ev, prefilter=v, **NAMES)
            self.assertNotIn("prefilter", row)
            self.assertEqual(res["node"], "incident_bug")

    def test_a_priority_1_or_critical_ado_template_is_judged(self):
        for ev in (ado("9", priority=1), ado("10", title="本番障害: 決済が落ちている")):
            res, row, spy = run(ev, prefilter="1")
            self.assertNotIn("prefilter", row)
            self.assertTrue(res["needs_human"] or res["notify"])

    def test_teams_text_is_never_sieved_only_the_sender(self):
        for text in ("なし", "特になし", "ありがとうございます"):
            res, row, _ = run(chat("c4", text, "Sato"), prefilter="1", **NAMES)
            self.assertNotIn("prefilter", row)

    def test_an_alert_is_untouched(self):
        for sev in ("Sev0", "Sev1", "Sev3"):
            self.assertNotIn("prefilter", run(alert("a2", sev), prefilter="1", **NAMES)[1])


class TestTheDigestCountsPerRule(unittest.TestCase):
    def test_numbers_by_rule_only_when_something_hit(self):
        with tempfile.TemporaryDirectory() as d, settings(**NAMES):
            out = Path(d)
            for i, (rule, ev) in enumerate(HITS + [HITS[0]]):
                cli.process(copy.deepcopy({**ev, "id": f"{ev['id']}-{i}"}), GRAPHS, Spy(), out, PBS)
            text = stats.render(stats.collect(out, now=datetime.now(timezone.utc)))
            self.assertIn("ふるいに当たった件（規則別）: ado_template 1 件、bot 2 件、minutes_none 1 件", text)
        with tempfile.TemporaryDirectory() as d:
            cli.process(chat("c9", "hi"), GRAPHS, Spy(), Path(d), PBS)
            self.assertNotIn("ふるい", stats.render(stats.collect(Path(d), now=datetime.now(timezone.utc))))


if __name__ == "__main__":
    unittest.main()
