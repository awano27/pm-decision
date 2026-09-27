import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from kimeru import daily, graph, plan
from kimeru.backends import BackendUnavailable, KevBackend, StubBackend
from kimeru.cli import process

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)
MINUTES = {"title": "定例 2026-10-01", "date": "2026-10-01",
           "text": "- 決定: A とする\n- タスク: B を作る / 担当: 田中 / 期限: 10/3\n- ログ基盤を見直す"}


def lines(p):
    return [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


class Overloaded(StubBackend):
    """The model is overloaded until `up` is set."""
    up = False

    def ask(self, state, questions):
        if not self.up:
            raise BackendUnavailable("Kev HTTP 503: busy")
        return super().ask(state, questions)


class TestRetry(unittest.TestCase):
    def test_429_after_retries_is_retryable(self):
        err = urllib.error.HTTPError("u", 429, "busy", {}, io.BytesIO(b"slow down"))
        with mock.patch("urllib.request.urlopen", side_effect=err), mock.patch("time.sleep"):
            with self.assertRaises(BackendUnavailable):
                KevBackend().ask({"x": 1}, {"q": {"type": "noul", "instructions": "x"}})

    def test_400_is_not_retryable(self):
        err = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b"bad request"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as c:
                KevBackend().ask({"x": 1}, {"q": {"type": "noul", "instructions": "x"}})
            self.assertNotIsInstance(c.exception, BackendUnavailable)

    def test_same_file_twice_decides_once(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            process(MINUTES, GRAPHS, StubBackend(), out, PBS, dedup=True)
            process(MINUTES, GRAPHS, StubBackend(), out, PBS, dedup=True)
            self.assertEqual(len(lines(out / "decisions.jsonl")), 3)

    def test_half_done_file_is_kept_and_finished_later(self):
        with tempfile.TemporaryDirectory() as d:
            inbox, out = Path(d) / "inbox", Path(d) / "out"
            inbox.mkdir()
            (inbox / "m.json").write_text(json.dumps(MINUTES, ensure_ascii=False), encoding="utf-8")
            be = Overloaded()
            # lines 1-2 are decided by rule; line 3 needs the model, which is down
            self.assertEqual(daily.process_inbox(inbox, out, GRAPHS, be, PBS, process), 0)
            self.assertTrue((inbox / "m.json").exists())
            self.assertEqual(len(lines(out / "decisions.jsonl")), 2)
            be.up = True
            self.assertEqual(daily.process_inbox(inbox, out, GRAPHS, be, PBS, process), 1)
            self.assertEqual(len(lines(out / "decisions.jsonl")), 3)      # no line decided twice
            self.assertTrue((inbox / "done" / "m.json").exists())


if __name__ == "__main__":
    unittest.main()
