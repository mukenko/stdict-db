"""형식 변경 모드: 영향받는 항목만 보류하고 나머지는 계속 갱신하며, 복구되면 자동으로 끝낸다 (2026-11-22)."""
import argparse
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stdict_fetch as sf  # noqa: E402
import stdict_sync as ss  # noqa: E402
import stdict_update as su  # noqa: E402

OLD_LINK = "https://stdict.korean.go.kr/search/searchView.do?word_no=1&searchKeywordTo=3"
NEW_LINK = "https://kdict.korean.go.kr/standard/search/searchView.do?wordNo=1&searchKeywordTo=3"


def entry(code, rel_type="동의어", link=OLD_LINK, definition="뜻", with_rel=True):
    rel = (f"<lexical_info><word>나</word><unit>의미</unit><type>{rel_type}</type>"
           f"<link_target_code>9</link_target_code><link><![CDATA[{link}]]></link></lexical_info>") if with_rel else ""
    return (f"<channel><total>1</total><item><target_code>{code}</target_code><word_info><word>말{code}</word>"
            f"<pos_info><comm_pattern_info><sense_info><definition>{definition}</definition>{rel}</sense_info>"
            f"</comm_pattern_info></pos_info></word_info></item></channel>")


EMPTY = "<channel><total>0</total></channel>"


class FakeClient(sf.Client):
    def __init__(self, db, server):
        super().__init__("k", db, 100000, 0)
        self.server = dict(server)

    def call(self, path, params):
        return self.server.get(int(params["q"]), EMPTY)


class DriftCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = sf.DB_PATH
        sf.DB_PATH = Path(self.tmp.name) / "raw.sqlite"
        self.addCleanup(setattr, sf, "DB_PATH", old)
        self.db = ss.open_sync_db()
        self.addCleanup(self.db.close)
        self.args = argparse.Namespace(scan_end=540000, confirm_days=1, verbose=False, max_deletes=20)

    def seed(self, code, status="ok", **kw):
        if status == "ok":
            self.db.execute("INSERT INTO raw_xml VALUES(?,?,?)", (code, "2026-11-01T00:00:00", zlib.compress(entry(code, **kw).encode())))
        self.db.execute("INSERT INTO fetch_state VALUES(?,?,?,?)", (code, status, "2026-11-01T00:00:00", None))
        self.db.commit()

    def run_codes(self, server, codes, mode="verify"):
        client = FakeClient(self.db, server)
        return ss.run_refresh(client, self.db, codes, mode, self.args)

    def queue(self):
        return [r[0] for r in self.db.execute("SELECT target_code FROM format_drift_held ORDER BY 1")]

    def raw(self, code):
        return zlib.decompress(self.db.execute("SELECT xml FROM raw_xml WHERE target_code=?", (code,)).fetchone()[0]).decode()

    def mark(self):
        return ss.meta_get(self.db, ss.DRIFT_MARK)


