"""stdict_mcp.py --http 테스트: 토큰 필수, Host 검사, 127.0.0.1 바인드, SDK 클라이언트, 동시 요청.
임시 토큰 파일(테스트용 값)과 빈 포트만 쓰며, 실제 토큰(~/.config/stdict-mcp/token)은 건드리지 않는다.

실행(저장소 루트): .venv/bin/python -m unittest tests.test_mcp_http -v
"""
import asyncio
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import stdict_lookup as sl  # noqa: E402

try:
    import httpx2 as httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False

needs = unittest.skipUnless(HAVE_SDK and sl.DEFAULT_DB.exists(), "mcp SDK(httpx2) 또는 조회용 DB가 없습니다")
SERVER = str(ROOT / "stdict_mcp.py")
TOKEN = "test-token-not-secret-123"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
HDR = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_token(path, content, mode=0o600):
    Path(path).write_text(content)
    os.chmod(path, mode)


def start(port, token_file, hosts=""):
    env = {**os.environ, "STDICT_MCP_TOKEN_FILE": str(token_file), "STDICT_MCP_HOSTS": hosts}
    return subprocess.Popen([sys.executable, SERVER, "--http", "--port", str(port)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")


def wait_open(port, proc):
    for _ in range(100):
        if proc.poll() is not None:
            return False
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def stop(proc):
    proc.terminate()
    try:
        out, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return out, err


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


@needs
class Startup(unittest.TestCase):
    def run_server(self, token_content=None, mode=0o600, env_extra=None):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / "token"
            if token_content is not None:
                write_token(tf, token_content, mode)
            env = {**os.environ, "STDICT_MCP_TOKEN_FILE": str(tf), **(env_extra or {})}
            return subprocess.run([sys.executable, SERVER, "--http", "--port", str(free_port())], env=env,
                                  capture_output=True, text=True, timeout=30, input="")

    def test_refuses_without_token_file(self):
        r = self.run_server(None)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("토큰 파일이 없습니다", r.stderr)
        self.assertIn("openssl rand", r.stderr)                      # 만드는 법을 알려 줌

    def test_refuses_empty_token(self):
        r = self.run_server("\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("비어 있습니다", r.stderr)

    def test_refuses_loose_permissions(self):
        for mode in (0o644, 0o640, 0o604):
            r = self.run_server(TOKEN, mode)
            self.assertNotEqual(r.returncode, 0, oct(mode))
            self.assertIn("권한이 느슨", r.stderr)
            self.assertNotIn(TOKEN, r.stderr)

    def test_token_problem_reported_before_db_problem(self):
        r = self.run_server(None, env_extra={"STDICT_DB": "/nonexistent/x.sqlite"})
        self.assertIn("토큰 파일이 없습니다", r.stderr)

    def test_port_requires_http(self):
        r = subprocess.run([sys.executable, SERVER, "--port", "9999"], capture_output=True, text=True, timeout=30, input="")
        self.assertNotEqual(r.returncode, 0)


@needs
class Running(unittest.IsolatedAsyncioTestCase):
    """서버 하나를 클래스 전체가 공유한다. 허용 Host 에 'mcp-test-host:<포트>' 를 추가해 둔다."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tf = Path(cls.tmp.name) / "token"
        write_token(cls.tf, TOKEN + "\n")
        cls.port = free_port()
        cls.proc = start(cls.port, cls.tf, hosts=f"mcp-test-host:{cls.port}")
        if not wait_open(cls.port, cls.proc):
            out, err = stop(cls.proc)
            raise RuntimeError(f"서버가 시작하지 않음: {err[-300:]}")
        cls.url = f"http://127.0.0.1:{cls.port}/mcp"

    @classmethod
    def tearDownClass(cls):
        _, cls.err = stop(cls.proc)
        cls.tmp.cleanup()

    async def status(self, headers):
        async with httpx.AsyncClient() as c:
            return (await c.post(self.url, json=INIT, headers={**HDR, **headers})).status_code

    async def test_auth_required(self):
        self.assertEqual(await self.status({}), 401)
        self.assertEqual(await self.status({"Authorization": "Bearer wrong"}), 401)
        self.assertEqual(await self.status({"Authorization": TOKEN}), 401)                # Bearer 없이 토큰만
        self.assertEqual(await self.status({"Authorization": f"bearer {TOKEN}"}), 401)    # 대소문자까지 정확히
        self.assertEqual(await self.status({"Authorization": f"Bearer {TOKEN}x"}), 401)   # 토큰 뒤에 글자가 붙음
        self.assertEqual(await self.status({"Authorization": f"Bearer {TOKEN[:-1]}"}), 401)  # 토큰이 한 글자 짧음
        self.assertEqual(await self.status({"Authorization": f"Bearer {TOKEN}"}), 200)

    async def test_401_for_every_method_and_path(self):
        async with httpx.AsyncClient() as c:
            for method in ("GET", "POST", "DELETE", "PUT"):
                for path in ("/mcp", "/", "/anything"):
                    r = await c.request(method, f"http://127.0.0.1:{self.port}{path}", headers=HDR)
                    self.assertEqual(r.status_code, 401, (method, path))
                    self.assertEqual(r.headers.get("www-authenticate"), "Bearer")

    async def test_host_header_check(self):
        auth = {"Authorization": f"Bearer {TOKEN}"}
        self.assertEqual(await self.status({**auth, "Host": f"127.0.0.1:{self.port}"}), 200)
        self.assertEqual(await self.status({**auth, "Host": f"localhost:{self.port}"}), 200)
        self.assertEqual(await self.status({**auth, "Host": f"mcp-test-host:{self.port}"}), 200)   # STDICT_MCP_HOSTS
        for bad in ("evil.example.com", f"mcp-test-host:{self.port + 1}", "mcp-test-host", f"other-host:{self.port}"):
            self.assertIn(await self.status({**auth, "Host": bad}), (400, 403, 421), bad)

    async def test_sdk_client_with_token(self):
        client = httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})
        async with client:
            async with streamable_http_client(self.url, http_client=client) as (r, w, *_):
                async with ClientSession(r, w) as s:
                    init = await s.initialize()
                    self.assertEqual(init.server_info.name, "stdict")
                    tools = {t.name for t in (await s.list_tools()).tools}
                    self.assertEqual(tools, {"lookup_word", "search_dictionary"})
                    res = await s.call_tool("lookup_word", {"word": "연기"})
                    t = "".join(c.text for c in res.content)
                    self.assertFalse(res.is_error)
                    self.assertIn("[연ː기] 장음 포함 2개", t)
                    res = await s.call_tool("search_dictionary", {"query": "演技", "by": "hanja"})
                    self.assertIn("연ː기", "".join(c.text for c in res.content))

    async def test_concurrent_sessions_and_slow_search_does_not_block(self):
        """느린 검색(전체 훑기)이 돌아가는 동안에도 가벼운 조회가 막히지 않아야 한다."""
        async def one(i):
            client = httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})
            async with client:
                async with streamable_http_client(self.url, http_client=client) as (r, w, *_):
                    async with ClientSession(r, w) as s:
                        await s.initialize()
                        if i % 2:     # 장음 기호 없는 발음 검색 = 전체 훑기
                            res = await s.call_tool("search_dictionary", {"query": "연기", "by": "pronunciation"})
                        else:
                            res = await s.call_tool("lookup_word", {"word": ["연기", "풀발", "나무", "가계하다"][i // 2 % 4]})
                        return "".join(c.text for c in res.content), res.is_error
        results = await asyncio.gather(*[one(i) for i in range(8)])
        self.assertTrue(all(t and not err and sl.ATTRIBUTION in t for t, err in results))

    def test_binds_only_to_loopback(self):
        ip = lan_ip()
        if not ip or ip.startswith("127."):
            self.skipTest("LAN IP 없음")
        with self.assertRaises(OSError):
            socket.create_connection((ip, self.port), timeout=1).close()

    def test_zz_log_has_no_secret_and_shows_token_required(self):
        # tearDownClass 보다 먼저 서버 stderr 를 읽을 수 없으므로 별도 서버로 확인한다
        port = free_port()
        proc = start(port, self.tf)
        try:
            self.assertTrue(wait_open(port, proc))
        finally:
            _, err = stop(proc)
        self.assertIn("토큰 필요", err)
        self.assertIn(f"127.0.0.1:{port}", err)
        self.assertNotIn(TOKEN, err)


@needs
class Shutdown(unittest.IsolatedAsyncioTestCase):
    async def test_exits_promptly_even_with_an_open_client_connection(self):
        """MCP 클라이언트가 연결을 열어 둔 채여도 SIGTERM 뒤 몇 초 안에 끝나야 한다(systemctl restart 가 멈추지 않게)."""
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / "token"
            write_token(tf, TOKEN + "\n")
            port = free_port()
            proc = start(port, tf)
            try:
                self.assertTrue(wait_open(port, proc))
                client = httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})
                async with client:
                    async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as (r, w, *_):
                        async with ClientSession(r, w) as s:
                            await s.initialize()
                            proc.terminate()                                   # 연결을 연 채로 종료 신호
                            t0 = time.monotonic()
                            await asyncio.get_running_loop().run_in_executor(None, lambda: proc.wait(timeout=15))
                            self.assertLess(time.monotonic() - t0, 12)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate()


if __name__ == "__main__":
    unittest.main()
