"""The examples README.md and docs/usage.md show are the real output: the demo summary, the self-chat post of `try`, and the
self-chat post with a writer (examples/readme_post.py)."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import contextlib
import hashlib
import importlib.util
import json
import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import cli

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
USAGE = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")


def volatile_masked(page):
    """A report page without what changes from run to run: clock times, ISO times, the numbers of the folded measurement
    notes, and the path separator."""
    page = page.replace("\\", "/")
    page = re.sub(r"\d{2}/\d{2} \d{2}:\d{2}", "MM/DD HH:MM", page)
    page = re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.]+(?:\+00:00|Z)?", "ISO", page)
    start, end = page.find("<details>"), page.find("</details>")
    if start >= 0 and end > start:
        page = page[:start] + re.sub(r"\d+", "0", page[start:end]) + page[end:]
    return page


def block_after(marker, doc=README):
    start = doc.index(marker)
    s = doc.index("```text\n", start) + len("```text\n")
    return doc[s:doc.index("```", s)].rstrip("\n")


class TestReadmeSamples(unittest.TestCase):
    def test_the_demo_summary_is_what_the_demo_prints(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                buf = io.StringIO()
                env = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
                env["KIMERU_STATE_DIR"] = str(Path(d) / "state")
                with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(buf):
                    rc = cli.main(["--graphs", str(ROOT / "graphs"), "--playbooks", str(ROOT / "playbooks"),
                                   "--backend", "stub", "demo", "--pace", "0",
                                   "--scenario", str(ROOT / "examples" / "demo_day.json")])
            finally:
                os.chdir(cwd)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        summary = out[out.index("=== まとめ"):].rstrip("\n").splitlines()
        shown = block_after("`demo` の最後に、こう出ます。").splitlines()
        # the model time is measured, so only its digits may differ
        norm = lambda lines: [re.sub(r"合計 [\d.]+ 秒", "合計 N 秒", l).replace("\\", "/") for l in lines]
        self.assertEqual(norm(summary), norm(shown))

    def test_the_self_chat_post_is_what_format_post_prints(self):
        spec = importlib.util.spec_from_file_location("readme_post", ROOT / "examples" / "readme_post.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        env = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        with mock.patch.dict(os.environ, env, clear=True):
            post = mod.post()
        self.assertEqual(post.rstrip("\n"), block_after("迷った件は、自分とのチャットに **1 通の短い投稿**（5 行ほど）として届きます。", USAGE))

    def test_the_try_post_is_what_try_prints(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                buf = io.StringIO()
                env = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
                with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(buf):
                    rc = cli.main(["try"])
            finally:
                os.chdir(cwd)
                cli.config.ARGS.clear()
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        step = out[out.index("③"):out.index("④")]
        post = [line[len("      | "):] for line in step.splitlines() if line.startswith("      | ")]
        self.assertEqual(post, block_after("自分とのチャットには、次の短い投稿として届きます").splitlines())

    def test_the_sample_page_is_what_try_writes_now(self):
        """docs/sample/try-report.html (and the pictures made from it by tools/readme_assets.py) is a real try run: after a
        change of the page or the scenario, run that script again. Times and other numbers, and the path separator, may differ."""
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                env = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
                with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["try"]), 0)
                fresh = (Path(d) / "out" / "try" / "report.html").read_text(encoding="utf-8")
            finally:
                os.chdir(cwd)
                cli.config.ARGS.clear()
        sample = (ROOT / "docs" / "sample" / "try-report.html").read_text(encoding="utf-8")
        self.assertEqual(volatile_masked(sample), volatile_masked(fresh), "run python tools/readme_assets.py")
        alt = re.search(r'<img src="\./assets/readme/try-report\.png"[^>]*alt="([^"]*)"', README).group(1)
        for state in ("判断: 承認済み", "作業: 進捗の記録なし", "相手への返信: 送っていない"):   # what the picture's alt text says
            self.assertIn(state, alt)
            self.assertIn(state, sample)
        for heading, said, count in (("足りない情報", "足りない情報", 3), ("進め方の案", "進め方の案", 5),
                                     ("人が決めること", "人が決める選択肢", 3)):   # the counts the alt text gives
            self.assertIn(f"{said} {count}", alt)
            block = sample[sample.index(f"<b>{heading}</b>"):]
            listed = block[block.index("<", block.index("</p>") + 4):block.index("</ul>" if heading != "進め方の案" else "</ol>")]
            self.assertEqual(listed.count("<li>"), count, heading)

    def test_the_pictures_were_made_from_the_sample_page_and_hero_as_they_are(self):
        """tools/readme_assets.py writes assets/readme/sources.json with what each picture was made from; a page or a hero.svg
        changed without making the pictures again fails here."""
        sources = json.loads((ROOT / "assets" / "readme" / "sources.json").read_text(encoding="utf-8"))
        for name, digest in sources.items():
            text = (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(hashlib.sha256(text).hexdigest(), digest, f"{name} changed: run python tools/readme_assets.py")
        for picture in ("try-report.png", "try-report-mobile.png", "social-preview.png"):
            self.assertTrue((ROOT / "assets" / "readme" / picture).is_file(), picture)


if __name__ == "__main__":
    unittest.main()
