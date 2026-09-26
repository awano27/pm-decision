import json
import tempfile
import unittest
from pathlib import Path

from kimeru import graph, notify, plan, writer
from kimeru.backends import StubBackend
from kimeru.cli import process

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)
MSG = {"type": "message", "chatId": "19:c1", "id": "m1", "createdDateTime": "2026-10-01T09:00:00Z",
       "from": {"user": {"displayName": "佐藤"}},
       "body": {"contentType": "text", "content": "来月のリリース日をずらすか判断をお願いします"}}


class FakeWriter:
    NAME = "fake"

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def draft(self, res, event, instruction=None):
        self.calls.append((res["node"], event.get("text"), instruction))
        if self.fail:
            raise RuntimeError("writer down")
        tasks = {a["title"]: f"説明: {a['title']}" for a in res["actions"] if a.get("type") == "ado.create"}
        return {"reply": "佐藤さん、承知しました。" + (f"（{instruction}）" if instruction else ""), "tasks": tasks}


class FakeTeams:
    def __init__(self):
        self.timeline, self.posts = [], []

    def post(self, text, send):
        self.posts.append(text)
        self.timeline.append("P:" + text.split("#", 1)[1].split("]", 1)[0])
        return {"ok": True}

    def read(self):
        return {"ok": True, "timeline": list(self.timeline)}


def rows(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


class TestWriter(unittest.TestCase):
    def test_drafted_reply_waits_for_approval_and_can_be_redrafted(self):
        with tempfile.TemporaryDirectory() as d:
            out, w, t = Path(d), FakeWriter(), FakeTeams()
            [res] = process(MSG, GRAPHS, StubBackend(), out, PBS, writer=w)
            self.assertEqual(res["outcome"], "decide")
            self.assertTrue(res["needs_human"])
            reply = [a for a in res["actions"] if a["type"] == "teams.reply"][0]
            self.assertEqual(reply["drafted_by"], "fake")
            self.assertTrue(reply["template_text"].startswith("受領しました"))
            self.assertNotIn("teams.reply", [e["action"]["type"] for e in res["executed"]])   # not sent yet
            self.assertTrue(all(a.get("description", "").startswith("説明: ")
                                for a in res["actions"] if a["type"] == "ado.create"))
            [q] = rows(out / "queue.jsonl")
            self.assertEqual([a["type"] for a in q["actions"]], ["teams.reply"])            # only the reply is held

            self.assertEqual(notify.notify(out, t, send=True), [1])
            self.assertIn("佐藤さん、承知しました。", t.posts[-1])
            self.assertIn("修正 1", t.posts[-1])

            t.timeline.append("R:修正 1 もっと短く")
            self.assertEqual(notify.collect(out, t, writer=w), [{"id": 1, "status": "redrafted", "instruction": "もっと短く"}])
            self.assertEqual(w.calls[-1][2], "もっと短く")
            self.assertEqual(notify.notify(out, t, send=True), [1])                          # same number, new text
            self.assertIn("（もっと短く）", t.posts[-1])

            t.timeline.append("R:OK 1")
            [ch] = notify.collect(out, t, writer=w)
            self.assertEqual(ch["status"], "approved")
            self.assertIn("（もっと短く）", ch["executed"][0]["action"]["text"])

    def test_writer_failure_keeps_template_and_old_flow(self):
        with tempfile.TemporaryDirectory() as d:
            [res] = process(MSG, GRAPHS, StubBackend(), Path(d), PBS, writer=FakeWriter(fail=True))
            self.assertFalse(res["needs_human"])
            self.assertIn("writer down", res["writer_error"])
            self.assertIn("teams.reply", [e["action"]["type"] for e in res["executed"]])
            self.assertFalse((Path(d) / "queue.jsonl").exists())

    def test_parse_and_selection(self):
        self.assertEqual(writer._parse('前置き {"reply": "はい"} 後ろ'), {"reply": "はい"})
        self.assertIsNone(writer._parse("JSON なし"))
        self.assertIsNone(writer.get_writer(""))
        self.assertEqual(writer.get_writer("claude").NAME, "claude")
        self.assertEqual(notify.parse_redraft("修正 3: 丁寧に"), ("3", "丁寧に"))
        self.assertIsNone(notify.parse_redraft("修正 3"))


if __name__ == "__main__":
    unittest.main()
