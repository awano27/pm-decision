try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import unittest
from pathlib import Path

from kimeru import events, graph, plan
from kimeru.backends import ReplayBackend

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = {k: v[0] for k, v in graph.load_dir(ROOT / "graphs", PBS).items()}
ADO = GRAPHS["ado.workitem.created"]
TEAMS = GRAPHS["teams.chat"]
CRIT_BUG = ADO["nodes"]["critical_bug"]
CRIT_CHAT = TEAMS["nodes"]["critical_incident"]


def workitem(title="", desc="", repro="", ac="", typ="Bug", severity=None, prio=2):
    f = {"System.Id": 1, "System.WorkItemType": typ, "System.Title": title, "System.Description": desc,
         "Microsoft.VSTS.TCM.ReproSteps": repro, "Microsoft.VSTS.Common.AcceptanceCriteria": ac,
         "System.CreatedDate": "2026-10-08T01:00:00Z", "Microsoft.VSTS.Common.Priority": prio}
    if typ is None:
        del f["System.WorkItemType"]
    if severity is not None:
        f["Microsoft.VSTS.Common.Severity"] = severity
    return events.normalize({"eventType": "workitem.created", "resource": {"id": 1, "fields": f}})[0]


def bug_rule(**kw):
    return graph.match_eval(CRIT_BUG, workitem(**kw))[0]


def chat_rule(text):
    return graph.match_eval(CRIT_CHAT, {"kind": "teams.chat", "text": text})[0]


# --- Teams critical_incident: sentence scope -------------------------------------------------------------------
CHAT_INCIDENT = [   # a current breakage stays an incident whatever else the message says
    "本番で決済が通りません。誰か見られますか？",
    "本番でログインできなくなっています。原因は調査中です。次の報告は30分後の予定です",
    "本番の決済が止まっています。次の報告は30分後の予定です",
    "Production is down. Let me know if you need anything.",
    "Production is down. Can someone take a look?",
    "本番で決済が通りません。テスト環境では正常です",
    "本番で決済が通りません",
    "本番が落ちています",
]
CHAT_TO_MODEL = [   # a question, plan, test or negation about an outage in the SAME sentence
    "ログインできない人は連絡ください", "本番リリースでエラーが出ないか確認お願いします", "決済できないケースのテストを追加しました",
    "購入できない場合の仕様は", "顧客からエラーの報告はありません", "本番は停止せずにリリース可能です", "本番反映できない理由を教えて",
    "no outage expected", "本番で決済できないのですか？", "本番でログインできなくならないよう確認します。以上です",
    "テスト環境でログインできなくなっています。本番は問題ありません", "Is production down? Please check", "本番で決済が通りませんか？",
]


class TestTeamsSentenceScope(unittest.TestCase):
    def test_table_sizes(self):
        self.assertEqual((len(CHAT_INCIDENT), len(CHAT_TO_MODEL)), (8, 13))

    def test_incident_next_to_a_question_or_plan_stays_an_incident(self):
        for text in CHAT_INCIDENT:
            self.assertEqual(chat_rule(text), "yes", text)

    def test_exclusion_words_in_the_same_sentence_still_send_it_to_the_model(self):
        for text in CHAT_TO_MODEL:
            self.assertNotEqual(chat_rule(text), "yes", text)

    def test_graph_routes(self):
        be = ReplayBackend({"intent": {"choice": "fyi", "confidence": 0.9}})
        r = graph.run(TEAMS, {"kind": "teams.chat", "id": "c", "text": CHAT_INCIDENT[0], "author": "a"}, be)
        self.assertEqual((r["node"], r["notify"]), ("incident_bug", True))


# --- ADO critical_bug --------------------------------------------------------------------------------------------
BUG_NOT_P1 = [   # review M1: improvement / design / docs / FAQ / postmortem / request wording must not be a P1
    "Crashlytics の SDK を更新", "クラッシュレポート収集基盤の導入", "強制終了ボタンを追加", "本番環境でエラーメッセージの文言を修正",
    "顧客から要望: ログインできない時の案内文を改善", "outage 時の連絡フロー整備", "起動しない場合のFAQページを作成",
    "ログイン不可時のヘルプリンク追加", "Investigate why the app crashed last quarter (postmortem)",
    "all users の一覧画面に列を追加", "顧客からエラー表示の色を変えたいと要望", "決済できない場合のエラーハンドリングを追加（設計）",
    "障害対応の手順書を更新", "本番障害の振り返り", "テスト環境でアプリがクラッシュする", "強制終了時のログ収集を追加",
    "本番のタイムアウト値を変更", "本番リリース作業が止まっている", "クラッシュ時のログ収集を追加", "Add a crash report button",
]
BUG_P1 = [   # review M2: typical outage wording a rule can be confident about
    "本番で全ユーザーがログインできない", "決済が通らない", "本番 API が 500 を返す", "HTTP 503 が多発", "本番でデータが消えた",
    "個人情報が他人に表示される", "ログインするとエラー画面になる", "Users can't log in", "Production is down",
    "Checkout is broken for everyone", "500 errors on all requests", "請求金額が二重に計上されている", "全員ログアウトされる",
    "本番でタイムアウトが多発", "サイトが真っ白になる", "注文が確定されない", "決済完了画面に進まない", "顧客のログインに失敗する",
    "サインインに失敗します", "アプリがクラッシュする", "Customers cannot check out", "多数の顧客から問い合わせが来ている",
]
BUG_LEFT_TO_MODEL = [   # a rule cannot be confident (no scope, no "now"): documented, the model decides
    "データ不整合で売上が合わない", "セキュリティ脆弱性: 認証回避が可能", "画面が真っ白になる", "ログインのエラーメッセージを見直したい",
]


