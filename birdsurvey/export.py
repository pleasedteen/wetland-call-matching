"""快照导出：JSONL（零依赖，默认）与 Parquet（需 pyarrow，可选）。

记录与覆盖信息都可导出。Parquet 为可选后端：
    pip install pyarrow
未安装时 export_parquet 抛出带安装指引的 ParquetUnavailable。
"""
from __future__ import annotations

import json
import os

from .storage import Snapshot, atomic_export_jsonl


class ParquetUnavailable(RuntimeError):
    """环境缺少 pyarrow，无法写 Parquet。"""


def export_jsonl(snapshot: Snapshot, out_dir: str) -> list[str]:
    """导出分区记录 JSONL + coverage.json + conclusions 不需要的原始清单。"""
    os.makedirs(out_dir, exist_ok=True)
    atomic_export_jsonl(snapshot, out_dir)
    cov_path = os.path.join(out_dir, "coverage.jsonl")
    with open(cov_path, "w", encoding="utf-8") as fh:
        for cov in snapshot.coverages:
            fh.write(json.dumps(cov.to_dict(), ensure_ascii=False) + "\n")
    written = sorted(
        os.path.join(out_dir, f)
        for f in os.listdir(out_dir)
        if f.endswith(".jsonl")
    )
    return written


def _load_pyarrow():
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise ParquetUnavailable(
            "Parquet 后端需要可选依赖，请先安装: pip install pyarrow"
        ) from exc
    return pa, pq


def export_parquet(snapshot: Snapshot, out_dir: str) -> list[str]:
    """把当前快照导出为 Parquet：每分区一个记录文件 + 覆盖状态文件。"""
    pa, pq = _load_pyarrow()
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    for key, part in snapshot.manifest["partitions"].items():
        rows = snapshot.read_partition(part)
        if not rows:
            continue
        table = pa.Table.from_pylist(rows)
        path = os.path.join(out_dir, f"{key}.parquet")
        pq.write_table(table, path, compression="zstd")
        written.append(path)

    cov_rows = [c.to_dict() for c in snapshot.coverages]
    if cov_rows:
        pq.write_table(pa.Table.from_pylist(cov_rows),
                       os.path.join(out_dir, "coverage.parquet"),
                       compression="zstd")
        written.append(os.path.join(out_dir, "coverage.parquet"))
    return sorted(written)


def export_snapshot(snapshot: Snapshot, out_dir: str, fmt: str) -> list[str]:
    if fmt == "jsonl":
        return export_jsonl(snapshot, out_dir)
    if fmt == "parquet":
        return export_parquet(snapshot, out_dir)
    raise ValueError(f"不支持的导出格式: {fmt}（可选 jsonl / parquet）")
