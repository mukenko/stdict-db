"""stdict_lookup 테스트. 실제 data/stdict.sqlite(공개 사전 데이터)를 읽기 전용으로 쓴다.

실행: python3 -m unittest tests.test_lookup -v   (저장소 루트에서)
다른 DB 로 시험하려면 환경변수 STDICT_DB 를 지정한다.
"""
import os
import sqlite3
import sys
import tempfile
import threading
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stdict_lookup as sl  # noqa: E402

DB_OK = sl.DEFAULT_DB.exists()
real = unittest.skipUnless(DB_OK, "조회용 DB(data/stdict.sqlite)가 없습니다")


def tiny_db(path, value):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT INTO meta VALUES('v', ?)", (value,))
    c.commit()
    c.close()


class Ranges(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(sl._ranges([1, 2, 3, 4, 6, 7, 8, 10, 11, 12]), "1~4, 6~8, 10~12")
        self.assertEqual(sl._ranges([3]), "3")
        self.assertEqual(sl._ranges([1, 3, 5]), "1, 3, 5")


class Normalize(unittest.TestCase):
    def test_hyphen_caret_space(self):
        self.assertEqual(sl.normalize("가계-하다"), "가계하다")
        self.assertEqual(sl.normalize("간^옹"), "간옹")
        self.assertEqual(sl.normalize("  돈   카를로스 "), "돈 카를로스")

    def test_nfd_input_becomes_nfc(self):
        nfd = unicodedata.normalize("NFD", "연기")
        self.assertNotEqual(nfd, "연기")
        self.assertEqual(sl.normalize(nfd), "연기")

    def test_long_vowel_mark(self):
        self.assertEqual(sl.pron_key("연ː기"), "연:기")
        self.assertEqual(sl.pron_key("연:기"), "연:기")

    def test_superscript(self):
        self.assertEqual(sl.sup(3), "³")
        self.assertEqual(sl.sup(12), "¹²")
        self.assertEqual(sl.sup(0), "")
        self.assertEqual(sl.sup(None), "")


@real
class Golden(unittest.TestCase):
    """HANDOFF 9장 골든 테스트: (코드, 표제어, 발음, 원어). 장단음 구분이 이 프로젝트의 출발점이다."""
    d = sl.Dictionary()

    @classmethod
    def tearDownClass(cls):
        cls.d.close()

    def entries(self, word, **kw):
        return {e["target_code"]: e for e in self.d.lookup(word, max_entries=50, **kw)["entries"]}

    def test_yeongi_homonyms_with_pronunciation(self):
        es = self.entries("연기")
        self.assertGreaterEqual(len(es), 10)
        want = {236214: ("연ː기", "演技"), 236208: ("연ː기", "捐棄"), 459493: ("연기", "延期"), 236213: ("연기", "煙氣")}
        for code, (pron, hanja) in want.items():
            self.assertIn(code, es)
            self.assertIn(pron, es[code]["pronunciations"], code)
            self.assertEqual(es[code]["original"], hanja)

    def test_pron_groups_separate_long_and_short(self):
        res = self.d.lookup("연기", max_entries=2)                  # 보이는 개수와 무관하게 전체 12개를 묶는다
        groups = {g["pronunciation"]: {e["target_code"] for e in g["entries"]} for g in res["pron_groups"]}
        self.assertEqual(groups["연ː기"], {236208, 236214})
        self.assertEqual(res["pron_groups"][0]["pronunciation"], "연ː기")   # 장음 묶음이 먼저
        self.assertEqual(sum(len(v) for v in groups.values()), res["total"])
        self.assertTrue({236195, 236213, 459493} <= groups["연기"])
        text = sl.format_lookup(res)
        self.assertLess(text.index("발음별 요약"), text.index("■"))
        self.assertIn("[연ː기] 장음 포함 2개: 5번(捐棄), 9번(演技)", text)
        self.assertIn("[연기] 10개: 어깨번호 1~4, 6~8, 10~12 (年忌, 年紀, 年期, 延期, 連記, 連棋/連碁, 煙氣, 緣起, 緣起, 聯騎)", text)
        self.assertIn("※ 장음(ː)이 있는 것은 2개(5번(捐棄), 9번(演技))이고, 나머지 10개는 장음이 없다.", text)

    def test_no_pron_summary_when_pronunciation_is_unique(self):
        self.assertEqual(self.d.lookup("풀발")["pron_groups"], [])
        self.assertNotIn("발음별 요약", sl.format_lookup(self.d.lookup("풀발")))

    def test_pulbal(self):
        res = self.d.lookup("풀발")
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["entries"][0]["target_code"], 358706)
        self.assertEqual(res["entries"][0]["pronunciations"], ["풀빨"])
        self.assertEqual(res["entries"][0]["headword"], "풀-발")        # 원 표기는 하이픈 포함

    def test_text_explains_long_mark(self):
        text = sl.format_lookup(self.d.lookup("연기"))
        self.assertIn("[연ː기]", text)
        self.assertIn(sl.LONG_NOTE, text)
        self.assertIn(sl.ATTRIBUTION, text)


