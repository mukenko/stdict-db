"""삭제 확정 개수 상한: 흩어져서 비는 API 장애도 대량 삭제로 이어지지 않게 한다 (2026-10-29)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stdict_sync as ss  # noqa: E402


def mk(kinds):
    return [({"kind": k}, None) for k in kinds]


class DeleteCap(unittest.TestCase):
    def test_default_is_small_but_above_normal_daily_rate(self):
        self.assertEqual(ss.MAX_DELETES_PER_RUN, 20)

    def test_scattered_outage_passes_the_ratio_check_but_hits_the_cap(self):
        batch = mk(["deleted"] * 3 + ["same"] * 47)              # 묶음당 6%: 비율 검사(10%)는 통과한다
        self.assertFalse(ss.is_anomaly(batch, "refresh"))
        self.assertFalse(ss.delete_cap_exceeded(0, batch, 20))   # 첫 묶음은 괜찮지만
        self.assertTrue(ss.delete_cap_exceeded(18, batch, 20))   # 쌓여 21번째 확정이 되면 막는다

    def test_exactly_at_the_cap_is_allowed(self):
        self.assertFalse(ss.delete_cap_exceeded(17, mk(["deleted"] * 3), 20))
        self.assertTrue(ss.delete_cap_exceeded(17, mk(["deleted"] * 4), 20))

    def test_only_confirmed_deletions_count(self):
        batch = mk(["suspect_deleted"] * 30 + ["suspect_waiting"] * 10 + ["edited"] * 5)
        self.assertFalse(ss.delete_cap_exceeded(0, batch, 20))   # 의심 표시는 되돌릴 수 있어 세지 않는다

    def test_zero_means_unlimited(self):
        self.assertFalse(ss.delete_cap_exceeded(500, mk(["deleted"] * 500), 0))

    def test_cli_option_exists(self):
        import subprocess
        out = subprocess.run([sys.executable, str(Path(ss.__file__)), "refresh", "--help"], capture_output=True, text=True).stdout
        self.assertIn("--max-deletes", out)


if __name__ == "__main__":
    unittest.main()
