"""kimeru diagnose (run-diagnose.cmd -> tools/diagnose.ps1): the sheet explains unknown deliveries, judgment paths and cycle
errors with numbers and names of graph nodes only. A made-up title, organization, chat name, person, path and event id are put
into every record it reads, and none of them may reach the sheet."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401
try:
    from . import hidden_words
except ImportError:
    import hidden_words

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kimeru import diagnose, fsutil, notify

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ("架空の題名ZQX", "fakeorg-zqx", "架空プロジェクトZQX", "架空チャット名ZQX", "山田架空", "C:\\Users\\zqx",
           "98765", "msg-id-zqx", "secret-pattern-zqx")
NOW = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)


def _at(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def post_body(n):
    return (f"[kimeru #{n}] 判断が必要（ado_workitem） / 改訂 1\n判断: 架空の題名ZQX を確認\n"
            f"ado.workitem.created #98765\n作業項目 98765「架空の題名ZQX」（fakeorg-zqx / 架空プロジェクトZQX）\n"
            f"元: 山田架空: 架空チャット名ZQX で相談\n⚠ 元の材料に無い日付\n返信: OK {n} / NG {n} / 保留 {n}")


def make_folder(d):
    """A data folder after a trial where every post ended as unknown (the texts are made up)."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    items = {
        "3": {"key": "ado_workitem:98765:triage_pm", "posted": False, "status": "pending", "revision": 1,
              "record": {"event_kind": "ado.workitem.created", "event_id": "98765", "summary": "架空の題名ZQX"},
              "delivery_unknown": True, "delivery_unknown_part": "post", "delivery_unknown_at": _at(30),
              "delivery_attempt": {"kind": "case", "target": "3", "part": "post", "revision": 1,
                                   "identity": "ado_workitem:98765:triage_pm", "at": _at(30), "body": post_body(3)}},
        "4": {"key": "teams_chat:msg-id-zqx:ask_pm", "posted": False, "status": "pending", "revision": 1,
              "record": {"event_kind": "teams.chat", "event_id": "msg-id-zqx", "summary": "架空チャット名ZQX"},
              "delivery_unknown": True, "delivery_unknown_part": "copilot_request", "delivery_unknown_at": _at(200),
              "delivery_attempt": {"kind": "case", "target": "4", "part": "copilot_request", "at": _at(200),
                                   "body": "[kimeru #4 Copilot 用]\n山田架空 さんへの返事を 架空チャット名ZQX 向けに"}},
        "5": {"key": "teams_chat:other:ask_pm", "posted": True, "status": "pending", "revision": 1, "record": {}},
    }
    notice_key = "ado_workitem:98765:set_p1"
    ap = {"next": 6, "items": items, "notice_delivery_unknown": {notice_key: _at(50)},
          "notice_delivery_attempts": {notice_key: {"at": _at(50), "body": "[kimeru 通知] 自動で決定しました（ado_workitem）\n元: 架空の題名ZQX"}},
          "outbox_delivery_unknown": True, "outbox_delivery_unknown_at": _at(10),
          "outbox_delivery_attempt": {"at": _at(10), "body": "[kimeru 実行 #3] fakeorg-zqx に書きました"}}
    (d / "approvals.json").write_text(json.dumps(ap, ensure_ascii=False), encoding="utf-8")
    (d / "daily_state.json").write_text(json.dumps({"brief_delivery_unknown": {
        "date": "2026-10-09", "at": _at(120), "part": "brief", "body": "[kimeru brief 2026-10-09] 今日の進め方\n1. 架空の題名ZQX"}},
        ensure_ascii=False), encoding="utf-8")
    decisions = [
        {"graph": "ado_workitem", "event_kind": "ado.workitem.created", "event_id": "98765", "node": "triage_pm", "outcome": "advise",
         "needs_human": True, "notify": False, "judge": {"name": "Kev", "model": "kev"}, "summary": "架空の題名ZQX",
         "event": {"title": "架空の題名ZQX", "org": "fakeorg-zqx", "project": "架空プロジェクトZQX"},
         "path": [{"node": "ready", "answer": {"matched": "secret-pattern-zqx"}, "edge": "yes"},
                  {"node": "priority", "answer": {"score": 1.37, "confidence": 0.41, "probabilities": [0.3, 0.4]}, "edge": "unsure"}]},
        {"graph": "teams_chat", "event_kind": "teams.chat", "event_id": "msg-id-zqx", "node": "ask_pm", "outcome": "advise",
         "needs_human": True, "judge": {"name": "Kev"}, "event": {"author": "山田架空", "chat": "架空チャット名ZQX"},
         "path": [{"node": "needs_reply", "answer": {"noul": 0.52, "confidence": 0.12}, "edge": "unsure"},
                  {"node": "bad", "answer": {"invalid": "架空の題名ZQX broke"}, "edge": "unsure"}]},
        {"graph": "teams_chat", "event_kind": "teams.chat", "event_id": "msg-id-zqx", "node": "fyi", "outcome": "decide",
         "needs_human": False, "notify": False, "judge": {"name": "Kev"},
         "path": [{"node": "needs_reply", "answer": {"noul": 0.05, "confidence": 0.9}, "edge": "no"}]},
    ]
    with (d / "decisions.jsonl").open("w", encoding="utf-8") as f:
        for r in decisions:
            f.write(json.dumps({**r, "at": _at(60)}, ensure_ascii=False) + "\n")
    log = [
        {"at": "2026-10-09T11:00:00", "step": "config", "effective": {"backend": {"source": "file"}}},
        {"at": "2026-10-09T11:01:00", "step": "cycle", "report": {
            "judge": 3, "notify": "error: RuntimeError: #3 was not posted with verified readback", "notices": 0, "waiting": 0,
            "approvals": [], "brief": "error: brief delivery_unknown; use delivery show/confirm/retry", "perf": {"judged": 3}}},
        {"at": "2026-10-09T11:06:00", "step": "brief", "status": "delivery_unknown_hold"},
        {"at": "2026-10-09T11:06:01", "step": "cycle", "report": {
            "judge": 0, "notify": "error: BridgeError: Teams window not found", "notices": "error: ValueError: fakeorg-zqx C:\\Users\\zqx 架空の題名ZQX",
            "approvals": ["#5:approved"], "busy": {"notify": "another run holds approvals.lock"}}},
    ]
    with (d / "daily.log.jsonl").open("w", encoding="utf-8") as f:
        for r in log:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (d / "warnings.jsonl").write_text(json.dumps({"at": _at(5), "writer": "架空の題名ZQX"}, ensure_ascii=False) + "\n", encoding="utf-8")
    return d


