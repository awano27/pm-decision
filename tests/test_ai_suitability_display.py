"""Offline display regressions for AI judgment suitability and separate approvals."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import cli, notify, report


def record(assessment=None):
    result = {
        "graph": "synthetic", "graph_key": "synthetic@v1:teams.chat:e1",
        "event_kind": "teams.chat", "event_id": "e1", "node": "decision",
        "path": [], "outcome": "decide", "needs_human": True, "notify": False,
        "advice": "対応案を確認してください", "summary": "合成テストの依頼",
        "event": {"kind": "teams.chat", "id": "e1", "chat_id": "synthetic-chat",
                  "author": "担当", "text": "合成テストの依頼"},
        "actions": [{"type": "teams.reply", "chat_id": "synthetic-chat", "text": "確認します"}],
        "executed": [], "revision": 1, "judge": {"name": "Jev"},
    }
    if assessment is not None:
        result["ai_suitability"] = assessment
    return result


def assessment(level, route, reason="与えられた情報で判断できます"):
    return {"level": level, "confidence": 0.9, "reason_code": "synthetic",
            "reason": reason, "reason_confidence": 0.9, "route": route, "status": "assessed"}


def render_report(out, rec):
    (out / "decisions.jsonl").write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "approvals.json").write_text(json.dumps({"items": {"1": {
        "key": "synthetic:e1:decision", "status": "pending", "revision": 1, "record": rec,
    }}}, ensure_ascii=False), encoding="utf-8")
    return report.build(out, {})


class TestSuitabilityDisplay(unittest.TestCase):
    def test_legacy_records_do_not_gain_an_assessment(self):
        legacy = record()
        explicit_none = {**legacy, "ai_suitability": None}
        for display in (cli._fmt, lambda rec: notify.format_post(1, rec), lambda rec: notify.format_detail(1, rec)):
            self.assertEqual(display(legacy), display(explicit_none))
            self.assertNotIn("AI判断適性", display(legacy))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            html = render_report(out, legacy)
            self.assertEqual(html, render_report(out, explicit_none))
            self.assertNotIn("AI判断適性", html)

    def test_high_scores_are_visible_without_approving_pending_work(self):
        for level in (4, 5):
            with self.subTest(level=level), tempfile.TemporaryDirectory() as directory:
                rec = record(assessment(level, "ai"))
                original = copy.deepcopy(rec)
                terminal = cli._fmt(rec)
                post = notify.format_post(1, rec)
                detail = notify.format_detail(1, rec)
                html = render_report(Path(directory), rec)
                for text in (terminal, post, detail, html):
                    self.assertIn(f"{level}/5", text)
                    self.assertIn("AI判断可能", text)
                    self.assertIn("与えられた情報で判断できます", text)
                self.assertIn("/人の確認", terminal)
                self.assertEqual(post.splitlines()[0], notify.format_post(1, record()).splitlines()[0])
                self.assertTrue(post.splitlines()[0].startswith("[kimeru #1]"))
                self.assertTrue(post.splitlines()[1].startswith("AI判断適性: "))
                self.assertEqual(detail.splitlines()[0], "[kimeru 詳細 #1] 判断が必要（synthetic） / 改訂 1")
                self.assertTrue(detail.splitlines()[1].startswith("AI判断適性: "))
                self.assertIn("OK 1 / NG 1", post)
                self.assertIn("詳細 1", post)
                self.assertIn("返信待ち", html)
                self.assertIn("承認待ちのため送っていない", html)
                self.assertEqual(html.count("AI判断適性: "), 2, "case and decision-history views both show the assessment")
                self.assertEqual(rec, original)
                saved = json.loads((Path(directory) / "approvals.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["items"]["1"]["status"], "pending")

    def test_local_information_gap_explains_human_handoff(self):
        reason = "現場固有の前提が不足しています"
        rec = record(assessment(1, "human", reason))
        rec["outcome"] = "advise"
        with tempfile.TemporaryDirectory() as directory:
            texts = (cli._fmt(rec), notify.format_post(1, rec), notify.format_detail(1, rec),
                     render_report(Path(directory), rec))
        for text in texts:
            self.assertIn("1/5", text)
            self.assertIn("人に確認", text)
            self.assertIn(reason, text)

    def test_human_assessment_is_not_presented_as_only_a_draft_check(self):
        rec = record(assessment(2, "human", "現場の条件を確認してください"))
        rec["held_reason"] = "draft"
        with mock.patch.dict(os.environ, {"KIMERU_POST_KIND_MARK": "1"}):
            text = notify.format_post(1, rec)
            self.assertNotIn("下書き:", text.splitlines()[0])
            self.assertNotIn("文面だけ確認", text)
            self.assertNotIn("判断は自動", text)
            self.assertIn("人に確認", text)

    def test_unavailable_assessment_does_not_display_a_numeric_score(self):
        rec = record({"level": None, "confidence": None, "reason_code": "unavailable",
                      "reason": "評価を取得できませんでした", "reason_confidence": None,
                      "route": "human", "status": "unavailable"})
        rec["outcome"] = "advise"
        with tempfile.TemporaryDirectory() as directory:
            texts = (cli._fmt(rec), notify.format_post(1, rec), notify.format_detail(1, rec),
                     render_report(Path(directory), rec))
        for text in texts:
            self.assertIn("評価不能", text)
            self.assertIn("人に確認", text)
            self.assertIn("評価を取得できませんでした", text)
            self.assertNotIn("None/5", text)

    def test_report_escapes_stored_assessment_text(self):
        rec = record(assessment(1, "human", '<script>alert("x")</script> & 現場情報'))
        with tempfile.TemporaryDirectory() as directory:
            html = render_report(Path(directory), rec)
        self.assertNotIn('<script>alert("x")</script>', html)
        self.assertEqual(html.count("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; 現場情報"), 2)


if __name__ == "__main__":
    unittest.main()
