"""数据采集：输入批次、单位归一、去重与跨层级重复校验。

设计要点：

- 报送值的单位可能各异（元/万元/亿元），入库时按指标提案声明的单位
  执行同量纲换算，跨量纲直接报错；
- 去重边界（提案中的 ``identity_keys``）在批次内、批次间同时生效，
  同一笔经济活动（同 region/activity/period/channel）不得出现两次；
- ``bottom_up`` 规则下，同一活动若县级与其所属市级同时报送，则视为
  跨层级重复——只能由最底层计数后逐级加总；
- 迟报数据不丢弃：超过来源允许的迟报窗口仍可入库，但标记为迟报，
  由序列层另立带原因的新版本，绝不改写原值；
- 采集只对外产出**区域级汇总点**，企业明细仅用于内部加总，
  不进入任何对外结果（访问控制见 :mod:`metric_council.access`）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .errors import DuplicationViolation, WorkflowError
from .proposals import MetricProposal
from .regions import RegionRegistry
from .units import convert as convert_unit

# 序列修订的三类原因（与 series 层共享）。
REASON_SOURCE_CORRECTION = "source_correction"  # 来源更正
REASON_REGION_CHANGE = "region_change"          # 行政区划变化
REASON_LATE_REPORT = "late_report"              # 迟报数据


@dataclass(frozen=True)
class Observation:
    region_code: str
    period: str                       # 统计期，如 2026-Q1 / 2026-03
    period_end: object                # 该期结束日（date），用于迟报与区划判定
    activity_id: str
    channel: str
    value: float
    unit: str
    level: str                        # province / city / county
    industry: str
    enterprise_id: str | None = None  # 仅企业明细来源允许非空
    submitted_at: object | None = None
    batch_id: str | None = None
    metric_code: str | None = None
    source_id: str | None = None
    normalized_value: float | None = None
    normalized_unit: str | None = None
    late: bool = False
    region_code_at_event: str | None = None
    reasons: tuple[str, ...] = ()

    def identity(self, keys: tuple[str, ...]) -> tuple:
        row = {
            "region_code": self.region_code_at_event or self.region_code,
            "activity_id": self.activity_id,
            "period": self.period,
            "channel": self.channel,
            "enterprise_id": self.enterprise_id,
        }
        return tuple(row[k] for k in keys)


@dataclass(frozen=True)
class InputBatch:
    batch_id: str
    source_id: str
    received_at: object               # datetime
    observations: tuple
    note: str = ""


@dataclass(frozen=True)
class AcceptedPoint:
    """采集通过后产出的区域级点（不携带企业标识）。"""

    metric_code: str
    region_code: str
    period: str
    value: float                      # 已按提案单位归一
    unit: str
    level: str
    source_id: str
    batch_id: str
    channel: str
    activity_id: str
    late: bool
    reasons: tuple[str, ...] = ()


class IngestService:
    def __init__(self, regions: RegionRegistry) -> None:
        self.regions = regions
        self._proposals: dict[str, MetricProposal] = {}
        self._parents: dict[str, str] = {}   # 子区划 -> 父区划
        self._seen: dict[str, set[tuple]] = {}
        self._obs: list[Observation] = []    # 内部归一后的活动记录
        self._points: list[AcceptedPoint] = []
        self._batches: dict[str, InputBatch] = {}

    def register_proposal(self, proposal: MetricProposal) -> None:
        if proposal.status != "approved":
            raise WorkflowError(
                f"提案 {proposal.record_id} 未通过审议，不能开始采集"
            )
        self._proposals[proposal.code] = proposal
        self._seen.setdefault(proposal.code, set())

    def set_parent(self, child_code: str, parent_code: str) -> None:
        self._parents[child_code] = parent_code

    def ancestors(self, code: str) -> list[str]:
        chain: list[str] = []
        cur = code
        while cur in self._parents:
            cur = self._parents[cur]
            chain.append(cur)
        return chain

    def ingest(self, batch: InputBatch, metric_code: str) -> list[AcceptedPoint]:
        proposal = self._proposals.get(metric_code)
        if proposal is None:
            raise WorkflowError(f"指标 {metric_code} 无已通过审议的定义")
        source = next(
            (d for d in proposal.data_sources if d.source_id == batch.source_id), None
        )
        if source is None:
            raise WorkflowError(
                f"来源 {batch.source_id} 不在指标 {metric_code} 已审议的来源清单内"
            )
        if batch.batch_id in self._batches:
            raise WorkflowError(f"批次 {batch.batch_id} 已入库，禁止重复提交")

        violations: list[dict] = []
        normalized: list[Observation] = []
        batch_seen: set[tuple] = set()

        for raw in batch.observations:
            obs = self._normalize(raw, batch, proposal, source, metric_code,
                                  violations)
            if obs is None:
                continue
            key = obs.identity(proposal.dedup.identity_keys)
            if key in batch_seen:
                violations.append({"type": "within_batch_duplicate",
                                   "key": list(key), "activity_id": obs.activity_id})
                continue
            if key in self._seen[metric_code]:
                violations.append({"type": "cross_batch_duplicate",
                                   "key": list(key), "activity_id": obs.activity_id})
                continue
            if self._hierarchy_violation(obs, proposal, normalized, violations):
                continue
            batch_seen.add(key)
            normalized.append(obs)

        if violations:
            raise DuplicationViolation(
                f"批次 {batch.batch_id} 存在 {len(violations)} 项重复/越界问题，"
                "整批拒收（未写入任何数据）",
                violations=violations,
            )

        points: list[AcceptedPoint] = []
        for obs in normalized:
            points.append(self._point_from(obs))
            self._seen[metric_code].add(
                obs.identity(proposal.dedup.identity_keys)
            )
        self._batches[batch.batch_id] = batch
        self._obs.extend(normalized)
        self._points.extend(points)
        return points

    # —— 内部 ——

    def _normalize(self, raw: Observation, batch: InputBatch,
                   proposal: MetricProposal, source, metric_code: str,
                   violations: list[dict]) -> Observation | None:
        if raw.industry not in proposal.industries:
            violations.append({"type": "industry_out_of_scope",
                               "industry": raw.industry,
                               "activity_id": raw.activity_id})
            return None
        if raw.enterprise_id and source.granularity != "enterprise":
            violations.append(
                {"type": "enterprise_detail_from_aggregated_source",
                 "activity_id": raw.activity_id}
            )
            return None
        try:
            value = convert_unit(raw.value, raw.unit, proposal.unit)
        except Exception as exc:  # UnitConversionError：未登记或跨量纲
            violations.append({"type": "unit_error", "unit": raw.unit,
                               "activity_id": raw.activity_id, "detail": str(exc)})
            return None

        submitted = raw.submitted_at or batch.received_at
        deadline = datetime.combine(raw.period_end, datetime.max.time(),
                                    tzinfo=submitted.tzinfo) \
            + timedelta(days=source.reporting_lag_days)
        late = submitted > deadline

        event_region = self.regions.code_on(raw.region_code, raw.period_end)
        reasons = []
        if late:
            reasons.append(REASON_LATE_REPORT)
        if event_region != raw.region_code:
            reasons.append(REASON_REGION_CHANGE)
        return replace(
            raw, batch_id=batch.batch_id, submitted_at=submitted,
            metric_code=metric_code, source_id=batch.source_id,
            normalized_value=value, normalized_unit=proposal.unit,
            late=late, region_code_at_event=event_region,
            reasons=tuple(reasons),
        )

    def _hierarchy_violation(self, obs: Observation, proposal: MetricProposal,
                             same_batch: list[Observation],
                             violations: list[dict]) -> bool:
        """同一活动（活动+期+渠道相同）出现在祖先/子孙层级即重复。

        区划已归一到事件时代码。无论 ``bottom_up`` 还是 ``top_level_only``，
        跨层级重复计数都被拒绝；两种规则都要求同一活动只在一个层级入账，
        区别只在合法入账的方向。
        """
        region = obs.region_code_at_event or obs.region_code
        ancestors = set(self.ancestors(obs.region_code))
        prior_records = list(self._obs) + same_batch
        for prior in prior_records:
            if (prior.activity_id, prior.period, prior.channel) != (
                    obs.activity_id, obs.period, obs.channel):
                continue
            prior_region = prior.region_code_at_event or prior.region_code
            related = (prior_region in ancestors
                       or region in self.ancestors(prior.region_code))
            if not related:
                continue
            violations.append({
                "type": "cross_hierarchy_duplicate",
                "activity_id": obs.activity_id, "period": obs.period,
                "region": region, "other_region": prior_region,
                "other_level": prior.level,
                "rule": proposal.dedup.hierarchy_rule,
            })
            return True
        return False

    # —— 来源更正与区划重映射：驱动带原因的新序列版本 ——

    def correct_batch(self, old_batch_id: str, new_batch: InputBatch,
                      metric_code: str) -> list[AcceptedPoint]:
        """来源更正：撤回旧批次、以更正批次重走校验。

        旧批次的点与身份键先整体移除，再按正常流程校验新批次，因此新批次
        可以携带同一活动的修正值。旧值在序列层仍由旧版本/已冻结快照保留，
        这里只影响"当前全集"。新批次来源必须与被更正批次一致。
        """
        if old_batch_id not in self._batches:
            raise WorkflowError(f"被更正批次不存在：{old_batch_id}")
        old = self._batches[old_batch_id]
        if new_batch.source_id != old.source_id:
            raise WorkflowError(
                "来源更正必须来自同一数据来源；"
                f"原批次来源 {old.source_id}，新批次来源 {new_batch.source_id}"
            )
        if new_batch.batch_id in self._batches:
            raise WorkflowError(f"批次 {new_batch.batch_id} 已入库")

        proposal = self._proposals[metric_code]
        old_keys = {
            o.identity(proposal.dedup.identity_keys)
            for o in self._obs if o.batch_id == old_batch_id
        }
        self._seen[metric_code] -= old_keys
        self._obs = [o for o in self._obs if o.batch_id != old_batch_id]
        self._points = [p for p in self._points if p.batch_id != old_batch_id]
        del self._batches[old_batch_id]
        return self.ingest(new_batch, metric_code)

    def reapply_region_mappings(self, metric_code: str) -> list[str]:
        """区划登记后，按事件期重新归一整条指标的点。

        返回区域归属发生变化的 ``region/period/activity`` 列表；
        随后由序列层以 ``region_change`` 原因另立版本。
        """
        moved: list[str] = []
        rebuilt_obs: list[Observation] = []
        for obs in self._obs:
            if obs.metric_code == metric_code:
                mapped = self.regions.code_on(obs.region_code, obs.period_end)
                if mapped != (obs.region_code_at_event or obs.region_code):
                    obs = replace(
                        obs, region_code_at_event=mapped,
                        reasons=tuple({*obs.reasons, REASON_REGION_CHANGE}))
                    moved.append(f"{mapped}/{obs.period}/{obs.activity_id}")
            rebuilt_obs.append(obs)
        self._obs = rebuilt_obs
        self._points = [self._point_from(o) for o in rebuilt_obs]
        return moved

    @staticmethod
    def _point_from(obs: Observation) -> AcceptedPoint:
        return AcceptedPoint(
            metric_code=obs.metric_code,
            region_code=obs.region_code_at_event or obs.region_code,
            period=obs.period, value=obs.normalized_value,
            unit=obs.normalized_unit, level=obs.level,
            source_id=obs.source_id, batch_id=obs.batch_id,
            channel=obs.channel, activity_id=obs.activity_id,
            late=obs.late, reasons=obs.reasons,
        )

    # —— 查询 ——

    def points(self, metric_code: str) -> list[AcceptedPoint]:
        return [p for p in self._points if p.metric_code == metric_code]

    def all_points(self) -> list[AcceptedPoint]:
        return list(self._points)
