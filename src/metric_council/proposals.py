"""指标口径提案的数据合同。

业务部门按 ``metric_proposal.json`` 的结构提交：定义、单位、适用行业、
数据来源、去重边界与生效区间。文件信封字段（``schema_version`` /
``record_id`` / ``domain`` / ``occurred_at`` / ``revision`` / ``source``）
沿用既有最小合同：v1 信封仍可被读取，新增的口径字段使样例升级为
完整提案；旧字段的含义保持不变。

状态迁移（新增状态必须在此说明迁移方式）::

    DRAFT ──submit──▶ SUBMITTED ──review──▶ APPROVED      （可被派生引用、可挂序列）
                                   │
                                   ├────────▶ REJECTED   （终态，可修改后以新 revision 重提）
                                   └────────▶ DISPUTED   （进入公开分歧，不得静默合并）

    APPROVED 定义不可变；需要修订时提交同一 ``metric_code`` 的新 ``revision``，
    旧版本在其生效区间内继续有效，序列按版本分段。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import Enum
from pathlib import Path

from . import units
from .errors import ValidationError

ENVELOPE_FIELDS = (
    "schema_version",
    "record_id",
    "domain",
    "occurred_at",
    "revision",
    "source",
)


class ProposalStatus(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISPUTED = "disputed"


# 合法的状态迁移表
_TRANSITIONS: dict[ProposalStatus, frozenset[ProposalStatus]] = {
    ProposalStatus.DRAFT: frozenset({ProposalStatus.SUBMITTED}),
    ProposalStatus.SUBMITTED: frozenset(
        {
            ProposalStatus.APPROVED,
            ProposalStatus.REJECTED,
            ProposalStatus.DISPUTED,
        }
    ),
    ProposalStatus.APPROVED: frozenset(),
    ProposalStatus.REJECTED: frozenset(),
    ProposalStatus.DISPUTED: frozenset(),
}


@dataclass(frozen=True)
class DomainRecord:
    """既有最小合同：读取 v1 信封时忽略前向新增字段。"""

    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str


def load_record(path: Path) -> DomainRecord:
    """读取提案文件的信封部分（与既有合同兼容，忽略未知字段）。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    envelope = {key: payload[key] for key in ENVELOPE_FIELDS if key in payload}
    return DomainRecord(**envelope)


@dataclass(frozen=True)
class DataSourceRef:
    """一个数据来源声明。"""

    source_id: str
    name: str
    granularity: str
    """上报粒度：``enterprise``（企业明细）/ ``aggregate``（综合汇总）。"""

    contains_enterprise_detail: bool = False
    """该来源是否携带企业明细；明细在服务边界被拒，不得流向决策者。"""


@dataclass(frozen=True)
class DedupBoundary:
    """去重边界：口径可比性的核心。

    两个定义只有在 *同一主体层级、同一去重键、同一统计窗口* 下才兼容。
    例如 ``人次``（visit/window=日）与 ``人``（person/window=年）即使
    数字相近也不是同一口径。
    """

    subject: str
    """去重主体：``enterprise`` / ``consumer`` / ``visit`` / ``transaction`` 等。"""

    keys: tuple[str, ...]
    """去重键，如 ``("enterprise_credit_code",)``。"""

    window: str
    """统计窗口：``day`` / ``month`` / ``quarter`` / ``year`` / ``event``。"""

    note: str = ""


@dataclass(frozen=True)
class EffectiveRange:
    """口径生效区间，左闭右开；``end`` 为 ``None`` 表示至今有效。"""

    start: date
    end: date | None = None

    def covers(self, day: date) -> bool:
        return self.start <= day and (self.end is None or day < self.end)

    def overlaps(self, other: "EffectiveRange") -> bool:
        if self.end is not None and self.end <= other.start:
            return False
        if other.end is not None and other.end <= self.start:
            return False
        return True


