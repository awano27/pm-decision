"""`kimeru digest --week`: the line that counts a revision instruction written again on another item. The same item and rows
older than seven days are not counted; the words are never in the log, so only the fingerprint is counted."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kimeru import stats

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)


class TestRepeatedInstructionLine(unittest.TestCase):
    def test_a_fingerprint_written_again_on_another_item_is_counted_once_more_in_the_week_report(self):
        def row(days, key, sig):
            return {"at": (NOW - timedelta(days=days)).isoformat(), "key": key, "id": 1, "status": "redrafted",
                    "instruction_len": 6, "instruction_sig": sig}
        rows = [row(1, "item-a", "aaaaaaaaaaaa"),
                row(1, "item-b", "aaaaaaaaaaaa"),     # the same wording on another item: counted
                row(2, "item-b", "aaaaaaaaaaaa"),     # the same item again: not counted
                row(30, "item-c", "aaaaaaaaaaaa"),    # older than seven days: not counted
                row(1, "item-d", "bbbbbbbbbbbb"),     # written once: not counted
                row(1, "item-e", None)]               # no fingerprint: not counted
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "approvals.log.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            text = stats.week(out, now=NOW)
        self.assertIn("同じ修正指示の繰り返し（別の項目で）: 1 件", text)


if __name__ == "__main__":
    unittest.main()
