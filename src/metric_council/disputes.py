"""公开分歧登记：冲突口径只能被并列展示，不能被静默合并。

当两个定义不兼容（或审议中委员会立场分裂）时，在此登记一条
:class:`DisputeRecord`。它记录分歧双方、冲突原因和各委员会立场；
此后任何报告或派生都只能 *并排引用* 两种口径，不允许产出单一
合并值。分歧的解决方式是其中一方提交新修订并走完审议，登记
随之关闭——关闭过程同样留痕。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .errors import WorkflowError
from .proposals import MetricProposal, compatibility_report
from .review import Committee, ReviewCase, Vote


@dataclass(frozen=True)
class DisputeEntry:
    """分歧中一方的口径快照。"""

    proposal_id: str
    metric_code: str
    revision: int
    unit_code: str
    definition: str


@dataclass(frozen=True)
class Resolution:
    """分歧的解决留痕。"""

    resolved_at: datetime
    winning_proposal_id: str
    note: str


@dataclass
class DisputeRecord:
    dispute_id: str
    topic: str
    opened_at: datetime
    sides: tuple[DisputeEntry, ...]
    reasons: tuple[str, ...]
    committee_positions: dict[Committee, Vote]
    resolution: Resolution | None = None

    @property
    def open(self) -> bool:
        return self.resolution is None

    def entry_for(self, proposal_id: str) -> DisputeEntry:
        for side in self.sides:
            if side.proposal_id == proposal_id:
                return side
        raise KeyError(proposal_id)


def _to_entry(proposal: MetricProposal) -> DisputeEntry:
    return DisputeEntry(
        proposal_id=proposal.record_id,
        metric_code=proposal.metric_code,
        revision=proposal.revision,
        unit_code=proposal.unit_code,
        definition=proposal.definition,
    )


class DisputeRegistry:
    def __init__(self) -> None:
        self._records: dict[str, DisputeRecord] = {}

    def open_from_conflict(
        self,
        dispute_id: str,
        topic: str,
        left: MetricProposal,
        right: MetricProposal,
        *,
        review_case: ReviewCase | None = None,
        now: datetime | None = None,
    ) -> DisputeRecord:
        """因两个定义冲突而登记公开分歧。"""

        ok, reasons = compatibility_report(left, right)
        if ok:
            raise WorkflowError(
                "两个定义相互兼容，不应登记为冲突分歧；"
                "兼容定义应通过派生指标处理"
            )
        return self._open(
            dispute_id,
            topic,
            (left, right),
            reasons,
            review_case,
            now,
        )

    def open_from_split(
        self,
        dispute_id: str,
        topic: str,
        proposals: tuple[MetricProposal, ...],
        case: ReviewCase,
        *,
        now: datetime | None = None,
    ) -> DisputeRecord:
        """因委员会立场分裂（DISPUTED 状态）登记公开分歧。"""

        if len(proposals) < 2:
            raise WorkflowError("立场分裂至少涉及两个口径版本")
        reasons = tuple(
            f"{c.label}委员会立场：{case.committee_votes(c).count(Vote.APPROVE)}"
            f" 赞成 / {case.committee_votes(c).count(Vote.REJECT)} 反对"
            for c in Committee
        )
        return self._open(dispute_id, topic, proposals, reasons, case, now)

    def _open(
        self,
        dispute_id: str,
        topic: str,
        proposals: tuple[MetricProposal, ...],
        reasons: tuple[str, ...],
        review_case: ReviewCase | None,
        now: datetime | None,
    ) -> DisputeRecord:
        if dispute_id in self._records:
            raise WorkflowError(f"分歧编号已存在：{dispute_id}")
        positions: dict[Committee, Vote] = {}
        if review_case is not None and review_case.closed:
            for committee in Committee:
                votes = review_case.committee_votes(committee)
                approves = votes.count(Vote.APPROVE)
                rejects = votes.count(Vote.REJECT)
                positions[committee] = (
                    Vote.APPROVE
                    if approves > rejects
                    else Vote.REJECT
                    if rejects > approves
                    else Vote.ABSTAIN
                )
        record = DisputeRecord(
            dispute_id=dispute_id,
            topic=topic,
            opened_at=now or datetime.now().astimezone(),
            sides=tuple(_to_entry(p) for p in proposals),
            reasons=reasons,
            committee_positions=positions,
        )
        self._records[dispute_id] = record
        return record

    def resolve(
        self,
        dispute_id: str,
        winning_proposal_id: str,
        note: str,
        *,
        now: datetime | None = None,
    ) -> DisputeRecord:
        record = self._records[dispute_id]
        if not record.open:
            raise WorkflowError(f"分歧 {dispute_id} 已关闭")
        record.entry_for(winning_proposal_id)  # 获胜方必须是分歧当事方
        record.resolution = Resolution(
            resolved_at=now or datetime.now().astimezone(),
            winning_proposal_id=winning_proposal_id,
            note=note,
        )
        return record

    def get(self, dispute_id: str) -> DisputeRecord:
        return self._records[dispute_id]

    def open_disputes(self) -> tuple[DisputeRecord, ...]:
        return tuple(r for r in self._records.values() if r.open)

    def all(self) -> tuple[DisputeRecord, ...]:
        return tuple(self._records.values())
