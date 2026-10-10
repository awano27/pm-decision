"""The examples README.md shows are the real output: the demo summary and the self-chat post."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import contextlib
import importlib.util
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


def block_after(marker):
    start = README.index(marker)
    s = README.index("```text\n", start) + len("```text\n")
    return README[s:README.index("```", s)].rstrip("\n")


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
        self.assertEqual(post.rstrip("\n"), block_after("迷った件は、自分とのチャットに **1 通の短い投稿**（5 行ほど）として届きます。"))


if __name__ == "__main__":
    unittest.main()
