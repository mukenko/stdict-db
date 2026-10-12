#!/usr/bin/env python3
"""표준국어대사전 MCP 서버. 조회는 모두 stdict_lookup.py(조회 코어)가 하고, 이 파일은 MCP 도구로 감싸기만 한다.

  .venv/bin/python stdict_mcp.py                      # stdio (LM Studio, Claude 데스크톱이 프로세스를 띄움)
  .venv/bin/python stdict_mcp.py --http [--port 8766] # Streamable HTTP (127.0.0.1 에만 바인드, Bearer 토큰 필수)
  python3 stdict_mcp.py --call lookup_word 연기        # SDK 없이 도구 결과 확인(시스템 python 도 가능)
  python3 stdict_mcp.py --call search_dictionary 연:기 pronunciation

읽기 전용 도구 2개. DB 경로는 환경변수 STDICT_DB(기본 data/stdict.sqlite).
모델에 전달하는 문구(지침·도구 설명)는 영어다: 작은 로컬 모델이 더 안정적으로 따르고 토큰이 덜 든다(personal-memory 와 같은 방침).
stdout 은 MCP 프로토콜 전용이므로 도구 실행 중의 출력은 stderr 로 돌린다. 도구가 실패해도 서버는 죽지 않고 오류 문장을 돌려준다.

HTTP 모드(서버에서 상시 실행하는 용도. 앞에 tailscale serve 같은 프록시를 두어 신뢰하는 네트워크에만 노출):
  - 바인드는 127.0.0.1 로 고정(옵션 없음). 외부 노출은 프록시(예: tailscale serve)가 맡는다.
  - 토큰 파일(STDICT_MCP_TOKEN_FILE, 기본 ~/.config/stdict-mcp/token)이 **반드시** 있어야 시작한다. 없거나 비어 있거나
    권한이 600 보다 느슨하면 시작하지 않는다. 요청마다 'Authorization: Bearer <토큰>' 을 요구하며 토큰은 어디에도 출력하지 않는다.
    토큰 파일은 프로젝트 폴더 밖에 둔다(폴더를 통째로 동기화·배포할 때 함께 복사되거나 지워지지 않도록).
  - DNS rebinding 보호는 켠 채, 허용 Host 는 127.0.0.1/localhost 와 환경변수 STDICT_MCP_HOSTS(쉼표로 구분, 예: myhost:8766)이다. 프록시는 클라이언트가 보낸 Host 를 그대로 넘기므로 접속에 쓰는 이름을 넣는다.
  - 도구는 스레드로 실행해 느린 검색(전체 훑기)이 다른 요청을 막지 않는다.
"""
import argparse
import asyncio
import contextlib
import functools
import hmac
import inspect
import os
import sys
from pathlib import Path
from typing import Literal

import stdict_lookup as sl

SERVER_NAME = "stdict"
HTTP_HOST = "127.0.0.1"                 # 바인드 주소는 고정한다(외부에 직접 열지 않는다)
HTTP_DEFAULT_PORT = 8766
TOKEN_FILE = Path(os.environ.get("STDICT_MCP_TOKEN_FILE") or Path.home() / ".config" / "stdict-mcp" / "token")

INSTRUCTIONS = (
    "Standard Korean Language Dictionary (표준국어대사전, National Institute of Korean Language). "
    "Use these tools to verify Korean spelling, word forms, pronunciation (long vowels are marked with ː), "
    "Hanja/original language and meanings instead of answering from memory. "
    "One spelling can have several homonyms (shown with superscript numbers, e.g. 연기¹ 연기²); read ALL of them and "
    "choose by context, and do not mix up their pronunciations (e.g. 연ː기 with a long vowel vs 연기). "
    "Base your answer ONLY on the tool output: do not add entries, readings, other-language words or examples from "
    "memory, copy pronunciations exactly as shown (keep ː), and when the output has a pronunciation summary, use it "
    "to separate long-vowel words from short-vowel ones. "
    "If a tool says a word is not an entry, say so; do not invent dictionary content. "
    "Answer in the user's language (Korean unless the user writes otherwise) and mention that the source is the "
    "Standard Korean Language Dictionary."
)

