#!/usr/bin/env python3
"""표준국어대사전 정기 업데이트: 변경 확인 -> 어깨번호 보충 -> 조회용 DB 재빌드를 한 번에 돌린다. 표준 라이브러리만 사용.

  python3 stdict_update.py                 # 오늘 할 일 전체(매일 launchd 가 부름)
  python3 stdict_update.py --dry-run       # 할 일만 보여 줌(호출·변경 없음)
  python3 stdict_update.py --steps updates,tail --oldest-n 100   # 일부 단계만(시험용)
  python3 stdict_update.py --status        # 마지막 실행 결과와 로그 끝부분

매일: updates(고친 날짜 검색) -> refresh --tail(뒤쪽 신규) -> refresh --oldest N(가장 오래 확인 안 한 코드를 돌려가며 재확인)
일요일 추가: refresh --empties(빈 코드 재확인), refresh --sample(무작위 표본으로 날짜 검색이 놓친 변경 점검)
그다음: 바뀐 표제어 그룹의 어깨번호 기록을 무효화 -> supno(필요한 것만) -> 바뀐 것이 있으면 stdict_build.py -> 체크포인트

DB 를 쓰는 동안 폴더 동기화 작업(rsync 등)이 불완전한 사본을 다른 기기로 보내지 않도록, 동기화 스크립트가 쓰는 잠금 폴더를 같이 잡는다
(기본 /tmp/rsync_to_rpi.lock, 환경변수 STDICT_UPDATE_LOCK 로 바꿈; 안의 pid 파일의 프로세스가 살아 있으면 동기화 스크립트가 건너뛰는 방식).
잠금을 안 쓰는 환경이면 --no-lock 으로 끈다. 끝나면 다음 동기화 주기에 새 사본이 간다.

로그: data/update.log (줄마다 [날짜 시간], 실행할 때마다 15일 지난 줄을 지운다).
인증키·요청 주소는 출력하지 않는다. 표제어 교체·삭제 확정 같은 드문 일이 있으면 macOS 알림을 띄운다.
"""
import argparse
import datetime as dt
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import stdict_fetch as sf
import stdict_sync as ss

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "data" / "update.log"
LAUNCHD_LOG = ROOT / "data" / "update_launchd.log"
RSYNC_LOCK = Path(os.environ.get("STDICT_UPDATE_LOCK") or "/tmp/rsync_to_rpi.lock")   # 동기화 스크립트와 같은 잠금 폴더
LOG_KEEP_DAYS = 15                                    # projects/CLAUDE.md 작업 원칙 1
CALL_LIMIT = 20000                                    # 하루 전체 호출이 이 값에 닿으면 멈춘다(수동 작업 여유)
OLDEST_PER_DAY = 4000                                 # 약 110일에 전체를 한 바퀴(날짜 검색이 못 잡는 삭제·수정 점검)
EMPTIES_WEEKLY = 3000
SAMPLE_WEEKLY = 2000
WEEKLY_DAY = 6                                        # 일요일(월요일=0)
ALERT_KINDS = ("headword_changed", "deleted", "added")
BUILD_KINDS = ("added", "edited", "headword_changed", "deleted")


class LockBusy(Exception):
    pass


class AlreadyRunning(Exception):
    pass


# ---------------------------------------------------------------- 로그
def stamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg, path=None):
    path = path or LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{stamp()}] {msg}\n")
    if sys.stdout.isatty():
        print(msg, flush=True)


