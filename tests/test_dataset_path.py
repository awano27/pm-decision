"""From labeled decisions to a traceable local evaluation: an anonymization *candidate* (never called anonymous), a review that
is void once the bytes change, a deterministic tune/eval split with duplicate / leakage / unscorable checks, provenance that a
result must match, six outcome categories, and NO_INPUT / NOT_RUN instead of 0%. Fictional data, the stub judge, no network."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import contextlib
import hashlib
import importlib.util
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import anonymize, cli, dataset
from kimeru.backends import StubBackend

ROOT = Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DSE = load("kimeru_eval_dataset_eval_t", "eval/dataset_eval.py")
RUN_EVAL = load("kimeru_eval_run_eval_t", "eval/run_eval.py")
E2E = load("kimeru_eval_e2e_t", "eval/e2e.py")

# Every value here is invented: reserved .test domains, 0000 phone numbers, a fake token.
SECRET = "ghp_FAKEfakeFAKEfake0123456789abcd"
API_KEY = "FAKEkey0000notreal"
PII_TEXT = ("架空 太郎です。@Kakuu さん、#98765 と AB#4321 の件。連絡は taro.kakuu@example.test か 03-0000-1234、"
            "詳細は https://dev.azure.com/fictional-org/proj/_workitems/edit/123456 。api_key=" + API_KEY + " token: " + SECRET +
            " サーバー 10.0.0.42 と 3f2504e0-4f89-11d3-9a0c-0305e82c3301 を確認。リリース日を来週にずらしてよいか判断お願いします。至急です")
ORIGINALS = ["架空 太郎", "Kakuu", "#98765", "AB#4321", "taro.kakuu@example.test", "03-0000-1234", "fictional-org",
             API_KEY, SECRET, "10.0.0.42", "3f2504e0-4f89-11d3-9a0c-0305e82c3301"]


def write_jsonl(path, rows):
    Path(path).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def pii_rows():
    return [
        {"id": "user-0000aaaa", "event": {"kind": "teams.chat", "id": "19:fake-thread@thread.v2", "author": "架空 太郎",
                                          "mentions_me": True, "text": PII_TEXT},
         "expect": {"intent": "decision"}},
        {"id": "user-0000bbbb", "event": {"kind": "teams.chat", "id": "t-2", "author": "Hanako Kakuu", "mentions_me": True,
                                          "text": "顧客の架空商事の田中さんから、ログイン画面の改修はどこまで進んでいるか質問です。連絡先 taro.kakuu@example.test"},
         "expect": {"intent": "status"}},
    ]


def shipped_rows(n=30):
    """The shipped fictional fixtures, with ids in the shape `kimeru review` writes."""
    rows = [json.loads(l) for l in (ROOT / "eval" / "fixtures.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [r for r in rows if r.get("expect")][:n]
    return [{**r, "id": "user-" + hashlib.sha1(r["id"].encode("utf-8")).hexdigest()[:8]} for r in rows]


@contextlib.contextmanager
def no_network():
    def refuse(*a, **k):
        raise AssertionError("no network in these tests")
    with mock.patch("urllib.request.urlopen", side_effect=refuse), \
            mock.patch("http.client.HTTPConnection.connect", side_effect=refuse), \
            mock.patch("socket.create_connection", side_effect=refuse):
        yield


class Temp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.graphs, self.pbs = dataset.load_graphs()

    def tearDown(self):
        self._tmp.cleanup()

    def candidate(self, rows, name="cand", **kw):
        src = self.tmp / f"{name}-src.jsonl"
        write_jsonl(src, rows)
        m = anonymize.make_candidate(src, self.tmp / name, graphs=self.graphs, playbooks=self.pbs, **kw)
        return src, self.tmp / name, m


class TestAnonymizationCandidate(Temp):
    def test_identifiers_are_replaced_the_source_is_untouched_and_every_replacement_is_listed(self):
        src = self.tmp / "fixtures_user.jsonl"
        write_jsonl(src, pii_rows())
        before_bytes, before_stat = src.read_bytes(), src.stat()
        before_hash = hashlib.sha256(before_bytes).hexdigest()
        m = anonymize.make_candidate(src, self.tmp / "cand", graphs=self.graphs, playbooks=self.pbs)
        self.assertEqual(src.read_bytes(), before_bytes)
        self.assertEqual(hashlib.sha256(src.read_bytes()).hexdigest(), before_hash)
        self.assertEqual(src.stat().st_mtime_ns, before_stat.st_mtime_ns)
        self.assertEqual(m["source"]["sha256"], before_hash)

        cand = (self.tmp / "cand" / anonymize.CANDIDATE).read_text(encoding="utf-8")
        for original in ORIGINALS + ["user-0000aaaa", "19:fake-thread"]:
            self.assertNotIn(original, cand, original)
        rows = [json.loads(l) for l in cand.splitlines()]
        self.assertEqual([r["expect"] for r in rows], [r["expect"] for r in pii_rows()])   # labels are not touched
        self.assertTrue(all(r["id"].startswith("anon-") for r in rows))
        self.assertEqual(rows[0]["event"]["kind"], "teams.chat")
        self.assertIs(rows[0]["event"]["mentions_me"], True)
        text = rows[0]["event"]["text"]
        for ph in ("<PERSON_", "<EMAIL_", "<PHONE_", "<URL_", "<WORK_ITEM_", "<SECRET_", "<IP_", "<GUID_"):
            self.assertIn(ph, text, ph)
        self.assertIn("@<PERSON_", text)   # a mention of a known author keeps its "@" and becomes that person
        # the same e-mail address in two rows gets the same placeholder
        emails = [re.findall(r"<EMAIL_\d+>", r["event"]["text"]) for r in rows]
        self.assertEqual(emails[0], emails[1])

        review = (self.tmp / "cand" / anonymize.REVIEW).read_text(encoding="utf-8")
        for original in ORIGINALS:
            self.assertIn(original, review, original)
        self.assertIn("共有禁止", review)
        self.assertIn("固有名詞", review)
        self.assertIn("秘密", review)

    def test_status_is_a_candidate_that_needs_a_human_and_never_claims_anonymity(self):
        _, cand, m = self.candidate(pii_rows())
        self.assertEqual(m["status"], "candidate")
        self.assertIs(m["human_review_required"], True)
        self.assertIsNone(m["human_review"])
        self.assertIn("匿名化済みデータではありません", m["warning"])
        self.assertIn("残る可能性", m["warning"])
        st, _ = anonymize.status(cand)
        self.assertEqual(st, anonymize.STATUS_REQUIRED)
        blob = json.dumps(m, ensure_ascii=False).lower()
        self.assertNotIn('"anonymized"', blob)
        self.assertNotRegex(blob, r'"status": "anonymi[sz]ed"')

    def test_free_text_names_that_no_pattern_knows_stay_and_are_flagged_for_the_person(self):
        _, cand, m = self.candidate(pii_rows())
        text = (cand / anonymize.CANDIDATE).read_text(encoding="utf-8")
        self.assertIn("田中さん", text)          # not in any author field: not replaced, and not claimed to be
        review = (cand / anonymize.REVIEW).read_text(encoding="utf-8")
        self.assertIn('honorific: "田中さん"', review)
        self.assertGreater(m["residual_risks"]["spots"], 0)
        self.assertGreater(m["residual_risks"]["free_text_fields"], 0)

    def test_a_review_counts_only_for_the_bytes_it_was_given_for(self):
        _, cand, _ = self.candidate(pii_rows())
        with self.assertRaises(ValueError):
            anonymize.mark_reviewed(cand, confirmed=False)
        self.assertEqual(anonymize.status(cand)[0], anonymize.STATUS_REQUIRED)
        anonymize.mark_reviewed(cand, confirmed=True, by="KT")
        self.assertEqual(anonymize.status(cand)[0], anonymize.STATUS_REVIEWED)
        p = cand / anonymize.CANDIDATE
        p.write_text(p.read_text(encoding="utf-8").replace("ログイン", "ログオン"), encoding="utf-8")
        st, reasons = anonymize.status(cand)
        self.assertEqual(st, anonymize.STATUS_VOID)
        self.assertTrue(any("human_review_required" in r for r in reasons))
        anonymize.mark_reviewed(cand, confirmed=True)   # reviewed again: valid for the new bytes
        self.assertEqual(anonymize.status(cand)[0], anonymize.STATUS_REVIEWED)
        (cand / anonymize.REVIEW).write_text("changed", encoding="utf-8")
        self.assertEqual(anonymize.status(cand)[0], anonymize.STATUS_VOID)

    def test_the_cli_mark_needs_an_explicit_flag(self):
        _, cand, _ = self.candidate(pii_rows())
        out = []
        self.assertEqual(dataset.dispatch(["mark-reviewed", str(cand)], print_fn=out.append), 2)
        self.assertEqual(anonymize.status(cand)[0], anonymize.STATUS_REQUIRED)
        self.assertEqual(dataset.dispatch(["mark-reviewed", str(cand), "--i-reviewed"], print_fn=out.append), 0)
        self.assertEqual(anonymize.status(cand)[0], anonymize.STATUS_REVIEWED)
        self.assertIn("保証するものではありません", "\n".join(out))

    def test_the_same_input_gives_the_same_candidate_and_output_never_overwrites_or_lands_in_the_repository(self):
        src, cand, _ = self.candidate(pii_rows())
        anonymize.make_candidate(src, self.tmp / "again", graphs=self.graphs, playbooks=self.pbs)
        self.assertEqual((cand / anonymize.CANDIDATE).read_bytes(), (self.tmp / "again" / anonymize.CANDIDATE).read_bytes())
        with self.assertRaises(ValueError):
            anonymize.make_candidate(src, cand)
        inside = ROOT / "tests" / "_no_dataset_here"
        with self.assertRaises(ValueError):
            anonymize.make_candidate(src, inside)
        self.assertFalse(inside.exists())

    def test_a_replacement_that_changes_a_route_is_reported(self):
        rows = [{"id": "user-0000cccc", "event": {"kind": "meeting.item", "id": "m1", "meeting": "週次定例",
                                                  "item": "@架空 10/3までにAPI仕様書のレビュー"},
                 "expect": {"item_type": "action"}}]
        _, cand, m = self.candidate(rows)
        self.assertIn("@<MENTION_", (cand / anonymize.CANDIDATE).read_text(encoding="utf-8"))
        self.assertEqual(m["residual_risks"]["route_changes"], 0)   # the "@" an owner check reads is kept
        with mock.patch.object(anonymize, "_LATE", [("MENTION", re.compile(r"@\S+"))] + anonymize._LATE[1:]):
            _, _, m2 = self.candidate(rows, name="cand2")
        self.assertEqual(m2["residual_risks"]["route_changes"], 1)


class TestDatasetChecks(Temp):
    def split_rows(self, rows, name="ds", tune_percent=50):
        src = self.tmp / f"{name}.jsonl"
        write_jsonl(src, rows)
        return dataset.split(src, self.tmp / name, tune_percent, self.graphs, self.pbs), self.tmp / name

    def kinds(self, m):
        return {(i["kind"], i["id"]) for i in m["issues"]}

    def test_missing_fields_unscorable_rows_and_duplicates_are_excluded_with_their_reason(self):
        ok = shipped_rows(4)
        rows = ok + [
            {"event": {"kind": "teams.chat", "text": "x"}, "expect": {"intent": "fyi"}},                       # no id
            {"id": "user-noevent", "expect": {"intent": "fyi"}},
            {"id": "user-nokind", "event": {"text": "x"}, "expect": {"intent": "fyi"}},
            {"id": "user-noexpect", "event": {"kind": "teams.chat", "text": "x"}},
            {"id": "user-notext", "event": {"kind": "teams.chat", "author": "A"}, "expect": {"intent": "fyi"}},
            {"id": "user-nograph", "event": {"kind": "fax.page", "text": "x"}, "expect": {"intent": "fyi"}},
            {"id": "user-oldnode", "event": {"kind": "teams.chat", "text": "古いラベル"}, "expect": {"gone_node": True}},
            {"id": "user-badlabel", "event": {"kind": "teams.chat", "text": "選択肢にない"}, "expect": {"intent": "nonsense"}},
            ok[0],                                                                                             # exact repeat
            {**ok[1], "event": {**ok[1]["event"], "text": "同じ id の別の行"}},                                # id conflict
            {**ok[2], "id": "user-relabel", "expect": {"intent": "fyi" if ok[2]["expect"].get("intent") != "fyi" else "status"}},
        ]
        m, _ = self.split_rows(rows)
        k = self.kinds(m)
        for want in [("missing_field", None), ("missing_field", "user-noevent"), ("missing_field", "user-nokind"),
                     ("missing_field", "user-noexpect"), ("missing_field", "user-notext"), ("unscorable", "user-nograph"),
                     ("unscorable", "user-oldnode"), ("unscorable", "user-badlabel"), ("duplicate", ok[0]["id"]),
                     ("id_conflict", ok[1]["id"]), ("label_conflict", "user-relabel"), ("label_conflict", ok[2]["id"])]:
            self.assertIn(want, k, want)
        kept = m["files"]["tune"]["rows"] + m["files"]["eval"]["rows"]
        self.assertEqual(kept, 2)   # ok[0] (once) and ok[3]
        issues = json.dumps(m["issues"], ensure_ascii=False)
        for content in ("同じ id の別の行", "古いラベル", "選択肢にない", ok[0]["event"].get("text", "-")):
            self.assertNotIn(content, issues)   # issues name rows and fields, never their content

    def test_the_split_is_deterministic_and_near_duplicates_stay_on_one_side(self):
        base = shipped_rows(30)
        near = [{**r, "id": r["id"] + "-copy", "event": {**r["event"], "id": "other",
                                                          **({"text": r["event"]["text"] + "。"} if "text" in r["event"] else {})}}
                for r in base[:10]]
        m1, d1 = self.split_rows(base + near, "a")
        m2, d2 = self.split_rows(base + near, "b")
        self.assertEqual(m1["data_version"], m2["data_version"])
        self.assertEqual((d1 / dataset.EVAL).read_bytes(), (d2 / dataset.EVAL).read_bytes())
        self.assertGreater(m1["near_duplicate_rows"], 0)
        t = {r["id"] for _, r in dataset.read_rows(d1 / dataset.TUNE)[0]}
        e = {r["id"] for _, r in dataset.read_rows(d1 / dataset.EVAL)[0]}
        for r in base[:10]:
            self.assertEqual(r["id"] in t, r["id"] + "-copy" in t)
        self.assertTrue(t and e)
        self.assertEqual(dataset.check_leakage(dataset.read_rows(d1 / dataset.TUNE)[0], dataset.read_rows(d1 / dataset.EVAL)[0]), [])
        m3, _ = self.split_rows(base + near, "c", tune_percent=30)
        self.assertNotEqual(m1["data_version"], m3["data_version"])

    def test_leakage_between_files_split_some_other_way_is_found(self):
        base = shipped_rows(6)
        tune = self.tmp / "tune.jsonl"
        ev = self.tmp / "eval.jsonl"
        write_jsonl(tune, base[:3])
        r = base[0]
        near = {**r, "id": "user-near", "event": {**r["event"], "id": "x9", **({"text": r["event"]["text"] + "！"} if "text" in r["event"] else {})}}
        write_jsonl(ev, [base[1], {**base[2], "id": "user-sameevent"}, near, base[5]])
        out = []
        self.assertEqual(dataset.dispatch(["check", str(tune), str(ev)], print_fn=out.append), 1)
        leaks = dataset.check_leakage(dataset.read_rows(tune)[0], dataset.read_rows(ev)[0])
        self.assertEqual(sorted(i["id"] for i in leaks), sorted([base[1]["id"], "user-sameevent", "user-near"]))

    def test_a_result_whose_claims_do_not_match_is_a_provenance_mismatch_not_a_score(self):
        m, d = self.split_rows(shipped_rows(20))
        fps = dataset.graph_fingerprints(self.graphs, self.pbs)
        rows = [{"id": r["id"], "category": "correct"} for r in dataset.eval_rows(d)]
        ident = {"judge": "Kev", "model": "kev-test", "model_key": hashlib.sha256(b"kev-test").hexdigest()}
        scoring = {"method": "m", "version": "1", "implementation_sha256": "x", "categories": list(dataset.CATEGORIES)}
        good = dataset.build_result(d, rows, ident, scoring, fps)
        self.assertEqual(dataset.check_result(good, d, fps, scoring, ident), [])

        def bad(mutate):
            r = json.loads(json.dumps(good))
            mutate(r)
            return dataset.check_result(r, d, fps, scoring, ident)

        self.assertTrue(bad(lambda r: r["provenance"].__setitem__("data_version", "v1:other")))
        self.assertTrue(bad(lambda r: r["provenance"]["graphs"].__setitem__("teams.chat", "v1:changed")))
        self.assertTrue(bad(lambda r: r["provenance"]["model"].__setitem__("model_key", "0" * 64)))
        self.assertTrue(bad(lambda r: r["provenance"]["scoring"].__setitem__("version", "2")))
        self.assertTrue(bad(lambda r: r["summary"].__setitem__("correct", 999)))
        self.assertTrue(bad(lambda r: r["rows"].pop()))
        self.assertTrue(dataset.check_result(good, d, {**fps, "teams.chat": "v1:now-different"}, scoring, ident))
        self.assertTrue(dataset.check_result(good, d, fps, scoring, {**ident, "model_key": "f" * 64}))
        jev = {"judge": "Jev", "model": None, "model_key": None}
        leaked = dataset.build_result(d, rows, jev, scoring, fps)
        leaked["summary"] = dataset.tally(rows)
        self.assertIn("Jev の結果に件数の集計が含まれています（出してはいけません）", dataset.check_result(leaked, d, fps, scoring))
        (d / dataset.EVAL).write_text((d / dataset.EVAL).read_text(encoding="utf-8") + "\n", encoding="utf-8")
        problems = dataset.check_result(good, d, fps, scoring, ident)
        self.assertTrue(any("変わりました" in p for p in problems))
        code, lines = dataset.status(d, self.write(good), fps, scoring)
        self.assertEqual(code, dataset.MISMATCH)
        self.assertFalse(any(label in "\n".join(lines) for label in dataset.LABELS_JA.values()))

    def write(self, result):
        p = self.tmp / "result.json"
        p.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return p


class FakeJev(StubBackend):
    NAME = "Jev"


class FakeKev(StubBackend):
    NAME = "Kev"
    model = "kev-fictional-test"


class TestLocalEvaluationPath(Temp):
    def test_existing_output_is_preserved_without_calling_the_judge(self):
        d = self.build()
        for path in (d / dataset.EVAL, self.tmp / "cand-src.jsonl", self.tmp / "prior-result.json"):
            if not path.exists():
                path.write_text("prior result", encoding="utf-8")
            before = path.read_bytes()
            with mock.patch.object(DSE, "score_rows", return_value=[]) as score:
                code, result = DSE.run(d, StubBackend(), out=path, print_fn=lambda *_: None)
            self.assertNotEqual(code, 0)
            self.assertIsNone(result)
            score.assert_not_called()
            self.assertEqual(path.read_bytes(), before)

    def test_missing_manifest_fields_and_changed_counts_fail_closed(self):
        d = self.build()
        path = d / dataset.MANIFEST
        original = json.loads(path.read_text(encoding="utf-8"))
        for change in (lambda m: m.pop("files"),
                       lambda m: m["files"].pop("eval"),
                       lambda m: m["files"]["eval"].update(rows=0),
                       lambda m: m.update(graphs=[]),
                       lambda m: m.update(graphs={"teams.chat": []}),
                       lambda m: m["files"]["eval"].update(file="../outside.jsonl")):
            m = json.loads(json.dumps(original))
            change(m)
            path.write_text(json.dumps(m), encoding="utf-8")
            self.assertEqual(dataset.status(d)[0], dataset.MISMATCH)
            with mock.patch.object(DSE, "score_rows") as score:
                code, result = DSE.run(d, StubBackend(), print_fn=lambda *_: None)
            self.assertEqual((code, result), (1, None))
            score.assert_not_called()

    def test_output_created_during_scoring_is_not_overwritten(self):
        d = self.build()
        path = self.tmp / "concurrent-result.json"

        def score(rows, backend):
            path.write_text("other run", encoding="utf-8")
            return []

        with mock.patch.object(DSE, "score_rows", side_effect=score):
            code, result = DSE.run(d, StubBackend(), out=path, print_fn=lambda *_: None)
        self.assertEqual((code, result), (1, None))
        self.assertEqual(path.read_text(encoding="utf-8"), "other run")

    def test_changed_data_is_reported_even_before_an_evaluation_result_exists(self):
        d = self.build()
        with (d / dataset.EVAL).open("a", encoding="utf-8") as stream:
            stream.write("not-json\n")
        self.assertEqual(dataset.status(d)[0], dataset.MISMATCH)

    def test_incomplete_result_is_a_mismatch_without_a_score(self):
        d = self.build()
        self.run_eval(d, StubBackend())
        path = d / "result-stubbackend.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for change in (lambda r: r.pop("summary"), lambda r: r.update(rows=[None]),
                       lambda r: r.update(provenance=[]),
                       lambda r: r.update(summary="WITHHELD_JEV"),
                       lambda r: r.update(summary=dataset.NO_INPUT),
                       lambda r: r["rows"][0].update(category="unknown")):
            r = json.loads(json.dumps(original))
            change(r)
            path.write_text(json.dumps(r), encoding="utf-8")
            self.assertEqual(dataset.status(d, path)[0], dataset.MISMATCH)

    def build(self, review=False):
        _, cand, _ = self.candidate(shipped_rows(30))
        if review:
            anonymize.mark_reviewed(cand, confirmed=True)
        d = self.tmp / "ds"
        dataset.split(cand, d, 50, self.graphs, self.pbs)
        return d

    def run_eval(self, d, backend):
        out = []
        with no_network():
            code, result = DSE.run(d, backend, print_fn=out.append)
        return code, result, "\n".join(out)

    def test_the_stub_completes_the_path_on_fictional_data_and_is_a_pipeline_check_not_an_accuracy(self):
        d = self.build()
        code, result, text = self.run_eval(d, StubBackend())
        self.assertEqual(code, 0)
        self.assertEqual(result["run_kind"], "pipeline_check")
        self.assertEqual(set(result["summary"]), set(dataset.CATEGORIES))
        self.assertEqual(sum(result["summary"].values()), len(dataset.eval_rows(d)))
        self.assertTrue({r["category"] for r in result["rows"]} <= set(dataset.CATEGORIES))
        prov = result["provenance"]
        self.assertTrue(prov["data_version"].startswith("v1:"))
        self.assertEqual(prov["graphs"], DSE.current_fingerprints())
        self.assertEqual(prov["model"]["judge"], "StubBackend")
        self.assertEqual(prov["scoring"], DSE.scoring_identity())
        self.assertEqual(prov["anonymization_status"], anonymize.STATUS_REQUIRED)
        self.assertIn("PIPELINE_CHECK", text)
        self.assertIn("判断精度ではありません", text)
        path = d / "result-stubbackend.json"
        self.assertEqual(dataset.check_result(json.loads(path.read_text(encoding="utf-8")), d,
                                              DSE.current_fingerprints(), DSE.scoring_identity()), [])
        self.assertEqual(dataset.roadmap_status(d, path, DSE.current_fingerprints(), DSE.scoring_identity())[0], dataset.NOT_RUN)

    def test_threshold_changes_invalidate_evaluation_but_not_calibration(self):
        from kimeru import graph
        d = self.build()
        changed = json.loads(json.dumps(self.graphs))
        node = next(n for n in changed["teams.chat"]["nodes"].values() if n.get("kind") == "judge")
        node["min_conf"] = 0.87
        self.assertEqual(graph.calibration_key(self.graphs["teams.chat"], self.pbs),
                         graph.calibration_key(changed["teams.chat"], self.pbs))
        fps = dataset.graph_fingerprints(changed, self.pbs)
        self.assertTrue(dataset.verify_dataset(d, fps))
        with mock.patch.object(DSE, "current_fingerprints", return_value=fps), \
                mock.patch.object(DSE, "score_rows") as score:
            code, result, text = self.run_eval(d, StubBackend())
        self.assertEqual((code, result), (1, None))
        score.assert_not_called()
        self.assertIn(dataset.MISMATCH, text)

    def test_graphs_changed_since_the_split_stop_the_run(self):
        d = self.build()
        with mock.patch.object(DSE, "current_fingerprints", return_value={"teams.chat": "v1:changed"}):
            code, result, text = self.run_eval(d, StubBackend())
        self.assertEqual((code, result), (1, None))
        self.assertIn(dataset.MISMATCH, text)
        self.assertFalse(list(d.glob("result-*.json")))

    def test_jev_results_stay_private_with_no_counts(self):
        d = self.build()
        code, result, text = self.run_eval(d, FakeJev())
        self.assertEqual(code, 0)
        self.assertEqual(result["summary"], "WITHHELD_JEV")
        self.assertIs(result["shareable"], False)
        self.assertIn("TypeSafe", text)
        for label in dataset.LABELS_JA.values():
            self.assertNotIn(label, text)
        out = []
        dataset.dispatch(["status", str(d), "--result", str(d / "result-jev.json")], print_fn=out.append)
        for label in dataset.LABELS_JA.values():
            self.assertNotIn(label, "\n".join(out))

    def test_six_categories_stay_apart(self):
        self.assertEqual(dataset.category_of({"outcome": "wrong", "severe_miss": True}), "critical_miss")
        self.assertEqual(dataset.category_of({"outcome": "wrong", "severe_miss": False}), "wrong_decision")
        self.assertEqual(dataset.category_of({"outcome": "human", "severe_miss": False}), "human_confirmation")
        self.assertEqual(dataset.category_of({"outcome": "fallback", "severe_miss": False}), "safe_fallback")
        self.assertEqual(dataset.category_of({"outcome": "correct", "severe_miss": False}), "correct")
        self.assertEqual(dataset.category_of(None), "undeterminable")

        class Broken:
            def ask(self, state, questions):
                raise RuntimeError("judge down")
        with mock.patch.object(DSE.e2e, "run_one", side_effect=RuntimeError("judge down")):
            rows = DSE.score_rows(shipped_rows(3), Broken())
        self.assertEqual({r["category"] for r in rows}, {"undeterminable"})
        self.assertEqual(dataset.tally(rows)["wrong_decision"], 0)

    def test_the_roadmap_item_is_done_only_with_a_reviewed_candidate_from_reviews_and_a_real_judge(self):
        d = self.build(review=False)
        self.run_eval(d, FakeKev())
        fp, sc = DSE.current_fingerprints(), DSE.scoring_identity()
        code, why = dataset.roadmap_status(d, d / "result-kev.json", fp, sc)
        self.assertEqual(code, dataset.NOT_RUN)
        self.assertIn("確認されていません", why)
        d2 = self.tmp / "reviewed"
        d2.mkdir()
        _, cand, _ = self.candidate(shipped_rows(30), name="cand-r")
        anonymize.mark_reviewed(cand, confirmed=True)
        dataset.split(cand, d2 / "ds", 50, self.graphs, self.pbs)
        self.run_eval(d2 / "ds", FakeKev())
        self.assertEqual(dataset.roadmap_status(d2 / "ds", d2 / "ds" / "result-kev.json", fp, sc)[0], "DONE")
        self.assertEqual(dataset.roadmap_status(d2 / "ds", d2 / "ds" / "result-kev.json")[0], dataset.NOT_RUN)
        self.assertEqual(dataset.roadmap_status(d2 / "ds", d2 / "ds" / "result-kev.json", fp)[0], dataset.NOT_RUN)

    def test_cli_status_checks_current_graphs_and_never_claims_roadmap_done_without_current_scoring(self):
        d = self.build(review=True)
        self.run_eval(d, FakeKev())
        args = ["status", str(d), "--result", str(d / "result-kev.json")]
        lines = []
        self.assertEqual(dataset.dispatch(args, print_fn=lines.append), 0)
        self.assertNotIn("DONE", "\n".join(lines))
        self.assertIn("採点実装は未照合", "\n".join(lines))
        with mock.patch.object(dataset, "graph_fingerprints", return_value={"teams.chat": "changed"}):
            lines = []
            self.assertEqual(dataset.dispatch(args, print_fn=lines.append), 1)
        self.assertIn(dataset.MISMATCH, "\n".join(lines))

    def test_all_undeterminable_rows_do_not_complete_the_real_evaluation_roadmap(self):
        d = self.build(review=True)
        rows = [{"id": r["id"], "category": "undeterminable", "reason": "RuntimeError"}
                for r in dataset.eval_rows(d)]
        with mock.patch.object(DSE, "score_rows", return_value=rows):
            self.run_eval(d, FakeKev())
        code, why = dataset.roadmap_status(d, d / "result-kev.json", DSE.current_fingerprints(), DSE.scoring_identity())
        self.assertEqual(code, dataset.NOT_RUN)
        self.assertIn("判定不能", why)


class TestNotRunIsNeverZeroPercent(Temp):
    def assert_no_percent(self, text):
        self.assertNotRegex(text, r"\d+(\.\d+)?\s*%")
        self.assertNotIn("0/0", text)

    def test_no_dataset_is_no_input_and_no_result_is_not_run(self):
        code, lines = dataset.status(None)
        self.assertEqual(code, dataset.NO_INPUT)
        self.assert_no_percent("\n".join(lines).replace("0% ではありません", ""))
        self.assertEqual(dataset.roadmap_status(None)[0], dataset.NO_INPUT)
        self.assertEqual(dataset.status(self.tmp / "missing")[0], dataset.NO_INPUT)

        src = self.tmp / "rows.jsonl"
        write_jsonl(src, shipped_rows(10))
        dataset.split(src, self.tmp / "ds", 50, self.graphs, self.pbs)
        code, lines = dataset.status(self.tmp / "ds")
        self.assertEqual(code, dataset.NOT_RUN)
        self.assertIn("NOT_RUN", "\n".join(lines))
        self.assert_no_percent("\n".join(lines).replace("0% ではありません", ""))
        self.assertEqual(dataset.roadmap_status(self.tmp / "ds")[0], dataset.NOT_RUN)

        dataset.split(src, self.tmp / "all-tune", 100, self.graphs, self.pbs)
        self.assertEqual(dataset.status(self.tmp / "all-tune")[0], dataset.NO_INPUT)
        with no_network():
            code, result = DSE.run(self.tmp / "all-tune", StubBackend(), print_fn=lambda *_: None)
        self.assertEqual(code, 1)
        self.assertEqual(result["summary"], dataset.NO_INPUT)
        self.assertEqual(dataset.status(self.tmp / "all-tune", self.tmp / "all-tune" / "result-stubbackend.json")[0], dataset.NO_INPUT)

    def test_the_cli_says_no_input_when_nothing_was_reviewed(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["dataset", "anonymize"]), 1)
            self.assertEqual(cli.main(["dataset", "status"]), 1)
        text = buf.getvalue()
        self.assertIn("NO_INPUT", text)
        self.assertNotIn("DONE", text)
        self.assert_no_percent(text.replace("0% ではありません", ""))

    def test_the_node_scorer_with_no_answers_says_no_input(self):
        answers = self.tmp / "answers.jsonl"
        answers.write_text("", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            RUN_EVAL.score(answers)
        self.assertIn("NO_INPUT", buf.getvalue())
        self.assertNotIn("ok/n", buf.getvalue())

    def test_the_final_action_eval_with_no_events_says_no_input(self):
        empty = self.tmp / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        buf = io.StringIO()
        with mock.patch.object(E2E, "KevBackend", StubBackend), \
                mock.patch("sys.argv", ["e2e.py", "--backend", "kev", "--fixtures", str(empty)]), \
                contextlib.redirect_stdout(buf):
            E2E.main()
        self.assertIn("NO_INPUT", buf.getvalue())
        self.assertNotIn("correct final action", buf.getvalue())

    def test_the_old_fixture_scores_are_documented_as_history_not_current_accuracy(self):
        doc = (ROOT / "docs" / "evaluation.md").read_text(encoding="utf-8")
        self.assertIn("NOT_RUN", doc)
        self.assertIn("履歴", doc)
        readme = (ROOT / "eval" / "README.md").read_text(encoding="utf-8")
        self.assertIn("NOT_RUN", readme)
        self.assertIn("dataset_eval.py", readme)


if __name__ == "__main__":
    unittest.main()
