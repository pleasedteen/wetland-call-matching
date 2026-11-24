"""查询入口：单次查询绑定同一次完整落盘快照。

子命令：
  pairs     按时间窗输出配对/未配对结论（正式结论与待核分组）
  coverage  按站点-时段输出覆盖确认状态
  summary   只输出汇总数字

时间参数支持 Unix 秒或 ISO-8601（如 2026-11-24T09:00:00）。
查询只打开与时间窗相交的分区文件，短时间窗的读取量不随历史总量增长。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from datetime import timezone

from .export import ParquetUnavailable, export_snapshot
from .matching import PENDING, build_conclusions
from .storage import CONFIRMED, Store


def parse_time(text: str | None) -> float | None:
    if text is None:
        return None
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        pass
    iso = text.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def fmt_ts(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


def _load_snapshot(data_dir: str):
    snapshot = Store(data_dir).current()
    if snapshot is None:
        raise SystemExit(f"数据目录 {data_dir} 还没有任何落盘快照，请先运行采集")
    return snapshot


def _base_filters(args) -> tuple[float | None, float | None, str | None]:
    return parse_time(args.start), parse_time(args.end), args.station


def cmd_pairs(args) -> int:
    snapshot = _load_snapshot(args.data_dir)
    start, end, station = _base_filters(args)
    records = snapshot.records_in_range(start, end, station)
    conclusions = build_conclusions(snapshot, records)
    if args.format == "json":
        print(json.dumps(
            {"generation": snapshot.generation, **conclusions},
            ensure_ascii=False, indent=2,
        ))
        return 0

    s = conclusions["summary"]
    print(f"快照代次: gen-{snapshot.generation:06d}（本次查询的记录与覆盖同属该次落盘）")
    print(
        f"记录 {s['records']} 条 (A={s['station_a_records']}, B={s['station_b_records']}) | "
        f"配对 {s['pairs']} 对 (待核 {s['pending_pairs']}) | "
        f"未配对 {s['unpaired']} | 总时间差 {s['total_time_diff']}s | "
        f"分组 {s['components']} (待核 {s['pending_components']})"
    )
    for c in conclusions["components"]:
        tag = "待核" if c["status"] == PENDING else "正式"
        print(f"  [{tag}] {c['component_id']}: {', '.join(c['record_ids'])}")
        for reason in c["reasons"]:
            print(f"      待核原因: {reason}")
    print("-" * 72)
    for r in conclusions["results"]:
        if r["pair_id"] is None:
            print(
                f"[{'待核' if r['status'] == PENDING else '正式'}] {r['component_id']} "
                f"未配对 {r['station_a']}:{r['record_a_id']} "
                f"{fmt_ts(r['record_a_ts'])} {r['record_a_species']}"
            )
        else:
            print(
                f"[{'待核' if r['status'] == PENDING else '正式'}] {r['component_id']} "
                f"{r['pair_id']} {r['record_a_id']} <-> {r['record_b_id']} "
                f"{r['record_a_species']} Δ={r['time_diff']}s "
                f"({fmt_ts(r['record_a_ts'])} / {fmt_ts(r['record_b_ts'])})"
            )
    return 0


def cmd_coverage(args) -> int:
    snapshot = _load_snapshot(args.data_dir)
    rows = snapshot.coverages
    if args.station:
        rows = [c for c in rows if c.station == args.station]
    if args.format == "json":
        print(json.dumps(
            {"generation": snapshot.generation,
             "coverages": [c.to_dict() for c in rows]},
            ensure_ascii=False, indent=2,
        ))
        return 0
    print(f"快照代次: gen-{snapshot.generation:06d}")
    print(f"{'站':<3}{'时段':<5}{'状态':<9}{'页':<10}{'条目(好+坏/总数)':<22}窗口")
    for c in rows:
        status = "已确认" if c.status == CONFIRMED else "未确认"
        print(
            f"{c.station:<4}{c.period:<6}{status:<10}"
            f"{c.pages_seen}/{c.total_pages:<8}"
            f"{c.records_ok}+{c.records_bad}/{c.entry_count:<16}"
            f"{fmt_ts(c.start_ts)} ~ {fmt_ts(c.end_ts)}"
        )
    return 0


def cmd_summary(args) -> int:
    snapshot = _load_snapshot(args.data_dir)
    start, end, station = _base_filters(args)
    records = snapshot.records_in_range(start, end, station)
    conclusions = build_conclusions(snapshot, records)
    if args.format == "json":
        print(json.dumps(
            {"generation": snapshot.generation, "summary": conclusions["summary"]},
            ensure_ascii=False, indent=2,
        ))
        return 0
    s = conclusions["summary"]
    print(f"快照代次: gen-{snapshot.generation:06d}")
    for key, value in s.items():
        print(f"  {key}: {value}")
    return 0


def cmd_export(args) -> int:
    snapshot = _load_snapshot(args.data_dir)
    try:
        written = export_snapshot(snapshot, args.out_dir, args.format)
    except ParquetUnavailable as exc:
        print(f"导出失败: {exc}", file=sys.stderr)
        return 2
    print(f"已导出快照 gen-{snapshot.generation:06d}（{args.format}）到 {args.out_dir}")
    for path in written:
        print(f"  {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="鸟鸣记录查询工具")
    ap.add_argument("--data-dir", default="./birddata")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--start", help="起始时间（含），Unix 秒或 ISO-8601")
        p.add_argument("--end", help="结束时间（不含），Unix 秒或 ISO-8601")
        p.add_argument("--station", choices=["A", "B"], help="只看单站")
        p.add_argument("--format", choices=["text", "json"], default="text")

    p_pairs = sub.add_parser("pairs", help="查看配对结论与待核分组")
    add_common(p_pairs)
    p_pairs.set_defaults(func=cmd_pairs)

    p_cov = sub.add_parser("coverage", help="查看各时段覆盖确认状态")
    p_cov.add_argument("--station", choices=["A", "B"])
    p_cov.add_argument("--format", choices=["text", "json"], default="text")
    p_cov.set_defaults(func=cmd_coverage)

    p_sum = sub.add_parser("summary", help="查看汇总指标")
    add_common(p_sum)
    p_sum.set_defaults(func=cmd_summary)

    p_exp = sub.add_parser("export", help="导出当前快照到 JSONL 或 Parquet")
    p_exp.add_argument("--out-dir", required=True, help="导出目录")
    p_exp.add_argument("--format", choices=["jsonl", "parquet"], default="jsonl")
    p_exp.set_defaults(func=cmd_export)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"查询失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
