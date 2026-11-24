"""采集编排：双站分页采集、覆盖确认、中断续采。

- 每个封存时段只有“分页读全 + 条目读全（含损坏计数）”才标记 confirmed；
- 任意时刻 Ctrl-C / kill 都会把当前进度原子落盘，重启后跳过已确认时段、
  从未确认时段继续；同一记录重取按 id 去重，只计一次；
- 会话轮换由 StationClient 内部处理；限流按站点给出的 retry_after 等待；
- 每隔若干页提交一次快照，既控制落盘开销，也把中断损失限制在少数页内。
"""
from __future__ import annotations

import argparse
import sys
import time

from .client import StationClient
from .storage import CONFIRMED, PARTIAL, Coverage, Store

FLUSH_EVERY_PAGES = 3


def _log(msg: str) -> None:
    print(f"[collect {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def collect(stations: dict[str, str], data_dir: str, max_rounds: int | None = None) -> int:
    store = Store(data_dir)
    clients = {name: StationClient(url) for name, url in stations.items()}
    writer = store.open_writer()

    interrupted = {"flag": False}

    def _on_signal(signum, frame):  # noqa: ANN001
        interrupted["flag"] = True

    import signal

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    def flush(tag: str) -> None:
        gen = writer.commit()
        _log(f"已落盘快照 gen-{gen:06d}（{tag}）")

    try:
        rounds = 0
        while not interrupted["flag"]:
            rounds += 1
            if max_rounds is not None and rounds > max_rounds:
                break
            pending_work = False

            for name, client in clients.items():
                periods = client.list_periods()
                for meta in periods:
                    p = meta["period"]
                    cov = writer.coverage(name, p)
                    if cov is None:
                        cov = Coverage(
                            station=name,
                            period=p,
                            start_ts=float(meta["start_ts"]),
                            end_ts=float(meta["end_ts"]),
                            entry_count=int(meta["entry_count"]),
                        )
                        writer.upsert_coverage(cov)
                    else:
                        # 以站点最新声明刷新元数据
                        cov.entry_count = int(meta["entry_count"])
                        cov.start_ts = float(meta["start_ts"])
                        cov.end_ts = float(meta["end_ts"])

                    if cov.status == CONFIRMED and cov.complete:
                        continue
                    pending_work = True
                    _log(
                        f"站 {name} 时段 p{p} 开始/续采 "
                        f"(pages={cov.pages_seen}/{cov.total_pages or '?'}, "
                        f"ok={cov.records_ok}, bad={cov.records_bad})"
                    )

                    pages_in_this_streak = 0
                    for event in client.fetch_period(p, meta["first_cursor"]):
                        cov.total_pages = event["total_pages"]
                        page_no = event["page"]
                        is_new_page = page_no not in cov.loaded_page_set
                        if is_new_page:
                            # 页级去重：重取同一页不重复累计损坏数与页数
                            cov.mark_page_loaded(page_no)
                            cov.records_bad += event["bad"]
                        if event["restarted"]:
                            _log(f"站 {name} p{p} 会话轮换，按新会话从首页恢复")
                        for rec in event["records"]:
                            rec["station"] = name
                            rec["period"] = p
                            if writer.upsert_record(rec):
                                cov.records_ok += 1
                        cov.updated_at = time.time()
                        pages_in_this_streak += 1

                        # 覆盖确认条件：分页读全 且 可读+损坏条目数读全
                        if (
                            cov.pages_seen >= cov.total_pages
                            and cov.records_ok + cov.records_bad >= cov.entry_count
                        ):
                            cov.status = CONFIRMED
                            flush(f"{name}/p{p} 覆盖确认")
                        elif pages_in_this_streak % FLUSH_EVERY_PAGES == 0:
                            flush(f"{name}/p{p} 分页进度")

                        if interrupted["flag"]:
                            break
                    writer.upsert_coverage(cov)
                    if interrupted["flag"]:
                        break
                if interrupted["flag"]:
                    break

            if not pending_work:
                _log("全部时段覆盖确认，采集结束")
                return 0
    finally:
        flush("收尾/中断保存")

    _log("收到中断信号，进度已保存，重新运行本命令可继续补采")
    return 130


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="双站鸟鸣采集器")
    ap.add_argument("--station-a", default="http://127.0.0.1:8101")
    ap.add_argument("--station-b", default="http://127.0.0.1:8102")
    ap.add_argument("--data-dir", default="./birddata")
    ap.add_argument("--max-rounds", type=int, default=None,
                    help="最多扫描轮数（主要用于测试）")
    args = ap.parse_args(argv)
    try:
        return collect(
            {"A": args.station_a, "B": args.station_b},
            args.data_dir,
            args.max_rounds,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[collect] 致命错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
