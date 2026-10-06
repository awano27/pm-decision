"""Passive measurements: actual stored lifecycle evidence, never live services."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from kimeru import cli, demo, graph, notify, plan, report, trial
from kimeru.backends import StubBackend
from tests.test_notify import FakeBridge, write_queue

ROOT = Path(__file__).resolve().parent.parent
READY = "2026-01-01T00:00:00+00:00"
POST = "2026-01-01T00:00:10+00:00"
OK = "2026-01-01T00:01:10+00:00"


def record(source="local"):
    return {"graph": "g", "event_id": "e", "node": "n", "event_kind": "teams.chat",
            "outcome": "advise", "needs_human": True, "advice": "review", "path": [],
            "summary": "PRIVATE_MESSAGE", "event": {"kind": "teams.chat", "chat_id": "chat", "author": "A"},
            "actions": [{"type": "teams.reply", "text": "確認します。"}],
            "measurement": {"schema": 1, "source": source, "generation": 1, "ready_at": READY},
            "judge": {"name": "Kev"}, "at": READY}


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name)
        self.rec = record()
        write_queue(self.out, self.rec)
        (self.out / "decisions.jsonl").write_text(json.dumps(self.rec) + "\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def post(self):
        with mock.patch("kimeru.notify.measurement_now", return_value=POST, create=True):
            notify.notify(self.out, FakeBridge(), send=True)

    def approve(self):
        def after(*args, **kwargs):
            self.assertEqual(args[3].get("measurement", {}).get("approval_observed_at"), OK)
            return []
        with mock.patch("kimeru.notify.measurement_now", return_value=OK, create=True), \
                mock.patch("kimeru.notify._after_approval", side_effect=after):
            notify.collect(self.out, FakeBridge(["OK 1"]), send=False)

    def test_verified_post_and_accepted_ok_are_saved_before_after_approval(self):
        self.post()
        self.assertEqual(notify.Approvals(self.out).data["items"]["1"].get("measurement", {}).get("posted_at"), POST)
        self.approve()
        item = notify.Approvals(self.out).data["items"]["1"]
        self.assertEqual(item["status"], "approved")
        with mock.patch("kimeru.notify.measurement_now", return_value="2026-01-02T00:00:00Z", create=True):
            self.assertEqual(notify.collect(self.out, FakeBridge(["OK 1"])), [])
        self.assertEqual(notify.Approvals(self.out).data["items"]["1"]["measurement"]["approval_observed_at"], OK)
        log = json.loads((self.out / "approvals.log.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(log["measurement"]["generation"], 1)

    def test_paste_only_does_not_supply_positive_posting_time(self):
        notify.notify(self.out, FakeBridge(), send=False)
        item = notify.Approvals(self.out).data["items"]["1"]
        self.assertEqual(item.get("measurement", {}).get("ready_at"), READY)
        self.assertIsNone(item["measurement"].get("posted_at"))

    def test_successful_paste_starts_metrics_generation_without_changing_revision(self):
        self.post()
        notify.collect(self.out, FakeBridge(["下書き 1 確認のうえ回答します。"]), send=False)
        item = notify.Approvals(self.out).data["items"]["1"]
        self.assertEqual(item.get("measurement", {}).get("generation"), 2)
        self.assertIsNone(item["measurement"].get("posted_at"))
        self.assertIsNone(item.get("revision"))
        self.assertEqual(item["status"], "pending")

    def test_failed_redraft_preserves_generation_but_invalidates_post_interval(self):
        self.post()
        notify.collect(self.out, FakeBridge(["修正 1 短く"]), send=False)
        item = notify.Approvals(self.out).data["items"]["1"]
        self.assertEqual(item.get("measurement", {}).get("generation"), 1)
        self.assertEqual(item["measurement"]["ready_at"], READY)
        self.assertIsNone(item["measurement"].get("posted_at"))

    def test_followup_replaces_only_metrics_generation_and_preserves_case_key(self):
        self.post()
        result = record()
        result["event_id"] = "new"
        with mock.patch("kimeru.notify.measurement_now", return_value=OK, create=True):
            self.assertTrue(cli._merge_locked(self.out, result["event"], result, notify))
        item = notify.Approvals(self.out).data["items"]["1"]
        self.assertEqual(item.get("measurement", {}).get("generation"), 2)
        self.assertIsNone(item["measurement"].get("posted_at"))
        self.assertEqual(result["measurement"]["case_key"], "g:e:n")

    def test_report_uses_explicit_adoption_not_approval_and_exposes_coverage(self):
        self.post()
        self.approve()
        data = trial.collect(self.out)
        self.assertEqual(data.get("measurement", {}).get("adoption_observed_cases"), 0)
        self.assertEqual(data["measurement"]["eligible_draft_cases"], 1)
        trial.dispatch(["record", "--case", "1", "--after-minutes", "4", "--draft", "unused", "--outcome", "unknown"], self.out)
        trial.dispatch(["record", "--case", "1", "--before-minutes", "8", "--after-minutes", "3", "--draft", "edited", "--outcome", "unknown"], self.out)
        data = trial.collect(self.out)
        self.assertEqual(data["measurement"]["adoption_observed_cases"], 1)
        self.assertEqual(data["measurement"]["adoption_rate"], 1)
        self.assertEqual(data["measurement"]["repeat_observations"], 1)
        self.assertEqual(data["measurement"]["local"]["approval_latency"]["median_seconds"], 60)
        html = report.build(self.out, {})
        for marker in ("Manual review", "model/writer", "Observed approval latency", "Event-arrival-to-approval: Unknown", "eligible", "generation 1"):
            self.assertIn(marker, html)

    def test_missing_legacy_future_and_reversed_times_remain_unknown(self):
        self.post()
        ap = notify.Approvals(self.out)
        for posted, approved in ((None, OK), (POST, "2099-01-01T00:00:00Z"), (OK, POST), ("invalid", OK)):
            ap.data["items"]["1"]["measurement"] = {"schema": 1, "generation": 1, "source": "local", "ready_at": READY,
                                                     "posted_at": posted, "approval_observed_at": approved, "posting_source": "bridge_readback",
                                                     "case_key": "g:e:n"}
            ap.data["items"]["1"]["status"] = "approved"
            ap.save()
            data = trial.collect(self.out).get("measurement", {}).get("local", {}).get("approval_latency", {})
            self.assertEqual(data.get("measured"), 0)
            self.assertEqual(data["unknown"], 1)

    def test_shared_measurement_omits_jev_and_unknown_judge_evidence(self):
        self.post()
        self.approve()
        for judge in ("Jev", "unknown"):
            ap = notify.Approvals(self.out)
            ap.data["items"]["1"]["record"]["judge"] = {"name": judge}
            ap.save()
            shared = trial.collect(self.out, share=True)
            self.assertEqual(shared.get("measurement", {}).get("eligible_draft_cases"), 0)
            text = trial.render(shared, share=True)
            self.assertNotIn("PRIVATE_MESSAGE", text)
            self.assertNotIn("g:e:n", text)

    def test_hold_to_ok_includes_hold_and_does_not_reset_post_time(self):
        self.post()
        notify.collect(self.out, FakeBridge(["保留 1"]), send=False)
        self.approve()
        self.assertEqual(trial.collect(self.out)["measurement"]["local"]["approval_latency"]["median_seconds"], 60)

    def test_ask_back_requires_new_timing_but_approval_rules_are_unchanged(self):
        self.rec["actions"][0]["ask_back"] = "状況を教えていただけますか。"
        write_queue(self.out, self.rec)
        self.post()
        notify.collect(self.out, FakeBridge(["聞き返し 1", "OK 1"]), send=False)
        item = notify.Approvals(self.out).data["items"]["1"]
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["measurement"]["generation"], 2)
        self.assertEqual(item["measurement_history"][0]["posted_at"], POST)

    def test_ambiguous_post_human_confirmation_does_not_invent_original_post_time(self):
        class Ambiguous(FakeBridge):
            def post(self, text, send):
                return {"ok": True, "typed": True, "sent": True, "readback": {"matched": True}}
        with self.assertRaises(RuntimeError):
            notify.notify(self.out, Ambiguous(), send=True)
        with mock.patch("kimeru.notify.measurement_now", return_value=POST, create=True):
            notify.confirm_delivery(self.out, "case", "1", confirm_delivered=True)
        item = notify.Approvals(self.out).data["items"]["1"]
        self.assertEqual(item["measurement"].get("posting_source"), "user_confirmation")
        self.assertEqual(item["measurement"]["posting_confirmed_at"], POST)
        self.assertIsNone(item["measurement"].get("posted_at"))
        self.approve()
        data = trial.collect(self.out)["measurement"]["local"]
        self.assertEqual(data["approval_latency"]["unknown"], 1)
        self.assertEqual(data["human_delivery_confirmations"], 1)

    def test_old_generation_recovery_adds_no_current_posting_measurement(self):
        self.post()
        ap = notify.Approvals(self.out)
        item = ap.data["items"]["1"]
        item.update(delivery_unknown=True, delivery_unknown_part="post", delivery_unknown_revision=1)
        ap.save()
        result = record()
        result["event_id"] = "new"
        self.assertTrue(cli._merge_locked(self.out, result["event"], result, notify))
        notify.confirm_delivery(self.out, "case", "1", confirm_delivered=True)
        item = notify.Approvals(self.out).data["items"]["1"]
        self.assertFalse(item["posted"])
        self.assertIsNone(item["measurement"].get("posted_at"))
        self.assertIsNone(item["measurement"].get("posting_confirmed_at"))

    def test_zero_adoption_and_future_or_synthetic_trial_values_are_not_business_evidence(self):
        self.post()
        rows = [{"case": 1, "before_minutes": 8, "after_minutes": 2, "draft": "edited", "outcome": "correct",
                 "at": 9999999999, "judge_group": "non_jev"},
                {"case": 1, "before_minutes": 8, "after_minutes": 2, "draft": "edited", "outcome": "correct",
                 "at": 1, "judge_group": "non_jev", "measurement_source": "demo"}]
        (self.out / "trial.json").write_text(json.dumps({"samples": rows}), encoding="utf-8")
        data = trial.collect(self.out)
        self.assertEqual(data["paired_case_count"], 0)
        self.assertIsNone(data["measurement"]["adoption_rate"])

    def test_malformed_timing_and_generation_mismatch_are_unknown(self):
        self.post()
        ap = notify.Approvals(self.out)
        for value in ([], {"schema": 1, "generation": 2, "source": "local", "posted_at": POST,
                           "approval_observed_at": OK, "posting_source": "bridge_readback", "case_key": "g:e:n"}):
            ap.data["items"]["1"]["measurement"] = value
            ap.data["items"]["1"]["status"] = "approved"
            ap.save()
            self.assertEqual(trial.collect(self.out)["measurement"]["local"]["approval_latency"]["unknown"], 1)

    def test_repost_for_new_execution_details_invalidates_old_display_interval(self):
        self.post()
        with mock.patch("kimeru.execute.is_gated", return_value=True):
            changes = notify.collect(self.out, FakeBridge(["OK 1"]), real=True, send=False)
        self.assertEqual(changes[0]["status"], "reposted")
        item = notify.Approvals(self.out).data["items"]["1"]
        self.assertEqual(item["measurement"]["generation"], 2)
        self.assertIsNone(item["measurement"].get("posted_at"))

    def test_trial_observation_for_previous_proposal_is_not_current_adoption(self):
        self.post()
        trial.dispatch(["record", "--case", "1", "--after-minutes", "3", "--draft", "unchanged", "--outcome", "unknown"], self.out)
        notify.collect(self.out, FakeBridge(["下書き 1 確認のうえ回答します。"]), send=False)
        self.assertEqual(trial.collect(self.out)["measurement"]["adoption_observed_cases"], 0)
        html = report.build(self.out, {})
        self.assertIn("Current proposal #1: generation 2", html)
        self.assertIn("superseded proposal", html)

    def test_redraft_archives_the_previous_post_and_uses_new_ready_time(self):
        from tests.test_writer import FakeWriter
        self.rec["material_event"] = {"text": "佐藤さんから依頼"}
        write_queue(self.out, self.rec)
        self.post()
        with mock.patch("kimeru.notify.measurement_now", return_value=OK):
            notify.collect(self.out, FakeBridge(["修正 1 短く"]), writer=FakeWriter(), send=False)
        item = notify.Approvals(self.out).data["items"]["1"]
        self.assertEqual(item["measurement"]["generation"], 2)
        self.assertEqual(item["measurement"]["ready_at"], OK)
        self.assertEqual(item["measurement_history"][0]["posted_at"], POST)
        self.assertIsNone(item["measurement"].get("posted_at"))

    def test_corrupt_measurement_cannot_repeat_a_post_or_block_approval(self):
        ap = notify.Approvals(self.out)
        if not ap.data["items"]:
            ap.add("g:e:n", self.rec)
        item = ap.data["items"]["1"]
        item.pop("measurement", None)
        item["record"]["measurement"] = [1]
        item["measurement_history"] = {}
        ap.save()
        bridge = FakeBridge()
        notify.notify(self.out, bridge, send=True)
        notify.notify(self.out, bridge, send=True)
        self.assertEqual(len(bridge.posts), 1)
        notify.collect(self.out, FakeBridge(["OK 1"]), send=False)
        self.assertEqual(notify.Approvals(self.out).data["items"]["1"]["status"], "approved")
        notify.next_measurement(item)
        self.assertIsInstance(item["measurement_history"], list)

    def test_invalid_generation_join_preserves_coverage_but_not_adoption(self):
        self.post()
        ap = notify.Approvals(self.out)
        ap.data["items"]["1"]["measurement"]["generation"] = 2
        ap.save()
        trial.dispatch(["record", "--case", "1", "--after-minutes", "3", "--draft", "unchanged", "--outcome", "unknown"], self.out)
        metrics = trial.collect(self.out)["measurement"]
        self.assertEqual(metrics["eligible_draft_cases"], 1)
        self.assertEqual(metrics["adoption_unknown_cases"], 1)
        self.assertIsNone(metrics["adoption_rate"])

    def test_html_requires_valid_provenance_and_handles_corrupt_metadata(self):
        self.post()
        self.approve()
        ap = notify.Approvals(self.out)
        item = ap.data["items"]["1"]
        for value in ([1], {**item["measurement"], "case_key": "wrong-case"},
                      {**item["measurement"], "posted_at": "2099-01-01T00:00:00Z"}):
            item["measurement"] = value
            item["measurement_history"] = [1, None]
            ap.save()
            html = report.build(self.out, {})
            self.assertIn("positively observed post Unknown", html)
            self.assertIn("verified post Unknown", html)
        self.rec["measurement"] = [1]
        (self.out / "decisions.jsonl").write_text(json.dumps(self.rec) + "\n", encoding="utf-8")
        report.build(self.out, {})

    def test_missing_source_and_boolean_reference_generation_are_invalid(self):
        self.post()
        item = notify.Approvals(self.out).data["items"]["1"]
        timing = dict(item["measurement"])
        reference = dict(item["record"]["measurement"])
        reference["generation"] = True
        self.assertFalse(trial.measurement_valid(item, timing, reference))
        reference["generation"] = 1
        timing.pop("source")
        reference.pop("source")
        self.assertFalse(trial.measurement_valid(item, timing, reference))
        item["measurement"] = [1]
        item["record"]["measurement"] = [1]
        ap = notify.Approvals(self.out)
        ap.data["items"]["1"] = item
        ap.save()
        self.assertEqual(trial.dispatch(["record", "--case", "1", "--after-minutes", "3", "--draft", "unchanged", "--outcome", "unknown"], self.out), 0)
        self.assertIsNone(trial.collect(self.out)["measurement"]["adoption_rate"])

    def test_missing_observation_generation_stays_unknown_for_generation_one(self):
        self.post()
        row = {"case": 1, "after_minutes": 3, "draft": "unchanged", "outcome": "unknown",
               "at": trial._timestamp(OK), "measurement_source": "local", "judge_group": "non_jev"}
        for generation in (None, True, "1"):
            row["proposal_generation"] = generation
            (self.out / "trial.json").write_text(json.dumps({"samples": [row]}), encoding="utf-8")
            metrics = trial.collect(self.out)["measurement"]
            self.assertEqual(metrics["eligible_draft_cases"], 1)
            self.assertEqual(metrics["adoption_unknown_cases"], 1)
            self.assertIsNone(metrics["adoption_rate"])

    def test_observation_before_current_ready_time_stays_unknown(self):
        self.post()
        row = {"case": 1, "after_minutes": 3, "draft": "unchanged", "outcome": "unknown",
               "at": 1, "measurement_source": "local", "proposal_generation": 1, "judge_group": "non_jev"}
        (self.out / "trial.json").write_text(json.dumps({"samples": [row]}), encoding="utf-8")
        metrics = trial.collect(self.out)["measurement"]
        self.assertEqual(metrics["adoption_unknown_cases"], 1)
        self.assertIsNone(metrics["adoption_rate"])
        row["at"] = trial._timestamp(READY)
        (self.out / "trial.json").write_text(json.dumps({"samples": [row]}), encoding="utf-8")
        self.assertEqual(trial.collect(self.out)["measurement"]["adoption_rate"], 1)

    def test_demo_outputs_are_synthetic_and_never_describe_actual_posting(self):
        books = plan.load_playbooks(ROOT / "playbooks")
        graphs = graph.load_dir(ROOT / "graphs", books)
        # self.out holds a real record: the demo must refuse it, and writes to a folder of its own
        with redirect_stdout(io.StringIO()), self.assertRaises(demo.RecordsInTheWay):
            demo.run(ROOT / "examples" / "demo_day.json", graphs, StubBackend(), books, cli.process, self.out, pace=0)
        self.assertEqual(json.loads((self.out / "decisions.jsonl").read_text(encoding="utf-8")), self.rec)
        out = self.out / "demo"
        with redirect_stdout(io.StringIO()):
            demo.run(ROOT / "examples" / "demo_day.json", graphs, StubBackend(), books, cli.process, out, pace=0)
        rows = [json.loads(line) for line in (out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(r.get("measurement", {}).get("source") == "demo" for r in rows))
        html = report.build(out, graphs)
        self.assertIn("simulated self-chat", html)
        self.assertNotIn("自分とのチャットへの実投稿", html)
        self.assertIn("synthetic", html)
        self.assertEqual(trial.collect(out)["measurement"]["local"]["approval_latency"]["measured"], 0)


if __name__ == "__main__":
    unittest.main()
