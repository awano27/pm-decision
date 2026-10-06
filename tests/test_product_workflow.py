"""Offline top-level integration coverage for onboarding, trial, requirements, and work commands."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import cli, config, notify


class ProductWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / "out"
        self.out.mkdir()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = str(self.root / "state")
        clean["KIMERU_PUSH"] = ""
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        config.apply([])
        self._approved_case(1)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _approved_case(self, number):
        ap = notify.Approvals(self.out)
        ap.data["next"] = max(ap.data.get("next", 1), number + 1)
        ap.data["items"][str(number)] = {
            "key": f"g:{number}:n", "status": "approved", "posted": True,
            "record": {"graph": "g", "event_id": str(number), "node": "n", "summary": "PRIVATE CASE TEXT",
                       "judge": {"name": "Kev"}},
        }
        ap.save()

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(["--out", str(self.out), *args])
        return code, stdout.getvalue() + stderr.getvalue()

    def test_new_commands_route_without_graph_backend_or_writer(self):
        with mock.patch("kimeru.cli._backend", side_effect=AssertionError("backend called")), \
                mock.patch("kimeru.graph.load_dir", side_effect=AssertionError("graphs loaded")), \
                mock.patch("kimeru.writer.apply", side_effect=AssertionError("writer called")):
            self.assertEqual(self.run_cli("onboarding", "status")[0], 0)
            self.assertEqual(self.run_cli("onboarding", "notification-test")[0], 0)
            self.assertEqual(self.run_cli("trial", "report", "--share")[0], 0)
            self.assertEqual(self.run_cli("work", "list")[0], 0)
        self.assertEqual(config.path(), self.root / "state" / "config.json")

    def test_onboarding_dry_run_fake_send_and_receipt_confirmation(self):
        os.environ.update(KIMERU_PUSH="webhook", KIMERU_PUSH_WEBHOOK_URL="https://example.invalid/fake")
        config.apply([])
        self.assertEqual(self.run_cli("onboarding", "notification-test")[0], 0)
        sent = []
        with mock.patch("kimeru.onboarding.secrets.token_urlsafe", return_value="sample-token"), \
                mock.patch("kimeru.push._sender", side_effect=lambda route: lambda text: sent.append((route, text))):
            code, output = self.run_cli("onboarding", "notification-test", "--send")
            self.assertEqual(code, 0)
            self.assertIn("sample-token", sent[0][1])
            confirm_code, confirm_output = self.run_cli("onboarding", "confirm", "sample-token")
            self.assertEqual(confirm_code, 0)
        self.assertIn("受理", output)
        self.assertIn("少なくとも1つ", confirm_output)
        self.assertEqual(len(sent), 1)
        self.assertFalse((self.out / "push_state.json").exists())

    def test_trial_record_and_shared_report_stay_local_and_private(self):
        self.assertEqual(self.run_cli("trial", "record", "--case", "1", "--before-minutes", "8", "--after-minutes", "5",
                                      "--draft", "edited", "--outcome", "wrong")[0], 0)
        ap = notify.Approvals(self.out)
        ap.data["items"]["1"]["handoff_evidence"] = [{"source": "user_confirmation"}]
        ap.data["items"]["1"]["record"]["actions"] = [{"type": "teams.reply", "text": "PRIVATE HANDOFF TEXT"}]
        ap.save()
        code, output = self.run_cli("trial", "report", "--share")
        self.assertEqual(code, 0)
        self.assertIn("delta -3", output)
        self.assertIn("1 confirmed copy-ready self-chat handoffs", output)
        self.assertNotIn("verified copy-ready", output)
        self.assertNotIn("PRIVATE CASE TEXT", output)
        self.assertNotIn(str(self.out), output)
        self.assertNotIn("case 1", output.lower())

    def test_requirements_edit_after_approval_invalidates_content_hash(self):
        with mock.patch("kimeru.cli._backend", side_effect=AssertionError("backend called")), \
                mock.patch("kimeru.graph.load_dir", side_effect=AssertionError("graphs loaded")), \
                mock.patch("kimeru.writer.apply", side_effect=AssertionError("writer called")):
            self.assertEqual(self.run_cli("requirements", "build", "1")[0], 0)
        md = self.out / "requirements" / "1.md"
        structured = self.out / "requirements" / "1.json"
        manifest = self.out / "requirements" / "approvals.json"
        self.assertTrue(md.exists())
        self.assertTrue(structured.exists())
        md.write_text(md.read_text(encoding="utf-8") + "\nEdited by the user.\n", encoding="utf-8")
        with mock.patch("kimeru.cli._backend", side_effect=AssertionError("backend called")), \
                mock.patch("kimeru.graph.load_dir", side_effect=AssertionError("graphs loaded")), \
                mock.patch("kimeru.writer.apply", side_effect=AssertionError("writer called")):
            self.assertEqual(self.run_cli("requirements", "approve", "1")[0], 0)
            self.assertIn("approved", self.run_cli("requirements", "status", "1")[1].lower())
        md.write_text(md.read_text(encoding="utf-8") + "\nChanged after approval.\n", encoding="utf-8")
        with mock.patch("kimeru.cli._backend", side_effect=AssertionError("backend called")), \
                mock.patch("kimeru.graph.load_dir", side_effect=AssertionError("graphs loaded")), \
                mock.patch("kimeru.writer.apply", side_effect=AssertionError("writer called")):
            code, output = self.run_cli("requirements", "status", "1")
        self.assertEqual(code, 0)
        self.assertIn("unapproved", output.lower())
        self.assertTrue(manifest.exists())
        self.assertTrue(json.loads(structured.read_text(encoding="utf-8")))

    def test_approved_work_progress_moves_through_in_progress_to_done(self):
        self.assertEqual(self.run_cli("work", "set", "1", "in_progress")[0], 0)
        self.assertEqual(self.run_cli("work", "set", "1", "done")[0], 0)
        code, output = self.run_cli("work", "list")
        self.assertEqual(code, 0)
        self.assertIn("done", output.lower())


if __name__ == "__main__":
    unittest.main()
