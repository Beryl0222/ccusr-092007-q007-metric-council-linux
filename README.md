# 文化指标口径审议后端

文化产业规划进入年度监测后，各地把数字展陈收入、文旅联票、平台创作者收入
按不同口径计入"文化消费"，汇总值看似增长却无法比较区域差异，也说不清
一次修订为何改变多年趋势。本服务为规划研究组提供**指标口径审议后端**：
业务部门按统一合同提交定义，统计/财政/行业三组分别审议、利益相关者回避，
冲突口径进入公开分歧而不被静默合并，数据修订只生成带原因的新版本，
正式报告永远引用发布时冻结的快照。

## 核心规则

| 需求 | 实现位置 | 规则 |
| --- | --- | --- |
| 统一口径合同 | `proposals.py` | `metric_proposal.json` 结构：定义、单位、适用行业、数据来源、去重边界、生效区间 |
| 三组影响意见与表决 | `review.py` | statistics/fiscal/industry 分别计票，三组多数通过才算通过；平票视为否决 |
| 利益回避 | `review.py` | 专家利益标识与提案来源/行业/部门任一重合即强制回避，可留书面意见但不能投票；某组全员回避则表决不成立 |
| 兼容定义派生 | `review.py` | 覆盖互斥或一方显式剔除另一方口径时可 `sum` 派生 |
| 冲突不静默合并 | `review.py` | 覆盖重叠且无剔除声明 → `CompatibilityConflictError`，只能登记 `PublicDisagreement` |
| 单位换算 | `units.py` | 同量纲换算（元/万元/亿元、人次/万人次），跨量纲（金额↔人次）拒绝 |
| 去重与跨层级校验 | `ingest.py` | 按 `identity_keys` 批内/批间去重；同一活动在县/市/省重复报送按 `bottom_up` 规则整批拒收 |
| 修订只另立版本 | `series.py` | 来源更正/区划变化/迟报数据分别以 `source_correction`/`region_change`/`late_report` 生成新版本，附文字原因 |
| 报告冻结 | `series.py` | `ReportSnapshot` 复制冻结时刻的值与批次引用，后续修订不改变报告数字 |
| 图表值谱系 | `council.py` `trace()` | 值 → 冻结快照 → 当时定义（含 revision）→ 输入批次 → 三组意见与表决 |
| 替代口径并排 | `council.py` `compare()` | 多口径同区域/期自动换算同单位并排，给出差值，并列示公开分歧 |
| 访问控制 | `access.py` | 角色权限位；决策者有报告/谱系/分歧读取权，但永无 `detail.read`；视图另有企业明细字段防泄漏第二道防线 |
| 审计留痕 | `storage.py` | 所有状态变更追加写入 JSONL 事件，不就地更新 |

## 数据合同版本与迁移

- **v1（信封）**：`schema_version=1` 的 `DomainRecord` 只有
  `record_id / domain / occurred_at / revision / source`，承载来源标识。
- **v2（完整提案）**：在信封之上增加 `code/definition/unit/industries/
  data_sources/dedup/effective/submitting_dept` 等业务要素。

迁移方式（`proposals.from_envelope`）：v1 记录**不做字段猜测或默认填充**，
必须由业务部门显式补齐全部业务要素后构造 v2；信封上的
`record_id/occurred_at/revision/source` 原样保留，维持追溯链。
`load_record` 信封读取器对 v2 附加字段保持宽容，旧调用方无需改动。

## 模块

```
src/metric_council/
├── contracts.py  # v1/v2 共用数据信封（既有标识与时间含义不变）
├── proposals.py  # v2 指标提案合同与校验、v1→v2 迁移
├── units.py      # 单位登记与同量纲换算
├── regions.py    # 行政区划变更链
├── review.py     # 专家、回避、三组表决、公开分歧、派生指标
├── ingest.py     # 输入批次、单位归一、去重、跨层级校验、迟报标记
├── series.py     # 序列版本（不可原地改写）与冻结快照
├── access.py     # 角色权限与企业明细防泄漏
├── storage.py    # JSONL 追加事件存储
└── council.py    # CouncilService 门面：编排全链 + trace/compare 查询
```

典型流程：

```python
from metric_council import CouncilService
svc = CouncilService(event_log="audit.jsonl")
svc.register_expert(chair, Expert(...))          # 登记三组专家
proposal = svc.submit_proposal(dept, payload)    # 业务部门提交 v2 提案
svc.open_review(chair, proposal.record_id, now)
svc.add_opinion(expert, proposal.record_id, impact, now)  # 利益专家自动标回避
svc.cast_vote(expert, proposal.record_id, True, rationale, now)
svc.close_vote(chair, proposal.record_id, "分歧标题", now)
svc.ingest_batch(engineer, batch, "CULT_CONSUME")          # 单位归一+去重校验
svc.build_initial_series(engineer, "CULT_CONSUME", now)
snapshot = svc.freeze_report(editor, "R-2026H1", "…", ["CULT_CONSUME"], now)
svc.correct_source(engineer, old, new, "CULT_CONSUME", "原因", now)  # 另立 v2
svc.trace(dm, snapshot.snapshot_id, "CULT_CONSUME", "330106", "2026-Q1")
svc.compare(dm, ["CULT_CONSUME", "CULT_CONSUME_WIDE"], "330106", "2026-Q1")
```

## 本地检查

```bash
python -m unittest discover -s tests   # 或 python -m pytest
```

49 个测试覆盖：合同与迁移、单位换算、回避与法定人数、平票否决与公开分歧、
冲突禁合并、兼容派生、批内/批间/跨层级重复、迟报、来源更正、区划变更、
快照不可变、权限拒绝、谱系字段完整、并排单位换算与冻结/当前对比。
