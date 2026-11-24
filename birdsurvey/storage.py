"""本地持久化：原子快照式 JSON Lines 存储。

目录结构：

    <root>/
        CURRENT                 # 指针文件，内容为当前代目录名
        gen-000001/
            manifest.json       # 覆盖状态 + 分区索引
            records/A-p0.jsonl  # 按 站点-时段 分区，一行一条记录
        gen-000002/ ...

一致性保证：
- 一次采集进度落盘 = 写出完整新一代目录后原子切换 CURRENT；
- 查询只打开一次 CURRENT，期间看到的记录与覆盖信息永远来自同一次完整落盘，
  即使采集中正在写下一代也不会读到半成品。

短查询保证：
- 记录按 站点-时段 分区，manifest 里带每个分区的时间窗；
- 时间范围查询只打开窗口相交的分区，读取量与无关历史总量无关。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass

PARTIAL = "partial"
CONFIRMED = "confirmed"


@dataclass
class Coverage:
    station: str
    period: int
    status: str = PARTIAL
    start_ts: float = 0.0
    end_ts: float = 0.0
    entry_count: int = 0          # 站点声明的条目总数（含损坏）
    total_pages: int = 0
    pages_seen: int = 0
    records_ok: int = 0           # 已读到的可读条目（去重后）
    records_bad: int = 0          # 已跳过的损坏条目（按页去重）
    updated_at: float = 0.0
    pages_loaded: str = ""        # 已完整解析的页号集合，逗号分隔（持久化用）

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "Coverage":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def loaded_page_set(self) -> set[int]:
        return {int(x) for x in self.pages_loaded.split(",") if x != ""}

    def mark_page_loaded(self, page: int) -> None:
        pages = self.loaded_page_set
        pages.add(page)
        self.pages_loaded = ",".join(str(x) for x in sorted(pages))
        self.pages_seen = len(pages)

    @property
    def complete(self) -> bool:
        """分页与条目都读全才算确认覆盖。"""
        return (
            self.status == CONFIRMED
            and self.total_pages > 0
            and self.pages_seen >= self.total_pages
            and self.entry_count > 0
            and self.records_ok + self.records_bad >= self.entry_count
        )


class Snapshot:
    """某一代快照的只读视图（查询期间始终绑定同一代目录）。"""

    def __init__(self, root: str, gen_dir: str, manifest: dict):
        self.root = root
        self.gen_dir = gen_dir
        self.manifest = manifest
        self.generation = manifest["generation"]

    @property
    def coverages(self) -> list[Coverage]:
        return [Coverage.from_dict(c) for c in self.manifest["coverages"]]

    def coverage(self, station: str, period: int) -> Coverage | None:
        for c in self.coverages:
            if c.station == station and c.period == period:
                return c
        return None

    def is_confirmed(self, station: str, period: int) -> bool:
        c = self.coverage(station, period)
        return bool(c and c.status == CONFIRMED and c.complete)

    def partitions_for_range(
        self,
        start: float | None,
        end: float | None,
        station: str | None = None,
    ) -> list[dict]:
        """按时间窗与站点裁剪分区，避免打开无关历史分区。"""
        out = []
        for part in self.manifest["partitions"].values():
            if station is not None and part["station"] != station:
                continue
            if start is not None and part["end_ts"] <= start:
                continue
            if end is not None and part["start_ts"] >= end:
                continue
            out.append(part)
        return out

    def read_partition(self, part: dict) -> list[dict]:
        path = os.path.join(self.gen_dir, part["path"])
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def records_in_range(
        self,
        start: float | None,
        end: float | None,
        station: str | None = None,
    ) -> list[dict]:
        rows: list[dict] = []
        for part in self.partitions_for_range(start, end, station):
            for rec in self.read_partition(part):
                if start is not None and rec["ts"] < start:
                    continue
                if end is not None and rec["ts"] >= end:
                    continue
                rows.append(rec)
        return rows

    def all_records(self) -> list[dict]:
        rows: list[dict] = []
        for part in self.manifest["partitions"].values():
            rows.extend(self.read_partition(part))
        return rows


def _partition_key(station: str, period: int) -> str:
    return f"{station}-{period}"


class StoreWriter:
    """采集期写句柄：内存累积，commit 时原子发布新一代快照。"""

    def __init__(self, store: "Store", base: Snapshot | None):
        self._store = store
        self._base = base
        self._records: dict[str, dict] = {}
        self._part_meta: dict[str, dict] = {}
        self._coverages: dict[tuple[str, int], Coverage] = {}
        self._touched_partitions: set[str] = set()
        self._closed = False
        if base is not None:
            for c in base.coverages:
                self._coverages[(c.station, c.period)] = c
            for key, part in base.manifest["partitions"].items():
                for rec in base.read_partition(part):
                    self._records[rec["id"]] = rec
                self._part_meta[key] = dict(part)

    # -- 记录（按 id 去重，重取只计一次） ---------------------------------
    def upsert_record(self, rec: dict) -> bool:
        rid = rec["id"]
        if rid in self._records:
            return False
        self._records[rid] = rec
        self._touched_partitions.add(_partition_key(rec["station"], rec["period"]))
        return True

    # -- 覆盖状态 ---------------------------------------------------------
    def coverage(self, station: str, period: int) -> Coverage | None:
        return self._coverages.get((station, period))

    def upsert_coverage(self, cov: Coverage) -> None:
        self._coverages[(cov.station, cov.period)] = cov

    def commit(self) -> int:
        gen = (self._base.generation + 1) if self._base else 1
        # 以最新覆盖信息补全分区元数据
        grouped: dict[str, list[dict]] = {}
        for rec in self._records.values():
            grouped.setdefault(_partition_key(rec["station"], rec["period"]), []).append(rec)

        for (station, period), cov in self._coverages.items():
            key = _partition_key(station, period)
            self._part_meta.setdefault(
                key,
                {
                    "station": station,
                    "period": period,
                    "start_ts": cov.start_ts,
                    "end_ts": cov.end_ts,
                    "path": f"records/{station}-p{period}.jsonl",
                    "count": 0,
                },
            )
            meta = self._part_meta[key]
            meta["start_ts"] = cov.start_ts
            meta["end_ts"] = cov.end_ts

        # 只有记录、尚无覆盖信息的分区也要落盘（窗口退化为记录时间范围）
        for key, rows in grouped.items():
            if key in self._part_meta:
                continue
            station = rows[0]["station"]
            period = rows[0]["period"]
            ts_values = [r["ts"] for r in rows]
            self._part_meta[key] = {
                "station": station,
                "period": period,
                "start_ts": min(ts_values),
                "end_ts": max(ts_values) + 1,
                "path": f"records/{station}-p{period}.jsonl",
                "count": 0,
            }

        gen = (self._base.generation + 1) if self._base else 1
        tmp_dir = os.path.join(
            self._store.root, f".gen-tmp-{os.getpid()}-{time.time_ns()}"
        )
        os.makedirs(os.path.join(tmp_dir, "records"), exist_ok=True)
        try:
            partitions = {}
            for key in sorted(self._part_meta):
                meta = self._part_meta[key]
                rows = sorted(grouped.get(key, []), key=lambda r: (r["ts"], r["id"]))
                target = os.path.join(tmp_dir, meta["path"])
                old_path = (
                    os.path.join(self._base.gen_dir, meta["path"]) if self._base else ""
                )
                if key not in self._touched_partitions and old_path and os.path.exists(old_path):
                    # 未变化分区直接硬链接（失败则复制），提交成本与历史总量解耦
                    try:
                        os.link(old_path, target)
                    except OSError:
                        shutil.copyfile(old_path, target)
                else:
                    with open(target, "w", encoding="utf-8") as fh:
                        for rec in rows:
                            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                partitions[key] = {**meta, "count": len(rows)}

            manifest = {
                "generation": gen,
                "created_at": time.time(),
                "coverages": [
                    self._coverages[k].to_dict()
                    for k in sorted(self._coverages, key=lambda x: (x[0], x[1]))
                ],
                "partitions": partitions,
            }
            tmp_manifest = os.path.join(tmp_dir, "manifest.json")
            with open(tmp_manifest, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, ensure_ascii=False, indent=2)

            final_dir = os.path.join(self._store.root, f"gen-{gen:06d}")
            os.replace(tmp_dir, final_dir)
            pointer = os.path.join(self._store.root, "CURRENT")
            tmp_pointer = pointer + ".tmp"
            with open(tmp_pointer, "w", encoding="utf-8") as fh:
                fh.write(os.path.basename(final_dir))
            os.replace(tmp_pointer, pointer)
        finally:
            if os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        # 重新绑定到新一代快照，句柄可继续累积并再次提交
        self._base = self._store.current()
        self._touched_partitions.clear()
        return gen


class Store:
    """快照存储根目录管理。"""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def current(self) -> Snapshot | None:
        pointer = os.path.join(self.root, "CURRENT")
        if not os.path.exists(pointer):
            return None
        with open(pointer, "r", encoding="utf-8") as fh:
            gen_name = fh.read().strip()
        gen_dir = os.path.join(self.root, gen_name)
        with open(os.path.join(gen_dir, "manifest.json"), "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        return Snapshot(self.root, gen_dir, manifest)

    def open_writer(self) -> StoreWriter:
        return StoreWriter(self, self.current())

    def prune_generations(self, keep: int = 3) -> list[str]:
        """回收旧代目录，保留最近 keep 代（含当前代）。"""
        gens = sorted(
            d for d in os.listdir(self.root) if d.startswith("gen-")
        )
        removed = []
        snapshot = self.current()
        current_name = os.path.basename(snapshot.gen_dir) if snapshot else None
        for name in gens[:-keep] if keep > 0 else gens:
            if name == current_name:
                continue
            shutil.rmtree(os.path.join(self.root, name), ignore_errors=True)
            removed.append(name)
        return removed


def atomic_export_jsonl(snapshot: Snapshot, out_dir: str) -> None:
    """把当前快照的分区文件导出到独立目录（便于外部系统读取）。"""
    os.makedirs(out_dir, exist_ok=True)
    for part in snapshot.manifest["partitions"].values():
        src = os.path.join(snapshot.gen_dir, part["path"])
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(out_dir, os.path.basename(src)))
