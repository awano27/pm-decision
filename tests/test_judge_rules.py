try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from kimeru import cli, events, graph, plan
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



class TestRecoveryInAnotherSentence(unittest.TestCase):
    """An outage report followed by a recovery note in another sentence goes to a person, not to paging."""
    NODE = json.loads((ROOT / "graphs" / "teams_chat.json").read_text(encoding="utf-8"))["nodes"]["critical_incident"]

    def test_recovery_anywhere_in_the_message_is_mixed(self):
        for text in ("本番で決済が通りません。復旧しました。", "本番で決済が通りません。先ほど復旧しました",
                     "Production is down. It is resolved now."):
            self.assertEqual(graph.match_eval(self.NODE, {"text": text})[0], "mixed", text)

    def test_without_a_recovery_it_stays_an_incident(self):
        for text in ("本番で決済が通りません。誰か見られますか？", "Production is down. Let me know if you need anything."):
            self.assertEqual(graph.match_eval(self.NODE, {"text": text})[0], "yes", text)

if __name__ == "__main__":
    unittest.main()


# --- R38: validate warns about how a question is written (never fails, never changes a graph) -------------------
NOUL_WARNING = ("noul has and/or in it, so one question asks two things; ask one condition per question")
CHOICE_WARNING = ("choice has no 'none' option, so a message that fits no option is forced into one")


def score_warning(i):
    return (f"score level {i} is a degree word, not a situation; describe the case the level stands for")


def lint_question(question):
    return graph.lint({"nodes": {"q": {"kind": "judge", "question": question, "routes": {}}}})


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as d, redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["--out", str(Path(d) / "out"), *argv])
    return rc, out.getvalue(), err.getvalue()


BUNDLED_WARNINGS = [   # the four questions the bundled graphs ask in a compound or 'none'-less way, fixed as they are now
    ("workitem-intake", "ready: " + NOUL_WARNING),
    ("minutes-followup", "item_type: " + CHOICE_WARNING),
    ("minutes-followup", "has_due: " + NOUL_WARNING),
    ("alert-triage", "noise_check: " + NOUL_WARNING),
]
COMPOUND_GRAPH = {"name": "compound-check", "event": "teams.chat", "start": "a", "nodes": {
    "a": {"kind": "judge", "question": {"type": "noul", "instructions": "Does the message give a due date or a deadline?"},
          "routes": {"yes": "b", "no": "b", "unsure": "b"}},
    "b": {"kind": "decide", "actions": []}}}


class TestQuestionWritingWarnings(unittest.TestCase):
    """validate lists the questions written as two conditions in one, a choice without a none, and score levels that
    are only degree words. A warning never stops validate and never changes a graph."""

    def test_bundled_graphs_keep_exactly_the_same_warnings(self):
        found = []
        for path in sorted((ROOT / "graphs").glob("*.json")):
            g = json.loads(path.read_text(encoding="utf-8"))
            found += [(g["name"], text) for text in graph.lint(g)]
        self.assertEqual(found, BUNDLED_WARNINGS)

    def test_and_or_or_mark_a_noul_with_two_conditions_in_english_and_japanese(self):
        for text in ("Does the item state a due date or a deadline?", "Is the owner AND the due date named?",
                     "期限または担当者が書かれていますか？", "期限かつ担当者が書かれていますか？"):
            self.assertEqual(lint_question({"type": "noul", "instructions": text}), ["q: " + NOUL_WARNING], text)

    def test_words_that_only_contain_or_or_for_are_not_flagged(self):
        for text in ("Does the message ask for a decision on the order?",
                     "Does the alert warn about a problem expected to happen in the future, rather than a problem happening now?"):
            self.assertEqual(lint_question({"type": "noul", "instructions": text}), [], text)

    def test_a_choice_without_a_none_option_is_flagged_and_a_none_option_clears_it(self):
        self.assertEqual(lint_question({"type": "choice", "instructions": "x",
                                        "criteria": {"decision": "d", "info": "i"}}), ["q: " + CHOICE_WARNING])
        self.assertEqual(lint_question({"type": "choice", "instructions": "x",
                                        "criteria": {"decision": "d", "none": "small talk"}}), [])
        self.assertEqual(lint_question({"type": "choice", "instructions": "x",
                                        "criteria": {"decision": "d", "info": "i", "x": "該当なし"}}), [])
        self.assertEqual(lint_question({"type": "choice", "instructions": "x",
                                        "criteria": {"decision": "d", "info": "i", "x": "none of these"}}), [])

    def test_a_score_level_that_is_only_a_degree_word_is_flagged(self):
        self.assertEqual(lint_question({"type": "score", "instructions": "x", "criteria": ["Low", "Medium", "High"]}),
                         ["q: " + score_warning(i) for i in range(3)])
        self.assertEqual(lint_question({"type": "score", "instructions": "x", "criteria": ["very high", "中程度", "重大"]}),
                         ["q: " + score_warning(i) for i in range(3)])

    def test_score_levels_that_describe_a_situation_are_not_flagged(self):
        self.assertEqual(lint_question({"type": "score", "instructions": "x", "criteria": [
            "No deadline is stated or implied", "Within this week", "By end of today", "Blocking someone right now"]}), [])
        self.assertEqual(lint_question({"type": "score", "instructions": "x",
                                        "criteria": ["軽微な表示の崩れ", "重大な障害が起きている"]}), [])

    def test_every_warning_is_ascii_so_the_console_can_print_it(self):
        texts = [text for _, text in BUNDLED_WARNINGS]
        texts += lint_question({"type": "score", "instructions": "x", "criteria": ["中程度", "重大"]})
        texts += lint_question({"type": "choice", "instructions": "x", "criteria": {"決定": "d", "情報": "i"}})
        for text in texts:
            self.assertTrue(text.isascii(), text)

    def test_lint_leaves_every_bundled_graph_as_it_was(self):
        for path in sorted((ROOT / "graphs").glob("*.json")):
            g = json.loads(path.read_text(encoding="utf-8"))
            before = copy.deepcopy(g)
            graph.lint(g)
            self.assertEqual(g, before, path.name)

    def test_loading_a_graph_prints_nothing_about_the_warnings(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            graph.load_dir(ROOT / "graphs", PBS)
        self.assertEqual(out.getvalue(), "")

    def test_validate_in_the_repo_prints_each_warning_and_still_exits_zero(self):
        rc, out, err = run_cli("validate")
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertIn("ok  teams-chat-triage (teams.chat, ", out)
        for name, text in BUNDLED_WARNINGS:
            self.assertIn(f"warn  {name}: {text}", lines)

    def test_a_graph_with_a_warning_still_validates_and_validate_exits_zero(self):
        graph.validate(COMPOUND_GRAPH)   # a warning is not a GraphError
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "compound.json").write_text(json.dumps(COMPOUND_GRAPH, ensure_ascii=False), encoding="utf-8")
            rc, out, err = run_cli("--graphs", d, "--playbooks", str(ROOT / "playbooks"), "validate")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"warn  compound-check: a: {NOUL_WARNING}", out.splitlines())
        self.assertNotIn("Traceback", err)


