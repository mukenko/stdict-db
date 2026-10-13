# stdict-db

표준국어대사전 Open API로 만드는 **개인용 로컬 사전 DB**와, 이를 LLM이 조회하는 **MCP 서버**입니다.

LLM은 국어 발음의 장단음 같은 것을 자주 틀립니다. 예를 들어 '연기'는 한 철자에 동음이의어가 12개 있고,
그중 演技·捐棄만 장음 `[연ː기]`입니다. 이 프로젝트는 사전 전체를 로컬 SQLite로 내려받아 두고,
LLM이 기억이 아니라 사전을 보고 표기·발음·뜻·한자를 확인하게 합니다.

데이터 출처: 국립국어원 표준국어대사전 (CC BY-SA 2.0 KR)

## 구성

| 파일 | 하는 일 |
|---|---|
| `stdict_fetch.py` | 사전 내용 API(`view.do`)로 전체 코드를 한 번씩 수집해 `data/raw.sqlite`에 원본 XML 저장 (이어받기, 하루 호출 상한 가드) |
| `stdict_sync.py` | 이미 받은 것을 다시 확인: 고친 날짜 검색으로 변경 감지, 변경·삭제 기록, 어깨번호 보충(`supno`) |
| `stdict_update.py` | 정기 업데이트: 변경 확인 → 어깨번호 보충 → 바뀐 것이 있으면 재빌드를 한 번에 (매일 한 번 launchd·cron으로 실행) |
| `stdict_build.py` | 원본 → 조회용 DB(`data/stdict.sqlite`, FTS5 포함). 검증을 통과한 것만 교체 |
| `stdict_lookup.py` | 조회 코어(표준 라이브러리만, 읽기 전용) + CLI |
| `stdict_mcp.py` | MCP 서버(stdio, 선택적으로 Streamable HTTP) |
| `docs/` | 국립국어원 오픈 API 명세 사본 |

## 준비

1. [오픈 API 사용 신청](https://stdict.korean.go.kr/openapi/openApiInfo.do)으로 인증키를 받아 `.env`에 둡니다.
   ```
   STDICT_API_KEY=발급받은키
   ```
   인증키는 저장소에 올리지 않습니다(`.gitignore`). 키 하나당 **하루 50,000건** 제한이 있어, 스크립트는 기본 45,000건에서 멈춥니다.
2. Python 3.9 이상. 수집·빌드·CLI 조회는 표준 라이브러리만 쓰고, MCP 서버만 `mcp` 패키지(Python 3.10 이상)가 필요합니다.
   ```
   python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
   ```

## 사용

```
python3 stdict_fetch.py fetch --range 1 540000   # 수집(하루 한도에서 멈추고, 다시 실행하면 이어서). 전체 약 12일
python3 stdict_sync.py baseline                  # 변경 감지용 기준선(호출 0건)
python3 stdict_sync.py supno                     # 동음이의어 어깨번호 보충(약 36,000회 호출)
python3 stdict_build.py                          # 조회용 DB 빌드
python3 stdict_lookup.py 연기                    # 조회
python3 stdict_lookup.py 연:기 --by pronunciation   # 발음으로 찾기(ː 와 : 모두 가능)
```

### 정기 업데이트

사전은 1년에 약 1%가 바뀝니다. 매일 한 번 `python3 stdict_update.py`를 실행하면(launchd·cron 등) 다음을 합니다.

- 매일: 고친 날짜 검색(`updates`), 뒤쪽 신규 확인(`refresh --tail`), 가장 오래 확인하지 않은 코드 4,000개 재확인(`refresh --oldest`, 약 110일에 전체를 한 바퀴)
- 일요일: 빈 코드 재확인, 무작위 표본 점검(날짜 검색이 놓친 변경이 있는지)
- 바뀐 표제어 그룹의 어깨번호를 다시 받고, 바뀐 것이 있을 때만 `stdict_build.py`로 재빌드(검증을 통과해야 교체)
- 하루 호출 상한 20,000건(`--limit`), 로그는 `data/update.log`(15일 보관). 표제어 교체·삭제 확정 같은 드문 일은 macOS 알림으로 알립니다.

`--dry-run`으로 할 일만 확인하고, `--status`로 마지막 실행 결과를 봅니다. 폴더 동기화 스크립트와 겹치지 않도록 잠금 폴더를 같이 잡습니다
(기본 `/tmp/rsync_to_rpi.lock`, 환경변수 `STDICT_UPDATE_LOCK`, 끄려면 `--no-lock`).
개별 명령은 `python3 stdict_sync.py --help`를 보세요.

### MCP 서버

도구 2개(읽기 전용): `lookup_word`(동음이의어 전부, 발음·한자·뜻풀이)와 `search_dictionary`(발음·한자·활용형·뜻풀이로 역검색).
응답 맨 위에 동음이의어를 발음별로 묶은 요약(장음/단음)을 넣어 작은 로컬 모델도 장단음을 가를 수 있게 했습니다.

- **stdio**(LM Studio, Claude 데스크톱 등): 클라이언트 설정에 다음처럼 등록합니다.
  ```json
  {"mcpServers": {"stdict": {"command": "/경로/stdict-db/.venv/bin/python", "args": ["/경로/stdict-db/stdict_mcp.py"]}}}
  ```
- **Streamable HTTP**: `.venv/bin/python stdict_mcp.py --http --port 8766`. 127.0.0.1에만 바인드하고
  Bearer 토큰이 필수입니다(`~/.config/stdict-mcp/token`, 권한 600). 외부 노출은 프록시(예: `tailscale serve`)가 맡고,
  접속에 쓰는 이름은 환경변수 `STDICT_MCP_HOSTS`에 넣습니다.

## 테스트

조회용 DB를 빌드한 뒤 실행합니다(DB는 저장소에 없습니다).
```
python3 -m unittest tests.test_lookup
.venv/bin/python -m unittest tests.test_mcp_stdio tests.test_mcp_http
```

## 데이터와 저작권

- 사전 텍스트는 **CC BY-SA 2.0 KR**입니다(저작자 표시, 동일조건변경허락). 이 저장소에는 **사전 데이터를 올리지 않으며**,
  각자 인증키로 수집합니다. 데이터를 재배포하려면 출처 표시와 같은 조건을 지켜야 합니다.
- 멀티미디어·발음 음성 파일은 별도 정책이라 수집하지 않고 링크만 저장합니다.
- **출전이 있는 용례는 원저작자의 권리**가 있어, 조회 결과에서는 기본으로 제외합니다(`--cited`로 포함).

## 라이선스

- 코드: [MIT License](LICENSE)
- 사전 데이터: 국립국어원 표준국어대사전의 CC BY-SA 2.0 KR (위 "데이터와 저작권" 참고). 이 저장소에는 데이터가 없습니다.

## 알려진 한계

- 사전이 싣는 활용형은 항목당 몇 개뿐이라 `먹었다`·`갔다` 같은 활용형으로는 표제어를 찾지 못합니다.
- 뜻풀이 검색은 3글자 이상이면 정확하고, 2글자는 앞쪽 일부만 보여 줍니다.
- 개인용 프로젝트입니다. 사전 본문의 정확성은 국립국어원 사전을 기준으로 하세요.
