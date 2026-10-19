"""eval/day_run.py on a console that is cp932 (no PYTHONIOENCODING): a post with a warning sign is printed with `?` in its place,
and the day goes on to its last line; it does not stop with UnicodeEncodeError."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def _load_day_run():
    spec = importlib.util.spec_from_file_location("day_run_console_under_test", ROOT / "eval" / "day_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestADayRunOnACp932Console(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = self.dir.name
        self.env = mock.patch.dict(os.environ, clean, clear=True)   # main sets KIMERU_WRITER; the patch puts the environment back
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def test_a_post_with_a_warning_sign_is_printed_with_a_question_mark_and_the_day_goes_on(self):
        day_run = _load_day_run()
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="cp932")   # the console of a PC without PYTHONIOENCODING
        argv = ["day_run.py", "--backend", "stub", "--writer", "cmd", "--n", "1", "--out", str(Path(self.dir.name) / "day")]
        with mock.patch.object(sys, "stdout", console), mock.patch.object(sys, "argv", argv):
            day_run.main()   # the posts with a warning sign are printed here; before the fix this raised UnicodeEncodeError
        console.flush()
        text = raw.getvalue().decode("cp932")
        self.assertIn("cycle 1:", text)
        self.assertTrue(any(l.startswith("  | ?") and "定型文" in l for l in text.splitlines()), text)
        self.assertIn("judge+draft time", text)   # the last line of main: the day was not stopped by the print


class TestTheDefaultWriterIsTheTemplate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = self.dir.name
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def _run(self, extra):
        day_run = _load_day_run()
        argv = ["day_run.py", "--backend", "stub", "--n", "1", "--out", str(Path(self.dir.name) / "day")] + extra
        asked = []
        real = day_run.writer.get_writer

        def spy(name=None):
            asked.append(name)
            return real(name)

        with mock.patch.object(day_run.writer, "get_writer", side_effect=spy), \
                mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "argv", argv):
            day_run.main()
        return asked

    def test_default_writer_argument_is_empty(self):
        import argparse
        day_run = _load_day_run()
        captured = {}
        real_parse = argparse.ArgumentParser.parse_args

        class Stop(Exception):
            pass

        def grab(self, args=None, namespace=None):
            captured["writer"] = real_parse(self, args, namespace).writer
            raise Stop()

        with mock.patch.object(argparse.ArgumentParser, "parse_args", grab), \
                mock.patch.object(sys, "argv", ["day_run.py", "--backend", "stub"]):
            with self.assertRaises(Stop):
                day_run.main()
        self.assertEqual(captured["writer"], "")

    def test_no_cli_writer_is_constructed_by_default(self):
        from kimeru import writer
        with mock.patch.dict(writer.WRITERS, {k: mock.Mock(side_effect=AssertionError("a CLI writer was built")) for k in writer.WRITERS}):
            asked = self._run([])
        self.assertEqual(asked[0], "")   # main asks for the template writer; get_writer("") is None
        self.assertFalse([a for a in asked if a], asked)   # nothing else named a CLI writer

    def test_the_default_writer_object_is_none(self):
        from kimeru import writer
        self.assertIsNone(writer.get_writer(""))

    def test_copilot_is_still_selectable_by_name(self):
        from kimeru import writer
        with mock.patch.dict(writer.WRITERS, {"copilot": mock.Mock(return_value="W")}):
            self.assertEqual(writer.get_writer("copilot"), "W")


if __name__ == "__main__":
    unittest.main()
