"""Evaluation and tuning: no Jev numbers, nothing personal in what is shared, tuning that applies where it can, speed measured
honestly. Local data and fakes only: no judge, Teams or network is used."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru import calibrate, cli, config, demo, fulltext, graph, profiles, review, stats
from kimeru.backends import StubBackend, is_jev, judge_name
from tests.test_daily import FakeTeams
from tests.test_review_stats import GRAPHS, MARK_NAME, MARK_PATH, PBS, ROOT, Base


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeJev(StubBackend):
    NAME = "Jev"


class FakeKev(StubBackend):
    NAME = "Kev"


def read_jsonl(p):
    return review.read_jsonl(p)


class Reviewing(Base):
    """Decisions made by a named judge, and reviews of them through the real `kimeru review` loop."""

    def make(self, backend, i, out=None, days_ago=0, text="リリース日を来週火曜にずらしてよいか判断お願いします。QAが止まっていて至急です"):
        out = out or self.out
        old = self.out
        self.out = out
        try:
            self.decide(self.chat(text, i=str(i)), backend)
        finally:
            self.out = old
        if days_ago:   # the decision is older than the week the digest looks at
            rows = read_jsonl(out / "decisions.jsonl")
            rows[-1]["at"] = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
            (out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def review_all(self, out, answers):
        it = iter(answers)
        lines = []
        tally = review.run(out, GRAPHS, PBS, input_fn=lambda prompt: next(it), print_fn=lines.append)
        return tally, "\n".join(lines)


class TestNoJevNumbers(Reviewing):
    def test_a_review_names_the_judge_that_made_the_decision(self):
        self.make(FakeJev(), 1)
        self.review_all(self.out, ["y"])
        self.assertEqual(read_jsonl(review.reviews_path())[0]["judge"], "Jev")

    def test_a_jev_decision_reviewed_today_stays_out_of_the_rate_even_when_the_latest_judge_is_kev(self):
        self.make(FakeJev(), 1, days_ago=10)        # ten days ago: outside the week, so the "recent judges" are only Kev
        self.make(FakeKev(), 2)
        tally, _ = self.review_all(self.out, ["y", "y"])
        self.assertEqual(tally["yes"], 2)
        s = stats.collect(self.out)
        self.assertEqual(s["judges"], ["Kev"])
        self.assertEqual((s["reviewed_yes"], s["reviewed_no"]), (1, 0))      # only Kev's
        self.assertEqual(s["reviewed_left_out"], 1)
        text = stats.render(s)
        self.assertIn("判断の一致率: 100%（確かめた 1 件", text)
        self.assertNotIn("2 件", text.split("判断の一致率")[1])

    def test_a_jev_decision_from_another_output_folder_does_not_count(self):
        other = Path(self.dir.name) / "other-out"
        other.mkdir()
        self.make(FakeJev(), 1, out=other)
        self.review_all(other, ["y"])                                        # reviews.jsonl is shared by every folder
        self.make(FakeKev(), 2)
        self.review_all(self.out, ["y"])
        s = stats.collect(self.out)
        self.assertEqual((s["reviewed_yes"], s["reviewed_no"]), (1, 0))

    def test_an_old_review_without_a_judge_that_cannot_be_traced_is_not_counted(self):
        self.make(FakeKev(), 1)
        review._append(review.reviews_path(), {"key": "elsewhere:9:node", "verdict": "yes", "at": datetime.now(timezone.utc).isoformat()})
        s = stats.collect(self.out)
        self.assertEqual((s["reviewed_yes"], s["reviewed_left_out"]), (0, 1))

    def test_an_old_review_without_a_judge_is_traced_through_this_folders_decisions(self):
        self.make(FakeKev(), 1)
        rec = read_jsonl(self.out / "decisions.jsonl")[0]
        review._append(review.reviews_path(), {"key": review.decision_key(rec), "verdict": "yes", "at": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(stats.collect(self.out)["reviewed_yes"], 1)

    def test_the_end_of_a_review_shows_no_breakdown_when_jev_decisions_were_reviewed(self):
        self.make(FakeJev(), 1)
        _, text = self.review_all(self.out, ["y"])
        self.assertIn("内訳は出しません", text)
        self.assertNotIn("合っている 1", text)

    def test_the_end_of_a_review_keeps_its_breakdown_for_other_judges(self):
        self.make(FakeKev(), 1)
        _, text = self.review_all(self.out, ["y"])
        self.assertIn("合っている 1", text)

    def test_the_week_digest_shows_no_agreement_rate_for_jev_alone(self):
        self.make(FakeJev(), 1)
        self.review_all(self.out, ["y"])
        text = stats.week(self.out, share=True)
        self.assertNotIn("判断の一致率: 100%", text)
        self.assertNotIn("件のうち", text)

    def test_the_plain_digest_shows_no_time_for_jev(self):
        self.make(FakeJev(), 1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.digest(self.out)
        self.assertNotIn("時間", buf.getvalue())

    def test_the_demo_and_the_daily_cycle_show_no_seconds_for_jev(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            demo.run(ROOT / "examples" / "demo_day.json", GRAPHS, FakeJev(), PBS, cli.process, self.out, pace=0)
        self.assertNotIn("秒", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            demo.run(ROOT / "examples" / "demo_day.json", GRAPHS, StubBackend(), PBS, cli.process, self.out, pace=0)
        self.assertIn("秒", buf.getvalue())               # the offline judge still shows it
        from kimeru import daily
        inbox = Path(self.dir.name) / "inbox"
        inbox.mkdir()
        (inbox / "a.json").write_text(json.dumps(self.chat("これは何ですか", i="7"), ensure_ascii=False), encoding="utf-8")
        r = daily.cycle(self.out, inbox, GRAPHS, FakeJev(), PBS, cli.process, bridge=FakeTeams(), send=False)
        self.assertNotIn("judge_sec", r["perf"])
        self.assertIn("calls", r["perf"])

    def test_the_judge_is_seen_through_wrappers(self):
        self.assertEqual(judge_name(cli.Meter(demo.Timed(FakeJev()))), "Jev")
        self.assertTrue(is_jev(demo.Timed(FakeJev())))
        self.assertFalse(is_jev(StubBackend()))

    def test_measure_speed_does_not_accept_jev(self):
        ms = load("kimeru_measure_speed", "eval/measure_speed.py")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            ms.main(["--backend", "jev"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("jev", err.getvalue())


class TestCalibrateByJudge(Reviewing):
    def data(self, judge, n=120):
        out = []
        for i in range(n):
            wrong = i % 4 == 0
            node = {"kind": "judge", "question": {"type": "choice", "instructions": "x", "criteria": {"a": "A", "b": "B"}},
                    "routes": {"a": "n1", "b": "n2", "unsure": "n3"}}
            out.append({"key": f"{judge}{i}", "node": node, "node_id": "q", "ans": {"choice": "a", "confidence": 0.65 if wrong else 0.95},
                        "label": "b" if wrong else "a", "judge": judge})
        return out

    def test_a_record_from_the_stub_is_not_applied_and_says_why(self):
        self.make(StubBackend(), 1)
        self.review_all(self.out, ["y"])
        lines = []
        self.assertIsNone(calibrate.run(self.out, GRAPHS, min_questions=1, do_apply=True, print_fn=lines.append))
        self.assertFalse(profiles.override_path().exists())
        text = "\n".join(lines)
        self.assertIn("適用しませんでした", text)
        self.assertIn("判断モデルが分からない", text)

    def test_the_unknown_judge_is_never_written_under_its_own_name(self):
        with self.assertRaises(ValueError):
            calibrate.apply("stubbackend", {"conf_scale": 1.2, "noul_scale": 1.0})
        with self.assertRaises(ValueError):
            calibrate.apply("stub", {"conf_scale": 1.2, "noul_scale": 1.0})
        self.assertFalse(profiles.override_path().exists())

    def test_a_mixed_record_is_searched_and_applied_judge_by_judge(self):
        mixed = self.data("Kev") + self.data("Jev", 10) + self.data("StubBackend", 5)
        lines = []
        with mock.patch.object(calibrate, "labeled_questions", return_value=mixed):
            res = calibrate.run(self.out, GRAPHS, do_apply=True, print_fn=lines.append)
        self.assertEqual(set(res), {"kev"})                                 # Jev has too few, the stub has no profile
        self.assertEqual(set(json.loads(profiles.override_path().read_text(encoding="utf-8"))), {"kev"})
        text = "\n".join(lines)
        self.assertIn("[Kev]", text)
        self.assertIn("[Jev]", text)
        self.assertIn("[StubBackend]", text)

    def test_jev_questions_alone_print_no_breakdown(self):
        lines = []
        with mock.patch.object(calibrate, "labeled_questions", return_value=self.data("Jev")):
            calibrate.run(self.out, GRAPHS, print_fn=lines.append)
        text = "\n".join(lines)
        self.assertIn("内訳は出しません", text)
        self.assertNotIn("確信して間違えた", text)


class TestSharing(Reviewing):
    def seed_graph(self, name):
        rec = {"graph": name, "event_id": "1", "event_kind": "teams.chat", "node": "n", "outcome": "decide", "needs_human": False,
               "notify": False, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "path": [], "judge": {"name": "Kev"}}
        with (self.out / "decisions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def test_a_graph_added_by_the_user_is_not_named_in_the_shared_output(self):
        self.seed_graph("teams-chat-triage")
        self.seed_graph("項目GRAPHZZ-プロジェクト名")
        self.seed_graph("別のGRAPHYY")
        shared = stats.week(self.out, share=True)
        self.assertNotIn("GRAPHZZ", shared)
        self.assertNotIn("GRAPHYY", shared)
        self.assertIn("追加のグラフ 1", shared)
        self.assertIn("追加のグラフ 2", shared)
        self.assertIn("teams-chat-triage", shared)                          # a graph that ships with kimeru keeps its name
        self.assertIn("GRAPHZZ", stats.week(self.out))                      # what is shown to the user alone is not changed

    def test_config_show_share_carries_no_path_organization_subscription_or_address(self):
        env = {"KIMERU_ADO_ORG": "ORGZZ-org", "KIMERU_ADO_PROJECT": "PROJZZ", "KIMERU_SUBSCRIPTION": "SUBZZ-0000",
               "KIMERU_AZ": r"C:\Users\ZZUSER\tools\az.cmd", "KIMERU_KEV_URL": "http://kev-host-ZZ:8009/v1",
               "KIMERU_SELF_MARKER": "SELFZZ", "KIMERU_WRITER_MODEL": "MODELZZ", "KIMERU_PUSH": "outlook",
               "KIMERU_PUSH_WEBHOOK_BODY": '{"k":"BODYZZ"}', "KIMERU_BRIEF_HOUR": "9"}
        with mock.patch.dict(os.environ, env):
            config.apply([])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli.main(["--out", str(self.out), "config", "show", "--share"]), 0)
        text = buf.getvalue()
        for mark in ("ORGZZ", "PROJZZ", "SUBZZ", "ZZUSER", "kev-host-ZZ", "SELFZZ", "MODELZZ", "BODYZZ", "Users", str(self.state),
                     MARK_PATH, "config file:"):
            self.assertNotIn(mark, text)
        self.assertIn("ado_org", text)
        self.assertIn("(set)", text)                                         # it says the setting is set, not to what
        self.assertIn("brief_hour", text)
        self.assertRegex(text, r"brief_hour\s+9\s")                          # a harmless value is kept
        self.assertRegex(text, r"push\s+outlook\s")
        self.assertIn("kimeru 1.0.0", text.splitlines()[0])

    def test_config_show_without_share_is_unchanged(self):
        with mock.patch.dict(os.environ, {"KIMERU_ADO_ORG": "ORGZZ-org"}):
            config.apply([])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main(["config", "show"])
        self.assertIn("ORGZZ-org", buf.getvalue())

    def test_share_only_goes_with_show(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["config", "path", "--share"]), 2)

    def test_the_display_scale_is_read_from_what_windows_applied_not_from_this_process(self):
        try:
            import winreg  # noqa: F401
        except ImportError:
            self.skipTest("Windows only")
        with mock.patch("winreg.OpenKey"), mock.patch("winreg.QueryValueEx", return_value=(144, 4)):
            self.assertEqual(stats._dpi_percent(), 150)
        with mock.patch("winreg.OpenKey"), mock.patch("winreg.QueryValueEx", return_value=(96, 4)):
            self.assertEqual(stats._dpi_percent(), 100)
        with mock.patch("winreg.OpenKey", side_effect=OSError), \
                mock.patch.object(stats.subprocess, "run", side_effect=OSError):
            self.assertIsNone(stats._dpi_percent())                          # unknown is left out, not shown as 100%


class TestWhatTheRecordKeeps(Reviewing):
    LONG = "長い依頼です。" + "詳細の説明が続きます。" * 40

    def test_by_default_the_recorded_text_is_the_length_of_a_summary(self):
        self.decide(self.chat(self.LONG))
        rec = read_jsonl(self.out / "decisions.jsonl")[0]
        self.assertLessEqual(len(rec["event"]["text"]), cli.EVENT_TEXT_SUMMARY)
        self.assertTrue(rec["event"]["text"].endswith("…"))
        self.assertEqual(rec["event"]["author"], "Sato")                     # what the records need to find the chat stays

    def test_the_whole_event_is_kept_only_when_the_setting_is_on(self):
        with mock.patch.dict(os.environ, {"KIMERU_RECORD_EVENT_FULL": "1"}):
            config.apply([])
            self.decide(self.chat(self.LONG))
        rec = read_jsonl(self.out / "decisions.jsonl")[0]
        self.assertGreater(len(rec["event"]["text"]), cli.EVENT_TEXT_SUMMARY)
        self.assertLessEqual(len(rec["event"]["text"]), cli.EVENT_TEXT_FULL)

    def test_a_short_text_and_an_identifier_are_never_cut(self):
        ev = {**self.chat("短い"), "chat_id": "19:" + "x" * 150 + "@thread.v2"}
        self.decide(ev)
        rec = read_jsonl(self.out / "decisions.jsonl")[0]
        self.assertEqual(rec["event"]["chat_id"], ev["chat_id"])
        self.assertEqual(rec["event"]["text"], "短い")

    def test_a_score_answer_is_kept_as_a_range(self):
        node = {"kind": "judge", "question": {"type": "score", "instructions": "x", "criteria": ["a", "b", "c"]}}
        lo, hi = review.label_of(node, {"score": 2.4}, "top")
        self.assertEqual((lo, hi), (1.5, 2.5))
        evalmod = load("kimeru_run_eval", "eval/run_eval.py")
        g_node = {"kind": "judge", "question": node["question"], "routes": {"bands": [[1, "x"], [2, "y"], [3, "z"]], "unsure": "u"}}
        self.assertTrue(evalmod.judge(g_node, [lo, hi], {"score": 2.4, "confidence": 0.9}, None)[0])   # a decimal is not a miss


class TestWholeTextCost(Reviewing):
    def test_both_judgments_are_added_but_the_draft_is_written_once(self):
        long = "これは何ですか。" + "詳しく言うと、" * 60 + "以上です。"
        ev = {"kind": "teams.chat", "id": "1", "chat_id": "19:c1@thread.v2", "author": "A", "text": long[:60], "mentions_me": True}
        base = cli.process(ev, GRAPHS, StubBackend(), self.out / "base", PBS, dedup=True)[0]["perf"]       # one judgment, no reading
        teams = FakeTeams()
        teams.chat_messages = {"19:c1@thread.v2": [long]}
        real = cli._judge

        with mock.patch.dict(os.environ, {"KIMERU_READ_FULL": "1"}):
            config.apply([])
            with mock.patch.object(cli, "_judge", side_effect=real) as m, mock.patch.object(cli, "_draft", wraps=cli._draft) as d:
                cli.process(ev, GRAPHS, StubBackend(), self.out, PBS, dedup=True, reader=fulltext.Reader(teams))
        self.assertEqual((m.call_count, d.call_count), (2, 1))
        rec = read_jsonl(self.out / "decisions.jsonl")[0]
        self.assertEqual((rec["perf"]["calls"], rec["perf"]["questions"]), (2 * base["calls"], 2 * base["questions"]))


def synthetic():
    def dec(t):
        return {"kind": "decide", "actions": [{"type": "log.only"}], "advice": t}
    q = lambda: {"type": "noul", "instructions": "x"}
    routes = lambda: {"yes": "end", "no": "end", "unsure": "end"}
    return {"name": "t", "event": "x.y", "start": "m", "nodes": {
        "m": {"kind": "match", "fields": ["text"], "patterns": ["outage"], "routes": {"yes": "after_rule", "no": "other"}},
        "after_rule": {"kind": "judge", "question": q(), "routes": routes()},
        "other": {"kind": "judge", "question": q(), "routes": {"yes": "next", "no": "end", "unsure": "end"}},
        "next": {"kind": "judge", "question": q(), "routes": routes()},
        "end": dec("e")}}


class TestBatchAsksOnlyWhatIsReachable(unittest.TestCase):
    def ask_log(self, text):
        asked = []

        class Rec(StubBackend):
            def ask(self, state, questions):
                asked.append(sorted(questions))
                return super().ask(state, questions)

        g = synthetic()
        graph.validate(g)
        graph.run(g, {"kind": "x.y", "id": "1", "text": text}, Rec(), batch=True)
        return asked

    def test_a_question_on_a_branch_the_rules_closed_is_not_asked(self):
        self.assertEqual(self.ask_log("outage now")[0], ["after_rule"])     # the rule sent it here: `other` and `next` are closed
        self.assertEqual(self.ask_log("nothing")[0], ["next", "other"])     # the rule did not fire: `after_rule` is closed

    def test_the_shipped_graphs_ask_fewer_questions_in_batch_mode_than_all_of_them(self):
        for lst in GRAPHS.values():
            g = lst[0]
            allq = [k for k, v in g["nodes"].items() if v["kind"] == "judge"]
            self.assertLessEqual(len(graph.reachable_judges(g["nodes"], g["start"], {"text": "x"})), len(allq))


class TestEvalScripts(Reviewing):
    def answers(self, evalmod, path):
        rows = []
        for fx in evalmod.fixtures():
            qs = {nid: n["question"] for nid, n, _ in evalmod.labeled_nodes(fx) if n["kind"] == "judge"}
            if qs:
                from kimeru.events import state_of
                rows.append({"id": fx["id"], "answers": StubBackend().ask(state_of(fx["event"]), qs)})
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def run_tune(self, evalmod, path, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            evalmod.tune(path, **kw)
        return buf.getvalue()

    def test_the_tuning_result_does_not_change_with_the_coefficients_left_in_the_state_folder(self):
        evalmod = load("kimeru_run_eval2", "eval/run_eval.py")
        path = Path(self.dir.name) / "answers.jsonl"
        self.answers(evalmod, path)
        before = self.run_tune(evalmod, path)
        calibrate.apply("jev", {"conf_scale": 2.5, "noul_scale": 0.4})
        self.assertTrue(profiles.override_path().exists())
        self.assertEqual(self.run_tune(evalmod, path), before)                # not read by default
        with_user = self.run_tune(evalmod, path, use_user_thresholds=True)    # read on request, and it says so
        self.assertIn("利用者の係数の上書き", with_user)
        self.assertNotEqual(with_user.replace(profiles.overrides_note() + "\n", ""), before)

    def test_score_reads_a_fixtures_file_of_your_own(self):
        evalmod = load("kimeru_run_eval3", "eval/run_eval.py")
        self.make(FakeKev(), 1)
        self.review_all(self.out, ["y"])
        rows = read_jsonl(review.fixtures_path())
        self.assertTrue(rows)
        answers = Path(self.dir.name) / "a.jsonl"
        from kimeru.events import state_of
        g = evalmod.GRAPHS[rows[0]["event"]["kind"]]
        qs = {nid: g["nodes"][nid]["question"] for nid in rows[0]["expect"] if g["nodes"][nid]["kind"] == "judge"}
        answers.write_text(json.dumps({"id": rows[0]["id"], "answers": StubBackend().ask(state_of(rows[0]["event"]), qs)}) + "\n",
                           encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            evalmod.score(answers, None, review.fixtures_path())
        self.assertIn("scored", buf.getvalue())
        self.assertNotIn("scored 0 ", buf.getvalue())

    def test_measure_speed_stops_with_one_line_when_kev_is_not_up_or_not_on_this_pc(self):
        ms = load("kimeru_measure_speed2", "eval/measure_speed.py")

        def refuse(*a, **k):
            raise ConnectionRefusedError()

        line = ms.kev_problem("http://127.0.0.1:8009/v1", connect=refuse)
        self.assertIn("起動していません", line)
        self.assertNotIn("\n", line)
        self.assertIn("この PC ではありません", ms.kev_problem("http://kev.example.test:8009/v1", connect=refuse))
        ok = mock.MagicMock()
        self.assertIsNone(ms.kev_problem("http://127.0.0.1:8009/v1", connect=ok))
        # and main() prints that line and does not measure
        buf = io.StringIO()
        with mock.patch.object(ms, "kev_problem", return_value="一行"), contextlib.redirect_stdout(buf), \
                mock.patch.object(ms, "measure", side_effect=AssertionError("must not measure")):
            self.assertEqual(ms.main(["--backend", "kev"]), 1)
        self.assertEqual(buf.getvalue().strip(), "一行")

    def test_repeat_runs_every_fixture_that_many_times(self):
        ms = load("kimeru_measure_speed3", "eval/measure_speed.py")
        pbs = PBS
        gs = {k: v[0] for k, v in GRAPHS.items()}
        fx = [json.loads(l) for l in (ROOT / "eval" / "fixtures.jsonl").read_text(encoding="utf-8").splitlines()[:3]]
        one = ms.measure(StubBackend(), fx, gs, pbs, repeat=1)
        three = ms.measure(StubBackend(), fx, gs, pbs, repeat=3)
        self.assertEqual(len(three["lean"]["rows"]), 3 * len(one["lean"]["rows"]))

    def test_measure_speed_does_not_read_the_users_coefficients_by_default(self):
        ms = load("kimeru_measure_speed4", "eval/measure_speed.py")
        calibrate.apply("kev", {"conf_scale": 2.5, "noul_scale": 0.4})
        seen = []
        real = ms.measure
        with mock.patch.object(ms, "measure", side_effect=lambda *a, **k: seen.append(profiles.effective(profiles.PROFILES["kev"])) or real(*a, **k)), \
                contextlib.redirect_stdout(io.StringIO()):
            ms.main(["--n", "4"])
            ms.main(["--n", "4", "--user-thresholds"])
        self.assertEqual(seen[0]["conf_scale"], profiles.PROFILES["kev"]["conf_scale"])
        self.assertEqual(seen[1]["conf_scale"], 2.5)


if __name__ == "__main__":
    unittest.main()
