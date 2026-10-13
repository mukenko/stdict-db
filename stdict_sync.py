#!/usr/bin/env python3
"""표준국어대사전 로컬 DB 동기화 도구 (stdict_fetch.py 와 같은 폴더에 둔다)

stdict_fetch.py 는 '아직 안 받은 코드를 받는' 도구이고, 이 파일은
'이미 받은 것을 다시 확인하고 변화를 기록하는' 도구다.

  python3 stdict_sync.py probe --official 436587          # 고친 날짜 검색 동작 시험(읽기 전용)
  python3 stdict_sync.py baseline                         # 이미 받은 원본의 해시를 로컬에서 계산(호출 0건)
  python3 stdict_sync.py updates --since 20260928         # 고친 날짜 검색으로 변경 코드를 찾아 다시 받아 비교
  python3 stdict_sync.py updates --dry-run                # 어떤 코드가 걸리는지만 보기
  python3 stdict_sync.py refresh --oldest 3000            # 가장 오래 확인 안 한 ok 코드 재확인(순환 점검)
  python3 stdict_sync.py refresh --sample 2000            # 무작위 ok 코드 표본 점검(변경 놓침 비율 측정)
  python3 stdict_sync.py refresh --empties 3000           # 오래된 빈 코드 재확인(빈 자리 신규)
  python3 stdict_sync.py refresh --tail                   # 최대 코드 뒤쪽 신규 확인
  python3 stdict_sync.py refresh --codes 358706 236214    # 코드 지정
  python3 stdict_sync.py report --official 436587         # 변경 기록과 현황 요약(호출 0건)
  python3 stdict_sync.py supno --status                   # 어깨번호 보충 현황과 필요한 호출 수(호출 0건, 읽기 전용)
  python3 stdict_sync.py supno --words 가경자 가도 --dry-run   # 지정한 표제어만 시험(저장 안 함)
  python3 stdict_sync.py supno                            # 중복 표제어를 검색해 search_hits 에 어깨번호 저장(이어받기)

변화는 4가지로 분류해 changes 표에 남긴다.
  added(신규) / edited(내용 수정) / headword_changed(표제어·원어 교체, 경고) / deleted(삭제)
삭제는 서로 다른 날 두 번 연속 비어 있어야 확정한다(그 전에는 delete_suspect 로만 표시).
바뀐 원본은 raw_xml_history 에 남고, 삭제 확정 항목은 raw_xml 에서 빠진다.
빌드는 fetch_state.status='ok' 인 항목만 대상으로 해야 한다.

수집(stdict_fetch.py)이 아직 지나가지 않은 코드는 건너뛴다(수집이 최신 내용을 받으므로 손실 없음).
인증키와 요청 주소는 출력하지 않는다.
"""
import argparse
import datetime as dt
import hashlib
import itertools
import random
import re
import signal
import sys
import zlib
import xml.etree.ElementTree as ET
from collections import Counter

import stdict_fetch as sf

# 실측: 와일드카드 '*', '?' 는 0건이라 쓸 수 없다. 뜻풀이(target=8)에서 '.'를 포함 검색하면 사실상 전체가 잡힌다(436,580 / 436,587).
ALL_CANDIDATES = [("가", "include", 1), (".", "include", 8), ("다", "include", 8), ("의", "include", 8)]
ALL_Q = (".", "include", 8)
PROBE_DEFAULT_LIMIT = sf.DEFAULT_LIMIT + 1000   # 수집이 45,000건까지 쓴 날에도 시험할 수 있도록 예비분을 조금 쓴다
SEARCH_MAX = 1000                               # 검색 한 번에 볼 수 있는 최대 건수(start 1~1000)
BATCH = 50                                      # 이만큼 모아서 이상 징후를 검사한 뒤 한 번에 반영

SYNC_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_state(
    target_code   INTEGER PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    word          TEXT,
    origin        TEXT,
    last_verified TEXT NOT NULL,
    last_changed  TEXT,
    missing_since TEXT);                 -- 삭제 의심으로 처음 확인된 날(YYYY-MM-DD)
CREATE TABLE IF NOT EXISTS raw_xml_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_code INTEGER NOT NULL, archived_at TEXT NOT NULL,
    content_hash TEXT, reason TEXT, fetched_at TEXT, xml BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS raw_xml_history_code ON raw_xml_history(target_code);
CREATE TABLE IF NOT EXISTS changes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, target_code INTEGER NOT NULL, kind TEXT NOT NULL,
    old_word TEXT, new_word TEXT, old_origin TEXT, new_origin TEXT,
    old_hash TEXT, new_hash TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS changes_at ON changes(at);
CREATE TABLE IF NOT EXISTS sync_meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS supno_issues(      -- 어깨번호 보충에서 검색 결과가 로컬과 안 맞은 표제어(저장하지 않음)
    plain TEXT PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL,
    local_n INTEGER, remote_n INTEGER, detail TEXT);
