"""N2: a Teams that runs with no window on screen (tray) is not reported as "not started". When the chat list cannot be read, one
status read tells the two cases apart and the error says which one it is. The bridge's script is replaced by answers (nothing is run,
no Teams is touched); the Windows-only tests run tests/fake-teams-self.ps1 the way the managed PC runs the real script."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from kimeru import daily, diagnose, notify, pull
from kimeru.backends import StubBackend
from kimeru.cli import process
from tests.test_daily import GRAPHS, PBS, FakeTeams

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fake-teams-self.ps1"
WINDOW = "Teams のウィンドウが表示されていません（起動はしています）。Teams のウィンドウを開いてチャット一覧を表示してください"
NOT_RUNNING = "Teams が起動していません"
MISSING = "Teams window not found"   # what tools/teams-self.ps1 says for `chats` when no window is there
TRAY = {"ok": True, "teams": False, "teams_process": True, "teams_window": False, "selfChatOpen": False}
GONE = {"ok": True, "teams": False, "teams_process": False, "teams_window": False, "selfChatOpen": False}
SHOWN = {"ok": True, "teams": True, "teams_process": True, "teams_window": True, "selfChatOpen": False}


def refused(message):
    """What `_run` raises when the script answers ok=false with its own message."""
    return notify.BridgeError(message, {"ok": False, "error": message})


class ScriptBridge(notify.PowerShellBridge):
    """The real PowerShellBridge (its chats() and its sentences) with the script's answers given: `answers` maps an action to the JSON
    the script prints, or to the error `_run` raises for a refused action. Nothing is run."""

    def __init__(self, answers):
        super().__init__(script=FAKE)
        self.answers = answers
        self.calls = []

    def _run(self, *args):
        action = args[args.index("-Action") + 1]
        self.calls.append(action)
        got = self.answers[action]
        if isinstance(got, Exception):
            raise got
        return got


class TrayTeams(ScriptBridge):
    """A daily cycle's bridge: the chat list is read as above; posting and reading the self chat go to the in-memory fake."""

    def __init__(self, answers):
        super().__init__(answers)
        self.fake = FakeTeams()

    def post(self, text, send):
        return self.fake.post(text, send)

    def read(self):
        return self.fake.read()


class TestTheChatListSaysWhyItIsEmpty(unittest.TestCase):
    def test_teams_that_runs_without_a_window_is_not_called_not_running(self):
        bridge = ScriptBridge({"chats": refused(MISSING), "status": TRAY})
        with self.assertRaises(notify.BridgeError) as got:
            bridge.chats()
        self.assertEqual(str(got.exception), WINDOW)
        self.assertNotIn("起動していません", str(got.exception))
        self.assertEqual(bridge.calls, ["chats", "status"])   # the status is read once, and only because the list failed

    def test_teams_that_is_not_running_is_called_not_running(self):
        bridge = ScriptBridge({"chats": refused(MISSING), "status": GONE})
        with self.assertRaises(notify.BridgeError) as got:
            bridge.chats()
        self.assertEqual(str(got.exception), NOT_RUNNING)

    def test_a_list_that_reads_is_returned_and_the_status_is_not_read(self):
        bridge = ScriptBridge({"chats": {"ok": True, "chats": [{"id": "a"}]}, "status": TRAY})
        self.assertEqual(bridge.chats(), [{"id": "a"}])
        self.assertEqual(bridge.calls, ["chats"])

    def test_a_shown_window_keeps_the_scripts_own_message(self):
        bridge = ScriptBridge({"chats": refused("could not read the chat list"), "status": SHOWN})
        with self.assertRaises(notify.BridgeError) as got:
            bridge.chats()
        self.assertEqual(str(got.exception), "could not read the chat list")

    def test_a_status_that_cannot_be_read_keeps_the_first_message(self):
        bridge = ScriptBridge({"chats": refused(MISSING), "status": refused("the script's output could not be read")})
        with self.assertRaises(notify.BridgeError) as got:
            bridge.chats()
        self.assertEqual(str(got.exception), MISSING)

    def test_a_script_without_the_new_fields_keeps_the_first_message(self):
        bridge = ScriptBridge({"chats": refused(MISSING), "status": {"ok": True, "teams": False}})
        with self.assertRaises(notify.BridgeError) as got:
            bridge.chats()
        self.assertEqual(str(got.exception), MISSING)

    def test_the_two_sentences_are_one_line_each(self):
        self.assertEqual(notify.TEAMS_NO_WINDOW, WINDOW)
        self.assertEqual(notify.TEAMS_NOT_RUNNING, NOT_RUNNING)
        for text in (notify.TEAMS_NO_WINDOW, notify.TEAMS_NOT_RUNNING):
            self.assertNotIn("\n", text)


