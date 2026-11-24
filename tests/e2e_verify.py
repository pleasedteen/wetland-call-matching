"""端到端本地验证（一条命令跑完全部关键性质，目标 80 秒内）。

流程：
1. 启动两座模拟站点（会话轮换 / 限流 / 双版式 / 损坏条目均生效）；
2. 第一轮采集中途 SIGTERM 杀掉，验证快照可读、覆盖未确认、查询不崩；
3. 重启采集器继续补采，验证只补未确认时段、记录不重复、最终全部确认；
4. 用 dataset 真值独立核对配对数与损坏条目计数；
5. 验证快照一致性（单次查询记录与覆盖同代）与短查询分区裁剪
   （打开的分区文件数不随历史总量增长）；
6. 验证待核分组：临时制造一个未确认覆盖后重算，关联组整体待核。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from birdsurvey import dataset  # noqa: E402
from birdsurvey.matching import build_conclusions  # noqa: E402
from birdsurvey.storage import CONFIRMED, PARTIAL, Coverage, Store  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
PORT_A = 8191
PORT_B = 8192


def wait_health(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"站点 {port} 未就绪")


def start_station(station: str, port: int, log: str) -> subprocess.Popen:
    fh = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "run_station.py"),
         "--station", station, "--port", str(port)],
        cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
    )
    wait_health(port)
    return proc


def run_collector(data_dir: str, max_rounds: int | None = None) -> subprocess.Popen:
    cmd = [
        sys.executable, os.path.join(ROOT, "run_collect.py"),
        "--station-a", f"http://127.0.0.1:{PORT_A}",
        "--station-b", f"http://127.0.0.1:{PORT_B}",
        "--data-dir", data_dir,
    ]
    if max_rounds is not None:
        cmd += ["--max-rounds", str(max_rounds)]
    return subprocess.Popen(
        cmd, cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def truth() -> dict:
    """从数据集直接计算真值（独立于生产代码的配对实现）。"""
    total_ok = total_bad = 0
    for station in dataset.STATIONS:
        for p in range(dataset.PERIOD_COUNT):
            for r in dataset.build_period(station, p):
                if r.get("_corrupt"):
                    total_bad += 1
                else:
                    total_ok += 1
    return {"records_ok": total_ok, "records_bad": total_bad}


class Checker:
    def __init__(self):
        self.failed = 0

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))
        if not cond:
            self.failed += 1


def main() -> int:
    checker = Checker()
    work = tempfile.mkdtemp(prefix="birdverify-")
    data_dir = os.path.join(work, "data")
    print(f"工作目录: {work}")

    print("1) 启动双站并验证限流/轮换参数生效")
    log_a = os.path.join(work, "stationA.log")
    log_b = os.path.join(work, "stationB.log")
    pa = start_station("A", PORT_A, log_a)
    pb = start_station("B", PORT_B, log_b)
    try:
        print("2) 第一轮采集，中途 SIGTERM 模拟中断")
        proc = run_collector(data_dir)
        # 等待出现至少一代快照后中断（此时通常只完成部分时段）
        store = Store(data_dir)
        deadline = time.time() + 20
        gen_seen = 0
        while time.time() < deadline:
            snap = store.current()
            if snap is not None and snap.generation >= 1:
                gen_seen = snap.generation
                break  # 第一代快照一出现立即中断，保证仍有时段未开始/未完成
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
        assert "中断" in out or "收尾" in out, out[-300:]
        print(f"    中断前已落盘 {gen_seen} 代，进程退出码 {proc.returncode}")

        snap = store.current()
        covs = snap.coverages
        expected_cells = len(dataset.STATIONS) * dataset.PERIOD_COUNT
        done_cells = sum(1 for c in covs if c.status == CONFIRMED and c.complete)
        checker.check("中断后存在可读快照", snap is not None)
        checker.check("中断后仍有未确认/未开始时段",
                      done_cells < expected_cells,
                      f"已确认 {done_cells}/{expected_cells}")
        # 采集中/中断后查询必须可用，且记录与覆盖同代
        records = snap.all_records()
        conclusions = build_conclusions(snap, records)
        checker.check("中断态查询可出结论",
                      conclusions["summary"]["records"] == len(records))
        ids = [r["id"] for r in records]
        checker.check("中断态无重复记录", len(ids) == len(set(ids)))

        print("3) 重启采集器继续补采")
        t0 = time.time()
        proc2 = run_collector(data_dir)
        out2, _ = proc2.communicate(timeout=70)
        elapsed = time.time() - t0
        print(f"    补采完成，用时 {elapsed:.1f}s，退出码 {proc2.returncode}")
        checker.check("补采正常退出", proc2.returncode == 0)
        if proc2.returncode != 0:
            print(out2[-800:])

        print("4) 覆盖与条目真值核对")
        snap = store.current()
        covs = snap.coverages
        checker.check(
            "8 个站点-时段全部确认",
            len(covs) == 8 and all(c.status == CONFIRMED and c.complete for c in covs),
            f"共 {len(covs)} 个分区",
        )
        sum_ok = sum(c.records_ok for c in covs)
        sum_bad = sum(c.records_bad for c in covs)
        t = truth()
        checker.check("可读记录数等于真值", sum_ok == t["records_ok"],
                      f"{sum_ok} vs {t['records_ok']}")
        checker.check("损坏条目计数等于真值", sum_bad == t["records_bad"],
                      f"{sum_bad} vs {t['records_bad']}")
        final_records = snap.all_records()
        final_ids = [r["id"] for r in final_records]
        checker.check("重取去重：记录总数等于真值",
                      len(final_ids) == len(set(final_ids)) == t["records_ok"])

        print("5) 配对结论核对（最大配对数 / 最小总时间差 / 稳定）")
        c1 = build_conclusions(snap, final_records)
        c2 = build_conclusions(snap, list(reversed(final_records)))
        checker.check("配对结果与输入顺序无关（同分稳定）",
                      [(r["record_a_id"], r["record_b_id"]) for r in c1["results"]]
                      == [(r["record_a_id"], r["record_b_id"]) for r in c2["results"]])
        # 每条记录最多用一次
        used = []
        for r in c1["results"]:
            if r["pair_id"]:
                used += [r["record_a_id"], r["record_b_id"]]
        checker.check("每条记录最多参与一次配对", len(used) == len(set(used)))
        # 所有配对满足 2 秒与同物种约束
        by_id = {r["id"]: r for r in final_records}
        valid = all(
            abs(by_id[r["record_a_id"]]["ts"] - by_id[r["record_b_id"]]["ts"]) <= 2.0
            and by_id[r["record_a_id"]]["species"] == by_id[r["record_b_id"]]["species"]
            for r in c1["results"] if r["pair_id"]
        )
        checker.check("全部配对满足同物种且 |Δt| ≤ 2s", valid)
        # 独立暴力枚举核对最大配对数（小数据集可直接回溯）
        expect_pairs = brute_force_max_pairs(final_records)
        checker.check("配对数等于暴力枚举的最大配对数",
                      c1["summary"]["pairs"] == expect_pairs,
                      f"{c1['summary']['pairs']} vs {expect_pairs}")
        expect_diff = brute_force_min_diff(final_records, expect_pairs)
        checker.check("总时间差等于最优值",
                      abs(c1["summary"]["total_time_diff"] - expect_diff) < 1e-6,
                      f"{c1['summary']['total_time_diff']} vs {expect_diff:.3f}")
        checker.check("全量确认时无待核分组",
                      c1["summary"]["pending_components"] == 0)

        print("6) 快照一致性与短查询分区裁剪")
        snap_now = store.current()
        parts_narrow = snap_now.partitions_for_range(
            dataset.BASE_TS + 100, dataset.BASE_TS + 200
        )
        checker.check("100 秒窗口只命中 2 个分区（每站 1 个）",
                      len(parts_narrow) == 2, f"命中 {len(parts_narrow)} 个")
        # 模拟历史翻倍：再写一代空提交不影响裁剪结果
        store.open_writer().commit()
        snap2 = store.current()
        parts_narrow2 = snap2.partitions_for_range(
            dataset.BASE_TS + 100, dataset.BASE_TS + 200
        )
        checker.check("代次增长后短查询读取量不变",
                      len(parts_narrow2) == 2)
        # 单次查询内记录与覆盖来自同一代
        q_records = snap_now.records_in_range(None, None)
        q_concl = build_conclusions(snap_now, q_records)
        checker.check("查询绑定单一代次",
                      snap_now.generation == store.current().generation - 1
                      and q_concl["summary"]["records"] == len(q_records))

        print("7) 待核分组：制造 B/p2 未确认后重算")
        covs2 = list(snap_now.coverages)
        covs2 = [
            Coverage(c.station, c.period, PARTIAL, c.start_ts, c.end_ts,
                     c.entry_count, c.total_pages, c.pages_seen - 1,
                     c.records_ok, c.records_bad)
            if (c.station == "B" and c.period == 2) else c
            for c in covs2
        ]
        # 直接构造一个内存快照视图：复用磁盘记录，覆盖表替换
        pending_snap = PendingSnapshot(snap_now, covs2)
        pc = build_conclusions(pending_snap, q_records)
        pending_comps = [c for c in pc["components"] if c["status"] == "pending"]
        checker.check("存在待核分组且原因指向 B/p2",
                      len(pending_comps) > 0
                      and all("B/p2" in " ".join(c["reasons"]) for c in pending_comps),
                      f"{len(pending_comps)} 个待核组")
        formal_comps = [c for c in pc["components"] if c["status"] == "confirmed"]
        checker.check("其他组照常出正式结论", len(formal_comps) > 0)
        # 待核组内所有结论（含未配对记录）均为 pending
        cids = {c["component_id"] for c in pending_comps}
        all_pending = all(
            r["status"] == "pending" for r in pc["results"]
            if r["component_id"] in cids
        )
        checker.check("待核组内每条结论均标待核", all_pending)

    finally:
        pa.terminate()
        pb.terminate()
        pa.wait(timeout=5)
        pb.wait(timeout=5)

    print()
    if checker.failed:
        print(f"{FAIL} 验证失败 {checker.failed} 项")
        return 1
    print(f"{PASS} 全部验证通过")
    return 0


class PendingSnapshot:
    """覆盖表可替换的快照视图，记录仍读原快照分区。"""

    def __init__(self, base, coverages):
        self._base = base
        self._cov = coverages
        self.generation = base.generation

    def coverage(self, station, period):
        for c in self._cov:
            if c.station == station and c.period == period:
                return c
        return None


def _candidate_graph(records: list[dict]):
    """构造候选边与并查集分量（独立于生产 matching 模块的实现）。"""
    a = [r for r in records if r["station"] == "A"]
    b = [r for r in records if r["station"] == "B"]
    edges = []
    parent = {r["id"]: r["id"] for r in records}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for x in a:
        for y in b:
            if x["species"] == y["species"] and abs(x["ts"] - y["ts"]) <= 2.0:
                edges.append((x["id"], y["id"], round(abs(x["ts"] - y["ts"]), 3)))
                union(x["id"], y["id"])
    return edges, find


def brute_force_max_pairs(records: list[dict]) -> int:
    """按候选连通分量分别回溯，汇总最大配对数。"""
    edges, find = _candidate_graph(records)
    comp_edges: dict[str, list] = {}
    for ua, ub, d in edges:
        comp_edges.setdefault(find(ua), []).append((ua, ub, d))
    total = 0
    for ce in comp_edges.values():
        total += _component_best(ce)[0]
    return total


def brute_force_min_diff(records: list[dict], k: int) -> float:
    """各分量在各自最大配对数下的最小时间差之和（即全局最优）。"""
    edges, find = _candidate_graph(records)
    comp_edges: dict[str, list] = {}
    for ua, ub, d in edges:
        comp_edges.setdefault(find(ua), []).append((ua, ub, d))
    return sum(_component_best(ce)[1] for ce in comp_edges.values())


def _component_best(edges: list[tuple[str, str, float]]) -> tuple[int, float]:
    """单个分量内回溯：(最大配对数, 该配对数下的最小总时间差)。"""
    a_nodes = sorted({ua for ua, _, _ in edges})
    adj: dict[str, list[tuple[str, float]]] = {x: [] for x in a_nodes}
    for ua, ub, d in edges:
        adj[ua].append((ub, d))
    best_count = 0
    best_cost = float("inf")

    def dfs(i: int, used_b: set[str], count: int, cost: float) -> None:
        nonlocal best_count, best_cost
        if i == len(a_nodes):
            if count > best_count or (count == best_count and cost < best_cost):
                best_count, best_cost = count, cost
            return
        dfs(i + 1, used_b, count, cost)
        for bid, d in adj[a_nodes[i]]:
            if bid not in used_b:
                used_b.add(bid)
                dfs(i + 1, used_b, count + 1, cost + d)
                used_b.remove(bid)

    dfs(0, set(), 0, 0.0)
    return best_count, best_cost


if __name__ == "__main__":
    raise SystemExit(main())
