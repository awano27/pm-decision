"""Review, week digest, sharing, calibration: local data only, the model is never called."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru import calibrate, cli, config, graph, notify, plan, profiles, review, stats
from kimeru.backends import StubBackend

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)

# strings that must never appear in anything meant to be shared
MARK_TEXT, MARK_NAME, MARK_ID, MARK_PATH = "本文ZZTEXT", "人名ZZNAME", "ID-ZZID-99", "ZZPATH"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name) / "state"
        self.out = Path(self.dir.name) / ("out-" + MARK_PATH)
        self.state.mkdir()
        self.out.mkdir()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = str(self.state)
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        config.apply([])

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def decide(self, events, backend=None):
        gs = GRAPHS
        payload = events if isinstance(events, list) else [events]
        res = []
        for ev in payload:      # process() takes one payload at a time (a normalized event is its own payload)
            res += cli.process(ev, gs, backend or StubBackend(), self.out, PBS, dedup=True)
        return res

    def chat(self, text, author="Sato", i="1"):
        return {"kind": "teams.chat", "id": i, "author": author, "text": text, "mentions_me": True}


class TestRecords(Base):
    def test_a_decision_keeps_its_event_the_judge_and_the_plan_answers(self):
        res = self.decide(self.chat("リリース日を来週火曜にずらしてよいか判断お願いします。QAが止まっていて至急です"))[0]
        rec = json.loads((self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["event"]["text"][:8], "リリース日を来週")
        self.assertEqual(rec["judge"]["name"], "StubBackend")
        plan_step = next(s for s in rec["path"] if s["node"] == "plan_decision")
        self.assertIn("answers", plan_step["answer"])                    # need_ / due_ / first are kept
        self.assertTrue(any(k.startswith("due_") for k in plan_step["answer"]["answers"]))
        self.assertIn("first", plan_step["answer"]["answers"])

    def test_approval_records_have_a_time_and_the_key_and_old_lines_are_still_read(self):
        (self.out / "approvals.log.jsonl").write_text(json.dumps({"id": 3, "status": "approved"}) + "\n", encoding="utf-8")   # an old line
        s = stats.collect(self.out)
        self.assertEqual(s["approvals_undated"], 1)
        # a new one, written by collect(): at + key
        ap = notify.Approvals(self.out)
        n = ap.add("g:e:n", {"graph": "g"})
        ap.data["items"][str(n)]["posted"] = True
        ap.save()

        class Bridge:
            def read(self):
                return {"timeline": [f"P:{n}", f"R:OK {n}"], "replies": [f"OK {n}"]}

        notify.collect(self.out, Bridge())
        last = json.loads((self.out / "approvals.log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertTrue(last["at"])
        self.assertEqual(last["key"], "g:e:n")


class TestReview(Base):
    def run_review(self, answers):
        it = iter(answers)
        lines = []
        gs = GRAPHS
        tally = review.run(self.out, gs, PBS, input_fn=lambda prompt: next(it), print_fn=lines.append)
        return tally, "\n".join(lines)

    def test_right_and_wrong_answers_become_fixture_rows_outside_the_repository(self):
        self.decide([self.chat("リリース日を来週火曜にずらしてよいか判断お願いします。QAが止まっていて至急です", i="1"),
                     self.chat("ログイン画面の改修、今どこまで進んでますか？", i="2")])
        tally, _ = self.run_review(["y", "n", "1", "1"])         # #1 right; #2 wrong at its first question, option 1
        self.assertEqual((tally["yes"], tally["no"]), (1, 1))
        rows = review.read_jsonl(review.fixtures_path())
        self.assertEqual(len(rows), 2)
        for r in rows:                                            # the shape of eval/fixtures.jsonl
            self.assertEqual(set(r), {"id", "event", "expect"})
        self.assertTrue(str(review.fixtures_path()).startswith(str(self.state)))
        self.assertFalse(str(review.fixtures_path()).startswith(str(ROOT)))
        self.assertEqual(len(review.read_jsonl(review.reviews_path())), 2)
        # reviewed decisions are not asked again
        self.assertEqual(self.run_review([])[0], {"yes": 0, "no": 0, "unknown": 0})

    def test_unknown_and_quit(self):
        self.decide([self.chat("これは何ですか", i="1"), self.chat("あれは何ですか", i="2")])
        tally, _ = self.run_review(["s", "q"])
        self.assertEqual(tally["unknown"], 1)
        self.assertEqual(review.read_jsonl(review.fixtures_path()), [])

    def test_old_decisions_without_an_event_are_reported_not_asked(self):
        (self.out / "decisions.jsonl").write_text(json.dumps({"graph": "g", "event_id": "1", "node": "n", "path": []}) + "\n", encoding="utf-8")
        _, text = self.run_review([])
        self.assertIn("古い判断", text)


class TestShare(Base):
    def seed(self, judge):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rec = {"graph": "teams-chat-triage", "event_id": MARK_ID, "event_kind": "teams.chat", "node": "decision_today", "outcome": "decide",
               "needs_human": False, "notify": True, "at": now, "summary": MARK_TEXT + MARK_NAME, "advice": MARK_TEXT,
               "event": {"kind": "teams.chat", "id": MARK_ID, "author": MARK_NAME, "text": MARK_TEXT},
               "path": [{"node": "intent", "answer": {"choice": "decision", "confidence": 0.9}, "edge": "decision"}],
               "judge": {"name": judge, "model": "m-1"}}
        (self.out / "decisions.jsonl").write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
        (self.out / "approvals.log.jsonl").write_text(json.dumps({"at": now, "key": MARK_ID, "id": 1, "status": "approved"}) + "\n", encoding="utf-8")
        review._append(review.reviews_path(), {"key": f"teams-chat-triage:{MARK_ID}:decision_today", "verdict": "yes", "at": now})

    def test_share_carries_no_text_name_id_or_path(self):
        self.seed("Kev")
        text = stats.week(self.out, share=True)
        for mark in (MARK_TEXT, MARK_NAME, MARK_ID, MARK_PATH, str(self.dir.name), "Users"):
            self.assertNotIn(mark, text)
        self.assertIn("判断: 1 件", text)
        self.assertIn("OK 1", text)
        self.assertIn("判断の一致率: 100%", text)
        self.assertIn("kimeru 1.0.0", text)

    def test_with_jev_the_agreement_rate_is_not_shown_and_the_reason_is(self):
        self.seed("Jev")
        text = stats.week(self.out, share=True)
        self.assertNotIn("判断の一致率: 100%", text)
        self.assertIn("Jev", text)
        self.assertIn("利用規約", text)

    def test_the_cli_prints_it(self):
        self.seed("Kev")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--out", str(self.out), "digest", "--week", "--share"]), 0)
        self.assertNotIn(MARK_TEXT, buf.getvalue())


def question(key, label, conf, choice="a"):
    node = {"kind": "judge", "question": {"type": "choice", "instructions": "x", "criteria": {"a": "A", "b": "B"}},
            "routes": {"a": "n1", "b": "n2", "unsure": "n3"}}
    return {"key": key, "node": node, "node_id": "q", "ans": {"choice": choice, "confidence": conf}, "label": label, "judge": "Kev"}


class TestCalibrate(Base):
    CURRENT = {"conf_scale": 1.0, "noul_scale": 0.95}

    def data(self, n=120):
        qs = []
        for i in range(n):
            if i % 4 == 0:      # confident and wrong, but only just above the threshold
                qs.append(question(f"k{i}", "b", 0.65))
            else:               # confident and right
                qs.append(question(f"k{i}", "a", 0.95))
        return qs

    def test_too_few_questions_propose_nothing_and_say_what_is_missing(self):
        prop, lines = calibrate.propose(self.data(30), self.CURRENT)
        self.assertIsNone(prop)
        self.assertIn("足りません", lines[0])
        self.assertIn("30", lines[0])

    def test_a_more_careful_change_is_proposed_when_the_other_half_agrees(self):
        prop, lines = calibrate.propose(self.data(), self.CURRENT)
        self.assertIsNotNone(prop, lines)
        self.assertGreaterEqual(prop["conf_scale"], 1.0)                    # never wider by default
        self.assertGreaterEqual(prop["noul_scale"], 0.95)
        self.assertTrue(any("確かめた側" in l for l in lines))

    def test_a_change_that_adds_confident_errors_on_the_confirming_half_is_not_proposed(self):
        qs = self.data()
        # every question of the confirming half is only right when the threshold stays low: a higher one turns them into
        # "human", never into errors; to force errors use wrong answers that a stricter threshold cannot hide
        real_half = calibrate._half
        with mock.patch.object(calibrate, "_score", side_effect=lambda q, cs, ns: (
                {"ok": 0, "wrong": 5 if (cs, ns) != (1.0, 0.95) and q and calibrate._half(q[0]) == 1 else 0, "human": 0}
                if q and real_half(q[0]) == 1 else
                {"ok": 0, "wrong": 9 if (cs, ns) == (1.0, 0.95) else 0, "human": 0})):
            prop, lines = calibrate.propose(qs, self.CURRENT)
        self.assertIsNone(prop)
        self.assertTrue(any("増える" in l for l in lines))

    def test_widening_needs_the_flag(self):
        qs = [question(f"w{i}", "a", 0.55) for i in range(120)]           # all right, all held back by the threshold
        prop, _ = calibrate.propose(qs, self.CURRENT)
        self.assertIsNone(prop)                                            # by default nothing wider is proposed
        wide, _ = calibrate.propose(qs, self.CURRENT, allow_wider=True)
        self.assertIsNotNone(wide)
        self.assertLess(wide["conf_scale"], 1.0)

    def test_apply_writes_a_local_file_and_leaves_graph_keys_alone(self):
        keys_before = {g["name"]: cli._graph_version(g) for lst in GRAPHS.values() for g in lst}
        text_before = (ROOT / "graphs" / "teams_chat.json").read_bytes()
        calibrate.apply("kev", {"conf_scale": 1.2, "noul_scale": 0.97})
        self.assertTrue(profiles.override_path().exists())
        self.assertTrue(str(profiles.override_path()).startswith(str(self.state)))
        self.assertEqual({g["name"]: cli._graph_version(g) for lst in graph.load_dir(ROOT / "graphs", PBS).values() for g in lst}, keys_before)
        self.assertEqual((ROOT / "graphs" / "teams_chat.json").read_bytes(), text_before)
        self.assertTrue(calibrate.revert())
        self.assertFalse(profiles.override_path().exists())

    def test_the_override_reaches_backends_wrapped_by_timed_and_recording(self):
        from kimeru import demo
        spec = importlib.util.spec_from_file_location("kimeru_e2e", ROOT / "eval" / "e2e.py")
        e2e = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(e2e)

        class Kev:
            profile = profiles.PROFILES["kev"]

        node = {"kind": "judge", "question": {"type": "choice", "instructions": "x", "criteria": {"a": "A", "b": "B"}},
                "routes": {"a": "n1", "b": "n2", "unsure": "n3"}}
        ans = {"choice": "a", "confidence": 0.7}
        for wrapper in (lambda b: b, demo.Timed, e2e.Recording):
            edge, _ = graph.route(node, ans, wrapper(Kev()).profile)
            self.assertEqual(edge, "a")                                    # 0.7 >= 0.6: decided
        calibrate.apply("kev", {"conf_scale": 1.5, "noul_scale": 0.95})    # now 0.9 is needed
        for wrapper in (lambda b: b, demo.Timed, e2e.Recording):
            edge, _ = graph.route(node, ans, wrapper(Kev()).profile)
            self.assertEqual(edge, "unsure")

    def test_calibrate_without_data_changes_nothing(self):
        lines = []
        self.assertIsNone(calibrate.run(self.out, GRAPHS, print_fn=lines.append))
        self.assertIn("足りません", lines[0])
        self.assertFalse(profiles.override_path().exists())


class TestThresholdChecks(unittest.TestCase):
    def graph_with(self, **node_extra):
        return {"name": "t", "event": "x.y", "start": "q", "nodes": {
            "q": {"kind": "judge", "question": {"type": "noul", "instructions": "x"}, "routes": {"yes": "a", "no": "b", "unsure": "c"}, **node_extra},
            "a": {"kind": "decide", "actions": [{"type": "log.only"}]}, "b": {"kind": "decide", "actions": [{"type": "log.only"}]}, "c": {"kind": "decide", "actions": [{"type": "log.only"}]}}}

    def test_yes_at_below_no_at_is_refused(self):
        with self.assertRaises(graph.GraphError):
            graph.validate(self.graph_with(yes_at=0.3, no_at=0.7))

    def test_out_of_range_thresholds_are_refused(self):
        for k in ("yes_at", "no_at", "split_at", "need_at"):
            with self.assertRaises(graph.GraphError, msg=k):
                graph.validate(self.graph_with(**{k: 1.5}))

    def test_a_sane_graph_passes(self):
        graph.validate(self.graph_with(yes_at=0.8, no_at=0.2))


if __name__ == "__main__":
    unittest.main()
