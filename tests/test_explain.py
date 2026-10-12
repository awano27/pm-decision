"""R25: with post_reason=1 the detail (詳細 N) starts with the words a rule read: the words it hit (当たった語) and the words it
rejected or sent to a person (拒否した語 / 人へ回した語), at most 3 lines. graph.match_eval and graph.match_explain are one judgment
(graph._match), and explain.rule_evidence works the words out again on the event the record was judged on: the whole text while
the item waits (fulltext.load), else the recorded event. A record whose graph version, rule edge or matched pattern does not agree
gets no line. The approval post does not change, no model is asked, nothing is written, and at 0 (the default) the post and the
detail are the golden text, byte for byte."""
try:   # isolation from the real state folder, whichever way the tests are started
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

from kimeru import cli, explain, fulltext, graph, notify, plan
from kimeru.backends import ReplayBackend
from tests.test_daily import FakeTeams

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)
CHAT = next(g for lst in GRAPHS.values() for g in lst if g["name"] == "teams-chat-triage")

DIST = {"decision": 0.2, "requirements": 0.02, "status": 0.62, "incident": 0.03, "fyi": 0.08, "none": 0.05}
UNSURE = {"intent": {"choice": "status", "confidence": 0.45, "probabilities": DIST}}
VETO_TEXT = "本番で決済が落ちています（検証環境の話）"
MIXED_TEXT = "本番で決済が落ちています。復旧しました。"
YES_TEXT = "本番で決済が落ちています"
NONE_TEXT = "この件、どうしましょう"
LONG_TEXT = "作業を進めています。" * 14 + VETO_TEXT          # the report is more than 120 characters in
RULE_VETO = "規則: 当たった語「本番で決済が落ちてい」 / 拒否した語「検証環境」"
RULE_MIXED = "規則: 当たった語「本番で決済が落ちてい」 / 人へ回した語「復旧」"
RULE_YES = "規則: 当たった語「本番で決済が落ちてい」"
QUESTION = "迷った点: 依頼の種類（近いのは 状況確認）"       # R11's line for the veto and the none chat
MIXED_QUESTION = explain.MIXED_LINE                       # R11's line for a rule that sent the chat to a person

