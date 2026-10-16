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
import stdict_view as view

SERVER_NAME = "stdict"
HTTP_HOST = "127.0.0.1"                 # 바인드 주소는 고정한다(외부에 직접 열지 않는다)
HTTP_DEFAULT_PORT = 8766
HTTP_MODE = False                       # --http 로 띄우면 True(원격 서버는 사용자 화면에서 링크를 열 수 없다)
TOKEN_FILE = Path(os.environ.get("STDICT_MCP_TOKEN_FILE") or Path.home() / ".config" / "stdict-mcp" / "token")

INSTRUCTIONS = (
    "Standard Korean Language Dictionary (표준국어대사전, National Institute of Korean Language). "
    "Use these tools to verify Korean spelling, word forms, pronunciation (long vowels are marked with ː), "
    "Hanja/original language and meanings instead of answering from memory. "
    "One spelling can have several homonyms (shown with superscript numbers, e.g. 연기¹ 연기²); read ALL of them and "
    "choose by context, and do not mix up their pronunciations (e.g. 연ː기 with a long vowel vs 연기). "
    "Keep three tiers of your answer clearly apart. "
    "(1) Dictionary facts: quote them exactly from the tool output and never invent or \"recall\" them: entries and "
    "homonym numbers, readings and pronunciations (copy them exactly as shown, keeping ː), Hanja/original language, "
    "definitions, examples and other-language words. Never attach glosses, classifications or usage registers "
    "(구어, 문어, 옛말, Hanja readings) to a word unless the tool output states them. "
    "(2) \"(추론)\": your own comparison or judgement based on the listed entries, e.g. that two entries have "
    "identical definitions. "
    "(3) \"[사전 외 설명]\": general explanation, background or advice that is not based on the tool output, in a "
    "separate part. "
    "Match your research effort to the question and keep the user in control of how long it takes. A simple check "
    "(spelling, pronunciation, meaning) needs no extra lookups. When asked for synonyms or similar words, first read "
    "the 동의어/비슷한말/참고 어휘 notes of the entry and report what the dictionary lists. If it lists none, say so "
    "and offer, in one line, to search wider (definition searches with different key phrases, a Hanja search for "
    "Sino-Korean words); do not start that search yet. When the user agrees, or asks from the start for a broad or "
    "thorough search (e.g. \"더 찾아줘\", \"자세히\", \"비슷한 말을 모두\"), do a bounded search of about 3 to 4 extra "
    "tool calls: one or two definition searches with different key phrases taken from the definition, one Hanja "
    "search if the word is Sino-Korean, and lookups of the two or three most promising candidates (even ones you "
    "think of yourself) to verify them against the dictionary. Present such findings as words whose definitions "
    "overlap, not as dictionary-listed synonyms, and mark your own comparison as \"(추론)\"; then stop and say what you "
    "did not cover. Go wider only when the user asks. "
    "When the output has a pronunciation summary, use it to separate long-vowel words from short-vowel ones. "
    "If a tool says a word is not an entry, say so; do not invent dictionary content. "
    "When a tool result contains a line starting with \"원본 보기:\" followed by a link, end your answer with that link "
    "as a Markdown link ([원본 보기](URL)). "
    "Answer in the user's language (Korean unless the user writes otherwise) and mention that the source is the "
    "Standard Korean Language Dictionary."
)

# 모든 조회 결과 끝에 붙이는 알림(personal-memory 의 NOTICE 와 같은 방식). 모델에 대한 부탁이며 강제는 아니다.
# 인용·표시 방법만 담는다. 탐색 횟수 같은 행동 지시는 도구 결과 안에 넣지 않는다(Claude 가 프롬프트 주입으로 보고 따르지 않고
# 매번 사용자에게 알린다, 2026-10-15). 그런 기준은 서버 지침(INSTRUCTIONS)과 도구 설명에만 둔다.
# 서버 지침을 읽는 클라이언트(Claude 등)에는 환경변수 STDICT_MCP_NOTICE=0 으로 알림을 끌 수 있다.
NOTICE = ("[Note to assistant: quote dictionary facts exactly and never invent them or attach labels, glosses or "
          "registers the output does not state; keep ː as shown. Mark your own judgement as \"(추론)\" and anything "
          "beyond the tool output as \"[사전 외 설명]\". State that the source is the Standard Korean Language "
          "Dictionary.]")


def _notice_tail():
    """결과 끝에 붙일 알림(줄바꿈 포함). STDICT_MCP_NOTICE 가 0/off/false/no 면 붙이지 않는다."""
    if os.environ.get("STDICT_MCP_NOTICE", "1").strip().lower() in ("0", "off", "false", "no"):
        return ""
    return "\n" + NOTICE


def _view_link_enabled():
    """이 컴퓨터의 로컬 서버일 때만 링크를 만든다(원격 서버의 127.0.0.1 은 사용자 컴퓨터가 아니다). STDICT_VIEW_LINK=0 이면 끈다."""
    return not HTTP_MODE and os.environ.get("STDICT_VIEW_LINK", "1").strip().lower() not in ("0", "off", "false", "no")


