"""A post names the writer that was actually asked, says nothing is guessed when no writer is set, and the demo
describes recording as recording (not as execution)."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
    from .isolate import detail_of
except ImportError:
    import isolate  # noqa: F401
    from isolate import detail_of

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from kimeru import demo, graph, notify, plan
from kimeru.backends import StubBackend
from kimeru.cli import process

try:
    from .test_writer import MSG, FakeTeams, FakeWriter
except ImportError:
    from test_writer import MSG, FakeTeams, FakeWriter

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)


class Memo(FakeWriter):
    def draft(self, res, event, instruction=None):
        d = super().draft(res, event, instruction)
        d["memo"] = {"summary": "要点", "missing": ["期限"], "next": "確認する"}
        return d


def post_with(writer, detail=True):
    with tempfile.TemporaryDirectory() as d:
        out, t = Path(d), FakeTeams()
        process(MSG, GRAPHS, StubBackend(), out, PBS, writer=writer)
        notify.notify(out, t, send=True)
        return detail_of(out) if detail else t.posts[0]


class TestTheWriterIsNamedAsItIs(unittest.TestCase):
    def test_claude_grok_and_a_cli_are_not_called_copilot(self):
        for name, label in (("claude", "Claude"), ("grok", "Grok"), ("cmd", "文面 CLI")):
            w = Memo()
            w.NAME = name
            post = post_with(w)
            self.assertIn(f"{label} のメモ:", post, name)
            self.assertNotIn("Copilot", post, name)

    def test_copilot_is_still_called_copilot(self):
        w = Memo()
        w.NAME = "copilot"
        self.assertIn("Copilot のメモ:", post_with(w))

    def test_a_failure_names_the_writer(self):
        w = FakeWriter(fail=True)
        w.NAME = "claude"
        post = post_with(w, detail=False)   # the short post says it too
        self.assertIn("⚠ Claude の下書きを作れなかったため定型文です", post)
        self.assertIn("⚠ Claude の下書きを作れなかったため定型文です", post_with(w))
        self.assertNotIn("Copilot", post)


class TestNoWriter(unittest.TestCase):
    def test_nothing_is_shown_as_unknown_and_approval_is_not_called_execution(self):
        rec = {"graph": "teams-chat-triage", "event_kind": "teams.chat", "event_id": "7", "advice": "確認が必要",
               "actions": [{"type": "teams.reply", "text": "受領しました"}, {"type": "ado.create", "title": "t", "description": "d"}]}
        short = notify.format_post(1, rec)   # the short post carries no placeholder at all
        for word in ("Unknown", "判断メモなし", "承認で実行", "teams-chat-triage", "改訂", "承認の対象"):
            self.assertNotIn(word, short)
        post = notify.format_detail(1, rec)
        self.assertNotIn("Unknown", post)
        self.assertIn("判断メモなし", post)
        self.assertNotIn("承認で実行", post)
        self.assertIn("既定は記録だけ", post)


class TestTheDemoSaysRecording(unittest.TestCase):
    def test_the_demo_output_has_no_unknown_and_no_automatic_execution(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                demo.run(ROOT / "examples" / "demo_day.json", GRAPHS, StubBackend(), PBS, process, Path(d) / "o", pace=0)
        # the posts of the demo (lines starting with "| [kimeru #" up to the next blank step) guess nothing;
        # the work list of the brief keeps "Unknown" for progress nobody has recorded (kimeru/work.py, on purpose)
        text = buf.getvalue()
        posts = [l for l in text.splitlines() if l.strip().startswith("| ") and "担当:" not in l and "/ Unknown /" not in l]
        self.assertFalse([l for l in posts if "Unknown" in l])
        self.assertNotIn("自動実行", text)
        self.assertNotRegex(text, r"→ 実行（試し）")
        self.assertIn("判断: StubBackend（キーワードの簡易判定）", text)


if __name__ == "__main__":
    unittest.main()
