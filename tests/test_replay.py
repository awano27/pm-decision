"""`kimeru replay`: reviewed cases run again with the recorded answers. The model is not called, nothing is posted, the queue, the
notices and the approvals are not touched, and no text of an event is printed or saved."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from kimeru import backends, cli, replay, review
from tests.test_review_stats import GRAPHS, PBS, Base

TEXT_A = "ZZREPLAYTEXTA リリース日を来週火曜にずらしてよいか判断お願いします。QAが止まっていて至急です"
TEXT_B = "ZZREPLAYTEXTB ログイン画面の改修、今どこまで進んでますか？"
GUARDED = ("queue.jsonl", "notices.jsonl", "approvals.json")


class TestReplay(Base):
    def seed(self):
        self.decide([self.chat(TEXT_A, i="1"), self.chat(TEXT_B, i="2")])
        it = iter(["y", "y"])
        review.run(self.out, GRAPHS, PBS, input_fn=lambda p: next(it), print_fn=lambda *_: None)

    def snapshot(self):
        snap = {}
        for folder in (self.out, self.state):
            for name in GUARDED:
                p = folder / name
                snap[str(p)] = p.read_bytes() if p.exists() else None
        return snap

    def run_cli(self, *extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["--out", str(self.out), "replay", *extra])
        return code, buf.getvalue()

    def test_unchanged_graph_replays_to_the_same_final_action(self):
        self.seed()
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("最終行動が変わった件: 0 件", text)
        self.assertIn("記録なし（playbook）", text)   # a plan keeps only some of its answers: that case stops, the other one is replayed
        self.assertIn("再生した事例: 1 / 2 件", text)
        data = json.loads((self.state / "replay" / "replay.json").read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["cases"], 2)
        self.assertEqual(data["summary"]["no_record"], 1)
        self.assertEqual(data["summary"]["changed"], 0)

    def test_a_changed_action_is_listed_by_id_and_node_and_counted_by_verdict(self):
        self.seed()
        rows = review.read_jsonl(self.out / "decisions.jsonl")
        rows[1]["outcome"] = "an_old_outcome"            # as if the graph then ended somewhere else
        (self.out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("最終行動が変わった件: 1 件", text)
        self.assertIn("an_old_outcome", text)
        self.assertIn("正しいと付いた件のうち変わった: 1 / 1 件", text)
        self.assertIn(review.fixture_row(rows[1], {})["id"], text)

    def test_a_question_without_a_record_says_so_and_only_that_case_stops(self):
        self.seed()
        rows = review.read_jsonl(self.out / "decisions.jsonl")
        rows[1]["path"] = [s for s in rows[1]["path"] if "matched" in (s.get("answer") or {})]   # the judge answers are gone
        (self.out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        _, text = self.run_cli()
        self.assertIn("記録なし", text)
        self.assertIn("再生した事例: 0 / 2 件", text)
        self.assertIn("記録なし（intent）", text)

    def test_the_model_is_not_called_and_the_records_are_byte_for_byte_unchanged(self):
        self.seed()
        for folder in (self.out, self.state):          # present beforehand, so a write would show
            for name in GUARDED:
                p = folder / name
                if not p.exists():
                    p.write_text('{"keep": "me"}\n', encoding="utf-8")
        before = self.snapshot()
        decisions = (self.out / "decisions.jsonl").read_bytes()
        with mock.patch.object(backends.StubBackend, "ask", side_effect=AssertionError("model called")), \
                mock.patch.object(backends.JevBackend, "ask", side_effect=AssertionError("model called")):
            code, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.out / "decisions.jsonl").read_bytes(), decisions)
        self.assertFalse(any(self.out.glob("replay*")))   # nothing is written into --out

    def test_no_text_of_an_event_is_printed_or_saved(self):
        self.seed()
        _, text = self.run_cli()
        saved = (self.state / "replay" / "replay.json").read_text(encoding="utf-8")
        for mark in ("ZZREPLAYTEXT", "リリース日", "ログイン画面", "Sato"):
            self.assertNotIn(mark, text)
            self.assertNotIn(mark, saved)

    def test_nothing_to_replay_writes_nothing(self):
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("再生する事例がありません", text)
        self.assertFalse((self.state / "replay").exists())

    def test_the_result_folder_cannot_be_the_records_folder(self):
        self.seed()
        _, text = self.run_cli("--result", str(self.out))
        self.assertIn("別のフォルダ", text)
        self.assertFalse((self.out / "replay.json").exists())

    def test_a_jev_record_hides_the_right_and_wrong_counts(self):
        self.seed()
        rows = review.read_jsonl(self.out / "decisions.jsonl")
        for r in rows:
            r["judge"] = {"name": "Jev"}
        (self.out / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        _, text = self.run_cli()
        self.assertIn("利用規約", text)
        self.assertNotIn("正しいと付いた件のうち", text)
        saved = json.loads((self.state / "replay" / "replay.json").read_text(encoding="utf-8"))
        self.assertNotIn("right_changed", saved["summary"])


if __name__ == "__main__":
    unittest.main()