def _with_footer(text, label="", spec=None, found=True):
    """결과의 출처 줄(sl.ATTRIBUTION, 맨 끝) 앞에 (로컬 서버이고 찾았으면) '원본 보기:' 줄을 끼운다: 링크 줄 → 출처 줄 순서.
    링크 줄은 지시가 아니라 주소만 담은 사실이다. 링크를 적는 규칙은 서버 지침·도구 설명에 둔다(도구 결과 안의 지시는 Claude 가
    주입으로 본다). spec 은 링크 서버가 클릭할 때 다시 그릴 검색 조건이다."""
    if spec is None or not found or not _view_link_enabled():
        return text
    try:
        line = f"원본 보기: {view.get_server().add_query(label or 'result', **spec)}"
    except Exception:  # noqa: BLE001  서버를 못 띄워도 조회는 돌려준다
        return text
    body, sep, tail = text.rpartition("\n")
    if sep and tail == sl.ATTRIBUTION:
        return f"{body}\n{line}\n{tail}"
    return f"{text}\n{line}"


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
    starts with a summary that groups them by pronunciation; use that summary and the listed entries for the facts. If the word is not an entry but is a conjugated or
    abbreviated form (e.g. 먹어 -> 먹다), the base word is shown; for regular conjugated forms not listed in the dictionary
    (e.g. 갔다, 먹었다) a base-form candidate is shown labeled '규칙으로 추정' (rule-based guess; irregular verbs only when the dictionary lists an altered stem); otherwise similar entries are suggested.
    Use this for spelling, pronunciation (including long vs short vowels), meaning, Hanja and homonym checks.
    Hyphens and spaces typed by the user are handled; do not add them yourself.
    A final line may list headwords that start with "<word>-" (e.g. 제작-하다, 제작-되다, 제작-권), shown exactly as the
    dictionary spells them; it does not say which homonym they belong to, so do not assign them to one.
    Anything that explains WHY or HOW beyond what this tool returns (for example the sound-change rule behind a
    pronunciation such as 비음화, spelling rules, usage advice) is not dictionary content: put it in a separate part
    labeled "[사전 외 설명]". End the answer with the source line.
    If the result has a line starting with "원본 보기:", it is a link to a page that shows the entries in full from the
    local database; end your answer with that link as a Markdown link ([원본 보기](URL)).
    Args: word = the Korean word or phrase, e.g. "연기"; max_entries = how many homonyms to show (default 12);
    include_examples = also show usage examples (with their cited source when the dictionary gives one)."""
    res = _dict().lookup(word, max_entries, include_examples)
    spec = {"kind": "lookup", "q": word, "max_entries": max_entries, "examples": bool(include_examples)}
    return _with_footer(sl.format_lookup(res), word, spec, bool(res["total"])) + _notice_tail()


@_safe
def search_dictionary(query: str, by: Literal["pronunciation", "hanja", "form", "definition"],
                      limit: int = 10) -> str:
    """Reverse search in the Standard Korean Language Dictionary (표준국어대사전).
    by="pronunciation": find words pronounced like the query; write the long vowel mark ː (or a colon :) to match
    only long vowels, e.g. "연ː기"; without it, both long and short matches are returned.
    by="hanja": find words containing the given Hanja, e.g. "演技".
    by="form": find the base word of a conjugated or abbreviated form listed in the dictionary, e.g. "가계해";
    regular forms that are not listed (e.g. "갔다") get a rule-based guess labeled '규칙으로 추정' with the rule used.
    by="definition": find words whose definition contains the phrase (2 or more characters; 3 or more is exact).
    To find similar words, vary the query: use key phrases taken from the definition (a different phrase can reveal
    different words) and, for Sino-Korean words, a Hanja search; a few calls are enough.
    For checking a single known word, use lookup_word instead.
    Explanations beyond the returned entries belong in a separate part labeled "[사전 외 설명]"; end the answer
    with the source line.
    If the result has a line starting with "원본 보기:", it is a link to a page that shows the found entries in full from
    the local database; end your answer with that link as a Markdown link ([원본 보기](URL)).
    Args: query = the text to search; by = the search kind above; limit = maximum results (default 10)."""
    res = _dict().search(query, by, limit)
    spec = {"kind": "search", "q": query, "by": by, "limit": limit}
    return _with_footer(sl.format_search(res), query, spec, bool(res["hits"])) + _notice_tail()


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
    global HTTP_MODE
    HTTP_MODE = bool(args.http)
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
        # MCP 클라이언트는 SSE 연결을 계속 열어 두므로, 종료 신호를 받아도 연결이 닫히기를 기다리면 끝나지 않는다.
        # 5초 뒤에는 남은 연결을 끊고 종료한다(systemctl restart/stop 이 멈추지 않게).
        uvicorn.run(app, host=HTTP_HOST, port=port, log_level="warning", timeout_graceful_shutdown=5)
        return
    print(f"[stdict MCP] {info}", file=sys.stderr)
    server.run()      # 기본 transport = stdio


if __name__ == "__main__":
    main()
