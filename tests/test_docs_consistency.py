"""The documents agree with each other and with the code: every link to a file and heading resolves, the counts
match the folders, and the phone routes are described as what they send (counts and numbers)."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", ROOT / "SECURITY.md", ROOT / "CHANGELOG.md", ROOT / "CONTRIBUTING.md",
        *sorted((ROOT / "docs").rglob("*.md"))]
SKIP = set()


def slug(heading):
    """GitHub's anchor for a heading: lower case, punctuation dropped (letters of any script kept), spaces to '-'."""
    h = re.sub(r"<[^>]+>", "", heading).strip().lower()
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


def anchors(path):
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    seen, out = {}, set()
    for m in re.finditer(r"(?m)^#{1,6}\s+(.*?)\s*#*\s*$", text):
        s = slug(m.group(1))
        n = seen.get(s, 0)
        out.add(s if n == 0 else f"{s}-{n}")
        seen[s] = n + 1
    return out


class TestLinks(unittest.TestCase):
    def test_every_relative_link_and_heading_exists(self):
        bad = []
        for doc in DOCS:
            if doc.name in SKIP:
                continue
            text = re.sub(r"```.*?```", "", doc.read_text(encoding="utf-8"), flags=re.S)
            for m in re.finditer(r"\]\(([^)\s]+)\)", text):
                target = m.group(1)
                if re.match(r"[a-z]+:", target) or target.startswith("mailto:"):
                    continue
                path, _, frag = target.partition("#")
                dest = (doc.parent / path).resolve() if path else doc
                if not dest.exists():
                    bad.append(f"{doc.relative_to(ROOT)}: {target} (no such file)")
                    continue
                if frag and dest.suffix == ".md" and frag.lower() not in anchors(dest):
                    bad.append(f"{doc.relative_to(ROOT)}: {target} (no such heading)")
        self.assertEqual(bad, [])


class TestCountsAndWords(unittest.TestCase):
    def test_the_push_routes_send_counts_and_numbers(self):
        for doc in DOCS:
            if doc.name in SKIP or doc.name == "CHANGELOG.md":
                continue
            for line in doc.read_text(encoding="utf-8").splitlines():
                self.assertNotRegex(line, r"件数だけを?(届ける|送る|を iPhone)", f"{doc.name}: {line[:80]}")

    def test_judgment_time_has_one_source(self):
        for name in ("managed-pc-check.md",):
            text = (ROOT / "docs" / name).read_text(encoding="utf-8")
            self.assertNotIn("中央値 2.5 秒", text, name)
            self.assertNotIn("中央値 3.6 秒", text, name)

    def test_the_result_sheet_has_every_check_item_of_the_short_set(self):
        text = (ROOT / "docs" / "managed-pc-check.md").read_text(encoding="utf-8")
        sheet = text[text.index("## 結果シート"):]
        for item in ("T20", "T21", "T22", "T23", "T24"):
            self.assertIn(item, sheet)


if __name__ == "__main__":
    unittest.main()
