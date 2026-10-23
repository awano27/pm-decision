"""Run the production Teams timeline walker on synthetic UIA-shaped nodes only."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import notify
from tests.test_notify import FakeBridge, REC, TimelineBridge, write_queue


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "teams-self.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def production_walk_code():
    """Return the actual read-action collection and Walk function, excluding real UI access."""
    source = SCRIPT.read_text(encoding="utf-8")
    read = source.index("if ($Action -eq 'read') {")
    start = source.index("  $posts = New-Object", read)
    end = source.index("  Walk $w 0", start) + len("  Walk $w 0")
    code = source[start:end]
    walker_setup = "  $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker"
    if walker_setup not in code:
        raise AssertionError("production read walker setup changed; update the synthetic adapter")
    return code.replace(walker_setup, "  $walker = $SyntheticWalker", 1)


def synthetic_timeline(names):
    """Run the production Walk against a tiny in-memory tree; never launches Teams/UIA."""
    runner = POWERSHELL
    code = r'''
function New-Node([string]$name, [object[]]$children = @(), [string]$kind = 'Text', [string]$aid = '', [string]$class = '') {
  $node = [pscustomobject]@{ Current = [pscustomobject]@{
    Name = $name; ControlType = [pscustomobject]@{ ProgrammaticName = ('ControlType.' + $kind) }
    AutomationId = $aid; ClassName = $class
  }; FirstChild = $null; NextSibling = $null; Parent = $null }
  $prev = $null
  foreach ($child in $children) {
    $child.Parent = $node
    if ($prev) { $prev.NextSibling = $child } else { $node.FirstChild = $child }
    $prev = $child
  }
  return $node
}
function Convert-Node($item) {
  if ($item -is [string]) { return (New-Node $item) }
  $children = @()
  foreach ($child in $item.children) { $children += (Convert-Node $child) }
  $nested = @()
  foreach ($child in $item.nested) { $nested += (Convert-Node $child) }
  if ($nested.Count -gt 0 -and $children.Count -gt 0) {
    $children[0].FirstChild = $nested[0]
    for ($i = 0; $i -lt $nested.Count; $i++) {
      $nested[$i].Parent = $children[0]
      if ($i + 1 -lt $nested.Count) { $nested[$i].NextSibling = $nested[$i + 1] }
    }
  }
  $kind = if ($item.kind) { [string]$item.kind } else { 'Text' }
  return (New-Node ([string]$item.name) $children $kind ([string]$item.aid) ([string]$item.class))
}
$SyntheticWalker = [pscustomobject]@{}
$SyntheticWalker | Add-Member ScriptMethod GetFirstChild { param($node) $node.FirstChild }
$SyntheticWalker | Add-Member ScriptMethod GetNextSibling { param($node) $node.NextSibling }
$names = ConvertFrom-Json -InputObject $env:InputNames
$nodes = @()
foreach ($item in $names) {
  $nodes += (Convert-Node $item)
}
$w = New-Node '' $nodes
'''
    code += "\n" + production_walk_code() + "\nConvertTo-Json -InputObject @($timeline) -Compress\n"
    with tempfile.TemporaryDirectory(prefix="kimeru-walk-") as d:
        payload = json.dumps(names, ensure_ascii=False)
        env = {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
               "WINDIR": os.environ.get("WINDIR", ""), "PYTHONUTF8": "1"}
        proc = subprocess.run([runner, "-NoProfile", "-NonInteractive", "-Command", code], input="",
                              capture_output=True, text=True, encoding="utf-8", timeout=180,
                              env={**env, "InputNames": payload, "TEMP": d, "TMP": d})
    if proc.returncode:
        raise AssertionError(proc.stderr or proc.stdout)
    output = proc.stdout.strip().splitlines()[-1]
    return json.loads(output) if output.startswith("[") else [output]


def non_reply_subtrees():
    """Synthetic exclusion markers; these are not claimed Teams build identities."""
    return [
        {"name": "OK 1", "kind": "Edit", "children": ["NG 1", "再実行 1"]},
        {"name": "OK 1", "kind": "Document", "aid": "new-message-editor", "children": ["済 1"]},
        {"name": "OK 1", "aid": "new-message-draft", "children": ["下書き 1: no"]},
        {"name": "OK 1", "aid": "quoted-reply", "children": ["修正 1: no"]},
        {"name": "OK 1", "class": "blockquote", "children": ["NG 1"]},
        # An aggregate Name must not bypass its excluded child.
        {"name": "OK 1", "children": [{"name": "OK 1", "kind": "Edit"}]},
        {"name": "NG 1", "children": [{"name": "NG 1", "class": "quote"}]},
        {"name": "quoted text\nOK 1", "children": []},
    ]


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "needs Windows PowerShell (UI Automation types)")
class TestApprovalReaderIsolation(unittest.TestCase):
    def test_document_container_preserves_timeline_and_legitimate_reply(self):
        timeline = synthetic_timeline([{
            "name": "[message view]", "kind": "Document", "children": [
                {"name": "[kimeru #1] original"},
                *non_reply_subtrees(),
                {"name": "OK 1"},
            ],
        }])
        self.assertEqual(timeline, ["P:1", "R:OK 1"])

    def test_inputs_quotes_and_aggregate_names_are_not_replies(self):
        timeline = synthetic_timeline([
            {"name": "[kimeru #1] original"},
            *non_reply_subtrees(),
            {"name": "ＯＫ　１"},
        ])
        self.assertEqual(timeline, ["P:1", "R:OK 1"])

    def test_exclusions_leave_pending_and_valid_sibling_approves_once(self):
        nodes = [{"name": "[kimeru #1] original"}, *non_reply_subtrees()]
        excluded = synthetic_timeline(nodes)
        valid = synthetic_timeline([*nodes, {"name": "OK 1"}])
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC)
            notify.notify(d, FakeBridge(), send=True)
            before = notify.Approvals(d).data
            with mock.patch("kimeru.execute.run_approved", side_effect=AssertionError("external write attempted")) as run:
                self.assertEqual(notify.collect(d, TimelineBridge(excluded)), [])
                self.assertEqual(notify.Approvals(d).data, before)
                changes = notify.collect(d, TimelineBridge(valid))
                self.assertEqual([change["status"] for change in changes], ["approved"])
                self.assertEqual(notify.collect(d, TimelineBridge(valid)), [])
                run.assert_not_called()

    def test_owned_post_subtrees_never_become_replies_and_siblings_still_do(self):
        timeline = synthetic_timeline([
            {"name": "[kimeru #1] original\nOK 1", "children": ["NG 1", "wrapper"],
             "nested": ["再実行 1", "修正 1: change", "下書き 1: copy"]},
            {"name": "[kimeru brief 2026-10-17] pending case summary", "children": ["wrapper"],
             "nested": ["OK 1", "再実行 1", "修正 1: write", "下書き 1: copy"]},
            {"name": "[kimeru 試験 #2] test body", "children": ["NG 2", "nested"],
             "nested": ["OK 2"]},
            {"name": "[kimeru 実行 #3 1] result body", "children": ["再実行 3", "child"],
             "nested": ["修正 3: no", "下書き 3: no"]},
            {"name": "[kimeru 試験 実行 #4 2] test result", "children": ["OK 4"]},
            {"name": "OK 4", "children": []},
        ])
        self.assertEqual(timeline, ["P:1", "T:2", "X:3:1", "TX:4:2", "R:OK 4"])

    def test_extracted_timeline_cannot_approve_or_write_in_collect(self):
        timeline = synthetic_timeline([
            {"name": "[kimeru #1] original\nOK 1", "children": ["wrapper"],
             "nested": ["OK 1", "NG 1", "再実行 1", "修正 1: write this", "下書き 1: write this"]},
            {"name": "[kimeru brief 2026-10-17] summary", "children": ["wrapper"],
             "nested": ["OK 1", "再実行 1", "修正 1: write this", "下書き 1: write this"]},
            {"name": "[kimeru 試験 #2] test", "children": ["NG 2"]},
            {"name": "[kimeru 実行 #3 1] result", "children": ["再実行 3"]},
            *non_reply_subtrees(),
        ])
        with tempfile.TemporaryDirectory() as d:
            rec = {**REC, "actions": [{"type": "ado.comment", "id": "4812", "text": "must not be written"}],
                   "event": {"origin": {"org": "example", "project": "example"}}}
            write_queue(d, rec)
            bridge = FakeBridge()
            with mock.patch("kimeru.execute.enabled_types", return_value={"ado.comment"}):
                notify.notify(d, bridge, send=True, real=True)
            before = notify.Approvals(d).data
            with mock.patch("kimeru.execute.run_approved", side_effect=AssertionError("ADO write attempted")) as run, \
                    mock.patch("kimeru.execute._token", side_effect=AssertionError("token requested")), \
                    mock.patch("kimeru.pull.http_json", side_effect=AssertionError("HTTP attempted")), \
                    mock.patch("kimeru.notify.writer_mod.get_writer", return_value=None):
                self.assertEqual(notify.collect(d, TimelineBridge(timeline), real=True), [])
                run.assert_not_called()
            after = notify.Approvals(d).data
            self.assertEqual(after, before)
            self.assertEqual(after["items"]["1"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
