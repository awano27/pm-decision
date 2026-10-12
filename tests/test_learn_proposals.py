"""R21 (learn=1): one threshold proposal for one question, as an approval card. Only an OK (on a run with --send, from the daily
or approvals path) changes the local node entry; an NG changes nothing and is not asked again for 28 days. The graph files, the
processed records and the approval counts of the brief, work, stats and trial do not see the card as a case."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru import brief, calibrate, cli, config, graph, learn, notify, plan, profiles, stats, work
from kimeru.backends import ReplayBackend
from tests.test_notify import FakeBridge

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)
BY_NAME = {g["name"]: g for lst in GRAPHS.values() for g in lst}
INTENT = BY_NAME["teams-chat-triage"]["nodes"]["intent"]
NOW = datetime(2026, 10, 12, 9, 0)            # naive local, as the daily cycle's clock
ANSWER_HELD = {"choice": "fyi", "confidence": 0.65}
ANSWER_SURE = {"choice": "fyi", "confidence": 0.95}
ALERT = GRAPHS["monitor.alert"][0]
OUTAGE = {"kind": "monitor.alert", "id": "s0", "rule": "checkout-api 5xx rate", "severity": "Sev0", "condition": "Fired",
          "description": "5xx error rate 23% for 10 minutes; customers cannot complete checkout (service down)"}
STORAGE = {"kind": "monitor.alert", "id": "s1", "rule": "prod-db storage", "severity": "Sev1", "condition": "Fired",
           "description": "Storage 92% used on prod-db; at current growth it fills within 2 days"}


class FakeKev:
    NAME = "Kev"
    profile = profiles.PROFILES["kev"]


class FakeJev:
    NAME = "Jev"
    profile = profiles.PROFILES["jev"]


class RecordingKev(FakeKev):
    """A judge that records the subjects it is asked to rank (brief.rank sends them as state["items"])."""

    def __init__(self):
        self.asked = []

    def ask(self, state, qs):
        self.asked.append(list(state.get("items", [])))
        return {q: {"score": 0.0} for q in qs}


class Chat(FakeBridge):
    """A self chat that shows these lines (the timeline tools/teams-self.ps1 reads)."""

    def __init__(self, lines):
        super().__init__()
        self.lines = list(lines)

    def read(self):
        return {"ok": True, "timeline": list(self.lines)}


def row(i, judge="Kev", graph_name="teams-chat-triage", node_id="intent", wrong_every=4):
    """A labeled question of the shipped graph. Every fourth one is answered confidently and wrongly, at a confidence the
    default bar (0.6) accepts and a higher bar (0.72) holds back."""
    node = BY_NAME[graph_name]["nodes"][node_id]
    held = i % wrong_every == 0
    ans = {"choice": "status", "confidence": 0.65} if held else dict(ANSWER_SURE)
    return {"key": f"ev{i:02d}", "graph": graph_name, "node": node, "node_id": node_id, "ans": ans, "label": "fyi",
            "judge": judge}


def forty(judge="Kev"):
    return [row(i, judge) for i in range(40)]


def labels(rows):
    return mock.patch.object(calibrate, "labeled_questions", return_value=rows)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name) / "state"
        self.out = Path(self.dir.name) / "out"
        self.state.mkdir()
        self.out.mkdir()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean.update(KIMERU_STATE_DIR=str(self.state), KIMERU_LEARN="1")
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        config.apply([])

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def offer(self, rows=None, now=NOW, backend=None):
        with labels(forty() if rows is None else rows):
            return learn.offer(self.out, GRAPHS, backend or FakeKev(), now=now)

    def post_card(self, now=NOW):
        bridge = FakeBridge()
        notify.notify(self.out, bridge, send=True, real=True)
        return bridge.posts[-1][0] if bridge.posts else ""

    def card_number(self, text):
        return text.split("#", 1)[1].split("]", 1)[0]

    def answer(self, line, real=True, send=True, now=NOW):
        text = self.post_card()
        n = self.card_number(text) if text else "1"
        return notify.collect(self.out, Chat([f"P:{n}", f"R:{line} {n}"]), real=real, send=send, now=now)

    def add_waiting_case(self, number):
        """A real case, waiting for the PM, with the given number (the next card takes the number after it)."""
        ap = notify.Approvals(self.out)
        ap.data["items"][str(number)] = {"key": f"g:{number}:n", "status": "pending", "posted": True,
                                         "record": {"graph": "g", "event_id": str(number), "node": "n",
                                                    "advice": f"case {number}", "actions": []}}
        ap.data["next"] = number + 1
        ap.save()

    def thresholds(self):
        p = profiles.override_path()
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def ledger(self):
        p = learn.ledger_path()
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []

    def route_held(self, prof):
        return graph.route(INTENT, ANSWER_HELD, profiles.for_node(prof, BY_NAME["teams-chat-triage"], "intent"))[0]

    def graph_files(self):
        return {p.name: p.read_bytes() for p in (ROOT / "graphs").glob("*.json")}


class TestTheProposalForOneQuestion(unittest.TestCase):
    def test_forty_labels_with_confident_errors_on_both_halves_propose_one_more_careful_bar(self):
        rows = forty()
        halves = {calibrate._half(r) for r in rows if r["ans"]["confidence"] == 0.65}
        self.assertEqual(halves, {0, 1})                              # the test data reaches both halves
        prop = learn.propose_node(rows, INTENT, "teams-chat-triage:intent", "Kev",
                                  profiles.PROFILES["kev"], {"conf_scale": 1.0, "noul_scale": 1.0})
        self.assertIsNotNone(prop)
        self.assertEqual(prop["after"], {"conf_scale": 1.1, "noul_scale": 1.0})   # the smallest step that holds the errors back
        self.assertLess(prop["search"]["wrong"][1], prop["search"]["wrong"][0])
        self.assertEqual(prop["confirm"]["wrong"][1], 0)

    def test_fewer_than_thirty_labels_propose_nothing(self):
        self.assertIsNone(learn.propose_node(forty()[:29], INTENT, "teams-chat-triage:intent", "Kev",
                                             profiles.PROFILES["kev"], {"conf_scale": 1.0, "noul_scale": 1.0}))

    def test_a_change_that_adds_confident_errors_on_the_confirming_half_is_not_proposed(self):
        real_half = calibrate._half

        def fake_score(qs, cs, ns):
            on_confirm = bool(qs) and real_half(qs[0]) == 1
            if on_confirm:
                return {"ok": 0, "wrong": 0 if cs <= 1.0 else 9, "human": 0}
            return {"ok": 0, "wrong": 5 if cs <= 1.0 else 0, "human": 1}

        with mock.patch.object(calibrate, "_score", side_effect=fake_score):
            self.assertIsNone(learn.propose_node(forty(), INTENT, "teams-chat-triage:intent", "Kev",
                                                 profiles.PROFILES["kev"], {"conf_scale": 1.0, "noul_scale": 1.0}))

    def test_only_more_careful_changes_are_ever_proposed(self):
        # the wrong answers are at 0.8: decided at the current bar (1.2 x 0.6 = 0.72), held back by a higher one
        rows = []
        for r in forty():
            if r["ans"]["confidence"] == 0.65:
                r["ans"] = {"choice": "status", "confidence": 0.8}
            rows.append(r)
        cur = {"conf_scale": 1.2, "noul_scale": 1.05}
        prop = learn.propose_node(rows, INTENT, "teams-chat-triage:intent", "Kev", profiles.PROFILES["kev"], cur)
        self.assertIsNotNone(prop)                                    # the data does give a proposal from this bar
        self.assertEqual(prop["before"], cur)
        self.assertGreaterEqual(prop["after"]["conf_scale"], 1.2)
        self.assertGreaterEqual(prop["after"]["noul_scale"], 1.05)
        self.assertEqual(prop["after"], {"conf_scale": 1.5, "noul_scale": 1.05})



class TestTheCardAndItsAnswer(Base):
    def test_a_question_whose_unsure_does_not_go_straight_to_a_person_is_never_proposed(self):
        rows = [row(i, graph_name="workitem-intake", node_id="ready") for i in range(40)]   # ready: unsure goes to another judge
        self.assertEqual(self.offer(rows=rows), {"state": "nothing"})

    def test_a_card_is_queued_once_a_week_and_posted_with_what_ok_does_and_how_to_undo_it(self):
        res = self.offer()
        self.assertEqual(res, {"state": "queued", "ids": ["L1"]})
        text = self.post_card()
        self.assertTrue(text.startswith("[kimeru #1] 学習の提案: 「依頼の種類」の質問: 確信の基準を 1.0→1.1 倍に（探した側 "), text)
        self.assertIn("kimeru learn revert L1", text)
        self.assertTrue(text.endswith("OK 1 / NG 1 / 詳細 1"))
        self.assertLessEqual(len(text.splitlines()), 5)
        self.assertEqual(self.offer(now=NOW + timedelta(hours=1)), {"state": "this_week"})

    def test_ok_adopts_the_bar_of_that_one_question_and_changes_nothing_else(self):
        files = self.graph_files()
        self.offer()
        self.post_card()
        changes = self.answer("OK")
        self.assertIn("adopted", str(changes[0].get("learn")))
        self.assertEqual(self.thresholds()["nodes"], {"teams-chat-triage:intent": {"conf_scale": 1.1, "noul_scale": 1.0}})
        self.assertEqual(self.route_held(profiles.PROFILES["kev"]), "unsure")
        self.assertEqual(graph.route(INTENT, ANSWER_SURE, profiles.for_node(
            profiles.PROFILES["kev"], BY_NAME["teams-chat-triage"], "intent"))[0], "fyi")
        self.assertEqual(self.graph_files(), files)
        self.assertFalse((self.out / "processed.txt").exists())
        entry = self.ledger()[0]
        self.assertEqual((entry["id"], entry["kind"], entry["node"]), ("L1", "adopt", "teams-chat-triage:intent"))
        self.assertIn("counts", entry)                               # a Kev answer: its counts are kept in the ledger

    def test_ng_changes_nothing_and_the_same_question_is_not_asked_again_for_28_days(self):
        self.offer()
        self.post_card()
        self.answer("NG")
        self.assertFalse(profiles.override_path().exists())
        self.assertEqual(self.offer(now=NOW + timedelta(days=7)), {"state": "nothing"})
        self.assertEqual(self.offer(now=NOW + timedelta(days=14)), {"state": "nothing"})
        self.assertEqual(self.offer(now=NOW + timedelta(days=27)), {"state": "nothing"})
        again = self.offer(now=NOW + timedelta(days=29))
        self.assertEqual(again, {"state": "queued", "ids": ["L2"]})

    def test_a_card_that_waits_for_an_answer_blocks_the_next_card(self):
        self.offer()
        self.assertEqual(self.offer(now=NOW + timedelta(days=7)), {"state": "pending"})
        self.assertEqual(len(self.queue_rows()), 1)

    def test_without_real_or_without_send_an_ok_is_kept_and_adopted_on_the_next_real_send_run(self):
        self.offer()
        self.post_card()
        self.answer("OK", real=False, send=True)                     # demo, eval-like run: recorded, never adopted
        self.assertFalse(profiles.override_path().exists())
        self.answer("OK", real=True, send=False)                     # a cycle without --send: still not adopted
        self.assertFalse(profiles.override_path().exists())
        notify.collect(self.out, Chat([]), real=True, send=True, now=NOW)   # the next real run with --send adopts it
        self.assertEqual(self.thresholds()["nodes"]["teams-chat-triage:intent"]["conf_scale"], 1.1)

    def test_a_bar_that_changed_since_the_card_is_not_overwritten(self):
        self.offer()
        self.post_card()
        profiles.override_path().parent.mkdir(parents=True, exist_ok=True)
        profiles.override_path().write_text(json.dumps({"nodes": {"teams-chat-triage:intent": {"conf_scale": 1.3}}}), encoding="utf-8")
        changes = self.answer("OK")
        self.assertIn("stale", str(changes[0].get("learn")))
        self.assertEqual(self.thresholds()["nodes"]["teams-chat-triage:intent"]["conf_scale"], 1.3)

    def test_a_card_with_a_held_number_keeps_the_number_of_the_ledger_unique(self):
        profiles.override_path().parent.mkdir(parents=True, exist_ok=True)
        learn.ledger_path().write_text(json.dumps({"id": "L3", "kind": "adopt", "node": "x:y"}) + "\n", encoding="utf-8")
        self.assertEqual(self.offer(), {"state": "queued", "ids": ["L4"]})

    def test_the_card_posts_with_the_memory_promise_and_link_settings_on(self):
        with mock.patch.dict(os.environ, {"KIMERU_MEMORY": "1", "KIMERU_PROMISES": "1", "KIMERU_LINK_RELATED": "1"}):
            self.offer()
            text = self.post_card()
            self.answer("OK")
        self.assertTrue(text.startswith("[kimeru #1] 学習の提案"), text)
        self.assertEqual(self.thresholds()["nodes"]["teams-chat-triage:intent"]["conf_scale"], 1.1)

    def test_a_jev_card_and_its_ledger_show_no_counts(self):
        self.offer(backend=FakeJev(), rows=forty("Jev"))
        text = self.post_card()
        self.assertNotIn("探した側", text)
        self.assertNotIn("人に回る", text)
        self.answer("OK")
        self.assertNotIn("counts", self.ledger()[0])
        buf = io.StringIO()
        with redirect_stdout(buf):
            learn.dispatch(["list"], self.out, print_fn=print)
        self.assertNotIn("探した側", buf.getvalue())

    def test_no_card_is_made_for_a_judge_without_a_profile(self):
        class Stub:
            NAME = "Stub"
        self.assertEqual(self.offer(backend=Stub(), rows=forty()), {"state": "no_profile"})

    def test_learn_off_makes_no_card_and_reads_no_labels(self):
        with mock.patch.dict(os.environ, {"KIMERU_LEARN": "0"}), labels(forty()) as lab:
            self.assertEqual(learn.offer(self.out, GRAPHS, FakeKev(), now=NOW), {"state": "off"})
        lab.assert_not_called()

    def queue_rows(self):
        p = self.out / "queue.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


class TestTheCardIsNotACase(Base):
    def test_the_card_is_no_work_and_no_stats_approval_after_its_ok(self):
        self.offer()
        self.post_card()
        self.answer("OK")
        self.assertEqual(work.list_work(self.out), [])
        self.assertEqual(stats._approval_items(self.out), {})
        with self.assertRaises(work.WorkError):
            work.set_state(self.out, 1, "in_progress")

    def test_an_ok_on_the_card_is_no_approval_in_the_stats(self):
        self.offer()
        self.post_card()
        self.answer("OK")
        s = stats.collect(self.out, now=datetime.now(timezone.utc))   # the reply was logged at the real clock
        self.assertEqual(s["approvals"], {})
        self.assertEqual((s["week"]["replied_items"], s["week"]["replies"]), (0, 0))

    def test_an_ok_on_the_card_leaves_the_report_as_it_was_without_the_card(self):
        from kimeru import report
        self.add_waiting_case(2)
        before = report.build(self.out, GRAPHS)
        self.offer()
        self.post_card()                                              # the card is #3, beside the waiting case #2
        self.answer("OK")
        self.assertEqual(report.build(self.out, GRAPHS), before)

    def test_an_ok_on_the_card_is_no_reply_in_the_result_sheet(self):
        from kimeru import autorun
        self.offer()
        self.post_card()
        self.answer("OK")
        lines = autorun.build_report(self.out, now=datetime.now())
        reply = next(line for line in lines if line.startswith("replies"))
        self.assertTrue(reply.endswith("読み取った返信: なし"), reply)

    def test_an_ok_on_a_card_that_names_a_chat_is_no_earlier_entry_of_the_memory_index(self):
        from kimeru import memory
        # A card carries no event today; this pins the guard for the day one does: its reply must not become a chat's history.
        items = {"1": {"key": "kimeru.learn:L1:threshold", "status": "approved",
                       "record": {"graph": "kimeru.learn", "event_kind": "kimeru.learn", "event_id": "L1", "node": "threshold",
                                  "event": {"chat_id": "chat-1", "author": "someone"}}}}
        row = {"id": 1, "key": "kimeru.learn:L1:threshold", "status": "approved",
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        (self.out / "approvals.log.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        self.assertEqual(memory.index(self.out, items, now=datetime.now(timezone.utc)), {})


class TestTheBriefNeverRanksACard(Base):
    def test_a_posted_card_still_waiting_is_no_brief_item(self):
        self.offer()
        self.post_card()
        self.assertEqual(brief.collect(self.out, now=NOW.astimezone()), [])

    def test_a_card_queued_but_not_posted_is_no_brief_item(self):
        self.offer()
        self.assertEqual(brief.collect(self.out, now=NOW.astimezone()), [])

    def test_the_brief_judge_is_asked_about_the_real_cases_and_never_about_a_card(self):
        self.offer()
        self.post_card()
        self.add_waiting_case(2)
        judge = RecordingKev()
        text, ranked = brief.build(self.out, judge, now=NOW.astimezone())
        asked = [s for subjects in judge.asked for s in subjects]
        self.assertTrue(asked, "the judge must be asked about the real case, or the check below proves nothing")
        self.assertFalse(any("学習の提案" in s for s in asked), asked)
        self.assertFalse(any("学習の提案" in r["text"] for r in ranked))
        self.assertNotIn("学習の提案", text)

    def test_the_approval_log_keeps_the_card_but_the_reply_counts_leave_it_out(self):
        from kimeru import autorun
        self.offer()
        self.post_card()
        self.answer("OK")
        logged = [json.loads(l) for l in (self.out / "approvals.log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertTrue(any(str(r.get("key", "")).startswith("kimeru.learn:") for r in logged))
        start = datetime(2000, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(brief._replies(self.out, start)["approved"], 0)
        self.assertTrue(autorun._card_key("kimeru.learn:L1:threshold"))
        self.assertFalse(autorun._card_key("teams-chat-triage:t1:reply"))

    def test_the_card_is_never_a_case_of_the_requirements_draft(self):
        from kimeru import requirements
        self.offer()
        self.post_card()
        self.assertIsNone(requirements._case(self.out, 1))

    def test_after_ok_the_card_is_no_approved_work_and_no_trial_approval_beside_a_real_case(self):
        from kimeru import trial
        self.offer()
        self.post_card()
        self.answer("OK")
        ap = notify.Approvals(self.out)                               # one real approved case, #2, beside the card #1
        ap.data["items"]["2"] = {"key": "g:2:n", "status": "approved", "posted": True,
                                 "record": {"graph": "g", "event_id": "2", "node": "n", "advice": "case 2", "actions": []}}
        ap.data["next"] = 3
        ap.save()
        evidence = work.evidence(self.out)
        self.assertEqual((evidence["approved_cases"], evidence["unknown_progress_cases"]), (1, 1))
        data = trial.collect(self.out)
        self.assertEqual(data["approval_count"], 1)
        self.assertTrue(data["work_completion"].startswith("done 0 / tracked 0 / approved 1; progress Unknown for 1 untracked"),
                        data["work_completion"])
        lines = []
        self.assertEqual(trial.dispatch(["record", "--case", "1", "--after-minutes", "5", "--draft", "unused", "--outcome", "unknown"],
                                        self.out, print_fn=lines.append), 1)           # the card is not a trial case
        self.assertEqual(trial.dispatch(["record", "--case", "2", "--after-minutes", "5", "--draft", "unused", "--outcome", "unknown"],
                                        self.out, print_fn=lines.append), 0)           # a real case still is


class TestLearnCommand(Base):
    def test_list_shows_the_pending_card_and_the_ledger_and_revert_restores_the_bar(self):
        self.offer()
        self.post_card()
        self.answer("OK")
        buf = io.StringIO()
        with redirect_stdout(buf):
            learn.dispatch(["list"], self.out, print_fn=print)
        self.assertIn("L1", buf.getvalue())
        lines = []
        self.assertEqual(learn.dispatch(["revert", "L1"], self.out, print_fn=lines.append), 0)
        self.assertNotIn("teams-chat-triage:intent", self.thresholds().get("nodes", {}))
        self.assertEqual(self.route_held(profiles.PROFILES["kev"]), "fyi")
        self.assertEqual(self.ledger()[-1]["kind"], "revert")
        self.assertEqual(learn.dispatch(["revert", "L1"], self.out, print_fn=lines.append), 1)

    def test_a_broken_entry_is_listed_as_ignored_and_does_not_break_the_list(self):
        profiles.override_path().parent.mkdir(parents=True, exist_ok=True)
        profiles.override_path().write_text(json.dumps({"nodes": {"teams-chat-triage:intent": "x"}}), encoding="utf-8")
        lines = []
        self.assertEqual(learn.dispatch(["list"], self.out, print_fn=lines.append), 0)
        self.assertTrue(any("無視" in l for l in lines), lines)

    def test_a_nodes_value_that_is_not_an_object_is_listed_as_ignored_and_the_list_never_crashes(self):
        for broken in (5, True, [1, "a"], "abc", None, float("nan"), []):
            with self.subTest(nodes=broken):
                profiles.override_path().parent.mkdir(parents=True, exist_ok=True)
                profiles.override_path().write_text(json.dumps({"nodes": broken}), encoding="utf-8")
                lines = []
                self.assertEqual(learn.dispatch(["list"], self.out, print_fn=lines.append), 0)
                self.assertEqual(sum("無視している設定: nodes" in l for l in lines), 1, lines)

    def test_an_empty_nodes_object_lists_nothing_as_ignored(self):
        profiles.override_path().parent.mkdir(parents=True, exist_ok=True)
        profiles.override_path().write_text(json.dumps({"nodes": {}}), encoding="utf-8")
        lines = []
        self.assertEqual(learn.dispatch(["list"], self.out, print_fn=lines.append), 0)
        self.assertFalse(any("無視" in l for l in lines), lines)

    def test_an_older_adoption_is_not_taken_back_while_a_newer_one_holds_the_same_question(self):
        node = "teams-chat-triage:intent"
        rows = [{"id": "L5", "kind": "adopt", "node": node, "before": {"conf_scale": 1.0, "noul_scale": 1.0},
                 "after": {"conf_scale": 1.1, "noul_scale": 1.0}},
                {"id": "L6", "kind": "adopt", "node": node, "before": {"conf_scale": 1.1, "noul_scale": 1.0},
                 "after": {"conf_scale": 1.2, "noul_scale": 1.0}}]
        learn.ledger_path().write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        profiles.override_path().parent.mkdir(parents=True, exist_ok=True)
        profiles.override_path().write_text(json.dumps({"nodes": {node: {"conf_scale": 1.2, "noul_scale": 1.0}}}), encoding="utf-8")
        self.assertEqual(learn.revert(self.out, "L5"), "newer")       # refused: nothing changes, no ledger line
        self.assertEqual(self.thresholds()["nodes"][node]["conf_scale"], 1.2)
        self.assertEqual(self.ledger()[-1]["id"], "L6")
        lines = []
        self.assertEqual(learn.dispatch(["revert", "L5"], self.out, print_fn=lines.append), 1)
        self.assertIn("新しいもの", lines[0])
        self.assertEqual(learn.dispatch(["revert", "L6"], self.out, print_fn=lines.append), 0)
        self.assertEqual(self.thresholds()["nodes"][node]["conf_scale"], 1.1)
        self.assertEqual(learn.dispatch(["revert", "L5"], self.out, print_fn=lines.append), 0)
        self.assertNotIn(node, self.thresholds().get("nodes", {}))

    def test_the_command_line_runs_list_and_revert(self):
        self.offer()
        self.post_card()
        self.answer("OK")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["--out", str(self.out), "learn", "list"])
            revert_code = cli.main(["--out", str(self.out), "learn", "revert", "L1"])
        self.assertEqual(code, 0)
        self.assertIn("L1", buf.getvalue())
        self.assertEqual(revert_code, 0)
        self.assertNotIn("teams-chat-triage:intent", self.thresholds().get("nodes", {}))


class TestABrokenNodesValueNeverRaises(Base):
    """A broken value under "nodes" changes no route, never raises, and `kimeru learn list` says it is ignored."""
    NODE = "teams-chat-triage:intent"
    BIG = "1" + "0" * 400   # an int JSON reads exactly; float() of it raises OverflowError
    BROKEN_ENTRIES = ['"x"', "null", "true", "false", "3", "[1.2]", "NaN", "{}", '{"threshold": 1.2}', '{"conf_scale": true}',
                      '{"conf_scale": "1.2"}', '{"conf_scale": null}', '{"conf_scale": NaN}', '{"conf_scale": 3.5}',
                      '{"conf_scale": 0.05}', '{"noul_scale": -1}', '{"conf_scale": ' + BIG + "}"]
    BROKEN_NODES = ["5", "true", '[1, "a"]', '"abc"', "null", "NaN", "[]", '{"conf_scale": ' + BIG + "}", '{"conf_scale": 1.5}']

    def write(self, text):
        p = profiles.override_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        profiles._cache.update(path=None, mtime=None, data={})   # the global reader caches by mtime; a rewrite must be seen

    def listed(self):
        lines = []
        self.assertEqual(learn.dispatch(["list"], self.out, print_fn=lines.append), 0)
        return lines

    def test_a_broken_entry_changes_no_route_and_is_listed_as_ignored(self):
        kev = profiles.PROFILES["kev"]
        for broken in self.BROKEN_ENTRIES:
            with self.subTest(entry=broken[:24]):
                self.write('{"nodes": {"%s": %s}}' % (self.NODE, broken))
                self.assertEqual(profiles.node_overrides(), {})
                self.assertEqual(self.route_held(kev), "fyi")
                self.assertEqual(profiles.for_node(kev, BY_NAME["teams-chat-triage"], "intent")["name"], "kev")
                self.assertIn(f"無視している設定: {self.NODE}（値を読めないため、何も変えません）", self.listed())

    def test_a_broken_nodes_value_never_raises_in_the_routes_or_the_global_overrides(self):
        kev = profiles.PROFILES["kev"]
        for broken in self.BROKEN_NODES:
            with self.subTest(nodes=broken[:24]):
                self.write('{"nodes": %s}' % broken)
                self.assertEqual(self.route_held(kev), "fyi")
                self.assertEqual(profiles.overrides(), {})
                self.assertTrue(any(l.startswith("無視している設定:") for l in self.listed()))


class TestWhatTheDailyCycleDoesNotDo(Base):
    def cycle(self, real):
        from kimeru import daily
        with mock.patch.object(learn, "offer", return_value={"state": "queued"}) as offer, mock.patch.object(daily, "_log"):
            daily.cycle(self.out, self.out / "inbox", GRAPHS, FakeKev(), PBS, lambda *a, **k: [], bridge=FakeBridge(),
                        send=False, now=NOW, real=real, brief_hour=24)
        return offer

    def test_a_run_without_the_daily_path_never_offers_a_card(self):
        self.cycle(real=False).assert_not_called()

    def test_the_daily_path_offers_a_card_once_per_cycle(self):
        self.cycle(real=True).assert_called_once()

    def test_the_card_is_never_adopted_by_a_run_that_only_records(self):
        self.offer()
        self.post_card()
        notify.collect(self.out, Chat(["P:1", "R:OK 1"]), real=False, send=False, now=NOW)
        self.assertFalse(profiles.override_path().exists())


class TestSevereAlertsAreNotChangedByBars(Base):
    def test_a_sev0_alert_still_reaches_the_pm_when_every_entry_is_at_its_highest(self):
        profiles.override_path().parent.mkdir(parents=True, exist_ok=True)
        profiles.override_path().write_text(json.dumps({"nodes": {
            "alert-triage:impact": {"conf_scale": 3.0, "noul_scale": 3.0},
            "alert-triage:horizon": {"conf_scale": 3.0}}}), encoding="utf-8")
        r = graph.run(ALERT, OUTAGE, ReplayBackend({}))
        self.assertEqual(r["node"], "page")
        self.assertTrue(r["notify"])

    def test_a_sev0_the_impact_judge_is_unsure_of_at_a_held_bar_goes_to_the_pm(self):
        judge_sev0 = {"kind": "monitor.alert", "id": "s2", "rule": "api latency", "severity": "Sev0", "condition": "Fired",
                      "description": "p95 latency 4s on the search API for 10 minutes"}
        answers = {"future_risk": {"noul": 0.1}, "impact": {"score": 3.0, "confidence": 0.95}}
        without = graph.run(ALERT, judge_sev0, ReplayBackend(answers))
        self.assertNotEqual([t["edge"] for t in without["path"] if t["node"] == "impact"], ["unsure"])
        profiles.override_path().parent.mkdir(parents=True, exist_ok=True)
        profiles.override_path().write_text(json.dumps({"nodes": {"alert-triage:impact": {"conf_scale": 3.0, "noul_scale": 3.0}}}),
                                            encoding="utf-8")
        r = graph.run(ALERT, judge_sev0, ReplayBackend(answers))
        self.assertEqual([t["edge"] for t in r["path"] if t["node"] == "impact"], ["unsure"])
        self.assertEqual(r["node"], "page_advice")
        self.assertTrue(r["needs_human"] or r["notify"])

    def test_a_sev1_warning_still_reaches_the_pm_with_a_held_bar(self):
        profiles.override_path().parent.mkdir(parents=True, exist_ok=True)
        profiles.override_path().write_text(json.dumps({"nodes": {"alert-triage:horizon": {"conf_scale": 3.0}}}), encoding="utf-8")
        r = graph.run(ALERT, STORAGE, ReplayBackend({"future_risk": {"noul": 0.9},
                                                     "horizon": {"score": 1.0, "confidence": 0.8}}))
        self.assertTrue(r["notify"] or r["needs_human"])


class TestDefaultTextIsUnchanged(unittest.TestCase):
    """Golden strings from before R21: with learn=0 and no entries, what the PM reads has not changed."""

    def test_the_approval_post_detail_and_toast_text_are_byte_identical(self):
        from tests.test_notify import REC
        self.assertEqual(notify.format_post(3, REC), "[kimeru #3] ADO #4812\n→ 優先度を判定できないチケット #4812\nOK 3 / NG 3 / 詳細 3")
        self.assertEqual(notify.format_detail(3, REC),
                         "[kimeru 詳細 #3] 判断が必要（workitem-intake） / 改訂 1\n判断: 優先度を判定できないチケット #4812\n"
                         "次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n"
                         "不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\nOK の効果: 記録のみ\n"
                         "ado.workitem.created #4812\n対象: #4812\n内容: 優先度を判定できないチケット #4812\n"
                         "承認の対象: ado.update（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n"
                         "返信: OK 3 / NG 3 / 保留 3")
        self.assertEqual(notify.ack_text([{"id": 3, "status": "approved"}]), ("kimeru: 返事を受け付けました", "#3 承認"))


class TestTheDecisionCountIsNotPublished(unittest.TestCase):
    def test_the_card_identifier_is_a_key_of_its_own_kind(self):
        self.assertTrue(learn.is_card({"key": "kimeru.learn:L1:threshold"}))
        self.assertTrue(learn.is_card({"record": {"graph": "kimeru.learn"}}))
        self.assertFalse(learn.is_card({"key": "teams-chat-triage:t1:reply", "record": {"graph": "teams-chat-triage"}}))


if __name__ == "__main__":
    unittest.main()
