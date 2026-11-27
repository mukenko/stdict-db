"""API 응답 형식 변경 감지 (2026-11-20: 어휘 관계 type 이 비고 link 가 새 사이트 주소로 바뀐 일)."""
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stdict_sync as ss  # noqa: E402

OLD_LINK = "https://stdict.korean.go.kr/search/searchView.do?word_no=1&searchKeywordTo=3"
NEW_LINK = "https://kdict.korean.go.kr/standard/search/searchView.do?wordNo=1&searchKeywordTo=3"


def item(type_="동의어", link=OLD_LINK, extra=""):
    return ET.fromstring(f"<item><word_info><word>가</word><lexical_info><word>나</word><unit>의미</unit>"
                         f"<type>{type_}</type><link><![CDATA[{link}]]></link></lexical_info>{extra}</word_info></item>")


class LinkNormalisation(unittest.TestCase):
    """링크 주소만 새 사이트 형식으로 바뀐 항목은 해시가 같아야 한다(안 그러면 관계가 있는 항목 전부가 '수정'으로 잡힌다)."""

    def test_same_hash_for_old_and_new_link_forms(self):
        self.assertEqual(ss.item_info(item())[0], ss.item_info(item(link=NEW_LINK))[0])

    def test_real_differences_still_change_the_hash(self):
        self.assertNotEqual(ss.item_info(item())[0], ss.item_info(item(type_="비슷한말"))[0])
        self.assertNotEqual(ss.item_info(item())[0], ss.item_info(item(link=NEW_LINK.replace("wordNo=1", "wordNo=2")))[0])
        self.assertNotEqual(ss.item_info(item())[0], ss.item_info(item(type_=""))[0])

    def test_other_links_are_untouched(self):
        other = "https://dicmedia.korean.go.kr/front/view/mediaView.do?file_no=1"
        self.assertEqual(ss.canon_text(ET.fromstring(f"<link>{other}</link>")), other)


class BuildParser(unittest.TestCase):
    def entry_code(self, link):
        import stdict_build as sb
        R = {"relations": []}
        e = ET.fromstring(f"<relation_info><word>나</word><type>동의어</type><link_target_code>9</link_target_code>"
                          f"<link><![CDATA[{link}]]></link></relation_info>")
        sb.add_rel(R, "relation_info", 1, 0, e, False)
        return R["relations"][0][-1]

    def test_old_and_new_parameter_names_give_the_same_entry_code(self):
        self.assertEqual(self.entry_code(OLD_LINK), 1)
        self.assertEqual(self.entry_code(NEW_LINK), 1)

    def test_no_link_number(self):
        self.assertIsNone(self.entry_code("https://example.org/x"))


class FormatDrift(unittest.TestCase):
    def test_type_blanked_is_drift(self):
        self.assertTrue(ss.format_drift(item(), item(type_="")))

    def test_new_site_link_alone_is_not_drift(self):                                      # 사이트 이전에 따른 영구 변경(2026-11-27 type 복구 후)
        self.assertFalse(ss.format_drift(item(), item(link=NEW_LINK)))

    def test_the_observed_change_is_drift(self):
        self.assertTrue(ss.format_drift(item(), item(type_="", link=NEW_LINK)))

    def test_new_entry_check_looks_only_at_blank_types(self):
        self.assertTrue(ss.format_drift_new(item(type_="")))
        self.assertFalse(ss.format_drift_new(item(link=NEW_LINK)))
        self.assertFalse(ss.format_drift_new(item()))

    def test_unchanged_or_real_edit_is_not_drift(self):
        self.assertFalse(ss.format_drift(item(), item()))
        self.assertFalse(ss.format_drift(item(), item(type_="비슷한말")))                 # type 이 다른 값으로 바뀐 것은 내용 수정
        self.assertFalse(ss.format_drift(item(), item(extra="<origin>새 어원</origin>")))

    def test_blank_type_in_both_is_not_drift(self):
        self.assertFalse(ss.format_drift(item(type_=""), item(type_="")))

    def test_new_relation_entry_is_not_drift(self):                                       # 개수가 달라지면 실제 편집
        more = ET.fromstring("<item><word_info><lexical_info><type>동의어</type></lexical_info>"
                             "<lexical_info><type></type></lexical_info></word_info></item>")
        self.assertFalse(ss.format_drift(item(), more))

    def test_no_relations_at_all(self):
        plain = ET.fromstring("<item><word_info><word>가</word></word_info></item>")
        self.assertFalse(ss.format_drift(plain, plain))


if __name__ == "__main__":
    unittest.main()
