"""stdict_update 테스트: 로그 정리, rsync 와 공유하는 잠금, 할 일 계획, 어깨번호 무효화. API 호출·실제 DB 없이 임시 파일만 쓴다.

실행(저장소 루트): python3 -m unittest tests.test_update -v
"""
import datetime as dt
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stdict_update as su  # noqa: E402


class TrimLog(unittest.TestCase):
    def test_drops_only_old_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "u.log"
            p.write_text("[2026-09-01 08:00:00] 옛 줄\n이어지는 옛 줄(시각 없음)\n"
                         "[2026-10-10 08:00:00] 최근 줄\n이어지는 최근 줄\n", encoding="utf-8")
            su.trim_log(p, days=15, now=dt.datetime(2026, 10, 13, 9, 0, 0))
            self.assertEqual(p.read_text(encoding="utf-8"), "[2026-10-10 08:00:00] 최근 줄\n이어지는 최근 줄\n")

    def test_missing_file_is_fine(self):
        su.trim_log(Path("/nonexistent/none.log"))

    def test_trim_by_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "l.log"
            p.write_text("x")
            old = dt.datetime.now().timestamp() - 20 * 86400
            os.utime(p, (old, old))
            su.trim_by_mtime(p, days=15)
            self.assertEqual(p.read_text(), "")
            p.write_text("y")
            su.trim_by_mtime(p, days=15)
            self.assertEqual(p.read_text(), "y")


class Lock(unittest.TestCase):
    def test_acquire_and_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "lock"
            su.acquire_lock(lock, wait_s=0)
            self.assertEqual((lock / "pid").read_text().strip(), str(os.getpid()))   # rsync 스크립트가 읽는 형식
            self.assertEqual((lock / "owner").read_text().strip(), "stdict_update")
            su.release_lock(lock)
            self.assertFalse(lock.exists())

    def test_stale_lock_is_taken_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "lock"
            lock.mkdir()
            (lock / "pid").write_text("999999\n")                       # 죽은 PID
            su.acquire_lock(lock, wait_s=0)
            self.assertEqual((lock / "pid").read_text().strip(), str(os.getpid()))
            su.release_lock(lock)

    def test_lock_without_pid_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "lock"
            lock.mkdir()
            su.acquire_lock(lock, wait_s=0)
            su.release_lock(lock)

    def test_rsync_running_makes_us_wait_then_give_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "lock"
            lock.mkdir()
            (lock / "pid").write_text(f"{os.getpid()}\n")               # 살아 있는 PID, owner 없음 = rsync 스크립트
            with self.assertRaises(su.LockBusy):
                su.acquire_lock(lock, wait_s=0.3, poll_s=0.1)
            self.assertTrue(lock.exists())                               # 남의 잠금은 건드리지 않는다

    def test_rsync_finishing_lets_us_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "lock"
            lock.mkdir()
            sleeper = subprocess.Popen(["sleep", "30"])
            (lock / "pid").write_text(f"{sleeper.pid}\n")
            try:
                import threading
                threading.Timer(0.4, lambda: (sleeper.kill(), sleeper.wait())).start()
                su.acquire_lock(lock, wait_s=10, poll_s=0.1)
            finally:
                sleeper.kill()
                sleeper.wait()
            su.release_lock(lock)

    def test_second_update_run_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "lock"
            su.acquire_lock(lock, wait_s=0)
            with self.assertRaises(su.AlreadyRunning):
                su.acquire_lock(lock, wait_s=0)
            su.release_lock(lock)

    def test_release_does_not_remove_someone_elses_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "lock"
            lock.mkdir()
            (lock / "pid").write_text("1\n")
            su.release_lock(lock)
            self.assertTrue(lock.exists())


class Plan(unittest.TestCase):
    SUNDAY, MONDAY = dt.date(2026, 10, 18), dt.date(2026, 10, 19)

    def names(self, **kw):
        return [n for n, _ in su.plan_steps(**kw)]

    def test_weekday_has_no_weekly_steps(self):
        self.assertEqual(self.names(today=self.MONDAY), ["updates", "tail", "oldest", "supno", "build"])

    def test_sunday_adds_empties_and_sample(self):
        self.assertEqual(self.names(today=self.SUNDAY), ["updates", "tail", "oldest", "empties", "sample", "supno", "build"])

    def test_weekly_can_be_forced(self):
        self.assertIn("sample", self.names(today=self.MONDAY, weekly=True))

    def test_only_filters_and_oldest_n_is_used(self):
        steps = su.plan_steps(self.MONDAY, only={"updates", "oldest"}, oldest_n=100)
        self.assertEqual([n for n, _ in steps], ["updates", "oldest"])
        self.assertEqual(dict(steps)["oldest"], ["refresh", "--oldest", "100"])

    def test_every_step_is_a_valid_sync_command(self):
        for name, argv in su.plan_steps(self.SUNDAY):
            if argv:
                r = subprocess.run([sys.executable, str(Path(su.ROOT) / "stdict_sync.py"), *argv[:1], "--help"],
                                   capture_output=True, text=True, timeout=30)
                self.assertEqual(r.returncode, 0, name)


