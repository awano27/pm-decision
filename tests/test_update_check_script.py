"""run-update-check.cmd / tools/check-update.ps1 stay a check that sends nothing and touches no real state."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PS1 = ROOT / "tools" / "check-update.ps1"
CMD = ROOT / "run-update-check.cmd"


class TestUpdateCheckScript(unittest.TestCase):
    def setUp(self):
        self.raw = PS1.read_bytes()
        self.text = self.raw.decode("utf-8-sig")

    def test_the_launcher_is_ascii_and_runs_the_script(self):
        CMD.read_bytes().decode("ascii")
        self.assertIn(r"tools\check-update.ps1", CMD.read_text(encoding="ascii"))

    def test_windows_powershell_reads_the_japanese_text(self):
        self.assertTrue(self.raw.startswith(b"\xef\xbb\xbf"), "PowerShell 5.1 needs the BOM to read UTF-8")

    def test_the_only_web_request_is_to_this_pc(self):
        urls = re.findall(r"Invoke-WebRequest\s+'([^']+)'", self.text)
        self.assertTrue(urls)
        for u in urls:
            self.assertRegex(u, r"^http://127\.0\.0\.1:")
        for word in ("Invoke-RestMethod", "teams-self.ps1", "teams-copilot.ps1", "Start-Process"):
            self.assertNotIn(word, self.text)

    def test_it_runs_without_this_pcs_settings_and_keys_in_a_state_folder_of_its_own(self):
        self.assertIn("$_.Name -like 'KIMERU_*'", self.text)
        for key in ("TYPESAFE_API_KEY", "KEV_API_KEY", "CLM_API_KEY"):
            self.assertIn(key, self.text)
        self.assertIn("$env:KIMERU_STATE_DIR = Join-Path $tmp 'state'", self.text)

    def test_schedule_install_is_never_really_registered(self):
        self.assertIn("mock.patch.object(subprocess, ''run''", self.text)
        self.assertNotRegex(self.text, r"(?m)^\s*\$\w+\s*=\s*K @\([^)]*'schedule'")

    def test_every_check_is_recorded_and_the_real_records_are_compared(self):
        for k in ("U0", "U1", "U2", "U3", "U4", "U5", "U6", "U7", "U8", "U9"):
            self.assertIn(f"Rec '{k}'", self.text)
        self.assertIn("$before = @{ out = (Hashes $realOut); state = (Hashes $realState) }", self.text)


if __name__ == "__main__":
    unittest.main()