class TestAdoCriticalRule(unittest.TestCase):
    def test_table_sizes(self):
        self.assertEqual((len(BUG_NOT_P1), len(BUG_P1), len(BUG_LEFT_TO_MODEL)), (20, 22, 4))

    def test_improvement_wording_is_not_p1(self):
        for t in BUG_NOT_P1:
            self.assertNotEqual(bug_rule(title=t), "yes", t)

    def test_typical_outage_wording_is_p1(self):
        for t in BUG_P1:
            self.assertEqual(bug_rule(title=t), "yes", t)

    def test_left_to_the_model(self):
        for t in BUG_LEFT_TO_MODEL:
            self.assertEqual(bug_rule(title=t), "no", t)

    def test_outage_in_description_or_repro(self):
        self.assertEqual(bug_rule(title="障害", desc="本番で全ユーザーがログインできない"), "yes")
        self.assertEqual(bug_rule(title="障害", repro="再現手順: 本番で決済が通らない"), "yes")

    def test_the_veto_is_sentence_scoped_for_wording_and_field_scoped_for_environment(self):
        self.assertEqual(bug_rule(title="強制終了ボタンを追加。本番で全ユーザーがログインできない"), "yes")
        self.assertEqual(bug_rule(title="本番で全ユーザーがログインできない。テスト環境でも再現する"), "mixed")
        self.assertEqual(bug_rule(title="本番で決済できない", desc="テスト環境では再現しない"), "yes")

    def test_severity_one_critical_is_a_hit_and_other_severities_are_not(self):
        self.assertEqual(bug_rule(title="画面の誤字", severity="1 - Critical"), "yes")
        self.assertEqual(bug_rule(title="画面の誤字", severity="2 - High"), "no")
        self.assertEqual(bug_rule(title="画面の誤字", severity="3 - Medium"), "no")

    def test_severity_is_carried_to_the_event_and_the_judge(self):
        ev = workitem(title="x", severity="1 - Critical")
        self.assertEqual(ev["severity"], "1 - Critical")
        self.assertEqual(events.state_of(ev)["severity"], "1 - Critical")
        from kimeru import pull
        self.assertIn("Microsoft.VSTS.Common.Severity", pull.ADO_FIELDS)


class TestWorkItemType(unittest.TestCase):
    def first_edge(self, typ):
        ev = workitem(title="本番で全ユーザーがログインできない", typ=typ)
        r = graph.run(ADO, ev, ReplayBackend({"ready": {"noul": 0.1}, "priority": {"score": 1.0, "confidence": 0.9}}))
        return [s["node"] for s in r["path"]], r["node"]

    def test_bug_like_types_and_untyped_items_are_eligible(self):
        for typ in ("Bug", "Issue", "Incident", "Defect", "bug", None):
            self.assertEqual(self.first_edge(typ)[1], "set_p1_critical", typ)

    def test_other_types_are_not(self):
        for typ in ("Epic", "Feature", "Task", "Test Case", "User Story", "Product Backlog Item"):
            self.assertNotEqual(self.first_edge(typ)[1], "set_p1_critical", typ)
            self.assertEqual(graph.match_eval(CRIT_BUG, workitem(title="本番で全ユーザーがログインできない", typ=typ))[0], "no", typ)


# --- readiness: has_ac / has_repro and HTML -----------------------------------------------------------------------
class TestHtmlToText(unittest.TestCase):
    CASES = [
        ("<div>ログイン画面で</div><div>エラーになる</div>", "ログイン画面で\nエラーになる"),
        ("<ol><li>開く</li><li>押す</li></ol>", "1. 開く\n2. 押す"),
        ("<ul><li>開く</li><li>押す</li></ul>", "開く\n押す"),
        ("<p>a</p><p>b</p>", "a\nb"),
        ("a<br>b<br/>c", "a\nb\nc"),
        ("<!--[if gte mso 9]><xml>x</xml><![endif]--><style>p.MsoNormal{margin:0}</style><p class=MsoNormal>本文</p>", "本文"),
        ("<script>alert(1)</script>本文", "本文"),
        ("<div>&nbsp;</div>", ""),
        ("<div>​</div>", ""),
        ("a&amp;b &lt;c&gt;", "a&b <c>"),
        ("<table><tr><td>項目</td><td>値</td></tr></table>", "項目 値"),
        ("x​y", "xy"),
    ]

    def test_cases(self):
        self.assertEqual(len(self.CASES), 12)
        for src, want in self.CASES:
            self.assertEqual(events._strip_html(src), want, src)


