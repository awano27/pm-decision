import unittest

from kimeru import writer
from tests.text_check_cases import BAD, GOOD, TOKENS


class TestUnusable(unittest.TestCase):
    def test_ordinary_business_japanese_is_accepted(self):
        for t in GOOD:
            self.assertEqual(writer.unusable(t), "", t)

    def test_refusals_placeholders_and_broken_output_are_held_with_the_right_reason(self):
        for t, why in BAD:
            self.assertEqual(writer.unusable(t), why, t)


class TestInventedTokens(unittest.TestCase):
    def test_labeled_cases(self):
        for draft, material, expected in TOKENS:
            self.assertEqual(writer.unverified(draft, material), expected, f"{draft!r} vs {material!r}")


class TestMemoAndAskBackAreChecked(unittest.TestCase):
    def test_memo_texts_that_are_unusable_are_dropped_and_invented_facts_flagged(self):
        from kimeru import graph, plan
        from kimeru.backends import StubBackend
        from pathlib import Path
        pbs = plan.load_playbooks(Path(__file__).resolve().parent.parent / "playbooks")
        gs = graph.load_dir(Path(__file__).resolve().parent.parent / "graphs", pbs)
        ev = {"kind": "teams.chat", "id": "m", "author": "佐藤", "text": "来月のリリース日をずらすか判断をお願いします"}
        res = graph.run(gs["teams.chat"][0], ev, StubBackend(), playbooks=pbs)

        class W:
            NAME = "fake"

            def draft(self, res, event, instruction=None):
                d = {k: "受領しました。確認して進めます。" for k, _ in writer.targets(res)}
                d["memo"] = {"summary": "申し訳ありませんが、この内容では作成できません。", "next": "10/5 までに確認する",
                             "missing": ["復旧見込み"], "ask_back": "…"}
                return d
        writer.apply(res, ev, W())
        self.assertNotIn("summary", res["memo"])                       # a refusal is not a summary
        self.assertEqual(res["memo"]["missing"], ["復旧見込み"])
        self.assertEqual(res["memo_unverified"], ["10/5"])              # invented in the memo, shown as ⚠


if __name__ == "__main__":
    unittest.main()