def screen():
    """What the probe returns: #3 shown in full, #4 cut to its first node, a result post, someone else's message."""
    full = diagnose.norm(post_body(3))
    copilot = diagnose.norm("[kimeru #4 Copilot 用]\n山田架空 さんへの返事を 架空チャット名ZQX 向けに")
    return {"ok": True, "how": "ids", "rows": 9, "selfTitle": True, "messages": [
        {"text": "架空チャット名ZQX の雑談", "joined": "架空チャット名ZQX の雑談", "raw_lines": 1, "fragments": 1, "top": 0.1, "height": 0.05},
        {"text": full, "joined": full, "raw_lines": 7, "fragments": 1, "top": 0.2, "height": 0.7},
        {"text": "[kimeru #4 Copilot 用]", "joined": copilot, "raw_lines": 1, "fragments": 3, "top": 0.5, "height": 0.2},
        {"text": "[kimeru 実行 #3] fakeorg-zqx に…", "joined": "[kimeru 実行 #3] fakeorg-zqx に…", "raw_lines": 1, "fragments": 1,
         "top": 0.8, "height": 0.04},
    ]}


class FakeBridge:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def probe(self):
        self.calls.append("probe")
        if self.error:
            raise self.error
        return self.result

    def post(self, *a, **k):   # the diagnosis must never post
        raise AssertionError("post called")


