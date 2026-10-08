"""The trial autopilot: `setup-managed.cmd trial` / `report`. The result sheet is computed in Python (kimeru/autorun.py) from the
records of the scheduled run: numbers, OK/NG and short codes only. Everything here is synthetic; nothing touches the Task
Scheduler, the environment variables or the clipboard."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401
try:
    from . import hidden_words
except ImportError:
    import hidden_words

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kimeru import autorun, daily, graph, notify, plan
from kimeru.backends import ReplayBackend
from kimeru.cli import process

ROOT = Path(__file__).resolve().parent.parent
PS1 = ROOT / "tools" / "setup-managed.ps1"
CMD = ROOT / "setup-managed.cmd"
START = datetime(2026, 10, 8, 9, 0, 0)
END = START + timedelta(hours=8)

FAKE_TITLE = "架空の題名ZZQ"      # words that must never reach the sheet
FAKE_ORG = "fake-org-zzq"
FAKE_PROJECT = "Fake Project ZZQ"
FAKE_PERSON = "架空 花子"


def stamp(t):
    return t.isoformat(timespec="seconds")


def utc(t):
    """A local naive time as the UTC string the decision records use."""
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def put_jsonl(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def decision(t, kind, eid, *, node="x", outcome="decide", human=False, notify_=False, actions=None, title=FAKE_TITLE):
    return {"at": utc(t), "event_kind": kind, "event_id": eid, "node": node, "outcome": outcome,
            "needs_human": human, "notify": notify_, "actions": actions or [],
            "event": {"title": title, "author": FAKE_PERSON, "origin": {"org": FAKE_ORG, "project": FAKE_PROJECT}}}


def p(priority):
    return [{"type": "ado.update", "fields": {"Microsoft.VSTS.Common.Priority": priority}}]


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def trial(self, **extra):
        rec = {"start": stamp(START), "end": stamp(END), "hours": 8, "minutes": 5, "ado": True, "sample": "dropped",
               "sample_id": autorun.SAMPLE_ID, **extra}
        (self.out / autorun.FILE).write_text(json.dumps(rec), encoding="utf-8")

    def build(self, now=END):
        return autorun.build_report(self.out, now=now)

    def line(self, lines, name, nth=0):
        got = [l for l in lines if l.startswith(name)]
        self.assertTrue(len(got) > nth, f"no {name!r} line in\n" + "\n".join(lines))
        return got[nth]

    def synthetic(self):
        """3 cycles (one over the time limit, one with the judge down and a failed step), 5 decisions + the fictional item,
        2 posted confirmations + the fictional one, replies OK / NG / 保留 + a reply to the fictional item."""
        t = START
        log = []
        for i, rep in enumerate([
                {"pull_teams": 2, "judge": 2, "notify": [1], "notices": 1, "waiting": 0, "brief": 2, "perf": {"chats_opened": 1}},
                {"pull_teams": 0, "judge": "error: ValueError: boom", "notify": [], "waiting": 3, "busy": {"notify": "lock"}},
                {"pull_teams": 1, "judge": 1, "notify": [2], "notices": 0, "waiting": 0}]):
            s = t + timedelta(minutes=5 * i)
            log.append({"at": stamp(s), "step": "config", "effective": {}})
            if i == 0:
                log.append({"at": stamp(s + timedelta(seconds=5)), "step": "judge", "over_budget": 4})
            if i == 1:
                log.append({"at": stamp(s + timedelta(seconds=5)), "step": "judge", "waiting": "kev down"})
                log.append({"at": stamp(s + timedelta(seconds=6)), "step": "judge", "read_budget": "キーボード・マウスを使っている間は"})
            log.append({"at": stamp(s + timedelta(seconds=20 + 10 * i)), "step": "cycle", "report": rep})
        put_jsonl(self.out / "daily.log.jsonl", log)
        d = START + timedelta(minutes=1)
        put_jsonl(self.out / "decisions.jsonl", [
            decision(d, "teams.chat", "c1", human=True),
            decision(d, "teams.chat", "c2"),
            decision(d, "ado.workitem.created", "501", node="set_p2", actions=p(2)),
            decision(d, "ado.workitem.created", "502", node="set_p3", actions=p(3)),
            decision(d, "ado.workitem.created", "503", node="request_info", human=True, title=FAKE_TITLE),
            decision(d, "monitor.alert", "a1", notify_=True),
            decision(d, "ado.workitem.created", autorun.SAMPLE_ID, node="request_info", human=True, title="試験: x"),
            decision(START - timedelta(hours=3), "teams.chat", "old1"),      # before the window
        ])
        put_jsonl(self.out / "notices.jsonl", [decision(d, "monitor.alert", "a1", notify_=True),
                                               decision(d, "ado.workitem.created", autorun.SAMPLE_ID, notify_=True)])
        items = {
            "1": {"key": "teams-chat:c1:reply", "status": "approved", "posted": True,
                  "record": {"at": utc(d), "event_kind": "teams.chat", "event_id": "c1",
                             "actions": [{"type": "teams.reply", "text": "了解です"}]}},
            "2": {"key": "workitem-intake:503:request_info", "status": "pending", "posted": True,
                  "record": {"at": utc(d), "event_kind": "ado.workitem.created", "event_id": "503", "actions": []}},
            "3": {"key": f"workitem-intake:{autorun.SAMPLE_ID}:request_info", "status": "rejected", "posted": True,
                  "record": {"at": utc(d), "event_kind": "ado.workitem.created", "event_id": autorun.SAMPLE_ID, "actions": []}},
        }
        (self.out / "approvals.json").write_text(json.dumps({"next": 4, "items": items}), encoding="utf-8")
        a = utc(START + timedelta(minutes=7))
        put_jsonl(self.out / "approvals.log.jsonl", [
            {"at": a, "key": "teams-chat:c1:reply", "id": 1, "status": "approved"},
            {"at": a, "key": "workitem-intake:503:request_info", "id": 2, "status": "held"},
            {"at": a, "key": "teams-chat:c9:reply", "id": 9, "status": "rejected"},
            {"at": a, "key": f"workitem-intake:{autorun.SAMPLE_ID}:request_info", "id": 3, "status": "rejected"},
        ])


class TestReport(Fixture):
    def test_numbers_from_synthetic_records(self):
        self.trial()
        self.synthetic()
        r = self.build()
        text = "\n".join(r)
        self.assertIn("NG=", r[0])
        self.assertIn("実行 3 / 見込み 96", self.line(r, "run"))
        self.assertIn("時間切れ（持ち越しあり）のサイクル 1", self.line(r, "limit"))
        self.assertIn("使えなかったサイクル 1", self.line(r, "kev"))
        self.assertIn("judge 1", self.line(r, "failed"))
        self.assertIn("notify 1", self.line(r, "busy"))
        # the fictional item is left out of every real count
        self.assertIn("teams 2 / ado 3 / alert 1", self.line(r, "events"))
        self.assertIn("自動 3 / PM へ 2 / 通知 1", self.line(r, "decide"))
        self.assertIn("P1 通知 1 / 情報不足の確認待ち 1", self.line(r, "notice"))
        self.assertIn("P2 1 / P3 1", self.line(r, "ado"))
        self.assertIn("投稿した確認待ち 2 / 配信結果不明 0", self.line(r, "posts"))
        self.assertIn("OK 1 / NG 1 / 保留 1", self.line(r, "replies"))
        self.assertIn("推定）1", self.line(r, "ready"))
        sample = self.line(r, "sample")
        self.assertIn("投稿済み", sample)
        self.assertIn("返信 NG", sample)
        self.assertIn("開いたチャット 1 / 戻せなかった 0", self.line(r, "screen", 0))
        self.assertIn("キーボード・マウス 1", self.line(r, "screen", 1))
        self.assertIn("記録なし", self.line(r, "screen", 2))
        self.assertIn("10/08 投稿", self.line(r, "brief"))
        self.assertEqual(hidden_words.found(text), [])

    def test_time_per_cycle(self):
        self.trial()
        self.synthetic()
        t = self.line(self.build(), "time")
        self.assertRegex(t, r"中央値 30 秒 / 最大 40 秒")   # cycles took 20, 30 and 40 seconds

    def test_the_sheet_holds_no_title_name_organization_or_path(self):
        self.trial()
        self.synthetic()
        text = "\n".join(self.build())
        for secret in (FAKE_TITLE, FAKE_ORG, FAKE_PROJECT, FAKE_PERSON, "試験: x", "了解です", str(self.out), "kimeru-test-state", "\\"):
            self.assertNotIn(secret, text)
        self.assertEqual(hidden_words.found(text), [])

    def test_verdicts_a_good_run_has_no_ng(self):
        self.trial()
        put_jsonl(self.out / "daily.log.jsonl", [
            {"at": stamp(START), "step": "config"}, {"at": stamp(START + timedelta(seconds=9)), "step": "cycle", "report": {"judge": 1, "waiting": 0}}])
        r = self.build()
        self.assertIn("NG=0", r[0])
        self.assertFalse([l for l in r if re.match(r"^\S+\s+NG\b", l)])

    def test_verdict_no_cycle_ran(self):
        self.trial()
        r = self.build()
        self.assertIn("NG=1", r[0])
        self.assertRegex(self.line(r, "cycles"), r"cycles\s+NG")

    def test_verdict_a_failed_step(self):
        self.trial()
        put_jsonl(self.out / "daily.log.jsonl", [
            {"at": stamp(START), "step": "config"},
            {"at": stamp(START + timedelta(seconds=9)), "step": "cycle", "report": {"pull_teams": "error: OSError: x", "waiting": 0}}])
        r = self.build()
        self.assertRegex(self.line(r, "steps"), r"steps\s+NG")
        self.assertIn("pull_teams 1", self.line(r, "failed"))

    def test_verdict_the_judge_down_in_more_than_half_of_the_cycles(self):
        self.trial()
        rows = []
        for i in range(4):
            s = START + timedelta(minutes=5 * i)
            rows += [{"at": stamp(s), "step": "config"}]
            if i < 3:
                rows += [{"at": stamp(s + timedelta(seconds=2)), "step": "judge", "waiting": "x"}]
            rows += [{"at": stamp(s + timedelta(seconds=9)), "step": "cycle", "report": {"waiting": 1 if i < 3 else 0}}]
        put_jsonl(self.out / "daily.log.jsonl", rows)
        self.assertRegex(self.line(self.build(), "judge"), r"judge\s+NG")
        rows = [r for r in rows if r.get("waiting") != "x"]
        put_jsonl(self.out / "daily.log.jsonl", rows)
        self.assertNotRegex("\n".join(self.build()), r"judge\s+NG")

    def test_verdict_delivery_unknown(self):
        self.trial()
        self.synthetic()
        ap = json.loads((self.out / "approvals.json").read_text(encoding="utf-8"))
        ap["items"]["2"]["delivery_unknown"] = True
        ap["notice_delivery_unknown"] = {"k": "t"}
        (self.out / "approvals.json").write_text(json.dumps(ap), encoding="utf-8")
        (self.out / "daily_state.json").write_text(json.dumps({"brief_delivery_unknown": {"date": "2026-10-08"}}), encoding="utf-8")
        r = self.build()
        self.assertRegex(self.line(r, "delivery"), r"delivery\s+NG.*3 件")
        self.assertIn("配信結果不明 3", self.line(r, "posts"))

    def test_verdict_a_chat_that_could_not_be_put_back(self):
        self.trial()
        put_jsonl(self.out / "daily.log.jsonl", [
            {"at": stamp(START), "step": "config"},
            {"at": stamp(START + timedelta(seconds=3)), "step": "read_restore", "alerts": ["failed"]},
            {"at": stamp(START + timedelta(seconds=4)), "step": "read_restore", "alerts": ["self"]},
            {"at": stamp(START + timedelta(seconds=9)), "step": "cycle", "report": {"waiting": 0}}])
        r = self.build()
        self.assertRegex(self.line(r, "teams"), r"teams\s+NG")
        self.assertIn("戻せなかった 1 / 自分のチャットへ移した 1", self.line(r, "screen", 0))

    def test_the_brief_is_reported_per_day(self):
        self.trial(end=stamp(START + timedelta(hours=30)))
        put_jsonl(self.out / "daily.log.jsonl", [
            {"at": stamp(START), "step": "config"},
            {"at": stamp(START + timedelta(seconds=9)), "step": "cycle", "report": {"brief": 2, "waiting": 0}},
            {"at": stamp(START + timedelta(hours=24, seconds=9)), "step": "cycle", "report": {"brief": "error: brief delivery failed", "waiting": 0}}])
        r = self.build(now=START + timedelta(hours=30))
        b = self.line(r, "brief")
        self.assertIn("10/08 投稿", b)
        self.assertIn("10/09 NG", b)

    def test_an_ended_trial_is_cut_at_its_end(self):
        self.trial()
        autorun.mark_ended(self.out, "manual", now=START + timedelta(hours=1))
        put_jsonl(self.out / "daily.log.jsonl", [
            {"at": stamp(START), "step": "config"}, {"at": stamp(START + timedelta(seconds=9)), "step": "cycle", "report": {"waiting": 0}},
            {"at": stamp(START + timedelta(hours=2)), "step": "config"}, {"at": stamp(START + timedelta(hours=2, seconds=9)), "step": "cycle", "report": {"waiting": 0}}])
        r = self.build(now=END)
        self.assertIn("実行 1 / 見込み 12", self.line(r, "run"))
        self.assertIn("手で停止", self.line(r, "window"))

    def test_report_without_a_trial_uses_since(self):
        put_jsonl(self.out / "daily.log.jsonl", [
            {"at": stamp(START), "step": "config"}, {"at": stamp(START + timedelta(seconds=9)), "step": "cycle", "report": {"waiting": 0}},
            {"at": stamp(START + timedelta(hours=2)), "step": "config"}, {"at": stamp(START + timedelta(hours=2, seconds=9)), "step": "cycle", "report": {"waiting": 0}}])
        r = autorun.build_report(self.out, since=START + timedelta(hours=1), now=END)
        self.assertRegex(self.line(r, "run"), r"実行 1 ")

    def test_an_empty_folder_gives_a_sheet(self):
        r = autorun.build_report(self.out, now=END)
        self.assertTrue(any(l.startswith("cycles") and "NG" in l for l in r))

    def test_the_file_is_written_with_a_bom_and_crlf(self):
        self.trial()
        self.synthetic()
        dest = self.out / "result.txt"
        autorun.write_report(self.out, dest=dest, now=END)
        raw = dest.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", raw)
        self.assertTrue(raw.decode("utf-8-sig").startswith("kimeru 試験運転の結果"))


class TestStartAndEnd(Fixture):
    def test_start_records_the_window(self):
        rec = autorun.start_trial(self.out, hours=8, minutes=5, ado=True, now=START)
        self.assertEqual((rec["start"], rec["end"], rec["minutes"], rec["ado"], rec["sample"]), (stamp(START), stamp(END), 5, True, "no"))
        self.assertEqual(json.loads((self.out / autorun.FILE).read_text(encoding="utf-8"))["end"], stamp(END))
        self.assertFalse((self.out / "inbox").exists())

    def test_hours_are_whole_when_they_are_whole(self):
        self.assertEqual(autorun.start_trial(self.out, hours=8.0, now=START)["hours"], 8)
        self.assertEqual(autorun.start_trial(self.out, hours=0.5, now=START)["end"], stamp(START + timedelta(minutes=30)))

    def test_the_notes_say_what_happens_in_plain_words(self):
        rec = autorun.start_trial(self.out, hours=8, minutes=5, now=START, sample=True)
        text = "\n".join(autorun.start_notes(rec))
        for needle in ("5 分ごと", "自分とのチャット", "10/08 17:00", "setup-managed.cmd remove", "ADO へは何も書きません", "試験用", "NG 番号"):
            self.assertIn(needle, text)
        self.assertEqual(hidden_words.found(text), [])

    def test_the_sample_is_dropped_with_its_origin_only_when_given(self):
        autorun.start_trial(self.out, ado=True, sample=True, org=FAKE_ORG, project=FAKE_PROJECT, now=START)
        f = self.out / "inbox" / f"ado-sample-{autorun.SAMPLE_ID}.json"
        payload = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(payload["resource"]["id"], int(autorun.SAMPLE_ID))
        self.assertTrue(payload["resource"]["fields"]["System.Title"].startswith("試験:"))
        self.assertEqual(payload["kimeru_origin"], {"org": FAKE_ORG, "project": FAKE_PROJECT})
        f.unlink()
        autorun.start_trial(self.out, ado=False, sample=True, org=FAKE_ORG, project=FAKE_PROJECT, now=START)
        self.assertNotIn("kimeru_origin", json.loads(f.read_text(encoding="utf-8")))

    def test_the_sample_is_not_added_twice(self):
        (self.out / "processed.txt").write_text(f"workitem-intake@abcd1234:ado.workitem.created:{autorun.SAMPLE_ID}\n", encoding="utf-8")
        rec = autorun.start_trial(self.out, sample=True, now=START)
        self.assertEqual(rec["sample"], "already")
        self.assertFalse((self.out / "inbox").exists())

    def test_overdue_and_ended(self):
        self.assertFalse(autorun.overdue(self.out, now=END))                    # no trial recorded
        autorun.start_trial(self.out, hours=8, now=START)
        self.assertFalse(autorun.overdue(self.out, now=END - timedelta(minutes=1)))
        self.assertTrue(autorun.overdue(self.out, now=END))
        autorun.mark_ended(self.out, "timer", now=END)
        self.assertFalse(autorun.overdue(self.out, now=END + timedelta(days=1)))
        self.assertEqual(autorun.read_trial(self.out)["ended_by"], "timer")

    def test_the_cli_commands(self):
        env = {"KIMERU_STATE_DIR": str(self.out / "state")}
        run = lambda *a: subprocess.run([sys.executable, "-m", "kimeru", "--out", str(self.out), "schedule", *a], cwd=ROOT,
                                        capture_output=True, text=True, encoding="utf-8", env={**__import__("os").environ, **env})
        r = run("trial-start", "--hours", "2", "--minutes", "10", "--sample")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("end="))
        self.assertTrue((self.out / "inbox" / f"ado-sample-{autorun.SAMPLE_ID}.json").exists())
        dest = self.out / "sheet.txt"
        r = run("report", "--end-trial", "manual", "--write", str(dest))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("kimeru 試験運転の結果", r.stdout)
        self.assertTrue(dest.exists())
        self.assertEqual(autorun.read_trial(self.out)["ended_by"], "manual")
        self.assertEqual(run("report", "--since", "not-a-time").returncode, 2)


class FakeTeams:
    """The self chat in memory (the same shape the daily cycle drives in tests/test_daily.py)."""

    def __init__(self):
        self.timeline, self.posts = [], []

    def chats(self):
        return []

    def post(self, text, send):
        self.posts.append((text, send))
        if send and text.startswith("[kimeru #"):
            self.timeline.append("P:" + text.split("#", 1)[1].split("]", 1)[0])
        return {"ok": True, "typed": True, "sent": bool(send),
                "readback": {"matched": bool(send), "message_id": f"fake-{len(self.posts)}" if send else ""}}

    def read(self):
        return {"ok": True, "timeline": list(self.timeline)}


class TestTheSampleThroughTheRealCycle(Fixture):
    """The fictional item goes through daily.cycle: it is posted as a confirmation with its line and link, NG closes it, and it
    stays out of the sheet's real counts."""

    def test_first_cycle_posts_it_and_ng_closes_it(self):
        pbs = plan.load_playbooks(ROOT / "playbooks")
        graphs = graph.load_dir(ROOT / "graphs", pbs)
        inbox = self.out / "inbox"
        now = datetime.now().replace(microsecond=0)
        autorun.start_trial(self.out, hours=1, minutes=5, ado=True, sample=True, org=FAKE_ORG, project=FAKE_PROJECT, now=now - timedelta(minutes=1))
        teams = FakeTeams()
        judge = ReplayBackend({"ready": {"type": "noul", "noul": 0.1}})   # the judge finds the information missing
        run = lambda: daily.cycle(self.out, inbox, graphs, judge, pbs, process, bridge=teams, send=True, now=now)
        r = run()
        self.assertEqual(r["judge"], 1, r)
        post = next(t for t, s in teams.posts if f"#{autorun.SAMPLE_ID}" in t)
        self.assertIn("試験:", post)
        self.assertIn(f"https://dev.azure.com/{FAKE_ORG}/Fake%20Project%20ZZQ/_workitems/edit/{autorun.SAMPLE_ID}", post)
        n = r["notify"][0]
        teams.timeline.append(f"R:NG {n}")
        r = run()
        self.assertEqual(r["approvals"], [f"#{n}:rejected"])
        sheet = autorun.build_report(self.out, now=now + timedelta(minutes=2))
        text = "\n".join(sheet)
        self.assertIn("取り込み: なし", self.line(sheet, "events"))
        self.assertIn("投稿した確認待ち 0", self.line(sheet, "posts"))
        self.assertIn("返信 NG", self.line(sheet, "sample"))
        self.assertNotIn("試験:", text)
        self.assertNotIn(FAKE_ORG, text)
        self.assertEqual(hidden_words.found(text), [])


