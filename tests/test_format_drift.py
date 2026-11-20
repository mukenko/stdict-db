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


class FormatDrift(unittest.TestCase):
    def test_type_blanked_is_drift(self):
        self.assertTrue(ss.format_drift(item(), item(type_="")))

    def test_new_site_link_is_drift(self):
        self.assertTrue(ss.format_drift(item(), item(link=NEW_LINK)))

    def test_the_observed_change_is_drift(self):
        self.assertTrue(ss.format_drift(item(), item(type_="", link=NEW_LINK)))

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
