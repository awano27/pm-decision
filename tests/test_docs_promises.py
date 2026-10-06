"""What the first screen promises matches what kimeru does (README top, hero image, talk script, status table)."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import re
import unittest
from pathlib import Path

from kimeru import config

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
TOP = "\n".join(README.splitlines()[:50])


def status_row(mark):
    return next(l for l in README.splitlines() if l.startswith(f"| {mark}"))


class TestTheFirstScreen(unittest.TestCase):
    def test_the_top_says_the_phone_is_not_notified_and_you_send_it_yourself(self):
        self.assertIn("スマホには通知されません", TOP)
        self.assertIn("あなたがコピーして", TOP)
        self.assertNotRegex(TOP, r"スマホから\s*`?OK")

    def test_the_hero_image_does_not_promise_a_phone(self):
        svg = (ROOT / "assets" / "readme" / "hero.svg").read_text(encoding="utf-8")
        self.assertNotIn("iPhone", svg)

    def test_the_default_judge_is_described_as_the_code_has_it(self):
        self.assertEqual(config.SETTINGS["backend"][1], "stub")
        self.assertIn("キーワードによる簡易判定", TOP)
        self.assertNotIn("既定はこの PC に接続した **Kev**", README)


class TestTheStatusTable(unittest.TestCase):
    def test_codex_is_not_listed_as_usable(self):
        self.assertNotIn("Codex", status_row("✅"))
        self.assertIn("Codex", status_row("⛔"))

    def test_what_has_no_record_on_a_real_pc_is_not_usable_yet(self):
        usable = status_row("✅")
        for thing in ("アラート", "Claude", "Grok"):
            self.assertNotIn(thing, usable)
            self.assertIn(thing, status_row("🔬"))

    def test_no_unsupported_third_party_review_claim(self):
        for path in [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]:
            self.assertNotIn("第三者レビュー", path.read_text(encoding="utf-8"), path.name)


class TestCounts(unittest.TestCase):
    def test_the_playbook_count_in_the_docs_is_the_real_one(self):
        n = len(list((ROOT / "playbooks").glob("*.json")))
        self.assertIn(f"進め方の型（{n} 種類）", README)
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        self.assertIn(f"{n} 種類", ref)

    def test_the_fixture_count_in_eval_readme_is_the_real_one(self):
        n = sum(1 for l in (ROOT / "eval" / "fixtures.jsonl").read_text(encoding="utf-8").splitlines() if l.strip())
        first = (ROOT / "eval" / "README.md").read_text(encoding="utf-8").splitlines()[2]
        self.assertIn(f"({n} events)", first)

    def test_every_file_security_md_names_exists_in_the_code(self):
        sec = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        code = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                         for p in [*(ROOT / "kimeru").glob("*.py"), *(ROOT / "tools").glob("*.ps1")])
        for name in set(re.findall(r"`([\w.-]+\.(?:jsonl|json|txt|bak|html))`", sec)):
            base = name.removesuffix(".bak")
            self.assertTrue(base in code or base.split(".")[0] in code, name)


if __name__ == "__main__":
    unittest.main()
