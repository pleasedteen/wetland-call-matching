"""跨站鸟鸣配对与结论分组。

规则：
1. 仅同物种、两站时间差 <= 2 秒的记录构成候选边；
2. 每条记录最多用一次；先最大化配对数，再最小化总时间差；
3. 同分结果稳定：边按确定性顺序枚举，费用取整毫秒，
   增广算法在等费用路径上保持 FIFO 顺序；
4. 候选关系相连的记录同属一个组（并查集）；
   组内只要有一条记录，其对站在候选时段上仍存在未确认覆盖，
   整组标记“待核”，其余组照常出具正式结论。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from .dataset import MAX_DIFF_SECONDS
from .storage import Snapshot

PENDING = "pending"
CONFIRMED_RESULT = "confirmed"


@dataclass
class PairResult:
    pair_id: str | None
    station_a: str
    record_a: dict
    station_b: str | None = None
    record_b: dict | None = None
    time_diff: float | None = None
    component_id: str = ""
    status: str = CONFIRMED_RESULT

    def to_dict(self) -> dict:
        return {
            "component_id": self.component_id,
            "status": self.status,
            "pair_id": self.pair_id,
            "station_a": self.station_a,
            "record_a_id": self.record_a["id"],
            "record_a_ts": self.record_a["ts"],
            "record_a_species": self.record_a["species"],
            "station_b": self.station_b,
            "record_b_id": self.record_b["id"] if self.record_b else None,
            "record_b_ts": self.record_b["ts"] if self.record_b else None,
            "record_b_species": (
                self.record_b["species"] if self.record_b else None
            ),
            "time_diff": self.time_diff,
        }


@dataclass
class _Edge:
    to: int
    rev: int
    cap: int
    cost: int


class _MinCostFlow:
    """单位容量二分图的最小费用流（Dijkstra + 势函数）。"""

    def __init__(self, n: int):
        self.n = n
        self.g: list[list[_Edge]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cost: int) -> None:
        # 相同费用时先加入的边先被增广，保证同分结果稳定
        self.g[u].append(_Edge(v, len(self.g[v]), 1, cost))
        self.g[v].append(_Edge(u, len(self.g[u]) - 1, 0, -cost))

    def flow(self, s: int, t: int) -> tuple[int, int]:
        n = self.n
        potential = [0] * n
        total_flow = 0
        total_cost = 0
        inf = 10**18
        while True:
            dist = [inf] * n
            prev_v = [-1] * n
            prev_e = [-1] * n
            dist[s] = 0
            heap: list[tuple[int, int]] = [(0, s)]
            while heap:
                d, u = heapq.heappop(heap)
                if d != dist[u]:
                    continue
                for ei, e in enumerate(self.g[u]):
                    if e.cap <= 0:
                        continue
                    nd = d + e.cost + potential[u] - potential[e.to]
                    if nd < dist[e.to]:
                        dist[e.to] = nd
                        prev_v[e.to] = u
                        prev_e[e.to] = ei
                        heapq.heappush(heap, (nd, e.to))
            if dist[t] == inf:
                break
            for v in range(n):
                if dist[v] < inf:
                    potential[v] += dist[v]
            # 单位容量：每次增广 1
            v = t
            while v != s:
                u = prev_v[v]
                e = self.g[u][prev_e[v]]
                e.cap = 0
                self.g[v][e.rev].cap = 1
                v = u
            total_flow += 1
            total_cost += potential[t]
        return total_flow, total_cost

    def matched_edges(self, source: int, left_ids: list[int]) -> list[tuple[int, int, int]]:
        """从残量网络反推匹配，返回 (左节点, 右节点, 费用) 列表。"""
        out = []
        for u in left_ids:
            for e in self.g[u]:
                # 正向边容量已耗尽 => 已匹配
                if e.to != source and e.cap == 0 and e.cost >= 0:
                    out.append((u, e.to, e.cost))
        return out


class _DSU:
    def __init__(self, keys):
        self.parent = {k: k for k in keys}

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != x:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # 让较小 id 当根，组件命名更稳定
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb


def _candidate_edges(a_records: list[dict], b_records: list[dict]) -> list[dict]:
    """生成候选边并按确定性顺序排序。"""
    edges = []
    b_by_species: dict[str, list[dict]] = {}
    for r in b_records:
        b_by_species.setdefault(r["species"], []).append(r)
    for recs in b_by_species.values():
        recs.sort(key=lambda r: r["ts"])
    for ra in a_records:
        for rb in b_by_species.get(ra["species"], []):
            diff = rb["ts"] - ra["ts"]
            if abs(diff) <= MAX_DIFF_SECONDS:
                edges.append(
                    {
                        "a": ra,
                        "b": rb,
                        "diff": round(abs(diff), 3),
                        "cost": int(round(abs(diff) * 1000)),
                    }
                )
    edges.sort(key=lambda e: (e["cost"], e["a"]["id"], e["b"]["id"]))
    return edges


def build_conclusions(snapshot: Snapshot, records: list[dict]) -> dict:
    """对给定记录集合计算配对与分组结论。

    records 已由查询层按时间窗裁剪；待核判定仍通过 snapshot 的完整
    覆盖表进行，因此不依赖历史明细。
    """
    a_records = sorted(
        [r for r in records if r["station"] == "A"],
        key=lambda r: (r["ts"], r["id"]),
    )
    b_records = sorted(
        [r for r in records if r["station"] == "B"],
        key=lambda r: (r["ts"], r["id"]),
    )

    edges = _candidate_edges(a_records, b_records)
    dsu = _DSU([r["id"] for r in records])
    for e in edges:
        dsu.union(e["a"]["id"], e["b"]["id"])

    groups: dict[str, dict] = {}
    for r in records:
        root = dsu.find(r["id"])
        groups.setdefault(
            root,
            {"records": [], "edges": [], "pending": False, "reasons": []},
        )["records"].append(r)

    for e in edges:
        root = dsu.find(e["a"]["id"])
        groups[root]["edges"].append(e)

    def opposite_periods_unconfirmed(rec: dict) -> list[tuple[str, int]]:
        """该记录对站在同一封存时段上是否还有未确认覆盖。"""
        other = "B" if rec["station"] == "A" else "A"
        period = rec["period"]
        cov = snapshot.coverage(other, period)
        if cov is None or cov.status != "confirmed" or not cov.complete:
            return [(other, period)]
        return []

    for g in groups.values():
        for rec in g["records"]:
            missing = opposite_periods_unconfirmed(rec)
            if missing:
                g["pending"] = True
                g["reasons"].append(
                    f"{rec['id']} 的对站时段 {missing[0][0]}/p{missing[0][1]} 尚未确认覆盖"
                )

    # 组件 id 用组内最小记录 id 决定，保证同分稳定
    root_to_cid = {
        root: f"C{min(r['id'] for r in g['records'])}"
        for root, g in groups.items()
    }

    pair_results: list[PairResult] = []
    pair_counter = 0
    total_pairs = 0
    total_diff = 0.0
    pending_pairs = 0
    unpaired = 0

    for root in sorted(groups, key=lambda x: root_to_cid[x]):
        g = groups[root]
        cid = root_to_cid[root]
        ga = sorted(
            [r for r in g["records"] if r["station"] == "A"],
            key=lambda r: r["id"],
        )
        gb = sorted(
            [r for r in g["records"] if r["station"] == "B"],
            key=lambda r: r["id"],
        )
        idx_a = {r["id"]: i for i, r in enumerate(ga)}
        idx_b = {r["id"]: i for i, r in enumerate(gb)}
        n_left, n_right = len(ga), len(gb)
        source = n_left + n_right
        sink = source + 1
        mcf = _MinCostFlow(sink + 1)
        for i in range(n_left):
            mcf.add_edge(source, i, 0)
        for j in range(n_right):
            mcf.add_edge(n_left + j, sink, 0)
        for e in g["edges"]:
            mcf.add_edge(idx_a[e["a"]["id"]], n_left + idx_b[e["b"]["id"]], e["cost"])
        flow, cost = mcf.flow(source, sink)

        status = PENDING if g["pending"] else CONFIRMED_RESULT
        used_a: set[str] = set()
        used_b: set[str] = set()
        for u, v, _c in mcf.matched_edges(source, list(range(n_left))):
            ra, rb = ga[u], gb[v - n_left]
            pair_counter += 1
            total_pairs += 1
            diff = round(abs(rb["ts"] - ra["ts"]), 3)
            total_diff += diff
            if g["pending"]:
                pending_pairs += 1
            pair_results.append(
                PairResult(
                    pair_id=f"P{pair_counter:05d}",
                    station_a="A",
                    record_a=ra,
                    station_b="B",
                    record_b=rb,
                    time_diff=diff,
                    component_id=cid,
                    status=status,
                )
            )
            used_a.add(ra["id"])
            used_b.add(rb["id"])

        for r in g["records"]:
            if r["id"] in used_a or r["id"] in used_b:
                continue
            unpaired += 1
            if r["station"] == "A":
                pair_results.append(
                    PairResult(
                        pair_id=None,
                        station_a="A",
                        record_a=r,
                        component_id=cid,
                        status=status,
                    )
                )
            else:
                pair_results.append(
                    PairResult(
                        pair_id=None,
                        station_a="B",
                        record_a=r,
                        component_id=cid,
                        status=status,
                    )
                )

    pair_results.sort(
        key=lambda pr: (
            pr.component_id,
            pr.record_a["ts"],
            pr.record_a["id"],
        )
    )
    components = [
        {
            "component_id": root_to_cid[root],
            "status": PENDING if g["pending"] else CONFIRMED_RESULT,
            "record_ids": sorted(r["id"] for r in g["records"]),
            "reasons": g["reasons"],
        }
        for root, g in sorted(groups.items(), key=lambda kv: root_to_cid[kv[0]])
    ]
    return {
        "summary": {
            "records": len(records),
            "station_a_records": len(a_records),
            "station_b_records": len(b_records),
            "pairs": total_pairs,
            "pending_pairs": pending_pairs,
            "unpaired": unpaired,
            "total_time_diff": round(total_diff, 3),
            "components": len(components),
            "pending_components": sum(1 for c in components if c["status"] == PENDING),
        },
        "components": components,
        "results": [pr.to_dict() for pr in pair_results],
    }
