# 滩涂鸟鸣双站采集与配对系统

两座录音站（A/B）记录同一片滩涂的鸟鸣。本程序解决两件事：

1. **按时间查看每条记录的配对状态**——同物种、两站时间差 ≤ 2 秒才可配对，
   先最大化配对数，再最小化总时间差，同分结果稳定；候选关系相连的记录整组出结论，
   组内任一记录的对站候选时段尚未确认覆盖时，整组标“待核”，其余组照常出正式结论。
2. **采集中断后覆盖结论不丢失**——分页与条目都读全才确认覆盖，进度原子快照落盘，
   中断重启只补未确认时段，同一记录重取只计一次。

仅依赖 Python 3.11+ 标准库（Parquet 导出为可选，见文末）。

---

## 1. 目录结构

```
birdsurvey/
  dataset.py    确定性模拟数据集（固定种子，可复现，含真值）
  station.py    单站 HTTP 服务：会话轮换 / 限流 / 双版式 / 损坏条目 / 回执链
  client.py     站点客户端：自动会话恢复、限流等待、table/card 解析
  storage.py    原子快照 JSONL 存储（按 站点-时段 分区，支持中断续采）
  collect.py    采集编排：覆盖确认、去重、信号处理、定期落盘
  matching.py   最小费用最大流配对 + 并查集分组 + 待核判定
  query.py      查询 CLI：pairs / coverage / summary / export
  export.py     JSONL（默认）与 Parquet（可选）导出
run_station.py  启动一座模拟站点
run_collect.py  启动采集
run_query.py    查询
tests/          24 个单元/协议测试 + 端到端验证
verify.sh       一条命令跑完全部本地验证（约 16 秒，远低于 80 秒）
```

---

## 2. 启动本地站点（两个终端，或加 & 后台）

```bash
python3 run_station.py --station A --port 8101
python3 run_station.py --station B --port 8102
```

站点协议（两站一致）：

| 机制 | 行为 |
| --- | --- |
| 会话 | `POST /sessions` 建立；每个会话有请求预算，用尽即**轮换**，旧会话与旧翻页令牌立即失效（409） |
| 翻页令牌 | HMAC 签名，第 2 页起必须带 `X-Page-Receipt`（上一页回执），链断返回 400 |
| 限流 | 站点级窗口限流，超限返回 429 与 `retry_after`（秒），客户端按此等待后重试同一页 |
| 版式 | 页面在 `table` / `card` 间交替，字段名不同，客户端统一解析 |
| 损坏条目 | 残缺结构（缺 id/species、类型非法等）下发，客户端跳过并计数，同页可读记录保留 |

---

## 3. 运行采集

```bash
python3 run_collect.py \
  --station-a http://127.0.0.1:8101 \
  --station-b http://127.0.0.1:8102 \
  --data-dir ./birddata
```

要点：

- **覆盖确认**：一个站点-时段只有“分页读全”且“可读 + 损坏条目数达到站点声明总数”
  才标记 `confirmed`，否则保持 `partial`。
- **中断续采**：`Ctrl-C` / `SIGTERM` 会先把当前进度原子落盘再退出；重复运行同一条
  采集命令即可，已确认时段跳过，未确认时段从首页重取补齐。
- **重取去重**：可读记录按 id 去重（只计一次）；损坏数按已读页号集合去重，
  会话轮换导致的整段重取不会重复计数。
- 采集过程中可随时用第 4 节的命令查询，看到的永远是某一次**完整落盘**的快照。

数据目录（原子快照，代次递增）：

```
birddata/
  CURRENT                # 指针，指向当前代
  gen-000001/
    manifest.json        # 覆盖状态 + 分区索引（含每分区时间窗）
    records/A-p0.jsonl   # 按 站点-时段 分区，一行一条 JSON
```

---

## 4. 运行查询

时间参数支持 Unix 秒或 ISO-8601（如 `2026-11-24T03:00:00`）。

```bash
# 配对结论（正式 / 待核、配对与未配对明细、分组、总时间差）
python3 run_query.py --data-dir ./birddata pairs
python3 run_query.py --data-dir ./birddata pairs \
    --start 1795488100 --end 1795488200          # 只看某时间窗
python3 run_query.py --data-dir ./birddata pairs --station A --format json

# 覆盖确认状态（每个站点-时段的页数、好/坏条目数、窗口）
python3 run_query.py --data-dir ./birddata coverage

# 汇总指标
python3 run_query.py --data-dir ./birddata summary

# 导出当前快照
python3 run_query.py --data-dir ./birddata export --out-dir ./out --format jsonl
python3 run_query.py --data-dir ./birddata export --out-dir ./out --format parquet
```

### 一致性与短查询保证

- **单次查询一致**：查询打开一次 `CURRENT` 并绑定该代目录，期间采集器发布新一代
  不影响本次查询——记录与覆盖信息必然来自同一次完整落盘；补采完成后再次查询即反映新结论。
- **短查询不随历史膨胀**：记录按站点-时段分区，manifest 记录每分区时间窗。
  时间窗查询只打开窗口相交的分区（验证：100 秒窗口在 8 个时段下只打开 2 个分区，
  历史再增长也不变），读取量与无关历史总量无关。

### 配对与待核语义

- 候选边：同物种且 `|ts_A − ts_B| ≤ 2s`。用最小费用最大流（费用为毫秒级时间差）
  先求最大配对数，再在其中求最小总时间差；边按确定性顺序枚举，等费用路径 FIFO，
  结果与输入顺序无关（测试用独立回溯暴力枚举核对了最大配对数与最优时间差）。
- 每条记录最多参与一次配对；未配对记录也随所属分组输出。
- 并查集把候选关系相连的记录聚成组；组内任一记录的**对站同时段**覆盖未确认时，
  整组（含其中的配对与未配对条目）标 `pending` 并给出原因，其他组仍为 `confirmed`。

---

## 5. 一条命令完成本地验证（80 秒内）

```bash
./verify.sh
```

实测约 **16 秒**完成，输出可读的逐项 PASS/FAIL，包含：

- 24 个单元/协议测试：双版式解析、损坏跳过、限流 429 与 `retry_after`、
  会话轮换 409 与客户端自动恢复、回执链断裂拒绝、旧令牌失效、
  存储去重、原子快照隔离、短查询分区裁剪、覆盖确认规则、配对最优性与稳定性、待核分组、导出；
- 端到端：采集中途 SIGTERM → 快照可读且存在未确认时段 → 重启补采 →
  8 个站点-时段全确认、307 条可读 + 16 条损坏与数据集真值一致、无重复 →
  配对数 89 / 总时间差 39.976s 与暴力枚举一致 → 快照一致性与分区裁剪 →
  人工制造 B/p2 未确认后 40 个关联组整组待核、其余组照常出正式结论。

也可分开运行：

```bash
python3 tests/run_tests.py      # 单元/协议测试
python3 -u tests/e2e_verify.py  # 端到端（自动启停站点）
```

---

## 6. Parquet 支持（可选）

核心存储与 JSONL 导出零依赖。需要 Parquet 时：

```bash
pip install pyarrow
python3 run_query.py --data-dir ./birddata export --out-dir ./out --format parquet
```

未安装 `pyarrow` 时该命令返回退出码 2 并提示安装方式，其余功能不受影响。
