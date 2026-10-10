"""tools/teams-self.ps1: a script-level variable must not reuse the name of a script parameter. PowerShell names are not
case sensitive and a parameter keeps its type, so `$priorIds = New-Object HashSet` turned the [string] parameter -PriorIds
into a string and every post failed between the paste and Enter ("String has no method Add")."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "tools" / "teams-self.ps1").read_text(encoding="utf-8-sig")


class TestNoParameterShadowing(unittest.TestCase):
    def test_no_script_level_assignment_to_a_parameter_name(self):
        params = SRC[SRC.index("param("):SRC.index("\n)\n", SRC.index("param("))]
        names = set(re.findall(r"\]\s*\$(\w+)", params))
        self.assertIn("PriorIds", names)
        body = SRC[SRC.index("\n)\n", SRC.index("param(")):]
        depth, bad = 0, []
        for line in body.splitlines():
            if depth == 0:
                m = re.match(r"\s*\$(\w+)\s*=\s*(New-Object|@\(|@\{|\[System\.Collections)", line)   # a collection into a typed parameter
                if m and m.group(1).lower() in {n.lower() for n in names}:
                    bad.append(line.strip()[:80])
            if re.match(r"\s*function\s", line):
                depth += line.count("{") - line.count("}")
            elif depth:
                depth += line.count("{") - line.count("}")
        self.assertEqual(bad, [])



def readback(text, rows, prior_all=""):
    with tempfile.TemporaryDirectory() as d:
        rows_path = Path(d) / "rows.json"
        rows_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "tools" / "teams-self.ps1"),
                "-Action", "test-readback", "-Text", text, "-TestRows", str(rows_path), "-PriorCount", "0"]
        if prior_all:
            args += ["-PriorAllIds", prior_all]
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert r.returncode == 0, r.stderr or r.stdout
        return json.loads(r.stdout.lstrip("\ufeff"))["readback"]


@unittest.skipUnless(os.name == "nt", "Windows PowerShell only: tools/teams-self.ps1 loads the Windows UI Automation types")
class TestReadbackOfWrappedMessages(unittest.TestCase):
    """Newer Teams builds read a message back as "<name> 送信済み <text> 今日の 10:33." (screen-reader label)."""
    SENT = "[kimeru #7] 判断が必要\n対象: #12 [Bug] 一覧\n返信: OK 7 / NG 7"

    def rows(self, visible):
        return [{"container_id": "old-1", "runtime_id": "b1", "y": 1, "text": "前の投稿"},
                {"container_id": "new-1", "runtime_id": "b2", "y": 2, "text": visible}]

    def test_the_label_and_the_time_around_the_text_are_accepted(self):
        for visible in ("山田 太郎 送信済み " + self.SENT + " 今日の 10:33.",
                        "Taro Yamada Sent " + self.SENT + " Today at 10:33 AM.",
                        self.SENT):
            self.assertEqual(readback(self.SENT, self.rows(visible)), {"matched": True, "message_id": "new-1"}, visible[:20])

    def test_a_different_or_partial_text_is_not(self):
        for visible in ("山田 太郎 送信済み [kimeru #7] 判断が必要 今日の 10:33.",                      # cut short
                        "山田 太郎 送信済み [kimeru #8] 別件 " + self.SENT + " 今日の 10:33.",       # inside another post
                        "あ" * 120 + " " + self.SENT):                                                  # too much before it
            self.assertFalse(readback(self.SENT, self.rows(visible))["matched"], visible[:30])


class TestAnchorIsTheNewestMessages(unittest.TestCase):
    def test_send_and_post_keep_only_the_newest_messages_as_the_anchor(self):
        self.assertIn("function Get-AnchorIds($messages, [int]$keep = 3)", SRC)
        self.assertEqual(SRC.count("$baseAllIds = Get-AnchorIds $baseMessages"), 2)

if __name__ == "__main__":
    unittest.main()
