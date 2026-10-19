"""T7: the week digest says how many of the decided draft checks were OK'd, NG'd and sent back (numbers only), and how long the
items that wait for an answer have waited (median and longest)."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kimeru import notify, stats
from tests.test_notify import FakeBridge
from tests.test_post_kind import DRAFT, queue_of

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
JUDGE = {"name": "Kev", "model": "m-1"}


def build(out, ages_h, decided):
    """One draft item per entry of ages_h (hours since its post); `decided` {number: [statuses]} go to approvals.log.jsonl."""
    queue_of(out, *[{**DRAFT, "event_id": str(i), "judge": JUDGE} for i in range(1, len(ages_h) + 1)])
    notify.notify(out, FakeBridge(), send=True)
    ap = notify.Approvals(out)
    for n, h in enumerate(ages_h, 1):
        notify.measurement_for(ap.data["items"][str(n)])["posted_at"] = (NOW - timedelta(hours=h)).isoformat(timespec="milliseconds")
    ap.save()
    at = (NOW - timedelta(days=1)).isoformat(timespec="seconds")
    rows = [{"at": at, "key": ap.data["items"][str(n)]["key"], "id": n, "status": st} for n, sts in decided.items() for st in sts]
    (Path(out) / "approvals.log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


class TestDraftUse(unittest.TestCase):
    def test_the_rates_count_each_decided_draft_once(self):
        with tempfile.TemporaryDirectory() as d:
            build(d, [1, 1, 1, 1, 1], {1: ["approved"], 2: ["redrafted", "approved"], 3: ["rejected"], 4: ["expired"]})
            wk = stats.collect(d, NOW)["week"]
            self.assertEqual(wk["draft_use"], {"drafts": 4, "ok": 2, "ng": 1, "fix": 1})
            text = stats.render(stats.collect(d, NOW))
            self.assertIn("下書きの使われ方: OK 2 件（50%）・NG 1 件（25%）・修正 1 件（25%）（決まった下書きの件 4 件・参考）", text)

    def test_no_decided_draft_is_unknown_and_not_a_division_error(self):
        with tempfile.TemporaryDirectory() as d:
            build(d, [1], {})
            self.assertIn("下書きの使われ方: 不明（決まった下書きの件 0 件）", stats.render(stats.collect(d, NOW)))

    def test_the_waiting_age_is_the_median_and_the_longest_of_the_undecided_items(self):
        with tempfile.TemporaryDirectory() as d:
            build(d, [2, 10, 100], {})
            self.assertEqual(stats.collect(d, NOW)["week"]["waiting_age"], {"n": 3, "median": 10 * 3600.0, "max": 100 * 3600.0})
            self.assertIn("確認待ちの古さ: 中央値 10 時間 / 最長 4.2 日（3 件）", stats.render(stats.collect(d, NOW)))

    def test_a_decided_item_does_not_count_as_waiting(self):
        with tempfile.TemporaryDirectory() as d:
            build(d, [2, 10], {})
            ap = notify.Approvals(d)
            ap.data["items"]["2"]["status"] = "approved"
            ap.save()
            self.assertEqual(stats.collect(d, NOW)["week"]["waiting_age"]["n"], 1)

    def test_no_text_of_a_draft_is_in_the_new_lines(self):
        with tempfile.TemporaryDirectory() as d:
            build(d, [3], {1: ["approved"]})
            text = stats.render(stats.collect(d, NOW))
            new = [l for l in text.splitlines() if l.startswith(("下書きの使われ方", "確認待ちの古さ"))]
            self.assertEqual(len(new), 2)
            self.assertNotIn("承知しました", "\n".join(new))


if __name__ == "__main__":
    unittest.main()