# The golden text (HEAD before R25): the post and the detail of the four chats at post_reason=0, line by line.
GOLD = {
    "veto.post": [
        "[kimeru #7] 佐藤さん: 本番で決済が落ちています（検証環境の話）",
        "→ 意図が判定できないチャット: 佐藤「本番で決済が落ちています（検証環境の話）」",
        "案: 受領しました。内容を確認して返信します。",
        "OK 7 / NG 7 / 詳細 7",
    ],
    "veto.detail": [
        "[kimeru 詳細 #7] 判断が必要（teams-chat-triage） / 改訂 1",
        "判断: 意図が判定できないチャット: 佐藤「本番で決済が落ちています（検証環境の話）」",
        "次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）",
        "不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）",
        "OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）; 記録のみ",
        "teams.chat #t9",
        "内容: 意図が判定できないチャット: 佐藤「本番で決済が落ちています（検証環境の話）」",
        "承認の対象: teams.reply, ado.create（既定は記録だけ。実行するのは設定で有効にした種類だけ）",
        "返信: OK 7 / NG 7 / 保留 7",
    ],
    "veto.full.detail": [
        "[kimeru 詳細 #7] 判断が必要（teams-chat-triage） / 改訂 1",
        "判断: 意図が判定できないチャット: 佐藤「本番で決済が落ちています（検証環境の話）」",
        "次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）",
        "不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）",
        "OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）; 記録のみ",
        "teams.chat #t9",
        "内容: 意図が判定できないチャット: 佐藤「本番で決済が落ちています（検証環境の話）」",
        "承認の対象: teams.reply, ado.create（既定は記録だけ。実行するのは設定で有効にした種類だけ）",
        "元（全文）: 佐藤: 本番で決済が落ちています（検証環境の話）",
        "　直前のやり取り: 前の話",
        "返信: OK 7 / NG 7 / 保留 7",
    ],
    "mixed.post": [
        "[kimeru #7] 佐藤さん: 本番で決済が落ちています。復旧しました。",
        "→ 意図が判定できないチャット: 佐藤「本番で決済が落ちています。復旧しました。」",
        "案: 受領しました。内容を確認して返信します。",
        "OK 7 / NG 7 / 詳細 7",
    ],
    "mixed.detail": [
        "[kimeru 詳細 #7] 判断が必要（teams-chat-triage） / 改訂 1",
        "判断: 意図が判定できないチャット: 佐藤「本番で決済が落ちています。復旧しました。」",
        "次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）",
        "不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）",
        "OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）; 記録のみ",
        "teams.chat #t8",
        "内容: 意図が判定できないチャット: 佐藤「本番で決済が落ちています。復旧しました。」",
        "承認の対象: teams.reply, ado.create（既定は記録だけ。実行するのは設定で有効にした種類だけ）",
        "返信: OK 7 / NG 7 / 保留 7",
    ],
    "yes.post": [
        "[kimeru #7] 佐藤さん: 本番で決済が落ちています",
        "→ 障害報告をBug化した。監視アラートと突き合わせること",
        "起票案: [Teams報告] 本番で決済が落ちています",
        "OK 7 / NG 7 / 詳細 7",
    ],
    "yes.detail": [
        "[kimeru 詳細 #7] 判断が必要（teams-chat-triage） / 改訂 1",
        "判断: 障害報告をBug化した。監視アラートと突き合わせること",
        "次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）",
        "不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）",
        "OK の効果: 記録のみ",
        "teams.chat #t7",
        "内容: 障害報告をBug化した。監視アラートと突き合わせること",
        "承認の対象: ado.create（既定は記録だけ。実行するのは設定で有効にした種類だけ）",
        "返信: OK 7 / NG 7 / 保留 7",
    ],
    "none.post": [
        "[kimeru #7] 佐藤さん: この件、どうしましょう",
        "→ 意図が判定できないチャット: 佐藤「この件、どうしましょう」",
        "案: 受領しました。内容を確認して返信します。",
        "OK 7 / NG 7 / 詳細 7",
    ],
    "none.detail": [
        "[kimeru 詳細 #7] 判断が必要（teams-chat-triage） / 改訂 1",
        "判断: 意図が判定できないチャット: 佐藤「この件、どうしましょう」",
        "次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）",
        "不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）",
        "OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）; 記録のみ",
        "teams.chat #t6",
        "内容: 意図が判定できないチャット: 佐藤「この件、どうしましょう」",
        "承認の対象: teams.reply, ado.create（既定は記録だけ。実行するのは設定で有効にした種類だけ）",
        "返信: OK 7 / NG 7 / 保留 7",
    ],
}


def inserted(lines, at, *new):
    """The golden lines with the new lines put in before position `at`."""
    lines = list(lines)
    return lines[:at] + list(new) + lines[at:]


def chat_record(text, ev_id, answers=None):
    """The record cli.process keeps for a chat (graph_key, the recorded event), judged by graph.run with these answers."""
    ev = {"kind": "teams.chat", "id": ev_id, "author": "佐藤", "chat_id": "19:c", "mentions_me": True, "text": text}
    rec = graph.run(CHAT, ev, ReplayBackend(answers or {}), playbooks=PBS)
    rec.update({"revision": 1, "graph_key": f"{CHAT['name']}@{cli._graph_version(CHAT)}:teams.chat:{ev_id}",
                "event": cli._recorded_event(ev)})
    return rec


def chats():
    """The golden chats: a rule vetoed a report (intent unsure), a rule sent a report to a person, a rule decided, and a chat no rule reads."""
    return {"veto": chat_record(VETO_TEXT, "t9", UNSURE), "mixed": chat_record(MIXED_TEXT, "t8"),
            "yes": chat_record(YES_TEXT, "t7"), "none": chat_record(NONE_TEXT, "t6", UNSURE)}


