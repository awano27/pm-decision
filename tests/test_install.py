"""A pip install (no network, no dependencies) brings the decision graphs, playbooks, demo scripts and the PowerShell kimeru calls,
and `kimeru demo` runs from a folder that is not the clone."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _have(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


@unittest.skipUnless(_have("setuptools") and _have("venv") and _have("ensurepip"), "setuptools or venv is not available")
class TestPipInstall(unittest.TestCase):
    def test_install_then_validate_and_demo_from_another_folder(self):
        with tempfile.TemporaryDirectory(prefix="kimeru-install-") as tmp:
            tmp = Path(tmp)
            venv = tmp / "venv"
            # --system-site-packages: the build needs the setuptools of this Python (no network, no build isolation)
            subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)], check=True, capture_output=True)
            exe_dir = venv / ("Scripts" if os.name == "nt" else "bin")
            py = exe_dir / ("python.exe" if os.name == "nt" else "python")
            work = tmp / "work"
            work.mkdir()
            src = tmp / "src"   # a copy, so that building leaves nothing (build/, *.egg-info) in the clone
            shutil.copytree(REPO, src, ignore=shutil.ignore_patterns(".git", "out", "__pycache__", "build", "*.egg-info", "tests"))
            install = subprocess.run([str(py), "-m", "pip", "install", "--no-build-isolation", "--no-deps", "--no-index",
                                      "--disable-pip-version-check", str(src)], capture_output=True, text=True, cwd=work)
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
            prefix = Path(subprocess.run([str(py), "-c", "import sys;print(sys.prefix)"], capture_output=True, text=True,
                                         check=True).stdout.strip())
            share = prefix / "share" / "kimeru"
            for name in ("graphs", "playbooks", "examples"):
                self.assertTrue((share / name).is_dir(), name)
            self.assertTrue(list((share / "graphs").glob("*.json")))
            self.assertTrue(list((share / "playbooks").glob("*.json")))
            self.assertTrue((share / "examples" / "demo_day.json").is_file())
            for ps1 in ("teams-self.ps1", "teams-copilot.ps1", "toast.ps1", "outlook-mail.ps1"):
                self.assertTrue((share / "tools" / ps1).is_file(), ps1)
            env = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_") and k != "PYTHONPATH"}
            env["KIMERU_STATE_DIR"] = str(tmp / "state")
            env["PYTHONUTF8"] = "1"
            kimeru = exe_dir / ("kimeru.exe" if os.name == "nt" else "kimeru")
            out = tmp / "out"
            valid = subprocess.run([str(kimeru), "--backend", "stub", "--out", str(out), "validate"], capture_output=True,
                                   text=True, encoding="utf-8", cwd=work, env=env)
            self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
            demo = subprocess.run([str(kimeru), "--backend", "stub", "--out", str(out), "demo", "--pace", "0"],
                                  capture_output=True, text=True, encoding="utf-8", cwd=work, env=env, timeout=300)
            self.assertEqual(demo.returncode, 0, demo.stdout + demo.stderr)
            self.assertIn("=== まとめ", demo.stdout)


if __name__ == "__main__":
    unittest.main()
