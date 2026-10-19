"""T9 (e): every call of a writer is counted (calls and seconds, one line a day in the state folder) and the week digest shows the
two numbers. Numbers only; the script writer is not an AI call."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru import stats, writer
from tests.test_writer import PromptSpy, reply_res

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
EVENT = {"id": "t1", "author": "Sato", "text": "いつ頃お返事をもらえますか"}


class TestWriterUse(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"KIMERU_STATE_DIR": self.state.name})
        self.env.start()
        self.file = Path(self.state.name) / writer.CALLS_FILE

    def tearDown(self):
        self.env.stop()
        self.state.cleanup()

    def rows(self):
        return json.loads(self.file.read_text(encoding="utf-8")) if self.file.exists() else {}

    def test_one_call_is_one_count_and_the_script_writer_is_not_counted(self):
        writer.apply(reply_res(), EVENT, PromptSpy([{"a1": "確認してお返事します。"}]))
        (calls, seconds), = self.rows().values()
        self.assertEqual(calls, 1)
        self.assertGreaterEqual(seconds, 0)
        spy = PromptSpy([{"a1": "確認してお返事します。"}])
        spy.NAME = "script"
        writer.apply(reply_res(), EVENT, spy)
        self.assertEqual(list(self.rows().values())[0][0], 1)

    def test_a_second_try_is_a_second_call(self):
        spy = PromptSpy([{"a1": "確認させていただきます。ご連絡させていただきます。"}, {"a1": "確認してお返事します。"}])
        writer.apply(reply_res(), EVENT, spy)
        self.assertEqual(len(spy.prompts), 2)
        self.assertEqual(list(self.rows().values())[0][0], 2)

    def test_a_failure_to_record_does_not_touch_the_draft(self):
        res = reply_res()
        with mock.patch.object(writer.fsutil, "write_atomic", side_effect=OSError("disk")):
            writer.apply(res, EVENT, PromptSpy([{"a1": "確認してお返事します。"}]))
        self.assertEqual(res["actions"][0]["text"], "確認してお返事します。")

    def test_the_week_shows_the_calls_and_seconds_of_its_days_only(self):
        day = lambda n: (NOW - timedelta(days=n)).astimezone().date().isoformat()
        self.file.write_text(json.dumps({day(1): [3, 40.2], day(2): [2, 20.0], day(30): [9, 999.0]}), encoding="utf-8")
        with tempfile.TemporaryDirectory() as d:
            wk = stats.collect(d, NOW)["week"]
            self.assertEqual(wk["writer_use"], {"calls": 5, "seconds": 60.2})
            self.assertIn("writer の呼び出し: 5 回・合計 60 秒", stats.render(stats.collect(d, NOW)))

    def test_no_record_is_zero_calls(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIn("writer の呼び出し: 0 回", stats.render(stats.collect(d, NOW)))


if __name__ == "__main__":
    unittest.main()
