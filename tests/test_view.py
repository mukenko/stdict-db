"""'원본 보기' 링크: 결과 끝줄 규칙(stdict_mcp._with_footer), 링크 서버(stdict_view), 문서형 화면(stdict_page). 실제 data/stdict.sqlite 를 읽기 전용으로 쓴다.

실행(저장소 루트): python3 -m unittest tests.test_view -v
"""
import http.client
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import stdict_lookup as sl  # noqa: E402
import stdict_mcp as sm  # noqa: E402
import stdict_page as sp  # noqa: E402
import stdict_view as sv  # noqa: E402

DB_OK = sl.DEFAULT_DB.exists()
needs_db = unittest.skipUnless(DB_OK, "조회용 DB가 없습니다")
LOOKUP = {"kind": "lookup", "q": "연기", "max_entries": 12, "examples": False}
LINK = re.compile(r"^원본 보기: (http://127\.0\.0\.1:(\d+)/v/([0-9a-f]{32}))$", re.M)


def get(port, path, host=None, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        c.putrequest(method, path, skip_host=True)
        c.putheader("Host", host or f"127.0.0.1:{port}")
        c.endheaders()
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        c.close()


class Footer(unittest.TestCase):
    """_with_footer: 링크 줄 → 출처 줄 순서, 찾았을 때만, 원격에서는 없음."""

    def setUp(self):
        self.addCleanup(setattr, sm, "HTTP_MODE", sm.HTTP_MODE)
        self.addCleanup(self.restore_env, os.environ.get("STDICT_VIEW_LINK"))
        os.environ.pop("STDICT_VIEW_LINK", None)
        sm.HTTP_MODE = False

    @staticmethod
    def restore_env(old):
        if old is None:
            os.environ.pop("STDICT_VIEW_LINK", None)
        else:
            os.environ["STDICT_VIEW_LINK"] = old

    def text(self):
        return "결과 글\n" + sl.ATTRIBUTION

    def test_footer_link_rules(self):
        lines = sm._with_footer(self.text(), "연기", LOOKUP).split("\n")
        self.assertEqual(lines[0], "결과 글")
        self.assertTrue(LINK.match(lines[1]), lines[1])          # 원본 보기 줄이 출처 줄 앞에
        self.assertEqual(lines[2], sl.ATTRIBUTION)
        self.assertEqual(len(lines), 3)
        self.assertEqual(sm._with_footer(self.text(), "연기", LOOKUP, found=False), self.text())   # 못 찾았으면 링크 없음
        self.assertEqual(sm._with_footer(self.text(), "연기"), self.text())                        # 검색 조건이 없으면 링크 없음
        sm.HTTP_MODE = True                                                                      # 원격 서버: 링크 없음
        self.assertEqual(sm._with_footer(self.text(), "연기", LOOKUP), self.text())
        sm.HTTP_MODE = False
        for value in ("0", "off", "false", "no"):                                                # 환경변수로 끄기
            os.environ["STDICT_VIEW_LINK"] = value
            self.assertEqual(sm._with_footer(self.text(), "연기", LOOKUP), self.text(), value)
        os.environ["STDICT_VIEW_LINK"] = "1"
        self.assertIn("원본 보기:", sm._with_footer(self.text(), "연기", LOOKUP))

    def test_server_failure_still_returns_the_result(self):
        with mock.patch.object(sv, "get_server", side_effect=OSError("port")):
            self.assertEqual(sm._with_footer(self.text(), "연기", LOOKUP), self.text())

    def test_same_condition_same_link_different_condition_different_link(self):
        a = LINK.search(sm._with_footer(self.text(), "연기", LOOKUP)).group(1)
        b = LINK.search(sm._with_footer(self.text(), "연기", dict(LOOKUP))).group(1)
        c = LINK.search(sm._with_footer(self.text(), "연기", {**LOOKUP, "examples": True})).group(1)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    @needs_db
    def test_link_always_present_for_found_entries(self):
        for word in ("연기", "사과", "먹다"):
            out = sm.lookup_word(word)
            self.assertRegex(out, LINK, word)
            lines = out.split("\n")
            self.assertTrue(lines[-3].startswith("원본 보기: "), word)       # 링크 → 출처 → 알림
            self.assertEqual(lines[-2], sl.ATTRIBUTION)
            self.assertTrue(lines[-1].startswith("[Note to assistant"))
        for by, q in (("pronunciation", "연:기"), ("hanja", "演技"), ("form", "가계해"), ("definition", "배우가 배역")):
            self.assertRegex(sm.search_dictionary(q, by), LINK, by)

    @needs_db
    def test_no_link_when_nothing_found(self):
        self.assertNotIn("원본 보기", sm.lookup_word("가나다라마바사없는말"))
        self.assertNotIn("원본 보기", sm.search_dictionary("가나다라마바사없는말", "hanja"))

    @needs_db
    def test_remote_mode_returns_the_same_result_without_link(self):
        local = sm.lookup_word("연기")
        sm.HTTP_MODE = True
        remote = sm.lookup_word("연기")
        self.assertNotIn("원본 보기", remote)
        self.assertEqual(remote, "\n".join(ln for ln in local.split("\n") if not ln.startswith("원본 보기:")))

    def test_link_rule_is_in_instructions_and_tool_descriptions_not_in_results(self):
        self.assertIn('"원본 보기:"', sm.INSTRUCTIONS)
        self.assertIn("[원본 보기](URL)", sm.INSTRUCTIONS)
        for fn in sm.TOOLS:
            self.assertIn("[원본 보기](URL)", fn.__doc__, fn.__name__)
        self.assertNotIn("Markdown", sm._with_footer(self.text(), "연기", LOOKUP))     # 결과에는 사실과 주소만


@needs_db
class Server(unittest.TestCase):
    def setUp(self):
        self.srv = sv.ViewServer()
        self.srv.start()
        self.addCleanup(self.srv.stop)
        self.port = self.srv.port

    def path(self, **spec):
        return "/v/" + self.srv.add_query(spec.get("q", "연기"), **{**LOOKUP, **spec}).rsplit("/", 1)[1]

    def test_binds_to_loopback_only_in_a_daemon_thread(self):
        self.assertEqual(self.srv.httpd.server_address[0], "127.0.0.1")
        self.assertTrue(self.srv.thread.daemon)

    def test_page_is_served(self):
        status, headers, body = get(self.port, self.path())
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        page = body.decode("utf-8")
        self.assertIn('lang="ko"', page)
        self.assertEqual(page.count("<article"), 12)               # max_entries 가 작아도 화면에는 전부
        self.assertIn("[연ː기]", page)                             # 장음 표시 그대로
        self.assertIn(sl.ATTRIBUTION, page)

    def test_page_is_not_truncated_when_chat_result_is_(self):
        status, _, body = get(self.port, self.path(max_entries=2))
        self.assertEqual(body.decode("utf-8").count("<article"), 12)
        self.assertEqual(sm._dict().lookup("연기", 2)["shown"], 2)   # 채팅 결과는 2개

    def test_examples_shown_in_full_on_the_page(self):
        d = sl.Dictionary()
        self.addCleanup(d.close)
        res = d.lookup("사과", 12, True, full=True)
        n = sum(len(s["examples"]) for e in res["entries"] for p in e["pos"] for s in p["senses"])
        self.assertGreater(n, 3)
        page = get(self.port, self.path(q="사과"))[2].decode("utf-8")
        self.assertEqual(page.count('<ul class="ex">'), sum(1 for e in res["entries"] for p in e["pos"] for s in p["senses"] if s["examples"]))

    def test_each_entry_button_opens_its_own_stdict_entry(self):
        """항목마다 누리집 주소(word_no)가 그 항목의 target_code 와 같다."""
        page = get(self.port, self.path())[2].decode("utf-8")
        d = sl.Dictionary()
        self.addCleanup(d.close)
        codes = [e["target_code"] for e in d.lookup("연기", 12)["entries"]]
        hrefs = re.findall(r'<article id="e(\d+)">.*?<a class="site" href="([^"]+)"', page)
        self.assertEqual([int(c) for c, _ in hrefs], codes)
        for code, href in hrefs:                                   # HTML 속성이므로 & 는 &amp; 로 이스케이프됨
            self.assertEqual(href, f"https://stdict.korean.go.kr/search/searchView.do?word_no={code}&amp;searchKeywordTo=3")
        self.assertEqual(sp.original_url(236214), "https://stdict.korean.go.kr/search/searchView.do?word_no=236214&searchKeywordTo=3")
        self.assertEqual(page.count('rel="noopener noreferrer"'), 12)
        self.assertEqual(page.count('target="_blank"'), 12)

    def test_conjugation_pronunciation_is_shown_in_page_and_text(self):
        """활용형의 발음(제작² 제작만[제ː장만])은 DB 에 있으므로 화면과 텍스트 출력에 모두 나온다."""
        self.assertIn("제작만[제ː장만]", sl.format_lookup(sl.Dictionary().lookup("제작")))
        self.assertIn("제작만[제ː장만]", get(self.port, self.path(q="제작"))[2].decode("utf-8"))

    def test_cited_examples_are_shown_with_their_source(self):
        """제작하다²: 출전 있는 용례 2개(김동인·이병주)도 출전과 함께 나온다."""
        d = sl.Dictionary()
        self.addCleanup(d.close)
        code = [e["target_code"] for e in d.lookup("제작하다", 12)["entries"] if e["sup_no"] == 2][0]
        page = get(self.port, self.path(q="제작하다"))[2].decode("utf-8")
        art = page.split(f'<article id="e{code}">')[1].split("</article>")[0]
        self.assertEqual(art.count("<li>"), 5)                           # 용례 5(뜻 <li value=…> 은 제외)
        self.assertIn("<cite>≪김동인, 대수양≫</cite>", art)
        self.assertIn("≪김동인, 대수양≫", sl.format_lookup(d.lookup("제작하다", 12, True)))     # 채팅 결과에도 같은 용례

    def test_search_page(self):
        status, _, body = get(self.port, self.path(kind="search", q="연:기", by="pronunciation", limit=10))
        self.assertEqual(status, 200)
        page = body.decode("utf-8")
        self.assertIn("演技", page)
        self.assertNotIn("(聯騎)", page)                           # 장음 [연ː기] 만

    def test_bad_requests(self):
        self.assertEqual(get(self.port, "/v/zzzz")[0], 400)               # 잘못된 키
        self.assertEqual(get(self.port, "/v/" + "0" * 32)[0], 404)        # 없는 항목
        self.assertEqual(get(self.port, "/")[0], 404)
        self.assertEqual(get(self.port, "/f/font.otf")[0], 404)
        self.assertEqual(get(self.port, "/v/a/b")[0], 404)
        status, headers, _ = get(self.port, self.path(), method="POST")
        self.assertEqual((status, headers.get("Allow")), (405, "GET"))
        self.assertEqual(get(self.port, self.path(), method="DELETE")[0], 405)
        self.assertEqual(get(self.port, self.path(), host="evil.example.com")[0], 421)
        self.assertEqual(get(self.port, self.path(), host=f"localhost:{self.port}")[0], 200)

    def test_condition_that_no_longer_matches_is_404(self):
        self.assertEqual(get(self.port, self.path(q="가나다라마바사없는말"))[0], 404)

    def test_only_recent_views_are_kept(self):
        first = self.srv.add_query("a0", **{**LOOKUP, "q": "연기"})
        for i in range(sv.MAX_VIEWS + 1):
            self.srv.add_query(f"w{i}", **{**LOOKUP, "q": f"w{i}"})
        self.assertEqual(get(self.port, "/v/" + first.rsplit("/", 1)[1])[0], 404)
        self.assertLessEqual(len(self.srv._views), sv.MAX_VIEWS)


class Escaping(unittest.TestCase):
    def test_text_from_db_is_only_escaped(self):
        e = {"target_code": 1, "word": "<b>x</b>", "sup_no": 0, "unit": "단어", "type": "고유어", "original": "<i>",
             "pronunciations": ["a&b"], "conjugations": [], "etymology": "<script>alert(1)</script>", "allomorph": None,
             "norm_info": [], "relations": [{"type": "t", "words": ["<u>"], "total": 1}],
             "pos": [{"pos": "명사", "patterns": [], "more_senses": 0, "senses": [
                 {"type": "일반어", "cats": [], "patterns": [], "definition": "<img src=x onerror=1>", "scientific_name": None,
                  "examples": ["\"quote\" & <tag>"], "relations": []}]}]}
        out = sp.entry_html(e)
        for raw in ("<script>", "<img", "<b>x", "<i>", "<u>", "<tag>"):
            self.assertNotIn(raw, out)
        self.assertIn("&lt;script&gt;", out)


if __name__ == "__main__":
    unittest.main()