class TestTheTextAtZeroIsTheGoldenText(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("KIMERU_POST_REASON", None)

    def test_by_default_the_post_and_the_detail_of_each_chat_are_the_golden_text(self):
        for name, rec in chats().items():
            with self.subTest(chat=name):
                self.assertEqual(notify.format_post(7, rec), "\n".join(GOLD[f"{name}.post"]))
                self.assertEqual(notify.format_detail(7, rec), "\n".join(GOLD[f"{name}.detail"]))

    def test_at_zero_the_detail_with_the_whole_text_is_the_golden_text(self):
        os.environ["KIMERU_POST_REASON"] = "0"
        full = {"text": VETO_TEXT, "thread": ["前の話"]}
        self.assertEqual(notify.format_detail(7, chats()["veto"], full), "\n".join(GOLD["veto.full.detail"]))


class TestTheWordsAtOne(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "1"})
        env.start()
        self.addCleanup(env.stop)

    def test_the_post_gets_only_the_question_line_as_before(self):
        recs = chats()
        for name, extra in (("veto", QUESTION), ("mixed", MIXED_QUESTION), ("yes", None), ("none", QUESTION)):
            with self.subTest(chat=name):
                gold = GOLD[f"{name}.post"]
                want = gold if extra is None else inserted(gold, 2, extra)
                self.assertEqual(notify.format_post(7, recs[name]), "\n".join(want))

    def test_the_detail_names_the_words_above_the_question_line(self):
        recs = chats()
        cases = (("veto", [RULE_VETO, QUESTION]), ("mixed", [RULE_MIXED, MIXED_QUESTION]), ("yes", [RULE_YES]), ("none", [QUESTION]))
        for name, new in cases:
            with self.subTest(chat=name):
                self.assertEqual(notify.format_detail(7, recs[name]), "\n".join(inserted(GOLD[f"{name}.detail"], 1, *new)))

    def test_the_whole_text_of_a_waiting_item_gives_the_same_words(self):
        full = {"text": VETO_TEXT, "thread": ["前の話"]}
        self.assertEqual(notify.format_detail(7, chats()["veto"], full),
                         "\n".join(inserted(GOLD["veto.full.detail"], 1, RULE_VETO, QUESTION)))


class TestTheChatThatReachedThePM(unittest.TestCase):
    """The acceptance case: the Teams text with a production report and a test-environment note, the model unsure of the intent."""

    def test_the_detail_names_the_word_the_rule_hit_and_the_word_it_rejected(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "1"}):
            payload = {"kind": "teams.chat", "id": "t9", "chat_id": "19:c", "author": "佐藤", "text": VETO_TEXT, "mentions_me": True}
            [res] = cli.process(payload, GRAPHS, ReplayBackend(UNSURE), Path(d), PBS)
            lines = notify.format_detail(7, res).splitlines()
        self.assertEqual([(s["node"], s["edge"]) for s in res["path"]][0], ("critical_incident", "no"))
        self.assertEqual(lines[1:3], [RULE_VETO, QUESTION])

    def test_a_long_chat_read_in_full_while_it_waits_names_the_words_from_its_whole_text(self):
        teams = FakeTeams()
        teams.chat_messages = {"19:c": [LONG_TEXT]}   # the chat as it is on screen: the preview is cut, the whole text is read
        payload = {"kind": "teams.chat", "id": "t5", "chat_id": "19:c", "author": "佐藤", "text": LONG_TEXT[:70] + "…", "mentions_me": True}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "1", "KIMERU_READ_FULL": "1"}):
            [res] = cli.process(payload, GRAPHS, ReplayBackend(UNSURE), Path(d), PBS, dedup=True, reader=fulltext.Reader(teams))
            full = fulltext.load(Path(d), f"{res['graph']}:{res['event_id']}:{res['node']}")
            with_full = notify.format_detail(7, res, full).splitlines()
            without = notify.format_detail(7, res).splitlines()
        self.assertEqual(res["read_full"]["state"], "full")
        self.assertIsNotNone(full)
        self.assertEqual(with_full[1:3], [RULE_VETO, QUESTION])
        self.assertEqual(without[1], QUESTION)   # the record's cut text alone reads no rule word, so no words are named


