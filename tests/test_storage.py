"""存储测试：去重、原子快照、短查询分区裁剪。"""
import json
import os
import tempfile
import unittest

from birdsurvey.storage import CONFIRMED, Coverage, Store


def make_rec(rid, station, period, ts):
    return {"id": rid, "station": station, "period": period, "ts": ts, "species": "HERON"}


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(self.tmp)

    def test_writer_starts_empty_and_commits_snapshot(self):
        self.assertIsNone(self.store.current())
        w = self.store.open_writer()
        w.upsert_record(make_rec("A-0", "A", 0, 100.0))
        w.upsert_coverage(Coverage("A", 0, CONFIRMED, 0, 3600, 1, 1, 1, 1, 0))
        w.commit()
        snap = self.store.current()
        self.assertIsNotNone(snap)
        self.assertEqual(len(snap.all_records()), 1)

    def test_dedup_across_refetch(self):
        w = self.store.open_writer()
        self.assertTrue(w.upsert_record(make_rec("A-0", "A", 0, 100.0)))
        self.assertFalse(w.upsert_record(make_rec("A-0", "A", 0, 100.0)))
        w.commit()
        # 模拟中断后重开写句柄，再次写入同 id
        w2 = self.store.open_writer()
        self.assertFalse(w2.upsert_record(make_rec("A-0", "A", 0, 100.0)))
        w2.commit()
        snap = self.store.current()
        self.assertEqual(len(snap.all_records()), 1)

    def test_snapshot_isolation_during_write(self):
        w = self.store.open_writer()
        w.upsert_record(make_rec("A-0", "A", 0, 100.0))
        w.upsert_coverage(Coverage("A", 0, CONFIRMED, 0, 3600, 1, 1, 1, 1, 0))
        w.commit()
        snap = self.store.current()  # 查询绑定 gen-1
        w2 = self.store.open_writer()
        w2.upsert_record(make_rec("A-1", "A", 0, 200.0))
        w2.commit()
        # 旧快照视图不受新一代影响
        self.assertEqual(len(snap.all_records()), 1)
        self.assertEqual(self.store.current().generation, 2)
        self.assertEqual(len(self.store.current().all_records()), 2)

    def test_short_range_query_prunes_partitions(self):
        # 造 8 个时段分区，只查询中间一个窗口
        w = self.store.open_writer()
        for p in range(8):
            w.upsert_record(make_rec(f"A-p{p}", "A", p, p * 3600 + 100))
            w.upsert_coverage(
                Coverage("A", p, CONFIRMED, p * 3600, (p + 1) * 3600,
                         1, 1, 1, 1, 0)
            )
        w.commit()
        snap = self.store.current()
        parts = snap.partitions_for_range(3 * 3600 + 50, 3 * 3600 + 200)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["period"], 3)
        rows = snap.records_in_range(3 * 3600 + 50, 3 * 3600 + 200)
        self.assertEqual([r["id"] for r in rows], ["A-p3"])

    def test_read_volume_independent_of_history(self):
        """读取量指标：短窗口打开的分区文件数不随历史总量变化。"""
        w = self.store.open_writer()
        for p in range(12):
            for i in range(50):
                w.upsert_record(make_rec(f"A-p{p}-{i}", "A", p, p * 3600 + i))
            w.upsert_coverage(
                Coverage("A", p, CONFIRMED, p * 3600, (p + 1) * 3600,
                         50, 3, 3, 50, 0)
            )
        w.commit()
        snap_small = self.store.current()
        opened_small = len(
            snap_small.partitions_for_range(3600 + 10, 3600 + 20)
        )

        # 再把历史拉长到 40 个时段
        w2 = self.store.open_writer()
        for p in range(12, 40):
            for i in range(50):
                w2.upsert_record(make_rec(f"A-p{p}-{i}", "A", p, p * 3600 + i))
            w2.upsert_coverage(
                Coverage("A", p, CONFIRMED, p * 3600, (p + 1) * 3600,
                         50, 3, 3, 50, 0)
            )
        w2.commit()
        snap_big = self.store.current()
        opened_big = len(
            snap_big.partitions_for_range(3600 + 10, 3600 + 20)
        )
        self.assertEqual(opened_small, opened_big)
        self.assertEqual(opened_big, 1)

    def test_coverage_complete_rule(self):
        ok = Coverage("A", 0, CONFIRMED, 0, 3600, 10, 2, 2, 8, 2)
        self.assertTrue(ok.complete)
        missing_page = Coverage("A", 0, CONFIRMED, 0, 3600, 10, 2, 1, 8, 2)
        self.assertFalse(missing_page.complete)
        missing_entry = Coverage("A", 0, CONFIRMED, 0, 3600, 10, 2, 2, 7, 2)
        self.assertFalse(missing_entry.complete)
        partial = Coverage("A", 0, "partial", 0, 3600, 10, 2, 2, 8, 2)
        self.assertFalse(partial.complete)

    def test_jsonl_partition_format(self):
        w = self.store.open_writer()
        w.upsert_record(make_rec("A-0", "A", 0, 100.0))
        w.upsert_coverage(Coverage("A", 0, CONFIRMED, 0, 3600, 1, 1, 1, 1, 0))
        w.commit()
        path = os.path.join(self.store.current().gen_dir, "records", "A-p0.jsonl")
        with open(path, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(lines[0]["id"], "A-0")


if __name__ == "__main__":
    unittest.main()
