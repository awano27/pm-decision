"""`kimeru demo` never clears real records: it writes to out/demo by default, stops (exit 2) when the folder holds
records that are not its own, moves them aside only with --fresh, checks the judge before touching anything, and
does not read an earlier approvals.json.bak back as its own state."""
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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from kimeru import cli, config, demo

ROOT = Path(__file__).resolve().parent.parent
ALERT = str(ROOT / "examples" / "monitor_alert.json")


def snapshot(folder):
    folder = Path(folder)
    if not folder.exists():
        return None
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.work = Path(self.dir.name) / "work"
        self.work.mkdir()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_") and k not in config.SECRET_ENV}
        clean["KIMERU_STATE_DIR"] = str(Path(self.dir.name) / "state")
        clean["KIMERU_TOAST"] = "0"
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        self.cwd = os.getcwd()
        os.chdir(self.work)   # the default --out is relative to the working folder

    def tearDown(self):
        os.chdir(self.cwd)
        self.env.stop()
        self.dir.cleanup()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def demo(self, *extra):
        return self.cli("--backend", "stub", *extra, "demo", "--pace", "0")

    def real_records(self, out):
        rc, _, err = self.cli("--backend", "stub", "--out", str(out), "run", ALERT)
        self.assertEqual(rc, 0, err)
        self.assertTrue((Path(out) / "decisions.jsonl").exists())


class TestTheDefaultFolder(Base):
    def test_the_demo_writes_to_out_demo_and_leaves_out_alone(self):
        self.real_records("out")
        before = snapshot(self.work / "out")
        rc, text, err = self.demo()
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.work / "out" / "demo" / "decisions.jsonl").exists())
        self.assertTrue((self.work / "out" / "demo" / "report.html").exists())
        after = {k: v for k, v in snapshot(self.work / "out").items() if not k.startswith("demo")}
        self.assertEqual(after, before)
        self.assertIn(str(Path("out") / "demo" / "report.html"), text)

    def test_a_second_demo_in_its_own_folder_starts_again_from_one(self):
        self.assertEqual(self.demo()[0], 0)
        self.assertEqual(self.demo()[0], 0)
        ap = json.loads((self.work / "out" / "demo" / "approvals.json").read_text(encoding="utf-8"))
        first = json.loads(json.dumps(ap))
        self.assertEqual(self.demo()[0], 0)
        again = json.loads((self.work / "out" / "demo" / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(again["items"]), sorted(first["items"]))   # same numbers each time, nothing carried over

    def test_other_commands_keep_out_as_their_default(self):
        self.real_records(self.work / "out")
        self.assertFalse((self.work / "out" / "demo").exists())


class TestRealRecordsAreKept(Base):
    def test_a_folder_with_real_records_stops_the_demo_untouched(self):
        self.real_records("rec")
        before = snapshot(self.work / "rec")
        rc, _, err = self.demo("--out", "rec")
        self.assertEqual(rc, 2)
        self.assertIn("消さずに止めました", err)
        self.assertIn("--fresh", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(snapshot(self.work / "rec"), before)

    def test_fresh_moves_them_aside_and_deletes_nothing(self):
        self.real_records("rec")
        before = snapshot(self.work / "rec")
        rc, _, err = self.cli("--backend", "stub", "--out", "rec", "demo", "--pace", "0", "--fresh")
        self.assertEqual(rc, 0, err)
        kept = [p for p in (self.work / "rec").iterdir() if p.name.startswith("before-demo-")]
        self.assertEqual(len(kept), 1)
        moved = snapshot(kept[0])
        for name, digest in moved.items():
            self.assertEqual(before[name], digest, name)
        self.assertIn("decisions.jsonl", moved)

    def test_an_old_backup_alone_counts_as_a_record_and_is_not_read_back(self):
        rec = self.work / "rec"
        rec.mkdir()
        bak = {"next": 7, "items": {"5": {"status": "approved", "exec": {"0": {"state": "done"}}}}}
        (rec / "approvals.json.bak").write_text(json.dumps(bak), encoding="utf-8")
        rc, _, err = self.demo("--out", "rec")
        self.assertEqual(rc, 2, err)
        self.assertEqual(json.loads((rec / "approvals.json.bak").read_text(encoding="utf-8")), bak)
        rc, _, err = self.cli("--backend", "stub", "--out", "rec", "demo", "--pace", "0", "--fresh")
        self.assertEqual(rc, 0, err)
        ap = json.loads((rec / "approvals.json").read_text(encoding="utf-8"))
        self.assertNotIn("5", ap["items"])   # the demo's own approvals, not the backup's
        self.assertEqual(min(int(k) for k in ap["items"]), 1)

    def test_a_command_that_writes_real_records_takes_the_folder_back(self):
        self.assertEqual(self.demo()[0], 0)
        demo_dir = self.work / "out" / "demo"
        self.assertTrue((demo_dir / demo.MARK).exists())
        self.real_records(demo_dir)
        self.assertFalse((demo_dir / demo.MARK).exists())
        before = snapshot(demo_dir)
        self.assertEqual(self.demo()[0], 2)
        self.assertEqual(snapshot(demo_dir), before)

    def test_replay_touches_no_record(self):
        self.real_records("rec")
        before = snapshot(self.work / "rec")
        recording = self.work / "talk.json"
        self.assertEqual(self.cli("--backend", "stub", "--out", "d", "demo", "--pace", "0", "--record", str(recording))[0], 0)
        rc, _, err = self.cli("--out", "rec", "demo", "--pace", "0", "--replay", str(recording))
        self.assertEqual(rc, 0, err)
        after = {k: v for k, v in snapshot(self.work / "rec").items() if k != "report.html"}
        self.assertEqual(after, before)


class TestAnUnreachableJudgeTouchesNothing(Base):
    def setUp(self):
        super().setUp()
        os.environ["KIMERU_KEV_URL"] = "http://127.0.0.1:1/v1"   # a closed port on this PC: refused at once

    def test_nothing_is_created_and_the_reason_is_one_line(self):
        rc, _, err = self.cli("--backend", "kev", "demo", "--pace", "0")
        self.assertEqual(rc, 1)
        self.assertNotIn("Traceback", err)
        self.assertIn("判断モデルに接続できません", err)
        self.assertIn("--backend stub", err)
        self.assertFalse((self.work / "out").exists(), "no folder may be made before the judge answers")

    def test_real_records_stay_exactly_as_they_were(self):
        self.real_records("rec")
        before = snapshot(self.work / "rec")
        rc, _, _ = self.cli("--backend", "kev", "--out", "rec", "demo", "--pace", "0", "--fresh")
        self.assertEqual(rc, 1)
        self.assertEqual(snapshot(self.work / "rec"), before)


if __name__ == "__main__":
    unittest.main()