@dataclass(frozen=True)
class MetricProposal:
    """完整指标口径提案。"""

    # --- 既有信封字段（标识与时间含义不变） ---
    record_id: str
    occurred_at: datetime
    revision: int
    source: str
    schema_version: int = 1
    domain: str = "metric_council"

    # --- 口径内容（metric_proposal.json 新增结构） ---
    metric_code: str = ""
    """同一指标跨修订共享的稳定代码。"""

    metric_name: str = ""
    definition: str = ""
    unit_code: str = ""
    industries: frozenset[str] = frozenset()
    """适用行业代码集合，如 ``{"digital_exhibition", "cultural_tourism"}``。"""

    sources: tuple[DataSourceRef, ...] = ()
    dedup: DedupBoundary | None = None
    effective: EffectiveRange | None = None
    proposer: str = ""

    status: ProposalStatus = ProposalStatus.DRAFT

    def validate(self) -> None:
        """提交前自检；缺项或语义矛盾时抛出 :class:`ValidationError`。"""

        if not self.metric_code:
            raise ValidationError("提案缺少 metric_code")
        if not self.metric_name or not self.definition:
            raise ValidationError("提案缺少指标名称或定义")
        if self.revision < 1:
            raise ValidationError("revision 必须从 1 开始")
        if not units.is_registered(self.unit_code):
            raise ValidationError(f"未登记的计量单位：{self.unit_code}")
        if not self.industries:
            raise ValidationError("提案至少声明一个适用行业")
        if not self.sources:
            raise ValidationError("提案至少声明一个数据来源")
        if self.dedup is None:
            raise ValidationError("提案必须声明去重边界")
        if not self.dedup.subject or not self.dedup.keys or not self.dedup.window:
            raise ValidationError("去重边界的主体、键、窗口均不能为空")
        if self.effective is None:
            raise ValidationError("提案必须声明生效区间")
        if self.effective.end is not None and self.effective.end <= self.effective.start:
            raise ValidationError("生效区间结束日必须晚于开始日")
        for src in self.sources:
            if not src.source_id or not src.name:
                raise ValidationError("数据来源的 id 与名称不能为空")

    def with_status(self, status: ProposalStatus) -> "MetricProposal":
        allowed = _TRANSITIONS[self.status]
        if status not in allowed:
            raise ValidationError(
                f"非法状态迁移：{self.status.value} → {status.value}；"
                f"允许：{sorted(s.value for s in allowed)}"
            )
        return replace(self, status=status)

    def to_payload(self) -> dict:
        """序列化为 ``metric_proposal.json`` 结构。"""

        assert self.effective is not None and self.dedup is not None
        return {
            "schema_version": self.schema_version,
            "record_id": self.record_id,
            "domain": self.domain,
            "occurred_at": self.occurred_at.isoformat(),
            "revision": self.revision,
            "source": self.source,
            "metric_code": self.metric_code,
            "metric_name": self.metric_name,
            "definition": self.definition,
            "unit": self.unit_code,
            "industries": sorted(self.industries),
            "data_sources": [
                {
                    "source_id": s.source_id,
                    "name": s.name,
                    "granularity": s.granularity,
                    "contains_enterprise_detail": s.contains_enterprise_detail,
                }
                for s in self.sources
            ],
            "dedup_boundary": {
                "subject": self.dedup.subject,
                "keys": list(self.dedup.keys),
                "window": self.dedup.window,
                "note": self.dedup.note,
            },
            "effective_range": {
                "start": self.effective.start.isoformat(),
                "end": self.effective.end.isoformat() if self.effective.end else None,
            },
            "proposer": self.proposer,
            "status": self.status.value,
        }


def proposal_from_payload(payload: dict) -> MetricProposal:
    """从 ``metric_proposal.json`` 反序列化完整提案。"""

    missing = [key for key in ENVELOPE_FIELDS if key not in payload]
    if missing:
        raise ValidationError(f"提案缺少信封字段：{missing}")

    effective_in = payload.get("effective_range") or {}
    dedup_in = payload.get("dedup_boundary") or {}
    sources_in = payload.get("data_sources") or []

    proposal = MetricProposal(
        schema_version=payload["schema_version"],
        record_id=payload["record_id"],
        domain=payload["domain"],
        occurred_at=datetime.fromisoformat(payload["occurred_at"]),
        revision=payload["revision"],
        source=payload["source"],
        metric_code=payload.get("metric_code", ""),
        metric_name=payload.get("metric_name", payload.get("name", "")),
        definition=payload.get("definition", ""),
        unit_code=payload.get("unit", payload.get("unit_code", "")),
        industries=frozenset(payload.get("industries", ())),
        sources=tuple(
            DataSourceRef(
                source_id=s["source_id"],
                name=s["name"],
                granularity=s["granularity"],
                contains_enterprise_detail=s.get("contains_enterprise_detail", False),
            )
            for s in sources_in
        ),
        dedup=DedupBoundary(
            subject=dedup_in.get("subject", ""),
            keys=tuple(dedup_in.get("keys", ())),
            window=dedup_in.get("window", ""),
            note=dedup_in.get("note", ""),
        )
        if dedup_in
        else None,
        effective=EffectiveRange(
            start=date.fromisoformat(effective_in["start"]),
            end=date.fromisoformat(effective_in["end"])
            if effective_in.get("end")
            else None,
        )
        if effective_in
        else None,
        proposer=payload.get("proposer", ""),
        status=ProposalStatus(payload.get("status", "draft")),
    )
    proposal.validate()
    return proposal


def load_proposal(path: Path) -> MetricProposal:
    return proposal_from_payload(
        json.loads(path.read_text(encoding="utf-8"))
    )


# ---------------------------------------------------------------------------
# 兼容性判定
# ---------------------------------------------------------------------------

def same_unit_family(a: MetricProposal, b: MetricProposal) -> bool:
    return units.get(a.unit_code).family == units.get(b.unit_code).family


def same_dedup_boundary(a: MetricProposal, b: MetricProposal) -> bool:
    assert a.dedup is not None and b.dedup is not None
    return a.dedup == b.dedup


def compatibility_report(
    a: MetricProposal, b: MetricProposal
) -> tuple[bool, tuple[str, ...]]:
    """返回 ``(是否兼容, 不兼容原因列表)``。兼容判定只看口径本身。

    兼容要求：同单位族、同去重边界、行业口径不互相冲突。生效区间不同
    不构成冲突（那是同一指标的版本分段）。
    """

    reasons: list[str] = []
    if not same_unit_family(a, b):
        reasons.append(
            f"单位族不同：{units.get(a.unit_code).label} ≠ "
            f"{units.get(b.unit_code).label}"
        )
    if not same_dedup_boundary(a, b):
        reasons.append(
            f"去重边界不同：{a.dedup.subject}/{a.dedup.window} ≠ "
            f"{b.dedup.subject}/{b.dedup.window}"
        )
    if a.industries.isdisjoint(b.industries) and a.metric_code != b.metric_code:
        # 行业完全不相交且不是同一指标修订：不能当作同一口径派生
        reasons.append("适用行业不相交，且并非同一指标的版本修订")
    return (not reasons, tuple(reasons))


def are_compatible(a: MetricProposal, b: MetricProposal) -> bool:
    return compatibility_report(a, b)[0]
