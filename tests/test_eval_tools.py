"""K01: the evaluation tools that measure what a context does to a draft. draft_quality.py runs the same fixtures once per group of
settings (--settings), records how grounded each draft is and which blocks its request carried; revision_rate.py counts the PM's 修正
per writer from a state folder. Both only read; the writer is a fixed script, so nothing here calls a model."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"
SCRIPT = (EVAL / "scripted_writer.py").as_posix()
ORIGINAL_FOUR_SHA = "f2473af43d371697dd5637944b34c44e85dd3b4ee13e05f383fc21e087ee51c9"   # the 4 lines before K01, LF endings


def load(name):
    spec = importlib.util.spec_from_file_location(f"eval_{name}", EVAL / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dq = load("draft_quality")
rr = load("revision_rate")


def run_dq(*argv):
    out = io.StringIO()
    with mock.patch.object(sys, "argv", ["draft_quality.py", *argv]), contextlib.redirect_stdout(out), \
            mock.patch.dict(os.environ, {"KIMERU_WRITER_CMD": f'"{sys.executable}" "{SCRIPT}"'}):
        dq.main()
    return out.getvalue()


class TestSettingsGroups(unittest.TestCase):
    def test_two_groups_run_the_same_fixtures_twice_and_the_environment_is_put_back(self):
        before = dict(os.environ)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        text = run_dq("--writer", "cmd", "--fixtures", "fixtures_context.jsonl", "--kinds", "teams.chat", "--per-kind", "12",
                      "--settings", "writer_precedent=0", "--settings", "writer_precedent=1", "--out", str(tmp / "q.jsonl"))
        self.assertEqual(dict(os.environ), before)
        table = text.split("== 設定の組 x 種別")[1]
        for label in ("writer_precedent=0", "writer_precedent=1"):
            self.assertEqual(sum(1 for l in table.splitlines() if l.strip().startswith(label + " ") and "teams.reply" in l), 1, label)
        rows = [json.loads(l) for l in (tmp / "q.jsonl").read_text(encoding="utf-8").splitlines()]
        per_group = {g: [r["id"] for r in rows if r["settings"] == g] for g in ("writer_precedent=0", "writer_precedent=1")}
        self.assertEqual(per_group["writer_precedent=0"], per_group["writer_precedent=1"])
        self.assertGreaterEqual(len(per_group["writer_precedent=0"]), 12)

    def test_the_settings_are_in_the_environment_only_while_a_group_runs(self):
        env = dq.config.SETTINGS["writer_precedent"][0]
        with mock.patch.dict(os.environ, {env: "0"}):
            with dq.settings_env({"writer_precedent": "1"}):
                self.assertEqual(dq.config.value("writer_precedent"), "1")
            self.assertEqual(os.environ[env], "0")
        self.assertNotIn(env, os.environ)
        with self.assertRaises(RuntimeError), dq.settings_env({"writer_precedent": "1"}):
            raise RuntimeError("a run that fails")
        self.assertNotIn(env, os.environ)

    def test_an_unknown_setting_is_refused_and_no_setting_is_one_group_of_the_defaults(self):
        with self.assertRaises(SystemExit):
            dq.parse_settings(["no_such_setting=1"])
        self.assertEqual(dq.parse_settings(None), [("(既定)", {})])
        self.assertEqual(dq.parse_settings(["writer_precedent=1,memory=1"]), [("writer_precedent=1,memory=1", {"writer_precedent": "1", "memory": "1"})])


class TestDraftRecord(unittest.TestCase):
    def rows(self, *settings):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        args = ["--writer", "cmd", "--fixtures", "fixtures_context.jsonl", "--kinds", "teams.chat", "--per-kind", "12", "--out", str(tmp / "q.jsonl")]
        for s in settings:
            args += ["--settings", s]
        run_dq(*args)
        return [json.loads(l) for l in (tmp / "q.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_a_draft_carries_the_grounding_numbers_and_which_blocks_its_request_had(self):
        rows = self.rows("writer_precedent=0", "writer_precedent=1")
        for key in ("grounded_rate", "tokens_total", "tokens_unverified", "flags", "thread_given", "precedent_given", "style_given", "today_given"):
            self.assertIn(key, rows[0])
        replies = [r for r in rows if r["type"] == "teams.reply"]
        self.assertTrue(replies)
        for r in replies:
            self.assertEqual(r["precedent_given"], r["settings"] == "writer_precedent=1" and r["id"] not in ("ctx-1", "ctx-2", "ctx-3", "ctx-4"), r["id"])
            self.assertTrue(r["thread_given"], r["id"])
            self.assertFalse(r["style_given"])
            self.assertFalse(r["today_given"])

    def test_grounded_rate_is_the_share_of_the_drafts_tokens_that_the_material_has(self):
        material = "10/15 までに 3件 を 佐藤さん が確認します"
        self.assertEqual(dq.grounding("10/15 に佐藤さんが確認します", material), {"tokens_total": 2, "tokens_unverified": 0, "grounded_rate": 1.0})
        g = dq.grounding("10/20 に佐藤さんが 5件 確認します", material)
        self.assertEqual((g["tokens_total"], g["tokens_unverified"]), (3, 2))
        self.assertAlmostEqual(g["grounded_rate"], 1 / 3)
        self.assertEqual(dq.grounding("確認します", material), {"tokens_total": 0, "tokens_unverified": 0, "grounded_rate": None})

    def test_a_token_written_twice_counts_once(self):
        g = dq.grounding("10/20 の件です。10/20 に直します", "材料なし")
        self.assertEqual((g["tokens_total"], g["tokens_unverified"], g["grounded_rate"]), (1, 1, 0.0))


class TestFixtures(unittest.TestCase):
    def lines(self):
        return [l for l in (EVAL / "fixtures_context.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_the_four_original_fixtures_are_unchanged_and_the_new_ones_carry_a_precedent_and_the_facts_a_reply_may_have(self):
        lines = self.lines()
        self.assertGreaterEqual(len(lines), 12)
        head = "".join(l.rstrip("\r") + "\n" for l in lines[:4])
        self.assertEqual(hashlib.sha256(head.encode("utf-8")).hexdigest(), ORIGINAL_FOUR_SHA)
        for l in lines[4:]:
            fx = json.loads(l)
            self.assertTrue(fx["precedent"] and all(isinstance(p, str) for p in fx["precedent"]), fx["id"])
            self.assertTrue(fx["expect_facts"] and all(isinstance(p, str) for p in fx["expect_facts"]), fx["id"])
            self.assertTrue(fx["context"]["earlier"], fx["id"])

    def test_run_eval_and_e2e_still_read_every_fixture(self):
        run_eval = load("run_eval")
        fxs = run_eval.fixtures(EVAL / "fixtures_context.jsonl")
        self.assertGreaterEqual(len(fxs), 12)
        for fx in fxs:
            req = run_eval.request(fx)
            self.assertEqual(req["id"], fx["id"])
            self.assertTrue(run_eval.labeled_nodes(fx), fx["id"])
        e2e = load("e2e")
        self.assertEqual([f["id"] for f in e2e.fixtures("fixtures_context.jsonl")], [f["id"] for f in fxs])


class TestRevisionRate(unittest.TestCase):
    SECRET_DRAFT, SECRET_INSTRUCTION = "下書きの本文ひみつ", "指示の文ひみつ"

    def folder(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)

        def item(key, writer):
            actions = [{"type": "teams.reply", "text": self.SECRET_DRAFT, **({"drafted_by": writer} if writer else {})}]
            return {"key": key, "status": "approved", "record": {"actions": actions}}

        items = {"1": item("k1", "copilot"), "2": item("k2", "copilot"), "3": item("k3", "copilot"), "4": item("k4", "copilot"),
                 "5": item("k5", "claude"), "6": item("k6", None)}
        (out / "approvals.json").write_text(json.dumps({"next": 7, "items": items}, ensure_ascii=False), encoding="utf-8")
        log = [{"at": "2026-10-12T09:00:00", "key": "k1", "id": 1, "status": "redrafted", "instruction": self.SECRET_INSTRUCTION},
               {"at": "2026-10-12T09:05:00", "key": "k1", "id": 1, "status": "approved"},
               {"at": "2026-10-12T09:10:00", "key": "k2", "id": 2, "status": "redrafted"},
               {"at": "2026-10-12T09:20:00", "key": "k5", "id": 5, "status": "rejected"},
               {"at": "2026-10-12T09:30:00", "key": "k6", "id": 6, "status": "redrafted"},
               {"at": "2026-10-12T09:40:00", "key": "gone", "id": 99, "status": "redrafted"}]
        (out / "approvals.log.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in log), encoding="utf-8")
        return out

    def test_the_rate_is_the_revisions_over_the_drafts_of_each_writer(self):
        got = rr.rates(self.folder())
        self.assertEqual(got["copilot"], {"drafted": 4, "revised": 2, "rate": 0.5})
        self.assertEqual(got["claude"], {"drafted": 1, "revised": 0, "rate": 0.0})
        self.assertEqual(set(got), {"copilot", "claude"})   # a template's revision and an item that is gone have no writer

    def test_the_output_is_a_table_of_names_and_counts_and_the_files_are_not_touched(self):
        out = self.folder()
        before = {p.name: p.read_bytes() for p in out.iterdir()}
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["revision_rate.py", "--out", str(out)]), contextlib.redirect_stdout(buf):
            rr.main()
        text = buf.getvalue()
        self.assertIn("copilot", text)
        self.assertIn("50%", text)
        for secret in (self.SECRET_DRAFT, self.SECRET_INSTRUCTION, "k1"):
            self.assertNotIn(secret, text)
        self.assertEqual({p.name: p.read_bytes() for p in out.iterdir()}, before)

    def test_an_empty_folder_says_so(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        self.assertEqual(rr.rates(out), {})


if __name__ == "__main__":
    unittest.main()
