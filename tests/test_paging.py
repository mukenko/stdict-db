"""search.do 페이지 넘김 테스트. start 는 항목 번호가 아니라 페이지 번호다(2026-10-13 실측).
가짜 API 는 실제처럼 start=2,num=100 에 101~200번째 항목을 돌려주고, start=101 처럼 마지막 페이지를 넘으면 빈 결과를 돌려준다.
이 테스트는 예전 코드(start += 100)에서는 100건에서 잘려 실패한다.

실행(저장소 루트): python3 -m unittest tests.test_paging -v
"""
import datetime as dt
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stdict_fetch as sf  # noqa: E402
import stdict_sync as ss  # noqa: E402


def page_xml(total, params):
    page, num = int(params["start"]), int(params["num"])
    first = (page - 1) * num
    items = "".join(
        f"<item><target_code>{1000 + i}</target_code><word>w{i}</word><sup_no>0</sup_no><pos>명사</pos></item>"
        for i in range(first, min(first + num, total)))
    return f"<channel><total>{total}</total><start>{page}</start><num>{num}</num>{items}</channel>"


class FakeApi:
    """search.do 만 흉내 내는 클라이언트. 호출된 start 값을 기록한다."""

    def __init__(self, total):
        self.total, self.pages = total, []

    def call(self, path, params):
        assert path == "/api/search.do"
        self.pages.append(int(params["start"]))
        return page_xml(self.total, params)


class CollectChanged(unittest.TestCase):
    def collect(self, total):
        api, hits, warns = FakeApi(total), {}, []
        ss.collect_changed(api, dt.date(2026, 10, 1), dt.date(2026, 10, 13), hits, warns)
        return api, hits, warns

    def test_more_than_one_page_is_fully_collected(self):
        for total in (1, 99, 100, 101, 250, 4467):
            api, hits, warns = self.collect(total)
            self.assertEqual(len(hits), total, total)
            self.assertEqual(warns, [])

    def test_pages_are_page_numbers_not_item_offsets(self):
        api, hits, _ = self.collect(250)
        self.assertEqual(api.pages, [1, 2, 3])                  # 예전 코드는 [1, 101] 을 보냈다

    def test_no_empty_extra_request(self):
        api, _, _ = self.collect(200)
        self.assertEqual(api.pages, [1, 2])

    def test_zero_results(self):
        api, hits, _ = self.collect(0)
        self.assertEqual((api.pages, hits), ([1], {}))


class SupnoSearch(unittest.TestCase):
    def test_collects_all_pages(self):
        for total in (3, 43, 100, 230):
            api = FakeApi(total)
            got_total, hits = ss.supno_search(api, "단어")
            self.assertEqual((got_total, len(hits)), (total, total), total)

    def test_pages_are_page_numbers(self):
        api = FakeApi(230)
        ss.supno_search(api, "단어")
        self.assertEqual(api.pages, [1, 2, 3])


class FetchSearchWord(unittest.TestCase):
    def test_search_word_collects_all_pages_and_stores_hits(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE search_hits(target_code INTEGER PRIMARY KEY, word TEXT, sup_no TEXT, origin TEXT, pos TEXT, seen_at TEXT)")

        class Client(sf.Client):
            pages = []

            def call(self, path, params):
                self.pages.append(int(params["start"]))
                return page_xml(250, params)

        c = Client("k", db, 1000, 0)
        hits = c.search_word("가-나", "exact")
        self.assertEqual(len(hits), 250)
        self.assertEqual(Client.pages, [1, 2, 3])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM search_hits").fetchone()[0], 250)


class ParseXml(unittest.TestCase):
    """search.do 는 원어 표기 안의 '<일>' 을 이스케이프하지 않는다(고 4,616건 구간의 43페이지에서 확인)."""
    BROKEN = ("<channel><total>2</total>"
              "<item><target_code>1</target_code><word>고치</word><origin><일>Kôchi[高知]縣</origin></item>"
              "<item><target_code>2</target_code><word>나무</word><origin>木</origin></item></channel>")

    def test_unescaped_hangul_marker_in_origin_is_recovered(self):
        root = sf.parse_xml(self.BROKEN)
        origins = [i.findtext("origin") for i in root.findall("item")]
        self.assertEqual(origins, ["<일>Kôchi[高知]縣", "木"])
        self.assertEqual([i.findtext("target_code") for i in root.findall("item")], ["1", "2"])

    def test_valid_xml_is_untouched(self):
        root = sf.parse_xml("<channel><total>1</total><item><origin>&lt;일&gt;x</origin></item></channel>")
        self.assertEqual(root.find("item").findtext("origin"), "<일>x")

    def test_cdata_with_hangul_is_untouched(self):
        root = sf.parse_xml("<channel><item><word><![CDATA[<일>가]]></word></item></channel>")
        self.assertEqual(root.find("item").findtext("word"), "<일>가")

    def test_really_broken_xml_still_raises(self):
        with self.assertRaises(sf.ApiError):
            sf.parse_xml("<channel><total>1</total><item>")

    def test_search_helper_survives_the_broken_page(self):
        class Api:
            def call(self, path, params):
                return ParseXml.BROKEN
        total, items = ss.search(Api(), "고", "start", 1, page=43, num=100)
        self.assertEqual(total, 2)
        self.assertEqual(items[0]["origin"], "<일>Kôchi[高知]縣")


class Constants(unittest.TestCase):
    def test_limits(self):
        self.assertEqual((ss.PAGE_SIZE, ss.MAX_PAGE, ss.SEARCH_MAX), (100, 1000, 100000))


if __name__ == "__main__":
    unittest.main()