EMPTY = [   # an unfilled template is empty: the item goes on to "ready", and the model (ready=0.1) asks for information
    ("AC empty div", dict(ac="<div></div>")),
    ("AC br", dict(ac="<div><br></div>")),
    ("AC nbsp", dict(ac="<div>&nbsp;</div>")),
    ("AC zero width", dict(ac="<div>​</div>")),
    ("AC heading only", dict(ac="<div><b>受け入れ条件:</b></div><ul><li></li></ul>")),
    ("AC given/when/then", dict(ac="<div>Given:</div><div>When:</div><div>Then:</div>")),
    ("AC acceptance criteria EN", dict(ac="<div>Acceptance criteria:</div>")),
    ("Repro JA template", dict(repro="<div><b>再現手順:</b></div><div><br></div><div><b>期待結果:</b></div><div><br></div><div><b>実際の結果:</b></div>")),
    ("Repro EN template", dict(repro="<div>Steps to reproduce:</div><div>Expected result:</div><div>Actual result:</div>")),
    ("Repro EN short", dict(repro="<div>Steps:</div><div>Expected:</div><div>Actual:</div>")),
    ("Repro empty numbers", dict(repro="<div>再現手順:</div><ol><li></li><li></li></ol>")),
    ("Desc 再現性は不明", dict(desc="<div>たまに画面が固まる。再現性は不明。</div>")),
    ("Desc 期待しています", dict(desc="<div>新しいダッシュボードに期待しています</div>")),
    ("Desc 再現する", dict(desc="<div>たまに再現する</div>")),
    ("Repro screenshot only", dict(repro='<div><img src="x.png"></div>')),
    ("Prerequisite heading only", dict(ac="<div>前提条件:</div><div>手順:</div>")),
]
FILLED = [   # real content: ready without asking the model
    ("AC text", dict(ac="<div>ログインできること</div>")),
    ("AC heading + content", dict(ac="<div><b>受け入れ条件:</b></div><div>ログインできる</div>")),
    ("AC Given/When/Then filled", dict(ac="<div>Given: ログイン済み</div><div>When: 押す</div><div>Then: 表示される</div>")),
    ("Repro numbered steps", dict(repro="<ol><li>アプリを開く</li><li>ボタンを押す</li></ol>")),
    ("Repro heading + numbered", dict(repro="<div>再現手順:</div><div>1. 開く</div><div>2. 押す</div>")),
    ("Repro inline numbered", dict(desc="1. 開く 2. 押す 3. 落ちる")),
    ("Expected/actual pair JA", dict(desc="<div>期待結果: 保存される</div><div>実際の結果: エラーになる</div>")),
    ("Expected/actual pair EN", dict(desc="<div>Expected: it saves</div><div>Actual: error 500</div>")),
    ("Expected/actual under headings", dict(repro="<div>期待結果:</div><div>保存される</div><div>実際の結果:</div><div>エラー</div>")),
    ("Steps to reproduce with text", dict(desc="Steps to reproduce: open the app and tap save")),
    ("Repro heading with content", dict(desc="再現手順: 設定画面で保存を押す")),
]


class TestReadiness(unittest.TestCase):
    def path(self, **kw):
        ev = workitem(title="対応する", **kw)
        r = graph.run(ADO, ev, ReplayBackend({"ready": {"noul": 0.1}, "priority": {"score": 1.0, "confidence": 0.9}}))
        return [s["node"] for s in r["path"]], r["node"]

    def test_table_sizes(self):
        self.assertEqual((len(EMPTY), len(FILLED)), (16, 11))

    def test_unfilled_templates_and_loose_words_are_asked_to_the_model(self):
        for label, kw in EMPTY:
            nodes, last = self.path(**kw)
            self.assertIn("ready", nodes, label)
            self.assertEqual(last, "request_info", label)

    def test_real_content_is_ready_without_the_model(self):
        for label, kw in FILLED:
            nodes, _ = self.path(**kw)
            self.assertNotIn("ready", nodes, label)

    def test_template_headings_are_removed_but_content_is_kept(self):
        ev = workitem(title="t", repro="<div>再現手順:</div><div>1. 開く</div><div>期待結果:</div><div></div>")
        self.assertEqual(ev["repro_steps"], "再現手順: 1. 開く")
        self.assertEqual(workitem(title="t", ac="<div>Given:</div>")["acceptance_criteria"], "")


class TestLongFieldsAreCut(unittest.TestCase):
    def test_judge_state_cuts_long_ado_fields_but_the_event_keeps_them(self):
        ev = workitem(title="t", desc="<div>" + "ERROR line<br>" * 400 + "</div>", ac="<div>x</div>")
        self.assertGreater(len(ev["description"]), 1200)
        self.assertLessEqual(len(events.state_of(ev)["description"]), 1200)
        self.assertEqual(events.state_of(ev)["acceptance_criteria"], "x")


if __name__ == "__main__":
    unittest.main()
