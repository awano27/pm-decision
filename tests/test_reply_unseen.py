"""A decision reply of a post that has left the screen is never an approval, and it is not dropped silently: kimeru says so once and posts
the item again. A test post's reply, and a visible number, are as before. Every Teams is a fake."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import notify
from tests.test_fix16 import TimelineBridge, state_with_item


def collect(out, tl, real=True):
    b = TimelineBridge(tl)
    return list(notify.collect(out, b, real=real, send=False)), b


class TestUnseenReply(unittest.TestCase):
    def test_unseen_number_is_told_once_and_posted_again_without_approval(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "s"
            state_with_item(out)
            ch, _ = collect(out, ["R:OK 1"])                       # the post of #1 is off the screen
            self.assertEqual([(c["id"], c["status"]) for c in ch], [(1, "reply_ignored")])
            it = notify.Approvals(out).data["items"]["1"]
            self.assertEqual(it["status"], "pending")
            self.assertFalse(it["posted"])
            outbox = notify.Approvals(out).data.get("outbox") or []
            self.assertEqual(len([t for t in outbox if "返信を読めませんでした" in t]), 1)
            self.assertTrue(outbox[0].startswith("[kimeru 通知] #1 への返信を読めませんでした"))
            notify.notify(out, TimelineBridge([]), send=True)      # the item is posted again
            self.assertTrue(notify.Approvals(out).data["items"]["1"]["posted"])
            ch, _ = collect(out, ["R:OK 1"])                       # the same reply again: no second notice
            self.assertEqual(ch, [])
            self.assertEqual(len([t for t in (notify.Approvals(out).data.get("outbox") or []) if "返信を読めませんでした" in t]), 1)

    def test_a_held_item_is_told_about_but_not_posted_again(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "s"
            state_with_item(out)
            ap = notify.Approvals(out)
            ap.data["items"]["1"].update(status="held", held_until="2099-01-01T09:00")
            ap.save()
            ch, _ = collect(out, ["R:OK 1"])                       # the post of #1 is off the screen
            self.assertEqual([(c["id"], c["status"]) for c in ch], [(1, "reply_ignored")])
            it = notify.Approvals(out).data["items"]["1"]
            self.assertEqual(it["status"], "held")
            self.assertTrue(it["posted"])                          # not reset: it is not posted before its day
            self.assertEqual(it["held_until"], "2099-01-01T09:00")
            outbox = notify.Approvals(out).data.get("outbox") or []
            self.assertEqual(len([t for t in outbox if "返信を読めませんでした" in t]), 1)
            self.assertNotIn("もう一度投稿します", outbox[0])
            ch, _ = collect(out, ["R:OK 1"])                       # told once
            self.assertEqual(ch, [])

    def test_visible_number_is_approved_as_before(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "s"
            state_with_item(out)
            ch, _ = collect(out, ["P:1", "R:OK 1"])
            self.assertEqual([(c["id"], c["status"]) for c in ch], [(1, "approved")])

    def test_test_post_reply_and_test_mode_are_not_told(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "s"
            state_with_item(out)
            ch, _ = collect(out, ["T:1", "R:OK 1"])
            self.assertEqual(ch, [])
            self.assertTrue(notify.Approvals(out).data["items"]["1"]["posted"])
            with mock.patch.dict(os.environ, {notify.TEST_ENV: "1"}):
                ch, _ = collect(out, ["R:OK 1"])
            self.assertEqual(ch, [])

    def test_a_failing_notice_does_not_stop_the_cycle(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "s"
            state_with_item(out)
            with mock.patch.object(notify, "make_post", return_value=mock.Mock(side_effect=OSError("x"))):
                ch, _ = collect(out, ["R:OK 1"])
            self.assertEqual([(c["id"], c["status"]) for c in ch], [(1, "reply_ignored")])


if __name__ == "__main__":
    unittest.main()