# 모든 조회 결과 끝에 붙이는 알림(personal-memory 의 NOTICE 와 같은 방식). 모델에 대한 부탁이며 강제는 아니다.
NOTICE = ("[Note to assistant: answer only from the entries above. Do not add example sentences, other words, "
          "other-language words or readings that are not listed; quote pronunciations exactly (keep ː); "
          "state that the source is the Standard Korean Language Dictionary.]")

_dictionary = None


def _dict():
    global _dictionary
    if _dictionary is None:
        _dictionary = sl.Dictionary()
    return _dictionary


def _safe(fn):
    """도구 실패가 서버를 죽이지 않게 하고, 실행 중 stdout 출력은 stderr 로 돌린다(stdio 프로토콜 보호)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            with contextlib.redirect_stdout(sys.stderr):
                return fn(*args, **kwargs)
        except sl.StdictError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            print(f"[stdict MCP] {fn.__name__} 오류: {type(e).__name__}", file=sys.stderr)
            return "사전 조회 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    return wrapper


@_safe
def lookup_word(word: str, max_entries: int = 12, include_examples: bool = False) -> str:
    """Look up a Korean word in the Standard Korean Language Dictionary (표준국어대사전).
    Returns ALL homonyms of the spelling, each with its superscript number, Hanja/original language, pronunciation
    (ː marks a long vowel), part of speech and definitions. When the homonyms are pronounced differently, the result
    starts with a summary that groups them by pronunciation; answer from that summary and the listed entries only. If the word is not an entry but is a conjugated or
    abbreviated form (e.g. 먹어 -> 먹다), the base word is shown; otherwise similar entries are suggested.
    Use this for spelling, pronunciation (including long vs short vowels), meaning, Hanja and homonym checks.
    Hyphens and spaces typed by the user are handled; do not add them yourself.
    Args: word = the Korean word or phrase, e.g. "연기"; max_entries = how many homonyms to show (default 12);
    include_examples = also show usage examples that have no cited source."""
    return sl.format_lookup(_dict().lookup(word, max_entries, include_examples)) + "\n" + NOTICE


@_safe
def search_dictionary(query: str, by: Literal["pronunciation", "hanja", "form", "definition"],
                      limit: int = 10) -> str:
    """Reverse search in the Standard Korean Language Dictionary (표준국어대사전).
    by="pronunciation": find words pronounced like the query; write the long vowel mark ː (or a colon :) to match
    only long vowels, e.g. "연ː기"; without it, both long and short matches are returned.
    by="hanja": find words containing the given Hanja, e.g. "演技".
    by="form": find the base word of a conjugated or abbreviated form listed in the dictionary, e.g. "가계해".
    by="definition": find words whose definition contains the phrase (2 or more characters; 3 or more is exact).
    For checking a single known word, use lookup_word instead.
    Args: query = the text to search; by = the search kind above; limit = maximum results (default 10)."""
    return sl.format_search(_dict().search(query, by, limit)) + "\n" + NOTICE


TOOLS = (lookup_word, search_dictionary)


def _check_db():
    """시작할 때 DB 를 한 번 열어 본다. 안 열리면 이유를 stderr 에 남기고 바로 끝낸다(클라이언트에는 연결 끊김으로만 보임)."""
    try:
        meta = _dict().meta()
    except sl.StdictError:
        sys.exit(f"[stdict MCP] 사전 DB를 열 수 없습니다: {sl.DEFAULT_DB} (STDICT_DB 환경변수 또는 data/stdict.sqlite 확인)")
    return meta


def _cli_call(argv):
    """--call 도구이름 인자...: SDK 없이 도구 결과를 확인한다."""
    names = {t.__name__: t for t in TOOLS}
    if not argv or argv[0] not in names:
        sys.exit(f"사용법: --call {{{'|'.join(names)}}} 인자...  (인자는 도구 설명의 순서대로)")
    fn = names[argv[0]]
    params = list(inspect.signature(fn).parameters.values())
    args = []
    for p, raw in zip(params, argv[1:]):
        args.append(int(raw) if p.annotation is int else (raw.lower() in ("1", "true", "yes") if p.annotation is bool else raw))
    print(fn(*args))


def _load_token():
    """HTTP 는 토큰이 필수다. 없거나, 비어 있거나, 다른 사용자가 읽을 수 있으면 시작하지 않는다(ssh 키와 같은 규칙)."""
    try:
        st = TOKEN_FILE.stat()
    except FileNotFoundError:
        sys.exit(f"[stdict MCP] 토큰 파일이 없습니다: {TOKEN_FILE}\n"
                 "  만드는 법: mkdir -p ~/.config/stdict-mcp && chmod 700 ~/.config/stdict-mcp && "
                 "(umask 077; openssl rand -hex 24 > ~/.config/stdict-mcp/token)")
    if st.st_mode & 0o077:
        sys.exit(f"[stdict MCP] 토큰 파일의 권한이 느슨합니다({oct(st.st_mode & 0o777)}): {TOKEN_FILE}  → chmod 600")
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        sys.exit(f"[stdict MCP] 토큰 파일이 비어 있습니다: {TOKEN_FILE}")
    return token


def _allowed_hosts():
    extra = [h.strip() for h in os.environ.get("STDICT_MCP_HOSTS", "").split(",") if h.strip()]
    return ["127.0.0.1:*", "localhost:*", "[::1]:*"] + extra


def _http_security():
    """DNS rebinding 보호는 켠 채 허용 Host 만 정한다."""
    from mcp.server.transport_security import TransportSecuritySettings
    hosts = _allowed_hosts()
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                     allowed_origins=[f"http://{h}" for h in hosts])


class _BearerAuth:
    """모든 HTTP 요청에 'Authorization: Bearer <토큰>' 을 요구하는 ASGI 래퍼(lifespan 등 다른 scope 는 그대로 통과)"""

    def __init__(self, app, token):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


def _threaded(fn):
    """동기 도구를 스레드에서 실행한다. 조회 코어는 스레드마다 연결을 따로 쓰므로 안전하다."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)
    return wrapper


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--call":
        return _cli_call(argv[1:])
    ap = argparse.ArgumentParser(prog="stdict_mcp.py", description="표준국어대사전 MCP 서버(기본 stdio). 도구 확인은 --call 도구 인자...")
    ap.add_argument("--http", action="store_true", help=f"Streamable HTTP 로 실행({HTTP_HOST} 에만 바인드, 토큰 필수)")
    ap.add_argument("--port", type=int, help=f"--http 의 포트(기본 {HTTP_DEFAULT_PORT})")
    args = ap.parse_args(argv)
    if args.port is not None and not args.http:
        ap.error("--port 는 --http 와 함께 쓰세요.")
    port = args.port or HTTP_DEFAULT_PORT
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP    # mcp 2.x (FastMCP 의 새 이름)
    except ImportError:
        if args.http:
            sys.exit("--http 는 mcp 2.x 가 필요합니다: pip install 'mcp>=2.2,<3'")
        try:
            from mcp.server.fastmcp import FastMCP               # mcp 1.x
        except ImportError:
            sys.exit("MCP SDK가 없습니다. 저장소의 .venv 로 실행하세요: .venv/bin/python stdict_mcp.py")
    token = _load_token() if args.http else None               # 토큰 문제는 DB 보다 먼저 알려 준다
    meta = _check_db()
    server = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)
    for fn in TOOLS:
        server.tool(description=inspect.cleandoc(fn.__doc__ or ""))(_threaded(fn))
    info = f"DB 빌드 {meta.get('built_at', '?')} / 스키마 {meta.get('schema_version', '?')}"
    if args.http:
        import uvicorn
        print(f"[stdict MCP] {info} / HTTP http://{HTTP_HOST}:{port}/mcp / 인증: 토큰 필요 / "
              f"허용 Host: {', '.join(_allowed_hosts())}", file=sys.stderr)
        app = _BearerAuth(server.streamable_http_app(host=HTTP_HOST, transport_security=_http_security()), token)
        uvicorn.run(app, host=HTTP_HOST, port=port, log_level="warning")
        return
    print(f"[stdict MCP] {info}", file=sys.stderr)
    server.run()      # 기본 transport = stdio


if __name__ == "__main__":
    main()