class TestTheWordsAreWorkedOutOnlyWhenTheRecordAgrees(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "1"})
        env.start()
        self.addCleanup(env.stop)

    def test_a_record_from_another_graph_version_gets_no_words(self):
        rec = chat_record(VETO_TEXT, "t9", UNSURE)
        rec["graph_key"] = f"{CHAT['name']}@00000000:teams.chat:t9"
        self.assertEqual(explain.rule_evidence(rec), [])
        self.assertEqual(notify.format_detail(7, rec), "\n".join(inserted(GOLD["veto.detail"], 1, QUESTION)))

    def test_a_record_whose_rule_edge_or_matched_pattern_differs_gets_no_words(self):
        rec = chat_record(VETO_TEXT, "t9", UNSURE)
        edge = {**rec, "path": [{**rec["path"][0], "edge": "yes"}, *rec["path"][1:]]}
        matched = {**rec, "path": [{**rec["path"][0], "answer": {"matched": "別の語"}}, *rec["path"][1:]]}
        self.assertEqual(explain.rule_evidence(edge), [])
        self.assertEqual(explain.rule_evidence(matched), [])

    def test_a_record_whose_recorded_text_gives_another_edge_gets_no_words(self):
        rec = chat_record(VETO_TEXT, "t9", UNSURE)
        rec["event"] = {**rec["event"], "text": YES_TEXT}   # judged no, but this text reads yes: the line would be a guess
        self.assertEqual(explain.rule_evidence(rec), [])

    def test_a_record_without_a_path_a_graph_key_or_a_node_of_its_graph_gets_no_words(self):
        rec = chat_record(VETO_TEXT, "t9", UNSURE)
        renamed = {**rec, "path": [{**rec["path"][0], "node": "renamed_node"}, *rec["path"][1:]]}
        for changed in ({k: v for k, v in rec.items() if k != "path"}, {k: v for k, v in rec.items() if k != "graph_key"}, renamed):
            with self.subTest(keys=sorted(changed)):
                self.assertEqual(explain.rule_evidence(changed), [])

    def test_a_record_whose_text_is_cut_gives_no_words_when_the_cut_text_reads_no_rule_word(self):
        rec = chat_record(LONG_TEXT, "t5", UNSURE)   # judged on the whole text; the recorded event keeps 120 characters
        self.assertEqual(explain.rule_evidence(rec), [])
        self.assertEqual(explain.rule_evidence(rec, {"text": LONG_TEXT}), [RULE_VETO])


class TestTheExplanationIsReadOnly(unittest.TestCase):
    def test_the_words_are_worked_out_without_a_model_call_or_a_written_file(self):
        rec = chats()["veto"]
        before = copy.deepcopy(rec)
        state = os.environ["KIMERU_STATE_DIR"]

        def listing():
            return sorted((root, sorted(files)) for root, _, files in os.walk(state))
        seen = listing()
        with mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "1"}), \
                mock.patch.object(ReplayBackend, "ask", side_effect=AssertionError("the explanation asked the model")):
            notify.format_detail(7, rec)
        self.assertEqual(rec, before)
        self.assertEqual(listing(), seen)

    def test_the_bundled_graphs_are_read_once_for_any_number_of_records(self):
        rec = chats()["veto"]
        with mock.patch.object(explain, "_BUNDLED", None), mock.patch.object(graph, "load_dir", wraps=graph.load_dir) as load:
            for _ in range(3):
                explain.rule_evidence(rec)
        self.assertEqual(load.call_count, 1)


def rule_node(**extra):
    """A match node that reads the text for 障害 (a fault word)."""
    return {"kind": "match", "fields": ["text"], "patterns": ["障害"], "routes": {"yes": "a", "no": "b", "mixed": "c"}, **extra}


CASES = [
    ("a hit with nothing against it", {}, {"text": "本番で障害が起きた"}, "yes", "障害", None, None),
    ("a hit with a recovery word in the field", {"mixed_field": ["復旧"]}, {"text": "障害が起きたが復旧した"},
     "mixed", "障害", None, "復旧"),
    ("a hit vetoed in its field", {"exclude": ["テスト"]}, {"text": "障害が起きた（テストの話）"}, "no", "障害", "テスト", None),
    ("a vetoed hit with a mixed_if word", {"exclude": ["テスト"], "mixed_if": ["復旧"]},
     {"text": "障害が起きた（テストの話）。復旧した"}, "mixed", "障害", "テスト", "復旧"),
    ("a hit whose sentence an exclude_sentence pattern covers", {"exclude_sentence": ["改善"]},
     {"text": "障害の改善を提案します"}, "no", "障害", "改善", None),
    ("no hit", {}, {"text": "問題なし"}, "no", None, None, None),
    ("a node whose when is not met", {"when": {"type": "^bug$"}}, {"text": "障害が起きた", "type": "Task"}, "no", None, None, None),
]


