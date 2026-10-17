"""'원본 보기' 링크가 여는, 이 컴퓨터에서만 열리는 작은 웹 서버. 표준 라이브러리만 사용.

조회(lookup_word, search_dictionary) 결과 끝의 링크(http://127.0.0.1:포트/v/키)를 누르면 그때 조회용 DB 에서 읽어
문서형 HTML(stdict_page)로 보여 준다. 조회마다 파일을 만들지 않고 검색 조건(spec)만 메모리에 둔다(키는 조건의 해시, 같은 조건은 같은 링크,
최근 MAX_VIEWS 개만 보관).

  GET /v/<32자리 16진 키>  -> HTML (lang="ko", 시스템 글꼴)
  GET /f/font.otf          -> 나눔바른고딕 옛한글판(설치된 것을 읽을 뿐, 어원의 옛 글자용)
  GET /e/<target_code>     -> 항목 하나의 HTML(화면 안의 '제작-하다' 같은 링크가 연다. 읽기 전용 공개 데이터이고 등록이 필요 없다)

안전: 127.0.0.1 에만 바인드, 포트는 자동, Host 헤더 검사(DNS 재바인딩 방지, 421), GET 만 허용(405), 다른 경로 404, 잘못된 키 400,
Cache-Control: no-store, 로그 출력 없음(stdout 은 MCP 프로토콜 전용). 보여 주는 것은 공개 사전 데이터(읽기 전용)뿐이다.
서버는 MCP 서버 프로세스가 끝나면 같이 끝난다(데몬 스레드).
"""
import hashlib
import http.server
import json
import re
import threading
from collections import OrderedDict

import stdict_lookup as sl
import stdict_page as sp

MAX_VIEWS = 50
HOST = "127.0.0.1"
_PATH = re.compile(r"^/v/([^/]*)$")
_ENTRY = re.compile(r"^/e/([^/]*)$")
_KEY = re.compile(r"^[0-9a-f]{32}$")


class ViewServer:
    def __init__(self):
        self._views = OrderedDict()                 # 키 -> (제목, spec), 오래된 것부터
        self._lock = threading.Lock()
        self.httpd = None
        self.thread = None
        self.port = None

    def start(self):
        """한 번만 띄운다(이미 떠 있으면 아무것도 안 한다)."""
        with self._lock:
            if self.httpd is not None:
                return
            server = self

            class Handler(http.server.BaseHTTPRequestHandler):
                server_version = "stdict-view"

                def log_message(self, *args):       # 로그를 남기지 않는다(stdout 은 MCP 프로토콜 전용)
                    pass

                def _send(self, status, body=b"", ctype="text/plain; charset=utf-8", extra=None):
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    if not (extra and "Cache-Control" in extra):
                        self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    for k, v in (extra or {}).items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self):
                    host = (self.headers.get("Host") or "").lower()
                    if host not in (f"{HOST}:{server.port}", f"localhost:{server.port}"):
                        return self._send(421, b"misdirected request")
                    path = self.path.split("?", 1)[0]
                    if path == sp.FONT_URL:
                        font = sp.find_font()
                        try:
                            data = font.read_bytes() if font else None
                        except OSError:
                            data = None
                        if data is None:
                            return self._send(404, b"not found")
                        return self._send(200, data, "font/otf", {"Cache-Control": "max-age=86400"})
                    m, e = _PATH.match(path), _ENTRY.match(path)
                    if not m and not e:
                        return self._send(404, b"not found")
                    if e:                                # 항목 하나: 화면 안의 링크(표제어 목록)가 연다
                        if not re.fullmatch(r"\d{1,9}", e.group(1)):
                            return self._send(400, b"bad request")
                        view = ("entry", {"kind": "entry", "code": int(e.group(1))})
                    else:
                        if not _KEY.match(m.group(1)):
                            return self._send(400, b"bad request")
                        view = server.get(m.group(1))
                        if view is None:
                            return self._send(404, b"not found")
                    try:
                        html = server.render_spec(view[0], view[1])
                    except sl.StdictError:
                        return self._send(503, "사전 DB를 열 수 없습니다.".encode("utf-8"))
                    if html is None:
                        return self._send(404, "찾지 못했습니다.".encode("utf-8"))
                    return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

                def _deny(self):
                    self._send(405, b"method not allowed", extra={"Allow": "GET"})

                do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _deny

            httpd = http.server.ThreadingHTTPServer((HOST, 0), Handler)
            httpd.daemon_threads = True
            self.httpd, self.port = httpd, httpd.server_address[1]
            self.thread = threading.Thread(target=httpd.serve_forever, name="stdict-view", daemon=True)
            self.thread.start()

    def stop(self):
        with self._lock:
            httpd, self.httpd = self.httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    def add_query(self, title, **spec):
        """검색 조건을 등록하고 링크를 돌려준다. 클릭하면 그때 DB 에서 읽어 그린다. 같은 조건은 같은 링크다.
        spec: kind="lookup"(q, max_entries, examples) 또는 kind="search"(q, by, limit)."""
        self.start()
        key = hashlib.sha256((title + "\0" + json.dumps(spec, sort_keys=True, ensure_ascii=False)).encode("utf-8")).hexdigest()[:32]
        with self._lock:
            self._views.pop(key, None)
            self._views[key] = (title, spec)
            while len(self._views) > MAX_VIEWS:
                self._views.popitem(last=False)
        return f"http://{HOST}:{self.port}/v/{key}"

    def render_spec(self, title, spec):
        """등록된 조건으로 HTML 을 만든다. 이제 찾지 못하면 None. 요청마다 연결을 열고 닫는다(요청 스레드마다 연결이 쌓이지 않게)."""
        d = sl.Dictionary()
        try:
            return sp.render_spec(d, spec)
        except sp.Miss:
            return None
        finally:
            d.close()

    def get(self, key):
        with self._lock:
            return self._views.get(key)


_server = None
_server_lock = threading.Lock()


def get_server():
    """프로세스당 하나의 서버."""
    global _server
    with _server_lock:
        if _server is None:
            _server = ViewServer()
        _server.start()
        return _server
