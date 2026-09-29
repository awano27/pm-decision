"""No test may read or write the user's real state folder (%LOCALAPPDATA%\\kimeru): use a throw-away one."""
import atexit
import os
import shutil
import tempfile

if not os.environ.get("KIMERU_STATE_DIR"):
    _state = tempfile.mkdtemp(prefix="kimeru-test-state-")
    os.environ["KIMERU_STATE_DIR"] = _state
    atexit.register(shutil.rmtree, _state, ignore_errors=True)