class HoldInsteadOfAbort(DriftCase):
    def test_only_the_drifted_entries_are_held_and_the_rest_continues(self):
        for c in (1, 2, 3):
            self.seed(c)
        server = {1: entry(1, rel_type="", link=NEW_LINK),                     # 형식 변경
                  2: entry(2, definition="새 뜻"),                              # 진짜 수정(형식 정상)
                  3: entry(3)}                                                  # 그대로
        completed, counts = self.run_codes(server, [1, 2, 3])
        self.assertTrue(completed)                                              # 중단하지 않는다
        self.assertEqual((counts["format_drift"], counts["edited"], counts["verified"]), (1, 1, 1))
        self.assertIn("동의어", self.raw(1))                                    # 보류: 이전 버전 유지
        self.assertIn("새 뜻", self.raw(2))                                     # 정상 수정은 반영
        self.assertEqual(self.queue(), [1])
        self.assertTrue(self.mark())

    def test_held_entry_counts_as_checked_so_the_oldest_cycle_moves_on(self):
        self.seed(1)
        self.run_codes({1: entry(1, rel_type="", link=NEW_LINK)}, [1])
        self.assertGreater(self.db.execute("SELECT last_verified FROM sync_state WHERE target_code=1").fetchone()[0], "2026-11-02")

    def test_holding_again_does_not_duplicate_the_change_record(self):
        self.seed(1)
        server = {1: entry(1, rel_type="", link=NEW_LINK)}
        self.run_codes(server, [1])
        self.run_codes(server, [1])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM changes WHERE kind='format_drift'").fetchone()[0], 1)
        self.assertEqual(self.queue(), [1])

    def test_a_flood_of_drift_does_not_abort_the_batch(self):
        for c in range(1, 61):
            self.seed(c)
        completed, counts = self.run_codes({c: entry(c, rel_type="", link=NEW_LINK) for c in range(1, 61)}, list(range(1, 61)))
        self.assertTrue(completed)
        self.assertEqual(counts["format_drift"], 60)

    def test_new_entry_in_the_new_format_is_not_stored(self):
        self.seed(7, status="empty")
        completed, counts = self.run_codes({7: entry(7, rel_type="", link=NEW_LINK)}, [7])
        self.assertEqual(counts["format_drift"], 1)
        self.assertIsNone(self.db.execute("SELECT 1 FROM raw_xml WHERE target_code=7").fetchone())
        self.assertEqual(self.db.execute("SELECT status FROM fetch_state WHERE target_code=7").fetchone()[0], "empty")
        self.assertEqual(self.db.execute("SELECT is_new FROM format_drift_held WHERE target_code=7").fetchone()[0], 1)

    def test_new_entry_in_the_normal_format_is_added(self):
        self.seed(7, status="empty")
        _, counts = self.run_codes({7: entry(7)}, [7])
        self.assertEqual(counts["added"], 1)
        self.assertEqual(self.queue(), [])


class Recovery(DriftCase):
    def setUp(self):
        super().setUp()
        for c in (1, 2):
            self.seed(c)
        self.run_codes({c: entry(c, rel_type="", link=NEW_LINK) for c in (1, 2)}, [1, 2])    # 모드 켜짐
        self.assertTrue(self.mark())

    def settle(self, server):
        _, counts = self.run_codes(server, self.queue())
        return ss.settle_drift(self.db, counts)

    def test_still_drifted_keeps_the_mode(self):
        self.assertEqual(self.settle({c: entry(c, rel_type="", link=NEW_LINK) for c in (1, 2)}), "still")
        self.assertTrue(self.mark())
        self.assertEqual(self.queue(), [1, 2])

    def test_recovery_ends_the_mode_and_applies_real_edits_made_meanwhile(self):
        result = self.settle({1: entry(1, definition="그 사이 고친 뜻"), 2: entry(2)})      # 형식 정상 + 1번은 실제 수정
        self.assertEqual(result, "recovered")
        self.assertIsNone(self.mark())
        self.assertEqual(self.queue(), [])                                                 # 보류 해제
        self.assertIn("그 사이 고친 뜻", self.raw(1))

    def test_empty_queue_with_the_mode_on_ends_it(self):
        self.db.execute("DELETE FROM format_drift_held")
        self.db.commit()
        self.assertEqual(ss.settle_drift(self.db, {}), "recovered")
        self.assertIsNone(self.mark())

    def test_no_mode_no_change(self):
        self.db.execute("DELETE FROM sync_meta WHERE key=?", (ss.DRIFT_MARK,))
        self.db.commit()
        self.assertIsNone(ss.settle_drift(self.db, {"verified": 3}))


class UpdatePlanning(unittest.TestCase):
    def test_probe_while_the_mode_is_on(self):
        self.assertEqual(su.held_per_run("2026-11-22T01:00:00", 500), su.HELD_PROBE)
        self.assertEqual(su.held_per_run("2026-11-22T01:00:00", 0), su.HELD_PROBE)

    def test_catch_up_after_recovery_only_if_something_is_left(self):
        self.assertEqual(su.held_per_run(None, 500), su.HELD_CATCHUP)
        self.assertEqual(su.held_per_run(None, 0), 0)

    def test_held_step_follows_updates_and_is_absent_when_zero(self):
        import datetime as dt
        names = [n for n, _ in su.plan_steps(dt.date(2026, 11, 24), held_n=20)]
        self.assertEqual(names[:3], ["updates", "held", "tail"])
        self.assertNotIn("held", [n for n, _ in su.plan_steps(dt.date(2026, 11, 24))])


if __name__ == "__main__":
    unittest.main()
