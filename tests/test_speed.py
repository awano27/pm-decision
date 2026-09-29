"""Measuring, ordering, posting as soon as ready, a time budget, fewer questions, one-call batches. No model is called."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import cli, config, daily, events, graph, plan
from kimeru.backends import StubBackend
from tests.test_daily import FakeTeams
from tests.test_plan import EV, NODE, Scripted

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)
ONE = {k: v[0] for k, v in GRAPHS.items()}


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

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def drop(self, name, ev):
        (self.inbox / name).write_text(json.dumps(ev, ensure_ascii=False), encoding="utf-8")

    def chat(self, i, text="これは何ですか"):
        return {"kind": "teams.chat", "id": str(i), "chat_id": f"19:c{i}", "author": "X", "text": text, "mentions_me": True}


class TestMeasuring(Base):
    def test_a_decision_records_calls_questions_and_seconds(self):
        cli.process(self.chat(1, "リリース日を来週火曜にずらしてよいか判断お願いします"), GRAPHS, StubBackend(), self.out, PBS)
        rec = json.loads((self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
        p = rec["perf"]
        self.assertGreaterEqual(p["calls"], 1)
        self.assertGreaterEqual(p["questions"], p["calls"])
        self.assertIsInstance(p["judge_sec"], float)
        self.assertIsInstance(p["writer_sec"], float)

    def test_the_cycle_log_holds_the_totals_and_digest_shows_median_and_max(self):
        self.drop("a.json", self.chat(1))
        self.drop("b.json", self.chat(2, "あれは何ですか"))
        r = daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=FakeTeams(), send=True, toaster=lambda a, b: None)
        self.assertEqual(r["perf"]["events"], 2)
        self.assertGreaterEqual(r["perf"]["questions"], 2)
        log = [json.loads(l) for l in (self.out / "daily.log.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn("perf", next(x for x in log if x["step"] == "cycle")["report"])
        from kimeru import stats
        line = stats.perf_line([json.loads(l) for l in (self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()])
        self.assertIn("中央値", line)
        self.assertIn("最大", line)


class TestOrder(Base):
    def test_the_most_urgent_kind_is_judged_first_and_the_order_can_be_changed(self):
        kinds = {"a-ado.json": "ado.workitem.created", "b-minutes.json": "meeting.item", "c-chat.json": "teams.chat",
                 "d-alert.json": "monitor.alert"}
        for name, kind in kinds.items():
            self.drop(name, {"kind": kind, "id": name})
        seen = []

        def process(payload, *a, **k):
            seen.append(payload["kind"] if isinstance(payload, dict) else payload[0]["kind"])
            return []

        daily.process_inbox(self.inbox, self.out, GRAPHS, StubBackend(), PBS, process)
        self.assertEqual(seen, ["monitor.alert", "teams.chat", "ado.workitem.created", "meeting.item"])
        for name in kinds:
            (self.inbox / "done" / name).replace(self.inbox / name)
        seen.clear()
        os.environ["KIMERU_PRIORITY"] = "meeting.item,ado.workitem.created"
        config.apply([])
        daily.process_inbox(self.inbox, self.out, GRAPHS, StubBackend(), PBS, process)
        self.assertEqual(seen[:2], ["meeting.item", "ado.workitem.created"])


class TestPostAsSoonAsReady(Base):
    def test_a_ready_item_is_posted_before_the_next_file_is_judged(self):
        self.drop("1.json", self.chat(1))
        self.drop("2.json", self.chat(2, "あれは何ですか"))
        t = FakeTeams()
        order = []
        real_post = t.post
        t.post = lambda text, send: (order.append("post"), real_post(text, send))[1]

        def process(payload, *a, **k):
            order.append("judge")
            return cli.process(payload, *a, **k)

        daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, process, bridge=t, send=True, toaster=lambda a, b: None)
        first_post = order.index("post")
        self.assertEqual(order[:first_post], ["judge"])          # one file judged, then its post ...
        self.assertEqual(order.count("judge"), 2)
        self.assertLess(first_post, len(order) - order[::-1].index("judge"))   # ... before the last file was judged

    def test_the_cycle_report_still_lists_what_was_posted(self):
        self.drop("1.json", self.chat(1))
        r = daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=FakeTeams(), send=True, toaster=lambda a, b: None)
        self.assertEqual(len(r["notify"]), 1)


class TestBudget(Base):
    def test_files_over_the_budget_stay_and_are_judged_next_time_once(self):
        self.drop("1.json", self.chat(1))
        self.drop("2.json", self.chat(2, "あれは何ですか"))
        ticks = iter([0.0, 0.1, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0])      # the deadline, then one check per file
        with mock.patch("time.monotonic", lambda: next(ticks)):
            r = daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=FakeTeams(), send=False, budget=1)
        self.assertEqual(r["perf"]["left_for_next_cycle"], 1)
        self.assertEqual(r["waiting"], 1)                           # the second file is still in the inbox
        r2 = daily.cycle(self.out, self.inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=FakeTeams(), send=False)
        self.assertEqual(r2["waiting"], 0)
        decided = [json.loads(l) for l in (self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted(d["event_id"] for d in decided), ["1", "2"])    # each event exactly once


class TestFewerQuestions(unittest.TestCase):
    ANSWERS = {"first": {"choice": "blocker_eta", "confidence": 0.9}, "need_blocker_eta": {"noul": 0.9}, "need_dependencies": {"noul": 0.1},
               "need_notify": {"noul": 0.1}}

    def build(self, lean):
        b = Scripted({"choice": "schedule_change", "confidence": 0.95}, dict(self.ANSWERS))
        with mock.patch.dict(os.environ, {"KIMERU_PLAN_LEAN": "1" if lean else "0"}):
            edge, p, _ = plan.build(NODE, EV, b, PBS)
        return edge, p, sum(len(q) for q in b.calls), len(b.calls)

    def test_a_dropped_step_is_not_asked_about_its_deadline_and_the_plan_is_the_same(self):
        e0, p0, q0, c0 = self.build(lean=False)
        e1, p1, q1, c1 = self.build(lean=True)
        self.assertEqual((e0, p0), (e1, p1))          # the same plan, deadlines included
        self.assertLess(q1, q0)

    def test_when_every_conditional_step_is_dropped_there_is_no_third_call(self):
        b = Scripted({"choice": "schedule_change", "confidence": 0.95}, {k: v for k, v in self.ANSWERS.items()} | {"need_blocker_eta": {"noul": 0.1}})
        with mock.patch.dict(os.environ, {"KIMERU_PLAN_LEAN": "1"}):
            plan.build(NODE, EV, b, PBS)
        self.assertLessEqual(len(b.calls), 2 + 1)


class TestBatch(unittest.TestCase):
    def run_all(self, batch):
        res = {}
        for l in (ROOT / "eval" / "fixtures.jsonl").read_text(encoding="utf-8").splitlines():
            fx = json.loads(l)
            m = cli.Meter(StubBackend())
            r = graph.run(ONE[fx["event"]["kind"]], fx["event"], m, playbooks=PBS, batch=batch)
            res[fx["id"]] = (json.dumps([r["node"], r["actions"]], sort_keys=True, ensure_ascii=False), m.calls, m.questions)
        return res

    def test_batching_reaches_the_same_final_action_with_fewer_calls(self):
        one, many = self.run_all(False), self.run_all(True)
        self.assertTrue(all(one[k][0] == many[k][0] for k in one))
        self.assertLess(sum(v[1] for v in many.values()), sum(v[1] for v in one.values()))

    def test_a_route_decided_by_rules_alone_asks_nothing_in_either_mode(self):
        one, many = self.run_all(False), self.run_all(True)
        zero = [k for k, v in one.items() if v[2] == 0]
        self.assertTrue(zero)
        self.assertTrue(all(many[k][2] == 0 for k in zero))

    def test_batching_is_off_by_default_and_the_setting_turns_it_on(self):
        ev = {"kind": "teams.chat", "id": "1", "text": "リリース日を来週火曜にずらしてよいか判断お願いします"}

        class Rec(StubBackend):
            def __init__(self):
                self.sizes = []

            def ask(self, state, questions):
                self.sizes.append(len(questions))
                return super().ask(state, questions)

        b = Rec()
        graph.run(ONE["teams.chat"], ev, b, playbooks=PBS)
        self.assertEqual(b.sizes[0], 1)                       # off by default: one question per call
        with mock.patch.dict(os.environ, {"KIMERU_BATCH": "1"}):
            b2 = Rec()
            graph.run(ONE["teams.chat"], ev, b2, playbooks=PBS)
        self.assertGreater(b2.sizes[0], 1)                    # on: the graph's judge questions go together


if __name__ == "__main__":
    unittest.main()
