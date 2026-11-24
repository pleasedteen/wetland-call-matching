"""确定性模拟数据集。

两座录音站 A / B 的数据由同一份“鸟鸣事件计划”派生：
- 部分事件两站都录到（时间抖动控制在 2 秒内，用于配对）；
- 部分事件只被一个站录到（无法配对）；
- 每个站每时段另有独立记录与固定数量的损坏条目。

所有随机数都来自固定种子，任意进程重复生成结果完全一致，
因此验证脚本可以直接用本模块计算期望值。
"""
from __future__ import annotations

import random

SPECIES = [
    "REED_WARBLER",
    "SANDPIPER",
    "HERON",
    "RAIL",
    "PLOVER",
    "TERN",
]

BASE_TS = 1_795_488_000          # 固定基准时间戳（UTC 秒），保证可复现
PERIOD_SECONDS = 3600
PERIOD_COUNT = 4
EVENTS_PER_PERIOD = 40
SHARED_PROB = 0.6
EXTRA_PER_STATION = 8
CORRUPT_PER_PERIOD = 2
PAGE_SIZE = 20
MAX_DIFF_SECONDS = 2.0
STATIONS = ("A", "B")


def _shared_plan(period_index: int) -> list[dict]:
    """生成某一时段的全局鸟鸣事件计划（两站共用）。"""
    rng = random.Random(9100 + period_index)
    plan = []
    for _ in range(EVENTS_PER_PERIOD):
        both = rng.random() < SHARED_PROB
        plan.append(
            {
                "species": SPECIES[rng.randrange(len(SPECIES))],
                "ts": BASE_TS
                + period_index * PERIOD_SECONDS
                + rng.randrange(30, PERIOD_SECONDS - 30),
                "both": both,
                "owner": rng.choice(STATIONS),
                "jitter_a": round(rng.uniform(-0.8, 0.8), 3),
                "jitter_b": round(rng.uniform(-0.8, 0.8), 3),
            }
        )
    return plan


def build_period(station: str, period_index: int) -> list[dict]:
    """构造单站单时段的全部记录（含损坏标记，尚未分页）。"""
    records: list[dict] = []
    for ev in _shared_plan(period_index):
        if ev["both"]:
            jitter = ev["jitter_a"] if station == "A" else ev["jitter_b"]
        elif ev["owner"] == station:
            jitter = ev["jitter_a"] if station == "A" else ev["jitter_b"]
        else:
            continue
        records.append(
            {
                "ts": round(ev["ts"] + jitter, 3),
                "species": ev["species"],
            }
        )

    rng = random.Random(2000 + period_index * 17 + (1 if station == "B" else 0))
    for _ in range(EXTRA_PER_STATION):
        records.append(
            {
                "ts": BASE_TS
                + period_index * PERIOD_SECONDS
                + rng.randrange(30, PERIOD_SECONDS - 30),
                "species": SPECIES[rng.randrange(len(SPECIES))],
            }
        )

    records.sort(key=lambda r: (r["ts"], r["species"]))
    for i, r in enumerate(records):
        r["id"] = f"{station}-p{period_index}-{i:04d}"
        r["station"] = station
        r["period"] = period_index
        r["freq_hz"] = rng.randint(1800, 6200)
        r["confidence"] = round(rng.uniform(0.55, 0.99), 3)

    # 固定位置的损坏条目：重取结果一致，客户端应跳过且计数。
    crng = random.Random(3000 + period_index * 31 + ord(station))
    for idx in crng.sample(range(len(records)), CORRUPT_PER_PERIOD):
        records[idx]["_corrupt"] = True
    return records


def build_station(station: str) -> dict[int, list[dict]]:
    return {p: build_period(station, p) for p in range(PERIOD_COUNT)}


def period_window(period_index: int) -> tuple[float, float]:
    start = BASE_TS + period_index * PERIOD_SECONDS
    return float(start), float(start + PERIOD_SECONDS)