class TestSheet(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="kimeru-diag-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        make_folder(self.dir)

    def sheet(self, **kw):
        kw.setdefault("kev", "down")
        return "\n".join(diagnose.build(self.dir, now=NOW, **kw))

    def assertClean(self, text):
        for s in SECRETS:
            self.assertNotIn(s, text)
        self.assertEqual(hidden_words.found(text), [])

    def test_nothing_private_reaches_the_sheet(self):
        self.assertClean(self.sheet(probe=screen(), teams_version="25290.205.4069.4894"))

    def test_unknown_deliveries_are_listed_with_shapes_and_ages(self):
        text = self.sheet()
        self.assertIn("件数 5", text)
        self.assertIn("- case #3 post  30 分前  本文 ", text)
        self.assertIn("- case #4 copilot_request  3 時間前", text)
        self.assertIn("- notice 通知1 notice", text)
        self.assertIn("- outbox #3 outbox", text)
        self.assertIn("- brief 2026-10-09 brief  2 時間前", text)
        self.assertIn("7 行", text)
        self.assertIn("保存された読み返しの記録: なし", text)

    def test_the_probe_compares_numbers_only(self):
        text = self.sheet(probe=screen())
        self.assertIn("読み方 ids / ノード 9 / メッセージ 4 / kimeru の投稿 3", text)
        line3 = next(l for l in text.splitlines() if l.startswith("- case #3 post: "))
        self.assertIn("読み返しの比較 一致", line3)
        line4 = next(l for l in text.splitlines() if l.startswith("- case #4 copilot_request: "))
        self.assertIn("読み返しの比較 不一致", line4)
        self.assertIn("全ノードをつなぐと 一致", line4)
        self.assertIn("ノード 3", line4)
        self.assertIn("画面に見つからない", next(l for l in text.splitlines() if l.startswith("- brief 2026-10-09 brief: ")))
        self.assertIn("- result #3: ", text)
        self.assertIn("省略の印 あり", next(l for l in text.splitlines() if l.startswith("- result #3: ")))
        self.assertIn("高さのメッセージ 1", text)

    def test_a_difference_is_named_by_its_kind_not_its_letter(self):
        c = diagnose.compare("[kimeru #1] ⚠ 架空", "[kimeru #1] ⚠\ufe0f 架空")
        self.assertFalse(c["equal"])
        self.assertIn("U+0020", c["at"])
        self.assertIn("U+FE0F", c["at"])
        c = diagnose.compare("[kimeru #1] 架空A", "[kimeru #1] 架空B")
        self.assertEqual(c["at"], "期待 Lu / 画面 Lu")

    def test_judgment_paths_keep_names_and_numbers(self):
        text = self.sheet()
        self.assertIn("- ado Kev: ready[yes match] > priority[unsure score=1.37 c=0.41] -> triage_pm advise / PM", text)
        self.assertIn("needs_reply[unsure noul=0.52 c=0.12] > bad[unsure invalid] -> ask_pm advise / PM", text)
        self.assertIn("行き先: PM 2, 自動 1", text)
        self.assertIn("  ado, priority(unsure): 1", text)
        self.assertIn("  teams, needs_reply(unsure): 1", text)

    def test_cycles_show_the_exception_class_and_known_messages_only(self):
        text = self.sheet()
        self.assertIn("notify=error(RuntimeError: #3 was not posted with verified readback)", text)
        self.assertIn("notify=error(BridgeError: Teams window not found)", text)
        self.assertIn("notices=error(ValueError)", text)
        self.assertIn("brief=error(brief delivery_unknown; use delivery show/confirm/retry)", text)
        self.assertIn("approvals=ok(1)", text)
        self.assertIn("busy=notify", text)
        self.assertIn("朝のまとめを保留した記録 1 回", text)
        self.assertIn("warnings.jsonl の直近 20 件の項目: writer 1", text)
        self.assertIn("delivery list: 5 件", text)
        self.assertIn("status: last cycle: 2026-10-09T11:06:01", text)

    def test_environment_line(self):
        text = self.sheet(teams_version="25290.205.4069.4894")
        self.assertIn("Teams 25290.205.4069.4894", text)
        self.assertIn("Kev down", text)
        self.assertNotIn("この節を作れませんでした", self.sheet(kev=None))
        self.assertIn("Teams 不明", self.sheet(teams_version="x; rm"))

    def test_an_unknown_message_is_hidden(self):
        self.assertEqual(diagnose.error_text("error: OSError: C:\\Users\\zqx\\x denied"), "OSError")
        self.assertEqual(diagnose.error_text("error: 架空の題名ZQX"), "(内容は非表示)")


class TestProbe(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="kimeru-diag-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        make_folder(self.dir)
        self.before = {p.name: p.read_bytes() for p in self.dir.iterdir()}

    def run_cli(self, bridge, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = diagnose.dispatch(list(args), self.dir, bridge=bridge, folders=[])
        return rc, buf.getvalue()

    def test_a_held_lock_skips_the_probe(self):
        bridge = FakeBridge(screen())
        with fsutil.exclusive(notify.lock_path(self.dir)) as got:
            self.assertTrue(got)
            note = diagnose.run_probe(self.dir, bridge)
        self.assertEqual(note[0], None)
        self.assertIn("approvals.lock", note[1])
        self.assertEqual(bridge.calls, [])

    def test_a_bridge_failure_is_one_short_line(self):
        err = notify.BridgeError("Teams window not found", {"ok": False})
        rc, out = self.run_cli(FakeBridge(error=err))
        self.assertEqual(rc, 0)
        self.assertIn("スキップ（BridgeError: Teams window not found）", out)
        err = notify.BridgeError("架空チャット名ZQX could not be read", {})
        rc, out = self.run_cli(FakeBridge(error=err))
        self.assertIn("スキップ（BridgeError）", out)
        self.assertNotIn("架空チャット名ZQX", out)

    def test_nothing_is_changed_and_nothing_posted(self):
        bridge = FakeBridge(screen())
        rc, out = self.run_cli(bridge)
        self.assertEqual(bridge.calls, ["probe"])
        after = {p.name: p.read_bytes() for p in self.dir.iterdir()}
        self.assertEqual(after, self.before)

    def test_the_file_and_the_drive_copy(self):
        res = self.dir / "result.txt"
        drive = self.dir / "drive" / "kimeru-release"
        drive.mkdir(parents=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            diagnose.dispatch(["--no-probe", "--write", str(res), "--drive"], self.dir, folders=[self.dir / "none", drive])
        self.assertTrue(res.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual((drive / diagnose.RESULT_FILE).read_bytes(), res.read_bytes())
        self.assertIn("Google ドライブへコピーしました", buf.getvalue())
        self.assertNotIn("Google", res.read_text(encoding="utf-8-sig"))   # where it was copied is not in the sheet
        buf = io.StringIO()
        with redirect_stdout(buf):
            diagnose.dispatch(["--no-probe", "--write", str(res), "--drive"], self.dir, folders=[self.dir / "none"])
        self.assertIn("見つかりませんでした", buf.getvalue())

    def test_the_drive_folders_searched(self):
        names = [str(p) for p in diagnose.drive_folders()]
        self.assertTrue(any("マイドライブ" in n for n in names))
        self.assertTrue(any("My Drive" in n for n in names))
        self.assertTrue(any("Google Drive" in n for n in names))
        self.assertTrue(all(n.endswith("kimeru-release") for n in names))


class TestCommandLine(unittest.TestCase):
    def test_python_m_kimeru_diagnose(self):
        d = Path(tempfile.mkdtemp(prefix="kimeru-diag-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        make_folder(d / "data")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "USERPROFILE": str(d), "HOME": str(d)}
        r = subprocess.run([sys.executable, "-m", "kimeru", "--out", str(d / "data"), "diagnose", "--no-probe",
                            "--write", str(d / "r.txt")], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env,
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (d / "r.txt").read_text(encoding="utf-8-sig")
        self.assertIn("== A. 配信結果不明", text)
        self.assertIn("実行しませんでした（--no-probe）", text)
        for s in SECRETS:
            self.assertNotIn(s, text + r.stdout)


class TestScripts(unittest.TestCase):
    def test_the_launcher_and_the_script(self):
        cmd = (ROOT / "run-diagnose.cmd").read_bytes()
        cmd.decode("ascii")
        self.assertIn(rb"tools\diagnose.ps1", cmd)
        ps = (ROOT / "tools" / "diagnose.ps1").read_bytes()
        self.assertTrue(ps.startswith(b"\xef\xbb\xbf"), "PowerShell 5.1 needs the BOM")
        text = ps.decode("utf-8-sig")
        for bad in ("Read-Host", "-Send", "'post'", "Remove-Item $ResultFile", "Invoke-WebRequest"):
            self.assertNotIn(bad, text)
        self.assertIn("kimeru-diagnose-result.txt", (ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_the_probe_action_reads_only(self):
        src = (ROOT / "tools" / "teams-self.ps1").read_text(encoding="utf-8-sig")
        block = src[src.index("if ($Action -eq 'probe') {"):]
        block = block[:block.index("\n}\n") if "\n}\n" in block else block.index("\r\n}\r\n")]
        for bad in ("SendKeys", "SendWait", "Clipboard", "Send-Box", "Get-Box", "SetFocus", "mouse_event"):
            self.assertNotIn(bad, block)
        lock = next(l for l in src.splitlines() if l.startswith("if ($Action -eq 'probe') {") and "Enter-UiLock" in l)
        self.assertLess(src.index(lock), src.index("if ($Action -eq 'probe') {" + chr(10) + "  # read only"))

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell only")
    def test_the_script_runs_under_windows_powershell(self):
        d = Path(tempfile.mkdtemp(prefix="kimeru-diag-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        make_folder(d / "data")
        res = d / "result.txt"
        env = {**os.environ, "KIMERU_STATE_DIR": str(d / "data")}
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "tools" / "diagnose.ps1"),
                            "-NoProbe", "-NoClipboard", "-NoDrive", "-ResultFile", str(res)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = res.read_text(encoding="utf-8-sig")
        self.assertIn("== B. 判断の経路", text)
        self.assertIn("件数 5", text)
        for s in SECRETS:
            self.assertNotIn(s, text)


if __name__ == "__main__":
    unittest.main()
