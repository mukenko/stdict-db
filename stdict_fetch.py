#!/usr/bin/env python3
"""표준국어대사전 Open API 원본 XML 수집기 (표준 라이브러리만 사용)

원본(view.do 응답 XML)을 그대로 SQLite(data/raw.sqlite)에 압축 저장한다.
스키마 변환은 이 스크립트의 책임이 아니다. 나중에 원본에서 다시 빌드한다.

사용 예
  python stdict_fetch.py fetch --codes 2 358706          # 코드 지정
  python stdict_fetch.py fetch --range 1 1000            # 코드 범위 스캔
  python stdict_fetch.py fetch --word 연기 풀발          # 검색으로 코드 확인 후 수집
  python stdict_fetch.py fetch --range 460000 999999 --stop-after-empty 300
  python stdict_fetch.py sample --n 300 --max-code 500000
  python stdict_fetch.py stats                            # XML 태그 경로 통계
  python stdict_fetch.py status                           # 진행 현황

인증키는 환경변수 STDICT_API_KEY 또는 이 파일 옆의 .env 에서 읽는다.
"""
import argparse
import csv
import datetime as dt
import os
import random
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("STDICT_DB", ROOT / "data" / "raw.sqlite"))
BASE_URL = os.environ.get("STDICT_BASE_URL", "https://stdict.korean.go.kr").rstrip("/")
DEFAULT_LIMIT = 45000          # 하루 공식 한도 50,000건보다 여유를 둔다
MAX_CONSECUTIVE_ERRORS = 3     # 연속 오류가 이 횟수에 이르면 중단
KNOWN_CODES = [1, 2, 236195, 236214, 358706, 459493]  # 표본에 항상 포함할 코드


class LimitReached(Exception):
    pass


class ApiError(Exception):
    pass


class FatalApiError(ApiError):
    """재시도해도 소용없는 API 오류(미등록 키, 사용 중지 키, 잘못된 요청 등)."""


