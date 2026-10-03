try:   # isolate from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import hashlib
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from kimeru import requirements


class RequirementsArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.case = {
            "key": "ado_workitem:42:triage",
            "status": "pending",
            "posted": True,
            "record": {
                "event_kind": "ado.workitem",
                "event_id": "42",
                "graph": "ado_workitem",
                "material_event": {
                    "title": "Export report",
                    "description": "The current export omits the selected date range.",
                    "repro_steps": "Choose a range and export CSV.",
                    "url": "https://dev.azure.com/example/project/_workitems/edit/42",
                },
                "actions": [],
            },
        }
        (self.out / "approvals.json").write_text(json.dumps({"next": 2, "items": {"1": self.case}}), encoding="utf-8")
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, *args):
        return requirements.dispatch(args, self.out, self.lines.append)

    def test_build_keeps_answers_sources_and_marks_unknowns_without_approval(self):
        answers_path = self.out / "answers.json"
        answers = {
            "must_have": ["CSV export includes the selected date range"],
            "optional": [],
            "out_of_scope": [],
            "acceptance_criteria": ["Reproduction steps pass with the selected range"],
            "open_questions": [],
        }
        answers_path.write_text(json.dumps(answers), encoding="utf-8")
        self.assertEqual(self.call("build", "1", "--answers", str(answers_path)), 0)

        folder = self.out / "requirements"
        doc = json.loads((folder / "1.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["answers"], answers)
        self.assertEqual(doc["sources"][0]["case"], 1)
        self.assertEqual(doc["sources"][0]["event_id"], "42")
        self.assertEqual(doc["source_material"]["description"], self.case["record"]["material_event"]["description"])
        self.assertEqual(doc["answers"], answers)
        self.assertIn("Which improvements are optional?", doc["generated_open_questions"])
        md = (folder / "1.md").read_text(encoding="utf-8")
        self.assertIn("https://dev.azure.com/example/project/_workitems/edit/42", md)
        self.assertIn("The current export omits the selected date range.", md)
        self.assertFalse((folder / "approvals.json").exists())
        current = json.loads((self.out / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual(current["items"]["1"]["status"], "pending")

    def test_invalid_answer_schema_and_missing_case_do_not_create_artifacts(self):
        answers_path = self.out / "bad.json"
        answers_path.write_text(json.dumps({"must_have": ["ok"], "extra": []}), encoding="utf-8")
        self.assertEqual(self.call("build", "1", "--answers", str(answers_path)), 2)
        self.assertFalse((self.out / "requirements").exists())
        self.assertEqual(self.call("build", "2"), 1)
        self.assertFalse((self.out / "requirements").exists())

    def test_existing_artifacts_are_not_overwritten_unless_forced(self):
        self.assertEqual(self.call("build", "1"), 0)
        path = self.out / "requirements" / "1.json"
        path.write_text("keep me", encoding="utf-8")
        self.assertEqual(self.call("build", "1"), 1)
        self.assertEqual(path.read_text(encoding="utf-8"), "keep me")
        self.assertEqual(self.call("build", "1", "--force"), 0)
        self.assertNotEqual(path.read_text(encoding="utf-8"), "keep me")

    def test_missing_answers_are_explicitly_unknown(self):
        self.assertEqual(self.call("build", "1"), 0)
        doc = json.loads((self.out / "requirements" / "1.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["unknown_categories"], list(requirements.FIELDS))
        self.assertEqual(len(doc["generated_open_questions"]), len(requirements.FIELDS))

    def test_approval_hashes_both_files_and_edits_invalidate_status(self):
        self.assertEqual(self.call("build", "1"), 0)
        self.assertEqual(self.call("approve", "1"), 0)
        manifest = json.loads((self.out / "requirements" / "approvals.json").read_text(encoding="utf-8"))
        hashes = manifest["1"]["sha256"]
        for name in ("1.md", "1.json"):
            self.assertEqual(hashlib.sha256((self.out / "requirements" / name).read_bytes()).hexdigest(), hashes[name])
        self.assertEqual(self.call("status", "1"), 0)
        self.assertIn("approved", self.lines[-1])
        (self.out / "requirements" / "1.md").write_text("edited", encoding="utf-8")
        self.assertEqual(self.call("status", "1"), 0)
        self.assertIn("unapproved", self.lines[-1])
        current = json.loads((self.out / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual(current["items"]["1"]["status"], "pending")

    def test_answer_object_requires_exact_keys_and_string_lists(self):
        bad_answers = self.out / "bad.json"
        payload = {
            "must_have": [1], "optional": [], "out_of_scope": [],
            "acceptance_criteria": [], "open_questions": [],
        }
        bad_answers.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.call("build", "1", "--answers", str(bad_answers)), 2)
        self.assertFalse((self.out / "requirements").exists())

    def test_duplicate_json_keys_are_rejected_before_output(self):
        path = self.out / "duplicate.json"
        path.write_text('{"must_have":[],"must_have":[],"optional":[],"out_of_scope":[],"acceptance_criteria":[],"open_questions":[]}', encoding="utf-8")
        self.assertEqual(self.call("build", "1", "--answers", str(path)), 2)
        self.assertFalse((self.out / "requirements").exists())

    def test_explicit_build_copies_full_text_while_it_is_available(self):
        from kimeru import fulltext
        fulltext.save(self.out, self.case["key"], {"text": "Complete source body", "thread": []},
                      material={"description": "Complete source description"})
        self.assertEqual(self.call("build", "1"), 0)
        doc = json.loads((self.out / "requirements" / "1.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["source_material"]["text"], "Complete source body")
        self.assertEqual(doc["source_material"]["description"], "Complete source description")
        self.assertTrue(doc["sources"][0]["full_text_included"])

    def test_case_without_material_event_uses_recorded_event_and_summary(self):
        self.case["revision"] = 3
        self.case["record"].pop("material_event")
        self.case["record"]["event"] = {
            "kind": "ado.workitem", "id": "42", "title": "Export report",
            "description": "Export omits the selected date range.",
            "repro_steps": "Choose range; export CSV.",
        }
        self.case["record"]["summary"] = "CSV export omits selected date range"
        (self.out / "approvals.json").write_text(json.dumps({"next": 2, "items": {"1": self.case}}), encoding="utf-8")
        self.assertEqual(self.call("build", "1"), 0)
        doc = json.loads((self.out / "requirements" / "1.json").read_text(encoding="utf-8"))
        self.assertIn("Export omits the selected date range.", doc["source_material"]["description"])
        self.assertIn("Choose range; export CSV.", doc["source_material"]["repro_steps"])
        self.assertEqual(doc["source_material"]["summary"], "CSV export omits selected date range")
        self.assertEqual(doc["sources"][0]["revision"], 3)
        self.assertTrue(any("record.event.description" in ref for ref in doc["sources"][0]["read_refs"]))
        self.assertEqual(doc["sources"][0]["revision_ref"], 'approval items["1"].revision')

    def test_corrupted_or_wrong_case_artifact_cannot_be_approved_or_reported_approved(self):
        self.assertEqual(self.call("build", "1"), 0)
        jpath = self.out / "requirements" / "1.json"
        jpath.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.call("approve", "1"), 2)
        self.assertEqual(self.call("status", "1"), 2)
        self.assertFalse((self.out / "requirements" / "approvals.json").exists())
        self.assertEqual(self.call("build", "1", "--force"), 0)
        doc = json.loads(jpath.read_text(encoding="utf-8"))
        doc["case"] = 9
        jpath.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(self.call("approve", "1"), 2)
        self.assertFalse((self.out / "requirements" / "approvals.json").exists())

    def test_malformed_answers_shape_cannot_be_approved_or_reported_approved(self):
        self.assertEqual(self.call("build", "1"), 0)
        path = self.out / "requirements" / "1.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["answers"]["must_have"] = "not a list"
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(self.call("approve", "1"), 2)
        self.assertEqual(self.call("status", "1"), 2)
        self.assertFalse((self.out / "requirements" / "approvals.json").exists())

    def test_concurrent_builds_for_same_case_only_one_can_create_artifacts(self):
        barrier = threading.Barrier(2)
        calls_lock = threading.Lock()
        calls = 0
        original_case = requirements._case

        def synchronized_case(out, number):
            nonlocal calls
            with calls_lock:
                calls += 1
                synchronize = calls <= 2
            if synchronize:
                barrier.wait(timeout=3)
            value = original_case(out, number)
            return value

        def build():
            lines = []
            return requirements.dispatch(["build", "1"], self.out, lines.append)

        with patch.object(requirements, "_case", synchronized_case):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = (pool.submit(build), pool.submit(build))
                codes = sorted(f.result(timeout=5) for f in results)
        self.assertEqual(codes, [0, 1])

    def test_concurrent_approvals_merge_manifest_entries(self):
        for number in (1, 2, 3):
            item = json.loads(json.dumps(self.case))
            item["record"]["event_id"] = str(41 + number)
            items = json.loads((self.out / "approvals.json").read_text(encoding="utf-8"))["items"]
            items[str(number)] = item
            (self.out / "approvals.json").write_text(json.dumps({"next": 4, "items": items}), encoding="utf-8")
            self.assertEqual(requirements.dispatch(["build", str(number)], self.out, lambda _: None), 0)

        original_manifest = requirements._manifest

        def slow_manifest(path):
            value = original_manifest(path)
            time.sleep(0.02)
            return value

        def approve(number):
            lines = []
            code = requirements.dispatch(["approve", str(number)], self.out, lines.append)
            return code, lines

        with patch.object(requirements, "_manifest", slow_manifest):
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(approve, (1, 2, 3)))
        self.assertEqual([code for code, _ in results], [0, 0, 0], results)
        manifest = json.loads((self.out / "requirements" / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual(set(manifest), {"1", "2", "3"})


if __name__ == "__main__":
    unittest.main()
