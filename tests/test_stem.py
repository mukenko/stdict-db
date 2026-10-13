"""규칙 활용형 어간 후보(stdict_stem)와 이를 쓰는 조회 테스트.

순수 함수 부분은 DB 없이, 조회 부분은 실제 data/stdict.sqlite 가 있을 때만 실행한다.
실행(저장소 루트): python3 -m unittest tests.test_stem -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stdict_lookup as sl  # noqa: E402
import stdict_stem as st  # noqa: E402

real = unittest.skipUnless(sl.DEFAULT_DB.exists(), "조회용 DB(data/stdict.sqlite)가 없습니다")


def stems(word):
    return [s for s, _ in st.candidates(word)]


class Candidates(unittest.TestCase):
    def test_consonant_stem_past(self):
        self.assertIn("먹", stems("먹었다"))
        self.assertIn("잡", stems("잡았어요"))

    def test_contracted_past(self):
        for word, stem in [("갔다", "가"), ("봤다", "보"), ("줬다", "주"), ("마셨다", "마시"), ("됐다", "되"),
                           ("왔어요", "오"), ("섰다", "서"), ("냈다", "내")]:
            self.assertIn(stem, stems(word), word)

    def test_ha_verbs(self):
        self.assertIn("공부하", stems("공부했다"))
        self.assertIn("하", stems("해요"))
        self.assertIn("공부하", stems("공부하여"))

    def test_contracted_aeo(self):
        for word, stem in [("봐", "보"), ("와서", "오"), ("줘요", "주"), ("마셔", "마시"), ("돼서", "되")]:
            self.assertIn(stem, stems(word), word)

    def test_final_consonant_endings(self):
        for word, stem in [("간", "가"), ("갈", "가"), ("감", "가"), ("간다", "가"), ("갑니다", "가")]:
            self.assertIn(stem, stems(word), word)
        self.assertIn("먹", stems("먹은"))
        self.assertIn("먹", stems("먹는다"))
        self.assertIn("먹", stems("먹으면"))

    def test_base_form_plus_ending(self):
        self.assertIn("예쁘", stems("예쁘다고"))

    def test_explanation_names_the_rule(self):
        rule = dict(st.candidates("갔다"))["가"]
        self.assertIn("가", rule)
        self.assertIn("과거", rule)

    def test_no_crash_on_odd_input(self):
        for w in ("", "a", "ㅋㅋ", "다", "1234", "먹", "\U0001F600"):
            st.candidates(w)


@real
class InferBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = sl.Dictionary()

    @classmethod
    def tearDownClass(cls):
        cls.d.close()

    def words(self, q):
        res = self.d.lookup(q)
        return res["total"], [b["word"] for b in res["base_forms"]], res

    def test_regular_forms_reach_the_headword(self):
        for q, base in [("먹었다", "먹다"), ("갔다", "가다"), ("공부했다", "공부하다"), ("마셨다", "마시다"),
                        ("먹는다", "먹다"), ("예쁘다고", "예쁘다")]:
            total, bases, res = self.words(q)
            self.assertEqual(total, 0, q)
            self.assertIn(base, bases, q)
            self.assertTrue(res["inferred"], q)

    def test_output_states_it_is_inferred_and_gives_the_rule(self):
        text = sl.format_lookup(self.d.lookup("갔다"))
        self.assertIn("규칙으로 추정", text)
        self.assertIn("어간 '가'", text)
        self.assertIn("불규칙", text)

    def test_only_verbs_and_adjectives_are_proposed(self):
        res = self.d.lookup("먹었다")
        for b in res["base_forms"]:
            self.assertIn(b["pos"], self.d.INFER_POS)

    def test_dictionary_listed_forms_are_not_marked_inferred(self):
        res = self.d.lookup("가계해")
        self.assertTrue(res["base_forms"])
        self.assertFalse(res["inferred"])

    def test_existing_headword_is_not_replaced_by_inference(self):
        res = self.d.lookup("가다")
        self.assertGreater(res["total"], 0)
        self.assertEqual(res["base_forms"], [])

    def test_search_by_form_uses_inference_when_nothing_is_listed(self):
        res = self.d.search("갔다", "form")
        self.assertIn("가다", [h["word"] for h in res["hits"]])
        self.assertTrue(res["hits"][0]["match"].startswith("규칙으로 추정"))

    def test_nonsense_finds_nothing(self):
        self.assertEqual(self.d.lookup("ㅋㅋㅋ")["base_forms"], [])
        self.assertEqual(self.d.search("쀏쀏쀏", "form")["hits"], [])

    def test_irregular_is_not_invented(self):
        # 듣다(ㄷ 불규칙)의 '들었다'는 규칙 활용으로는 들다만 나온다. 듣다를 지어내지 않고, 응답이 불규칙 가능성을 밝힌다.
        res = self.d.lookup("들었다")
        self.assertNotIn("듣다", [b["word"] for b in res["base_forms"]])
        self.assertIn("불규칙", sl.format_lookup(res))


@real
class FastPaths(unittest.TestCase):
    """pron_plain 열·FTS 를 쓰는 빠른 경로가 옛 방식(replace·LIKE)과 같은 결과를 내는지."""

    @classmethod
    def setUpClass(cls):
        cls.d = sl.Dictionary()

    @classmethod
    def tearDownClass(cls):
        cls.d.close()

    def test_pron_plain_matches_replace(self):
        if not self.d._has_column("pronunciations", "pron_plain"):
            self.skipTest("pron_plain 이 없는 옛 DB")
        for q in ("연기", "사과", "말", "눈", "밤"):
            fast = self.d.q("SELECT DISTINCT target_code FROM pronunciations WHERE pron_plain=?", (q,))
            slow = self.d.q("SELECT DISTINCT target_code FROM pronunciations WHERE replace(pron_norm, ':', '')=?", (q,))
            self.assertEqual(sorted(r[0] for r in fast), sorted(r[0] for r in slow), q)

    def test_old_db_without_pron_plain_still_works(self):
        d = sl.Dictionary()
        d._has_column = lambda *a: False
        try:
            self.assertEqual(d.search("연기", "pronunciation", 50)["total"], self.d.search("연기", "pronunciation", 50)["total"])
        finally:
            d.close()

    def test_hanja_fts_matches_like(self):
        for q in ("大韓民國", "高知縣", "Kôchi", "演技", "木"):
            like = self.d.q("SELECT target_code FROM entries e WHERE e.original_text LIKE ? ESCAPE '\\'", (sl._like(q),))
            self.assertEqual(self.d.search(q, "hanja", 50)["total"], len(like), q)


if __name__ == "__main__":
    unittest.main()
