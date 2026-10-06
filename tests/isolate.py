"""Every test run gets a throw-away state folder and none of the machine's kimeru settings: no test may read or write the
user's real state folder (%LOCALAPPDATA%\\kimeru), nor use a KIMERU_* setting or a key the machine already has. Without the
last part, a PC that sets a writer (KIMERU_WRITER=copilot, m365-auto, ...) would have the tests call that writer with their
fictional texts, and a PC that sets a judge or a phone route would have the tests reach it. A test that needs a setting
sets it itself. Imported by every test module (see the top of each), so it holds however the tests are started:
`python -m unittest`, `discover -s tests -p test_x.py`, or a single file."""
import atexit
import os
import shutil
import tempfile

# keys and addresses that are never inherited by a test (kimeru.config.SECRET_ENV and the writer CLIs' own keys)
SECRETS = ("TYPESAFE_API_KEY", "KEV_API_KEY", "CLM_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY",
           "GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN")

if os.environ.get("KIMERU_TEST_STATE_PID") != str(os.getpid()):   # once per process (not inherited by a child), even if imported under two names
    # the machine's settings are dropped in the process that starts the tests only: a child process a test starts gets
    # the settings that test chose (it inherits KIMERU_TEST_STATE_PID from its parent)
    for _k in ([] if "KIMERU_TEST_STATE_PID" in os.environ else list(os.environ)):
        if (_k.startswith("KIMERU_") and not _k.startswith("KIMERU_TEST_")) or _k in SECRETS:
            del os.environ[_k]
    _state = tempfile.mkdtemp(prefix="kimeru-test-state-")
    os.environ["KIMERU_STATE_DIR"] = _state
    os.environ["KIMERU_TEST_STATE_PID"] = str(os.getpid())
    atexit.register(shutil.rmtree, _state, ignore_errors=True)