@real
class Lookup(unittest.TestCase):
    d = sl.Dictionary()

    @classmethod
    def tearDownClass(cls):
        cls.d.close()

    def test_input_variants_give_same_result(self):
        base = self.d.lookup("가계하다")["total"]
        self.assertGreater(base, 0)
        self.assertEqual(self.d.lookup("가계-하다")["total"], base)
        self.assertEqual(self.d.lookup(unicodedata.normalize("NFD", "가계하다"))["total"], base)

    def test_spacing_variants_are_one_headword(self):
        """사전은 띄어쓰기를 무시하고 묶는다: '감정교육'(6716)과 '감정 교육'(387939)은 어깨번호 1·2."""
        for q in ("감정교육", "감정 교육", "감정  교육"):
            res = self.d.lookup(q)
            self.assertEqual({e["target_code"] for e in res["entries"]}, {6716, 387939}, q)
            self.assertEqual(sorted(e["sup_no"] for e in res["entries"]), [1, 2], q)

    def test_all_homonym_numbers_are_assigned_except_known_gaps(self):
        n_null = self.d.q("SELECT COUNT(*) FROM entries WHERE sup_no IS NULL")[0][0]
        self.assertLess(n_null, 100)                              # supno 후: 검색 색인에 없는 로컬 항목 50건 안팎만 NULL
        row = self.d.q("SELECT sup_no FROM entries WHERE target_code=236214")[0]
        self.assertEqual(row[0], 9)                               # 연기⁹ 演技

    def test_max_entries_caps_output_and_reports_total(self):
        res = self.d.lookup("가", max_entries=3)
        self.assertGreater(res["total"], 3)
        self.assertEqual(res["shown"], 3)
        self.assertEqual(len(res["entries"]), 3)
        self.assertIn("생략됨", sl.format_lookup(res))

    def test_response_size_is_bounded(self):
        text = sl.format_lookup(self.d.lookup("가", max_entries=12))
        self.assertLess(len(text), 20000)

    def test_cited_examples_are_included_with_their_source(self):
        text = sl.format_lookup(self.d.lookup("제작하다", include_examples=True))
        self.assertIn("용례: 성인의 글 아닌 언문 따위를 제작하는 것은 아무리 왕명이라도 거절할 것이다. ≪김동인, 대수양≫", text)
        self.assertIn("용례: 사람은 도구를 제작하고 사용할 줄 아는 동물이다.\n", text)   # 출전 없는 용례는 그대로(채팅은 뜻마다 3개까지)

    def test_hyphen_headwords_listed_without_assigning_homonyms(self):
        res = self.d.lookup("제작")
        self.assertEqual(res["hyphen_total"], 16)
        self.assertEqual(len(res["hyphen_words"]), sl.HYPHEN_SHOWN)
        names = [w["word"] + sl.sup(w["sup_no"]) for w in res["hyphen_words"]]
        self.assertEqual(names[:4], ["제작-되다", "제작-하다¹", "제작-하다²", "제작-하다³"])   # -하다·-되다 가 먼저
        text = sl.format_lookup(res)
        self.assertIn("「제작-」 형태로 시작하는 표제어 16개: 제작-되다", text)
        self.assertIn("외 6개", text)
        self.assertEqual(len(self.d.lookup("제작", full=True)["hyphen_words"]), 16)
        self.assertEqual(self.d.lookup("가")["hyphen_words"], [])                      # 한 글자는 찾지 않는다
        self.assertEqual(self.d.lookup("가나다라마바사없는말")["hyphen_words"], [])

    def test_no_examples_by_default(self):
        res = self.d.lookup("나무", max_entries=5)
        self.assertFalse([1 for e in res["entries"] for p in e["pos"] for s in p["senses"] if s["examples"]])

    def test_unknown_word_has_no_entries(self):
        res = self.d.lookup("없는말어쩌구")
        self.assertEqual(res["total"], 0)
        self.assertEqual(res["entries"], [])
        self.assertIn("표제어에 없습니다", sl.format_lookup(res))

    def test_conjugated_form_points_to_base(self):
        res = self.d.lookup("먹어")
        self.assertEqual(res["total"], 0)
        self.assertIn("먹다", [b["word"] for b in res["base_forms"]])

    def test_prefix_suggestions(self):
        self.assertTrue(self.d.suggest("가계하"))

    def test_empty_input(self):
        with self.assertRaises(sl.StdictError):
            self.d.lookup("  -^ ")


