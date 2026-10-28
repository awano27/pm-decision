"""tools/ship.py: the version bump, the release check and the release notes (tools/ship.ps1, release.yml)."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ship", ROOT / "tools" / "ship.py")
ship = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ship)

CHANGELOG = """# 変更履歴

## 未リリース

- **新しい項目。**説明。

## 1.3.0（2026-10-24）

前の版の要約。

- **前の項目。**
"""


class TestShip(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "kimeru").mkdir()
        (self.root / "pyproject.toml").write_text('[project]\nname = "kimeru"\nversion = "1.3.0"\n', encoding="utf-8")
        (self.root / "kimeru" / "__init__.py").write_text('"""kimeru."""\n__version__ = "1.3.0"\n', encoding="utf-8")
        # the README badge was left at v1.2 in the 1.3.0 release: the bump has to fix it too
        (self.root / "README.md").write_text('<img src="https://img.shields.io/badge/status-v1.2%20%C2%B7%20x">\n', encoding="utf-8")
        (self.root / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")

    def text(self, name):
        return (self.root / name).read_text(encoding="utf-8")

    def test_bump_then_summary_passes_the_check_and_gives_the_notes(self):
        ship.bump(self.root, "1.4.0", "2026-11-01")
        self.assertIn('version = "1.4.0"', self.text("pyproject.toml"))
        self.assertIn('__version__ = "1.4.0"', self.text("kimeru/__init__.py"))
        self.assertIn("status-v1.4%20", self.text("README.md"))
        self.assertNotIn("未リリース", self.text("CHANGELOG.md"))
        self.assertEqual(ship.check(self.root, "1.4.0"), ["CHANGELOG.md: the 1.4.0 section needs a summary paragraph before the entries"])

        log = self.text("CHANGELOG.md").replace("## 1.4.0（2026-11-01）\n\n", "## 1.4.0（2026-11-01）\n\n今回の要約。\n\n")
        (self.root / "CHANGELOG.md").write_text(log, encoding="utf-8")
        self.assertEqual(ship.check(self.root, "1.4.0"), [])
        notes = ship.section(self.text("CHANGELOG.md"), "1.4.0")
        self.assertEqual(notes, "今回の要約。\n\n- **新しい項目。**説明。\n")

    def test_check_names_every_place_that_disagrees(self):
        problems = ship.check(self.root, "1.4.0")
        self.assertEqual([p.split(":")[0] for p in problems], ["pyproject.toml", "kimeru/__init__.py", "README.md", "CHANGELOG.md"])

    def test_unreleased_entries_above_the_version_block_it(self):
        ship.bump(self.root, "1.4.0", "2026-11-01")
        log = self.text("CHANGELOG.md").replace("## 1.4.0（2026-11-01）\n\n", "## 未リリース\n\n- 後から足した項目\n\n## 1.4.0（2026-11-01）\n\n要約。\n\n")
        (self.root / "CHANGELOG.md").write_text(log, encoding="utf-8")
        self.assertEqual(ship.check(self.root, "1.4.0"), ["CHANGELOG.md: '## 未リリース' entries sit above 1.4.0; move them into it"])

    def test_bump_is_idempotent_and_needs_an_unreleased_section_only_once(self):
        ship.bump(self.root, "1.4.0", "2026-11-01")
        self.assertEqual(ship.bump(self.root, "1.4.0", "2026-11-02"), [])
        self.assertEqual(self.text("CHANGELOG.md").count("## 1.4.0（"), 1)

    def test_cli_rejects_a_tag_that_is_not_a_version(self):
        self.assertEqual(ship.main(["check", "vnext", "--root", str(self.root)]), 1)

    def test_the_released_versions_in_this_repo_have_notes(self):
        log = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        for version in ("1.1.0", "1.2.0", "1.3.0"):
            self.assertTrue(ship.section(log, version).strip(), version)


if __name__ == "__main__":
    unittest.main()
