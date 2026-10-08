"""ADO decisions reach the PM: an information request is always queued, automatic P2/P3 appear in the morning brief
(not as one post each), and every ADO post / notice names the work item (id, type, title, creator, link)."""
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
from unittest import mock

from kimeru import brief, cli, config, execute, graph, notify
from kimeru.backends import ReplayBackend, StubBackend

ROOT = Path(__file__).resolve().parent.parent
G = {"ado.workitem.created": [graph.load(ROOT / "graphs/ado_workitem.json")]}


def workitem(id_, typ="Bug", title="t", ac="", prio=2, by="山田 太郎", origin=("contoso-not-real", "Proj A")):
    f = {"System.WorkItemType": typ, "System.Title": title, "System.CreatedBy": {"displayName": by} if by else None,
         "Microsoft.VSTS.Common.Priority": prio, "System.Description": "",
         "Microsoft.VSTS.Common.AcceptanceCriteria": ac}
    p = {"eventType": "workitem.created", "resource": {"id": id_, "fields": f}}
    if origin:
        p["kimeru_origin"] = {"org": origin[0], "project": origin[1]}
    return p


def answers(ready=0.1, score=1.0):
    lvl = int(round(score))
    return ReplayBackend({
        "ready": {"type": "noul", "noul": ready},
        "priority": {"type": "score", "score": score, "confidence": 0.9,
                     "probabilities": {str(i): (0.8 if i == lvl else 0.1) for i in range(3)}}})


class Bridge:
    def __init__(self):
        self.posts = []

    def post(self, text, send):
        self.posts.append(text)
        return {"ok": True, "typed": True, "sent": bool(send),
                "readback": {"matched": bool(send), "message_id": f"m{len(self.posts)}" if send else ""}}

    def read(self):
        return {"ok": True, "posts": [], "replies": []}


def rows(out, name):
    p = Path(out) / name
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def run_one(self, payload, be):
        return cli.process(payload, G, be, self.out, playbooks={}, dedup=True)[0]


class TestInfoRequestIsQueued(Base):
    def test_queued_without_writer_or_execute(self):
        r = self.run_one(workitem(501, "Task", "レビュー指摘の修正"), answers(ready=0.1))
        self.assertEqual(r["node"], "request_info")
        self.assertTrue(r["needs_human"])
        self.assertEqual([x["event_id"] for x in rows(self.out, "queue.jsonl")], ["501"])
        self.assertEqual(rows(self.out, "notices.jsonl"), [])      # a queued post, not a second notice

    def test_the_post_names_the_work_item_and_shows_the_fixed_comment(self):
        self.run_one(workitem(501, "Task", "レビュー指摘の修正"), answers(ready=0.1))
        bridge = Bridge()
        notify.notify(self.out, bridge, send=True, real=True)
        post = bridge.posts[0]
        for needle in ("#501", "Task", "レビュー指摘の修正", "山田 太郎",
                       "https://dev.azure.com/contoso-not-real/Proj%20A/_workitems/edit/501",
                       "着手に必要な情報が不足しています"):
            self.assertIn(needle, post)
        self.assertIn("記録のみ", post)                              # OK N writes nothing unless ado.comment is switched on
        self.assertNotIn("判断の詳細は以下を確認してください", post)
        self.assertEqual(hidden_words.found(post), [])

    def test_no_link_without_an_origin(self):
        self.run_one(workitem(502, "Task", "出所なし", origin=None), answers(ready=0.1))
        bridge = Bridge()
        notify.notify(self.out, bridge, send=True, real=True)
        self.assertIn("#502", bridge.posts[0])
        self.assertNotIn("dev.azure.com", bridge.posts[0])

    def test_a_long_title_is_cut_to_title_max(self):
        with mock.patch.dict("os.environ", {"KIMERU_TITLE_MAX": "30"}):
            config.apply([])
            self.run_one(workitem(503, "Task", "あ" * 80), answers(ready=0.1))
            bridge = Bridge()
            notify.notify(self.out, bridge, send=True, real=True)
        config.apply([])
        self.assertNotIn("あ" * 40, bridge.posts[0])
        self.assertIn("あ" * 20, bridge.posts[0])

    def test_with_execute_the_comment_has_one_signature_and_is_not_called_a_draft(self):
        with mock.patch.dict("os.environ", {"KIMERU_EXECUTE": "ado.comment"}):
            config.apply([])
            self.run_one(workitem(504, "Task", "実行あり"), answers(ready=0.1))
            bridge = Bridge()
            notify.notify(self.out, bridge, send=True, real=True)
        config.apply([])
        post = bridge.posts[0]
        self.assertEqual(post.count("— kimeru"), 0)                 # the sign-off is the signature line, not both
        self.assertNotIn("下書き", post)
        self.assertNotIn("本人が承認した文面です）\n\n（", post)
        self.assertIn("着手に必要な情報が不足しています", post)
        self.assertIn("https://dev.azure.com/contoso-not-real/Proj%20A/_workitems/edit/504", post)


