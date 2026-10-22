try:   # isolation from the real state folder, whichever way the tests are started
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

from kimeru import cli, demo, graph, plan
from kimeru.backends import StubBackend

ROOT = Path(__file__).resolve().parent.parent
SCENARIO = ROOT / "examples" / "demo_day.json"
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)


class TestDemoStatePreservation(unittest.TestCase):
    def _occupied(self, out):
        out.mkdir(parents=True)
        contents = {
            "decisions.jsonl": b"decision\n",
            "queue.jsonl": b"queue\n",
            "approvals.json": b"{\"approval\":true}\n",
            "other.bin": b"untouched\x00bytes",
        }
        for name, data in contents.items():
            (out / name).write_bytes(data)
        return contents

    def _snapshot(self, out):
        return {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}

    def test_direct_run_refuses_any_existing_state_without_changes(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            self._occupied(out)
            before = self._snapshot(out)
            with self.assertRaisesRegex(ValueError, "empty"):
                demo.run(SCENARIO, GRAPHS, StubBackend(), PBS, cli.process, out, pace=0)
            self.assertEqual(self._snapshot(out), before)

    def test_cli_default_output_refuses_existing_data_before_backend_or_warnings(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out" / "demo"
            before = self._occupied(out)
            old_cwd = Path.cwd()
            try:
                os.chdir(d)
                with mock.patch("kimeru.cli._backend", side_effect=AssertionError("backend loaded")):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        result = cli.main(["demo", "--pace", "0"])
            finally:
                os.chdir(old_cwd)
            self.assertNotEqual(result, 0)
            self.assertIn("empty --out", stderr.getvalue())
            self.assertEqual(self._snapshot(out), before)

    def test_cli_demo_works_with_fresh_explicit_output(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "fresh"
            with contextlib.redirect_stdout(io.StringIO()):
                result = cli.main(["--backend", "stub", "--out", str(out), "demo", "--pace", "0"])
            self.assertEqual(result, 0)
            self.assertTrue((out / "decisions.jsonl").exists())
            self.assertTrue((out / "report.html").exists())

    def test_demo_records_config_warning_after_success_but_refusal_preserves_state(self):
        warning = "synthetic invalid setting fell back to its default"
        with tempfile.TemporaryDirectory() as d:
            refused = Path(d) / "occupied"
            before = self._occupied(refused)
            fresh = Path(d) / "fresh"
            def apply_warning(_argv):
                cli.config.WARNINGS[:] = [warning]
                return [warning]

            with mock.patch.object(cli.config, "WARNINGS", []), \
                    mock.patch.object(cli.config, "apply", side_effect=apply_warning):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    result = cli.main(["--out", str(refused), "demo", "--pace", "0"])
                self.assertNotEqual(result, 0)
                self.assertEqual(self._snapshot(refused), before)
                self.assertFalse((refused / "warnings.jsonl").exists())
                with contextlib.redirect_stdout(io.StringIO()):
                    result = cli.main(["--backend", "stub", "--out", str(fresh), "demo", "--pace", "0"])
            self.assertEqual(result, 0)
            records = [json.loads(line) for line in (fresh / "warnings.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(record.get("config") == warning for record in records))

    def test_repeated_direct_demo_refuses_and_preserves_first_output(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                demo.run(SCENARIO, GRAPHS, StubBackend(), PBS, cli.process, out, pace=0)
            before = self._snapshot(out)
            with self.assertRaisesRegex(ValueError, "empty"):
                demo.run(SCENARIO, GRAPHS, StubBackend(), PBS, cli.process, out, pace=0)
            self.assertEqual(self._snapshot(out), before)

    def test_explicit_fresh_archives_known_records_and_leaves_unrelated_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            before = self._occupied(out)
            with contextlib.redirect_stdout(io.StringIO()):
                demo.run(SCENARIO, GRAPHS, StubBackend(), PBS, cli.process, out, pace=0, fresh=True)
            backups = list(out.glob("before-demo-*"))
            self.assertEqual(len(backups), 1)
            for name in ("decisions.jsonl", "queue.jsonl", "approvals.json"):
                self.assertEqual((backups[0] / name).read_bytes(), before[name])
            self.assertEqual((out / "other.bin").read_bytes(), before["other.bin"])
            self.assertFalse((backups[0] / "other.bin").exists())

    def test_failed_demo_keeps_partial_output_and_blocks_retry(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            calls = 0

            def fail_after_first(payload, graphs, backend, dest, playbooks, **kwargs):
                nonlocal calls
                calls += 1
                result = cli.process(payload, graphs, backend, dest, playbooks, **kwargs)
                if calls == 1:
                    raise RuntimeError("synthetic failure")
                return result

            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                with contextlib.redirect_stdout(io.StringIO()):
                    demo.run(SCENARIO, GRAPHS, StubBackend(), PBS, fail_after_first, out, pace=0)
            before = self._snapshot(out)
            self.assertTrue(before)
            with self.assertRaisesRegex(ValueError, "empty"):
                demo.run(SCENARIO, GRAPHS, StubBackend(), PBS, cli.process, out, pace=0)
            self.assertEqual(self._snapshot(out), before)

    def test_replay_refuses_nonempty_output_without_model_or_file_changes(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            before = self._occupied(out)
            rec = Path(d) / "record.json"
            rec.write_text(json.dumps({"lines": [], "report_html": "new report"}), encoding="utf-8")
            with mock.patch("kimeru.cli._backend", side_effect=AssertionError("backend loaded")):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    result = cli.main(["--out", str(out), "demo", "--replay", str(rec), "--pace", "0"])
            self.assertNotEqual(result, 0)
            self.assertIn("empty --out", stderr.getvalue())
            self.assertEqual(self._snapshot(out), before)

    def test_cli_replay_uses_fresh_output_without_loading_backend(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "fresh"
            rec = Path(d) / "record.json"
            rec.write_text(json.dumps({"lines": [["replayed", 0]], "report_html": "saved report"}), encoding="utf-8")
            with mock.patch("kimeru.cli._backend", side_effect=AssertionError("backend loaded")):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = cli.main(["--out", str(out), "demo", "--replay", str(rec), "--pace", "0"])
            self.assertEqual(result, 0)
            self.assertIn("replayed", stdout.getvalue())
            self.assertEqual((out / "report.html").read_text(encoding="utf-8"), "saved report")


if __name__ == "__main__":
    unittest.main()
