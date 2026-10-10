"""Fewer posts to the PM from Teams chat-list previews: a message that is only thanks / OK / a greeting / emoji is recorded as
FYI by a rule (`ack_only` in graphs/teams_chat.json) without asking the model, and is counted in the morning brief. Everything
else still goes through the safety net and the model, and a model that is unsure still sends it to the PM.
The labeled previews are eval/fixtures_preview_ja.jsonl (synthetic; `want` = auto | pm, `severe` = must reach the PM)."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401
try:
    from . import hidden_words
except ImportError:
    import hidden_words

import json
import tempfile
import unittest
from pathlib import Path

from kimeru import brief, cli, graph
from kimeru.backends import ReplayBackend, StubBackend

ROOT = Path(__file__).resolve().parent.parent
TEAMS = graph.load(ROOT / "graphs/teams_chat.json")
FIXTURES = [json.loads(l) for l in (ROOT / "eval/fixtures_preview_ja.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


class NoModel:
    def ask(self, state, questions):
        raise AssertionError(f"the model was asked {sorted(questions)}")


# What Kev answered on short previews: the right option, under the intent node's threshold (0.6)
UNSURE = ReplayBackend({"intent": {"type": "choice", "choice": "fyi", "confidence": 0.3},
                        "urgency": {"type": "score", "score": 1.0, "confidence": 0.1, "probabilities": {}}})


def ack(text):
    return graph.match_eval(TEAMS["nodes"]["ack_only"], {"text": text})[0] == "yes"


def run(text, be):
    return graph.run(TEAMS, {"kind": "teams.chat", "id": "x", "author": "山田 太郎", "text": text}, be, playbooks={})


class TestAckRule(unittest.TestCase):
    def test_bare_acknowledgements_are_taken(self):
        for t in ["了解です、ありがとうございます", "承知しました！", "ありがとうございます🙏", "山田 太郎: 了解です、ありがとうございます",
                  "お疲れ様です！", "👍", "OKです！", "かしこまりました。", "了解しました。対応しておきます",
                  "承知しました。よろしくお願いいたします。", "いえいえ、こちらこそありがとうございます", "後藤 樹: 了解です👍",
                  "@鈴木 さん ありがとうございます！", "ありがとうございます、確認します！", "お疲れさまでした！今日もありがとうございました"]:
            self.assertTrue(ack(t), t)

    def test_anything_more_goes_to_the_model(self):
        for t in ["了解です、明日のリリース判断お願いします",        # a request after the thanks
                  "ありがとうございます。ところで来週のリリース判定ですが、予定通り…",   # cut preview
                  "了解です…", "了解です...", "OKですか？", "了解です?",           # cut, or a question
                  "よろしくお願いします！",                              # usually follows a request the preview does not show
                  "お願いします", "確認お願いします", "ご確認ください", "資料を共有しました",
                  "本番障害: 対応します", "障害の件、了解です", "至急: 了解です", "了解です。ただ、テストが間に合わないかもしれません",
                  "了解です。では明日の10時でお願いします", "ありがとうございます！ところで、来月の体制について相談させてください", ""]:
            self.assertFalse(ack(t), t)

    def test_no_model_question_and_no_external_action(self):
        r = run("承知しました！", NoModel())
        self.assertEqual(r["node"], "log_fyi")
        self.assertEqual([s["node"] for s in r["path"]], ["critical_incident", "ack_only"])
        self.assertEqual([a["type"] for a in r["actions"]], ["log.only"])
        self.assertFalse(r["needs_human"] or r["notify"])

    def test_the_incident_rule_runs_first(self):
        r = run("本番でログインできなくなっています。ありがとうございます", NoModel())
        self.assertEqual(r["node"], "incident_bug")
        self.assertTrue(r["notify"])


class TestPreviewFixtures(unittest.TestCase):
    def test_labels(self):
        self.assertGreaterEqual(len(FIXTURES), 40)
        for fx in FIXTURES:
            self.assertIn(fx["want"], ("auto", "pm"), fx["id"])
            if fx["severe"]:
                self.assertEqual(fx["want"], "pm", fx["id"])
            self.assertEqual(hidden_words.found(json.dumps(fx, ensure_ascii=False)), [], fx["id"])

    def test_the_rule_never_takes_an_event_that_must_reach_the_pm(self):
        for fx in FIXTURES:
            if fx["event"]["kind"] == "teams.chat" and fx["want"] == "pm":
                self.assertFalse(ack(fx["event"]["text"]), fx["id"])

    def test_with_an_unsure_model_every_pm_event_still_reaches_the_pm(self):
        for fx in FIXTURES:
            if fx["event"]["kind"] != "teams.chat":
                continue
            r = graph.run(TEAMS, fx["event"], UNSURE, playbooks={})
            to_pm = bool(r["needs_human"] or r["notify"])
            if fx["want"] == "pm":
                self.assertTrue(to_pm, fx["id"])
            if ack(fx["event"]["text"]):
                self.assertEqual(r["node"], "log_fyi", fx["id"])

    def test_most_acknowledgements_no_longer_need_the_model(self):
        auto = [fx for fx in FIXTURES if fx["event"]["kind"] == "teams.chat" and fx["want"] == "auto"]
        taken = [fx for fx in auto if ack(fx["event"]["text"])]
        self.assertGreaterEqual(len(taken), 15)


class TestBrief(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def process(self, i, text, author="山田 太郎"):
        ev = {"kind": "teams.chat", "id": f"c{i}", "chat_id": f"19:c{i}", "author": author, "chat_title": author, "text": text}
        return cli.process(ev, {"teams.chat": [TEAMS]}, UNSURE, self.out, playbooks={}, dedup=True)[0]

    def test_settled_chats_are_counted_in_the_brief_and_not_queued(self):
        self.process(1, "了解です、ありがとうございます", "山田 太郎")
        self.process(2, "👍", "佐藤 花子")
        r = self.process(3, "設計書のレビューをお願いできますか？", "高橋 健")
        self.assertTrue(r["needs_human"])
        queued = [json.loads(l) for l in (self.out / "queue.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual([q["event_id"] for q in queued], ["c3"])
        text, _ = brief.build(self.out, StubBackend())          # ranks the one waiting item
        self.assertIn("Teams の自動判断（直近 24 時間）: 2 件（返信不要 2 件）", text)
        self.assertIn("山田 太郎", text)
        self.assertIn("佐藤 花子", text)
        self.assertEqual(hidden_words.found(text), [])

    def test_nothing_without_settled_chats(self):
        text, _ = brief.build(self.out, ReplayBackend({}))
        self.assertNotIn("Teams の自動判断", text)


if __name__ == "__main__":
    unittest.main()