class TestAutomaticDecisionsInTheBrief(Base):
    def test_p2_and_p3_are_neither_queued_nor_noticed(self):
        self.run_one(workitem(601, "Bug", "CSV 出力に列を追加", ac="列 X が出力されること"), answers(ready=0.9, score=1.0))
        self.run_one(workitem(602, "Bug", "余白のずれ", ac="余白が 8px であること"), answers(ready=0.9, score=0.0))
        self.assertEqual(rows(self.out, "queue.jsonl"), [])
        self.assertEqual(rows(self.out, "notices.jsonl"), [])

    def test_the_brief_lists_them_with_counts_and_a_capped_list(self):
        for i in range(7):
            self.run_one(workitem(610 + i, "Bug", f"項目{i}", ac="結果が出ること"), answers(ready=0.9, score=1.0))
        self.run_one(workitem(620, "Task", "余白のずれ", ac="余白が 8px であること"), answers(ready=0.9, score=0.0))
        text, _ = brief.build(self.out, ReplayBackend({}))
        self.assertIn("ADO", text)
        self.assertIn("P2 7 件", text)
        self.assertIn("P3 1 件", text)
        self.assertEqual(sum(1 for l in text.splitlines() if l.startswith("・#")), 5)
        self.assertIn("ほか 3 件", text)
        self.assertRegex(text, r"#6\d\d \[(Bug|Task)\] .+ → P[23]")
        self.assertEqual(hidden_words.found(text), [])

    def test_items_waiting_for_the_pm_are_not_listed_twice(self):
        self.run_one(workitem(630, "Task", "情報なし"), answers(ready=0.1))
        text, _ = brief.build(self.out, StubBackend())
        self.assertNotIn("ADO の自動判断", text)                     # it is a confirmation, shown as one
        self.assertIn("確認待ち", text)

    def test_the_old_ones_are_not_repeated(self):
        self.run_one(workitem(640, "Bug", "古い", ac="結果が出ること"), answers(ready=0.9, score=1.0))
        later = brief.datetime.now(brief.timezone.utc) + brief.timedelta(days=3)
        text, _ = brief.build(self.out, ReplayBackend({}), now=later)
        self.assertNotIn("ADO の自動判断", text)

    def test_nothing_changes_without_ado_decisions(self):
        text, _ = brief.build(self.out, ReplayBackend({}))
        self.assertNotIn("ADO", text)


class TestNotice(Base):
    def test_p1_notice_names_the_item_and_does_not_contradict_itself(self):
        r = self.run_one(workitem(701, "Bug", "請求書 PDF が生成されない", ac="生成されること"), answers(ready=0.9, score=2.0))
        self.assertTrue(r["notify"])
        text = notify.format_notice(rows(self.out, "notices.jsonl")[0])
        for needle in ("#701", "Bug", "請求書 PDF が生成されない", "山田 太郎",
                       "https://dev.azure.com/contoso-not-real/Proj%20A/_workitems/edit/701"):
            self.assertIn(needle, text)
        self.assertIn("判断", text)
        self.assertNotIn("返信は不要です", text)                     # the advice asks for a decision
        self.assertEqual(hidden_words.found(text), [])

    def test_the_teams_notice_is_unchanged(self):
        rec = {"graph": "g", "event_kind": "teams.chat", "event_id": "1", "summary": "a: b", "advice": "x", "executed": []}
        self.assertTrue(notify.format_notice(rec).endswith("返信は不要です"))


if __name__ == "__main__":
    unittest.main()
