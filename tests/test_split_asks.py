"""R35 (setting split_asks=1): a chat message with two or more requests is judged request by request, and its route is combined
toward people only. With the default 0 the judge is asked exactly what it was asked before: the golden outputs below were taken
at the commit before R35. Every judge here is a fake that answers by question id: no network, no real Teams, no setting of the
machine (tests/isolate.py)."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from kimeru import asks, cli, config, graph, notify
from kimeru.backends import ReplayBackend
from tests.test_daily import GRAPHS, PBS

ROOT = Path(__file__).resolve().parent.parent
TEAMS = GRAPHS["teams.chat"][0]
NODES = TEAMS["nodes"]
CHAT = "19:synthetic-chat-1@unq.gbl.spaces"
NOW = datetime(2026, 10, 12, 3, 0, tzinfo=timezone.utc)
CHOICES = ("decision", "requirements", "status", "incident", "fyi", "none")
TWO = "進捗どうですか？あとリリース日を決めてください"   # a status request and a decision request in one message
LOW = 0.2   # a confidence under the intent node's min_conf: the route is unsure


def ev(text, eid="g1"):
    return {"kind": "teams.chat", "source": "teams", "id": eid, "ts": "2026-10-12T03:00:00+00:00", "chat_id": CHAT,
            "author": "Sato", "mentions_me": True, "text": text}


def ans(choice=None, conf=0.9, score=None):
    if score is not None:
        return {"type": "score", "score": score, "confidence": conf, "probabilities": {}}
    return {"type": "choice", "choice": choice, "confidence": conf, "probabilities": {}}


def pick(label):
    """An intent answer for `label`; "unsure" is a low-confidence answer, which the route sends to unsure."""
    return ans("status", conf=LOW) if label == "unsure" else ans(label)


def answers_for(body, requests=(), playbook="none", score=2.0):
    """The answers of one message: its own intent, one per request (intent_0, intent_1 ...), and the plan and urgency answers of
    the decision paths (no playbook fits; the urgency is `score`)."""
    a = {"intent": pick(body), "playbook": ans(playbook), "urgency": ans(score=score)}
    for k, label in enumerate(requests):
        a[f"intent_{k}"] = pick(label)
    return a


class Answers(ReplayBackend):
    """Answers by question id, and keeps every call: the keys of the state it was given, and the question ids."""

    def __init__(self, answers):
        super().__init__(answers)
        self.calls = []

    def ask(self, state, questions):
        self.calls.append((copy.deepcopy(state), sorted(questions)))
        return super().ask(state, questions)


def judge(text, body, requests=(), split=None, batch=None, playbook="none", score=2.0):
    """graph.run on one message. split=None reads the setting (off here); True or False pins it."""
    be = Answers(answers_for(body, requests, playbook, score))
    return graph.run(TEAMS, ev(text), be, playbooks=PBS, split_asks=split, batch=batch), be


def rank(res):
    """What reaches the PM: 2 a person is asked, 1 a notice, 0 nothing."""
    return 2 if res.get("needs_human") else 1 if res.get("notify") else 0


def question_ids(be):
    return [qids for _, qids in be.calls]


# The default outputs of nine messages (split_asks off), taken at the commit before R35. A change here changes what the PM reads.
GOLDEN_CASES = {
    "status_two_asks": (TWO, {"intent": ans("status")}),
    "decision_today": ("リリース日を決めてください。", {"intent": ans("decision"), "playbook": ans("none"), "urgency": ans(score=2.0)}),
    "decision_later": ("リリース日を決めてください。", {"intent": ans("decision"), "playbook": ans("none"), "urgency": ans(score=1.0)}),
    "none_ask_pm": ("今日も暑いですね", {"intent": ans("none")}),
    "incident_bug": ("ログインでエラーが出ています", {"intent": ans("incident")}),
    "fyi_log": ("共有します。", {"intent": ans("fyi")}),
    "requirements_ask_pm": ("仕様を整理してもらえますか？", {"intent": ans("requirements"), "playbook": ans("none")}),
    "unsure_ask_pm": ("すみません、少し確認したいです", {"intent": ans("status", conf=0.2)}),
    "ack_only": ("ありがとうございます", {}),
}
GOLDEN = {'posts': {'status_two_asks': '[kimeru #7] teams.chat\n'
                              '→ 内容を確認してください\n'
                              '案: 最新状況はボードをご確認ください（kimeru 自動返信）。不足があれば返信ください。\n'
                              'OK 7 / NG 7 / 詳細 7',
           'decision_today': '[kimeru #7] 本日中に判断が必要: Sato「リリース日を決めてください。」\n'
                             '→ 本日中に判断が必要: Sato「リリース日を決めてください。」\n'
                             '案: 受領しました。本日中に判断して返信します。\n'
                             'OK 7 / NG 7 / 詳細 7',
           'decision_later': '[kimeru #7] 今週の判断リストに追加: Sato「リリース日を決めてください。」\n'
                             '→ 今週の判断リストに追加: Sato「リリース日を決めてください。」\n'
                             '案: 受領しました。今週中に判断します。\n'
                             'OK 7 / NG 7 / 詳細 7',
           'none_ask_pm': '[kimeru #7] 意図が判定できないチャット: Sato「今日も暑いですね」\n'
                          '→ 意図が判定できないチャット: Sato「今日も暑いですね」\n'
                          '案: 受領しました。内容を確認して返信します。\n'
                          'OK 7 / NG 7 / 詳細 7',
           'incident_bug': '[kimeru #7] 障害報告をBug化した。監視アラートと突き合わせること\n'
                           '→ 障害報告をBug化した。監視アラートと突き合わせること\n'
                           '起票案: [Teams報告] ログインでエラーが出ています\n'
                           'OK 7 / NG 7 / 詳細 7',
           'fyi_log': '[kimeru #7] teams.chat\n→ 内容を確認してください\nOK 7 / NG 7 / 詳細 7',
           'requirements_ask_pm': '[kimeru #7] 意図が判定できないチャット: Sato「仕様を整理してもらえますか？」\n'
                                  '→ 意図が判定できないチャット: Sato「仕様を整理してもらえますか？」\n'
                                  '案: 受領しました。内容を確認して返信します。\n'
                                  'OK 7 / NG 7 / 詳細 7',
           'unsure_ask_pm': '[kimeru #7] 意図が判定できないチャット: Sato「すみません、少し確認したいです」\n'
                            '→ 意図が判定できないチャット: Sato「すみません、少し確認したいです」\n'
                            '案: 受領しました。内容を確認して返信します。\n'
                            'OK 7 / NG 7 / 詳細 7',
           'ack_only': '[kimeru #7] teams.chat\n→ 内容を確認してください\nOK 7 / NG 7 / 詳細 7'},
 'details': {'status_two_asks': '[kimeru 詳細 #7] 判断が必要（teams-chat-triage） / 改訂 1\n'
                                '判断: 判断の詳細は以下を確認してください\n'
                                '次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                                '不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                                'OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）\n'
                                'teams.chat #g1\n'
                                '承認の対象: teams.reply（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n'
                                '返信: OK 7 / NG 7 / 保留 7',
             'decision_today': '[kimeru 詳細 #7] 判断が必要（teams-chat-triage） / 改訂 1\n'
                               '判断: 本日中に判断が必要: Sato「リリース日を決めてください。」\n'
                               '次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                               '不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                               'OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）\n'
                               'teams.chat #g1\n'
                               '内容: 本日中に判断が必要: Sato「リリース日を決めてください。」\n'
                               '承認の対象: teams.reply（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n'
                               '返信: OK 7 / NG 7 / 保留 7',
             'decision_later': '[kimeru 詳細 #7] 判断が必要（teams-chat-triage） / 改訂 1\n'
                               '判断: 今週の判断リストに追加: Sato「リリース日を決めてください。」\n'
                               '次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                               '不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                               'OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）; 記録のみ\n'
                               'teams.chat #g1\n'
                               '内容: 今週の判断リストに追加: Sato「リリース日を決めてください。」\n'
                               '承認の対象: teams.reply, ado.create（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n'
                               '返信: OK 7 / NG 7 / 保留 7',
             'none_ask_pm': '[kimeru 詳細 #7] 判断が必要（teams-chat-triage） / 改訂 1\n'
                            '判断: 意図が判定できないチャット: Sato「今日も暑いですね」\n'
                            '次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                            '不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                            'OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）; 記録のみ\n'
                            'teams.chat #g1\n'
                            '内容: 意図が判定できないチャット: Sato「今日も暑いですね」\n'
                            '承認の対象: teams.reply, ado.create（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n'
                            '返信: OK 7 / NG 7 / 保留 7',
             'incident_bug': '[kimeru 詳細 #7] 判断が必要（teams-chat-triage） / 改訂 1\n'
                             '判断: 障害報告をBug化した。監視アラートと突き合わせること\n'
                             '次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                             '不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                             'OK の効果: 記録のみ\n'
                             'teams.chat #g1\n'
                             '内容: 障害報告をBug化した。監視アラートと突き合わせること\n'
                             '承認の対象: ado.create（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n'
                             '返信: OK 7 / NG 7 / 保留 7',
             'fyi_log': '[kimeru 詳細 #7] 判断が必要（teams-chat-triage） / 改訂 1\n'
                        '判断: 判断の詳細は以下を確認してください\n'
                        '次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                        '不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                        'OK の効果: 記録のみ\n'
                        'teams.chat #g1\n'
                        '承認の対象: log.only（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n'
                        '返信: OK 7 / NG 7 / 保留 7',
             'requirements_ask_pm': '[kimeru 詳細 #7] 判断が必要（teams-chat-triage） / 改訂 1\n'
                                    '判断: 意図が判定できないチャット: Sato「仕様を整理してもらえますか？」\n'
                                    '次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                                    '不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                                    'OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）; 記録のみ\n'
                                    'teams.chat #g1\n'
                                    '内容: 意図が判定できないチャット: Sato「仕様を整理してもらえますか？」\n'
                                    '承認の対象: teams.reply, ado.create（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n'
                                    '返信: OK 7 / NG 7 / 保留 7',
             'unsure_ask_pm': '[kimeru 詳細 #7] 判断が必要（teams-chat-triage） / 改訂 1\n'
                              '判断: 意図が判定できないチャット: Sato「すみません、少し確認したいです」\n'
                              '次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                              '不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                              'OK の効果: コピー用文面を自分チャットへ返す（相手には送信しない）; 記録のみ\n'
                              'teams.chat #g1\n'
                              '内容: 意図が判定できないチャット: Sato「すみません、少し確認したいです」\n'
                              '承認の対象: teams.reply, ado.create（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n'
                              '返信: OK 7 / NG 7 / 保留 7',
             'ack_only': '[kimeru 詳細 #7] 判断が必要（teams-chat-triage） / 改訂 1\n'
                         '判断: 判断の詳細は以下を確認してください\n'
                         '次の一手: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                         '不足情報: （判断メモなし。文面用の LLM〔writer〕を設定すると付きます）\n'
                         'OK の効果: 記録のみ\n'
                         'teams.chat #g1\n'
                         '承認の対象: log.only（既定は記録だけ。実行するのは設定で有効にした種類だけ）\n'
                         '返信: OK 7 / NG 7 / 保留 7'},
 'notices': {'status_two_asks': '[kimeru 通知] teams.chat\n→ 自動で決定しました',
             'decision_today': '[kimeru 通知] 本日中に判断が必要: Sato「リリース日を決めてください。」\n'
                               '→ 本日中に判断が必要: Sato「リリース日を決めてください。」',
             'decision_later': '[kimeru 通知] 今週の判断リストに追加: Sato「リリース日を決めてください。」\n'
                               '→ 今週の判断リストに追加: Sato「リリース日を決めてください。」',
             'none_ask_pm': '[kimeru 通知] 意図が判定できないチャット: Sato「今日も暑いですね」\n'
                            '→ 意図が判定できないチャット: Sato「今日も暑いですね」\n'
                            '下書きは別の投稿で承認待ちです',
             'incident_bug': '[kimeru 通知] 障害報告をBug化した。監視アラートと突き合わせること\n'
                             '→ 障害報告をBug化した。監視アラートと突き合わせること',
             'fyi_log': '[kimeru 通知] teams.chat\n→ 自動で決定しました',
             'requirements_ask_pm': '[kimeru 通知] 意図が判定できないチャット: Sato「仕様を整理してもらえますか？」\n'
                                    '→ 意図が判定できないチャット: Sato「仕様を整理してもらえますか？」\n'
                                    '下書きは別の投稿で承認待ちです',
             'unsure_ask_pm': '[kimeru 通知] 意図が判定できないチャット: Sato「すみません、少し確認したいです」\n'
                              '→ 意図が判定できないチャット: Sato「すみません、少し確認したいです」\n'
                              '下書きは別の投稿で承認待ちです',
             'ack_only': '[kimeru 通知] teams.chat\n→ 自動で決定しました'},
 'rows': {'status_two_asks': '{"actions": [{"chat_id": "19:synthetic-chat-1@unq.gbl.spaces", '
                             '"text": "最新状況はボードをご確認ください（kimeru 自動返信）。不足があれば返信ください。", "type": '
                             '"teams.reply"}], "advice": null, "event_id": "g1", "event_kind": '
                             '"teams.chat", "graph": "teams-chat-triage", "needs_human": false, '
                             '"node": "reply_status", "notify": false, "outcome": "decide", '
                             '"path": [{"answer": {"matched": null}, "edge": "no", "node": '
                             '"critical_incident"}, {"answer": {"matched": null}, "edge": "no", '
                             '"node": "ack_only"}, {"answer": {"choice": "status", "confidence": '
                             '0.9, "probabilities": {}}, "edge": "status", "node": "intent"}]}',
          'decision_today': '{"actions": [{"chat_id": "19:synthetic-chat-1@unq.gbl.spaces", '
                            '"text": "受領しました。本日中に判断して返信します。", "type": "teams.reply"}], "advice": '
                            '"本日中に判断が必要: Sato「リリース日を決めてください。」", "event_id": "g1", "event_kind": '
                            '"teams.chat", "graph": "teams-chat-triage", "needs_human": false, '
                            '"node": "decision_today", "notify": true, "outcome": "decide", '
                            '"path": [{"answer": {"matched": null}, "edge": "no", "node": '
                            '"critical_incident"}, {"answer": {"matched": null}, "edge": "no", '
                            '"node": "ack_only"}, {"answer": {"choice": "decision", "confidence": '
                            '0.9, "probabilities": {}}, "edge": "decision", "node": "intent"}, '
                            '{"answer": {"answers": {}, "confidence": 0.9, "playbook": "none", '
                            '"steps": []}, "edge": "none", "node": "plan_decision"}, {"answer": '
                            '{"confidence": 0.9, "probabilities": {}, "score": 2.0}, "edge": "<4", '
                            '"node": "urgency"}]}',
          'decision_later': '{"actions": [{"chat_id": "19:synthetic-chat-1@unq.gbl.spaces", '
                            '"text": "受領しました。今週中に判断します。", "type": "teams.reply"}, {"tags": '
                            '"kimeru;decision", "title": "判断: リリース日を決めてください。", "type": '
                            '"ado.create", "work_item_type": "Task"}], "advice": "今週の判断リストに追加: '
                            'Sato「リリース日を決めてください。」", "event_id": "g1", "event_kind": "teams.chat", '
                            '"graph": "teams-chat-triage", "needs_human": false, "node": '
                            '"decision_later", "notify": false, "outcome": "decide", "path": '
                            '[{"answer": {"matched": null}, "edge": "no", "node": '
                            '"critical_incident"}, {"answer": {"matched": null}, "edge": "no", '
                            '"node": "ack_only"}, {"answer": {"choice": "decision", "confidence": '
                            '0.9, "probabilities": {}}, "edge": "decision", "node": "intent"}, '
                            '{"answer": {"answers": {}, "confidence": 0.9, "playbook": "none", '
                            '"steps": []}, "edge": "none", "node": "plan_decision"}, {"answer": '
                            '{"confidence": 0.9, "probabilities": {}, "score": 1.0}, "edge": '
                            '"<1.5", "node": "urgency"}]}',
          'none_ask_pm': '{"actions": [{"chat_id": "19:synthetic-chat-1@unq.gbl.spaces", "text": '
                         '"受領しました。内容を確認して返信します。", "type": "teams.reply"}, {"description": '
                         '"今日も暑いですね", "tags": "kimeru;from-teams", "title": "対応: Satoさんからの依頼", '
                         '"type": "ado.create", "work_item_type": "Task"}], "advice": '
                         '"意図が判定できないチャット: Sato「今日も暑いですね」", "event_id": "g1", "event_kind": '
                         '"teams.chat", "graph": "teams-chat-triage", "needs_human": true, "node": '
                         '"ask_pm", "notify": false, "outcome": "advise", "path": [{"answer": '
                         '{"matched": null}, "edge": "no", "node": "critical_incident"}, '
                         '{"answer": {"matched": null}, "edge": "no", "node": "ack_only"}, '
                         '{"answer": {"choice": "none", "confidence": 0.9, "probabilities": {}}, '
                         '"edge": "none", "node": "intent"}]}',
          'incident_bug': '{"actions": [{"tags": "kimeru;from-teams", "title": "[Teams報告] '
                          'ログインでエラーが出ています", "type": "ado.create", "work_item_type": "Bug"}], '
                          '"advice": "障害報告をBug化した。監視アラートと突き合わせること", "event_id": "g1", '
                          '"event_kind": "teams.chat", "graph": "teams-chat-triage", '
                          '"needs_human": false, "node": "incident_bug", "notify": true, '
                          '"outcome": "decide", "path": [{"answer": {"matched": null}, "edge": '
                          '"no", "node": "critical_incident"}, {"answer": {"matched": null}, '
                          '"edge": "no", "node": "ack_only"}, {"answer": {"choice": "incident", '
                          '"confidence": 0.9, "probabilities": {}}, "edge": "incident", "node": '
                          '"intent"}]}',
          'fyi_log': '{"actions": [{"type": "log.only"}], "advice": null, "event_id": "g1", '
                     '"event_kind": "teams.chat", "graph": "teams-chat-triage", "needs_human": '
                     'false, "node": "log_fyi", "notify": false, "outcome": "decide", "path": '
                     '[{"answer": {"matched": null}, "edge": "no", "node": "critical_incident"}, '
                     '{"answer": {"matched": null}, "edge": "no", "node": "ack_only"}, {"answer": '
                     '{"choice": "fyi", "confidence": 0.9, "probabilities": {}}, "edge": "fyi", '
                     '"node": "intent"}]}',
          'requirements_ask_pm': '{"actions": [{"chat_id": "19:synthetic-chat-1@unq.gbl.spaces", '
                                 '"text": "受領しました。内容を確認して返信します。", "type": "teams.reply"}, '
                                 '{"description": "仕様を整理してもらえますか？", "tags": "kimeru;from-teams", '
                                 '"title": "対応: Satoさんからの依頼", "type": "ado.create", '
                                 '"work_item_type": "Task"}], "advice": "意図が判定できないチャット: '
                                 'Sato「仕様を整理してもらえますか？」", "event_id": "g1", "event_kind": '
                                 '"teams.chat", "graph": "teams-chat-triage", "needs_human": true, '
                                 '"node": "ask_pm", "notify": false, "outcome": "advise", "path": '
                                 '[{"answer": {"matched": null}, "edge": "no", "node": '
                                 '"critical_incident"}, {"answer": {"matched": null}, "edge": '
                                 '"no", "node": "ack_only"}, {"answer": {"choice": "requirements", '
                                 '"confidence": 0.9, "probabilities": {}}, "edge": "requirements", '
                                 '"node": "intent"}, {"answer": {"answers": {}, "confidence": 0.9, '
                                 '"playbook": "none", "steps": []}, "edge": "none", "node": '
                                 '"plan_requirements"}]}',
          'unsure_ask_pm': '{"actions": [{"chat_id": "19:synthetic-chat-1@unq.gbl.spaces", "text": '
                           '"受領しました。内容を確認して返信します。", "type": "teams.reply"}, {"description": '
                           '"すみません、少し確認したいです", "tags": "kimeru;from-teams", "title": "対応: '
                           'Satoさんからの依頼", "type": "ado.create", "work_item_type": "Task"}], '
                           '"advice": "意図が判定できないチャット: Sato「すみません、少し確認したいです」", "event_id": "g1", '
                           '"event_kind": "teams.chat", "graph": "teams-chat-triage", '
                           '"needs_human": true, "node": "ask_pm", "notify": false, "outcome": '
                           '"advise", "path": [{"answer": {"matched": null}, "edge": "no", "node": '
                           '"critical_incident"}, {"answer": {"matched": null}, "edge": "no", '
                           '"node": "ack_only"}, {"answer": {"choice": "status", "confidence": '
                           '0.2, "probabilities": {}}, "edge": "unsure", "node": "intent"}], '
                           '"via_unsure": ["intent"]}',
          'ack_only': '{"actions": [{"type": "log.only"}], "advice": null, "event_id": "g1", '
                      '"event_kind": "teams.chat", "graph": "teams-chat-triage", "needs_human": '
                      'false, "node": "log_fyi", "notify": false, "outcome": "decide", "path": '
                      '[{"answer": {"matched": null}, "edge": "no", "node": "critical_incident"}, '
                      '{"answer": {"matched": '
                      '"^(?!.*\\\\.\\\\.)(?:[^:?\\\\n]{1,30}:\\\\s*)?(?:@\\\\S+(?:\\\\s*さん)?\\\\s*)?(?:了解(?:しました|いたしました|です)?|承知(?:しました|いたしました|です)?|かしこまりました|(?:わかり|分かり)ました|ok(?:です)?|おk|オッケー(?:です)?|りょ(?:うかい)?(?:です)?|(?:どうも)?ありがとう(?:ございます|ございました)?|感謝(?:します|いたします|です)|助かり(?:ます|ました)|お?(?:疲れ|つかれ)(?:様|さま)(?:です|でした)|いえいえ|こちらこそ|(?:今日|本日)も|確認(?:します|しました|いたします|いたしました)|対応(?:します|しました|しておきます|いたします))(?:[\\\\s、。,.!~〜ーｰw笑☀-➿\\\\U0001F300-\\\\U0001FAFF\u200d️]*(?:(?:了解(?:しました|いたしました|です)?|承知(?:しました|いたしました|です)?|かしこまりました|(?:わかり|分かり)ました|ok(?:です)?|おk|オッケー(?:です)?|りょ(?:うかい)?(?:です)?|(?:どうも)?ありがとう(?:ございます|ございました)?|感謝(?:します|いたします|です)|助かり(?:ます|ました)|お?(?:疲れ|つかれ)(?:様|さま)(?:です|でした)|いえいえ|こちらこそ|(?:今日|本日)も|確認(?:します|しました|いたします|いたしました)|対応(?:します|しました|しておきます|いたします))|(?:よろしくお願い(?:します|いたします|致します))))*[\\\\s、。,.!~〜ーｰw笑☀-➿\\\\U0001F300-\\\\U0001FAFF\u200d️]*$"}, '
                      '"edge": "yes", "node": "ack_only"}]}'},
 'calls': {'status_two_asks': [{'state_keys': ['author',
                                               'chat_id',
                                               'id',
                                               'kind',
                                               'mentions_me',
                                               'source',
                                               'text',
                                               'ts'],
                                'questions': ['intent']}],
           'decision_today': [{'state_keys': ['author',
                                              'chat_id',
                                              'id',
                                              'kind',
                                              'mentions_me',
                                              'source',
                                              'text',
                                              'ts'],
                               'questions': ['intent']},
                              {'state_keys': ['author',
                                              'chat_id',
                                              'id',
                                              'kind',
                                              'mentions_me',
                                              'source',
                                              'text',
                                              'ts'],
                               'questions': ['playbook']},
                              {'state_keys': ['author',
                                              'chat_id',
                                              'id',
                                              'kind',
                                              'mentions_me',
                                              'source',
                                              'text',
                                              'ts'],
                               'questions': ['urgency']}],
           'decision_later': [{'state_keys': ['author',
                                              'chat_id',
                                              'id',
                                              'kind',
                                              'mentions_me',
                                              'source',
                                              'text',
                                              'ts'],
                               'questions': ['intent']},
                              {'state_keys': ['author',
                                              'chat_id',
                                              'id',
                                              'kind',
                                              'mentions_me',
                                              'source',
                                              'text',
                                              'ts'],
                               'questions': ['playbook']},
                              {'state_keys': ['author',
                                              'chat_id',
                                              'id',
                                              'kind',
                                              'mentions_me',
                                              'source',
                                              'text',
                                              'ts'],
                               'questions': ['urgency']}],
           'none_ask_pm': [{'state_keys': ['author',
                                           'chat_id',
                                           'id',
                                           'kind',
                                           'mentions_me',
                                           'source',
                                           'text',
                                           'ts'],
                            'questions': ['intent']}],
           'incident_bug': [{'state_keys': ['author',
                                            'chat_id',
                                            'id',
                                            'kind',
                                            'mentions_me',
                                            'source',
                                            'text',
                                            'ts'],
                             'questions': ['intent']}],
           'fyi_log': [{'state_keys': ['author',
                                       'chat_id',
                                       'id',
                                       'kind',
                                       'mentions_me',
                                       'source',
                                       'text',
                                       'ts'],
                        'questions': ['intent']}],
           'requirements_ask_pm': [{'state_keys': ['author',
                                                   'chat_id',
                                                   'id',
                                                   'kind',
                                                   'mentions_me',
                                                   'source',
                                                   'text',
                                                   'ts'],
                                    'questions': ['intent']},
                                   {'state_keys': ['author',
                                                   'chat_id',
                                                   'id',
                                                   'kind',
                                                   'mentions_me',
                                                   'source',
                                                   'text',
                                                   'ts'],
                                    'questions': ['playbook']}],
           'unsure_ask_pm': [{'state_keys': ['author',
                                             'chat_id',
                                             'id',
                                             'kind',
                                             'mentions_me',
                                             'source',
                                             'text',
                                             'ts'],
                              'questions': ['intent']}],
           'ack_only': []}}


class TestTheDefaultIsUnchanged(unittest.TestCase):
    def test_every_golden_case_gives_the_same_post_detail_notice_row_and_questions(self):
        for name, (text, answers) in GOLDEN_CASES.items():
            with self.subTest(name):
                be = Answers(answers)
                res = graph.run(TEAMS, ev(text), be, playbooks=PBS)   # no argument: the setting decides, and it is off
                self.assertEqual(notify.format_post(7, res), GOLDEN["posts"][name])
                self.assertEqual(notify.format_detail(7, res), GOLDEN["details"][name])
                self.assertEqual(notify.format_notice(res), GOLDEN["notices"][name])
                self.assertEqual(json.dumps(res, sort_keys=True, ensure_ascii=False, default=str), GOLDEN["rows"][name])
                self.assertEqual([{"state_keys": sorted(s), "questions": q} for s, q in be.calls], GOLDEN["calls"][name])

    def test_the_setting_is_off_by_default_and_takes_only_0_or_1(self):
        self.assertEqual(config.value("split_asks"), "0")
        self.assertEqual(config.CHOICES["split_asks"], ("0", "1"))

    def test_two_requests_with_the_setting_off_are_one_question_and_no_request_field(self):
        res, be = judge(TWO, "status", ["status", "decision"], split=False)
        self.assertEqual(res["node"], "reply_status")
        self.assertNotIn("asks", res)
        self.assertEqual(question_ids(be), [["intent"]])


class TestRequestsOfAMessage(unittest.TestCase):
    def test_two_questions_in_one_message_are_two_requests(self):
        self.assertEqual(asks.find(TWO), ["進捗どうですか？", "あとリリース日を決めてください"])

    def test_thanks_and_a_decision_already_made_are_not_requests(self):
        self.assertEqual(asks.find("決めました。よろしくお願いします。"), [])
        self.assertEqual(asks.find("決めましたので、次は見てください。"), ["決めましたので、次は見てください。"])

    def test_a_message_without_a_request_mark_has_no_requests(self):
        self.assertEqual(asks.find("共有します。ありがとう。"), [])
        self.assertEqual(asks.find(None), [])

    def test_at_most_three_requests_are_taken_in_order(self):
        self.assertEqual(asks.find("一つ目ですか？二つ目ですか？三つ目ですか？四つ目ですか？"),
                         ["一つ目ですか？", "二つ目ですか？", "三つ目ですか？"])

    def test_a_single_request_is_judged_as_before(self):
        res, be = judge("進捗どうですか？", "status", ["status"], split=True)
        self.assertEqual(res["node"], "reply_status")
        self.assertNotIn("asks", res)
        self.assertEqual(question_ids(be), [["intent"]])


class TestTheRouteOfASplitMessage(unittest.TestCase):
    def test_a_status_message_with_a_decision_request_is_not_answered_with_status(self):
        res, _ = judge(TWO, "status", ["status", "decision"], split=True)
        self.assertNotIn("reply_status", [s["node"] for s in res["path"]])
        self.assertIn(res["node"], ("decision_planned", "decision_today"))
        self.assertEqual(res["node"], "decision_today")
        self.assertTrue(res["notify"])

    def test_a_decision_that_is_not_urgent_is_kept_for_later_with_a_notice_and_a_guard_word(self):
        res, _ = judge(TWO, "status", ["status", "decision"], split=True, score=1.0)
        self.assertEqual(res["node"], "decision_later")
        self.assertTrue(res["notify"])
        self.assertEqual(res["asks_guard"], "decision")

    def test_a_none_message_with_fyi_requests_stays_with_the_pm(self):
        res, _ = judge("今日も暑いですね？次回の予定を教えてください。", "none", ["fyi", "fyi"], split=True)
        self.assertEqual(res["node"], "ask_pm")
        self.assertTrue(res["needs_human"])

    def test_an_incident_request_turns_a_status_message_into_an_incident(self):
        res, _ = judge("進捗どうですか？ログインでエラーが出ています、見てもらえますか？", "status", ["status", "incident"], split=True)
        self.assertEqual(res["node"], "incident_bug")
        self.assertTrue(res["notify"])

    def test_a_report_without_a_request_mark_is_not_a_request_of_its_own(self):
        self.assertEqual(asks.find("進捗どうですか？ログインでエラーが出ています。"), ["進捗どうですか？"])
        res, _ = judge("進捗どうですか？ログインでエラーが出ています。", "status", ["status", "incident"], split=True)
        self.assertEqual(res["node"], "reply_status")
        self.assertNotIn("asks", res)

    def test_an_incident_request_does_not_lower_a_none_message_to_a_notice(self):
        res, _ = judge("今日も暑いですね？ログインでエラーが出ています、見てもらえますか？", "none", ["none", "incident"], split=True)
        self.assertEqual(res["node"], "ask_pm")
        self.assertTrue(res["needs_human"])

    def test_an_unsure_request_sends_the_whole_message_to_the_pm(self):
        res, _ = judge(TWO, "status", ["status", "unsure"], split=True)
        self.assertEqual(res["node"], "ask_pm")
        self.assertTrue(res["needs_human"])
        self.assertIn("intent", [s["node"] for s in res["path"] if s["edge"] == "unsure"])

    def test_the_urgency_and_the_plan_are_asked_about_the_text_without_the_requests(self):
        _, be = judge(TWO, "status", ["status", "decision"], split=True)
        later = [state for state, qids in be.calls if qids != ["intent", "intent_0", "intent_1"]]
        self.assertTrue(later)
        for state in later:
            self.assertNotIn("asks", state)
        self.assertIn("asks", be.calls[0][0])


class TestTheRecordAndTheTextsCarryOnlyTheCount(unittest.TestCase):
    def test_the_record_keeps_the_count_and_the_confidences_only(self):
        res, _ = judge(TWO, "status", ["status", "decision"], split=True)
        self.assertEqual(res["asks"], {"n": 2, "confidence": [0.9, 0.9]})
        self.assertEqual(set(res["asks"]), {"n", "confidence"})

    def test_the_post_detail_and_notice_name_the_other_requests_when_split(self):
        res, _ = judge(TWO, "status", ["status", "decision"], split=True)
        self.assertIn("（ほかに依頼 1 件）", notify.format_post(7, res))
        self.assertIn("（ほかに依頼 1 件）", notify.format_detail(7, res))
        self.assertIn("（ほかに依頼 1 件）", notify.format_notice(res))

    def test_a_result_without_requests_has_no_other_request_text(self):
        res, _ = judge("リリース日を決めてください。", "decision", split=True)
        for text in (notify.format_post(7, res), notify.format_detail(7, res), notify.format_notice(res)):
            self.assertNotIn("ほかに依頼", text)

    def test_the_setting_alone_turns_the_split_on_for_the_run(self):
        with mock.patch.dict(os.environ, {"KIMERU_SPLIT_ASKS": "1"}):
            self.assertEqual(config.value("split_asks"), "1")
            be = Answers(answers_for("status", ["status", "decision"]))
            res = graph.run(TEAMS, ev(TWO), be, playbooks=PBS)
        self.assertEqual(res["asks"]["n"], 2)
        self.assertEqual(res["node"], "decision_today")

    def test_a_decisions_line_written_with_the_setting_carries_the_count_and_no_request_field_otherwise(self):
        with mock.patch.dict(os.environ, {"KIMERU_SPLIT_ASKS": "1"}):
            out = Path(tempfile.mkdtemp(prefix="split-asks-"))
            be = Answers(answers_for("status", ["status", "decision"]))
            cli.process(ev(TWO), GRAPHS, be, out, PBS, now=NOW)
        row = json.loads((out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["asks"], {"n": 2, "confidence": [0.9, 0.9]})


class TestTheSettingIsDocumented(unittest.TestCase):
    def test_the_settings_table_has_one_row_and_the_security_notes_and_the_changelog_name_split_asks(self):
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        self.assertEqual(sum(1 for line in ref.splitlines() if line.startswith("| `KIMERU_SPLIT_ASKS`")), 1)
        self.assertIn("split_asks", (ROOT / "SECURITY.md").read_text(encoding="utf-8"))
        self.assertIn("split_asks", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))


class TestBatchMode(unittest.TestCase):
    def test_in_batch_mode_the_requests_are_asked_in_their_own_call_and_the_rest_without_them(self):
        res, be = judge(TWO, "status", ["status", "decision"], split=True, batch=True)
        self.assertEqual(be.calls[0][1], ["intent", "intent_0", "intent_1"])
        self.assertEqual(be.calls[0][0]["asks"], asks.find(TWO))
        for state, qids in be.calls[1:]:
            self.assertNotIn("asks", state, qids)
        self.assertEqual(res["node"], "decision_today")


class TestThePlannedPathsAreNeverQuieterThanTheSingleIntent(unittest.TestCase):
    """The combination table of tests/test_judgment_gaps.py asks with no playbook that fits, so the plan nodes never answered ok.
    Here the playbook answer fits (ReplayBackend answers `playbook`), so the route can end in decision_planned or
    requirements_planned, and the final node is never quieter than the single-intent result."""
    LABELS = CHOICES + ("unsure",)

    @staticmethod
    def judge(text, body, requests=(), split=None, playbook="schedule_change"):
        """Like judge(), with the plan answers of the playbook: every step is needed, due today, and the first step goes first."""
        steps = PBS[playbook]["steps"]
        extra = {"first": ans(steps[0]["id"])}
        for s in steps:
            if s.get("check"):
                extra[f"need_{s['id']}"] = {"type": "noul", "noul": 0.9, "confidence": 0.9, "probabilities": {}}
            extra[f"due_{s['id']}"] = ans(score=2.0)
        be = Answers({**answers_for(body, requests, playbook), **extra})
        return graph.run(TEAMS, ev(text), be, playbooks=PBS, split_asks=split), be

    def test_a_status_body_with_a_decision_request_and_a_fitting_playbook_ends_in_decision_planned(self):
        single, _ = self.judge(TWO, "status", split=False, playbook="schedule_change")
        res, be = self.judge(TWO, "status", ["status", "decision"], split=True, playbook="schedule_change")
        self.assertEqual(single["node"], "reply_status")
        self.assertEqual(res["node"], "decision_planned")
        self.assertIn("plan_decision", [s["node"] for s in res["path"]])
        self.assertGreaterEqual(rank(res), rank(single))
        self.assertTrue(res["notify"])
        for state, qids in be.calls[1:]:
            self.assertNotIn("asks", state, qids)

    def test_no_pair_of_requests_ends_quieter_than_the_single_intent_when_a_playbook_fits(self):
        reached = set()
        for playbook in ("schedule_change", "requirements_review"):
            for body in self.LABELS:
                single, _ = self.judge(TWO, body, split=False, playbook=playbook)
                for first in self.LABELS:
                    for second in self.LABELS:
                        with self.subTest(playbook=playbook, body=body, requests=(first, second)):
                            res, _ = self.judge(TWO, body, [first, second], split=True, playbook=playbook)
                            reached.add(res["node"])
                            self.assertGreaterEqual(rank(res), rank(single))
                            self.assertIn(res["node"], NODES)
                            self.assertTrue(all(step["node"] in NODES for step in res["path"]))
        self.assertIn("decision_planned", reached)   # the table does cover the plan path
        self.assertIn("requirements_planned", reached)

    def test_a_planned_single_intent_stays_planned_or_louder_for_every_request_pair(self):
        for body, planned in (("decision", "decision_planned"), ("requirements", "requirements_planned")):
            playbook = "schedule_change" if body == "decision" else "requirements_review"
            single, _ = self.judge(TWO, body, split=False, playbook=playbook)
            self.assertEqual(single["node"], planned)
            for first in self.LABELS:
                for second in self.LABELS:
                    with self.subTest(body=body, requests=(first, second)):
                        res, _ = self.judge(TWO, body, [first, second], split=True, playbook=playbook)
                        self.assertGreaterEqual(rank(res), rank(single))


if __name__ == "__main__":
    unittest.main()