"""


# ---------------------------------------------------------------- 공통
def open_sync_db():
    db = sf.open_db()
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(SYNC_SCHEMA)
    return db


def meta_get(db, key):
    row = db.execute("SELECT value FROM sync_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(db, key, value):
    db.execute("INSERT OR REPLACE INTO sync_meta(key, value) VALUES(?,?)", (key, value))
    db.commit()


def ymd(d):
    return d.strftime("%Y%m%d")


def parse_ymd(s):
    return dt.datetime.strptime(s, "%Y%m%d").date()


def chan(root):
    """응답 루트가 channel 이든 그 위에 한 겹 더 있든 channel 을 돌려준다."""
    if root.find("total") is None and root.find("channel") is not None:
        return root.find("channel")
    return root


def canon(e):
    """공백 차이를 없앤 정규화 문자열(속성·tail 무시). 응답의 lastBuildDate 는 item 밖이라 해시에 들어가지 않는다."""
    return f"<{e.tag}>{(e.text or '').strip()}" + "".join(canon(c) for c in e) + f"</{e.tag}>"


def item_info(item):
    h = hashlib.sha256(canon(item).encode("utf-8")).hexdigest()
    word = (item.findtext("word_info/word") or "").strip()
    origin = " ".join((t.text or "").strip()
                      for t in item.findall("word_info/original_language_info/original_language")).strip()
    return h, word, origin


# ---------------------------------------------------------------- 검색 (probe, updates 공용)
def search(client, q, method, target=1, update_s=None, update_e=None, start=1, num=10, pos=None):
    params = {"q": q, "req_type": "xml", "advanced": "y", "target": str(target),
              "method": method, "start": str(start), "num": str(num)}
    if update_s:
        params["update_s"] = update_s
        params["update_e"] = update_e or update_s
    if pos:
        params["pos"] = str(pos)
    text = client.call("/api/search.do", params)
    root = sf.parse_xml(text)
    sf.check_error(root)
    ch = chan(root)
    total = int((ch.findtext("total") or "0").strip() or 0)
    items = []
    for it in ch.findall("item"):
        items.append({k: (it.findtext(k) or "").strip()
                      for k in ("target_code", "word", "sup_no", "pos", "origin")})
    return total, items


def attempt(client, label, **kw):
    """오류가 나도 다음 시험을 계속한다. 키 문제(020/021)나 한도 도달은 중단한다."""
    try:
        return search(client, **kw)
    except sf.LimitReached:
        raise
    except sf.ApiError as e:
        print(f"  {label}: 오류 - {e}")
        if "API 오류 020" in str(e) or "API 오류 021" in str(e):
            sys.exit("인증키 문제라서 중단합니다.")
        return None


def sample_text(items, n=3):
    return ", ".join(f"{i['word']}{i['sup_no']}({i['target_code']})" for i in items[:n])


# ---------------------------------------------------------------- probe
def cmd_probe(args):
    db = sf.open_db()
    client = sf.Client(sf.load_key(), db, args.limit, args.sleep)
    today = dt.date.today()
    try:
        run_probe(args, db, client, today)
    except sf.LimitReached:
        print(f"오늘 호출 한도({args.limit})에 도달했습니다. --limit 값을 조금 올리거나 내일 다시 실행하세요.")
    finally:
        print(f"\n오늘 호출 {client.used_today()}/{args.limit}")


def run_probe(args, db, client, today):
    # A. "전체"를 표현하는 방법 찾기 (날짜 조건 없이 전체 건수 비교)
    if args.q:
        best = (args.q, args.method, args.target)
        print(f"A. 검색어를 직접 지정: q={args.q!r}, method={args.method}, target={args.target}")
    else:
        print("A. 날짜 조건 없이 검색어 후보별 전체 건수 (전체 표제어 수와 가까운 후보가 '전체'입니다)")
        best, best_total = None, -1
        for q, method, target in ALL_CANDIDATES:
            r = attempt(client, f"q={q!r} {method} target={target}", q=q, method=method, target=target)
            if r is None:
                continue
            total, items = r
            note = ""
            if args.official:
                note = f"  (자세히 찾기 {args.official:,}건과의 차이 {total - args.official:+,})"
            print(f"  q={q!r:<4} method={method:<8} target={target:<2} total={total:>9,}{note}  예: {sample_text(items)}")
            if total > best_total:
                best, best_total = (q, method, target), total
        if best is None or best_total <= 0:
            print("  사용할 수 있는 후보가 없었습니다. 위 결과를 알려 주세요.")
            return
        print(f"  → 이후 시험에는 q={best[0]!r}, method={best[1]}, target={best[2]} 를 사용합니다.")
        if args.official and best_total < args.official * 0.95:
            print(f"  ⚠ 가장 큰 후보도 전체의 {best_total * 100 // args.official}% 뿐입니다. 아래 B·C 결과는 '전체'가 아니라 그 일부에 대한 것입니다.")
    q, method, target = best

    # B. 날짜별 '고친 날짜' 건수
    print(f"\nB. 고친 날짜별 건수 (오늘부터 {args.days}일 전까지, 하루씩)")
    for i in range(args.days):
        d = today - dt.timedelta(days=i)
        r = attempt(client, ymd(d), q=q, method=method, target=target, update_s=ymd(d), update_e=ymd(d))
        if r is None:
            continue
        total, items = r
        print(f"  {d.isoformat()}  {total:>7,}건  {sample_text(items)}")
    print("  넓은 구간:")
    for n in (30, 90, 365):
        s = today - dt.timedelta(days=n)
        r = attempt(client, f"{n}일", q=q, method=method, target=target, update_s=ymd(s), update_e=ymd(today))
        if r is not None:
            print(f"  최근 {n:>3}일  {r[0]:>7,}건")

    # C. 최근 구간에서 걸린 코드를 로컬 상태와 대조
    s = today - dt.timedelta(days=args.days - 1)
    print(f"\nC. 최근 {args.days}일 구간({s.isoformat()} ~ {today.isoformat()})의 코드를 로컬 상태와 대조")
    hits, start, total = [], 1, None
    while True:
        r = attempt(client, f"start={start}", q=q, method=method, target=target,
                    update_s=ymd(s), update_e=ymd(today), start=start, num=100)
        if r is None:
            break
        total, items = r
        hits += items
        start += 100
        if not items or start > total or start > SEARCH_MAX or len(hits) >= args.max_items:
            break
    if not hits:
        print("  걸린 항목이 없어서 대조할 수 없습니다.")
        return
    hits = hits[:args.max_items]
    status = dict(db.execute("SELECT target_code, status FROM fetch_state"))
    max_ok = db.execute("SELECT MAX(target_code) FROM fetch_state WHERE status='ok'").fetchone()[0] or 0
    cats, examples = Counter(), {}
    for h in hits:
        code = int(h["target_code"])
        st = status.get(code)
        if code > max_ok:
            cat = f"수집된 최대 코드({max_ok:,})보다 큼 (뒤쪽 신규 후보)"
        elif st == "empty":
            cat = "로컬에서 빈 코드였음 (빈 자리 신규 후보)"
        elif st == "ok":
            cat = "로컬에 있음 (내용 수정 후보, 비교는 refresh 단계)"
        elif st == "error":
            cat = "로컬에 오류로 기록됨"
        else:
            cat = "아직 수집 전 (전체 수집이 지나가면 판단 가능)"
        cats[cat] += 1
        examples.setdefault(cat, []).append(f"{h['word']}{h['sup_no']}({code})")
    print(f"  전체 {total:,}건 중 {len(hits):,}건을 확인했습니다.")
    for cat, n in cats.most_common():
        print(f"  {n:>5}건  {cat}")
        print(f"         예: {', '.join(examples[cat][:6])}")


# ---------------------------------------------------------------- 변경 코드 찾기 (updates)
def collect_changed(client, s, e, hits, warns, pos=None):
    """[s, e] 기간에 고친 날짜가 있는 항목을 hits(code -> 항목)에 모은다.
    한 번에 1,000건까지만 볼 수 있으므로, 넘으면 날짜 구간을 절반씩 나누고,
    하루짜리도 넘으면 품사(1~15)로 나눈다. 그래도 넘으면 경고를 남긴다."""
    q, method, target = ALL_Q
    total, items = search(client, q, method, target, ymd(s), ymd(e), start=1, num=100, pos=pos)
    if total == 0:
        return
    if total > SEARCH_MAX:
        if s < e:
            mid = s + (e - s) // 2
            collect_changed(client, s, mid, hits, warns)
            collect_changed(client, mid + dt.timedelta(days=1), e, hits, warns)
            return
        if pos is None:
            for p in range(1, 16):
                collect_changed(client, s, e, hits, warns, pos=p)
            return
        warns.append(f"{s.isoformat()} 품사 {pos}: {total}건 중 앞의 {SEARCH_MAX}건만 확인")
    start = 1
    while True:
        for it in items:
            hits[int(it["target_code"])] = it
        start += 100
        if start > min(total, SEARCH_MAX):
            break
        total, items = search(client, q, method, target, ymd(s), ymd(e), start=start, num=100, pos=pos)
        if not items:
            break


def cmd_updates(args):
    db = open_sync_db()
    client = sf.Client(sf.load_key(), db, args.limit, args.sleep)
    today = dt.date.today()
    until = parse_ymd(args.until) if args.until else today
    if args.since:
        since = parse_ymd(args.since)
    else:
        last = meta_get(db, "last_updates_date")
        if not last:
            sys.exit("처음 실행입니다. --since YYYYMMDD 를 지정해 주세요 (수집을 시작한 날보다 2~3일 앞이 안전합니다).")
        since = parse_ymd(last) - dt.timedelta(days=args.overlap)
    print(f"고친 날짜 검색: {since.isoformat()} ~ {until.isoformat()}")
    hits, warns = {}, []
    try:
        collect_changed(client, since, until, hits, warns)
    except sf.LimitReached:
        sys.exit("호출 한도에 도달했습니다. 내일 다시 실행하세요.")
    except sf.ApiError as e:
        sys.exit(f"검색 실패: {e}")
    for w in warns:
        print(f"  ⚠ {w}")
    print(f"  걸린 항목 {len(hits):,}건")
    if args.dry_run:
        for code in sorted(hits)[:30]:
            h = hits[code]
            print(f"  {code:>7}  {h['word']}{h['sup_no']}")
        if len(hits) > 30:
            print(f"  ... 외 {len(hits) - 30}건")
        print(f"\n오늘 호출 {client.used_today()}/{args.limit} (--dry-run: 저장하지 않았습니다)")
        return
    completed, _ = run_refresh(client, db, sorted(hits), "updates", args)
    if completed and not warns:
        meta_set(db, "last_updates_date", ymd(until))
        print(f"다음 실행의 기준일을 {until.isoformat()} 로 저장했습니다(겹침 {args.overlap}일).")
    elif completed:
        print("경고가 있어 기준일을 갱신하지 않았습니다. 같은 --since 로 다시 실행하거나 나눠서 실행하세요.")
    else:
        print("끝까지 처리하지 못해 기준일을 갱신하지 않았습니다. 같은 명령을 다시 실행하세요.")
    print(f"오늘 호출 {client.used_today()}/{args.limit}")
    if not completed:
        sys.exit(2)


# ---------------------------------------------------------------- 재확인 핵심 (refresh)
def fetch_item(client, code):
    text = client.call("/api/view.do", {"method": "TARGET_CODE", "q": str(code), "req_type": "xml"})
    root = sf.parse_xml(text)
    sf.check_error(root)
    ch = chan(root)
    total = int((ch.findtext("total") or "0").strip() or 0)
    item = ch.find("item")
    if total == 0 or item is None:
        return {"found": False}
    got = (item.findtext("target_code") or "").strip()
    if got != str(code):
        raise sf.ApiError(f"요청 코드와 응답 코드가 다릅니다 ({code} != {got})")
    h, w, o = item_info(item)
    return {"found": True, "hash": h, "word": w, "origin": o, "text": text}


def local_item(db, code):
    row = db.execute("SELECT xml FROM raw_xml WHERE target_code=?", (code,)).fetchone()
    if not row:
        return None
    root = ET.fromstring(zlib.decompress(row[0]).decode("utf-8").strip())
    return chan(root).find("item")


def load_old(db, code):
    """저장된 해시 정보. 없으면(아직 baseline 전) 로컬 원본에서 즉석 계산한다."""
    row = db.execute("SELECT content_hash, word, origin, missing_since, last_changed "
                     "FROM sync_state WHERE target_code=?", (code,)).fetchone()
    if row:
        return {"hash": row[0], "word": row[1], "origin": row[2], "missing_since": row[3], "last_changed": row[4]}
    item = local_item(db, code)
    if item is None:
        return None
    h, w, o = item_info(item)
    return {"hash": h, "word": w, "origin": o, "missing_since": None, "last_changed": None}


def precheck(status, code, ctx):
    """API 를 부르기 전에 건너뛸 코드를 거른다."""
    if status == "error":
        return "skip_error"            # 오류 코드는 stdict_fetch.py 가 재시도한다
    if status is None and code <= ctx["scan_end"]:
        return "skip_unscanned"        # 수집이 아직 지나가지 않음(수집이 최신 내용을 받는다)
    return None


def decide(db, code, status, res, ctx):
    a = {"code": code, "old": None, "recovered": False}
    if status == "ok":
        old = load_old(db, code)
        if old is None:                       # ok 인데 로컬 원본이 없는 비정상 상태
            if res["found"]:
                a["kind"], a["note"] = "added", "로컬 원본 없음"
                return a
            old = {"hash": "", "word": "", "origin": "", "missing_since": None, "last_changed": None}
        a["old"] = old
        if not res["found"]:
            ms = old["missing_since"]
            if ms is None:
                a["kind"] = "suspect_deleted"
            else:
                gap = (dt.date.fromisoformat(ctx["today"]) - dt.date.fromisoformat(ms)).days
                a["kind"] = "deleted" if gap >= ctx["confirm_days"] else "suspect_waiting"
            return a
        a["recovered"] = old["missing_since"] is not None
        if res["hash"] == old["hash"]:
            a["kind"] = "verified"
        elif res["word"] != old["word"] or res["origin"] != old["origin"]:
            a["kind"] = "headword_changed"
        else:
            a["kind"] = "edited"
        return a
    # 로컬에서 empty 였거나, 수집 범위 밖(뒤쪽)의 코드
    if not res["found"]:
        a["kind"] = "still_empty" if status == "empty" else "tail_empty"
    else:
        a["kind"] = "added"
        a["note"] = "tail" if code > ctx["max_ok"] else "gap"
    return a


def archive_current(db, code, reason, t, old_hash=None):
    row = db.execute("SELECT fetched_at, xml FROM raw_xml WHERE target_code=?", (code,)).fetchone()
    if not row:
        return
    db.execute("INSERT INTO raw_xml_history(target_code, archived_at, content_hash, reason, fetched_at, xml) "
               "VALUES(?,?,?,?,?,?)", (code, t, old_hash or None, reason, row[0], row[1]))


def log_change(db, t, code, kind, old=None, new=None, note=None):
    db.execute("INSERT INTO changes(at, target_code, kind, old_word, new_word, old_origin, new_origin, "
               "old_hash, new_hash, note) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (t, code, kind,
                old["word"] if old else None, new["word"] if new else None,
                old["origin"] if old else None, new["origin"] if new else None,
                old["hash"] if old else None, new["hash"] if new else None, note))


def put_raw(db, code, res, t):
    db.execute("INSERT OR REPLACE INTO raw_xml(target_code, fetched_at, xml) VALUES(?,?,?)",
               (code, t, zlib.compress(res["text"].encode("utf-8"), 6)))


def upsert_state(db, code, h, w, o, t, changed=False, missing_since=None):
    row = db.execute("SELECT last_changed FROM sync_state WHERE target_code=?", (code,)).fetchone()
    last_changed = t if changed else (row[0] if row else None)
    db.execute("INSERT OR REPLACE INTO sync_state(target_code, content_hash, word, origin, last_verified, "
               "last_changed, missing_since) VALUES(?,?,?,?,?,?,?)",
               (code, h, w, o, t, last_changed, missing_since))


def apply_action(db, a, res, ctx):
    kind, code, t, old = a["kind"], a["code"], ctx["now"], a["old"]
    if kind == "still_empty":
        db.execute("UPDATE fetch_state SET fetched_at=? WHERE target_code=?", (t, code))
    elif kind == "verified":
        upsert_state(db, code, old["hash"], old["word"], old["origin"], t)
        if a["recovered"]:
            log_change(db, t, code, "delete_cancelled", old, old, "삭제 의심 후 다시 나타남")
    elif kind in ("edited", "headword_changed"):
        archive_current(db, code, kind, t, old["hash"])
        put_raw(db, code, res, t)
        upsert_state(db, code, res["hash"], res["word"], res["origin"], t, changed=True)
        log_change(db, t, code, kind, old, res)
        if a["recovered"]:
            log_change(db, t, code, "delete_cancelled", old, res, "삭제 의심 후 다시 나타남")
    elif kind == "added":
        put_raw(db, code, res, t)
        db.execute("INSERT OR REPLACE INTO fetch_state(target_code, status, fetched_at, note) VALUES(?,?,?,?)",
                   (code, "ok", t, "sync:added"))
        upsert_state(db, code, res["hash"], res["word"], res["origin"], t, changed=True)
        log_change(db, t, code, "added", None, res, a.get("note"))
    elif kind == "suspect_deleted":
        upsert_state(db, code, old["hash"], old["word"], old["origin"], t, missing_since=ctx["today"])
        log_change(db, t, code, "delete_suspect", old, None, "조회 결과가 비어 있음(확정 전)")
    elif kind == "deleted":
        archive_current(db, code, "deleted", t, old["hash"])
        db.execute("DELETE FROM raw_xml WHERE target_code=?", (code,))
        db.execute("DELETE FROM sync_state WHERE target_code=?", (code,))
        db.execute("INSERT OR REPLACE INTO fetch_state(target_code, status, fetched_at, note) VALUES(?,?,?,?)",
                   (code, "empty", t, "sync:deleted"))
        log_change(db, t, code, "deleted", old, None, "삭제 확정")
    # suspect_waiting, tail_empty: 기록할 것 없음


def is_anomaly(pending, mode):
    """API 장애나 응답 형식 변경을 대량 변경/삭제로 오인하지 않도록 한 묶음 단위로 검사한다."""
    n = len(pending)
    k = Counter(a["kind"] for a, _ in pending)
    deletes = k["suspect_deleted"] + k["deleted"] + k["suspect_waiting"]
    if mode == "updates":
        return n >= 20 and deletes / n > 0.05
    return n >= 50 and (deletes + k["edited"] + k["headword_changed"]) / n > 0.10


def run_refresh(client, db, codes, mode, args, stop_after_empty=0):
    """codes 를 하나씩 다시 받아 비교하고 반영한다. (완료 여부, 집계)를 돌려준다."""
    max_ok = db.execute("SELECT MAX(target_code) FROM fetch_state WHERE status='ok'").fetchone()[0] or 0
    ctx = {"scan_end": args.scan_end, "confirm_days": args.confirm_days, "max_ok": max_ok,
           "today": sf.today(), "now": sf.now()}
    counts, pending = Counter(), []
    state = {"completed": True}
    consecutive_err = consecutive_empty = 0
    alerts = {"headword_changed", "deleted", "suspect_deleted", "added", "delete_cancelled"}

    def flush():
        if not pending:
            return True
        if is_anomaly(pending, mode):
            k = Counter(a["kind"] for a, _ in pending)
            print(f"\n⚠ 이상 징후: 이번 묶음 {len(pending)}건 중 변경·삭제가 비정상적으로 많습니다 {dict(k)}.")
            print("  API 장애나 응답 형식 변경일 수 있어 이 묶음은 반영하지 않고 중단합니다. 잠시 후 다시 실행하세요.")
            pending.clear()
            state["completed"] = False
            return False
        ctx["now"] = sf.now()
        try:
            for a, res in pending:
                apply_action(db, a, res, ctx)
            db.commit()
        except Exception:
            db.rollback()
            raise
        for a, _ in pending:
            counts[a["kind"]] += 1
            if a["kind"] in alerts or args.verbose:
                extra = ""
                if a["kind"] == "headword_changed":
                    extra = f"  ⚠ 표제어/원어 교체: {a['old']['word']} {a['old']['origin']} -> 새 항목"
                print(f"  {a['code']:>7}  {a['kind']}{extra}")
        pending.clear()
        return True

    try:
        for code in codes:
            row = db.execute("SELECT status FROM fetch_state WHERE target_code=?", (code,)).fetchone()
            status = row[0] if row else None
            skip = precheck(status, code, ctx)
            if skip:
                counts[skip] += 1
                continue
            try:
                res = fetch_item(client, code)
            except sf.LimitReached:
                print("\n오늘 호출 한도에 도달했습니다. 반영할 것을 저장하고 멈춥니다.")
                state["completed"] = False
                break
            except sf.FatalApiError as e:
                print(f"\n중단: {e}")
                state["completed"] = False
                break
            except sf.ApiError as e:
                counts["error"] += 1
                consecutive_err += 1
                if consecutive_err >= sf.MAX_CONSECUTIVE_ERRORS:
                    print(f"\n연속 {consecutive_err}회 오류로 중단합니다: {e}")
                    state["completed"] = False
                    break
                continue
            consecutive_err = 0
            a = decide(db, code, status, res, ctx)
            pending.append((a, res))
            consecutive_empty = consecutive_empty + 1 if a["kind"] in ("tail_empty", "still_empty") else 0
            if stop_after_empty and consecutive_empty >= stop_after_empty:
                break
            if len(pending) >= BATCH and not flush():
                break
    except KeyboardInterrupt:
        print("\n중단 요청을 받았습니다. 지금까지 확인한 것을 저장합니다.")
        state["completed"] = False
    flush()
    summarize(counts)
    return state["completed"], counts


LABELS = [("verified", "변화 없음"), ("edited", "내용 수정"), ("headword_changed", "표제어/원어 교체"),
          ("added", "신규"), ("delete_suspect", "삭제 의심"), ("suspect_deleted", "삭제 의심(첫 확인)"),
          ("suspect_waiting", "삭제 의심(대기 중)"), ("deleted", "삭제 확정"), ("still_empty", "여전히 빈 코드"),
          ("tail_empty", "뒤쪽 빈 코드"), ("skip_unscanned", "건너뜀(수집 전)"), ("skip_error", "건너뜀(오류 상태)"),
          ("error", "조회 오류")]


def summarize(counts):
    print("\n요약:")
    shown = False
    for key, label in LABELS:
        if counts.get(key):
            print(f"  {label:<18} {counts[key]:>7,}")
            shown = True
    if not shown:
        print("  (처리한 코드가 없습니다)")


def code_stream(db, args):
    """refresh 대상 코드 목록(또는 뒤쪽 스캔용 무한 스트림)을 만든다."""
    if args.codes:
        return list(args.codes), 0
    if args.sample:
        codes = [r[0] for r in db.execute("SELECT target_code FROM fetch_state WHERE status='ok'")]
        random.Random(args.seed).shuffle(codes)
        return codes[:args.sample], 0
    if args.oldest:
        rows = db.execute(
            "SELECT f.target_code FROM fetch_state f LEFT JOIN sync_state s ON s.target_code=f.target_code "
            "WHERE f.status='ok' ORDER BY COALESCE(s.last_verified, f.fetched_at) ASC, f.target_code LIMIT ?",
            (args.oldest,))
        return [r[0] for r in rows], 0
    if args.empties:
        rows = db.execute("SELECT target_code FROM fetch_state WHERE status='empty' "
                          "ORDER BY fetched_at ASC, target_code LIMIT ?", (args.empties,))
        return [r[0] for r in rows], 0
    max_ok = db.execute("SELECT MAX(target_code) FROM fetch_state WHERE status='ok'").fetchone()[0] or 0
    print(f"뒤쪽 스캔: {max_ok + 1:,} 부터, 빈 코드가 {args.stop_after_empty}개 연속되면 멈춥니다.")
    return itertools.count(max_ok + 1), args.stop_after_empty


def cmd_refresh(args):
    db = open_sync_db()
    client = sf.Client(sf.load_key(), db, args.limit, args.sleep)
    codes, stop = code_stream(db, args)
    completed, _ = run_refresh(client, db, codes, "verify", args, stop_after_empty=stop)
    print(f"오늘 호출 {client.used_today()}/{args.limit}")
    if not completed:
        sys.exit(2)


# ---------------------------------------------------------------- 어깨번호 보충 (supno)
# view.do 응답에는 어깨번호가 없고 search.do 결과에만 sup_no 가 있다. 같은 표제어가 여럿인 것만 검색한다.
SUPNO_MAX_CONSECUTIVE_ISSUES = 20       # 검색 결과가 로컬과 이만큼 연속으로 안 맞으면 API 이상으로 보고 중단


def plain_key(word):
    """어깨번호는 하이픈(-), ^, 띄어쓰기를 뺀 표제어 기준으로 매겨진다(실측: 철자 그대로 비교하면 43그룹이 어긋나고,
    띄어쓰기를 남기면 '감정교육'/'감정 교육'처럼 사전이 한 그룹으로 묶는 29그룹을 놓친다)."""
    return re.sub(r"[-^\s]", "", word)


def supno_groups(db):
    """로컬 ok 원본에서 (하이픈 제거 표제어 -> 코드 목록, 코드 -> 표제어, 링크로 확인된 어깨번호)를 만든다.
    원본의 어휘 링크 표제어는 '계년02'처럼 끝 두 자리가 어깨번호다."""
    words, links = {}, []
    rows = db.execute("SELECT r.target_code, r.xml FROM raw_xml r JOIN fetch_state f ON f.target_code=r.target_code "
                      "WHERE f.status='ok'")
    for code, blob in rows:
        item = chan(ET.fromstring(zlib.decompress(blob).decode("utf-8").strip())).find("item")
        if item is None:
            continue
        words[code] = (item.findtext("word_info/word") or "").strip()
        for l in item.iter("lexical_info"):
            if (l.findtext("unit") or "").strip() == "어휘":
                links.append(((l.findtext("link_target_code") or "").strip(), (l.findtext("word") or "").strip()))
        for l in item.findall("word_info/relation_info"):
            links.append(((l.findtext("link_target_code") or "").strip(), (l.findtext("word") or "").strip()))
    known = {}
    for tc, w in links:
        if not tc.isdigit() or int(tc) not in words:
            continue
        m = re.match(r"^(.*?)(\d{2})$", w)
        if m and m.group(1) == words[int(tc)]:
            known[int(tc)] = int(m.group(2))
    groups = {}
    for code, w in words.items():
        groups.setdefault(plain_key(w), []).append(code)
    return groups, words, known


def supno_pending(db, groups, known, retry_issues):
    """검색해야 할 그룹을 돌려준다. 단독 표제어(어깨번호 0)는 제외하되, 링크가 0 이 아닌 번호를 가리키면
    로컬에 없는 짝이 있다는 뜻이므로 포함한다. (할 그룹, 이미 끝난 그룹 수, 이슈로 건너뛴 그룹 수)"""
    have = {r[0] for r in db.execute("SELECT target_code FROM search_hits WHERE sup_no IS NOT NULL AND sup_no<>''")}
    issues = set() if retry_issues else {r[0] for r in db.execute("SELECT plain FROM supno_issues")}
    need, done, skipped = [], 0, 0
    for plain in sorted(groups):
        codes = groups[plain]
        if len(codes) == 1 and not known.get(codes[0]):
            continue
        if all(c in have for c in codes):
            done += 1
        elif plain in issues:
            skipped += 1
        else:
            need.append(plain)
    return need, done, skipped


def supno_search(client, plain):
    """하이픈을 뺀 표제어를 일치 검색해 모든 동음이의어(하이픈 표제어 포함)를 받는다."""
    hits, start = [], 1
    while True:
        total, items = search(client, plain, "exact", 1, start=start, num=100)
        hits += items
        start += 100
        if not items or len(hits) >= total or start > SEARCH_MAX:
            return total, hits


def supno_check(plain, codes, known, total, hits, all_local=None):
    """검색 결과가 로컬 그룹과 맞는지 검사한다. 이상 없으면 None.
    ('partial', 설명): 번호는 믿을 수 있어 저장하되 알려 둘 것이 있는 경우(검색 색인에 없는 로컬 항목, 번호 0).
    그 밖의 (종류, 설명): 저장하지 않는 이슈."""
    if len(hits) != total:
        return "total_mismatch", f"total {total}, 받은 항목 {len(hits)}"
    remote = {}
    for h in hits:
        if not h["target_code"].isdigit():
            return "bad_code", repr(h["target_code"])
        remote[int(h["target_code"])] = h
    if len(remote) != len(hits):
        return "dup_code", "같은 코드가 여러 번 나옴"
    local = set(codes)
    only_local, only_remote = local - set(remote), set(remote) - local
    note = []
    if only_remote:
        missing = sorted(only_remote - (all_local or set()))
        if missing:                                         # 로컬에 아예 없는 코드: 수집이 놓쳤을 가능성이 있어 저장하지 않는다
            return "remote_only", f"검색에만 있고 로컬에 없는 코드 {missing[:10]}"
        # 로컬에는 있지만 다른 그룹으로 저장된 코드(표기 차이): 번호는 검색 결과가 정확하므로 그대로 쓴다
    if only_local:
        note.append(f"로컬에만 있는 코드(검색 색인에 없음, 번호 알 수 없음) {sorted(only_local)[:10]}")
    nums = []
    for c, h in remote.items():
        if not h["sup_no"].isdigit():
            return "bad_sup_no", f"{c}: {h['sup_no']!r}"
        if plain_key(h["word"]) != plain:
            return "word_mismatch", f"{c}: {h['word']!r}"
        nums.append(int(h["sup_no"]))
    n = len(nums)
    if sorted(nums) != list(range(1, n + 1)) and not (n == 1 and nums == [0]):
        nz = [x for x in nums if x]                         # 0 은 '번호 없는 표기'(예: 같은 철자지만 표기가 달라 번호가 안 붙음)
        if len(nz) != len(set(nz)) or (nz and max(nz) > len(local | set(remote))):
            return "sup_no_sequence", f"번호 {sorted(nums)[:12]}"
        note.append("번호 0(번호 없음)이 섞여 있음" if 0 in nums else "번호에 빈 자리가 있음")
    for c in codes:
        if c in known and c in remote and known[c] != int(remote[c]["sup_no"]):
            return "link_conflict", f"{c}: 링크 {known[c]}, 검색 {remote[c]['sup_no']}"
    return ("partial", "; ".join(note)) if note else None


def supno_status(args):
    """읽기 전용 현황(DB 에 아무것도 쓰지 않는다)."""
    import sqlite3
    db = sqlite3.connect(f"file:{sf.DB_PATH}?mode=ro", uri=True)
    groups, words, known = supno_groups(db)
    has_issues = db.execute("SELECT name FROM sqlite_master WHERE name='supno_issues'").fetchone()
    have = {r[0] for r in db.execute("SELECT target_code FROM search_hits WHERE sup_no IS NOT NULL AND sup_no<>''")}
    issues = {r[0] for r in db.execute("SELECT plain FROM supno_issues")} if has_issues else set()
    multi = [p for p, c in groups.items() if len(c) > 1]
    extra = [p for p, c in groups.items() if len(c) == 1 and known.get(c[0])]
    done = sum(1 for p in multi + extra if all(c in have for c in groups[p]))
    skipped = sum(1 for p in multi + extra if p in issues and not all(c in have for c in groups[p]))
    todo = len(multi) + len(extra) - done - skipped
    print(f"ok 코드 {len(words):,}개, 하이픈 제거 표제어 {len(groups):,}종")
    print(f"  단독(어깨번호 0): {len(groups) - len(multi) - len(extra):,}종")
    print(f"  중복: {len(multi):,}종 ({sum(len(groups[p]) for p in multi):,}코드), 링크가 번호를 가리키는 단독: {len(extra):,}종")
    print(f"  링크로 확인된 어깨번호: {len(known):,}코드, search_hits에 저장된 코드: {len(have):,}")
    print(f"  보충 완료 {done:,}종 / 이슈로 건너뜀 {skipped:,}종 / 남음 {todo:,}종 → 예상 호출 {todo:,}회")


def cmd_supno(args):
    if args.status:
        return supno_status(args)
    db = open_sync_db()
    print("로컬 원본에서 표제어 그룹을 만드는 중...")
    groups, words, known = supno_groups(db)
    if args.words:
        need = []
        for w in args.words:
            p = plain_key(w)
            if p not in groups:
                sys.exit(f"로컬에 없는 표제어입니다: {w}")
            need.append(p)
        done = skipped = 0
    else:
        need, done, skipped = supno_pending(db, groups, known, args.retry_issues)
    print(f"할 그룹 {len(need):,}종 (이미 완료 {done:,}, 이슈로 건너뜀 {skipped:,}) → 호출 {len(need):,}회 예상")
    if args.max_words:
        need = need[:args.max_words]
    if not need:
        return
    client = sf.Client(sf.load_key(), db, args.limit, args.sleep)
    saved_g = saved_c = errors = consecutive_issue = consecutive_err = partial_n = 0
    all_local = set(words)
    issues = Counter()
    completed = True
    try:
        for i, plain in enumerate(need, 1):
            codes = groups[plain]
            try:
                total, hits = supno_search(client, plain)
            except sf.LimitReached:
                print("\n오늘 호출 한도에 도달했습니다. 저장한 것은 유지되며 같은 명령으로 이어받습니다.")
                completed = False
                break
            except sf.FatalApiError as e:
                print(f"\n중단: {e}")
                completed = False
                break
            except sf.ApiError as e:
                errors += 1
                consecutive_err += 1
                if consecutive_err >= sf.MAX_CONSECUTIVE_ERRORS:
                    print(f"\n연속 {consecutive_err}회 오류로 중단합니다: {e}")
                    completed = False
                    break
                continue
            consecutive_err = 0
            problem = supno_check(plain, codes, known, total, hits, all_local)
            partial = bool(problem and problem[0] == "partial")
            if partial:
                partial_n += 1
            if args.dry_run or args.verbose:
                shown = ", ".join(f"{h['word']}={h['sup_no']}({h['target_code']})" for h in hits[:12])
                print(f"  {plain}: total {total}, 로컬 {len(codes)}  {shown}" + (f"  ⚠ {problem[0]}: {problem[1]}" if problem else ""))
            if problem and not partial:
                issues[problem[0]] += 1
                consecutive_issue += 1
                if not args.dry_run:
                    db.execute("INSERT OR REPLACE INTO supno_issues(plain, at, kind, local_n, remote_n, detail) "
                               "VALUES(?,?,?,?,?,?)", (plain, sf.now(), problem[0], len(codes), total, problem[1]))
                    db.commit()
                if consecutive_issue >= SUPNO_MAX_CONSECUTIVE_ISSUES:
                    print(f"\n연속 {consecutive_issue}개 표제어가 로컬과 안 맞아 중단합니다. API 응답 형식이 바뀌었을 수 있습니다.")
                    completed = False
                    break
            else:
                consecutive_issue = 0
                saved_g += 1
                saved_c += len(hits)
                if not args.dry_run:
                    t = sf.now()
                    for h in hits:
                        db.execute("INSERT OR REPLACE INTO search_hits VALUES(?,?,?,?,?,?)",
                                   (int(h["target_code"]), h["word"], h["sup_no"], h["origin"], h["pos"], t))
                    if partial:                                 # 저장은 하되 알려 둘 것을 남긴다(빌드 검증이 예외로 인정)
                        db.execute("INSERT OR REPLACE INTO supno_issues(plain, at, kind, local_n, remote_n, detail) "
                                   "VALUES(?,?,?,?,?,?)", (plain, sf.now(), "partial", len(codes), total, problem[1]))
                    else:                                       # 이전에 이슈였다가 해결된 경우 기록을 지운다
                        db.execute("DELETE FROM supno_issues WHERE plain=?", (plain,))
                    if saved_g % 50 == 0:
                        db.commit()
            if i % 500 == 0:
                print(f"  {i:,}/{len(need):,}  (오늘 호출 {client.used_today()}/{args.limit})")
    except KeyboardInterrupt:
        print("\n중단 요청을 받았습니다. 지금까지 확인한 것을 저장합니다.")
        completed = False
    if not args.dry_run:
        db.commit()
    mode = " (--dry-run: 저장하지 않았습니다)" if args.dry_run else ""
    print(f"\n요약{mode}: 저장 {saved_g:,}종({saved_c:,}코드, 그중 알려 둘 것이 있는 부분 저장 {partial_n:,}종), "
          f"이슈 {sum(issues.values()):,}종, 조회 오류 {errors:,}")
    for kind, n in issues.most_common():
        print(f"  이슈 {kind}: {n:,}")
    print(f"오늘 호출 {client.used_today()}/{args.limit}")
    if not completed:
        sys.exit(2)


# ---------------------------------------------------------------- baseline / report (호출 0건)
def cmd_baseline(args):
    db = open_sync_db()
    codes = [r[0] for r in db.execute(
        "SELECT r.target_code FROM raw_xml r JOIN fetch_state f ON f.target_code=r.target_code "
        "LEFT JOIN sync_state s ON s.target_code=r.target_code "
        "WHERE f.status='ok' AND s.target_code IS NULL ORDER BY r.target_code")]
    print(f"해시가 없는 원본 {len(codes):,}건을 계산합니다(API 호출 없음).")
    done = bad = 0
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        marks = ",".join("?" * len(chunk))
        rows = db.execute(f"SELECT target_code, fetched_at, xml FROM raw_xml WHERE target_code IN ({marks})", chunk).fetchall()
        for code, fetched_at, blob in rows:
            try:
                item = chan(ET.fromstring(zlib.decompress(blob).decode("utf-8").strip())).find("item")
                if item is None:
                    bad += 1
                    continue
                h, w, o = item_info(item)
            except Exception:
                bad += 1
                continue
            db.execute("INSERT OR IGNORE INTO sync_state(target_code, content_hash, word, origin, last_verified) "
                       "VALUES(?,?,?,?,?)", (code, h, w, o, fetched_at))
            done += 1
        db.commit()
        if (i // 500) % 40 == 39:
            print(f"  {min(i + 500, len(codes)):,}/{len(codes):,}")
    print(f"완료: {done:,}건 저장, 건너뜀 {bad:,}건")


def cmd_report(args):
    db = open_sync_db()
    st = dict(db.execute("SELECT status, COUNT(*) FROM fetch_state GROUP BY status"))
    ok = st.get("ok", 0)
    print(f"로컬 상태: ok {ok:,}  empty {st.get('empty', 0):,}  error {st.get('error', 0):,}")
    if args.official:
        print(f"자세히 찾기 전체 표제어 {args.official:,}건과의 차이: {ok - args.official:+,} (수집이 끝나기 전에는 의미 없음)")
    hashed = db.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0]
    print(f"해시 기록: {hashed:,}건 (baseline 필요 {max(ok - hashed, 0):,}건)")
    print(f"마지막 updates 기준일: {meta_get(db, 'last_updates_date') or '없음'}")
    since = (dt.datetime.now() - dt.timedelta(days=args.days)).isoformat(timespec="seconds")
    rows = db.execute("SELECT kind, COUNT(*) FROM changes WHERE at>=? GROUP BY kind ORDER BY 2 DESC", (since,)).fetchall()
    print(f"\n최근 {args.days}일 변경 기록:")
    for kind, n in rows or []:
        print(f"  {kind:<18} {n:>7,}")
    if not rows:
        print("  (없음)")
    sus = db.execute("SELECT target_code, word, missing_since FROM sync_state WHERE missing_since IS NOT NULL "
                     "ORDER BY missing_since LIMIT 20").fetchall()
    if sus:
        print("\n삭제 의심(확정 대기):")
        for code, word, ms in sus:
            print(f"  {code:>7}  {word}  (처음 확인 {ms})")
    warn = db.execute("SELECT at, target_code, old_word, old_origin, new_word, new_origin FROM changes "
                      "WHERE kind='headword_changed' AND at>=? ORDER BY at DESC LIMIT 20", (since,)).fetchall()
    if warn:
        print("\n⚠ 표제어/원어 교체:")
        for at, code, ow, oo, nw, no in warn:
            print(f"  {at}  {code}  {ow} {oo}  ->  {nw} {no}")


def main():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--limit", type=int, default=PROBE_DEFAULT_LIMIT, help="하루 호출 상한")
    common.add_argument("--sleep", type=float, default=0.1)
    common.add_argument("-v", "--verbose", action="store_true")
    safety = argparse.ArgumentParser(add_help=False)
    safety.add_argument("--scan-end", type=int, default=540000,
                        help="수집 범위의 끝(기본 540000). 이 이하에서 아직 수집 안 된 코드는 건너뜁니다")
    safety.add_argument("--confirm-days", type=int, default=1,
                        help="삭제 의심 후 며칠이 지나야 삭제로 확정할지(기본 1: 다른 날 다시 비어 있으면 확정)")

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("probe", parents=[common], help="고친 날짜 검색(update_s/update_e) 동작 시험")
    pr.add_argument("--days", type=int, default=14, help="날짜별 건수를 볼 기간(일, 기본 14)")
    pr.add_argument("--max-items", type=int, default=300, help="대조할 최대 항목 수(기본 300)")
    pr.add_argument("--q", help="'전체' 검색어를 직접 지정(지정하면 A 단계 생략)")
    pr.add_argument("--method", default="include", help="--q 와 함께 쓸 검색 방식(기본 include)")
    pr.add_argument("--target", type=int, default=1, help="--q 와 함께 쓸 찾을 대상(1 표제어, 8 뜻풀이 등, 기본 1)")
    pr.add_argument("--official", type=int, help="자세히 찾기의 전체 표제어 수(비교용)")
    pr.set_defaults(func=cmd_probe)

    up = sub.add_parser("updates", parents=[common, safety], help="고친 날짜 검색으로 변경 코드를 찾아 재확인")
    up.add_argument("--since", help="시작일 YYYYMMDD (생략하면 지난 실행 기준일에서 --overlap 일 전)")
    up.add_argument("--until", help="종료일 YYYYMMDD (기본 오늘)")
    up.add_argument("--overlap", type=int, default=2, help="지난 기준일보다 며칠 앞에서 다시 볼지(기본 2)")
    up.add_argument("--dry-run", action="store_true", help="걸리는 코드만 보고 저장하지 않음")
    up.set_defaults(func=cmd_updates)

    rf = sub.add_parser("refresh", parents=[common, safety], help="이미 받은 코드를 다시 받아 비교")
    g = rf.add_mutually_exclusive_group(required=True)
    g.add_argument("--codes", type=int, nargs="+", metavar="CODE")
    g.add_argument("--sample", type=int, metavar="N", help="무작위 ok 코드 N개")
    g.add_argument("--oldest", type=int, metavar="N", help="가장 오래 확인 안 한 ok 코드 N개")
    g.add_argument("--empties", type=int, metavar="N", help="가장 오래 확인 안 한 빈 코드 N개")
    g.add_argument("--tail", action="store_true", help="최대 ok 코드 뒤쪽 신규 확인")
    rf.add_argument("--stop-after-empty", type=int, default=1000, help="--tail 에서 빈 코드가 N개 연속되면 중단(기본 1000)")
    rf.add_argument("--seed", type=int, default=None, help="--sample 난수 시드")
    rf.set_defaults(func=cmd_refresh)
    rf.set_defaults(limit=sf.DEFAULT_LIMIT)

    su = sub.add_parser("supno", parents=[common], help="어깨번호 보충: 중복 표제어를 검색해 search_hits 에 저장(이어받기)")
    su.add_argument("--status", action="store_true", help="현황과 필요한 호출 수만 보기(호출 0건, DB 읽기 전용)")
    su.add_argument("--words", nargs="+", metavar="WORD", help="지정한 표제어(하이픈은 있어도 없어도 됨)만 처리")
    su.add_argument("--max-words", type=int, metavar="N", help="이번에 처리할 표제어 수 상한(시험용)")
    su.add_argument("--dry-run", action="store_true", help="결과를 출력만 하고 저장하지 않음")
    su.add_argument("--retry-issues", action="store_true", help="이전에 로컬과 안 맞아 건너뛴 표제어도 다시 시도")
    su.set_defaults(func=cmd_supno)
    su.set_defaults(limit=sf.DEFAULT_LIMIT)

    bl = sub.add_parser("baseline", help="이미 받은 원본의 해시를 로컬에서 계산(호출 0건)")
    bl.set_defaults(func=cmd_baseline)

    rp = sub.add_parser("report", help="변경 기록과 현황 요약(호출 0건)")
    rp.add_argument("--days", type=int, default=30, help="변경 기록을 볼 기간(기본 30일)")
    rp.add_argument("--official", type=int, help="자세히 찾기의 전체 표제어 수(비교용)")
    rp.set_defaults(func=cmd_report)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
