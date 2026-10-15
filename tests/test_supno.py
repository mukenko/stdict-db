"""supno_check: 검색 결과(어깨번호)와 어휘 링크의 번호가 충돌할 때의 처리.

동음이의어가 새로 추가되면 번호가 하나씩 밀리는데, 다른 항목 안의 링크('채수03')는 한동안 옛 번호를 가리킨다(2026-10-15 채수).
검색 결과가 로컬 그룹과 코드가 정확히 같고 번호가 깨끗한 1~n이면 링크가 낡은 것이므로 검색 번호를 저장한다(알림 포함).
그 밖의 충돌은 지금처럼 저장하지 않는 이슈로 남긴다.

실행(저장소 루트): python3 -m unittest tests.test_supno -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stdict_sync as ss  # noqa: E402


def hit(code, no, word="채수"):
    return {"target_code": str(code), "word": word, "sup_no": str(no), "origin": "", "pos": "명사"}


# 오늘 아침 채수: 신규 536224 가 2번으로 끼어들어 뒤가 하나씩 밀림
FRESH = [hit(316272, 1), hit(536224, 2), hit(316273, 3), hit(316274, 4), hit(483674, 5, "채-수")]
CODES = [316272, 536224, 316273, 316274, 483674]


class LinkConflict(unittest.TestCase):
    def check(self, hits, codes=CODES, known=None):
        return ss.supno_check("채수", codes, known or {}, len(hits), hits, set(codes))

    def test_no_conflict_is_clean(self):
        self.assertIsNone(self.check(FRESH, known={316272: 1, 316274: 4}))

    def test_stale_link_with_clean_search_is_saved_with_a_note(self):
        kind, note = self.check(FRESH, known={316274: 3})            # 링크는 옛 번호(3), 검색은 4
        self.assertEqual(kind, "partial")
        self.assertIn("316274", note)
        self.assertIn("링크 3", note)
        self.assertIn("검색 4", note)

    def test_several_stale_links_are_all_noted(self):
        kind, note = self.check(FRESH, known={316273: 2, 316274: 3, 483674: 4})
        self.assertEqual(kind, "partial")
        for c in ("316273", "316274", "483674"):
            self.assertIn(c, note)

    def test_gap_in_search_numbers_still_blocks(self):
        hits = [hit(316272, 1), hit(536224, 2), hit(316273, 3), hit(316274, 5), hit(483674, 6, "채-수")]
        kind, _ = self.check(hits, known={316274: 3})
        self.assertIn(kind, ("link_conflict", "sup_no_sequence"))    # 번호에 빈 자리가 있는 검색 결과는 못 믿는다

    def test_search_missing_a_local_code_still_blocks(self):
        hits = FRESH[:-1]                                            # 483674 가 검색에 없음
        kind, _ = ss.supno_check("채수", CODES, {316274: 3}, len(hits), hits, set(CODES))
        self.assertEqual(kind, "link_conflict")

    def test_search_with_unknown_extra_code_is_still_an_issue(self):
        hits = FRESH + [hit(999999, 6)]
        kind, _ = ss.supno_check("채수", CODES, {316274: 3}, len(hits), hits, set(CODES))
        self.assertEqual(kind, "remote_only")

    def test_duplicate_numbers_still_block(self):
        hits = [hit(316272, 1), hit(536224, 2), hit(316273, 2), hit(316274, 4), hit(483674, 5, "채-수")]
        kind, _ = self.check(hits, known={316274: 3})
        self.assertIn(kind, ("link_conflict", "sup_no_sequence"))
        self.assertNotEqual(kind, "partial")


if __name__ == "__main__":
    unittest.main()