# --- T11: the fields a question names, and the checks of the playbooks ------------------------------------------
BUNDLED_PLAYBOOK_WARNINGS = [   # the check questions of the bundled playbooks that ask two things in one, fixed as they are now
    "incident/comms: " + NOUL_WARNING,
    "requirements_review/constraints: " + NOUL_WARNING,
    "requirements_review/review: " + NOUL_WARNING,
    "schedule_change/dependencies: " + NOUL_WARNING,
    "scope_change/tradeoff: " + NOUL_WARNING,
]


def field_warning(name, kind):
    return f"question reads `{name}`, which the {kind} event state does not have"


def output_warning(name):
    return f"question reads `{name}`, which kimeru writes (its own conclusion), not the event"


def lint_event_question(instructions, kind="teams.chat"):
    g = {"event": kind, "nodes": {"q": {"kind": "judge", "routes": {},
                                        "question": {"type": "score", "instructions": instructions, "criteria": ["a", "b"]}}}}
    return graph.lint(g)


class TestFieldAndPlaybookWarnings(unittest.TestCase):
    def test_bundled_playbooks_have_exactly_these_warnings(self):
        found = []
        for pid in sorted(PBS):
            found += graph.lint_playbook(PBS[pid])
        self.assertEqual(found, BUNDLED_PLAYBOOK_WARNINGS)

    def test_bundled_graphs_name_only_fields_their_event_has_and_no_output_field(self):
        for path in sorted((ROOT / "graphs").glob("*.json")):
            g = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([w for w in graph.lint(g) if "question reads" in w], [], path.name)

    def test_a_field_the_event_does_not_have_is_flagged(self):
        self.assertEqual(lint_event_question("Is `body` urgent?"), ["q: " + field_warning("body", "teams.chat")])
        self.assertEqual(lint_event_question("Is `text` urgent?"), [])
        self.assertEqual(lint_event_question("Is `items[0].text` urgent?", "meeting.item"),
                         ["q: " + field_warning("items", "meeting.item")])
        self.assertEqual(lint_event_question("Does `earlier[0]` change `text` today?"), [])
        self.assertEqual(lint_event_question("Is `item` urgent?", "no.such.kind"), [])

    def test_a_field_of_another_event_kind_is_flagged(self):
        self.assertEqual(lint_event_question("Is `rule` urgent?"), ["q: " + field_warning("rule", "teams.chat")])
        self.assertEqual(lint_event_question("Is `rule` urgent?", "monitor.alert"), [])

    def test_a_field_kimeru_writes_is_flagged_as_its_own_conclusion(self):
        for name in ("advice", "decision", "outcome", "plan"):
            self.assertEqual(lint_event_question(f"Is `{name}` right?"), ["q: " + output_warning(name)], name)

    def test_a_playbook_check_gets_the_same_three_checks(self):
        pb = {"id": "p", "steps": [
            {"id": "a", "check": "Is there a deadline or a customer?"},
            {"id": "b", "check": "Does `advice` say it is late?"},
            {"id": "c", "check": "Does `nothing` appear?"},
            {"id": "d", "check": "Is `text` late?"},
            {"id": "e"}]}
        self.assertEqual(graph.lint_playbook(pb), [
            "p/a: " + NOUL_WARNING, "p/b: " + output_warning("advice"),
            "p/c: " + field_warning("nothing", "any")])

    def test_validate_prints_the_playbook_warnings_and_still_exits_zero(self):
        rc, out, err = run_cli("validate")
        self.assertEqual(rc, 0, err)
        for text in BUNDLED_PLAYBOOK_WARNINGS:
            self.assertIn(f"warn  playbook {text}", out.splitlines())

    def test_the_new_warnings_are_ascii(self):
        for text in BUNDLED_PLAYBOOK_WARNINGS + [field_warning("x", "k"), output_warning("x")]:
            self.assertTrue(text.isascii(), text)
