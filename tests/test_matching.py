"""配对算法测试：最大配对数、最小总时间差、稳定性、待核分组。"""
import unittest

from birdsurvey.matching import build_conclusions
from birdsurvey.storage import CONFIRMED, PARTIAL, Coverage


def rec(rid, station, ts, species="HERON", period=0):
    return {
        "id": rid,
        "station": station,
        "period": period,
        "ts": float(ts),
        "species": species,
    }


class FakeSnapshot:
    """仅实现 build_conclusions 需要的 coverage 查询接口。"""

    def __init__(self, coverages):
        self._cov = {(c.station, c.period): c for c in coverages}
        self.generation = 1

    def coverage(self, station, period):
        return self._cov.get((station, period))


def confirmed_both():
    return [
        Coverage("A", 0, CONFIRMED, 0, 3600, 10, 1, 1, 8, 2),
        Coverage("B", 0, CONFIRMED, 0, 3600, 10, 1, 1, 8, 2),
    ]


class TestMatching(unittest.TestCase):
    def test_two_second_threshold_and_max_cardinality(self):
        # 3 条 A、2 条 B，候选图完整时最大配对数为 2；
        # 再用最小总时间差决定具体配对（B2 只能配距离最近的 A1）。
        records = [
            rec("A1", "A", 100.0),
            rec("A2", "A", 100.4),
            rec("A3", "A", 100.8),
            rec("B1", "B", 101.9),
            rec("B2", "B", 100.1),
        ]
        out = build_conclusions(FakeSnapshot(confirmed_both()), records)
        self.assertEqual(out["summary"]["pairs"], 2)
        pairing = {r["record_b_id"]: r["record_a_id"] for r in out["results"] if r["pair_id"]}
        self.assertEqual(pairing["B2"], "A1")   # 全局最小边 0.1s
        self.assertEqual(pairing["B1"], "A3")   # 剩余记录中的最小边 1.1s
        self.assertAlmostEqual(out["summary"]["total_time_diff"], 1.2, places=3)
        # 未配对记录仍出现在结论里
        unpaired_ids = [r["record_a_id"] for r in out["results"] if r["pair_id"] is None]
        self.assertEqual(unpaired_ids, ["A2"])

    def test_min_total_diff_among_equal_cardinality(self):
        # A1 可以配 B1(Δ1.8) 或 B2(Δ0.4)；只有一对的最优是 B2
        records = [
            rec("A1", "A", 100.0),
            rec("B1", "B", 101.8),
            rec("B2", "B", 100.4),
        ]
        out = build_conclusions(FakeSnapshot(confirmed_both()), records)
        self.assertEqual(out["summary"]["pairs"], 1)
        pair = next(r for r in out["results"] if r["pair_id"])
        self.assertEqual(pair["record_b_id"], "B2")
        self.assertAlmostEqual(pair["time_diff"], 0.4, places=3)

    def test_threshold_boundary_and_species_mismatch(self):
        records = [
            rec("A1", "A", 100.0, "HERON"),
            rec("B_exact", "B", 102.0, "HERON"),       # 恰好 2s，可配
            rec("B_over", "B", 102.01, "HERON"),        # 超界，不可配
            rec("B_other", "B", 100.5, "TERN"),         # 物种不同
        ]
        out = build_conclusions(FakeSnapshot(confirmed_both()), records)
        self.assertEqual(out["summary"]["pairs"], 1)
        pair = next(r for r in out["results"] if r["pair_id"])
        self.assertEqual(pair["record_b_id"], "B_exact")

    def test_stable_output_across_runs_and_input_order(self):
        records = [
            rec("A1", "A", 100.0), rec("A2", "A", 100.2),
            rec("B1", "B", 100.1), rec("B2", "B", 100.3),
        ]
        out1 = build_conclusions(FakeSnapshot(confirmed_both()), records)
        shuffled = list(reversed(records))
        out2 = build_conclusions(FakeSnapshot(confirmed_both()), shuffled)
        ids1 = [(r["record_a_id"], r["record_b_id"]) for r in out1["results"]]
        ids2 = [(r["record_a_id"], r["record_b_id"]) for r in out2["results"]]
        self.assertEqual(ids1, ids2)

    def test_connected_component_pending_as_whole(self):
        # A1 同时与 B1、B2 相邻 => 三条记录同组；
        # B 站 p0 未确认 => 该组待核，即使有确定配对也不出正式结论。
        # 另一组放在 p1（两站均确认），照常出正式结论。
        records = [
            rec("A1", "A", 100.0, period=0),
            rec("B1", "B", 100.5, period=0),
            rec("B2", "B", 101.2, period=0),
            rec("A9", "A", 900.0, period=1),
            rec("B9", "B", 900.3, period=1),
        ]
        cov = confirmed_both()
        cov[1].status = PARTIAL  # B/p0 未确认
        cov.append(
            Coverage("A", 1, CONFIRMED, 3600, 7200, 10, 1, 1, 8, 2)
        )
        cov.append(
            Coverage("B", 1, CONFIRMED, 3600, 7200, 10, 1, 1, 8, 2)
        )
        out = build_conclusions(FakeSnapshot(cov), records)
        pending = [c for c in out["components"] if c["status"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(
            sorted(pending[0]["record_ids"]), ["A1", "B1", "B2"]
        )
        # 待核组仍给出尝试性配对，但状态为 pending
        for r in out["results"]:
            if r["component_id"] == pending[0]["component_id"]:
                self.assertEqual(r["status"], "pending")
        formal = [c for c in out["components"] if c["status"] == "confirmed"]
        self.assertEqual(len(formal), 1)
        self.assertEqual(
            sorted(formal[0]["record_ids"]), ["A9", "B9"]
        )

    def test_each_record_used_at_most_once(self):
        records = [
            rec("A1", "A", 100.0),
            rec("B1", "B", 100.2),
            rec("B2", "B", 100.4),
            rec("B3", "B", 100.6),
        ]
        out = build_conclusions(FakeSnapshot(confirmed_both()), records)
        used = []
        for r in out["results"]:
            if r["pair_id"]:
                used.extend([r["record_a_id"], r["record_b_id"]])
        self.assertEqual(len(used), len(set(used)))


if __name__ == "__main__":
    unittest.main()
