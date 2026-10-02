"""指标提案：业务部门提交的"口径"完整定义。

业务部门必须以本结构提交，字段缺一不可——这正是为了终结
"数字展陈收入、文旅联票、平台创作者收入都被笼统计入文化消费"
而无法比较的局面。``schema_version=2`` 在 v1 信封之上增加业务要素，
迁移方式见 :func:`metric_council.proposals.from_envelope`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

from .errors import ProposalValidationError
from .units import get as get_unit

SCHEMA_VERSION = 2

# 三种专家分组，提案必须同时经受统计、财政、行业视角审议。
PANEL_STATISTICS = "statistics"
PANEL_FISCAL = "fiscal"
PANEL_INDUSTRY = "industry"
PANELS = (PANEL_STATISTICS, PANEL_FISCAL, PANEL_INDUSTRY)

# 生命周期状态。
ST_DRAFT = "draft"          # 部门填报中
ST_REVIEW = "in_review"     # 已进入审议
ST_APPROVED = "approved"    # 三组通过、口径生效
ST_REJECTED = "rejected"    # 表决未通过，进入公开分歧
ST_SUPERSEDED = "superseded"  # 被后续修订替代（保留历史）
LIFECYCLE = (ST_DRAFT, ST_REVIEW, ST_APPROVED, ST_REJECTED, ST_SUPERSEDED)


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class EffectiveRange:
    """口径生效区间，左闭右开；end 为 None 表示长期有效。"""

    start: date
    end: date | None = None

    def covers(self, day: date) -> bool:
        if day < self.start:
            return False
        return self.end is None or day < self.end

    def overlaps(self, other: "EffectiveRange") -> bool:
        if other.end is not None and other.end <= self.start:
            return False
        if self.end is not None and self.end <= other.start:
            return False
        return True


@dataclass(frozen=True)
class DataSource:
    source_id: str
    name: str
    granularity: str          # enterprise(企业明细) / aggregated(已汇总)
    reporting_lag_days: int  # 允许的迟报天数
    industries: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.granularity not in ("enterprise", "aggregated"):
            raise ProposalValidationError(
                f"来源 {self.source_id} 的粒度必须是 enterprise 或 aggregated"
            )
        if self.reporting_lag_days < 0:
            raise ProposalValidationError("迟报天数不能为负")


@dataclass(frozen=True)
class DedupBoundary:
    """去重边界：声明同一笔经济活动在哪些维度上不得重复计入，
    以及跨行政层级汇总时的规则。
    """

    identity_keys: tuple[str, ...]   # 如 ("region_code","enterprise_id","period")
    hierarchy_rule: str              # bottom_up：只允许最底层直接计数后加总
    exclusions: tuple[str, ...] = ()  # 明确剔除的收入类型，如文旅联票中的交通段

    def __post_init__(self) -> None:
        if not self.identity_keys:
            raise ProposalValidationError("去重边界必须声明 identity_keys")
        if self.hierarchy_rule not in ("bottom_up", "top_level_only"):
            raise ProposalValidationError(
                "hierarchy_rule 仅支持 bottom_up / top_level_only"
            )


@dataclass(frozen=True)
class MetricProposal:
    schema_version: int
    record_id: str
    domain: str
    occurred_at: datetime
    revision: int
    source: str

    # —— v2 业务要素 ——
    code: str                              # 指标代码，如 CULT_CONSUME
    name: str
    definition: str                        # 文字定义（计入什么、不计入什么）
    unit: str                              # 计量单位代码，须在 units 登记
    industries: tuple[str, ...]            # 适用行业（国民经济行业分类代码）
    data_sources: tuple[DataSource, ...]
    dedup: DedupBoundary
    effective: EffectiveRange
    submitting_dept: str
    status: str = ST_DRAFT
    supersedes: str | None = None          # 修订时指向被替代的 record_id

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ProposalValidationError(
                f"不支持的提案合同版本 {self.schema_version}，期望 {SCHEMA_VERSION}"
            )
        if not self.code or not self.name or not self.definition:
            raise ProposalValidationError("指标代码、名称与定义均为必填")
        try:
            get_unit(self.unit)  # 单位必须已登记
        except Exception as exc:  # UnitConversionError
            raise ProposalValidationError(str(exc)) from exc
        if not self.industries:
            raise ProposalValidationError("必须声明适用行业")
        if not self.data_sources:
            raise ProposalValidationError("必须声明至少一个数据来源")
        if self.effective.end is not None and self.effective.end <= self.effective.start:
            raise ProposalValidationError("生效区间结束日必须晚于开始日")
        if self.status not in LIFECYCLE:
            raise ProposalValidationError(f"未知生命周期状态：{self.status}")

    def with_status(self, status: str, **changes) -> "MetricProposal":
        if status not in LIFECYCLE:
            raise ProposalValidationError(f"未知生命周期状态：{status}")
        return replace(self, status=status, **changes)


def _ds_from(raw: dict) -> DataSource:
    return DataSource(
        source_id=raw["source_id"],
        name=raw["name"],
        granularity=raw["granularity"],
        reporting_lag_days=raw.get("reporting_lag_days", 0),
        industries=tuple(raw.get("industries", ())),
    )


def proposal_from_dict(payload: dict) -> MetricProposal:
    """从 v2 JSON 结构构造提案并校验。"""
    if payload.get("schema_version") == 1:
        raise ProposalValidationError(
            "schema_version=1 仅为数据信封，不是完整指标提案；"
            "请由提交部门补齐定义/单位/行业/来源/去重边界/生效区间后按 v2 提交"
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ProposalValidationError(
            f"未知 schema_version={payload.get('schema_version')}"
        )
    try:
        eff = payload["effective"]
        proposal = MetricProposal(
            schema_version=SCHEMA_VERSION,
            record_id=payload["record_id"],
            domain=payload.get("domain", "metric_council"),
            occurred_at=_parse_dt(payload["occurred_at"]),
            revision=payload["revision"],
            source=payload.get("source", ""),
            code=payload["code"],
            name=payload["name"],
            definition=payload["definition"],
            unit=payload["unit"],
            industries=tuple(payload["industries"]),
            data_sources=tuple(_ds_from(d) for d in payload["data_sources"]),
            dedup=DedupBoundary(
                identity_keys=tuple(payload["dedup"]["identity_keys"]),
                hierarchy_rule=payload["dedup"]["hierarchy_rule"],
                exclusions=tuple(payload["dedup"].get("exclusions", ())),
            ),
            effective=EffectiveRange(
                start=_parse_day(eff["start"]),
                end=_parse_day(eff["end"]) if eff.get("end") else None,
            ),
            submitting_dept=payload["submitting_dept"],
            status=payload.get("status", ST_DRAFT),
            supersedes=payload.get("supersedes"),
        )
    except KeyError as exc:
        raise ProposalValidationError(
            f"指标提案缺少必填字段：{exc.args[0]}") from exc
    except (TypeError, ValueError) as exc:
        raise ProposalValidationError(f"指标提案字段格式有误：{exc}") from exc
    proposal.validate()
    return proposal


def load_proposal(path: Path) -> MetricProposal:
    return proposal_from_dict(json.loads(path.read_text(encoding="utf-8")))


def from_envelope(*, record_id: str, occurred_at: datetime, revision: int,
                  source: str, **business) -> MetricProposal:
    """v1 → v2 迁移入口。

    v1 记录（schema_version=1 的 DomainRecord 信封）只承载来源标识，
    不含任何业务要素；迁移不做字段猜测或默认填充，而是要求业务部门
    显式补齐 code/definition/unit 等要素后构造 v2 提案。
    信封上的 record_id/occurred_at/revision/source 原样保留以维持追溯链。
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "record_id": record_id,
        "domain": "metric_council",
        "occurred_at": occurred_at.isoformat(),
        "revision": revision,
        "source": source,
        **business,
    }
    return proposal_from_dict(payload)
