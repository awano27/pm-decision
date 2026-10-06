"""The tests and the "no outside traffic" checks never use the machine's kimeru settings: a PC that sets a writer
(Copilot, m365-auto, ...), a phone route or execution must not have the tests or the sample judgments reach them."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestTheTestsDropTheMachineSettings(unittest.TestCase):
    def test_a_fresh_test_process_drops_kimeru_settings_and_keys(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_TEST_")}
        env.update({"KIMERU_WRITER": "copilot", "KIMERU_PUSH": "webhook", "KIMERU_EXECUTE": "ado.comment",
                    "TYPESAFE_API_KEY": "not-a-key", "KIMERU_TEST_MARK": "1"})
        prog = ("import os, sys; sys.path.insert(0, 'tests'); import isolate; "
                "print(sorted(k for k in os.environ if k.startswith('KIMERU_') or k == 'TYPESAFE_API_KEY'))")
        r = subprocess.run([sys.executable, "-c", prog], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        left = eval(r.stdout.strip())
        self.assertEqual(set(left), {"KIMERU_STATE_DIR", "KIMERU_TEST_STATE_PID", "KIMERU_TEST_MARK"})

    def test_a_child_of_a_test_keeps_what_the_test_gave_it(self):
        env = {**os.environ, "KIMERU_ADO_ORG": "fake-org"}   # this process already ran isolate: a child keeps the test's choice
        prog = "import os, sys; sys.path.insert(0, 'tests'); import isolate; print(os.environ.get('KIMERU_ADO_ORG'))"
        r = subprocess.run([sys.executable, "-c", prog], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "fake-org", r.stderr)

    def test_this_process_has_no_writer_from_the_machine(self):
        self.assertNotIn("KIMERU_WRITER", os.environ)


class TestTheCheckScriptIsolatesItsSampleRuns(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "tools" / "check.ps1").read_text(encoding="utf-8-sig")

    def test_py_drops_the_settings_when_asked(self):
        py = self.text[self.text.index("function Py([string[]]$a)"):]
        py = py[:py.index("\n}\n")]
        self.assertIn("if ($Auto -or $script:IsolatePy)", py)
        self.assertIn("$_.Name -like 'KIMERU_*'", py)
        self.assertIn("$env:KIMERU_STATE_DIR = Join-Path $tmp 'auto-state'", py)

    def test_t7_t8_and_t14_run_isolated(self):
        for say in ('Say "T7 テストと検証"', 'Say "T8 サンプルで判断と朝のまとめ"'):
            i = self.text.index(say)
            self.assertIn("$script:IsolatePy = $true", self.text[i:i + 200], say)
        k = self.text.index("'--backend', 'kev', '--out', (Join-Path $tmp 'kev'), 'run'")
        self.assertIn("$script:IsolatePy = $true", self.text[k - 120:k])


if __name__ == "__main__":
    unittest.main()