# ---------------------------------------------------------------- 설정/DB
def load_key():
    key = os.environ.get("STDICT_API_KEY", "").strip()
    if key:
        return key
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("STDICT_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("인증키를 찾을 수 없습니다. .env 에 STDICT_API_KEY=... 를 적어 주세요.")


def open_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS raw_xml(
            target_code INTEGER PRIMARY KEY,
            fetched_at  TEXT NOT NULL,
            xml         BLOB NOT NULL);           -- zlib 압축한 원본 XML
        CREATE TABLE IF NOT EXISTS fetch_state(
            target_code INTEGER PRIMARY KEY,
            status      TEXT NOT NULL,            -- ok | empty | error
            fetched_at  TEXT NOT NULL,
            note        TEXT);
        CREATE TABLE IF NOT EXISTS api_usage(
            day   TEXT PRIMARY KEY,
            calls INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS search_hits(   -- search.do 결과(어깨번호 보충용)
            target_code INTEGER PRIMARY KEY,
            word TEXT, sup_no TEXT, origin TEXT, pos TEXT, seen_at TEXT);
        """
    )
    return db


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def today():
    return dt.date.today().isoformat()


# ---------------------------------------------------------------- API 클라이언트
class Client:
    def __init__(self, key, db, limit, sleep):
        self.key, self.db, self.limit, self.sleep = key, db, limit, sleep

    def used_today(self):
        row = self.db.execute("SELECT calls FROM api_usage WHERE day=?", (today(),)).fetchone()
        return row[0] if row else 0

    def _count_call(self):
        self.db.execute(
            "INSERT INTO api_usage(day, calls) VALUES(?, 1) "
            "ON CONFLICT(day) DO UPDATE SET calls = calls + 1",
            (today(),),
        )
        self.db.commit()

    def call(self, path, params):
        """한 번의 API 호출(재시도 포함). 시도마다 호출 수에 포함한다.
        주소에 키가 들어 있으므로 주소는 어디에도 출력하지 않는다."""
        params = dict(params, key=self.key)
        url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
        last = "알 수 없는 오류"
        for attempt, wait in enumerate((0, 2, 5)):
            if wait:
                time.sleep(wait)
            if self.used_today() >= self.limit:
                raise LimitReached()
            self._count_call()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "stdict-db/0.1"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    text = resp.read().decode("utf-8")
                if self.sleep:
                    time.sleep(self.sleep)
                return text
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = f"네트워크 오류 ({type(e).__name__})"
        raise ApiError(last)

    # -- view.do
    def fetch_code(self, code):
        text = self.call("/api/view.do", {"method": "TARGET_CODE", "q": str(code), "req_type": "xml"})
        root = parse_xml(text)
        check_error(root)
        total = int((root.findtext("total") or "0").strip() or 0)
        item = root.find("item")
        if total == 0 or item is None:
            set_state(self.db, code, "empty", None)
            return "empty"
        got = (item.findtext("target_code") or "").strip()
        if got != str(code):
            raise ApiError(f"요청 코드와 응답 코드가 다릅니다 ({code} != {got})")
        self.db.execute(
            "INSERT OR REPLACE INTO raw_xml(target_code, fetched_at, xml) VALUES(?,?,?)",
            (code, now(), zlib.compress(text.encode("utf-8"), 6)),
        )
        set_state(self.db, code, "ok", None)
        return "ok"

    # -- search.do (표제어 → target_code)
    def search_word(self, word, method):
        q = word.replace("-", "").strip()      # 표제어의 하이픈은 검색어에서 뺀다
        hits, start = [], 1
        while True:
            text = self.call("/api/search.do", {
                "q": q, "req_type": "xml", "advanced": "y", "target": "1",
                "method": method, "start": str(start), "num": "100"})
            root = parse_xml(text)
            check_error(root)
            total = int((root.findtext("total") or "0").strip() or 0)
            for it in root.findall("item"):
                hit = {k: (it.findtext(k) or "").strip() for k in ("target_code", "word", "sup_no", "origin", "pos")}
                if hit["target_code"]:
                    hits.append(hit)
                    self.db.execute(
                        "INSERT OR REPLACE INTO search_hits VALUES(?,?,?,?,?,?)",
                        (int(hit["target_code"]), hit["word"], hit["sup_no"], hit["origin"], hit["pos"], now()))
            self.db.commit()
            start += 100
            if start > total or start > 1000 or not root.findall("item"):
                break
        return hits


def parse_xml(text):
    try:
        return ET.fromstring(text.strip())
    except ET.ParseError:
        raise ApiError("응답이 올바른 XML이 아닙니다")


def check_error(root):
    if root.tag == "error" or root.find("error_code") is not None:
        code = (root.findtext("error_code") or "?").strip()
        msg = (root.findtext("message") or "").strip()
        text = f"API 오류 {code}: {msg}"
        if code == "021":
            text += " (일시 사용 중지된 키입니다. 한도 초과나 과다 호출일 수 있으니 시간을 두고 확인하세요)"
        # 000(시스템 오류)만 일시적 오류로 보고, 나머지는 재시도해도 같은 결과이므로 즉시 중단한다.
        raise (ApiError if code == "000" else FatalApiError)(text)


def set_state(db, code, status, note):
    db.execute(
        "INSERT OR REPLACE INTO fetch_state(target_code, status, fetched_at, note) VALUES(?,?,?,?)",
        (code, status, now(), note))
    db.commit()


# ---------------------------------------------------------------- 수집 루프
def run_codes(client, codes, force=False, stop_after_empty=0, verbose=False):
    db = client.db
    known = dict(db.execute("SELECT target_code, status FROM fetch_state"))
    counts = Counter()
    consecutive_err = consecutive_empty = 0
    try:
        for i, code in enumerate(codes, 1):
            status = known.get(code)
            if not force and status in ("ok", "empty"):
                counts["skipped"] += 1
                consecutive_empty = consecutive_empty + 1 if status == "empty" else 0
            else:
                try:
                    result = client.fetch_code(code)
                    counts[result] += 1
                    consecutive_err = 0
                    consecutive_empty = consecutive_empty + 1 if result == "empty" else 0
                    if verbose:
                        print(f"  {code}: {result}")
                except FatalApiError as e:
                    print(f"  {code}: {e}\n재시도해도 해결되지 않는 오류라서 중단합니다.")
                    break
                except ApiError as e:
                    counts["error"] += 1
                    consecutive_err += 1
                    set_state(db, code, "error", str(e))
                    print(f"  {code}: 오류 - {e}")
                    if consecutive_err >= MAX_CONSECUTIVE_ERRORS:
                        print(f"연속 오류 {MAX_CONSECUTIVE_ERRORS}회, 중단합니다. 잠시 후 다시 실행하세요.")
                        break
            if stop_after_empty and consecutive_empty >= stop_after_empty:
                print(f"빈 코드가 {stop_after_empty}개 연속되어 중단합니다 (마지막 코드 {code}).")
                break
            if i % 100 == 0:
                print(f"  진행 {i}/{len(codes)}  (오늘 호출 {client.used_today()}/{client.limit})")
    except LimitReached:
        print(f"오늘 호출 한도({client.limit})에 도달했습니다. 내일 같은 명령을 다시 실행하면 이어서 진행합니다.")
    except KeyboardInterrupt:
        print("\n중단했습니다. 같은 명령을 다시 실행하면 이어서 진행합니다.")
    finally:
        db.commit()
    print("요약:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "처리한 코드 없음",
          f"| 오늘 호출 {client.used_today()}/{client.limit}")


# ---------------------------------------------------------------- 명령
def cmd_fetch(args):
    db = open_db()
    client = Client(load_key(), db, args.limit, args.sleep)
    if args.codes:
        codes = args.codes
    elif args.range:
        codes = range(args.range[0], args.range[1] + 1)
    else:
        codes = []
        try:
            for word in args.word:
                hits = client.search_word(word, args.method)
                if not hits:
                    print(f"'{word}': 검색 결과 없음 (--method include 로 다시 시도해 보세요)")
                for h in hits:
                    print(f"  {h['target_code']:>7}  {h['word']} {h['sup_no']:>3}  {h['origin']}  {h['pos']}")
                codes += [int(h["target_code"]) for h in hits]
        except (LimitReached, ApiError) as e:
            sys.exit(f"검색 중단: {e or '호출 한도'}")
    run_codes(client, list(codes) if not isinstance(codes, range) else codes,
              force=args.force, stop_after_empty=args.stop_after_empty, verbose=args.verbose)


def cmd_sample(args):
    db = open_db()
    client = Client(load_key(), db, args.limit, args.sleep)
    rng = random.Random(args.seed)
    codes = [] if args.no_extras else list(KNOWN_CODES)
    pool = rng.sample(range(1, args.max_code + 1), min(args.n, args.max_code))
    codes += [c for c in pool if c not in codes]
    print(f"표본 {len(codes)}개 (1~{args.max_code} 무작위, seed={args.seed}); 빈 코드는 empty로 기록됩니다.")
    run_codes(client, codes, verbose=args.verbose)


def cmd_status(args):
    db = open_db()
    print("상태별 코드 수:")
    for status, n in db.execute("SELECT status, COUNT(*) FROM fetch_state GROUP BY status"):
        print(f"  {status:<6} {n}")
    row = db.execute("SELECT MIN(target_code), MAX(target_code), COUNT(*) FROM raw_xml").fetchone()
    print(f"원본 XML: {row[2]}건 (코드 {row[0]} ~ {row[1]})")
    row = db.execute("SELECT MAX(target_code) FROM fetch_state WHERE status='ok'").fetchone()
    print(f"수집된 최대 코드: {row[0]}")
    used = db.execute("SELECT calls FROM api_usage WHERE day=?", (today(),)).fetchone()
    print(f"오늘 호출: {used[0] if used else 0} (기본 상한 {DEFAULT_LIMIT})")
    n = db.execute("SELECT COUNT(*) FROM search_hits").fetchone()[0]
    print(f"검색 결과 기록(어깨번호 보충용): {n}건")
    errs = db.execute("SELECT target_code, note FROM fetch_state WHERE status='error' LIMIT 5").fetchall()
    if errs:
        print("오류 기록(최대 5건, 다음 실행 때 자동 재시도):")
        for code, note in errs:
            print(f"  {code}: {note}")


def cmd_stats(args):
    """수집한 원본 전체에서 XML 태그 경로별 통계를 낸다 (스키마 설계용)."""
    db = open_db()
    entries_with = Counter()          # 경로가 나타난 항목 수
    max_repeat = defaultdict(int)     # 같은 부모 아래 같은 태그가 반복된 최대 횟수
    sample = {}                       # 리프 경로의 예시 값
    n = 0

    def walk(elem, path, seen):
        counts = Counter(child.tag for child in elem)
        for tag, c in counts.items():
            p = f"{path}/{tag}"
            seen.add(p)
            max_repeat[p] = max(max_repeat[p], c)
        for child in elem:
            p = f"{path}/{child.tag}"
            if len(child) == 0:
                val = " ".join((child.text or "").split())
                if val and p not in sample:
                    sample[p] = val[:30]
            walk(child, p, seen)

    for (blob,) in db.execute("SELECT xml FROM raw_xml"):
        root = ET.fromstring(zlib.decompress(blob).decode("utf-8"))
        item = root.find("item")
        if item is None:
            continue
        n += 1
        seen = {"item"}
        walk(item, "item", seen)
        entries_with.update(seen)
    if not n:
        sys.exit("수집된 원본이 없습니다. 먼저 fetch 또는 sample 을 실행하세요.")
    rows = sorted(entries_with)
    print(f"항목 {n}건 분석\n")
    print(f"{'경로':<70} {'항목수':>6} {'비율':>6} {'최대반복':>7}  예시")
    for p in rows:
        print(f"{p:<70} {entries_with[p]:>6} {entries_with[p] * 100 // n:>5}% "
              f"{max_repeat.get(p, 1):>7}  {sample.get(p, '')}")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["path", "entries", "max_repeat", "sample"])
            for p in rows:
                w.writerow([p, entries_with[p], max_repeat.get(p, 1), sample.get(p, "")])
        print(f"\nCSV 저장: {args.csv}")


def main():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # `| head` 등으로 출력이 끊겨도 오류 없이 종료
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="하루 호출 상한 (기본 45000)")
    common.add_argument("--sleep", type=float, default=0.1, help="호출 사이 대기 초 (기본 0.1)")
    common.add_argument("-v", "--verbose", action="store_true")

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", parents=[common], help="원본 XML 수집")
    g = f.add_mutually_exclusive_group(required=True)
    g.add_argument("--codes", type=int, nargs="+", metavar="CODE")
    g.add_argument("--range", type=int, nargs=2, metavar=("START", "END"))
    g.add_argument("--word", nargs="+", metavar="WORD")
    f.add_argument("--method", choices=["exact", "include", "start"], default="exact",
                   help="--word 검색 방식 (기본 exact)")
    f.add_argument("--stop-after-empty", type=int, default=0, metavar="N",
                   help="빈 코드가 N개 연속되면 중단 (범위 스캔의 끝 찾기/신규 코드 확인용)")
    f.add_argument("--force", action="store_true", help="이미 수집한 코드도 다시 수집")
    f.set_defaults(func=cmd_fetch)

    s = sub.add_parser("sample", parents=[common], help="무작위 표본 수집")
    s.add_argument("--n", type=int, default=300)
    s.add_argument("--max-code", type=int, default=500000)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--no-extras", action="store_true", help="기본 포함 코드(1, 2, 236214, 358706 등) 제외")
    s.set_defaults(func=cmd_sample)

    t = sub.add_parser("stats", help="XML 태그 경로 통계")
    t.add_argument("--csv", help="CSV로도 저장")
    t.set_defaults(func=cmd_stats)

    u = sub.add_parser("status", help="진행 현황")
    u.set_defaults(func=cmd_status)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
