"""Every test run gets a throw-away state folder: no test may read or write the user's real one (%LOCALAPPDATA%\kimeru), nor
a KIMERU_STATE_DIR that the machine already sets. Imported by every test module (see the top of each), so it holds however
the tests are started: `python -m unittest`, `discover -s tests -p test_x.py`, or a single file."""
import atexit
import os
import shutil
import tempfile

if os.environ.get("KIMERU_TEST_STATE_PID") != str(os.getpid()):   # once per process (not inherited by a child), even if imported under two names
    _state = tempfile.mkdtemp(prefix="kimeru-test-state-")
    os.environ["KIMERU_STATE_DIR"] = _state
    os.environ["KIMERU_TEST_STATE_PID"] = str(os.getpid())
    atexit.register(shutil.rmtree, _state, ignore_errors=True)