@real
class Search(unittest.TestCase):
    d = sl.Dictionary()

    @classmethod
    def tearDownClass(cls):
        cls.d.close()

    def codes(self, query, by, limit=50):
        return [h["target_code"] for h in self.d.search(query, by, limit)["hits"]]

    def test_pronunciation_with_long_mark_is_exact(self):
        res = self.d.search("연ː기", "pronunciation", 50)
        codes = {h["target_code"] for h in res["hits"]}
        self.assertTrue({236214, 236208} <= codes)
        self.assertTrue(all("연ː기" in h["pronunciations"] for h in res["hits"]))
        self.assertEqual(self.codes("연:기", "pronunciation"), self.codes("연ː기", "pronunciation"))

    def test_pronunciation_without_mark_matches_both_lengths(self):
        short = {h["target_code"] for h in self.d.search("연기", "pronunciation", 50)["hits"]}
        long_ = {h["target_code"] for h in self.d.search("연ː기", "pronunciation", 50)["hits"]}
        self.assertTrue(long_ < short)
        self.assertIn(459493, short)

    def test_hanja_exact_first(self):
        res = self.d.search("演技", "hanja", 5)
        self.assertEqual(res["hits"][0]["target_code"], 236214)
        self.assertGreater(res["total"], 1)

    def test_form_conjugation_and_short_form(self):
        res = self.d.search("가계해", "form", 20)
        self.assertIn(2, [h["target_code"] for h in res["hits"]])
        self.assertTrue(all("준말" in h["match"] for h in res["hits"]))

    def test_definition_long_phrase_uses_fts(self):
        res = self.d.search("목질로 된 여러해살이", "definition", 10)
        self.assertIn("나무", [h["word"] for h in res["hits"]])

    def test_definition_two_chars_is_one_pass_and_capped(self):
        res = self.d.search("식물", "definition", 5)
        self.assertIsNone(res["total"])                         # 2글자는 전체 개수를 세지 않음
        self.assertEqual(res["shown"], 5)
        self.assertEqual(len({h["target_code"] for h in res["hits"]}), 5)
        self.assertIn("전체 개수를 세지 않음", sl.format_search(res))

    def test_definition_needs_two_chars(self):
        with self.assertRaises(sl.StdictError) as cm:
            self.d.search("식", "definition")
        msg = str(cm.exception)                                  # 대신 쓸 방법을 안내해야 한다
        for hint in ("2글자", "lookup_word", "pronunciation", "hanja", "form"):
            self.assertIn(hint, msg)

    def test_bad_kind(self):
        with self.assertRaises(sl.StdictError):
            self.d.search("연기", "nonsense")

    def test_special_characters_are_safe(self):
        for q in ('"', "%", "_", "'; DROP TABLE entries;--", "a*b", "(", "NEAR(a b)"):
            for by in sl.SEARCH_KINDS:
                try:
                    self.d.search(q, by, 3)                     # 오류 없이 끝나야 함
                except sl.StdictError as e:                     # 허용되는 것: 너무 짧은 뜻풀이 검색어뿐
                    self.assertTrue(by == "definition" and len(sl.normalize(q)) < 2, (q, by, str(e)))
        self.d.lookup("%")
        self.assertGreater(self.d.q("SELECT COUNT(*) FROM entries")[0][0], 0)

    def test_format_search(self):
        text = sl.format_search(self.d.search("연ː기", "pronunciation", 3))
        self.assertIn("연ː기", text)
        self.assertIn(sl.LONG_NOTE, text)


@real
class Safety(unittest.TestCase):
    def test_database_is_read_only(self):
        d = sl.Dictionary()
        self.addCleanup(d.close)
        with self.assertRaises(sqlite3.OperationalError):
            d.conn().execute("INSERT INTO meta VALUES('x','y')")

    def test_threads(self):
        d, errors = sl.Dictionary(), []
        self.addCleanup(d.close)

        def work():
            try:
                for w in ("연기", "가계하다", "나무"):
                    self.assertGreater(d.lookup(w)["total"], 0)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=work) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])


class Plumbing(unittest.TestCase):
    """실제 DB 없이 확인할 수 있는 동작: 파일 교체 후 재연결, 시간 제한, 오류 문장."""

    def test_reconnects_after_file_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, new = Path(tmp) / "live.sqlite", Path(tmp) / "new.sqlite"
            tiny_db(live, "1")
            d = sl.Dictionary(live)
            self.addCleanup(d.close)
            self.assertEqual(d.meta(), {"v": "1"})
            tiny_db(new, "2")
            os.replace(new, live)                                  # 빌드가 하는 교체와 같은 방식
            self.assertEqual(d.meta(), {"v": "2"})

    def test_query_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.sqlite"
            tiny_db(p, "1")
            d = sl.Dictionary(p)
            self.addCleanup(d.close)
            with self.assertRaises(sl.QueryTimeout):
                d.q("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<1000000000) "
                    "SELECT count(*) FROM c", timeout=0.2)
            self.assertEqual(d.meta(), {"v": "1"})                 # 중단 뒤에도 연결이 멀쩡함

    def test_missing_db_message_hides_path(self):
        d = sl.Dictionary("/nonexistent/dir/stdict.sqlite")
        with self.assertRaises(sl.DictUnavailable) as cm:
            d.lookup("연기")
        self.assertNotIn("nonexistent", str(cm.exception))

    def test_wrong_file_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.sqlite"
            p.write_bytes(b"not a database")
            with self.assertRaises(sl.DictUnavailable):
                sl.Dictionary(p).lookup("연기")


if __name__ == "__main__":
    unittest.main()
