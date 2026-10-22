try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from datetime import datetime, timezone

from kimeru import config, notify, review, trial

ONE = "19:a_b@unq.gbl.spaces"   # a chat id (the one the R14 tests use)


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name) / "out"
        self.out.mkdir()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = str(Path(self.temp.name) / "state")
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        config.apply([])
        ap = notify.Approvals(self.out)
        ap.add("g:1:n", {"graph": "g", "event_id": "1", "node": "n", "summary": "PRIVATE TEXT",
                          "judge": {"name": "Kev"}})
        ap.data["items"]["1"]["status"] = "approved"
        ap.save()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_record_validates_case_minutes_and_choices(self):
        self.assertEqual(trial.dispatch(["record", "--case", "1", "--before-minutes", "8", "--after-minutes", "5",
                                         "--draft", "edited", "--outcome", "wrong"], self.out), 0)
        self.assertEqual(trial.dispatch(["record", "--case", "999", "--after-minutes", "5", "--draft", "unused",
                                         "--outcome", "unknown"], self.out), 1)
        for value in ("nan", "-1"):
            self.assertEqual(trial.dispatch(["record", "--case", "1", "--before-minutes", value, "--after-minutes", "5",
                                             "--draft", "unused", "--outcome", "unknown"], self.out), 2)

    def test_missing_baseline_and_low_sample_are_unknown_or_inconclusive(self):
        for _ in range(4):
            self.assertEqual(trial.dispatch(["record", "--case", "1", "--after-minutes", "5", "--draft", "edited",
                                             "--outcome", "correct"], self.out), 0)
        report = trial.collect(self.out)
        self.assertEqual(report["sample_count"], 4)
        self.assertEqual(report["effect"], "inconclusive")
        self.assertEqual(report["paired_deltas"], [])
        self.assertEqual(report["baseline_missing"], 4)

    def test_repeated_records_for_one_case_do_not_reach_five_case_threshold(self):
        args = ["record", "--case", "1", "--before-minutes", "8", "--after-minutes", "5",
                "--draft", "edited", "--outcome", "correct"]
        for _ in range(5):
            self.assertEqual(trial.dispatch(args, self.out), 0)
        report = trial.collect(self.out)
        self.assertEqual(report["sample_count"], 5)
        self.assertEqual(report["distinct_case_count"], 1)
        self.assertEqual(report["effect"], "inconclusive")

    def test_five_cases_without_baselines_remain_inconclusive(self):
        ap = notify.Approvals(self.out)
        for i in range(2, 6):
            ap.add(f"g:{i}:n", {"graph": "g", "event_id": str(i), "node": "n", "judge": {"name": "Kev"}})
        ap.save()
        for case in range(1, 6):
            self.assertEqual(trial.dispatch(["record", "--case", str(case), "--after-minutes", "5", "--draft", "unchanged",
                                             "--outcome", "unknown"], self.out), 0)
        report = trial.collect(self.out)
        self.assertEqual(report["distinct_case_count"], 5)
        self.assertEqual(report["paired_case_count"], 0)
        self.assertEqual(report["effect"], "inconclusive")

    def test_five_distinct_paired_cases_clear_only_the_sample_count_gate(self):
        ap = notify.Approvals(self.out)
        for i in range(2, 6):
            ap.add(f"g:{i}:n", {"graph": "g", "event_id": str(i), "node": "n", "judge": {"name": "Kev"}})
        ap.save()
        for case in range(1, 6):
            self.assertEqual(trial.dispatch(["record", "--case", str(case), "--before-minutes", "8", "--after-minutes", "5",
                                             "--draft", "edited", "--outcome", "unknown"], self.out), 0)
        report = trial.collect(self.out)
        self.assertEqual(report["distinct_case_count"], 5)
        self.assertEqual(report["paired_case_count"], 5)
        self.assertEqual(report["effect"], "observed")

    def test_share_contains_aggregates_only_and_report_keeps_evidence_unknown(self):
        trial.dispatch(["record", "--case", "1", "--before-minutes", "8", "--after-minutes", "5",
                        "--draft", "edited", "--outcome", "wrong"], self.out)
        shared = trial.render(trial.collect(self.out, share=True), share=True)
        self.assertNotIn("PRIVATE TEXT", shared)
        self.assertNotIn(str(self.out), shared)
        self.assertNotIn("case 1", shared.lower())
        self.assertIn("approval-to-handoff elapsed time Unknown", shared)
        self.assertIn("explicit manual trial observations", shared)
        self.assertIn("progress Unknown for 1 untracked cases", shared)
        self.assertIn("notification receipt: 0 challenge confirmations (each confirms at least 1 route)", shared)
        self.assertIn("-3", shared)

    def test_shared_report_shows_aggregate_pair_stats_without_individual_pairs(self):
        ap = notify.Approvals(self.out)
        for i in (2, 3):
            ap.add(f"g:{i}:n", {"graph": "g", "event_id": str(i), "node": "n", "judge": {"name": "Kev"}})
            ap.data["items"][str(i)]["status"] = "approved"
        ap.save()
        for case, before, after in ((1, 8, 5), (2, 10, 6), (3, 12, 10)):
            self.assertEqual(trial.dispatch(["record", "--case", str(case), "--before-minutes", str(before),
                                             "--after-minutes", str(after), "--draft", "edited",
                                             "--outcome", "correct"], self.out), 0)
        shared = trial.render(trial.collect(self.out, share=True), share=True)
        self.assertIn("3 explicit pairs", shared)
        self.assertIn("mean delta -3 minutes", shared)
        self.assertIn("range -4 to -2 minutes", shared)
        self.assertNotIn("pair 1:", shared)
        self.assertNotIn("8 -> 5", shared)

        local = trial.render(trial.collect(self.out), share=False)
        self.assertIn("pair 1: 8 -> 5 minutes", local)

    def test_user_confirmed_handoff_is_labeled_as_confirmed(self):
        ap = notify.Approvals(self.out)
        ap.data["items"]["1"]["handoff_evidence"] = [{"source": "user_confirmation"}]
        ap.data["items"]["1"]["record"]["actions"] = [{"type": "teams.reply", "text": "PRIVATE HANDOFF TEXT"}]
        ap.save()
        report = trial.render(trial.collect(self.out, share=True), share=True)
        self.assertIn("1 confirmed copy-ready self-chat handoffs", report)
        self.assertNotIn("verified copy-ready", report)

    def test_share_excludes_jev_and_unknown_provenance_outcomes(self):
        ap = notify.Approvals(self.out)
        ap.add("g:2:n", {"graph": "g", "event_id": "2", "node": "n", "judge": {"name": "Jev"}})
        ap.add("g:3:n", {"graph": "g", "event_id": "3", "node": "n"})
        ap.save()
        for case in (2, 3):
            self.assertEqual(trial.dispatch(["record", "--case", str(case), "--after-minutes", "5", "--draft", "unused",
                                             "--outcome", "wrong"], self.out), 0)
        local = trial.collect(self.out)
        shared = trial.collect(self.out, share=True)
        self.assertEqual(local["outcomes"]["wrong"], 2)
        self.assertEqual(shared["outcomes"]["wrong"], 0)
        self.assertEqual(shared["sample_count"], 0)
        self.assertNotIn("case outcomes", trial.render(local, share=True))

    def test_corrupt_samples_and_invalid_timestamps_are_excluded(self):
        rows = [
            {"case": 1, "before_minutes": float("inf"), "after_minutes": 1, "draft": "edited", "outcome": "wrong",
             "judge_group": "non_jev", "at": 1},
            {"case": 1, "before_minutes": None, "after_minutes": 1, "draft": "edited", "outcome": "wrong",
             "judge_group": "non_jev", "at": "not-a-time"},
            {"case": 1, "before_minutes": None, "after_minutes": 10**1000, "draft": "edited", "outcome": "wrong",
             "judge_group": "non_jev", "at": 1},
            None,
        ]
        (self.out / "trial.json").write_text(json.dumps({"samples": rows}), encoding="utf-8")
        result = trial.collect(self.out)
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(result["invalid_samples"], 4)
        self.assertEqual(result["outcomes"]["wrong"], 0)

    def test_report_uses_only_aggregate_work_handoff_evidence(self):
        ap = notify.Approvals(self.out)
        item = ap.data["items"]["1"]
        item["work"] = {"state": "done"}
        item["record"]["actions"] = [{"type": "teams.reply", "text": "PRIVATE HANDOFF TEXT"}]
        item["handoff_evidence"] = [{"source": "bridge_readback"}]
        ap.add("g:2:n", {"graph": "g", "event_id": "2", "node": "n",
                          "judge": {"name": "Kev"},
                          "actions": [{"type": "teams.post", "text": "PRIVATE OTHER TEXT"}]})
        ap.data["items"]["2"]["status"] = "approved"
        ap.save()
        report = trial.render(trial.collect(self.out, share=True), share=True)
        self.assertIn("done 1 / tracked 1 / approved 2", report)
        self.assertIn("progress Unknown for 1 untracked cases", report)
        self.assertIn("1 confirmed copy-ready self-chat handoffs; 1 cases with ambiguous handoff evidence", report)
        self.assertIn("approval-to-handoff elapsed time Unknown", report)
        self.assertNotIn("PRIVATE", report)

    def test_final_wrong_without_a_question_does_not_invent_a_fixture_label(self):
        decisions = [{"graph": "g", "event_id": str(i), "node": "n", "event_kind": "x", "event": {"kind": "x"},
                      "path": [], "summary": "local", "judge": {"name": "test"}} for i in (1, 2)]
        (self.out / "decisions.jsonl").write_text("".join(json.dumps(d) + "\n" for d in decisions), encoding="utf-8")
        answers = iter(["n", "w"])
        tally = review.run(self.out, {}, {}, input_fn=lambda prompt: next(answers), print_fn=lambda line: None)
        rows = review.read_jsonl(review.reviews_path())
        self.assertEqual(rows[-1]["verdict"], "wrong")
        self.assertEqual(tally["wrong"], 2)
        self.assertEqual(len(rows), 2)
        self.assertFalse(review.fixtures_path().exists())


