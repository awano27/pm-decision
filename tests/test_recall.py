"""K07 recall.py: the words of a text, their overlap (Dice) and the choice of the best few. Pure functions, no file, no clock."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import unittest

from kimeru import recall


class TestTerms(unittest.TestCase):
    def test_nfkc_makes_full_width_and_half_width_the_same_word(self):
        self.assertEqual(set(recall.terms("ＱＡ環境")), set(recall.terms("QA環境")))
        self.assertIn("環境", recall.terms("ＱＡ環境"))

    def test_symbols_and_spaces_separate_runs_so_no_gram_crosses_them(self):
        t = recall.terms("リリース、延期。")
        self.assertIn("延期", t)
        self.assertNotIn("ス延", t)
        self.assertNotIn("期。", t)
        self.assertEqual(set(recall.terms("環境 確認")), {"環境", "確認"})

    def test_a_latin_word_is_a_term_as_a_whole_and_keeps_its_hyphen(self):
        t = recall.terms("checkout-api と ABC-123 と QA")
        for word in ("w:checkout-api", "w:abc-123", "w:qa"):
            self.assertIn(word, t)
        self.assertEqual(t["w:abc-123"], "ABC-123")   # shown as written
        self.assertIn("ch", t)                        # and its 2-grams as well

    def test_a_stop_gram_is_dropped_and_a_single_character_gives_nothing(self):
        self.assertNotIn("です", recall.terms("確認です"))
        self.assertIn("確認", recall.terms("確認です"))
        self.assertEqual(recall.terms("a"), {})
        self.assertEqual(recall.terms(None), {})

    def test_a_letter_that_changes_length_when_case_folded_does_not_shift_the_shown_words(self):
        t = recall.terms("Straße のQA環境")   # casefold turns ß into ss, which would move every later slice by one
        self.assertEqual(t["w:qa"], "QA")
        self.assertEqual(t["環境"], "環境")

    def test_the_middle_dot_separates_katakana_and_the_long_vowel_mark_stays(self):
        t = recall.terms("テスト・サーバー")
        self.assertIn("サー", t)
        self.assertIn("バー", t)
        self.assertNotIn("ト・", t)
        self.assertNotIn("・サ", t)

    def test_the_stop_list_is_fixed_and_at_most_forty(self):
        self.assertLessEqual(len(recall.STOP_GRAMS), 40)
        self.assertTrue(all(len(g) == 2 for g in recall.STOP_GRAMS))


class TestDiceAndRank(unittest.TestCase):
    def test_dice_is_twice_the_common_over_the_sum(self):
        self.assertAlmostEqual(recall.dice({"a", "b", "c"}, {"b", "c", "d", "e"}), 2 * 2 / 7)
        self.assertEqual(recall.dice({"a"}, {"b"}), 0.0)
        self.assertEqual(recall.dice({}, {"b"}), 0.0)
        self.assertEqual(recall.dice({"a", "b"}, {"a", "b"}), 1.0)

    def test_a_document_under_the_minimum_is_dropped_and_the_limit_cuts(self):
        q = {"a": "a", "b": "b", "c": "c", "d": "d"}
        docs = [(1, "low", {"a", "x", "y", "z", "w", "v", "u"}), (2, "mid", {"a", "b", "x"}), (3, "high", {"a", "b", "c", "d"})]
        self.assertEqual([p for _, p in recall.rank(q, docs, 0.25, 5)], ["high", "mid"])
        self.assertEqual([p for _, p in recall.rank(q, docs, 0.25, 1)], ["high"])
        self.assertEqual(recall.rank(q, docs, 0.25, 0), [])

    def test_a_tie_goes_to_the_newer_document(self):
        q = {"a": "a", "b": "b"}
        docs = [(1, "old", {"a", "b"}), (3, "new", {"a", "b"}), (2, "mid", {"a", "b"})]
        self.assertEqual([p for _, p in recall.rank(q, docs, 0.25, 2)], ["new", "mid"])

    def test_the_shared_terms_name_words_first_and_name_a_word_once(self):
        q = recall.terms("QA 環境が止まっていてリリース日を動かしたい")
        d = recall.terms("QA 環境の復旧待ちでリリースを 1 週ずらす")
        self.assertEqual(recall.shared(q, d), ["QA", "環境"])
        self.assertEqual(recall.shared(q, d, limit=1), ["QA"])

    def test_the_shared_terms_are_whole_strings_never_grammar_fragments(self):
        q = recall.terms("QA環境が止まっていてリリース日を来週に動かしたいです。判断お願いします")
        self.assertEqual(recall.shared(q, recall.terms("QA 環境の復旧待ちで、リリースを1週ずらす件の判断をお願いします"), 3),
                         ["QA", "環境", "リリース"])   # リリ, リー, ース chained back into the word
        self.assertEqual(recall.shared(q, recall.terms("リリース延期の相談です。QAが止まっています"), 3), ["QA", "リリース"])   # が止 / 止ま: no
        self.assertEqual(recall.shared(q, recall.terms("検証環境がダウンしているので出荷日を延ばしてよいでしょうか"), 3), ["環境"])   # 環境が -> 環境
        self.assertEqual(recall.shared(q, recall.terms("QA チームの来月の人員を2名増やしたいです"), 3), ["QA"])   # たい / いで: no

    def test_the_display_does_not_change_the_score(self):
        q = recall.terms("QA環境が止まっていてリリース日を来週に動かしたいです")
        d = recall.terms("リリース延期の相談です。QAが止まっています")
        self.assertAlmostEqual(recall.dice(q, d), 2 * len(set(q) & set(d)) / (len(q) + len(d)))


if __name__ == "__main__":
    unittest.main()
