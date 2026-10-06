"""Crash and failure boundaries for requirements artifact pair publication."""
try:   # isolate from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kimeru import requirements


class RequirementsGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        case = {
            "key": "ado_workitem:42:triage", "status": "pending", "posted": True,
            "record": {
                "event_kind": "ado.workitem", "event_id": "42", "graph": "ado_workitem",
                "material_event": {"title": "Export report", "description": "Export omits the date range."},
                "actions": [],
            },
        }
        (self.out / "approvals.json").write_text(
            json.dumps({"next": 2, "items": {"1": case}}), encoding="utf-8")
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, *args):
        self.lines.clear()
        return requirements.dispatch(args, self.out, self.lines.append)

    def files(self):
        folder = self.out / "requirements"
        return folder / "1.json", folder / "1.md", folder / "1.generation.json"

    def test_failure_publishing_second_file_blocks_approval_until_forced_rebuild(self):
        self.assertEqual(self.call("build", "1"), 0)
        self.assertEqual(self.call("approve", "1"), 0)
        json_path, md_path, marker_path = self.files()
        manifest_path = self.out / "requirements" / "approvals.json"
        approved_manifest = manifest_path.read_bytes()
        old_json = json_path.read_bytes()
        old_md = md_path.read_bytes()
        answers_path = self.out / "answers.json"
        answers_path.write_text(json.dumps({
            "must_have": ["NEW_GENERATION_ONLY"], "optional": [], "out_of_scope": [],
            "acceptance_criteria": [], "open_questions": [],
        }), encoding="utf-8")
        original_publish = requirements._publish_staged

        def fail_markdown(stage, destination):
            if destination == md_path:
                raise OSError("injected second publication failure")
            return original_publish(stage, destination)

        with patch.object(requirements, "_publish_staged", fail_markdown):
            self.assertEqual(self.call("build", "1", "--force", "--answers", str(answers_path)), 1)
        self.assertNotEqual(json_path.read_bytes(), old_json)
        self.assertIn("NEW_GENERATION_ONLY", json_path.read_text(encoding="utf-8"))
        self.assertEqual(md_path.read_bytes(), old_md)
        self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8"))["phase"], "pending")
        self.assertEqual(self.call("approve", "1"), 1)
        self.assertIn("--force", self.lines[-1])
        self.assertEqual(manifest_path.read_bytes(), approved_manifest)
        self.assertEqual(self.call("status", "1"), 1)
        self.assertIn("--force", self.lines[-1])

        self.assertEqual(self.call("build", "1", "--force", "--answers", str(answers_path)), 0)
        self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8"))["phase"], "complete")
        self.assertEqual(self.call("approve", "1"), 0)

    def test_interruption_after_first_publication_stays_pending_until_forced_rebuild(self):
        self.assertEqual(self.call("build", "1"), 0)
        json_path, md_path, marker_path = self.files()
        old_json = json_path.read_bytes()
        old_md = md_path.read_bytes()
        answers_path = self.out / "answers.json"
        answers_path.write_text(json.dumps({
            "must_have": ["INTERRUPTED_GENERATION"], "optional": [], "out_of_scope": [],
            "acceptance_criteria": [], "open_questions": [],
        }), encoding="utf-8")
        original_publish = requirements._publish_staged

        def interrupt_after_json(stage, destination):
            original_publish(stage, destination)
            if destination == json_path:
                raise KeyboardInterrupt

        with patch.object(requirements, "_publish_staged", interrupt_after_json):
            with self.assertRaises(KeyboardInterrupt):
                self.call("build", "1", "--force", "--answers", str(answers_path))
        self.assertNotEqual(json_path.read_bytes(), old_json)
        self.assertIn("INTERRUPTED_GENERATION", json_path.read_text(encoding="utf-8"))
        self.assertEqual(md_path.read_bytes(), old_md)
        self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8"))["phase"], "pending")
        self.assertEqual(self.call("approve", "1"), 1)
        self.assertIn("--force", self.lines[-1])

        self.assertEqual(self.call("build", "1", "--force", "--answers", str(answers_path)), 0)
        self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8"))["phase"], "complete")
        self.assertEqual(self.call("approve", "1"), 0)

    def test_failure_marking_final_generation_complete_remains_blocked(self):
        original_marker = requirements._write_generation_marker

        def fail_complete(path, number, generation, phase):
            if phase == "complete":
                raise OSError("injected final marker failure")
            return original_marker(path, number, generation, phase)

        with patch.object(requirements, "_write_generation_marker", fail_complete):
            self.assertEqual(self.call("build", "1"), 1)
        _, _, marker_path = self.files()
        self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8"))["phase"], "pending")
        self.assertEqual(self.call("approve", "1"), 1)
        self.assertEqual(self.call("status", "1"), 1)

    def test_staging_failure_keeps_existing_pair_and_approval_usable(self):
        self.assertEqual(self.call("build", "1"), 0)
        self.assertEqual(self.call("approve", "1"), 0)
        json_path, md_path, marker_path = self.files()
        before = (json_path.read_bytes(), md_path.read_bytes(), marker_path.read_bytes())
        original_stage = requirements._write_staged
        calls = 0

        def fail_second_stage(path, text):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected staging failure")
            return original_stage(path, text)

        with patch.object(requirements, "_write_staged", fail_second_stage):
            self.assertEqual(self.call("build", "1", "--force"), 1)
        self.assertEqual((json_path.read_bytes(), md_path.read_bytes(), marker_path.read_bytes()), before)
        self.assertEqual(list((self.out / "requirements").glob(".1.*.stage")), [])
        self.assertEqual(self.call("status", "1"), 0)
        self.assertIn("approved", self.lines[-1])

    def test_legacy_pair_and_human_edits_remain_approvable(self):
        self.assertEqual(self.call("build", "1"), 0)
        json_path, md_path, marker_path = self.files()
        marker_path.unlink()
        document = json.loads(json_path.read_text(encoding="utf-8"))
        document["answers"]["must_have"] = ["Human-authored requirement"]
        json_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        md_path.write_text("Human-edited Markdown", encoding="utf-8")
        self.assertEqual(self.call("approve", "1"), 0)
        self.assertEqual(self.call("status", "1"), 0)
        self.assertIn("approved", self.lines[-1])

    def test_invalid_marker_fails_closed_for_approval_and_status(self):
        self.assertEqual(self.call("build", "1"), 0)
        _, _, marker_path = self.files()
        marker_path.write_text(json.dumps({"schema_version": 1, "case": 9, "phase": "complete"}), encoding="utf-8")
        self.assertEqual(self.call("approve", "1"), 2)
        self.assertIn("marker", self.lines[-1])
        self.assertEqual(self.call("status", "1"), 2)


if __name__ == "__main__":
    unittest.main()