class TestTrialCountsSendsWithoutJev(unittest.TestCase):
    NOW = datetime(2026, 10, 13, tzinfo=timezone.utc).timestamp()

    def write_items(self, out):
        Path(out).mkdir(parents=True, exist_ok=True)
        base = {"chat_id": ONE, "p": "x", "approved_at": "2026-10-12T00:00:00+00:00"}
        items = {
            "1": {"key": "k1", "status": "approved", "posted": True, "record": {"judge": {"name": "Kev"}},
                  "sent_base": base, "sent_seen": {"state": "as_is", "at": "2026-10-12T00:05:00+00:00", "ratio": 1.0}},
            "2": {"key": "k2", "status": "approved", "posted": True, "record": {"judge": {"name": "Jev"}},
                  "sent_base": base, "sent_seen": {"state": "other", "at": "2026-10-12T00:07:00+00:00", "ratio": 0.1}},
            "3": {"key": "k3", "status": "approved", "posted": True, "record": {"judge": {"name": "Kev"}}, "sent_base": base},
        }
        (Path(out) / "approvals.json").write_text(json.dumps({"next": 4, "items": items}, ensure_ascii=False), encoding="utf-8")

    def test_the_shared_report_counts_only_the_sends_of_a_non_jev_judge(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_items(d)
            shared = trial.measurement_collect(d, [], share=True, now=self.NOW)["local"]
            self.assertEqual(shared["sent_states"], {"as_is": 1, "edited": 0, "other": 0})
            self.assertEqual(shared["sent_approval_to_seen"]["eligible"], 2)
            self.assertEqual(shared["sent_approval_to_seen"]["unknown"], 1)
            whole = trial.measurement_collect(d, [], share=False, now=self.NOW)["local"]
            self.assertEqual(whole["sent_states"], {"as_is": 1, "edited": 0, "other": 1})

    def test_the_lines_name_the_sends_only_when_there_are_some(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_items(d)
            lines = "\n".join(trial.measurement_lines(trial.measurement_collect(d, [], share=True, now=self.NOW)))
            self.assertIn("sent after the OK", lines)
        with tempfile.TemporaryDirectory() as d:
            lines = "\n".join(trial.measurement_lines(trial.measurement_collect(d, [], share=True, now=self.NOW)))
            self.assertNotIn("sent after the OK", lines)

class TrialNotificationReceiptTests(unittest.TestCase):
    """The trial report keeps route acceptance, a person's receipt and the real-device verification apart."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name) / "out"
        self.out.mkdir()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = str(Path(self.temp.name) / "state")
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        config.apply([])

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def write(self, challenges):
        (self.out / "onboarding.json").write_text(json.dumps({"challenges": challenges}), encoding="utf-8")

    def test_without_any_record_the_device_verification_is_not_run(self):
        data = trial.collect(self.out)
        self.assertIn("measurement", data)  # public-main timing evidence survives the route-receipt integration
        self.assertEqual(data["measurement"], trial.measurement_collect(self.out, []))
        self.assertEqual(data["device_verification"], "NOT_RUN")
        self.assertIn("real-device verification: NOT_RUN", trial.render(data))
        self.assertIn("real-device verification: NOT_RUN", trial.render(trial.collect(self.out, share=True), share=True))

    def test_accepted_sends_and_unknown_route_confirmations_do_not_make_a_real_device_check(self):
        self.write([
            {"token_hash": "h0", "created_at": 1, "sender_accepted": True, "receipt_confirmed": True, "confirmed_at": 2},
            {"version": 2, "created_at": 3, "sender_accepted": True, "receipt_confirmed": False,
             "routes": {"webhook": {"token_hash": "h1", "sender_accepted": True, "receipt_confirmed": False},
                        "outlook": {"token_hash": "h2", "sender_accepted": False, "receipt_confirmed": False}}}])
        data = trial.collect(self.out)
        self.assertEqual(data["notification_receipts"], 1)                  # the old record: at least one route, unknown which
        self.assertEqual(data["device_verification"], "NOT_RUN")
        self.assertEqual(data["receipt_routes"]["webhook"], {"accepted": 1, "confirmed": 0, "phone_confirmed": 0})
        self.assertEqual(data["receipt_routes"]["outlook"], {"accepted": 0, "confirmed": 0, "phone_confirmed": 0})
        text = trial.render(data)
        self.assertIn("route unknown (older record): 1 confirmations, not counted for any route", text)
        self.assertIn("route webhook: send accepted 1 / person confirmed 0 / on phone 0", text)
        self.assertIn("real-device verification: NOT_RUN", text)

    def test_a_phone_confirmation_of_a_route_is_counted_for_that_route_only(self):
        os.environ["KIMERU_PUSH"] = "webhook"
        os.environ["KIMERU_PUSH_WEBHOOK_URL"] = "https://example.invalid/hook"
        config.apply([])
        self.write([{"version": 2, "created_at": 3, "sender_accepted": True, "receipt_confirmed": True,
                     "routes": {"webhook": {"token_hash": "h1", "sender_accepted": True, "receipt_confirmed": True,
                                            "confirmed_at": 4, "device": "phone"},
                                "outlook": {"token_hash": "h2", "sender_accepted": True, "receipt_confirmed": False}}}])
        data = trial.collect(self.out)
        self.assertEqual(data["receipt_routes"]["webhook"]["phone_confirmed"], 1)
        self.assertEqual(data["receipt_routes"]["outlook"]["phone_confirmed"], 0)
        self.assertEqual(data["device_verification"], "CONFIRMED_ON_PHONE")      # the only enabled route

    def test_the_shared_report_has_route_names_and_counts_only(self):
        self.write([{"version": 2, "created_at": 3, "sender_accepted": True, "receipt_confirmed": False,
                     "routes": {"webhook": {"token_hash": "SECRETHASH", "sender_accepted": True, "receipt_confirmed": False}}}])
        shared = trial.render(trial.collect(self.out, share=True), share=True)
        self.assertNotIn("SECRETHASH", shared)
        self.assertIn("route webhook: send accepted 1", shared)


if __name__ == "__main__":
    unittest.main()
