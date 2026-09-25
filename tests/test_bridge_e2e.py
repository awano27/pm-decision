"""End-to-end T9 path through the real PowerShellBridge and CLI, with a fake Teams.

Covers what the company PC exercises except the Teams UI: PowerShell argument
passing of long Japanese multi-line posts, JSON round-trips, notify -> reply -> approvals.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kimeru import notify

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fake-teams-self.ps1"


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
class TestBridgeE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.chat = self.dir / "chat.json"
        os.environ["KIMERU_FAKE_CHAT"] = str(self.chat)
        self.out = self.dir / "out"
        # the same sample the company check uses: produces queue items with long Japanese advice
        r = subprocess.run([sys.executable, "-m", "kimeru", "--out", str(self.out), "run",
                            str(ROOT / "examples" / "meeting_minutes.json")], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        (self.out / "approvals.json").write_text('{"next": 278, "items": {}}', encoding="utf-8")

    def tearDown(self):
        os.environ.pop("KIMERU_FAKE_CHAT", None)
        self.tmp.cleanup()

    def reply(self, text):
        chat = json.loads(self.chat.read_text(encoding="utf-8-sig"))
        chat["messages"].append(text)
        self.chat.write_text(json.dumps(chat, ensure_ascii=False), encoding="utf-8")

    def test_notify_reply_approvals(self):
        bridge = notify.PowerShellBridge(script=FAKE)
        ids = notify.notify(self.out, bridge, send=True)
        self.assertEqual(ids[:2], [278, 279])
        posted = json.loads(self.chat.read_text(encoding="utf-8-sig"))["messages"]
        self.assertTrue(posted[0].startswith("[kimeru #278]"))
        self.assertIn("\r\n返信: OK 278 / NG 278 / 保留 278", posted[0])   # multi-line, Japanese intact

        self.reply("ＯＫ　２７８")          # phone-style full-width
        self.reply("NG 279")
        changes = notify.collect(self.out, bridge)
        self.assertEqual(sorted((c["id"], c["status"]) for c in changes), [(278, "approved"), (279, "rejected")])
        self.assertEqual(notify.collect(self.out, bridge), [])                 # no double processing

    def test_cli_commands(self):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        # the CLI resolves tools/teams-self.ps1; point it at the fake through the bridge default
        code = ("import sys; from kimeru import notify; from pathlib import Path; "
                f"notify.PowerShellBridge.__init__.__defaults__ = (Path(r'{FAKE}'),); "
                "from kimeru.cli import main; sys.exit(main(sys.argv[1:]))")
        run = lambda *a: subprocess.run([sys.executable, "-c", code, "--out", str(self.out), *a], cwd=ROOT,
                                        capture_output=True, text=True, encoding="utf-8", env=env)
        r = run("notify", "--send")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("posted: [278, 279", r.stdout)
        self.reply("OK 278")
        r = run("approvals")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("#278 -> approved", r.stdout)


if __name__ == "__main__":
    unittest.main()