POWERSHELL = shutil.which("powershell")


class TestScripts(unittest.TestCase):
    src = PS1.read_text(encoding="utf-8-sig")

    def test_the_actions_and_the_end_task(self):
        for needle in ("'trial'", "'report'", "'trial-end'", "$endTask = 'kimeru-trial-end'", "[switch]$Sample", "[double]$Hours",
                       "<StartWhenAvailable>true</StartWhenAvailable>", "<RunLevel>LeastPrivilege</RunLevel>", "<Hidden>true</Hidden>",
                       "schedule', 'trial-start'", "schedule', 'report'", "kimeru-autorun-result.txt", "Set-Clipboard",
                       "function Test-TrialOverdue", "function End-Trial", "function Register-EndTask", "run-trial-end.vbs"):
            self.assertIn(needle, self.src, needle)
        self.assertNotIn("/RL HIGHEST", self.src)
        self.assertNotIn("RunLevel>HighestAvailable", self.src)

    def test_remove_stops_the_end_task_and_a_running_trial_writes_its_sheet(self):
        remove = self.src[self.src.index("  'remove' {"):]
        self.assertIn("End-Trial 'manual'", remove)
        self.assertIn("Remove-Everything", remove)
        self.assertIn("Remove-EndTask", self.src[self.src.index("function Remove-Everything"):self.src.index("function End-Trial")])

    def test_an_overdue_trial_is_ended_by_status_and_report(self):
        for action in ("  'status' {", "  'report' {"):
            block = self.src[self.src.index(action):]
            block = block[:block.index("\n  }\n")]
            self.assertIn("Test-TrialOverdue", block, action)
            self.assertIn("End-Trial 'timer'", block, action)

    def test_a_failed_trial_setup_leaves_nothing_running(self):
        block = self.src[self.src.index("if ($Action -eq 'trial')"):]
        block = block[:block.index("\n  'remove'")]
        self.assertIn("Start-Trial", block)
        self.assertIn("Remove-Everything", block)

    def test_native_stderr_is_kept_from_becoming_an_error(self):
        report = self.src[self.src.index("function Write-Report"):self.src.index("function Remove-EndTask")]
        self.assertIn("$ErrorActionPreference = 'Continue'", report)
        self.assertIn("2>&1", report)
        self.assertNotRegex(self.src, r"python -c")                             # no quotes inside python -c arguments
        self.assertIn('cmd /c "schtasks /Delete /TN $endTask /F >nul 2>nul"', self.src)

    def test_the_text_does_not_say_where_it_runs(self):
        launcher = CMD.read_text(encoding="ascii")
        self.assertIn("trial", launcher)
        self.assertEqual(hidden_words.found(self.src + launcher), [])
        self.assertEqual(hidden_words.found((ROOT / "kimeru" / "autorun.py").read_text(encoding="utf-8")), [])

    def test_the_script_keeps_its_bom_and_the_launcher_is_ascii(self):
        raw = PS1.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "PowerShell 5.1 needs the BOM")
        CMD.read_bytes().decode("ascii")

    def test_the_result_file_is_not_committed(self):
        self.assertIn("kimeru-autorun-result.txt", (ROOT / ".gitignore").read_text(encoding="utf-8"))

    @unittest.skipUnless(sys.platform == "win32" and POWERSHELL, "Windows PowerShell is needed to parse the script")
    def test_the_script_parses(self):
        r = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                            "$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                            f"'{PS1}', [ref]$t, [ref]$e); if ($e) {{ $e | ForEach-Object {{ $_.Message }}; exit 1 }}"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
