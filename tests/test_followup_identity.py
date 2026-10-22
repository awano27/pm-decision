"""A follow-up can revise one pending case only with an explicit source-event link."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import cli, graph, notify, plan
from kimeru.backends import StubBackend


ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)


def seed(out, *, status="pending", key="g:old:n", old_id="old", graph_name="g", chat="c", author="A"):
    ap = notify.Approvals(out)
    record = {"graph": graph_name, "event_id": old_id, "node": "n", "outcome": "advise",
              "needs_human": True, "summary": "original", "actions": [{"type": "ado.comment", "text": "draft"}],
              "event": {"kind": "teams.chat", "id": old_id, "chat_id": chat, "author": author}}
    ap.data["items"]["1"] = {"key": key, "revision": 3, "status": status, "record": record,
                              "delivery_unknown": True, "delivery_unknown_part": "post",
                              "delivery_unknown_at": "2026-10-01T00:00:00Z"}
    ap.save()
    return record


def incoming(*, link="old", event_id="new", graph_name="g", chat="c", author="A", node="n", no_action=False):
    ev = {"kind": "teams.chat", "id": event_id, "chat_id": chat, "author": author,
          "text": "補足", "supersedes_event_id": link}
    actions = [{"type": "log.only"}] if no_action else [{"type": "ado.comment", "text": "new draft"}]
    result = {"graph": graph_name, "event_id": event_id, "node": node,
              "outcome": "decide" if no_action else "advise", "needs_human": not no_action,
              "event": ev, "actions": actions}
    return ev, result


class TestFollowupIdentity(unittest.TestCase):
    def test_unlinked_thanks_cannot_replace_pending_record(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            seed(out)
            before = json.loads((out / "approvals.json").read_text(encoding="utf-8"))
            ev, res = incoming(link=None, no_action=True)
            self.assertFalse(cli._merge_locked(out, ev, res, notify))
            self.assertEqual(json.loads((out / "approvals.json").read_text(encoding="utf-8")), before)

    def test_explicit_link_updates_only_target_and_keeps_unknown_delivery_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            seed(out)
            ev, res = incoming()
            self.assertTrue(cli._merge_locked(out, ev, res, notify))
            item = notify.Approvals(out).data["items"]["1"]
            self.assertEqual(item["revision"], 4)
            self.assertEqual(item["record"]["event_id"], "new")
            self.assertTrue(item["delivery_unknown"])
            self.assertEqual(item["delivery_unknown_revision"], 3)
            self.assertEqual(item["delivery_unknown_part"], "post")

    def test_explicitly_linked_no_action_can_replace_pending_case(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            seed(out)
            ev, res = incoming(no_action=True)
            self.assertTrue(cli._merge_locked(out, ev, res, notify))
            item = notify.Approvals(out).data["items"]["1"]
            self.assertEqual(item["revision"], 4)
            self.assertEqual(item["record"]["event_id"], "new")
            self.assertEqual(item["record"]["actions"], [{"type": "log.only"}])

    def test_old_link_is_stale_after_case_advances_to_new_event_id(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            seed(out)
            first_ev, first_result = incoming(event_id="current")
            self.assertTrue(cli._merge_locked(out, first_ev, first_result, notify))
            before = json.loads((out / "approvals.json").read_text(encoding="utf-8"))
            stale_ev, stale_result = incoming(link="old", event_id="later")
            self.assertFalse(cli._merge_locked(out, stale_ev, stale_result, notify))
            self.assertEqual(json.loads((out / "approvals.json").read_text(encoding="utf-8")), before)

    def test_empty_whitespace_and_non_string_links_are_rejected(self):
        for link in ("", "   ", 42, None):
            with self.subTest(link=link), tempfile.TemporaryDirectory() as d:
                out = Path(d)
                seed(out)
                before = json.loads((out / "approvals.json").read_text(encoding="utf-8"))
                ev, res = incoming(link=link, no_action=True)
                self.assertFalse(cli._merge_locked(out, ev, res, notify))
                self.assertEqual(json.loads((out / "approvals.json").read_text(encoding="utf-8")), before)

    def test_event_id_fallback_is_used_only_when_record_field_is_absent(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            seed(out)
            ap = notify.Approvals(out)
            del ap.data["items"]["1"]["record"]["event_id"]
            ap.save()
            ev, res = incoming(link="old")
            self.assertTrue(cli._merge_locked(out, ev, res, notify))
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            seed(out)
            ap = notify.Approvals(out)
            ap.data["items"]["1"]["record"]["event_id"] = "current"
            ap.save()
            ev, res = incoming(link="old")
            self.assertFalse(cli._merge_locked(out, ev, res, notify))

    def test_missing_wrong_stale_mismatched_and_ambiguous_links_preserve_state(self):
        variants = [
            {"link": None}, {"link": "other"}, {"link": "old", "graph_name": "other"},
            {"link": "old", "chat": "other"}, {"link": "old", "author": "B"},
        ]
        for values in variants:
            with self.subTest(values=values), tempfile.TemporaryDirectory() as d:
                out = Path(d)
                seed(out)
                before = json.loads((out / "approvals.json").read_text(encoding="utf-8"))
                ev, res = incoming(**values)
                self.assertFalse(cli._merge_locked(out, ev, res, notify))
                self.assertEqual(json.loads((out / "approvals.json").read_text(encoding="utf-8")), before)
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            seed(out)
            ap = notify.Approvals(out)
            ap.data["items"]["2"] = json.loads(json.dumps(ap.data["items"]["1"]))
            ap.data["items"]["2"]["key"] = "g:duplicate:n"
            ap.save()
            before = json.loads((out / "approvals.json").read_text(encoding="utf-8"))
            ev, res = incoming()
            self.assertFalse(cli._merge_locked(out, ev, res, notify))
            self.assertEqual(json.loads((out / "approvals.json").read_text(encoding="utf-8")), before)

    def test_process_keeps_material_request_when_same_author_says_thanks(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            # Use the checked-in Teams graph and offline backend through the actual process path.
            request = {"kind": "teams.chat", "id": "req", "chat_id": "chat", "author": "A",
                       "text": "これは何ですか", "mentions_me": True}
            thanks = {"kind": "teams.chat", "id": "thanks", "chat_id": "chat", "author": "A",
                      "text": "ありがとうございます", "mentions_me": True}
            first = cli.process(request, GRAPHS, StubBackend(), out, PBS, dedup=True)
            self.assertEqual(len(first), 1)
            self.assertTrue(first[0]["needs_human"])
            class Bridge:
                def post(self, text, send):
                    return {"ok": True, "sent": False}
            notify.notify(out, Bridge(), send=False)
            before = json.loads((out / "approvals.json").read_text(encoding="utf-8"))
            with mock.patch("kimeru.notify.PowerShellBridge") as bridge:
                bridge.return_value.post.return_value = {"ok": True}
                second = cli.process(thanks, GRAPHS, StubBackend(), out, PBS, dedup=True)
            self.assertFalse(second[0].get("merged"))
            after = json.loads((out / "approvals.json").read_text(encoding="utf-8"))
            self.assertEqual(after, before)
            self.assertEqual(len(after["items"]), 1)
            self.assertEqual(after["items"]["1"]["record"]["event"]["id"], "req")

    def test_independent_same_node_request_gets_a_separate_queued_case(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            class Bridge:
                def post(self, text, send):
                    return {"ok": True, "sent": False}

            first = {"kind": "teams.chat", "id": "one", "chat_id": "chat", "author": "A",
                     "text": "これは何ですか", "mentions_me": True}
            second = {**first, "id": "two"}
            cli.process(first, GRAPHS, StubBackend(), out, PBS, dedup=True)
            notify.notify(out, Bridge(), send=False)
            before = json.loads((out / "approvals.json").read_text(encoding="utf-8"))
            cli.process(second, GRAPHS, StubBackend(), out, PBS, dedup=True)
            after = json.loads((out / "approvals.json").read_text(encoding="utf-8"))
            self.assertEqual(after, before)
            queued = [json.loads(line) for line in (out / "queue.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertIn("two", {item["event_id"] for item in queued})
            notify.notify(out, Bridge(), send=False)
            items = notify.Approvals(out).data["items"]
            self.assertEqual(len(items), 2)
            self.assertEqual({item["record"]["event_id"] for item in items.values()}, {"one", "two"})


if __name__ == "__main__":
    unittest.main()
