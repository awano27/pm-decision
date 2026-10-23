"""Offline packaging checks for the built-in Kimeru assets."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
import contextlib
import io
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIRS = ("graphs", "playbooks", "examples")
SCRIPTS = ("teams-self.ps1", "teams-copilot.ps1", "toast.ps1", "outlook-mail.ps1")
METADATA = ("pyproject.toml", "setup.py", "MANIFEST.in", "README.md", "LICENSE")
KEEP_ENV = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
            "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"}


def _env(temp_root, pythonpath=None):
    env = {k: v for k, v in os.environ.items() if k.upper() in KEEP_ENV}
    for key, name in (("HOME", "home"), ("USERPROFILE", "home"), ("APPDATA", "appdata"),
                      ("LOCALAPPDATA", "localappdata"), ("TEMP", "tmp"), ("TMP", "tmp")):
        path = temp_root / name
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    env.update({"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    return env


class DistributionAssetsTests(unittest.TestCase):
    def test_resources_use_the_canonical_install_root_without_checkout_fallback(self):
        from kimeru import paths, resources

        with tempfile.TemporaryDirectory(prefix="kimeru-resource-precedence-") as temp:
            shared = Path(temp) / "share" / "kimeru"
            shared.mkdir(parents=True)
            with mock.patch.object(paths, "base", return_value=shared):
                self.assertEqual(resources.resource_dir("graphs"), shared / "graphs")
                self.assertFalse(resources.resource_dir("graphs").exists())

    def test_loaders_reject_missing_empty_and_non_directory_paths(self):
        from kimeru import cli, graph, plan
        from kimeru.graph import GraphError
        from kimeru.plan import PlaybookError

        with tempfile.TemporaryDirectory(prefix="kimeru-loader-assets-") as temp:
            base = Path(temp)
            empty = base / "empty"
            empty.mkdir()
            file_path = base / "file"
            file_path.write_text("not a directory", encoding="utf-8")
            for path in (base / "missing", empty, file_path):
                with self.subTest(path=path.name, loader="graphs"):
                    with self.assertRaises(GraphError):
                        graph.load_dir(path)
                with self.subTest(path=path.name, loader="playbooks"):
                    with self.assertRaises(PlaybookError):
                        plan.load_playbooks(path)

            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                code = cli.main(["--graphs", str(empty), "validate"])
            self.assertNotEqual(code, 0)
            self.assertIn("検査するものがありません", output.getvalue())

            custom_graphs = base / "custom-graphs"
            custom_graphs.mkdir()
            import shutil
            shutil.copy2(ROOT / "graphs" / "monitor_alert.json", custom_graphs / "monitor_alert.json")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(["--graphs", str(custom_graphs), "--playbooks", str(ROOT / "playbooks"),
                                 "validate"])
            self.assertEqual(code, 0, output.getvalue())
            self.assertIn("alert-triage", output.getvalue())

    @unittest.skipUnless(importlib.util.find_spec("setuptools"), "offline build needs setuptools installed")
    def test_offline_wheel_and_sdist_assets(self):
        with tempfile.TemporaryDirectory(prefix="kimeru-distribution-") as temp:
            base = Path(temp)
            source = base / "source"
            source.mkdir()
            for name in ("kimeru", *ASSET_DIRS, "tools"):
                import shutil
                shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
            for name in METADATA:
                if (ROOT / name).is_file():
                    (source / name).write_bytes((ROOT / name).read_bytes())

            with self.subTest("wheel directly from selected source copy"):
                obsolete = source / "examples" / "obsolete.json"
                obsolete.write_text("{}", encoding="utf-8")
                wheel = self._wheel(source, base / "wheels")
                with zipfile.ZipFile(wheel) as archive:
                    self.assertTrue(any(name.endswith(".data/data/share/kimeru/examples/obsolete.json") for name in archive.namelist()))
                obsolete.unlink()
                wheel = self._wheel(source, base / "wheels")
                self._assert_wheel_assets(wheel)
                with zipfile.ZipFile(wheel) as archive:
                    self.assertFalse(any(name.endswith(".data/data/share/kimeru/examples/obsolete.json") for name in archive.namelist()))
                self._exercise_wheel(wheel, base / "wheel-runtime")

            with self.subTest("sdist to wheel"):
                sdist_dir = base / "sdists"
                sdist_dir.mkdir()
                env = _env(base / "env-sdist", source)
                built = subprocess.run(
                    [sys.executable, "-B", "-c",
                     "import setuptools.build_meta as b,sys; b.build_sdist(sys.argv[1])", str(sdist_dir)],
                    cwd=source, env=env, capture_output=True, text=True, check=False)
                self.assertEqual(built.returncode, 0, built.stderr[-3000:])
                sdists = list(sdist_dir.glob("*.tar.gz"))
                self.assertEqual(len(sdists), 1)
                import tarfile
                unpacked = base / "sdist-source"
                unpacked.mkdir()
                with tarfile.open(sdists[0]) as archive:
                    archive.extractall(unpacked)
                extracted_source = next(unpacked.iterdir())
                wheel_from_sdist = self._wheel(extracted_source, base / "wheels-from-sdist")
                self._assert_wheel_assets(wheel_from_sdist)

    def _wheel(self, source, destination):
        destination.mkdir(parents=True, exist_ok=True)
        env = _env(destination.parent / (destination.name + "-env"), source)
        result = subprocess.run(
            [sys.executable, "-B", "-m", "pip", "wheel", "--no-index", "--no-deps",
             "--no-build-isolation", "--wheel-dir", str(destination), "."],
            cwd=source, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        wheels = list(destination.glob("*.whl"))
        self.assertEqual(len(wheels), 1)
        return wheels[0]

    def _assert_wheel_assets(self, wheel):
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            roots = {name.split(".data/data/share/kimeru/", 1)[0] + ".data/data/share/kimeru/"
                     for name in names if ".data/data/share/kimeru/" in name}
            self.assertEqual(len(roots), 1)
            root = next(iter(roots))
            for folder in ASSET_DIRS:
                self.assertTrue(any(name.startswith(root + folder + "/") for name in names),
                                f"wheel is missing {folder} assets")
                for source in (ROOT / folder).glob("*.json"):
                    self.assertEqual(archive.read(root + folder + "/" + source.name), source.read_bytes())
            for script in SCRIPTS:
                self.assertEqual(archive.read(root + "tools/" + script), (ROOT / "tools" / script).read_bytes())
            self.assertFalse(any(name.startswith("kimeru/_resources/") for name in names),
                             "asset installation must have one canonical layout")

    def _exercise_wheel(self, wheel, runtime):
        import venv

        prefix = runtime / "installed"
        venv.EnvBuilder(with_pip=False).create(prefix)
        python = prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        cwd = runtime / "outside-checkout"
        cwd.mkdir(parents=True)
        env = _env(runtime / "env")
        installed = subprocess.run(
            [sys.executable, "-B", "-m", "pip", "--python", str(python), "install",
             "--no-index", "--no-deps", str(wheel)], cwd=cwd, env=env,
            capture_output=True, text=True, check=False)
        self.assertEqual(installed.returncode, 0, installed.stderr[-3000:])
        probe = """
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import kimeru
import sys
from kimeru import cli, graph, notify, paths, plan, push, resources, stats, writer
from kimeru.backends import StubBackend
root = Path(kimeru.__file__).resolve().parent
assert root.is_relative_to(Path(sys.prefix))
assert paths.base() == Path(sys.prefix) / 'share' / 'kimeru'
assert resources.resource_dir('graphs') == paths.base() / 'graphs'
assert resources.resource_path('examples', 'demo_day.json').is_file()
pbs = plan.load_playbooks(resources.resource_dir('playbooks'))
gs = graph.load_dir(resources.resource_dir('graphs'), pbs)
assert len([g for group in gs.values() for g in group]) == 4
assert len(pbs) == 9
result = graph.run(gs['monitor.alert'][0], {'kind':'monitor.alert','id':'sample','severity':'Sev1','condition':'Fired'}, StubBackend(), playbooks=pbs)
assert result['actions']
assert len(stats.bundled_graph_names()) == 4
expected = {name: paths.base() / 'tools' / name for name in ('teams-self.ps1','teams-copilot.ps1','toast.ps1','outlook-mail.ps1')}
assert Path(notify.PowerShellBridge().script) == expected['teams-self.ps1']
assert Path(writer.M365AutoWriter().script) == expected['teams-copilot.ps1']
with patch('kimeru.push.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='{"verified":true}')) as mocked:
    push.send_outlook('sample')
    assert Path(mocked.call_args.args[0][5]) == expected['outlook-mail.ps1']
with patch('kimeru.notify.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')) as mocked:
    notify.show_toast('title', 'body')
    assert Path(mocked.call_args.args[0][5]) == expected['toast.ps1']
"""
        result = subprocess.run([str(python), "-B", "-c", probe], cwd=cwd, env=env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])

        validate = subprocess.run([str(python), "-B", "-m", "kimeru", "validate"],
                                  cwd=cwd, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(validate.returncode, 0, validate.stderr[-3000:])
        self.assertEqual(validate.stdout.count("ok  "), 13)

        demo_out = runtime / "freshout"
        demo = subprocess.run([str(python), "-B", "-m", "kimeru", "--backend", "stub", "--out", str(demo_out), "demo", "--pace", "0"],
                              cwd=cwd, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(demo.returncode, 0, demo.stderr[-3000:])
        self.assertTrue((demo_out / "decisions.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
