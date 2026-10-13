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


def setUpModule():
    ss.RETRY_WAIT = 0                                           # 재시도 대기를 없앤다


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


class FlakyApi(FakeApi):
    """일시적 이상 응답을 주입하는 가짜 API. mutate(호출 번호(1부터), 페이지, 정상 XML) -> XML."""

    def __init__(self, total, mutate):
        super().__init__(total)
        self.mutate, self.n = mutate, 0

    def call(self, path, params):
        self.n += 1
        page = int(params["start"])
        self.pages.append(page)
        return self.mutate(self.n, page, page_xml(self.total, params))


def empty_xml(page):
    return f"<channel><total>0</total><start>{page}</start><num>100</num></channel>"


class TransientAnomalies(unittest.TestCase):
    """search.do 는 가끔 오류 표시 없이 total=0 이나 어긋난 페이지를 돌려준다(재전송하면 정상)."""

    def collect(self, total, mutate):
        api, hits, warns = FlakyApi(total, mutate), {}, []
        ss.collect_changed(api, dt.date(2026, 10, 1), dt.date(2026, 10, 13), hits, warns)
        return api, hits, warns

    def test_first_total_zero_once_is_rechecked(self):
        api, hits, warns = self.collect(250, lambda n, p, x: empty_xml(p) if n == 1 else x)
        self.assertEqual((len(hits), warns), (250, []))

    def test_total_zero_is_confirmed_a_bounded_number_of_times(self):
        api, hits, warns = self.collect(250, lambda n, p, x: empty_xml(p))
        self.assertEqual((hits, warns), ({}, []))               # 진짜 0건이면 경고 없이 끝
        self.assertEqual(api.n, 1 + ss.ZERO_RECHECKS)

    def test_page_with_different_total_is_retried(self):
        bad = lambda n, p, x: x.replace("<total>250</total>", "<total>251</total>") if (n == 2 and p == 2) else x
        api, hits, warns = self.collect(250, bad)
        self.assertEqual((len(hits), warns), (250, []))

    def test_empty_middle_page_is_retried(self):
        api, hits, warns = self.collect(250, lambda n, p, x: empty_xml(p).replace("<total>0", "<total>250") if (n == 2 and p == 2) else x)
        self.assertEqual((len(hits), warns), (250, []))

    def test_short_last_page_is_retried(self):
        def short(n, p, x):                                      # 마지막 페이지의 마지막 항목을 뺀다
            return x[:x.rfind("<item>")] + "</channel>" if (n == 3 and p == 3) else x
        api, hits, warns = self.collect(250, short)
        self.assertEqual((len(hits), warns), (250, []))

    def test_persistent_anomaly_warns_and_keeps_what_was_read(self):
        api, hits, warns = self.collect(250, lambda n, p, x: empty_xml(p).replace("<total>0", "<total>250") if p == 2 else x)
        self.assertEqual(len(warns), 1)
        self.assertGreaterEqual(len(hits), 100)                  # 읽은 만큼은 갱신 대상에 남긴다
        self.assertEqual(api.n, (1 + ss.RETRIES) * 2)            # 시도마다 1, 2페이지

    def test_normal_run_makes_no_extra_calls(self):
        api, hits, warns = self.collect(250, lambda n, p, x: x)
        self.assertEqual(api.pages, [1, 2, 3])

    def test_supno_search_recovers_from_zero_and_bad_total(self):
        api = FlakyApi(43, lambda n, p, x: empty_xml(p) if n == 1 else x)
        self.assertEqual(ss.supno_search(api, "단어")[0], 43)
        api = FlakyApi(230, lambda n, p, x: x.replace("<total>230</total>", "<total>231</total>") if n == 2 else x)
        total, hits = ss.supno_search(api, "단어")
        self.assertEqual((total, len(hits)), (230, 230))

    def test_supno_search_returns_persistent_mismatch_for_the_issue_check(self):
        api = FlakyApi(230, lambda n, p, x: x.replace("<total>230</total>", "<total>231</total>"))
        total, hits = ss.supno_search(api, "단어")
        self.assertNotEqual(total, len(hits))                    # supno_check 가 total_mismatch 이슈로 기록한다

    def test_supno_search_confirms_zero_total(self):
        api = FlakyApi(0, lambda n, p, x: x)
        self.assertEqual(ss.supno_search(api, "없는말"), (0, []))
        self.assertEqual(api.n, 1 + ss.ZERO_RECHECKS)


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
        self.assertEqual((api.pages, hits), ([1] * (1 + ss.ZERO_RECHECKS), {}))     # 0건은 몇 번 더 확인한 뒤 확정


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

    def test_unescaped_ampersand_is_recovered(self):
        root = sf.parse_xml("<channel><item><target_code>7</target_code><origin>R&B</origin></item></channel>")
        self.assertEqual(root.find("item").findtext("origin"), "R&B")

    def test_known_entities_are_not_double_escaped(self):
        root = sf.parse_xml("<channel><item><origin>a &amp; b &lt;일&gt; &#38; <일>c</origin></item></channel>")
        self.assertEqual(root.find("item").findtext("origin"), "a & b <일> & <일>c")

    def test_cdata_is_preserved_when_the_rest_needs_repair(self):
        link = "https://example.org/view?word_no=1&searchKeywordTo=3"
        root = sf.parse_xml(f"<channel><item><link><![CDATA[{link}]]></link><origin>R&B <일>x</origin></item></channel>")
        self.assertEqual(root.find("item").findtext("link"), link)                  # CDATA 안의 & 는 그대로
        self.assertEqual(root.find("item").findtext("origin"), "R&B <일>x")

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
        self.assertEqual((ss.RETRIES, ss.ZERO_RECHECKS), (3, 2))


if __name__ == "__main__":
    unittest.main()