def trim_log(path, days=LOG_KEEP_DAYS, now=None):
    """[YYYY-MM-DD HH:MM:SS] 로 시작하는 줄 중 days 일 지난 것을 지운다. 시각 없는 줄은 그 앞 줄과 함께 취급한다."""
    path = Path(path)
    if not path.exists():
        return
    cutoff = ((now or dt.datetime.now()) - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    keep, keeping = [], True
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True):
        m = re.match(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
        if m:
            keeping = m.group(1) >= cutoff
        if keeping:
            keep.append(line)
    path.write_text("".join(keep), encoding="utf-8")


def trim_by_mtime(path, days=LOG_KEEP_DAYS):
    """launchd 가 직접 받는 로그(줄마다 시간 없음): 마지막 수정 후 days 일이 지났으면 비운다."""
    path = Path(path)
    if path.exists() and time.time() - path.stat().st_mtime > days * 86400:
        path.write_text("")


# ---------------------------------------------------------------- rsync 와 공유하는 잠금
def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def acquire_lock(lock=None, wait_s=1800, poll_s=20):
    """잠금 폴더를 만든다. 동기화 작업이 돌고 있으면 wait_s 초까지 기다리고, 이 작업이 이미 돌고 있으면 AlreadyRunning."""
    lock = lock or RSYNC_LOCK
    lock = Path(lock)
    deadline = time.time() + wait_s
    while True:
        try:
            lock.mkdir()
            (lock / "owner").write_text("stdict_update\n")
            (lock / "pid").write_text(f"{os.getpid()}\n")
            return
        except FileExistsError:
            pass
        try:
            pid = int((lock / "pid").read_text().strip())
        except (OSError, ValueError):
            pid = None
        if pid is None or not _alive(pid):                 # 비정상 종료로 남은 잠금
            for f in ("pid", "owner"):
                try:
                    (lock / f).unlink()
                except OSError:
                    pass
            try:
                lock.rmdir()
            except OSError:
                pass
            continue
        owner = (lock / "owner").read_text().strip() if (lock / "owner").exists() else "rsync"
        if owner == "stdict_update":
            raise AlreadyRunning(f"이미 실행 중입니다(PID {pid})")
        if time.time() >= deadline:
            raise LockBusy(f"rsync(PID {pid})가 {wait_s}초 안에 끝나지 않았습니다")
        time.sleep(poll_s)


def release_lock(lock=None):
    lock = Path(lock or RSYNC_LOCK)
    try:
        if (lock / "owner").exists() and int((lock / "pid").read_text().strip()) == os.getpid():
            for f in ("pid", "owner"):
                (lock / f).unlink()
            lock.rmdir()
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------- 할 일 계획
def plan_steps(today, only=None, oldest_n=OLDEST_PER_DAY, weekly=None):
    """(이름, sync 인자 목록) 목록. build 는 별도 처리. only 가 있으면 그 이름만."""
    if weekly is None:
        weekly = today.weekday() == WEEKLY_DAY
    steps = [("updates", ["updates"]),
             ("tail", ["refresh", "--tail"]),
             ("oldest", ["refresh", "--oldest", str(oldest_n)])]
    if weekly:
        steps += [("empties", ["refresh", "--empties", str(EMPTIES_WEEKLY)]),
                  ("sample", ["refresh", "--sample", str(SAMPLE_WEEKLY)])]
    steps += [("supno", ["supno"])]
    steps += [("build", None)]
    if only:
        steps = [s for s in steps if s[0] in only]
    return steps


# ---------------------------------------------------------------- 어깨번호 무효화·빌드 필요 여부
def invalidate_groups(db, since):
    """since(ISO 시각) 이후 신규·표제어 교체·삭제된 코드의 표제어 그룹(하이픈·^·띄어쓰기 무시)에 대해,
    저장해 둔 어깨번호(search_hits)와 이슈 기록을 지워 supno 가 다시 조회하게 한다. 지운 그룹 수를 돌려준다."""
    rows = db.execute("SELECT target_code, old_word, new_word FROM changes WHERE at>=? AND kind IN "
                      "('added','headword_changed','deleted')", (since,)).fetchall()
    if not rows:
        return 0
    keys, touched = set(), set()
    for code, old, new in rows:
        touched.add(code)
        for w in (old, new):
            if w:
                keys.add(ss.plain_key(w))
    siblings = {c for c, w in db.execute("SELECT target_code, word FROM sync_state") if ss.plain_key(w or "") in keys}
    codes = sorted(touched | siblings)
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        db.execute(f"DELETE FROM search_hits WHERE target_code IN ({','.join('?' * len(chunk))})", chunk)
    for k in keys:
        db.execute("DELETE FROM supno_issues WHERE plain=?", (k,))
    db.commit()
    return len(keys)


def change_counts(db, since):
    return dict(db.execute("SELECT kind, COUNT(*) FROM changes WHERE at>=? GROUP BY kind", (since,)).fetchall())


KIND_LABEL = {"headword_changed": "표제어 교체", "deleted": "삭제", "added": "신규"}
ALERT_FILE = ROOT / "data" / "last_alert.txt"


def alert_lines(db, since):
    rows = db.execute("SELECT kind, target_code, old_word, new_word FROM changes WHERE at>=? AND kind IN (?,?) ORDER BY id",
                      (since, "headword_changed", "deleted")).fetchall()
    out = []
    for kind, code, old, new in rows:
        out.append(f"{KIND_LABEL[kind]} {code}: {old} -> {new}" if kind == "headword_changed" else f"{KIND_LABEL[kind]} {code}: {old}")
    return out


def needs_build(counts, supno_saved):
    return any(counts.get(k) for k in BUILD_KINDS) or supno_saved > 0


# ---------------------------------------------------------------- 실행
def _on_term(signum, frame):
    raise KeyboardInterrupt


def notify(msg):
    msg = re.sub(r'["\\\n]', " ", str(msg))[:200]            # osascript 문자열이 깨지지 않게
    try:
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "stdict 업데이트"'],
                       capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def run_step(name, argv, limit):
    """stdict_sync.py 를 자식 프로세스로 돌리고 출력을 로그에 남긴다. (종료 코드, 이상 징후 여부)를 돌려준다."""
    return _run(name, [sys.executable, "-u", str(ROOT / "stdict_sync.py")] + argv + ["--limit", str(limit)])


def _run(name, cmd):
    log(f"[{name}] 시작")
    anomaly = False
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line or re.match(r"^\s+\d[\d,]*/\d[\d,]*\s", line):    # 진행 줄(…/…)은 로그에 안 남긴다
                continue
            if "이상 징후" in line:
                anomaly = True
            log(f"[{name}] {line.strip()[:200]}")
        rc = proc.wait()
    except BaseException:
        proc.send_signal(signal.SIGINT)                                   # 자식이 지금까지의 것을 저장하고 끝내게 한다
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise
    log(f"[{name}] 종료 코드 {rc}")
    return rc, anomaly


def checkpoint():
    db = sqlite3.connect(sf.DB_PATH)
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()


def meta_write(**kv):
    db = ss.open_sync_db()
    try:
        for k, v in kv.items():
            ss.meta_set(db, f"update_{k}", str(v))
    finally:
        db.close()


def do_status():
    db = sqlite3.connect(f"file:{sf.DB_PATH}?mode=ro", uri=True)
    meta = dict(db.execute("SELECT key, value FROM sync_meta WHERE key LIKE 'update_%'").fetchall())
    for k in ("last_start", "last_end", "last_result", "last_summary"):
        print(f"{k}: {meta.get('update_' + k, '(없음)')}")
    print(f"updates 기준일: {dict(db.execute('SELECT key, value FROM sync_meta')).get('last_updates_date', '(없음)')}")
    if LOG_PATH.exists():
        print("\n--- 로그 끝")
        for line in LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]:
            print(line)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="할 일만 보여 줌")
    ap.add_argument("--status", action="store_true", help="마지막 실행 결과와 로그 끝부분")
    ap.add_argument("--alerts", action="store_true", help="마지막 알림(표제어 교체·삭제)의 전체 내용")
    ap.add_argument("--steps", help="일부 단계만(쉼표로 구분: updates,tail,oldest,empties,sample,supno,build)")
    ap.add_argument("--weekly", action="store_true", help="요일과 상관없이 주간 단계(empties, sample)도 실행")
    ap.add_argument("--oldest-n", type=int, default=OLDEST_PER_DAY, help=f"refresh --oldest 개수(기본 {OLDEST_PER_DAY})")
    ap.add_argument("--limit", type=int, default=CALL_LIMIT, help=f"하루 전체 호출 상한(기본 {CALL_LIMIT})")
    ap.add_argument("--force-build", action="store_true", help="바뀐 것이 없어도 빌드")
    ap.add_argument("--no-lock", action="store_true", help="rsync 잠금을 잡지 않음(시험용)")
    ap.add_argument("--lock-wait", type=int, default=1800, help="rsync 가 돌고 있을 때 기다리는 최대 초(기본 1800)")
    args = ap.parse_args()
    if args.status:
        return do_status()
    if args.alerts:
        print(ALERT_FILE.read_text(encoding="utf-8") if ALERT_FILE.exists() else "(알림 기록 없음)")
        return
    only = set(args.steps.split(",")) if args.steps else None
    steps = plan_steps(dt.date.today(), only, args.oldest_n, True if args.weekly else None)
    if args.dry_run:
        print("할 일(호출·변경 없음):")
        for name, argv in steps:
            print(f"  {name:<8} " + (" ".join(argv) + f" --limit {args.limit}" if argv else "stdict_build.py (바뀐 것이 있을 때)"))
        print(f"  로그 {LOG_PATH}, 잠금 {RSYNC_LOCK}, 하루 호출 상한 {args.limit}")
        return

    trim_log(LOG_PATH)
    trim_by_mtime(LAUNCHD_LOG)
    signal.signal(signal.SIGTERM, _on_term)                 # launchd 가 멈출 때도 자식을 저장시키고 잠금을 정리한다
    try:
        if not args.no_lock:
            acquire_lock(wait_s=args.lock_wait)
    except AlreadyRunning as e:
        log(f"건너뜀: {e}")
        return
    except LockBusy as e:
        log(f"중단: {e}. 다음 실행에서 다시 시도")
        notify("rsync 가 오래 돌고 있어 업데이트를 건너뜀")
        sys.exit(1)
    start_iso = sf.now()
    meta_write(last_start=start_iso, last_result="running")
    log(f"=== 정기 업데이트 시작 (할 일 {', '.join(n for n, _ in steps)}; 하루 호출 상한 {args.limit}) ===")
    result, failed, incomplete, aborted = "ok", [], [], False
    supno_saved = 0
    try:
        for name, argv in steps:
            if name == "build":
                continue
            if name == "supno":
                db = ss.open_sync_db()
                try:
                    n = invalidate_groups(db, start_iso)
                    before = db.execute("SELECT COUNT(*) FROM search_hits").fetchone()[0]
                finally:
                    db.close()
                if n:
                    log(f"[supno] 바뀐 표제어 그룹 {n}개의 어깨번호 기록을 무효화")
            rc, anomaly = run_step(name, argv, args.limit)
            if name == "supno":
                db = sqlite3.connect(f"file:{sf.DB_PATH}?mode=ro", uri=True)
                try:
                    supno_saved = max(db.execute("SELECT COUNT(*) FROM search_hits").fetchone()[0] - before, 0) + (1 if n else 0)
                finally:
                    db.close()
            if anomaly:
                aborted = True
                failed.append(f"{name}(이상 징후)")
                log(f"중단: [{name}]에서 API 이상 징후. 나머지 단계와 빌드를 하지 않음")
                break
            if rc == 2:
                incomplete.append(name)
            elif rc != 0:
                failed.append(f"{name}(종료 코드 {rc})")
        db = sqlite3.connect(f"file:{sf.DB_PATH}?mode=ro", uri=True)
        try:
            counts = change_counts(db, start_iso)
            alerts = alert_lines(db, start_iso)
        finally:
            db.close()
        built = False
        if not aborted and (args.force_build or needs_build(counts, supno_saved)) and (not only or "build" in only):
            checkpoint()
            rc, _ = _run("build", [sys.executable, "-u", str(ROOT / "stdict_build.py"), "--require-supno"])
            built = rc == 0
            if rc != 0:
                failed.append(f"build(종료 코드 {rc}, 조회 중인 DB는 그대로)")
        elif not aborted:
            log("[build] 바뀐 것이 없어 건너뜀")
        checkpoint()
        calls = sqlite3.connect(f"file:{sf.DB_PATH}?mode=ro", uri=True)
        try:
            used = calls.execute("SELECT COALESCE((SELECT calls FROM api_usage WHERE day=date('now','localtime')),0)").fetchone()[0]
        finally:
            calls.close()
        if failed:
            result = "failed"
        elif aborted or incomplete:
            result = "incomplete"
        summary = (f"변경 {dict(counts) or '없음'}, 어깨번호 갱신 {'예' if supno_saved else '아니오'}, 빌드 {'했음' if built else '안 함'}, "
                   f"미완료 단계 {incomplete or '없음'}, 실패 {failed or '없음'}, 오늘 호출 {used}/{args.limit}")
        log(f"=== 끝: {result} — {summary} ===")
        meta_write(last_end=sf.now(), last_result=result, last_summary=summary)
        rare = {k: counts[k] for k in ALERT_KINDS if counts.get(k)}
        if failed:
            notify("업데이트 실패: " + ", ".join(failed)[:150])
        elif rare.get("headword_changed") or rare.get("deleted"):
            ALERT_FILE.write_text(f"{sf.now()}\n" + "\n".join(alerts) + "\n", encoding="utf-8")
            kinds = ", ".join(f"{KIND_LABEL[k]} {v}건" for k, v in rare.items() if k in KIND_LABEL)
            notify(f"{kinds}. 자세히: stdict-db 폴더에서 python3 stdict_update.py --alerts")
        if failed:
            sys.exit(1)
    except KeyboardInterrupt:
        log("중단 요청을 받았습니다")
        meta_write(last_end=sf.now(), last_result="interrupted")
        sys.exit(130)
    finally:
        if not args.no_lock:
            release_lock()


if __name__ == "__main__":
    main()
