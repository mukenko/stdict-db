"""stdict_mcp.py(stdio) 테스트. 실제 data/stdict.sqlite(공개 사전 데이터)를 읽기 전용으로 쓴다.

실행(저장소 루트): .venv/bin/python -m unittest tests.test_mcp_stdio -v
mcp SDK 가 없는 파이썬(시스템 python3)에서는 SDK 가 필요한 테스트를 건너뛴다.
"""
import contextlib
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import stdict_lookup as sl  # noqa: E402

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False

DB_OK = sl.DEFAULT_DB.exists()
needs = unittest.skipUnless(HAVE_SDK and DB_OK, "mcp SDK 또는 조회용 DB가 없습니다")
SERVER = str(ROOT / "stdict_mcp.py")


def text(res):
    return "".join(c.text for c in res.content if getattr(c, "type", "") == "text")


@contextlib.asynccontextmanager
async def session():
    """테스트마다 서버를 띄운다. anyio 취소 범위는 같은 태스크에서 열고 닫아야 하므로 setUp/tearDown 으로 나누지 않는다."""
    params = StdioServerParameters(command=sys.executable, args=[SERVER])
    with open(os.devnull, "w") as err:
        async with stdio_client(params, errlog=err) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                yield s, init


@needs
class Protocol(unittest.IsolatedAsyncioTestCase):
    async def test_server_info_and_instructions(self):
        async with session() as (s, init):
            self.assertEqual(init.server_info.name, "stdict")
            self.assertIn("homonyms", init.instructions)
            self.assertIn("ː", init.instructions)

    async def test_extra_explanation_is_allowed_only_when_labeled(self):
        """사전 사실은 그대로 인용하게 하되, 사전 밖 설명은 표시하면 덧붙일 수 있다."""
        async with session() as (s, init):
            self.assertIn("[사전 외 설명]", init.instructions)
            self.assertIn("never invent", init.instructions.lower())
            out = text(await s.call_tool("lookup_word", {"word": "연기"}))
            self.assertIn("[사전 외 설명]", out.split("\n")[-1])           # 알림에도 같은 표시 방법
            self.assertNotIn("answer only from the entries", out)         # 예전의 전면 금지 문구는 없다

    async def test_two_tools_with_descriptions(self):
        async with session() as (s, _):
            tools = {t.name: t for t in (await s.list_tools()).tools}
            self.assertEqual(set(tools), {"lookup_word", "search_dictionary"})
            for t in tools.values():
                self.assertTrue(t.description)
            by = tools["search_dictionary"].input_schema["properties"]["by"]
            self.assertEqual(set(by["enum"]), set(sl.SEARCH_KINDS))

    async def test_lookup_word_golden(self):
        async with session() as (s, _):
            res = await s.call_tool("lookup_word", {"word": "연기", "max_entries": 12})
            t = text(res)
            self.assertFalse(res.is_error)
            self.assertIn("[연ː기]", t)
            self.assertIn("(演技)", t)
            self.assertIn(sl.LONG_NOTE, t)
            self.assertIn(sl.ATTRIBUTION, t)
            self.assertTrue(t.rstrip().endswith("]"))                         # 끝의 모델용 알림
            self.assertIn("never invent entries, readings, other-language words or example sentences", t)

    async def test_search_by_pronunciation(self):
        async with session() as (s, _):
            res = await s.call_tool("search_dictionary", {"query": "연:기", "by": "pronunciation"})
            self.assertFalse(res.is_error)
            self.assertIn("演技", text(res))

    async def test_all_search_kinds_work(self):
        async with session() as (s, _):
            for q, by in (("연기", "pronunciation"), ("演技", "hanja"), ("가계해", "form"),
                          ("목질로 된 여러해살이", "definition")):
                res = await s.call_tool("search_dictionary", {"query": q, "by": by})
                self.assertFalse(res.is_error, by)
                self.assertIn(sl.ATTRIBUTION, text(res))

    async def test_bad_arguments_do_not_kill_server(self):
        async with session() as (s, _):
            res = await s.call_tool("search_dictionary", {"query": "연기", "by": "nonsense"})
            self.assertTrue(res.is_error)
            res = await s.call_tool("lookup_word", {"word": "   "})             # 도구가 안내 문장으로 돌려줌
            self.assertFalse(res.is_error)
            self.assertIn("입력", text(res))
            res = await s.call_tool("search_dictionary", {"query": "식", "by": "definition"})
            self.assertIn("2글자", text(res))
            self.assertIn("lookup_word", text(res))                  # 대신 쓸 도구를 안내
            res = await s.call_tool("lookup_word", {"word": "풀발"})              # 이후에도 정상
            self.assertIn("[풀빨]", text(res))

    async def test_huge_limits_are_clamped(self):
        async with session() as (s, _):
            res = await s.call_tool("lookup_word", {"word": "가", "max_entries": 100000})
            self.assertFalse(res.is_error)
            self.assertLess(len(text(res)), 60000)

    async def test_special_characters(self):
        async with session() as (s, _):
            for w in ('"', "%", "'; DROP TABLE entries;--", "a*b", "NEAR(", "\u0000"):
                res = await s.call_tool("lookup_word", {"word": w})
                self.assertFalse(res.is_error, repr(w))


@needs
class Process(unittest.TestCase):
    def test_stdout_contains_only_jsonrpc(self):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "lookup_word", "arguments": {"word": "연기"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "search_dictionary", "arguments": {"query": "연ː기", "by": "pronunciation"}}},
        ]
        proc = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        lines = []
        for m in msgs:
            proc.stdin.write(json.dumps(m) + "\n")
            proc.stdin.flush()
            if "id" in m:
                lines.append(proc.stdout.readline().strip())
        time.sleep(0.3)
        proc.stdin.close()
        lines += [l for l in proc.stdout.read().splitlines() if l.strip()]
        proc.wait(timeout=30)
        proc.stdout.close()
        parsed = [json.loads(l) for l in lines]                                   # JSON 이 아닌 줄이 있으면 여기서 실패
        self.assertTrue(all(d.get("jsonrpc") == "2.0" for d in parsed))
        self.assertEqual(sorted(d["id"] for d in parsed if "id" in d), [1, 2, 3])

    def test_refuses_to_start_without_db_and_says_why(self):
        r = subprocess.run([sys.executable, SERVER], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "STDICT_DB": "/nonexistent/stdict.sqlite"}, input="")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("사전 DB를 열 수 없습니다", r.stderr)
        self.assertEqual(r.stdout, "")                                             # stdout 은 깨끗해야 함


class CallMode(unittest.TestCase):
    """--call 은 SDK 없이도 동작해야 한다(시스템 python 에서 확인용)."""

    @unittest.skipUnless(DB_OK, "조회용 DB가 없습니다")
    def test_call_lookup_and_search(self):
        r = subprocess.run([sys.executable, SERVER, "--call", "lookup_word", "풀발"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[풀빨]", r.stdout)
        r = subprocess.run([sys.executable, SERVER, "--call", "search_dictionary", "演技", "hanja"],
                           capture_output=True, text=True, timeout=60)
        self.assertIn("연ː기", r.stdout)

    def test_call_unknown_tool(self):
        r = subprocess.run([sys.executable, SERVER, "--call", "nope"], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