class TestPullAndCycleSayTheSameSentence(unittest.TestCase):
    def test_pull_teams_raises_the_window_sentence(self):
        bridge = ScriptBridge({"chats": refused(MISSING), "status": TRAY})
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(notify.BridgeError) as got:
                pull.pull_teams(Path(d) / "inbox", Path(d) / "out", bridge=bridge)
        self.assertEqual(str(got.exception), WINDOW)

    def test_the_daily_cycle_report_names_the_window_and_the_cycle_goes_on(self):
        bridge = TrayTeams({"chats": refused(MISSING), "status": TRAY})
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            r = daily.cycle(out, Path(d) / "inbox", GRAPHS, StubBackend(), PBS, process,
                            bridge=bridge, send=True, now=datetime(2026, 10, 15, 7, 0))
            self.assertEqual(r["pull_teams"], "error: BridgeError: " + WINDOW)
            self.assertEqual(r["judge"], 0)
            rows = [json.loads(line) for line in (out / "daily.log.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertIn(("pull_teams", "error: BridgeError: " + WINDOW), [(r.get("step"), r.get("error")) for r in rows])

    def test_a_cycle_report_shows_both_sentences_as_they_are(self):
        for text in (WINDOW, NOT_RUNNING):
            self.assertEqual(diagnose.error_text("error: BridgeError: " + text), "BridgeError: " + text)


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
class TestTheFakeScriptTellsTheThreeStatesApart(unittest.TestCase):
    """tests/fake-teams-self.ps1 answers status and chats the way tools/teams-self.ps1 does for each state of Teams."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("KIMERU_FAKE_TEAMS", "KIMERU_FAKE_CHAT")}
        os.environ["KIMERU_FAKE_CHAT"] = str(Path(self.tmp.name) / "chat.json")   # no such file: an empty self chat

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_a_tray_only_teams_is_reported_as_running_without_a_window(self):
        os.environ["KIMERU_FAKE_TEAMS"] = "tray"
        bridge = notify.PowerShellBridge(script=FAKE)
        st = bridge._run("-Action", "status")
        self.assertEqual((st["teams"], st["teams_process"], st["teams_window"]), (False, True, False))
        with self.assertRaises(notify.BridgeError) as got:
            bridge.chats()
        self.assertEqual(str(got.exception), WINDOW)

    def test_teams_that_is_not_running_is_reported_as_not_running(self):
        os.environ["KIMERU_FAKE_TEAMS"] = "none"
        bridge = notify.PowerShellBridge(script=FAKE)
        st = bridge._run("-Action", "status")
        self.assertEqual((st["teams"], st["teams_process"], st["teams_window"]), (False, False, False))
        with self.assertRaises(notify.BridgeError) as got:
            bridge.chats()
        self.assertEqual(str(got.exception), NOT_RUNNING)

    def test_a_shown_window_lists_the_chats_and_the_status_says_so(self):
        os.environ["KIMERU_FAKE_TEAMS"] = "window"
        bridge = notify.PowerShellBridge(script=FAKE)
        st = bridge._run("-Action", "status")
        self.assertEqual((st["teams"], st["teams_process"], st["teams_window"]), (True, True, True))
        self.assertEqual(bridge.chats(), [])

    def test_the_fake_script_parses(self):
        cmd = ("& { param($f) $e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($f,[ref]$t,[ref]$e);"
               "if($e){$e|%{$_.Message};exit 1} } '" + str(FAKE) + "'")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
