"""导出后端测试：JSONL 始终可用；Parquet 缺依赖时优雅报错。"""
import json
import os
import tempfile
import unittest

from birdsurvey.export import (
    ParquetUnavailable,
    export_jsonl,
    export_parquet,
    export_snapshot,
)
from birdsurvey.storage import CONFIRMED, Coverage, Store


def make_rec(rid, station, period, ts):
    return {"id": rid, "station": station, "period": period, "ts": ts, "species": "HERON"}


class TestExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store = Store(self.tmp)
        w = store.open_writer()
        w.upsert_record(make_rec("A-0", "A", 0, 100.0))
        w.upsert_record(make_rec("B-0", "B", 0, 101.0))
        w.upsert_coverage(Coverage("A", 0, CONFIRMED, 0, 3600, 1, 1, 1, 1, 0))
        w.upsert_coverage(Coverage("B", 0, CONFIRMED, 0, 3600, 1, 1, 1, 1, 0))
        w.commit()
        self.snapshot = store.current()

    def test_jsonl_export_contains_records_and_coverage(self):
        out = os.path.join(self.tmp, "exported")
        written = export_jsonl(self.snapshot, out)
        names = sorted(os.path.basename(p) for p in written)
        self.assertIn("coverage.jsonl", names)
        self.assertIn("A-p0.jsonl", names)
        self.assertIn("B-p0.jsonl", names)
        with open(os.path.join(out, "coverage.jsonl"), encoding="utf-8") as fh:
            cov = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual({c["station"] for c in cov}, {"A", "B"})

    def test_export_dispatch(self):
        out = os.path.join(self.tmp, "dispatch")
        paths = export_snapshot(self.snapshot, out, "jsonl")
        self.assertTrue(paths)
        with self.assertRaises(ValueError):
            export_snapshot(self.snapshot, out, "xml")

    def test_parquet_graceful_without_pyarrow(self):
        try:
            import pyarrow  # noqa: F401
            self.skipTest("环境已安装 pyarrow，跳过缺依赖分支测试")
        except ImportError:
            pass
        with self.assertRaises(ParquetUnavailable):
            export_parquet(self.snapshot, os.path.join(self.tmp, "pq"))


if __name__ == "__main__":
    unittest.main()