class TestTheRuleReadsTheSameAsItDecides(unittest.TestCase):
    def test_match_eval_and_match_explain_agree_on_every_shape_of_rule(self):
        for name, extra, ev, edge, word, veto, mixed in CASES:
            node = rule_node(**extra)
            with self.subTest(case=name):
                got = graph.match_explain(node, ev)
                self.assertEqual(graph.match_eval(node, ev)[0], got["edge"])
                self.assertEqual((got["edge"], got["word"], got["veto"], got["mixed"]), (edge, word, veto, mixed))

    def test_every_eval_fixture_gives_the_judged_edge_and_pattern_for_every_rule(self):
        count = 0
        for path in sorted((ROOT / "eval").glob("fixtures*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                ev = json.loads(line)["event"]
                for g in GRAPHS.get(ev.get("kind"), []):
                    for nid, n in g["nodes"].items():
                        if n["kind"] != "match":
                            continue
                        edge, pattern = graph.match_eval(n, ev)
                        got = graph.match_explain(n, ev)
                        count += 1
                        self.assertEqual((got["edge"], got["pattern"]), (edge, pattern), (path.name, nid))
        self.assertGreater(count, 100)


class TestTheLineIsShortAndCountsNoRule(unittest.TestCase):
    def bundle(self, nodes, path, text):
        g = {"name": "rule-words", "event": "teams.chat", "start": "r1", "nodes": nodes}
        rec = {"graph": "rule-words", "graph_key": f"rule-words@{graph.version(g)}:teams.chat:1", "event": {"text": text}, "path": path}
        return rec, {("rule-words", graph.version(g)): g}

    def test_at_most_three_rules_are_named_in_a_line(self):
        nodes = {f"r{i}": rule_node() for i in range(1, 5)}
        path = [{"node": f"r{i}", "answer": {"matched": "障害"}, "edge": "yes"} for i in range(1, 5)]
        rec, bundled = self.bundle(nodes, path, "障害")
        with mock.patch.object(explain, "_BUNDLED", bundled):
            self.assertEqual(explain.rule_evidence(rec), ["規則: 当たった語「障害」"] * 3)

    def test_a_word_longer_than_twenty_characters_is_cut_with_an_ellipsis(self):
        nodes = {"r1": rule_node(patterns=["障害.{0,60}起きた"])}
        rec, bundled = self.bundle(nodes, [{"node": "r1", "answer": {"matched": "障害.{0,60}起きた"}, "edge": "yes"}],
                                   "障害" + "あ" * 30 + "起きた")
        with mock.patch.object(explain, "_BUNDLED", bundled):
            self.assertEqual(explain.rule_evidence(rec), ["規則: 当たった語「障害" + "あ" * 17 + "…」"])

    def test_a_word_of_one_character_or_none_is_not_quoted(self):
        for pattern in ("^", "障"):
            nodes = {"r1": rule_node(patterns=[pattern])}
            rec, bundled = self.bundle(nodes, [{"node": "r1", "answer": {"matched": pattern}, "edge": "yes"}], "障害")
            with self.subTest(pattern=pattern), mock.patch.object(explain, "_BUNDLED", bundled):
                self.assertEqual(explain.rule_evidence(rec), [])


ADO = next(g for lst in GRAPHS.values() for g in lst if g["name"] == "workitem-intake")
ADO_LONG = "作業を進めています。" * 14 + "本番で決済ができません。"   # the rule word comes after the 120 characters a record keeps
RULE_ADO = "規則: 当たった語「本番で決済ができません」"


class TestTheWholeMaterialOfAWaitingWorkItemGivesTheWords(unittest.TestCase):
    """An ADO work item: the record keeps its description cut to 120 characters, so the rule word is in the whole material only."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {"KIMERU_POST_REASON": "1"})
        env.start()
        self.addCleanup(env.stop)

    def ado_record(self):
        ev = {"kind": "ado.workitem.created", "id": "w5", "type": "Bug", "title": "注文画面の確認", "description": ADO_LONG}
        rec = graph.run(ADO, ev, ReplayBackend({}), playbooks=PBS)
        rec.update({"revision": 1, "graph_key": f"{ADO['name']}@{cli._graph_version(ADO)}:ado.workitem.created:w5",
                    "event": cli._recorded_event(ev)})
        return rec

    def test_a_cut_description_names_its_words_only_from_the_whole_material_of_the_item(self):
        rec = self.ado_record()
        full = {"material": {"description": ADO_LONG}}
        self.assertNotIn("本番", rec["event"]["description"])   # the recorded text reads no rule word
        self.assertEqual(explain.rule_evidence(rec), [])
        self.assertEqual(explain.rule_evidence(rec, full), [RULE_ADO])
        self.assertNotIn(RULE_ADO, notify.format_detail(7, rec).splitlines())
        self.assertEqual(notify.format_detail(7, rec, full).splitlines()[1], RULE_ADO)


if __name__ == "__main__":
    unittest.main()
