"""Pure mock-UI regression tests: one bridge invocation must perform at most one send action."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "teams-self.ps1"
RUNNER = "powershell" if os.name == "nt" else "pwsh"


def run_scenario(scenario):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "scenario.json"
        path.write_text(json.dumps(scenario), encoding="utf-8")
        return subprocess.run([RUNNER, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
                               "-Action", "test-send-once", "-TestRows", str(path)],
                              capture_output=True, text=True, encoding="utf-8", timeout=30)


class TestSendOnce(unittest.TestCase):
    def test_button_invoke_with_delayed_send_does_not_fall_back_to_keys(self):
        result = run_scenario({"self_ok": True, "compose_ok": True, "button_present": True,
                               "invoke_throws": False, "wait_sent": False, "focused": True})
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        data = json.loads(result.stdout.lstrip("\ufeff"))
        self.assertEqual(data["invokes"], 1)
        self.assertEqual(data["keys"], 0)
        self.assertIsNone(data["sent"])
        self.assertEqual(data["via"], "button-unknown")

    def test_button_exception_does_not_fall_back_to_keys(self):
        result = run_scenario({"self_ok": True, "compose_ok": True, "button_present": True,
                               "invoke_throws": True, "wait_sent": False, "focused": True})
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        data = json.loads(result.stdout.lstrip("\ufeff"))
        self.assertEqual((data["invokes"], data["keys"]), (1, 0))
        self.assertIsNone(data["sent"])
        self.assertTrue(data["attempted"])

    def test_compose_disappearance_alone_does_not_claim_delivery(self):
        result = run_scenario({"self_ok": True, "compose_ok": True, "button_present": True,
                               "invoke_throws": False, "wait_sent": True, "focused": True})
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        data = json.loads(result.stdout.lstrip("\ufeff"))
        self.assertEqual(data["invokes"], 1)
        self.assertEqual(data["keys"], 0)
        self.assertIsNone(data["sent"])

    def test_absent_button_uses_only_one_existing_key(self):
        result = run_scenario({"self_ok": True, "compose_ok": True, "button_present": False,
                               "invoke_throws": False, "wait_sent": False, "focused": True})
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        data = json.loads(result.stdout.lstrip("\ufeff"))
        self.assertEqual(data["keys"], 1)
        self.assertEqual(data["key_values"], ["^{ENTER}"])
        self.assertIsNone(data["sent"])
        self.assertTrue(data["attempted"])

    def test_self_chat_and_compose_guards_abort_before_any_send(self):
        for scenario in (
            {"self_ok": False, "compose_ok": True, "button_present": True, "focused": True},
            {"self_ok": True, "compose_ok": False, "button_present": True, "focused": True},
        ):
            with self.subTest(scenario=scenario):
                result = run_scenario({"invoke_throws": False, "wait_sent": False, **scenario})
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                data = json.loads(result.stdout.lstrip("\ufeff"))
                self.assertEqual((data["invokes"], data["keys"]), (0, 0))
                self.assertFalse(data["attempted"])
                self.assertFalse(data["sent"])

    def test_key_fallback_respects_foreground_and_compose_focus_guards(self):
        for scenario in (
            {"foreground_ok": False, "focused": True},
            {"foreground_ok": True, "focused": False},
        ):
            with self.subTest(scenario=scenario):
                result = run_scenario({"self_ok": True, "compose_ok": True, "button_present": False,
                                       "invoke_throws": False, "wait_sent": False, **scenario})
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                data = json.loads(result.stdout.lstrip("\ufeff"))
                self.assertEqual((data["invokes"], data["keys"]), (0, 0))
                self.assertFalse(data["attempted"])
                self.assertFalse(data["sent"])


if __name__ == "__main__":
    unittest.main()