def make_db():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE sync_state(target_code INTEGER PRIMARY KEY, content_hash TEXT, word TEXT, origin TEXT, last_verified TEXT);
        CREATE TABLE search_hits(target_code INTEGER PRIMARY KEY, word TEXT, sup_no TEXT, origin TEXT, pos TEXT, seen_at TEXT);
        CREATE TABLE supno_issues(plain TEXT PRIMARY KEY, at TEXT, kind TEXT, local_n INTEGER, remote_n INTEGER, detail TEXT);
        CREATE TABLE changes(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, target_code INTEGER, kind TEXT,
                             old_word TEXT, new_word TEXT, old_origin TEXT, new_origin TEXT, old_hash TEXT, new_hash TEXT, note TEXT);
    """)
    return db


class Invalidate(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        d = self.db
        for code, word in ((1, "감정^교육"), (2, "감정 교육"), (3, "가-도"), (4, "가도"), (5, "나무")):
            d.execute("INSERT INTO sync_state(target_code, word) VALUES(?,?)", (code, word))
            d.execute("INSERT INTO search_hits VALUES(?,?,?,?,?,?)", (code, word, "1", "", "", "2026-10-13"))
        d.execute("INSERT INTO supno_issues VALUES('감정교육','x','partial',2,2,'')")
        d.execute("INSERT INTO supno_issues VALUES('나무','x','partial',2,2,'')")

    def hits(self):
        return {r[0] for r in self.db.execute("SELECT target_code FROM search_hits")}

    def test_added_entry_invalidates_whole_group_including_spacing_variant(self):
        self.db.execute("INSERT INTO changes(at, target_code, kind, old_word, new_word) VALUES('2026-10-14T08:20:00', 6, 'added', NULL, '감정교육')")
        self.db.execute("INSERT INTO sync_state(target_code, word) VALUES(6, '감정교육')")
        n = su.invalidate_groups(self.db, "2026-10-14T08:00:00")
        self.assertEqual(n, 1)
        self.assertEqual(self.hits(), {3, 4, 5})                               # 감정교육 그룹(1,2)만 지워짐
        issues = {r[0] for r in self.db.execute("SELECT plain FROM supno_issues")}
        self.assertEqual(issues, {"나무"})

    def test_headword_change_invalidates_old_and_new_groups(self):
        self.db.execute("UPDATE sync_state SET word='나무' WHERE target_code=4")      # 가도 -> 나무 로 교체됐다고 가정
        self.db.execute("INSERT INTO changes(at, target_code, kind, old_word, new_word) VALUES('2026-10-14T08:20:00', 4, 'headword_changed', '가도', '나무')")
        su.invalidate_groups(self.db, "2026-10-14T08:00:00")
        self.assertEqual(self.hits(), {1, 2})                                  # 가도 그룹(3,4)과 나무 그룹(4,5) 지워짐

    def test_deleted_entry_invalidates_remaining_siblings(self):
        self.db.execute("DELETE FROM sync_state WHERE target_code=3")
        self.db.execute("INSERT INTO changes(at, target_code, kind, old_word, new_word) VALUES('2026-10-14T08:20:00', 3, 'deleted', '가-도', NULL)")
        su.invalidate_groups(self.db, "2026-10-14T08:00:00")
        self.assertEqual(self.hits(), {1, 2, 5})

    def test_old_changes_and_edits_do_not_invalidate(self):
        self.db.execute("INSERT INTO changes(at, target_code, kind, old_word, new_word) VALUES('2026-10-01T08:20:00', 5, 'added', NULL, '나무')")
        self.db.execute("INSERT INTO changes(at, target_code, kind, old_word, new_word) VALUES('2026-10-14T08:20:00', 5, 'edited', '나무', '나무')")
        self.assertEqual(su.invalidate_groups(self.db, "2026-10-14T08:00:00"), 0)
        self.assertEqual(self.hits(), {1, 2, 3, 4, 5})


class Build(unittest.TestCase):
    def test_needs_build(self):
        self.assertFalse(su.needs_build({}, 0))
        self.assertFalse(su.needs_build({"delete_suspect": 3, "delete_cancelled": 1}, 0))   # 아직 확정 안 된 것은 빌드 대상 아님
        for kind in ("added", "edited", "headword_changed", "deleted"):
            self.assertTrue(su.needs_build({kind: 1}, 0), kind)
        self.assertTrue(su.needs_build({}, 5))                                              # 어깨번호가 새로 저장됨

    def test_change_counts(self):
        db = make_db()
        self.addCleanup(db.close)
        db.execute("INSERT INTO changes(at, target_code, kind) VALUES('2026-10-14T08:20:00', 1, 'edited')")
        db.execute("INSERT INTO changes(at, target_code, kind) VALUES('2026-10-14T08:21:00', 2, 'edited')")
        db.execute("INSERT INTO changes(at, target_code, kind) VALUES('2026-10-01T08:21:00', 2, 'added')")
        self.assertEqual(su.change_counts(db, "2026-10-14T08:00:00"), {"edited": 2})


class Cli(unittest.TestCase):
    def test_dry_run_makes_no_calls_and_shows_plan(self):
        r = subprocess.run([sys.executable, str(Path(su.ROOT) / "stdict_update.py"), "--dry-run", "--weekly"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in ("updates", "tail", "oldest", "empties", "sample", "supno", "build"):
            self.assertIn(name, r.stdout)
        self.assertIn("--limit 20000", r.stdout)


if __name__ == "__main__":
    unittest.main()
