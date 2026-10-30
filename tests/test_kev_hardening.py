"""The Kev on this PC: the key start-kev.cmd makes (only key holders may call it), the pinned model files checked before
torch unpickles head.pt, and the SHA-256 lists of what is carried to the managed PC."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from kimeru import backends, diagnose

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "tools" / "kev-bundle"


class Env:
    """Temporarily set (or with None, remove) environment variables."""
    def __init__(self, **values):
        self.values, self.saved = values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestTheKeyOfTheKevOnThisPC(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.file = self.dir / "api-key"
        self.file.write_text("secret-from-start-kev\n", encoding="utf-8")

    def test_a_kev_on_this_pc_sends_the_key_start_kev_made(self):
        with Env(KIMERU_KEV_KEY_FILE=str(self.file), KEV_API_KEY=None, KIMERU_KEV_URL=None):
            self.assertEqual(backends.KevBackend()._key, "secret-from-start-kev")
            self.assertEqual(backends.kev_headers("http://127.0.0.1:8009/v1"), {"Authorization": "Bearer secret-from-start-kev"})

    def test_the_key_of_this_pc_never_goes_to_a_kev_elsewhere(self):
        with Env(KIMERU_KEV_KEY_FILE=str(self.file), KEV_API_KEY=None, KIMERU_KEV_URL="http://192.0.2.10:8009/v1",
                 KIMERU_KEV_URL_REMOTE_OK="1"):
            self.assertEqual(backends.KevBackend()._key, "")
            self.assertEqual(backends.kev_headers("http://192.0.2.10:8009/v1"), {})

    def test_kev_api_key_wins_over_the_file(self):
        with Env(KIMERU_KEV_KEY_FILE=str(self.file), KEV_API_KEY="from-env", KIMERU_KEV_URL=None):
            self.assertEqual(backends.KevBackend()._key, "from-env")

    def test_no_file_means_no_key_as_before(self):
        with Env(KIMERU_KEV_KEY_FILE=str(self.dir / "missing"), KEV_API_KEY=None, KIMERU_KEV_URL=None):
            self.assertEqual(backends.KevBackend()._key, "")
            self.assertEqual(backends.kev_headers("http://127.0.0.1:8009/v1"), {})

    def test_the_request_carries_the_key(self):
        seen = {}

        class Resp:
            def __init__(self, body): self.body = body
            def read(self): return self.body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_open(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            return Resp(json.dumps({"answers": {"q": {"noul": 0.9}}}).encode())

        with Env(KIMERU_KEV_KEY_FILE=str(self.file), KEV_API_KEY=None, KIMERU_KEV_URL=None), \
                mock.patch.object(backends, "_urlopen", side_effect=fake_open):
            backends.KevBackend().ask({"text": "x"}, {"q": {"type": "noul", "instructions": "y"}})
        self.assertEqual(seen["auth"], "Bearer secret-from-start-kev")

    def test_diagnose_asks_with_the_key(self):
        seen = {}

        class Resp:
            def close(self): pass

        def fake_open(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            return Resp()

        with Env(KIMERU_KEV_KEY_FILE=str(self.file), KEV_API_KEY=None, KIMERU_KEV_URL=None), \
                mock.patch.object(backends._DIRECT, "open", side_effect=fake_open):
            self.assertEqual(diagnose.kev_state(), "up")
        self.assertEqual(seen["auth"], "Bearer secret-from-start-kev")


class TestTheLauncher(unittest.TestCase):
    def setUp(self):
        self.cmd = (BUNDLE / "start-kev.cmd").read_bytes().decode("ascii")

    def test_head_pt_is_checked_before_kev_starts(self):
        self.assertLess(self.cmd.index("kev-models.py\" verify --quick || goto :err"), self.cmd.index("-m kev.serve"))

    def test_kev_starts_with_a_key_kept_per_windows_user(self):
        self.assertIn(r"%LOCALAPPDATA%\kev\api-key", self.cmd)
        self.assertIn("secrets.token_urlsafe(32)", self.cmd)
        self.assertLess(self.cmd.index("set /p KEV_API_KEY="), self.cmd.index("-m kev.serve"))

    def test_the_base_model_is_read_from_the_cache_in_the_folder_and_head_pt_is_not_rewritten(self):
        self.assertIn('set "HF_HOME=%~dp0hf"', self.cmd)
        self.assertNotIn("localize", self.cmd)
        self.assertFalse((BUNDLE / "localize.py").exists())


class TestThePins(unittest.TestCase):
    def setUp(self):
        self.pins = json.loads((BUNDLE / "pins.json").read_text(encoding="utf-8"))

    def test_revisions_and_hashes_are_full_length(self):
        self.assertRegex(self.pins["kev_commit"], r"^[0-9a-f]{40}$")
        for m in self.pins["models"].values():
            self.assertRegex(m["revision"], r"^[0-9a-f]{40}$")
            for h in m["files"].values():
                self.assertRegex(h, r"^[0-9a-f]{64}$")

    def test_head_pt_is_pinned_and_the_base_sits_at_its_revision_in_the_cache_layout(self):
        self.assertIn("head.pt", self.pins["models"]["kev-4b"]["files"])
        base = self.pins["models"]["base"]
        self.assertEqual(base["dir"], "hf/hub/models--Qwen--Qwen3.5-4B-Base/snapshots/" + base["revision"])

    def test_the_precision_check_uses_the_torch_cpu_functions_that_the_pinned_torch_still_has(self):
        # torch._C._cpu lost _is_avx512_bf16_supported by 2.13: read alone, every CPU would quietly get fp32 (14GB, slower)
        text = (BUNDLE / "pick-dtype.py").read_text(encoding="utf-8")
        self.assertIn('hasattr(torch.cpu, "_is_avx512_bf16_supported")', text)

    def test_torch_is_past_the_unpickler_fix(self):
        major, minor = (int(x) for x in self.pins["torch"].split(".")[:2])
        self.assertGreaterEqual((major, minor), (2, 10))   # CVE-2026-24747: torch.load(weights_only=True) fixed in 2.10.0


class TestKevModelsVerify(unittest.TestCase):
    """kev-models.py verify, run on a small folder with its own pins.json."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        shutil.copy(BUNDLE / "kev-models.py", self.dir)
        files = {"models/kev-4b/head.pt": b"pickle bytes", "models/kev-4b/adapter_model.safetensors": b"weights",
                 "hf/hub/base/config.json": b"{}"}
        for rel, data in files.items():
            (self.dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.dir / rel).write_bytes(data)
        h = lambda b: hashlib.sha256(b).hexdigest()   # noqa: E731
        pins = {"models": {
            "kev-4b": {"repo": "x/kev", "revision": "0" * 40, "dir": "models/kev-4b",
                       "files": {"head.pt": h(b"pickle bytes"), "adapter_model.safetensors": h(b"weights")}},
            "base": {"repo": "x/base", "revision": "1" * 40, "dir": "hf/hub/base", "files": {"config.json": h(b"{}")}}}}
        (self.dir / "pins.json").write_text(json.dumps(pins), encoding="utf-8")

    def run_verify(self, *args):
        r = subprocess.run([sys.executable, str(self.dir / "kev-models.py"), "verify", *args], capture_output=True,
                           encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=60)
        return r.returncode, r.stdout

    def test_matching_files_pass(self):
        self.assertEqual(self.run_verify()[0], 0)
        self.assertEqual(self.run_verify("--quick")[0], 0)

    def test_a_changed_head_pt_stops_the_start(self):
        (self.dir / "models/kev-4b/head.pt").write_bytes(b"pickle bytes, changed")
        code, out = self.run_verify("--quick")
        self.assertEqual(code, 1)
        self.assertIn("head.pt", out)

    def test_quick_skips_the_big_weights_but_the_full_check_does_not(self):
        (self.dir / "models/kev-4b/adapter_model.safetensors").write_bytes(b"weights, changed")
        self.assertEqual(self.run_verify("--quick")[0], 0)
        self.assertEqual(self.run_verify()[0], 1)

    def test_an_added_pickle_or_code_file_is_refused(self):
        for name in ("adapter_model.bin", "modeling_x.py"):
            extra = self.dir / "models/kev-4b" / name
            extra.write_bytes(b"x")
            code, out = self.run_verify("--quick")
            self.assertEqual(code, 1, name)
            self.assertIn(name, out)
            extra.unlink()

    def test_a_missing_file_is_named(self):
        (self.dir / "hf/hub/base/config.json").unlink()
        code, out = self.run_verify()
        self.assertEqual(code, 1)
        self.assertIn("config.json", out)


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell")
class TestSha256Sums(unittest.TestCase):
    SCRIPT = ROOT / "tools" / "sha256sums.ps1"

    def ps(self, *args):
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.SCRIPT), *args],
                           capture_output=True, encoding="utf-8", errors="replace", timeout=120)
        return r.returncode, r.stdout + r.stderr

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for rel in ("kimeru/kimeru/cli.py", "kimeru/README.md", "kimeru/.python/python.exe", "kev/start-kev.cmd",
                    "kev/models/kev-4b/head.pt"):
            (self.dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.dir / rel).write_bytes(rel.encode())
        for part in ("kimeru/.python", "kimeru", "kev"):
            self.assertEqual(self.ps("-Write", str(self.dir / part))[0], 0, part)

    def test_an_untouched_copy_passes(self):
        code, out = self.ps("-Check", str(self.dir))
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("OK "), 3, out)

    def test_a_changed_file_is_named(self):
        (self.dir / "kev/models/kev-4b/head.pt").write_bytes(b"changed")
        code, out = self.ps("-Check", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("head.pt", out)

    def test_an_added_code_file_is_named_but_an_added_record_is_not(self):
        (self.dir / "kimeru/kimeru/sitecustomize.py").write_text("import os", encoding="utf-8")
        (self.dir / "kimeru/kimeru-check-result.txt").write_text("OK", encoding="utf-8")
        (self.dir / "kimeru/kimeru/__pycache__").mkdir()
        (self.dir / "kimeru/kimeru/__pycache__/cli.cpython-312.pyc").write_bytes(b"x")
        code, out = self.ps("-Check", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("sitecustomize.py", out)
        self.assertNotIn("kimeru-check-result.txt", out)
        self.assertNotIn("cli.cpython", out)

    def test_an_update_zip_list_without_dot_python_still_passes(self):
        # the update zip replaces kimeru/SHA256SUMS with a list that has no .python: .python keeps its own list
        (self.dir / "kimeru/kimeru/cli.py").write_bytes(b"new version")
        self.assertEqual(self.ps("-Write", str(self.dir / "kimeru"))[0], 0)
        lines = (self.dir / "kimeru/SHA256SUMS").read_text(encoding="utf-8")
        self.assertNotIn(".python", lines)
        self.assertEqual(self.ps("-Check", str(self.dir))[0], 0)

    def test_a_zip_is_compared_with_the_published_hash(self):
        z = self.dir / "kimeru-update.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("kimeru-pc/kimeru/VERSION", "kimeru test")
        good = hashlib.sha256(z.read_bytes()).hexdigest()
        self.assertEqual(self.ps("-Zip", str(z), "-Sha256", good.upper())[0], 0)
        self.assertEqual(self.ps("-Zip", str(z), "-Sha256", "0" * 64)[0], 1)


class TestThePublishedHash(unittest.TestCase):
    def test_the_tag_and_the_release_notes_name_the_update_zip_hash(self):
        ship = (ROOT / "tools" / "ship.ps1").read_text(encoding="utf-8-sig")
        self.assertLess(ship.index("release.ps1') -Zip"), ship.index('git tag -a $tag'))
        self.assertIn('-m "kimeru-update.zip SHA-256: $sha"', ship)
        wf = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertLess(wf.index("SHA-256: ([0-9a-f]{64})"), wf.index("gh release create"))

    def test_release_ps1_lists_the_files_and_writes_the_zip_hash(self):
        text = (ROOT / "tools" / "release.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("sha256sums.ps1') -Write (Join-Path $pack 'kimeru')", text)
        self.assertIn('"$Zip.sha256"', text)


if __name__ == "__main__":
    unittest.main()
