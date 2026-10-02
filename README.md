# 文化指标口径审议后端

文化产业规划进入年度监测后，数字展陈收入、文旅联票、平台创作者收入常被各地
按不同口径计入"文化消费"：汇总值看似增长，却无法比较区域差异，也说不清一次
修订为何改变多年趋势。本服务为规划研究组提供**指标口径审议后端**：

* 业务部门按 `metric_proposal.json` 结构提交定义、单位、适用行业、数据来源、
  去重边界和生效区间；
* 统计、财政、行业专家分别提出影响意见并表决，存在利益关系者必须回避；
* 兼容定义可形成派生指标，冲突定义进入**公开分歧**，不能被静默合并；
* 来源更正、行政区划变化、迟报数据只生成**带原因的新序列版本**，正式报告
  继续引用发布时冻结的快照；
* 服务执行单位换算、跨层级重复校验与访问控制；
* 决策者可从一个图表值追到当时的**定义、输入批次、审议意见**，并排查看
  替代口径造成的变化，全程接触不到企业明细。

## `metric_proposal.json` 结构

见 `fixtures/metric_proposal.json`（脱敏样例）。信封字段
`schema_version / record_id / domain / occurred_at / revision / source`
沿用既有最小合同，其标识与时间含义不变；新增口径字段：

| 字段 | 含义 |
| --- | --- |
| `metric_code` / `metric_name` | 跨修订稳定的指标代码与名称 |
| `definition` | 口径定义文本 |
| `unit` | 计量单位代码（如 `CNY_10k_YUAN`、`PERSON_VISIT`、`PERCENT`） |
| `industries` | 适用行业代码集合 |
| `data_sources` | 来源声明：id、名称、粒度、是否含企业明细 |
| `dedup_boundary` | 去重主体 / 去重键 / 统计窗口（可比性的核心） |
| `effective_range` | 生效区间，左闭右开，`end=null` 表示至今 |
| `proposer` / `status` | 提交方与状态 |

旧信封仍可用 `load_record()` 读取（只取信封，忽略未知字段）。

## 状态机（新增状态的迁移方式）

```
draft ──submit──▶ submitted ──┬─ 三委员会全部多数赞成 ─▶ approved   可派生、可挂序列
                              ├─ 只有反对无赞成       ─▶ rejected   终态，修订后以新 revision 重提
                              └─ 委员会立场分裂       ─▶ disputed   进入公开分歧，不得静默合并
```

`approved / rejected / disputed` 均为终态；定义不可变，修订走同一
`metric_code` 的新 `revision`，旧版本在其生效区间内继续有效。

## 模块划分

| 模块 | 职责 |
| --- | --- |
| `proposals.py` | 提案合同、校验、状态机、兼容性判定 |
| `units.py` | 单位登记与换算（**单位族**内可换算，跨族拒绝：万元↔人次无意义） |
| `geography.py` | 行政区划层级；边界变更按生效日登记，可回放历史隶属 |
| `review.py` | 三委员会意见、利益回避登记、表决与汇审 |
| `derivations.py` | 派生指标（sum/weighted/ratio）；冲突组件直接拒绝 |
| `disputes.py` | 公开分歧登记与解决留痕 |
| `series.py` | 输入批次、带原因版本（来源更正/区划变化/迟报）、企业明细边界守卫、跨层级重复校验 |
| `snapshots.py` | 发布冻结；快照值不可改写，冻结值 vs 最新值对照 |
| `access.py` | 角色矩阵；`enterprise_detail.read` 对所有角色为空集 |
| `lineage.py` | 图表值 → 定义 / 批次 / 意见 / 回避 的证据链 |
| `compare.py` | 替代口径并排对比；同族给差额但不合并，跨族只并列 |
| `service.py` | 统一门面，每个方法先过访问策略 |
| `api.py` / `__main__.py` | 零依赖 WSGI JSON 接口 |

## 关键规则

* **利益回避**：专家对提案声明利益关系后，表决资格被取消；仍尝试投票抛
  `ConflictOfInterestError`，回避记录进入血缘。委员会全员回避时审议不足
  法定人数，不能关闭。
* **版本只增不改**：三类事后变化（`source_correction` /
  `boundary_change` / `late_report`）必须填写原因说明，生成新版本并链接
  父版本；已发布快照永远返回发布时值，改写尝试抛 `SnapshotFrozenError`。
* **跨层级重复校验**：上级"已含下级"总量与下级点同时存在、或隶属两点的
  去重主体指纹出现交集，均抛 `DoubleCountError`。上报只接受不可逆指纹
  （`fingerprint_subject`），出现企业名称/信用代码等字段的批次直接拒收。
* **角色分离**：业务部门只能提交与上报；专家只能审议；发布人才能发布；
  决策者可读图表与血缘；企业明细对任何角色不可达。

## HTTP 接口

```bash
PYTHONPATH=src python -m metric_council 8080
```

请求头携带 `X-Actor-Id`、`X-Actor-Role`
（`proposer / stat_reviewer / fiscal_reviewer / industry_reviewer /
publisher / decision_maker / auditor`）。端点：

```
POST /api/proposals
POST /api/proposals/{rid}/recusal
POST /api/proposals/{rid}/opinions
POST /api/proposals/{rid}/close
POST /api/series
POST /api/series/{code}/ingest | /revise
POST /api/snapshots
GET  /api/snapshots/{id}/value | /lineage | /frozen-vs-latest | /compare
```

## 本地检查

```bash
python -m unittest discover -s tests
```
